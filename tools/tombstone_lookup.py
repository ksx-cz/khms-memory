#!/usr/bin/env python3
"""Read the tombstone ledger. The ONLY retrieval channel for retired false claims.

A tombstone is deliberately not a card and does not live under memory/know/ or
memory/archive/know/, the two directories khms_search.load_cards() globs — so it can never
be served to the recall surface as an assertion. It is reachable only through here, and here
never prints a bare statement: every hit is rendered behind a fixed

    ⛔ ERROR RECORD — this was asserted, and it was wrong

prefix, so the claim can be recognised but never mistaken for a fact.

WHY IT EXISTS AT ALL. On 2026-08-31 the most useful thing the base produced was not a fact:
it was the record that the same mistake had been made before. A repeated mistake is evidence
that the previous countermeasure failed (K-90500: a measure is a step that is visible in the
record; K-90801: writing the lesson down is not the measure). So a repeat does not create a
second tombstone — it appends a date to the existing one, and `--repeats` ranks the ledger by
how many times each error has come back. That ranking is the base's own list of countermeasures
that are not working.

Usage:
  tombstone_lookup.py sensor-code symptom     # by subject key or free text
  tombstone_lookup.py --repeats                  # errors that came back, worst first
  tombstone_lookup.py --all
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_triage as T  # noqa: E402

PREFIX = "⛔ ERROR RECORD — this was asserted, and it was wrong"
# khms_erratum.py --kind dated-pointer (#219): a VALUE true when written that has since moved
# in the repo; its `wrong=` starts with this tag. Calling it an error would be a false record.
DATED_TAG = "DATED POINTER;"
DATED_PREFIX = "⌛ DATED VALUE — true when written; the value has since moved in the repo"


def render(t, verbose=True):
    n = len(t["recurrences"])
    dated = t["wrong"].startswith(DATED_TAG)
    prefix = DATED_PREFIX if dated else PREFIX
    head = f"{prefix} ({n}× — {', '.join(t['recurrences'])})" if n > 1 else \
           f"{prefix} ({t['date']})"
    out = [f"{head}  [{t['ts']}, was {t['was']}]",
           f"    claimed : {t['claim']}",
           f"    {'moved' if dated else 'wrong'}   : {t['wrong']}",
           f"    truth   : {t['truth']}"]
    if n > 1:
        out.append(f"    NOTE    : this error has recurred {n - 1}× after being recorded — "
                   f"the previous countermeasure did not hold.")
    if verbose:
        out.append(f"    keys    : {', '.join(t['keys'])}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("terms", nargs="*")
    ap.add_argument("--repeats", action="store_true",
                    help="only errors that came back, most recurrences first")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ledger", default=T.LEDGER)
    args = ap.parse_args()

    rows = T.load_ledger(args.ledger)
    if not rows:
        print(f"(no tombstones yet — ledger {args.ledger} is empty or absent)")
        return
    if args.repeats:
        rows = [r for r in rows if len(r["recurrences"]) > 1]
        rows.sort(key=lambda r: -len(r["recurrences"]))
    elif not args.all:
        if not args.terms:
            ap.error("give search terms, --repeats or --all")
        pats = [re.compile(re.escape(t), re.I) for t in args.terms]
        rows = [r for r in rows
                if any(p.search(" ".join(r["keys"]) + " " + r["claim"] + " " + r["wrong"])
                       for p in pats)]
    if not rows:
        print("(no matching tombstone)")
        return
    for r in rows:
        print(render(r))
        print()


if __name__ == "__main__":
    main()
