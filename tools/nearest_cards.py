#!/usr/bin/env python3
"""Annotate nightly candidates with the existing cards nearest to them.

    nearest_cards.py FILE [--topn 3] [--only-missing] [--no-dense] [--quiet]

WHY THIS EXISTS
---------------
Measured 2026-09-02: the base holds 17 cards about "SVD48V bus voltage", 14 (+3)
about "minimum turning radius", 8 about "wheel radius" — and 8 superseded +
7 refuted edges in the whole base.  The nightly pipeline PRODUCES; nothing in it
MERGES.  The consolidate stage was asked to "link candidates to existing cards"
while being handed only topic views, so the question "which card does this
replace?" was one it could only answer from whatever it happened to read — the
same shape as the grounding failure verify_quotes.py exists to close.

This tool answers the mechanical half BEFORE the model is asked the editorial
half: for every `### C<n>` candidate it runs the SAME retrieval the recall layer
uses (khms_search, plus the dense channel when the daemon answers) over the
candidate's own title and body, and writes back

    NEAREST: K-01234 (41.2), K-00987 (33.8), K-02201 (28.1)

as the last line of the candidate.  0 API tokens.  The consolidate prompt then
requires exactly one RELATION line naming one of these ids (or another existing
id it can justify), and tools/verify_relations.py enforces that mechanically.

IDEMPOTENT.  Re-running replaces the line it wrote, so a re-run in the pipeline
or by hand during the morning review cannot pile lines up (`cmp` clean).
`--only-missing` leaves an existing NEAREST line alone and computes one only
where none exists — that is the mode used on the CONSOLIDATE output, where a
line the model carried through from the sweep is the one verify_relations must
judge `RELATION: new` against.

ONLY ACTIVE, NON-FOG CARDS are proposed: pointing a supersedes edge at a card
that is already superseded chains onto a dead end, and the fog (archive) is
where retired cards live.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_search as ks  # noqa: E402
import khms_paths as P  # noqa: E402

# Same label grammar as verify_quotes.py: a big day's sweep numbers its chunks
# A1.., B1.., C1.., so a regex hardcoded to C matches nothing exactly when the
# night is biggest.
CARD_RE = re.compile(r"^#{2,3}\s+([A-Z]\d+[a-z]?)\b.*$", re.M)
NEAREST_RE = re.compile(r"^NEAREST:.*$\n?", re.M)
FENCE_RE = re.compile(r"```(?:ya?ml)?\n.*?```\n?", re.S)
SRC_RE = re.compile(r"^\s*-\s*src=[\w-]+\s*::\s*", re.M)
RELATION_RE = re.compile(r"^\s*\*{0,2}RELATION:.*$\n?", re.M)
# Query terms are capped: khms_search scores every token against every card, and
# a 4 kB candidate carries several hundred tokens of prose whose tail adds noise,
# not signal.  Measured on the 2026-09-03 sweep (54 candidates, 2510 cards).
MAX_QUERY_TOKENS = 220


def split_cards(text):
    """-> [(label, start, end)] over the candidate blocks only.

    A card ends at the next card heading OR the next top-level `## ` section,
    whichever comes first — so `## Flagged` / `## Dropped` / `## DEFERRED`
    trailers are never absorbed into the last candidate (the 2026-07-30 bug in
    verify_quotes.py, fixed there, not repeated here).
    """
    out, marks = [], list(CARD_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        section = re.search(r"^## \S", text[m.end():end], re.M)
        if section:
            end = m.end() + section.start()
        out.append((m.group(1), m.start(), end))
    return out


def query_text(block):
    """The candidate reduced to what is worth searching on."""
    q = NEAREST_RE.sub("", block)
    q = RELATION_RE.sub("", q)
    q = FENCE_RE.sub("", q)
    q = SRC_RE.sub("", q)
    q = re.sub(r"^#{2,3}\s+[A-Z]\d+[a-z]?:?\s*", "", q)      # drop the label
    q = re.sub(r"\*\*QUOTES:\*\*", "", q)
    toks = ks.tokens(q)
    return " ".join(toks[:MAX_QUERY_TOKENS])


def fmt(score):
    return f"{score:.1f}" if score >= 1 else f"{score:.4f}"


def open_dense(timeout_ms):
    """-> the hybrid module when the dense daemon really answers, else None.

    PROBED ONCE, not per candidate.  Measured 2026-09-04 on the 09-03 sweep: with
    the interactive 150 ms budget the daemon lost the race on the first run and
    won it on the second, so two runs of the same file produced two different
    NEAREST lines — an idempotence failure caused entirely by the timeout, not by
    the ranking.  A nightly batch is not an interactive hook: one probe with a
    generous budget decides the channel for the whole file, so a run is internally
    consistent and a re-run is byte-identical.
    """
    try:
        import khms_recall_hybrid as hy
    except Exception:
        return None
    try:
        hits, err, _ms = hy.dense_query("probe", topn=1, timeout_ms=timeout_ms)
    except Exception:
        return None
    # FAIL-BACK IS SILENT BY DESIGN (same contract as recall.sh): a half-installed
    # experiment must never cost the nightly its NEAREST lines, and lexical alone
    # is the channel this base was built on.
    return None if (err or not hits) else hy


def rank(query, cards, topn, hy, timeout_ms):
    if hy is not None:
        try:
            res, meta = hy.hybrid_rank(query, cards=cards, topn=topn,
                                       timeout_ms=timeout_ms)
            if meta.get("mode") == "hybrid":
                return res, "hybrid"
        except Exception:
            pass
    res, _ = ks.search(query, cards=cards, topn=topn)
    return res, "lexical"


def annotate(text, cards, topn=3, only_missing=False, dense=True,
             timeout_ms=1500):
    """-> (new text, n_annotated, n_skipped, mode)."""
    hy = open_dense(timeout_ms) if dense else None
    spans = split_cards(text)
    out, prev, n, skipped, mode = [], 0, 0, 0, "lexical"
    for _label, start, end in spans:
        out.append(text[prev:start])
        block = text[start:end]
        if only_missing and NEAREST_RE.search(block):
            out.append(block)
            prev = end
            skipped += 1
            continue
        stripped = NEAREST_RE.sub("", block)
        res, mode = rank(query_text(stripped), cards, topn, hy, timeout_ms)
        ids = ", ".join(f"{c['id']} ({fmt(s)})" for s, _m, c in res)
        line = f"NEAREST: {ids}" if ids else "NEAREST: (no card scored above zero)"
        out.append(stripped.rstrip("\n") + "\n\n" + line + "\n\n")
        prev = end
        n += 1
    out.append(text[prev:])
    return "".join(out), n, skipped, mode


def active_cards():
    """Active, non-fog cards only — see the module docstring."""
    return [c for c in ks.load_cards()
            if c.get("status") == "active" and not c.get("fog")]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("file", help="a sweep or consolidate candidate file")
    ap.add_argument("--topn", type=int, default=3)
    ap.add_argument("--only-missing", action="store_true",
                    help="leave an existing NEAREST line alone")
    ap.add_argument("--no-dense", action="store_true",
                    help="lexical channel only (the dense one already "
                         "fails back silently when the daemon is absent)")
    ap.add_argument("--dense-timeout-ms", type=int, default=1500,
                    help="batch budget for the dense channel, not the 150 ms "
                         "interactive one — see open_dense()")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    try:
        text = open(a.file, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    cards = active_cards()
    if not cards:
        print("FATAL: no active cards loaded — wrong KHMS_ROOT?", file=sys.stderr)
        return 2
    new, n, skipped, mode = annotate(text, cards, topn=a.topn,
                                     only_missing=a.only_missing,
                                     dense=not a.no_dense,
                                     timeout_ms=a.dense_timeout_ms)
    if not n and not skipped:
        print(f"FATAL: no '### C<n>' candidates found in {a.file}",
              file=sys.stderr)
        return 2
    if new != text:
        if not P.may_write(a.file):
            P.require_writable(f"rewrite {a.file}")
        # Atomic: a half-written candidate file would be handed to the
        # consolidate stage as if it were whole.
        tmp = a.file + ".nearest.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, a.file)
    if not a.quiet:
        print(f"nearest_cards: {n} candidate(s) annotated"
              f"{f', {skipped} already had a NEAREST line' if skipped else ''}"
              f" · channel={mode} · {len(cards)} active cards · {a.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
