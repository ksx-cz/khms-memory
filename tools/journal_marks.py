#!/usr/bin/env python3
"""Append findings once, independently of tokens, transcripts and board availability.

A package relays ONLY its ## MARKs section; a session appends directly with --mark.
Stable source+content identities are embedded in the journal itself. Thus losing a
cache or dying after append cannot duplicate a finding on retry, even the next day.
All cooperating writers use one flock; each complete line is one O_APPEND write.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sys

import khms_paths as P
import mark_ledger as ML


def marks(text):
    active = False
    for line in text.splitlines():
        if re.match(r'^##\s+MARKs\b', line, re.I):
            active = True
            continue
        if re.match(r'^#{1,2}\s', line):
            active = False
        if active:
            line = re.sub(r'^\s*(?:[-*]\s+)?', '', line).strip('` ')
            # Preserve an original timestamp in backlog handoffs.
            if ML.parse_marks(line):
                yield line


def append_once(journal, runtime, entries, source, dry_run=False):
    journal, runtime = Path(journal), Path(runtime)
    now = dt.datetime.now().astimezone()
    rows = []
    for entry in entries:
        if '\n' in entry or '\r' in entry:
            raise ValueError('a MARK must be one complete line')
        ident = hashlib.sha256((source + '\0' + entry).encode()).hexdigest()
        rows.append((ident, entry))
    if dry_run:
        return len(rows)
    P.require_writable('append journal MARKs')
    runtime.mkdir(parents=True, exist_ok=True)
    journal.mkdir(parents=True, exist_ok=True)
    with (runtime / 'journal.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # The journal is the durable receipt, not a second state write which could
        # fail between committing a line and marking it delivered.
        seen = set()
        for path in journal.glob('????-??-??.md'):
            with path.open(encoding='utf-8') as fh:
                for line in fh:
                    seen.update(re.findall(r'(?:<!-- |\()mark-id:([0-9a-f]{64})(?: -->|\))', line))
        fd = os.open(journal / (now.date().isoformat() + '.md'),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        count = 0
        try:
            for ident, entry in rows:
                if ident in seen:
                    continue
                token = ML.TOKEN_RE.search(entry)
                if token and token.start() and (ML.ENTRY_RE.match(entry) or re.match(r'^\d{4}-\d{2}-\d{2}\b', entry)):
                    # Keep an original timestamp before the receipt, not nested after
                    # the delivery timestamp: the ledger must still parse the record.
                    recorded = entry[:token.start()] + f'(mark-id:{ident}) ' + entry[token.start():]
                else:
                    recorded = f'{now.isoformat(timespec="seconds")} (mark-id:{ident}) {entry}'
                payload = ('- ' + recorded + '\n').encode()
                if os.write(fd, payload) != len(payload):
                    raise OSError('short journal append; inspect journal before retry')
                seen.add(ident)
                count += 1
            os.fsync(fd)
        finally:
            os.close(fd)
        return count


def destinations(journal_dir=None, runtime_dir=None):
    """Explicit destinations or the generic memory deployment's configured layout."""
    journal = journal_dir or os.path.join(P.get('KHMS_ROOT'), 'journal')
    runtime = runtime_dir or P.get('KHMS_RUNTIME')
    return journal, runtime


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--handoff')
    g.add_argument('--mark')
    p.add_argument('--source', help='stable session/finding id for a direct MARK')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--journal-dir', help='journal directory (default: KHMS_ROOT/journal)')
    p.add_argument('--runtime-dir', help='writer lock directory (default: KHMS_RUNTIME)')
    a = p.parse_args(argv)
    try:
        journal, runtime = destinations(a.journal_dir, a.runtime_dir)
        if a.handoff:
            path = Path(a.handoff).resolve(strict=True)
            entries = list(marks(path.read_text()))
            source = a.source or str(path)
        else:
            if not a.source or not re.match(r'^MARK\[[^\]]+\]', a.mark) or not ML.parse_marks(a.mark):
                p.error('--mark requires MARK[kind] and a stable --source')
            entries, source = [a.mark], a.source
        n = append_once(journal, runtime, entries, source, a.dry_run)
        print(f'journal: {n} MARKs {"planned" if a.dry_run else "appended"}; source={source}')
    except (OSError, ValueError, P.KhmsPathError) as exc:
        print(f'journal: FAILED: {exc}', file=sys.stderr)
        return 8
    return 0


if __name__ == '__main__':
    sys.exit(main())
