#!/usr/bin/env python3
"""KHMS three-way triage — "untrue" is three different objects and they must not be alike.

Usage: python3 tools/khms_triage.py <card.md ...> [--refuted]   (also imported by khms_lint, khms_erratum)

Owner, 2026-08-31: "uplny nepravdy fakt vyhodit a nechat jenom cisty informace a pravdy.
mozna domnenky, dokud se nepotvrdi nebo nevyvrati."

  1 FALSE_FACT             an assertion about the world that is simply wrong.
                           No residual value -> RETIRE from the recall surface + TOMBSTONE.
  2 REFUTED_HYPOTHESIS     "we thought the driver was hiding the mode; it was not."
    REJECTED_APPROACH      "we chose B over A, and here is why A lost."
                           KEPT in know/, typed as a dead end. Its value IS the refutation:
                           delete it and someone searches the driver again.
  3 UNCONFIRMED_CONJECTURE kept, marked, and carrying WHAT WOULD CONFIRM OR REFUTE IT.

THE ASYMMETRY IS THE WHOLE SAFETY DESIGN. FALSE_FACT requires the ABSENCE of every
attempt-marker plus positive external evidence that the claim is false. There is no code
path from an attempt-marked card to FALSE_FACT — see `classify`. Getting that wrong in the
other direction would delete the base's most valuable content, so the classifier is built to
fail towards "keep and mark", never towards "remove".

A second, structural brake: this module PROPOSES. It never writes to memory/know/, and it
never writes a tombstone. The ONE file it does write is the tombstone id counter
(`TS_COUNTER`), because a number that has been published into memory/inbox/ must never be
handed to a second error — see `next_ts_id`.
"""
from __future__ import annotations

import datetime
import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402
import khms_patterns as KP  # noqa: E402

BRAIN = P.ROOT
TOMB_DIR = f"{BRAIN}/memory/tombstones"
LEDGER = f"{TOMB_DIR}/LEDGER.md"
# The tombstone id counter. It lives BESIDE the ledger it numbers, not in tools/, so that a
# restore of memory/ (brain_snapshot.sh --restore; the base is not in git) brings the ledger
# and the number that must not collide with it back together.
TS_COUNTER = f"{TOMB_DIR}/.next_ts_id"

FALSE_FACT = "FALSE_FACT"
REFUTED_HYPOTHESIS = "REFUTED_HYPOTHESIS"
REJECTED_APPROACH = "REJECTED_APPROACH"
UNCONFIRMED_CONJECTURE = "UNCONFIRMED_CONJECTURE"
STILL_TRUE = "STILL_TRUE"

ACTION = {
    FALSE_FACT: "RETIRE",                    # status=refuted, move to fog, tombstone
    REFUTED_HYPOTHESIS: "MARK_DEAD_END",     # status=refuted, STAYS in know/
    REJECTED_APPROACH: "MARK_DEAD_END",
    UNCONFIRMED_CONJECTURE: "MARK_CONJECTURE",  # level=assumption, status=challenged
    STILL_TRUE: "NONE",
}

# --- marker vocabularies ----------------------------------------------------
# Anything here means the card is ABOUT AN ATTEMPT, not about the world.
ATTEMPT = re.compile(
    r"\b(hypothes\w+|assum\w+|presum\w+|suspect\w+|suspicion|expect\w+|"
    r"theor\w+|conjectur\w+|tried|trying|attempt\w+|we looked|looked for|searched|"
    r"search(?:ed)? the|proposed|proposal|candidate explanation|ruled out|dead end|"
    r"did ?n[o']t work|didn't work|turned out (?:not|to be)|"
    r"disproven|disproved|refuted|falsified|"
    r"initially read as|first (?:guess|thought)|we thought|believed"
    + KP.extra("TRIAGE_ATTEMPT") + r")\b", re.I | re.UNICODE)

# A CHOICE that was made against something. These count as attempt-markers in their own
# right: "this approach was abandoned; the alternative was chosen instead" carries no
# hypothesis vocabulary at all, and the first version of this classifier duly filed it as a
# FALSE_FACT — i.e. it proposed deleting a record of a rejected design, which is exactly the
# content the three-way split exists to protect. Caught by
# test_refuted_hypothesis_is_never_a_false_fact.
APPROACH_REJECT = re.compile(
    r"(\bREJECTED:|\brejected\b|\babandon\w+|\bdropped\b|\binstead of\b|\bwe chose\b|"
    r"\bin favou?r of\b|\bmigrat\w+ (?:away|from)|\bswitch(?:ed)? away"
    + KP.extra("TRIAGE_APPROACH_REJECT") + r")", re.I | re.UNICODE)

# Vocabulary that only STEERS the split between a rejected approach and a refuted
# explanation. Deliberately not an attempt-marker: "the design uses X" is a plain fact.
APPROACH_TOPIC = re.compile(
    r"(\bDECIDED:|\bapproach\b|\barchitectur\w+|\bdesign\b|\balternativ\w+|"
    r"\bswitch(?:ed)? to\b" + KP.extra("TRIAGE_APPROACH_TOPIC") + r")", re.I | re.UNICODE)

# Explanation-shaped refutation (a belief about a cause that failed a test).
EXPLANATION = re.compile(
    r"\b(hypothes\w+|theor\w+|cause was not|not the cause|mechanis\w+|explanation|"
    r"disproven|disproved|falsified|ruled out|is not there|was not there|"
    r"no such|does not exist)\b", re.I)

# Epistemic hedge with no resolution yet.
HEDGE = re.compile(
    r"\b(likely|probabl\w+|may be|maybe|might|appears to|seems? to|presumably|"
    r"unverified|UNVERIFIED|unconfirmed|to be confirmed|needs? (?:testing|verif\w+)|"
    r"not yet (?:measured|verified|confirmed|tested)|open question|unknown whether|"
    r"suspect\w+" + KP.extra("TRIAGE_HEDGE") + r")\b", re.I | re.UNICODE)

RESOLVED = re.compile(
    r"\b(confirmed|verified|VERIFIED:|measured|reproduced|proven|proved|"
    r"disproven|disproved|refuted|falsified|settled|owner confirmed"
    + KP.extra("TRIAGE_RESOLVED") + r")\b", re.I | re.UNICODE)

# No trailing \b: "FALSIFIER:" ends on a colon, and a \b after a non-word character never
# matches. The first version had one and silently reported every falsifier as missing.
FALSIFIER = re.compile(r"\b(?:FALSIFIER:|would confirm|would refute|decisive test|"
                       r"discriminating experiment|test that would|"
                       r"confirms? or refutes?" + KP.extra("TRIAGE_FALSIFIER") + r")", re.I | re.UNICODE)

ATTEMPT_TYPES = {"decision→rationale", "action→outcome", "goal→method"}


@dataclass
class Triage:
    card_id: str
    bucket: str
    action: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    needs_falsifier: bool = False

    def __repr__(self):  # test-friendly
        return f"<Triage {self.card_id} {self.bucket} conf={self.confidence:.2f}>"


def classify(card: dict, refuted_by_evidence: bool = False) -> Triage:
    """Sort one card into the three-way scheme.

    `card` is a dict with at least: id, type, level, status, body.
    `refuted_by_evidence` — a contradiction verdict named THIS card as the wrong side.
      Without it a card can never be FALSE_FACT: you cannot tell that a fact is false by
      reading it, only by comparing it to something that beats it.
    """
    body = card.get("body", "") or ""
    ctype = (card.get("type") or "").strip()
    level = (card.get("level") or "").strip()
    reasons: list[str] = []

    attempt_hits = ATTEMPT.findall(body) + APPROACH_REJECT.findall(body)
    is_attempt = bool(attempt_hits) or level == "assumption" or ctype in ATTEMPT_TYPES
    if attempt_hits:
        reasons.append(f"attempt-markers: {sorted({str(h).lower() for h in attempt_hits})[:5]}")
    if level == "assumption":
        reasons.append("level: assumption")
    if ctype in ATTEMPT_TYPES:
        reasons.append(f"type: {ctype} (records an attempt or a choice)")

    hedged = bool(HEDGE.search(body))
    resolved = bool(RESOLVED.search(body))

    # ---- 3 UNCONFIRMED CONJECTURE ------------------------------------------
    # An open belief: hedged, not resolved, not beaten by evidence.
    if (hedged and not resolved and not refuted_by_evidence) or \
       (level == "assumption" and not resolved and not refuted_by_evidence):
        reasons.append("hedged and unresolved -> conjecture, keep and mark")
        return Triage(card.get("id", "?"), UNCONFIRMED_CONJECTURE,
                      ACTION[UNCONFIRMED_CONJECTURE], 0.7, reasons,
                      needs_falsifier=not FALSIFIER.search(body))

    # ---- 2 REFUTED HYPOTHESIS / REJECTED APPROACH --------------------------
    # THE RAIL: any attempt-marker at all forecloses FALSE_FACT, whatever else is true.
    if is_attempt:
        approach_hits = len(APPROACH_REJECT.findall(body)) + len(APPROACH_TOPIC.findall(body))
        expl_hits = len(EXPLANATION.findall(body))
        bucket = REJECTED_APPROACH if approach_hits > expl_hits else REFUTED_HYPOTHESIS
        reasons.append(f"attempt-framed (approach={approach_hits}, explanation={expl_hits}) "
                       f"-> {bucket}; KEPT in know/, its value is the refutation")
        conf = 0.85 if (refuted_by_evidence or resolved) else 0.6
        return Triage(card.get("id", "?"), bucket, ACTION[bucket], conf, reasons)

    # ---- 1 FALSE FACT -------------------------------------------------------
    if refuted_by_evidence and not is_attempt and not hedged:
        reasons.append("plain assertion, no attempt-marker, no hedge, beaten by evidence "
                       "-> FALSE_FACT: retire from the recall surface + tombstone")
        return Triage(card.get("id", "?"), FALSE_FACT, ACTION[FALSE_FACT], 0.8, reasons)

    if refuted_by_evidence:
        # Beaten by evidence but hedged: keep it, as a conjecture that lost.
        reasons.append("beaten by evidence but hedged -> keep as a marked conjecture, "
                       "not a removal")
        return Triage(card.get("id", "?"), UNCONFIRMED_CONJECTURE,
                      ACTION[UNCONFIRMED_CONJECTURE], 0.5, reasons,
                      needs_falsifier=not FALSIFIER.search(body))

    reasons.append("no refuting evidence -> no action")
    return Triage(card.get("id", "?"), STILL_TRUE, ACTION[STILL_TRUE], 0.9, reasons)


# ------------------------------------------------- claim-level triage
# WHY CLAIM GRANULARITY IS NOT OPTIONAL (a motor card, operator-verified 2026-08-31):
# that card is TRUE about the motor — identity, 100 N.m, 48 V, 600/800 W, gear ratio 5,
# the 8-pin aviation connector, the SVD48V50A pairing, several of which exist nowhere else —
# and FALSE about exactly one clause: it repeats the vendor page's default "13x5-6 tubeless
# fat tire ... (~340mm OD)", where the machine actually wears a 90/70-6 (free radius
# 139.2 mm; measured wheel-centre height 135 mm). That single wrong clause is where
# `rear_wheels_radius: 0.17` came from, overestimating hardware odometry by ~26 %.
# A whole-card verdict here has only two moves, and both are wrong: keep the card and keep
# the error, or delete the card and destroy a page of correct hardware facts to remove one
# sentence. So the unit of triage is the CLAIM, the tombstone attaches to the CLAIM, and a
# card is retired whole only when every claim in it is false.

_CZ = re.compile(r"^\s*CZ\s*:", re.I)
_SPEC_LIST = re.compile(r":\s")


@dataclass
class Claim:
    idx: int
    text: str
    kind: str = "claim"      # claim | cz (mirror translation, corrected with its source)
    span: tuple = (0, 0)


def split_claims(body: str) -> list[Claim]:
    """Cut a card body into independently-checkable assertions.

    Sentences first; then, inside a sentence that is a spec LIST ("Vendor-page specs: a, b,
    c, ..."), on the commas as well — the motor card above puts fourteen separate vendor claims in one
    sentence and only one of them is wrong.
    """
    claims: list[Claim] = []
    pos = 0
    for line in body.splitlines(keepends=True):
        start = pos
        pos += len(line)
        s = line.strip()
        if not s:
            continue
        if _CZ.match(s):
            claims.append(Claim(len(claims), s, "cz", (start, start + len(line))))
            continue
        off = start + (len(line) - len(line.lstrip()))
        for sent in re.split(r"(?<=[.!?])\s+", s):
            if len(sent.strip()) < 8:
                off += len(sent) + 1
                continue
            parts = [sent]
            if _SPEC_LIST.search(sent) and sent.count(",") >= 3:
                head, _, tail = sent.partition(": ")
                parts = [head + ":"] + [p for p in re.split(r",\s+", tail) if p.strip()]
            for p in parts:
                p = p.strip()
                if len(p) >= 8:
                    claims.append(Claim(len(claims), p, "claim", (off, off + len(p))))
            off += len(sent) + 1
    return claims


@dataclass
class CardTriage:
    card_id: str
    action: str                       # RETIRE_WHOLE | AMEND | MARK_DEAD_END | MARK_CONJECTURE | NONE
    claims: list = field(default_factory=list)          # (Claim, Triage)
    false_claims: list = field(default_factory=list)    # Claim objects judged FALSE_FACT
    surviving_claims: list = field(default_factory=list)
    reasons: list = field(default_factory=list)


def triage_card(card: dict, false_claim_idx=None, refuted_by_evidence: bool = False) -> CardTriage:
    """Claim-level triage.

    `false_claim_idx` — indices (from split_claims) that a contradiction verdict, an owner
    statement or a measurement has shown to be FALSE. Everything not named there is treated
    as still standing; the classifier never invents a false claim on its own.
    """
    body = card.get("body", "") or ""
    claims = split_claims(body)
    false_idx = set(false_claim_idx or ())
    # THE CARD'S FRAME IS INHERITED. "We hypothesised X" makes the bare claim "X" part of an
    # attempt, and a claim read out of its frame looks like a plain assertion — which is the
    # single easiest way to misfile a refuted hypothesis as a false fact.
    frame_attempt = bool(ATTEMPT.search(body) or APPROACH_REJECT.search(body)) \
        or (card.get("level") or "").strip() == "assumption" \
        or (card.get("type") or "").strip() in ATTEMPT_TYPES

    rows, false_claims, survivors, reasons = [], [], [], []
    for c in claims:
        sub = dict(card)
        sub["body"] = c.text if not frame_attempt else c.text + "\nhypothesis (card frame)"
        t = classify(sub, refuted_by_evidence=(c.idx in false_idx))
        rows.append((c, t))
        if t.bucket == FALSE_FACT and c.kind == "claim":
            false_claims.append(c)
        elif c.kind == "claim":
            survivors.append(c)

    real = [c for c in claims if c.kind == "claim"]
    if false_claims and len(false_claims) == len(real):
        action = "RETIRE_WHOLE"
        reasons.append("every claim in the card is false — nothing survives to preserve")
    elif false_claims:
        action = "AMEND"
        reasons.append(
            f"{len(false_claims)} of {len(real)} claims false; the other {len(survivors)} "
            f"stand. Correct each false claim IN PLACE with tools/khms_erratum.py "
            f"(declare -> apply): the retracted wording stays inside the card in an ERRATUM "
            f"block and gets one tombstone PER FALSE CLAIM (operator policy 2026-09-20: a "
            f"wrong record is fixed, a refuted belief is kept). The card is not removed and "
            f"its status does not change.")
    elif frame_attempt and refuted_by_evidence:
        action = "MARK_DEAD_END"
        reasons.append("attempt-framed card beaten by evidence — kept, typed as a dead end")
    else:
        card_level = classify(card, refuted_by_evidence=refuted_by_evidence)
        action = {"RETIRE": "RETIRE_WHOLE"}.get(card_level.action, card_level.action)
        reasons.extend(card_level.reasons)
    if any(c.kind == "cz" for c in claims) and false_claims:
        reasons.append("the CZ mirror line repeats the card's claims and must be corrected "
                       "with them, not left behind")
    return CardTriage(card.get("id", "?"), action, rows, false_claims, survivors, reasons)


# ---------------------------------------------------------------- tombstones

TS_LINE = re.compile(
    r"^(TS-\d{5})\s*\|\s*(\S+)\s*\|\s*was=(\S+)\s*\|\s*keys=([^|]*)\|\s*"
    r"claim=\"(.*?)\"\s*\|\s*wrong=\"(.*?)\"\s*\|\s*truth=(\S+)\s*\|\s*"
    r"recurrences=([^|]*)\|\s*body=(\S+)\s*$")


def _norm_claim(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def load_ledger(path: str = LEDGER) -> list[dict]:
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        m = TS_LINE.match(line.strip())
        if m:
            out.append({
                "ts": m.group(1), "date": m.group(2), "was": m.group(3),
                "keys": [k.strip() for k in m.group(4).split(",") if k.strip()],
                "claim": m.group(5), "wrong": m.group(6), "truth": m.group(7),
                "recurrences": [r.strip() for r in m.group(8).split(",") if r.strip()],
                "body": m.group(9),
            })
    return out


def render_tombstone(ts_id, date, was, keys, claim, wrong, truth, recurrences, body_path):
    """One line. Field-delimited, greppable, and NEVER a card.

    It does not live in memory/know/ or memory/archive/know/, the two directories
    khms_search.load_cards() globs, so it can never be served to the recall surface as an
    assertion. It is found by subject key and rendered by tombstone_lookup.py behind a fixed
    "this was asserted and was wrong" prefix.

    `recurrences` is the load-bearing field: a repeat of the same wrong claim appends a date
    HERE instead of creating a second tombstone, so a tombstone with two or more dates is the
    machine-readable statement that the previous countermeasure failed (writing a lesson
    down is not a measure).
    """
    return (f"{ts_id} | {date} | was={was} | keys={','.join(keys)} | "
            f"claim=\"{claim}\" | wrong=\"{wrong}\" | truth={truth} | "
            f"recurrences={','.join(recurrences)} | body={body_path}")


def _read_counter(path: str) -> int:
    """The next free tombstone number, or 1 when there is no counter yet."""
    try:
        return max(1, int(open(path, encoding="utf-8").read().strip()))
    except (OSError, ValueError):
        return 1


def next_ts_id(ledger: list[dict] | None = None, counter: str = TS_COUNTER) -> str:
    """Hand out ONE tombstone id and commit it to disk BEFORE returning it.

    One allocator, one persisted counter — the `tools/.next_id` idiom used for card ids
    (`new_card.sh`: write the incremented value first, then print the number, so an id that
    has been handed out is never handed out again, even if the caller dies mid-run; gaps are
    harmless because the id is opaque).

    THE LEDGER IS A FLOOR, NOT THE SOURCE OF THE NUMBER. It used to be the source
    (`max(ledger) + 1`), and because `propose_tombstone` never put the tombstone it had just
    allocated back into that ledger, every call within one run read the same maximum: the
    2026-09-05 triage published seven proposals, all `TS-00001`, all
    `body=bodies/TS-00001.md`. The floor is still read because the base is not in git — a
    ledger restored from a snapshot can be AHEAD of the counter, and reissuing a number that
    already stands in the ledger would merge two different errors into one record.
    (`build_board.py` treats `.board_next_id` against the board the same way.)
    """
    ledger = load_ledger() if ledger is None else ledger
    floor = max((int(t["ts"].split("-")[1]) for t in ledger), default=0) + 1
    # Through the base's one locked allocator (khms_alloc.py): two writers never get the same number.
    import khms_alloc
    n = khms_alloc.reserve("tombstone", 1, floor_extra=floor, path=counter)[0]
    return f"TS-{n:05d}"


def propose_tombstone(card, claim, wrong, truth, keys, today=None, ledger=None,
                      counter=TS_COUNTER):
    """Return (ledger_line, is_recurrence, ts_id). Writes no tombstone and no card.

    It does advance the id counter (and only that): the number is published into
    `memory/inbox/` by the triage report the moment it is proposed, so a later run must not
    hand the same number to a different error. A recurrence allocates nothing.

    IT ALSO APPENDS THE NEW TOMBSTONE TO `ledger`, in place. The caller loads the ledger once
    and passes it to every claim it triages (`khms_triage_report.py`), so the run's own
    proposals have to be visible to the calls that follow — otherwise the second claim of a
    run is numbered as if the first had never happened (7 x TS-00001 on 2026-09-05), and a
    repeat of the SAME claim inside one run becomes a second tombstone instead of a
    recurrence, which is the one signal the ledger exists to carry. Keeping that invariant
    here rather than in the caller is the fix: on 2026-09-05 it lived nowhere.
    """
    today = today or datetime.date.today().isoformat()
    ledger = load_ledger() if ledger is None else ledger
    nc = _norm_claim(claim)
    for t in ledger:
        if _norm_claim(t["claim"]) == nc or (set(t["keys"]) & set(keys) and
                                             _norm_claim(t["claim"])[:60] == nc[:60]):
            recs = t["recurrences"] + ([today] if today not in t["recurrences"] else [])
            return (render_tombstone(t["ts"], t["date"], t["was"], sorted(set(t["keys"]) | set(keys)),
                                     t["claim"], t["wrong"], truth or t["truth"], recs, t["body"]),
                    True, t["ts"])
    ts = next_ts_id(ledger, counter=counter)
    rec = {"ts": ts, "date": today, "was": card.get("id", "?"), "keys": list(keys),
           "claim": claim, "wrong": wrong, "truth": truth or "UNKNOWN",
           "recurrences": [today], "body": f"bodies/{ts}.md"}
    ledger.append(rec)                # visible to the rest of THIS run
    return (render_tombstone(rec["ts"], rec["date"], rec["was"], rec["keys"], rec["claim"],
                             rec["wrong"], rec["truth"], rec["recurrences"], rec["body"]),
            False, ts)


if __name__ == "__main__":
    import json
    for path in sys.argv[1:]:
        text = open(path, encoding="utf-8").read()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        front, body = (m.group(1), m.group(2)) if m else ("", text)

        def fm(k, d=""):
            mm = re.search(rf"^{k}:\s*(.+)$", front, re.M)
            return mm.group(1).strip() if mm else d
        card = {"id": fm("id", path), "type": fm("type"), "level": fm("level"),
                "status": fm("status"), "body": body}
        t = classify(card, refuted_by_evidence="--refuted" in sys.argv)
        print(json.dumps(t.__dict__, ensure_ascii=False))
