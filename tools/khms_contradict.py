#!/usr/bin/env python3
"""KHMS contradiction detector — find cards that assert conflicting things about one subject.

WHY THIS EXISTS (measured 2026-08-31): 2295 cards, of which 2260 active, 21 challenged,
7 superseded, 7 refuted. Months of work contain far more than seven proven errors, so the
base does not have a *deletion* problem — it has a *detection* problem. Corrections are
written down and the wrong card is simply never marked, which is how one rear-camera fact
was got wrong twice, months apart, with the correction already sitting in the base.

DESIGN: two contradicting cards are almost never lexically similar — they are about the same
SUBJECT and differ in the PREDICATE. So candidate generation keys on subject (cheap,
deterministic, zero tokens) and adjudication keys on predicate (local model, batched).

Blockers, unioned:
  1 rare-key      shared identifier with document frequency in [2, DF_MAX]
  2 quantity      same subject key + same unit family + disjoint values
  3 dense         cosine top-k over the pre-built KHMS_EMBED_DIR/<slug>.npz vectors
                  (reads stored float16 vectors — numpy only, no torch, no GPU)

Shapes: quantity | identity | negation | correction-orphan | stale-value | semantic

TRUNCATION IS A FIRST-CLASS FAILURE. Every model response's prompt_eval_count is compared
against the num_ctx it was given; at or near the cap the input was silently cut and the
"no contradiction" answer is worthless. Such a batch is halved and retried, and an
unrecoverable one is reported as TRUNCATED — never as a clean result. (A truncated listing
is the exact defect that produced the rear-camera error twice; the tool that hunts it must
not commit it.)

AN INTERRUPTED RUN IS A FIRST-CLASS OUTCOME TOO (added 2026-09-20). The report is written
before the first model call and again after every batch, atomically, and says on its first
line that it is PARTIAL and how far it got. Every model call is bounded (timeout x a finite
number of attempts) and the run as a whole has a deadline; a batch that was not answered, or
never sent, is recorded under its own name (UNANSWERED / DEADLINE / LLM-ERROR / TRUNCATED) and
never as "no contradiction found". See the run-bounds block below for the measurements.

Reads only. Writes only the report path it is given. Never touches memory/know/.

Usage:
  khms_contradict.py --pairs-only                 # blocking + lexical shapes, no model
  khms_contradict.py --limit 400 --out report.md  # + model adjudication of the top 400
  khms_contradict.py --cards K-90643,K-92197,K-92072   # focused run (positive control)
  khms_contradict.py --since 2026-08-24           # incremental: touched cards x whole base
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
# Card ids in comments (K-9xxxx) are illustrative stand-ins for the reference deployment's cards
# the measurements below were made on; they are not ids of any shipped base.
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_search as ks  # noqa: E402  (reused so the pools cannot drift — see embed_index.py)

import khms_paths as P  # noqa: E402
import khms_patterns as KP  # noqa: E402
import khms_messages as KM  # noqa: E402

BRAIN = ks.BRAIN
MEM = f"{BRAIN}/memory"


def _setting(key, default):
    """A KHMS setting (env > settings file > default); the environment alone when no base resolves
    (an import for a unit test), so importing this module never needs a base."""
    try:
        return P.get(key)
    except P.KhmsNoBaseError:
        return os.environ.get(key, default)


# The LOCAL judge: an Ollama-style /api/generate endpoint (KHMS_LLM_URL; "" = none configured).
LLM_URL = _setting("KHMS_LLM_URL", "")
LLM_MODEL = _setting("KHMS_LLM_MODEL", "qwen3-coder-next:latest")

# THE JUDGE IS A SWITCH: KHMS_JUDGE=local|sonnet|haiku (settings default: sonnet).
# local = the local model above, $0, only when KHMS_LLM_URL is set. sonnet / haiku = `claude -p` (the call pattern the
# nightly sweep/consolidate stages already use), fed the IDENTICAL prompt and batch on stdin,
# with no tools, no settings sources (no hooks, no CLAUDE.md) and no MCP, so a judge call costs
# the prompt and the answer and nothing else (measured: a 1-line prompt = 443 input tokens).
# Every call's tokens and the CLI's own `total_cost_usd` are tallied in JUDGE_USAGE and carried
# in the sweep JSON's meta — the price is the CLI's, never a number typed into this file.
JUDGE_CHOICES = ("local", "sonnet", "haiku")
CLAUDE_MODELS = {"sonnet": "sonnet", "haiku": "haiku"}   # judge name -> `claude --model` alias
CLAUDE_CTX = 200000                                      # context window the CLI reports
CLAUDE_SYSTEM = ("You are a careful auditor. Follow the instructions in the user message "
                 "exactly and output only what it asks for.")


def judge_from_env(env=None) -> str:
    """The judge named by KHMS_JUDGE (env > settings > sonnet); an unknown name is refused, and so is
    `local` with no KHMS_LLM_URL -- a judge that cannot be reached is a configuration error, not a
    night of "no contradiction"."""
    if env is None:
        name = _setting("KHMS_JUDGE", "sonnet")
    else:
        name = env.get("KHMS_JUDGE") or "sonnet"
    name = name.strip().lower() or "sonnet"
    if name not in JUDGE_CHOICES:
        raise ValueError(f"KHMS_JUDGE={name!r}: not one of {', '.join(JUDGE_CHOICES)}")
    if name == "local" and not LLM_URL:
        raise ValueError("KHMS_JUDGE=local needs KHMS_LLM_URL (the local model endpoint)")
    return name


JUDGE = judge_from_env()


def resolve_agent(settings=None):
    """-> (absolute path of the agent CLI, "") or ("", one-line reason).

    The rule every driver uses (#232, khms_agent_resolve in khms_paths.sh): KHMS_AGENT is an ABSOLUTE path;
    a bare name is looked up on PATH only with KHMS_AGENT_FROM_PATH=1. Until 2026-10-12 a bare `claude` was
    resolved through PATH here and in the drivers, and under systemd that found a stale CLI whose `sonnet`
    alias served an older model (the judge's own usage line said `claude-sonnet-5` that night).
    `settings` (a dict) replaces the settings lookup in tests."""
    def val(key, default):
        return settings.get(key, default) if settings is not None else _setting(key, default)
    a = val("KHMS_AGENT", "claude") or "claude"
    if os.path.isabs(a):
        if os.path.isfile(a) and os.access(a, os.X_OK):
            return a, ""
        return "", f"AGENT UNAVAILABLE: KHMS_AGENT={a!r} is not an executable file"
    if val("KHMS_AGENT_FROM_PATH", "0") != "1":
        return "", (f"AGENT REFUSED: KHMS_AGENT={a!r} is not an absolute path; PATH would pick "
                    f"{shutil.which(a) or 'nothing'}, which can silently be another CLI and model -- set "
                    f"KHMS_AGENT=<absolute path>, or KHMS_AGENT_FROM_PATH=1 to allow the PATH lookup")
    found = shutil.which(a)
    if found:
        return found, ""
    return "", f"AGENT UNAVAILABLE: KHMS_AGENT={a!r} (KHMS_AGENT_FROM_PATH=1) is not an executable command on PATH"


# The agent CLI: KHMS_CLAUDE_BIN (the nightly's verified CLI, or a test stub), else KHMS_AGENT by the rule above.
if os.environ.get("KHMS_CLAUDE_BIN"):
    CLAUDE_BIN, CLAUDE_BIN_WHY = os.environ["KHMS_CLAUDE_BIN"], ""
else:
    CLAUDE_BIN, CLAUDE_BIN_WHY = resolve_agent()
CLAUDE_RUNNER = subprocess.run          # replaced by a stub in tests
CTX_CAP = 262144
TRUNC_RATIO = 0.92        # prompt_eval_count/num_ctx at or above this => treat as truncated

# ---------------------------------------------------------------- run bounds
# AN INTERRUPTED RUN MUST LEAVE WHAT IT ALREADY FOUND, AND A STUCK MODEL MUST GIVE UP BY ITSELF.
# Measured 2026-09-20, the night this section exists for. The nightly sweep was interrupted
# twice and left ZERO bytes both times, because the report was written only after the last
# batch returned:
#   1. 04:20:23 -> 04:47:14, rc=143. pid 2604251 sat 25 min at 0.0 % CPU, state S,
#      wchan=poll_schedule_timeout, one socket 10.0.1.54:58414 -> 10.0.1.187:11434 ESTAB.
#      The endpoint itself was healthy (`/api/tags` answered in 0.2 s) — the REQUEST was stuck.
#   2. the manual re-run was reaped by the harness's background-shell memory-pressure reaper
#      (`task_local_shell_pressure_reap`), not by the kernel: the unit's own
#      cgroup's memory.events reads `oom_kill 0`
#      while MemoryHigh=8 GiB / MemoryMax=12 GiB throttled it 72,162,288 times.
# Either way the process can vanish without warning and without a signal it can handle, so
# the ONLY defence is that the answer is already on disk. See `Checkpoint` below.
#
# WHY THESE NUMBERS. A healthy full night is 42 calls in 1474-2172 s (`logs/nightly.log`,
# 15 consecutive runs), i.e. 35-52 s per call. The per-call cap stays at its old 600 s — over
# ten times the measured mean, so no slow-but-honest call is being cut off; what was missing
# was a bound on the RUN, where 42 calls x 600 s is seven hours inside a 120 m wrapper.
LLM_TIMEOUT = float(os.environ.get("KHMS_LLM_TIMEOUT", "600"))       # seconds, per attempt
LLM_ATTEMPTS = max(1, int(os.environ.get("KHMS_LLM_ATTEMPTS", "2")))  # finite, incl. the first
RUN_DEADLINE_S = float(os.environ.get("KHMS_RUN_DEADLINE_S", "6600"))  # 110 m, under the 120 m
CHECKPOINT_EVERY = max(1, int(os.environ.get("KHMS_CHECKPOINT_EVERY", "1")))  # batches

# Named outcomes of a batch. NONE of them may reach a consumer as a clean "no contradiction":
# every one is carried as contradiction="unclear" with the outcome beside it, exactly as
# TRUNCATED already was.
OUTCOME_OK = "OK"
OUTCOME_TRUNCATED = "TRUNCATED"
OUTCOME_UNANSWERED = "UNANSWERED"   # the model did not answer inside its timeout
OUTCOME_LLM_ERROR = "LLM-ERROR"     # the endpoint refused, reset, resolved nowhere
OUTCOME_DEADLINE = "DEADLINE"       # the run's own deadline arrived before this pair's turn
OUTCOME_NO_VERDICT = "NO-VERDICT"   # answered, but said nothing about this pair
OUTCOME_BUDGET = "BUDGET"           # the judge's spend bound refused the call (#208 SONNETJUDGE)


# ------------------------------------------------------------ what "judged" means, once
# MEASURED 2026-09-20 (#149), on this tool's own output of the same day: a run that reached
# its deadline, sent 12 pairs into a hung endpoint and never sent the other 6, wrote
# `progress = {"partial": false, "adjudicated": 18, "planned": 18}` — a finished, complete,
# fully adjudicated night, in a file whose outcome histogram is `UNANSWERED 12, DEADLINE 6`
# and whose judgement count is ZERO. Two separate lies, both of them the same species:
#
#   1. `adjudicated` was `len(results)`, i.e. how many pairs were RECORDED. A DEADLINE pair is
#      appended to `results` precisely BECAUSE it was never sent — it is a note-to-self, not a
#      verdict — so the number that means "not forgotten" was published as "judged".
#   2. the closing checkpoint was written `partial=False` unconditionally. The loop did return
#      normally after the deadline branch, so "finished" was true in its own terms and said
#      nothing whatever about whether the plan was carried out.
#
# So completeness is COMPUTED here from the outcomes, never asserted by the caller, and the
# definition of "judged" lives in one function. It is the same definition the consumer
# (`khms_triage_report.judgement_census`) uses — a pair is judged only if its outcome is OK,
# a MISSING outcome is legacy-OK, and every name this file has never heard of is NOT judged,
# so an outcome invented tomorrow cannot join the clean pile by default. `test_planjudged.py`
# pins the two definitions to each other on shared fixtures. K-90585, K-90770.
def judged_count(results) -> int:
    """How many recorded pairs carry an actual model judgement."""
    return sum(1 for r in results
               if ((r.get("verdict") or {}).get("outcome") or OUTCOME_OK) == OUTCOME_OK)


def run_census(results, planned, finished):
    """The arithmetic of a run: planned = judged + no-judgement + never-recorded.

    `finished` says only whether the adjudication loop returned; it is never on its own a
    claim that the plan was carried out. COMPLETE means both: the loop ended AND every
    planned pair came back with a verdict.
    """
    recorded = len(results)
    judged = judged_count(results)
    planned = max(int(planned or 0), recorded)      # a plan below what is on disk is nonsense
    oc = Counter((r.get("verdict") or {}).get("outcome") or OUTCOME_OK for r in results)
    return {"planned": planned, "recorded": recorded, "judged": judged,
            "not_judged": recorded - judged,
            "no_record": max(0, planned - recorded),
            "outcomes": {k: v for k, v in sorted(oc.items())},
            "finished": bool(finished),
            "complete": bool(finished) and judged == planned}

DF_MIN, DF_MAX = 2, 60    # a subject key must be shared, but must not be a topic word
DF_WORD_MAX = 12          # plain-word channel: only genuinely rare nouns (see rare_words)
DENSE_K = 6               # nearest neighbours per card
MAX_PAIRS_PER_CARD = 40   # blocking blow-up guard

# ---------------------------------------------------------------- subject keys

_KEY_PATTERNS = [
    re.compile(r"\b[0-9a-f]{4}:[0-9a-f]{4}\b"),                 # usb vid:pid
    re.compile(r"\b0x[0-9a-fA-F]{2,}\b"),                       # hex addresses
    re.compile(r"/dev/[\w.+\-]+"),                              # device nodes
    re.compile(r"\b[A-Z]{2,}[0-9]{2,}[A-Z0-9]*\b"),             # AR0144 SVD48V GC2093 MD30C
    re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,4}\b"),       # snake_case identifiers
    re.compile(r"\b[a-z][a-z0-9]*(?:\.[a-z0-9_]+){1,3}\b"),     # dotted / filenames
    re.compile(r"/[a-z][a-z0-9_]*(?:/[a-z0-9_]+)+"),            # ros topics / paths
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+)+\b"),          # CamelCase
]
# Words that look like identifiers but name the machinery, not a subject of knowledge.
_KEY_STOP = {
    "src", "www", "http", "https", "e.g", "i.e", "etc", "vs", "ie", "eg",
    "derived_from", "refuted_by", "problem", "solution", "action", "outcome",
    "goal", "method", "first", "second", "third", "one_line", "one",
}


# Keys that pin a subject hard enough that a shared one is evidence on its own:
# a USB vid:pid, a device node, a hex address, a part number. Two cards that both
# talk about 26e0:ccb1 are about the same device, full stop.
_HIGH_SPEC = re.compile(r"^(?:[0-9a-f]{4}:[0-9a-f]{4}|0x[0-9a-f]{2,}|/dev/.+|"
                        r"[a-z]{2,}\d{2,}[a-z0-9]*)$")

_PLAIN_WORD = re.compile(r"\b[a-zA-Z\u00c0-\u017e]{5,}\b")
# Prose that is frequent in this base but names no subject.
_WORD_STOP = set("""
about after again against because before being below between both cannot could
during every first found further having however itself least might never other
rather should since still their there these thing those three through under until
using where which while would write wrote written measured observed reported
already always another before better cause caused change changed check checked
could every exactly finally instead itself later means never nothing only really
right second simply since something still than that then there therefore they
thing think three under until value values what when where whether which while
whole whose within without would card cards knowledge base owner session journal
""".split())


def subject_keys(card) -> set[str]:
    text = card["first"] + "\n" + card["body"]
    out = set()
    for pat in _KEY_PATTERNS:
        for m in pat.findall(text):
            k = m.strip(".").lower()
            if len(k) < 4 or k in _KEY_STOP:
                continue
            if k.isdigit():
                continue
            out.add(k)
    for t in card["tags"]:
        out.add("tag:" + t.strip().lower())
    return out


_CAPWORD = re.compile(r"\b[A-Z][a-zA-Z]{3,}\b")
_SENT_START = set(".!?:;|-*#>\"'([\n")


def proper_nouns(cards) -> set[str]:
    """Lowercased words that appear Capitalised MID-SENTENCE somewhere in the base.

    This is the one cheap signal that separates a NAME from prose in the rare-word channel,
    and the channel is otherwise blind to it because `rare_words` lowercases. Measured
    2026-08-31: `waveshare` (df 11) and `theory` / `crossing` / `evaluated` / `descriptor`
    (df 8-12) are indistinguishable by document frequency, yet `Waveshare` occurs capitalised
    inside a sentence and the others occur capitalised in NO card in the base. A correction
    landing on a name is about a thing; a correction landing on a prose word is about
    nothing in particular, and ranking the two alike buried the rear-camera finding under
    eight groups keyed on common prose words.
    """
    names = set()
    for c in cards:
        text = c["first"] + "\n" + c["body"]
        for m in _CAPWORD.finditer(text):
            i = m.start() - 1
            while i >= 0 and text[i] == " ":
                i -= 1
            if i < 0 or text[i] in _SENT_START:
                continue                      # sentence- or heading-initial: no evidence
            names.add(m.group(0).lower())
    return names


def rare_words(card) -> set[str]:
    """Plain lowercase words that can carry a subject ("waveshare", "traversability").

    The identifier patterns above are blind to unhyphenated brand and part names, which is
    exactly how K-90643 ("Waveshare Stereo USB AR0144") and K-92197 ("not the
    waveshare-branded device") failed to block together on the first build of this tool.
    Gated hard by document frequency (DF_WORD_MAX) so this stays a subject channel and does
    not degenerate into topic matching.
    """
    text = card["first"] + "\n" + card["body"]
    return {w.lower() for w in _PLAIN_WORD.findall(text)
            if w.lower() not in _WORD_STOP}


# ---------------------------------------------------------------- quantities

# canonical unit families: name -> (regex alternation, multiplier to base)
_UNITS = [
    ("deg", r"°|deg\b|degrees?\b", 1.0),
    ("len", r"mm\b", 1.0), ("len", r"cm\b", 10.0), ("len", r"m\b", 1000.0),
    ("time", r"ms\b", 1.0), ("time", r"s\b|sec\b|seconds?\b", 1000.0),
    ("time", r"min\b|minutes?\b", 60000.0),
    ("freq", r"hz\b", 1.0), ("freq", r"khz\b", 1000.0),
    ("rate", r"fps\b", 1.0),
    ("volt", r"v\b|volts?\b", 1.0),
    ("curr", r"a\b|amps?\b", 1.0),
    ("px", r"px\b|pixels?\b", 1.0),
    ("pct", r"%", 1.0),
    ("bytes", r"kb\b", 1024.0), ("bytes", r"mb\b", 1048576.0),
    # currency words are the base's own (patterns file TRIAGE_MONEY replaces the default)
    ("money", KP.value("TRIAGE_MONEY", r"czk\b|eur\b|usd\b"), 1.0),
]
_NUM = r"(\d+(?:[.,]\d+)?)"
_QPAT = [(fam, re.compile(_NUM + r"\s*(?:" + u + r")", re.I), mul) for fam, u, mul in _UNITS]
_RESPAT = re.compile(r"\b(\d{3,4})\s*[x×]\s*(\d{3,4})\b")


def quantities(card) -> dict[str, set[float]]:
    text = card["first"] + "\n" + card["body"]
    out: dict[str, set[float]] = defaultdict(set)
    for fam, pat, mul in _QPAT:
        for m in pat.finditer(text):
            try:
                v = float(m.group(1).replace(",", "."))
            except ValueError:
                continue
            out[fam].add(round(v * mul, 4))
    for m in _RESPAT.finditer(text):
        out["res"].add(float(m.group(1)) * 100000 + float(m.group(2)))
    return out


def _quant_from_text(text: str) -> dict[str, set[float]]:
    out: dict[str, set[float]] = defaultdict(set)
    for fam, pat, mul in _QPAT:
        for m in pat.finditer(text):
            try:
                out[fam].add(round(float(m.group(1).replace(",", ".")) * mul, 4))
            except ValueError:
                pass
    for m in _RESPAT.finditer(text):
        out["res"].add(float(m.group(1)) * 100000 + float(m.group(2)))
    return out


def key_local_quantities(text: str, keys, window: int = 140) -> dict[str, set[float]]:
    """Quantities appearing within `window` characters of one of the shared subject keys."""
    low = text.lower()
    out: dict[str, set[float]] = defaultdict(set)
    for k in keys[:10]:
        i = low.find(k)
        while i >= 0:
            for fam, vals in _quant_from_text(
                    text[max(0, i - window): i + len(k) + window]).items():
                out[fam] |= vals
            i = low.find(k, i + len(k))
    return out


def _disjoint(a: set[float], b: set[float], tol=0.02) -> bool:
    """True when no value in a is within tol relative of any value in b."""
    for x in a:
        for y in b:
            if x == y or (max(abs(x), abs(y)) and abs(x - y) / max(abs(x), abs(y)) <= tol):
                return False
    return True


# ---------------------------------------------------------------- lexical shapes

_CORRECTION = re.compile(
    r"\b(corrected|correction|was wrong|were wrong|turned out|in fact|actually is|"
    r"disproven|disproved|refuted|not\s+\w+\s+as (?:prior|previously|earlier)|"
    r"contrary to|previously assumed|prior assumption|earlier (?:read|reading|claim|belief)|"
    r"mis(?:read|diagnos\w+|identif\w+)|supersedes|no longer (?:true|holds)|"
    r"proved false|invalidat\w+)\b", re.I)
# The correction vocabulary splits into two grades, and the split is load-bearing.
# MEASURED 2026-08-31 over all 2295 cards: the undivided, card-global shape fired on
# 31,803 of 54,807 candidate pairs — 58 %, the MOST COMMON shape in the base, i.e. carrying
# almost no information, because its predicate was effectively a property of ONE card
# ("card A contains a correction word somewhere and card B does not"). It linked K-90643 to
# forty unrelated cards and ranked the real conflict nowhere. Three conditions make it a
# statement about the PAIR: the phrase must sit within CORR_WINDOW of a key the two cards
# SHARE (the correction must be about the shared subject), the victim must PREDATE the
# corrector (you cannot correct something written later), and the `strong` grade admits only
# phrases that can mean nothing except "an earlier claim was wrong". Result: 998 pairs from
# 85 corrector cards, and K-92197 ⇄ K-90643 — "previously assumed", key `waveshare` — is
# one of them. That pair is the rear-camera failure of 2026-08-31.
_CORRECTION_STRONG = re.compile(
    r"\b(previously assumed|prior assumption|disproven|disproved|invalidat\w+|"
    r"no longer (?:true|holds)|proved false|was wrong|were wrong|"
    r"mis(?:read|diagnos\w+|identif\w+)|not\s+\w+\s+as (?:prior|previously|earlier)|"
    r"earlier (?:read|reading|claim|belief))\b|\bSUPERSEDES\s+K-\d", re.I)
# `supersedes` as a bare word is NOT in that list on purpose: the two spec cards K-90024 and
# K-90025 document the supersedes MECHANISM, and admitting the bare word made the KHMS
# specification itself the base's single biggest "corrector", with 49 orphan victims. Only
# the convention's actual form `SUPERSEDES K-00xxx` states a correction of something.
CORR_WINDOW = 240   # chars between the correction phrase and an occurrence of a shared key
# Czech negations are LOAD-BEARING, not decoration: every card carries a trailing "CZ:"
# summary and the nightly quotes are frequently in another language. K-92197's English body says "not
# waveshare-branded" (-1) while its second-language line says the same with a native
# negation — read as +1 by an
# English-only matcher, which made the card's polarity MIXED and silenced the very
# negation shape that identifies the rear-camera conflict. The base's own negation words
# are therefore configured (patterns file TRIAGE_NEGATION), not left out.
_NEGATION = re.compile(
    r"\b(no|not|never|cannot|can't|without|lacks?|missing|absent|none|neither|"
    r"does not|doesn't|is not|isn't|zero" + KP.extra("TRIAGE_NEGATION") + r")\b",
    re.I | re.UNICODE)
# Copula match is case-insensitive on purpose: K-92072 writes "the mounted module IS the
# two-lens Waveshare stereo AR0144" in caps for emphasis, and a case-sensitive `is` missed
# the single most important identity claim in the seed set. Leading quote characters are
# skipped for the same reason (K-92197: is an unbranded "CCB Camera").
_IDENTITY = re.compile(
    r"\b(?i:is|are|was|were)\s+(?:(?i:an?|the)\s+)?"
    r"(?:(?i:unbranded|generic|two-lens|actually|really|in fact)\s+)?[\"'“„]?"
    r"([A-Z][\w\-]*(?:\s+[A-Z0-9][\w\-]*){0,3})")
_HYPOTHESIS = re.compile(
    r"\b(hypothes\w+|assum\w+|suspect\w+|expect\w+|theor\w+|conjectur\w+|"
    r"tried|attempt\w+|we looked|searched|proposed|candidate explanation|"
    r"ruled out|dead end|guess\w*|believed|thought)\b", re.I)


_PARTNUM = re.compile(r"[A-Z]{2,}\d{2,}|\d{3,}")


def identity_heads(text: str) -> set[str]:
    """Product-like heads of `X is <HEAD>` claims.

    A head qualifies only if it looks like a NAMED THING — it carries a part number
    (AR0144) or is a multi-capitalised phrase ("CCB Camera"). Without that filter the
    pattern also harvests "is the same", "was The first", and the identity shape becomes
    noise instead of signal.
    """
    heads = set()
    for m in _IDENTITY.finditer(text):
        h = m.group(1).strip(" \"'“„")
        if len(h) < 3:
            continue
        caps = len(re.findall(r"\b[A-Z][\w\-]*", h))
        if _PARTNUM.search(h) or caps >= 2:
            heads.add(h)
    return heads


def correction_near(text: str, subj_keys) -> tuple:
    """(match, key) for a correction phrase sitting within CORR_WINDOW of a SHARED key.

    A correction that is nowhere near the subject the two cards have in common is a
    correction about something else. Without this the shape is a property of one card and
    fires on 58 % of all candidate pairs; with it, 18 %.
    """
    low = text.lower()
    for m in _CORRECTION.finditer(text):
        for k in subj_keys[:12]:
            j = low.find(k)
            while j >= 0:
                if abs(j - m.start()) <= CORR_WINDOW:
                    return m, k
                j = low.find(k, j + len(k))
    return None, None


def negation_polarity(text: str, key: str) -> int | None:
    """-1 if the sentence carrying `key` is negated, +1 if plainly asserted, None if absent."""
    pol = None
    for sent in re.split(r"(?<=[.;:!?])\s+|\n", text):
        if key not in sent.lower():
            continue
        p = -1 if _NEGATION.search(sent) else 1
        pol = p if pol is None else (pol if pol == p else 0)
    return pol


SUBJECT_LIKE = ("high-spec", "identifier", "name")


def default_key_class(k: str) -> str:
    return "high-spec" if _HIGH_SPEC.match(k) else "prose"


def lexical_shapes(a, b, shared_keys, key_class=default_key_class) -> list[tuple[str, float, str]]:
    """(shape, weight, evidence) triples for a candidate pair.

    `key_class` maps a subject key to high-spec | identifier | name | prose. It is what
    decides whether a correction-orphan is a finding about a THING or a coincidence of
    English prose; see `proper_nouns`.
    """
    out = []
    ta, tb = a["first"] + "\n" + a["body"], b["first"] + "\n" + b["body"]
    # QUANTITIES ARE COMPARED ONLY NEAR A SHARED SUBJECT KEY. Comparing the whole card's
    # numbers fired on any two cards that happen to mention a length, which made the shape
    # useless as a ranking signal (61k "shaped" pairs on the first full sweep). A number
    # only contradicts another number when both are about the same thing.
    # Deterministic order, most subject-pinning key first: a vid:pid or a /dev node beats an
    # ordinary identifier. The old code iterated a SET, so which keys were examined at all
    # (the `[:12]` slice below) varied between runs on the same input.
    subj = sorted((k for k in shared_keys if not k.startswith("tag:")),
                  key=lambda k: (0 if _HIGH_SPEC.match(k) else 1, k))
    qa, qb = key_local_quantities(ta, subj), key_local_quantities(tb, subj)
    for fam in set(qa) & set(qb):
        if _disjoint(qa[fam], qb[fam]):
            out.append(("quantity", 3.0,
                        f"{fam} near {subj[:3]}: {sorted(qa[fam])[:4]} vs {sorted(qb[fam])[:4]}"))
    ida, idb = identity_heads(ta), identity_heads(tb)
    if ida and idb and not (ida & idb):
        out.append(("identity", 2.0,
                    f"{sorted(ida)[:3]} vs {sorted(idb)[:3]}"))
    for k in subj[:12]:
        pa, pb = negation_polarity(ta, k), negation_polarity(tb, k)
        if pa and pb and pa == -pb:
            out.append(("negation", 3.0, f"'{k}' asserted {pa:+d} vs {pb:+d}"))
            break
    ca, ka = correction_near(ta, subj)
    cb, kb = correction_near(tb, subj)
    if bool(ca) != bool(cb):
        corrector, victim, m, key = (a, b, ca, ka) if ca else (b, a, cb, kb)
        lk = corrector["links"]
        linked = (victim["id"] in lk.get("contradicts", []) or
                  victim["id"] in lk.get("refuted_by", []) or
                  lk.get("supersedes") == victim["id"] or
                  victim["id"] in (lk.get("supersedes") or ""))
        # A card cannot correct a card written after it. Cheap, and it halves the shape.
        older = (victim.get("date") or "") <= (corrector.get("date") or "￿")
        if not linked and victim["status"] == "active" and older:
            strong = bool(_CORRECTION_STRONG.search(m.group(0)))
            kc_ = key_class(key)
            # Both halves must hold for the top weight: a phrase that can only mean "an
            # earlier claim was wrong", landing on a key that names a THING.
            w = 4.0 if (strong and kc_ in SUBJECT_LIKE) else (2.5 if strong else 2.0)
            out.append(("correction-orphan", w,
                        f"{corrector['id']} corrects ('{m.group(0)}') about '{key}' "
                        f"[key_class={kc_}] but {victim['id']} "
                        f"({victim.get('date') or '?'}) still active and unlinked"
                        + ("" if strong else "  [weak correction phrase]")))
    return out


# ------------------------------------------------- provenance: doc vs machine
# CONFIRMED FIVE TIMES IN ONE DAY (2026-08-31): a document describing an INTENTION or a
# CATALOGUE was read as a record of the machine. The tyre (vendor page vs fitted), the mux
# default (config comment vs running parameter), the safety chain (spec vs wiring), the rear
# camera's role (project doc vs the owner's actual requirement), the CAD geometry (drawing vs
# tape). The machine won every time. These are not contradictions today — there is no second
# card to conflict with. They are next month's contradictions, and they are findable NOW from
# provenance alone: `evidence: reported`, a documentary source, and a claim that a tape
# measure or a register read could settle.
_DOC_SOURCE = re.compile(
    r"(datasheet|vendor|product page|retail|manual|spec[s]?\.md|catalog|"
    r"docs?/|\.xacro|\.yaml|\.yml|config|README|drawing|CAD|design doc|plan[s]?/|"
    r"web|http)", re.I)
_CHECKABLE = ("len", "deg", "volt", "curr", "rate", "res", "freq", "px")


def provenance_flags(cards):
    """Cards asserting machine-checkable numbers on documentary evidence, ranked."""
    out = []
    for c in cards:
        if (c.get("evidence") or "").strip() != "reported":
            continue
        if not _DOC_SOURCE.search(c.get("source", "") or ""):
            continue
        q = quantities(c)
        n = sum(len(q[f]) for f in _CHECKABLE if f in q)
        if n < 2:
            continue
        out.append({"id": c["id"], "checkable_values": n,
                    "families": sorted(f for f in _CHECKABLE if f in q),
                    "source": (c.get("source") or "")[:150],
                    "first": c["first"][:160]})
    out.sort(key=lambda r: -r["checkable_values"])
    return out


# ---------------------------------------------------------------- blocking

def build_candidates(cards, focus_ids=None, dense_k=DENSE_K, verbose=True):
    by_id = {c["id"]: c for c in cards}
    keys = {c["id"]: subject_keys(c) for c in cards}
    words = {c["id"]: rare_words(c) for c in cards}
    df, dfw = Counter(), Counter()
    for ks_ in keys.values():
        df.update(ks_)
    for ws_ in words.values():
        dfw.update(ws_)
    inv = defaultdict(list)
    for cid, ks_ in keys.items():
        for k in ks_:
            if DF_MIN <= df[k] <= DF_MAX:
                inv[k].append(cid)
    invw = defaultdict(list)
    for cid, ws_ in words.items():
        for w in ws_:
            if DF_MIN <= dfw[w] <= DF_WORD_MAX:
                invw[w].append(cid)
    # A card's full key set for pair annotation = identifiers + its rare words.
    allkeys = {cid: keys[cid] | {w for w in words[cid]
                                 if DF_MIN <= dfw[w] <= DF_WORD_MAX} for cid in keys}
    ident = set(df)                       # everything the identifier patterns produced
    names = proper_nouns(cards)           # rare words that are Names, not prose

    def key_class(k: str) -> str:
        if _HIGH_SPEC.match(k):
            return "high-spec"
        if k in ident:
            return "identifier"
        if k in names:
            return "name"
        return "prose"

    pairs: dict[tuple[str, str], dict] = {}

    def add(x, y, why, w, keyset):
        if x == y:
            return
        p = (x, y) if x < y else (y, x)
        e = pairs.setdefault(p, {"score": 0.0, "why": set(), "keys": set(), "hispec": False})
        e["score"] += w
        e["why"].add(why)
        e["keys"] |= keyset
        if any(_HIGH_SPEC.match(k) for k in keyset):
            e["hispec"] = True

    # 1 rare-key blocking; rarer key => stronger evidence of a shared subject
    for k, ids in inv.items():
        if len(ids) > DF_MAX:
            continue
        w = 1.0 / math.log2(len(ids) + 2)
        if _HIGH_SPEC.match(k):
            w *= 3.0        # a shared vid:pid or device node pins the subject on its own
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                add(ids[i], ids[j], "rare-key", w, {k})
    if verbose:
        print(f"  rare-key blocking: {len(inv)} keys -> {len(pairs)} pairs", file=sys.stderr)

    # 1b rare-WORD blocking — brand and part names the identifier patterns cannot see
    for k, ids in invw.items():
        w = 0.6 / math.log2(len(ids) + 2)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                add(ids[i], ids[j], "rare-word", w, {k})
    if verbose:
        print(f"  + rare-word: {len(invw)} words -> {len(pairs)} pairs", file=sys.stderr)

    # 2 dense neighbours from the pre-built index
    n_dense = 0
    try:
        import numpy as np
        slug = os.environ.get("KHMS_EMBED_SLUG", "bge")
        # where the path resolver keeps the index (KHMS_EMBED_DIR; moved out of memory/.embed at #173)
        z = np.load(os.path.join(P.EMBED_DIR, f"{slug}.npz"), allow_pickle=True)
        ids = [str(i) for i in z["ids"]]
        keep = [i for i, cid in enumerate(ids) if cid in by_id]
        V = z["vectors"][keep].astype("float32")
        ids = [ids[i] for i in keep]
        V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
        B = 256
        for s in range(0, len(ids), B):
            sim = V[s:s + B] @ V.T
            for r in range(sim.shape[0]):
                sim[r, s + r] = -1
                top = np.argpartition(-sim[r], dense_k)[:dense_k]
                for t in top:
                    if sim[r, t] < 0.55:
                        continue
                    add(ids[s + r], ids[t], "dense", float(sim[r, t]),
                        allkeys[ids[s + r]] & allkeys[ids[t]])
                    n_dense += 1
    except Exception as e:  # index absent or numpy missing — lexical channel still works
        print(f"  dense channel unavailable ({e.__class__.__name__}: {e})", file=sys.stderr)
    if verbose:
        print(f"  + dense: {n_dense} edges -> {len(pairs)} pairs", file=sys.stderr)

    # 3 shapes, on the blocked pairs only
    out = []
    for (x, y), e in pairs.items():
        # `is not None`, not truthiness (#208 PD): an EMPTY focus is an incremental window that
        # holds no card, so no pair touches it. Read as falsy it silently became a FULL sweep
        # (2026-10-03, 2026-10-05: 0 cards in the window -> 53k pairs, morning file still `--since`).
        if focus_ids is not None and x not in focus_ids and y not in focus_ids:
            continue
        a, b = by_id[x], by_id[y]
        shapes = lexical_shapes(a, b, e["keys"], key_class=key_class)
        # RETENTION. A pair survives if a shape fired, or the blocking score is high, or the
        # two cards share a high-specificity key (vid:pid, /dev node, part number). The last
        # clause is load-bearing: K-92072 and K-92197 both name 26e0:ccb1 and nothing else,
        # and the first build of this tool dropped that pair for having "no shape and a low
        # score" — i.e. it dropped one third of the very trio it was written to find.
        if not shapes and e["score"] < 0.9 and not e["hispec"]:
            continue
        # ONE VOTE PER SHAPE FAMILY. The first build summed every shape INSTANCE, so a pair
        # that merely happened to mention three different units near a shared key collected
        # 9.0 of bonus while a pair with a single genuine negation collected 3.0. Measured
        # on the 2026-08-31 sweep: the adjudication cut fell at score 11.469 and the head of
        # the ranking was owned by number-rich hub cards, which is how K-90643 ⇄ K-92197
        # (one negation, score 3.786) ended up at global rank 17,813 of 54,807 and was never
        # sent to the model at all. A second hint of the same kind is not a second finding.
        fam: dict[str, float] = {}
        for s, w, _ in shapes:
            fam[s] = max(fam.get(s, 0.0), w)
        score = e["score"] + sum(fam.values())
        out.append({
            "a": x, "b": y, "score": round(score, 3),
            "blockers": sorted(e["why"]),
            "shapes": [{"shape": s, "evidence": ev} for s, _, ev in shapes],
            "keys": sorted(k for k in e["keys"] if not k.startswith("tag:"))[:8],
        })
    out.sort(key=lambda p: -p["score"])

    # Blow-up guard: no single card may flood the adjudication queue. SHAPE-BEARING PAIRS
    # ARE EXEMPT — the first build capped by score alone and evicted K-90643 ⇄ K-92197,
    # a pair with a real negation shape, because K-90643 is a hub card with 40 higher-scoring
    # but shapeless neighbours. A quota that throws away the evidence and keeps the noise is
    # worse than no quota.
    seen = Counter()
    capped = []
    for p in out:
        if p["shapes"]:
            seen[p["a"]] += 1
            seen[p["b"]] += 1
            capped.append(p)
            continue
        if seen[p["a"]] >= MAX_PAIRS_PER_CARD or seen[p["b"]] >= MAX_PAIRS_PER_CARD:
            continue
        seen[p["a"]] += 1
        seen[p["b"]] += 1
        capped.append(p)
    if verbose:
        print(f"  shaped+capped: {len(capped)} candidate pairs", file=sys.stderr)
    return capped


# ------------------------------------------------- adjudication budget + accounting
# WHY THIS IS NOT `pairs[:limit]` ANY MORE (measured 2026-08-31): a single global rank cut
# lets one shape family own the whole budget. The 2026-08-31 sweep adjudicated 132 of 54,807
# candidates by score alone; the two pairs of the rear-camera trio the tool was WRITTEN to
# find sat at rank 11,118 and 17,813, were never sent to the model, therefore never got a
# verdict, therefore could not appear in the proposal a human reads. Nothing filtered them —
# they never arrived. A reserved quota per shape class is a diversity guarantee, not a
# threshold: each class is still consumed in rank order, and the leftover budget is still
# filled globally by rank.
SHAPE_QUOTA = [
    ("correction-orphan-strong", 0.40),   # the sharpest shape in the package — see below
    ("identity", 0.10),
    ("negation-high-spec", 0.15),
    ("quantity", 0.15),
]


def shape_classes(p) -> set[str]:
    """Budget classes a candidate pair belongs to."""
    out = set()
    for s in p["shapes"]:
        if s["shape"] == "correction-orphan":
            m = re.search(r"key_class=(\w[\w-]*)", s["evidence"])
            subj_like = bool(m) and m.group(1) in SUBJECT_LIKE
            out.add("correction-orphan-strong"
                    if ("[weak correction phrase]" not in s["evidence"] and subj_like)
                    else "correction-orphan-weak")
        elif s["shape"] == "negation":
            key = s["evidence"].split("'")[1] if "'" in s["evidence"] else ""
            out.add("negation-high-spec" if _HIGH_SPEC.match(key) else "negation")
        else:
            out.add(s["shape"])
    return out or {"none"}


def select_for_adjudication(pairs, limit):
    """(todo, accounting). `pairs` must already be sorted by score, best first."""
    picked, idx = [], set()
    for cls, frac in SHAPE_QUOTA:
        quota = int(round(limit * frac))
        n = 0
        for i, p in enumerate(pairs):
            if n >= quota:
                break
            if i in idx or cls not in shape_classes(p):
                continue
            idx.add(i)
            n += 1
    for i, p in enumerate(pairs):          # remainder by global rank
        if len(idx) >= limit:
            break
        idx.add(i)
    picked = [pairs[i] for i in sorted(idx)][:limit]
    left = Counter()
    for i, p in enumerate(pairs):
        if i not in idx:
            for c in shape_classes(p):
                left[c] += 1
    acc = {
        "candidate_pairs": len(pairs),
        "adjudicated": len(picked),
        "not_adjudicated": len(pairs) - len(picked),
        "reason": f"LLM budget: --limit {limit} pairs per run "
                  f"(reserved quotas {SHAPE_QUOTA}); the rest are ranked but not sent to "
                  f"the model. They are NOT judged to be non-contradictions.",
        "not_adjudicated_by_shape": dict(left.most_common()),
        "quota_used": {cls: sum(1 for p in picked if cls in shape_classes(p))
                       for cls, _ in SHAPE_QUOTA},
    }
    return picked, acc


# ------------------------------------------------- do not re-judge last nights' pairs
# WHY (#208 P208F, 2026-10-05): the owner made every night a FULL sweep. The candidate set and its
# ranking are a deterministic function of the base, so consecutive full nights pick nearly the
# same top `--limit` pairs: measured on the staging JSONs, 10-03 ⇄ 10-04 shared 220 of 250 pairs
# and 10-04 ⇄ 10-05 215 of 250. A re-judged unchanged pair returns the same verdict (13 of 13
# identical across 10-02/10-03, handoff-PD.md), so the repeat buys nothing and the budget never
# reaches rank 251+. A pair JUDGED (outcome OK — the one definition, `judged_count`) in one of the
# last N nights' sweep JSONs is skipped, UNLESS one of its two cards changed after that night's
# run started (then the old verdict is about other text and the pair is eligible again). Pairs
# that were planned and never judged (DEADLINE, BUDGET, LLM-ERROR …) are NOT skipped. This
# night's own files are ignored, so a rerun of the same night or the qwen fallback plans the same
# pairs again.
# NAMED BY NIGHT KEY (#237): a sweep JSON is `triage-<night key>-sweep[.<judge>].json`, the key being
# DATE for the first night of a date and DATE-<n> for a later one (nightly_distill.sh claims it). Until
# then the skip read only the files of EARLIER DATES, so a second night of one date re-judged the
# pairs the date's first night had judged, and a DATE-<n> file matched no pattern at all.
# Now every OTHER night of tonight's date counts like an earlier date; only `night_key`'s own are ignored.
_SWEEP_FILE = re.compile(r"^triage-((\d{4}-\d{2}-\d{2})(?:-\d+)?)-sweep(?:\.[\w-]+)?\.json$")


def recently_judged(staging, nights, today, mtime_of, night_key=None):
    """{(a, b) sorted: judged-at epoch} for pairs judged OK in the sweep JSONs of the `nights`
    nights before `today` (ISO date) and of every night of `today` other than `night_key` (default:
    `today`, the date's first night) in `staging`; `mtime_of(card_id)` -> epoch or None.
    Returns (skip, accounting)."""
    import datetime
    t = datetime.date.fromisoformat(today)
    lo = (t - datetime.timedelta(days=nights)).isoformat()
    own = night_key or today
    files, judged, unreadable = [], {}, []
    for name in sorted(os.listdir(staging)) if os.path.isdir(staging) else []:
        m = _SWEEP_FILE.match(name)
        if not m or m.group(1) == own or not (lo <= m.group(2) <= today):
            continue
        path = os.path.join(staging, name)
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception as e:                              # noqa: BLE001
            unreadable.append(f"{name}: {type(e).__name__}")
            continue
        rows = d["results"] if isinstance(d, dict) else d
        prog = (d.get("meta") or {}).get("progress") or {} if isinstance(d, dict) else {}
        try:
            when = datetime.datetime.fromisoformat(prog["started"]).timestamp()
        except Exception:                                   # noqa: BLE001  (older JSONs)
            when = os.path.getmtime(path) - (RUN_DEADLINE_S + 600)
        files.append(name)
        for r in rows:
            if ((r.get("verdict") or {}).get("outcome") or OUTCOME_OK) != OUTCOME_OK:
                continue
            k = tuple(sorted((r["a"], r["b"])))
            judged[k] = max(judged.get(k, 0), when)
    skip, changed = set(), 0
    for k, when in judged.items():
        mt = [mtime_of(x) for x in k]
        if any(m is None or m >= when for m in mt):
            changed += 1                                    # a card changed (or is gone)
            continue
        skip.add(k)
    return skip, {"nights": nights, "files": files, "unreadable": unreadable,
                  "judged_in_window": len(judged), "card_changed_since": changed}


def skip_recently_judged(pairs, skip):
    """(kept, n_skipped), order preserved."""
    kept = [p for p in pairs if tuple(sorted((p["a"], p["b"]))) not in skip]
    return kept, len(pairs) - len(kept)


def gate_before_model(pairs, by_id, rules=None):
    """(kept, accounting). Drop a candidate pair the triage gate (khms_triage_gate.pair_gate)
    refuses in BOTH orientations — whichever card the model would call wrong, the row it
    produced would be refused after the model, so the budget goes to another pair instead.
    Added 2026-10-01 (issue #208). Order is preserved; nothing here is silent: the counts by
    rule travel in the run's meta and are printed."""
    import khms_triage_gate as G          # local: khms_triage_gate imports this module
    # EVERY refused pair keeps its identity and its rules (`refused_pairs`), not
    # just a count: a pair two decision-type cards make is a SUPERSESSION candidate the morning
    # file lists, and any other refusal must be re-checkable from the sweep JSON (Diblin's
    # review of #208, P2 #4/#5, 2026-10-01).
    kept, by_rule, examples, refused = [], Counter(), [], []
    for p in pairs:
        a, b = by_id.get(p["a"]), by_id.get(p["b"])
        hits = G.pair_gate(a, b, rules=rules) if (a and b) else []
        if hits:
            by_rule[hits[0][0]] += 1
            if len(examples) < 20:
                examples.append(f"{p['a']} ⇄ {p['b']}: {hits[0][1][:160]}")
            # compact [a, b, [rules]]: thousands of pairs per night, rewritten at every
            # checkpoint — the rule names are the reason, the ids find the cards
            refused.append([p["a"], p["b"], sorted({n for n, _ in hits})])
        else:
            kept.append(p)
    acc = {"refused": len(pairs) - len(kept), "of": len(pairs),
           "rules_in_force": G.enabled_rules(rules), "by_rule": dict(by_rule),
           "examples": examples, "refused_pairs": refused}
    return kept, acc


# ---------------------------------------------- deterministic findings (zero tokens)
# The detector computes tens of thousands of deterministic facts every night and, before
# this, threw away every one that the LLM budget did not reach. A STRONG correction-orphan
# needs no model to be worth a reviewer's eye: it says a card in the base records that an
# earlier claim was wrong, and the card holding that claim is still `active` and carries no
# link to its correction. That is the exact failure of 2026-08-31 (K-92197 corrected the
# rear-camera identity "previously assumed"; K-90643 stood unmarked and was believed again
# months later). Surfacing it costs nothing and does not compete for the model budget.

_CLASS_RANK = {"high-spec": 0, "identifier": 1, "name": 2}


def deterministic_findings(pairs, cap=60):
    """Strong correction-orphans, grouped by the card doing the correcting.

    RANKED, NOT CUT (K-90024 P4). Order is the specificity of the subject the correction
    lands on — a USB vid:pid or a `/dev` node first, then an identifier, then a proper name —
    and within a class by the pair score. Correction phrases landing on a prose word are not
    findings and are counted, not listed.

    An earlier build of this function ranked by "fewest orphans first" on the theory that a
    real correction leaves one or two cards standing, and hid everything above six behind a
    "diffuse" label. That threshold hid the exact case this whole package exists for:
    K-92197 corrected the rear-camera identity and leaves EIGHT cards keyed on `waveshare`
    standing unlinked, K-90643 among them. A big orphan set is the finding, not the noise.
    """
    groups: dict[str, dict] = {}
    n_prose = 0
    for p in pairs:
        for s in p["shapes"]:
            if s["shape"] != "correction-orphan" or "[weak" in s["evidence"]:
                continue
            kcm = re.search(r"key_class=(\w[\w-]*)", s["evidence"])
            if not kcm or kcm.group(1) not in SUBJECT_LIKE:
                n_prose += 1        # correction landed on a prose word, not on a subject
                continue
            corrector = s["evidence"].split(" ", 1)[0]
            victim = re.search(r"but (K-\d+)", s["evidence"])
            phrase = re.search(r"corrects \('([^']*)'\) about '([^']*)'", s["evidence"])
            g = groups.setdefault(corrector, {"corrector": corrector, "victims": [],
                                              "phrases": set(), "keys": set(),
                                              "top_score": 0.0, "best_class": "name"})
            if victim:
                g["victims"].append(victim.group(1))
            if phrase:
                g["phrases"].add(phrase.group(1))
                g["keys"].add(phrase.group(2))
            if _CLASS_RANK[kcm.group(1)] < _CLASS_RANK[g["best_class"]]:
                g["best_class"] = kcm.group(1)
            g["top_score"] = max(g["top_score"], p["score"])
    out = []
    for g in groups.values():
        g["victims"] = sorted(set(g["victims"]))
        g["phrases"] = sorted(g["phrases"])
        g["keys"] = sorted(g["keys"])
        out.append(g)
    out.sort(key=lambda g: (_CLASS_RANK[g["best_class"]], -g["top_score"], g["corrector"]))
    return {"total_groups": len(out), "shown": min(cap, len(out)),
            "not_shown": max(0, len(out) - cap),
            "not_shown_reason": f"render cap {cap}, lowest-specificity subjects first to go",
            "prose_key_pairs_skipped": n_prose,
            "groups": out[:cap]}


# ---------------------------------------------------------------- model adjudication

def is_timeout(e: BaseException) -> bool:
    """True when this exception is 'the endpoint did not answer in time'.

    A socket timeout reaches the caller in three different disguises depending on where in
    the request it fires — bare `TimeoutError`/`socket.timeout`, or wrapped in a `URLError`
    whose `.reason` is one of those. Reading only the outer class is how a timeout gets
    filed as a generic error and then read as 'the base is clean'.
    """
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(e, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError))


def _post_qwen(prompt, timeout=None):
    timeout = LLM_TIMEOUT if timeout is None else timeout
    num_ctx = int(min(CTX_CAP, len(prompt) / 2.5 + 2048))
    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps({"model": LLM_MODEL, "prompt": prompt, "stream": False,
                         "options": {"num_ctx": num_ctx, "temperature": 0.1}}).encode(),
        headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=timeout))
    pec = int(r.get("prompt_eval_count") or 0)
    # THE TRUNCATION CHECK. A prompt_eval_count at or near num_ctx means the tail of the
    # input was dropped before the model ever saw it, and every "no contradiction" in the
    # answer is then a statement about text that was never read.
    return r.get("response", ""), pec, num_ctx, pec >= TRUNC_RATIO * num_ctx


class ClaudeJudgeError(OSError):
    """`claude -p` exited nonzero, said is_error, or printed no JSON. An OSError so the
    adjudication loop's bounded retry treats it like any other endpoint failure."""


_USAGE_LOCK = threading.Lock()
JUDGE_USAGE: dict = {}


def reset_judge_usage():
    with _USAGE_LOCK:
        JUDGE_USAGE.clear()
        JUDGE_USAGE.update({"calls": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                            "thinking_tokens": 0, "cli_cost_usd": 0.0, "wall_s": 0.0,
                            "models": set()})


reset_judge_usage()


def judge_usage() -> dict:
    """A snapshot of what the Claude judge has spent in this process (zeros for qwen)."""
    with _USAGE_LOCK:
        u = dict(JUDGE_USAGE)
    u["models"] = sorted(u["models"])
    u["judge"] = JUDGE
    u["cli_cost_usd"] = round(u["cli_cost_usd"], 6)
    u["wall_s"] = round(u["wall_s"], 1)
    return u


def _account(r, ok, wall):
    u = r.get("usage") or {}
    with _USAGE_LOCK:
        JUDGE_USAGE["calls" if ok else "errors"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens"):
            JUDGE_USAGE[k] += int(u.get(k) or 0)
        JUDGE_USAGE["thinking_tokens"] += int(
            (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
        JUDGE_USAGE["cli_cost_usd"] += float(r.get("total_cost_usd") or 0.0)
        JUDGE_USAGE["wall_s"] += wall
        JUDGE_USAGE["models"].update(
            (m.get("canonicalModel") or k) for k, m in (r.get("modelUsage") or {}).items())


# ------------------------------------------------- the paid judge's spend bound (#208)
# SONNETJUDGE (2026-10-01): the operator made Sonnet the nightly judge of record ("use
# sonnet", chat 2026-10-01; a daily spend it accepted). A paid judge gets the bound idea of the
# shadow (tools/triage_shadow.py, BUDGET), not its code: an ESTIMATED bound, not a guaranteed
# hard cap. Before EVERY attempt (first try and retry) its estimated worst case is reserved:
# (prompt bytes + system bytes + JUDGE_IN_TOKEN_ALLOWANCE) input tokens at the dearest input
# rate (1-hour cache write) + JUDGE_OUT_TOKEN_CAP output tokens at the output rate, prices from
# tools/tok_account.py (none typed here). The attempt starts only if what the night already
# charged (the ledger: every run of the date) + this run's charges + the estimate stay within
# max_usd. A known cost (the CLI's total_cost_usd) replaces the reservation; an UNKNOWN cost
# (timeout, killed CLI, no JSON) keeps the whole reservation charged. An attempt that cost
# more than its estimate is a BREACH: recorded, and no paid call follows it in this night
# (a later run of the same date reads it from the ledger). Pairs the bound kept from the model
# are OUTCOME_BUDGET — never sent, never a "no contradiction". The assumptions, none proven
# for a whole `claude -p` invocation, are the shadow's: the CLI adds at most the allowance;
# CLAUDE_CODE_MAX_OUTPUT_TOKENS bounds the whole invocation's output; prices in tok_account
# are the ones in force; a breach is seen only after its attempt returned.
JUDGE_OUT_TOKEN_CAP = 32000         # CLAUDE_CODE_MAX_OUTPUT_TOKENS for every judge call
JUDGE_IN_TOKEN_ALLOWANCE = 16000    # tokens the CLI may add beyond prompt + system bytes
# judge name -> the model id its `--model` alias bills as. sonnet = claude-sonnet-5-5, measured
# (TRIAGESONNET modelUsage). haiku's id is NOT measured: under a bound its price is unknown and
# every attempt is refused (an unknown price is never $0).
CLAUDE_MODEL_IDS = {"sonnet": "claude-sonnet-5-5"}
JUDGE_BUDGET = None                 # a JudgeBudget, or None = no bound (the evaluators)


class JudgeBudgetStop(Exception):
    """The bound refused an attempt. Deliberately NOT an OSError: the retry loop must not
    read it as a failed call and retry it."""


USAGE_LIMIT_RE = re.compile(r"hit your [a-z0-9 -]*limit|usage limit reached", re.I)


class JudgeUsageLimit(JudgeBudgetStop):
    """An account limit is terminal for this run, never a transient retry."""


class JudgeModelUnpriced(ValueError):
    """The judge model has no row in tok_account's price table, so no attempt can be bounded.
    Deliberately NOT a JudgeBudgetStop: on 2026-10-11 this gap was reported as `outcomes
    {'BUDGET': 250}` at $0 spent -- a missing price read as an exhausted budget. It names the model."""


class JudgeBudget:
    """Per-night estimated spend bound of the Claude judge (see the block comment above)."""

    def __init__(self, max_usd, ledger=None, model_id=None, estimate=None):
        self.max_usd = float(max_usd)
        self.ledger = ledger
        self.model_id = model_id
        if estimate is None:
            self.price()                    # an unpriced judge fails HERE, by name, before any pair
        self.estimate = estimate or self.worst_case_usd
        self.known = self.unknown = self.inflight = 0.0
        self.attempts = self.refused = 0
        self.breach = None
        self.stop = None
        self.lock = threading.Lock()
        self.run = f"{os.getpid()}@{time.strftime('%Y-%m-%dT%H:%M:%S')}"
        self.spent_before, self.ledger_lines = self._read_ledger()

    def _read_ledger(self):
        if not self.ledger or not os.path.exists(self.ledger):
            return 0.0, 0
        tot, n = 0.0, 0
        for k, ln in enumerate(open(self.ledger, encoding="utf-8"), 1):
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
                tot += float(e["charged_usd"])
            except (ValueError, KeyError, TypeError):
                self.stop = (f"BUDGET: spend ledger {self.ledger} line {k} unreadable — what "
                             f"the night spent is unknown, so no paid call starts")
                continue
            n += 1
            if e.get("breach") and not self.stop:
                self.stop = (f"BREACH earlier this night (ledger {self.ledger} line {k}: "
                             f"{e.get('breach')}) — no paid call starts")
        return tot, n

    def model(self):
        return self.model_id or CLAUDE_MODEL_IDS.get(JUDGE)

    def price(self):
        """(input, output) USD/MTok of the judge model from tok_account.PRICES -- the one price table
        every accounting tool reads. No model or no row -> JudgeModelUnpriced naming the model."""
        import tok_account as T
        model = self.model()
        pr = T.price_for_model(model) if model else None
        if pr is None:
            raise JudgeModelUnpriced(
                f"judge model {model or '(none: judge ' + repr(JUDGE) + ' has no CLAUDE_MODEL_IDS entry)'!s} "
                f"has no price in tok_account.PRICES ({T.__file__}) — the judge bound cannot estimate an "
                f"attempt, so no paid call starts; add the model's row from the official price list")
        return pr

    def worst_case_usd(self, prompt):
        """The most one attempt is ESTIMATED to cost (JudgeModelUnpriced when the model has no price)."""
        import tok_account as T
        pin, pout = self.price()
        n_in = (len(prompt.encode("utf-8")) + len(CLAUDE_SYSTEM.encode("utf-8"))
                + JUDGE_IN_TOKEN_ALLOWANCE)
        mult = max(T.MULT_CW1, T.MULT_CW5, T.MULT_CWX, 1.0)
        return (n_in * pin * mult + JUDGE_OUT_TOKEN_CAP * pout) / 1e6

    def charged(self):
        return self.spent_before + self.known + self.unknown + self.inflight

    def reserve(self, prompt):
        w = self.estimate(prompt)
        with self.lock:
            if self.stop is None and self.charged() + w > self.max_usd + 1e-12:
                self.stop = (f"BUDGET: estimated bound ${self.max_usd:.2f}/night reached — "
                             f"charged ${self.charged():.4f} (earlier runs of the night "
                             f"${self.spent_before:.4f}) + this attempt's estimated worst "
                             f"case ${w:.4f} would pass it")
            if self.stop is not None:
                self.refused += 1
                raise JudgeBudgetStop(self.stop)
            self.attempts += 1
            self.inflight += w
        return w

    def settle(self, w, cost):
        """`cost` = the CLI's total_cost_usd, or None when it is unknown."""
        with self.lock:
            self.inflight -= w
            if cost is None:
                self.unknown += w
                charged = w
            else:
                self.known += cost
                charged = cost
            breach = None
            if cost is not None and cost > w + 1e-12:
                breach = f"an attempt cost ${cost:.4f}, above its estimated worst case ${w:.4f}"
                self.breach = self.breach or breach
                if self.stop is None:
                    self.stop = f"BREACH: {breach} — an assumption of the estimate failed; " \
                                f"no paid call follows it this night"
            if self.ledger:
                with open(self.ledger, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"run": self.run, "at": time.strftime("%FT%T%z"),
                                         "charged_usd": round(charged, 6),
                                         "cost_usd": cost, "reserved_usd": round(w, 6),
                                         "breach": breach}) + "\n")

    def summary(self):
        with self.lock:
            return {"contract": "estimated bound, not a guaranteed hard cap",
                    "max_usd": self.max_usd, "model": self.model(),
                    "out_token_cap": JUDGE_OUT_TOKEN_CAP,
                    "in_token_allowance": JUDGE_IN_TOKEN_ALLOWANCE,
                    "ledger": self.ledger, "spent_before_usd": round(self.spent_before, 6),
                    "known_usd": round(self.known, 6),
                    "unknown_reserved_usd": round(self.unknown, 6),
                    "charged_usd": round(self.known + self.unknown, 6),
                    "night_charged_usd": round(self.charged(), 6),
                    "attempts": self.attempts, "refused": self.refused,
                    "breach": self.breach, "stop": self.stop}


def claude_cmd(model):
    return [CLAUDE_BIN, "-p", "--model", model, "--output-format", "json",
            "--tools", "", "--system-prompt", CLAUDE_SYSTEM, "--setting-sources", "",
            "--strict-mcp-config", "--no-session-persistence"]


def _post_claude(prompt, timeout=None, model=None):
    """One `claude -p` call; same return shape as _post_qwen.

    The prompt goes on stdin (a batch is up to 60 kB — no argv limit). There is no silent
    input truncation to detect on this path (an over-long prompt is an API error, not a cut
    tail), so the 4th value is always False; an answer cut at max_tokens fails to parse and
    lands as NO-VERDICT, which is the honest outcome."""
    timeout = LLM_TIMEOUT if timeout is None else timeout
    model = model or CLAUDE_MODELS[JUDGE]
    if not CLAUDE_BIN:
        raise ClaudeJudgeError(CLAUDE_BIN_WHY or "no agent CLI configured (KHMS_AGENT)")
    budget = JUDGE_BUDGET
    w = budget.reserve(prompt) if budget is not None else None   # may raise JudgeBudgetStop
    env = dict(os.environ)
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(JUDGE_OUT_TOKEN_CAP)
    t0 = time.time()
    try:
        cp = CLAUDE_RUNNER(claude_cmd(model), input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=tempfile.gettempdir(), env=env)
    except subprocess.TimeoutExpired as e:
        with _USAGE_LOCK:
            JUDGE_USAGE["errors"] += 1
        if budget is not None:
            budget.settle(w, None)
        raise TimeoutError(f"claude -p --model {model}: no answer within {timeout:.0f}s") from e
    try:
        r = json.loads(cp.stdout)
        if not isinstance(r, dict):
            raise ValueError("not an object")
    except ValueError:
        with _USAGE_LOCK:
            JUDGE_USAGE["errors"] += 1
        if budget is not None:
            budget.settle(w, None)
        raise ClaudeJudgeError(f"claude -p --model {model} rc={cp.returncode}: no JSON on "
                               f"stdout ({(cp.stdout or '')[:160]!r}; stderr "
                               f"{(cp.stderr or '')[:160]!r})")
    ok = cp.returncode == 0 and not r.get("is_error")
    _account(r, ok, time.time() - t0)
    if budget is not None:
        c = r.get("total_cost_usd")
        budget.settle(w, None if c is None else float(c))
    if not ok and USAGE_LIMIT_RE.search(str(r.get("result") or "")):
        raise JudgeUsageLimit(f"the account's usage limit answered claude -p --model {model} "
                              f"({str(r.get('result'))[:160]}) -- no further judge call tonight")
    if not ok:
        raise ClaudeJudgeError(f"claude -p --model {model} rc={cp.returncode} is_error="
                               f"{r.get('is_error')}: {str(r.get('result'))[:200]}")
    u = r.get("usage") or {}
    pec = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                             "cache_creation_input_tokens"))
    return r.get("result") or "", pec, CLAUDE_CTX, False


def _post(prompt, timeout=None):
    """The judge call. Dispatches on JUDGE at CALL time (tests and the evaluator set it)."""
    if JUDGE == "local":
        return _post_qwen(prompt, timeout=timeout)
    return _post_claude(prompt, timeout=timeout)


def judge_description() -> str:
    if JUDGE == "local":
        return f"local: {LLM_URL} {LLM_MODEL}"
    return f"{JUDGE}: claude -p --model {CLAUDE_MODELS[JUDGE]} ({CLAUDE_BIN})"


# The domain words of the base ("an engineering knowledge base") are message TRIAGE_KB_DOMAIN.
ADJ_SYSTEM = "You audit " + KM.msg("TRIAGE_KB_DOMAIN") + """ for CONTRADICTIONS.

For each numbered PAIR of cards decide whether they assert conflicting things about the SAME
subject. A contradiction is: the same physical quantity with different values; an identity
claim contradicted by another ("device X is a Waveshare AR0144" vs "device X is an unbranded
CCB Camera"); one card asserting P and another asserting not-P; or one card recording that a
claim was CORRECTED while another card still states the old claim as current.

NOT contradictions: two cards about different subjects; a card that refines or adds detail;
different values that are measurements of genuinely different things; a card that already
links to the other as its correction.

Answer with ONE JSON array, one object per pair, no prose, no markdown fence:
[{"pair":<n>,"contradiction":"yes|no|unclear","shape":"quantity|identity|negation|correction-orphan|stale-value|other","subject":"<short>","quote_a":"<verbatim from A>","quote_b":"<verbatim from B>","likely_wrong":"a|b|unclear","severity":1-5,"why":"<one sentence>"}]

severity: 5 = a wrong hardware/config fact that would misdirect real work; 1 = cosmetic."""

# The definition the MORNING REVIEW applies (issue #208, JUDGEDEF, 2026-10-01), derived from the
# REVIEW VERDICT reasons of the 20 oldest reviewed nights — plans/triage-review-standard.md.
# ADJ_SYSTEM above excludes "a card that refines or adds detail" and "a card that already links
# to the other as its correction"; the review APPLIES exactly those (an overturned causal story,
# an undercut instrument, a one-way correction). Used by the Claude judges only: the local judge keeps the
# prompt it was measured with, because a recall-first prompt on a judge that already says "yes"
# to almost everything was never measured. Measurement: tools/eval_triage_judge.py.
ADJ_SYSTEM_REVIEW = "You screen pairs of cards from " + KM.msg("TRIAGE_KB_DOMAIN") + """ for the morning review.
The reviewer adds a CORRECTION link (refute, challenge or erratum) to a card whenever another
card's finding means that a reader must no longer take one of its claims at face value. Your
job is to pass the reviewer EVERY pair that may need such a link. A pair you drop is never
looked at again, so a missed correction stays in the base and misleads later work; a pair you
pass costs the reviewer one line. When in doubt, answer "yes".

Answer "yes" when the cards are about the same area — the same device, component, file, tool,
quantity, symptom, failure, instrument or investigation — and one card's finding CHANGES how a
claim of the other must be read. The reviewer applies all of these:
- a different value for a quantity the other card states or uses (a re-measurement, a re-base,
  a later version or platform table);
- a different CAUSE, culprit or failing device for the same symptom or defect, even when the
  newer card calls it "a separate bug", "another defect" or a follow-up: the older explanation is
  now at best incomplete, and the reader must be pointed to the measured one;
- the counter, instrument, test or premise a conclusion rests on shown not to measure what it
  claims; or a conclusion ("X is the source to trust", "Y is the cure", "only Z works", "every
  case is handled") weakened by a measured case, even if the other card's own numbers stand;
- a fix, guarantee, rule or recommended action shown not to do what it says, to target the wrong
  thing, or to be forbidden;
- an identity conflict, or P against not-P;
- only one claim of a long card affected while the rest stands;
- the correcting card already naming the other, or the other already marked challenged or
  superseded, while the claim is still stated in it;
- two investigations of the same symptom, figure, instrument or recovery step that name
  different culprits, causes or devices — even when each card describes itself as a separate
  issue from the other.

Answer "no" only when one of these is plainly true:
- UNRELATED: the cards share a word, a file or a subsystem but no claim — neither finding has
  any bearing on anything the other card says;
- they AGREE: one confirms or restates the other and changes nothing in how it is read;
- the card whose finding would do the correcting is itself a decision→rationale, policy,
  principle, requirement, overview or goal→method card: a decision can make a card obsolete but
  cannot make a measured claim false. (The other way round is a "yes": a measured card can
  correct a factual sentence inside a policy or decision card.)
- a MISREAD: the conflict exists only in a quoted, negated, conditional or counterfactual
  sentence, or the other card, read whole, confirms the passage.

Answer with ONE JSON array, one object per pair, no prose, no markdown fence:
[{"pair":<n>,"contradiction":"yes|no|unclear","shape":"quantity|identity|negation|correction-orphan|stale-value|other","subject":"<short>","quote_a":"<verbatim from A>","quote_b":"<verbatim from B>","likely_wrong":"a|b|unclear","severity":1-5,"why":"<one sentence>"}]

likely_wrong names the card whose claim must no longer be taken at face value. severity: 5 = a
wrong hardware/config fact that would misdirect real work; 1 = cosmetic."""


def adj_system():
    """The judge instructions for the judge in force (JUDGE is read at call time)."""
    return ADJ_SYSTEM if JUDGE == "local" else ADJ_SYSTEM_REVIEW


# How much of each card body the judge is shown (characters; the rest is cut with a marker).
JUDGE_CARD_CHARS = 1600


def judge_prompt_id():
    """A short fingerprint of what the judge is asked and shown (the instructions plus the card
    cut). The evaluator stores it with every recorded answer, so a measurement can never be
    reported for a prompt it was not made with."""
    import hashlib
    h = hashlib.sha256(f"{adj_system()}\n#card_chars={JUDGE_CARD_CHARS}".encode()).hexdigest()
    return f"adj-{h[:10]}"


def card_blob(c, maxlen=None):
    maxlen = JUDGE_CARD_CHARS if maxlen is None else maxlen
    body = c["body"]
    if len(body) > maxlen:
        body = body[:maxlen] + " …[truncated by the caller]"
    return (f"id={c['id']} type={c.get('type','?')} status={c['status']} "
            f"level={c['level']} date={c.get('date','?')}\n{body}")


def adjudicate(pairs, by_id, batch=6, max_bytes=60000, verbose=True,
               deadline=None, on_progress=None, attempts=None):
    """Adjudicate `pairs`, bounded in three ways and checkpointed after every batch.

    BOUNDS. Each model call gets `LLM_TIMEOUT` seconds and `attempts` tries; the whole run
    gets `deadline` (an absolute time.time()). When the deadline arrives, every pair that has
    not had its turn is recorded as OUTCOME_DEADLINE and the loop ends — the run finishes
    inside a named period instead of being killed at some hour of the morning.

    `on_progress(results, done, planned)` is called after every batch so the caller can put
    what exists on disk. It is called from the LOOP, not from a signal handler or atexit:
    the two interruptions this defends against (SIGKILL, a harness reap) run no handler.
    """
    attempts = LLM_ATTEMPTS if attempts is None else max(1, attempts)
    results, i, calls, trunc = [], 0, 0, 0
    counts = Counter()
    t0 = time.time()
    since_checkpoint = 0

    def finish_batch(chunk, outcome, why, n_done):
        for p in chunk:
            p["verdict"] = {"contradiction": "unclear", "outcome": outcome, "why": why}
            counts[outcome] += 1
            results.append(p)
        return n_done

    while i < len(pairs):
        if deadline is not None and time.time() >= deadline:
            rest = pairs[i:]
            over = time.time() - t0
            finish_batch(rest, OUTCOME_DEADLINE,
                         f"run deadline reached after {over:.0f}s; this pair was never sent "
                         f"to the model — it is NOT a finding of 'no contradiction'",
                         len(rest))
            if verbose:
                print(f"  DEADLINE after {over:.0f}s — {len(rest)} pairs left unadjudicated",
                      file=sys.stderr)
            i = len(pairs)
            break
        n = batch
        while n >= 1:
            chunk = pairs[i:i + n]
            body = []
            for j, p in enumerate(chunk, 1):
                body.append(f"### PAIR {j}\n--- CARD A ---\n{card_blob(by_id[p['a']])}\n"
                            f"--- CARD B ---\n{card_blob(by_id[p['b']])}\n"
                            f"(lexical hint: {p['shapes'] or 'none'})")
            prompt = adj_system() + "\n\n" + "\n\n".join(body) + "\n\nJSON array:"
            if len(prompt) > max_bytes and n > 1:
                n //= 2
                continue
            txt = pec = ctx = cut = None
            last_err = None
            timed_out = False
            stopped = None
            for attempt in range(1, attempts + 1):
                budget = LLM_TIMEOUT
                if deadline is not None:
                    budget = min(budget, max(1.0, deadline - time.time()))
                try:
                    txt, pec, ctx, cut = _post(prompt, timeout=budget)
                    last_err = None
                    break
                except JudgeBudgetStop as e:
                    stopped = (e, attempt, last_err)
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    last_err, timed_out = e, is_timeout(e)
                    kind = "timeout" if timed_out else "error"
                    print(f"  LLM {kind} ({e}) on pairs {i}..{i+n}, "
                          f"attempt {attempt}/{attempts}", file=sys.stderr)
                    if deadline is not None and time.time() >= deadline:
                        break
            if stopped is not None:
                # THE BOUND REFUSED A PAID CALL: this chunk and everything after it are
                # recorded as BUDGET and the loop ends — no later call may start.
                e, attempt, prev = stopped
                tail = " — it is NOT a finding of 'no contradiction'"
                if attempt > 1:
                    finish_batch(chunk, OUTCOME_BUDGET,
                                 f"{e}; this pair's first attempt failed ({prev}) and the "
                                 f"bound refused the retry{tail}", n)
                else:
                    finish_batch(chunk, OUTCOME_BUDGET,
                                 f"{e}; this pair was never sent to the model{tail}", n)
                rest = pairs[i + len(chunk):]
                finish_batch(rest, OUTCOME_BUDGET,
                             f"{e}; this pair was never sent to the model{tail}", len(rest))
                print(f"  JUDGE BOUND: {e} — {len(chunk) + len(rest)} pairs left unsent",
                      file=sys.stderr)
                i = len(pairs)
                n = 0
                break
            if last_err is not None:
                outcome = OUTCOME_UNANSWERED if timed_out else OUTCOME_LLM_ERROR
                i += finish_batch(
                    chunk, outcome,
                    f"{'no answer within' if timed_out else 'LLM error'} "
                    f"{LLM_TIMEOUT:.0f}s x {attempts} attempts: {last_err}", n)
                n = 0
                break
            calls += 1
            if cut and n > 1:
                if verbose:
                    print(f"  TRUNCATION at pairs {i}..{i+n} "
                          f"(prompt_eval={pec}/{ctx}) — halving", file=sys.stderr)
                n //= 2
                continue
            if cut:
                trunc += 1
                chunk[0]["verdict"] = {"contradiction": "unclear",
                                       "outcome": OUTCOME_TRUNCATED,
                                       "why": f"TRUNCATED prompt_eval={pec}/{ctx}"}
                counts[OUTCOME_TRUNCATED] += 1
                results.append(chunk[0])
                i += 1
                n = 0
                break
            try:
                s = txt[txt.index("["):txt.rindex("]") + 1]
                verdicts = json.loads(s)
            except (ValueError, json.JSONDecodeError):
                verdicts = []
            vmap = {int(v.get("pair", k + 1)): v for k, v in enumerate(verdicts)
                    if isinstance(v, dict)}
            for j, p in enumerate(chunk, 1):
                v = vmap.get(j)
                if v is None:
                    v = {"contradiction": "unclear", "outcome": OUTCOME_NO_VERDICT,
                         "why": "model returned no verdict for this pair"}
                else:
                    v.setdefault("outcome", OUTCOME_OK)
                counts[v["outcome"]] += 1
                p["verdict"] = v
                p["prompt_eval"] = f"{pec}/{ctx}"
                results.append(p)
            i += n
            n = 0
        if n == 0:
            since_checkpoint += 1
            if on_progress and since_checkpoint >= CHECKPOINT_EVERY:
                since_checkpoint = 0
                on_progress(results, len(results), len(pairs))
            continue
        i += 1
    if on_progress:
        on_progress(results, len(results), len(pairs))
    if verbose:
        named = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
        print(f"  judged {judged_count(results)} of {len(pairs)} planned pairs "
              f"({len(results)} recorded) in {calls} calls, "
              f"{time.time()-t0:.0f}s, {trunc} unrecoverable truncations", file=sys.stderr)
        print(f"  batch outcomes: {named}", file=sys.stderr)
    return results


# ------------------------------------------------- verdict integrity (post-adjudication)
# MEASURED 2026-09-03, on this tool's own sweep of the night before. The morning review of
# `memory/inbox/2026-09-03-triage.md` applied 6 of 41 rows and threw out 35 — every one for a
# defect in the ROW, not in the knowledge. Four rows quoted text that is not in the card the
# quote was attached to, and BOTH `RETIRE_WHOLE` rows were in that group: the two most
# destructive proposals in the file (they would have archived K-91377 and K-92375, two of the
# base's freshest measured corrections) were both built on a quote lifted from the other card.
# Seven more rows quoted two passages that AGREE.
#
# A verdict is a claim about two cards, and the only part of it that can be checked without a
# model is whether the evidence it cites exists where it says it does. That check is here.
#
# WHY COVERAGE AND NOT VERBATIM CONTAINMENT. Strict containment fails on 32 of the 58 confirmed
# rows, including 2 of the 6 the review APPLIED: the model silently elides ("wheelbase 0.850 →
# 0.880 m … radius moved from 1.698 m to 1.791704 m" drops "(the old cad-derived constant)")
# and drops inner quote marks. Token coverage against the card's own body is bimodal on that
# same data — 105 of 116 quote sides at ≥ 0.90, 11 at ≤ 0.36, nothing between — and every one
# of the 11 sits in a row the reviewer rejected. QUOTE_COV_MIN sits in that gap; it is §7
# configuration, calibrated, not a rule.
#
# WHAT IS DELIBERATELY NOT CHECKED: the reviewer also proposed requiring the pair's shared
# subject key to occur in both bodies. Measured on the same file, that guard drops exactly one
# row — K-92197 ⇄ K-92463, the rear-camera identity pair this whole package exists to find —
# and none of the 18 wrong-subject rows, whose shared keys are ordinary prose words present in
# both cards. It would cost the finding and buy nothing.
QUOTE_COV_MIN = 0.75      # §7 configuration — see the measured gap above
AGREE_JACCARD = 0.80      # two quotes this lexically alike are one sentence, not two claims
_WSP = re.compile(r"\s+")
_QTOK = re.compile(r"[0-9a-zA-Z\u00c0-\u017e]+")


def _norm_text(s: str) -> str:
    return _WSP.sub(" ", (s or "").strip().lower())


def _qtokens(s: str) -> set[str]:
    return {t for t in _QTOK.findall(_norm_text(s)) if len(t) >= 3}


def card_text(c) -> str:
    return _norm_text((c.get("first") or "") + "\n" + (c.get("body") or ""))


def quote_coverage(quote: str, card_text_norm: str) -> float:
    """How much of the model's quote actually occurs in that card. 1.0 on verbatim."""
    q = _qtokens(quote)
    if not q:
        return 0.0
    if _norm_text(quote) in card_text_norm:
        return 1.0
    body_tokens = set(_QTOK.findall(card_text_norm))
    return sum(1 for t in q if t in body_tokens) / len(q)


def _jaccard(x: set, y: set) -> float:
    return len(x & y) / len(x | y) if (x or y) else 0.0


def integrity_filter(results, by_id, cov_min=QUOTE_COV_MIN, verbose=False, texts=None):
    """Drop confirmed rows whose own evidence does not hold up. Nothing is deleted silently:
    the row keeps its verdict, marked `contradiction: dropped` with `dropped_reason`, so the
    report can list it. Returns counts by reason.

    Idempotent: a row already marked `dropped` is skipped on a second pass, which is what lets
    a checkpoint run the same filter on the same results after every batch. `texts` is the
    normalised card text, passed in by a caller that runs this repeatedly so the whole base is
    not re-normalised on every checkpoint.
    """
    # Stable shape: a consumer must never have to tell "zero" from "key absent".
    counts = Counter({"checked": 0, "kept": 0, "quote-not-in-card": 0, "quotes-agree": 0})
    if texts is None:
        texts = {cid: card_text(c) for cid, c in by_id.items()}
    for r in results:
        v = r.get("verdict") or {}
        if v.get("contradiction") != "yes":
            continue
        counts["checked"] += 1
        qa, qb = v.get("quote_a"), v.get("quote_b")
        ta, tb = texts.get(r["a"], ""), texts.get(r["b"], "")

        def drop(reason_class, why):
            v["contradiction"] = "dropped"
            v["dropped_class"] = reason_class
            v["dropped_reason"] = why
            counts[reason_class] += 1

        if not (_qtokens(qa) and _qtokens(qb)):
            drop("quote-not-in-card", "the model returned no usable quote for one side")
            continue
        # (4) the two quotes agree — one sentence read twice is not a contradiction
        if (_jaccard(_qtokens(qa), _qtokens(qb)) >= AGREE_JACCARD
                or (len(_norm_text(qa)) >= 25
                    and (_norm_text(qa) in _norm_text(qb) or _norm_text(qb) in _norm_text(qa)))):
            drop("quotes-agree",
                 "the quoted passages are the same claim, not two conflicting ones")
            continue
        # (1) each quote must be found in the card it is attached to
        ca, cb = quote_coverage(qa, ta), quote_coverage(qb, tb)
        if ca < cov_min or cb < cov_min:
            bad, cid, other, txt_other = (("a", r["a"], r["b"], tb) if ca < cb
                                          else ("b", r["b"], r["a"], ta))
            elsewhere = quote_coverage(qa if bad == "a" else qb, txt_other)
            why = (f"quote_{bad} (coverage {min(ca, cb):.2f}) is not in {cid}"
                   + (f" — it is text from {other} (coverage {elsewhere:.2f})"
                      if elsewhere >= 0.9 else ""))
            drop("quote-not-in-card", why)
            continue
        counts["kept"] += 1
    if verbose:
        print(f"  integrity: {counts['kept']} of {counts['checked']} confirmed rows kept; "
              f"dropped {counts['quote-not-in-card']} quote-not-in-card, "
              f"{counts['quotes-agree']} quotes-agree", file=sys.stderr)
    return dict(counts)


# ---------------------------------------------------------------- report

def atomic_write(path, text):
    """Write `text` to `path` so a reader never sees half of it, and a kill never truncates it.

    Same directory (os.replace is only atomic within a filesystem), fsync before the rename,
    and the temporary file is removed if anything goes wrong. A partial report is worth having
    only if it is a WHOLE file describing partial work — a half-written one would be the very
    silent truncation this tool exists to catch.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Checkpoint:
    """Puts the answer on disk WHILE the run happens, not after it.

    MEASURED 2026-09-20: two interruptions of the same night's sweep, both of them leaving
    zero bytes, because `--out` and `--json` were written after the last batch returned. A
    SIGKILL and a harness reap both run no handler, so nothing can be flushed on the way out;
    the only thing that survives is what was already written. So the report is written once
    before the first model call (0 of N adjudicated) and again after every batch.
    """

    def __init__(self, out_path, json_path, by_id, meta, top=10, verbose=True):
        self.out_path, self.json_path = out_path, json_path
        self.by_id, self.meta, self.top, self.verbose = by_id, meta, top, verbose
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.writes = 0
        self.texts = {cid: card_text(c) for cid, c in by_id.items()}

    def write(self, results, done, planned, partial=True, finished=None):
        """Put what exists on disk, described by what it IS.

        `partial=True` / `finished=False` is the caller saying "the loop is still in flight".
        `partial=False` is the caller saying "the loop returned" — and that is ALL it can say.
        Whether the report is complete is decided here, from the outcomes: a run that ended at
        its deadline having judged nothing now writes `partial: true`, because it is.
        `done` is kept in the signature for the callers that pass `len(results)`; it is
        deliberately NOT trusted — `run_census` recounts from the rows themselves.
        """
        finished = (not partial) if finished is None else bool(finished)
        cen = run_census(results, planned, finished)
        progress = {"partial": not cen["complete"],
                    # `adjudicated` keeps its name and LOSES its old meaning: from today it is
                    # the number of pairs that came back JUDGED, not the number recorded.
                    "adjudicated": cen["judged"],
                    "judged": cen["judged"], "recorded": cen["recorded"],
                    "not_judged": cen["not_judged"], "no_record": cen["no_record"],
                    "finished": cen["finished"], "complete": cen["complete"],
                    "outcomes": cen["outcomes"],
                    "planned": cen["planned"],
                    "started": self.started,
                    "written": time.strftime("%Y-%m-%dT%H:%M:%S")}
        integ = integrity_filter(results, self.by_id, verbose=False, texts=self.texts)
        if self.out_path:
            md, _, _ = render(results, self.by_id, top=self.top, progress=progress)
            atomic_write(self.out_path, md + "\n")
        if self.json_path:
            meta = dict(self.meta)
            meta.update({"generated": progress["written"], "integrity": integ,
                         "progress": progress,
                         "outcomes": dict(Counter(
                             (r.get("verdict") or {}).get("outcome", "?") for r in results))})
            atomic_write(self.json_path,
                         json.dumps({"meta": meta, "results": results},
                                    indent=1, default=str))
        self.writes += 1
        # Only claim "on disk" when something was actually written — a checkpoint line for a
        # run with no --out and no --json would be the same species of lie as a report that
        # does not say it is partial.
        if self.verbose and progress["partial"] and (self.out_path or self.json_path):
            print(f"  checkpoint {self.writes}: {cen['judged']}/{cen['planned']} planned pairs "
                  f"JUDGED ({cen['recorded']} recorded) written to "
                  f"{self.out_path or self.json_path}", file=sys.stderr)


def outcome_counts(results) -> Counter:
    return Counter((r.get("verdict") or {}).get("outcome") or OUTCOME_OK for r in results)


def render(results, by_id, top=10, progress=None):
    conf = [r for r in results if r.get("verdict", {}).get("contradiction") == "yes"]
    unclear = [r for r in results if r.get("verdict", {}).get("contradiction") == "unclear"]
    conf.sort(key=lambda r: (-int(r["verdict"].get("severity", 0) or 0), -r["score"]))
    hist = Counter(r["verdict"].get("shape", "other") for r in conf)
    L = []
    # THE FIRST LINE SAYS WHETHER THIS IS THE WHOLE ANSWER, and it is recomputed from the rows
    # rather than taken from the caller: a reader who cannot tell a finished sweep from an
    # interrupted one reads "3 contradictions" as "the base holds 3", and a reader told
    # "complete" by a run that judged nothing is worse off still. THREE states, never two:
    # still running · finished but did not judge its plan · complete.
    cen = run_census(results, (progress or {}).get("planned", 0),
                     (progress or {}).get("finished",
                                          not (progress or {}).get("partial", True)))
    if progress:
        done, planned = cen["judged"], cen["planned"]
        pct = (100.0 * done / planned) if planned else 0.0
        named = ", ".join(f"{k} {v}" for k, v in cen["outcomes"].items() if k != OUTCOME_OK)
        gap = f"{cen['not_judged']} recorded with no verdict ({named or 'none'})"
        never = f", {cen['no_record']} never written at all" if cen["no_record"] else ""
        if not cen["finished"]:
            L += [f"> **PARTIAL REPORT — this run has not finished.** {done} of {planned} "
                  f"planned pairs judged ({pct:.0f} %); {gap}{never}. "
                  f"Started {progress.get('started')}, "
                  f"last written {progress.get('written')}. The pairs not listed here were NOT "
                  f"judged free of contradictions — they were not judged at all.",
                  ""]
        elif not cen["complete"]:
            # The loop RETURNED and the plan was still not carried out — the deadline branch
            # ends the run normally, which is why "it finished" was never evidence of anything.
            L += [f"> **INCOMPLETE REPORT — this run finished without judging its plan.** "
                  f"{done} of {planned} planned pairs judged ({pct:.0f} %); {gap}{never}. "
                  f"Started {progress.get('started')}, "
                  f"last written {progress.get('written')}. The pairs with no verdict were NOT "
                  f"judged free of contradictions — they were not judged at all.",
                  ""]
    L += [f"# KHMS contradiction sweep — {time.strftime('%Y-%m-%d %H:%M')}",
          ""]
    if progress and cen["complete"]:
        L += [f"complete: {cen['judged']} of {cen['planned']} planned pairs judged", ""]
    # "scanned pairs" used to be len(results), and a DEADLINE row was never scanned by
    # anything. The denominator of every ratio below it is JUDGED; the rows that carry no
    # verdict are counted on their own line so they cannot pass for cleared ones.
    model_unclear = sum(1 for r in unclear
                        if ((r.get("verdict") or {}).get("outcome") or OUTCOME_OK)
                        == OUTCOME_OK)
    clean = cen["judged"] - len(conf) - model_unclear
    L += [f"judged pairs: {cen['judged']} of {cen['planned']} planned  ·  "
          f"confirmed: {len(conf)}  ·  model could not decide: {model_unclear}  ·  "
          f"no contradiction: {clean}  ·  "
          f"no judgement at all: {cen['not_judged'] + cen['no_record']} "
          f"(recorded rows: {cen['recorded']})",
          ""]
    # WHAT THIS RUN DID NOT DECIDE, by name. An unanswered batch that is merely absent from the
    # report is indistinguishable from a batch the model cleared.
    oc = outcome_counts(results)
    undecided = {k: v for k, v in oc.items() if k != OUTCOME_OK}
    if undecided:
        L += ["## Not decided (named outcomes — none of these means 'no contradiction')", ""]
        for k in sorted(undecided):
            L.append(f"- **{k}**: {undecided[k]} pair(s)")
        for k in sorted(undecided):
            ex = next((r for r in results
                       if (r.get("verdict") or {}).get("outcome") == k), None)
            if ex:
                L.append(f"  - {k} e.g. {ex['a']} ⇄ {ex['b']}: "
                         f"{str((ex.get('verdict') or {}).get('why', ''))[:200]}")
        L.append("")
    L += ["## Histogram by shape",
          ""]
    for s, n in hist.most_common():
        L.append(f"- {s}: {n}")

    # CLUSTERS. A subject that is asserted three ways produces three pair-edges, and a
    # reviewer reading pairs one at a time cannot see that they are one disagreement. The
    # rear-camera case is exactly this shape: K-90643 / K-92197 / K-92072 are a trio, not
    # three unrelated rows. Connected components over the confirmed edges restore that view.
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in conf:
        ra, rb = find(r["a"]), find(r["b"])
        if ra != rb:
            parent[ra] = rb
    comps = defaultdict(list)
    for r in conf:
        comps[find(r["a"])].append(r)
    multi = sorted((c for c in comps.values() if len({r['a'] for r in c} |
                                                     {r['b'] for r in c}) >= 3),
                   key=lambda c: -len(c))
    if multi:
        L += ["", "## Clusters (one subject asserted three or more ways)", ""]
        for c in multi[:12]:
            ids = sorted({r["a"] for r in c} | {r["b"] for r in c})
            subj = Counter(r["verdict"].get("subject", "?") for r in c).most_common(1)[0][0]
            L.append(f"- **{subj}** — {', '.join(ids)} "
                     f"({len(c)} conflicting edges, max severity "
                     f"{max(int(r['verdict'].get('severity', 0) or 0) for r in c)})")

    L += ["", f"## Top {top} by severity", ""]
    for r in conf[:top]:
        v = r["verdict"]
        L += [f"### {r['a']} ⇄ {r['b']} — {v.get('shape','?')} · severity "
              f"{v.get('severity','?')} · subject: {v.get('subject','?')}",
              f"- **{r['a']}**: \"{v.get('quote_a','')}\"",
              f"- **{r['b']}**: \"{v.get('quote_b','')}\"",
              f"- likely wrong: **{v.get('likely_wrong','unclear')}** — "
              f"{str(v.get('why',''))[:400]}",
              f"- blockers: {', '.join(r['blockers'])} · lexical shapes: "
              f"{[s['shape'] for s in r['shapes']] or 'none'}",
              ""]
    return "\n".join(L), conf, unclear


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs-only", action="store_true", help="blocking only, no model call")
    ap.add_argument("--limit", type=int, default=300, help="top-N pairs to adjudicate")
    ap.add_argument("--cards", help="comma-separated card ids to focus on")
    ap.add_argument("--since", help="ISO date: only pairs touching cards modified since")
    ap.add_argument("--out", help="write markdown report here")
    ap.add_argument("--json", help="write raw results json here")
    ap.add_argument("--batch", type=int, default=6)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--deadline", type=float, default=RUN_DEADLINE_S,
                    help="seconds; the run finishes by itself when this is reached and marks "
                         "every pair it never sent as DEADLINE. 0 disables (the old, "
                         "unbounded behaviour — a hung endpoint then costs the whole night)")
    ap.add_argument("--max-usd", type=float,
                    default=(float(os.environ["KHMS_JUDGE_MAX_USD"])
                             if os.environ.get("KHMS_JUDGE_MAX_USD") else None),
                    help="Claude judge only: ESTIMATED per-night spend bound (not a guaranteed "
                         "hard cap; every attempt reserves its estimated worst case first). "
                         "Unset = no bound")
    ap.add_argument("--spend-ledger", default=os.environ.get("KHMS_JUDGE_LEDGER"),
                    help="JSONL of every paid attempt of the night; earlier runs of the same "
                         "night count against --max-usd")
    ap.add_argument("--skip-judged-dir", metavar="STAGING",
                    help="skip pairs judged (outcome OK) in the triage-<night key>-sweep*.json files "
                         "of the last --skip-nights nights in this directory, unless a card of "
                         "the pair changed since that run started (#208: full nights must not "
                         "re-judge the same top pairs)")
    ap.add_argument("--skip-nights", type=int, default=0,
                    help="how many nights before --today the skip looks back (0 = off)")
    ap.add_argument("--today", help="ISO date of this night (default: today)")
    ap.add_argument("--night-key", help="this night's key, DATE or DATE-<n> (default: --today); "
                                        "its own sweep files are never read by the skip, the "
                                        "other nights of the same date are (#237)")
    ap.add_argument("--provenance", action="store_true",
                    help="report cards asserting machine-checkable numbers on doc evidence")
    args = ap.parse_args()
    # Every file this run can write -- the report, the JSON checkpoint, the judge's spend ledger -- is checked
    # against the write policy before the first card is read: a reader may sweep into a detached directory,
    # never into the base, its configuration or its shared state.
    P.require_may_write([args.out, args.json, args.spend_ledger], "write the sweep's outputs")

    cards = [c for c in ks.load_cards() if not c["fog"]]
    by_id = {c["id"]: c for c in cards}
    for c in cards:  # type/evidence/source are not parsed by khms_search; read them back
        head = open(c["path"], encoding="utf-8").read().split("\n---\n")[0]
        for k in ("type", "evidence", "source", "date"):
            m = re.search(rf"^{k}:\s*(.+)$", head, re.M)
            c[k] = m.group(1).strip() if m else ""
        c["type"] = c["type"] or "?"
    print(f"loaded {len(cards)} know/ cards", file=sys.stderr)

    if args.provenance:
        rows = provenance_flags(cards)
        print(f"# Unverified against the machine — {len(rows)} cards\n")
        print("Documentary evidence (`reported`) asserting numbers a tape measure or a "
              "register read could settle. Not contradictions yet.\n")
        for r in rows[:args.limit]:
            print(f"- **{r['id']}** ({r['checkable_values']} checkable values, "
                  f"{','.join(r['families'])}) — {r['first']}\n  source: {r['source']}")
        return

    # WHICH POPULATION this run judges, stated in the JSON (#208 P208F): the morning file printed
    # nothing and the log said `--since` on the nights the run silently swept the whole base.
    focus = None
    mode = "full"
    if args.cards:
        focus = {x.strip() for x in args.cards.split(",") if x.strip()}
        mode = f"cards ({len(focus)} named)"
    elif args.since:
        import datetime
        cut = datetime.datetime.fromisoformat(args.since).timestamp()
        focus = {c["id"] for c in cards if os.path.getmtime(c["path"]) >= cut}
        print(f"incremental: {len(focus)} cards touched since {args.since}", file=sys.stderr)
        mode = f"since {args.since} ({len(focus)} cards touched)"
        if not focus:
            print("incremental: the window holds no card -> no pair to judge (an empty "
                  "window is NOT widened to a full sweep; a full sweep is a run without --since)",
                  file=sys.stderr)

    pairs = build_candidates(cards, focus_ids=focus)
    # Deterministic findings read the WHOLE candidate set, gate or no gate: a correction
    # orphan is reported even when the pair would not be worth a model call.
    det = deterministic_findings(pairs)
    pairs, gate_acc = gate_before_model(pairs, by_id)
    print(f"  triage gate before the model: refused {gate_acc['refused']} of "
          f"{gate_acc['of']} candidate pairs (rules {gate_acc['rules_in_force']}; "
          f"by rule {gate_acc['by_rule']})", file=sys.stderr)
    print(f"mode: {mode}", file=sys.stderr)
    skip_acc = None
    if args.skip_judged_dir and args.skip_nights > 0:
        import datetime
        mtimes = {c["id"]: os.path.getmtime(c["path"]) for c in cards}
        skip, skip_acc = recently_judged(args.skip_judged_dir, args.skip_nights,
                                         args.today or datetime.date.today().isoformat(),
                                         mtimes.get, night_key=args.night_key)
        pairs, skip_acc["skipped"] = skip_recently_judged(pairs, skip)
        print(f"  judged in the last {args.skip_nights} nights and unchanged since: skipped "
              f"{skip_acc['skipped']} candidate pairs ({skip_acc['judged_in_window']} judged in "
              f"{len(skip_acc['files'])} sweep files, {skip_acc['card_changed_since']} eligible "
              f"again because a card changed"
              + (f"; UNREADABLE {skip_acc['unreadable']}" if skip_acc["unreadable"] else "")
              + ")", file=sys.stderr)
    if args.pairs_only:
        todo, acc = select_for_adjudication(pairs, args.limit)
        acc["gated_before_model"] = gate_acc
        acc["skipped_recently_judged"] = skip_acc
        print(f"  would adjudicate {len(todo)}; {acc['not_adjudicated']} not adjudicated; "
              f"{det['total_groups']} deterministic strong correction-orphan groups",
              file=sys.stderr)
        if args.json:
            json.dump({"meta": {"cards": len(cards), "limit": args.limit, "mode": mode,
                                "focus": sorted(focus) if focus is not None else None,
                                "selection": acc, "deterministic": det},
                       "results": []},
                      open(args.json, "w", encoding="utf-8"), indent=1, default=str)
        else:
            print(json.dumps(todo, indent=1))
        return

    todo, acc = select_for_adjudication(pairs, args.limit)
    acc["gated_before_model"] = gate_acc
    acc["skipped_recently_judged"] = skip_acc
    print(f"adjudicating {len(todo)} of {len(pairs)} pairs "
          f"(quota {acc['quota_used']}); {acc['not_adjudicated']} not adjudicated",
          file=sys.stderr)
    # The envelope travels with every checkpoint, so that WHAT THE RUN DID NOT DO is on disk
    # from the first write. A consumer that only sees the surviving verdicts cannot tell a
    # clean base from an exhausted budget, and that is precisely how a confirmed finding
    # disappeared between the detector and the proposal.
    meta = {"cards": len(cards), "limit": args.limit, "mode": mode,
            "focus": sorted(focus) if focus is not None else None,
            "selection": acc, "deterministic": det,
            "bounds": {"llm_timeout_s": LLM_TIMEOUT, "llm_attempts": LLM_ATTEMPTS,
                       "run_deadline_s": args.deadline, "llm_url": LLM_URL,
                       "judge": judge_description()}}
    ck = Checkpoint(args.out, args.json, by_id, meta, top=args.top)
    # Written BEFORE the first model call: an interruption in the first batch still leaves a
    # report that names its own emptiness.
    ck.write([], 0, len(todo), partial=True)
    deadline = time.time() + args.deadline if args.deadline > 0 else None
    if deadline:
        print(f"run deadline in {args.deadline:.0f}s "
              f"({time.strftime('%H:%M:%S', time.localtime(deadline))}); "
              f"per call {LLM_TIMEOUT:.0f}s x {LLM_ATTEMPTS} attempts", file=sys.stderr)
    print(f"judge: {judge_description()}", file=sys.stderr)
    global JUDGE_BUDGET
    if JUDGE != "local" and args.max_usd is not None:
        try:
            JUDGE_BUDGET = JudgeBudget(args.max_usd, ledger=args.spend_ledger)
        except JudgeModelUnpriced as e:
            print(f"judge bound: FAILED — {e}", file=sys.stderr)
            return 3
        b = JUDGE_BUDGET.summary()
        meta["bounds"]["judge_budget"] = b
        print(f"judge bound: estimated bound (not a guaranteed hard cap) ${b['max_usd']:.2f}"
              f"/night · model {b['model']} · out cap {JUDGE_OUT_TOKEN_CAP} tok · earlier runs "
              f"of the night ${b['spent_before_usd']:.4f} · ledger {b['ledger']}"
              + (f" · REFUSING: {b['stop']}" if b["stop"] else ""), file=sys.stderr)
    elif JUDGE != "local":
        print("judge bound: NONE (no --max-usd) — this run's spend is not bounded",
              file=sys.stderr)
    results = adjudicate(todo, by_id, batch=args.batch, deadline=deadline,
                         on_progress=lambda res, done, planned:
                         ck.write(res, done, planned, partial=True))
    # A verdict whose own quotes do not survive a lookup in the cards it names is not a
    # finding. Dropped here, before anything downstream can propose an edit from it.
    integrity_filter(results, by_id, verbose=True)
    if JUDGE != "local":
        # What the night cost, from the CLI's own usage report (#208) — in the JSON, so the
        # morning and the token accounting read one number.
        meta["judge_usage"] = judge_usage()
        u = meta["judge_usage"]
        print(f"judge usage: {u['calls']} calls ({u['errors']} failed) · in "
              f"{u['input_tokens']} (+cache r {u['cache_read_input_tokens']} / w "
              f"{u['cache_creation_input_tokens']}) · out {u['output_tokens']} (thinking "
              f"{u['thinking_tokens']}) · ${u['cli_cost_usd']:.4f} (CLI total_cost_usd) · "
              f"models {u['models']}", file=sys.stderr)
        if JUDGE_BUDGET is not None:
            b = meta["bounds"]["judge_budget"] = JUDGE_BUDGET.summary()
            print(f"judge bound result: charged ${b['charged_usd']:.4f} this run (known "
                  f"${b['known_usd']:.4f} + unknown-cost reservations "
                  f"${b['unknown_reserved_usd']:.4f}), ${b['night_charged_usd']:.4f} this "
                  f"night of ${b['max_usd']:.2f} · {b['attempts']} attempts, {b['refused']} "
                  f"refused · breach {b['breach'] or 'none'} · stop {b['stop'] or 'none'}",
                  file=sys.stderr)
    # `finished=True` says the LOOP returned — nothing more. Whether the report may call
    # itself complete is decided inside, from the outcomes (#149).
    ck.write(results, len(results), len(todo), finished=True)
    cen = run_census(results, len(todo), True)
    print(f"census: {cen['judged']} of {cen['planned']} planned pairs judged · "
          f"{cen['not_judged']} recorded without a verdict · "
          f"{cen['no_record']} never recorded · complete={cen['complete']}", file=sys.stderr)
    if args.out:
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        md, _, _ = render(results, by_id, top=args.top,
                          progress={"finished": True, "planned": len(todo)})
        print(md)


if __name__ == "__main__":
    sys.exit(main())
