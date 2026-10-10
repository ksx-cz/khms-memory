#!/usr/bin/env python3
"""Inventory/import a historical MARK backlog; default is a read-only preview.

Original handoff paths and mtimes are retained in the manifest; journal receipt
identities are the SAME as regular handoff delivery, so a second import is inert.
"""
import argparse
import hashlib
import json
from pathlib import Path
import journal_marks as J
import khms_paths as P


def inventory(directory):
    out = []
    for path in sorted(Path(directory).glob('*.md')):
        entries = list(J.marks(path.read_text()))
        if entries:
            source = str(path.resolve())
            out.append({'source': source, 'mtime_ns': path.stat().st_mtime_ns,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'marks': entries})
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory'); p.add_argument('--apply', action='store_true')
    p.add_argument('--manifest', help='expected preview manifest; mandatory when applying')
    p.add_argument('--journal-dir', help='journal directory (default: KHMS_ROOT/journal)')
    p.add_argument('--runtime-dir', help='writer lock directory (default: KHMS_RUNTIME)')
    a = p.parse_args(argv)
    rows = inventory(a.directory)
    if not a.apply:
        print(json.dumps({'files':len(rows), 'marks':sum(len(r['marks']) for r in rows),
                          'entries':rows}, ensure_ascii=False, indent=2))
        return 0
    if not a.manifest or json.loads(Path(a.manifest).read_text())['entries'] != rows:
        p.error('apply requires the unchanged reviewed preview manifest')
    P.require_writable('import journal backlog')
    journal, runtime = J.destinations(a.journal_dir, a.runtime_dir)
    count = sum(J.append_once(journal,runtime,r['marks'],r['source']) for r in rows)
    print(f'backlog: {len(rows)} handoffs; {count} newly appended MARKs')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
