#!/usr/bin/env python3
"""khms_boundary.py -- what this repository may ship: a disclosure and boundary scanner.

Usage:
  khms_boundary.py [--repo DIR]                     generic rules over the tracked tree (CI runs this)
  khms_boundary.py --terms FILE --control DIR [--repo DIR] [--history RANGE]
                                                    + the private terms, with a positive control
  khms_boundary.py --scan-files F [F ...]           generic rules over explicit files (positive controls)

GENERIC RULES (no private knowledge needed, safe to run in public CI):
  home-path       an absolute path into a user's home directory (/home/<name>/, /Users/<name>/)
  secret          a credential-shaped string (GitHub tokens, private-key headers)
  project-import  a runtime import of, or a call into, project tooling (`tools/ops/`, `ops_paths`,
                  `board_gh`): KHMS is generic and never depends on one project's operations code
  non-english     a letter with a diacritic in shipped code or docs: everything this repository ships is
                  English. A file that must carry such text as DATA (a normalisation test, a Unicode
                  round-trip) says so with the line `boundary: allow-diacritics` near its top.

PRIVATE TERMS (--terms): a file OUTSIDE every repository, one `<class>\\t<literal|regex>\\t<pattern>` per
line. A hit is reported as path:line plus the CLASS, never the matched text: a gate that echoes the values
it guards moves them into logs and transcripts. The scan refuses to certify anything unless the same terms
light up at least MIN_CONTROL_CLASSES classes in --control, an explicit private corpus: a "0 hits" from a
broken scanner or an empty terms file looks exactly like a clean tree (positive control).

HISTORY (--history RANGE, e.g. origin/master..HEAD): every blob that the range would newly make reachable is
scanned, not only the final tree -- an intermediate commit that carried private text publishes it too.

Exit: 0 clean · 1 findings · 2 usage error or a failed positive control.
"""
import argparse
import os
import re
import subprocess
import sys

MIN_CONTROL_CLASSES = 3
ALLOW_DIACRITICS = "boundary: allow-diacritics"
# Fictional example card ids, allowed inside examples/ only (the private card-id class would match them).
ALLOWED_EXAMPLE_IDS = {f"K-{n:05d}" for n in range(2, 10)}

GENERIC = [
    ("home-path", re.compile(r"/home/[a-z_][a-z0-9_-]*/|/Users/[A-Za-z][A-Za-z0-9_-]*/")),
    ("secret", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"
                          r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("project-import", re.compile(r"^\s*(?:from|import)\s+(?:ops_paths|board_gh)\b|tools/ops/", re.M)),
    ("non-english", re.compile(r"[À-ÖØ-öø-ɏ]")),
]
# The scanner and its own tests name the forbidden things in order to find them.
SELF_EXEMPT = {"tools/khms_boundary.py", "tests/test_boundary.py"}
TEXT_EXT = {".py", ".sh", ".md", ".json", ".jsonl", ".txt", ".yml", ".yaml", ".toml", ".cfg", ".env", ""}


def is_text(path):
    return os.path.splitext(path)[1].lower() in TEXT_EXT


def generic_hits(rel, text):
    out = []
    if rel in SELF_EXEMPT:
        return out
    allow_dia = ALLOW_DIACRITICS in "\n".join(text.splitlines()[:40])
    for n, line in enumerate(text.splitlines(), 1):
        for cls, rx in GENERIC:
            if cls == "non-english" and allow_dia:
                continue
            if rx.search(line):
                out.append((rel, n, cls))
    return out


def load_terms(path):
    terms = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            ln = raw.rstrip("\n")
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) < 3:
                continue
            cls, kind, pat = parts[0].strip(), parts[1].strip(), "\t".join(parts[2:])
            try:
                terms.append((cls, re.compile(re.escape(pat) if kind == "literal" else pat)))
            except re.error as exc:
                print(f"TERMS FILE BROKEN: class {cls}: {exc}")
                sys.exit(2)
    return terms


def term_hits(rel, text, terms):
    out = []
    in_examples = rel.startswith("examples/")
    for n, line in enumerate(text.splitlines(), 1):
        for cls, rx in terms:
            for m in rx.finditer(line):
                if in_examples and m.group(0) in ALLOWED_EXAMPLE_IDS:
                    continue
                out.append((rel, n, cls))
                break
    return out


def tracked_files(repo):
    r = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"not a git checkout: {repo}", file=sys.stderr)
        sys.exit(2)
    return [p for p in r.stdout.split("\0") if p]


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def history_blobs(repo, rng):
    """[(path, blob sha)] for every blob reachable from the range's commits but not from its base."""
    r = subprocess.run(["git", "-C", repo, "rev-list", "--objects", *rng.split()],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"git rev-list failed for {rng}: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    out = []
    for line in r.stdout.splitlines():
        sha, _, path = line.partition(" ")
        if not path:
            continue
        t = subprocess.run(["git", "-C", repo, "cat-file", "-t", sha], capture_output=True, text=True)
        if t.stdout.strip() == "blob":
            out.append((path, sha))
    return out


def walk(d):
    for dirpath, dirnames, filenames in os.walk(d):
        dirnames[:] = [x for x in dirnames if x not in (".git", "__pycache__")]
        for f in filenames:
            yield os.path.join(dirpath, f)


def report(hits, label):
    for rel, n, cls in hits:
        print(f"{label}: {rel}:{n}: {cls}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--terms")
    ap.add_argument("--control")
    ap.add_argument("--history")
    ap.add_argument("--scan-files", nargs="+")
    a = ap.parse_args(argv)

    if a.scan_files:
        hits = []
        for f in a.scan_files:
            hits += generic_hits(f, read(f))
        report(hits, "HIT")
        return 1 if hits else 0

    terms = None
    if a.terms:
        if not a.control:
            print("--terms needs --control <private corpus> (positive control)", file=sys.stderr)
            return 2
        terms = load_terms(a.terms)
        lit = set()
        for path in walk(a.control):
            for _rel, _n, cls in term_hits(os.path.relpath(path, a.control), read(path), terms):
                lit.add(cls)
        print(f"positive control: {len(lit)} term class(es) found in the private corpus "
              f"(need >= {MIN_CONTROL_CLASSES})")
        if len(lit) < MIN_CONTROL_CLASSES:
            print("GATE BROKEN: the positive control did not fire -- the scanner, the terms file or the "
                  "corpus is wrong, and a clean result would mean nothing")
            return 2

    hits = []
    files = tracked_files(a.repo)
    for rel in files:
        if not is_text(rel):
            continue
        text = read(os.path.join(a.repo, rel))
        hits += generic_hits(rel, text)
        if terms:
            hits += term_hits(rel, text, terms)
    if a.history:
        for rel, sha in history_blobs(a.repo, a.history):
            if not is_text(rel):
                continue
            text = subprocess.run(["git", "-C", a.repo, "cat-file", "-p", sha],
                                  capture_output=True, text=True, errors="replace").stdout
            hh = generic_hits(rel, text) + (term_hits(rel, text, terms) if terms else [])
            hits += [(f"{rel}@{sha[:10]}", n, c) for rel, n, c in hh]
    hits = sorted(set(hits))
    report(hits, "HIT")
    print(f"boundary: {len(files)} tracked file(s){' + history ' + a.history if a.history else ''}, "
          f"{len(hits)} hit(s){' (private terms included)' if terms else ''}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
