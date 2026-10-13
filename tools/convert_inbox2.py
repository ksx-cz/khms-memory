#!/usr/bin/env python3
"""Turn a nightly/weekly inbox file into approve-ready cards for approve_inbox.py.

Usage:
    convert_inbox2.py memory/inbox/DATE.md [OUT.md] [--date YYYY-MM-DD]
                      [--report FILE] [--quiet]

Exit: 0 = every candidate-shaped heading of the input is a card of the output; 1 = no cards,
a schema problem, or a heading the strict form did not take (its text was absorbed into the card
before it -- NOT approve-ready); 4 = the output is valid, but N candidates stand under a trailer
section (`## Dropped`, `## DROPPED (no valid RELATION)`, `## DEFERRED`, ...) and were not
converted -- each has a `FLAG not converted:` line (report and stderr) and needs a decision (#232).

Default OUT is the input with `.approve.md` instead of `.md`; default `--report`
is OUT with `.report.txt`.  Reads nothing but the input, the tag registry and
the spec's own rules; writes nothing but OUT and the report.  It never assigns
K-ids and never touches `know/` — that is still `approve_inbox.py`'s job, run
after a human has read the report.

WHAT IT SOLVES.  The nightly consolidate stage emits

    ### C1: <title>
    DECIDED: ...
    WHY: ...
    CZ: ...
    **QUOTES:**
    - src=digest :: ...

with no YAML frontmatter at all, while `approve_inbox.py` parses `--- yaml ---`
blocks.  Every morning since 2026-08-17 had closed that gap by hand (a recorded
friction: "the morning review still needs a format converter"), most
recently with a throwaway script carrying a 53-row hand-written metadata table.
The older `convert_inbox.py` does not help: it was written for a format that put
`key: value` meta lines under the heading, so against the current format it
reads the card's own BODY as frontmatter — run on the real 2026-08-27 inbox it
produced 52 cards (it also misses the `C25b` label) whose YAML block is the
prose and whose body is one line.  This tool replaces that path.

WHY A CONVERTER (b) AND NOT A PROMPT CHANGE (a).  The obvious alternative is to
make the consolidate stage emit frontmatter itself.  Rejected, on four measured
grounds:

1.  The metadata is DERIVABLE, so asking a model for it buys nothing and costs
    tokens on the one stage that has already died of them.  §4.2 of the spec
    fixes a body template per type (`SYMPTOM/CAUSE/FIX` = problem→solution,
    `DECIDED/WHY/REJECTED` = decision→rationale, ...), so the body *is* the type
    declaration; `source` follows from the card's own `src=` labels; `level`
    follows from the type.  Measured against the 53 hand-curated rows of the
    2026-08-27 review, the rules below reproduce type 53/53 and source 53/53.
2.  Cost, on the stage least able to pay it.  Frontmatter is ~10 lines per card;
    at 53–120 cards a night that is 500–1,200 extra output lines from a stage
    that hit the 64,000-token output ceiling three times on 2026-08-25 and died
    mid-write on its timeout on 2026-08-24 (rc=124, truncated inbox).  The
    converter's output costs zero model tokens.
3.  Prompt-borne schema has a MEASURED failure rate here.  The consolidate
    prompt already spells out the nested `links:` shape; on 2026-08-25 eight of
    120 cards came back with the key at top level anyway — silently dropped by
    `build_views.py` — and both `convert_inbox.py` and `approve_inbox.py` carry
    repair code for it.  Adding nine more fields to that prompt adds nine more
    fields that can arrive subtly wrong, in a place where wrong looks right.
4.  A model-written field cannot be checked; a derived one can.  `tags` must
    come from the registry, `evidence` must match the card's own VERIFIED line,
    `derived` must carry `derived_from`.  Here those are invariants enforced in
    code (see `selfcheck`), and the tool exits non-zero when it breaks one.

The honest limit of (b) is that two fields are genuinely editorial: `tags` and
`evidence`.  They are derived here too, but every heuristic call is listed in the
report with a `?`, so the morning review reads one table instead of doing regex
archaeology — which is the actual thing that friction was about.

LINKS are the one thing that is NOT derivable: which existing K-card a candidate
supports or contradicts is a judgement the consolidate stage makes and, until
now, buried in `## Flagged` prose.  The consolidate prompt now asks for it as a
machine-readable line in the card body,

    **LINKS:** supports=[K-90001] derived_from=[C12]

which this tool parses into `links:` and strips from the body.  Only the five
spec keys (§4.10) are accepted; anything else is reported and dropped, because
an unknown key is not an error anywhere downstream — `build_views.py` reads only
the five — it is just silently gone.  (That is how the 2026-08-27 review's
`related: [K-90002]` links vanished.)
"""
import argparse
import datetime as dt
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402  (the base: tag registry, approve_inbox beside this file)
import khms_patterns as KP  # noqa: E402  (the base's own tag vocabulary and scope names)

# --- card / body structure -------------------------------------------------

# Temp labels are `C1`, `C12`, `C25b` (the review's own split-card suffix) and the
# weekly's `F1`.  `convert_inbox.py` matched `C\d+` only and dropped C25b.
# 2026-09-23: the consolidating sweep writes the provenance of a merged candidate
# into the heading — `### C1 (from C8):`, `### C10 (from C46+C67+C89):` — and this
# regex required the colon straight after the label, so EVERY card in that night's
# file was invisible and the converter reported "no cards found" on a 58 KB inbox.
# A silent zero is the failure shape that matters here: the review would have been
# recorded as done with nothing approved.  The parenthesis is accepted and dropped;
# the provenance stays readable in the source file.
LABEL = r"[A-Z]\d+[a-z]?(?: *\([^)\n]*\))?"
CARD_RE = re.compile(r"^#{2,3} +([A-Z]\d+[a-z]?)(?: *\([^)\n]*\))?: *(.*?)\n(.*?)"
                     r"(?=^#{2,3} +" + LABEL + r": |\Z)",
                     re.S | re.M)
# Everything from the first of these is review/plumbing prose, not cards.
# `## DEFERRED` and `## DROPPED (no valid RELATION)` are written by
# tools/verify_relations.py, which MOVES candidates there whole rather than
# deleting them (package C2/C1).  They must be invisible here for the same
# reason `## Dropped` is: a candidate the night's own gate rejected must not
# become a card because the converter cannot tell one heading from another.
# Matched case-sensitively by `find`, so the upper-case spellings need their own
# entries — `\n## Dropped` does NOT match `\n## DROPPED (no valid RELATION)`.
TRAILERS = ("\n## Flagged", "\n## Dropped", "\n## Already carded", "\n## SUSPECTED",
            "\n## Condense-confirmed", "\n## Experiments", "\n## STATS", "\n## NEW-TAGS",
            "\n## DEFERRED", "\n## DROPPED",
            # board #209: the review's list of MARK ids the night did NOT card (with the
            # reason) — verdicts for tools/mark_ledger.py, never card text.  Without it here
            # the last card of the file would swallow the whole list as its own body.
            "\n## MARKS NOT CARDED")
QUOTES_MARK = "**QUOTES:**"
SRC_RE = re.compile(r"^\s*-\s*src=([\w-]+)\s*::", re.M)
# a body line the pipeline writes for us, not a claim: `**LINKS:** supports=[K-1] ...`
# The colon is REQUIRED (review FRVCONVB finding 2): the nightly always writes it,
# and a colon-optional pattern took a prose line that merely starts with the word
# (`LINKS between cards are …`) out of the body — and `conserved_lines` skipped the
# same line as plumbing, so the loss was silent.
LINKS_LINE_RE = re.compile(r"^\s*\*{0,2}LINKS:\*{0,2}\s*(.+?)\s*$", re.M)
LINK_KEYS = ("derived_from", "supports", "contradicts", "refuted_by", "supersedes")

# --- RELATION / NEAREST: pipeline plumbing, mapped onto the SAME links channel -
# `NEAREST:` is written by tools/nearest_cards.py (0 tokens, mechanical) and
# `RELATION:` is the one line the consolidate stage must answer it with.  Both
# are addressed to the pipeline, not to a future reader, so both leave the body
# here — but RELATION leaves a graph EDGE behind, which is the whole point:
# an edge that stays in prose is an edge build_views.py never sees (measured
# 2026-08-25, 8 of 120 cards).  RELATION is NOT a second link channel; it is
# folded into the same `links` dict `parse_links()` fills from `**LINKS:**`.
NEAREST_LINE_RE = re.compile(r"^[ \t]*NEAREST:.*$\n?", re.M)
# --- MARKS: the coverage ledger's ids (board #209) ------------------------------
# `MARKS: M-1a2b3c4d M-...` is written by the nightly (tools/mark_ledger.py attribute,
# the MARK stage) and names the journal MARKs a candidate covers.  It becomes the
# card's `marks:` frontmatter list, which is what tells the ledger the mark is
# CARDED — so it is moved out of the body rather than left as prose.  The colon is
# REQUIRED, as for LINKS (review FRVCONVB probe PH: a wrapped prose line `MARKS it
# finds there become verdicts.` was deleted from the body and the audit was silent).
MARKS_LINE_RE = re.compile(r"^[ \t]*\*{0,2}MARKS:\*{0,2}[ \t]*(.*?)[ \t]*$\n?", re.M)
MARK_ID_RE = re.compile(r"\b[MG]-[0-9a-f]{8}\b")   # M- journal MARK, G- generated line (#237)


def parse_marks(prose):
    """-> (prose without MARKS lines, [mark ids in order, unique])."""
    ids = []
    for m in MARKS_LINE_RE.finditer(prose):
        for i in MARK_ID_RE.findall(m.group(1)):
            if i not in ids:
                ids.append(i)
    return MARKS_LINE_RE.sub("", prose), ids

RELATION_LINE_RE = re.compile(
    r"^[ \t]*\*{0,2}RELATION:?\*{0,2}[ \t]*(.+?)[ \t]*$\n?", re.M)
# A RELATION ENTRY is not one physical line (board #209).  From 2026-09-13 on
# the consolidate stage wraps the explanation over 2-4 lines; RELATION_LINE_RE
# alone took the first one, so every continuation stayed in the body as orphan
# prose and a `supersedes` rewrite kept only the first line of its BECAUSE
# (2026-10-03 b04/b05/b06: 52 of 56 cards, 2 truncated SUPERSEDES sentences).
# The entry therefore runs from its RELATION line up to the next FIELD/SECTION
# boundary, which is any of: a blank line, a heading (`#` + space — a wrapped
# line may well start with `#90,`), a list item, a `---` separator, a bold
# UPPER-CASE label WITH its colon (`**LINKS:**`, `**QUOTES:**` — a bold WORD
# such as `**not**` is emphasis that wrapping happened to put first, and
# reading it as a boundary split the entry and leaked the rest, review
# FRVCONV probe C2) or an UPPER-CASE field label of letters only (`CZ:`,
# `MARKS:`, `AGENT:`, `NEAREST:`, `SYMPTOM:`).  Letters only, so a wrapped line
# that opens with a card id and a colon later on (`C5101 found … backends: …`,
# measured in 2026-10-03-b06) or a `K-90001:` stays a continuation; `BECAUSE:`
# is excluded because it belongs to the entry itself.  The class is NOT widened
# to hyphens/digits for labels like `DONE-CRITERIA:` — that would make
# `K-90001:` a boundary again; such a label swallowed into an entry is caught
# by `relation_audit` instead (SWALLOWED, from the opposite end).
RELATION_BOUNDARY_RE = re.compile(
    r"^[ \t]*(?:$|#{1,6}[ \t]|[-*+][ \t]|-{3,}[ \t]*$|>"
    r"|\*\*[A-Z][A-Z _]*:\*\*"
    r"|\*{0,2}(?!BECAUSE:)[A-Z][A-Z _]*[A-Z]:)")
REL_SUPERSEDES_RE = re.compile(
    r"^supersedes\s+(K-\d{5})\b\s*BECAUSE\b\s*:?\s*(\S.*)\Z", re.I | re.S)


def relation_block(text):
    """-> (start, end, [entry lines, stripped]) of the FIRST RELATION entry, or None.

    `start`/`end` delimit the whole entry in `text` (the trailing newline of its
    last line included, exactly as RELATION_LINE_RE's own `$\\n?` did), so a
    one-line entry yields the same span and the same payload as before.  The
    first element of the list is the RELATION line's payload; the rest are its
    continuation lines.  Shared by `parse_relation`, `expected_edges` and
    `relation_audit`, so the three cannot disagree on where an entry ends.
    """
    m = RELATION_LINE_RE.search(text)
    if not m:
        return None
    lines, end = [m.group(1).strip()], m.end()
    if not m.group(0).endswith("\n"):
        return m.start(), end, lines
    while end < len(text):
        nl = text.find("\n", end)
        ln = text[end:] if nl == -1 else text[end:nl]
        if RELATION_BOUNDARY_RE.match(ln):
            break
        lines.append(ln.strip())
        end = len(text) if nl == -1 else nl + 1
    return m.start(), end, lines
REL_EDGE_RE = re.compile(r"^(supports|contradicts)\s+(K-\d{5})\b", re.I)
REL_NEW_RE = re.compile(r"^new\b", re.I)
# meta lines of the LEGACY inbox format (`type: fact` directly under the heading)
META_LINE_RE = re.compile(r"^([a-z][a-z0-9_]*):\s*(.*)$")
META_KEYS = ("id", "type", "level", "status", "tags", "scope", "evidence", "source",
             "date", "links") + LINK_KEYS

# --- shape 4: the 2026-09-26+ nightly's single-line compound annotation ----
# `type: X · level: Y · evidence: Z · scope: S · tags: [...] · date: D ·
# source: SRC` on ONE line right under the heading, replacing the per-key
# lines of shapes 1-3.  Unhandled, this line still matches `_is_meta_line` on
# its FIRST field (`type:`) and META_LINE_RE's `(.*)$` swallows the rest of
# the line whole — so `level`, `evidence`, `scope`, `tags`, `date` and
# `source` all vanish into the `type:` value and every one of them is then
# guessed by heuristic instead of read from what the author declared
# (the reason this converter is deterministic at all; a heading regex that
# silently matched nothing was the same "converts clean but loses the field" shape). Fixed by recognising this ONE line and exploding
# it back into one `key: value` line per field, so it re-enters the SAME
# per-line meta path shapes 1-3 already use — not a fifth parser.
COMPOUND_KEYS = ("type", "level", "evidence", "scope", "tags", "date", "source")
_COMPOUND_KEY_ALT = "|".join(COMPOUND_KEYS)
# Requires the line to START with `type:` AND contain at least one further
# ` · <key>:` — a bare legacy `type: fact` line (shape 2/3, one key per line)
# has neither and must not be mistaken for this shape.
COMPOUND_LINE_RE = re.compile(
    r"^type:\s*\S.*\s·\s(?:%s):" % _COMPOUND_KEY_ALT)
# Each field runs from its own `<key>:` (at the start, or right after a
# ` · ` separator) up to the NEXT ` · <key>:` or end of line — never up to
# the next bare colon, which is how a `scope: platform:ros2-jazzy` or a
# `source: ...(owner: "...")` value survives intact.
COMPOUND_FIELD_RE = re.compile(
    r"(?:^|\s·\s)(%s):\s*(.*?)(?=\s·\s(?:%s):|$)"
    % (_COMPOUND_KEY_ALT, _COMPOUND_KEY_ALT))


def parse_compound_line(line):
    """-> {key: str|list} if `line` is the shape-4 compound annotation, else None.

    Values are taken VERBATIM off the line, never re-run through
    `yaml.safe_load` here: a `source` value routinely carries its own colon
    (an owner quote in parens) and re-parsing that as a lone YAML scalar
    silently truncates it —
    measured: `yaml.safe_load("session 3d146c5f, investigator #177 (owner: "
    "...)")` returns just `"session 3d146c5f, investigator"`, the rest gone
    without a trace. `tags` is the one structured field, split on its own
    brackets/commas for the same reason.
    """
    if not COMPOUND_LINE_RE.match(line):
        return None
    out = {}
    for key, val in COMPOUND_FIELD_RE.findall(line):
        val = val.strip()
        if key == "tags":
            val = [t.strip() for t in val.strip("[]").split(",") if t.strip()]
        out[key] = val
    return out


def _yaml_line(key, val):
    """One safely-quoted `key: value` line for a value taken verbatim off a
    compound line — built with `yaml.safe_dump`, not an f-string, so a value
    that itself contains a colon, quote or other flow character round-trips
    through the SAME per-line YAML parse `merge_legacy_meta` already applies
    to shapes 1-3, instead of silently breaking it (see `parse_compound_line`).
    `tags` is written as a flat `[a, b, c]` flow list by hand: registry tags
    are plain words/hyphens, always safe unquoted, and this keeps it on one
    line without leaning on `yaml.safe_dump`'s block-list default.
    """
    if key == "tags":
        return "tags: [" + ", ".join(val) + "]"
    return yaml.safe_dump({key: val}, allow_unicode=True, default_flow_style=False,
                          width=4096).rstrip("\n")

# --- §4.2: the body template IS the type -----------------------------------
# Ordered; first match wins.  Each entry is (type, level, required markers).
# `problem→solution` is "a symptom was diagnosed AND the fix verified" (§4.2), so a
# card with SYMPTOM and no FIX is a fact about a symptom, not a solved case — which
# is exactly the call the 2026-08-27 reviewer made by hand on C37 and C41.
TYPE_RULES = [
    ("principle",         "derived",     ("HOLDS",), ("LIMITS", "IMPLICATIONS")),
    ("policy",            "derived",     ("RULE",), ()),
    ("goal→method",       "derived",     ("GOAL", "METHOD"), ()),
    ("overview",          "derived",     ("OVERVIEW",), ()),
    ("requirement",       "observation", ("WHO", "WHAT"), ()),
    ("decision→rationale", "observation", ("DECIDED",), ()),
    ("problem→solution",  "observation", ("SYMPTOM", "FIX"), ()),
    ("problem→solution",  "observation", ("FIX",), ()),
    ("action→outcome",    "observation", ("WHEN", "THEN"), ()),
]
DERIVED_TYPES = {"principle", "policy", "goal→method", "overview"}
# The nine types of spec §4.2, each with the LEVEL its row of that table names:
# `problem→solution` IS an observation, `principle` IS derived.  So `type` and
# `level` are not two independent fields — a pair that disagrees is one fact
# written twice and contradicted, and the only honest move is to refuse it.
LEVEL_OF_TYPE = {t: lvl for t, lvl, *_ in TYPE_RULES}
LEVEL_OF_TYPE["fact"] = "observation"      # §4.2's free-statement type
SPEC_TYPES = set(LEVEL_OF_TYPE)
LEVELS = {"observation", "derived", "assumption"}          # §4.3
# `observation (problem→solution)`: the LEVEL outside, the type inside.  The
# 2026-09-20 nightly wrote all 20 cards in this shape and know/ holds 0 of 2,851
# cards in it, so no tool knows it.  Nothing objected, because build_views.py
# validated `level` and `status` only and its own OBS_TYPES / DER_TYPES sets were
# declared and never read (both fixed 2026-09-20, package TYPEGATE): the compound
# is split HERE, at the source, and rejected by the gate there.
COMPOUND_TYPE_RE = re.compile(r"^([A-Za-z]+)\s*\((.+)\)$")

# --- tag vocabulary --------------------------------------------------------
# Extra surface forms per registry tag: what the material actually SAYS when it
# means that tag.  Registry name + aliases are added automatically in load_tags();
# the extra forms are the base's own vocabulary (its domain words, its language),
# so they live in its patterns file (khms_patterns.py), one form per line:
#     CONVERT_TAG_HINT_<n>=<tag>::<regex>
# Case-sensitive patterns are marked by a leading "(?-i)" — `CAN` the bus vs "can"
# the English verb is the whole reason the list is not just the tag names.  Every
# matching form counts as one hit, so a tag with more forms can score higher.
def _tag_hints():
    out = {}
    for tag, rx in KP.raw_labelled("CONVERT_TAG_HINT_"):
        out.setdefault(tag, []).append(rx)
    return out


TAG_HINTS = _tag_hints()
# A device/subsystem/platform tag says what the card is ABOUT; a process/meta tag
# says how it was arrived at.  When trimming to MAX_TAGS the first kind wins, so a
# card never ends up tagged `testing, dev-process, gotcha, debugging` and nothing else.
GROUP_WEIGHT = {"device": 3.0, "subsystem": 3.0, "platform": 2.5,
                "infra": 2.0, "process": 1.5, "meta": 1.0}
SUBJECT_GROUPS = {"device", "subsystem", "platform", "infra"}
MAX_TAGS = 5
# ...but pure ranking then evicts process/meta tags ENTIRELY, because their weights
# are lower by construction.  Measured on the 2026-08-27 corpus, the five most-missed
# tags against the reviewer's own choices were `gotcha` (28), `dev-process` (11),
# `design` (10), `deployment` (9) and `testing` (8) — every one of them process/meta.
# The reviewer's cards run roughly three subject tags plus two process/meta ones, so
# the slots are split that way rather than contested.
MAX_METHOD_TAGS = 2

# --- scope -----------------------------------------------------------------
# §4.6: one position on the generality axis.  In a base that serves one project nearly
# everything the nightly sees is that project; the exception that recurs is the agent's
# own machinery.  Both scope names and the tags that mean "the agent's machinery" are the
# base's own (patterns file: CONVERT_SCOPE_DEFAULT, CONVERT_SCOPE_AGENT, CONVERT_AGENT_TAGS).
SCOPE_DEFAULT = KP.value("CONVERT_SCOPE_DEFAULT", "universal")
SCOPE_AGENT = KP.value("CONVERT_SCOPE_AGENT", "universal")
AGENT_TAGS = set(KP.words("CONVERT_AGENT_TAGS", ("khms", "llm-tooling")))
PROJECT_TAGS = set(TAG_HINTS) - AGENT_TAGS - {"gotcha", "dev-process", "collaboration",
                                              "debugging", "design", "testing", "git",
                                              "research", "core", "business"}


def load_tags(path=None):
    """-> {tag: (group, [compiled patterns])} from the registry table
    (default: the selected base's memory/views/tags.md).

    The registry is the single source of the vocabulary (§4.5) — a tag this tool
    invents is one `build_views.py` will flag at review, so the output is
    constrained to what is actually in the table, by construction.
    """
    path = path or os.path.join(P.VIEWS, "tags.md")
    out = {}
    for line in open(path, encoding="utf-8"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("tag", "---") or cells[0].startswith("-"):
            continue
        tag, group, aliases = cells[0], cells[1], cells[3]
        forms = [tag] + ([] if aliases == "—" else
                         [a.strip() for a in aliases.split(",") if a.strip()])
        pats = [r"\b%s\b" % re.escape(f).replace(r"\-", "[- ]?") for f in forms]
        pats += TAG_HINTS.get(tag, [])
        compiled = []
        for p in pats:
            if p.startswith("(?-i)"):
                compiled.append(re.compile(p[5:]))
            else:
                compiled.append(re.compile(p, re.I))
        out[tag] = (group, compiled)
    return out


def split_trailers(text):
    for marker in TRAILERS:
        i = text.find(marker)
        if i != -1:
            text = text[:i]
    return text


def _is_meta_line(ln):
    """`tags: [...]` yes, `CZ: …`/`RELATION: …`/prose no — one rule, one place."""
    m = META_LINE_RE.match(ln)
    return bool(m and m.group(1) in META_KEYS)


def split_body(rest):
    """-> (existing meta lines, prose, quotes block text).

    Handles ALL THREE inbox shapes with one code path, which is not a nicety: the
    consolidate stage emitted a different one on each of three consecutive nights —
    2026-08-24 and 08-26 a fenced ```yaml block, 08-25 a bare run of `key: value`
    lines, 08-27 no metadata at all.  A converter written for whichever shape last
    night produced is a converter that breaks tomorrow, which is how we got here.
    """
    # shape 1: fenced ```yaml ... ``` block right under the heading
    fence = re.match(r"\s*```ya?ml\s*\n(.*?)\n```[ \t]*\n?", rest, re.S)
    if fence:
        return fence.group(1).split("\n"), *split_prose(rest[fence.end():])

    lines = rest.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    # shape 4: explode the single compound line back into one `key: value`
    # line per field BEFORE the per-line meta loop below ever sees it, so it
    # is consumed the same way shapes 1-3 already are.
    if i < len(lines):
        compound = parse_compound_line(lines[i])
        if compound is not None:
            synthetic = [_yaml_line(k, v) for k, v in compound.items()]
            lines = lines[:i] + synthetic + lines[i + 1:]
    meta = []
    # only treat the opening run as meta if its FIRST line is a known meta key —
    # `DECIDED: ...` is a body marker, and reading it as frontmatter is precisely
    # what convert_inbox.py did to every card of this format.
    #
    # The run ends at the first line that is NOT a meta line, NOT at the first
    # blank line.  A blank line is how the 2026-08-25 writer happened to separate
    # its meta block from its prose; it is not a rule, and on 2026-09-20 the
    # writer stopped leaving one.  Ending at the blank line then swallowed every
    # card WHOLE — prose, `CZ:`, `RELATION:` and `**LINKS:**` all landed in
    # meta_lines, where `merge_legacy_meta`'s line-by-line fallback drops
    # anything that is not `key: value` WITHOUT A WORD.  All 20 cards of that
    # night lost their claim and all 25 of their edges, and the report said "no
    # schema problems" (fixed together with `edge_audit`).  A body line cannot
    # be mistaken for meta here, because the body's own markers are UPPERCASE
    # (`SYMPTOM:`, `CZ:`, `RELATION:`) and META_LINE_RE only matches lowercase
    # keys from META_KEYS.
    if i < len(lines) and _is_meta_line(lines[i]):
        while i < len(lines) and lines[i].strip():
            if _is_meta_line(lines[i]):
                meta.append(lines[i].rstrip())
                i += 1
            elif meta and lines[i][:1] in (" ", "\t"):
                # a YAML continuation of the key above: a nested
                # `links:\n  supports: [K-1]` block or a `- item` list
                meta.append(lines[i].rstrip())
                i += 1
            else:
                break
    return (meta, *split_prose("\n".join(lines[i:])))


def split_prose(body):
    """-> (prose, quotes block).  The quotes block is carried through verbatim —
    except `NEAREST:`, which the nightly writes LAST, i.e. after `**QUOTES:**`
    (measured in know/ by review FRVCONV: 796 cards carry it, every one after the
    quotes, none before).  `parse_relation` only ever saw the prose, so the line
    went into the card; it is plumbing for the pipeline, not a quote."""
    body = re.sub(r"```(?:markdown|ya?ml)?\n?", "", body)
    head, sep, quotes = body.partition(QUOTES_MARK)
    prose = head.strip()
    quotes = NEAREST_LINE_RE.sub("", (sep + quotes)).strip() if sep else ""
    # inter-card `---` separators would open a bogus YAML block downstream
    prose = re.sub(r"\n?-{3,}\s*$", "", prose).strip()
    quotes = re.sub(r"\n?-{3,}\s*$", "", quotes).strip()
    return prose, quotes


def markers(prose):
    """The ALL-CAPS body markers, in order: `SYMPTOM:` -> 'SYMPTOM'."""
    return [m.group(1) for m in re.finditer(r"^\*{0,2}([A-Z][A-Z\-]{2,}):", prose, re.M)]


def derive_type(prose):
    """-> (type, level, certain?) from the §4.2 body template."""
    have = set(markers(prose))
    for typ, level, need, need_any in TYPE_RULES:
        if not set(need) <= have:
            continue
        if need_any and not (set(need_any) & have):
            continue
        return typ, level, True
    # No template matched: `fact` is the schema's free-statement type, and the
    # nightly's FACT:/GOTCHA: cards land here as intended.
    return "fact", "observation", bool({"FACT", "GOTCHA"} & have)


def _canon_type(raw):
    """`Problem->Solution ` -> `problem→solution`.  Unknown values come back stripped."""
    return re.sub(r"\s*→\s*", "→", str(raw).strip().lower().replace("->", "→"))


def normalise_type(written, body_type, written_level=None):
    """-> (type, level, note).  A §4.2 type and the level it implies — or (None, None, why).

    This NEVER GUESSES.  Three things can be written and only two of them carry a
    type:

      `problem→solution`                 a type -> its level follows (§4.2)
      `observation (problem→solution)`   level and type glued together -> split
      `observation (design finding)`     the inner half is prose, not a type

    The third case drops the prose LOUDLY — the same treatment `parse_links`
    gives a link key outside §4.10 — and falls back to the body template, which
    is the only other §4.2 evidence there is.  It does so ONLY where the two
    agree on the level; where they disagree (2026-09-20's `derived (pattern from
    four observations)` over a body with no derived template marker) no type is
    returned at all, the card keeps the string its writer sent, and `selfcheck`
    refuses the file.  A wrong type is not a cosmetic defect: triage reads it
    (by the winner's type), cards are immutable once approved (§4.4), and this converter is
    the last place where a human can still be asked.
    """
    if written is None:
        typ, note = body_type, ""
    else:
        raw = str(written).strip()
        typ, note = _canon_type(raw), ""
        if typ not in SPEC_TYPES:
            m = COMPOUND_TYPE_RE.match(raw)
            if not m:
                return None, None, (f"'{raw}' is not one of §4.2's nine types and is "
                                    f"not a `level (type)` compound either")
            outer, inner = m.group(1).strip().lower(), _canon_type(m.group(2))
            if outer not in LEVELS:
                return None, None, (f"'{raw}': '{outer}' is neither a §4.2 type nor a "
                                    f"§4.3 level, so the value cannot be split")
            if inner in SPEC_TYPES:
                if LEVEL_OF_TYPE[inner] != outer:
                    return None, None, (
                        f"'{raw}': '{inner}' is a {LEVEL_OF_TYPE[inner]}-level type "
                        f"(§4.2) but the value declares level '{outer}' — the two "
                        f"halves contradict each other, NOT guessed")
                typ = inner
                note = (f"type was written '{raw}' — level and type in one string; "
                        f"type '{inner}', level '{outer}' put where each belongs")
            elif LEVEL_OF_TYPE[body_type] == outer:
                typ = body_type
                note = (f"type was written '{raw}' — '{inner}' is not one of §4.2's "
                        f"nine types, so it was DROPPED and the body template's "
                        f"'{body_type}' used at the declared level '{outer}'")
            else:
                return None, None, (
                    f"'{raw}': '{inner}' is not one of §4.2's nine types, and the body "
                    f"reads as '{body_type}' ({LEVEL_OF_TYPE[body_type]}), which "
                    f"contradicts the declared level '{outer}' — NOT guessed; a human "
                    f"has to name the type (a derived card also needs its template)")
    level = LEVEL_OF_TYPE[typ]
    if written_level and written_level not in (level, "assumption"):
        return None, None, (f"type '{typ}' is a {level}-level type (§4.2) but the card "
                            f"declares level '{written_level}' — NOT guessed")
    if written_level == "assumption":
        # §4.3: a hypothesis not yet evidenced.  Orthogonal to the type's row.
        level = "assumption"
    return typ, level, note


def declared_type(raw):
    """The §4.2 type a written `type:` value names by itself (`fact`, `observation (fact)`),
    or None — a value whose type half is prose is resolved from the body template, which
    is a guess (normalise_type), so it does not count as declared."""
    canon = _canon_type(raw)
    if canon in SPEC_TYPES:
        return canon
    m = COMPOUND_TYPE_RE.match(str(raw).strip())
    inner = _canon_type(m.group(2)) if m else None
    return inner if inner in SPEC_TYPES else None


def derive_evidence(prose, typ):
    """-> (evidence, certain?).  §4.7.

    Calibrated against the 53 hand-set values of the 2026-08-27 review: 49/53.
    An `UNGROUNDED:` line deliberately does NOT downgrade the card — it says one
    claim lacks a source, not that the card is hearsay, and the reviewer kept
    exactly such a card (C6) at `measured`.  It does make the call uncertain.
    """
    have = set(markers(prose))
    if typ in ("decision→rationale", "requirement"):
        # someone stated it; we did not measure it
        return "reported", True
    verified = re.search(r"^\*{0,2}VERIFIED:\s*(\S.*)$", prose, re.M)
    if verified and not re.match(r"(?i)^(none|n/?a|not yet|pending)\b", verified.group(1)):
        return "measured", "UNGROUNDED" not in have
    # An observation with no VERIFIED line is the genuinely editorial case: it is
    # usually still a measurement quoted from a tool run, but it can be hearsay
    # (the reviewer downgraded 2 of these 12 to `reported`).  Default to the
    # common case and MARK IT, rather than guess quietly.
    return "measured", False


def derive_tags(text, registry):
    """-> (tags, certain?).  Registry-constrained by construction (§4.5)."""
    scored = []
    for tag, (group, pats) in registry.items():
        hits = sum(1 for p in pats if p.search(text))
        if hits:
            scored.append((hits * GROUP_WEIGHT.get(group, 1.0), hits, tag, group))
    scored.sort(key=lambda r: (-r[0], -r[1], r[2]))
    subject = [r for r in scored if r[3] in SUBJECT_GROUPS]
    method = [r for r in scored if r[3] not in SUBJECT_GROUPS]
    picked = method[:MAX_METHOD_TAGS]
    picked += subject[:MAX_TAGS - len(picked)]
    # a card with no subject tags at all still gets its full complement
    picked += [r for r in method[MAX_METHOD_TAGS:] if len(picked) < MAX_TAGS]
    tags = [t for _w, _h, t, _g in picked]
    if not tags:
        return ["dev-process"], False
    # "certain" here means only: the top pick is not a lone weak hit.
    return sorted(tags), scored[0][1] > 1 or len(tags) > 2


def derive_scope(tags):
    """-> (scope, certain?).

    Any card touching the agent's own machinery is a coin-flip between
    the agent scope and the project scope — the 2026-08-27 reviewer went one way on
    C46 and the other on C28, on cards this rule cannot tell apart.  So the rule
    picks the majority case and declares itself UNCERTAIN whenever an agent tag is
    present at all, which puts both of those cards in front of review.
    """
    if set(tags) & AGENT_TAGS:
        if not (set(tags) & PROJECT_TAGS):
            return SCOPE_AGENT, False
        return SCOPE_DEFAULT, False
    return SCOPE_DEFAULT, True


def derive_source(quotes, date):
    """-> source string, from the card's OWN src= labels.  §4.8 provenance."""
    srcs = []
    for s in SRC_RE.findall(quotes):
        if s not in srcs:
            srcs.append(s)
    if not srcs:
        return f"inbox {date}"
    order = {"digest": 0, "journal": 1, "gitlog": 2, "headers": 3, "inbox": 4}
    srcs.sort(key=lambda s: (order.get(s, 9), s))
    return f"{'+'.join(srcs)} {date}"


def parse_links(prose):
    """-> (links dict, prose without the LINKS line, [rejected keys])."""
    links, rejected = {}, []
    m = LINKS_LINE_RE.search(prose)
    if not m:
        return links, prose, rejected
    for key, vals in re.findall(r"([a-z_]+)\s*[=:]\s*\[([^\]]*)\]", m.group(1)):
        ids = [v.strip() for v in vals.split(",") if v.strip()]
        if key not in LINK_KEYS:
            # Unknown keys are DROPPED LOUDLY.  build_views.py reads only the five
            # of §4.10, so an unknown one is not rejected anywhere downstream — it
            # just never enters the graph, which is how the 2026-08-27 review's
            # `related:` links disappeared without a word.
            rejected.append(f"{key}={ids}")
            continue
        links.setdefault(key, [])
        for i in ids:
            if i not in links[key]:
                links[key].append(i)
    return links, (prose[:m.start()] + prose[m.end():]).strip(), rejected


def parse_relation(prose, links):
    """-> (prose without NEAREST/RELATION plumbing, relation label or '').

    Mutates `links` in place — the SAME dict `parse_links` fills, so there is
    exactly one path from "a relation was named" to `links:` in the frontmatter.

    `supersedes` is the one relation that also has to survive in the BODY: the
    spec (§4.2) and `khms_lint.py` both require a superseding card to read
    `SUPERSEDES K-xxxxx BECAUSE …`, so the RELATION line is REWRITTEN into that
    canonical form in place rather than stripped.  The others carry no meaning a
    reader needs — `supports K-x` says nothing the edge does not — and `new`
    is a statement about the base, not about the world, so they are removed.
    """
    # The entry is located BEFORE the NEAREST lines are stripped, so the writer
    # reads the same entry `relation_audit` reads on the raw card: stripped
    # first, a NEAREST line between the entry and the next prose line vanished
    # and that prose line became a "continuation" deleted with the entry
    # (review FRVCONV finding 1, second shape).
    blk = relation_block(prose)
    if not blk:
        return NEAREST_LINE_RE.sub("", prose).strip(), ""
    start, end, entry = blk
    # the WHOLE entry is the payload (board #209); kept line-broken so a
    # rewritten SUPERSEDES sentence keeps the author's wrapping
    payload = "\n".join(entry)

    sm = REL_SUPERSEDES_RE.match(payload)
    if sm:
        cid, why = sm.group(1), sm.group(2).strip()
        if not links.get("supersedes"):
            links["supersedes"] = cid          # scalar per spec §4.10
        why = why if why.endswith((".", "!", "?")) else why + "."
        prose = prose[:start] + f"SUPERSEDES {cid} BECAUSE: {why}\n" + \
            prose[end:]
        return NEAREST_LINE_RE.sub("", prose).strip(), f"supersedes {cid}"

    em = REL_EDGE_RE.match(payload)
    if em:
        key, cid = em.group(1).lower(), em.group(2)
        vals = links.setdefault(key, [])
        if cid not in vals:
            vals.append(cid)
        prose = prose[:start] + prose[end:]
        return NEAREST_LINE_RE.sub("", prose).strip(), f"{key} {cid}"

    if REL_NEW_RE.match(payload):
        prose = prose[:start] + prose[end:]
        return NEAREST_LINE_RE.sub("", prose).strip(), "new"

    # An unparseable RELATION is left in the body ON PURPOSE — the whole entry,
    # not its first line: dropping it would hide text the morning review has to
    # see, and `verify_relations.py` has already moved genuinely malformed
    # candidates out of the cards section.  `relation_audit` fails the run on
    # it (a RELATION line in a written body), so it is seen, not approved.
    return NEAREST_LINE_RE.sub("", prose).strip(), "?" + " ".join(entry)[:40]


def _put(meta, key, val):
    """One place where an incoming meta key lands, links normalised on the way."""
    if key in LINK_KEYS:
        vals = val if isinstance(val, list) else ([] if val is None else [val])
        cur = meta["links"].setdefault(key, [])
        for v in vals:
            if str(v) not in cur:
                cur.append(str(v))
    elif key == "links" and isinstance(val, dict):
        for k, v in val.items():
            if k in LINK_KEYS:
                _put(meta, k, v)
    elif key == "tags" and val is not None:
        # `tags: network, host-desktop, gotcha` is ONE YAML scalar, not a list —
        # unbracketed commas mean nothing to YAML.  Stored as the string, every
        # consumer that iterates it walks CHARACTERS: the 2026-09-16 inbox
        # produced 439 schema problems of the shape "tag 'o' is not in the
        # registry" because the night's writer began emitting explicit tag lines
        # in this shape.  Split here, where an incoming meta key lands, so the
        # rest of the pipeline only ever sees a list.
        vals = val if isinstance(val, list) else str(val).split(",")
        meta["tags"] = [t for t in (str(v).strip() for v in vals) if t]
    elif key != "id" and val is not None:
        meta[key] = val


def merge_legacy_meta(meta_lines, meta):
    """Fold an existing meta block (fenced ```yaml or bare lines) into the dict.

    Explicit beats derived for everything EXCEPT link keys, which are merged —
    and a link written at top level (`supports: [K-1]`) is nested here, since
    that shape is silently dropped by build_views.py (measured: 8 of 120 cards,
    2026-08-25).

    The block is YAML, so it is parsed AS YAML first.  Line-by-line parsing is
    the fallback, not the primary path: it cannot see a nested
    `links:\\n  derived_from: [C17, C18, C19]` block (the indented line matches no
    top-level key), which on the 2026-08-24 inbox turned three fully-linked
    `principle` cards into cards that would hard-fail `build_views.py`.  But it
    IS needed as a fallback, because an unquoted colon inside a value makes the
    whole block unparseable — the 2026-08-05 failure that silently dropped 12 of
    69 cards — and one bad line must not cost the other nine.
    """
    block = "\n".join(meta_lines)
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, dict):
        for key, val in parsed.items():
            _put(meta, key, val)
        return meta

    for ln in meta_lines:
        m = META_LINE_RE.match(ln)
        if not m:
            continue
        key, raw = m.group(1), m.group(2).strip()
        try:
            val = yaml.safe_load(raw) if raw else None
        except yaml.YAMLError:
            val = raw            # e.g. an unquoted value with a colon in it
        _put(meta, key, val)
    return meta


def convert(text, date, registry):
    """-> (list of (meta, title, body), list of report rows)."""
    cards, rows = [], []
    for m in CARD_RE.finditer(split_trailers(text)):
        cid, title, rest = m.group(1), m.group(2).strip(), m.group(3)
        meta_lines, prose, quotes = split_body(rest)
        links, prose, rejected = parse_links(prose)
        prose, relation = parse_relation(prose, links)
        prose, mark_ids = parse_marks(prose)
        typ, level, t_ok = derive_type(prose)
        ev, e_ok = derive_evidence(prose, typ)
        tags, tag_ok = derive_tags(f"{title}\n{prose}\n{quotes}", registry)
        scope, s_ok = derive_scope(tags)
        meta = {
            "id": cid,
            "type": typ,
            "level": level,
            "status": "active",
            "tags": tags,
            "scope": scope,
            "evidence": ev,
            "source": derive_source(quotes, date),
            "date": date,
            "links": links,
        }
        if mark_ids:
            meta["marks"] = mark_ids
        written = merge_legacy_meta(meta_lines, {"links": {}}) if meta_lines else {}
        if meta_lines:
            meta = merge_legacy_meta(meta_lines, meta)
        # The writer's `type:` is read through §4.2 rather than copied: the
        # 2026-09-20 nightly's `observation (problem→solution)` is a level and a
        # type in one string, and a string no tool in this base knows.
        resolved, r_level, t_note = normalise_type(written.get("type"), typ,
                                                   written.get("level"))
        if resolved:
            meta["type"], meta["level"] = resolved, r_level
        # A field the writer DECLARED is not a guess (#237). The t/e/g/s calls above were
        # made from the prose before the card's own lines were merged, so on 2026-10-13-2
        # 24 of 31 candidates carried flags for type/evidence/tags they had written out.
        # And a scope nobody wrote was derived from the GUESSED tags, not the declared
        # ones: a card declaring `tags: [khms, …]` got the project scope with no `s`.
        declared = {k for k in ("type", "evidence", "tags", "scope")
                    if written.get(k) not in (None, "", [])}
        t_ok = t_ok or ("type" in declared and resolved == declared_type(written["type"]))
        e_ok = e_ok or "evidence" in declared
        tag_ok = tag_ok or "tags" in declared
        if "scope" in declared:
            s_ok = True
        elif "tags" in declared:
            meta["scope"], s_ok = derive_scope(meta["tags"])
        # §4.7 puts `evidence` on OBSERVATION cards only, and the corpus agrees:
        # 86 of 121 derived cards in know/ carry none. Derived cards MUST instead
        # carry derived_from (§4.3) — build_views.py hard-fails on an empty one,
        # so the self-check says so here rather than at approve time.
        if meta["type"] in DERIVED_TYPES and meta["level"] != "assumption":
            meta["level"] = "derived"
            meta.pop("evidence", None)
            # a principle's reach is exactly the editorial question the body cannot
            # answer — a principle may hold `universal`ly, this tool's default does not
            s_ok = s_ok and "scope" in declared
        body = (prose + ("\n" + quotes if quotes else "")).strip()
        cards.append((meta, title, body))
        notes = []
        if resolved and t_note:
            notes.append(t_note)
        if not resolved:
            notes.append(f"TYPE LEFT AS WRITTEN, this file is NOT approve-ready: "
                         f"{t_note}")
        flags = "".join([
            "" if t_ok else "t", "" if e_ok else "e", "" if tag_ok else "g",
            "" if s_ok else "s", "" if resolved else "T",
            "L" if meta["type"] in DERIVED_TYPES and not meta["links"].get("derived_from")
            else "",
            # R = no RELATION line: the candidate names no existing card at all.
            # verify_relations.py drops those before this point on a nightly
            # file; the flag catches hand-written and legacy inputs.
            "" if relation else "R",
        ])
        rows.append((cid, meta["type"], meta.get("evidence", "—"),
                     ",".join(meta["tags"]), meta["source"], meta["scope"],
                     flags, rejected, relation, notes))
    return cards, rows


# EVERY CANDIDATE IS ACCOUNTED FOR (#232; K-02720, its seventh instance). A LOOSE superset of the card
# heading: any `##`/`###` heading whose first word is label-shaped -- `C1`, `C6A`, `M52031+M53017`, `F1` --
# whatever follows it. Card ids (`### K-00011 …`, triage files) are not candidates and do not match.
# On 2026-10-11 and 2026-10-12 a unit held candidates this converter did not turn into cards -- six with labels
# the strict form does not know (`C6A`), one under the consolidate's own mixed-case `## Dropped` section that
# the night's relation gate had filled -- and the report said `dropped: 0`, rc 0; the reviewers found them by
# counting headings by hand. Now every candidate-shaped heading is either a converted card or a FLAG line.
CANDIDATE_RE = re.compile(r"^(#{2,3}) +([A-Z][A-Za-z]?\d+[A-Za-z0-9]*(?:\+[A-Z][A-Za-z]?\d+[A-Za-z0-9]*)*)\b",
                          re.M)
SECTION_HEAD_RE = re.compile(r"^## +(.+?)\s*$", re.M)


def census(text):
    """-> (n candidate headings, [(label, where, why)] for each one NOT converted).

    `where` is the section heading it stands in; `why` is `absorbed into <label>` for a heading inside the
    cards region that the strict form did not take (its text became part of the card before it -- the output
    is wrong) or `withheld` for one under a trailer section (`## Dropped`, `## DROPPED (no valid RELATION)`,
    `## DEFERRED`, ...: the output is right, a decision about the candidate is owed)."""
    region = split_trailers(text)
    converted = {m.start() for m in CARD_RE.finditer(region)}
    heads = list(CANDIDATE_RE.finditer(text))
    sections = [h for h in SECTION_HEAD_RE.finditer(text) if not CANDIDATE_RE.match(text, h.start())]
    missed = []
    for m in heads:
        if m.start() in converted:
            continue
        secs = [h for h in sections if h.start() < m.start()]
        where = "## " + secs[-1].group(1) if secs else "(no section)"
        if m.start() < len(region):
            prev = [c for c in CARD_RE.finditer(region) if c.start() < m.start()]
            why = f"absorbed into {prev[-1].group(1)}" if prev else "absorbed into no card (lost)"
        else:
            why = "withheld"
        missed.append((m.group(2), where, why))
    return len(heads), missed


def count_withheld(text):
    """-> (candidates under a DEFERRED section, candidates under a DROPPED section), in ANY spelling.

    `## Dropped` (the consolidate's own section, where the night's gate also puts its drops) counted as 0
    until #232: only the upper-case `## DROPPED` was searched, so a gate-dropped candidate under the mixed-case
    heading was reported as `dropped: 0` (2026-10-12, unit m07-b01)."""
    _n, missed = census(text)
    deferred = sum(1 for _l, where, why in missed if why == "withheld" and where.lower().startswith("## deferred"))
    dropped = sum(1 for _l, where, why in missed if why == "withheld" and where.lower().startswith("## dropped"))
    return deferred, dropped


def census_lines(text, n_cards):
    """-> (report lines, n absorbed, n withheld)."""
    n_heads, missed = census(text)
    lines = [f"candidate headings: {n_heads}, converted: {n_cards}, not converted: {len(missed)}"]
    for label, where, why in missed:
        lines.append(f"FLAG not converted: {label} under '{where}' -- {why}"
                     + (" (the card before it now holds its text: NOT approve-ready)" if why.startswith("absorbed")
                        else " (move it under ## Cards with a valid RELATION line, or leave it with a reason)"))
    n_abs = sum(1 for m in missed if m[2].startswith("absorbed"))
    return lines, n_abs, len(missed) - n_abs


def expected_edges(rest):
    """-> {link key: [ids]} a card's RAW text names, read with THIS module's regexes.

    The input half of `edge_audit`.  It is a second READING of the source, not a
    second parser: it uses `LINKS_LINE_RE`, `RELATION_LINE_RE` and the `REL_*`
    patterns `parse_links`/`parse_relation` use, so it cannot drift from what
    they accept — but it reads the untouched card text, before any stage of the
    pipeline has had the chance to drop a line.
    """
    want = {}
    m = LINKS_LINE_RE.search(rest)
    if m:
        for key, vals in re.findall(r"([a-z_]+)\s*[=:]\s*\[([^\]]*)\]", m.group(1)):
            if key in LINK_KEYS:
                for i in (v.strip() for v in vals.split(",")):
                    if i and i not in want.setdefault(key, []):
                        want[key].append(i)
    blk = relation_block(rest)
    if blk:
        payload = "\n".join(blk[2])
        sm = REL_SUPERSEDES_RE.match(payload)
        em = REL_EDGE_RE.match(payload)
        key, cid = ("supersedes", sm.group(1)) if sm else \
                   ((em.group(1).lower(), em.group(2)) if em else (None, None))
        # the same edge named on BOTH lines is one edge, not two (the nightly
        # writes `RELATION: supports K-x` and `**LINKS:** supports=[K-x]`)
        if key and cid not in want.setdefault(key, []):
            want[key].append(cid)
    return want


def edge_audit(src_text, cards):
    """-> (problems, edges checked).  Every edge the SOURCE names must be IN the card.

    The two halves are read from opposite ends of the tool — the left from the
    raw inbox text, the right from the meta dict about to be written — so a
    stage that loses a line between them cannot satisfy the check by losing it
    on both sides.  That is precisely how 2026-09-20 passed: twelve
    `RELATION: supports K-…` lines and twenty `**LINKS:**` lines reached no
    `links:` at all, every card also lost its prose, and the report still said
    "no schema problems" (the one visible trace, the `R` flag on all twenty, was
    produced BY the loss — a check that passes whether or not the thing is healthy).

    The count is returned so the report can state what was verified: an audit
    over a file that names no edges is not a pass, and says so.
    """
    problems, checked = [], 0
    raws = [m.group(3) for m in CARD_RE.finditer(split_trailers(src_text))]
    if len(raws) != len(cards):
        return ([f"edge audit: {len(raws)} source cards vs {len(cards)} converted "
                 f"— the audit cannot be trusted"], 0)
    for rest, (meta, _title, _body) in zip(raws, cards):
        links = meta.get("links") or {}
        for key, ids in expected_edges(rest).items():
            have = links.get(key)
            have = [have] if isinstance(have, str) else list(have or [])
            for i in ids:
                checked += 1
                if i not in have:
                    problems.append(
                        f"{meta.get('id', '?')}: the source names {key} {i}, the "
                        f"converted card does not (links.{key} = "
                        f"{have or 'absent'})")
    return problems, checked


# --- relation_audit's own readers (review FRVCONV, findings 1 and 4) -----------
# A line that LOOKS like a field label, in a class deliberately WIDER than the
# boundary rule's: any capital-initial run of up to 31 letters, digits, spaces and
# `_/()-` ending in a colon followed by whitespace or end of line, bold or not
# (`DONE-CRITERIA:`, `**Done criteria:**`, `Q1:`, `WHY (owner):`, `DONE-criteria:`,
# `FIX/VERIFIED:`).  It is never a boundary — only an alarm: such a line inside a
# RELATION entry was swallowed by it.  The asymmetry sets the width (review
# FRVCONVB finding 1): a false alarm is one loud line in the morning report, a
# miss is a silent deletion.  The exclusions are NAMED, not implied by a narrower
# class: a card / candidate id (`K-90001:`, `C5101 …`) and `BECAUSE:`, which are
# continuation text.  Measured on 2026-10-03 over every continuation line of every
# raw inbox unit: 0 hits; a lower-case-initial variant had 1 false hit, hence `[A-Z]`.
LABEL_LIKE_RE = re.compile(r"^[ \t]*\*{0,2}([A-Z][A-Za-z0-9 _/()-]{0,30}):(?:\*\*)?(?:\s|$)")
LABEL_NOT_A_LABEL_ID_RE = re.compile(r"[A-Z]-?\d{3,}")
# A pipeline field line: addressed to the converter, never to a reader, so it
# must not be in ANY written body, prose or quotes.  It names exactly what the
# writer strips: `NEAREST:`, `MARKS:`, `LINKS:` with their colon (a prose line
# that starts with the word is prose and stays), and `RELATION` with or without
# it, because RELATION_LINE_RE takes both and an unparseable one stays in the body.
PLUMBING_LINE_RE = re.compile(
    r"^[ \t]*\*{0,2}(?:RELATION\b|(?:NEAREST|MARKS|LINKS):)")
FENCE_TOKEN_RE = re.compile(r"```(?:markdown|ya?ml)?")


def _is_label_like(ln):
    m = LABEL_LIKE_RE.match(ln)
    return bool(m and not LABEL_NOT_A_LABEL_ID_RE.match(m.group(1))
                and m.group(1).strip() != "BECAUSE")


def conserved_lines(rest, span):
    """-> [(raw line, stripped text)] of one RAW card that must reach its body.

    Everything except: the RELATION entry `span` (start, end offsets in `rest`),
    the leading meta block (a ```yaml fence, or a run of lowercase `key: value`
    lines / the compound line with their indented continuations), and plumbing —
    blank lines, `---`, fence lines, `**LINKS:**`, `NEAREST:`, `MARKS:`.
    Written as its own walk over the raw text, not by calling `split_body` /
    `relation_block`'s boundary: the point of conservation is that it holds
    whatever rule decided where an entry or a meta block ends.
    """
    out, pos, lines, i = [], 0, rest.split("\n"), 0
    offs = []
    for ln in lines:
        offs.append(pos)
        pos += len(ln) + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and re.match(r"[ \t]*```ya?ml[ \t]*$", lines[i]):
        i += 1
        while i < len(lines) and not re.match(r"[ \t]*```[ \t]*$", lines[i]):
            i += 1
        i += 1
    elif i < len(lines) and (_is_meta_line(lines[i])
                             or parse_compound_line(lines[i]) is not None):
        while i < len(lines) and lines[i].strip() and (
                _is_meta_line(lines[i]) or lines[i][:1] in (" ", "\t")):
            i += 1
    for k in range(i, len(lines)):
        ln = lines[k]
        if span and span[0] <= offs[k] < span[1]:
            continue
        text = FENCE_TOKEN_RE.sub("", ln).strip()
        if (not text or re.fullmatch(r"-{3,}", text) or LINKS_LINE_RE.match(ln)
                or NEAREST_LINE_RE.match(ln) or MARKS_LINE_RE.match(ln)):
            continue
        out.append((ln, text))
    return out


def relation_audit(src_text, cards):
    """-> (problems, entries checked).  No RELATION text may be split or truncated,
    no source line lost, no pipeline line written.

    Read from opposite ends like `edge_audit`: the left half is the RAW source
    card, the right half is the body about to be written.  Board #209: from
    2026-09-13 on, wrapped entries left their continuation lines in the body as
    orphan prose and cut a `SUPERSEDES … BECAUSE:` sentence after its first
    line, and nothing in the report said so.  Failures named here:
      * LEAKED — a continuation line of a stripped entry (supports /
        contradicts / new) is still a body line;
      * TRUNCATED — a supersedes entry's full BECAUSE text is not in the body
        as one SUPERSEDES sentence (compared whitespace-normalised);
      * UNREAD — a card carries a second RELATION line the converter never
        takes (only the first entry is parsed) — loud rather than silent;
      * SWALLOWED — a line inside the entry looks like a field label
        (`DONE-CRITERIA:` …): the boundary rule did not see it, so it was
        deleted or rewritten with the entry (review FRVCONV probe C1);
      * LOST — conservation: a raw line outside the entry and outside plumbing
        (`conserved_lines`) is not a line of the written body.  This direction
        does not depend on the boundary rule at all, which is what makes the
        audit see a swallow, not only a leak (a check must be able to fail);
      * PLUMBING — a `RELATION`/`NEAREST`/`MARKS`/`LINKS` line is in the
        written body, prose or quotes (probe C3: NEAREST after `**QUOTES:**`).
    """
    problems, checked = [], 0
    raws = [m.group(3) for m in CARD_RE.finditer(split_trailers(src_text))]
    if len(raws) != len(cards):
        return ([f"relation audit: {len(raws)} source cards vs {len(cards)} "
                 f"converted — the audit cannot be trusted"], 0)
    norm = lambda s: " ".join(s.split())  # noqa: E731
    for rest, (meta, _title, body) in zip(raws, cards):
        cid = meta.get("id", "?")
        body_lines = {ln.strip() for ln in body.split("\n")}
        for ln in body.split("\n"):
            if PLUMBING_LINE_RE.match(ln):
                problems.append(f"{cid}: pipeline field line (PLUMBING) in the "
                                f"written body: {ln.strip()[:60]!r}")
        n_rel = len(RELATION_LINE_RE.findall(rest))
        if n_rel > 1:
            problems.append(f"{cid}: {n_rel} RELATION lines in the source, only the "
                            f"first is parsed — the rest would stay in the body")
        blk = relation_block(rest)
        lost = [t for _ln, t in conserved_lines(rest, blk[:2] if blk else None)
                if t not in body_lines]
        if lost:
            problems.append(f"{cid}: {len(lost)} source line(s) LOST — not in the "
                            f"written body, first: {lost[0][:60]!r}")
        if not blk:
            continue
        checked += 1
        entry = blk[2]
        swallowed = [ln for ln in entry[1:] if _is_label_like(ln)]
        if swallowed:
            problems.append(f"{cid}: a field line was SWALLOWED into the RELATION "
                            f"entry (the boundary rule does not know its label): "
                            f"{swallowed[0][:60]!r}")
        payload = "\n".join(entry)
        sm = REL_SUPERSEDES_RE.match(payload)
        if sm:
            why = sm.group(2).strip()
            if not norm(f"SUPERSEDES {sm.group(1)} BECAUSE: {why}") in norm(body):
                problems.append(f"{cid}: SUPERSEDES sentence TRUNCATED — the body "
                                f"does not carry the source's whole BECAUSE "
                                f"({len(entry)} line(s): {norm(why)[:60]}…)")
            continue
        if not (REL_EDGE_RE.match(payload) or REL_NEW_RE.match(payload)):
            continue        # unparseable: left in the body whole (PLUMBING above)
        leaked = [ln for ln in entry[1:] if ln and ln in body_lines]
        if leaked:
            problems.append(f"{cid}: {len(leaked)} RELATION continuation line(s) "
                            f"LEAKED into the body, first: {leaked[0][:60]!r}")
    return problems, checked


def render(cards):
    out = []
    for meta, title, body in cards:
        # Always dumped by the YAML writer, never hand-quoted: this is what kills
        # the 2026-08-05 class for good (a colon inside `source:` silently dropped
        # 12 of 69 cards, and the run still exited 0).
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True,
                               default_flow_style=None, width=1000).strip()
        out.append(f"---\n{front}\n---\n{title}\n\n{body}\n")
    return "\n".join(out)


def selfcheck(path, expected, registry):
    """Re-parse the output with approve_inbox.py's OWN parser and validate it.

    The point is that this is the parser that will actually read the file, not a
    reimplementation of it: a check that passes for reasons unrelated to the
    thing it guards is worth nothing (K-90585).  Returns a list of problems.
    """
    import approve_inbox
    import build_views

    skipped, problems = [], []
    parsed = approve_inbox.parse_cards(open(path, encoding="utf-8").read(), skipped)
    if len(parsed) != expected:
        problems.append(f"parse count {len(parsed)} != {expected} cards converted")
    for cid, err in skipped:
        problems.append(f"{cid}: approve_inbox could not parse it: {err}")
    for meta, body in parsed:
        cid = meta.get("id", "?")
        # The per-card gate build_views.py applies at the door — required keys,
        # level/status, §4.2's closed type set and its type/level pair, derived
        # without derived_from, observation without evidence — from build_views
        # itself, not a copy (package PAPPR, #209); approve_inbox runs the same
        # function before it allocates an id.  New cards: no legacy exception.
        problems += [f"{cid}: {p}" for p in build_views.card_problems(meta)[0]]
        # What the converter demands ON TOP of the door: its output always has an
        # id and a links block, and an observation names its source and evidence.
        for key in ("id", "links"):
            if key not in meta:
                problems.append(f"{cid}: missing '{key}'")
        if meta.get("level") == "observation":
            if not meta.get("source"):
                problems.append(f"{cid}: observation without 'source'")
            if "evidence" in meta and not meta.get("evidence"):
                problems.append(f"{cid}: observation with an empty 'evidence'")
        for t in meta.get("tags") or []:
            if t not in registry:
                problems.append(f"{cid}: tag '{t}' is not in the registry")
        if not (meta.get("tags") or []):
            problems.append(f"{cid}: no tags")
        if not body.strip():
            problems.append(f"{cid}: empty body")
        # ...and a body that is ONLY the title and the quotes block is empty in
        # every sense that matters: the card's own claim is what a reader reads
        # and what `derive_type`/`derive_evidence` grade.  The plain `not body`
        # test above cannot see that loss, because the title is always there —
        # which is why 2026-09-20 converted twenty claim-less cards green.
        claim = body.split("\n", 1)[1] if "\n" in body else ""
        claim = NEAREST_LINE_RE.sub("", claim.split(QUOTES_MARK)[0]).strip()
        if body.strip() and not claim:
            problems.append(f"{cid}: body is the title and the quotes block only "
                            f"— the card's own prose did not survive conversion")
    return problems, parsed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--date", help="knowledge date (§4.9); default = the inbox "
                                   "file's date minus one day, which is the day "
                                   "a 03:30 nightly actually distilled")
    ap.add_argument("--report", help="where to write the review table "
                                     "(default: alongside dst)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    dst = a.dst or re.sub(r"\.md$", "", a.src) + ".approve.md"
    report_path = a.report or re.sub(r"\.md$", "", dst) + ".report.txt"
    # The converter is a READER that writes two files beside its input by default. On a reader deployment
    # (KHMS_READ_ONLY=1) both must lie outside the base, its configuration and its shared state -- checked
    # before anything is written; a conversion into a detached directory stays allowed.
    P.require_may_write([dst, report_path], "write the converted inbox file and its report")

    date = a.date
    if not date:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(a.src))
        if not m:
            print("cannot infer --date from the filename; pass --date", file=sys.stderr)
            return 2
        date = str(dt.date.fromisoformat(m.group(1)) - dt.timedelta(days=1))

    registry = load_tags()
    src_text = open(a.src, encoding="utf-8").read()
    cards, rows = convert(src_text, date, registry)
    if not cards:
        print(f"no cards found in {a.src}", file=sys.stderr)
        c_lines, _a, _w = census_lines(src_text, 0)
        for ln in c_lines[1:]:
            print(ln, file=sys.stderr)
        return 1
    open(dst, "w", encoding="utf-8").write(render(cards))

    # What the night's own gate withheld.  Counted from the SOURCE, because the
    # converter stops at those headings and would otherwise report a clean run
    # over a file whose gate rejected half of it — the shape of a check whose
    # passing condition holds whether or not anything happened.
    n_deferred, n_dropped = count_withheld(src_text)
    c_lines, n_absorbed, n_withheld = census_lines(src_text, len(cards))

    lines = [f"convert_inbox2: {a.src} -> {dst}",
             f"{len(cards)} cards, knowledge date {date}",
             f"withheld by the night's own gate — deferred: {n_deferred}, "
             f"dropped: {n_dropped} (see ## DEFERRED / ## DROPPED in {a.src})",
             *c_lines,
             "",
             "flags: t=type guessed (no body template)  e=evidence guessed (no "
             "VERIFIED line)", "       g=tags weak  s=scope ambiguous (agent vs "
             "project)  L=derived card with", "       no derived_from (WILL fail "
             "build_views)  R=no RELATION line",
             "       T=type NOT resolvable to one of §4.2's nine without guessing",
             "",
             f"{'id':6} {'type':19} {'evidence':9} {'flags':6} tags / scope / source"]
    for cid, typ, ev, tags, source, scope, flags, rejected, relation, notes in rows:
        lines.append(f"{cid:6} {typ:19} {ev:9} {flags:6} {tags}")
        lines.append(f"{'':43} {scope}  |  src: {source}")
        if relation:
            lines.append(f"{'':43} rel: {relation}")
        for r in rejected:
            lines.append(f"{'':43} !! link key not in §4.10, DROPPED: {r}")
        for n in notes:
            lines.append(f"{'':43} !! {n}")
    n_flag = sum(1 for r in rows if r[6])
    lines += ["", f"{n_flag} of {len(rows)} cards carry at least one flag — "
                  f"read those rows before approving."]

    problems, parsed = selfcheck(dst, len(cards), registry)
    lines += ["", "SELF-CHECK (approve_inbox.parse_cards over the output):",
              f"  parsed {len(parsed)}/{len(cards)} cards"]
    lines += [f"  FAIL {p}" for p in problems] or ["  no schema problems"]

    lost, checked = edge_audit(src_text, cards)
    lines += ["", "EDGE AUDIT (every link the SOURCE names, present in the card):"]
    if lost:
        lines += [f"  FAIL {p}" for p in lost]
    elif checked:
        lines.append(f"  all {checked} edge(s) named in {a.src} are in the output")
    else:
        lines.append(f"  NOTHING CHECKED — {a.src} names no edge at all "
                     f"(no RELATION and no **LINKS:** line); that is a finding "
                     f"about the night, not a pass")
    problems += lost

    split, n_rel = relation_audit(src_text, cards)
    lines += ["", "RELATION AUDIT (no RELATION entry split, leaked, truncated or "
                  "swallowed; no source line lost; no pipeline line in a body):"]
    if split:
        lines += [f"  FAIL {p}" for p in split]
    else:
        lines.append(f"  all {n_rel} RELATION entr(y/ies) in {a.src} converted "
                     f"whole, every source line conserved")
    problems += split
    problems += [ln for ln in c_lines[1:] if "absorbed" in ln]
    open(report_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    if not a.quiet:
        print("\n".join(lines))
    print(f"\nwrote {len(cards)} cards -> {dst}\nreport -> {report_path}")
    for ln in c_lines[1:]:
        print(ln, file=sys.stderr)
    if problems:
        print(f"!! {len(problems)} schema problem(s) — NOT approve-ready", file=sys.stderr)
        return 1
    if n_withheld:
        # rc 4, not 0: rc 0 means "every candidate of the file is in the output"; the output is right, but
        # a decision about the withheld candidate(s) is owed before the unit counts as reviewed (#232).
        print(f"!! {n_withheld} candidate(s) withheld under a trailer section, not converted — "
              f"see the FLAG lines; the approve file is valid for the rest", file=sys.stderr)
        return 4
    print("next: read the report, fix what it flags, then "
          f"tools/approve_inbox.py {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
