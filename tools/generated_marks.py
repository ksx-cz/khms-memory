#!/usr/bin/env python3
"""generated_marks.py — commit the night generator's candidate MARK lines (#237 step 2).

Usage:
  generated_marks.py build --out FILE --window "SINCE -> END" --source digest=F [--source gitlog=F]
                           [--rejects R] PART...
      Read the generator's part outputs, keep every well-formed, know-how, GROUNDED line once, and
      commit them to FILE (memory/ledger/generated/<night key>.md) with ONE rename.  Refuses an
      existing FILE (rc 3): a night key is never reused.  Prints one summary line; the rejected lines
      and their reasons go to R.
  generated_marks.py window DIR
      The latest `WINDOW:` end recorded in DIR's files (empty when none) — the night driver folds it
      into its stamp, so a run killed after the commit and before the stamp never generates the same
      window twice.

WHY.  Until #237 the transcript/git sweep wrote full candidate cards (31 parts a night) and a second
model call per batch consolidated them; the morning kept a quarter of what survived.  Now the sweep is
a GENERATOR: it writes one MARK-shaped line per piece of know-how, with the source label and a verbatim
span, and those lines go through the same per-mark evaluator as the journal's MARKs (permark.py), in
one time-sorted queue.  This tool is the 0-token gate between the two:

  * shape — `- <UTC ISO>Z MARK[<kind>] <finding> (src=digest sess <id> @<HH:MM> | src=gitlog <sha>)
    :: "<span>"`; anything else is rejected as malformed;
  * know-how — a status kind (mark_ledger.STATUS_KINDS) is rejected, as in the journal;
  * grounding — the span must occur in the source its label names (verify_quotes.classify: EXACT,
    WRAPPED or ELIDED); an ABSENT span is rejected as ungrounded.  The committed file is therefore a
    faithful excerpt of the window's sources, and a later night grounds the evaluator's quotes of a
    generated line against this file (the window's digest is not that night's digest);
  * once — a line whose id (`G-` + the ledger hash of kind|text) is already in the file is a duplicate.

Generated lines are NOT journal MARKs: they live in memory/ledger/generated/, never in the journal; the
ledger tracks them in their own `G-` id space with `origin: generated` (mark_ledger.py).
"""
import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mark_ledger as ML  # noqa: E402
import verify_quotes as VQ  # noqa: E402

TAIL_RE = re.compile(r"\((?P<label>src=(?P<src>digest|gitlog))\s+(?P<ref>[^)]+?)\)\s*::\s*"
                     r"[\"“„']?(?P<span>.*?)[\"”“']?\s*$")
SESS_RE = re.compile(r"^sess\s+\S+\s+@\d{1,2}:\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
MAX_SPAN = 600          # the prompt asks for <= 300; a longer span is still evidence, a 2 kB one is a paste
WINDOW_RE = re.compile(r"^WINDOW:\s*(.+?)\s*$", re.M)


def parse_line(line):
    """-> (dict, None) for a well-formed line, (None, reason) otherwise."""
    m = ML.GEN_LINE_RE.match(line.rstrip("\n").strip())
    if not m:
        return None, "malformed"
    t = TAIL_RE.search(m.group("text"))
    if not t:
        return None, "no source label and span"
    ref = t.group("ref").strip()
    if t.group("src") == "digest" and not SESS_RE.match(ref):
        return None, "malformed digest reference (want `sess <id> @HH:MM`)"
    if t.group("src") == "gitlog" and not SHA_RE.match(ref):
        return None, "malformed gitlog reference (want a commit sha)"
    span = t.group("span").strip()
    if not span:
        return None, "empty span"
    if len(span) > MAX_SPAN:
        return None, f"span longer than {MAX_SPAN} characters"
    if ML.status_kind(m.group("kind")):
        return None, "status kind (not know-how)"
    kind = ML.canon_kind(m.group("kind"))
    if not kind:
        return None, "kind is not a word"
    text = m.group("text").strip()
    return {"iso": m.group("iso"), "kind": kind, "text": text, "src": t.group("src"),
            "span": span, "id": ML.gen_id(kind, text),
            "line": f"- {m.group('iso')} MARK[{m.group('kind')}] {text}"}, None


def build(a):
    sources = {}
    for spec in a.source:
        name, _, path = spec.partition("=")
        try:
            sources[name] = VQ.Source(open(path, encoding="utf-8", errors="replace").read())
        except OSError:
            sources[name] = None
    if os.path.exists(a.out):
        print(f"generate: {a.out} exists — a night key is never reused, refusing", file=sys.stderr)
        return 3
    kept, seen, rejects = [], set(), []
    counts = {}
    for part in a.parts:
        try:
            lines = open(part, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for ln in lines:
            if not ln.strip() or "MARK[" not in ln:
                continue
            rec, why = parse_line(ln)
            if rec is not None:
                src = sources.get(rec["src"])
                if src is None:
                    why = f"source {rec['src']} not given or unreadable"
                elif VQ.classify(rec["span"], src)[0] == "ABSENT":
                    why = "ungrounded (span not in its source)"
                elif rec["id"] in seen:
                    why = "duplicate"
            if why:
                counts[why] = counts.get(why, 0) + 1
                rejects.append(f"{why}\t{os.path.basename(part)}\t{ln.strip()[:400]}")
                continue
            seen.add(rec["id"])
            kept.append(rec)
    kept.sort(key=lambda r: (r["iso"], r["id"]))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    head = (f"# generated lines of window {a.window} — night generator (#237 step 2); NOT journal MARKs\n"
            f"WINDOW: {a.window.split('->')[-1].strip()}\n\n")
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(head + "".join(r["line"] + "\n" for r in kept))
    os.replace(tmp, a.out)
    if a.rejects:
        with open(a.rejects, "w", encoding="utf-8") as fh:
            fh.write("".join(r + "\n" for r in rejects))
    why = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    print(f"generate: {len(kept)} line(s) committed to {a.out}; {len(rejects)} rejected"
          + (f" ({why})" if why else "") + (f" -> {a.rejects}" if a.rejects and rejects else ""))
    return 0


def window(a):
    best = ""
    for f in sorted(glob.glob(os.path.join(a.dir, "2*.md"))):
        try:
            m = WINDOW_RE.search(open(f, encoding="utf-8", errors="replace").read(4096))
        except OSError:
            continue
        if m and m.group(1) > best:
            best = m.group(1)
    if best:
        print(best)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("parts", nargs="*")
    b.add_argument("--out", required=True)
    b.add_argument("--window", required=True)
    b.add_argument("--source", action="append", default=[])
    b.add_argument("--rejects")
    b.set_defaults(fn=build)
    w = sub.add_parser("window")
    w.add_argument("dir")
    w.set_defaults(fn=window)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
