#!/usr/bin/env python3
"""Move cards that are no longer worth keeping OUT of the base, into memory/archive/know/ — no tombstone.

WHY (#219, owner 2026-10-15). Two classes of card leave the working base whole:
  * a card that only describes a state, fix or setup that no longer exists and carries no lesson
    ("state-only"): the owner wants it thrown out, moved to the archive without a tombstone;
  * a superseded card: its successor carries what still holds, and the old card moves to the archive.
A card with a lesson is superseded by a new card instead, and a refuted card stays (its value is the
warning). This tool does the move; the judgement which card belongs to which class is the caller's.

WHAT IT DOES, per card:
  1. every link a LIVE card (memory/know/) holds to it in supports / derived_from / contradicts /
     refuted_by is taken back through apply_triage's `unlink` path (the one frontmatter writer for link
     removal, #242; the contradicts mirror half goes with it). A `supersedes` edge INTO the card stays: it
     is bound to the successor's own SUPERSEDES body line, and an edge into the archive is valid history
     for build_views and khms_lint (both count the archive as known). A `superseded_by` back-edge on a
     live predecessor is the chain case below;
  2. moves memory/know/K-x.md to memory/archive/know/K-x.md;
  3. appends one line to memory/ledger/archive.jsonl: id, date, by, reason, the links taken back and
     the edges left in place.
Retrieval (khms_search.load_cards: recall, the hook, the dense index) does not read the archive, and the
mark ledger keeps a mark CARDED when the card that covered it is archived (the know-how decision for that
mark was taken; the archive line is its reason).

REFUSED, card by card (the rest of the run goes on; exit 2 when anything was refused):
  * SUPERSEDE CHAIN: the card is the only live successor of a live superseded card — archiving it would
    leave the predecessor pointing at nothing current. Allowed only with --allow-chain AND the predecessor
    named in the same run (the caller vouches that it goes too); otherwise both stay and it is reported;
  * an unlink the writer refuses (the last refuted_by of a refuted card, the last derived_from of a
    derived card), or a card that is not in memory/know/.
A card already in the archive is a no-op ("already archived"), so a re-run changes nothing.

--all-superseded selects every `status: superseded` card whose successor chain (cards that DECLARE
`supersedes: K-x`; a bare `superseded_by` whose target declares nothing does not count) reaches a live
(active|challenged) card; a card with no successor edge, or whose chain ends only in refuted / missing
cards, is REPORTED and left in place. Chains inside the selection are allowed (both ends go). Each card's
reason gets `superseded-by K-y (live K-z)`.

--keep-links leaves every live link to the card in place, as history into the archive, instead of
unlinking it — the superseded class: a `derived_from` / `supports` / `refuted_by` into a superseded card is
provenance that stays true, and taking it back would strip it from ~250 live cards and is refused by the
writer wherever it is a derived card's last derived_from or a refuted card's last refuted_by (measured on
the reference base, 2026-10-15). build_views and khms_lint count the archive as known, so such an edge is
not dangling.

Usage:
  khms_archive.py --card K-x [K-y ...] --reason "<one line>" --by owner|coordinator   (dry run)
  khms_archive.py --all-superseded --reason "<one line>" --by owner|coordinator --apply
  options: --allow-chain, --keep-links, --backup-dir DIR (copy every file the run will change or move, before the
           first write), --brain DIR (default: the resolver's KHMS_ROOT)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_files as glob  # noqa: E402  (canonical card files only)
import khms_paths as P  # noqa: E402

LIVE = ("active", "challenged")
LEDGER_NAME = "archive.jsonl"


def ledger_path(brain):
    return os.path.join(brain, "memory", "ledger", LEDGER_NAME)


def read_ledger(brain):
    """{id: the LAST archive.jsonl record of that id}. Missing or broken lines are skipped."""
    out = {}
    try:
        with open(ledger_path(brain), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    out[rec["id"]] = rec
    except OSError:
        pass
    return out


# ------------------------------------------------------------------------------- reading the base

def _ids(val):
    import khms_lint as KL
    return [x for x in KL.split_link_ids(val) if x not in ("null", "~")]


def load_live(brain):
    """{id: {'path','meta','links': {field: [ids]},'body'}} of every card in memory/know/."""
    import yaml
    out = {}
    for path in sorted(glob.glob(os.path.join(brain, "memory", "know", "K-*.md"))):
        text = open(path, encoding="utf-8").read()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            continue
        meta = yaml.safe_load(m.group(1)) or {}
        cid = os.path.basename(path)[:-3]
        links = {k: _ids(v) for k, v in (meta.get("links") or {}).items()}
        out[cid] = {"path": path, "meta": meta, "links": links, "body": m.group(2),
                    "status": str(meta.get("status", "active"))}
    return out


class Index:
    """The base's edges, read once: who links to whom, who mentions whom, who replaces whom.
    (Rescanning every card per candidate made --all-superseded quadratic: > 10 min on ~3k cards.)"""

    def __init__(self, live):
        import apply_triage as AT
        self.live = live
        self.inbound, self.mentions, self.succ = {}, {}, {}
        for y, c in live.items():
            for field, ids in c["links"].items():
                for x in ids:
                    self.inbound.setdefault(x, []).append((y, field))
                    if field == "supersedes":
                        self.succ.setdefault(x, []).append(y)
            for x in set(re.findall(r"K-\d{5}", c["body"])):
                self.mentions.setdefault(x, []).append(y)
        # A successor is a card that DECLARES `supersedes: K-x` (the edge bound to its SUPERSEDES body
        # line). A bare `superseded_by` back-pointer whose target declares nothing is not one: such a card
        # is reported, never archived. For the chain guard both directions count (conservative).
        self.pointer = {}
        for y, c in live.items():
            for z in c["links"].get("superseded_by", []):
                self.pointer.setdefault(y, []).append(z)
        self.preds = {}
        for y in set(self.succ) | set(self.pointer):
            if live.get(y, {}).get("status") == "superseded":
                for z in set(self.succ.get(y, [])) | set(self.pointer.get(y, [])):
                    self.preds.setdefault(z, []).append(y)
        self.unlink_fields = AT.UNLINK_FIELDS

    def successors(self, cid):
        """Cards that replace `cid`: the cards declaring `supersedes: cid`."""
        return list(self.succ.get(cid, []))

    def chain_end(self, cid):
        """-> (live end id or None, why). Follows successors through superseded cards."""
        seen, todo, dead = {cid}, self.successors(cid), []
        if not todo:
            ptr = self.pointer.get(cid)
            return None, ("no successor edge" + (f" (only superseded_by {', '.join(ptr)}, which does not "
                                                  f"declare supersedes: {cid})" if ptr else ""))
        while todo:
            z = todo.pop(0)
            if z in seen:
                continue
            seen.add(z)
            c = self.live.get(z)
            if not c:
                dead.append(f"{z} not in memory/know/")
            elif c["status"] in LIVE:
                return z, ""
            elif c["status"] == "superseded":
                todo += self.successors(z)
            else:
                dead.append(f"{z} is {c['status']}")
        return None, "successor chain reaches no live card (" + "; ".join(dead or ["cycle"]) + ")"

    def inspect(self, x, cand, allow_chain, keep_links=False):
        """What archiving `x` touches, given the set `cand` archived in the same run.
        -> {'unlink': [(card, field)], 'kept': [(card, field)], 'mentions': [card], 'refuse': str|None}"""
        out = {"unlink": [], "kept": [], "mentions": [], "refuse": None}
        for y, field in sorted(set(self.inbound.get(x, []))):
            if y == x or y in cand:
                continue
            (out["unlink"] if field in self.unlink_fields and not keep_links else out["kept"]).append((y, field))
        out["mentions"] = sorted(y for y in set(self.mentions.get(x, [])) if y != x and y not in cand)
        # supersede chain: a live superseded card whose successors would all be gone with x
        for y in sorted(set(self.preds.get(x, []))):
            if y == x:
                continue
            others = [z for z in set(self.successors(y)) | set(self.pointer.get(y, []))
                      if z != x and z in self.live and z not in cand]
            if others or (y in cand and allow_chain):
                continue
            out["refuse"] = (f"supersede chain: {y} is superseded and {x} is its only live successor; "
                             + ("pass --allow-chain to archive both" if y in cand else
                                f"{y} is not named in this run, so both stay (the caller decides)"))
            break
        return out


# ------------------------------------------------------------------------------- planning

def unlink_rows(pairs, x, reason):
    """apply_triage `unlink` rows (the shape its plan() takes) for (card, field) pairs that hold `x`."""
    return [{"n": 0, "loser": y, "winner": x, "action": "UNLINK", "_class": "unlink", "_field": f,
             "_target": x, "_reason": reason} for y, f in pairs]


def decide(brain, idx, wanted, allow_chain, reason_of, keep_links=False):
    """-> (accepted {x: inspect-result}, refused {x: why}, edits of the final unlink plan).
    Refusing one card can change what another touches (a link from it, a chain through it), so the
    checks repeat until nothing more is refused; the final plan is ONE apply_triage.plan over all rows."""
    import apply_triage as AT
    cand, refused, alone = set(wanted), {}, {}
    while True:
        info = {x: idx.inspect(x, cand, allow_chain, keep_links) for x in sorted(cand)}
        bad = {x: r["refuse"] for x, r in info.items() if r["refuse"]}
        for x in sorted(cand - set(bad)):
            rows = unlink_rows(info[x]["unlink"], x, reason_of(x))
            key = tuple((r["loser"], r["_field"]) for r in rows)
            if alone.get(x, (None,))[0] != key:          # the writer's own guards, card by card
                try:
                    for i, r in enumerate(rows):
                        r["n"] = i + 1
                    AT.plan(rows, brain)
                    alone[x] = (key, None)
                except AT.Refusal as e:
                    alone[x] = (key, re.sub(r"^ROW [0-9]+: ", "", str(e)))
            if alone[x][1]:
                bad[x] = f"unlink refused by apply_triage: {alone[x][1]}"
        if not bad:
            rows = [r for x in sorted(cand) for r in unlink_rows(info[x]["unlink"], x, reason_of(x))]
            for i, r in enumerate(rows):
                r["n"] = i + 1
            try:
                return {x: info[x] for x in sorted(cand)}, refused, AT.plan(rows, brain)
            except AT.Refusal as e:                      # only the rows together fail: refuse that row's card
                n = int(re.match(r"ROW ([0-9]+):", str(e)).group(1))
                x = rows[n - 1]["winner"]
                bad = {x: f"unlink refused by apply_triage together with the other cards: "
                          f"{re.sub(r'^ROW [0-9]+: ', '', str(e))}"}
        refused.update(bad)
        cand -= set(bad)


# ------------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument("--card", nargs="+", action="extend", metavar="K-x")
    sel.add_argument("--all-superseded", action="store_true")
    ap.add_argument("--reason", required=True, help="one line, recorded per card")
    ap.add_argument("--by", required=True, choices=("owner", "coordinator"))
    ap.add_argument("--apply", action="store_true", help="write; the default is a dry run")
    ap.add_argument("--allow-chain", action="store_true")
    ap.add_argument("--keep-links", action="store_true",
                    help="leave every live link to the card in place, as history into the archive (the "
                         "superseded class: a derived_from/supports into a superseded card is provenance)")
    ap.add_argument("--backup-dir", help="copy every file this run changes or moves here before writing")
    ap.add_argument("--brain", default=P.soft("ROOT"))
    a = ap.parse_args(argv)
    if not a.brain:
        print("REFUSED: no base selected (KHMS_ROOT) and no --brain", file=sys.stderr)
        return 2
    if "\n" in a.reason or not a.reason.strip():
        print("REFUSED: --reason is one non-empty line", file=sys.stderr)
        return 2
    brain = a.brain
    selected = P.same_path(brain, P.soft("ROOT"))
    arch = os.path.join(brain, "memory", "archive", "know")
    if a.apply:
        if selected:
            P.require_writable("archive cards")
        P.require_may_write([os.path.join(brain, "memory", "know"), arch, ledger_path(brain)], "archive cards")
    import khms_lock as L
    with L.lock("cards", brain):
        rc = run_locked(a, brain, arch)
    if a.apply and selected and rc in (0, 2) and getattr(a, "_wrote", False):
        subprocess.run([sys.executable, os.path.join(P.CODE, "build_views.py")], check=True)
    return rc


def run_locked(a, brain, arch):
    import khms_health
    import khms_lock as L
    import apply_triage as AT
    khms_health.require_healthy(brain)
    live = load_live(brain)
    idx = Index(live)
    notes, refused, already, reasons = [], {}, [], {}
    if a.all_superseded:
        wanted = []
        allow_chain = True                   # chains inside the selection go together
        for x, c in sorted(live.items()):
            if c["status"] != "superseded":
                continue
            end, why = idx.chain_end(x)
            if not end:
                notes.append(f"REPORTED {x}: {why} — left in place")
                continue
            direct = ", ".join(idx.successors(x))
            reasons[x] = f"{a.reason}; superseded-by {direct}" + (f" (live {end})" if end not in direct else "")
            wanted.append(x)
    else:
        allow_chain = a.allow_chain
        wanted = []
        for x in dict.fromkeys(a.card):
            if not re.fullmatch(r"K-\d{5}", x):
                refused[x] = "not a card id"
            elif x in live:
                if os.path.exists(os.path.join(arch, f"{x}.md")):
                    refused[x] = "a file of that id is already in the archive — resolve by hand"
                else:
                    wanted.append(x)
            elif os.path.exists(os.path.join(arch, f"{x}.md")):
                already.append(x)
            else:
                refused[x] = "no such card in memory/know/ or the archive"

    def reason_of(x):
        return reasons.get(x, a.reason)

    accepted, more, edits = decide(brain, idx, wanted, allow_chain, reason_of, a.keep_links)
    refused.update(more)

    mode = "APPLY" if a.apply else "DRY RUN"
    print(f"# khms_archive — {mode} — {len(accepted)} to archive, {len(refused)} refused, "
          f"{len(already)} already archived, {len(notes)} reported")
    for x, r in accepted.items():
        print(f"ARCHIVE {x} ({live[x]['status']}) — {reason_of(x)}")
        for y, f in r["unlink"]:
            print(f"  unlink {y} links.{f} -= {x}")
        for y, f in r["kept"]:
            print(f"  keep   {y} links.{f} {x} (history into the archive)")
        if r["mentions"]:
            print(f"  text   mentioned in the body of {', '.join(r['mentions'])} (bodies are immutable)")
        marks = live[x]["meta"].get("marks") or []
        if marks:
            print(f"  marks  {', '.join(map(str, marks))} stay CARDED by {x} (archived)")
    for x in already:
        print(f"SKIP {x}: already archived")
    for x, why in sorted(refused.items()):
        print(f"REFUSED {x}: {why}")
    for n in notes:
        print(n)

    if not a.apply or not accepted:
        return 2 if refused else 0

    # every file this run writes or moves, checked and (optionally) copied before the first write
    changed = [(cid, c) for cid, c in sorted(edits.items()) if c["front"] != c["front0"]]
    outputs = [c["path"] for _, c in changed]
    for x in accepted:
        outputs += [live[x]["path"], os.path.join(arch, f"{x}.md")]
    P.require_may_write(outputs + [ledger_path(brain)], "archive cards")
    for cid, c in changed:
        after = f"---\n{c['front']}\n---\n{c['body']}"
        if hashlib.sha256(AT.split_card(after)[1].encode()).hexdigest() != \
                hashlib.sha256(c["body"].encode()).hexdigest():
            print(f"REFUSED: the body of {cid} would change — nothing written", file=sys.stderr)
            return 3
    if a.backup_dir:
        for src in sorted({c["path"] for _, c in changed} | {live[x]["path"] for x in accepted}):
            dst = os.path.join(a.backup_dir, os.path.relpath(src, brain))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        if os.path.exists(ledger_path(brain)):
            dst = os.path.join(a.backup_dir, os.path.relpath(ledger_path(brain), brain))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(ledger_path(brain), dst)
        print(f"backup -> {a.backup_dir}")

    L.dirty(brain)
    for cid, c in changed:
        L.atomic_text(c["path"], f"---\n{c['front']}\n---\n{c['body']}")
    now = datetime.datetime.now().astimezone()
    os.makedirs(arch, exist_ok=True)
    os.makedirs(os.path.dirname(ledger_path(brain)), exist_ok=True)
    with open(ledger_path(brain), "a", encoding="utf-8") as fh:
        for x, r in accepted.items():
            shutil.move(live[x]["path"], os.path.join(arch, f"{x}.md"))
            rec = {"id": x, "date": now.date().isoformat(), "ts": now.isoformat(timespec="seconds"),
                   "by": a.by, "reason": reason_of(x), "status": live[x]["status"],
                   "unlinked": [{"card": y, "field": f} for y, f in r["unlink"]],
                   "kept": [{"card": y, "field": f} for y, f in r["kept"]],
                   "mentioned_in": r["mentions"],
                   "marks": [str(m) for m in (live[x]["meta"].get("marks") or [])]}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    a._wrote = True
    print(f"archived: {', '.join(accepted)}")
    return 2 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
