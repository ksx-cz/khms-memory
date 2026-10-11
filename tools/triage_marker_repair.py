#!/usr/bin/env python3
"""One-off repair of triage review markers written under the old marker name (#173). DRY RUN unless --apply.

    triage_marker_repair.py [--apply] UNIT[=SHA256] ...

Before #173's fix `apply_triage.py` named a unit's review marker `.reviewed-<first date>-triage`, while the reader
(`khms_review_state.triage_reviewed`) looks for `.reviewed-<file stem>`. A SUFFIXED unit applied under the old
writer (`DATE-coordinator-triage.md`, `DATE-lerr10-triage.md`) therefore never had the marker its reader wants,
and a legacy EMPTY marker older than its unit (written once, never refreshed when the verdict was finished) is
rejected by the mtime rule of `mark_ledger.marker_holds`. Both read as "unreviewed" although the units were applied.

For each UNIT named on the command line (never a glob, never the whole inbox) the marker `.reviewed-<stem>` is
written as `sha256=<the unit's sha256 now>` -- the form the reader checks -- ONLY when all of these hold:
  * `=SHA256`, when given, equals the unit's sha256 now (the caller checked THIS content);
  * `<stem>.applied.md` exists beside the unit, is not older than the unit (the review was applied after the
    unit's last edit; a unit edited after its apply still owes a review), and is a GENUINE APPLIED report of THIS
    unit: its first line is `# apply_triage — <unit file name> (APPLIED <timestamp>)`. `apply_triage.py --dry-run`
    writes the same file with `(DRY RUN …)`, so existence and mtime alone prove nothing; a DRY RUN,
    malformed or other unit's report is refused (review FIX, 2026-10-11). A unit really applied whose report a
    later dry run overwrote is refused too -- the safe direction: apply it again;
  * the marker is absent, or is a legacy marker with no sha256 line. A marker that records the sha256 of OTHER
    content is a review of something else and is never overwritten.
A marker that already holds for the unit is reported ALREADY and left as it is, so a second run changes nothing.
With --apply every check is made again and the marker written while holding the `cards` lock -- the lock
apply_triage.py holds while it writes the report and the marker -- so no apply can run between check and write.
No other marker is touched: the date unit's `.reviewed-DATE-triage` of a suffixed unit stays byte-for-byte.
It does not run apply_triage.py and writes no card, no report and no `.applied.md`.

Output: one line per unit (WOULD WRITE / WROTE / ALREADY / REFUSED <reason>). Exit 0 when nothing was refused,
1 when any unit was refused, 2 on a usage error.
"""
import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_lock as L  # noqa: E402
import khms_paths as P  # noqa: E402
import khms_review_state as RS  # noqa: E402
import mark_ledger as ML  # noqa: E402


# The first line apply_triage.apply_locked writes: `# apply_triage — <unit basename> (APPLIED|DRY RUN <iso stamp>)`.
APPLIED_HEADER = re.compile(r"# apply_triage — (?P<unit>\S+) \((?P<mode>APPLIED|DRY RUN) (?P<at>\S+)\)")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plan(unit, expect, markers):
    """(verdict, marker path, body) for one unit; verdict is WRITE, ALREADY or 'REFUSED <why>'."""
    u = Path(unit)
    marker = Path(markers) / RS.triage_marker_name(u)
    if not u.is_file() or not u.name.endswith("-triage.md") or not re.match(r"\d{4}-\d{2}-\d{2}", u.name):
        return "REFUSED not a triage unit file (DATE[-suffix]-triage.md)", marker, None
    now = sha256(u)
    if expect and expect != now:
        return f"REFUSED the unit's sha256 is {now}, not the expected {expect}", marker, None
    applied = u.with_name(u.stem + ".applied.md")
    if not applied.is_file():
        return f"REFUSED no {applied.name} beside it — nothing proves the review was applied", marker, None
    if applied.stat().st_mtime < u.stat().st_mtime:
        return (f"REFUSED {applied.name} is older than the unit — the unit was edited after its apply "
                f"and still owes a review"), marker, None
    with open(applied, encoding="utf-8", errors="replace") as fh:
        head = fh.readline().rstrip("\n")
    mh = APPLIED_HEADER.fullmatch(head)
    if not mh:
        return (f"REFUSED {applied.name} is not an apply_triage report (first line {head[:80]!r}) — "
                f"nothing proves the review was applied"), marker, None
    if mh.group("unit") != u.name:
        return (f"REFUSED {applied.name} reports on {mh.group('unit')}, not on {u.name}"), marker, None
    if mh.group("mode") != "APPLIED":
        return (f"REFUSED {applied.name} is a DRY RUN report — a preview, not an application "
                f"(apply_triage --dry-run writes it too)"), marker, None
    if ML.marker_holds(str(marker), str(u)):
        return "ALREADY", marker, None
    if marker.exists():
        hm = re.search(r"sha256=([0-9a-f]{64})", marker.read_text(encoding="utf-8", errors="replace"))
        if hm:
            return (f"REFUSED {marker.name} records sha256 {hm.group(1)} — a review of other content, "
                    f"not overwritten"), marker, None
    return "WRITE", marker, f"sha256={now}\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("units", nargs="+", metavar="UNIT[=SHA256]")
    ap.add_argument("--apply", action="store_true", help="write the markers (default: dry run, writes nothing)")
    a = ap.parse_args(argv)
    root = P.ROOT
    markers = RS.triage_markers_dir(root)
    targets = []
    for arg in a.units:
        unit, _, expect = arg.partition("=")
        if expect and not re.fullmatch(r"[0-9a-f]{64}", expect):
            print(f"usage: {arg}: the expected sha256 must be 64 lowercase hex digits", file=sys.stderr)
            return 2
        targets.append((unit, expect))
    if not a.apply:
        return report([(u, *plan(u, e, markers)) for u, e in targets], False)
    with L.lock("cards", root):    # check and write in one critical section with apply_triage.py
        rows = [(u, *plan(u, e, markers)) for u, e in targets]
        if any(v == "WRITE" for _u, v, _m, _b in rows):
            P.require_writable("repair triage review markers")
            P.require_may_write([str(m) for _u, v, m, _b in rows if v == "WRITE"], "repair triage review markers")
        return report(rows, True)


def report(rows, apply):
    refused = 0
    for unit, verdict, marker, body in rows:
        if verdict == "WRITE":
            if apply:
                L.atomic_text(str(marker), body)
                print(f"WROTE {marker} {body.strip()}  ({unit})")
            else:
                print(f"WOULD WRITE {marker} {body.strip()}  ({unit})")
        elif verdict == "ALREADY":
            print(f"ALREADY {marker} holds for {unit}")
        else:
            refused += 1
            print(f"{verdict}  ({unit})")
    if not apply:
        print("dry run — nothing written; repeat with --apply to write the lines above")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
