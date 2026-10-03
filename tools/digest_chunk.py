#!/usr/bin/env python3
"""Split a large pipeline input file into chunks a headless stage can actually READ.

WHY THIS EXISTS (measured 2026-08-25, and again in hindsight on 2026-08-22).
The Read tool refuses a file over 256 kB outright and refuses any slice whose
content exceeds 25,000 tokens.  The 2026-08-25 nightly digest was 297,906 B /
3,768 lines: the sweep stage got

    File content (290.9KB) exceeds maximum allowed size (256KB)
    41774 tokens exceeds maximum allowed tokens (25000)     <- a 950-line slice

and ended up having read lines 1-550 of 3768 — 15 % of its own input — while
reporting a normal-looking run.  That is the failure class the KB calls a check
that passes whether or not the thing it guards is healthy: nothing downstream
can tell "the day was thin" from "the stage was blind".

CALIBRATION (real numbers from that digest, not estimates).  The refused slice
was lines 551-1500 = 70,099 B / 67,104 chars and the Read tool priced it at
41,774 tokens:

    1.678 bytes/token          1.606 chars/token

Both caps are therefore enforced here, and the token estimate deliberately uses
a DENSER ratio than measured (1.5 chars/token) so the estimate over-reports
rather than under-reports.  Default budget is 17,000 est. tokens per chunk =
32 % margin under the 25,000 cap, and the byte ceiling (175,000 B) keeps 30 %
margin under 250,000 B; on this material the token cap binds first, at about
25.5 kB per chunk.

Splitting respects structure: `===== SESSION <id> =====` blocks are kept whole
when they fit, a session too big for one chunk is continued in the next with a
`===== SESSION <id> (cont. N) =====` header, and the cut is ALWAYS on a line
boundary.  Everything the input holds appears in exactly one chunk (verified by
--verify, which re-joins the parts and compares against the source).

Usage:
    digest_chunk.py SRC OUTDIR PREFIX [--max-tokens N] [--max-bytes N]
                    [--chars-per-token F] [--index FILE] [--verify]

Writes OUTDIR/PREFIX-part01.txt ... and prints one report line per chunk plus a
final summary line.  With --index, the chunk paths (one per line) are also
written to FILE, which is what the caller interpolates into the stage prompt.
"""
import argparse
import math
import os
import re
import sys

SESSION_RE = re.compile(r"^===== SESSION (.+?) =====$")

# Read-tool caps this file exists to stay under (see module docstring).
CAP_TOKENS = 25_000
CAP_BYTES = 250_000


def est_tokens(chars, chars_per_token):
    """Conservative token estimate: fewer chars per token than measured."""
    return int(math.ceil(chars / chars_per_token))


def split_lines(lines, max_tokens, max_bytes, chars_per_token):
    """-> list of chunks, each a list of lines (with their newlines).

    Greedy fill on line boundaries; a `===== SESSION` header starts a new chunk
    when the current one is already more than half full, so a chunk boundary
    lands on a session boundary whenever that costs little.
    """
    budget_chars = max_tokens * chars_per_token
    chunks, cur = [], []
    cur_chars = cur_bytes = 0
    cur_session = None

    def flush():
        nonlocal cur, cur_chars, cur_bytes
        if cur:
            chunks.append(cur)
        cur, cur_chars, cur_bytes = [], 0, 0

    for line in lines:
        m = SESSION_RE.match(line.rstrip("\n"))
        lc, lb = len(line), len(line.encode("utf-8"))
        if m:
            cur_session = m.group(1)
            # prefer a session boundary once the chunk is half full
            if cur and cur_chars > budget_chars / 2:
                flush()
        elif cur_chars + lc > budget_chars or cur_bytes + lb > max_bytes:
            flush()
            if cur_session:
                cont = "===== SESSION %s (cont.) =====\n" % cur_session
                cur.append(cont)
                cur_chars += len(cont)
                cur_bytes += len(cont.encode("utf-8"))
        cur.append(line)
        cur_chars += lc
        cur_bytes += lb
    flush()
    return chunks


def write_chunks(chunks, outdir, prefix, chars_per_token):
    """-> list of (path, bytes, chars, lines, est_tokens)."""
    # A shorter night must not leave last night's longer tail lying around with
    # a valid-looking name: clear this prefix's parts before writing.
    stale = re.compile(re.escape(prefix) + r"-part\d+\.txt$")
    for name in os.listdir(outdir):
        if stale.match(name):
            os.unlink(os.path.join(outdir, name))
    rows = []
    for i, lines in enumerate(chunks, 1):
        path = os.path.join(outdir, "%s-part%02d.txt" % (prefix, i))
        text = "".join(lines)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        rows.append((path, len(text.encode("utf-8")), len(text), len(lines),
                     est_tokens(len(text), chars_per_token)))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src")
    ap.add_argument("outdir")
    ap.add_argument("prefix")
    ap.add_argument("--max-tokens", type=int, default=17_000,
                    help="est. tokens per chunk (default 17000 = 32%% margin "
                         "under the 25000 Read cap)")
    ap.add_argument("--max-bytes", type=int, default=175_000,
                    help="bytes per chunk (default 175000 = 30%% margin under "
                         "the 250000 Read cap)")
    ap.add_argument("--chars-per-token", type=float, default=1.5,
                    help="denser than the 1.606 measured on the 2026-08-25 "
                         "digest, so the estimate over-reports")
    ap.add_argument("--index", help="write the chunk paths here, one per line")
    ap.add_argument("--verify", action="store_true",
                    help="re-join the chunks and assert nothing was lost")
    a = ap.parse_args(argv)

    with open(a.src, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    os.makedirs(a.outdir, exist_ok=True)
    chunks = split_lines(lines, a.max_tokens, a.max_bytes, a.chars_per_token)
    rows = write_chunks(chunks, a.outdir, a.prefix, a.chars_per_token)

    src_bytes = os.path.getsize(a.src)
    over = []
    for path, nb, nc, nl, nt in rows:
        flag = ""
        if nt >= CAP_TOKENS or nb >= CAP_BYTES:
            flag = "  !! OVER CAP"
            over.append(path)
        print("chunk %-70s %8d B  %7d chars  %5d lines  ~%6d tok%s"
              % (os.path.basename(path), nb, nc, nl, nt, flag))
    if a.verify:
        joined = "".join("".join(c) for c in chunks)
        stripped = re.sub(r"^===== SESSION .+? \(cont\.\) =====\n", "",
                          joined, flags=re.M)
        original = "".join(lines)
        if stripped != original:
            print("VERIFY FAILED: rejoined chunks differ from source",
                  file=sys.stderr)
            return 2
        print("verify: rejoined %d chunk(s) == source (%d B)"
              % (len(rows), src_bytes))
    if a.index:
        with open(a.index, "w", encoding="utf-8") as f:
            for path, *_ in rows:
                f.write(path + "\n")
    print("digest_chunk: %s (%d B, %d lines) -> %d chunk(s) in %s, "
          "max %d B / ~%d tok"
          % (os.path.basename(a.src), src_bytes, len(lines), len(rows),
             a.outdir, max((r[1] for r in rows), default=0),
             max((r[4] for r in rows), default=0)))
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
