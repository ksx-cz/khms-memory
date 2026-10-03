#!/usr/bin/env python3
"""mark_ledger.py — the coverage ledger for journal MARKs.

Usage: python3 mark_ledger.py [--root DIR] <subcommand> ...   (`--help` lists them; run by
nightly_distill.sh and approve_inbox.py)

WHY.  A night pipeline with a daily card cap once lost a calibration result: the day overflowed the
cap, the overflow went to a `## DEFERRED` section that nothing ever read again, and nothing said so —
a knowledge item that is neither carded nor consciously dismissed simply stopped existing. The rule
since: no daily card limit at all, everything in the queue is processed, and every tracked MARK is
accounted for.

WHAT.  Every journal MARK of a knowledge-bearing kind gets a stable id and ends in exactly one state:

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

WHAT THE LEDGER DOES NOT COVER (printed by `sync`/`status`): journals before the floor, MARK kinds
outside the tracked kind table, and knowledge that never got a MARK line.  "OPEN 0" therefore means
"every tracked MARK since the floor is carded or dismissed", not "nothing was lost anywhere".

MARK KINDS.  The tracked kinds and their spellings are a table (canonical kind -> aliases).  The
shipped table is English.  A deployment whose journals use other words sets LEDGER_KINDS in its private
patterns file (KHMS_PATTERNS) to `canon:alias,alias;canon:alias,...`, which REPLACES the table.  The
canonical name is part of every mark id (`M-` + sha1 of `<canon>|<text>`), so a deployment keeps its
canonical names for as long as its ledger lives — renaming one re-ids every mark of that kind.

FLOOR.  The first journal day tracked: --floor, else $MARK_LEDGER_FLOOR, else memory/ledger/FLOOR,
else 1970-01-01 (everything).  `floor` prints the one in effect.

Subcommands (all accept --root DIR, the base; default: the resolved KHMS_ROOT):
    sync [--dry]          derive the state, write memory/ledger/{marks.jsonl,
                          summary.json}; --dry writes nothing
    status                one line: OPEN count + oldest OPEN item (hook, review)
    floor                 the floor date in effect
    queue [--out F] [--chunk N --outdir D --prefix P] [--since DATE] [--until DATE]
                          OPEN, not-proposed marks, oldest first
    dedup FILE [--moved-to F]   drop candidates whose MARKs are all already held
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
import glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_messages as M  # noqa: E402
import khms_paths as P  # noqa: E402
import khms_patterns as PT  # noqa: E402

DEFAULT_ROOT = P.soft("ROOT")
# First journal day the ledger tracks when nothing else says (see FLOOR in the docstring).
DEFAULT_FLOOR = "1970-01-01"

# --- which MARKs are knowledge -------------------------------------------------
# Knowledge kinds: decisions, findings, requirements, gotchas, errors, knowledge-base issues,
# measurements and refutations. Status kinds (done, result, board replies, spawned agents, token
# reports, ...) are not knowledge and are not tracked. A deployment replaces this table with
# LEDGER_KINDS (see the docstring); its canonical names are part of the mark ids.
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

# A MARK line: optional bullet, optional ISO stamp or HH:MM, optional
# "(X handoff)" tag, then MARK[kind].  The token must open the line (after
# those prefixes) — prose that merely MENTIONS `MARK[khms-issue]` (rules,
# design notes) is not a mark.
MARK_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?"
    r"(?:(?P<iso>\d{4}-\d{2}-\d{2}T(?P<isot>\d{2}:\d{2})(?::\d{2})?(?:\.\d+)?"
    r"(?:[+-]\d{2}:?\d{2}|Z)?)\s+)?"
    r"(?:(?P<hm>\d{1,2}:\d{2})(?::\d{2})?\s+)?"
    r"(?:\([^)\n]{0,80}\)\s+)?"
    r"MARK\[(?P<kind>[^\]\s]+)\](?:\*\*)?\s*(?P<text>.*)$")
LEAD_TIME_RE = re.compile(r"^(?P<hm>\d{1,2}:\d{2})(?::\d{2})?\b\s*")
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


def parse_line(line):
    """-> (canonical kind, time 'HH:MM' or '', text) or None."""
    m = MARK_RE.match(line)
    if not m:
        return None
    kind = CANON.get(m.group("kind").lower())
    if not kind:
        return None
    text = m.group("text").strip()
    if not text:
        return None
    hm = m.group("isot") or m.group("hm") or ""
    if not hm:
        lt = LEAD_TIME_RE.match(text)
        if lt:
            hm = lt.group("hm")
    if hm and len(hm) == 4:
        hm = "0" + hm
    return kind, hm, text


def scan_journal(path):
    """-> list of mark dicts found in one journal file (first occurrence per id)."""
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
            p = parse_line(line.rstrip("\n"))
            if not p:
                continue
            kind, hm, text = p
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


def collect_marks(paths, floor, until=None):
    """-> ordered dict id -> mark; a mark seen on several days keeps its first."""
    marks = {}
    for f in journal_files(paths, floor, until):
        for mk in scan_journal(f):
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

    review P1-1 (Diblin, 2026-10-01): an id mentioned in a card's prose ("This
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
UNIT_RE = re.compile(r"^(?P<stem>(?P<date>\d{4}-\d{2}-\d{2})"
                     r"(?P<tail>-weekly|(?:-\d+)?-b\d{2,})?)\.md$")
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


def proposed_ids(paths, extra=()):
    prop = {}
    for f in list(pending_files(paths)) + list(extra):
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
    with open(os.path.join(paths.ledger, ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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


def derive(paths, floor, until=None, extra_pending=()):
    """State of every tracked mark.  CARDED only by a structured link — the card's
    `marks:` frontmatter or the coordinator's `card` event; the LAST event of an id
    wins; a `reopen` ignores the `marks:` links of the cards it names (the ones that
    existed when it was recorded), so it really reopens, and a card written later closes
    the mark again.  Heuristic matches are kept as `suggested`, never as coverage."""
    marks = collect_marks(paths, floor, until)
    events = read_events(paths)
    cards = read_cards(paths)
    links = structural_links(cards)
    suggested = suggest_cards(marks, cards, floor)
    known_cards = {c[0] for c in cards}
    prop = proposed_ids(paths, extra_pending)
    for mid, mk in marks.items():
        ev = events.get(mid) or {}
        kind = ev.get("ev")
        linked = links.get(mid, [])
        if kind == "reopen":
            gone = set(ev.get("cards") or [])
            linked = [c for c in linked if c not in gone]
        state, card, how, reason = "OPEN", "", "", ""
        if kind == "dismiss":
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
    return marks


def carry_count(paths):
    """Consolidate batches still waiting (flat legacy names and run directories)."""
    return len([f for f in carry_files(paths)
                if re.search(r"(?:^|-)cons-\d+\.md$", os.path.basename(f))])


TRACKED_KINDS = sorted(KIND_ALIASES)
NOT_COVERED = ("journals before the floor; MARK kinds outside the tracked list "
               "(status kinds: done, result, board-reply, ...); knowledge without a MARK "
               "line")


def summarize(marks, floor, paths=None):
    counts = {"OPEN": 0, "CARDED": 0, "DISMISSED": 0}
    proposed = suggested = 0
    oldest = None
    for mk in marks.values():
        counts[mk["state"]] += 1
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
        "scope": {"floor": floor, "kinds": TRACKED_KINDS, "not_covered": NOT_COVERED},
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
                 nkinds=len(s.get("scope", {}).get("kinds", TRACKED_KINDS)))


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


def read_summary(root=DEFAULT_ROOT):
    try:
        with open(Paths(root).summary, encoding="utf-8") as fh:
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


def dedup_text(text, held):
    """Candidates whose `MARKS:` ids are ALL already held — CARDED/DISMISSED, or proposed
    in an unreviewed unit or a carry batch — leave the candidate list (0 tokens).  A sweep
    reads every journal FILE touched since the stamp, so a second run on the same day (or a
    journal appended after the night) re-proposes marks a pending batch already holds;
    without this they were consolidated and proposed twice (KHMSFIX, review P1-2's
    exactly-once).  Candidates without a MARKS line are untouched.  Returns
    (new text, [(label, ids, removed block)])."""
    heads = list(CAND_RE.finditer(text))
    if not heads:
        return text, []
    out, pos, moved = [], 0, []
    for i, h in enumerate(heads):
        start = h.start()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        tm = TRAILER_RE.search(text, h.end(), end)
        if tm:
            end = tm.start()
        block = text[start:end]
        out.append(text[pos:start])
        have = MARKS_LINE_RE.search(block)
        ids = ID_RE.findall(have.group(1)) if have else []
        if ids and all(i_ in held for i_ in ids):
            moved.append((h.group(1).split(":")[0].lstrip("# ").strip(), ids, block))
        else:
            out.append(block)
        pos = end
    out.append(text[pos:])
    return "".join(out), moved


# --- CLI ----------------------------------------------------------------------------
def cmd_sync(a, paths):
    floor = floor_of(paths, a.floor)
    if a.dry:
        marks = derive(paths, floor, a.until, a.pending or ())
        s = summarize(marks, floor, paths)
    else:
        with ledger_lock(paths):
            marks = derive(paths, floor, a.until, a.pending or ())
            s = summarize(marks, floor, paths)
            write_state(paths, marks, s)
    print(status_line(s))
    print(f"total tracked marks: {s['total']} (floor {floor}"
          f"{', until ' + a.until if a.until else ''}){' [dry: nothing written]' if a.dry else ''}")
    print(f"scope: kinds {', '.join(TRACKED_KINDS)}; NOT covered: {NOT_COVERED}")
    return 0


def cmd_status(a, paths):
    s = read_summary(paths.root)
    print(status_line(s))
    return 0


def cmd_queue(a, paths):
    floor = floor_of(paths, a.floor)
    marks = derive(paths, floor, a.until, a.pending or ())
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


def cmd_dedup(a, paths):
    floor = floor_of(paths, a.floor)
    marks = derive(paths, floor)
    held = {mid: (mk.get("card") or mk.get("reason") or mk.get("proposed_in") or mk["state"])
            for mid, mk in marks.items()
            if mk["state"] != "OPEN" or mk.get("proposed_in")}
    text = open(a.file, encoding="utf-8").read()
    new, moved = dedup_text(text, held)
    if moved and not a.dry:
        if a.moved_to:
            with open(a.moved_to, "a", encoding="utf-8") as fh:
                for lab, ids, block in moved:
                    fh.write(f"<!-- {lab}: every MARK already held: "
                             f"{'; '.join(f'{i} -> {held[i]}' for i in ids)} -->\n{block}\n")
        tmp = a.file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(tmp, a.file)
    print(f"dedup: {len(moved)} candidate(s) whose MARKs are all already carded or proposed "
          f"left {a.file}{' (dry)' if a.dry else ''}"
          f"{' -> ' + a.moved_to if moved and a.moved_to else ''}")
    for lab, ids, _b in moved:
        print(f"  {lab}: {' '.join(ids)}")
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


def cmd_dismiss(a, paths):
    floor = floor_of(paths, a.floor)
    known = _known_ids(paths, floor)
    evs, bad = [], []
    if a.from_inbox:
        text = open(a.from_inbox, encoding="utf-8").read()
        sec = re.search(rf"^{re.escape(NOT_CARDED_HEAD)}.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
        if not sec:
            print(f"dismiss: no '{NOT_CARDED_HEAD}' section in {a.from_inbox}")
            return 1
        cards = {os.path.basename(f)[:-3] for f in glob.glob(os.path.join(paths.know, "K-*.md"))}
        for line in sec.group(1).splitlines():
            m = re.match(r"^\s*-\s*(M-[0-9a-f]{8})\s*::\s*(.+?)\s*$", line)
            if not m:
                continue
            mid, verdict = m.group(1), m.group(2)
            if mid not in known:
                bad.append(mid)
                continue
            k = CARD_ID_RE.search(verdict)
            if re.match(r"(?i)already carded", verdict) and k and k.group(0) in cards:
                evs.append({"ts": _now(), "ev": "card", "id": mid, "card": k.group(0),
                            "by": a.by, "reason": f"nightly verdict accepted: {verdict}"})
            else:
                evs.append({"ts": _now(), "ev": "dismiss", "id": mid, "by": a.by,
                            "reason": f"nightly verdict accepted at review: {verdict}"})
    else:
        if not a.reason:
            print("dismiss: --reason is required (a dismissal without a reason is a silent loss)")
            return 2
        for mid in a.ids:
            if mid not in known:
                bad.append(mid)
                continue
            evs.append({"ts": _now(), "ev": "dismiss", "id": mid, "by": a.by,
                        "reason": a.reason})
    if bad:
        print(f"dismiss: unknown id(s), not recorded: {' '.join(bad)}")
    append_events(paths, evs)
    print(f"dismiss: recorded {len(evs)} event(s) in {paths.events}")
    return 1 if bad and not evs else 0


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


def cmd_mark_reviewed(a, paths):
    """Close the review of ONE unit as it is now (sha256 in the marker) — for a unit the
    reviewer read and approved nothing from, or approved through a differently named file."""
    rc = 0
    for u in a.units:
        unit = u if os.path.isabs(u) else os.path.join(paths.root, u)
        if not os.path.exists(unit) or not UNIT_RE.match(os.path.basename(unit)):
            print(f"mark-reviewed: {u} is not a review unit in memory/inbox/")
            rc = 1
            continue
        stem = os.path.basename(unit)[:-3]
        os.makedirs(paths.reviewed, exist_ok=True)
        marker = os.path.join(paths.reviewed, f".reviewed-{stem}-cards")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"sha256={sha256_file(unit)}\nunit=memory/inbox/{stem}.md\n"
                     f"by={a.by}\nreason={a.reason}\n")
        print(f"mark-reviewed: {stem} -> {marker}")
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
    ap.add_argument("--root", "--brain", dest="root", default=None,
                    help="the base (default: the resolved KHMS_ROOT)")
    ap.add_argument("--floor", default=None, help="first journal date tracked")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("floor")
    s.set_defaults(fn=cmd_floor)

    s = sub.add_parser("sync")
    s.add_argument("--dry", action="store_true")
    s.add_argument("--until", default=None)
    s.add_argument("--pending", nargs="*", help="extra files counted as pending proposals")
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
    s.add_argument("--pending", nargs="*")
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("attribute")
    s.add_argument("files", nargs="+")
    s.add_argument("--dry", action="store_true")
    s.set_defaults(fn=cmd_attribute)

    s = sub.add_parser("dedup")
    s.add_argument("file")
    s.add_argument("--moved-to", default=None)
    s.add_argument("--dry", action="store_true")
    s.set_defaults(fn=cmd_dedup)

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
    reads_only = (a.fn in (cmd_status, cmd_floor, cmd_coverage, cmd_scan, cmd_suggest, cmd_units, cmd_list)
                  or (a.fn is cmd_sync and a.dry) or (a.fn is cmd_queue and not a.out and not a.chunk))
    if not reads_only:
        P.require_writable(f"run `mark_ledger.py {a.cmd}`")
    return a.fn(a, Paths(root))


if __name__ == "__main__":
    sys.exit(main())
