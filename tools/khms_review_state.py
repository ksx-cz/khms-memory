#!/usr/bin/env python3
"""One pending census for cards and triage; hashes invalidate changed review units."""
import argparse
import os
from pathlib import Path
import sys
import khms_files
import khms_paths as P
import mark_ledger as ML


def triage_marker_name(unit):
    """THE name of a triage unit's review marker: `.reviewed-<file stem>`. One definition for the writer
    (apply_triage.py) and this reader. Until #173 the writer used `.reviewed-<first date>-triage`, so a
    suffixed unit (`DATE-lerr10-triage.md`) was never found reviewed and applying it overwrote the marker of
    the nightly `DATE-triage.md`; for a plain `DATE-triage.md` both spellings are the same file."""
    return '.reviewed-' + Path(unit).stem


def triage_markers_dir(root=None):
    root = root or P.ROOT
    return P.REVIEW_MARKERS if P.same_path(root, P.soft('ROOT')) else str(Path(root)/'state/shared/reviewed')


def triage_reviewed(unit, root=None, markers=None):
    markers = markers or triage_markers_dir(root)
    name = triage_marker_name(unit)
    # the second spelling (stem without `-triage`) is a hand-written legacy form, read but never written
    return any(ML.marker_holds(str(Path(markers)/n), str(unit))
               for n in (name, name.removesuffix('-triage')))


def pending(root=None):
    root = root or P.ROOT
    paths = ML.Paths(root)
    out = [(str(p), 'cards') for p, ok in ML.review_units(paths) if not ok]
    for p in khms_files.glob(str(Path(root)/'memory/inbox/*-triage.md')):
        if not triage_reviewed(p, root):
            out.append((p, 'triage'))
    import nightly_state
    night = nightly_state.in_progress(root)
    if night:
        inbox = str(Path(root)/'memory/inbox')
        ready, _wip = nightly_state.split_ready_wip([Path(p).name for p, _k in out], inbox, night)
        out = [(p,k) for p,k in out if Path(p).name in ready]
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--triage-reviewed')
    a = ap.parse_args()
    if a.triage_reviewed:
        return 0 if triage_reviewed(a.triage_reviewed) else 1
    for path, kind in pending():
        print('UNREVIEWED ' + kind + ' ' + path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
