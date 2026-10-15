#!/usr/bin/env python3
"""One-off repair of card `source:` values cut at " #" by the old inbox converter (#244). DRY RUN unless --apply.

    source_repair.py [--apply] [--brain DIR]

Until #239 `convert_inbox2.py` read a proposal's plain meta line `source: journal/D.md:585 (agent #143)` as
YAML, and a plain YAML scalar ends where ` #` starts a comment: the approved card got
`source: journal/D.md:585 (agent`. The text of the card is whole; only that field lost its tail. The
proposal the card came from still holds the whole value in `memory/inbox/`.

For every card in memory/know/ whose `source:` line is a PLAIN (unquoted) one-line scalar, the original is
looked up in the raw proposal units of memory/inbox/*.md (converted `.approve.md` files, triage files and
reports are not proposals and are not read). A proposal is read with the converter's OWN parser
(`CARD_RE`, `split_body`, `merge_legacy_meta` -- the fixed reading, which keeps a `#`), so the original is
exactly the value today's converter would have written. An original ORIG matches a card value CUT when
  * CUT is a strict prefix of ORIG and the rest of ORIG starts with whitespace and `#` (the YAML comment
    cut), and
  * the proposal's title is the card's title -- or, when no proposal has the card's title (a reviewer edited
    it) and the card's value looks cut (more `(` than `)`), the proposal's `date:` is the card's `date:`.
Exactly ONE distinct original repairs the card; two or more, or none for a cut-looking card, is a SKIPPED
line with the reason, and the card is not touched. A card whose value is whole is not mentioned.

The repair rewrites ONLY the `source:` line, as `convert_inbox2._yaml_line` writes it (the YAML writer's
quoting since #239), and is checked before it is written: the new frontmatter must read back as the old one
with `source` = ORIG and nothing else changed, else the card is REFUSED. A repaired card's line is quoted,
so a second run finds nothing to do.

With --apply every card is planned again and written while holding the `cards` lock (the lock apply_triage.py
and approve_inbox.py hold), one JSON line per card appended to memory/ledger/repair-244.jsonl (old and new
value, the proposal file:line, sha256 of the card before and after). On the selected base build_views.py runs
afterwards. No other file is written; no inbox file, no other card field.

Output: one line per card (WOULD-REPAIR / REPAIRED / SKIPPED / REFUSED), then the counts
`WOULD-REPAIR N` or `REPAIRED N`, `SKIPPED k`, `REFUSED r`. Exit 0 unless a card was REFUSED (1); 2 on usage.
"""
import argparse
import collections
import datetime
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import apply_triage as AT  # noqa: E402  (the card reader and frontmatter split -- one parser, not two)
import convert_inbox2 as CV  # noqa: E402  (the proposal reader and the source quoting -- likewise)
import khms_lock as L  # noqa: E402
import khms_paths as P  # noqa: E402

DECISION = "#244"
LOG_NAME = "repair-244.jsonl"
# Files in the inbox that are not raw proposal units: the converter's output (holds the CUT value), its
# report, triage units and their reports, quote checks.
NOT_A_PROPOSAL = re.compile(r"(approve|triage|quotecheck|report|applied|preview)")
SOURCE_LINE = re.compile(r"^source:[ \t]*(.*)$", re.M)
CUT_REST = re.compile(r"\s+#")


def proposals(brain):
    """-> list of dicts {title, source, date, file, line} for every proposal that names a source."""
    out = []
    for path in sorted(glob.glob(os.path.join(brain, "memory", "inbox", "*.md"))):
        if NOT_A_PROPOSAL.search(os.path.basename(path)):
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        rel = os.path.relpath(path, brain)
        for m in CV.CARD_RE.finditer(CV.split_trailers(text)):
            meta_lines, _prose, _quotes = CV.split_body(m.group(3))
            if not meta_lines:
                continue
            meta = CV.merge_legacy_meta(meta_lines, {"links": {}})
            src = meta.get("source")
            if not isinstance(src, str):
                continue
            sm = re.search(r"^[ \t]*source:", m.group(3), re.M)
            at = m.start(3) + sm.start() if sm else m.start()
            out.append({"title": m.group(2).strip(), "source": src, "date": str(meta.get("date") or ""),
                        "file": rel, "line": text.count("\n", 0, at) + 1})
    return out


def is_cut_of(cut, orig):
    return len(orig) > len(cut) and orig.startswith(cut) and bool(CUT_REST.match(orig[len(cut):]))


def plan_card(c, by_title, all_props):
    """(verdict, detail) for one loaded card; verdict is None (nothing to do), REPAIR, SKIPPED or REFUSED."""
    lines = SOURCE_LINE.findall(c["front"])
    cut = c["meta"].get("source")
    if len(lines) != 1 or not isinstance(cut, str):
        return None, None
    raw = lines[0].strip()
    if not raw or raw[0] in "'\"[{|>&*!" or raw != cut:
        return None, None                      # quoted, multi-line or otherwise not a plain cut scalar
    title = c["body"].strip().split("\n", 1)[0].strip()
    looks_cut = cut.count("(") > cut.count(")")
    named = by_title.get(title, [])
    hits = [p for p in named if is_cut_of(cut, p["source"])]
    how = "by title"
    if not hits and not named and looks_cut:
        date = str(c["meta"].get("date") or "")
        hits = [p for p in all_props if p["date"] == date and is_cut_of(cut, p["source"])]
        how = "by source+date (title differs)"
    origs = sorted({p["source"] for p in hits})
    if len(origs) > 1:
        return "SKIPPED", f"{len(origs)} different originals in the inbox: " + " | ".join(origs)
    if not origs:
        if not looks_cut:
            return None, None
        same = [p for p in named if p["source"] == cut]
        why = ("the proposal already holds the cut value " + f"({same[0]['file']}:{same[0]['line']})" if same
               else "no original in the inbox")
        return "SKIPPED", f"{why} -- {cut!r}"
    orig = origs[0]
    p = sorted(hits, key=lambda h: (h["file"], h["line"]))[0]
    new_line = CV._yaml_line("source", orig)
    new_front = SOURCE_LINE.sub(lambda _m: new_line, c["front"], count=1)
    want = dict(c["meta"], source=orig)
    try:
        got = yaml.safe_load(new_front)
    except yaml.YAMLError as e:
        return "REFUSED", f"the new line breaks the frontmatter: {e}"
    if got != want:
        return "REFUSED", "the new line would change more than `source`"
    after = c["text"].replace("---\n" + c["front"] + "\n---\n", "---\n" + new_front + "\n---\n", 1)
    return "REPAIR", {"orig": orig, "how": how, "file": p["file"], "line": p["line"], "after": after,
                      "new_line": new_line}


def plan(brain):
    props = proposals(brain)
    by_title = collections.defaultdict(list)
    for p in props:
        by_title[p["title"]].append(p)
    rows = []
    for path in sorted(glob.glob(os.path.join(brain, "memory", "know", "K-*.md"))):
        cid = os.path.basename(path)[:-3]
        try:
            c = AT.load(brain, cid)
        except (ValueError, yaml.YAMLError):
            continue                            # no frontmatter this tool can read: not its business
        if c is None or not isinstance(c["meta"], dict):
            continue
        verdict, detail = plan_card(c, by_title, props)
        if verdict:
            rows.append((cid, c, verdict, detail))
    return rows


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brain", default=AT.DEFAULT_BRAIN, help="the base (default: the resolver's KHMS_ROOT)")
    ap.add_argument("--apply", action="store_true", help="write the cards (default: dry run, writes nothing)")
    a = ap.parse_args(argv)
    if not a.brain:
        print("REFUSED: no base selected (KHMS_ROOT) and no --brain", file=sys.stderr)
        return 2
    brain = a.brain
    log = os.path.join(brain, "memory", "ledger", LOG_NAME)
    if not a.apply:
        return report(plan(brain), False, brain, log)
    selected = P.same_path(brain, AT.DEFAULT_BRAIN) if AT.DEFAULT_BRAIN else False
    if selected:
        P.require_writable("repair card source fields")
    P.require_may_write([os.path.join(brain, "memory", "know"), log], "repair card source fields")
    with L.lock("cards", brain):     # plan and write in one critical section with apply_triage / approve_inbox
        rows = plan(brain)
        rc = report(rows, True, brain, log)
    if selected and any(v == "REPAIR" for _c, _x, v, _d in rows):
        subprocess.run([sys.executable, os.path.join(P.CODE, "build_views.py")], check=True)
    return rc


def report(rows, apply, brain, log):
    n = collections.Counter()
    tool = os.path.abspath(__file__)
    tool_sha = hashlib.sha256(open(tool, "rb").read()).hexdigest()
    if apply and any(v == "REPAIR" for _c, _x, v, _d in rows):
        P.require_may_write([c["path"] for _cid, c, v, _d in rows if v == "REPAIR"], "repair card source fields")
        L.dirty(brain)
    for cid, c, verdict, d in rows:
        n[verdict] += 1
        if verdict != "REPAIR":
            print(f"{verdict} {cid} {d}")
            continue
        head = "REPAIRED" if apply else "WOULD-REPAIR"
        print(f"{head} {cid} {d['file']}:{d['line']} {d['how']}: {c['meta']['source']!r} -> {d['orig']!r}")
        if apply:
            mode = os.stat(c["path"]).st_mode & 0o7777
            L.atomic_text(c["path"], d["after"])
            os.chmod(c["path"], mode)          # the shared writer creates 0600; the card keeps its own mode
            entry = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), "card": cid,
                     "decision": DECISION, "unit": d["file"], "line": d["line"], "match": d["how"],
                     "old_source": c["meta"]["source"], "new_source": d["orig"], "new_line": d["new_line"],
                     "before_sha256": sha(c["text"]), "after_sha256": sha(d["after"]),
                     "tool": "tools/source_repair.py", "tool_sha256": tool_sha}
            os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"{'REPAIRED' if apply else 'WOULD-REPAIR'} {n['REPAIR']}")
    print(f"SKIPPED {n['SKIPPED']}")
    print(f"REFUSED {n['REFUSED']}")
    if not apply:
        print("dry run -- nothing written; repeat with --apply to write the lines above")
    return 1 if n["REFUSED"] else 0


if __name__ == "__main__":
    sys.exit(main())
