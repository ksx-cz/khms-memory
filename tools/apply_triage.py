#!/usr/bin/env python3
"""Apply a reviewed KHMS triage file to card FRONTMATTER — and to nothing else.

WHY THIS EXISTS (measured 2026-09-03). The triage stage proposes; a human decides; and until
today the decision was executed BY HAND, with a throwaway helper written in the scratchpad
each morning. That is the shape K-91631 already names for the card converter, and it has the
same two consequences: the edit is unverified (nothing checks that only `status` and links
moved) and the decision is unrecorded (nothing but prose says which row was applied and why).
The 2026-09-03 review also showed the sharper failure mode: both `RETIRE_WHOLE` rows in that
file quoted text that is not in the card they name, so the two most destructive proposals
were the two least defensible — and a hand edit has nothing that could have refused them.

WHAT IT MAY CHANGE. `status:` and the link lists, in `memory/know/K-*.md`. Nothing else,
ever: the body is checksummed before and after and the run aborts if a single byte moved
(K-90024 P2 — approved cards are immutable, corrections are new cards).

THE LINK VOCABULARY IS THE SPEC'S, NOT AN INVENTED ONE (K-90024 §4.10): `contradicts`
(symmetric), `refuted_by` (filled when status→refuted), `supersedes` (needs a body line, so
this tool never writes it), `derived_from`, `supports`. There is no `challenged_by` and no
`refutes` in this base — grep of all 2295 cards finds zero — so a challenge is recorded as
`status: challenged` on the loser plus the symmetric `contradicts` edge on both cards.

  refute      loser: status refuted, refuted_by += winner   · winner: contradicts += loser
  challenge   loser: status challenged, contradicts += winner · winner: contradicts += loser
  contradicts both: contradicts += the other, no status change
  retire      refute + move the loser to memory/archive/know/ (only with --archive)

Usage:
  apply_triage.py memory/inbox/2026-09-03-triage.md --dry-run
  apply_triage.py memory/inbox/2026-09-03-triage.md --apply [--archive]

The REVIEW VERDICT section is the canonical record of the decision, one line per row:

  ROW 12: APPLY refute — the loser's own claim, the winner is measured
  ROW 13: REJECT wrong subject entirely

A row with no verdict line is REJECTED: silence is not approval.
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import os
import re
import shutil
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_contradict as kc        # noqa: E402  (quote_coverage — one definition, not two)
import khms_triage_report as R      # noqa: E402  (role coherence — likewise)

import khms_paths as P  # noqa: E402

# The base: the resolver's root; --brain names another one (tests use a throwaway base).
# Card ids in comments (K-9xxxx) are illustrative stand-ins for the reference deployment's cards
# the measurements below were made on; they are not ids of any shipped base.
DEFAULT_BRAIN = P.soft("ROOT")


def snapshot_first(brain, tag):
    """Take a rollback point before the first frontmatter write (audit 2026-09-02 §6 A1).

    Same reasoning as approve_inbox.snapshot_first: there is no git under the brain, so the
    undo of a wrong triage is a copy out of a snapshot, and it has to be taken before the
    write, not after the surprise. Only for the selected base (`--brain <tmp>` in the tests is a
    throwaway and gets nothing) and never when KHMS_NO_SNAPSHOT=1. A failed snapshot warns
    loudly and does not block the apply — refusing to write would be the worse failure.
    """
    if os.environ.get("KHMS_NO_SNAPSHOT") == "1" or not P.same_path(brain, DEFAULT_BRAIN):
        return
    script = os.path.join(P.CODE, "khms_snapshot.sh")
    if not os.path.exists(script):
        print(f"WARN: {script} is missing — writing WITHOUT a rollback point", file=sys.stderr)
        return
    r = subprocess.run(["bash", script, "--tag", tag], capture_output=True,
                       text=True, timeout=900)
    if r.returncode == 0:
        print((r.stdout or "").strip().splitlines()[0], file=sys.stderr)
    else:
        print(f"WARN: snapshot rc={r.returncode} — NO rollback point for this run: "
              f"{(r.stderr or '').strip()[:200]}", file=sys.stderr)
STATUS_RANK = {"active": 0, "challenged": 1, "refuted": 2, "superseded": 3, "condensed": 3}
CLASS_FROM_ACTION = {"MARK_DEAD_END": "refute", "MARK_CONJECTURE": "challenge",
                     "RETIRE_WHOLE": "retire", "AMEND": None}
CLASSES = ("refute", "challenge", "contradicts", "retire")
# AMEND maps to None ON PURPOSE: "one false claim in an otherwise true card" is not a status or a
# link, it is an erratum — tools/khms_erratum.py corrects the claim in place and keeps the retracted
# wording in the card and in the tombstone ledger (KBFIX, issue #136, 2026-09-20). Until that tool
# existed this map made every AMEND row die in silence: 151 rows between 2026-09-04 and 2026-09-19,
# none applied (plans/kbfix-scope-2026-09-20.md). The hint is what a reviewer sees instead.
ERRATUM_HINT = (
    "AMEND is not applied by apply_triage: one false claim in an otherwise true card is corrected "
    "IN PLACE by the erratum tool. Answer the three questions first (checkable when written? source "
    "unchanged since? rest of the card stands? — plans/kbfix-erratum-design.md §1), then run: "
    "tools/khms_erratum.py declare --card {loser} --was \"{quote}\" --evidence \"<cmd + output>\" "
    "--by owner|coordinator  ->  tools/khms_erratum.py apply --card {loser} --ts TS-nnnnn "
    "--now \"<corrected wording>\". If only links are wanted, name refute|challenge|contradicts "
    "in the verdict line.")

ROW_RE = re.compile(
    r"^### (K-\d+) — action \*\*(\w+)\*\* \(contradicted by (K-\d+), severity ([\d?]+)\)$",
    re.M)
VERDICT_RE = re.compile(
    r"^ROW\s+(\d+)\s*:\s*(APPLY|REJECT)\b\s*(refute|challenge|contradicts|retire)?\s*"
    r"(?:[—:-]\s*)?(.*)$", re.M)
QUOTE_RE = re.compile(r'^- offending quote: "(.*)"\s*$', re.M)
WIN_QUOTE_RE = re.compile(r'^- winning quote \((K-\d+)\): "(.*)"\s*$', re.M)


# ------------------------------------------------------------------------------- parsing

def parse_rows(text):
    """Every `### K-xxxxx — action **ACTION** (contradicted by K-yyyyy, severity N)` block,
    numbered from 1 in file order. The number is the row's identity in the verdict section."""
    rows, marks = [], list(ROW_RE.finditer(text))
    for i, m in enumerate(marks):
        chunk = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        chunk = chunk.split("\n## ")[0]
        q = QUOTE_RE.search(chunk)
        w = WIN_QUOTE_RE.search(chunk)
        rows.append({"n": i + 1, "loser": m.group(1), "action": m.group(2),
                     "winner": m.group(3),
                     "severity": int(m.group(4)) if m.group(4).isdigit() else 0,
                     "quote": q.group(1) if q else "",
                     "winner_quote": w.group(2) if w else "",
                     "score": 0.0, "winner_evidence": "", "winner_date": ""})
    return rows


def parse_verdicts(text):
    """{row number: (APPLY|REJECT, class or None, reason)} from the REVIEW VERDICT section."""
    out = {}
    for m in VERDICT_RE.finditer(text):
        out[int(m.group(1))] = (m.group(2), m.group(3), m.group(4).strip())
    return out


# ------------------------------------------------------------------- frontmatter surgery

def split_card(text):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError("no frontmatter")
    return m.group(1), m.group(2)


def set_status(front, new):
    if not re.search(r"^status:\s*.+$", front, re.M):
        raise ValueError("no status: field")
    return re.sub(r"^status:\s*.+$", f"status: {new}", front, count=1, flags=re.M)


def add_link_id(front, key, cid):
    """Add `cid` to links.<key>, in whichever of the two frontmatter styles the card uses.
    Returns (front, changed). Only the one line that holds the list is rewritten."""
    meta = yaml.safe_load(front) or {}
    links = meta.get("links") or {}
    cur = links.get(key) or []
    if isinstance(cur, str):
        cur = [cur]
    if cid in cur:
        return front, False
    rendered = "[" + ", ".join(list(cur) + [cid]) + "]"
    inline = re.search(r"^links:\s*(\{.*\})\s*$", front, re.M)
    if inline:
        inner = inline.group(1)
        km = re.search(rf"(?<![\w-]){key}:\s*(?:\[[^\]]*\]|null|[^,}}]*)", inner)
        new_inner = (inner[:km.start()] + f"{key}: {rendered}" + inner[km.end():] if km
                     else inner.rstrip()[:-1].rstrip().rstrip(",") + f", {key}: {rendered}}}")
        return front[:inline.start(1)] + new_inner + front[inline.end(1):], True
    bm = re.search(r"^links:\s*$", front, re.M)
    if not bm:
        raise ValueError("no links: mapping")
    tail = front[bm.end():]
    km = re.search(rf"^(\s+){key}:\s*.*$", tail, re.M)
    if km:
        return (front[:bm.end()] + tail[:km.start()] + f"{km.group(1)}{key}: {rendered}"
                + tail[km.end():], True)
    sib = re.search(r"^(\s+)\w+:", tail, re.M)
    ind = sib.group(1) if sib else "  "
    return front[:bm.end()] + f"\n{ind}{key}: {rendered}" + tail, True


# ------------------------------------------------------------------------------ planning

def card_path(brain, cid):
    return os.path.join(brain, "memory", "know", f"{cid}.md")


def load(brain, cid):
    p = card_path(brain, cid)
    if not os.path.exists(p):
        return None
    text = open(p, encoding="utf-8").read()
    front, body = split_card(text)
    meta = yaml.safe_load(front) or {}
    return {"id": cid, "path": p, "text": text, "front": front, "body": body, "meta": meta}


def ops_for(cls, loser, winner):
    """(card, kind, argument) triples. `kind` is 'status' or a link key."""
    if cls in ("refute", "retire"):
        return [(loser, "status", "refuted"), (loser, "refuted_by", winner),
                (winner, "contradicts", loser)]
    if cls == "challenge":
        return [(loser, "status", "challenged"), (loser, "contradicts", winner),
                (winner, "contradicts", loser)]
    if cls == "contradicts":
        return [(loser, "contradicts", winner), (winner, "contradicts", loser)]
    raise ValueError(f"unknown class {cls}")


def decide(rows, verdicts, brain, cov_min=kc.QUOTE_COV_MIN, assume_apply=False):
    """Per-row outcome, before any file is touched. Returns (applied, outcomes).

    `assume_apply` is a PREVIEW for a triage file that has not been reviewed yet: rows with no
    verdict line are read as "apply what the row proposes", so a reviewer can see which rows
    would survive the guards before writing a verdict. It can never write (main refuses to
    combine it with --apply)."""
    outcomes, applied = {}, []
    for row in rows:
        n = row["n"]
        d = verdicts.get(n)
        if not d and assume_apply:
            d = ("APPLY", None, "assumed from the row's proposed action (preview only)")
        if not d:
            outcomes[n] = ("REJECT", "no verdict line in the REVIEW VERDICT section — "
                                     "silence is not approval")
            continue
        decision, cls, reason = d
        if decision == "REJECT":
            outcomes[n] = ("REJECT", reason or "rejected by the review")
            continue
        cls = cls or CLASS_FROM_ACTION.get(row["action"])
        if not cls:
            if row["action"] == "AMEND":
                outcomes[n] = ("SKIP", ERRATUM_HINT.format(
                    loser=row["loser"], quote=row.get("quote") or "<offending quote>"))
            else:
                outcomes[n] = ("SKIP", f"{row['action']} needs a human-written superseding card; "
                                       "name refute|challenge|contradicts to apply links only")
            continue
        lc, wc = load(brain, row["loser"]), load(brain, row["winner"])
        if not lc or not wc:
            outcomes[n] = ("SKIP", f"missing card file for "
                                   f"{row['loser'] if not lc else row['winner']}")
            continue
        # THE SAME GUARD THE DETECTOR NOW APPLIES, RE-RUN AT APPLY TIME. A triage file can be
        # older than the fix, and a mis-quoted row must never reach a card — least of all a
        # RETIRE_WHOLE one, which is how the two freshest measured corrections in the base
        # came within one hand edit of being archived on 2026-09-03.
        for who, quote, card in ((row["loser"], row["quote"], lc),
                                 (row["winner"], row["winner_quote"], wc)):
            cov = kc.quote_coverage(quote, kc._norm_text(card["body"]))
            if cov < cov_min:
                outcomes[n] = ("REJECT", f"quote-not-in-card: the quote attributed to {who} "
                                         f"scores {cov:.2f} against its body")
                break
        else:
            row["winner_evidence"] = wc["meta"].get("evidence", "")
            row["winner_date"] = str(wc["meta"].get("date", ""))
            row["score"] = float(row["severity"])
            row["_class"] = cls
            row["_reason"] = reason
            applied.append(row)
    # A card may not be winner in one applied row and loser in another (2026-09-03: four were).
    kept, conflicting = R.resolve_winner_loser_conflicts(applied)
    for c in conflicting:
        outcomes[c["row"]["n"]] = ("REJECT", "winner-loser conflict — " + c["reason"])
    for row in kept:
        outcomes[row["n"]] = ("APPLY", f"{row['_class']}: {row['_reason']}")
    return kept, outcomes


def plan(rows, brain):
    """{card id: {'path','before','after','ops'}} — frontmatter only."""
    edits = {}
    for row in rows:
        for cid, kind, arg in ops_for(row["_class"], row["loser"], row["winner"]):
            c = edits.get(cid)
            if not c:
                card = load(brain, cid)
                c = edits[cid] = {"path": card["path"], "body": card["body"],
                                  "front0": card["front"], "front": card["front"],
                                  "meta": card["meta"], "ops": [], "archive": False}
            if kind == "status":
                cur = str(c["meta"].get("status", "active"))
                if STATUS_RANK.get(arg, 0) > STATUS_RANK.get(cur, 0):
                    c["front"] = set_status(c["front"], arg)
                    c["ops"].append(f"status: {cur} -> {arg}")
                    c["meta"]["status"] = arg
                else:
                    c["ops"].append(f"status: {cur} kept ({arg} is not stronger)")
            else:
                new, changed = add_link_id(c["front"], kind, arg)
                c["front"] = new
                c["ops"].append(f"links.{kind} += {arg}" if changed
                                else f"links.{kind} already has {arg}")
        if row["_class"] == "retire":
            edits[row["loser"]]["archive"] = True
    return edits


def render_diff(cid, before, after):
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile=f"{cid} (frontmatter, before)",
                                        tofile=f"{cid} (frontmatter, after)"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("triage_file")
    ap.add_argument("--brain", default=DEFAULT_BRAIN,
                    help="the base (default: the resolver's KHMS_ROOT)")
    ap.add_argument("--apply", action="store_true", help="write; default is a dry run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--archive", action="store_true",
                    help="permit RETIRE_WHOLE rows to move the card to memory/archive/know/")
    ap.add_argument("--assume-apply", action="store_true",
                    help="preview: read rows with no verdict line as APPLY-as-proposed. "
                         "Never writes; use it to see what a triage file would do.")
    args = ap.parse_args()
    if not args.brain:
        print("REFUSED: no base selected (KHMS_ROOT) and no --brain", file=sys.stderr)
        sys.exit(2)
    write = args.apply and not args.dry_run
    # THE WRITE POLICY, BEFORE ANYTHING IS READ OR WRITTEN. `--brain` is compared with the selected base
    # by what it names (real path), never as text: `<base>/.` or a symlink to the base IS the selected base
    # and gets its policy, its snapshot and its shared review marker. Every output of the run is checked,
    # the preview's report included -- a dry run is a promise not to change the base, not an exemption
    # from the reader boundary.
    selected = P.same_path(args.brain, DEFAULT_BRAIN)
    rp = re.sub(r"\.md$", ".preview.md" if args.assume_apply else ".applied.md", args.triage_file)
    if rp == args.triage_file:
        rp = args.triage_file + (".preview.md" if args.assume_apply else ".applied.md")
    markers = (P.REVIEW_MARKERS if selected
               else os.path.join(args.brain, "state", "shared", "reviewed"))
    if write:
        if selected:
            P.require_writable("apply triage verdicts")
        P.require_may_write([os.path.join(args.brain, "memory", "know"),
                             os.path.join(args.brain, "memory", "archive", "know"), markers, rp],
                            "apply triage verdicts")
    else:
        P.require_may_write([rp], "write the apply preview")
    if args.assume_apply and write:
        print("REFUSED: --assume-apply is a preview and cannot write. Write the REVIEW "
              "VERDICT lines first.", file=sys.stderr)
        sys.exit(2)

    text = open(args.triage_file, encoding="utf-8").read()
    rows = parse_rows(text)
    verdicts = parse_verdicts(text)
    applied, outcomes = decide(rows, verdicts, args.brain,
                               assume_apply=args.assume_apply)
    edits = plan(applied, args.brain)

    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    report = [f"# apply_triage — {os.path.basename(args.triage_file)} "
              f"({'APPLIED' if write else 'DRY RUN'} {stamp})",
              "", f"rows: {len(rows)} · verdict lines: {len(verdicts)} · "
                  f"applied: {sum(1 for o in outcomes.values() if o[0] == 'APPLY')} · "
                  f"rejected: {sum(1 for o in outcomes.values() if o[0] == 'REJECT')} · "
                  f"skipped: {sum(1 for o in outcomes.values() if o[0] == 'SKIP')}", ""]
    for row in rows:
        state, why = outcomes.get(row["n"], ("REJECT", "no outcome computed"))
        report.append(f"ROW {row['n']}: {state} {row['loser']} ⇄ {row['winner']} "
                      f"({row['action']}) — {why}")
    report.append("")

    # A body that moves is a bug in this tool, not a decision of the review.
    changed_files, refuse = [], []
    for cid, c in sorted(edits.items()):
        after_text = f"---\n{c['front']}\n---\n{c['body']}"
        if hashlib.sha256(split_card(after_text)[1].encode()).hexdigest() != \
           hashlib.sha256(c["body"].encode()).hexdigest():
            refuse.append(cid)
        if c["front"] != c["front0"]:
            changed_files.append((cid, c, after_text))
    if refuse:
        print(f"REFUSED: the body of {', '.join(refuse)} would change — this tool only "
              f"edits frontmatter", file=sys.stderr)
        sys.exit(2)

    report += ["## Frontmatter changes", ""]
    for cid, c, _ in changed_files:
        report += [f"### {cid}", "", "```diff", render_diff(cid, c["front0"], c["front"]).rstrip(),
                   "```", "", "  " + "; ".join(c["ops"]), ""]
    noop = [cid for cid, c in sorted(edits.items()) if c["front"] == c["front0"]]
    if noop:
        report += ["", f"already in the target state (no-op): {', '.join(noop)}", ""]

    # THE PLAN'S OWN OUTPUTS, BY REAL PATH, BEFORE THE FIRST WRITE (review F5a). The guard above checked the
    # DIRECTORIES; the real path of a directory says nothing about the files in it -- a card in a detached
    # `--brain` can be a symlink into the protected base, and `open(path, "w")` follows it. So every file this
    # run is about to write, move or create is checked here (an archive move also removes its source entry),
    # all of them before any of them: a refusal of a later card must not come after an earlier one changed.
    m_date = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(args.triage_file))
    marker = os.path.join(markers, f".reviewed-{m_date.group(1)}-triage") if m_date else None
    if write:
        outputs = [c["path"] for _, c, _ in changed_files]
        if args.archive:
            for cid, c in sorted(edits.items()):
                if c["archive"]:
                    outputs += [c["path"], os.path.join(args.brain, "memory", "archive", "know", f"{cid}.md")]
        P.require_may_write(outputs + [marker, rp], "apply triage verdicts")

    archived = []
    if write:
        snapshot_first(args.brain, "pre-triage")
        for cid, c, after_text in changed_files:
            open(c["path"], "w", encoding="utf-8").write(after_text)
        for cid, c in sorted(edits.items()):
            if c["archive"]:
                if not args.archive:
                    report.append(f"NOT ARCHIVED {cid}: --archive was not given")
                    continue
                dst = os.path.join(args.brain, "memory", "archive", "know", f"{cid}.md")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(c["path"], dst)
                archived.append(cid)
        if marker:
            # The review marker the morning ping reads: the shared markers dir of the selected base;
            # a throwaway --brain keeps its own under <brain>/state/shared/reviewed.
            os.makedirs(markers, exist_ok=True)
            open(marker, "a").close()
    elif any(c["archive"] for c in edits.values()):
        report.append("archive moves pending (--apply --archive): "
                      + ", ".join(cid for cid, c in edits.items() if c["archive"]))
    if archived:
        report.append(f"archived: {', '.join(archived)}")

    out = "\n".join(report) + "\n"
    print(out)
    open(rp, "w", encoding="utf-8").write(out)
    print(f"report -> {rp}", file=sys.stderr)


if __name__ == "__main__":
    main()
