#!/usr/bin/env python3
"""night_yield.py — what one night's proposals cost and how many of them the morning accepted (0 tokens).

Usage: night_yield.py <night key> [--log LOG] [--root ROOT] [--approvals DIR] [--json]

The metric of #237 (owner 2026-10-13): there is no success threshold; the night is run to MINIMISE the cost of
one finally accepted card.  Per stage and in total this prints
    calls · $ · proposals · accepted · accepted/proposed · $ per proposal · $ per accepted card
and how many proposals still wait for their review (pending).

WHERE THE NUMBERS COME FROM (all written by the pipeline itself, nothing estimated):
  $, calls   the `served-by:` lines of the night's segment of the night log (from `nightly start (night key K)`
             to the next `nightly start`): `(…, $X, N turns)` is the CLI's own total_cost_usd.  A call without a
             `$` (no JSON result) is counted as a call with $0 and said in `calls without $`.
  proposals  every candidate card of the inbox units the night published (memory/ledger/published.jsonl records
             whose time lies in the segment), up to the first trailer section (`## DROPPED …` etc. are not
             proposals the review saw as cards).
  accepted   for a unit named in a COMPLETE approval receipt (<shared state>/approvals/*.json), the cards of its
             `<unit>.approve.md` — the file the reviewer edits down to what is approved (`^id: ` blocks).  A unit
             whose review is closed with no receipt (mark-reviewed: nothing approved) counts 0 accepted; a unit
             with neither is PENDING: its proposals count for $ per proposal, not for accepted/proposed (that
             ratio is over the reviewed proposals), and $ per accepted card is marked not final (`*`).
STAGES: `mark` = the journal MARKs (marks-N chunks, permark-M-… calls, consolidate-<night>-mNN-… calls); `sweep`
= what came from the transcripts and the git log (sweep-N parts, generator parts, permark-G-… calls for generated
lines, consolidate-<night>-NN calls).  A candidate is attributed by its `MARKS:` ids when it has any (an `M-` id =
mark, only `G-` ids = sweep), else by its unit (`-mNN-bKK` = mark).
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

START_RE = re.compile(r"^(\S+) nightly start \(night key (\S+)\)")
SERVED_RE = re.compile(r"^(\S+) (\S+) served-by: \S+ \((.*)\)\s*$")
COST_RE = re.compile(r"\$(\d+(?:\.\d+)?)")
HEAD_RE = re.compile(r"^#{2,3} +[A-Z]\d+[a-z]?\b", re.M)
TRAILER_RE = re.compile(r"^## (?!#)(?!cards\b)", re.M | re.I)
MARKS_RE = re.compile(r"^[ \t]*\*{0,2}MARKS:\*{0,2}[ \t]*(.*)$", re.M | re.I)   # `marks:` in an approved card
FRONT_ID_RE = re.compile(r"^id:\s*\S+", re.M)
ID_RE = re.compile(r"\b([MG])-[0-9a-f]{8}\b")
STAGES = ("mark", "sweep")


def stage_of_call(name):
    if name.startswith("permark-"):
        return "sweep" if name.startswith("permark-G-") else "mark"
    if name.startswith("marks"):
        return "mark"
    if name.startswith("consolidate-"):
        return "mark" if re.search(r"-m\d+-\d+$", name) else "sweep"
    if name.startswith(("sweep", "generate")):
        return "sweep"
    return None


def segment(log_lines, key):
    """-> (start iso, end iso or None, lines) of the LAST run with this night key."""
    starts = [(i, m.group(1)) for i, ln in enumerate(log_lines) for m in [START_RE.match(ln)] if m]
    mine = [(n, i, t) for n, (i, t) in enumerate(starts) if START_RE.match(log_lines[i]).group(2) == key]
    if not mine:
        return None, None, []
    n, i, t = mine[-1]
    j, end = (starts[n + 1][0], starts[n + 1][1]) if n + 1 < len(starts) else (len(log_lines), None)
    return t, end, log_lines[i:j]


def costs(lines):
    out = {s: {"calls": 0, "usd": 0.0, "nocost": 0} for s in STAGES}
    other = 0
    for ln in lines:
        m = SERVED_RE.match(ln)
        if not m:
            continue
        st = stage_of_call(m.group(2))
        if st is None:
            other += 1
            continue
        out[st]["calls"] += 1
        c = COST_RE.search(m.group(3))
        if c:
            out[st]["usd"] += float(c.group(1))
        else:
            out[st]["nocost"] += 1
    return out, other


def _when(iso):
    try:
        return dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def night_units(root, start, end):
    units = []
    try:
        fh = open(os.path.join(root, "memory", "ledger", "published.jsonl"), encoding="utf-8")
    except OSError:
        return units
    t0, t1 = _when(start), _when(end) if end else None
    with fh:
        for ln in fh:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            t = _when(r.get("ts"))
            if t and t0 and t >= t0 and (t1 is None or t < t1) and r.get("unit") not in units:
                units.append(r.get("unit"))
    return units


def cards_section(text):
    m = TRAILER_RE.search(text)
    return text[:m.start()] if m else text


def classify(block, unit):
    m = MARKS_RE.search(block)
    kinds = set(ID_RE.findall(m.group(1))) if m else set()
    if "M" in kinds:
        return "mark"
    if kinds == {"G"}:
        return "sweep"
    return "mark" if re.search(r"-m\d+-b\d+\.md$", unit) else "sweep"


def blocks(text, head_re):
    hs = list(head_re.finditer(text))
    return [text[h.start():(hs[i + 1].start() if i + 1 < len(hs) else len(text))] for i, h in enumerate(hs)]


def receipts(approvals):
    """-> [set of unit realpaths] of every COMPLETE approval receipt."""
    out = []
    for f in glob.glob(os.path.join(approvals, "*.json")):
        try:
            r = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("complete"):
            out.append({os.path.realpath(u) for u in r.get("units", [])})
    return out


def review_closed(markers, stem):
    return os.path.exists(os.path.join(markers, f".reviewed-{stem}-cards"))


def report(key, log, root, approvals, markers):
    try:
        lines = open(log, encoding="utf-8", errors="replace").read().splitlines()
    except OSError as exc:
        raise SystemExit(f"night_yield: cannot read the night log {log}: {exc}")
    start, end, seg = segment(lines, key)
    if not seg:
        raise SystemExit(f"night_yield: no `nightly start (night key {key})` line in {log}")
    cost, other = costs(seg)
    res = {s: dict(cost[s], proposals=0, reviewed=0, accepted=0, pending=0) for s in STAGES}
    recs = receipts(approvals)
    approved = set().union(*recs) if recs else set()
    units = night_units(root, start, end)
    mine = {os.path.realpath(os.path.join(root, "memory", "inbox", u)) for u in units}
    # the same morning's receipts may approve units the night did not publish (a hand-made triage unit, an
    # agent's own unit): their cards are said, not counted
    outside = 0
    for rec in recs:
        if rec & mine:
            for u in rec - mine:
                ap = re.sub(r"\.md$", ".approve.md", u)
                try:
                    outside += len(re.findall(r"(?m)^---\s*\nid:", open(ap, encoding="utf-8").read()))
                except OSError:
                    pass
    unit_rows = []
    for u in units:
        path = os.path.join(root, "memory", "inbox", u)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        props = [classify(b, u) for b in blocks(cards_section(text), HEAD_RE)]
        if not props:
            continue
        stem = u[:-3]
        if os.path.realpath(path) in approved:
            try:
                ap = open(os.path.join(root, "memory", "inbox", stem + ".approve.md"), encoding="utf-8").read()
                acc = [classify(b, u) for b in blocks(ap, re.compile(r"^---\s*\nid:", re.M))]
            except OSError:
                acc = []
            state = "approved"
        elif review_closed(markers, stem):
            acc, state = [], "closed"
        else:
            acc, state = None, "pending"
        for s in props:
            res[s]["proposals"] += 1
            res[s]["pending" if acc is None else "reviewed"] += 1
        for s in acc or []:
            res[s]["accepted"] += 1
        unit_rows.append({"unit": u, "proposals": len(props), "accepted": None if acc is None else len(acc),
                          "state": state})
    tot = {k: sum(res[s][k] for s in STAGES)
           for k in ("calls", "usd", "nocost", "proposals", "reviewed", "accepted", "pending")}
    res["total"] = tot
    for s in res.values():
        s["ratio"] = s["accepted"] / s["reviewed"] if s["reviewed"] else None      # over REVIEWED proposals
        s["usd_per_proposal"] = s["usd"] / s["proposals"] if s["proposals"] else None
        # final only when nothing is pending: a pending proposal may still be accepted
        s["usd_per_accepted"] = s["usd"] / s["accepted"] if s["accepted"] else None
        s["final"] = s["pending"] == 0
    return {"night": key, "start": start, "end": end, "stages": res, "units": unit_rows,
            "other_served_calls": other, "accepted_outside_the_night": outside}


def fmt(v, kind):
    if v is None:
        return "—"
    return {"usd": f"${v:.2f}", "ratio": f"{v:.2f}", "per": f"${v:.3f}"}.get(kind, str(v))


def print_table(r):
    print(f"night {r['night']} ({r['start']} -> {r['end'] or 'end of log'}): yield of its proposals")
    print(f"{'stage':<7} {'calls':>5} {'$':>9} {'proposals':>9} {'accepted':>8} {'acc/prop':>8} "
          f"{'$/proposal':>10} {'$/accepted':>10} {'pending':>7}")
    for name in STAGES + ("total",):
        s = r["stages"][name]
        print(f"{name:<7} {s['calls']:>5} {fmt(s['usd'], 'usd'):>9} {s['proposals']:>9} {s['accepted']:>8} "
              f"{fmt(s['ratio'], 'ratio'):>8} {fmt(s['usd_per_proposal'], 'per'):>10} "
              f"{fmt(s['usd_per_accepted'], 'per') + ('' if s['final'] else '*'):>10} {s['pending']:>7}")
    nc = r["stages"]["total"]["nocost"]
    if nc:
        print(f"calls without $ (no JSON result, counted as $0): {nc}")
    states = {}
    for u in r["units"]:
        states[u["state"]] = states.get(u["state"], 0) + 1
    print("units with candidates: " + (", ".join(f"{k} {v}" for k, v in sorted(states.items())) or "none")
          + "; acc/prop is over the REVIEWED proposals; `*` = proposals still pending, $/accepted not final")
    if r["accepted_outside_the_night"]:
        print(f"not counted: {r['accepted_outside_the_night']} card(s) the same approvals took from units this "
              f"night did not publish")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("night")
    ap.add_argument("--log", default=None)
    ap.add_argument("--root", default=None)
    ap.add_argument("--approvals", default=None)
    ap.add_argument("--markers", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = a.root or P.get("KHMS_ROOT")
    log = a.log or P.get("KHMS_NIGHT_LOG")
    approvals = a.approvals or os.path.join(P.SHARED_STATE, "approvals")
    markers = a.markers or P.REVIEW_MARKERS
    r = report(a.night, log, root, approvals, markers)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        print_table(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
