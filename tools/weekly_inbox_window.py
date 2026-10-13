#!/usr/bin/env python3
"""Select the weekly synthesis's inbox input BY DATE, not by file count.

Until 2026-10-02 `weekly_synthesis.sh` took `ls memory/inbox/2*.md | tail -7` — seven FILES.
The pipeline writes four to twenty files per day (candidates, batches, approve, triage …), so
seven files were about a day and a half, and the cut was collation order, so it could keep a
day's triage file and drop that day's candidates. Twice (2026-09-20, 2026-09-27) five of the
window's days never reached the stage, and nothing said so.

THE WINDOW. It starts on the day of the previous weekly run (the newest `YYYY-MM-DD-weekly.md`
in the inbox dated before DATE) and ends on DATE, both days INCLUSIVE:
  * the start day is included because files of that date written AFTER the previous weekly ran
    (the coordinator's triage, a late batch) were never seen by it — re-reading the few it did
    see costs little, dropping the rest is what the owner's rule forbids;
  * a missed week therefore widens the window instead of losing a week;
  * with no previous weekly the window is the seven days before DATE plus DATE.
The date is read from the FILE NAME (the stage that wrote it), never from mtime: a sync or a
copy rewrites mtime, the name does not.

The weekly's own artefacts (`YYYY-MM-DD-weekly*`) are not input: they are this stage's earlier
proposals, already reviewed into cards the headers digest carries.

Only source proposals are input: plain nightly units (including rerun batches), plain
triage proposals and other dated source names. Approval derivatives, triage processing
artifacts and sync conflict copies are excluded. Raw directory names are inspected before
canonical checks so even conflict copies are accounted for. Each excluded `2*.md` candidate
is named on stderr with its reason (including dates outside the window); days without a
source are listed as EMPTY. Unknown source names are retained rather than silently dropped.

Usage:  weekly_inbox_window.py <inbox dir> <DATE>
  stdout: one selected path per line, in chronological order (date, then name)
  stderr: one summary line `weekly inbox window START..DATE: N files over D of W days …`
Exit 0 always when the inbox is readable; 2 on a bad DATE.
"""
import datetime as dt
import os
import khms_files
import re
import sys

DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})")
OWN = re.compile(r"^\d{4}-\d{2}-\d{2}-weekly")
TRIAGE_ARTIFACT = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:-\d+)?-triage(?:\.md)?\."
    r"(preview|verdict-draft|verdict-final|applied|doubtful-resolved)\.md$")
DEFAULT_SPAN = 7   # days before DATE when there is no previous weekly


def _date(s):
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def select(inbox, today, exclusions=None):
    exclusions = exclusions if exclusions is not None else []
    names = sorted(n for n in os.listdir(inbox) if n.startswith("2") and n.endswith(".md"))
    dated, undated = [], []
    for n in names:
        if not khms_files.canonical(n):
            exclusions.append((n, "sync conflict copy"))
            continue
        m = DATED.match(n)
        d = _date(m.group(1)) if m else None
        (dated if d else undated).append((d, n))
    prev = [d for d, n in dated if OWN.match(n) and n.endswith("-weekly.md") and d < today]
    start = max(prev) if prev else today - dt.timedelta(days=DEFAULT_SPAN)
    picked = []
    for d, n in dated:
        if n.endswith(".approve.md"):
            reason = "approval derivative"
        elif TRIAGE_ARTIFACT.fullmatch(n):
            reason = "triage artifact"
        elif OWN.match(n):
            reason = "weekly output (boundary only)"
        elif not start <= d <= today:
            reason = "outside window"
        else:
            picked.append((d, n))
            continue
        exclusions.append((n, reason))
    exclusions.extend((n, "no valid date in name") for _, n in undated)
    picked.sort()
    days = [start + dt.timedelta(days=i) for i in range((today - start).days + 1)]
    have = {d for d, _ in picked}
    empty = [d for d in days if d not in have]
    return start, bool(prev), picked, days, empty, [n for _, n in undated]


def main(argv):
    if len(argv) != 3:
        print("usage: weekly_inbox_window.py <inbox dir> <YYYY-MM-DD>", file=sys.stderr)
        return 2
    inbox, today = argv[1], _date(argv[2])
    if today is None:
        print(f"weekly inbox window: bad DATE {argv[2]!r}", file=sys.stderr)
        return 2
    exclusions = []
    start, had_prev, picked, days, empty, undated = select(inbox, today, exclusions)
    for _, n in picked:
        print(os.path.join(inbox, n))
    line = (f"weekly inbox window {start}..{today}: {len(picked)} files over "
            f"{len(days) - len(empty)} of {len(days)} days"
            f" (start = {'previous weekly' if had_prev else 'no previous weekly, default %d days' % DEFAULT_SPAN})")
    if empty:
        line += "; EMPTY days: " + ", ".join(str(d) for d in empty)
    for name, reason in sorted(exclusions):
        line += f"; NOT SELECTED {name}: {reason}"
    print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
