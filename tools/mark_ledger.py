#!/usr/bin/env python3
"""mark_ledger.py — the coverage ledger for journal MARKs.

Usage: python3 mark_ledger.py [--root DIR] <subcommand> ...   (`--help` lists them; run by
nightly_distill.sh and approve_inbox.py)

WHY.  A night pipeline with a daily card cap once lost a calibration result: the day overflowed the
cap, the overflow went to a `## DEFERRED` section that nothing ever read again, and nothing said so —
a knowledge item that is neither carded nor consciously dismissed simply stopped existing. The rule
since: no daily card limit at all, everything in the queue is processed, and every tracked MARK is
accounted for.

WHAT.  Every actual journal MARK of any kind except a STATUS kind (below) gets a stable id and ends in
exactly one state:

    CARDED     a card in memory/know/ covers it (card id recorded)
    DISMISSED  the reviewer dismissed it at morning review, with a reason, through this tool
               (`dismiss`)
    OPEN       everything else.  An OPEN mark that is already in an UNREVIEWED inbox proposal or in
               the nightly carry queue is "proposed" and waits for that review; every other OPEN mark
               is in the queue the next night works from.  Nothing leaves OPEN except by a card or a
               recorded dismissal — there is no third way out, and that is the whole point.

The state is DERIVED on every `sync` from these sources, so a killed run can never leave it
inconsistent:
    journal/*.md          the marks themselves (from the floor date on)
    memory/know/*.md      cards — CARDED only through the card's `marks:` FRONTMATTER list (a
                          structured link written by the approve path).  A mention of the id anywhere
                          else, a `journal <date> <HH:MM>` source reference or a verbatim quote only
                          SUGGESTS a card (`suggested` field, `suggest` subcommand); the reviewer turns
                          a suggestion into coverage with `card ID K-x`.  ("This card does NOT cover
                          M-…" once counted as coverage; it no longer can.)
    memory/inbox/*.md     unreviewed proposal UNITS (+ memory/ledger/carry/)
    memory/ledger/events.jsonl   the reviewer's decisions (dismiss / card / reopen) — the only state
                          that cannot be derived; the LAST event of an id wins, and `reopen` really
                          reopens (it also overrides the `marks:` links of the cards that existed when
                          it was recorded)

REVIEW IS PER UNIT, NOT PER DATE.  A unit is one inbox file: the legacy `DATE.md` / `DATE-weekly.md`,
or an immutable published batch `<night>-bNN.md`.  Its `.reviewed-<stem>-cards` marker (in the review
marker directory, KHMS_SHARED_STATE/reviewed) counts only while it still describes the file: it carries
`sha256=<hash of the unit>` (approve writes it), and a legacy marker without a hash counts only when
the unit is not newer than the marker.  Anything appended after a review is unreviewed again.

WHAT THE LEDGER DOES NOT COVER (printed by `sync`/`status`): journals before the floor, MARKs of a status
kind (counted as `status_skipped`) and knowledge that never got a MARK line.  "OPEN 0" therefore means
"every tracked MARK since the floor is carded or dismissed", not "nothing was lost anywhere".

MARK KINDS.  The tracked kinds and their spellings are a table (canonical kind -> aliases).  The
shipped table is English.  A deployment whose journals use other words sets LEDGER_KINDS in its private
patterns file (KHMS_PATTERNS) to `canon:alias,alias;canon:alias,...`, which REPLACES the table.  The
canonical name is part of every mark id (`M-` + sha1 of `<canon>|<text>`), so a deployment keeps its
canonical names for as long as its ledger lives — renaming one re-ids every mark of that kind.

STATUS KINDS (#236, owner 2026-10-13).  A status line (package done, board reply, deployed, checked, ...)
is a plain journal line without MARK.  A MARK of a kind on the status DENYLIST (DEFAULT_STATUS_KINDS, or
LEDGER_STATUS_KINDS in the patterns file) is not tracked; `sync`, summary.json (`status_skipped`) and the
status line say how many such lines were skipped.  Every kind NOT on the denylist stays tracked.

FLOOR.  The first journal day tracked: --floor, else $MARK_LEDGER_FLOOR, else memory/ledger/FLOOR,
else 1970-01-01 (everything).  `floor` prints the one in effect.

Subcommands (all accept --root DIR, the base; default: the resolved KHMS_ROOT):
    sync [--dry]          derive the state, write memory/ledger/{marks.jsonl,
                          summary.json}; --dry writes nothing
    status                one line: OPEN count + oldest OPEN item (hook, review)
    floor                 the floor date in effect
    queue [--out F] [--chunk N --outdir D --prefix P] [--since DATE] [--until DATE]
                          OPEN, not-proposed marks, oldest first
    attribute FILE        add `MARKS:` lines to candidates whose journal quotes
                          match a mark (0 tokens), in place
    coverage FILE --queue Q   how many queue ids FILE accounts for
    scan JOURNAL...       list the tracked marks of the given journal files
    dismiss ID... --reason R | dismiss --from-inbox FILE
    card ID K-xxxxx       record a hand-carded mark (also: accept a suggestion)
    mark-reviewed UNIT --reason R   close one unit's review as it is now (sha256)
    reopen ID             back to OPEN, even against a `marks:` link of a card
                          that exists now (a card written LATER closes it again)
    suggest [--limit N]   OPEN marks with a heuristic card suggestion
    units [--unreviewed]  inbox review units and whether their review holds
    list [--state S] [--limit N]
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import khms_lock as KL
import khms_files as glob
import hashlib
import json
import os
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_messages as M  # noqa: E402
import khms_paths as P  # noqa: E402
import khms_patterns as PT  # noqa: E402

DEFAULT_ROOT = P.soft("ROOT")
# First journal day the ledger tracks when nothing else says (see FLOOR in the docstring).
DEFAULT_FLOOR = "1970-01-01"

# --- which MARKs are knowledge -------------------------------------------------
# Canonical aliases preserve existing IDs; unlisted real kinds are tracked under their
# own names -- except the STATUS kinds below, which are never tracked.
DEFAULT_KINDS = {
    "decision": ("decision", "decided"),
    "finding": ("finding", "found", "solved", "problem"),
    "requirement": ("requirement", "req", "owner", "order", "owner-comment"),
    "gotcha": ("gotcha",),
    "error": ("error", "mistake"),
    "khms-issue": ("khms-issue",),
    "measurement": ("measurement", "measured"),
    "refuted": ("refuted",),
}


def _kind_table():
    raw = PT.value("LEDGER_KINDS", "")
    if not raw:
        return DEFAULT_KINDS
    table = {}
    for item in raw.split(";"):
        if not item.strip():
            continue
        canon, _, aliases = item.partition(":")
        canon = canon.strip()
        table[canon] = tuple(a.strip() for a in aliases.split(",") if a.strip()) or (canon,)
    return table


KIND_ALIASES = _kind_table()
CANON = {alias: canon for canon, aliases in KIND_ALIASES.items() for alias in aliases}

# Status / progress kinds are NOT know-how and are never tracked (#236, owner 2026-10-13: drop the status
# lines and stop producing them): a status line is a plain journal line without MARK.  A DENYLIST, not an
# allowlist: the closed allowlist before #209 A2 lost know-how written under kinds nobody had listed
# (K-05341), so every kind not named here stays tracked.  The default is the status kinds the ledger
# counted on 2026-10-13; a deployment replaces it with LEDGER_STATUS_KINDS in its patterns file (words
# separated by spaces, commas or semicolons; `none` = track every kind).  Matching ignores case, the
# diacritics (an accented spelling equals the plain one) and a `→status` tail.  A skipped status line is COUNTED
# (`status_skipped` in summary.json, the sync output and the status line), never dropped silently (K-00586).
DEFAULT_STATUS_KINDS = (
    "agent-done", "agent-done-check", "agent-fail", "agent-partial", "board-reply", "board-archive",
    "board", "hotovo", "stav", "done", "check", "dispatch", "deploy", "review", "merge", "plan",
    "handoff", "report", "overeno", "potvrzeno", "pending", "resolved", "verified", "khms")


def fold_kind(raw):
    """A MARK kind token as the status denylist compares it: lower-case, no diacritics, no `**`/`` ` ``."""
    k = unicodedata.normalize("NFKD", raw.strip("*`:").lower())
    return "".join(c for c in k if not unicodedata.combining(c))


def _status_table():
    raw = PT.value("LEDGER_STATUS_KINDS", "")
    if not raw.strip():
        return frozenset(fold_kind(k) for k in DEFAULT_STATUS_KINDS)
    if raw.strip().lower() == "none":
        return frozenset()
    return frozenset(fold_kind(w) for w in re.split(r"[\s,;]+", raw) if w.strip())


STATUS_KINDS = _status_table()


def status_kind(raw):
    """-> the (folded) status kind of a MARK token when it is on the status denylist, else None."""
    k = fold_kind(raw).split("→", 1)[0]
    return k if k in STATUS_KINDS else None

# A kind is a word (Czech letters, digits, `-`, `_`, one optional `→status` tail); a
# placeholder in prose (`MARK[...]`, `MARK[<kind>]`, `MARK[..]`) is not a mark.
KIND_SHAPE_RE = re.compile(r"^[^\W\d_][\w-]*(?:→[\w-]+)?$")
KIND_PLACEHOLDERS = frozenset(("kind", "x", "xxx"))


def canon_kind(raw):
    """-> the canonical tracked kind of a MARK token, or None (status kind, prose placeholder)."""
    if status_kind(raw):
        return None
    k = raw.strip("*`:").lower()
    if k in CANON:
        return CANON[k]
    if k in KIND_PLACEHOLDERS or not KIND_SHAPE_RE.match(k):
        return None
    return k

# A MARK line: optional bullet, optional ISO stamp or bare date, optional time,
# optional "(X handoff)" tag, then MARK[kind].  The token must open the line (after
# those prefixes) — prose that merely MENTIONS `MARK[khms-issue]` (rules,
# design notes) is not a mark.  The time may be approximate (#209 A3: `~07:1x`,
# `≤14:08`, `15:0x` and a bare `2026-09-30` prefix were 18 unparsed knowledge lines).
MARK_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?"
    r"(?:(?P<iso>\d{4}-\d{2}-\d{2}T(?P<isot>\d{2}:\d{2})(?::\d{2})?(?:\.\d+)?"
    r"(?:[+-]\d{2}:?\d{2}|Z)?)\s+)?"
    r"(?:\d{4}-\d{2}-\d{2},?\s+)?"
    r"(?:[~≈≤<>]?(?P<hm>\d{1,2}:\d[\dx])(?::\d{2})?\s+)?"
    r"(?:\([^)\n]{0,80}\)\s+)?"
    r"MARK\[(?P<kind>[^\]\s]+)\](?:\*\*)?\s*(?P<text>.*)$")
# Timestamped journal entries may record narrative before their first actual MARK.
# Require a timestamp and a sentence boundary, so prose examples stay excluded.
ENTRY_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?"
    r"(?:\d{4}-\d{2}-\d{2}T(?P<isot>\d{2}:\d{2})(?::\d{2})?"
    r"(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?\s+|"
    r"(?:\d{4}-\d{2}-\d{2},?\s+)?"
    r"[~≈≤<>]?(?P<hm>\d{1,2}:\d[\dx])(?::\d{2})?\s+)")
LEAD_TIME_RE = re.compile(r"^(?P<hm>\d{1,2}:\d{2})(?::\d{2})?\b\s*")
# A LATER MARK token on a line that opens with a MARK (#209 A3): it starts a mark of its
# own only after a sentence end (`. ; ! ? )` or a dash) and outside backticks — a MARK
# named mid-sentence ("earlier MARK[finding] about …", "journal 10-01 MARK[vyvraceno] …") or
# quoted (`MARK[done]`) is a reference, not a mark.
TOKEN_RE = re.compile(r"(?:\*\*)?MARK\[(?P<kind>[^\]\s]+)\](?:\*\*)?:?\s*")
LATER_LEAD_RE = re.compile(r"(?:[.;!?)]|\s[—–-])\s*(?:\*\*)?$")
ID_RE = re.compile(r"\bM-[0-9a-f]{8}\b")
CARD_ID_RE = re.compile(r"\bK-\d{5}\b")
JOURNAL_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
REVIEWED_SUFFIXES = ("", "-cards")
NOT_CARDED_HEAD = "## MARKS NOT CARDED"


# --- paths ---------------------------------------------------------------------
def _review_dir(root):
    """The review-marker directory of `root`: the resolved REVIEW_MARKERS when `root` is the configured
    base, else the default layout beneath `root` (a sandbox base named only by --root)."""
    try:
        if os.path.realpath(P.get("KHMS_ROOT")) == os.path.realpath(root):
            return P.REVIEW_MARKERS
    except P.KhmsNoBaseError:
        pass
    return os.path.join(root, "state", "shared", "reviewed")

class Paths:
    def __init__(self, root, reviewed=None):
        self.root = root
        self.journal = os.path.join(root, "journal")
        self.know = os.path.join(root, "memory", "know")
        self.inbox = os.path.join(root, "memory", "inbox")
        self.reviewed = reviewed or _review_dir(root)
        self.ledger = os.path.join(root, "memory", "ledger")
        self.carry = os.path.join(self.ledger, "carry")
        self.events = os.path.join(self.ledger, "events.jsonl")
        self.marks = os.path.join(self.ledger, "marks.jsonl")
        self.summary = os.path.join(self.ledger, "summary.json")
        self.floor_file = os.path.join(self.ledger, "FLOOR")


def norm(s):
    """Whitespace-collapsed, markdown-emphasis-free, quote-unified text."""
    s = s.replace("**", "").replace("`", "")
    s = s.replace("„", '"').replace("“", '"').replace("”", '"')
    s = s.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip()


def mark_id(kind, text):
    body = LEAD_TIME_RE.sub("", norm(text), count=1)
    h = hashlib.sha1(f"{kind}|{body}".encode("utf-8")).hexdigest()
    return "M-" + h[:8]


def _skip(skipped, raw, text):
    """True when the MARK token `raw` is a status kind; counts it into `skipped` (a dict) when given."""
    sk = status_kind(raw)
    if sk and text and skipped is not None:
        skipped[sk] = skipped.get(sk, 0) + 1
    return bool(sk)


def parse_marks(line, skipped=None):
    """-> [(canonical kind, time 'HH:MM' or '', text), ...] — every tracked mark of a line.
    The FIRST mark's text runs to the end of the line exactly as before #209 A3, so its id
    is unchanged; a later MARK token (after a sentence end, outside backticks) adds a mark
    whose text starts at that token.  A token of a STATUS kind is not a tracked mark: it is
    counted into `skipped` ({status kind: n}) when the caller passes one."""
    m = MARK_RE.match(line)
    if not m:
        entry = ENTRY_RE.match(line)
        if not entry:
            return []
        hm = entry.group("isot") or entry.group("hm") or ""
        if len(hm) == 4:
            hm = "0" + hm
        out = []
        for token in TOKEN_RE.finditer(line, entry.end()):
            before = line[:token.start()]
            if before.count("`") % 2 or not LATER_LEAD_RE.search(before):
                continue
            text = line[token.end():].strip()
            if _skip(skipped, token.group("kind"), text):
                continue
            kind = canon_kind(token.group("kind"))
            if kind and text:
                out.append((kind, hm, text))
        return out
    hm = m.group("isot") or m.group("hm") or ""
    text = m.group("text").strip()
    if not hm:
        lt = LEAD_TIME_RE.match(text)
        if lt:
            hm = lt.group("hm")
    if hm and len(hm) == 4:
        hm = "0" + hm
    out = []
    if not _skip(skipped, m.group("kind"), text):
        kind = canon_kind(m.group("kind"))
        if kind and text:
            out.append((kind, hm, text))
    rest_at = m.start("text")
    for t in TOKEN_RE.finditer(line, rest_at):
        before = line[:t.start()]
        if before.count("`") % 2 or not LATER_LEAD_RE.search(before):
            continue
        t2 = line[t.end():].strip()
        if _skip(skipped, t.group("kind"), t2):
            continue
        k2 = canon_kind(t.group("kind"))
        if k2 and t2:
            out.append((k2, hm, t2))
    return out


def parse_line(line):
    """-> (canonical kind, time 'HH:MM' or '', text) of the line's FIRST tracked mark, or None."""
    p = parse_marks(line)
    return p[0] if p else None


def scan_journal(path, skipped=None):
    """-> list of mark dicts found in one journal file (first occurrence per id).  Status-kind
    lines are counted into `skipped` ({status kind: n}) when given."""
    name = os.path.basename(path)
    jm = JOURNAL_NAME_RE.match(name)
    date = jm.group(1) if jm else ""
    out, seen = [], set()
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for n, line in enumerate(fh, 1):
            if "MARK[" not in line:
                continue
            for kind, hm, text in parse_marks(line.rstrip("\n"), skipped):
                mid = mark_id(kind, text)
                if mid in seen:
                    continue
                seen.add(mid)
                out.append({"id": mid, "date": date, "time": hm, "kind": kind,
                            "text": norm(text), "loc": f"journal/{name}:{n}"})
    return out


def journal_files(paths, floor, until=None):
    files = []
    for f in sorted(glob.glob(os.path.join(paths.journal, "2*.md"))):
        m = JOURNAL_NAME_RE.match(os.path.basename(f))
        if not m:
            continue
        if m.group(1) < floor or (until and m.group(1) > until):
            continue
        files.append(f)
    return files


def collect_marks(paths, floor, until=None, skipped=None):
    """-> ordered dict id -> mark; a mark seen on several days keeps its first.  Status-kind
    lines are counted into `skipped` when given."""
    marks = {}
    for f in journal_files(paths, floor, until):
        for mk in scan_journal(f, skipped):
            if mk["id"] in marks:
                marks[mk["id"]].setdefault("also", []).append(mk["loc"])
                continue
            marks[mk["id"]] = mk
    return marks


# --- cards -------------------------------------------------------------------
FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
SRC_JOURNAL_RE = re.compile(r"^\s*-\s*src=journal\s*::\s*(.+?)\s*$", re.M)
ELISION_RE = re.compile(r"\s*(?:\.\.\.|…|\[\.\.\.\]|\[…\])\s*")
SOURCE_RE = re.compile(r"^source:\s*(.*)$", re.M)
DATE_RE = re.compile(r"^date:\s*'?(\d{4}-\d{2}-\d{2})", re.M)
MARKS_FIELD_RE = re.compile(r"^marks:\s*(.*)$", re.M)
JREF_RE = re.compile(r"journal(?:/|\s+)(\d{4}-\d{2}-\d{2})(?:\.md)?")
HM_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2})(?![\d])")
WORD_RE = re.compile(r"[A-Za-z\u00c0-\u017e0-9_./-]{5,}")
MIN_QUOTE = 30


def read_cards(paths):
    """-> list of (card id, date, text) for every card file."""
    cards = []
    for f in sorted(glob.glob(os.path.join(paths.know, "K-*.md"))):
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        cid = os.path.basename(f)[:-3]
        dm = DATE_RE.search(text[:3000])
        cards.append((cid, dm.group(1) if dm else "", text))
    return cards


def journal_refs(source):
    """`journal 2026-09-26 22:42; journal/2026-09-21.md MARKs 17:43, 18:00`
    -> {(date, 'HH:MM'), ...}.  Times are taken up to the next `;` or the
    next journal reference, so one card's times never bleed into another
    date."""
    refs = set()
    ms = list(JREF_RE.finditer(source))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(source)
        seg = source[m.end():end]
        semi = seg.find(";")
        if semi >= 0:
            seg = seg[:semi]
        for hm in HM_RE.findall(seg):
            refs.add((m.group(1), hm if len(hm) == 5 else "0" + hm))
    return refs


def overlap(mark_text, card_text_lower):
    words = {w.lower() for w in WORD_RE.findall(mark_text)}
    if not words:
        return 0.0
    hit = sum(1 for w in words if w in card_text_lower)
    return hit / len(words)


def card_mark_links(text):
    """The `marks:` ids of a card's FRONTMATTER — the one structured link that
    makes a mark CARDED.  Flow (`marks: [M-…, M-…]`) and block (`- M-…`) lists;
    nothing outside the frontmatter block and nothing outside that key counts."""
    fm = FRONT_RE.match(text)
    if not fm:
        return []
    ids, inside = [], False
    for line in fm.group(1).splitlines():
        if re.match(r"^marks:", line):
            inside = True
            ids += ID_RE.findall(line)
            continue
        if inside:
            if re.match(r"^\s*-\s*M-", line) or re.match(r"^\s+M-", line):
                ids += ID_RE.findall(line)
                continue
            inside = False
    return list(dict.fromkeys(ids))


def structural_links(cards):
    """-> dict mark id -> [card ids whose frontmatter `marks:` names it]."""
    links = {}
    for cid, _cdate, text in cards:
        for mid in card_mark_links(text):
            links.setdefault(mid, []).append(cid)
    return links


def match_cards(marks, cards, floor=None):
    """-> dict mark id -> (card id, "marks"): COVERAGE — only a card's `marks:`
    frontmatter link to a tracked mark (review P1-1).  The heuristics live in
    `suggest_cards` and never close a mark."""
    found = {}
    for mid, cids in structural_links(cards).items():
        if mid in marks:
            found[mid] = (cids[0], "marks")
    return found


def suggest_cards(marks, cards, floor):
    """-> dict mark id -> (card id, how): HEURISTIC SUGGESTIONS, never coverage.

    review P1-1 (2026-10-01): an id mentioned in a card's prose ("This
    card does NOT cover M-12345678"), a card that merely shares the MARK's
    minute, or a quote overlap can only PROPOSE a link — `derive` records it as
    `suggested`, and only the coordinator's `card ID K-x` (or a `marks:`
    frontmatter link) makes the mark CARDED."""
    found = {}
    by_dt = {}
    for mk in marks.values():
        by_dt.setdefault((mk["date"], mk["time"]), []).append(mk)
    norm_marks = [(mk["id"], mk["text"].lower()) for mk in marks.values()]
    try:
        lo = (dt.date.fromisoformat(floor) - dt.timedelta(days=3)).isoformat()
    except ValueError:
        lo = floor
    for cid, cdate, text in cards:
        # (1) an id mentioned anywhere in the card (not the `marks:` key)
        for mid in ID_RE.findall(text):
            if mid in marks and mid not in found:
                found[mid] = (cid, "mention")
        if cdate and cdate < lo:
            continue
        # (2) a `journal <date> <HH:MM>` reference in the card's source line
        fm = FRONT_RE.match(text)
        src = ""
        if fm:
            sm = SOURCE_RE.search(fm.group(1))
            src = sm.group(1) if sm else ""
        lower = None
        for ref in journal_refs(src):
            cands = [mk for mk in by_dt.get(ref, ()) if mk["id"] not in found]
            if not cands:
                continue
            if len(cands) == 1:
                found[cands[0]["id"]] = (cid, "source-time")
                continue
            # several marks share that minute: only those the card talks about
            lower = lower if lower is not None else text.lower()
            for mk in cands:
                if overlap(mk["text"], lower) >= 0.25:
                    found[mk["id"]] = (cid, "source-time+words")
        # (3) verbatim journal quotes
        for q in SRC_JOURNAL_RE.findall(text):
            for seg in ELISION_RE.split(norm(q)):
                seg = seg.strip(" \"'").lower()
                if len(seg) < MIN_QUOTE:
                    continue
                for mid, mtext in norm_marks:
                    if mid in found:
                        continue
                    if seg in mtext or (len(mtext) >= MIN_QUOTE and mtext in seg):
                        found[mid] = (cid, "quote")
    return found


# --- proposals: review UNITS (unreviewed inbox files) + carry --------------------------
# A unit is one inbox file a review approves as a whole.  Legacy units are DATE.md and
# DATE-weekly.md; since review P1-3 the nightly publishes every consolidated batch as its
# own immutable unit `<night key>-bNN.md` (night key = DATE or DATE-<n> for the n-th run
# of that date), so approving one batch can never cover a batch published after it.
# A MARK-stage chunk is committed as its own carry directory <night key>-mNN (PMARKFIRST,
# nightly_distill.sh step 1e), so its batches are published as `<night key>-mNN-bKK.md`.
UNIT_RE = re.compile(r"^(?P<stem>(?P<date>\d{4}-\d{2}-\d{2})"
                     r"(?P<tail>-weekly|(?:-\d+)?(?:-m\d{2,})?-b\d{2,})?)\.md$")
MTIME_SLACK_S = 5.0   # a legacy (hash-less) marker written in the same second as the unit


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def unit_markers(paths, stem):
    """Marker files that may describe the review of unit `stem` (newest spelling first)."""
    m = UNIT_RE.match(stem + ".md")
    names = [f".reviewed-{stem}-cards"]
    if m and not m.group("tail"):
        names.append(f".reviewed-{stem}")          # the bare legacy date marker
    return [os.path.join(paths.reviewed, n) for n in names]


def marker_holds(marker, unit):
    """True when `marker` records a review of the unit AS IT IS NOW: its sha256 line
    equals the unit's hash, or — a legacy marker without a hash — the unit was not
    modified after the marker was written."""
    try:
        body = open(marker, encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    hm = re.search(r"sha256=([0-9a-f]{64})", body)
    try:
        if hm:
            return hm.group(1) == sha256_file(unit)
        return os.path.getmtime(unit) <= os.path.getmtime(marker) + MTIME_SLACK_S
    except OSError:
        return False


def unit_reviewed(paths, unit):
    stem = os.path.basename(unit)[:-3]
    return any(marker_holds(mk, unit) for mk in unit_markers(paths, stem))


def review_units(paths):
    """-> [(path, reviewed: bool)] for every review unit in the inbox, sorted."""
    out = []
    for f in sorted(glob.glob(os.path.join(paths.inbox, "*.md"))):
        if UNIT_RE.match(os.path.basename(f)):
            out.append((f, unit_reviewed(paths, f)))
    return out


def reviewed(paths, date, weekly=""):
    """Legacy date-level question, kept for callers: is the unit DATE[-weekly].md reviewed
    as it is now?"""
    return unit_reviewed(paths, os.path.join(paths.inbox, f"{date}{weekly}.md"))


def carry_files(paths):
    """Every file of the carry queue that still holds proposals: batches, their durable
    outputs and NOT-CARDED verdicts, flat (pre-2026-10-01 night) or in run directories."""
    files = sorted(glob.glob(os.path.join(paths.carry, "*.md")))
    files += sorted(glob.glob(os.path.join(paths.carry, "[0-9]*", "*.md")))
    return files


def pending_files(paths):
    files = [f for f, ok in review_units(paths) if not ok]
    files += carry_files(paths)
    return files


def proposed_ids(paths):
    prop = {}
    for f in pending_files(paths):
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for mid in ID_RE.findall(text):
            prop.setdefault(mid, os.path.relpath(f, paths.root))
    return prop


# --- events ----------------------------------------------------------------------
def read_events(paths):
    last = {}
    try:
        with open(paths.events, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("id"):
                    last[ev["id"]] = ev
    except OSError:
        pass
    return last


@contextlib.contextmanager
def ledger_lock(paths):
    """One lock for every ledger WRITE — sync's snapshot, the coordinator's events, the
    approve path's re-derive — so a sync can never publish a state derived from an
    events file another writer is in the middle of (review P2-6)."""
    os.makedirs(paths.ledger, exist_ok=True)
    with KL.lock('ledger', paths.root):
        yield


def append_events(paths, events):
    with ledger_lock(paths):
        with open(paths.events, "a", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


# --- derive ----------------------------------------------------------------------
def floor_of(paths, arg):
    if arg:
        return arg
    if re.match(r"^\d{4}-\d{2}-\d{2}$", os.environ.get("MARK_LEDGER_FLOOR", "")):
        return os.environ["MARK_LEDGER_FLOOR"]
    try:
        v = open(paths.floor_file, encoding="utf-8").read().strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            return v
    except OSError:
        pass
    return DEFAULT_FLOOR


def derive(paths, floor, until=None, skipped=None):
    """State of every tracked mark.  CARDED only by a structured link — the card's
    `marks:` frontmatter or the coordinator's `card` event; the LAST event of an id
    wins; a `reopen` ignores the `marks:` links of the cards it names (the ones that
    existed when it was recorded), so it really reopens, and a card written later closes
    the mark again.  Heuristic matches are kept as `suggested`, never as coverage."""
    marks = collect_marks(paths, floor, until, skipped)
    events = read_events(paths)
    cards = read_cards(paths)
    links = structural_links(cards)
    suggested = suggest_cards(marks, cards, floor)
    known_cards = {c[0] for c in cards}
    prop = proposed_ids(paths)
    dups = {}
    for mid, mk in marks.items():
        ev = events.get(mid) or {}
        kind = ev.get("ev")
        linked = links.get(mid, [])
        if kind == "reopen":
            gone = set(ev.get("cards") or [])
            linked = [c for c in linked if c not in gone]
        state, card, how, reason = "OPEN", "", "", ""
        target = dup_target(ev)
        if target and not linked:
            dups[mid] = target          # resolved below, once every target has its state
        elif kind == "dismiss" and not target:
            state, reason = "DISMISSED", ev.get("reason", "")
        elif kind == "card" and ev.get("card") in known_cards:
            state, card, how = "CARDED", ev["card"], "event"
            reason = ev.get("reason") or f"card event by {ev.get('by', '?')} {ev.get('ts', '')}"
        elif linked:
            state, card, how = "CARDED", linked[0], "marks"
            reason = f"`marks:` frontmatter of {linked[0]}"
        mk["state"] = state
        if card:
            mk["card"], mk["how"] = card, how
        if reason:
            mk["reason"] = reason
        if state == "OPEN":
            sg = suggested.get(mid)
            if sg and not (kind == "reopen" and sg[0] in set(ev.get("cards") or [])):
                mk["suggested"] = {"card": sg[0], "how": sg[1]}
            if mid in prop:
                mk["proposed_in"] = prop[mid]
    resolve_duplicates(marks, dups)
    return marks


# "duplicate of M-x" — #209 review R209AC A5.  It used to be a plain DISMISSED: when M-x
# was later dismissed or reopened, the duplicate stayed dismissed and its content was in
# no card (66 such dismissals; 8 targets were themselves dismissed, one a khms-issue).
DUP_RE = re.compile(r"(?i)^(?:nightly verdict accepted at review:\s*)?duplicate of\s+"
                    r"(M-[0-9a-f]{8})\b")


def dup_target(ev):
    """The mark id an event says this mark duplicates: a `dup` event, or a legacy dismiss
    whose recorded reason is the night's `duplicate of M-x` verdict."""
    if ev.get("ev") == "dup":
        return ev.get("dup_of")
    if ev.get("ev") == "dismiss":
        m = DUP_RE.match(ev.get("reason") or "")
        return m.group(1) if m else None
    return None


def resolve_duplicates(marks, dups):
    """A duplicate takes the state of its target, followed along a chain of duplicates:
    target CARDED -> CARDED with the target's card; target OPEN -> OPEN and, while the
    target waits in an unreviewed proposal, waiting with it; target DISMISSED, unknown or a
    cycle -> OPEN on its own (the night judges it alone, it is never folded into a dismissal)."""
    for mid, first in dups.items():
        t, seen = first, {mid}
        while t in dups and t not in seen:
            seen.add(t)
            t = dups[t]
        mk = marks[mid]
        tgt = marks.get(t) if t not in seen else None
        mk["dup_of"] = first
        tstate = tgt["state"] if tgt else "UNKNOWN"
        if tstate == "CARDED":
            mk.pop("suggested", None)
            mk.pop("proposed_in", None)
            mk["state"], mk["card"], mk["how"] = "CARDED", tgt["card"], "dup"
            mk["reason"] = f"duplicate of {first}" + (f" -> {t}" if t != first else "") \
                + f", carded in {tgt['card']}"
            continue
        mk["state"] = "OPEN"
        if tstate == "OPEN":
            mk["reason"] = f"duplicate of {first}, which is OPEN"
            if tgt.get("proposed_in"):
                mk["proposed_in"] = tgt["proposed_in"]
        else:
            mk["reason"] = (f"duplicate of {first}, whose target is "
                            f"{'DISMISSED' if tstate == 'DISMISSED' else 'unknown'} — "
                            f"judged on its own")


def carry_count(paths):
    """Consolidate batches still waiting (flat legacy names and run directories)."""
    return len([f for f in carry_files(paths)
                if re.search(r"(?:^|-)cons-\d+\.md$", os.path.basename(f))])


TRACKED_KINDS = sorted(KIND_ALIASES)  # base canonical kinds, not a tracking allowlist
NOT_COVERED = ("journals before the floor; status MARK kinds (LEDGER_STATUS_KINDS: agent-done, "
               "board-reply, done, check, ...; counted as skipped); knowledge without an actual MARK line")


def summarize(marks, floor, paths=None, skipped=None):
    """`skipped` = {status kind: n} from derive(); None = not counted (the summary then says so)."""
    counts = {"OPEN": 0, "CARDED": 0, "DISMISSED": 0}
    proposed = suggested = 0
    oldest = None
    new_kinds = {}
    for mk in marks.values():
        counts[mk["state"]] += 1
        if mk["kind"] not in KIND_ALIASES:
            new_kinds[mk["kind"]] = new_kinds.get(mk["kind"], 0) + 1
        if mk["state"] == "OPEN":
            if mk.get("suggested"):
                suggested += 1
            if mk.get("proposed_in"):
                proposed += 1
            key = (mk["date"], mk["time"], mk["loc"])
            if oldest is None or key < (oldest["date"], oldest["time"], oldest["loc"]):
                oldest = mk
    return {
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "floor": floor,
        "total": len(marks),
        "open": counts["OPEN"],
        "open_proposed": proposed,
        "open_queued": counts["OPEN"] - proposed,
        "carded": counts["CARDED"],
        "dismissed": counts["DISMISSED"],
        # OPEN marks with a heuristic card suggestion (`suggest` lists them) — a
        # suggestion is NOT coverage (review P1-1)
        "open_suggested": suggested,
        "oldest_open_age_days": (
            (dt.date.today() - dt.date.fromisoformat(oldest["date"])).days
            if oldest and re.match(r"^\d{4}-\d{2}-\d{2}$", oldest["date"]) else None),
        # what "OPEN 0" does and does not promise (review P2-6)
        "scope": {"floor": floor, "kinds": TRACKED_KINDS, "status_kinds": sorted(STATUS_KINDS),
                  "all_kinds": not STATUS_KINDS, "not_covered": NOT_COVERED},
        # status-kind MARK lines left out of the ledger (#236) -- counted, never silent (K-00586)
        "status_skipped": sum(skipped.values()) if skipped is not None else None,
        "status_skipped_kinds": (dict(sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0])))
                                 if skipped is not None else None),
        # marks of kinds outside the eight base kinds — tracked since #209 A2
        "new_kinds": dict(sorted(new_kinds.items(), key=lambda kv: (-kv[1], kv[0]))),
        # consolidate batches waiting in memory/ledger/carry/ — the night's unfinished work
        "carry": carry_count(paths) if paths else None,
        "oldest_open": ({k: oldest[k] for k in ("id", "date", "time", "kind", "text", "loc")}
                        if oldest else None),
    }


def status_line(s):
    """The one-line ledger state for the session-start hook and the morning review. Wording: message
    keys LEDGER_* (khms_messages.py; a deployment may replace them in its private messages file)."""
    if s is None:
        return M.msg("LEDGER_NO_STATE")
    o = s.get("oldest_open")
    old = (M.msg("LEDGER_OLDEST", id=o["id"], date=o["date"], time=o["time"], kind=o["kind"],
                 text=o["text"][:90]) if o else M.msg("LEDGER_NONE_OPEN"))
    age = s.get("oldest_open_age_days")
    sug = s.get("open_suggested")
    return M.msg("LEDGER_STATUS", floor=s["floor"], open=s["open"], queued=s["open_queued"],
                 proposed=s["open_proposed"],
                 suggested=M.msg("LEDGER_SUGGESTED", n=sug) if sug else "",
                 carded=s["carded"], dismissed=s["dismissed"], carry=s.get("carry", "?"), oldest=old,
                 age=M.msg("LEDGER_AGE", days=age) if age is not None else "",
                 generated=s["generated"],
                 nkinds="all actual MARK") + skipped_part(s, "LEDGER_STATUS_SKIPPED")


def skipped_part(s, key, top=3):
    """The status-line tail that says how many status-kind lines the ledger left out.  Appended OUTSIDE the
    deployment's status template, so a private wording cannot hide the skip.  A summary written before
    #236 has no count: nothing was skipped then, and nothing is said."""
    n = s.get("status_skipped")
    if n is None:
        return ""
    kinds = s.get("status_skipped_kinds") or {}
    items = list(kinds.items())
    shown = ", ".join(f"{k} {c}" for k, c in items[:top])
    more = f", +{len(items) - top} more kinds" if len(items) > top else ""
    return M.msg(key, n=n, kinds=f" ({shown}{more})" if shown else "")


def _new_kinds_part(nk, top=3):
    if not nk:
        return ""
    items = sorted(nk.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = ", ".join(f"{k} {n}" for k, n in items[:top])
    more = f", +{len(items) - top} more kinds" if len(items) > top else ""
    return f"; outside the base kinds {sum(nk.values())} ({shown}{more})"


def write_state(paths, marks, summary):
    os.makedirs(paths.ledger, exist_ok=True)
    tmp = paths.marks + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for mk in marks.values():
            fh.write(json.dumps(mk, ensure_ascii=False) + "\n")
    os.replace(tmp, paths.marks)
    tmp = paths.summary + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, paths.summary)


def read_summary(brain=DEFAULT_ROOT):
    try:
        with open(Paths(brain).summary, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# --- queue -------------------------------------------------------------------------
def queue_marks(marks, since=None, until=None):
    q = [mk for mk in marks.values()
         if mk["state"] == "OPEN" and not mk.get("proposed_in")
         and (not since or mk["date"] >= since) and (not until or mk["date"] <= until)]
    # a mark with a heuristic card suggestion goes after the ones without: it probably
    # needs only the night's "already carded K-x" verdict, the others need a card
    q.sort(key=lambda mk: (bool(mk.get("suggested")), mk["date"], mk["time"], mk["loc"]))
    return q


def render_queue(q, title):
    lines = [f"# {title} — {len(q)} OPEN mark(s), oldest first",
             "# id | journal date time | kind | where | text", ""]
    for mk in q:
        lines.append(f"- {mk['id']} | {mk['date']} {mk['time'] or '--:--'} | {mk['kind']} "
                     f"| {mk['loc']} | {mk['text']}")
    return "\n".join(lines) + "\n"


# --- attribute: add MARKS lines by verbatim quotes ----------------------------------
CAND_RE = re.compile(r"^(#{2,3} +[A-Z]\d+[a-z]?\b.*)$", re.M)
TRAILER_RE = re.compile(r"^## (?!#)", re.M)
MARKS_LINE_RE = re.compile(r"^[ \t]*\*{0,2}MARKS:\*{0,2}[ \t]*(.*)$", re.M)


def attribute_text(text, marks):
    """Add/extend a `MARKS:` line on every candidate whose `src=journal` quotes
    match a mark verbatim.  Returns (new text, n candidates touched)."""
    norm_marks = [(mk["id"], mk["text"].lower()) for mk in marks.values()]
    heads = list(CAND_RE.finditer(text))
    if not heads:
        return text, 0
    out, pos, touched = [], 0, 0
    for i, h in enumerate(heads):
        start = h.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        tm = TRAILER_RE.search(text, h.end(), end)
        if tm:
            end = tm.start()
        block = text[start:end]
        out.append(text[pos:start])
        ids = []
        for q in SRC_JOURNAL_RE.findall(block):
            for seg in ELISION_RE.split(norm(q)):
                seg = seg.strip(" \"'").lower()
                if len(seg) < MIN_QUOTE:
                    continue
                for mid, mtext in norm_marks:
                    if mid not in ids and (seg in mtext or
                                           (len(mtext) >= MIN_QUOTE and mtext in seg)):
                        ids.append(mid)
        if ids:
            have = MARKS_LINE_RE.search(block)
            if have:
                cur = ID_RE.findall(have.group(1))
                new = cur + [i_ for i_ in ids if i_ not in cur]
                if new != cur:
                    block = (block[:have.start()] + "MARKS: " + " ".join(new)
                             + block[have.end():])
                    touched += 1
            else:
                qm = block.find("**QUOTES:**")
                line = "MARKS: " + " ".join(ids) + "\n"
                if qm >= 0:
                    block = block[:qm] + line + block[qm:]
                else:
                    block = block.rstrip("\n") + "\n" + line + "\n"
                touched += 1
        out.append(block)
        pos = end
    out.append(text[pos:])
    return "".join(out), touched


# --- CLI ----------------------------------------------------------------------------
def cmd_sync(a, paths):
    floor = floor_of(paths, a.floor)
    skipped = {}
    if a.dry:
        marks = derive(paths, floor, a.until, skipped)
        s = summarize(marks, floor, paths, skipped)
    else:
        with ledger_lock(paths):
            marks = derive(paths, floor, a.until, skipped)
            s = summarize(marks, floor, paths, skipped)
            write_state(paths, marks, s)
    print(status_line(s))
    print(f"total tracked marks: {s['total']} (floor {floor}"
          f"{', until ' + a.until if a.until else ''}){' [dry: nothing written]' if a.dry else ''}")
    nk = s.get("new_kinds") or {}
    print("scope: every actual MARK kind except the status kinds; "
          f"base kinds {', '.join(TRACKED_KINDS)}; NOT covered: {NOT_COVERED}")
    if nk:
        print(f"tracked kinds outside the base list: "
              f"{', '.join(f'{k} {n}' for k, n in nk.items())}")
    print(f"status lines skipped (status MARK kinds are not tracked, owner 2026-10-13): "
          f"{s['status_skipped']}"
          + (f" ({', '.join(f'{k} {n}' for k, n in s['status_skipped_kinds'].items())})"
             if s['status_skipped'] else ""))
    return 0


def cmd_status(a, paths):
    s = read_summary(paths.root)
    print(status_line(s))
    return 0


def cmd_queue(a, paths):
    floor = floor_of(paths, a.floor)
    marks = derive(paths, floor, a.until)
    q = queue_marks(marks, a.since, a.until)
    if a.chunk:
        os.makedirs(a.outdir, exist_ok=True)
        names = []
        for k in range(0, len(q), a.chunk):
            part = q[k:k + a.chunk]
            name = os.path.join(a.outdir, f"{a.prefix}-{k // a.chunk + 1:02d}.md")
            with open(name, "w", encoding="utf-8") as fh:
                fh.write(render_queue(part, f"MARK QUEUE chunk {k // a.chunk + 1}"))
            names.append(name)
        for n in names:
            print(n)
        print(f"queue: {len(q)} OPEN mark(s) in {len(names)} chunk(s)", file=sys.stderr)
        return 0
    text = render_queue(q, "MARK QUEUE")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"queue: {len(q)} OPEN mark(s) -> {a.out}")
    else:
        sys.stdout.write(text)
    return 0


def cmd_attribute(a, paths):
    floor = floor_of(paths, a.floor)
    marks = collect_marks(paths, floor)
    total = 0
    for f in a.files:
        try:
            text = open(f, encoding="utf-8").read()
        except OSError as exc:
            print(f"attribute: {f}: {exc}", file=sys.stderr)
            continue
        new, n = attribute_text(text, marks)
        if n and not a.dry:
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(new)
        total += n
        print(f"attribute: {n} candidate(s) got MARKS ids from journal quotes in {f}")
    return 0


def cmd_coverage(a, paths):
    q = ID_RE.findall(open(a.queue, encoding="utf-8").read())
    q = list(dict.fromkeys(q))
    have = set()
    for f in a.files:
        try:
            have |= set(ID_RE.findall(open(f, encoding="utf-8").read()))
        except OSError:
            pass
    missing = [i for i in q if i not in have]
    print(f"coverage: {len(q) - len(missing)}/{len(q)} queued marks accounted for; "
          f"{len(missing)} not — they stay OPEN and go to the next queue")
    if a.verbose:
        for i in missing:
            print(f"  missing {i}")
    return 0


def cmd_scan(a, paths):
    n = 0
    for f in a.journals:
        for mk in scan_journal(f):
            n += 1
            print(f"{mk['id']} | {mk['date']} {mk['time'] or '--:--'} | {mk['kind']} | "
                  f"{mk['loc']} | {mk['text'][:a.width]}")
    print(f"scan: {n} tracked mark(s)", file=sys.stderr)
    return 0


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _known_ids(paths, floor):
    return set(collect_marks(paths, floor))


# --- "already carded K-x" must be PROVEN by a quote of K-x (#209 review R209AC A1) -------
# The night's verdict `already carded K-x` used to become CARDED when the file K-x merely
# existed: 1185 of 4365 CARDED marks (27 %) rested on that unverified model verdict, e.g.
# M-9fc01dad (the H3 memory-bandwidth refutation, "EMC 0 %") closed onto K-93150, which
# holds neither.  Now the verdict carries the sentence of K-x that holds the mark
# (`already carded K-x: "<verbatim sentence of K-x>"`) and that quote is checked against
# the card (0 tokens, the verify_quotes idea); no quote or no match = no coverage, the mark
# stays OPEN and the next night writes a candidate (or a verdict with a real quote).
ALREADY_RE = re.compile(r"(?i)^already carded\s+(K-\d{5})\b\s*[:—–-]?\s*(?P<q>.*)$")
MIN_CARD_QUOTE = 25


def _match_norm(s):
    s = norm(s).lower()
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    return s


def quote_in_card(quote, card_text):
    """True when `quote` (outer quote marks stripped, `...` elisions allowed) is verbatim in
    the card — every segment present, at least MIN_CARD_QUOTE characters in all."""
    q = norm(quote).strip()
    if len(q) >= 2 and q[0] in "\"'" and q[-1] in "\"'":
        q = q[1:-1]
    segs = [g.strip(" \"'").lower() for g in ELISION_RE.split(_match_norm(q))]
    segs = [g for g in segs if g]
    if not segs or sum(len(g) for g in segs) < MIN_CARD_QUOTE:
        return False
    body = _match_norm(card_text)
    return all(g in body for g in segs)


# --- the night's NOT CARDED verdicts of a unit (#209 A1, A4) ---------------------------
# ONE path for both ways a reviewer accepts them: `dismiss --from-inbox FILE`, and closing
# the unit's review (`mark-reviewed`, `approve_inbox.py`'s marker).  Before A4 (review
# R209AC) closing the unit did not record them: the marks left "proposed" and the night
# queued them again — 235 marks were proposed on 2 nights, 28 on 3, all NOT CARDED.
NOT_CARDED_SEC_RE = re.compile(rf"^{re.escape(NOT_CARDED_HEAD)}.*?$(.*?)(?=^## |\Z)",
                               re.M | re.S)


def not_carded_events(paths, text, known, by):
    """-> (events, unknown ids, unproven) for the `## MARKS NOT CARDED` section of `text`,
    or None when the text has no such section.  `already carded K-x` is coverage only with
    a quote verifiably in K-x; `duplicate of M-x` follows M-x; anything else is a dismissal
    with the verdict as its reason."""
    sec = NOT_CARDED_SEC_RE.search(text)
    if not sec:
        return None
    evs, bad, unproven = [], [], []
    for line in sec.group(1).splitlines():
        m = re.match(r"^\s*-\s*(M-[0-9a-f]{8})\s*::\s*(.+?)\s*$", line)
        if not m:
            continue
        mid, verdict = m.group(1), m.group(2)
        if mid not in known:
            bad.append(mid)
            continue
        am = ALREADY_RE.match(verdict)
        dm = DUP_RE.match(verdict)
        if am:
            cid = am.group(1)
            try:
                ctext = open(os.path.join(paths.know, cid + ".md"),
                             encoding="utf-8", errors="replace").read()
            except OSError:
                ctext = None
            if ctext is not None and quote_in_card(am.group("q"), ctext):
                evs.append({"ts": _now(), "ev": "card", "id": mid, "card": cid,
                            "by": by, "quote_verified": True,
                            "reason": f"nightly verdict accepted: {verdict}"})
            else:
                unproven.append((mid, cid, "no such card" if ctext is None else
                                 "no quote" if not am.group("q").strip() else
                                 "quote not in the card"))
        elif dm:
            evs.append({"ts": _now(), "ev": "dup", "id": mid, "dup_of": dm.group(1),
                        "by": by, "reason": f"nightly verdict accepted at review: {verdict}"})
        else:
            evs.append({"ts": _now(), "ev": "dismiss", "id": mid, "by": by,
                        "reason": f"nightly verdict accepted at review: {verdict}"})
    return evs, bad, unproven


def _same_event(a, b):
    return all(a.get(k) == b.get(k) for k in ("ev", "card", "dup_of", "reason"))


def record_not_carded(paths, text, by, floor=None):
    """Record the section's verdicts.  Skipped: an id whose last event already says the same
    (closing a unit twice, or `dismiss --from-inbox` after the close, adds nothing), and an
    id that is CARDED now — a verdict is about a mark the night did NOT card; once a card
    holds it (a later unit, a hand card), a "not know-how" verdict recorded on top would
    turn CARDED into DISMISSED (derive: a dismiss event outranks a `marks:` link; measured
    2026-10-08 on the reviewed units: 16 such verdicts).
    -> None without a section, else (recorded, unknown, unproven, skipped_carded)."""
    marks = derive(paths, floor_of(paths, floor))
    got = not_carded_events(paths, text, set(marks), by)
    if got is None:
        return None
    evs, bad, unproven = got
    last = read_events(paths)
    carded = [e["id"] for e in evs if marks[e["id"]]["state"] == "CARDED"]
    evs = [e for e in evs if e["id"] not in carded
           and not (e["id"] in last and _same_event(last[e["id"]], e))]
    append_events(paths, evs)
    return evs, bad, unproven, carded


def report_not_carded(prog, bad, unproven, carded=()):
    if carded:
        print(f"{prog}: {len(carded)} verdict(s) not recorded — the mark is CARDED already: "
              f"{' '.join(carded)}")
    if bad:
        print(f"{prog}: unknown id(s), not recorded: {' '.join(bad)}")
    if unproven:
        print(f"{prog}: {len(unproven)} 'already carded' verdict(s) with no verifiable quote "
              f"of the card — NOT coverage, they stay OPEN for the next night:")
        for mid, cid, why in unproven:
            print(f"  {mid} -> {cid}: {why}")


def cmd_dismiss(a, paths):
    floor = floor_of(paths, a.floor)
    known = _known_ids(paths, floor)
    if a.from_inbox:
        text = open(a.from_inbox, encoding="utf-8").read()
        got = record_not_carded(paths, text, a.by, floor=floor)
        if got is None:
            print(f"dismiss: no '{NOT_CARDED_HEAD}' section in {a.from_inbox}")
            return 1
        evs, bad, unproven, carded = got
    else:
        if not a.reason:
            print("dismiss: --reason is required (a dismissal without a reason is a silent loss)")
            return 2
        bad = [mid for mid in a.ids if mid not in known]
        unproven, carded = [], []
        evs = [{"ts": _now(), "ev": "dismiss", "id": mid, "by": a.by, "reason": a.reason}
               for mid in a.ids if mid in known]
        append_events(paths, evs)
    report_not_carded("dismiss", bad, unproven, carded)
    print(f"dismiss: recorded {len(evs)} event(s) in {paths.events}")
    return 1 if bad and not evs else 0


# --- re-check of the verdicts accepted before the quote rule (#209 A1, one-off) ----------
# 0 tokens: a CARDED-by-verdict mark whose DISTINCTIVE terms (numbers, identifiers, paths,
# ACRONYMS, camelCase — the words that survive a Czech mark becoming an English card) are
# mostly absent from K-x is reopened and goes back to the night's queue.  Measured
# 2026-10-08 on the 1185 such marks: 36 have no distinctive term (kept — cannot be judged
# lexically), 130 have none of theirs in K-x, 141 under a third, 725 half or more.
RECHECK_DROP = re.compile(
    r"\((?:agent|sess|session)\b[^)]*\)|\bM-[0-9a-f]{8}\b|\bK-\d{5}\b"
    r"|\b\d{4}-\d{2}-\d{2}(?:T[\d:+.-]+)?\b|\b\d{1,2}:\d{2}(?::\d{2})?\b|#\d+")
RECHECK_TOK = re.compile(r"[0-9A-Za-z\u00c0-\u017e_][0-9A-Za-z\u00c0-\u017e_./%+-]*[0-9A-Za-z\u00c0-\u017e_%]")
RECHECK_MIN_SHARE = 1 / 3


def distinctive_terms(text):
    t = re.sub(r"(?<=\d),(?=\d)", ".", RECHECK_DROP.sub(" ", text))
    out = set()
    for w in RECHECK_TOK.findall(t):
        if len(w) < 3:
            continue
        if (re.search(r"\d", w) or re.search(r"[_/]", w) or re.search(r"\w\.\w", w)
                or re.fullmatch(r"[A-Z][A-Z0-9-]{2,}", w) or re.search(r"[a-z][A-Z]", w)):
            out.add(w.lower())
    return out


def cmd_recheck_carded(a, paths):
    floor = floor_of(paths, a.floor)
    marks = derive(paths, floor)
    events = read_events(paths)
    links = structural_links(read_cards(paths))
    texts, checked, reopen, unjudged = {}, 0, [], 0
    for mid, mk in marks.items():
        ev = events.get(mid) or {}
        if not (mk["state"] == "CARDED" and mk.get("how") == "event" and ev.get("ev") == "card"
                and not ev.get("quote_verified") and not links.get(mid)
                and (ev.get("reason") or "").startswith("nightly verdict accepted: already carded")):
            continue
        checked += 1
        cid = ev["card"]
        if cid not in texts:
            try:
                texts[cid] = _match_norm(open(os.path.join(paths.know, cid + ".md"),
                                              encoding="utf-8", errors="replace").read())
            except OSError:
                texts[cid] = ""
        terms = distinctive_terms(mk["text"])
        if not terms:
            unjudged += 1
            continue
        hit = sorted(w for w in terms if w in texts[cid])
        if len(hit) / len(terms) < RECHECK_MIN_SHARE:
            miss = sorted(terms - set(hit))
            reopen.append((mid, cid, len(hit), len(terms), miss))
    if a.verbose:
        for mid, cid, h, n, miss in reopen:
            print(f"  {mid} -> {cid}: {h}/{n} distinctive terms in the card; missing "
                  f"{', '.join(miss[:6])} | {marks[mid]['text'][:90]}")
    if a.apply and reopen:
        append_events(paths, [{"ts": _now(), "ev": "reopen", "id": mid, "by": a.by, "cards": [],
                               "reason": f"#209 A1 lexical re-check: {h}/{n} distinctive terms "
                                         f"of the mark in {cid} (missing {', '.join(miss[:6])})"}
                              for mid, cid, h, n, miss in reopen])
    print(f"recheck-carded: {checked} mark(s) CARDED by an unquoted 'already carded' verdict; "
          f"{unjudged} without a distinctive term (kept); "
          f"{'reopened' if a.apply else 'would reopen (dry, --apply writes)'} {len(reopen)} "
          f"whose distinctive terms are mostly (< {RECHECK_MIN_SHARE:.2f}) absent from the card")
    return 0


def cmd_card(a, paths):
    floor = floor_of(paths, a.floor)
    if a.id not in _known_ids(paths, floor):
        print(f"card: unknown mark id {a.id}")
        return 1
    if not os.path.exists(os.path.join(paths.know, a.card + ".md")):
        print(f"card: no such card {a.card}")
        return 1
    append_events(paths, [{"ts": _now(), "ev": "card", "id": a.id, "card": a.card,
                           "by": a.by}])
    print(f"card: {a.id} -> {a.card}")
    return 0


def cmd_reopen(a, paths):
    """The reopen names the cards that link the mark NOW; derive ignores exactly those
    links, so the mark is OPEN until a card written later (or a new event) closes it."""
    links = structural_links(read_cards(paths))
    append_events(paths, [{"ts": _now(), "ev": "reopen", "id": i, "by": a.by,
                           "cards": links.get(i, []),
                           "reason": a.reason or ""} for i in a.ids])
    print(f"reopen: {len(a.ids)} id(s)")
    return 0


def cmd_suggest(a, paths):
    floor = floor_of(paths, a.floor)
    marks = derive(paths, floor)
    rows = [mk for mk in marks.values() if mk["state"] == "OPEN" and mk.get("suggested")]
    rows.sort(key=lambda mk: (mk["date"], mk["time"], mk["loc"]))
    for mk in rows[:a.limit]:
        sg = mk["suggested"]
        print(f"{mk['id']} -> {sg['card']} ({sg['how']}) {mk['date']} {mk['time'] or '--:--'} "
              f"[{mk['kind']}] {mk['text'][:90]}")
    print(f"suggest: {len(rows)} OPEN mark(s) with a heuristic card suggestion — a suggestion "
          f"is not coverage; accept one with: mark_ledger.py card <id> <K-id>", file=sys.stderr)
    return 0


def close_unit_review(paths, unit, by, fields, floor=None, prog="review"):
    """THE way a unit's review is closed — `mark-reviewed` and `approve_inbox.py` both call
    it.  First the unit's NOT CARDED verdicts are recorded (#209 A4: closing the unit
    without them sent the marks back to the night's queue), then the marker with the unit's
    sha256 is written.  A failure while recording leaves the unit unreviewed (raises).
    -> (marker path, recorded events)."""
    stem = os.path.basename(unit)[:-3]
    text = open(unit, encoding="utf-8", errors="replace").read()
    got = record_not_carded(paths, text, by, floor=floor)
    recorded = []
    if got is not None:
        recorded, bad, unproven, carded = got
        report_not_carded(prog, bad, unproven, carded)
        print(f"{prog}: {stem}: recorded {len(recorded)} NOT CARDED verdict event(s)")
    marker = os.path.join(paths.reviewed, f".reviewed-{stem}-cards")
    body = f"sha256={sha256_file(unit)}\nunit=memory/inbox/{stem}.md\n"
    body += "".join(f"{k}={v}\n" for k, v in fields.items())
    KL.atomic_text(marker, body)
    return marker, recorded


def cmd_mark_reviewed(a, paths):
    """Close the review of ONE unit as it is now (sha256 in the marker) — for a unit the
    reviewer read and approved nothing from, or approved through a differently named file.
    The unit's NOT CARDED verdicts are recorded on the way (close_unit_review)."""
    rc = 0
    for u in a.units:
        unit = u if os.path.isabs(u) else os.path.join(paths.root, u)
        if not os.path.exists(unit) or not UNIT_RE.match(os.path.basename(unit)):
            print(f"mark-reviewed: {u} is not a review unit in memory/inbox/")
            rc = 1
            continue
        marker, _ = close_unit_review(paths, unit, a.by, {"by": a.by, "reason": a.reason},
                                      floor=a.floor, prog="mark-reviewed")
        print(f"mark-reviewed: {os.path.basename(unit)[:-3]} -> {marker}")
    return rc


def cmd_units(a, paths):
    n = 0
    for f, ok in review_units(paths):
        if a.unreviewed and ok:
            continue
        n += 1
        print(f"{'reviewed  ' if ok else 'UNREVIEWED'} {os.path.relpath(f, paths.root)}")
    print(f"units: {n} listed", file=sys.stderr)
    return 0


def cmd_list(a, paths):
    floor = floor_of(paths, a.floor)
    marks = derive(paths, floor, a.until)
    rows = [mk for mk in marks.values() if not a.state or mk["state"] == a.state.upper()]
    rows.sort(key=lambda mk: (mk["date"], mk["time"], mk["loc"]))
    for mk in rows[:a.limit]:
        extra = mk.get("card") or mk.get("reason") or mk.get("proposed_in") or ""
        if not extra and mk.get("suggested"):
            extra = f"suggested {mk['suggested']['card']} ({mk['suggested']['how']})"
        print(f"{mk['id']} {mk['state']:<9} {mk['date']} {mk['time'] or '--:--'} "
              f"[{mk['kind']}] {mk['text'][:100]}  {extra}")
    print(f"list: {len(rows)} row(s)", file=sys.stderr)
    return 0


def cmd_floor(a, paths):
    print(floor_of(paths, a.floor))
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", "--brain", dest="root", default=None)
    ap.add_argument("--floor", default=None, help="first journal date tracked")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("floor")
    s.set_defaults(fn=cmd_floor)

    s = sub.add_parser("sync")
    s.add_argument("--dry", action="store_true")
    s.add_argument("--until", default=None)
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("queue")
    s.add_argument("--out")
    s.add_argument("--chunk", type=int, default=0)
    s.add_argument("--outdir", default=".")
    s.add_argument("--prefix", default="marks")
    s.add_argument("--since", default=None)
    s.add_argument("--until", default=None)
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("attribute")
    s.add_argument("files", nargs="+")
    s.add_argument("--dry", action="store_true")
    s.set_defaults(fn=cmd_attribute)

    s = sub.add_parser("coverage")
    s.add_argument("files", nargs="+")
    s.add_argument("--queue", required=True)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_coverage)

    s = sub.add_parser("scan")
    s.add_argument("journals", nargs="+")
    s.add_argument("--width", type=int, default=140)
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("dismiss")
    s.add_argument("ids", nargs="*")
    s.add_argument("--reason")
    s.add_argument("--from-inbox")
    s.add_argument("--by", default="coordinator")
    s.set_defaults(fn=cmd_dismiss)

    s = sub.add_parser("recheck-carded")
    s.add_argument("--apply", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--by", default="coordinator")
    s.set_defaults(fn=cmd_recheck_carded)

    s = sub.add_parser("card")
    s.add_argument("id")
    s.add_argument("card")
    s.add_argument("--by", default="coordinator")
    s.set_defaults(fn=cmd_card)

    s = sub.add_parser("reopen")
    s.add_argument("ids", nargs="+")
    s.add_argument("--reason")
    s.add_argument("--by", default="coordinator")
    s.set_defaults(fn=cmd_reopen)

    s = sub.add_parser("suggest")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_suggest)

    s = sub.add_parser("mark-reviewed")
    s.add_argument("units", nargs="+")
    s.add_argument("--reason", required=True)
    s.add_argument("--by", default="coordinator")
    s.set_defaults(fn=cmd_mark_reviewed)

    s = sub.add_parser("units")
    s.add_argument("--unreviewed", action="store_true")
    s.set_defaults(fn=cmd_units)

    s = sub.add_parser("list")
    s.add_argument("--state")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--until", default=None)
    s.set_defaults(fn=cmd_list)

    a = ap.parse_args(argv)
    root = a.root or os.environ.get("MARK_LEDGER_ROOT") or P.get("KHMS_ROOT")
    if a.fn not in (cmd_status, cmd_floor, cmd_queue, cmd_coverage, cmd_scan, cmd_suggest, cmd_units,
                    cmd_list) and not (a.fn is cmd_sync and a.dry):
        P.require_writable(f"run `mark_ledger.py {a.cmd}`")
    return a.fn(a, Paths(root))


if __name__ == "__main__":
    sys.exit(main())
