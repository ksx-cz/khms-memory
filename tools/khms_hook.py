#!/usr/bin/env python3
"""KHMS harness hook — the RETRIEVAL FLOOR.

One executable, wired to a handful of harness events (see claude-code/README.md):

  SessionStart        report an unreviewed inbox, and put the reply directive into
                      the session ONCE
  UserPromptSubmit    score the operator's message, inject matching cards, open a
                      gated turn for the retrieval-claim gate
  PreToolUse:Bash     run precheck.sh automatically for a named risky-command class
  PreToolUse:Edit|Write   score the file path and the edited text
  PreToolUse:<report tool>  the retrieval-claim gate: DENY a report whose `base:`
                      line cites cards that no recall of this turn backs
  PostToolUse:Bash / PostToolUseFailure   score the command and its error lines.
                      MEASURED AND SWITCHED OFF in the reference deployment — see
                      docs/measuring-injection.md before wiring them.

Why a floor at all: a rule that says "consult the base first" fails by never
firing, not by being wrong, and the moments it fails in are exactly the moments
the actor is too deep in a problem to remember it exists. So the cheap part is
automated, on a budget. It does NOT discharge the duty to search explicitly
before a hypothesis or a proposal — the hook cannot fire on a sentence that has
not been said yet.

Design rules this file obeys:
  * FAIL OPEN. Any exception is logged and swallowed; a broken memory system must
    never break the session it is trying to help.
  * EVERY DECISION IS LOGGED, including the silent ones, with the reason a card
    above the bar was not injected (dedup / cap / threshold). A hook that stops
    firing otherwise looks exactly like a quiet day.
  * BOUNDED. Thresholds, at most two cards, a character cap, a rate cap per
    window, a per-card dedup TTL — all of them §7 configuration, listed together
    at the top and meant to be recalibrated against your own .inject.log.
  * THE BUDGET IS SPENT BEFORE THE BASE IS LOADED. Every gate that needs no
    search result — the kill switch, the domain filter, the query cooldown, the
    rate cap, the per-event switch — runs before load_cards(). Measured in the
    reference deployment: 2324 hook calls in one day paid ~172 ms each for a
    card base they then threw away on a gate, and each of them also wrote a
    "nothing found" line into the retrieval log, which is how 87 % of that log
    came to be a record of non-retrievals.
  * WHAT IT INJECTS IS AN EXPERIMENT, NOT A CONSTANT. tools/khms_experiment.json
    switches injection off per event so the value of each one can be measured
    rather than assumed; deleting the file rolls everything back.

Kill switch: touch $KHMS_ROOT/tools/.hooks-off   (or export KHMS_HOOKS_OFF=1)
"""
import datetime
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P      # noqa: E402
import khms_search as ks    # noqa: E402

# --------------------------------------------------------------- parameters
# Spec §7: calibrate these against your own logs; do not treat them as rules.
THRESHOLD = {                  # minimum score for the top card, per event
    "UserPromptSubmit": 18.0,
    "PostToolUse": 16.0,
    "PostToolUseFailure": 15.0,
    "PreToolUse": 16.0,
}
SECOND_CARD_MIN = 14.0         # a second card rides along only above this
MAX_PRIMARY = 2                # cards per injection, before correctors/umbrella
CHAR_CAP = 900                 # hard cap on injected characters
RATE_MAX = 3                   # injections per RATE_WINDOW …
RATE_WINDOW = 600.0            # … seconds
BYPASS_MIN = 18.0              # a top hit this good buys ONE extra ride per window
BYPASS_PER_WINDOW = 1
QUERY_COOLDOWN = 900.0         # do not re-answer the same query text for 15 min
DEDUP_TTL = 12 * 3600.0        # a card may be re-injected after this long
DAMP_ALPHA = 0.5               # length damping exponent (0 disables)
DAMP_PIVOT = 85                # ≈ median distinct-token count of a card body
MIN_PROMPT_LEN = 12
# The dense channel, if you have one (see khms_recall_hybrid.py for the daemon
# protocol and the fall-back). Consulted only on operator messages: the agent's own
# tool traffic is the cheap side of the budget, and buying it extra searches spends
# the budget where it is worth least. With no daemon these are inert.
DENSE_EVENTS = frozenset({"UserPromptSubmit"})
DENSE_TIMEOUT_MS = float(os.environ.get("KHMS_DENSE_TIMEOUT_MS", "150"))
DENSE_MIN_COS = 0.55           # calibrate: 0.50 doubles the rescues and visibly
DENSE_RESCUE_N = 5             #   lowers precision
RESCUE_MIN_FIRST_LEN = 40      # a card whose first line is a bare field says nothing
DENSE_RESERVED_SLOT = False    # see the block in main(); OFF by default
PRECHECK_COOLDOWN = 1800.0     # per session, per tag set
PRECHECK_CAP = 800             # chars
PRECHECK_TIMEOUT = 8           # seconds
LOG_Q = 200                    # chars of the query kept in the audit log

# Length damping (why it exists): the score is a SUM of per-token idf, so it grows
# with the number of distinct tokens a card happens to contain, and long grab-bag
# cards win queries they have nothing to do with. Pivoted sqrt normalisation, with
# the pivot set to the base's own median, leaves a typical card untouched.

# Which tool events are worth scoring at all. Text that matches neither of these
# is not searched, so the hook stays quiet on routine `ls` and `git status`.
ERROR_RE = re.compile(
    r"error|fail|traceback|exception|denied|timeout|refused|no such|not found"
    r"|segfault|abort|cannot|unable|invalid|dead|inactive", re.I)
# DOMAIN_RE is the one thing you MUST edit for your own work: it is the list of
# words that mean "this is about the domain the base knows something about".
# Too broad and every keystroke searches; too narrow and the floor never fires.
DOMAIN_RE = re.compile(
    r"deploy|config|service|daemon|sensor|driver|firmware|schema|migration"
    r"|pipeline|calibrat|threshold|timeout|latency|checksum", re.I)

# Risky-command class: these run precheck.sh by themselves, because "run precheck
# before dangerous things" is a duty that fails exactly when it is needed. Tags
# must be tags your base actually uses.
RISKY = [
    (re.compile(r"rsync\b[^\n|;]*--delete"), ("deployment",)),
    (re.compile(r"\bgit\s+(push\s+(-f|--force)|tag\s+(-f|--force))"), ("git",)),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"), ("filesystem",)),
    (re.compile(r"\bsystemctl\b|\bservice\s+\w+\s+(stop|restart)"), ("service", "deployment")),
    (re.compile(r"\b(docker|podman)\s+(system\s+prune|rm|rmi)\b"), ("containers",)),
    (re.compile(r"\bkubectl\s+delete\b"), ("containers", "deployment")),
    (re.compile(r"\bterraform\s+(apply|destroy)\b"), ("deployment",)),
    (re.compile(r"\bdrop\s+(table|database)\b", re.I), ("database",)),
    (re.compile(r"\bdd\s+if="), ("filesystem",)),
    (re.compile(r"(^|[\s;&|'\"])[\w./-]*deploy/"), ("deployment",)),
]

# ------------------------------------------------------------- the directive
# The three mandatory reply lines. This was a 1813-character constant appended to
# EVERY operator prompt until it was measured: 46 firings on one audited day, the
# same paragraph re-sent each time, restating a rule the agent's own instructions
# already carried. The text now lives in ONE file, enters the session ONCE at
# SessionStart, and every prompt carries the pointer line below instead.
DIRECTIVE_LINE = (
    "REQUIRED: base: <what I searched for -> the cards, or 'did not search - why'>"
    " \u00b7 verified: <the command or file:line whose output I read | unverified>"
    " \u00b7 if I were wrong: <how that output would have looked different>"
    "  (definition: tools/prompts/report_directive.md)"
)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def directive_text():
    """The full directive, from its single source. A missing or unreadable file
    degrades to the one-liner: the session start must never fail on it."""
    try:
        with open(P.DIRECTIVE_FILE, encoding="utf-8") as f:
            body = COMMENT_RE.sub("", f.read())
    except OSError:
        return DIRECTIVE_LINE
    return body.strip() or DIRECTIVE_LINE


# --------------------------------------------------------------- experiment
# tools/khms_experiment.json — the per-event injection switch and the flag of the
# retrieval-claim gate. FAIL-OPEN IN BOTH DIRECTIONS: a missing, empty or broken
# file means "behave exactly as before the experiment", so deleting the file is a
# complete rollback without touching a line of code. See docs/measuring-injection.md
# for how to run one and what to compare afterwards; an example config ships as
# tools/khms_experiment.example.json.
INJECT_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse",
                 "PostToolUseFailure")
SHORT_EVENT = {"UserPromptSubmit": "ups", "PreToolUse": "pre",
               "PostToolUse": "ptu", "PostToolUseFailure": "ptuf"}
EXP_TAG = ""                   # set once per run in main(); stamped into the log


def load_experiment():
    try:
        with open(P.EXPERIMENT, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def inject_on(exp, event):
    return bool((exp.get("inject") or {}).get(event, True))


def exp_tag(exp):
    """What goes into the audit log, so a day's records say which regime produced
    them. Without it a before/after comparison has to be reconstructed from file
    mtimes, which is not evidence."""
    if not exp:
        return ""
    name = exp.get("name")
    if not name:
        off = [SHORT_EVENT[e] for e in INJECT_EVENTS if not inject_on(exp, e)]
        name = ("no-" + "+".join(off)) if off else "on"
    return f"exp:{name}"


# ------------------------------------------------- the retrieval-claim gate
# The `base:` line of the directive above is a CHECK, and until this gate existed
# it was a check whose passing condition held whether or not a retrieval had
# happened: the hook's own injection could satisfy it. That is the shape of the
# failure the base has a card about, applied to the compliance line itself.
#
# So: a report to the principal that CITES CARD IDS in its `base:` line, while no
# explicit recall of this turn matches the query it claims to have run, is DENIED.
# The gate is deliberately narrow. It denies exactly two things:
#   (i)  a substantial report with no `base:` line at all;
#   (ii) a `base:` line that cites card ids with no matching recall in this turn.
# A line that cites no card claims nothing about the base and is allowed; so is
# every honest form ("did not search", "nothing on record"). The thing being
# checked is a CITATION, not the wording of the line.
#
# REPORT_TOOL_RE is the tool through which the agent reports to its principal —
# the one action it cannot skip, because the principal only ever learns anything
# through it. Set KHMS_REPORT_TOOL to your own (a chat tool, a mail tool, a ticket
# tool); an empty value disables the gate entirely.
REPORT_TOOL_RE = re.compile(
    os.environ.get("KHMS_REPORT_TOOL") or r"(?i)discord.*reply|reply.*discord")
# When set, ONLY prompts matching this pattern open a gated turn (in the reference
# deployment: the ones that arrived through the reporting channel, since replies in
# the terminal are not reports). Unset, every operator prompt opens one.
REPORT_CHANNEL_RE = (re.compile(os.environ["KHMS_REPORT_CHANNEL"])
                     if os.environ.get("KHMS_REPORT_CHANNEL") else None)
CLAIM_MIN_CHARS = 200          # below this a report need not carry the line
CLAIM_SHARE = 0.5              # token overlap with a recall of THIS turn
CLAIM_SLACK = 3.0              # s; the recall log stores whole seconds
CLAIM_TAIL = 262144            # bytes of the recall log read backwards
# The honest forms. A `base:` line that says it did not search is ALWAYS allowed:
# the gate exists to stop unbacked CITATIONS, and "nothing on record" is a valid
# and useful answer.
CLAIM_HONEST = ("did not search", "didn't search", "no search", "from memory",
                "nothing on record", "nothing found", "found nothing",
                "nothing above the bar", "base is silent")

CLAIM_DENY_CITE = (
    "Your `base:` line cites cards but no explicit recall ran in this turn - run "
    "`tools/recall.sh '<query>'` and send again, or write `base: did not search - "
    "<why>`. (reason: %s; the check reads src=cli lines of tools/.recall.log since "
    "this turn began)")
CLAIM_DENY_MISSING = (
    "This report to your principal is missing the mandatory `base:` line (%s). "
    "Write `base: <query from tools/recall.sh> -> <cards>`, or honestly `base: did "
    "not search - <why>`; both are valid, silence is not. "
    "Definition: tools/prompts/report_directive.md")


def _report_text(payload):
    ti = payload.get("tool_input") or {}
    for k in ("text", "content", "message", "body"):
        v = ti.get(k)
        if isinstance(v, str) and v:
            return v
    return " ".join(str(v) for v in ti.values() if isinstance(v, str))


def _claim_line(text):
    """The `base:` line, raw and normalised, tolerant of bullets and bold."""
    for ln in (text or "").splitlines():
        n = ks.norm(ln).strip().lstrip("-*_>+ \t")
        if n.startswith("base:") or n.startswith("base :"):
            return ln.strip(), n
    return None, None


def _claim_query_tokens(nline):
    """Tokens of the QUERY part: after `base:`, before the arrow, card ids out."""
    q = nline.split(":", 1)[1] if ":" in nline else nline
    q = re.split(r"\u2192|->|=>", q)[0]
    q = re.sub(r"k-\d+", " ", q)
    return set(ks.tokens(q))


def recall_cli_since(since_t):
    """Token sets of the src=cli lines of the recall log at or after since_t.

    Reads the TAIL only: the log grows without bound and a hook may not read a
    whole log on a tool call. ONLY src=cli counts - the hook's own lines are what
    made the claim fakeable in the first place."""
    try:
        with open(ks.recall_log_path(), "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - CLAIM_TAIL))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > CLAIM_TAIL and lines:
        lines = lines[1:]                      # the seek cut one line in half
    out = []
    for ln in lines:
        parts = ln.split(" | ")
        if len(parts) < 5 or not parts[-1].startswith("src=cli"):
            continue
        try:
            ts = datetime.datetime.fromisoformat(parts[0]).timestamp()
        except ValueError:
            continue
        if ts + CLAIM_SLACK < since_t:
            continue
        out.append(set(ks.tokens(" | ".join(parts[1:-3]))))
    return out


def claim_verdict(text, st):
    """(ALLOW|DENY|EXEMPT, reason, query-part). Never raises."""
    turn_start = st.get("turn_start")
    pending = bool(st.get("claim_pending")) and turn_start is not None
    line, nline = _claim_line(text)
    if line is None:
        if pending and len(text or "") >= CLAIM_MIN_CHARS:
            return "DENY", "no base: line (%d chars)" % len(text), ""
        return ("EXEMPT",
                "short report" if pending else "no gated turn open", "")
    qpart = nline[:120]
    for h in CLAIM_HONEST:
        if h in nline:
            return "ALLOW", "honest form (%s)" % h, qpart
    if not re.search(r"k-\d{4,6}", nline):
        return "ALLOW", "line cites no card", qpart
    if turn_start is None:
        return "EXEMPT", "no gated turn in this session", qpart
    recalls = recall_cli_since(turn_start)
    if not recalls:
        return "DENY", "no src=cli recall in this turn", qpart
    qtok = _claim_query_tokens(nline)
    if not qtok:
        return "ALLOW", "no query given, but a recall did run in this turn", qpart
    best = max(len(qtok & r) / float(len(qtok)) for r in recalls)
    if best >= CLAIM_SHARE:
        return "ALLOW", "%.0f%% overlap with a recall in this turn" % (100 * best), qpart
    return "DENY", "best overlap with a recall in this turn only %.0f%%" % (100 * best), qpart


def claim_log(session, verdict, why, qpart):
    try:
        with open(P.CLAIM_LOG, "a", encoding="utf-8") as f:
            f.write("%s | %s | %s | %s | %s\n" % (
                datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                session or "-", verdict, why, (qpart or "")[:120]))
    except OSError:
        pass


def run_claim_gate(payload, event, exp):
    """True == the report was DENIED (and the denial has been printed)."""
    if not exp.get("claim_gate", True):
        return False
    session = payload.get("session_id", "")
    path, st = load_state(session)
    verdict, why, qpart = claim_verdict(_report_text(payload), st)
    claim_log(session, verdict, why, qpart)
    if verdict == "DENY":
        reason = (CLAIM_DENY_MISSING % why) if why.startswith("no base:") \
            else (CLAIM_DENY_CITE % why)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
        log(event, "report-tool", "DENY (claim: %s)" % why)
        return True
    if verdict == "ALLOW" and st.get("claim_pending"):
        st["claim_pending"] = False
        save_state(path, st)
    return False


def now():
    return time.time()


def log(event, query, action, results=None, skips=None, qh=""):
    results = results or []
    top = f"{results[0][2]['id']}:{results[0][0]:.1f}" if results else "-"
    if skips:
        action += " skipped=" + ",".join(f"{k}:{w}" for k, w in skips[:3])
    q = re.sub(r"\s+", " ", query or "")[:LOG_Q]
    try:
        with open(P.INJECT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}"
                    f" | {event} | q={q} | nhits={len(results)} | top={top} | {action}"
                    f"{(' qh=' + qh) if qh else ''}"
                    f"{(' ' + EXP_TAG) if EXP_TAG else ''}\n")
    except OSError:
        pass


def load_state(session_id):
    os.makedirs(P.HOOK_STATE, exist_ok=True)
    path = os.path.join(
        P.HOOK_STATE,
        re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "nosession") + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    for k, v in (("injected", {}), ("recent", []), ("queries", {}),
                 ("bypass", []), ("precheck", {})):
        st.setdefault(k, v)
    return path, st


def save_state(path, st):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass


def emit(event, text):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": text}}))


# ------------------------------------------------------------------ scoring
def prepare_cards(cards):
    """Attach the distinct-token count used for length damping, memoised per
    (card id, body length). Cards are immutable once approved and a body that
    does change changes its length, so the key is sound; a corrupt cache only
    costs the recomputation."""
    cache = {}
    try:
        with open(P.LEN_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    dirty = False
    for c in cards:
        key = f"{c['id']}:{len(c['nbody'])}"
        n = cache.get(key)
        if n is None:
            n = max(1, len(set(ks.tokens(c["nbody"]))))
            cache[key] = n
            dirty = True
        c["_ntok"] = n
    if dirty:
        try:
            tmp = P.LEN_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, P.LEN_CACHE)
        except OSError:
            pass
    return cards


def damp(score, ntok):
    if DAMP_ALPHA <= 0:
        return score
    return score * min(1.0, math.pow(DAMP_PIVOT / float(ntok), DAMP_ALPHA))


MAX_WARN = 2                   # correction pointers per served card
WARN_CHARS = 110               # of the corrector's first line


def warn_line(cor):
    """The reverse edge, one line: who corrects the card just served."""
    return f"  ! CORRECTED BY {cor['id']}: {cor['first'][:WARN_CHARS]}"


def card_line(c, score=None, prefix="- "):
    flags = []
    if c["status"] in ("refuted", "challenged", "superseded"):
        flags.append("!" + c["status"].upper())
    if "gotcha" in c["tags"]:
        flags.append("gotcha")
    flag = ("[" + ",".join(flags) + "] ") if flags else ""
    sc = f" ({score:.0f})" if score is not None else ""
    return f"{prefix}{c['id']}{sc} {flag}{c['first'][:100]}"


def correctors_of(c, by_id, rev):
    """Cards that correct c: the reverse edges (someone's contradicts/supersedes
    points AT c) plus c's own forward refuted_by. Never raises — a base with one
    malformed link must degrade to "no correctors", not cost the injection."""
    out, seen = [], set()
    try:
        for cor in ([by_id[k] for k in (c["links"].get("refuted_by") or []) if k in by_id]
                    + list(rev.get(c["id"], []))):
            if cor["id"] != c["id"] and cor["id"] not in seen:
                seen.add(cor["id"])
                out.append(cor)
    except Exception:
        return []
    return out


def build_injection(cands, cards, injected, t, skips):
    """At most MAX_PRIMARY fresh cards, each optionally preceded by the principle
    it supports and followed by whatever corrects it. A refuted card is never
    served alone: the correction is the useful half.

    Every served card carries its correctors, WHATEVER ITS STATUS. This used to
    happen only for cards already marked challenged/refuted/superseded — but a
    correction arrives before anyone re-statuses the old card, and `active` is
    exactly the state in which a stale card is believed. In the source deployment
    that gap cost four days: a card corrected in August kept its `active` July
    predecessors, and they were served to the operator as the current state of
    the machine, correctly, by the rules as they then stood."""
    by_id = {c["id"]: c for c in cards}
    rev = ks.reverse_correctors(cards)
    lines, used, primaries = [], [], 0
    warn_chars = 0

    def fresh(cid):
        ts = injected.get(cid)
        return ts is None or (t - ts) >= DEDUP_TTL

    for score, _matched, c in cands:
        if primaries >= MAX_PRIMARY:
            skips.append((c["id"], "slots-full"))
            continue
        if not fresh(c["id"]):
            skips.append((c["id"], "dedup"))
            continue
        if c["id"] in used:
            continue
        umbrella = None
        for pid in c["links"]["supports"]:
            u = by_id.get(pid)
            if u and u["level"] == "derived" and u["status"] == "active":
                umbrella = u
                break
        if umbrella and umbrella["id"] not in used and fresh(umbrella["id"]):
            lines.append(card_line(umbrella, prefix="- principle: "))
            used.append(umbrella["id"])
        lines.append(card_line(c, score))
        used.append(c["id"])
        primaries += 1
        correctors = correctors_of(c, by_id, rev)
        if c["status"] in ("challenged", "refuted", "superseded"):
            # unchanged: the refuter is served as a full card line and marked
            # injected, because a refuted card without it is worse than nothing.
            for cor in correctors[:1]:
                if cor["id"] not in used:
                    lines.append(card_line(cor, prefix="  -> corrected by: "))
                    used.append(cor["id"])
                    correctors = [x for x in correctors if x["id"] != cor["id"]]
        # An ACTIVE card that something corrects gets a POINTER, not a card: it
        # consumes no primary slot and is not marked injected, so it can repeat
        # as often as its subject comes up and the card behind it can still be
        # served in full later.
        try:
            for cor in correctors[:MAX_WARN]:
                if cor["id"] in used:
                    continue
                line = warn_line(cor)
                lines.append(line)
                warn_chars += len(line) + 1
        except Exception:
            pass        # a pointer that cannot be built is dropped, never raised
    if not lines:
        return None, []
    text = ("KHMS (auto-recall — cards that may bear on what is happening):\n"
            + "\n".join(lines)
            + "\nIf one is relevant, open it whole (memory/know/<id>.md) and follow its links.")
    # BUDGET: the correction pointers are EXEMPT from CHAR_CAP — the cap on
    # everything else is unchanged (cap plus exactly the characters the pointers
    # added). They are not extra retrieval: they correct content that is being
    # injected anyway, and a truncated correction is the failure this exists to
    # prevent. Bounded by construction: at most MAX_WARN per card, WARN_CHARS
    # each, i.e. ~280 characters against a 900-character cap.
    return text[:CHAR_CAP + warn_chars], used


# ------------------------------------------------------------------- events
def handle_session_start():
    """The morning review is the first duty of the day, so an unreviewed inbox is
    the one thing worth saying before any work starts."""
    unreviewed = []
    try:
        for f in sorted(os.listdir(P.INBOX)):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})(-weekly)?\.md$", f)
            stem = f[:-3]
            # A review is recorded per unit by approve_inbox.py (`.reviewed-<unit>-cards`); the plain
            # `.reviewed-<date>` marker is still honoured.
            if m and not any(os.path.exists(os.path.join(P.REVIEW_MARKERS, n))
                             for n in (f".reviewed-{m.group(1)}", f".reviewed-{stem}-cards")):
                unreviewed.append(f)
    except OSError:
        unreviewed = []      # no inbox yet; the directive below still goes in
    parts = []
    if unreviewed:
        parts.append(
            "KHMS: UNREVIEWED INBOX: " + ", ".join(unreviewed)
            + " — review it (approve_inbox.py, build_views.py, report to the operator) "
              "BEFORE other work. Mark it done with: touch the review marker .reviewed-<date> in the shared state")
        log("SessionStart", "inbox-check", f"inject inbox={','.join(unreviewed)}")
    else:
        log("SessionStart", "inbox-check", "silent (inbox clean)")

    # The full directive enters the session HERE, once, instead of being appended
    # to every one of the day's operator prompts.
    parts.append(directive_text())
    emit("SessionStart", "\n\n".join(parts))


def risky_tags(cmd):
    out = []
    for rx, tags in RISKY:
        if rx.search(cmd or ""):
            for t in tags:
                if t not in out:
                    out.append(t)
    return out


def run_precheck(tags):
    try:
        r = subprocess.run([os.path.join(P.TOOLS_DIR, "precheck.sh"), *tags],
                           capture_output=True, text=True, timeout=PRECHECK_TIMEOUT)
        out = (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or out.startswith("precheck: nothing on record"):
        return None
    return out[:PRECHECK_CAP]


def try_precheck(payload, st, t):
    """PreToolUse(Bash) only. The output counts against the injection budget but
    is not blocked by it: the whole point is the command that is about to delete
    something, and that command must not lose its warning to a busy window. The
    per-session, per-tag-set cooldown is what bounds this path instead."""
    cmd = str((payload.get("tool_input") or {}).get("command", ""))
    tags = risky_tags(cmd)
    if not tags:
        return None
    key = "+".join(tags)
    if t - st["precheck"].get(key, 0) < PRECHECK_COOLDOWN:
        return None
    st["recent"] = [x for x in st["recent"] if t - x < RATE_WINDOW]
    body = run_precheck(tags)
    st["precheck"][key] = t
    if not body:
        return None
    st["recent"].append(t)
    return ("KHMS precheck (risky command class: " + key + ") — active policies, "
            "gotchas and refuted dead ends:\n" + body
            + "\nIf any of it bears on this command, open the card before running it.")


def dense_hits(query):
    """[(card id, cosine)] from the dense channel, or [] — never raises, never
    waits. A missing module, a missing daemon or a slow one all mean the same
    thing: this call is lexical, exactly as it was before the channel existed."""
    try:
        import khms_recall_hybrid as hy
    except Exception:
        return []
    try:
        hits, err, _ms = hy.dense_query(query, topn=hy.CAND,
                                        timeout_ms=DENSE_TIMEOUT_MS)
        return [] if err else hits
    except Exception:
        return []


def extract_bash_text(payload):
    ti = payload.get("tool_input") or {}
    cmd = str(ti.get("command", ""))[:200]
    out = payload.get("tool_output")
    if isinstance(out, dict):
        out = json.dumps(out, ensure_ascii=False)
    out = str(out or "")
    err_lines = [ln for ln in out[-2500:].splitlines() if ERROR_RE.search(ln)][:3]
    text = cmd + " " + " ".join(err_lines)
    if not DOMAIN_RE.search(text) and not err_lines:
        return None
    return text[:400]


def main():
    if os.environ.get("KHMS_HOOKS_OFF") == "1" or os.path.exists(P.HOOKS_OFF):
        return
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name", "")
    global EXP_TAG
    exp = load_experiment()
    EXP_TAG = exp_tag(exp)

    # The report to the principal is gated BEFORE anything else costs anything:
    # it is a permission decision, not a retrieval.
    if (event == "PreToolUse"
            and REPORT_TOOL_RE.pattern
            and REPORT_TOOL_RE.search(str(payload.get("tool_name", "")))):
        try:
            if run_claim_gate(payload, event, exp):
                return
        except Exception as exc:                                   # noqa: BLE001
            # FAIL OPEN, loudly: a gate that throws must never stand between the
            # agent and its report.
            log(event, "report-tool", f"claim-gate error {exc}")
        return

    if event == "SessionStart":
        handle_session_start()
        return
    if event not in THRESHOLD:
        return

    tool = payload.get("tool_name")
    if event == "UserPromptSubmit":
        raw = str(payload.get("prompt", ""))
        # A gated TURN starts here — detected BEFORE the tag stripping below eats
        # the channel tag. Everything until the next such prompt is "this turn"
        # for the retrieval-claim gate.
        if REPORT_CHANNEL_RE is None or REPORT_CHANNEL_RE.search(raw):
            _p, _st = load_state(payload.get("session_id", ""))
            _st["turn_start"] = now()
            _st["claim_pending"] = True
            save_state(_p, _st)
        raw = re.sub(r"<[^>]{1,400}>", " ", raw)          # strip harness/channel tags
        if len(raw.strip()) < MIN_PROMPT_LEN:
            log(event, raw, "skip (short)")
            return
        query = raw[:500]
    elif event == "PreToolUse" and tool in ("Edit", "Write"):
        ti = payload.get("tool_input") or {}
        text = " ".join([str(ti.get("file_path", "")),
                         str(ti.get("old_string", ""))[:200],
                         str(ti.get("new_string", ""))[:200],
                         str(ti.get("content", ""))[:200]])
        if not DOMAIN_RE.search(text):
            return
        query = text[:400]
    else:
        query = extract_bash_text(payload)
        if query is None and not (event == "PreToolUse" and tool == "Bash"):
            return

    # An event whose card injection is switched off exits HERE — no state, no
    # card base, no search, no line in the recall log. The UserPromptSubmit
    # handler above still ran (the turn marker), and the prompt still gets the
    # one-line directive: the experiment removes the CARDS, not the duty to say
    # where an answer came from.
    if not inject_on(exp, event) and not (event == "PreToolUse"
                                          and tool == "Bash"):
        if event == "UserPromptSubmit":
            emit(event, DIRECTIVE_LINE)
        log(event, query, "skip (exp:no-inject)")
        return

    path, st = load_state(payload.get("session_id", ""))
    t = now()

    # The risky-command class checks itself, BEFORE retrieval, so a destructive
    # command never loses its warning to the ordinary card budget.
    if event == "PreToolUse" and tool == "Bash":
        pc = try_precheck(payload, st, t)
        if pc is not None:
            save_state(path, st)
            emit(event, pc)
            log(event, str((payload.get("tool_input") or {}).get("command", ""))[:200],
                "PRECHECK")
            return
        if query is None:
            save_state(path, st)
            return
        # PreToolUse(Bash) reaches here only after precheck: the automatic
        # precheck is NOT part of the injection experiment, the cards are.
        if not inject_on(exp, event):
            save_state(path, st)
            log(event, query, "skip (exp:no-inject)")
            return

    qh = hashlib.sha1(query.encode("utf-8", "replace")).hexdigest()[:12]
    thr = THRESHOLD[event]

    if t - st["queries"].get(qh, 0) < QUERY_COOLDOWN:
        save_state(path, st)
        log(event, query, "skip (query cooldown)", qh=qh)
        return
    st["queries"][qh] = t
    st["recent"] = [x for x in st["recent"] if t - x < RATE_WINDOW]
    st["bypass"] = [x for x in st["bypass"] if t - x < RATE_WINDOW]

    capped = len(st["recent"]) >= RATE_MAX
    bypass_allowed = (capped and event == "UserPromptSubmit"
                      and len(st["bypass"]) < BYPASS_PER_WINDOW)
    if capped and not bypass_allowed:
        save_state(path, st)
        log(event, query, "skip (rate cap)", qh=qh)
        return

    cards = prepare_cards(ks.load_cards())
    expanded, _added = ks.expand_query(query, ks.load_glossary())
    raw_results, _ = ks.search(expanded, cards=cards, exclude_meta=True, topn=8)
    results = sorted(((damp(s, c["_ntok"]), m, c) for s, m, c in raw_results),
                     key=lambda r: -r[0])
    ks.log_query(query, results, src=f"hook:{event}")

    top_score = results[0][0] if results else 0.0
    skips = []

    if capped:
        # The rate cap used to spend the window first-come, dropping the best
        # question of the hour because three routine ones arrived first. A top hit
        # that clears a high bar buys one extra ride per window; everything else is
        # still dropped, but now the score is in the log, so the miss is diagnosable.
        if top_score < BYPASS_MIN:
            save_state(path, st)
            if results:
                skips.append((results[0][2]["id"], f"cap top={top_score:.1f}"))
            if event == "UserPromptSubmit":
                emit(event, DIRECTIVE_LINE
                     + "  (no cards this turn: rate cap)")
            log(event, query, "skip (rate cap)", results, skips, qh)
            return
        st["bypass"].append(t)

    cands = [r for r in results[:4] if r[0] >= SECOND_CARD_MIN] if results else []
    if not results or top_score < thr:
        if results:
            skips.append((results[0][2]["id"], f"threshold {top_score:.1f}<{thr:.0f}"))
        cands = []

    # ---------------------------------------------------------- dense channel
    dense = dense_hits(query) if event in DENSE_EVENTS else []
    mode = "lexical"
    if dense:
        by_id = {c["id"]: c for c in cards}
        have = {c["id"] for _s, _m, c in cands}

        # RESCUE — only ADDS candidates, never reorders the lexical ones, so a
        # query the lexical channel already answers is untouched. This is the path
        # that catches a card the lexical channel did not reach AT ALL: the
        # cross-lingual miss the hybrid channel exists for.
        rescue = []
        for cid, cos in dense[:DENSE_RESCUE_N]:
            if cos < DENSE_MIN_COS or cid in have:
                continue
            c = by_id.get(cid)
            if c is None or c["status"] == "superseded":
                continue
            if len(c["first"]) < RESCUE_MIN_FIRST_LEN:
                continue
            rescue.append((thr, [], c))
        if rescue:
            cands = cands + rescue
            mode = "hybrid+rescue"

        # RESERVED SLOT — off by default, and it is a different policy from the
        # rescue: the rescue cannot help when the slots are already spoken for,
        # which is the case where a lexically-mediocre but semantically-right card
        # is logged `slots-full` and never served. This gives the dense channel's
        # best card THAT THE LEXICAL PICKS DO NOT ALREADY CONTAIN the last primary
        # slot. Scoped to calls that were going to inject anyway: with no lexical
        # candidate there is no contested slot, and "inject the nearest neighbour
        # of every prompt" is a much noisier policy than this one.
        # Measured caveat before you switch it on: on the very incident that
        # motivated it, the dense channel ALSO ranked the wanted card third, so
        # this lever did not fix it — raising MAX_PRIMARY did. Switch it on from
        # your own log, not from this paragraph.
        if DENSE_RESERVED_SLOT and cands:
            have = {c["id"] for _s, _m, c in cands}
            for cid, _cos in dense[:DENSE_RESCUE_N]:
                if cid in have:
                    continue
                c = by_id.get(cid)
                if c is None or c["status"] == "superseded":
                    continue
                if len(c["first"]) < RESCUE_MIN_FIRST_LEN:
                    continue
                keep = max(0, MAX_PRIMARY - 1)
                cands = cands[:keep] + [(thr, [], c)] + cands[keep:]
                mode = "hybrid+slot"
                break

    if not cands:
        save_state(path, st)
        if event == "UserPromptSubmit":
            emit(event, DIRECTIVE_LINE
                 + "  (no cards this turn: nothing above the bar)")
        log(event, query, "silent (below threshold)", results, skips, qh)
        return

    text, used = build_injection(cands, cards, st["injected"], t, skips)
    if not text:
        save_state(path, st)
        if event == "UserPromptSubmit":
            emit(event, DIRECTIVE_LINE
                 + "  (no cards this turn: the relevant ones already came up)")
        log(event, query, "silent (all deduped)", results, skips, qh)
        return

    for kid in used:
        st["injected"][kid] = t
    st["recent"].append(t)
    save_state(path, st)
    if event == "UserPromptSubmit":
        text += "\n" + DIRECTIVE_LINE
    emit(event, text)
    log(event, query, f"INJECT [{','.join(used)}] chars={len(text)}"
        + ("" if mode == "lexical" else f" mode={mode}"), results, skips, qh)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:                      # fail open, always
        try:
            with open(P.INJECT_LOG, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}"
                        f" | ERROR | {type(e).__name__}: {e}\n")
        except OSError:
            pass
        sys.exit(0)
