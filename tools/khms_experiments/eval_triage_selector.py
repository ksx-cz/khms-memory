#!/usr/bin/env python3
"""Offline evaluation of the triage gate against every reviewed night (reviewed history).

Usage: python3 tools/eval_triage_selector.py [--rules a,b] [--per-rule] [--show-lost]

Reads every `memory/inbox/*-triage.md` that carries a `## REVIEW VERDICT`, maps each
`ROW n:` verdict to its card pair (the n-th `### K-x \u2014 action \u2026` heading, the same numbering
apply_triage.py uses), and asks tools/khms_triage_gate.py whether the gate would have refused
the row. Nights are ordered by file name; the oldest 20 tune, the newest 8 are held out.

THE CARDS ARE READ AS THEY STOOD THAT NIGHT. A morning review writes statuses and edges
(`refuted_by`, `contradicts`, `status: refuted` \u2026) into the very cards its rows name, so
judging an old row against today's base would let the review's own output decide the review.
Each pair requires retained Nextcloud versions predating the review. Missing history
is UNVERIFIABLE; today's cards are never a substitute.

Reads only. Writes nothing (stdout only). No model is called unless --rejudge-cache names a
file of answers recorded earlier (`what-if` lines only; the verdict line is the gate's).

Exit 0 when the acceptance holds (held-out: >= 60 % of REJECT rows refused and >= 95 % of
APPLY rows kept; whole set: >= 95 % of APPLY rows kept), 1 when it does not, 2 on no data.
"""
from __future__ import annotations

# Public helpers live beside the experiment package.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apply_triage as A  # noqa: E402  (ROW numbering + verdict parsing, one definition)
import khms_triage_gate as G  # noqa: E402

import khms_paths as P
from khms_versions import Versions, VersionError
BRAIN = P.ROOT
N_HELD = 8
REJECT_MIN, APPLY_MIN = 60.0, 95.0


def load_rows(brain, client=None):
    client = client or Versions.configured()
    rows, notes = [], []
    for f in sorted(glob.glob(os.path.join(brain, "memory", "inbox", "*-triage.md"))):
        text = open(f, encoding="utf-8").read()
        if "## REVIEW VERDICT" not in text:
            continue
        head, ver = text.split("## REVIEW VERDICT", 1)
        name = os.path.basename(f)
        import datetime as dt
        # The file's date alone is insufficient to prove a pre-review time. The
        # original applied report supplies the explicit cutoff (no local copies).
        applied = Path(f).with_suffix('.applied.md')
        if not applied.exists():
            raise VersionError(f"{name}: UNVERIFIABLE: no applied report time")
        match = re.search(r"20\d\d-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[+-]\d\d:\d\d|Z)", applied.read_text())
        if not match:
            raise VersionError(f"{name}: UNVERIFIABLE: report needs timezone-qualified time")
        cutoff = dt.datetime.fromisoformat(match.group(0)).timestamp()
        parsed = A.parse_rows(head)
        for n, (verdict, _cls, why) in sorted(A.parse_verdicts(ver).items()):
            if n > len(parsed):
                notes.append(f"{name}: ROW {n} has no matching proposal heading — skipped")
                continue
            r = parsed[n - 1]
            cards = {}
            for cid in (r["loser"], r["winner"]):
                errors = []
                for sub in ('know', 'archive/know'):
                    try:
                        content, proof = client.before(f'memory/{sub}/{cid}.md', cutoff)
                        c = G.parse_card_text(cid, content.decode('utf-8'))
                        c['history_proof'] = proof
                        break
                    except VersionError as exc:
                        errors.append(str(exc))
                else:
                    raise VersionError(f"{name} {cid}: UNVERIFIABLE: " + '; '.join(errors))
                cards[cid] = c
            rows.append({"file": name, "n": n, "verdict": verdict, "why": why,
                         "loser": cards[r["loser"]], "winner": cards[r["winner"]],
                         "loser_id": r["loser"], "winner_id": r["winner"],
                         "quote": r["quote"], "win_quote": r["winner_quote"]})
    return rows, notes


def gate_reasons(row, rules):
    if not row["loser"] or not row["winner"]:
        return []                       # a card that cannot be read is never refused
    return G.row_gate(row["loser"], row["winner"], row["quote"], row["win_quote"], rules=rules)


def tally(rows, refused):
    a = [r for r in rows if r["verdict"] == "APPLY"]
    j = [r for r in rows if r["verdict"] == "REJECT"]
    a_lost = sum(1 for r in a if refused(r))
    j_cut = sum(1 for r in j if refused(r))
    return {"apply": len(a), "apply_lost": a_lost, "reject": len(j), "reject_cut": j_cut,
            "apply_kept_pct": 100.0 * (len(a) - a_lost) / len(a) if a else float("nan"),
            "reject_cut_pct": 100.0 * j_cut / len(j) if j else float("nan")}


def fmt(t):
    return (f"REJECT refused {t['reject_cut']:3d}/{t['reject']:3d} ({t['reject_cut_pct']:5.1f} %)"
            f" · APPLY kept {t['apply'] - t['apply_lost']:2d}/{t['apply']:2d} "
            f"({t['apply_kept_pct']:5.1f} %)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--brain", default=BRAIN)
    ap.add_argument("--rules", help="comma-separated rule names (default: the enabled set)")
    ap.add_argument("--per-rule", action="store_true", help="also measure every rule alone")
    ap.add_argument("--show-lost", action="store_true", help="list every APPLY row refused")
    ap.add_argument("--rejudge-cache",
                    help="JSONL of recorded second-model answers (what-if lines only)")
    args = ap.parse_args()

    rules = [x for x in args.rules.split(",") if x] if args.rules is not None else None
    in_force = G.enabled_rules(rules)
    try:
        rows, notes = load_rows(args.brain)
    except (VersionError, P.KhmsPathError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if not rows:
        print("no reviewed triage rows found", file=sys.stderr)
        return 2
    files = sorted({r["file"] for r in rows})
    held = set(files[-N_HELD:])
    tune_rows = [r for r in rows if r["file"] not in held]
    held_rows = [r for r in rows if r["file"] in held]
    for r in rows:
        r["gate"] = gate_reasons(r, in_force)

    print(f"nights: {len(files)} reviewed files · rows {len(rows)} "
          f"(APPLY {sum(r['verdict'] == 'APPLY' for r in rows)}, "
          f"REJECT {sum(r['verdict'] == 'REJECT' for r in rows)})")
    print(f"tune    : {files[0][:10]} … {files[-N_HELD - 1][:10]} ({len(files) - N_HELD} files)")
    print(f"held-out: {files[-N_HELD][:10]} … {files[-1][:10]} ({N_HELD} files)")
    for n in notes:
        print(f"  note: {n}")
    print(f"rules in force: {', '.join(in_force) or '(none)'}")
    if args.per_rule:
        print("\nper rule (each alone)                 tune                                    "
              "            | held-out")
        for name in G.RULES:
            ref = (lambda nm: (lambda r: bool(r["loser"] and r["winner"] and
                   G.RULES[nm][0](r["loser"], r["winner"], r["quote"], r["win_quote"]))))(name)
            on = "ON " if name in in_force else "off"
            print(f"  [{on}] {name:26s} {fmt(tally(tune_rows, ref))} | "
                  f"{fmt(tally(held_rows, ref))}")
    refused = lambda r: bool(r["gate"])  # noqa: E731
    tt, th, tf = tally(tune_rows, refused), tally(held_rows, refused), tally(rows, refused)
    print("\nthe gate in force:")
    print(f"  tune     {fmt(tt)}")
    print(f"  held-out {fmt(th)}")
    print(f"  full     {fmt(tf)}")
    if args.show_lost:
        for r in rows:
            if r["verdict"] == "APPLY" and r["gate"]:
                print(f"  lost APPLY {r['file'][:10]} ROW {r['n']} {r['loser_id']} ⇄ "
                      f"{r['winner_id']}: {r['gate'][0][0]} — {r['why'][:120]}")

    if args.rejudge_cache and os.path.exists(args.rejudge_cache):
        ans = {}
        for line in open(args.rejudge_cache, encoding="utf-8"):
            o = json.loads(line)
            ans[(o["file"], o["n"])] = (o.get("j") or {}).get("contradiction")
        cov = sum(1 for r in rows if (r["file"], r["n"]) in ans)
        no = lambda r: ans.get((r["file"], r["n"])) == "no"  # noqa: E731
        print(f"\nwhat-if (NOT the gate): a second, order-swapped model judgement refuses on "
              f"'no' — answers for {cov}/{len(rows)} rows, a missing answer keeps the row")
        print(f"  tune     {fmt(tally(tune_rows, no))}")
        print(f"  held-out {fmt(tally(held_rows, no))}")
        print(f"  full     {fmt(tally(rows, no))}")
        both = lambda r: no(r) or bool(r["gate"])  # noqa: E731
        print(f"  + gate, held-out {fmt(tally(held_rows, both))}")
        print(f"  + gate, full     {fmt(tally(rows, both))}")

    ok = (th["reject_cut_pct"] >= REJECT_MIN and th["apply_kept_pct"] >= APPLY_MIN
          and tf["apply_kept_pct"] >= APPLY_MIN)
    print(f"\nTRIAGEFIX-VERDICT: heldout_reject_filtered={th['reject_cut_pct']:.1f}% "
          f"heldout_apply_kept={th['apply_kept_pct']:.1f}% "
          f"full_apply_kept={tf['apply_kept_pct']:.1f}% llm_usd=0.00 "
          f"-> {'PASS' if ok else 'FAIL'} (needs held-out reject >= {REJECT_MIN:.0f} %, "
          f"apply >= {APPLY_MIN:.0f} %, full apply >= {APPLY_MIN:.0f} %)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
