#!/usr/bin/env python3
"""KHMS selection policy — the decision engine shared by the LIVE hook
(khms_hook.py) and the offline replay harness (hook_replay.py).

Usage: library — imported by khms_hook.py and hook_replay.py; not run directly.

WHY IT IS A SEPARATE MODULE: the live hook (khms_hook.py) hard-codes its policy
inside main(), so the only way to evaluate a change was to run it live. Here the
policy is a pure function of (event, query, state, t, cards, Params); the harness
can therefore replay a week of .inject.log decisions under LIVE params and under
V2 params against the same card base and diff them. (Until 2026-08-23 the live
hook did not import this module and the docstring said so; it has imported it
since, and khms_hook_v2.py — the pre-swap copy that still matched the old
sentence — was deleted on 2026-09-04.)

The parameter set is §7 CONFIGURATION (K-01553: "parameter values … are to be
calibrated, not invented"). LIVE reproduces khms_hook.py exactly:
    dedup_ttl=inf, damp_alpha=0, bypass_min=inf, dense_events=(), job_filter=off
so `decide(..., LIVE)` is the control arm of every experiment.

Implements K-01553(a)+(b) and the four F10 defects:
  (b)  dedup TTL                     — dedup_ttl
  F10c length attractors             — damp_alpha / damp_pivot
  F10c rate cap spends first-come    — bypass_min / bypass_events
  F10c undiagnosable misses          — Decision.skips (why the top hit lost)
  F10b lexical-only Czech misses      — dense_events / dense_min_cos (rescue)
  F10a KHMS jobs get robot hardware   — job_filter
  (a)  precheck on risky commands     — RISKY (used by the hook, not by scoring)
"""
import math
import os
import re
import sys
from dataclasses import dataclass, field, replace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_search as ks  # noqa: E402
import khms_paths as P  # noqa: E402
import khms_messages as M  # noqa: E402

INF = float("inf")


# ---------------------------------------------------------------- parameters
@dataclass(frozen=True)
class Params:
    name: str = "live"
    # --- identical to khms_hook.py's module constants ---
    threshold: dict = field(default_factory=lambda: {
        "UserPromptSubmit": 18.0, "PostToolUse": 16.0,
        "PostToolUseFailure": 15.0, "PreToolUse": 16.0})
    second_card_min: float = 14.0
    max_primary: int = 2
    char_cap: int = 900
    rate_max: int = 3
    rate_window: float = 600.0
    query_cooldown: float = 900.0
    topn: int = 8
    # --- v2 knobs; the values below are the LIVE (disabled) settings ---
    dedup_ttl: float = INF          # seconds until a card may be re-injected
    damp_alpha: float = 0.0         # score *= (pivot/ntok)**alpha, capped at 1
    damp_pivot: int = 85            # median distinct-token count of the base
    bypass_min: float = INF         # top score that buys a ride past the cap
    bypass_per_window: int = 1
    bypass_events: frozenset = frozenset()
    dense_events: frozenset = frozenset()
    dense_min_cos: float = 1.0
    dense_rescue_n: int = 5
    dense_gate_bonus: float = 0.0   # synthetic score = threshold + this
    rescue_min_first_len: int = 0   # a rescue is served as its FIRST LINE only
    # Finding #1 of the 2026-09-04 injection experiment (tools/khms_experiment.md):
    # replaying the owner's 09:19 prompt under the PRE-experiment policy logged
    # `INJECT [K-00562,K-00857] … skipped=K-00542:slots-full` — the dense channel
    # HAD the card that answered the question and the two-slot budget had no room
    # for it. This is the lever for that: when True, the dense channel's rank-1
    # card takes the LAST primary slot whenever the lexical picks do not already
    # contain it. OFF in LIVE and in V2 — nothing about today's behaviour changes
    # (hook_replay --compare: 0 decision diffs).
    dense_reserved_slot: bool = False
    rrf_k: float = 60.0
    rrf_cand: int = 50
    job_filter: bool = False
    cap_emits_directive: bool = False   # UPS: still ask for the retrieval-claim line


LIVE = Params(name="live")

# V2 defaults; every non-live value here is justified by a sweep in the report.
V2 = Params(
    name="v2",
    dedup_ttl=12 * 3600.0,      # sweep: p50 of dedup-suppressed top hits = 9.1 h
    damp_alpha=0.5,             # pivoted sqrt length normalisation
    damp_pivot=85,              # measured median distinct-token count (1865 cards)
    bypass_min=18.0,            # = the UserPromptSubmit threshold; +8 inj/week
    bypass_per_window=1,
    bypass_events=frozenset({"UserPromptSubmit"}),
    dense_events=frozenset({"UserPromptSubmit"}),
    dense_min_cos=0.55,         # sweep: 25 rescues/week; 0.50 doubles them and
    dense_rescue_n=5,           #        visibly lowers precision
    rescue_min_first_len=40,
    job_filter=True,
    cap_emits_directive=True,
)


# ------------------------------------------------------------------- KHMS jobs
# The nightly/weekly pipeline talks to the same harness, so its prompts hit the
# same hook. Measured (F10a): the consolidate stage drew K-00203 (dump-bed Cytron
# wiring) twice and the weekly synthesis drew K-00080 ("neither npm nor
# passwordless sudo"). These markers are the literal first lines of
# tools/prompts/*.md and of khms_review_ping.sh's message.
# The list is configuration (KHMS_MESSAGES key JOB_MARKERS): a deployment whose
# review ping is worded differently names its own prefix there.
JOB_MARKERS = M.job_markers()
JOB_TAGS = {"khms", "core"}


def is_job_prompt(query, env=None):
    """True when this prompt comes from the KHMS pipeline, not from the owner.
    env marker (KHMS_JOB=1) wins if the job scripts ever set it; the text
    markers work today with no change to any live script."""
    env = os.environ if env is None else env
    if env.get("KHMS_JOB") == "1":
        return True
    q = (query or "").lstrip()
    return any(q.startswith(m) for m in JOB_MARKERS)


# -------------------------------------------------------- risky-command class
# K-01553(a): precheck.sh stops being a duty the actor must remember. The class
# is the one the policy card names; the tags are what precheck.sh can match on.
RISKY = [
    (re.compile(r"rsync\b[^\n|;]*--delete"), ("deployment", "jetson")),
    (re.compile(r"\bnvpmodel\b"), ("jetson", "power")),
    (re.compile(r"\bnmcli\b"), ("network",)),
    (re.compile(r"\bgit\s+tag\s+(-f|--force)"), ("git",)),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"),
     ("deployment",)),
    (re.compile(r"\bsystemctl\b"), ("systemd", "deployment")),
    (re.compile(r"(^|[\s;&|'\"])[\w./-]*deploy/"), ("deployment",)),
]


def risky_tags(cmd):
    """Ordered, de-duplicated tag list for a command, or [] when it is not in
    the named risky class. First matching rule wins its tags first."""
    out = []
    for rx, tags in RISKY:
        if rx.search(cmd or ""):
            for t in tags:
                if t not in out:
                    out.append(t)
    return out


# ------------------------------------------------------------------- scoring
LEN_CACHE = P.soft("LEN_CACHE")   # agent-local cache (KHMS_LEN_CACHE)


def prepare_cards(cards, cache_path=LEN_CACHE):
    """Attach the length used for damping: the number of DISTINCT tokens in the
    body, because that is exactly what the idf SUM in khms_search.search can
    accumulate over (F10c: 'an idf sum grows with the number of distinct tokens
    a card happens to contain').

    Tokenising 1865 bodies costs ~80 ms, which a hook pays on every single call,
    so the counts are memoised per card id + body length. Cards are immutable
    once approved, and a body that does change changes its length, so the key is
    sound; a corrupt or unwritable cache just costs the recomputation."""
    import json as _json
    cache = {}
    if cache_path:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = _json.load(f)
        except (OSError, ValueError):
            cache = {}
    dirty = False
    for c in cards:
        if "_ntok" in c:
            continue
        key = f"{c['id']}:{len(c['nbody'])}"
        n = cache.get(key)
        if n is None:
            n = max(1, len(set(ks.tokens(c["nbody"]))))
            cache[key] = n
            dirty = True
        c["_ntok"] = n
    if dirty and cache_path and P.may_write(cache_path):
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(cache, f)
            os.replace(tmp, cache_path)
        except OSError:
            pass
    return cards


def damp_factor(ntok, p):
    if p.damp_alpha <= 0:
        return 1.0
    return min(1.0, (p.damp_pivot / float(ntok)) ** p.damp_alpha)


def lexical_raw(query, cards, want, job_filter=False):
    """UNDAMPED live scoring. `job_filter` restricts WHAT MAY BE SERVED, not what
    df/idf is computed over: idf in ks.search comes from its `cards` argument, so
    shrinking that would silently rescale every score. The ranking is filtered
    afterwards instead."""
    expanded, _ = ks.expand_query(query, load_glossary())
    if job_filter:
        res, _ = ks.search(expanded, cards=cards, exclude_meta=False, topn=want * 6)
        return [r for r in res if JOB_TAGS & set(r[2]["tags"])][:want]
    res, _ = ks.search(expanded, cards=cards, exclude_meta=True, topn=want)
    return res


def apply_damp(res, p):
    if p.damp_alpha <= 0:
        return list(res)
    out = [(s * damp_factor(c["_ntok"], p), m, c) for s, m, c in res]
    out.sort(key=lambda r: -r[0])
    return out


def lexical(query, cards, p, job=False, lex_fn=None):
    """Live scoring (+ damping, + job restriction). With damp_alpha == 0 and
    job == False this returns exactly what khms_hook.py gets from
    ks.search(expanded, exclude_meta=True)."""
    want = max(p.topn, p.rrf_cand)
    jf = bool(job and p.job_filter)
    raw = (lex_fn(query, jf) if lex_fn is not None
           else lexical_raw(query, cards, want, job_filter=jf))
    return apply_damp(raw, p)[:want]


_GLOSSARY = None


def load_glossary():
    global _GLOSSARY
    if _GLOSSARY is None:
        _GLOSSARY = ks.load_glossary()
    return _GLOSSARY


def rrf_order(ids_a, ids_b, k=60.0):
    fused = {}
    for rank, cid in enumerate(ids_a, 1):
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
    for rank, cid in enumerate(ids_b, 1):
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
    return fused


# ------------------------------------------------------------------ decision
@dataclass
class Decision:
    action: str                 # log verb, same vocabulary as .inject.log
    used: list = field(default_factory=list)
    text: str = None            # additionalContext, or None
    results: list = field(default_factory=list)   # scored (score, matched, card)
    skips: list = field(default_factory=list)     # (card_id, reason)
    searched: bool = False
    rescued: list = field(default_factory=list)   # ids offered by the dense channel
    dense_ms: float = 0.0
    dense_err: str = None
    mode: str = "lexical"

    @property
    def top(self):
        return self.results[0] if self.results else None


REPORT_DIRECTIVE_FOUND = M.msg("REPORT_DIRECTIVE_FOUND")


def _fresh(cid, injected, t, p):
    """Dedup with a TTL (K-01553b). LIVE: dedup_ttl == inf → never fresh again,
    which is what silenced the top-scoring K-00058 on 2026-08-17 after it was
    injected on 2026-08-14 in the same still-open session."""
    ts = injected.get(cid)
    if ts is None:
        return True
    return (t - ts) >= p.dedup_ttl


def correctors_of(c, by_id, rev):
    """Cards that correct c: the reverse edges (someone's contradicts/supersedes
    points AT c) plus c's own forward refuted_by. Never raises — a card base with
    a malformed link must degrade to "no correctors", not kill the hook."""
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


def build_injection(cands, cards, injected, t, p, skips):
    """≤max_primary fresh hits + correctors + one principle umbrella, exactly as
    khms_hook.build_injection, plus a reason for every candidate that lost.

    Every served card also carries its CORRECTORS, whatever its status. Until
    2026-08-28 that only happened for cards already marked challenged/refuted/
    superseded — but a correction arrives BEFORE anyone re-statuses the old card,
    and the old card stays `active` in the meantime. That is exactly what
    happened with K-00448/K-00449 (July: "IMU branches have no pull-ups, keep
    them disconnected", still `active`) versus K-02006 (2026-08-24, owner: the
    pull-ups are installed): the reverse index knew, the status gate hid it, and
    the stale July card was recited to the owner four days later.
    """
    by_id = {c["id"]: c for c in cards}
    rev = ks.reverse_correctors(cards)
    lines, used = [], []
    warn_chars = 0
    primaries = 0
    for score, _m, c in cands:
        if primaries >= p.max_primary:
            skips.append((c["id"], "slots-full"))
            continue
        if not _fresh(c["id"], injected, t, p):
            skips.append((c["id"], "dedup"))
            continue
        if c["id"] in used:
            continue
        umb = None
        for pid in c["links"]["supports"]:
            u = by_id.get(pid)
            if u and u["level"] in ("principle", "derived") and u["status"] == "active":
                umb = u
                break
        if umb and umb["id"] not in used and _fresh(umb["id"], injected, t, p):
            lines.append(card_line(umb, prefix=M.msg("SELECT_UMBRELLA_PREFIX")))
            used.append(umb["id"])
        lines.append(card_line(c, score))
        used.append(c["id"])
        primaries += 1
        correctors = correctors_of(c, by_id, rev)
        if c["status"] in ("challenged", "refuted", "superseded"):
            # unchanged path: the refuter is served as a full card line and is
            # marked injected, because a refuted card without it is useless.
            for cor in correctors[:1]:
                if cor["id"] not in used:
                    lines.append(card_line(cor, prefix=M.msg("SELECT_CORRECTED_BY_PREFIX")))
                    used.append(cor["id"])
                    correctors = [x for x in correctors if x["id"] != cor["id"]]
        # An ACTIVE card that something corrects gets a POINTER, not a card:
        # short, and it does not consume a primary slot or mark the corrector as
        # injected (a pointer must be able to repeat; the card behind it must
        # still be servable in full later).
        try:
            for cor in correctors[:MAX_WARN]:
                if cor["id"] in used:
                    continue
                line = warn_line(cor)
                lines.append(line)
                warn_chars += len(line) + 1
        except Exception:
            pass          # a pointer that cannot be built is dropped, never raised
    if not lines:
        return None, []
    text = (M.msg("SELECT_HEADER") + "\n"
            + "\n".join(lines)
            + "\n" + M.msg("SELECT_FOOTER"))
    # BUDGET: the ⚠ pointers are EXEMPT from char_cap — the cap on everything
    # else is unchanged (cap + exactly the characters the pointers added). They
    # are not extra retrieval: they are a correction to content that is being
    # injected anyway, and truncating one recreates the failure this exists to
    # prevent. Bounded by construction: ≤ MAX_WARN per card, ≤ WARN_CHARS chars
    # each, so ≤ 2 × ~140 chars per injection against a 900-char cap.
    return text[:p.char_cap + warn_chars], used


MAX_WARN = 2          # corrector pointers per served card
WARN_CHARS = 110      # of the corrector's first line


def warn_line(cor):
    """The reverse edge, one line: who corrects the card just served."""
    return M.msg("SELECT_CORRECTOR_POINTER", id=cor["id"], first=cor["first"][:WARN_CHARS])


def card_line(c, score=None, prefix="- "):
    flags = []
    if c["status"] in ("refuted", "challenged", "superseded"):
        flags.append("⛔" + c["status"].upper())
    if "gotcha" in c["tags"]:
        flags.append("⚠")
    if c.get("errata"):
        flags.append("✎")               # corrected in place (khms_erratum.py)
    flag = ("[" + ",".join(flags) + "] ") if flags else ""
    sc = f" ({score:.0f})" if score is not None else ""
    return f"{prefix}{c['id']}{sc} {flag}{c['first'][:100]}"


@dataclass
class PreGate:
    """What pregate() decided, and what decide() still has to know."""
    decision: "Decision" = None
    capped: bool = False


def pregate(event, query, state, t, p, query_hash=None):
    """Every budget decision that needs NO search result, so the caller can take
    it BEFORE paying for the card base.

    Audit 2026-09-02 §5: khms_hook.py called load_cards() (172 ms, 2467 cards)
    and only THEN let decide() drop the call on the query cooldown or the rate
    cap — 2324 times on the audited day (~6.7 min of wall clock spread over the
    session's tool calls), each one also writing a nhits=0 line into
    .recall.log (11 001 of 12 658 hook2 lines = 87 % of the log that is supposed
    to BE the record of retrieval).

    decide() calls this itself when the caller did not, so the offline replay
    (hook_replay.py) and the live hook take the same decisions in the same
    order; the hook passes its PreGate back in via decide(pre=...) so the state
    is mutated exactly once.

    The one budget rule that DOES need the search is the V2 rate-cap bypass
    ("an owner message whose top hit clears the bar buys one ride per window"):
    it stays inside decide(), gated by p.bypass_events — which contains only
    UserPromptSubmit. Deliberately NOT extended to PreToolUse: the bypass exists
    to rescue an owner QUESTION, while PreToolUse calls are the agent's own tool
    traffic (2044 of 2755 injections in 30 days), so buying them extra searches
    past the cap would spend the budget on the cheapest side of it. Under the
    2026-09-04 experiment (UPS injection off) the bypass has no event left to
    fire on; it is left in place unchanged, for the rollback.
    """
    state.setdefault("injected", {})
    state.setdefault("recent", [])
    state.setdefault("queries", {})
    state.setdefault("bypass", [])
    qh = query_hash if query_hash is not None else _qhash(query)
    if t - state["queries"].get(qh, 0) < p.query_cooldown:
        return PreGate(Decision("skip (query cooldown)"))
    state["queries"][qh] = t
    state["recent"] = [x for x in state["recent"] if t - x < p.rate_window]
    state["bypass"] = [x for x in state["bypass"] if t - x < p.rate_window]
    capped = len(state["recent"]) >= p.rate_max
    bypass_allowed = (capped and event in p.bypass_events
                      and len(state["bypass"]) < p.bypass_per_window)
    if capped and not bypass_allowed:
        # LIVE and V2-without-a-bypass-slot: no search at all.
        return PreGate(Decision("skip (rate cap)"))
    return PreGate(None, capped)


def decide(event, query, state, t, cards, p, dense_fn=None, job=None,
           query_hash=None, lex_fn=None, pre=None):
    """The whole gate, as one pure-ish function (it mutates `state` the same way
    khms_hook.py mutates its session file, and nothing else).

    state: {"injected": {id: ts}, "recent": [ts], "queries": {qh: ts},
            "bypass": [ts]}
    """
    thr = p.threshold[event]
    if job is None:
        job = (event == "UserPromptSubmit") and is_job_prompt(query)
    if pre is None:
        pre = pregate(event, query, state, t, p, query_hash=query_hash)
    if pre.decision is not None:
        return pre.decision
    capped = pre.capped

    results = lexical(query, cards, p, job=job, lex_fn=lex_fn)
    d = Decision("", results=results, searched=True)

    dense = []
    if event in p.dense_events and dense_fn is not None:
        dense, d.dense_err, d.dense_ms = dense_fn(query, p.rrf_cand)
        d.mode = "lexical" if d.dense_err else "hybrid"

    top_score = results[0][0] if results else 0.0

    if capped:
        # F10c: the cap spends the budget first-come. A query whose top hit
        # clears a HIGH bar buys one ride per window; everything else is still
        # dropped, but now with the score in the log so the miss is diagnosable.
        if top_score < p.bypass_min:
            d.action = "skip (rate cap)"
            if results:
                d.skips.append((results[0][2]["id"], f"cap top={top_score:.1f}"))
            if event == "UserPromptSubmit" and p.cap_emits_directive:
                d.text = "%DIRECTIVE%" + M.msg("NOTHING_ABOVE_BAR_CAPPED")
            return d
        state["bypass"].append(t)

    cands = [r for r in results[:4] if r[0] >= p.second_card_min] if results else []
    if not results or top_score < thr:
        for s, _m, c in results[:1]:
            d.skips.append((c["id"], f"threshold {s:.1f}<{thr:.0f}"))
        cands = []

    # Dense rescue (F10b). Only ADDS candidates; it never reorders the lexical
    # ones, so a query the lexical channel already answers is untouched.
    if dense:
        by_id = {c["id"]: c for c in cards}
        have = {c["id"] for _s, _m, c in cands}
        rescue = []
        for cid, cos in dense[:p.dense_rescue_n]:
            if cos < p.dense_min_cos or cid in have:
                continue
            c = by_id.get(cid)
            if c is None or c["status"] == "superseded":
                continue
            if job and p.job_filter and not (JOB_TAGS & set(c["tags"])):
                continue
            # A rescue has no lexical evidence behind it, and what gets injected
            # is the card's FIRST LINE. Cards whose first line is a bare field
            # ("WHO: <owner name>.") are dense neighbours of every owner-voiced sentence
            # and say nothing when served — measured in the cos=0.50 sample.
            if len(c["first"]) < p.rescue_min_first_len:
                continue
            rescue.append((thr + p.dense_gate_bonus, [], c))
        if rescue:
            d.rescued = [c["id"] for _s, _m, c in rescue]
            fused = rrf_order([c["id"] for _s, _m, c in results],
                              [h[0] for h in dense], k=p.rrf_k)
            rescue.sort(key=lambda r: -fused.get(r[2]["id"], 0.0))
            cands = cands + rescue
            d.mode = "hybrid+rescue"

    # Reserved dense slot (finding #1, 2026-09-04; OFF in LIVE and V2).
    # The rescue above only fires for cards the lexical channel did not reach
    # AT ALL and only above dense_min_cos — it cannot help when the slots are
    # already spoken for, which is exactly what happened to K-00542 (logged
    # `K-00542:slots-full`). This gives the dense channel's rank-1 card the LAST
    # primary slot instead of the weakest lexical pick. Deliberately scoped to
    # calls that were going to inject anyway: with no lexical candidate there is
    # no contested slot, and "inject the nearest neighbour of every prompt" is a
    # different (and much noisier) policy than this one.
    if p.dense_reserved_slot and dense and cands:
        by_id = {c["id"]: c for c in cards}
        have = {x[2]["id"] for x in cands}
        # "the dense channel's top hit THAT THE LEXICAL PICKS DO NOT CONTAIN",
        # not "dense rank 1". Measured on the case this lever exists for (the
        # owner's 09:19 prompt, 2026-09-04): dense rank 1 was K-00562, which the
        # picks already held, so a strict rank-1 rule left the decision exactly
        # as it was and K-00542 stayed `slots-full` — a lever inert on its own
        # motivating example. Scan depth = dense_rescue_n, the same depth the
        # rescue above trusts.
        for cid, _cos in dense[:p.dense_rescue_n]:
            if cid in have:
                continue
            c = by_id.get(cid)
            if c is None or c["status"] == "superseded":
                continue
            if job and p.job_filter and not (JOB_TAGS & set(c["tags"])):
                continue
            if len(c["first"]) < p.rescue_min_first_len:
                continue
            keep = max(0, p.max_primary - 1)
            cands = cands[:keep] + [(thr + p.dense_gate_bonus, [], c)] + cands[keep:]
            d.rescued = d.rescued + ([cid] if cid not in d.rescued else [])
            d.mode = "hybrid+slot"
            break

    if not cands:
        d.action = ("report-nothing (below threshold)"
                    if event == "UserPromptSubmit" else "silent (below threshold)")
        if event == "UserPromptSubmit":
            d.text = "%DIRECTIVE%" + M.msg("NOTHING_ABOVE_BAR")
        return d

    text, used = build_injection(cands, cards, state["injected"], t, p, d.skips)
    if not text:
        d.action = ("report-nothing (all deduped)"
                    if event == "UserPromptSubmit" else "silent (all deduped)")
        if event == "UserPromptSubmit":
            d.text = "%DIRECTIVE%" + M.msg("NOTHING_NEW")
        return d

    for kid in used:
        state["injected"][kid] = t
    state["recent"].append(t)
    d.action = f"INJECT [{','.join(used)}]"
    d.used = used
    d.text = text
    return d


def _qhash(query):
    import hashlib
    return hashlib.sha1(query.encode("utf-8", "replace")).hexdigest()[:12]


__all__ = ["Params", "LIVE", "V2", "decide", "Decision", "prepare_cards",
           "pregate", "PreGate",
           "lexical", "damp_factor", "risky_tags", "is_job_prompt",
           "rrf_order", "build_injection", "replace"]
