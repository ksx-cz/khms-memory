#!/usr/bin/env python3
"""Erratum — correct ONE claim of ONE approved card in place — a claim that was false when it
was written (default kind `wrong-record`), or a VALUE that was true when written and has since
moved in the repo (kind `dated-pointer`) — keeping the retracted wording visible inside the
card and in the tombstone ledger.

Usage: python3 tools/khms_erratum.py declare --card K-x --was '…' --evidence '…' --by owner|coordinator,
       then  python3 tools/khms_erratum.py apply --card K-x --ts TS-n --now '…'

WHY THIS EXISTS (owner, 2026-09-20 08:09, verbatim): "To ze jsme si neco mysleli a pak se
ukazalo ze je to spatne je informace co chceme zachovat. Ale ze zapiseme nejakou uplnou
blbost a vime ze ta samotna informace je blbe - to je potreba spravit."

Two kinds of "untrue" live in the base and the immutability rule (spec P2, K-90025) treated
them alike:

  * a SUPERSEDED BELIEF — true to the evidence when written, overtaken by events later.
    KEPT, visible, as a warning against the dead end. `supersedes` / `refuted`, never edited.
  * a WRONG RECORD — false against a source that already existed when it was written, and the
    source has not changed since. K-92929's NO-CORRECTION-TARGET sentence (a claim about what
    `build_views.py`'s reverse index does, checkable by grep at the time, false then and now)
    is the canonical case. This is what an erratum corrects.

The decision to split them was taken on 2026-08-31 (K-92396: "a lying FACT is deleted with a
tombstone; a refuted HYPOTHESIS stays") and `khms_triage.py` has classified at claim level
since 2026-09-05 — but its AMEND action ("one false claim in an otherwise true card") mapped
to nothing (`apply_triage.py`: `"AMEND": None`), so the tombstone counter stood at 134 with an
empty ledger. This tool is the missing action.

THE THREE-QUESTION TEST the declarer answers (plans/kbfix-erratum-design.md §1):
  1. Could the claim have been checked when written, against a source that existed then?
     NO -> belief -> not an erratum.
  2. Did that source change between writing and discovery?  YES -> dated pointer: a BELIEF
     is superseded, never edited; a VALUE that has its own repo source (a key in a config,
     a dimension, a limit) is replaced by a pointer to that key and file through
     `--kind dated-pointer --source "<key> in <file>"` (owner 2026-10-08 on K-92603: "Erratum
     udelej. Rovnou do ty karty uprav jeji text"; K-91076: a value with its own repo source
     is not know-how). Its block says the sentence was TRUE when written and where the value
     lives now — never "wrong at the time of writing" (12 blocks said that on 2026-10-08,
     TS-00410..421, and contradicted their own evidence; `reword` re-renders such a block).
     NO -> wrong record (the default kind).
  3. Does the rest of the card stand?  YES -> erratum on the claim.  NO -> retire the card
     whole (`apply_triage.py`, status refuted + tombstone). Never delete.

WHAT PROTECTS THE SUPERSEDED-BELIEF PROPERTY (mechanisms, not intentions):
  * the retracted wording never disappears — it is quoted in the card's ERRATUM block and in
    memory/tombstones/bodies/TS-*.md; the ledger line carries it too;
  * `khms_triage.classify` is run on the CLAIM SENTENCE with refuted_by_evidence=True: any
    attempt-marker inside it ("we thought", "hypothesised", "tried"…) or `level: assumption`
    on the card forecloses FALSE_FACT and the tool REFUSES; `--override "<reason>"` is the
    only way past, and the reason is written into the ledger where a review sees it;
  * one run = one card = one claim; there is no batch mode on purpose;
  * `id`, `status`, `type`, `links` are never touched — an erratum changes the TEXT of a
    claim, nothing about the card's place in the graph;
  * nothing is written without `--by` (owner|coordinator) and `--evidence` (the command and
    its output, or the source), under the base's card lock. Recovery uses server file versions,
    followed by reconciliation of the card and tombstone ledger.

TWO STEPS, so the check between them is a real check (K-90585):
  declare  — verifies the claim stands verbatim in the card, runs the guard, dry-runs the
             lint `apply` will run over the WHOLE card (QUOTES and CZ included: K-91189's
             claim stood once in the body and once, without backticks, in its QUOTES — the
             token match passed, the normalised R1 match refused apply, and the orphan
             tombstone was repaired by hand), and only then allocates a tombstone id and
             writes LEDGER.md + bodies/TS-nnnnn.md.  From this moment
             khms_lint rule R1 FAILS on every card that still states the claim unmarked —
             build_views.py and approve_inbox.py both run it.
  apply    — replaces the claim with the corrected wording followed by `[ERRATUM TS-nnnnn]`,
             appends an ERRATUM block at the end of the body quoting the original, adds
             `errata:` to the frontmatter.  R1 passes again; rule R2 keeps head, body and
             ledger consistent from then on.

  reword   — re-renders the ERRATUM block of an APPLIED erratum in another kind (or with a
             new evidence/source), in place: same TS id, same ledger line (its `wrong=` field
             rewritten where it stands), same head, same corrected sentence.

Usage:
  khms_erratum.py declare --card K-x --was "<verbatim claim>" --evidence "<cmd + output>"
                          --by owner|coordinator [--kind wrong-record|dated-pointer]
                          [--source "<key> in <repo file>"] [--keys a,b]
                          [--override "<reason>"] [--brain DIR] [--dry-run]
  khms_erratum.py apply   --card K-x --ts TS-nnnnn --now "<corrected wording>"
                          [--was-cz "<cz sentence>" --now-cz "<cz sentence>"]
                          [--brain DIR] [--dry-run]
  khms_erratum.py reword  --card K-x --ts TS-nnnnn --kind wrong-record|dated-pointer
                          [--source "<key> in <repo file>"] [--evidence "<text>"]
                          [--was-cz "<cz sentence, verbatim>"] [--brain DIR] [--dry-run]

The kind is carried by the ledger line itself (no new field: the ledger grammar is
khms_triage.TS_LINE): a dated pointer's `wrong=` reads
`DATED POINTER; true when written; value now in <source>; record: <evidence>`, which
tombstone_lookup.py renders as a dated record, not as an error.

Exit codes: 0 done · 1 refused (nothing written) · 2 usage.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import textwrap

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_lint    # noqa: E402
import khms_triage  # noqa: E402

import khms_paths as KP
import khms_lock as KL

REAL_BRAIN = KP.soft("ROOT")
BY_VALUES = ("owner", "coordinator")
MIN_EVIDENCE = 20      # characters; "see chat" is not evidence
WRAP = 100
KINDS = ("wrong-record", "dated-pointer")
DATED_TAG = "DATED POINTER"
DATED_RE = re.compile(r"^DATED POINTER; true when written; value now in (.*?); record: (.*)$",
                      re.S)


class Refusal(Exception):
    pass


# ------------------------------------------------------------------ paths & io
def paths(brain):
    tomb = os.path.join(brain, "memory", "tombstones")
    return {
        "brain": brain,
        "know": os.path.join(brain, "memory", "know"),
        "tomb": tomb,
        "ledger": os.path.join(tomb, "LEDGER.md"),
        "counter": os.path.join(tomb, ".next_ts_id"),
        "bodies": os.path.join(tomb, "bodies"),
    }


def read_card(know, cid):
    """-> (front_text, body_text, meta) or raises a Refusal."""
    path = os.path.join(know, f"{cid}.md")
    if not os.path.exists(path):
        raise Refusal(f"{cid}: no such card in {know}")
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise Refusal(f"{cid}: no frontmatter")
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise Refusal(f"{cid}: bad YAML: {e}")
    if str(meta.get("id")) != cid:
        raise Refusal(f"{cid}: id in frontmatter is '{meta.get('id')}'")
    return m.group(1), m.group(2), meta


def write_atomic(path, text):
    KL.atomic_text(path, text)


def claim_regex(claim):
    """The claim as written on one line, matched across the card's own line wrapping."""
    toks = (claim or "").split()
    if not toks:
        raise Refusal("empty claim")
    return re.compile(r"\s+".join(re.escape(t) for t in toks))


def find_claim(body, claim):
    """-> [(start, end)] spans of `claim` in the body, excluding any occurrence that sits
    inside an ERRATUM block (a quoted retraction is not the card stating it)."""
    blocks = [(m.start(), m.end()) for m in khms_lint.ERRATUM_BLOCK_RE.finditer(body)]
    return [(m.start(), m.end()) for m in claim_regex(claim).finditer(body)
            if not any(bs <= m.start() < be for bs, be in blocks)]


def guard(cid, meta, claim, refuted=True):
    """The rail: the claim SENTENCE (not the card's frame) must classify as FALSE_FACT.

    The frame is deliberately not inherited here — K-92929 carries a PROPOSAL paragraph, which
    is an attempt-marker for the card, while the false sentence in it is a plain assertion
    about a tool. `triage_card` inherits the frame because it decides what to do with a whole
    card; this tool is handed the sentence by a human who has answered the three questions,
    and the rail's job is to catch a sentence that is itself a hypothesis, or a card that is
    itself an assumption.
    """
    probe = {"id": cid, "type": "fact", "level": meta.get("level", "observation"),
             "status": meta.get("status", "active"), "body": claim}
    return khms_triage.classify(probe, refuted_by_evidence=refuted)


def _ledger_safe(s):
    """The ledger grammar delimits with ` | ` and quotes claim/wrong with double quotes; both
    must not appear inside a field. The verbatim text lives in bodies/TS-*.md."""
    return " ".join((s or "").replace('"', "'").replace("|", "/").split())


def _write_ledger_line(path, line, ts, replace=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        lines = open(path, encoding="utf-8").read().splitlines()
    else:
        lines = ["# tombstone ledger — one line per retracted claim "
                 "(format: khms_triage.render_tombstone)",
                 "# NEVER a card: nothing in memory/know/ or memory/archive/know/ globs this file.",
                 "# Two or more dates in recurrences= on one line = the previous measure failed.",
                 ""]
    if replace:
        lines = [l for l in lines if not l.startswith(f"{ts} |")]
    lines.append(line)
    write_atomic(path, "\n".join(lines) + "\n")


def _body_file_field(bpath, rx, flags=0):
    if not os.path.exists(bpath):
        return None
    m = re.search(rx, open(bpath, encoding="utf-8").read(), flags)
    return m.group(1).strip() if m else None


def wrong_field(kind, source, evidence):
    """The ledger's `wrong=` text for a kind. Default: the evidence itself (unchanged)."""
    if kind == "dated-pointer":
        return f"{DATED_TAG}; true when written; value now in {source}; record: {evidence}"
    return evidence


def split_wrong(wrong):
    """-> (kind, source, evidence) from a ledger `wrong=` field."""
    m = DATED_RE.match(wrong or "")
    if m:
        return "dated-pointer", m.group(1), m.group(2)
    return "wrong-record", "", wrong or ""


def check_source(kind, source):
    if kind not in KINDS:
        raise Refusal(f"--kind must be one of {KINDS}, got '{kind}'")
    if kind == "dated-pointer":
        src = " ".join((source or "").split())
        if len(src) < 5 or ";" in src or "record:" in src:
            raise Refusal("--kind dated-pointer needs --source \"<key> in <repo file>\" — where "
                          "the value lives now (no ';', no 'record:'); the block points there")
        return src
    if source:
        raise Refusal("--source belongs to --kind dated-pointer only")
    return ""


def render_block(ts, date, signer, claim, evidence, now, kind="wrong-record", source="",
                 was_cz=""):
    """The ERRATUM block appended at the end of the card. The default wording is the one
    every erratum before 2026-10-09 carries; a dated pointer says what is true of it."""
    if kind == "dated-pointer":
        text = (f"ERRATUM {ts} ({date}, {signer}, dated pointer): the sentence "
                f"\"{claim.strip()}\" was true when it was written; the value has since moved "
                f"in the repo and lives in {source}, so the number was replaced by a pointer "
                f"to it (a value with its own repo source is not know-how). Record: "
                f"{evidence.strip().rstrip('.')}. It now reads: \"{now.strip()}\"."
                + (f" The CZ mirror sentence \"{was_cz.strip()}\" got the same pointer."
                   if was_cz else ""))
    else:
        text = (f"ERRATUM {ts} ({date}, {signer}): the sentence \"{claim.strip()}\" was wrong at "
                f"the time of writing — {evidence}. It now reads: \"{now.strip()}\"."
                + (f" The CZ mirror sentence \"{was_cz.strip()}\" was corrected likewise."
                   if was_cz else ""))
    return textwrap.fill(text, width=WRAP)


def preflight_lint(cid, body, span, claim, P, today):
    """The lint `apply` will run over the whole card (khms_lint R1: normalised match, ERRATUM
    blocks excluded, QUOTES and CZ INCLUDED), run at declare time on the card as apply would
    leave it: the claim's one verbatim occurrence gone, the new tombstone in the ledger.
    A hit means apply could never succeed — refuse before a tombstone id is spent."""
    retracted = khms_lint.load_retracted(P["brain"])
    pending = {"ts": "TS-(this declare)", "was": cid, "date": today, "claim": claim,
               "nclaim": khms_lint.norm_claim(claim)}
    s, e = span
    msg = khms_lint.check_retracted_claims(cid, body[:s] + "\n" + body[e:],
                                           retracted + [pending])
    if msg:
        raise Refusal(
            f"{cid}: apply would fail khms_lint R1 on this card even after the claim is "
            f"replaced — the card states the same sentence (normalised: case, punctuation, "
            f"backticks ignored) a second time, e.g. in its QUOTES tail or CZ mirror, or it "
            f"still states an earlier retracted claim. Lint says: {msg} — choose a wording "
            f"that occurs once in normalised form (carry unquoted context), or fix the other "
            f"copy first.")


# ------------------------------------------------------------------- declare
def declare(a, P):
    if a.by not in BY_VALUES:
        raise Refusal(f"--by must be one of {BY_VALUES}, got '{a.by}' — an erratum is a "
                      f"signed statement (P6: jobs propose, review declares)")
    if len((a.evidence or "").strip()) < MIN_EVIDENCE:
        raise Refusal(f"--evidence is the command and its output, or the source that shows "
                      f"the claim false; got {len((a.evidence or '').strip())} characters")
    _, body, meta = read_card(P["know"], a.card)
    spans = find_claim(body, a.was)
    if not spans:
        raise Refusal(f"{a.card}: the claim is not in the card's live text (whitespace-"
                      f"insensitive match). An erratum retracts what the card SAYS, verbatim.")
    if len(spans) > 1:
        raise Refusal(f"{a.card}: the claim occurs {len(spans)} times — give a longer, "
                      f"unambiguous wording")
    source = check_source(a.kind, a.source)
    today = a.date or datetime.date.today().isoformat()
    preflight_lint(a.card, body, spans[0], a.was, P, today)
    t = guard(a.card, meta, a.was)
    if t.bucket != khms_triage.FALSE_FACT:
        if not a.override:
            raise Refusal(
                f"{a.card}: the classifier files this sentence as {t.bucket}, not FALSE_FACT "
                f"({'; '.join(t.reasons)}). A hypothesis or an assumption that lost is KEPT, "
                f"not corrected (K-92396). If you have answered the three questions and this "
                f"really is a wrong record, re-run with --override \"<why the rail is wrong "
                f"here>\" — the reason goes into the ledger.")
        wrong = f"[override: {a.override.strip()}] {a.evidence.strip()}"
    else:
        wrong = a.evidence.strip()
    wrong = wrong_field(a.kind, source, wrong)
    keys = [k.strip() for k in (a.keys or "").split(",") if k.strip()] or [a.card.lower()]
    if a.dry_run:
        # no id is spent on a dry run — the counter is the one file declare must not touch here
        print(f"DRY declare {a.card}: kind={a.kind}; guard={t.bucket}; claim found once; "
              f"apply's lint passes on the whole card; would allocate a tombstone and write "
              f"{P['ledger']}")
        return 0
    KL.dirty(P["brain"])
    os.makedirs(P["bodies"], exist_ok=True)
    ledger = khms_triage.load_ledger(P["ledger"])
    line, is_rec, ts = khms_triage.propose_tombstone(
        {"id": a.card}, _ledger_safe(a.was), _ledger_safe(wrong), "pending", keys,
        today=today, ledger=ledger, counter=P["counter"])
    # truth= is a pointer (the ledger grammar allows no spaces there): the ERRATUM block
    # that `apply` writes into the card.
    line = line.replace("truth=pending", f"truth={a.card}#ERRATUM-{ts}", 1)
    bpath = os.path.join(P["bodies"], f"{ts}.md")
    block = (f"# {ts} — erratum declared {today} by {a.by} on {a.card}\n\n"
                 f"CLAIM (verbatim, retracted):\n{a.was.strip()}\n\n"
                 f"WHY IT IS WRONG (evidence):\n{wrong}\n\n"
                 + (f"KIND: dated-pointer — true when written; the value now lives in "
                    f"{source}\n\n" if a.kind == "dated-pointer" else "")
                 + f"GUARD: {t.bucket} ({'; '.join(t.reasons)})\n\n")
    previous = open(bpath, encoding='utf-8').read() if os.path.exists(bpath) else ''
    if block not in previous:
        write_atomic(bpath, previous + block)
    _write_ledger_line(P["ledger"], line, ts, replace=is_rec)
    print(f"declared {ts} on {a.card} "
          f"({'RECURRENCE — the same claim was retracted before' if is_rec else 'new'})")
    print(f"  ledger: {P['ledger']}")
    print(f"  body:   {bpath}")
    print(f"  next:   khms_erratum.py apply --card {a.card} --ts {ts} --now \"<corrected wording>\"")
    print("  until then khms_lint R1 fails on every card still stating the claim — "
          "build_views.py and approve_inbox.py both refuse.")
    return 0


# --------------------------------------------------------------------- apply
def apply(a, P):
    front, body, meta = read_card(P["know"], a.card)
    if not (a.now or "").strip():
        raise Refusal("--now is the corrected wording; it cannot be empty")
    ledger = {t["ts"]: t for t in khms_triage.load_ledger(P["ledger"])}
    t = ledger.get(a.ts)
    if not t:
        raise Refusal(f"{a.ts}: not in {P['ledger']} — declare first")
    if t["was"] != a.card:
        raise Refusal(f"{a.ts} was declared on {t['was']}, not {a.card}")
    if f"[ERRATUM {a.ts}]" in body:
        raise Refusal(f"{a.card}: already carries [ERRATUM {a.ts}]")
    bpath = os.path.join(P["bodies"], f"{a.ts}.md")
    claim = _body_file_field(bpath, r"CLAIM \(verbatim, retracted\):\n(.*?)\n\n", re.S) \
        or t["claim"]
    signer = _body_file_field(bpath, r"declared \S+ by (\w+) on K-") or "coordinator"
    spans = find_claim(body, claim)
    if len(spans) != 1:
        raise Refusal(f"{a.card}: the retracted claim is found {len(spans)} times in the "
                      f"live text (expected exactly once)")
    if bool(a.was_cz) != bool(a.now_cz):
        raise Refusal("--was-cz and --now-cz go together")
    if a.was_cz and len(find_claim(body, a.was_cz)) != 1:
        raise Refusal(f"{a.card}: the CZ sentence is found "
                      f"{len(find_claim(body, a.was_cz))} times")

    today = a.date or datetime.date.today().isoformat()
    s, e = spans[0]
    new_body = body[:s] + marked(a.now, a.ts) + body[e:]
    if a.was_cz:
        new_body = claim_regex(a.was_cz).sub(
            lambda m: a.now_cz.strip() + f" [ERRATUM {a.ts}]", new_body, count=1)
    kind, source, evidence = split_wrong(t["wrong"])
    block = render_block(a.ts, today, signer, claim, evidence, a.now, kind, source, a.was_cz)
    new_body = new_body.rstrip("\n") + "\n\n" + block + "\n"
    new_front = _add_erratum_to_front(front, a.ts, today, signer)
    try:
        new_meta = yaml.safe_load(new_front)
    except yaml.YAMLError as e:
        raise Refusal(f"{a.card}: frontmatter would not parse after the edit: {e}")
    # the result has to pass the very rules that will be run over it tonight
    retracted = khms_lint.load_retracted(P["brain"])
    for check in (khms_lint.check_retracted_claims(a.card, new_body, retracted),
                  khms_lint.check_errata_consistency(
                      a.card, new_meta, new_body, set(ledger),
                      {k: v["was"] for k, v in ledger.items()})):
        if check:
            raise Refusal(f"the result would fail lint: {check}")
    out = f"---\n{new_front}\n---\n{new_body}"
    if a.dry_run:
        print(out)
        return 0
    KL.dirty(P["brain"])
    write_atomic(os.path.join(P["know"], f"{a.card}.md"), out)
    with open(bpath, "a", encoding="utf-8") as fh:
        fh.write(f"APPLIED {today}: the card now reads:\n{a.now.strip()}\n"
                 + (f"CZ: {a.now_cz.strip()}\n" if a.now_cz else "") + "\n")
    print(f"applied {a.ts} to {a.card}: claim replaced, ERRATUM block appended, "
          f"errata: added to frontmatter. Run tools/build_views.py.")
    return 0


# -------------------------------------------------------------------- reword
def _dewrap(block):
    """A block as one line: textwrap's line breaks undone. A break after a word-internal
    hyphen ("wrong-\nwritten") is textwrap's break_on_hyphens and joins without a space."""
    return re.sub(r"(?<=\w-)\n", "", block).replace("\n", " ")


def reword(a, P):
    """Re-render the ERRATUM block of an applied erratum in place (kind, source, evidence).
    The claim and the corrected wording come from the tombstone body file (verbatim), the
    apply date and signer from the old block's head, the CZ mirror sentence from the old
    block (or --was-cz). Nothing else in the card changes; the ledger line keeps its place."""
    front, body, meta = read_card(P["know"], a.card)
    source = check_source(a.kind, a.source)
    rows = khms_triage.load_ledger(P["ledger"])
    ledger = {t["ts"]: t for t in rows}
    t = ledger.get(a.ts)
    if not t:
        raise Refusal(f"{a.ts}: not in {P['ledger']}")
    if t["was"] != a.card:
        raise Refusal(f"{a.ts} was declared on {t['was']}, not {a.card}")
    blocks = [m for m in khms_lint.ERRATUM_BLOCK_RE.finditer(body) if m.group(1) == a.ts]
    if len(blocks) != 1:
        raise Refusal(f"{a.card}: {len(blocks)} ERRATUM {a.ts} blocks in the body (expected "
                      f"exactly one — reword works on an APPLIED erratum)")
    old = blocks[0]
    old_text = old.group(0).rstrip()          # at the end of the card the match takes the \n
    old_end = old.start() + len(old_text)
    flat_old = _dewrap(old_text)
    head = re.match(r"ERRATUM \S+ \((\d{4}-\d\d-\d\d), (\w+)", flat_old)
    if not head:
        raise Refusal(f"{a.card}: cannot read the date and signer of the {a.ts} block")
    date, signer = head.group(1), head.group(2)
    bpath = os.path.join(P["bodies"], f"{a.ts}.md")
    claim = _body_file_field(bpath, r"CLAIM \(verbatim, retracted\):\n(.*?)\n\n", re.S) \
        or t["claim"]
    applied = re.findall(r"APPLIED \S+: the card now reads:\n(.*?)\n(?:CZ: .*?\n)?\n",
                         open(bpath, encoding="utf-8").read() if os.path.exists(bpath) else "",
                         re.S)
    if not applied:
        raise Refusal(f"{bpath}: no APPLIED record — the corrected wording is read from there")
    now = applied[-1]
    was_cz = a.was_cz.strip()
    if not was_cz:
        m = re.search(r" The CZ mirror sentence \"(.*?)\" (?:was corrected likewise|got the "
                      r"same pointer)\.$", flat_old, re.S)
        was_cz = m.group(1) if m else ""
    _, _, old_evidence = split_wrong(t["wrong"])
    evidence = " ".join((a.evidence or "").split()) or old_evidence
    if len(evidence) < MIN_EVIDENCE:
        raise Refusal(f"--evidence too short ({len(evidence)} characters)")
    new_wrong = _ledger_safe(wrong_field(a.kind, source, evidence))
    block = render_block(a.ts, date, signer, claim, evidence, now, a.kind, source, was_cz)
    new_body = body[:old.start()] + block + body[old_end:]
    retracted = khms_lint.load_retracted(P["brain"])
    for check in (khms_lint.check_retracted_claims(a.card, new_body, retracted),
                  khms_lint.check_errata_consistency(
                      a.card, meta, new_body, set(ledger),
                      {k: v["was"] for k, v in ledger.items()})):
        if check:
            raise Refusal(f"the result would fail lint: {check}")
    if new_body == body and new_wrong == t["wrong"]:
        print(f"{a.card} {a.ts}: already reads as {a.kind} with this evidence — nothing to do")
        return 0
    if a.dry_run:
        print(block)
        print(f"ledger wrong= -> {new_wrong}")
        return 0
    KL.dirty(P["brain"])
    write_atomic(os.path.join(P["know"], f"{a.card}.md"), f"---\n{front}\n---\n{new_body}")
    line = khms_triage.render_tombstone(t["ts"], t["date"], t["was"], t["keys"], t["claim"],
                                        new_wrong, t["truth"], t["recurrences"], t["body"])
    lines = open(P["ledger"], encoding="utf-8").read().splitlines()
    idx = [i for i, l in enumerate(lines) if l.startswith(f"{a.ts} |")]
    if len(idx) != 1:
        raise Refusal(f"{a.ts}: {len(idx)} ledger lines (expected one)")
    lines[idx[0]] = line
    write_atomic(P["ledger"], "\n".join(lines) + "\n")
    today = a.date or datetime.date.today().isoformat()
    with open(bpath, "a", encoding="utf-8") as fh:
        fh.write(f"REWORDED {today}: kind={a.kind}"
                 + (f", value now in {source}" if source else "")
                 + f"; the block now reads:\n{_dewrap(block)}\n\n")
    print(f"reworded {a.ts} on {a.card} as {a.kind}: block re-rendered in place, ledger "
          f"line {idx[0] + 1} rewritten in place. Run tools/build_views.py.")
    return 0


def marked(text, ts):
    """The corrected wording, wrapped, with its `[ERRATUM TS-nnnnn]` marker appended AFTER
    wrapping — textwrap would otherwise be free to break inside the marker (it did, on
    K-92929's first apply: R2 then refused the result as 'markers [] != blocks'). The last
    line may exceed WRAP by the marker's length; the marker is one token on purpose."""
    return textwrap.fill(text.strip(), width=WRAP) + f" [ERRATUM {ts}]"


def _add_erratum_to_front(front, ts, date, by):
    """Textual insert, not a YAML re-dump: the rest of the frontmatter keeps its exact
    spelling (quoting, flow style, key order), so the card's diff is the erratum and nothing
    else. Nothing is guessed: the result is parsed before it is written."""
    item = f"  - {{ts: {ts}, date: '{date}', by: {by}}}"
    lines = front.split("\n")
    idx = next((i for i, l in enumerate(lines) if re.match(r"^errata:\s*$", l)), None)
    if idx is None:
        return front.rstrip("\n") + "\nerrata:\n" + item
    j = idx + 1
    while j < len(lines) and re.match(r"^\s+-\s", lines[j]):
        j += 1
    lines.insert(j, item)
    return "\n".join(lines)


# ---------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("declare", help="retract one claim of one card into the ledger")
    d.add_argument("--card", required=True)
    d.add_argument("--was", required=True, help="the claim, verbatim, as the card states it")
    d.add_argument("--evidence", required=True, help="command + output, or the source")
    d.add_argument("--by", required=True, help="owner | coordinator")
    d.add_argument("--keys", default="", help="subject keys for recurrence lookup, comma-separated")
    d.add_argument("--override", default="", help="reason the classifier rail is wrong here")
    d.add_argument("--kind", default="wrong-record", choices=KINDS,
                   help="wrong-record (false when written; default) | dated-pointer (a value "
                        "true when written that now lives in the repo — needs --source)")
    d.add_argument("--source", default="", help="dated-pointer: '<key> in <repo file>'")
    p = sub.add_parser("apply", help="write the corrected wording into the card")
    p.add_argument("--card", required=True)
    p.add_argument("--ts", required=True)
    p.add_argument("--now", required=True, help="the corrected wording")
    p.add_argument("--was-cz", default="")
    p.add_argument("--now-cz", default="")
    r = sub.add_parser("reword", help="re-render an applied erratum's block (kind/source/evidence)")
    r.add_argument("--card", required=True)
    r.add_argument("--ts", required=True)
    r.add_argument("--kind", required=True, choices=KINDS)
    r.add_argument("--source", default="")
    r.add_argument("--evidence", default="", help="default: the ledger's evidence")
    r.add_argument("--was-cz", default="", help="the CZ mirror's retracted sentence, verbatim "
                                                "(default: read from the old block)")
    for s in (d, p, r):
        s.add_argument("--brain", default=REAL_BRAIN)
        s.add_argument("--date", default="", help="ISO date (default: today)")
        s.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.brain:
        ap.error('select a base with KHMS_ROOT or --brain')
    if not KP.same_path(a.brain, KP.ROOT):
        ap.error('--brain must match the selected KHMS_ROOT; select its config before allocating ids')
    if not a.dry_run:
        KP.require_writable('write an erratum')
    P = paths(a.brain)
    try:
        with KL.lock('cards', a.brain):
            import khms_health
            khms_health.require_healthy(a.brain)
            result = {"declare": declare, "apply": apply, "reword": reword}[a.cmd](a, P)
        if not a.dry_run and a.cmd != 'declare' and KP.same_path(a.brain, KP.soft('ROOT')):
            subprocess.run([sys.executable, os.path.join(KP.CODE, 'build_views.py')], check=True)
        return result
    except Refusal as r:
        print(f"REFUSED: {r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
