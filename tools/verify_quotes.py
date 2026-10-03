#!/usr/bin/env python3
"""Mechanically verify that candidate cards are grounded in their sources.

WHY THIS EXISTS
---------------
On 2026-07-27 the nightly sweep produced 15 structurally perfect cards whose
factual detail was largely invented: a verification that never ran ("injected
stale condition via kill -STOP"), log strings that appear nowhere in the code,
constants off by 4x, an inverted causal claim, and a fabricated shell command.
The consolidate prompt already asked "is it covered by its cited source?" — but
that stage is handed only the candidate file, never the digest, so the question
was unanswerable by construction. The gate meant to catch fabrication was blind.

Asking a model not to fabricate is a request. This is a check: a claim may only
carry a specific value if the card supplies a verbatim quote containing it, and
that quote must actually occur in the named source file. Both conditions are
greps, so no judgement — and no amount of fluent prose — can satisfy them
without the source really saying so.

FORMAT EXPECTED IN CANDIDATE FILES
----------------------------------
Cards are separated by a `### C<n>` heading. Each card carries a quotes block:

    **QUOTES:**
    - src=journal :: 'deadman_s', 0.4
    - src=digest :: Servo channel 4 initialised on attempt 5

`src=` names a key passed via --source NAME=PATH.

A quote is matched in four classes (see MATCHING below): EXACT, WRAPPED (the
source only wrapped or re-typeset it), ELIDED (the card compressed a passage
with a MARKED "..." and every segment is present, in order) and ABSENT. Only
ABSENT means the source does not say it, and only ABSENT is fatal — measured on
2026-09-02, when all 27 "quote not found" lines of that night were false and 20
of them were merely wrapped or elided.

USAGE
    verify_quotes.py cards.md --source digest=/path/digest.txt \\
                              --source journal=/path/journal.md [--strict]
    verify_quotes.py --self-test        # matcher fixtures; prints SELFTEST OK/FAIL

Exit code 0 if every card passes; 1 if any quote is ABSENT (always fatal)
or, with --strict, if any unsupported specific remains.
"""
import argparse
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

# Temp-label prefix is ANY capital letter, not just C: when a day is too big for one pass the sweep
# chunks its own output and numbers the chunks A1.., B1.., C1.. — on 2026-08-15 that produced 221
# cards across A-G and this regex (hardcoded to C) matched zero of them. A checker that FATALs on
# the naming scheme of a big day is a checker that is absent exactly when it is needed most.
CARD_RE = re.compile(r"^#{2,3}\s+([A-Z]\d+[a-z]?)\b(.*)$", re.M)  # '## Cn:' (sweep) or '### Cn' (consolidate)
QUOTE_RE = re.compile(r"^\s*-\s*src=([\w-]+)\s*::\s*(.+?)\s*$", re.M)

# Specifics a card must not state without a quote backing them.
NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s*(?:ms|s\b|hz|Hz|kHz|Hz\b|%|MB|KB|GB|×|x\b)?")
# A leading slash only starts a PATH when it opens a token. Matching one mid-word turned
# every "A/B" shorthand in mixed-language prose into a phantom file: `time/cost` -> '/cost',
# `traversability/odom` -> '/odom', `with/without` -> '/without', `heartbeat/acknack` ->
# '/acknack'. Same defect as the frontmatter one below — a FRAGMENT of an identifier read as
# a claim. Real paths and topics are untouched: they are preceded by a space, a backtick or a
# line start, so `/robot/odom` is still extracted whole and still has to be grounded.
IDENT_RE = re.compile(r"(?:(?<![\w.])/[\w/]+|\w+\.(?:py|sh|cpp|hpp|yaml|json|service|timer|conf|md)\b|\w+_\w+(?:_\w+)*)")

# Things that are legitimately not from the source: bookkeeping, card plumbing.
IGNORE = re.compile(
    r"^(?:20\d\d(?:-\d\d){0,2}|K-\d{5}|C\d+|TMP-\d+|v?\d(?:\.\d+)*|"
    r"derived_from|supersedes|refuted_by|problem_solution|action_outcome|"
    r"goal_method|decision_rationale|problem→solution|action→outcome|"
    r"goal→method|decision→rationale|project:\w+|platform:[\w-]+|"
    r"\d{5}|[0-9a-f]{6,40})$")   # card-id digits and commit hashes are plumbing, not measurements


def norm(s: str) -> str:
    """Whitespace- and quote-insensitive comparison form.

    Sources are transcripts and code; a quote that differs only by wrapping or
    by a smart quote is still the same quote, and rejecting it would push the
    sweep toward quoting nothing.
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("’", "'").replace("‘", "'").replace("‚", "'")
    s = s.replace("“", '"').replace("”", '"').replace("„", '"')
    s = s.replace("—", "-").replace("–", "-")
    # A decimal separator is a LANGUAGE difference, not a value difference. The
    # journal is Czech and writes 0,15 Hz; the card body is English by KB policy and
    # writes 0.15 Hz. Compared raw, every translated number looked ungrounded against
    # the card's own verbatim quote — a whole false-positive class measured on the
    # 2026-08-20 corpus. Both sides pass through here, so normalising comma-between-
    # digits to a dot is symmetric; a thousands separator normalises identically on
    # both sides, so no real mismatch is hidden.
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    # Markdown emphasis and code ticks are typography, not content: the journal
    # writes **30,0°** where the sweep quotes 30,0°, and both are the same
    # sentence. Same for hyphen runs: an em-dash normalises to "-" above while
    # the git log spells the same dash "--", so runs collapse to one. Both
    # mismatches produced dozens of false FAILs on 2026-08-05 and again on
    # 2026-08-06 before this was patched (the morning reviews traced every one
    # of them to genuinely-present text).
    s = s.replace("**", "").replace("`", "")
    s = re.sub(r"-{2,}", "-", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# MATCHING — four classes, and only one of them means "fabricated"
#
# Measured on the 2026-09-02 nightly: all 27 quotes this file reported as
# "quote not found" were hand-verified afterwards, and 16 of them WERE in the
# named source — the card had compressed a long passage with a marked ellipsis
# ("A... B"), which no contiguous substring can ever match. The sweep stage had
# grep and restored them; a stage that trusted this report would have gutted
# dozens of well-grounded cards. A gate that calls a grounded card fabricated is
# worse than no gate, because the stage downstream is instructed to strip
# exactly what the gate names.
#
# So the answer is not one boolean but four classes, and only the last one keeps
# the old severity:
#   EXACT   — the quote occurs verbatim in the raw source.
#   WRAPPED — it occurs only after normalisation: the source wrapped it across a
#             newline, indented it, doubled a space, or spelled a dash, a quote
#             or a decimal separator differently. Same text, different
#             typesetting — never evidence of invention.
#   ELIDED  — the quote carries a MARKED elision ("...", "…", "[...]", "[…]") or
#             a marked editorial insertion ("now exis[ts]"), and every segment
#             between the marks is found in the source, IN ORDER and without
#             overlap. Order is what keeps this a check instead of a wish: two
#             passages quoted backwards are a montage, not a quote, and stay
#             ABSENT. Bracketed insertions are by convention the card's own
#             words, so they are dropped from the text that may vouch for the
#             card's specifics — an insertion must not become a hiding place.
#   ABSENT  — none of the above. The only class that means "the source does not
#             say this", and the only one that still prints FAIL and exits 1.
# ---------------------------------------------------------------------------

# NFKC (applied by norm) already folds "…" into "...", so one dot form suffices;
# the ellipsis character is kept in the pattern anyway so the matcher does not
# depend on that detail of norm().
MARK_RE = re.compile(r"\[([^\[\]]*)\]|\.{3,}|…")
# A bracketed span longer than this is not an editorial completion, it is a
# claim; it stays literal text and must be found in the source like anything else.
INSERT_MAX = 20
# An elision mark absorbs the punctuation that was used to weld the two halves
# together ("try_push / ...Interprocess mutex"): that slash belongs to the card,
# not to either passage. Digits and letters are never stripped.
SEG_EDGE = " \t\"'/,;:|-.()[]{}<>*→«»"
# Floor under the elision rule. Segments this short carry no evidence — "a... b"
# would match nearly any source, which would turn the marked-elision allowance
# into a way to quote nothing at all.
MIN_SEG = 4
MIN_EVIDENCE = 24


class Source:
    """A source text kept in both forms: raw for EXACT, normalised for the rest."""

    __slots__ = ("raw", "normed")

    def __init__(self, text):
        self.raw = text
        self.normed = norm(text)


def split_marked(nq):
    """Split a NORMALISED quote at its elision/insertion marks.

    Returns (segments, marked): segments are the stretches of the quote that must
    be found in the source; marked says whether any mark was seen at all (an
    unmarked quote must not get the piecewise allowance — that is the difference
    between a compressed quote and a spliced one).
    """
    segs, last, marked = [], 0, False
    for m in MARK_RE.finditer(nq):
        if m.group(0).startswith("["):
            inner = m.group(1).strip()
            if not re.fullmatch(r"\.{2,}|…", inner) and len(inner) > INSERT_MAX:
                continue            # long bracketed span: content, not a mark
        segs.append(nq[last:m.start()])
        last = m.end()
        marked = True
    segs.append(nq[last:])
    return [t for t in (s.strip(SEG_EDGE) for s in segs) if t], marked


def find_pieces(segs, hay, tries=20):
    """Find every segment in `hay`, in order and without overlap.

    Returns (ok, max_gap). The gap is reported because an elision that jumps
    tens of kilobytes has welded together two unrelated passages, and the
    reviewer should see that even when the match is technically valid.

    The first segment is retried at each of its occurrences (bounded), so a
    common opening phrase cannot make a valid quote look absent.
    """
    if not segs:
        return False, -1
    starts, i = [], hay.find(segs[0])
    while i >= 0 and len(starts) < tries:
        starts.append(i)
        i = hay.find(segs[0], i + 1)
    for s0 in starts:
        pos, gap, ok = s0 + len(segs[0]), 0, True
        for seg in segs[1:]:
            j = hay.find(seg, pos)
            if j < 0:
                ok = False
                break
            gap = max(gap, j - pos)
            pos = j + len(seg)
        if ok:
            return True, gap
    return False, -1


def classify(quote, source):
    """Classify one quote against one source.

    Returns (class, detail, vouching_text) where vouching_text is what this quote
    is allowed to vouch for in the unsupported-specifics scan (empty when ABSENT).
    """
    q = quote.strip()
    if not q:
        return "ABSENT", "empty quote", ""
    if q in source.raw:
        return "EXACT", "", q
    # Enclosing quotation marks are the card's punctuation, not the source's —
    # the same class of typography as the ** and ` that norm() already drops.
    nq = norm(q).strip(" \"'")
    if not nq:
        return "ABSENT", "empty quote", ""
    if nq in source.normed:
        return "WRAPPED", "", q
    segs, marked = split_marked(nq)
    if marked and segs:
        if min(len(s) for s in segs) < MIN_SEG or sum(len(s) for s in segs) < MIN_EVIDENCE:
            return "ABSENT", "elided segments too short to carry evidence", ""
        ok, gap = find_pieces(segs, source.normed)
        if ok:
            # The segments vouch, joined by a separator no specific can span:
            # an elision must not manufacture an adjacency the source never had.
            return ("ELIDED",
                    f"{len(segs)} segment{'s' if len(segs) != 1 else ''}, "
                    f"max gap {gap} chars",
                    " || ".join(segs))
    return "ABSENT", "", ""


SELFTEST = [
    ("exact",
     "the port is marked as not ok!",
     "18:04 SHM Port 7455 failure: the port is marked as not ok! -> try_push\n",
     "EXACT"),
    ("wrapped-across-newline-tab-and-double-space",
     "the follower stopped because the goal was behind it",
     "MARK[gotcha] 11:12 the follower stopped\n\tbecause  the goal was  behind it\n",
     "WRAPPED"),
    ("wrapped-typography-and-decimal-comma",
     "30.0° za 5 s",
     "MARK[done] otoceni o **30,0°** za 5 s\n",
     "WRAPPED"),
    ("elided-ellipsis-compression",
     "NOT ONE HISTORICAL ROW CHANGES VERDICT... 14 rows, 0 flips",
     "MARK[done] 09:12 NOT ONE HISTORICAL ROW CHANGES VERDICT: board-geometry-v1, its\n"
     "retest and every floor before it were recomputed with the new numerator and not\n"
     "one of them changes side; 14 rows, 0 flips.\n",
     "ELIDED"),
    ("elided-bracketed-editorial-insertion",
     "A fourth level `NEEDS: unit:<name>` now exis[ts]",
     "a fourth level `NEEDS: unit:<name>` now exists, resolved in ONE ssh\n",
     "ELIDED"),
    ("absent-fabricated",
     "servo channel 4 initialised on attempt 5",
     "MARK[bug] mapa se publikuje pomalu a nikdo si toho nevsiml\n",
     "ABSENT"),
    ("absent-elided-segments-in-wrong-order",
     "test PRIPINAL idealni drahu... metrika zacala polomer pocitat",
     "nejdriv: metrika zacala polomer pocitat. potom padl test, protoze\n"
     "test PRIPINAL idealni drahu na 5.12 m.\n",
     "ABSENT"),
    ("absent-elided-segments-too-short",
     "a... b... c",
     "a b c and a great deal of other text that says nothing in particular\n",
     "ABSENT"),
    ("absent-spliced-tail",
     "mapa se publikuje pomalu a nikdo si toho nevsiml",
     "MARK[bug] mapa se publikuje pomalu\n",
     "ABSENT"),
]


def self_test():
    """Prove the four classes on fixtures, including that ABSENT can still fire.

    A checker whose passing condition holds whether or not the thing it guards is
    healthy carries no weight, so the fixtures deliberately contain the two ways
    this relaxation could go wrong: an out-of-order montage and an elision with
    nothing between the marks.
    """
    fails = 0
    for name, quote, text, want in SELFTEST:
        got, detail, _ = classify(quote, Source(text))
        ok = got == want
        fails += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {name}: want {want}, got {got}"
              f"{' — ' + detail if detail else ''}")
    print(f"\n{len(SELFTEST)} cases · {fails} failed")
    print("SELFTEST OK" if not fails else f"SELFTEST FAIL ({fails} case(s))")
    return 1 if fails else 0


def split_cards(text):
    # The LAST card must not absorb trailer sections (## Flagged, ## Dropped,
    # ## SUSPECTED...) — found 2026-07-30: C23 "owned" the whole file tail and
    # produced 20+ phantom unsupported-specifics. A card ends at the next card
    # heading OR the next top-level section heading, whichever comes first.
    out, marks = [], list(CARD_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        section = re.search(r"^## \S", text[m.end():end], re.M)
        if section:
            end = m.end() + section.start()
        out.append((m.group(1), m.group(2).strip(), text[m.start():end]))
    return out


# A card opens with a run of `key: value` lines (type/level/tags/scope/evidence/
# source/date/links). That block is card PLUMBING written by the pipeline, not a claim
# about the world.
FM_LINE_RE = re.compile(r"^[a-z][a-z0-9_]*:(?:\s|$)")


def split_frontmatter(body):
    """Split a card into (frontmatter, prose-without-frontmatter).

    Scanning the frontmatter for "specifics" turned `source: journal/2026-08-19.md`
    and `scope: robotics/project:example` into the findings '/2026', '19.md' and
    '/project' on nearly every card — 181 of the 359 flags on the 2026-08-20 corpus,
    raised even on cards containing no number at all. A checker that flags 181 items
    nobody can act on is a checker whose real findings are unreadable.

    The frontmatter is returned rather than discarded because it is also an ALLOWLIST:
    a card that repeats its own declared source path or section reference in the prose
    is citing itself, not asserting an unsourced measurement.
    """
    lines = body.splitlines(keepends=True)
    i = 1 if lines and lines[0].lstrip().startswith("#") else 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    start = i
    while i < len(lines) and FM_LINE_RE.match(lines[i]):
        i += 1
    if i == start:
        return "", body
    return "".join(lines[start:i]), "".join(lines[:start] + lines[i:])


def specifics(body):
    """Values a reader would take as measured fact."""
    found = set()
    # Fenced code blocks (```yaml frontmatter etc.) are structure, not prose —
    # and their triple backticks shift the single-backtick pairing for the whole
    # rest of the card (found 2026-07-29: 120 of 182 findings were frontmatter
    # noise while genuinely backticked values went unchecked). Strip them first.
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    body = re.sub(r"```+", " ", body)
    # Anything the card puts in backticks is asserted as literal.
    for lit in re.findall(r"`([^`]+)`", body):
        if not IGNORE.match(lit.strip()):
            found.add(lit.strip())
    for m in NUMBER_RE.finditer(body):
        tok = m.group(0).strip()
        if tok and not IGNORE.match(tok) and not re.fullmatch(r"\d{1,2}", tok):
            found.add(tok)
    for m in IDENT_RE.finditer(body):
        tok = m.group(0).strip()
        if len(tok) > 3 and not IGNORE.match(tok):
            found.add(tok)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cards", nargs="?")
    ap.add_argument("--source", action="append", default=[],
                    metavar="NAME=PATH", help="repeatable")
    ap.add_argument("--strict", action="store_true",
                    help="also fail on specifics not covered by any quote")
    ap.add_argument("--self-test", action="store_true",
                    help="run the matcher fixtures and exit (prints SELFTEST OK/FAIL)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.cards:
        ap.error("cards file required (or use --self-test)")

    sources = {}
    # `src=K-NNNNN` resolves to that card automatically, so the weekly stage —
    # which reasons over existing cards rather than transcripts — can be checked
    # with no extra wiring. It has misattributed a card before (a rule credited to
    # the wrong id), and "card X says Y" is exactly as checkable as any other quote.
    # The card is looked up in the base's know/ and then in its archive.
    for m in re.finditer(r"src=(K-\d{5})\b", open(args.cards, encoding="utf-8",
                                                  errors="replace").read()):
        cid = m.group(1)
        if cid in sources:
            continue
        for root in (P.KNOW, P.ARCHIVE_KNOW):
            path = os.path.join(root, f"{cid}.md")
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as fh:
                    sources[cid] = Source(fh.read())
                break
        else:
            print(f"WARN: quote names card {cid}, which is in neither "
                  f"memory/know nor the archive")

    for spec in args.source:
        name, _, path = spec.partition("=")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                sources[name] = Source(fh.read())
        except OSError as exc:
            # A source that is simply absent (e.g. no git activity that day, so
            # no gitlog) must NOT abort the run: bailing out here would skip the
            # check entirely and silently switch the protection off — the worst
            # possible outcome for a guard against fabrication. Quotes naming a
            # missing source still fail below, which is the correct treatment.
            print(f"WARN: source {name!r} unreadable, quotes citing it will fail: {exc}")
            continue
    if not sources:
        print("FATAL: no readable --source given", file=sys.stderr)
        return 2

    with open(args.cards, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    cards = split_cards(text)
    if not cards:
        print("FATAL: no '### C<n>' cards found — wrong format?", file=sys.stderr)
        return 2

    # The legend is part of the report because the report is READ MECHANICALLY by
    # the consolidate stage, which deletes whatever it names. It has to be able to
    # tell a finding from a pass without knowing this file.
    print("LEGEND: FAIL = the source does not contain the quote (act on it). "
          "WARN = right quote, wrong src label. NOTE = quote verified, "
          "recorded only. Quotes matched verbatim or after line-wrap/typography "
          "normalisation are not printed at all.")

    bad_quotes = unsupported = mislabeled = 0
    n_quotes = n_unknown = n_noquotes = 0
    classes = {"EXACT": 0, "WRAPPED": 0, "ELIDED": 0, "ABSENT": 0}
    for cid, title, body in cards:
        quotes = QUOTE_RE.findall(body)
        if not quotes:
            print(f"{cid}: FAIL no QUOTES block — every card must ground itself")
            bad_quotes += 1
            n_noquotes += 1
            continue

        ok_quotes = []
        for src, quote in quotes:
            n_quotes += 1
            if src not in sources:
                # Public behaviour kept: when the named source is absent, say where the text IS, without
                # vouching for it (the label is still wrong, and that is the finding).
                found_in = [n for n, b in sources.items() if classify(quote, b)[0] != "ABSENT"]
                extra = f" (text IS present in: {', '.join(found_in)})" if found_in else ""
                print(f"{cid}: FAIL quote names unknown source '{src}'{extra}")
                bad_quotes += 1
                n_unknown += 1
                continue
            cls, detail, vouch = classify(quote, sources[src])
            where = src
            if cls == "ABSENT":
                # Before declaring fabrication, look in the OTHER sources: the
                # sweep regularly labels the assistant's Discord prose (which
                # lives in the digest) as src=journal. A mislabel is a real
                # defect worth a line, but it is not an invented quote, and
                # 2026-08-06's review traced 10+ "FAIL not found" to exactly
                # this. The quote still vouches for the card's specifics.
                for name, blob in sources.items():
                    if name == src:
                        continue
                    c2, d2, v2 = classify(quote, blob)
                    if c2 != "ABSENT":
                        cls, detail, vouch, where = c2, d2, v2, name
                        break
                if where != src:
                    print(f"{cid}: WARN quote labeled src={src} actually in "
                          f"{where}: {quote[:70]!r}")
                    mislabeled += 1
            classes[cls] += 1
            if cls == "ABSENT":
                print(f"{cid}: FAIL quote not found in {src}: {quote[:90]!r}"
                      + (f" [{detail}]" if detail else ""))
                bad_quotes += 1
                continue
            if cls == "ELIDED":
                # Printed, not silent: the match is real but the card decided
                # what to leave out, and the gap says how far apart the halves
                # were. The wording says OK on purpose — the consolidate prompt
                # acts mechanically on this report, and a line it cannot tell
                # from a finding would delete grounded material.
                # WRAPPED and EXACT stay silent: nothing happened there.
                print(f"{cid}: NOTE quote OK in {where} — marked elision, every "
                      f"segment verified in order ({detail}): {quote[:90]!r}")
            ok_quotes.append(vouch)

        # Strip the quotes block before scanning the prose, or quotes would
        # trivially vouch for themselves.
        prose = QUOTE_RE.sub("", body)
        prose = re.sub(r"\*\*QUOTES:\*\*", "", prose)
        frontmatter, prose = split_frontmatter(prose)
        # Commit hashes are dropped from the allowlist: `source: gitlog commit
        # 204febd4...` would otherwise silently vouch for a prose "204" that no
        # measurement supports — the allowlist must not become a hiding place.
        allow = re.sub(r"\b[0-9a-f]{7,40}\b", " ", frontmatter)
        haystack = norm(" || ".join(ok_quotes) + " || " + allow)
        missing = sorted(s for s in specifics(prose) if norm(s) not in haystack)
        if missing:
            unsupported += len(missing)
            shown = ", ".join(repr(m) for m in missing[:8])
            more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
            print(f"{cid}: UNSUPPORTED specifics not in any quote: {shown}{more}")

    # The key=value SUMMARY and the class line go ABOVE the summary on purpose: nightly_distill.sh and
    # weekly_synthesis.sh log `tail -1` of this report, and that line has to stay
    # the familiar one-line verdict.
    print(f"\nSUMMARY cards={len(cards)} quotes={n_quotes} unverified={bad_quotes} "
          f"unknown-source={n_unknown} no-quotes={n_noquotes} unbacked-specifics={unsupported}")
    print(f"quote classes: {classes['EXACT']} exact · {classes['WRAPPED']} wrapped "
          f"(matched after whitespace/typography normalisation) · {classes['ELIDED']} elided "
          f"(matched piecewise across a marked elision) · {classes['ABSENT']} absent")
    print(f"{len(cards)} cards · {bad_quotes} unverifiable quotes · "
          f"{mislabeled} mislabeled sources · {unsupported} unsupported specifics")
    if bad_quotes:
        return 1
    if unsupported and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
