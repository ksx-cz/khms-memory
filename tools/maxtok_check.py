#!/usr/bin/env python3
"""Post-stage guard: did a headless model stage hit the output-token ceiling?

WHY (measured on one nightly sweep transcript of the reference deployment): three assistant
responses ended
`stop_reason: max_tokens` at 64,000 output tokens each, with NO tool call —
192k tokens, 36 % of the run's output, produced and then thrown away, because
the model was re-emitting its whole card set into the reply instead of
appending to its output file.  Nothing in the pipeline noticed: the log said
`sweep FAILED (rc=124 = TIMEOUT)` and the truncation looked like a capacity
problem rather than a mechanical one.

This check turns that into a line in nightly.log that the morning review cannot
miss.  It is a REPORT, never a gate: it always exits 0, so a caller under
`set -e` cannot be killed by it (a non-fatal check that killed the whole
script is a defect this pipeline has already paid for once).

Dedup is by message id, keeping ONE count per message: streamed transcripts
write several rows per response and the naive count over-reports (the raw grep
on that transcript finds 4 rows for 3 responses).

Usage:
  maxtok_check.py --stage sweep --since MARKERFILE --projects ~/.claude/projects
  maxtok_check.py --stage sweep FILE.jsonl [FILE.jsonl ...]
"""
import argparse
import datetime
import json
import os
import sys


def scan_file(path):
    """-> (hits, wasted_output_tokens).  hits = distinct messages that ended
    with stop_reason == max_tokens."""
    seen = {}
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0
    with f:
        for line in f:
            if '"max_tokens"' not in line:
                continue
            try:
                j = json.loads(line)
            except Exception:
                continue
            if not isinstance(j, dict):
                continue
            msg = j.get("message")
            if not isinstance(msg, dict) or msg.get("stop_reason") != "max_tokens":
                continue
            mid = msg.get("id") or j.get("requestId") or ("anon%d" % len(seen))
            out = 0
            u = msg.get("usage")
            if isinstance(u, dict):
                try:
                    out = int(u.get("output_tokens") or 0)
                except (TypeError, ValueError):
                    out = 0
            # last snapshot of a streamed message wins
            seen[mid] = out
    return len(seen), sum(seen.values())


def newest_transcript(root, since_epoch):
    """Newest *.jsonl under `root` (recursively) modified at or after
    `since_epoch` — the session the stage that just finished wrote."""
    best, best_mt = None, -1.0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, name)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt + 1e-6 < since_epoch:
                continue
            if mt > best_mt:
                best, best_mt = p, mt
    return best


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", help="transcript JSONL(s) to inspect")
    ap.add_argument("--stage", default="stage", help="name used in the warning")
    ap.add_argument("--since", help="marker file; its mtime bounds the search")
    ap.add_argument("--since-epoch", type=float)
    ap.add_argument("--projects", help="root to search for the newest JSONL")
    a = ap.parse_args(argv)

    files = list(a.files)
    if not files and a.projects:
        since = a.since_epoch
        if since is None and a.since:
            try:
                since = os.path.getmtime(a.since)
            except OSError:
                since = 0.0
        p = newest_transcript(os.path.expanduser(a.projects), since or 0.0)
        if p:
            files = [p]
    if not files:
        # Not finding the transcript is itself worth one quiet line: the check
        # must never look like it passed when it did not run.
        print("maxtok_check: %s — no transcript found (nothing inspected)"
              % a.stage)
        return 0

    total_hits = total_out = 0
    for p in files:
        hits, out = scan_file(p)
        total_hits += hits
        total_out += out
    if total_hits:
        ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        print("%s !!!!! MAX_TOKENS: the %s stage hit the 64k output ceiling on "
              "%d response(s), %s output tokens produced and DISCARDED "
              "(no tool call in them). The stage was composing its output into "
              "the reply instead of appending it to its output file — see the "
              "incremental-write mandate in tools/prompts/. Transcript: %s"
              % (ts, a.stage, total_hits, format(total_out, ","),
                 ", ".join(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
