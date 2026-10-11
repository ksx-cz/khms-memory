#!/usr/bin/env python3
"""Transcript token accounting — the ONE implementation.

Why this file exists: in the reference deployment a status line reimplemented
transcript accounting from scratch and overcounted 2.57x because it summed every
JSONL line instead of deduping by requestId, while a pre-clear report script had
carried the correct measured implementation for days.  Two implementations of the
same arithmetic means one of them is silently wrong.  **No second implementation
of transcript accounting may exist** — every caller imports this module.

Callers (a deployment's own scripts import it from this clone):
  * a pre-clear report     — whole-file, exact-model pricing, pre-clear line
  * a status line          — incremental (byte offset), bucket pricing
  * preprocess_transcripts.py — run_parts(), to attribute a continued subagent run

THE CORE FACT (measured on a real transcript):
one API request emits one JSONL line PER CONTENT BLOCK, and every one of those
lines carries a usage snapshot for the WHOLE request.  Summing the lines
overcounts input/cache by the block count (1.64x .. 2.57x measured).  Dedup by
requestId; the LAST snapshot per request is the complete one.

INCREMENTAL READS AND WHY `recent` EXISTS
-----------------------------------------
Dedup-by-requestId must survive a reader that only sees the bytes appended
since last time.  When a later chunk carries another snapshot of a request
already counted in an earlier chunk, the running totals must be corrected, not
re-added.  So the caller's persisted state keeps the last snapshot per request
(`state["recent"]`, insertion-ordered) and `feed_*` subtracts the old snapshot
before adding the new one — arithmetically identical to whole-file last-wins.

`window` bounds that map so a long session's state file stays small.  Measured
2026-08-23 over every transcript in the project directory — 383 files, 88,648
usage lines, 42,450 requests: the largest distinct-request gap between two
snapshots of the SAME request is 1, i.e. a request's snapshots are always
contiguous (the most any one request emitted was 32 consecutive snapshots).
So any window >= 2 is exact on real data; statusline uses 64 for margin.  The
default (None = unbounded) is used for whole-file accounting, and the test
proves the two paths agree to the cent on a frozen real transcript.

EFFORT (H5 of docs/design/team-rules.md, issue #110, 2026-09-22)
---------------------------------------------------------------
Every assistant record carries the reasoning effort it ran at, as a top-level
`effort` field beside `timestamp` (measured 2026-09-22: 80/80 assistant lines
of agent-af93160b015c87d1f say "xhigh", 56/56 of the frozen fixture "max").
The state keeps a second map, `by_group`, keyed by (model, effort) — encoded
as "model|effort" because the state is persisted as JSON — with its own
request count.  `by_model` is untouched: price depends on the model alone, and
statusline/tok_share read that map.  A record without the field is grouped
under EFFORT_UNKNOWN, never merged into a real level.
"""

import datetime
import json
import os
import re
import sys

# ---------------------------------------------------------------- token layout

NBUCKET = 6
IDX_IN, IDX_OUT, IDX_CW5, IDX_CW1, IDX_CWX, IDX_CR = range(NBUCKET)
#   in       out      cache_write_5m  cache_write_1h  cache_write_legacy  cache_read
#
# IDX_CWX is the pre-`cache_creation`-dict form: older transcripts carry only a
# flat `cache_creation_input_tokens` with no 5m/1h split.  It is priced at the
# 5m rate but reported separately so the pre-clear line stays honest about not
# knowing the TTL.  Not seen in any transcript since 2026-07; kept for old ones.

STATE_VERSION = 2          # shape of the dict returned by new_state()
# v2 (2026-09-22, #110): + by_group / req_by_group, and `recent` entries carry
# the effort as a third element.  A persisted v1 state is REFUSED by
# valid_state(), so statusline re-reads that file from byte 0 — exact — rather
# than patching a group map that would miss every byte read before the upgrade.

# ------------------------------------------------------------------- price list
# List prices USD/MTok, (input, output).
# SOURCE: https://platform.claude.com/docs/en/about-claude/pricing (model pricing
# table), cross-checked against https://claude.com/pricing, re-verified 2026-09-15.
# NEVER write a price here from memory — read it out of the official page (a
# price list recited from memory is a guess).  A model missing from this table prices
# as UNKNOWN in the exact path, and falls back to its family bucket in the
# statusline path.
PRICES = {
    "claude-fable-5-1":  (10.0, 50.0),
    "claude-mythos-5-1": (10.0, 50.0),
    "claude-fable-5":    (10.0, 50.0),
    "claude-mythos-5":   (10.0, 50.0),
    # claude-opus-5-5: $4/$20 per MTok (input/output). SOURCE:
    # https://platform.claude.com/docs/en/about-claude/pricing (model pricing
    # table, row "Claude Opus 5.5"), fetched 2026-09-24: "Claude Opus 5.5 |
    # $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok^2 | $20 / MTok" (columns:
    # base input, 5m cache writes, 1h cache writes, cache hits/refreshes,
    # output). Cache write multipliers ($5/$4=1.25, $8/$4=2.0) match the
    # existing MULT_CW5/MULT_CW1 constants below, so no per-model override is
    # needed there. Cache-read multiplier is NOT the standard 0.1x: footnote 2
    # on the same page says "Cache hits and refreshes on Claude Opus 5.5 are
    # priced at 0.05x the base input price" ($0.20/$4=0.05) -- see
    # CACHE_READ_MULT_OVERRIDE below. "Long context pricing" section on the
    # same page: "Claude 4.6 and later models ... include the full 1M token
    # context window at standard pricing" -- no separate long-context rate
    # for this model, same as the other rows in this table.
    "claude-opus-5-5":   (4.0, 20.0),
    "claude-opus-5":     (5.0, 25.0),
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-opus-4-7":   (5.0, 25.0),
    "claude-opus-4-6":   (5.0, 25.0),
    # claude-sonnet-5-5: SOURCE https://platform.claude.com/docs/en/about-claude/pricing
    # (model pricing table, row "Claude Sonnet 5.5"), fetched 2026-10-08 (#144
    # PMETER — it priced as UNKNOWN until then): "Claude Sonnet 5.5 | $2 / MTok |
    # $2.50 / MTok | $4 / MTok | $0.10 / MTok^2 | $10 / MTok". Cache writes are
    # the standard 1.25x/2x; footnote 2: "Cache hits and refreshes on Claude
    # Opus 5.5 and Claude Sonnet 5.5 are priced at 0.05x the base input price"
    # -> CACHE_READ_MULT_OVERRIDE below.
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}
# claude-sonnet-5 is priced by date (intro pricing) — see price_for_model().
# 2026-09-15: the official pricing page confirms the previously scheduled
# 2026-09-01 increase to $3/$15 will NOT occur — $2/$10 is now the permanent
# list price.  SONNET5_LIST kept equal to SONNET5_INTRO (rather than deleting
# the date branch) so a future re-priced model can reuse the same shape.
SONNET5_INTRO_LAST_DAY = datetime.date(2026, 8, 31)
SONNET5_INTRO = (2.0, 10.0)
SONNET5_LIST = (2.0, 10.0)

# Cache multipliers on the INPUT price (source: same reference).
MULT_CW5 = 1.25    # 5-minute cache write
MULT_CW1 = 2.0     # 1-hour cache write
MULT_CWX = 1.25    # legacy flat cache write — assume 5m
MULT_CR = 0.1      # cache read
# claude-fable-5-1 and claude-mythos-5-1 get a special 0.025x cache-read rate
# ($0.25/MTok on a $10/MTok input price) instead of the standard 0.1x; every
# other model in PRICES uses MULT_CR.  Source: same pricing page, prompt
# caching table + footnote 1 on the model pricing table.
CACHE_READ_MULT_OVERRIDE = {
    "claude-fable-5-1":  0.025,
    "claude-mythos-5-1": 0.025,
    "claude-opus-5-5":   0.05,
    "claude-sonnet-5-5": 0.05,   # same footnote 2, fetched 2026-10-08
}

BUCKETS = ("fable", "mythos", "opus", "sonnet", "haiku")
_DATED = re.compile(r"-\d{8}$")


def normalize_model(model):
    """`claude-haiku-4-5-20251001` -> `claude-haiku-4-5`; None -> ''."""
    m = (model or "").strip()
    return _DATED.sub("", m)


def bucket_of(model):
    """Family bucket for a model string.  Unknown -> 'fable' (most expensive,
    so a bucket-priced estimate over-reports rather than under-reports)."""
    m = (model or "").lower()
    for b in BUCKETS:
        if b in m:
            return b
    return "fable"


def prices_for_bucket(bucket, today=None):
    """(input, output) USD/MTok for a family bucket.  Never returns None."""
    if bucket in ("opus",):
        return (5.0, 25.0)
    if bucket == "haiku":
        return (1.0, 5.0)
    if bucket == "sonnet":
        d = today or datetime.date.today()
        return SONNET5_INTRO if d <= SONNET5_INTRO_LAST_DAY else SONNET5_LIST
    return (10.0, 50.0)          # fable / mythos / unknown


def price_for_model(model, today=None):
    """Exact list price for a model string, or None when it is not in the
    table.  None means UNKNOWN — it does NOT mean free (tok.py used to price
    unknown models at (0, 0), which silently reported $0.00 runs)."""
    m = normalize_model(model)
    if m == "claude-sonnet-5":
        d = today or datetime.date.today()
        return SONNET5_INTRO if d <= SONNET5_INTRO_LAST_DAY else SONNET5_LIST
    return PRICES.get(m)


def cache_read_mult(model):
    """Cache-read multiplier for a model string.  MULT_CR for almost every
    model; CACHE_READ_MULT_OVERRIDE for the few with a different rate."""
    return CACHE_READ_MULT_OVERRIDE.get(normalize_model(model), MULT_CR)


def cost_of_vals(vals, pin, pout, cr_mult=MULT_CR):
    """USD for one 6-wide token vector at the given per-MTok prices.  cr_mult
    defaults to the standard 0.1x cache-read multiplier; pass
    cache_read_mult(model) for a model with a non-standard rate."""
    return (vals[IDX_IN] * pin
            + vals[IDX_OUT] * pout
            + vals[IDX_CW5] * pin * MULT_CW5
            + vals[IDX_CW1] * pin * MULT_CW1
            + vals[IDX_CWX] * pin * MULT_CWX
            + vals[IDX_CR] * pin * cr_mult) / 1e6


# ------------------------------------------------------------------ usage split

def _n(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def split_usage(u):
    """One usage object -> (in, out, cw5m, cw1h, cw_legacy, cache_read).

    `cache_creation` (the 5m/1h split) wins when present; only when the field
    is absent entirely does the flat `cache_creation_input_tokens` land in the
    legacy bucket.  An all-zero `cache_creation` dict is authoritative — it
    means zero cache writes, not "unknown"."""
    cc = u.get("cache_creation")
    if isinstance(cc, dict):
        cw5 = _n(cc.get("ephemeral_5m_input_tokens"))
        cw1 = _n(cc.get("ephemeral_1h_input_tokens"))
        cwx = 0
    else:
        cw5 = cw1 = 0
        cwx = _n(u.get("cache_creation_input_tokens"))
    return (_n(u.get("input_tokens")), _n(u.get("output_tokens")),
            cw5, cw1, cwx, _n(u.get("cache_read_input_tokens")))


def usage_of(obj):
    """-> (usage_dict, model_string) or (None, None)."""
    msg = obj.get("message")
    if isinstance(msg, dict):
        u = msg.get("usage")
        if isinstance(u, dict):
            return u, msg.get("model") or obj.get("model")
    u = obj.get("usage")
    if isinstance(u, dict):
        return u, obj.get("model")
    return None, None


EFFORT_UNKNOWN = "UNKNOWN"
GROUP_SEP = "|"            # model ids and effort levels never contain it


def effort_of(obj):
    """The reasoning effort a record ran at -> str, EFFORT_UNKNOWN when absent.

    A sibling of usage_of() rather than a third element of its tuple:
    ~/.claude/statusline.py unpacks `u, _ = ta.usage_of(obj)`."""
    e = obj.get("effort")
    if isinstance(e, str) and e.strip():
        return e.strip()
    return EFFORT_UNKNOWN


def group_key(model, effort):
    return "%s%s%s" % (model or "", GROUP_SEP, effort or EFFORT_UNKNOWN)


def split_group_key(key):
    model, _, effort = key.rpartition(GROUP_SEP)
    return model, effort or EFFORT_UNKNOWN


# ----------------------------------------------------------------------- state

def new_state():
    """Fresh accounting state.  JSON-serializable: statusline persists it."""
    return {
        "v": STATE_VERSION,
        "by_model": {},     # model -> 6-wide token vector
        "by_group": {},     # "model|effort" -> 6-wide token vector
        "req_by_group": {},  # "model|effort" -> deduped requests
        "requests": 0,      # deduped API requests seen
        "recent": {},       # rid -> [model, 6-wide vals, effort]  (insertion-ordered)
        "anon": 0,          # counter for lines carrying no usable request id
        "t0": "",           # first timestamp seen
        "t1": "",           # last timestamp seen
        "last_ctx": None,   # in+cw5+cw1+cwx+cr of the newest snapshot
    }


def valid_state(st):
    return (isinstance(st, dict) and st.get("v") == STATE_VERSION
            and isinstance(st.get("by_model"), dict)
            and isinstance(st.get("by_group"), dict)
            and isinstance(st.get("req_by_group"), dict)
            and isinstance(st.get("recent"), dict))


def _add(st, model, vals, sign=1):
    cur = st["by_model"].get(model)
    if not isinstance(cur, list) or len(cur) != NBUCKET:
        cur = [0] * NBUCKET
    for i in range(NBUCKET):
        cur[i] += sign * vals[i]
    st["by_model"][model] = cur


def _add_group(st, key, vals, sign=1):
    cur = st["by_group"].get(key)
    if not isinstance(cur, list) or len(cur) != NBUCKET:
        cur = [0] * NBUCKET
    for i in range(NBUCKET):
        cur[i] += sign * vals[i]
    st["by_group"][key] = cur
    st["req_by_group"][key] = st["req_by_group"].get(key, 0) + sign


def feed_lines(st, lines, *, window=None, skip_sidechain=False,
               assistant_only=True, prefilter=False):
    """Fold JSONL `lines` (str or bytes, any iterable) into state `st`.

    window          None = keep every requestId (exact, whole-file use).
                    int  = keep only the N most recent (bounded state file).
    skip_sidechain  drop `isSidechain: true` lines — the main transcript's copy
                    of subagent traffic, which is counted in agent-*.jsonl.
    assistant_only  only `type == "assistant"` lines carry usage (measured:
                    true for every transcript in the project directory).
    prefilter       skip lines without the literal `"usage"` — a speed
                    optimization that also skips their timestamps, so t0/t1
                    then span only usage-bearing lines.
    """
    for line in lines:
        if isinstance(line, bytes):
            if prefilter and b'"usage"' not in line:
                continue
            try:
                line = line.decode("utf-8", "replace")
            except Exception:
                continue
        elif prefilter and '"usage"' not in line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        if not isinstance(j, dict):
            continue

        ts = j.get("timestamp")
        if isinstance(ts, str) and ts:
            if not st["t0"]:
                st["t0"] = ts
            st["t1"] = ts

        if skip_sidechain and j.get("isSidechain") is True:
            continue
        if assistant_only and j.get("type") != "assistant":
            continue
        u, model = usage_of(j)
        if u is None:
            continue

        rid = j.get("requestId")
        if not rid:
            msg = j.get("message")
            if isinstance(msg, dict):
                rid = msg.get("id")
        if not rid:
            st["anon"] += 1
            rid = "anon%d" % st["anon"]

        vals = list(split_usage(u))
        model = model or ""
        effort = effort_of(j)
        prev = st["recent"].get(rid)
        if prev is None:
            st["requests"] += 1
        else:
            # Same request, later snapshot: undo the earlier one (last wins).
            _add(st, prev[0], prev[1], sign=-1)
            peff = prev[2] if len(prev) > 2 else EFFORT_UNKNOWN
            _add_group(st, group_key(prev[0], peff), prev[1], sign=-1)
            del st["recent"][rid]          # re-insert to refresh LRU order
        _add(st, model, vals)
        _add_group(st, group_key(model, effort), vals)
        st["recent"][rid] = [model, vals, effort]
        if window is not None and len(st["recent"]) > window:
            for stale in list(st["recent"])[:len(st["recent"]) - window]:
                del st["recent"][stale]
        st["last_ctx"] = (vals[IDX_IN] + vals[IDX_CW5] + vals[IDX_CW1]
                          + vals[IDX_CWX] + vals[IDX_CR])
    return st


def feed_bytes(st, data, **kw):
    """Fold a byte chunk.  Returns (state, consumed_bytes) — consumed stops at
    the last newline so a half-written trailing line is re-read next time."""
    nl = data.rfind(b"\n")
    if nl < 0:
        return st, 0
    feed_lines(st, data[:nl + 1].split(b"\n"), **kw)
    return st, nl + 1


# ------------------------------------------------------------------- reporting

def totals(st):
    """Sum every model's vector -> one 6-wide vector."""
    out = [0] * NBUCKET
    for vals in st["by_model"].values():
        for i in range(NBUCKET):
            out[i] += vals[i]
    return out


def dominant_model(st):
    """The model that produced the most tokens.  The pre-clear report used to report
    the LAST model seen, which a trailing `<synthetic>` entry (an all-zero
    usage record written on interrupt) can hijack — turning a correctly priced
    run into `cost_list=UNKNOWN`.  Ranking by token volume cannot."""
    best, best_key = "", (-1, -1)
    for model, vals in st["by_model"].items():
        key = (sum(vals), model)
        if key > best_key:
            best, best_key = model, key
    return best


def groups(st, today=None, bucket_fallback=False):
    """-> [ {model, effort, requests, in, out, cache_w5m, cache_w1h, cache_wx,
    cache_r, cost} ], one per (model, effort), largest token volume first.

    `cost` is None for a model with no price (bucket_fallback=False), exactly
    as cost() treats it.  Groups whose every counter is zero — a `<synthetic>`
    record, or a snapshot later replaced under another key — are left out."""
    out = []
    for key, vals in st.get("by_group", {}).items():
        nreq = st.get("req_by_group", {}).get(key, 0)
        if not any(vals) and not nreq:
            continue
        model, effort = split_group_key(key)
        p = price_for_model(model, today)
        if p is None and bucket_fallback:
            p = prices_for_bucket(bucket_of(model), today)
        usd = (cost_of_vals(vals, p[0], p[1], cache_read_mult(model))
               if p is not None else (0.0 if not any(vals) else None))
        out.append({"model": model, "effort": effort, "requests": nreq,
                    "in": vals[IDX_IN], "out": vals[IDX_OUT],
                    "cache_w5m": vals[IDX_CW5], "cache_w1h": vals[IDX_CW1],
                    "cache_wx": vals[IDX_CWX], "cache_r": vals[IDX_CR],
                    "cost": usd})
    out.sort(key=lambda g: (-sum(g[k] for k in ("in", "out", "cache_w5m",
                                                  "cache_w1h", "cache_wx",
                                                  "cache_r")),
                            -g["requests"], g["model"], g["effort"]))
    return out


def effort_label(grps):
    """The run's effort as one token: `xhigh` when a single level was used,
    else `xhigh:80,high:3` (requests per level, most first).  UNKNOWN when no
    record said."""
    per = {}
    for g in grps:
        if g["requests"] > 0:
            per[g["effort"]] = per.get(g["effort"], 0) + g["requests"]
    if not per:
        return EFFORT_UNKNOWN
    if len(per) == 1:
        return next(iter(per))
    ranked = sorted(per.items(),               # UNKNOWN last on a tie
                    key=lambda kv: (-kv[1], kv[0] == EFFORT_UNKNOWN, kv[0]))
    return ",".join("%s:%d" % kv for kv in ranked)


def cost(st, today=None, bucket_fallback=False):
    """-> (usd, unknown_models).

    Every model is priced with ITS OWN rate, so a mixed transcript is correct.
    A model with no entry in PRICES and no tokens is ignored (this is what
    keeps a zero-usage `<synthetic>` record from poisoning the total).  With
    tokens and no price: bucket_fallback=False -> usd is None (UNKNOWN, never
    0.0); True -> priced at its family bucket, the statusline behaviour.
    """
    total = 0.0
    unknown = []
    for model, vals in sorted(st["by_model"].items()):
        if not any(vals):
            continue
        p = price_for_model(model, today)
        if p is None:
            unknown.append(model)
            if not bucket_fallback:
                continue
            p = prices_for_bucket(bucket_of(model), today)
        total += cost_of_vals(vals, p[0], p[1], cache_read_mult(model))
    if unknown and not bucket_fallback:
        return None, unknown
    return total, unknown


def summary(st, today=None, bucket_fallback=False):
    t = totals(st)
    usd, unknown = cost(st, today, bucket_fallback)
    grps = groups(st, today, bucket_fallback)
    return {
        "model": dominant_model(st),
        "effort": effort_label(grps),
        "groups": grps,
        "models": dict(st["by_model"]),
        "requests": st["requests"],
        "in": t[IDX_IN], "out": t[IDX_OUT],
        "cache_w5m": t[IDX_CW5], "cache_w1h": t[IDX_CW1],
        "cache_wx": t[IDX_CWX], "cache_r": t[IDX_CR],
        "cost": usd, "unknown_models": unknown,
        "t0": st["t0"], "t1": st["t1"], "last_ctx": st["last_ctx"],
    }


def account_file(path, *, window=None, skip_sidechain=False,
                 assistant_only=True, today=None, bucket_fallback=False):
    """Whole-file accounting -> summary dict (see summary())."""
    st = new_state()
    with open(path, "rb") as f:
        feed_lines(st, f, window=window, skip_sidechain=skip_sidechain,
                   assistant_only=assistant_only)
    s = summary(st, today, bucket_fallback)
    s["file"] = os.path.basename(path)
    return s


def newest_transcript(dirpath, pattern="*.jsonl"):
    """Newest *.jsonl in a directory — for a pre-clear report, the session that is
    calling it is the one actively appending to its own transcript."""
    import glob
    files = glob.glob(os.path.join(dirpath, pattern))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


# --------------------------------------------------------------- AGENT RUNS
#
# THE UNIT OF AN AGENT RUN IS THE AGENT ID, NOT THE FILE.
#
# Measured 2026-09-19 over the whole project transcript directory (1,117 files,
# 1,103 distinct agent ids): 14 ids exist in TWO session directories.  The
# harness files a subagent's jsonl under the session that is current when the
# line is written, so an agent that outlives the MAIN session's `/clear` has its
# transcript cut in half and the second half filed under the NEW session uuid.
# For every one of those 14 the two parts are time-CONTIGUOUS and never overlap
# (part 1 ends before part 2 begins), which is what makes them halves of one run
# rather than two runs sharing a name.
#
# The session directory is where a run is STORED; it changes for a reason that
# has nothing to do with the agent (its parent rotated).  The agent id is what
# the harness assigns per Agent invocation and what the `…/tasks/<id>.output`
# symlink is named after — so the id is the unit, and the run is every file
# named `agent-<id>.jsonl` anywhere under the same project transcript root.
#
# THE PARTS ARE FOLDED INTO ONE STATE, NEVER ADDED UP.  Two of the 14 pairs
# (`a2b5947bced7e3a31`, `ade06766e8b17b09c`) share exactly ONE requestId across
# the seam — the request in flight when the session rotated writes a snapshot on
# each side.  Adding two summaries counts it twice; folding every part into one
# state lets the existing last-wins dedup handle the seam exactly as it handles
# a streamed request inside a single file.
#
# WHY THIS LIVES HERE (2026-09-19, package TOKFIX1).  Accounting agent
# a2c9fdde6f7fbff90 off the `.output` symlink reported $9.27 of a $20.58 run —
# a third — and said nothing about a missing part.  Under-reporting a subagent
# mechanically OVER-reports the coordinator's share of the day, which is the
# metric the owner watches.  The caller cannot be asked to remember this; the
# module that knows how to read a transcript is the one that must know what a
# whole run is.

AGENT_PREFIX = "agent-"
JSONL = ".jsonl"

def _account_dir_setting():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import khms_paths as P
    try:
        return P.soft("KHMS_ACCOUNT_DIR") or os.environ.get("KHMS_ACCOUNT_DIR") or ""
    except P.KhmsPathError:
        return os.environ.get("KHMS_ACCOUNT_DIR") or ""


# Compatibility name that callers import: the configured project transcript directory ("" when the
# deployment names none). Read once at import, like the literal it replaces.
PROJ_FALLBACK = _account_dir_setting()


def default_project_dir():
    """The project's transcript directory: KHMS_ACCOUNT_DIR (settings or environment) when it is set --
    a deployment names it once, because a hook and a subagent shell do not always agree about $HOME --
    else the agent CLI's directory for the current working directory
    (KHMS_AGENT_TRANSCRIPTS/<cwd with / . _ replaced by ->).  Callers that delegate here (a share
    report's default directory) get the same answer: one copy, one place to change."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import khms_paths as P
    named = P.soft("KHMS_ACCOUNT_DIR") or os.environ.get("KHMS_ACCOUNT_DIR") or ""
    if named:
        return named
    base = (P.soft("KHMS_AGENT_TRANSCRIPTS") or os.environ.get("KHMS_AGENT_TRANSCRIPTS")
            or os.path.join(os.environ.get("HOME") or "", ".claude", "projects"))
    return os.path.join(base, re.sub(r"[/._]", "-", os.getcwd()))


def agent_id_of(path):
    """`…/subagents/agent-a2c9….jsonl` -> `a2c9…`.  A main session transcript
    (or anything else) -> None."""
    b = os.path.basename(path or "")
    if b.startswith(AGENT_PREFIX) and b.endswith(JSONL):
        return b[len(AGENT_PREFIX):-len(JSONL)]
    return None


def project_root_of(path):
    """The project transcript directory a part belongs to, or None.

    `<project>/<session>/subagents/agent-X.jsonl` -> `<project>`, and the same
    for the nested `<project>/<session>/subagents/<x>/agent-X.jsonl` layout that
    also occurs.  Resolved through symlinks first: the caller usually hands over
    `/tmp/claude-…/<session>/tasks/<id>.output`, which is a symlink INTO the
    project directory — searching from /tmp would find nothing."""
    parts = os.path.realpath(path).split(os.sep)
    if "subagents" not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index("subagents")
    if i < 2:
        return None
    return os.sep.join(parts[:i - 1]) or os.sep


def first_timestamp(path):
    """The first `timestamp` in a transcript, or "" — used to order the parts
    of a run by TIME rather than by the session uuid, which sorts arbitrarily."""
    try:
        fh = open(path, "rb")
    except OSError:
        return ""
    with fh:
        for line in fh:
            if b'"timestamp"' not in line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            ts = isinstance(j, dict) and j.get("timestamp")
            if isinstance(ts, str) and ts:
                return ts
    return ""


def run_parts(target, roots=None):
    """Every transcript file of ONE agent run, oldest part first.

    `target` is a path to any part (a symlink to one is fine) or a bare agent
    id.  A path that is not an `agent-<id>.jsonl` — a main session transcript —
    is its own single part, because a main transcript is not split: a `/clear`
    starts a NEW session with a new uuid, which is a different run.

    `roots` are project transcript directories to search; by default the root
    the given part lives in, or `default_project_dir()` for a bare id.
    """
    import glob
    # Resolve the symlink BEFORE reading the id: the caller normally hands over
    # `…/tasks/<id>.output`, whose own basename carries neither the `agent-`
    # prefix nor `.jsonl`, and whose directory is in /tmp.
    given = os.path.realpath(target) if os.path.exists(target) else None
    aid = agent_id_of(given or target)
    if aid is None:
        if given:
            return [given]
        aid = target          # a bare agent id, nothing on disk to resolve

    if roots is None:
        root = project_root_of(given) if given else None
        roots = [root] if root else [default_project_dir()]

    found = set()
    for root in roots:
        if not root:
            continue
        for pat in (os.path.join(root, "*", "subagents",
                                 AGENT_PREFIX + aid + JSONL),
                    os.path.join(root, "*", "subagents", "*",
                                 AGENT_PREFIX + aid + JSONL)):
            found.update(os.path.realpath(p) for p in glob.glob(pat))
    if given:
        found.add(given)          # never lose the file the caller named
    return sorted(found, key=lambda p: (first_timestamp(p), p))


def account_run(target, *, roots=None, only=None, window=None,
                skip_sidechain=False, assistant_only=True, today=None,
                bucket_fallback=False):
    """Whole-RUN accounting -> summary dict, with the part bookkeeping.

    Every part is folded into ONE state, so a requestId that straddles the seam
    counts once.  `only` accounts a deliberately chosen subset (normally a
    single part) while still DISCOVERING the rest, so the summary — and
    `format_line` — can say `parts=1/2 INCOMPLETE`.  A tool that reads the same
    with the whole run and with a third of it is not a check.

    Extra keys over summary(): `parts` (one dict per part actually accounted:
    path, t0, t1, requests), `n_parts` (found), `n_parts_used`, `complete`.
    """
    parts = run_parts(target, roots)
    used = list(only) if only is not None else parts
    used = [os.path.realpath(p) if os.path.exists(p) else p for p in used]
    kw = dict(window=window, skip_sidechain=skip_sidechain,
              assistant_only=assistant_only)

    st = new_state()
    info = []
    for p in used:
        if len(used) == 1:
            with open(p, "rb") as f:
                feed_lines(st, f, **kw)
            info.append({"path": p, "t0": st["t0"], "t1": st["t1"],
                         "requests": st["requests"]})
            continue
        pst = new_state()
        with open(p, "rb") as f:
            lines = f.readlines()
        feed_lines(st, lines, **kw)
        feed_lines(pst, lines, **kw)
        info.append({"path": p, "t0": pst["t0"], "t1": pst["t1"],
                     "requests": pst["requests"]})

    s = summary(st, today, bucket_fallback)
    if len(used) > 1:
        # The span over the records actually counted, not "first line fed ..
        # last line fed": the parts are ordered by first timestamp, but taking
        # tok_account's own t0/t1 across a multi-file fold produced a reversed
        # window elsewhere (tok_share, 2026-09-18).  min/max cannot reverse.
        t0s = [i["t0"] for i in info if i["t0"]]
        t1s = [i["t1"] for i in info if i["t1"]]
        s["t0"] = min(t0s) if t0s else ""
        s["t1"] = max(t1s) if t1s else ""
    s["file"] = os.path.basename(used[0]) if used else os.path.basename(target)
    s["parts"] = info
    s["n_parts"] = len(parts)
    s["n_parts_used"] = len(used)
    s["complete"] = len(used) >= len(parts)
    return s


def format_line(s):
    """The one-line pre-clear report (a pre-clear script writes this to the
    journal and echoes it).  Format is load-bearing: TOKENS-PRE-CLEAR lines
    are grepped out of past journals."""
    if s["cost"] is None:
        cost_s = ("cost_list=UNKNOWN(model "
                  + (", ".join(s["unknown_models"]) or "?")
                  + " not in price table)")
    else:
        cost_s = "cost_list=$%.2f" % s["cost"]
    wx = (" cache_w?=" + format(s["cache_wx"], ",")) if s["cache_wx"] else ""
    t0, t1 = s["t0"], s["t1"]
    # `parts=` is APPENDED, after file=, so the fields grepped out of past
    # journals (TOKENS-PRE-CLEAR) and parsed by tok_fmt.py keep their places.
    # Absent on an account_file() summary, where a part count means nothing.
    # `effort=` (#110) goes between the two for the same reason: after every
    # parsed field, and before `parts=`, whose INCOMPLETE(...) tail has spaces.
    eff = " effort=%s" % s.get("effort", EFFORT_UNKNOWN)
    parts = ""
    if "n_parts" in s:
        parts = " parts=%d/%d" % (s["n_parts_used"], s["n_parts"])
        if not s["complete"]:
            parts += (" INCOMPLETE(%d part(s) of this agent run not counted; "
                      "run without --only-file)"
                      % (s["n_parts"] - s["n_parts_used"]))
    return ("model=%s api=%d in=%s out=%s cache_w5m=%s cache_w1h=%s%s "
            "cache_r=%s %s span=%sZ..%sZ file=%s%s%s") % (
        s["model"], s["requests"], format(s["in"], ","),
        format(s["out"], ","), format(s["cache_w5m"], ","),
        format(s["cache_w1h"], ","), wx, format(s["cache_r"], ","),
        cost_s, t0[5:16], t1[5:16], s.get("file", "?"), eff, parts)


def format_groups(s):
    """One line per (model, effort) group — the effort COLUMN.  Deliberately
    NOT in format_line's `model=… api=…` shape: tok_fmt.py sums every output
    line of that shape, and a group line in it would double-count the run."""
    out = []
    for g in s.get("groups") or []:
        cw = g["cache_w5m"] + g["cache_w1h"] + g["cache_wx"]
        usd = "UNKNOWN" if g["cost"] is None else "$%.2f" % g["cost"]
        out.append("  group %-18s effort=%-8s req=%d in=%s out=%s cache_w=%s "
                   "cache_r=%s cost=%s"
                   % (g["model"] or "?", g["effort"], g["requests"],
                      format(g["in"], ","), format(g["out"], ","),
                      format(cw, ","), format(g["cache_r"], ","), usd))
    return "\n".join(out)


def format_parts(s):
    """One line per part, with its own span and request count — printed when a
    run has more than one.  Without it the merge is invisible and a caller
    cannot tell a whole run from a third of one."""
    out = []
    n = s.get("n_parts", 1)
    for i, p in enumerate(s.get("parts") or [], 1):
        out.append("  part %d/%d: %s  %sZ..%sZ  api=%d"
                   % (i, n, p["path"], p["t0"][5:19], p["t1"][5:19],
                      p["requests"]))
    return "\n".join(out)


def main(argv=None):
    """tok_account.py [--only-file] PATH_OR_AGENT_ID ...

    Default: the WHOLE agent run, every part of it, wherever the sessions put
    them.  `--only-file` accounts just the named file and says `parts=1/N`.
    """
    import sys as _sys
    argv = list(_sys.argv[1:] if argv is None else argv)
    only_file = False
    targets = []
    for a in argv:
        if a == "--only-file":
            only_file = True
        elif a in ("-h", "--help"):
            print(main.__doc__)
            return 0
        else:
            targets.append(a)
    if not targets:
        print(main.__doc__)
        return 2
    for t in targets:
        only = [t] if only_file else None
        s = account_run(t, only=only)
        print(format_line(s))
        if len(s["groups"]) > 1:
            # More than one (model, effort) pair: the split is the point of
            # the effort column, one line each.  A single pair is already
            # fully named by model= and effort= on the line above.
            print(format_groups(s))
        if s["n_parts"] > 1:
            # When only one part was counted, list them ALL anyway — the point
            # is to show which one was left out.
            print(format_parts(s if s["complete"] else account_run(t)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
