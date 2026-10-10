#!/usr/bin/env python3
"""Queue pending review through an explicit transport; delivery never approves cards.

The transport receives --receipt TOKEN --units-json JSON, performs its own busy check,
and returns 0 plus TOKEN in stdout only after a confirmed reply. Exit 3 means busy;
other failures remain pending. No reviewed marker is written by this tool.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

import khms_paths as P


def pending(tools, env):
    result = subprocess.run([sys.executable, str(Path(tools) / 'khms_review_state.py')],
                            env=env, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('review census failed: ' + result.stderr[-500:])
    return [line for line in result.stdout.splitlines() if line.startswith('UNREVIEWED')]


def dispatch(tools, runtime, transport, env, dry=False, timeout=3600):
    units = pending(tools, env)
    if not units:
        print('REVIEW-DEPUTY: clean; no delivery')
        return 0
    if not transport:
        raise ValueError('no review transport configured')
    receipt = 'REVIEW-REQUEST:' + uuid.uuid4().hex
    argv = [*transport, '--receipt', receipt, '--units-json', json.dumps(units)]
    if dry:
        print('REVIEW-DEPUTY: dry-run ' + json.dumps({'units': units, 'transport': transport}))
        return 0
    result = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=timeout)
    if result.stdout:
        print(result.stdout, end='')
    if result.returncode == 3:
        print('REVIEW-DEPUTY: busy; review remains pending')
        return 3
    if result.returncode or receipt not in result.stdout:
        print('REVIEW-DEPUTY: delivery not confirmed; review remains pending', file=sys.stderr)
        return 4
    print('REVIEW-DEPUTY: reply received; approval belongs to the shared review tools')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--transport', default=None,
                        help='quoted argv of an explicit delivery transport')
    parser.add_argument('--runtime', default=None)
    parser.add_argument('--timeout', type=int, default=3600)
    args = parser.parse_args(argv)
    try:
        transport = args.transport
        if transport is None:
            transport = P.get('KHMS_REVIEW_DEPUTY_TRANSPORT')
        runtime = Path(args.runtime or P.get('KHMS_RUNTIME'))
        runtime.mkdir(parents=True, exist_ok=True)
        with (runtime / 'review-deputy.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('REVIEW-DEPUTY: another delivery running; pending unchanged')
                return 3
            return dispatch(P.CODE, runtime, shlex.split(transport), dict(os.environ),
                            args.dry_run, args.timeout)
    except (OSError, ValueError, subprocess.TimeoutExpired, P.KhmsPathError) as exc:
        print(f'REVIEW-DEPUTY: UNKNOWN {exc}; review remains pending', file=sys.stderr)
        return 4


if __name__ == '__main__':
    sys.exit(main())
