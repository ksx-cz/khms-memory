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
  unlink      card: links.<field> -= target · target: contradicts -= card (contradicts only)

UNLINK (#242, 2026-10-15) IS THE ONE WAY A LINK LEAVES A CARD. Until it existed this tool could only
add links, so a link written by hand against the spec (13 `challenged` cards carried a `refuted_by`,
which the spec fills only when status→refuted) could not be taken back by any sanctioned tool. The
verdict names the field and the target, and the row's card (its first id) is the card it edits:

  ### K-00001 — action **UNLINK** (refuted_by K-00002)
  ROW 7: APPLY unlink refuted_by K-00002 — challenged is not refuted (spec 4.10)

It removes exactly that link — and, for the symmetric `contradicts`, the mirror half on the target —
and nothing else: status is never touched (a status is lowered by a superseding card, not here), the
reason goes into the run's report next to the triage file. `refuted_by` has no inverse field in the
spec, so the target's own `contradicts` edge (which a refute also wrote) stays; unlink it by its own
row if it is wrong too. Fields: contradicts, refuted_by, supports, derived_from — never `supersedes`,
which is bound to a body line. REFUSED (exit 2, nothing written, not even the report) when the link is
not there, when the verdict names a field or target other than the UNLINK row's own, when it would
leave a `refuted` card with an empty `refuted_by` or a `derived` card with an empty `derived_from`, or
when the edit would move anything but that one list — a missing link is never a silent no-op.

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

import khms_lint as KL  # noqa: E402  (#241: the one link-list splitter and id grammar)
import khms_lock as L
import khms_paths as P  # noqa: E402
import khms_review_state as RS  # noqa: E402  (the triage marker's one name)

# The base: the resolver's root; --brain names another one (tests use a throwaway base).
# Card ids in comments (K-9xxxx) are illustrative stand-ins for the reference deployment's cards
# the measurements below were made on; they are not ids of any shipped base.
DEFAULT_BRAIN = P.soft("ROOT")


STATUS_RANK = {"active": 0, "challenged": 1, "refuted": 2, "superseded": 3, "condensed": 3}
CLASS_FROM_ACTION = {"MARK_DEAD_END": "refute", "MARK_CONJECTURE": "challenge",
                     "RETIRE_WHOLE": "retire", "AMEND": None, "UNLINK": "unlink"}
CLASSES = ("refute", "challenge", "contradicts", "retire", "unlink")
UNLINK_FIELDS = ("contradicts", "refuted_by", "supports", "derived_from")


class Refusal(Exception):
    """An unlink row that cannot be applied as written: the whole run stops before its first write."""
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
# The target is any link element the #241 grammar admits: a card id, or a temp label that never became
# a card (`C22`) — exactly the links an unlink exists to take back.
UNLINK_ROW_RE = re.compile(rf"^### (K-\d+) — action \*\*UNLINK\*\* \((\w+) ({KL.LINK_ID_RE.pattern})\)\s*$",
                           re.M)
UNLINK_VERDICT_RE = re.compile(
    rf"^ROW\s+(\d+)\s*:\s*APPLY\s+unlink\s+(\w+)\s+({KL.LINK_ID_RE.pattern})\s*(?:[—:-]\s*)?(.*)$", re.M)
VERDICT_RE = re.compile(
    r"^ROW\s+(\d+)\s*:\s*(APPLY|REJECT)\b\s*(refute|challenge|contradicts|retire)?\s*"
    r"(?:[—:-]\s*)?(.*)$", re.M)
QUOTE_RE = re.compile(r'^- offending quote: "(.*)"\s*$', re.M)
WIN_QUOTE_RE = re.compile(r'^- winning quote \((K-\d+)\): "(.*)"\s*$', re.M)


# ------------------------------------------------------------------------------- parsing

def parse_rows(text):
    """Every `### K-xxxxx — action **ACTION** (contradicted by K-yyyyy, severity N)` block,
    numbered from 1 in file order. The number is the row's identity in the verdict section."""
    rows = []
    marks = sorted(list(ROW_RE.finditer(text)) + list(UNLINK_ROW_RE.finditer(text)),
                   key=lambda m: m.start())
    for i, m in enumerate(marks):
        if m.re is UNLINK_ROW_RE:        # `### K-x — action **UNLINK** (<field> K-y)`: no quotes, no severity
            rows.append({"n": i + 1, "loser": m.group(1), "action": "UNLINK", "winner": m.group(3),
                         "field": m.group(2), "severity": 0, "quote": "", "winner_quote": "",
                         "score": 0.0, "winner_evidence": "", "winner_date": ""})
            continue
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
    """{row number: (APPLY|REJECT, class or None, reason, (field, target) or None)} from the REVIEW
    VERDICT section. An `APPLY unlink <field> <K-target>` line carries its field and target."""
    out = {}
    for m in VERDICT_RE.finditer(text):
        out[int(m.group(1))] = (m.group(2), m.group(3), m.group(4).strip(), None)
    for m in UNLINK_VERDICT_RE.finditer(text):
        out[int(m.group(1))] = ("APPLY", "unlink", m.group(4).strip(), (m.group(2), m.group(3)))
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


def remove_link_id(front, key, cid):
    """Remove `cid` from links.<key>, in whichever of the two frontmatter styles the card uses.
    Returns (front, changed); changed is False when the link is not there. Only the one line that
    holds the list is rewritten, and the result is re-read: if anything but that one list would differ
    (a multi-line YAML list, an odd layout), it raises instead of writing a guess."""
    meta = yaml.safe_load(front) or {}
    cur = KL.split_link_ids((meta.get("links") or {}).get(key))
    if cid not in cur:
        return front, False
    rest = [x for x in cur if x != cid]
    rendered = "[" + ", ".join(rest) + "]"
    inline = re.search(r"^links:\s*(\{.*\})\s*$", front, re.M)
    if inline:
        inner = inline.group(1)
        km = re.search(rf"(?<![\w-]){key}:\s*(?:\[[^\]]*\]|[^,}}]*)", inner)
        new = (front[:inline.start(1)] + inner[:km.start()] + f"{key}: {rendered}" + inner[km.end():]
               + front[inline.end(1):])
    else:
        bm = re.search(r"^links:\s*$", front, re.M)
        km = re.search(rf"^(\s+){key}:\s*.*$", front[bm.end():], re.M) if bm else None
        if not km:
            raise ValueError(f"links.{key} is not a one-line list this tool can edit")
        tail = front[bm.end():]
        new = front[:bm.end()] + tail[:km.start()] + f"{km.group(1)}{key}: {rendered}" + tail[km.end():]
    want = yaml.safe_load(front)
    want["links"][key] = rest
    try:
        got = yaml.safe_load(new)
    except yaml.YAMLError as e:
        raise ValueError(f"removing {cid} from links.{key} would break the frontmatter: {e}")
    if got != want:
        raise ValueError(f"removing {cid} from links.{key} would change more than that one list")
    return new, True


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
    """Per-row outcome, before any file is touched. Returns (applied, outcomes, refusals); a
    non-empty `refusals` (an unlink row that cannot be applied as written) stops the whole run.

    `assume_apply` is a PREVIEW for a triage file that has not been reviewed yet: rows with no
    verdict line are read as "apply what the row proposes", so a reviewer can see which rows
    would survive the guards before writing a verdict. It can never write (main refuses to
    combine it with --apply)."""
    outcomes, applied, unlinks, refusals = {}, [], [], []
    for row in rows:
        n = row["n"]
        d = verdicts.get(n)
        if not d and assume_apply:
            d = ("APPLY", None, "assumed from the row's proposed action (preview only)", None)
        if not d:
            outcomes[n] = ("REJECT", "no verdict line in the REVIEW VERDICT section — "
                                     "silence is not approval")
            continue
        decision, cls, reason, named = d
        if decision == "REJECT":
            outcomes[n] = ("REJECT", reason or "rejected by the review")
            continue
        cls = cls or CLASS_FROM_ACTION.get(row["action"])
        if cls == "unlink":
            # Not a contradiction verdict: no quote guard and no winner/loser role. Whether the link is
            # there is checked by plan(), against the card as earlier rows of this file left it.
            field, target = named or (row.get("field"), row["winner"])
            if row["action"] == "UNLINK" and (field, target) != (row.get("field"), row["winner"]):
                refusals.append(f"ROW {n}: the verdict names links.{field} {target}, which does not match "
                                f"the row's own links.{row.get('field')} {row['winner']}")
            elif field not in UNLINK_FIELDS:
                refusals.append(f"ROW {n}: links.{field} cannot be unlinked (only "
                                f"{', '.join(UNLINK_FIELDS)}; supersedes is bound to the card's "
                                f"SUPERSEDES body line and is never edited by this tool)")
            else:
                row.update(_class="unlink", _reason=reason, _field=field, _target=target)
                unlinks.append(row)
            continue
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
    for row in unlinks:
        outcomes[row["n"]] = ("APPLY", f"unlink {row['_field']} {row['_target']}: {row['_reason']}")
    return sorted(kept + unlinks, key=lambda r: r["n"]), outcomes, refusals


def plan(rows, brain):
    """{card id: {'path','before','after','ops'}} — frontmatter only."""
    edits = {}

    def get(cid):
        c = edits.get(cid)
        if not c:
            card = load(brain, cid)
            if not card:
                return None
            c = edits[cid] = {"path": card["path"], "body": card["body"],
                              "front0": card["front"], "front": card["front"],
                              "meta": card["meta"], "ops": [], "archive": False}
        return c

    for row in rows:
        if row["_class"] == "unlink":
            unlink(row, get)
            continue
        for cid, kind, arg in ops_for(row["_class"], row["loser"], row["winner"]):
            c = get(cid)
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


def unlink(row, get):
    """Remove the row's one link (and the mirror half of a `contradicts` edge) from the planned
    frontmatter. Raises Refusal for anything that cannot be applied exactly as the verdict says."""
    n, cid, field, target = row["n"], row["loser"], row["_field"], row["_target"]
    c = get(cid)
    if not c:
        raise Refusal(f"ROW {n}: no card file for {cid}")
    try:
        new, changed = remove_link_id(c["front"], field, target)
    except ValueError as e:
        raise Refusal(f"ROW {n}: {cid}: {e}")
    if not changed:
        raise Refusal(f"ROW {n}: {cid} links.{field} has no {target} — the link does not exist "
                      f"(or an earlier row of this file already removed it)")
    meta = yaml.safe_load(new) or {}
    rest = (meta.get("links") or {}).get(field) or []
    status, level = str(meta.get("status", "active")), str(meta.get("level", ""))
    if field == "refuted_by" and status == "refuted" and not rest:
        raise Refusal(f"ROW {n}: {cid} is refuted and {target} is its last refuted_by — the spec fills "
                      f"refuted_by when status→refuted; a status is not lowered by this tool")
    if field == "derived_from" and level == "derived" and not rest:
        raise Refusal(f"ROW {n}: {cid} is a derived card and {target} is its last derived_from — the "
                      f"spec requires it non-empty")
    c["front"], c["meta"] = new, meta
    c["ops"].append(f"links.{field} -= {target} (ROW {n}: {row['_reason']})")
    if field != "contradicts":
        return
    m = get(target)
    if not m:
        c["ops"].append(f"mirror half: no card file for {target}")
        return
    try:
        new, changed = remove_link_id(m["front"], "contradicts", cid)
    except ValueError as e:
        raise Refusal(f"ROW {n}: mirror half on {target}: {e}")
    if changed:
        m["front"], m["meta"] = new, yaml.safe_load(new) or {}
        m["ops"].append(f"links.contradicts -= {cid} (mirror of ROW {n}: {row['_reason']})")
    else:
        c["ops"].append(f"mirror half: {target} links.contradicts had no {cid}")


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
    # and gets its policy and its shared review marker. Every output of the run is checked,
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

    with L.lock('cards', args.brain):
        apply_locked(args, write, markers, rp)
    if write and selected:
        subprocess.run([sys.executable, os.path.join(P.CODE, 'build_views.py')], check=True)


def apply_locked(args, write, markers, rp):
    import khms_health
    khms_health.require_healthy(args.brain)
    text = open(args.triage_file, encoding="utf-8").read()
    rows = parse_rows(text)
    verdicts = parse_verdicts(text)
    applied, outcomes, refusals = decide(rows, verdicts, args.brain,
                                         assume_apply=args.assume_apply)
    edits = None
    if not refusals:
        try:
            edits = plan(applied, args.brain)
        except Refusal as e:
            refusals = [str(e)]
    if refusals:
        # Before the report, the cards and the marker: a refused unlink writes nothing at all.
        for r in refusals:
            print(f"REFUSED {r}", file=sys.stderr)
        print("REFUSED: nothing was written — fix or REJECT the rows above and run again", file=sys.stderr)
        sys.exit(2)

    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
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
    # The marker is named by the unit's FILE STEM (khms_review_state.triage_marker_name, the reader's own
    # definition): a suffixed unit (`DATE-lerr10-triage.md`) gets its own marker and never touches the
    # nightly `DATE-triage.md`'s one, which the date-only name used to overwrite.
    m_date = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(args.triage_file))
    marker = os.path.join(markers, RS.triage_marker_name(args.triage_file)) if m_date else None
    if write:
        outputs = [c["path"] for _, c, _ in changed_files]
        if args.archive:
            for cid, c in sorted(edits.items()):
                if c["archive"]:
                    outputs += [c["path"], os.path.join(args.brain, "memory", "archive", "know", f"{cid}.md")]
        P.require_may_write(outputs + [marker, rp], "apply triage verdicts")

    archived = []
    if write:
        L.dirty(args.brain)
        for cid, c, after_text in changed_files:
            L.atomic_text(c["path"], after_text)
        for cid, c in sorted(edits.items()):
            if c["archive"]:
                if not args.archive:
                    report.append(f"NOT ARCHIVED {cid}: --archive was not given")
                    continue
                dst = os.path.join(args.brain, "memory", "archive", "know", f"{cid}.md")
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(c["path"], dst)
                archived.append(cid)
    elif any(c["archive"] for c in edits.values()):
        report.append("archive moves pending (--apply --archive): "
                      + ", ".join(cid for cid, c in edits.items() if c["archive"]))
    if archived:
        report.append(f"archived: {', '.join(archived)}")

    out = "\n".join(report) + "\n"
    print(out)
    L.atomic_text(rp, out)
    if write and marker:
        # Commit the review fact only after cards, archive moves and the report succeeded.
        L.atomic_text(marker, 'sha256=' + hashlib.sha256(text.encode()).hexdigest() + '\n')
    print(f"report -> {rp}", file=sys.stderr)


if __name__ == "__main__":
    main()
