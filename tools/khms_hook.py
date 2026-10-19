#!/usr/bin/env python3
"""KHMS harness hook -- the retrieval floor and the reply gates of the memory system.

Usage: a Claude Code command hook; it reads the event JSON on stdin and prints at most one JSON answer.
Wire it (see claude-code/README.md) to:

  SessionStart                 the unreviewed-inbox / night-in-progress line, the mark-ledger line, the
                               gates' fail-open records, and the reply directive -- ONCE per session
  UserPromptSubmit             open a turn, score the prompt, inject matching cards, one directive line
  PreToolUse  Bash             record recall intents; run precheck.sh for a risky-command class; score
  PreToolUse  Edit|Write       score the file path and the edited text (only text matching HOOK_DOMAIN)
  PreToolUse  Edit|Write|Bash  the status-MARK gate: a write that adds a `MARK[<status kind>]` line to a
                               journal file of the base is refused -- a status line is a plain line (#236)
  PreToolUse  Agent|Task       the dispatch gate: a proposal inside a brief needs a bound recall
  PreToolUse  <report tool>    stamp the reply for the Stop gate (a report itself is never refused: the
                               claim, proposal and terminology gates on it were removed, #247)
  Stop                         the reply-owed gate: the principal's last channel message gets an answer
  PostToolUse / ...Failure     score the command and its error lines (switched off by the experiment file
                               in the reference deployment; measure before wiring them)

Project-specific context (boards, queues, cost shares) is NOT this file's business: a deployment adds its
own hook command beside this one; neither depends on the other's ordering or output.

Design rules:
  * FAIL OPEN. Any exception is logged and swallowed; a gate that cannot measure never refuses.
    A gate's own error is written as `ERROR->ALLOW` and shown at the next SessionStart / prompt,
    because a broken gate otherwise looks exactly like an idle one.
  * THE BUDGET IS SPENT BEFORE THE BASE IS LOADED (khms_select.pregate): kill switch, cooldowns,
    rate cap and the per-event experiment switch run before load_cards().
  * EVERY DECISION IS LOGGED, including the silent ones and the reason a card above the bar stayed out.
  * WORDING AND INPUT VOCABULARY ARE CONFIGURATION. Every message has an English default in
    khms_messages.py and can be overridden by the private KHMS_MESSAGES file; every language-specific
    matcher (proposal markers, stop words, domain words) is a key of the
    private KHMS_PATTERNS file. A public deployment works with neither.

Locations come from khms_paths. The legacy per-file overrides KHMS_STATE_DIR, KHMS_LOG, KHMS_BAZE_LOG,
KHMS_PROPOSAL_LOG, KHMS_STOP_LOG, KHMS_RECALL_INTENT and KHMS_NIGHTLY_LOCK are honoured from the
environment, so existing tests and wrappers keep working.
Kill switch: touch the file named by KHMS_HOOKS_OFF_FILE (default ${KHMS_STATE}/hooks-off) or export
KHMS_HOOKS_OFF=1.
"""
import collections
import datetime
import hashlib
import json
import os
import khms_files
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P          # noqa: E402
import khms_messages as M       # noqa: E402
import khms_patterns as PAT     # noqa: E402
import khms_search as ks        # noqa: E402
import khms_select as sel       # noqa: E402

# Resolve locations from THIS process's environment: a test that sets an override and reloads this module
# must not see a resolution cached by an earlier import.
P.reset()
CODE = P.CODE
# BRAIN is the data root; tests may point it at a temporary directory. TOOLS is the code directory
# (scripts such as precheck.sh live beside this file).
BRAIN = P.soft("ROOT")
TOOLS = CODE


def _loc(legacy_env, key, default=None):
    """A location: the legacy per-file environment override, else the resolved key, else `default`."""
    return os.environ.get(legacy_env) or P.soft(key, default)


STATE_DIR = _loc("KHMS_STATE_DIR", "HOOK_STATE")
LOG = _loc("KHMS_LOG", "INJECT_LOG")
HOOKS_OFF_FILE = P.soft("HOOKS_OFF")
# KHMS_PARAMS=live runs the LIVE selection policy: a runtime rollback that needs no file swap.
PARAMS = sel.LIVE if os.environ.get("KHMS_PARAMS") == "live" else sel.V2

MIN_PROMPT_LEN = 12
PRECHECK_COOLDOWN = 1800      # per session, per tag set
PRECHECK_CAP = 800            # chars
PRECHECK_TIMEOUT = 8          # s
LOG_Q = 200                   # chars of the query kept in the inject log

EXPERIMENT = P.soft("EXPERIMENT")
DIRECTIVE_FILE = P.soft("DIRECTIVE_FILE", os.path.join(CODE, "prompts", "report_directive.md"))
INJECT_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure")
SHORT_EVENT = {"UserPromptSubmit": "ups", "PreToolUse": "pre",
               "PostToolUse": "ptu", "PostToolUseFailure": "ptuf"}
EXP_TAG = ""                  # set once per run in main(); stamped into the log

# The tool through which the agent reports to its principal, and the channel whose prompts open a
# "report turn". Both are regular expressions; see khms_paths for the defaults.
REPORT_TOOL_RE = re.compile(P.soft("KHMS_REPORT_TOOL") or r"(?i)discord.*reply|reply.*discord")
_CHANNEL = P.soft("KHMS_REPORT_CHANNEL") or ""
REPORT_CHANNEL_RE = re.compile(_CHANNEL) if _CHANNEL else None
REPORT_SERVER = P.soft("KHMS_REPORT_SERVER") or "discord"

# --- the recall-binding window of the dispatch gate ------------------------------------------------------
CLAIM_SLACK = 3.0             # s; the recall log stores whole seconds
CLAIM_TAIL = 262144           # bytes of a log read backwards

# Which tool events are worth scoring. Text matching neither is not searched, so the hook stays quiet
# on routine commands. HOOK_DOMAIN is the deployment's own vocabulary; without it only error lines count.
DOMAIN_RE = PAT.regex("HOOK_DOMAIN", r"(?!x)x", flags=re.I)
ERROR_RE = re.compile(
    r"error|fail|traceback|exception|denied|timeout|refused|no such|not found"
    r"|segfault|abort|cannot|unable|invalid|dead|inactive", re.I)


def directive_line():
    return M.msg("HOOK_DIRECTIVE_LINE")


def directive_text():
    """The full directive from its single source. A missing or unreadable file degrades to the one-liner;
    it must never take the session start with it."""
    try:
        with open(DIRECTIVE_FILE, encoding="utf-8") as f:
            body = f.read()
    except (OSError, TypeError):
        return directive_line()
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    return body.strip() or directive_line()


def now():
    return time.time()


def _stamp():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _append(path, line):
    """Append one line to an agent-state log; never raises, never writes where a reader may not."""
    if not path:
        return
    try:
        if not P.may_write(path):
            return
    except P.KhmsPathError:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def log(event, query, decision=None, action=None, qh=""):
    results = decision.results if decision else []
    top = f"{results[0][2]['id']}:{results[0][0]:.1f}" if results else "-"
    act = action if action is not None else decision.action
    if decision is not None:
        if decision.text and decision.action.startswith("INJECT"):
            act += f" chars={len(decision.text)}"
        if decision.skips:
            act += " skipped=" + ",".join(f"{k}:{w}" for k, w in decision.skips[:3])
        if decision.mode != "lexical":
            act += f" mode={decision.mode}"
    q = re.sub(r"\s+", " ", query or "")[:LOG_Q]
    _append(LOG, f"{_stamp()} | {event} | q={q} | nhits={len(results)} | top={top} | {act}"
                 f"{(' qh=' + qh) if qh else ''}{(' mode=' + EXP_TAG) if EXP_TAG else ''}\n")


def load_state(session_id):
    path = f"{STATE_DIR}/{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id or 'nosession')}.json"
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("injected", {})
    st.setdefault("recent", [])
    st.setdefault("queries", {})
    st.setdefault("bypass", [])
    st.setdefault("precheck", {})
    return path, st


def save_state(path, st):
    try:
        if not P.may_write(path):
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except (OSError, P.KhmsPathError):
        pass


def emit(event, text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


# --- SessionStart --------------------------------------------------------------------------------------
def mark_ledger_line():
    """The journal-MARK coverage ledger: OPEN count and the oldest OPEN mark. Reads only
    memory/ledger/summary.json (written by the night, approve_inbox.py and `mark_ledger.py sync`), so the
    hook scans nothing. A missing summary is SAID, never skipped: silence would read as "nothing OPEN"."""
    path = f"{BRAIN}/memory/ledger/summary.json"
    try:
        with open(path, encoding="utf-8") as fh:
            s = json.load(fh)
    except (OSError, ValueError) as exc:
        return M.msg("HOOK_LEDGER_NONE", exc=exc.__class__.__name__)
    o = s.get("oldest_open")
    old = (M.msg("HOOK_LEDGER_OLDEST", id=o.get("id"), date=o.get("date"), time=o.get("time"),
                 kind=o.get("kind"), text=(o.get("text") or "")[:90])
           if o else M.msg("HOOK_LEDGER_NO_OPEN"))
    sug = s.get("open_suggested")
    return M.msg("HOOK_LEDGER_LINE", floor=s.get("floor"), open=s.get("open"),
                 queued=s.get("open_queued"), proposed=s.get("open_proposed"),
                 suggested=M.msg("HOOK_LEDGER_SUGGESTED", n=sug) if sug else "",
                 carded=s.get("carded"), dismissed=s.get("dismissed"), carry=s.get("carry", "?"),
                 oldest=old, generated=s.get("generated")) + _ledger_skipped_part(s)


def _ledger_skipped_part(s):
    """How many status-kind lines the ledger left out (#236), appended outside HOOK_LEDGER_LINE so a private
    wording cannot hide it. A summary from before #236 has no count and gets no tail."""
    try:
        import mark_ledger as _ml
    except Exception:                                              # noqa: BLE001
        return ""
    return _ml.skipped_part(s, "HOOK_LEDGER_SKIPPED")


# Is the night pipeline running? Without per-stage caps the night publishes its inbox units one batch at a
# time, so "unreviewed" in the middle of the night is a half-written night, not the morning's review.
# nightly_state.py owns the answer (shared with the review ping): it READS the night's flock out of
# /proc/locks (never takes it) and takes the start from the line the holder writes into the lock file.
PROC_LOCKS = "/proc/locks"


def nightly_lock_path():
    return _loc("KHMS_NIGHTLY_LOCK", "KHMS_NIGHTLY_LOCK")


def nightly_in_progress():
    """{"kind", "start", "nk"} while the night holds its flock; None when nobody holds it. Raises OSError
    when the lock table cannot be read -- the caller says so instead of guessing."""
    import nightly_state as _ns
    return _ns.night_running(BRAIN, nightly_lock_path(), os.environ.get("KHMS_PROC_LOCKS", PROC_LOCKS))


def _hhmm(t):
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M")


def inbox_reminder_line(unreviewed, inbox):
    """The inbox line of SessionStart, or None. While the night runs, files it wrote since its start are
    named as in progress, never offered as the review."""
    try:
        night = nightly_in_progress()
    except OSError as exc:
        if not unreviewed:
            return None
        return (M.msg("HOOK_INBOX_UNREVIEWED", files=", ".join(unreviewed))
                + M.msg("HOOK_INBOX_NIGHT_UNKNOWN", exc=exc.__class__.__name__))
    if night is None:
        if not unreviewed:
            return None
        return M.msg("HOOK_INBOX_UNREVIEWED", files=", ".join(unreviewed))
    import nightly_state as _ns
    ready, wip = _ns.split_ready_wip(unreviewed, inbox, night)
    head = M.msg("HOOK_NIGHT_RUNNING", start=_hhmm(night["start"]),
                 night=M.msg("HOOK_NIGHT_KEY", nk=night["nk"]) if night["nk"] else "")
    if ready:
        head += M.msg("HOOK_NIGHT_READY", files=", ".join(ready))
    else:
        head += M.msg("HOOK_NIGHT_NOTHING_READY")
    if wip:
        head += M.msg("HOOK_NIGHT_WIP", files=", ".join(wip))
    return head


def review_dir():
    return P.soft("REVIEW_MARKERS") or os.path.join(BRAIN or "", "state", "shared", "reviewed")


def unreviewed_inbox(inbox):
    """Inbox files whose review does not hold. mark_ledger.py owns the per-unit rule when it is installed
    (a review holds only while its marker still describes the file); without it the marker files decide:
    `.reviewed-<date>[-cards]` for a card batch, `.reviewed-<date>[-triage]` for a contradiction triage."""
    rdir = review_dir()
    card_units = None
    try:
        import mark_ledger as _ml
        _mp = _ml.Paths(BRAIN)
        _mp.reviewed = rdir
        card_units = {os.path.basename(f) for f, ok in _ml.review_units(_mp) if not ok}
    except Exception as exc:                                       # noqa: BLE001
        log("SessionStart", "inbox-check", action="unit check unavailable (%s)" % exc.__class__.__name__)
    out = []
    for f in sorted(khms_files.listdir(inbox)):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})(-weekly)?\.md$", f)
        if card_units is not None:
            if f in card_units:
                out.append(f)
        elif m and not any(os.path.exists(f"{rdir}/.reviewed-{m.group(1)}{s}")
                           for s in ("", f"{m.group(2) or ''}-cards")):
            out.append(f)
        if f.endswith('-triage.md'):
            import khms_review_state
            if not khms_review_state.triage_reviewed(os.path.join(inbox, f), BRAIN, rdir):
                out.append(f)
    return out


def handle_session_start(payload):
    state_path, state = load_state(payload.get('session_id', ''))
    clear_proposal_state(state)
    save_state(state_path, state)
    parts = []
    inbox = f"{BRAIN}/memory/inbox"
    try:
        unreviewed = unreviewed_inbox(inbox)
    except OSError:
        unreviewed = []
    night_running = False
    try:
        _line = inbox_reminder_line(unreviewed, inbox)
        try:
            night_running = bool(_line) and nightly_in_progress() is not None
        except OSError:
            night_running = False
    except Exception as exc:                                       # noqa: BLE001
        log("SessionStart", "inbox-check", action="night check error %s" % exc)
        _line = M.msg("HOOK_INBOX_UNREVIEWED", files=", ".join(unreviewed)) if unreviewed else None
    if _line:
        parts.append(_line)
        log("SessionStart", "inbox-check", action=f"inject inbox={','.join(unreviewed)}"
            + (" night-running" if night_running else ""))
    else:
        log("SessionStart", "inbox-check", action="silent (inbox clean)")

    try:
        parts.append(mark_ledger_line())
        log("SessionStart", "mark-ledger", action="inject ledger")
    except Exception as exc:                                       # noqa: BLE001
        parts.append(M.msg("HOOK_LEDGER_ERROR", exc=exc))
        log("SessionStart", "mark-ledger", action="error %s" % exc)

    # The gates' fail-open records of the last 24 h, marked shown for this session.
    try:
        _fo = failopen_line(now() - FAILOPEN_WINDOW_S)
        if _fo:
            parts.append(_fo)
        _fp, _fst = load_state(payload.get("session_id", ""))
        _fst["failopen_seen_t"] = now()
        save_state(_fp, _fst)
        log("SessionStart", "failopen", action="inject %d B" % len(_fo))
    except Exception as exc:                                       # noqa: BLE001
        log("SessionStart", "failopen", action="error %s" % exc)

    # The full directive enters the session ONCE, here, instead of riding on every prompt.
    parts.append(directive_text())
    emit("SessionStart", "\n".join(parts))


# --- the status-MARK gate (#236, owner 2026-10-13) ---------------------------------------------------------
# "drop the status lines, stop producing them, nobody is to even write them": a status line (package done, board
# reply, deployed, checked, ...) is a plain journal line without MARK. A write that would ADD a MARK line of a
# status kind (mark_ledger.STATUS_KINDS, the ledger's own denylist and grammar) to a journal file of the base
# is refused, for every agent: Edit and Write by their text, Bash when the command redirects or tees into a
# journal file. A know-how MARK and a plain status line pass; so does an edit that only keeps an old one.
# What is refused here is exactly what the ledger would skip, because both use mark_ledger.parse_marks().
BASH_TARGET_RE = re.compile(r"""(?:&>>?|\d?>>?|\btee\b(?:\s+-{1,2}[\w-]+)*)\s*(["']?)([^\s"';|&<>()]+)\1""")
SHELL_EXPANSION_RE = re.compile(r"\$\([^()]*\)|\$\{[^{}]*\}|\$\w+|%[-\d.]*[sdif]")
QUOTED_RE = re.compile(r"'([^']*)'|\"((?:[^\"\\]|\\.)*)\"")


def _journal_dir():
    j = P.soft("JOURNAL") or (os.path.join(BRAIN, "journal") if BRAIN else None)
    return os.path.realpath(j) if j else None


def _in_journal(path, cwd):
    """True for a file directly inside the base's journal directory."""
    jd = _journal_dir()
    if not jd or not path:
        return False
    p = os.path.expanduser(str(path))
    if "$" in p:                                       # an unexpanded variable: judge by the shape
        return bool(re.search(r"(?:^|/)journal/[^/]+\.md$", p))
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    rp = os.path.realpath(p)
    return os.path.dirname(rp) == jd and rp.endswith(".md")


def _status_mark_lines(text):
    """The lines of `text` that carry a status-kind MARK by the ledger's own grammar."""
    import mark_ledger as _ml
    out = []
    for line in str(text or "").splitlines():
        if "MARK[" not in line:
            continue
        skipped = {}
        _ml.parse_marks(line, skipped)
        if skipped:
            out.append(line.strip())
    return out


def _bash_candidates(cmd):
    """Text a shell command would write: its own lines and every quoted string (split on real and `\\n`
    newlines), with shell expansions and printf conversions read as a time so `"$(date -Is) MARK[x] ..."`
    and `printf '- %s MARK[x] ...'` parse like the journal line they produce."""
    parts = list(cmd.splitlines())
    for m in QUOTED_RE.finditer(cmd):
        parts.extend(re.split(r"\\n|\n", m.group(1) if m.group(1) is not None else m.group(2)))
    return "\n".join(SHELL_EXPANSION_RE.sub("00:00", x) for x in parts)


def status_mark_violations(payload):
    """-> the status-MARK lines a PreToolUse payload would ADD to a journal file ([] = allowed)."""
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or ""
    if tool in ("Edit", "Write"):
        path = ti.get("file_path", "")
        if not _in_journal(path, cwd):
            return []
        if tool == "Write":
            new = ti.get("content", "")
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    old = fh.read()
            except OSError:
                old = ""
        else:
            new, old = ti.get("new_string", ""), ti.get("old_string", "")
        before = set(_status_mark_lines(old))
        return [ln for ln in _status_mark_lines(new) if ln not in before]
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        if "MARK" not in cmd:
            return []
        if not any(_in_journal(m.group(2), cwd) for m in BASH_TARGET_RE.finditer(cmd)):
            return []
        # the redirection itself must not read as part of the line: drop `>> target` before parsing
        return list(dict.fromkeys(_status_mark_lines(_bash_candidates(BASH_TARGET_RE.sub(" ", cmd)))))
    return []


def run_status_mark_gate(payload):
    """True == refused (the deny was printed)."""
    bad = status_mark_violations(payload)
    if not bad:
        return False
    shown = " | ".join(b[:140] for b in bad[:3]) + (" | +%d more" % (len(bad) - 3) if len(bad) > 3 else "")
    _deny("PreToolUse", M.msg("HOOK_STATUS_MARK_DENY", lines=shown))
    log("PreToolUse", shown[:200], action="DENY (status MARK into the journal: %d line(s))" % len(bad))
    return True


# --- tool events -----------------------------------------------------------------------------------------
def extract_bash_text(payload):
    ti = payload.get("tool_input") or {}
    cmd = str(ti.get("command", ""))[:200]
    out = payload.get("tool_output")
    if isinstance(out, dict):
        out = json.dumps(out, ensure_ascii=False)
    out = str(out or "")
    tail = out[-2500:]
    err_lines = [ln for ln in tail.splitlines() if ERROR_RE.search(ln)][:3]
    text = cmd + " " + " ".join(err_lines)
    if not DOMAIN_RE.search(text) and not err_lines:
        return None
    return text[:400]


def run_precheck(tags):
    """Zero LLM tokens: precheck.sh prints the policies, gotchas and refuted cards for the tags. Any
    failure is silent -- this must never block a call."""
    try:
        r = subprocess.run([os.path.join(TOOLS, "precheck.sh"), *tags],
                           capture_output=True, text=True, timeout=PRECHECK_TIMEOUT)
        out = (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out or out.startswith("precheck: nothing on record"):
        return None
    return out[:PRECHECK_CAP]


def try_precheck(payload, st, t):
    """PreToolUse(Bash) only. Returns text to emit, or None.

    The output is COUNTED against the injection budget but not BLOCKED by it: a warning on the one
    destructive command of a capped window is exactly the one that must not be dropped. The per-session,
    per-tag-set cooldown is what bounds this path."""
    cmd = str((payload.get("tool_input") or {}).get("command", ""))
    tags = sel.risky_tags(cmd)
    if not tags:
        return None
    key = "+".join(tags)
    if t - st["precheck"].get(key, 0) < PRECHECK_COOLDOWN:
        return None
    st["recent"] = [x for x in st["recent"] if t - x < PARAMS.rate_window]
    body = run_precheck(tags)
    st["precheck"][key] = t
    if not body:
        return None
    st["recent"].append(t)
    return M.msg("HOOK_PRECHECK_HEAD", tags=key) + "\n" + body + "\n" + M.msg("HOOK_PRECHECK_FOOT")


# --- the experiment file ---------------------------------------------------------------------------------
def load_experiment():
    """The per-event injection switch and the gate flags. FAIL-OPEN in both directions: a missing, empty or
    broken file means "behave as without the experiment", so deleting it is a complete rollback."""
    try:
        with open(EXPERIMENT, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError, TypeError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def inject_on(exp, event):
    return bool((exp.get("inject") or {}).get(event, True))


def exp_tag(exp):
    """Stamped into every log line, so a day's records say which regime produced them."""
    if not exp:
        return ""
    name = exp.get("name")
    if not name:
        off = [SHORT_EVENT[e] for e in INJECT_EVENTS if not inject_on(exp, e)]
        name = ("no-" + "+".join(off)) if off else "on"
    return f"exp:{name}"


# --- the proposal gate -----------------------------------------------------------------------------------
# A report or a brief that carries a PROPOSAL (a recommendation, a "shall I ...?", an A) B) choice) is
# denied unless a recall of the same turn shares the proposal's own key terms: a recall on the symptom
# alone does not show that the base was asked about the proposed remedy. The matchers below have English
# defaults; a deployment adds its own language through the private KHMS_PATTERNS file.
PROPOSAL_LOG = (os.environ.get("KHMS_PROPOSAL_LOG")
                or (os.path.join(os.path.dirname(os.environ["KHMS_BAZE_LOG"]), ".proposal_gate.log")
                    if os.environ.get("KHMS_BAZE_LOG") else None)
                or P.soft("KHMS_PROPOSAL_LOG"))
PROPOSAL_EN_MARKER_RE = re.compile(
    r"\b(?:i|we)\s+(?:would\s+|'d\s+|do\s+|strongly\s+)?(?:propose|recommend|suggest)\b"
    r"|\b(?:my|our)\s+(?:proposal|recommendation|suggestion|advice)\b"
    r"|\b(?:shall|should)\s+(?:i|we)\b[^?]*\?")
PROPOSAL_EN_LABEL_RE = re.compile(
    r"(?:^|[(\[]\s*|[.!;:]\s+|\s[-–—]\s+)(recommendation|recommended|recommend|proposal|"
    r"suggestion|proposed fix|proposed change)\s*:", re.I)
PROPOSAL_EN_CHOICE_RE = re.compile(r"\bchoose\b|\bwhich (?:option|one|variant)\b|\bpick (?:one|a|an)\b")
# Report-mode marker: the deployment's own (HOOK_PROPOSAL_MARKER) or the English one. Brief mode always
# accepts both, because a brief may be written in either language.
PROPOSAL_MARKER_RE = PAT.regex("HOOK_PROPOSAL_MARKER", PROPOSAL_EN_MARKER_RE.pattern)
PROPOSAL_OPTION_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\d+[.)]\s*)?(?:\*\*)?\(?([A-E])\)")
PROPOSAL_LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+(?:[–-]\d+)?[.)])\s")
# A line that refers back to a previous proposal ("item 2", "A or B").
PROPOSAL_REF_RE = PAT.regex(
    "HOOK_PROPOSAL_REF", r"\b(?:[Ii]tem|[Pp]oint)s?\s+\d|\b[A-E]\s*(?:,|\band\b|\bor\b|/)\s*[A-E]\b")
# Words that make an option block a CHOICE put to the principal.
PROPOSAL_CHOICE_RE = PAT.regex("HOOK_PROPOSAL_CHOICE", PROPOSAL_EN_CHOICE_RE.pattern)
# The mandatory report lines are not proposals (normalised line start, with the colon).
PROPOSAL_SKIP_RE = PAT.regex("HOOK_PROPOSAL_SKIP", r"^(?:base|verified|if i were wrong)\s*:")
# ...and are not context either (normalised line start).
PROPOSAL_META_RE = PAT.regex("HOOK_PROPOSAL_META", r"^(base|verified|if i were wrong)\b")
_QUOTED_RE = re.compile(r"`[^`]*`|\"[^\"\n]*\"|„[^“\n]*“|“[^”\n]*”")
_SLASH_NEAR = 3                # a marker this close to a `/` is an item of a word list

PROPOSAL_MAX_SHOWN = 3        # proposals named in one refusal
PROPOSAL_QUERY_TERMS = 8      # terms in the suggested query
PROPOSAL_MIN_TERMS = 3        # fewer on the marker line -> take the line above
PROPOSAL_STOP = frozenset("""
the and for with this that from into then than are was
its not our you your will would should shall propose proposed proposal recommend recommended
recommendation suggest suggestion option variant choose
""".split()) | frozenset(PAT.words("HOOK_PROPOSAL_STOPWORDS", ()))
# Term prefixes too generic to tell one proposal from another in this deployment.
PROPOSAL_GENERIC = tuple(PAT.words("HOOK_PROPOSAL_GENERIC", ()))
_PROPOSAL_NOISE_RE = re.compile(r"^(?:k-\d+|[\d.,:/x%\-]+|https?:.*|.*://.*)$")


def _unquoted(text):
    return _QUOTED_RE.sub(" ", text or "")




def brief_marker(raw, n):
    """A proposal marker on one brief line -- either language, not quoted, not in a `/` word list, not a
    section title (a markdown heading or an ALL-CAPS label)."""
    if raw.lstrip().startswith("#"):
        return False
    t = _unquoted(n)
    for rx in (PROPOSAL_MARKER_RE, PROPOSAL_EN_MARKER_RE):
        if any(_not_listed(m, t) for m in rx.finditer(t)):
            return True
    r = _unquoted(raw)
    for m in PROPOSAL_EN_LABEL_RE.finditer(r):
        if m.group(1).isupper():
            continue
        return True
    return False


def _prop_same(a, b):
    """Same word up to an inflection: equal, or a common prefix of at least 4 letters that leaves at most
    2 letters of the shorter word over (calibrate/calibration)."""
    if a == b:
        return True
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 4 and n >= min(len(a), len(b)) - 2


def _prop_line_norm(raw):
    return ks.norm(raw).strip().lstrip("-*_>•·+ \t")




def proposal_terms(span):
    """Key terms of a proposal in order of appearance, one per stem."""
    out = []
    for t in ks.tokens(span):
        t = t.strip(".,-/")
        if len(t) < 3 or t in PROPOSAL_STOP or _PROPOSAL_NOISE_RE.match(t) \
                or (PROPOSAL_GENERIC and t.startswith(PROPOSAL_GENERIC)):
            continue
        if any(_prop_same(t, o) for o in out):
            continue
        out.append(t)
    return out


def proposal_query(body, terms):
    """The suggested recall: the most specific terms, in reading order. Specific = a capitalised name
    inside a sentence, an identifier (digits, `_`, `.`, `/`), then length."""
    caps = {ks.norm(w) for w in re.findall(r"(?<![.!?]\s)(?<!^)\b([A-Z][\w\-]+)", body or "")}
    rank = sorted(range(len(terms)), key=lambda i: (
        -(terms[i] in caps or bool(re.search(r"[\d_./]", terms[i]))), -len(terms[i]), i))
    keep = sorted(rank[:PROPOSAL_QUERY_TERMS])
    return " ".join(terms[i] for i in keep)


def _prop_words(terms, glossary):
    words = set(terms)
    for t in terms:                        # the base may be written in another language than the report
        for key, en in glossary.items():
            if t.startswith(key):
                words.update(ks.tokens(en))
    return words


def _prop_covered(words, recall_tokens):
    return any(_prop_same(w, r) for w in words for r in recall_tokens)


# Recall binding. Only recalls of THIS session's turn count: the session's own transcript (a recall.sh
# Bash call whose tool_result is not an error), and a subagent's recall only through its own transcript
# AND only when the gated text cites a card that call returned. The PreToolUse intent record says which
# subagent issued which recall.
RECALL_CMD_RE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?recall\.sh\s+(.+?)(?=\s+\d*[<>]|\s*[|;\n]|\s*&&|\s*\)|$)")


def recall_intent_path():
    if os.environ.get("KHMS_RECALL_INTENT"):
        return os.environ["KHMS_RECALL_INTENT"]
    if os.environ.get("KHMS_RECALL_LOG"):
        return os.environ["KHMS_RECALL_LOG"] + ".intent.jsonl"
    return P.soft("KHMS_RECALL_INTENT")


def recall_queries_of(cmd):
    """Queries of every `recall.sh ...` in a shell command (flags and redirects out)."""
    out = []
    for m in RECALL_CMD_RE.finditer(cmd or ""):
        arg = m.group(1)
        try:
            words = shlex.split(arg)
        except ValueError:
            words = arg.split()
        words = [w for w in words if not w.startswith("--")]
        if words:
            out.append(" ".join(words))
    return out


def record_recall_intent(payload):
    """PreToolUse(Bash): one intent line per recall.sh in the command. Never raises."""
    try:
        cmd = str((payload.get("tool_input") or {}).get("command", ""))
        if "recall.sh" not in cmd:
            return 0
        qs = recall_queries_of(cmd)
        if not qs:
            return 0
        path = recall_intent_path()
        if not path or not P.may_write(path):
            return 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for q in qs:
                f.write(json.dumps({
                    "ts": round(time.time(), 3), "sid": payload.get("session_id", ""),
                    "agent_id": payload.get("agent_id") or "",
                    "tool_use_id": payload.get("tool_use_id") or "",
                    "query": q}, ensure_ascii=False) + "\n")
        return len(qs)
    except Exception:                                              # noqa: BLE001
        return 0


def _jsonl_tail(path, nbytes=CLAIM_TAIL):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return []
    lines = data.splitlines()
    if size > nbytes and lines:
        lines = lines[1:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _result_text(content):
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
    return str(content or "")


def transcript_recalls(path, since_t, main=True, only_ids=None):
    """[(tool_use_id, query, cards)] of the recall.sh Bash calls in transcript `path` issued at/after
    `since_t` whose tool_result (matched by tool_use_id) is NOT an error. `main` skips sidechain lines;
    `only_ids` restricts to those calls."""
    uses, results = {}, {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for ln in fh:
            is_use = '"tool_use"' in ln and "recall.sh" in ln
            is_res = '"tool_result"' in ln and uses and any(i in ln for i in uses)
            if not (is_use or is_res):
                continue
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if main and d.get("isSidechain"):
                continue
            msg = d.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_use" and d.get("type") == "assistant":
                    tid = blk.get("id") or ""
                    cmd = str((blk.get("input") or {}).get("command", ""))
                    ts = _ts_epoch(d.get("timestamp"))
                    if (not tid or blk.get("name") != "Bash" or "recall.sh" not in cmd
                            or ts is None or ts + CLAIM_SLACK < since_t
                            or (only_ids is not None and tid not in only_ids)):
                        continue
                    uses[tid] = recall_queries_of(cmd)
                elif blk.get("type") == "tool_result" and d.get("type") == "user":
                    tid = blk.get("tool_use_id")
                    if tid in uses:
                        results[tid] = (bool(blk.get("is_error")), _result_text(blk.get("content")))
    out = []
    for tid, qs in uses.items():
        if tid not in results or results[tid][0]:
            continue                       # still running, or failed: no recall
        cards = set(re.findall(r"\bK-\d{5}\b", results[tid][1]))
        out += [(tid, q, cards) for q in qs]
    return out


AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def bound_recalls(session, since_t, text="", transcript="", agent_id=""):
    """[(token set, how)] of the recalls bound to THIS session's turn. `agent_id`: the gated call comes
    from that subagent, so ITS transcript is the "own" one."""
    if not session or since_t is None or not transcript:
        return []
    transcript = os.path.expanduser(str(transcript))
    cited = set(re.findall(r"K-\d{5}", text or ""))

    def terms(q):
        return {t for t in ks.tokens(q) if t not in PROPOSAL_STOP
                and not (PROPOSAL_GENERIC and t.startswith(PROPOSAL_GENERIC))}
    out = []
    base = transcript[:-len(".jsonl")] if transcript.endswith(".jsonl") else transcript
    own, own_main = transcript, True
    if agent_id:
        if not AGENT_ID_RE.match(str(agent_id)):
            return []
        own, own_main = os.path.join(base, "subagents", "agent-%s.jsonl" % agent_id), False
    for tid, q, _cards in transcript_recalls(own, since_t, main=own_main):
        if ks.tokens(q):
            out.append((terms(q), M.msg("HOOK_HOW_OWN")))
    by_agent = {}
    for it in _jsonl_tail(recall_intent_path()):
        aid, tid = str(it.get("agent_id") or ""), str(it.get("tool_use_id") or "")
        if (it.get("sid") == session and aid and tid and AGENT_ID_RE.match(aid)
                and it.get("ts", 0) + CLAIM_SLACK >= since_t):
            by_agent.setdefault(aid, set()).add(tid)
    for aid, tids in sorted(by_agent.items()):
        if aid == agent_id:
            continue                       # already counted as the caller's own
        sub = os.path.join(base, "subagents", "agent-%s.jsonl" % aid)
        for tid, q, cards in transcript_recalls(sub, since_t, main=False, only_ids=tids):
            if ks.tokens(q) and cited & cards:
                out.append((terms(q), M.msg("HOOK_HOW_SUBAGENT", agent=aid[:12])))
    return out


def _prop_context_terms(text, body):
    """Terms of the text OUTSIDE this proposal (the mandatory report lines out): a word the whole text is
    about cannot be what tells this proposal apart."""
    rest = []
    for ln in (text or "").splitlines():
        core = ln.strip().lstrip("-*•·+> \t").strip()
        if not core or core in (body or "") or PROPOSAL_META_RE.match(_prop_line_norm(ln)):
            continue
        rest.append(ln)
    return proposal_terms("\n".join(rest))


def proposal_covered(body, terms, ctx_terms, recalls, glossary):
    """A recall covers a proposal when it shares at least two of its terms (one, if the proposal has only
    one) AND one shared term DISTINGUISHES it -- a term the rest of the text does not use. Heuristic: it
    shows a recall on the proposal's own subject was evidenced, not that the proposal is right."""
    distinct = [t for t in terms if not any(_prop_same(t, c) for c in ctx_terms)]
    if not distinct:
        distinct = proposal_query(body, terms).split()[:3]
    need = min(2, len(terms))
    for rt in recalls:
        shared = [t for t in terms if _prop_covered(_prop_words([t], glossary), rt)]
        if len(shared) >= need and any(t in distinct for t in shared):
            return True
    return False


def proposal_verdict(text, st, session="", transcript="", since_t=None, agent_id="", brief=False):
    """(ALLOW|DENY|EXEMPT, reason, [(label, suggested query)] of the uncovered proposals). `since_t`
    overrides the report turn (the dispatch gate passes its own); `agent_id` names the calling subagent."""
    turn_start = st.get("turn_start") if since_t is None else since_t
    if turn_start is None:
        return "EXEMPT", M.msg("HOOK_WHY_NO_TURN"), []
    spans = proposal_spans(text, brief=brief)
    if not spans:
        return "EXEMPT", M.msg("HOOK_WHY_NO_PROPOSAL"), []
    bound = bound_recalls(session, turn_start, text, transcript, agent_id)
    recalls = [r for r, _how in bound]
    try:
        glossary = ks.load_glossary()
    except Exception:                                            # noqa: BLE001
        glossary = {}
    missing = []
    for label, body in spans:
        terms = proposal_terms(body)
        if not terms:
            continue
        if proposal_covered(body, terms, _prop_context_terms(text, body), recalls, glossary):
            continue
        missing.append((label, proposal_query(body, terms)))
    if missing:
        return ("DENY", M.msg("HOOK_WHY_UNCOVERED", n=len(missing), total=len(spans),
                              bound=len(recalls)), missing)
    return ("ALLOW", M.msg("HOOK_WHY_COVERED", n=len(spans),
                           how=", ".join(sorted({h for _r, h in bound})) or "-"), [])


def proposal_log(session, verdict, why, label):
    _append(PROPOSAL_LOG, "%s | %s | %s | %s | %s\n" % (
        _stamp(), session or "-", verdict, why, re.sub(r"\s+", " ", label or "")[:120]))


def _proposal_items(missing):
    """One line per uncovered proposal, each with the exact recall to run."""
    return "\n".join(M.msg("HOOK_PROPOSAL_ITEM", label=re.sub(r"\s+", " ", label)[:90], query=q)
                     for label, q in missing[:PROPOSAL_MAX_SHOWN])




# --- the dispatch gate: the same rule when a subagent is launched -------------------------------------
# A PreToolUse on the Agent tool whose prompt, or the brief file the prompt names, carries a proposal is
# denied without a bound recall on that proposal in the same turn (every UserPromptSubmit opens a turn,
# task notifications too). An unreadable brief fails open, and the fail-open is logged as one.
AGENT_TOOLS = ("Agent", "Task")
ISSUE_LINE_RE = re.compile(r"^\s*Issue:\s*#(\d+)\b", re.M)
BRIEF_PATH_RE = re.compile(r"(/[^\s'\"`<>|;()]+\.md)\b")
MD_TOKEN_RE = re.compile(r"[^\s'\"`<>|;()]+\.md\b")
HEAD_LINES = 5


def _head(text, n=HEAD_LINES):
    return "\n".join(str(text or "").splitlines()[:n])


def brief_head(prompt):
    """(head text, source) -- the first lines that carry an `Issue:` line. The prompt's own first lines
    win; otherwise the first named `.md` file that exists and carries the line in ITS first lines. The
    source is "prompt", the path, or None."""
    head = _head(prompt)
    if ISSUE_LINE_RE.search(head):
        return head, "prompt"
    for path in BRIEF_PATH_RE.findall(str(prompt or "")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                fhead = "".join(fh.readline() for _ in range(HEAD_LINES))
        except OSError:
            continue
        if ISSUE_LINE_RE.search(fhead):
            return fhead, path
    return head, None


def _is_brief_name(p, src=None):
    return p == src or "/briefs/" in p or os.path.basename(p).startswith("brief")


def dispatch_brief_paths(prompt, cwd=None, unresolved=None):
    """The brief files a dispatch prompt names: the one brief_head found, and every named .md under a
    `briefs/` directory or named `brief...` -- a design or a handoff it points at is context, not the brief.
    `~` is expanded and a relative path is joined to `cwd`; a relative brief path with no cwd is appended to
    `unresolved`, never dropped."""
    _head_text, src = brief_head(prompt)
    out = []
    for tok in MD_TOKEN_RE.findall(str(prompt or "")):
        if tok.startswith("/"):
            p = tok
        elif tok.startswith("~"):
            p = os.path.expanduser(tok)
        elif cwd:
            p = os.path.normpath(os.path.join(cwd, tok))
        else:
            if _is_brief_name("/" + tok) and unresolved is not None and tok not in unresolved:
                unresolved.append(tok)
            continue
        if _is_brief_name(p, src) and p not in out:
            out.append(p)
    return out








# --- fail-open records: a broken gate is not an idle one ---------------------------------------------
STOP_LOG = _loc("KHMS_STOP_LOG", "KHMS_STOP_LOG")
FAILOPEN_WINDOW_S = 24 * 3600


def failopen_logs():
    """[(gate label, log path)] of the gates whose fail-opens are reported."""
    return [(M.msg("HOOK_GATE_PROPOSALS"), PROPOSAL_LOG), (M.msg("HOOK_GATE_STOP"), STOP_LOG)]


def failopen_records(since_t):
    """[(epoch, gate, why, log path)] of `ERROR->ALLOW` records newer than since_t."""
    out = []
    for gate, path in failopen_logs():
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-2000:]
        except (OSError, TypeError):
            continue
        for ln in lines:
            if "| ERROR->ALLOW |" not in ln:
                continue
            f = [x.strip() for x in ln.split("|")]
            try:
                t = datetime.datetime.fromisoformat(f[0]).timestamp()
            except (ValueError, IndexError):
                continue
            if t > since_t:
                out.append((t, gate, " | ".join(f[3:]).strip(" |")[:140], path))
    return sorted(out)


def failopen_line(since_t):
    recs = failopen_records(since_t)
    if not recs:
        return ""
    t, gate, why, path = recs[-1]
    gates = collections.Counter(g for _t, g, _w, _p in recs)
    try:
        shown = os.path.relpath(path, BRAIN) if BRAIN and P.under(path, BRAIN) else path
    except (TypeError, ValueError):
        shown = path
    return M.msg("HOOK_FAILOPEN", n=len(recs),
                 gates=", ".join("%s %d" % kv for kv in sorted(gates.items())),
                 hhmm=_hhmm(t), gate=gate, why=why, path=shown)


def _with_failopen(text, payload):
    """Prepend the fail-open records this session has not been shown yet."""
    try:
        path, st = load_state(payload.get("session_id", ""))
        since = st.get("failopen_seen_t", now() - FAILOPEN_WINDOW_S)
        line = failopen_line(since)
        st["failopen_seen_t"] = now()
        save_state(path, st)
    except Exception as exc:                                       # noqa: BLE001
        log("UserPromptSubmit", "failopen", action="error %s" % exc)
        return text
    return (line + "\n" + text) if line else text


# --- Stop: the reply-owed gate -------------------------------------------------------------------------
# When the principal's LAST message came in over the report channel and no successful report-tool call
# followed it, the turn may not end: the principal does not see the terminal.
_TAG_ATTR_RE = re.compile(r'\b(chat_id|message_id)="([^"]*)"')
_SCRIPT_PROMPTS = ("[AUTO-PING]", "[POST-CLEAR", "[SYSTEM NOTIFICATION",
                   "<task-notification>", "<local-command-")


def _channel_tag_re():
    return re.compile(r'^\s*<channel\s+source="[^"]*%s[^"]*"' % re.escape(REPORT_SERVER))


def _prompt_of(d):
    """(text, origin, meta) of a transcript entry that carries a PROMPT, None for everything else
    (assistant turns, tool results, hook attachments)."""
    if not isinstance(d, dict) or d.get("isSidechain"):
        return None
    typ = d.get("type")
    if typ == "user":
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            blocks = [b for b in c if isinstance(b, dict)]
            if any(b.get("type") == "tool_result" for b in blocks):
                return None
            text = " ".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
        else:
            text = str(c or "")
        return (text, d.get("origin") or {}, bool(d.get("isMeta") or d.get("isCompactSummary")))
    if typ == "attachment":
        a = d.get("attachment") or {}
        if a.get("type") != "queued_command":
            return None
        return str(a.get("prompt") or ""), a.get("origin") or {}, bool(a.get("isMeta"))
    return None


def owner_message_kind(d):
    """'report' | 'terminal' | 'channel:<server>' | None (not the principal).

    Transcript shapes (Claude Code): a channel message is a `user` entry with isMeta and origin
    {"kind": "channel", "server": ...} or, when it lands mid-turn, an `attachment` of type `queued_command`
    with the same origin; a subagent's completion has origin kind `task-notification`; a prompt typed into
    the terminal has kind `human`. An unknown kind is not the principal."""
    got = _prompt_of(d)
    if got is None:
        return None
    text, origin, meta = got
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind == "channel":
        server = str(origin.get("server", ""))
        return "report" if REPORT_SERVER in server else "channel:" + server
    if _channel_tag_re().match(text):
        return "report"           # a transcript written before `origin` existed
    s = text.lstrip()
    if not s or s.startswith(_SCRIPT_PROMPTS):
        return None
    if kind == "human":
        return "terminal"
    if kind is not None or meta:
        return None
    return "terminal"


def reply_owed(transcript_path):
    """(owed, why, owner) -- owed only when the principal's LAST message came over the report channel and
    no report-tool call after it succeeded. Read backwards and stops at that message. A call whose result
    is an error (a gate denied it) does not count; one whose result is not written yet does -- unknown
    resolves toward letting the turn end."""
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    replies, errors = [], set()
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        content = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
        if d.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" \
                        and REPORT_TOOL_RE.search(str(b.get("name") or "")):
                    replies.append(b.get("id"))
            continue
        if d.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                    errors.add(b.get("tool_use_id"))
        kind = owner_message_kind(d)
        if kind is None:
            continue
        text = _prompt_of(d)[0]
        ts = d.get("timestamp") or (d.get("attachment") or {}).get("timestamp")
        owner = {"kind": kind, "ts": ts, "text": text}
        if kind != "report":
            return False, "last owner message: " + kind, owner
        ok = [r for r in replies if r not in errors]
        if ok:
            return False, "replied (%d reply call(s) after it)" % len(ok), owner
        return True, ("no reply call after it" if not replies else
                      "%d reply call(s) after it, all failed" % len(replies)), owner
    return False, "no owner message in the transcript", None


discord_reply_owed = reply_owed      # legacy name


def _ts_epoch(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def stop_log(session, verdict, why, owner=None):
    snip = re.sub(r"\s+", " ", re.sub(r"<[^>]{1,400}>", " ", (owner or {}).get("text", "")))[:80].strip()
    _append(STOP_LOG, f"{_stamp()} | {(session or 'nosession')[:8]} | {verdict} | {why}"
                      f"{(' | owner: ' + snip) if snip else ''}\n")


def run_stop_gate(payload):
    """Prints the block decision when a reply is owed; silent (= the turn ends) otherwise. Every outcome is
    one line in the stop log -- the count of BLOCK lines is the measurement."""
    session = payload.get("session_id", "")
    if not load_experiment().get("stop_gate", True):
        stop_log(session, "ALLOW", "stop_gate off in the experiment file")
        return
    if payload.get("stop_hook_active"):
        stop_log(session, "ALLOW", "stop_hook_active (already continuing after a block)")
        return
    if payload.get("agent_id"):
        stop_log(session, "ALLOW", "subagent")
        return
    tp = str(payload.get("transcript_path") or "")
    if not tp:
        stop_log(session, "ALLOW", "no transcript_path")
        return
    owed, why, owner = reply_owed(os.path.expanduser(tp))
    if not owed:
        stop_log(session, "ALLOW", why, owner)
        return
    _p, st = load_state(session)
    replied_t, owner_t = st.get("discord_reply_t"), _ts_epoch(owner.get("ts"))
    if replied_t and owner_t and float(replied_t) >= owner_t:
        stop_log(session, "ALLOW", "reply passed the PreToolUse gates %.0f s after the "
                 "owner message; transcript lags" % (float(replied_t) - owner_t), owner)
        return
    attrs = dict(_TAG_ATTR_RE.findall(owner.get("text", "")))
    print(json.dumps({"decision": "block", "reason": M.msg(
        "HOOK_STOP_REASON", chat_id=attrs.get("chat_id", "?"), message_id=attrs.get("message_id", "?"),
        tool=REPORT_TOOL_RE.pattern)}, ensure_ascii=False))
    stop_log(session, "BLOCK", why, owner)


# --- the entry point ---------------------------------------------------------------------------------------
def is_report_tool(payload):
    return bool(REPORT_TOOL_RE.pattern) and bool(REPORT_TOOL_RE.search(str(payload.get("tool_name", ""))))


def handle_report(payload, event, exp):
    """PreToolUse on the report tool: never refused (#247 removed the claim, proposal and terminology gates
    on a report); the reply is stamped for the Stop gate, which cannot count on the transcript showing it yet."""
    try:
        _sp, _sst = load_state(payload.get("session_id", ""))
        _sst["discord_reply_t"] = now()
        save_state(_sp, _sst)
    except Exception as exc:                                       # noqa: BLE001
        log(event, "report", action="stop-stamp error %s" % exc)


_GATE_CARDS = []
_SLASH_NEAR = 3
PROPOSAL_TOPN = 5
PROPOSAL_MAX_SHOWN = 3
PROPOSAL_Q_CHARS = 400
PROPOSAL_LOG_WORDS = 12
PROPOSAL_SRC = "gate:proposal"
PROPOSAL_CZ_LABEL_RE = PAT.regex('HOOK_PROPOSAL_LABEL', r'(?!)')
PROPOSAL_CZ_NONE = set(PAT.words('HOOK_PROPOSAL_NONE', ['none', 'no', 'unchanged']))
PROPOSAL_CZ_PAREN_ADJ_RE = PAT.regex('HOOK_PROPOSAL_PAREN_ADJ', r'(?!)')
PROPOSAL_CZ_PAREN_RE = PAT.regex('HOOK_PROPOSAL_PAREN', r'(?!)')
PROPOSAL_DENY_HEAD = {'dispatch':'This brief proposes a change (%s); relevant recall results:'}
PROPOSAL_DENY_TAIL = 'Read the relevant cards, revise the proposal or resend it after reading. The same proposal is shown once per author context.'

PROPOSAL_EN_MARKER_RE = re.compile(
    r"\b(?:i|we)\s+(?:would\s+|'d\s+|do\s+|strongly\s+)?(?:propose|recommend|suggest)\b"
    r"|\b(?:my|our)\s+(?:proposal|recommendation|suggestion|advice)\b"
    r"|\b(?:shall|should)\s+(?:i|we)\b[^?]*\?")

PROPOSAL_EN_LABEL_RE = re.compile(
    r"(?:^|[(\[]\s*|[.!;:]\s+|\s[-–—]\s+)(recommendation|recommended|recommend|proposal|"
    r"suggestion|proposed fix|proposed change)\s*:", re.I)

PROPOSAL_LIST_RE = re.compile(r"^\s*(?:[-*•]|\d+(?:[–-]\d+)?[.)])\s")

_QUOTED_RE = re.compile(r"`[^`]*`|\"[^\"\n]*\"|„[^“\n]*“|“[^”\n]*”")

def _not_listed(m, text, group=0):
    a, b = m.start(group), m.end(group)
    return "/" not in text[max(0, a - _SLASH_NEAR):a] and "/" not in text[b:b + _SLASH_NEAR]

def proposal_marker(raw, n):
    'Shared proposal/terminology gate: proposal_marker'
    if raw.lstrip().startswith("#"):
        return False
    t = _unquoted(n)
    for rx in (PROPOSAL_MARKER_RE, PROPOSAL_EN_MARKER_RE):
        if any(_not_listed(m, t) for m in rx.finditer(t)):
            return True
    for m in PROPOSAL_EN_LABEL_RE.finditer(_unquoted(raw)):
        if not m.group(1).isupper():
            return True
    if _cz_label(t) is not None:
        return True
    rxs = (PROPOSAL_CZ_PAREN_ADJ_RE,) if PROPOSAL_LIST_RE.match(raw) else (
        PROPOSAL_CZ_PAREN_ADJ_RE, PROPOSAL_CZ_PAREN_RE)
    return any(_not_listed(m, t, 1) for rx in rxs for m in rx.finditer(t))

def _cz_label(t):
    'Shared proposal/terminology gate: _cz_label'
    for m in PROPOSAL_CZ_LABEL_RE.finditer(t):
        if not _not_listed(m, t):
            continue
        rest = m.group("rest") if m.group("rest") is not None else m.group("box") or ""
        rest = re.sub(r"[\W_]+", " ", rest).strip()
        if rest not in PROPOSAL_CZ_NONE:
            return rest
    return None

def proposal_spans(text, brief=False):
    'Shared proposal/terminology gate: proposal_spans'
    lines = (text or "").splitlines()
    usable = []                         # (index, raw, normalised) outside code
    pos = {}                            # raw line index -> usable index
    in_code = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or raw.lstrip().startswith(">"):
            continue
        n = _prop_line_norm(raw)
        if not n or PROPOSAL_SKIP_RE.match(n):
            continue
        pos[i] = len(usable)
        usable.append((i, raw, n))
    spans, taken = [], set()
    for k, (i, raw, n) in enumerate(usable):
        if k in taken or not proposal_marker(raw, n):
            continue
        body = raw.strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        head = _cz_label(_unquoted(n)) == "" and (i + 1) in pos   # standalone proposal label, text below
        if (PROPOSAL_LIST_RE.match(nxt) or head) and not PROPOSAL_LIST_RE.match(raw):
            j = i + 1
            while j < len(lines) and lines[j].strip() and j <= i + 15:
                if j in pos:
                    taken.add(pos[j])
                    body += " " + lines[j].strip()
                j += 1
        elif spans and PROPOSAL_REF_RE.search(raw):
            label, prev = spans[-1]
            spans[-1] = (label, prev + " " + body)
            taken.add(k)
            continue
        elif k > 0 and (k - 1) not in taken:
            body = usable[k - 1][1].strip() + " " + body
        taken.add(k)
        spans.append((raw.strip(), body))
    return spans

def _gate_cards():
    'Shared proposal/terminology gate: _gate_cards'
    if not _GATE_CARDS:
        _GATE_CARDS.append(ks.load_cards())
    return _GATE_CARDS[0]

def proposal_rank(query, cards):
    'Shared proposal/terminology gate: proposal_rank'
    import khms_recall_hybrid as hyb
    top, meta = hyb.hybrid_rank(query, cards=cards, topn=PROPOSAL_TOPN)
    channel = "lexical:fallback" if meta.get("dense_err") else "hybrid:%s" % hyb.MODEL
    return [c for _s, _m, c in top], channel

def clear_proposal_state(st):
    'Shared proposal/terminology gate: clear_proposal_state'
    st.pop("prop_seen", None)
    st.pop("prop_shown", None)
    st.pop("prop_turn", None)

def stamp_prompt_turn(payload):
    'Shared proposal/terminology gate: stamp_prompt_turn'
    path, st = load_state(payload.get("session_id", ""))
    st["prompt_t"] = now()
    save_state(path, st)

def _log_query(span_text):
    words = re.sub(r"[|\s]+", " ", span_text).split()
    return " ".join(words[:PROPOSAL_LOG_WORDS])

def proposal_gate(texts, author, st, cards=None, log=True, turn=None):
    'Shared proposal/terminology gate: proposal_gate'
    seen = st.setdefault("prop_seen", {}).setdefault(author, {})
    shown = st.setdefault("prop_shown", {}).setdefault(author, {})
    turns = st.setdefault("prop_turn", {})
    tkey = "%s|%s" % (author, turn[0]) if turn and turn[1] is not None else None
    this_turn = turns.get(tkey) if tkey else None
    if this_turn and this_turn.get("turn") != turn[1]:
        this_turn = None
    cited = set()
    for _w, t in texts:
        cited.update(re.findall(r"\bK-\d{5}\b", t or ""))
    out, done = [], set()
    for where, text in texts:
        for label, body in proposal_spans(text):
            q = re.sub(r"\s+", " ", body).strip()[:PROPOSAL_Q_CHARS]
            fp = hashlib.sha1(ks.norm(q).encode("utf-8")).hexdigest()[:16]
            if fp in done:
                continue
            done.add(fp)
            sp = {"where": where, "label": label, "query": _log_query(q), "fp": fp,
                  "top": [], "new": [], "lines": [], "channel": ""}
            out.append(sp)
            if fp in seen:
                sp["tag"] = "REPEAT"
                continue
            if cards is None:
                cards = _gate_cards()
            top, sp["channel"] = proposal_rank(q, cards)
            if this_turn and set(this_turn.get("cards", [])) & {c["id"] for c in top}:
                sp["tag"], sp["top"] = "SHOWN", list(this_turn.get("cards", []))
                continue
            if log:
                ks.log_query(sp["query"], [(0.0, [], c) for c in top],
                             src="%s:%s" % (PROPOSAL_SRC, sp["channel"]))
            sp["top"] = [c["id"] for c in top]
            sp["new"] = [c["id"] for c in top if c["id"] not in shown and c["id"] not in cited]
            sp["lines"] = ["%s%s %s" % (c["id"], "" if c.get("status") in ("active", "?")
                                        else " [%s]" % c.get("status"),
                                        re.sub(r"\s+", " ", c.get("first", ""))[:110])
                           for c in top]
            sp["tag"] = "NOHIT" if not top else "KNOWN" if not sp["new"] else "OPEN"
    t = now()
    opened = [sp for sp in out if sp["tag"] == "OPEN"]
    for sp in opened:
        seen[sp["fp"]] = t
        for cid in sp["top"]:
            shown[cid] = t
    if opened and tkey:
        prev = this_turn.get("cards", []) if this_turn else []
        turns[tkey] = {"turn": turn[1], "cards": list(dict.fromkeys(
            prev + [cid for sp in opened for cid in sp["top"]]))}
    if not out:
        return "EXEMPT", out
    return ("DENY" if opened else "ALLOW"), out

def proposal_log_spans(session, what, spans, _st=None):
    'Shared proposal/terminology gate: proposal_log_spans'
    for sp in spans:
        tag = sp["tag"]
        verdict = "DENY" if tag == "OPEN" else "ALLOW-" + tag
        why = {"OPEN": "new cards %s (of %s)" % (",".join(sp["new"]), ",".join(sp["top"])),
               "REPEAT": "same proposal already shown",
               "KNOWN": "all %d cards shown or cited (%s)"
                        % (len(sp["top"]), ",".join(sp["top"])),
               "NOHIT": "recall found no card",
               "SHOWN": "shown-this-turn %s" % ",".join(sp["top"])}[tag]
        chan = (" [%s]" % sp["channel"]) if sp.get("channel") else ""
        proposal_log(session, verdict, "%s: %s%s" % (what, why, chan), sp["label"])

def proposal_deny_text(head, spans):
    out = [head]
    for sp in [s for s in spans if s["tag"] == "OPEN"][:PROPOSAL_MAX_SHOWN]:
        out.append("- proposal %s (query: `%s`)" % (
            re.sub(r"\s+", " ", sp["label"])[:90], sp["query"]))
        out += ["    %s" % ln for ln in sp["lines"]]
    out.append(PROPOSAL_DENY_TAIL)
    return "\n".join(out)

def _deny(event, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": event,
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}, ensure_ascii=False))

def dispatch_texts(prompt, cwd=None):
    'Shared proposal/terminology gate: dispatch_texts'
    texts, errors, unresolved = [("prompt", str(prompt or ""))], [], []
    for p in dispatch_brief_paths(prompt, cwd, unresolved):
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts.append(("brief " + p, fh.read()))
        except OSError as exc:
            errors.append((p, exc.__class__.__name__))
    errors += [(t, "relativni cesta bez cwd") for t in unresolved]
    return texts, errors

def run_dispatch_proposal_gate(payload, exp):
    'Shared proposal/terminology gate: run_dispatch_proposal_gate'
    if not exp.get("proposal_gate", True):
        return False
    session = payload.get("session_id", "")
    path, st = load_state(session)
    ti = payload.get("tool_input") or {}
    what = "dispatch %s" % re.sub(r"\s+", " ", str(ti.get("description")
                                                   or ti.get("subagent_type") or "?"))[:60]
    texts, errors = dispatch_texts(ti.get("prompt") or "", payload.get("cwd") or None)
    for p, err in errors:
        proposal_log(session, "ERROR->ALLOW",
                     "%s: unreadable brief (%s, fail-open)" % (what, err), p)
    verdict, spans = proposal_gate(texts, str(payload.get("agent_id") or "main"), st,
                                   turn=("dispatch", st.get("prompt_t", st.get("turn_start"))))
    if verdict == "EXEMPT":
        proposal_log(session, "EXEMPT", "%s: bez navrhu" % what, "")
        return False
    save_state(path, st)
    proposal_log_spans(session, what, spans)
    if verdict != "DENY":
        return False
    where = ", ".join(sorted({s["where"] for s in spans if s["tag"] == "OPEN"}))
    _deny("PreToolUse", proposal_deny_text(PROPOSAL_DENY_HEAD["dispatch"] % where, spans))
    log("PreToolUse", what, action="DENY (proposal in brief: %d open)"
        % sum(1 for s in spans if s["tag"] == "OPEN"))
    return True

def main():
    global EXP_TAG
    if os.environ.get("KHMS_HOOKS_OFF") == "1" or (HOOKS_OFF_FILE and os.path.exists(HOOKS_OFF_FILE)):
        return
    payload = json.load(sys.stdin)
    event = payload.get("hook_event_name", "")
    if event == "Stop":
        # First and on its own: it needs nothing the other branches load, and whatever goes wrong inside
        # it must end in "let the turn end".
        try:
            run_stop_gate(payload)
        except Exception as exc:                                   # noqa: BLE001
            stop_log(payload.get("session_id", ""), "ERROR->ALLOW", "%s: %s" % (type(exc).__name__, exc))
        return
    # The status-MARK gate (#236) first: it needs no experiment, no state and no card base, and it applies
    # to every agent. Like every gate it fails OPEN, logged.
    if event == "PreToolUse" and payload.get("tool_name") in ("Edit", "Write", "Bash"):
        try:
            if run_status_mark_gate(payload):
                return
        except Exception as exc:                                   # noqa: BLE001
            log(event, "status-mark-gate", action="ERROR->ALLOW %s: %s" % (type(exc).__name__, exc))
    exp = load_experiment()
    EXP_TAG = exp_tag(exp)
    # Every prompt opens a turn for the dispatch gate -- task notifications too -- before anything below
    # can return early on such a prompt.
    if event == "UserPromptSubmit":
        try:
            stamp_prompt_turn(payload)
        except Exception as exc:                                   # noqa: BLE001
            log(event, "prompt-turn", action="stamp error %s" % exc)

    if event == "PreToolUse" and payload.get("tool_name") in AGENT_TOOLS:
        try:
            run_dispatch_proposal_gate(payload, exp)
        except Exception as exc:                                   # noqa: BLE001
            log(event, "dispatch", action="dispatch-proposal-gate error %s" % exc)
            proposal_log(payload.get("session_id", ""), "ERROR->ALLOW",
                         "dispatch bypass: %s" % str(exc)[:80], "")
        return

    if event == "PreToolUse" and payload.get("tool_name") == "Bash":
        record_recall_intent(payload)

    if event == "PreToolUse" and is_report_tool(payload):
        handle_report(payload, event, exp)
        return

    if event == "SessionStart":
        handle_session_start(payload)
        return
    if event not in PARAMS.threshold:
        return

    job = False
    if event == "UserPromptSubmit":
        raw = str(payload.get("prompt", ""))
        if "<task-notification>" in raw or raw.lstrip().startswith("[SYSTEM NOTIFICATION"):
            return
        job = sel.is_job_prompt(raw)
        raw = re.sub(r"<[^>]{1,400}>", " ", raw)
        raw = re.sub(r'\b(chat_id|message_id|user_id|ts|source)="[^"]*"', " ", raw)
        if len(raw.strip()) < MIN_PROMPT_LEN:
            log(event, raw, action="skip (short)")
            return
        query = raw[:500]
    elif event == "PreToolUse" and payload.get("tool_name") in ("Edit", "Write"):
        ti = payload.get("tool_input") or {}
        text = " ".join([
            str(ti.get("file_path", "")),
            str(ti.get("old_string", ""))[:200],
            str(ti.get("new_string", ""))[:200],
            str(ti.get("content", ""))[:200],
        ])
        if not DOMAIN_RE.search(text):
            return
        query = text[:400]
    else:
        query = extract_bash_text(payload)
        if query is None and not (event == "PreToolUse" and payload.get("tool_name") == "Bash"):
            return

    # An event whose card injection is switched off exits HERE -- no state, no base, no search, no recall
    # log line. A prompt still gets the directive line: the experiment removes the CARDS, not the duty to
    # say where an answer came from.
    if not inject_on(exp, event) and not (event == "PreToolUse" and payload.get("tool_name") == "Bash"):
        if event == "UserPromptSubmit":
            emit(event, _with_failopen(directive_line(), payload))
        log(event, query, action="skip (exp:no-inject)")
        return

    path, st = load_state(payload.get("session_id", ""))
    t = now()

    # The risky-command class checks itself, before retrieval, so a command about to delete something does
    # not lose its warning to the ordinary card budget.
    if event == "PreToolUse" and payload.get("tool_name") == "Bash":
        pc = try_precheck(payload, st, t)
        if pc is not None:
            save_state(path, st)
            emit(event, pc)
            log(event, str((payload.get("tool_input") or {}).get("command", ""))[:200], action="PRECHECK")
            return
        if query is None:
            save_state(path, st)
            return
        # precheck is not part of the injection experiment; ordinary Bash scoring is.
        if not inject_on(exp, event):
            save_state(path, st)
            log(event, query, action="skip (exp:no-inject)")
            return

    qh = hashlib.sha1(query.encode("utf-8", "replace")).hexdigest()[:12]
    # The budget FIRST, the card base second: everything pregate can decide needs no search result.
    pre = sel.pregate(event, query, st, t, PARAMS, query_hash=qh)
    if pre.decision is not None:
        save_state(path, st)
        # Not written to the recall log: nothing was searched.
        log(event, query, decision=pre.decision, qh=qh)
        return

    dense_fn = None
    if event in PARAMS.dense_events:
        try:
            import khms_recall_hybrid as hyb
            dense_fn = (lambda q, n: hyb.dense_query(
                q, topn=n, timeout_ms=float(os.environ.get("KHMS_DENSE_TIMEOUT_MS", "150"))))
        except Exception:                                          # noqa: BLE001
            dense_fn = None

    cards = sel.prepare_cards(ks.load_cards())
    d = sel.decide(event, query, st, t, cards, PARAMS, dense_fn=dense_fn, job=job, query_hash=qh, pre=pre)
    if d.searched:
        ks.log_query(query, d.results, src=f"hook2:{event}")
    save_state(path, st)

    text = d.text
    if text and text.startswith("%DIRECTIVE%"):
        note = text[len("%DIRECTIVE%"):].strip()
        text = directive_line() + (("  (" + note + ")") if note else "")
    elif text and event == "UserPromptSubmit":
        text = text + "\n" + directive_line()
    if event == "UserPromptSubmit":
        text = _with_failopen(text or "", payload)
    if text:
        emit(event, text)
    log(event, query, decision=d, qh=qh)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:      # fail open, always
        _append(LOG, f"{_stamp()} | ERROR | v2 {type(e).__name__}: {e}\n")
        sys.exit(0)
