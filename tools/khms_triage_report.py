#!/usr/bin/env python3
"""Turn a contradiction sweep into an inbox PROPOSAL a human can review.

Reads the JSON from khms_contradict.py, runs claim-level triage over the losing side of each
confirmed contradiction, and writes one markdown file to memory/inbox/.

IT WRITES NOTHING ELSE. No card is created, edited, moved, retracted or deleted; MEMORY.md
and views/ are not touched. The nightly's existing contract (K-90024 §9.2: "jobs propose")
is the whole reason this is a separate file in the inbox rather than a mutation.

Usage: khms_triage_report.py sweep.json [--provenance provenance.md] [--out inbox/DATE-triage.md]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_triage as T  # noqa: E402
import khms_search as ks  # noqa: E402
import khms_triage_gate as G  # noqa: E402  (deterministic row gate, issue #208)
import khms_paths as P  # noqa: E402  (the write policy of the report's output)
import khms_contradict as kc  # noqa: E402  (the settings reader)
import khms_patterns as KP  # noqa: E402
import khms_messages as KM  # noqa: E402

# Card ids in comments (K-9xxxx) are illustrative stand-ins for the reference deployment's cards
# the measurements below were made on; they are not ids of any shipped base.
BRAIN = T.BRAIN

# ------------------------------------------------------- shared-subject guard (KHMS-T2)
# MEASURED 2026-09-03 on `memory/inbox/.staging/triage-labels.tsv` — 57 triage rows
# adjudicated by hand across the 2026-09-02 and 2026-09-03 reviews (9 APPLY, 48 REJECT).
# The reviewer's dominant ground for refusal is that the losing quote and the winning quote
# are not about the same thing: 21 rows "wrong subject", 10 "the two quotes agree", 5
# "inverted" — 36 of the 48 rejections. The pair generator supplies an embedding neighbour and
# the model supplies a template reason; nothing between them ever asked whether the two claims
# meet. This is the guard that asks.
#
# The score has TWO channels, because neither one survives the data alone:
#   * quote channel — |A n B| / min(|A|,|B|) over stop-word-stripped tokens. Identifiers,
#     paths, versions and bare numbers are KEPT as terms: on this base they are the subject,
#     not noise. Alone it drops 2026-09-02 ROW 14 (K-90670 <-> K-92377), which the reviewer
#     APPLIED — its two quotes share no token at all, one being Czech and one English.
#   * tag channel — Jaccard of the two cards' `tags:`. Alone it is far too coarse (two cards
#     tagged `nav` are not thereby about one quantity).
#   score = max(quote_overlap, tag_jaccard) — a row passes when EITHER channel says the two
#   claims meet, which is the conservative direction: the guard drops rows, so it must be the
#   one that has to be convinced to drop, not to keep.
#
# CALIBRATION CURVE over all 57 labelled rows (full table in memory/inbox/khms-t2-report.md):
#     threshold   APPLY rows dropped   wrong-subject/agree/inverted caught (of 36)
#       0.10             0                          10
#       0.15             0                          13
#       0.20             0                          13     <- ceiling: weakest APPLY = 0.200
#       0.25             1                          14
# SUBJECT_SCORE_MIN = 0.15 is the largest threshold that catches everything 0.20 catches while
# leaving margin under the weakest human-APPLIED row (0.200 — 2026-09-03 ROW 18, K-90032 <->
# K-92436). A threshold set exactly at the minimum of the class it must never touch is not a
# calibration, it is a coincidence waiting for the next sweep.
#
# It ships OFF. Flip SHARED_SUBJECT_FILTER (or pass --shared-subject) when the orchestrator
# decides; --subject-threshold overrides the constant for a one-off measurement.
SHARED_SUBJECT_FILTER = True   # flipped 2026-09-03 by the orchestrator: 0 false drops on 57 labelled rows (T2 report)       # section 7 configuration — OFF until the orchestrator flips it
SUBJECT_SCORE_MIN = 0.15            # calibrated above; see memory/inbox/.staging/triage-labels.tsv
SUBJECT_MODEL_PASS = True   # flipped 2026-09-03: marks only (subject-unclear), precision 0.83, ~80 s per nightly          # second stage: marks rows `subject-unclear`, never drops them
# The local model (KHMS_LLM_URL / KHMS_LLM_MODEL; "" = none, and every row then keeps its place with
# the note "model pass unavailable" -- the pass marks, it never drops).
SUBJECT_MODEL_URL = kc._setting("KHMS_LLM_URL", "")
SUBJECT_MODEL_NAME = kc._setting("KHMS_LLM_MODEL", "qwen3-coder-next:latest")

# English function words; a base quoted in another language adds its own (patterns file
# TRIAGE_SUBJECT_STOP, whitespace-separated).
SUBJECT_STOP = set("""
a an the and or but if then than that this these those it its is are was were be been being am
of to in on at for from by with without into over under as not no nor so such same other another
we you they he she i our your their his her them us me my mine ours yours theirs who whom which
what when where why how all any both each few more most some only own too very can will just
should now do does did done have has had having would could may might must shall about after
before during while against between through above below up down out off again further once here
there because until also per via vs et al eg ie etc still yet even one two three
""".split()) | set(KP.words("TRIAGE_SUBJECT_STOP", ()))

_SUBJ_TOK = re.compile(r"[a-z0-9\u00c0-\u024f][a-z0-9\u00c0-\u024f_./+-]*")


def load_card(cid):
    p = f"{BRAIN}/memory/know/{cid}.md"
    if not os.path.exists(p):
        return None
    text = open(p, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    front, body = (m.group(1), m.group(2)) if m else ("", text)

    def fm(k, d=""):
        mm = re.search(rf"^{k}:\s*(.+)$", front, re.M)
        return mm.group(1).strip() if mm else d
    tags = fm("tags", "")
    tags = [t.strip() for t in tags.strip("[] ").split(",") if t.strip()]
    return {"id": cid, "type": fm("type"), "level": fm("level"), "status": fm("status"),
            "evidence": fm("evidence"), "date": fm("date"), "tags": tags,
            "body": body.strip()}


def match_claim(claims, quote):
    """Best claim for a verdict quote, by token overlap. None when nothing overlaps."""
    if not quote:
        return None
    qt = set(re.findall(r"[a-z0-9]+", quote.lower()))
    best, score = None, 0.0
    for c in claims:
        if c.kind != "claim":
            continue
        ct = set(re.findall(r"[a-z0-9]+", c.text.lower()))
        if not ct:
            continue
        s = len(qt & ct) / len(ct)
        if s > score:
            best, score = c, s
    return best if score >= 0.45 else None


# ------------------------------------------------- role coherence and per-side withholding
# MEASURED 2026-09-03 on the file this tool wrote the night before. K-92447, K-92463, K-92426
# and K-92470 each appeared as the WINNER of one row and the LOSER of another IN THE SAME
# FILE, which makes the file self-contradicting before a reviewer reads a word of it. And the
# withholding rule ("this card already has a row above") keyed on the CARD, so one bogus
# losing row silenced 14 later findings in which the same card was the winner — four of them
# severity-5 identity pairs against the measured rear-camera correction.
#
# Both defects are one shape: a card is not a row, and a row has SIDES. Roles are resolved
# first, by evidence rather than by position in the file; then the dedup keys on (card, side).
EVIDENCE_WEIGHT = {"measured": 3, "observed": 2, "reported": 1}


def row_strength(row):
    """Bigger is stronger. Severity first, then how well the WINNER is evidenced, then how
    fresh it is, then the blocking score. Position in the file is not an input."""
    return (int(row.get("severity") or 0),
            EVIDENCE_WEIGHT.get((row.get("winner_evidence") or "").strip(), 0),
            str(row.get("winner_date") or ""),
            float(row.get("score") or 0.0))


def resolve_winner_loser_conflicts(rows, strength=row_strength):
    """(kept, conflicting). A card takes the role it has in its strongest row; every row that
    gives it the opposite role is set aside and listed, never silently dropped."""
    roles = defaultdict(set)
    for r in rows:
        roles[r["loser"]].add("loser")
        roles[r["winner"]].add("winner")
    canonical = {}
    for cid, sides in roles.items():
        if len(sides) < 2:
            continue
        best = max((r for r in rows if cid in (r["loser"], r["winner"])), key=strength)
        canonical[cid] = "loser" if best["loser"] == cid else "winner"
    kept, conflicting = [], []
    for r in rows:
        bad = [cid for cid, want in canonical.items()
               if (cid == r["loser"] and want != "loser")
               or (cid == r["winner"] and want != "winner")]
        if bad:
            conflicting.append({
                "row": r,
                "reason": "; ".join(
                    f"{cid} is the {canonical[cid]} of a better-evidenced row in this file, "
                    f"so it cannot be the {'loser' if cid == r['loser'] else 'winner'} here"
                    for cid in bad)})
        else:
            kept.append(r)
    return kept, conflicting


def dedup_losers(rows):
    """(emitted, withheld). One row per LOSING card — being a winner never blocks a row."""
    emitted, withheld, seen = [], [], set()
    for r in rows:
        if (r["loser"], "loser") in seen:
            withheld.append({"row": r, "reason":
                             f"{r['loser']} already appears as a LOSER in a row above "
                             f"(higher severity first). Rows in which it WINS are unaffected."})
            continue
        seen.add((r["loser"], "loser"))
        emitted.append(r)
    return emitted, withheld



def subject_tokens(text: str) -> set:
    """Terms of a claim: stop words out, identifiers/paths/versions/numbers in."""
    out = set()
    for t in _SUBJ_TOK.findall((text or "").lower()):
        t = t.strip("._-+/")
        if not t or t in SUBJECT_STOP:
            continue
        if len(t) == 1 and not t.isdigit():
            continue
        out.add(t)
    return out


def _overlap_min(a: set, b: set) -> float:
    return len(a & b) / min(len(a), len(b)) if (a and b) else 0.0


def _jacc(a: set, b: set) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if (a or b) else 0.0


def shared_subject_score(quote_a, quote_b, tags_a=(), tags_b=()) -> float:
    """max(term overlap of the two quotes, Jaccard of the two cards' tags). See the block at
    the top of this file for why it is a max of two channels and where 0.15 comes from."""
    return max(_overlap_min(subject_tokens(quote_a), subject_tokens(quote_b)),
               _jacc(tags_a, tags_b))


def shared_subject_filter(candidates, thr=None):
    """(kept, dropped). A row whose two quotes talk past each other is not a contradiction
    that a reviewer can act on; it is a pair the generator found and the model narrated.
    Nothing is silent — each drop carries its two channel readings and the threshold."""
    thr = SUBJECT_SCORE_MIN if thr is None else thr
    kept, dropped = [], []
    for c in candidates:
        qa, qb = c.get("quote") or "", c.get("win_quote") or ""
        ov = _overlap_min(subject_tokens(qa), subject_tokens(qb))
        tj = _jacc(c.get("tags") or (), c.get("winner_tags") or ())
        s = max(ov, tj)
        c["subject_score"] = s
        if s < thr:
            dropped.append({"row": c, "reason": (
                f"no-shared-subject — the losing and winning quotes do not meet: quote-term "
                f"overlap {ov:.2f}, card-tag Jaccard {tj:.2f}, score {s:.2f} < {thr:.2f}")})
        else:
            kept.append(c)
    return kept, dropped


SUBJECT_PROMPT = """Two sentences come from two different engineering notes about {scope}.

A: {a}

B: {b}

Question: do A and B make claims about the SAME specific quantity, device, file, parameter or
object? Two sentences that merely share a general topic are NOT the same subject.

Reply with one JSON object and nothing else:
{{"same": "yes" or "no", "subject": "<the shared subject, or 'A: x / B: y' when they differ>"}}"""


def _ask_model(a, b, url=None, model=None, timeout=120):
    """(same, subject). Measured 2026-09-03: 1.6 s/row on a local 80B coder model, ~200 prompt
    tokens per call, so a nightly's ~50 rows cost about 80 s and no API budget at all."""
    import urllib.request
    if not (url or SUBJECT_MODEL_URL):
        raise OSError("no local model configured (KHMS_LLM_URL)")
    body = json.dumps({"model": model or SUBJECT_MODEL_NAME,
                       "prompt": SUBJECT_PROMPT.format(a=(a or "")[:900], b=(b or "")[:900],
                                                       scope=KM.msg("TRIAGE_SUBJECT_SCOPE")),
                       "stream": False,
                       "options": {"num_ctx": 4096, "temperature": 0.1}}).encode()
    req = urllib.request.Request(url or SUBJECT_MODEL_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = (json.loads(r.read()).get("response") or "")
    i, j = txt.find("{"), txt.rfind("}")
    if i >= 0 and j > i:
        try:
            o = json.loads(txt[i:j + 1])
            same = str(o.get("same", "")).strip().lower()
            if same in ("yes", "no"):
                return same, str(o.get("subject", ""))[:160]
        except ValueError:
            pass
    return "unclear", ""


def subject_model_pass(rows, ask=None):
    """(ok, unclear). The second stage MARKS, it never drops: measured on the labelled set it
    calls 'no' on 2 of 9 rows a human applied, so a drop here would cost real findings. An
    unreachable model leaves every row exactly where it was — a guard that fails closed on a
    network error would delete the night's work."""
    ask = ask or _ask_model
    ok, unclear = [], []
    for r in rows:
        try:
            same, subj = ask(r.get("quote") or "", r.get("win_quote") or "")
        except Exception as e:                                    # noqa: BLE001
            r["subject_note"] = f"model pass unavailable ({type(e).__name__}) — row untouched"
            ok.append(r)
            continue
        if same == "no":
            r["subject_unclear"] = True
            r["subject_note"] = subj or "the model could not name a subject the two claims share"
            unclear.append(r)
        else:
            ok.append(r)
    return ok, unclear


# ------------------------------------------------- what the sweep ACTUALLY judged
# MEASURED 2026-09-20. This file rendered "Sweep: 12 pairs adjudicated · 0 confirmed
# contradictions · 12 unclear" over twelve pairs THE MODEL NEVER ANSWERED, and the words
# UNANSWERED and DEADLINE appeared in the rendered proposal exactly zero times. The producer
# had named every one of those outcomes (khms_contradict.py OUTCOME_*, same day) and carried
# them in the JSON; the truth was lost HERE, in the consumer, in two places:
#   1. `len(results)` was printed as "pairs adjudicated" — but a pair the run marked DEADLINE
#      is in `results` precisely so that it is not forgotten, having never been sent at all;
#   2. the producer's deliberate `contradiction: "unclear"` was read as "a model looked and
#      hesitated", which is what "unclear" means for an OK outcome and what it never means
#      for the other five.
# So the morning saw a memory that had been checked, over a night where a third of the plan
# went nowhere. K-90585: a check whose passing condition is met whether or not the guarded
# thing is healthy is not a check. K-90770: "unmeasured" must never collapse into a verdict.
#
# A pair carries a judgement ONLY if its outcome is OK. Every other named outcome — and every
# name this file has never heard of — is NOT a judgement. A new name invented upstream will
# therefore show up as "not judged" rather than silently join the clean pile.
OUTCOME_JUDGED = "OK"
# Sweeps written before 2026-09-20 carry no `outcome` key at all, and every result in one of
# them IS a model verdict: a MISSING key reads as OK, a PRESENT unknown one never does.
NOT_JUDGED_CLASSES = [
    ("never sent to the model", ("DEADLINE", "BUDGET"),
     "the run's own deadline arrived before this pair's turn, or the paid judge's per-night "
     "spend bound refused the call (#208)"),
    ("sent, but no answer came back", ("UNANSWERED", "LLM-ERROR"),
     "the model did not answer inside its timeout, or the endpoint refused the call"),
    ("answered, but no usable verdict for this pair", ("TRUNCATED", "NO-VERDICT"),
     "the reply was cut off mid-listing, or it said nothing about this pair"),
]


def judgement_census(results, meta):
    """Split a sweep's planned pairs into judged / not judged / never recorded.

    The identity `judged + not_judged + no_record == planned` is computed here and printed in
    the proposal, because a header whose numbers add up is the one kind of header a reader
    can check without opening the JSON.
    """
    progress = meta.get("progress") or {}
    sel = meta.get("selection") or {}
    planned = progress.get("planned")
    if not isinstance(planned, int):
        planned = 0
    # What is on disk is the FLOOR of the plan, never its ceiling: a legacy sweep has no
    # progress meta at all, and a plan smaller than the recorded results would be nonsense.
    planned = max(planned, len(results), sel.get("adjudicated") or 0)

    by_outcome, examples = Counter(), defaultdict(list)
    for r in results:
        oc = (r.get("verdict") or {}).get("outcome") or OUTCOME_JUDGED
        by_outcome[oc] += 1
        examples[oc].append(r)

    classes, known = [], {OUTCOME_JUDGED}
    for label, names, why in NOT_JUDGED_CLASSES:
        known.update(names)
        members = [(n, by_outcome[n]) for n in names if by_outcome.get(n)]
        if members:
            classes.append({
                "label": label, "why": why,
                "n": sum(c for _, c in members),
                "names": members,
                "examples": [r for n, _ in members for r in examples[n]]})
    other = [(n, c) for n, c in sorted(by_outcome.items()) if n not in known]
    if other:
        classes.append({
            "label": "outcome this report does not recognise — counted as NOT judged",
            "why": "the sweep named an outcome newer than this reader; it is never assumed "
                   "to be a clean verdict",
            "n": sum(c for _, c in other), "names": other,
            "examples": [r for n, _ in other for r in examples[n]]})

    judged = by_outcome.get(OUTCOME_JUDGED, 0)
    not_judged = sum(c["n"] for c in classes)
    # Pairs the run planned to send and for which nothing at all reached the file: the sweep
    # was killed between two checkpoints. They are not in `results`, so nothing else counts
    # them — and they are the least visible failure of the lot.
    no_record = max(0, planned - len(results))
    model_unclear = sum(
        1 for r in results
        if (r.get("verdict") or {}).get("contradiction") == "unclear"
        and ((r.get("verdict") or {}).get("outcome") or OUTCOME_JUDGED) == OUTCOME_JUDGED)
    return {"planned": planned, "judged": judged, "not_judged": not_judged,
            "no_record": no_record, "classes": classes, "by_outcome": dict(by_outcome),
            "model_unclear": model_unclear,
            "partial": bool(progress.get("partial")),
            "adds_up": judged + not_judged + no_record == planned}


def census_sum_line(c):
    """The arithmetic, spelled out, so the reader can add it up themselves."""
    parts = [f"{c['judged']} judged"]
    parts += [f"{k['n']} {k['label']}" for k in c["classes"]]
    if c["no_record"]:
        parts.append(f"{c['no_record']} never recorded at all")
    line = f"Adds up: {' + '.join(parts)} = {c['planned']} planned."
    if not c["adds_up"]:                       # pragma: no cover - a defect, said out loud
        line += (" **THESE DO NOT ADD UP** — the sweep JSON is inconsistent with itself; "
                 "treat this whole file as unverified and say so in the review.")
    return line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sweep_json")
    ap.add_argument("--provenance")
    ap.add_argument("--out")
    ap.add_argument("--min-severity", type=int, default=3)
    ap.add_argument("--fallback-from", default=None,
                    help="the configured judge of record that judged nothing, so this sweep "
                         "was re-run with the judge named in its meta (nightly_triage.sh, #208)")
    ap.add_argument("--shared-subject", dest="shared_subject", action="store_true",
                    default=SHARED_SUBJECT_FILTER,
                    help="drop rows whose two quotes share no subject (calibrated, ships OFF)")
    ap.add_argument("--no-shared-subject", dest="shared_subject", action="store_false")
    ap.add_argument("--subject-threshold", type=float, default=SUBJECT_SCORE_MIN)
    ap.add_argument("--subject-model", dest="subject_model", action="store_true",
                    default=SUBJECT_MODEL_PASS,
                    help="second pass on the local model (KHMS_LLM_URL): MARK rows as "
                         "subject-unclear (never drops)")
    ap.add_argument("--no-subject-model", dest="subject_model", action="store_false")
    ap.add_argument("--triage-gate", dest="triage_gate", action="store_true", default=True,
                    help="deterministic row gate (tools/khms_triage_gate.py; rules in force: "
                         "KHMS_TRIAGE_GATE or the module's defaults)")
    ap.add_argument("--no-triage-gate", dest="triage_gate", action="store_false")
    ap.add_argument("--tomb-ledger", default=T.LEDGER,
                    help="tombstone ledger to number against (default: the real one)")
    ap.add_argument("--tomb-counter", default=T.TS_COUNTER,
                    help="tombstone id counter. Proposing a tombstone ADVANCES it, because "
                         "the number is published into memory/inbox/ and must never be "
                         "handed to a second error; point both of these at a scratch "
                         "directory for a dry re-render.")
    args = ap.parse_args()
    # The morning file is a detached report: a reader may write it outside the base only. Checked before
    # the first computation, so a reader never depends on a later refusal (the tombstone reservation) to
    # keep its output out of the base. The tombstone counter itself is reserved by khms_alloc, which
    # refuses a reader on its own.
    P.require_may_write([args.out], "write the triage report")

    raw = json.load(open(args.sweep_json, encoding="utf-8"))
    # Envelope since 2026-08-31 ({"meta":…, "results":[…]}); a bare list is the older shape.
    meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
    results = raw["results"] if isinstance(raw, dict) else raw
    conf = [r for r in results if (r.get("verdict") or {}).get("contradiction") == "yes"]
    conf.sort(key=lambda r: (-int(r["verdict"].get("severity", 0) or 0), -r.get("score", 0)))
    today = datetime.date.today().isoformat()
    # WHAT THE SWEEP ACTUALLY JUDGED, before anything is said about what it found.
    cen = judgement_census(results, meta)
    # EVERY DROP IS RECORDED WITH ITS REASON. Before this, a confirmed contradiction could
    # fail five separate `continue`s without leaving a trace, and on the 2026-08-31 sweep
    # exactly that happened to 30 of 99 confirmed findings. A pipeline whose findings do not
    # reach the one artefact a human reads is a pipeline that only looks like it works;
    # withholding is allowed, silence about it is not.
    withheld: list[tuple[str, str]] = []

    L = [f"# KHMS triage proposals — {today}", "",
         "**NOTHING IN `memory/know/` WAS CHANGED BY THIS FILE.** It is a proposal set for the "
         "morning review, in the same contract as every other nightly output (spec §9.2). "
         "Approving an item means a human performs the edit; the job never does.", "",
         f"Sweep: **{cen['judged']} of {cen['planned']} planned pairs were actually judged** "
         f"· {len(conf)} confirmed contradictions · {cen['model_unclear']} the model judged "
         f"and could not decide.", ""]
    # WHO JUDGED (#208 SONNETJUDGE): the judge of record became a switch, and a night can fall
    # back to the local judge — the reviewer has to know whose verdicts these are, and what they cost.
    jdesc = (meta.get("bounds") or {}).get("judge") or "local (sweep predates the judge switch)"
    jl = f"Judge: **{jdesc.split(':')[0]}** ({jdesc})"
    jb = (meta.get("bounds") or {}).get("judge_budget")
    if jb:
        jl += (f" · spent ${jb.get('charged_usd', 0):.4f} this run, "
               f"${jb.get('night_charged_usd', 0):.4f} this night of an estimated bound of "
               f"${jb.get('max_usd', 0):.2f} (not a guaranteed hard cap)"
               + (f" · **stopped: {jb['stop']}**" if jb.get("stop") else ""))
    L += [jl, ""]
    # WHICH POPULATION (#208 P208F): the night's mode as the sweep recorded it — "full" every
    # night since 2026-10-05. A JSON from before that has no field and says so; its `focus: null`
    # is NOT proof of a full sweep (an empty --since window also wrote null, handoff-PD.md).
    L += [f"Mode: **{meta.get('mode') or 'not recorded (sweep predates the mode field)'}**", ""]
    if getattr(args, "fallback_from", None):
        L += [f"> ⚠️ **FALLBACK: the judge of record `{args.fallback_from}` judged nothing "
              f"tonight** (see nightly.log and the kept `*.{args.fallback_from}.json`), so "
              f"these verdicts are the local model's.", ""]
    if cen["not_judged"] or cen["no_record"]:
        L += [f"> ⚠️ **{cen['not_judged'] + cen['no_record']} of the {cen['planned']} planned "
              f"pairs carry NO judgement at all.** Not one of them is a pair that was "
              f"cleared — nothing looked at them, or nothing came back. They are listed "
              f"under \"Not judged\" below and are NOT counted as adjudicated anywhere in "
              f"this file.", "", census_sum_line(cen), ""]
    else:
        L += [census_sum_line(cen), ""]
    if cen["partial"]:
        L += ["> ⚠️ **The sweep marked its own report PARTIAL** — it was still running when "
              "this JSON was written. Everything below is a slice of a plan that did not "
              "finish.", ""]

    sel = meta.get("selection") or {}
    if sel:
        L += ["## Coverage — what this sweep did NOT look at", "",
              f"- candidate pairs generated: **{sel.get('candidate_pairs','?')}**",
              f"- sent to the model: **{sel.get('adjudicated','?')}**",
              *([f"- skipped, judged in the last {sk['nights']} nights and unchanged since: "
                 f"**{sk.get('skipped', 0)}** (already left out of the candidate count above; "
                 f"{sk['card_changed_since']} pairs judged then are eligible again because "
                 f"a card changed)"] if (sk := sel.get("skipped_recently_judged")) else []),
              f"- **not adjudicated: {sel.get('not_adjudicated','?')}** — {sel.get('reason','')}",
              "",
              "A pair that was not adjudicated is **not** a pair that was cleared. Counts by "
              "shape of what the budget did not reach:", ""]
        for s, n in (sel.get("not_adjudicated_by_shape") or {}).items():
            L.append(f"    - {s}: {n}")
        L += ["", f"Budget actually spent per reserved class: {sel.get('quota_used', {})}", ""]
        # "sent to the model" is a statement about the PLAN — selection happens before the
        # first call. What came back is a different number, and until 2026-09-20 this file
        # printed only the first one.
        L += [f"Of those **{sel.get('adjudicated','?')}** planned pairs, "
              f"**{cen['judged']} came back judged**; "
              f"**{cen['not_judged'] + cen['no_record']} did not** — see \"Not judged\".", ""]

    # ---- pairs with no verdict: the plan minus what actually came back
    if cen["not_judged"] or cen["no_record"]:
        L += ["## Not judged — planned pairs that carry no verdict", "",
              "**Nothing on this list means \"no contradiction found\".** These pairs were "
              "selected for the model and then were never sent, or were sent and nothing "
              "usable came back. They are printed with their ids so the same pairs can be "
              "re-run; they are excluded from every \"judged\" count in this file. Two cards "
              "that contradict each other are as likely to sit here as in the list above — "
              "the difference is that nobody has looked.", ""]
        for k in cen["classes"]:
            names = ", ".join(f"`{n}`×{c}" for n, c in k["names"])
            L.append(f"- **{k['n']}× {k['label']}** ({names}) — {k['why']}")
            for r in k["examples"][:12]:
                why = ((r.get("verdict") or {}).get("why") or "")[:150]
                L.append(f"    - {r.get('a','?')} ⇄ {r.get('b','?')}"
                         + (f" — {why}" if why else ""))
            if k["n"] > 12:
                L.append(f"    - … and {k['n'] - 12} more")
        if cen["no_record"]:
            L.append(f"- **{cen['no_record']}× never written to the sweep file at all** — the "
                     f"run planned {cen['planned']} pairs and recorded {len(results)}. These "
                     f"have no ids to print, which is exactly what makes them the easiest "
                     f"kind of gap to miss: the sweep was interrupted between two "
                     f"checkpoints and nothing downstream would otherwise count them.")
        L += ["", census_sum_line(cen), ""]

    # ---- clusters
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for r in conf:
        a, b = find(r["a"]), find(r["b"])
        if a != b:
            parent[a] = b
    comps = defaultdict(list)
    for r in conf:
        comps[find(r["a"])].append(r)
    multi = sorted((c for c in comps.values()
                    if len({r['a'] for r in c} | {r['b'] for r in c}) >= 3),
                   key=lambda c: -len(c))
    if multi:
        L += ["## Clusters — one subject asserted three or more ways", ""]
        for c in multi[:15]:
            ids = sorted({r["a"] for r in c} | {r["b"] for r in c})
            subj = Counter(r["verdict"].get("subject", "?") for r in c).most_common(1)[0][0]
            L.append(f"- **{subj}** — {', '.join(ids)} ({len(c)} edges)")
        L.append("")

    # ---- triage of the losing side
    L += ["## Triage — claim level", "",
          "Buckets: `FALSE_FACT` retire the CLAIM (tombstone) · `REFUTED_HYPOTHESIS` / "
          "`REJECTED_APPROACH` KEEP in know/, typed as a dead end · `UNCONFIRMED_CONJECTURE` "
          "keep, mark, and state the falsifier.", ""]
    tomb_lines = []
    ledger = T.load_ledger(args.tomb_ledger)
    n_amend = n_retire = n_dead = n_conj = 0

    # PHASE 1 — candidate rows. Only filters that are properties of ONE row live here; the
    # ones that compare rows against each other (role coherence, per-side dedup) cannot be
    # decided while walking the list, which is how a card ended up on both sides of one file.
    candidates = []
    for n, r in enumerate(conf):
        v = r["verdict"]
        tag = f"{r['a']} ⇄ {r['b']} (severity {v.get('severity','?')}, " \
              f"{v.get('shape','?')}, subject: {v.get('subject','?')})"
        if n >= 120:
            withheld.append((tag, "render cap: only the first 120 confirmed rows are triaged"))
            continue
        if int(v.get("severity", 0) or 0) < args.min_severity:
            withheld.append((tag, f"severity below --min-severity {args.min_severity}"))
            continue
        side = v.get("likely_wrong")
        loser_id = r["a"] if side == "a" else (r["b"] if side == "b" else None)
        if not loser_id:
            withheld.append((tag, "the model could not say which side is wrong "
                                  "(likely_wrong=unclear) — triage needs a losing card"))
            continue
        card = load_card(loser_id)
        if not card:
            withheld.append((tag, f"no card file memory/know/{loser_id}.md"))
            continue
        winner_id = r["b"] if side == "a" else r["a"]
        wcard = load_card(winner_id) or {}
        candidates.append({
            "r": r, "v": v, "tag": tag, "side": side,
            "loser": loser_id, "winner": winner_id, "card": card,
            "severity": int(v.get("severity", 0) or 0), "score": r.get("score", 0),
            "winner_evidence": wcard.get("evidence", ""), "winner_date": wcard.get("date", ""),
            "quote": v.get("quote_a") if side == "a" else v.get("quote_b"),
            "win_quote": v.get("quote_b") if side == "a" else v.get("quote_a"),
            "tags": card.get("tags") or [], "winner_tags": wcard.get("tags") or [],
            "wcard": wcard,
        })

    # PHASE 1.5 — the shared-subject guard, AFTER the quote-integrity check (which runs in
    # khms_contradict.py and marks its casualties `contradiction: dropped`, so they never
    # reach `conf`). Integrity asks whether the quote is really in its own card; this asks
    # whether the two quotes are about one thing. Off by default; see the block at the top.
    subj_dropped = []
    if args.shared_subject:
        candidates, subj_dropped = shared_subject_filter(candidates, args.subject_threshold)
        for c in subj_dropped:
            withheld.append((c["row"]["tag"], c["reason"]))

    # PHASE 1.6 — the deterministic triage gate (tools/khms_triage_gate.py, issue #208).
    # Rules a reviewer applied by hand on 602 rejected rows over 28 nights, now in code: a
    # decision cannot make a card false, a winner that is itself in doubt refutes nothing,
    # two readings of a changing quantity on two dates are two measurements. Each refusal is
    # listed under Withheld with its rule and evidence; measured cost per rule:
    # `tools/eval_triage_selector.py --per-rule`.
    gate_dropped = []
    if args.triage_gate:
        kept_g = []
        for c in candidates:
            hits = G.row_gate(c["card"], c.get("wcard") or {"id": c["winner"]},
                              c.get("quote") or "", c.get("win_quote") or "") \
                if c.get("wcard") else []
            if hits:
                gate_dropped.append(c)
                withheld.append((c["tag"], "triage gate — " +
                                 "; ".join(f"{n}: {e}" for n, e in hits)))
            else:
                kept_g.append(c)
        candidates = kept_g

    # PHASE 2 — coherence across the whole file, then one row per LOSING card.
    kept, conflicting = resolve_winner_loser_conflicts(candidates)
    rows, deduped = dedup_losers(kept)
    for c in conflicting:
        withheld.append((c["row"]["tag"], "role conflict — " + c["reason"]))
    for c in deduped:
        withheld.append((c["row"]["tag"], c["reason"]))

    # PHASE 3 — the model pass. It MARKS: a row it cannot resolve is moved below the list, not
    # removed, because on the calibration set it calls "no" on 2 of 9 rows a human applied.
    rows_unclear = []
    if args.subject_model:
        rows, rows_unclear = subject_model_pass(rows)

    def emit(subset):
      nonlocal L, n_amend, n_retire, n_dead, n_conj
      for row in subset:
        r, v, side = row["r"], row["v"], row["side"]
        loser_id, other, card = row["loser"], row["winner"], row["card"]
        claims = T.split_claims(card["body"])
        quote = row.get("quote")
        win_quote = row.get("win_quote")
        hit = match_claim(claims, quote)
        res = T.triage_card(card, false_claim_idx=[hit.idx] if hit else [],
                            refuted_by_evidence=True)
        L += [f"### {loser_id} — action **{res.action}** "
              f"(contradicted by {other}, severity {v.get('severity')})",
              f"- subject: {v.get('subject','?')}",
              f"- offending quote: \"{(quote or '')[:220]}\"",
              f"- winning quote ({other}): \"{(win_quote or '')[:220]}\"",
              f"- reason: {' | '.join(res.reasons)[:500]}"]
        if row.get("subject_unclear"):
            L.append(f"- **subject-unclear** — the model pass could not name a quantity or "
                     f"object these two claims share: {row.get('subject_note','')}")
        if res.action == "AMEND":
            n_amend += 1
            L += [f"- **claims judged false ({len(res.false_claims)}):**"]
            L += [f"    - {c.text[:200]}" for c in res.false_claims]
            L += [f"- **claims that must survive into the superseding card "
                  f"({len(res.surviving_claims)}):**"]
            L += [f"    - {c.text[:160]}" for c in res.surviving_claims[:20]]
            L += ["- proposed: a NEW card carrying the surviving claims plus the correction, "
                  f"`supersedes: {loser_id}`, body containing "
                  f"`SUPERSEDES {loser_id} BECAUSE: <one false claim, corrected>`; "
                  f"{loser_id} → `status: superseded`. The card is NOT deleted."]
        elif res.action == "RETIRE_WHOLE":
            n_retire += 1
            L.append("- proposed: `status: refuted`, `refuted_by: [%s]`, move to "
                     "memory/archive/know/ (fog). Every claim in it is false." % other)
        elif res.action == "MARK_DEAD_END":
            n_dead += 1
            L.append("- proposed: `status: refuted`, `refuted_by: [%s]`, **stays in "
                     "memory/know/** as a signposted dead end. Its value is the refutation: "
                     "delete it and the same blind alley gets walked again." % other)
        elif res.action == "MARK_CONJECTURE":
            n_conj += 1
            L.append("- proposed: `level: assumption`, `status: challenged`, and a "
                     "`FALSIFIER:` line naming what would confirm or refute it.")
        # tombstone only for genuinely false claims
        for c in res.false_claims:
            # `ledger` is loaded once and passed to every call ON PURPOSE: propose_tombstone
            # appends what it allocates, so this loop's own proposals are visible to the
            # calls after them. Before 2026-09-05 it did not, and this loop published seven
            # tombstones all numbered TS-00001.
            line, is_rec, ts = T.propose_tombstone(
                card, claim=c.text[:200],
                wrong=(v.get("why") or "")[:200], truth=other,
                keys=(r.get("keys") or [])[:6], today=today, ledger=ledger,
                counter=args.tomb_counter)
            tomb_lines.append((line, is_rec, ts))
        L.append("")


    emit(rows)
    if rows_unclear:
        L += ["## Subject unclear — a second model pass could not name a shared subject", "",
              f"**{len(rows_unclear)} rows are listed here instead of above, and NOTHING has "
              "been dropped.** Each one passed the deterministic shared-subject guard, and a "
              "second cheap pass over the two quotes could not say what quantity or object "
              "they both talk about. Measured on the 57 human-adjudicated rows of "
              "2026-09-02/03, that pass says \"no\" to 2 of 9 rows the reviewer APPLIED — "
              "which is exactly why it sorts rather than deletes. Read these with the same "
              "attention as the list above; they are simply the ones to read second.", ""]
        emit(rows_unclear)

    # ---- supersession candidates: a decision-type card was the would-be winner. The gate
    # refuses the REFUTE (a decision makes a card obsolete, never false — K-92816), but the
    # successor-card debt is real and must stay findable, not vanish into a count (Diblin's
    # review of #208, P2 #4, 2026-10-01). Two sources: rows the row gate refused after the
    # model, and pairs of two decision-type cards the pair gate never sent to the model.
    sup_rows = [c for c in gate_dropped
                if any(n == "decision-winner" for n, _ in
                       G.row_gate(c["card"], c.get("wcard") or {"id": c["winner"]},
                                  c.get("quote") or "", c.get("win_quote") or "",
                                  rules=["decision-winner"]))]
    pre = ((meta.get("selection") or {}).get("gated_before_model") or {})
    sup_pairs = [p for p in (pre.get("refused_pairs") or []) if "decision-winner" in p[2]]
    if sup_rows or sup_pairs:
        L += ["## Supersession candidates — a decision-type card may have replaced the other", "",
              "Not contradictions: the gate refused a REFUTE because the would-be winner is a "
              "decision, policy, principle, requirement or overview card. Each line is a possible "
              "SUCCESSOR-CARD debt: if the newer card replaced the older one, the honest edit is a "
              "hand-written `supersedes` link (apply_triage cannot write it), never `refuted_by`.",
              ""]
        for c in sup_rows:
            L.append(f"- {c['winner']} (`{(c.get('wcard') or {}).get('type', '?')}`, "
                     f"{c.get('winner_date') or '?'}) may supersede {c['loser']} "
                     f"(`{c['card'].get('type', '?')}`, {c['card'].get('date', '?')}) — "
                     f"judged a contradiction by the model; {c['tag']}")
        if sup_pairs:
            L.append(f"- **{len(sup_pairs)} candidate pair(s) of two decision-type cards were "
                     f"not sent to the model** (pair gate; NOT judged — most are blocking-stage "
                     f"neighbours, not findings). Full list `[a, b, rules]`: sweep JSON "
                     f"`meta.selection.gated_before_model.refused_pairs`. First ones:")
            for p in sup_pairs[:15]:
                L.append(f"    - {p[0]} ⇄ {p[1]}")
        L.append("")

    # ---- withheld ledger: confirmed findings that produced no row above
    L += ["## Withheld — confirmed findings this file does not triage", ""]
    if withheld:
        L += [f"**{len(withheld)} of {len(conf)} confirmed contradictions produced no triage "
              "row.** Each is listed with the reason it was dropped. None of them has been "
              "judged harmless; they were filtered by a rule, and the rule is named.", ""]
        by_reason = defaultdict(list)
        for tag, why in withheld:
            by_reason[why].append(tag)
        for why, tags in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            L.append(f"- **{len(tags)}× {why}**")
            for t in tags[:12]:
                L.append(f"    - {t}")
            if len(tags) > 12:
                L.append(f"    - … and {len(tags)-12} more")
        L.append("")
    else:
        L += ["_none — every confirmed contradiction above produced a triage row._", ""]

    # ---- integrity drops: verdicts whose own quotes did not survive a lookup
    dropped = [r for r in results if (r.get("verdict") or {}).get("contradiction") == "dropped"]
    integ = meta.get("integrity") or {}
    if dropped or integ:
        L += ["## Dropped by the quote-integrity check (before any triage)", "",
              "A verdict is a claim about two cards; the part of it that can be checked "
              "without a model is whether the quoted evidence exists in the card it is "
              "attached to. On 2026-09-03 four rows quoted text from the OTHER card — "
              "including both `RETIRE_WHOLE` rows, i.e. the two most destructive proposals "
              "in that file — and seven more quoted two passages that agree. These rows are "
              "listed, not hidden: the sweep found the pair, the verdict is what failed.", "",
              f"kept {integ.get('kept', '?')} of {integ.get('checked', '?')} confirmed rows · "
              f"quote-not-in-card {integ.get('quote-not-in-card', 0)} · "
              f"quotes-agree {integ.get('quotes-agree', 0)}", ""]
        for r in dropped[:40]:
            v = r["verdict"]
            L.append(f"- {r['a']} ⇄ {r['b']} (severity {v.get('severity','?')}, "
                     f"{v.get('shape','?')}): **{v.get('dropped_class','?')}** — "
                     f"{v.get('dropped_reason','')}")
        if len(dropped) > 40:
            L.append(f"- … and {len(dropped)-40} more")
        L.append("")

    # ---- deterministic findings: zero-token evidence that needs no model budget
    det = meta.get("deterministic") or {}
    if det.get("groups"):
        L += ["## Deterministic flags — a correction exists and the corrected card is "
              "still active", "",
              "Zero tokens, no model, no ranking budget: a card in the base states that an "
              "earlier claim was wrong, the phrase sits next to a subject key it SHARES with "
              "another card, that other card predates it, is still `active`, and carries no "
              "`contradicts` / `refuted_by` / `supersedes` link back. This is the 2026-08-31 "
              "failure exactly — the correction was in the base, nothing tied it to what it "
              "corrected, and the wrong claim was believed again months later. These are "
              "LEXICAL flags, not model verdicts: read them as \"check this\", not as "
              "\"this is false\".", "",
              f"{det.get('shown')} of {det.get('total_groups')} correcting cards shown, "
              "ranked by how specifically the correction names its subject (a USB id or a "
              "`/dev` node first, then an identifier, then a proper name). "
              f"Not shown: {det.get('not_shown', 0)} — {det.get('not_shown_reason','')}. "
              f"A further {det.get('prose_key_pairs_skipped', 0)} correction phrases landed "
              "on an ordinary prose word rather than on a subject and are not findings.", ""]
        for g in det["groups"]:
            L.append(f"- **{g['corrector']}** corrects "
                     f"{', '.join(repr(p) for p in g['phrases'])} about "
                     f"{', '.join('`%s`' % k for k in g['keys'][:4])} "
                     f"({g.get('best_class','?')}) — still active and "
                     f"unlinked: {', '.join(g['victims'][:12])}"
                     + (f" … (+{len(g['victims'])-12})" if len(g['victims']) > 12 else ""))
        L.append("")

    L += ["## Tombstones proposed", "",
          "A tombstone is **not a card**. It lives in `memory/tombstones/`, outside both "
          "directories the card loader globs, so it can never be served to the recall surface "
          "as an assertion — only as a record that the error happened. `recurrences` is the "
          "point: a repeat appends a date to the SAME tombstone, so two dates on one line is "
          "the machine-readable statement that the previous countermeasure failed.", ""]
    if tomb_lines:
        L.append("```")
        for line, is_rec, ts in tomb_lines:
            L.append(("RECURRENCE " if is_rec else "NEW        ") + line)
        L.append("```")
    else:
        L.append("_none_")
    L.append("")

    if args.provenance and os.path.exists(args.provenance):
        L += ["## Unverified against the machine (no contradicting card — yet)", "",
              "Cards whose evidence is `reported` from a document (vendor page, datasheet, "
              "config, drawing) that assert numbers a tape measure or a register read could "
              "settle. On 2026-08-31 five separate errors had exactly this provenance — a "
              "document describing an intention or a catalogue, read as a record of the "
              "machine — and the machine won every time. These are next month's "
              "contradictions.", ""]
        L.append(open(args.provenance, encoding="utf-8").read().split("\n", 2)[-1])

    L += ["", "## Counts", "",
          f"- planned pairs: {cen['planned']} · **actually judged: {cen['judged']}** · "
          f"no judgement: {cen['not_judged'] + cen['no_record']}",
          f"  (by outcome: {cen['by_outcome'] or 'none named — a sweep older than 2026-09-20'}"
          + (f", plus {cen['no_record']} never recorded" if cen["no_record"] else "") + ")",
          f"- AMEND (card kept, one or more claims corrected): {n_amend}",
          f"- RETIRE_WHOLE (every claim false): {n_retire}",
          f"- MARK_DEAD_END (kept in know/, typed): {n_dead}",
          f"- MARK_CONJECTURE: {n_conj}",
          f"- tombstones proposed: {len(tomb_lines)} "
          f"({sum(1 for _, r, _ in tomb_lines if r)} recurrences)",
          f"- confirmed findings withheld from triage (listed above): {len(withheld)}",
          f"  (of which role conflicts: {len(conflicting)}, "
          f"same card losing twice: {len(deduped)})",
          f"- verdicts dropped by the quote-integrity check: {len(dropped)}",
          f"- rows dropped by the shared-subject guard: {len(subj_dropped)} "
          f"(threshold {args.subject_threshold}, guard "
          f"{'ON' if args.shared_subject else 'OFF — calibrated, awaiting the flip'})",
          f"- rows refused by the triage gate: {len(gate_dropped)} "
          f"(gate {'ON, rules ' + ', '.join(G.enabled_rules()) if args.triage_gate else 'OFF'};"
          f" each one is listed under Withheld with its rule)",
          f"- rows marked subject-unclear by the model pass: {len(rows_unclear)} "
          f"(pass {'ON' if args.subject_model else 'OFF'}; such rows are sorted below the "
          f"list, never dropped)",
          f"- candidate pairs never sent to the model: "
          f"{sel.get('not_adjudicated', 'unknown — sweep JSON predates the coverage meta')}",
          f"- deterministic correction-orphan groups: {det.get('total_groups', 0)}"]

    out = "\n".join(L) + "\n"
    # The census goes to stderr too, so that nightly.log carries it without the shell having
    # to re-derive it. A log that says "triage done" over a night where nothing was judged is
    # the same lie as a proposal that does (rule #5: the log is the first step of diagnosis).
    census = (f"judged {cen['judged']}/{cen['planned']} planned"
              f" · no judgement {cen['not_judged'] + cen['no_record']}"
              f" · outcomes {cen['by_outcome'] or 'unnamed (pre-2026-09-20 sweep)'}"
              + (f" · never recorded {cen['no_record']}" if cen["no_record"] else "")
              + (" · SWEEP SELF-REPORTED PARTIAL" if cen["partial"] else ""))
    if args.out:
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        open(args.out, "w", encoding="utf-8").write(out)
        print(f"wrote {args.out} ({len(out)} B) — {census}", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
