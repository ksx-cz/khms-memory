#!/usr/bin/env python3
"""Deterministic triage gate — rules that refuse a proposed contradiction row from the two
cards' own metadata and quotes, at zero tokens, before the row reaches the morning file.

Usage: library — imported by khms_contradict.py and the triage evaluators; not run directly.

WHY (issue #208, 2026-10-01): over the 28 reviewed nights 2026-09-04 … 2026-10-01 the morning
review APPLIED 70 rows and REJECTED 602. The reviewers' reasons repeat: two readings of a
changing quantity taken at different moments; a winner that cannot make anything false (a
decision, a policy); a winner whose own status is in doubt; a "winner" written long before the
loser it is said to beat; two cards that share no tag. Cards K-92816, K-92932, K-93087 and
K-93118 each asked for one of these filters as a recommendation; none was in the code.

WHAT THE HISTORICAL MEASUREMENT SAYS (the deployment evaluator must prove
pre-review card state from retained server versions; unavailable history is
UNVERIFIABLE, never reconstructed from the current cards): every rule here costs some human-APPLIED
rows, and each rule's cost is printed per rule by the evaluator. A rule is ENABLED below only
if the union of the enabled rules keeps >= 95 % of the APPLY rows over the whole labelled set.
The other rules are kept, measured and OFF — switching one on is a one-line decision with its
price already on the table.

A gated row is never silent: khms_triage_report.py lists it under "Withheld" with the rule
and its evidence. Nothing in memory/know/ is touched.

Two entry points:
  row_gate(loser, winner, quote, win_quote)   role-aware, after the model named the loser
  pair_gate(a, b)                             before the model: a pair is refused only when
                                              BOTH orientations would be refused by a rule
                                              that needs no quote — the model then cannot
                                              produce a row that survives the gate anyway.
"""
from __future__ import annotations

import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Card ids in comments (K-9xxxx) are illustrative stand-ins for the reference deployment's cards
# the measurements below were made on; they are not ids of any shipped base.
import khms_contradict as kc  # noqa: E402  (unit families, value disjointness)

# A card of one of these types states what we DECIDED or REQUIRE, not what IS. It can make
# a loser obsolete, never false (K-92816). Measured cost: see the evaluator.
DECISION_TYPES = {"decision→rationale", "policy", "principle", "requirement",
                  "goal→method", "overview"}
# Unit families whose values are READINGS of something that changes over time — disk use,
# a topic rate, a frame rate, a duration, a size on disk. Two such readings taken at
# different moments are two measurements, not a contradiction. Length, angle, voltage,
# current, resolution and pixel geometry are properties of a fixed machine and stay out.
TIME_VARYING = {"pct", "freq", "rate", "time", "bytes"}
WINNER_OLDER_DAYS = 7
# Words that make a quoted value a REFUTATION of an earlier one rather than a later reading:
# with one of these in either quote the pair is a belief dispute, and it goes to review.
_REFUTES = re.compile(
    r"\b(not|never|false|wrong|incorrect|mistaken|misread\w*|turned out|actually|instead|"
    r"corrected|correction|disprov\w+|refut\w+|in fact|bogus|bug)\b", re.I)
_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
# A value written as a BOUND ("over 1000 ms", "> 3 s", "timeout 5 s", "24 Hz floor") is a
# configured parameter, not a reading. When BOTH quotes give only bounds, the disagreement is
# about a setting — that is a real dispute and stays with the reviewer (2026-09-27 ROW 41:
# "no valid frame for over 1000ms" vs "stale (> 3 s)", APPLIED).
_BOUND_BEFORE = re.compile(
    r"(?:[<>≤≥]=?|\b(?:over|above|below|under|at least|at most|up to|max(?:imum)?|"
    r"min(?:imum)?|timeout|threshold|deadline|limit|within|after|every)\b)\W{0,3}$", re.I)
_BOUND_AFTER = re.compile(r"^\W{0,3}(?:floor|ceiling|cap|limit|timeout|threshold|deadline)\b",
                          re.I)


def _all_bounds(text, fam):
    """True when every value of unit family `fam` in `text` is written as a bound."""
    seen = False
    for f, pat, _mul in kc._QPAT:
        if f != fam:
            continue
        for m in pat.finditer(text):
            seen = True
            before = text[max(0, m.start() - 16):m.start()]
            after = text[m.end():m.end() + 12]
            if not (_BOUND_BEFORE.search(before) or _BOUND_AFTER.search(after)):
                return False
    return seen


def _date(s):
    try:
        return datetime.date.fromisoformat(str(s or "").strip().strip("'\"")[:10])
    except ValueError:
        return None


def _reading_date(quote):
    """The date a quoted reading belongs to — ONLY a date written inside the quote itself, and
    only when the quote names exactly one date. None means "unknown", and an unknown date never
    licenses a refusal.

    The card's own `date:` is NOT a fallback (fixed 2026-10-01 after Diblin's review of #208,
    P1 #1): it is when the card was WRITTEN, not when the reading was taken. Two cards written
    on 28 and 29 September can both quote run R17, and the fallback read them as two moments.
    A quote with two or more dates is ambiguous (which one is the reading's?) -> None."""
    found = {d for d in (_date(m) for m in _DATE.findall(quote or "")) if d}
    return found.pop() if len(found) == 1 else None


# An explicit run / session / recording identifier. Both quotes naming the SAME one is the
# opposite of "different moments": it is one run reported two ways, and must reach review.
_RUN_ID = re.compile(
    r"(?i:\b(?:run|session|bag|recording|drive|campaign|scenario|test|board|trial))\s+"
    r"[`'\"]?([A-Za-z0-9][\w.:/-]*\d[\w.:/-]*)|\b(R\d{1,4})\b")


def _run_ids(text):
    out = set()
    for m in _RUN_ID.finditer(text or ""):
        tok = (m.group(1) or m.group(2) or "").strip(".:/-").lower()
        if tok and not _DATE.fullmatch(tok):
            out.add(tok)
    return out


# --------------------------------------------------------------------------- the rules
# Each rule: (loser, winner, quote, win_quote) -> evidence string when it fires, else None.

def r_decision_winner(loser, winner, quote="", win_quote=""):
    if winner.get("type") in DECISION_TYPES:
        return f"winner {winner['id']} is a `{winner.get('type')}` — a decision can make a " \
               f"card obsolete, it cannot make it false"
    return None


def r_winner_not_active(loser, winner, quote="", win_quote=""):
    st = winner.get("status") or "active"
    if st != "active":
        return f"winner {winner['id']} is itself `{st}` — it cannot refute anything"
    return None


def r_winner_older(loser, winner, quote="", win_quote=""):
    dl, dw = _date(loser.get("date")), _date(winner.get("date"))
    if dl and dw and (dl - dw).days > WINNER_OLDER_DAYS:
        return f"winner {winner['id']} ({dw}) is {(dl - dw).days} days OLDER than the loser " \
               f"{loser['id']} ({dl}) — the loser was written with the winner already in the base"
    return None


def r_newer_loser_cites_winner(loser, winner, quote="", win_quote=""):
    dl, dw = _date(loser.get("date")), _date(winner.get("date"))
    if dl and dw and dl > dw and winner["id"] in (loser.get("body") or ""):
        return f"the loser {loser['id']} is newer and names {winner['id']} in its own text — " \
               f"it already accounts for it"
    return None


def r_tags_disjoint(loser, winner, quote="", win_quote=""):
    tl, tw = set(loser.get("tags") or ()), set(winner.get("tags") or ())
    if tl and tw and not (tl & tw):
        return f"the two cards share no tag ({sorted(tl)} vs {sorted(tw)})"
    return None


def r_different_moments(loser, winner, quote="", win_quote=""):
    """Two readings of the SAME time-varying quantity taken on DIFFERENT dates.

    Fires only when (1) both quotes carry values in a common unit family, (2) every family
    in which the values disagree is a time-varying one, (3) EACH QUOTE names exactly one date
    of its own and the two differ (the cards' `date:` fields are never used — they are when
    the cards were written), (4) the quotes do not name the same run / session / recording,
    and (5) neither quote speaks of a refutation. Anything less is unclear, and unclear goes
    to review. Same quantity, same date or same run, different value is a real conflict and
    is NOT touched here."""
    if not quote or not win_quote:
        return None
    if _REFUTES.search(quote) or _REFUTES.search(win_quote):
        return None
    if _run_ids(quote) & _run_ids(win_quote):
        return None                     # one run reported two ways: a real conflict
    qa, qb = kc._quant_from_text(quote), kc._quant_from_text(win_quote)
    shared = set(qa) & set(qb)
    disagree = {f for f in shared if kc._disjoint(qa[f], qb[f])}
    if not disagree or not disagree <= TIME_VARYING:
        return None
    da, db = _reading_date(quote), _reading_date(win_quote)
    if not da or not db or da == db:
        return None
    if all(_all_bounds(quote, f) and _all_bounds(win_quote, f) for f in disagree):
        return None                     # two settings disagree: a real dispute, keep it
    fam = sorted(disagree)[0]
    return f"two readings of a changing quantity at different moments: {fam} " \
           f"{sorted(qa[fam])} on {da} vs {sorted(qb[fam])} on {db}"


# name -> (rule, needs quotes, enabled). The ENABLED column is the measured decision — see
# the module docstring and `tools/eval_triage_selector.py --per-rule`.
RULES = {
    "decision-winner":         (r_decision_winner, False, True),
    "winner-not-active":       (r_winner_not_active, False, True),
    "winner-older":            (r_winner_older, False, False),
    "newer-loser-cites-winner": (r_newer_loser_cites_winner, False, False),
    "tags-disjoint":           (r_tags_disjoint, False, False),
    "different-moments":       (r_different_moments, True, True),
}


def enabled_rules(override=None):
    """Names of the rules in force. `override` (iterable of names) replaces the defaults;
    the environment variable KHMS_TRIAGE_GATE=name,name does the same for a nightly run."""
    if override is None:
        env = os.environ.get("KHMS_TRIAGE_GATE")
        if env is not None:
            override = [x.strip() for x in env.split(",") if x.strip()]
    if override is not None:
        unknown = set(override) - set(RULES)
        if unknown:
            raise ValueError(f"unknown triage-gate rule(s): {sorted(unknown)}")
        return list(override)
    return [n for n, (_, _, on) in RULES.items() if on]


def row_gate(loser, winner, quote="", win_quote="", rules=None):
    """[(rule, evidence)] for every enabled rule that refuses this row; [] keeps it."""
    out = []
    for name in enabled_rules(rules):
        fn = RULES[name][0]
        ev = fn(loser, winner, quote, win_quote)
        if ev:
            out.append((name, ev))
    return out


def pair_gate(a, b, rules=None):
    """Before the model: [(rule, evidence)] when EVERY orientation of the pair is refused by
    a quote-free enabled rule, else []. A pair refused here would produce a row the
    post-model gate refuses whichever card the model calls wrong, so it is not sent."""
    names = [n for n in enabled_rules(rules) if not RULES[n][1]]
    ab = row_gate(a, b, rules=names)
    if not ab:
        return []
    ba = row_gate(b, a, rules=names)
    if not ba:
        return []
    return [(n, f"{a['id']} as loser: {e}") for n, e in ab] + \
           [(n, f"{b['id']} as loser: {e}") for n, e in ba]


# ------------------------------------------------------------------------- card reading

def parse_card_text(cid, text):
    """The fields the rules read, from one card file's text."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    front, body = (m.group(1), m.group(2)) if m else ("", text)

    def fm(k, d=""):
        mm = re.search(rf"^{k}:\s*(.+)$", front, re.M)
        return mm.group(1).strip().strip("'\"") if mm else d
    tags = [t.strip() for t in fm("tags", "").strip("[] ").split(",") if t.strip()]
    return {"id": cid, "type": fm("type"), "level": fm("level"), "status": fm("status"),
            "evidence": fm("evidence"), "date": fm("date"), "tags": tags,
            "body": body.strip()}


def load_card_from(mem_dir, cid):
    for sub in ("know", "archive/know"):
        p = os.path.join(mem_dir, sub, f"{cid}.md")
        if os.path.exists(p):
            return parse_card_text(cid, open(p, encoding="utf-8", errors="replace").read())
    return None
