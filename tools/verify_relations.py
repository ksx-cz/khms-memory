#!/usr/bin/env python3
"""Mechanically verify that every nightly candidate names an existing card.

    verify_relations.py cards.md [--max N] [--know DIR] [--archive DIR]
                                 [--dry-run] [--self-test]

WHY THIS EXISTS
---------------
Measured 2026-09-02: the base holds 17 cards about "SVD48V bus voltage", 14 (+3)
about "minimum turning radius", 8 about "wheel radius"; the triage stage
confirmed 75 contradictions in a 250-pair sample; and the WHOLE base carries
8 superseded + 7 refuted + 23 challenged edges.  The nightly pipeline produces
and never merges, and the morning review's approval rate is ~100 %, so nothing
between the sweep and know/ ever asks "does this replace something we already
know?".  On 2026-09-04 the review did that work by hand: 36 candidates → 16
approved, 7 of them merges, 14 rejected.

Asking the model to link is a request (the consolidate prompt has asked for
links since 2026-07-27).  This is the check.  Every candidate must carry exactly
one line:

    RELATION: supersedes K-01234 BECAUSE <one sentence>
    RELATION: supports K-01234
    RELATION: contradicts K-01234
    RELATION: new — nearest K-01234 unrelated because <one sentence>

The id must EXIST; a `supersedes` target must still be LIVE — `status: active`
or `status: challenged` (an edge onto an already-superseded, refuted or
condensed card chains onto a dead end); and `new` must name one of the ids
`tools/nearest_cards.py` computed for that candidate, so "nothing in the base is
close" cannot be asserted about a card the retrieval layer never proposed.

NOTHING IS DELETED.  A candidate that fails is MOVED, whole, into
`## DROPPED (no valid RELATION)` at the end of the file; a candidate over
`--max` is MOVED, whole, into `## DEFERRED`.  Both sections are review plumbing:
`convert_inbox2.py` stops at them, so they never become cards, and the morning
review can rescue anything by moving it back.

Exit 0 when nothing was dropped, 1 when something was (NON-FATAL in the
pipeline: wired with `|| true`, exactly like verify_quotes.py, whose fatal
`set -e` death cost every nightly from 2026-07-28 — K-00490), 2 on a usage or
format error.
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

KNOW = None        # default: the base's memory/know, resolved when the CLI runs (khms_paths)
ARCHIVE = None     # default: the base's memory/archive/know

DROPPED_HEAD = "## DROPPED (no valid RELATION)"
DEFERRED_HEAD = "## DEFERRED"

# WHICH TARGET STATUSES A `supersedes` EDGE MAY POINT AT (widened 2026-09-04,
# package G2).  The rule was written as "must be active", and that literal
# reading dropped the one case the whole gate exists to create: `challenged` is
# the status the triage stage writes onto the LOSER of a contradiction, i.e.
# onto exactly the card a better-measured candidate ought to replace.  Dropping
# such a candidate left the challenged card standing AND threw its replacement
# away — merge pressure pointed backwards.  `superseded` / `refuted` /
# `condensed` stay out: those edges chain onto a dead end, which is the reason
# the rule existed in the first place.
SUPERSEDABLE = ("active", "challenged")

# Same label grammar as verify_quotes.py / nearest_cards.py.
CARD_RE = re.compile(r"^#{2,3}\s+([A-Z]\d+[a-z]?)\b(.*)$", re.M)
SECTION_RE = re.compile(r"^## +(\S.*)$", re.M)
RELATION_RE = re.compile(r"^[ \t]*\*{0,2}RELATION:?\*{0,2}[ \t]*(.+?)[ \t]*$", re.M)
NEAREST_RE = re.compile(r"^NEAREST:(.*)$", re.M)
CARD_ID_RE = re.compile(r"K-\d{5}")

# The four shapes, parsed strictly.  Tolerated: the emphasis markers around the
# keyword, an en/em dash or a colon after `new`, a `BECAUSE:` with a colon.
SUPERSEDES_RE = re.compile(
    r"^supersedes\s+(K-\d{5})\b\s*BECAUSE\b\s*:?\s*(\S.*)$", re.I)
SUPPORTS_RE = re.compile(r"^(supports|contradicts)\s+(K-\d{5})\b\s*(.*)$", re.I)
# WHAT THE `new` SHAPE IS ACTUALLY CHECKING: that the candidate NAMES its
# nearest card and SAYS, in its own words, why that card is not the same
# subject.  It is not checking the word "because".  Widened 2026-09-15 after the
# gate dropped 5 of 20 valid candidates in one night (C7, C8, C10, C12, C15 of
# the 2026-09-15 inbox), every one of which gave the justification in a
# PARENTHESIS — "unrelated (an earlier watchdog gotcha list, not a build
# cycle)" — instead of after "because".  That is K-02720's class exactly: a
# pipeline parser that accepts one exact textual form drops valid content, and
# the content looks like a night that produced nothing.  The justification is
# still MANDATORY: `unrelated` with nothing after it does not match.
NEW_RE = re.compile(
    r"^new\b\s*[—–:-]?\s*nearest\s+(K-\d{5})\b\s*unrelated\b"
    r"\s*(?:because\b\s*:?|[—–:,-]|(?=\())\s*(\S.*)$",
    re.I)


def card_status(cid, know, archive):
    """-> ('active'|'superseded'|..., 'know'|'archive') or (None, None)."""
    for root, where in ((know, "know"), (archive, "archive")):
        path = os.path.join(root, f"{cid}.md")
        if not os.path.exists(path):
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read(4096)
        except OSError:
            continue
        m = re.search(r"^status:\s*(\S+)", text, re.M)
        return (m.group(1).strip() if m else "?"), where
    return None, None


def split_head(text):
    """-> (candidate region, trailer).

    Everything from the first `## ` heading that is NOT the cards heading is
    review plumbing (`## Flagged`, `## Dropped`, `## DEFERRED`, ...) and is
    carried through verbatim.  The 2026-07-30 lesson from verify_quotes.py: a
    last candidate that absorbs the file's tail invents findings out of prose.
    """
    for m in SECTION_RE.finditer(text):
        if m.group(1).strip().lower().rstrip(":") != "cards":
            return text[:m.start()], text[m.start():]
    return text, ""


def split_cards(text):
    """-> [(label, title, block_text)] over the candidate region."""
    out, marks = [], list(CARD_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), m.group(2).strip(": ").strip(),
                    text[m.start():end]))
    return out, (marks[0].start() if marks else len(text)), \
        (len(text) if marks else len(text))


def nearest_ids(block):
    m = NEAREST_RE.search(block)
    return CARD_ID_RE.findall(m.group(1)) if m else []


def judge(label, block, know, archive):
    """-> (verdict, message) with verdict in {'OK', 'FLAG', 'DROP'}."""
    rels = RELATION_RE.findall(block)
    if not rels:
        return "DROP", ("no RELATION line — name the card this supersedes / "
                        "supports / contradicts, or say why its nearest is "
                        "unrelated")
    if len(rels) > 1:
        return "DROP", (f"{len(rels)} RELATION lines — exactly one is allowed "
                        f"(merge the candidates instead)")
    payload = rels[0].strip()

    m = SUPERSEDES_RE.match(payload)
    if m:
        cid = m.group(1)
        status, where = card_status(cid, know, archive)
        if status is None:
            return "DROP", f"supersedes {cid} — no such card in know/ or archive"
        if where == "archive":
            return "DROP", (f"supersedes {cid}, which lives in the archive "
                            f"(fog) — a retired card cannot be superseded")
        if status not in SUPERSEDABLE:
            return "DROP", (f"supersedes {cid} whose status is '{status}', not "
                            f"{' or '.join(SUPERSEDABLE)} — an edge onto it "
                            f"chains onto a dead end")
        return "OK", f"supersedes {cid}"

    m = SUPPORTS_RE.match(payload)
    if m:
        kind, cid = m.group(1).lower(), m.group(2)
        status, where = card_status(cid, know, archive)
        if status is None:
            return "DROP", f"{kind} {cid} — no such card in know/ or archive"
        if where == "archive":
            return "FLAG", (f"{kind} {cid} — target is in memory/archive/know "
                            f"(fog); the edge is kept, the review decides")
        return "OK", f"{kind} {cid}"

    m = NEW_RE.match(payload)
    if m:
        cid = m.group(1)
        near = nearest_ids(block)
        if not near:
            return "DROP", ("'new' without a NEAREST line — carry the sweep's "
                            "NEAREST line through, or nothing checks the claim")
        if cid not in near:
            return "DROP", (f"'new' names {cid}, which is not among this "
                            f"candidate's NEAREST ids ({', '.join(near)})")
        status, _where = card_status(cid, know, archive)
        if status is None:
            return "DROP", f"'new' names {cid} — no such card"
        return "OK", f"new (nearest {cid} declared unrelated)"

    return "DROP", (f"malformed RELATION: {payload[:90]!r} — expected "
                    f"'supersedes K-xxxxx BECAUSE <sentence>' | 'supports "
                    f"K-xxxxx' | 'contradicts K-xxxxx' | 'new — nearest "
                    f"K-xxxxx unrelated because <sentence>'")


def append_section(text, heading, blocks):
    """Put blocks at the END of `heading`'s section, creating it if absent."""
    if not blocks:
        return text
    payload = "\n".join(b.rstrip("\n") + "\n" for b in blocks)
    m = re.search(rf"^{re.escape(heading)}\s*$", text, re.M)
    if not m:
        return text.rstrip("\n") + f"\n\n{heading}\n\n" + payload
    nxt = SECTION_RE.search(text, m.end())
    at = nxt.start() if nxt else len(text)
    return (text[:at].rstrip("\n") + "\n\n" + payload
            + ("\n" + text[at:] if nxt else ""))


def process(text, know, archive, maxn=None):
    """-> (new text, report lines, counts dict)."""
    head, trailer = split_head(text)
    cards, first, _last = split_cards(head)
    preamble = head[:first] if cards else head
    report, kept, dropped, deferred, flagged = [], [], [], [], 0

    for label, _title, block in cards:
        verdict, msg = judge(label, block, know, archive)
        if verdict == "DROP":
            report.append(f"{label}: DROP {msg}")
            dropped.append(f"<!-- dropped: {msg} -->\n" + block)
        else:
            if verdict == "FLAG":
                flagged += 1
            report.append(f"{label}: {verdict} {msg}")
            kept.append((label, block))

    if maxn is not None and len(kept) > maxn:
        # THE CAP HOLDS REGARDLESS OF MODEL COMPLIANCE.  The consolidate prompt
        # asks for the ranking (measured > reported > inferred) and for the
        # overflow to go to ## DEFERRED itself; this is what happens when it
        # does not.  File order after the model's ranking IS the ranking.
        for label, block in kept[maxn:]:
            report.append(f"{label}: DEFER over the cap of {maxn}")
            deferred.append(f"<!-- deferred: over the cap of {maxn} -->\n" + block)
        kept = kept[:maxn]

    new_head = preamble + "".join(b for _l, b in kept)
    out = new_head.rstrip("\n") + "\n\n" + trailer.lstrip("\n") if trailer \
        else new_head
    out = append_section(out, DEFERRED_HEAD, deferred)
    out = append_section(out, DROPPED_HEAD, dropped)
    if not out.endswith("\n"):
        out += "\n"
    counts = {"candidates": len(cards), "kept": len(kept),
              "dropped": len(dropped), "deferred": len(deferred),
              "flagged": flagged}
    return out, report, counts


def self_test():
    """Fixture cases against a throwaway know/ — the same shape as
    verify_quotes.py --self-test, so the validator can be checked in one command
    on a machine where the real base is not present."""
    import tempfile
    fails = 0
    with tempfile.TemporaryDirectory() as d:
        know, arch = os.path.join(d, "know"), os.path.join(d, "arch")
        os.makedirs(know)
        os.makedirs(arch)
        for cid, status in (("K-00001", "active"), ("K-00003", "superseded"),
                            ("K-00005", "challenged"), ("K-00006", "refuted")):
            open(os.path.join(know, f"{cid}.md"), "w").write(
                f"---\nid: {cid}\nstatus: {status}\n---\nbody\n")
        open(os.path.join(arch, "K-00004.md"), "w").write(
            "---\nid: K-00004\nstatus: active\n---\nbody\n")
        near = ("NEAREST: K-00001 (1.0), K-00003 (0.5), K-00005 (0.4), "
                "K-00006 (0.3)\n")
        cases = [
            ("supersedes active",
             near + "RELATION: supersedes K-00001 BECAUSE it was mismeasured.",
             "OK"),
            ("supports active", near + "RELATION: supports K-00001", "OK"),
            ("new naming a nearest id",
             near + "RELATION: new — nearest K-00001 unrelated because other "
                    "subsystem.", "OK"),
            # 2026-09-15: the justification may be parenthetical or after a
            # dash/colon — the gate wants the reason, not the word "because".
            ("new, parenthetical justification",
             near + "RELATION: new — nearest K-00001 unrelated (an earlier "
                    "watchdog gotcha list, not a build cycle).", "OK"),
            ("new, dash justification",
             near + "RELATION: new — nearest K-00001 unrelated — other "
                    "subsystem.", "OK"),
            ("new, no justification at all",
             near + "RELATION: new — nearest K-00001 unrelated", "DROP"),
            ("archived target", near + "RELATION: supports K-00004", "FLAG"),
            ("no relation", near + "DECIDED: nothing.", "DROP"),
            ("supersedes a superseded card",
             near + "RELATION: supersedes K-00003 BECAUSE stale.", "DROP"),
            # G2: the target most in need of superseding is a CHALLENGED one.
            ("supersedes a challenged card",
             near + "RELATION: supersedes K-00005 BECAUSE remeasured on a fixed "
                    "rig.", "OK"),
            ("supersedes a refuted card",
             near + "RELATION: supersedes K-00006 BECAUSE stale.", "DROP"),
            ("unknown id", near + "RELATION: supports K-09999", "DROP"),
            ("malformed verb", near + "RELATION: refines K-00001", "DROP"),
            ("new naming a non-nearest id",
             near + "RELATION: new — nearest K-00004 unrelated because x.",
             "DROP"),
            ("two relation lines",
             near + "RELATION: supports K-00001\nRELATION: contradicts K-00001",
             "DROP"),
        ]
        for name, body, want in cases:
            got, msg = judge("C1", f"### C1: t\n{body}\n", know, arch)
            if got != want:
                print(f"SELFTEST FAIL {name}: {got} != {want} ({msg})")
                fails += 1
    print(f"SELFTEST {'OK' if not fails else 'FAIL'} — {len(cases) - fails}"
          f"/{len(cases)} cases")
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cards", nargs="?")
    ap.add_argument("--max", type=int, default=None,
                    help="cap on candidates kept; the overflow goes to "
                         "## DEFERRED in file order")
    ap.add_argument("--know", default=None)
    ap.add_argument("--archive", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="report only, do not rewrite the file")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()
    a.know = a.know or P.KNOW
    a.archive = a.archive or P.ARCHIVE_KNOW
    if a.cards and not a.dry_run and not P.may_write(a.cards):
        P.require_writable(f"rewrite {a.cards}")
    if not a.cards:
        ap.error("cards file required (or use --self-test)")
    try:
        text = open(a.cards, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    out, report, counts = process(text, a.know, a.archive, a.max)
    if not counts["candidates"]:
        print(f"FATAL: no '### C<n>' candidates found in {a.cards}",
              file=sys.stderr)
        return 2

    # The legend is part of the report because the report is read by a human at
    # 04:45 and by nothing else: it has to say what the tool DID to the file.
    print("LEGEND: OK = relation valid · FLAG = valid but the target is in the "
          "archive (fog) · DROP = candidate moved, whole, into "
          f"'{DROPPED_HEAD}' · DEFER = over the cap, moved into "
          f"'{DEFERRED_HEAD}'. Nothing is deleted; the review can move any of "
          "them back.")
    for line in report:
        print(line)
    if not a.dry_run and out != text:
        tmp = a.cards + ".relations.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(out)
        os.replace(tmp, a.cards)
    # A GATE THAT REJECTS EVERYTHING IS REPORTING ON ITSELF, not on the night.
    # Measured 2026-09-04 on a copy of that morning's real inbox (written before
    # this gate existed): 36 of 36 dropped. That is what a prompt the stage did
    # not follow looks like from here, and it must not read as "36 bad
    # candidates" at 04:45. Printed BEFORE the summary on purpose —
    # nightly_distill.sh logs `tail -1` of this report and that line has to stay
    # the counts.
    if counts["dropped"] >= 5 and counts["dropped"] > counts["kept"]:
        print(f"\n!! MASS DROP: {counts['dropped']} of {counts['candidates']} "
              f"candidates carried no valid RELATION. Read this as a PROMPT "
              f"COMPLIANCE failure first, not as {counts['dropped']} bad "
              f"candidates. NOTHING WAS DELETED — every one of them is in "
              f"'{DROPPED_HEAD}' in {a.cards}, whole, with its quotes. To "
              f"rescue the night: move the blocks back under '## Cards', add "
              f"one RELATION line each (that candidate's own NEAREST line names "
              f"the ids), re-run this tool, then convert as usual.")
    print(f"\n{counts['candidates']} candidates · {counts['kept']} kept · "
          f"{counts['dropped']} dropped · {counts['deferred']} deferred · "
          f"{counts['flagged']} flagged")
    return 1 if counts["dropped"] else 0


if __name__ == "__main__":
    sys.exit(main())
