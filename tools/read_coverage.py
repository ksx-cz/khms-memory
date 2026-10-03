#!/usr/bin/env python3
"""Did a headless model stage READ its input files whole? — proof from its own transcript.

Usage: python3 tools/read_coverage.py --transcript T.jsonl [--label L] [--list F | files ...]   (run by weekly_synthesis.sh)

WHY (#209, K209H; REV209 F5). A stage that is HANDED a file has not read it. The 2026-09-27
weekly synthesis was handed a 366 kB inbox concatenation; one Read failed with "39242 tokens
exceeds maximum allowed tokens (25000)", the successful Reads covered lines 1-400, 1429-1778,
1917-2096 and 2527-3256, and lines 401-1428 — most of 2026-09-26's candidates — were never read,
while the output looked like a normal week. Nothing in the pipeline could tell.

This tool reads the stage's Claude Code transcript (`<session-id>.jsonl`) and, for every file
the stage was required to read, joins each `Read` tool call to its result: a result that is an
error covers nothing; a successful result covers lines startLine .. startLine+numLines-1 of a
file of totalLines lines (the Read tool's own `toolUseResult.file` record). A file is READ when
the union of its covered ranges is 1..totalLines and totalLines matches the file on disk. Grep
hits, a Read of a different path, or a Read that was refused do not count.

Limit, said rather than hidden: the Read tool also shortens single lines over ~2000 characters.
Such a line is "covered" by line number while part of its text was not shown; every required
file holding one is named on a WARN line.

Usage:
  read_coverage.py --transcript T.jsonl --label NAME FILE [FILE ...]
  read_coverage.py --transcript T.jsonl --label NAME --list LISTFILE   (one path per line)
Prints `READ-COVERAGE <label>: <k>/<n> files read whole` and one `NOT READ <path>: <why>` line per
miss; exit 0 only when every required file was read whole, 1 otherwise, 2 on a usage/IO error.
"""
import argparse
import json
import os
import sys

LONG_LINE = 2000


def _iter_blocks(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(j, dict):
                continue
            msg = j.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if isinstance(b, dict):
                    yield j, b


def reads(transcript):
    """{abs path: [(start, end) covered]} plus {abs path: [error text]} from one transcript."""
    calls, covered, errors = {}, {}, {}
    for j, b in _iter_blocks(transcript):
        if b.get("type") == "tool_use" and b.get("name") == "Read":
            fp = (b.get("input") or {}).get("file_path")
            if fp:
                calls[b.get("id")] = os.path.realpath(fp)
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
            fp = calls[b["tool_use_id"]]
            tur = j.get("toolUseResult")
            f = tur.get("file") if isinstance(tur, dict) else None
            if b.get("is_error") or not isinstance(f, dict):
                txt = b.get("content")
                if isinstance(txt, list):
                    txt = " ".join(str(x.get("text", "")) for x in txt if isinstance(x, dict))
                errors.setdefault(fp, []).append(str(txt or "error")[:160])
                continue
            try:
                start, num, total = int(f.get("startLine", 1)), int(f["numLines"]), int(f["totalLines"])
            except (KeyError, TypeError, ValueError):
                errors.setdefault(fp, []).append("result without a line record")
                continue
            covered.setdefault(fp, []).append((start, start + num - 1, total))
    return covered, errors


def _lines_on_disk(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines = text.splitlines()
    return len(lines), sum(1 for x in lines if len(x) > LONG_LINE)


def check(transcript, required):
    """-> (ok list, [(path, why)] misses, [(path, n long lines)] warnings)."""
    covered, errors = reads(transcript)
    ok, miss, warn = [], [], []
    for p in required:
        rp = os.path.realpath(p)
        try:
            n_disk, n_long = _lines_on_disk(rp)
        except OSError as exc:
            miss.append((p, "cannot read it on disk (%s)" % exc.__class__.__name__))
            continue
        spans = covered.get(rp, [])
        if not spans:
            why = "never read" if rp not in errors else "every Read failed: " + errors[rp][-1]
            miss.append((p, why))
            continue
        # The Read tool counts the empty string after a final newline as a line of its own.
        totals = {t for _s, _e, t in spans}
        if n_disk and not totals <= {n_disk, n_disk + 1}:
            miss.append((p, "the Read saw %s lines, the file has %d" % (sorted(totals), n_disk)))
            continue
        have, gaps = sorted((s, e) for s, e, _t in spans), []
        nxt = 1
        for s, e in have:
            if s > nxt:
                gaps.append((nxt, s - 1))
            nxt = max(nxt, e + 1)
        if nxt <= n_disk:
            gaps.append((nxt, n_disk))
        if gaps:
            miss.append((p, "lines never read: " + ", ".join("%d-%d" % g for g in gaps[:8])))
            continue
        ok.append(p)
        if n_long:
            warn.append((p, n_long))
    return ok, miss, warn


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--label", default="stage")
    ap.add_argument("--list", help="file with one required path per line")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args(argv)
    req = list(a.files)
    try:
        if a.list:
            with open(a.list, encoding="utf-8") as fh:
                req += [ln.strip() for ln in fh if ln.strip()]
        if not os.path.isfile(a.transcript):
            raise OSError("no transcript at %s" % a.transcript)
        ok, miss, warn = check(a.transcript, req)
    except OSError as exc:
        print("READ-COVERAGE %s: ERROR %s" % (a.label, exc))
        return 2
    print("READ-COVERAGE %s: %d/%d files read whole" % (a.label, len(ok), len(req)))
    for p, why in miss:
        print("NOT READ %s: %s" % (p, why))
    for p, n in warn:
        print("WARN %s: %d line(s) over %d chars — the Read tool shortens such lines" % (p, n, LONG_LINE))
    return 0 if not miss and req else 1


if __name__ == "__main__":
    sys.exit(main())
