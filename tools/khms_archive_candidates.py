#!/usr/bin/env python3
"""Archive candidates from the v3 know-how answers: which "not know-how" cards nothing cites. Lists, never moves.

WHY (#219, owner 2026-10-21). The base keeps know-how only -- "when it was like this, it did this". A card
whose substance is only a STATE (where something is, what a value is, what is deployed, what a file does,
progress) is answered `knowhow: no` by the v3 judge (card_validity.py PROMPT_VERSION v3, or the know-how-only
mode v3-knowhow). Such a card still stays when something live points at it by id: then removing it would
break a reference. This tool turns the v3 "no" rows into one table, card by card:

  keep (cited)   the id appears in (a) a configured git tree at a configured ref (docs, code, configs),
                 (b) a configured file or directory (CLAUDE.md files, role loaders ...), (c) the body of a live
                 card, or (d) a live card's frontmatter links -- the citing place is named; or the card is the
                 ONLY live successor of a superseded card (khms_archive's chain rule);
  archive        nothing above cites it. A citation from another card that is itself listed `archive` does not
                 count (both leave together); the table names such citers.

Which rows count: the LAST ledger row of a card among the v3 prompt versions, taken at the card's CURRENT
file hash (a card changed since its answer is skipped and counted as stale), for live cards (active,
challenged). A v3 "yes" after a "no" at the same hash wins (the last row decides).

Nothing is moved: the `archive` rows end in the khms_archive.py dry-run command for the coordinator.

Usage:
  khms_archive_candidates.py --out FILE [--brain DIR] [--ledger FILE]
      [--git REPO@REF ...] [--path FILE_OR_DIR ...] [--versions v3,v3-knowhow]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_archive as KA  # noqa: E402
import khms_paths as P  # noqa: E402

ID = re.compile(r"K-\d{5}")
LIVE = ("active", "challenged")
TEXT_SKIP = {".git", "__pycache__", "node_modules", ".pytest_cache"}


def v3_versions():
    import card_validity as CV
    return CV.V3_VERSIONS


def card_hash(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def candidates(live, ledger_rows, versions):
    """-> ({id: last v3 row}, stale count). Live cards whose last v3 row at the CURRENT hash says no."""
    last, stale = {}, set()
    hashes = {}
    for r in ledger_rows:
        cid = r.get("id")
        if r.get("prompt_version") not in versions or cid not in live:
            continue
        if cid not in hashes:
            hashes[cid] = card_hash(live[cid]["path"])
        if r.get("sha256") != hashes[cid]:
            stale.add(cid)
            continue
        last[cid] = r
    out = {cid: r for cid, r in last.items() if r.get("knowhow") == "no" and live[cid]["status"] in LIVE}
    return out, len(stale - set(last))


def git_citations(spec, wanted):
    """{id: ["<repo name>@<ref>:<path>:<line>"]} for every K-id of `wanted` found in REPO at REF."""
    repo, _, ref = spec.rpartition("@")
    if not repo:
        raise SystemExit(f"--git {spec!r}: give REPO@REF")
    cp = subprocess.run(["git", "-C", repo, "grep", "-n", "-o", "-I", "-E", "K-[0-9]{5}", ref, "--"],
                        capture_output=True, text=True)
    if cp.returncode not in (0, 1):
        raise SystemExit(f"git grep in {repo}@{ref} failed: {cp.stderr.strip()[-300:]}")
    name = os.path.basename(os.path.abspath(repo)) + "@" + ref
    out = {}
    for line in cp.stdout.splitlines():
        # <ref>:<path>:<line>:<match>
        rest = line[len(ref) + 1:] if line.startswith(ref + ":") else line
        path, _, tail = rest.rpartition(":")
        path, _, lineno = path.rpartition(":")
        if tail in wanted:
            out.setdefault(tail, []).append(f"{name}:{path}:{lineno}")
    return out


def file_citations(paths, wanted):
    """{id: ["<file>:<line>"]} over files and directory trees (text files only)."""
    out = {}

    def scan(fp):
        try:
            with open(fp, encoding="utf-8", errors="strict") as fh:
                for n, line in enumerate(fh, 1):
                    for x in set(ID.findall(line)):
                        if x in wanted:
                            out.setdefault(x, []).append(f"{fp}:{n}")
        except (UnicodeDecodeError, OSError):
            pass
    for p in paths:
        if os.path.isdir(p):
            for d, dirs, files in os.walk(p):
                dirs[:] = sorted(x for x in dirs if x not in TEXT_SKIP)
                for f in sorted(files):
                    scan(os.path.join(d, f))
        elif os.path.exists(p):
            scan(p)
        else:
            print(f"warning: --path {p} does not exist", file=sys.stderr)
    return out


def decide(live, cand, ext):
    """-> {id: (decision, place)}. ext = {id: [external citation places]}."""
    idx = KA.Index(live)
    citers = {}
    for x in cand:
        cs = [(y, f"links.{field}") for y, field in sorted(set(idx.inbound.get(x, []))) if y != x]
        cs += [(y, "body") for y in sorted(set(idx.mentions.get(x, []))) if y != x]
        citers[x] = cs
    keep = {}
    for x in sorted(cand):
        if ext.get(x):
            keep[x] = "; ".join(ext[x][:3]) + (f" (+{len(ext[x]) - 3} more)" if len(ext[x]) > 3 else "")
            continue
        refuse = idx.inspect(x, set(), allow_chain=False, keep_links=True)["refuse"]
        if refuse:
            keep[x] = refuse
    while True:                       # a citation counts unless its card leaves too
        more = {}
        for x in sorted(set(cand) - set(keep)):
            live_citers = [(y, how) for y, how in citers[x] if y not in cand or y in keep]
            if live_citers:
                more[x] = "; ".join(f"{y} {how}" for y, how in live_citers[:3]) + \
                    (f" (+{len(live_citers) - 3} more)" if len(live_citers) > 3 else "")
        if not more:
            break
        keep.update(more)
    out = {}
    for x in sorted(cand):
        if x in keep:
            out[x] = ("keep (cited)", keep[x])
        else:
            gone = sorted({y for y, _h in citers[x]})
            out[x] = ("archive", ("cited only by archive candidates: " + ", ".join(gone)) if gone else "")
    return out


def _cell(s):
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="the table file of this run (markdown)")
    ap.add_argument("--brain", default=P.soft("ROOT"))
    ap.add_argument("--ledger", help="default: <brain>/memory/ledger/validity.jsonl")
    ap.add_argument("--git", action="append", default=[], metavar="REPO@REF")
    ap.add_argument("--path", action="append", default=[], metavar="FILE_OR_DIR")
    ap.add_argument("--versions", default=None, help="ledger prompt versions that count (default: v3 ones)")
    a = ap.parse_args(argv)
    if not a.brain:
        ap.error("--brain (or a configured KHMS_ROOT) is required")
    import card_validity as CV
    versions = tuple(x.strip() for x in a.versions.split(",")) if a.versions else v3_versions()
    ledger = a.ledger or os.path.join(a.brain, "memory", "ledger", "validity.jsonl")
    live = KA.load_live(a.brain)
    cand, stale = candidates(live, CV.read_ledger(ledger), versions)
    ext = {}
    refs = []
    for spec in a.git:
        repo, _, ref = spec.rpartition("@")
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "--short", ref], capture_output=True,
                             text=True).stdout.strip()
        refs.append(f"{spec} ({sha or '?'})")
        for x, places in git_citations(spec, set(cand)).items():
            ext.setdefault(x, []).extend(places)
    for x, places in file_citations(a.path, set(cand)).items():
        ext.setdefault(x, []).extend(places)
    res = decide(live, cand, ext)
    n_keep = sum(1 for d, _ in res.values() if d != "archive")
    arch = [x for x, (d, _) in res.items() if d == "archive"]
    lines = ["# Archive candidates from the v3 know-how answers -- NOTHING moved", "",
             f"Run {datetime.datetime.now().isoformat(timespec='seconds')}; ledger {ledger}; versions "
             f"{', '.join(versions)}; live cards {len(live)}.",
             f"Searched: git {'; '.join(refs) or '(none)'}; paths {'; '.join(a.path) or '(none)'}; live card "
             f"bodies and links.",
             f"CANDIDATES {len(cand)} keep={n_keep} archive={len(arch)} stale-rows-skipped={stale}", "",
             "| card | decision | cited from / why | knowhow_reason | answered |", "|---|---|---|---|---|"]
    for x, (d, place) in res.items():
        r = cand[x]
        lines.append(f"| {x} | {d} | {_cell(place)} | {_cell((r.get('knowhow_reason') or '')[:200])} | "
                     f"{r.get('prompt_version')} {r.get('date')} |")
    lines += ["", "## Command for the archive rows (dry run; the coordinator decides)", ""]
    if arch:
        lines += ["```sh", "tools/khms_archive.py --card " + " ".join(arch) +
                  " --reason 'state-only (v3 know-how: no; nothing cites it)' --by coordinator", "```"]
    else:
        lines.append("(none)")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"CANDIDATES {len(cand)} keep={n_keep} archive={len(arch)} stale-rows-skipped={stale} -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
