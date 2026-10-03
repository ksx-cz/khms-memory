#!/usr/bin/env python3
"""Check that every sentence a journal entry attributes to the principal is really theirs.

Usage: python3 tools/verify_owner_quotes.py --journal DIR|FILE --history H.jsonl --owner-id ID \
           [--journal-tz ZONE] [--self-test] [--suggest]          (run by nightly_distill.sh as KHMS_CHAT_VERIFY)

WHY THIS EXISTS
---------------
In the reference deployment a handoff written for the agent's own fresh context quoted the principal
as saying "do nothing". What the principal had written, eleven hours earlier, was a DELEGATION rule:
"you do nothing yourself — hand every package to a subagent with everything it needs". The journal cut
the sentence after its first clause, reversed two words and moved it next to an unrelated message. Read
after a context reset it became a stop order and nearly halted the queue.

So the defect class this guards is not fabrication.  It is **a true sentence severed
from its second half and re-dated** — which reads as authentic precisely because the
words really were said.  Two consequences for the design:

  * words alone are not enough: a quote whose attributed time is far from the real
    message's time is ungrounded even when every word matches (--max-hours),
  * a checker of quotes needs a POSITIVE CONTROL inside the same invocation.  The
    first diagnosis of this very defect concluded "never written" from a 30-message
    window while the local archive was 39 days stale — a search that could not have
    found the sentence, reporting that the sentence did not exist.  --self-test
    therefore proves, before any verdict is printed, that this code (a) finds a quote
    known to be in the history, (b) rejects a fabricated one, (c) rejects a real quote
    hung on the wrong date, (d) does not take the agent's own words addressed TO the
    principal as the principal's.  No SELFTEST OK, no trustworthy clean scan: the run
    exits nonzero regardless of how clean it looked.

WHAT IT DOES NOT DO
-------------------
It does not fetch the chat.  The archive is refreshed by the deployment's own command
(KHMS_CHAT_FETCH, run first by the night) and the normalisation of the comparison —
smart quotes, decimal comma vs point, markdown emphasis — is imported from
`tools/verify_quotes.py`.  This tool is the JOIN: extract principal-attributed quotes
from journal/*.md, ground each one in the chat record.

THE CHAT RECORD
    JSONL, one message per line: {"timestamp": ISO-8601, "content": str, "author": {"id": str}}
    (the shape of a chat-platform channel export). Only messages whose author id is --owner-id count.

LANGUAGE DATA (the private patterns file, KHMS_PATTERNS; see khms_patterns.py)
    Who is quoted is decided by the NAME FORM that introduces the quote: the speaker form ("Ann: '…'",
    "Ann wrote '…'") and the possessive form ("Ann's request '…'") attribute the words to the
    principal; any other form (in an inflected language: the dative "I wrote TO Ann '…'") does not.
    OWNERQ_NAME=<display name>              REQUIRED; without it the tool exits 2 (no clean scan)
    OWNERQ_SPEAKER=<regex>                  speaker form (default: the name as a whole word)
    OWNERQ_POSSESSIVE=<regex>               possessive form (default: the name + 's)
    OWNERQ_SPEECH_VERBS=<alternation>       verbs that attribute a quote after the speaker form
    OWNERQ_NOUNS=<alternation>              nouns that attribute a quote after the possessive form
    OWNERQ_FILLERS=<alternation>            prepositions / day words allowed next to such a noun
    OWNERQ_OWN_VOICE=<regex>                tokens that mark the AGENT's voice in the connector
    OWNERQ_CORRECTION_STAMP=<regex>         the word before a date that dates a correction, not a quote
    OWNERQ_SELFTEST_QUOTE=<text>            REQUIRED for --self-test: a sentence the principal wrote
    OWNERQ_SELFTEST_FABRICATED=<text>       a sentence the principal never wrote
    OWNERQ_SELFTEST_NOT_HIS=<template>      a journal line with the agent's words TO the principal;
                                            {name} and {quote} are filled in; must extract nothing

OUTPUT CONTRACT
    UNGROUNDED <file>:<line> [absent|time] ...   one line per violation
    ERROR ...                                    tool fault (unreadable input, ...)
    SELFTEST OK                                  positive+negative control passed
    <summary counts>
Exit 0 only when the self-test passed AND no UNGROUNDED and no ERROR line was printed; 2 when the
input or the configuration makes a scan impossible.
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
import unicodedata
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_quotes import norm  # noqa: E402  (typography normalisation, already solved)
import khms_patterns as KP  # noqa: E402  (the deployment's language data: OWNERQ_* keys)

JOURNAL_TZ_DEFAULT = "UTC"                  # journal clock times are local (--journal-tz); chat times are UTC
JOURNAL_TZ = JOURNAL_TZ_DEFAULT
MAX_HOURS_DEFAULT = 6.0
# Below this a "quote" is a fragment, not a sentence: 'nic', 'Ok go', '5 a 6'. The
# threshold has to admit the sentence that motivated the tool — "nic nedelej", two
# words / eleven characters — so it sits just under it and no lower.
MIN_WORDS = 2
MIN_CHARS = 10
MAX_CONNECTOR = 34          # how far the name may stand from the quote it attributes


# ---------------------------------------------------------------- text comparison
def fold(s: str) -> str:
    """Comparison form: verify_quotes.norm + diacritics stripped + punctuation to space.

    A principal may type without diacritics while the journal quotes them back WITH
    them, and the journal re-punctuates freely: "mount it far from the pivot (longer
    lever = finer angle)" comes back as "mount it far from the pivot, longer lever =
    finer angle".  Both sides pass through here, so folding is symmetric and hides no
    real difference of wording — only of typing.  What it deliberately does NOT hide
    is word order: "nothing do" does not fold onto "do nothing", which is the whole
    point.
    """
    s = norm(s)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.findall(r"[0-9a-z]+", s))


ELISION_RE = re.compile(r"\.{3}|…|\[\s*\.{3}\s*\]|\[\s*…\s*\]")


def segments(quote: str):
    """A quote split at its own elision marks; each segment must occur, in order."""
    return [f for f in (fold(p) for p in ELISION_RE.split(quote)) if f]


def find_in(haystack: str, segs) -> bool:
    """All segments occur in order, each as a whole-token run."""
    pos = 0
    hay = " " + haystack + " "
    for seg in segs:
        i = hay.find(" " + seg + " ", pos)
        if i < 0:
            return False
        pos = i + len(seg)
    return True


# ---------------------------------------------------------------- the chat record
class History:
    def __init__(self, path, owner_id):
        self.msgs = []          # (aware datetime UTC, raw content, folded content)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if (d.get("author") or {}).get("id") != owner_id:
                    continue
                content = d.get("content") or ""
                if not content.strip():
                    continue
                ts = dt.datetime.fromisoformat(d["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                self.msgs.append((ts, content, fold(content)))
        self.msgs.sort(key=lambda m: m[0])

    def __len__(self):
        return len(self.msgs)

    @property
    def span(self):
        return (self.msgs[0][0], self.msgs[-1][0]) if self.msgs else (None, None)

    def matches(self, quote):
        """Every owner message containing the quote (elisions and inserts honoured).

        Two readings are tried: the quote as written, and the quote with bracketed
        editorial inserts removed — `"switch it to [stereo mode]. did you do it…"`,
        where the bracket is the AGENT's clarification inside the principal's sentence,
        the standard convention.  Trying both only ever adds matches, and what a
        bracket can remove is the agent's own words, never the principal's: a severed
        or reordered sentence stays unmatched under either reading.
        """
        readings = [segments(quote)]
        stripped = re.sub(r"\[[^\]\.]{1,60}\]", " ", quote)
        if stripped != quote:
            readings.append(segments(stripped))
        out = []
        for m in self.msgs:
            if any(segs and find_in(m[2], segs) for segs in readings):
                out.append(m)
        return out

    def nearest_partial(self, quote):
        """Longest leading run of the quote's words that DOES occur, for diagnostics.

        Prints the difference between "never said anything like this" and "said it
        and the journal altered it" — the second is the defect this tool exists for
        and the first diagnosis of it got that distinction wrong.
        """
        words = " ".join(segments(quote)).split()
        best = (0, None)
        for n in range(len(words), 1, -1):
            probe = " ".join(words[:n])
            for m in self.msgs:
                if find_in(m[2], [probe]):
                    return n, m
        return best


# ---------------------------------------------------------------- quote extraction
# The NAME FORM carries the attribution. In an inflected language the case ending decides:
# the nominative ("<name>: ...", "<name> wrote ...": the principal is the speaker) and the
# genitive ("<name>'s '...'": the words are theirs) are quotes OF the principal; the dative
# ("I wrote TO <name> '...'") and the instrumental ("verified BY <name>") are quotes TO, or
# actions BY, them — measured on the reference corpus, the dative is where the AGENT'S OWN
# sentences live, so treating the name alone as an anchor mis-attributes the agent's words to
# the principal. That is the same error the tool is built to catch, made by the tool.
# The forms and the words around them are the deployment's language data (OWNERQ_* keys).
OWNER_NAME = KP.value("OWNERQ_NAME", "")
# (the default speaker form stops before an English possessive, which the possessive form takes)
NOM = KP.value("OWNERQ_SPEAKER", re.escape(OWNER_NAME) + r"(?!\w|['’]s\b)" if OWNER_NAME else "")
GEN = KP.value("OWNERQ_POSSESSIVE", re.escape(OWNER_NAME) + r"['’]s\b" if OWNER_NAME else "")
QUOTE_RE = re.compile(
    r"„([^„“”\n]{2,400})[“”]"          # „…“ (low-high)
    r"|“([^“”\n]{2,400})”"             # “…”
    r"|\"([^\"\n]{2,400})\""           # "…"
    r"|'([^'\n]{2,400})'"              # '…'
)
SPEECH_VERB = re.compile(r"\b(?:" + KP.value(
    "OWNERQ_SPEECH_VERBS",
    r"said|says|wrote|writes|asked|asks|answered|replied|confirmed|insisted|added|noted|"
    r"decided|approved|rejected|corrected|ordered|wants|wanted|repeated|stressed|verbatim|quote") + r")\b")
GEN_NOUN = re.compile(r"\b(?:" + KP.value(
    "OWNERQ_NOUNS",
    r"questions?|requests?|instructions?|orders?|words?|sentences?|messages?|quotes?|replies|reply|"
    r"answers?|comments?|complaints?|decisions?|objections?|notes?|remarks?|verdicts?|wording") + r")\b")
FILLERS = KP.value("OWNERQ_FILLERS", r"from|of|on|to|in|at|today|yesterday")
OWN_VOICE = KP.value("OWNERQ_OWN_VOICE", r"\bI\b|\bme\b|\bmy\b")
CORRECTION_STAMP = KP.value("OWNERQ_CORRECTION_STAMP", r"CORRECTION")
DATE_ONLY = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
ISO_TS = re.compile(r"\b(20\d\d-\d\d-\d\d)[T ](\d\d:\d\d(?::\d\d)?)\s*(Z|UTC|\+\d\d:?\d\d)?")
CLOCK = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
STRIKE_RE = re.compile(r"~~(.+?)~~", re.S)


def attribution_ok(case, connector):
    """Does `connector` between the name and the quote mark the quote as the principal's words?

    Deliberately narrow.  A quote several clauses downstream of the name is not
    attributed by that name, and taking it as such is how this scanner would invent
    the very defect it hunts: 'the card records that I read this camera as "mono",
    <name> corrected me, and the card states the mechanism — "I declared video1
    without checking…"' quotes the CARD, not the principal.  So: no clause boundary
    (comma, semicolon, dash) and no own-voice token may sit between the name and the
    quote, and the connector stays short.
    """
    # Strikethrough markers belong to the retraction, not to the attribution: a struck
    # quote must still be EXTRACTED so it is counted as retracted, rather than falling
    # out of the scan and leaving no trace that a citation was withdrawn.
    c = connector.strip().strip("~").strip()
    if len(c) > MAX_CONNECTOR or re.search(r"[,;–—]|" + OWN_VOICE, c):
        return False
    if case == "GEN":
        # "<name>'s 'do nothing'", "<name>'s question '...'", "<name>'s order of 2026-08-11 '...'"
        c2 = re.sub(r"^[\s:\(\[]+|[\s:\(\[]+$", "", c)
        if not c2:
            return True
        core = re.sub(r"\b(?:" + FILLERS + r")\b|"
                      r"\b20\d\d-\d\d-\d\d\b|[\s:\(\[\)\]]+", " ", c2).strip()
        return bool(GEN_NOUN.fullmatch(core)) or bool(DATE_ONLY.search(c2))
    # NOM: a colon, a speech verb, or a bare date ("<name> 2026-08-11, '...'")
    if re.match(r"^[\s\w]*[:\(]\s*$", c) and (":" in c or "(" in c):
        return True
    return bool(SPEECH_VERB.search(c)) or bool(DATE_ONLY.search(c))


def extract(text, path, day):
    """Yield dicts for every owner-attributed quote in one journal file."""
    lines = text.splitlines(keepends=True)
    starts, off = [], 0
    for ln in lines:
        starts.append(off)
        off += len(ln)

    def lineno(pos):
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    struck = [(m.start(), m.end()) for m in STRIKE_RE.finditer(text)]

    out = []
    for anchor in re.finditer(f"({NOM})|({GEN})", text):
        case = "NOM" if anchor.group(1) else "GEN"
        # The connector (name -> opening delimiter) is short by rule; the quote itself
        # may be long, and the window must hold its CLOSING delimiter too or the quote
        # is simply not seen.  Measured 2026-09-01: a 100-char window silently dropped
        # every corrected citation longer than ~70 characters — a scanner that goes
        # quiet on long quotes is a scanner you can pass by writing a long quote.
        window = text[anchor.end():anchor.end() + MAX_CONNECTOR + 420]
        q = QUOTE_RE.search(window)
        if not q or q.start() > MAX_CONNECTOR:
            continue
        connector = window[:q.start()]
        if "\n\n" in connector or not attribution_ok(case, connector):
            continue
        quote = next(g for g in q.groups() if g is not None)
        qpos = anchor.end() + q.start()
        n = lineno(qpos)
        line = lines[n - 1] if n - 1 < len(lines) else ""
        out.append({
            "file": path, "line": n, "quote": quote, "pos": qpos,
            "retracted": any(a <= qpos < b for a, b in struck),
            "attr": attributed_time(lines, n, day, line, qpos - starts[n - 1]),
        })
    return out


def nearest(rx, line, col):
    """The match of `rx` on `line` closest to column `col` (the quote's own column).

    A journal line can carry several dates — the day it is written and the day it
    cites.  The one that dates a quote is the one standing next to it, which is also
    how a correction is written: `<name>'s order 2026-08-31T20:04:48Z: "…"`.
    """
    ms = list(rx.finditer(line))
    return min(ms, key=lambda m: abs(m.start() - col)) if ms else None


def attributed_time(lines, n, day, line, col):
    """(kind, t_lo, t_hi) the journal claims for this quote, as an aware UTC interval.

    Precedence: an explicit ISO timestamp on the line (how a correction records the
    real message) > an explicit date > a clock time on the line or the nearest
    preceding one (journal local time) > the whole journal day.
    """
    tz = ZoneInfo(JOURNAL_TZ)
    # A `<correction stamp> <date>` dates the CORRECTION, not the sentence being corrected.
    # Left in, it re-attributed every corrected citation to the day it was fixed —
    # the tool committing the exact error it exists to catch, on its own repairs.
    line = re.sub(r"((?:" + CORRECTION_STAMP + r")\s+)20\d\d-\d\d-\d\d",
                  lambda m: m.group(1) + " " * 10, line)   # blank, not delete: keeps columns
    m = nearest(ISO_TS, line, col)
    if m:
        date, clock, zone = m.group(1), m.group(2), m.group(3)
        if len(clock) == 5:
            clock += ":00"
        t = dt.datetime.fromisoformat(f"{date}T{clock}")
        t = t.replace(tzinfo=dt.timezone.utc if zone else tz)
        return ("iso", t.astimezone(dt.timezone.utc), t.astimezone(dt.timezone.utc))
    d = nearest(DATE_ONLY, line, col)
    if d:
        base = dt.date(int(d.group(1)), int(d.group(2)), int(d.group(3)))
        lo = dt.datetime.combine(base, dt.time(0, 0), tz)
        return ("date", lo.astimezone(dt.timezone.utc),
                (lo + dt.timedelta(days=1)).astimezone(dt.timezone.utc))
    for k in range(n - 1, max(-1, n - 6), -1):
        c = nearest(CLOCK, lines[k], col if k == n - 1 else 0) if k < len(lines) else None
        if c:
            t = dt.datetime.combine(day, dt.time(int(c.group(1)), int(c.group(2))), tz)
            u = t.astimezone(dt.timezone.utc)
            return ("clock", u, u)
    lo = dt.datetime.combine(day, dt.time(0, 0), tz)
    return ("day", lo.astimezone(dt.timezone.utc),
            (lo + dt.timedelta(days=1)).astimezone(dt.timezone.utc))


def gap_hours(attr, t):
    _, lo, hi = attr
    if t < lo:
        return (lo - t).total_seconds() / 3600.0
    if t > hi:
        return (t - hi).total_seconds() / 3600.0
    return 0.0


# ---------------------------------------------------------------- verdicts
def judge(item, hist, max_hours):
    """('grounded'|'absent'|'time'|'short'|'retracted', detail)."""
    if item["retracted"]:
        return "retracted", None
    f = " ".join(segments(item["quote"]))
    if len(f.split()) < MIN_WORDS or len(f) < MIN_CHARS:
        return "short", None
    hits = hist.matches(item["quote"])
    if not hits:
        return "absent", hist.nearest_partial(item["quote"])
    best = min(hits, key=lambda m: gap_hours(item["attr"], m[0]))
    g = gap_hours(item["attr"], best[0])
    if g > max_hours:
        return "time", (g, best)
    return "grounded", best


def fmt(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def suggest(item, hist, top=3):
    """Nearest real messages by similarity — an AID FOR THE FIX, never a verdict.

    Deliberately kept out of judge(): a fuzzy score would have swallowed the case
    this tool was built for.  "nothing do" scores high against "do nothing",
    so any similarity threshold loose enough to forgive re-punctuation also forgives
    a reversal that inverts the meaning.  Similarity may therefore only ever point a
    human at the real sentence; it may never pronounce one grounded.
    """
    import difflib
    q = " ".join(segments(item["quote"]))
    toks = {t for t in q.split() if len(t) >= 4}
    out = []
    for t, raw, f in hist.msgs:
        if toks and not (toks & set(f.split())):
            continue
        r = difflib.SequenceMatcher(None, q, f).ratio()
        out.append((r, t, raw))
    out.sort(key=lambda x: -x[0])
    return out[:top]


def scan(files, hist, max_hours, emit, with_suggest=False):
    counts = dict(checked=0, grounded=0, absent=0, time=0, short=0, retracted=0)
    for path in files:
        base = os.path.basename(path)
        m = re.match(r"(20\d\d)-(\d\d)-(\d\d)\.md$", base)
        if not m:
            continue
        day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for item in extract(text, path, day):
            verdict, detail = judge(item, hist, max_hours)
            counts[verdict if verdict != "grounded" else "grounded"] += 1
            if verdict in ("grounded", "absent", "time"):
                counts["checked"] += 1
            q = item["quote"].replace("\n", " ")[:110]
            if verdict == "absent":
                n, msg = detail
                hint = (f' nearest: {n} leading words match {fmt(msg[0])} '
                        f'{msg[1][:90]!r}' if msg else " nearest: no run of >=2 words matches")
                emit(f'UNGROUNDED {item["file"]}:{item["line"]} [absent] "{q}" '
                     f'attributed {item["attr"][0]}={fmt(item["attr"][1])};{hint}')
            elif verdict == "time":
                g, msg = detail
                emit(f'UNGROUNDED {item["file"]}:{item["line"]} [time] "{q}" '
                     f'attributed {item["attr"][0]}={fmt(item["attr"][1])} but the '
                     f'message is {fmt(msg[0])} ({g:.1f} h away, limit {max_hours} h): '
                     f'{msg[1][:120]!r}')
            if with_suggest and verdict in ("absent", "time"):
                for r, t, raw in suggest(item, hist):
                    emit(f"    candidate {r:.2f} {fmt(t)} {raw[:200]!r}")
    return counts


# ---------------------------------------------------------------- positive control
# The self-test sentences are the deployment's data: a sentence the principal really wrote (it must be
# in the archive), one never written, and a line carrying the agent's own words addressed TO the principal.
SELFTEST_QUOTE = KP.value("OWNERQ_SELFTEST_QUOTE", "")
SELFTEST_FABRICATED = KP.value("OWNERQ_SELFTEST_FABRICATED", "switch the robot off and go to sleep forever")
SELFTEST_NOT_HIS = KP.value("OWNERQ_SELFTEST_NOT_HIS", 'I replied to {name}, "{quote}"')


def self_test(hist, max_hours):
    """Prove in THIS invocation that the scanner can find, and can fail.

    Returns (ok, [messages]).  Nothing here prints UNGROUNDED even when a case is
    expected to be ungrounded: the word is the output contract for real findings,
    and a control must not be able to fake one.
    """
    notes = []
    if not SELFTEST_QUOTE:
        return False, ["positive control: no OWNERQ_SELFTEST_QUOTE configured — nothing known to be "
                       "in the history, so the search cannot be shown to find anything"]
    hits = hist.matches(SELFTEST_QUOTE)
    if not hits:
        return False, [f"positive control: {SELFTEST_QUOTE!r} not found in history "
                       f"({len(hist)} owner messages) — the search cannot find what is there"]
    real = hits[0][0]
    notes.append(f"positive control: {SELFTEST_QUOTE!r} found {len(hits)}x, "
                 f"first {fmt(real)}")

    local_day = real.astimezone(ZoneInfo(JOURNAL_TZ))
    name = OWNER_NAME
    right = local_day.strftime("%Y-%m-%d")
    right_clock = local_day.strftime("%H:%M")
    shifted = (local_day + dt.timedelta(days=3)).strftime("%Y-%m-%d")

    cases = [
        (right, f'- {right_clock} MARK[x] {name}: "{SELFTEST_QUOTE}" (sess test)\n',
         "grounded", "a real quote on its real date must ground"),
        (right, f'- {right_clock} MARK[x] {name}: "{SELFTEST_FABRICATED}" (sess test)\n',
         "absent", "a fabricated quote must not ground"),
        (shifted, f'- {right_clock} MARK[x] {name}: "{SELFTEST_QUOTE}" (sess test)\n',
         "time", "a real quote on the wrong date must not ground"),
        (right, f'- {right_clock} MARK[x] '
                + SELFTEST_NOT_HIS.replace("{name}", name).replace("{quote}", SELFTEST_FABRICATED) + '\n',
         None, "the agent's own words addressed TO the principal are not the principal's"),
    ]
    ok = True
    for date, text, want, why in cases:
        day = dt.date.fromisoformat(date)
        items = extract(text, f"<selftest {date}>", day)
        if want is None:
            if items:
                ok = False
                notes.append(f"control FAILED ({why}): extracted {items[0]['quote']!r}")
            else:
                notes.append(f"control ok: {why}")
            continue
        if not items:
            ok = False
            notes.append(f"control FAILED ({why}): nothing extracted from {text.strip()!r}")
            continue
        got, _ = judge(items[0], hist, max_hours)
        if got != want:
            ok = False
            notes.append(f"control FAILED ({why}): expected {want}, got {got}")
        else:
            notes.append(f"control ok: {why} -> {got}")
    return ok, notes


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journal", required=True,
                    help="journal directory or a single YYYY-MM-DD.md file")
    ap.add_argument("--history", required=True, help="chat channel JSONL (see THE CHAT RECORD)")
    ap.add_argument("--owner-id", required=True, help="the principal's author id in the chat record")
    ap.add_argument("--journal-tz", default=JOURNAL_TZ_DEFAULT,
                    help="IANA zone of the journal's clock times (default UTC)")
    ap.add_argument("--max-hours", type=float, default=MAX_HOURS_DEFAULT,
                    help="how far the journal's attributed time may sit from the real "
                         "message before the quote counts as re-dated (default 6)")
    ap.add_argument("--max-age-hours", type=float, default=24.0,
                    help="refuse to judge against an archive older than this")
    ap.add_argument("--self-test", action="store_true",
                    help="run the positive+negative controls (required for a trusted pass)")
    ap.add_argument("--suggest", action="store_true",
                    help="for every finding, print the most similar real messages "
                         "(an aid for writing the correction, never a verdict)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    global JOURNAL_TZ
    JOURNAL_TZ = args.journal_tz

    errors = 0

    def err(msg):
        nonlocal errors
        errors += 1
        print(f"ERROR {msg}")

    if not OWNER_NAME:
        # No name form = no anchor: the scan would extract nothing and could only look clean.
        err("no principal name configured (OWNERQ_NAME in the patterns file) — nothing can be attributed")
        return 2
    try:
        ZoneInfo(JOURNAL_TZ)
    except Exception as exc:  # noqa: BLE001 (any lookup failure is a configuration error)
        err(f"unknown --journal-tz {JOURNAL_TZ!r}: {exc}")
        return 2
    if not os.path.exists(args.history):
        err(f"history not found: {args.history}")
        return 2
    age_h = (dt.datetime.now().timestamp() - os.stat(args.history).st_mtime) / 3600.0
    if age_h > args.max_age_hours:
        # The founding defect of this item was a 39-day-old archive read as if it
        # were the record.  A stale archive is not a clean scan, it is no scan.
        err(f"history is {age_h:.1f} h old (limit {args.max_age_hours} h) — "
            f"refresh it first (the night runs KHMS_CHAT_FETCH before this check)")
        return 2
    try:
        hist = History(args.history, args.owner_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        err(f"history unreadable: {exc}")
        return 2
    if not len(hist):
        err(f"no messages by owner {args.owner_id} in {args.history}")
        return 2

    if os.path.isdir(args.journal):
        files = sorted(glob.glob(os.path.join(args.journal, "*.md")))
    else:
        files = [args.journal]
    if not files:
        err(f"no journal files under {args.journal}")
        return 2

    lo, hi = hist.span
    print(f"history: {len(hist)} owner messages, {fmt(lo)} .. {fmt(hi)}, "
          f"archive age {age_h:.1f} h")
    print(f"journal: {len(files)} files, {os.path.dirname(files[0]) or '.'}")

    st_ok = None
    if args.self_test:
        st_ok, notes = self_test(hist, args.max_hours)
        if args.verbose or not st_ok:
            for n in notes:
                print(f"  selftest· {n}")
        if st_ok:
            print("SELFTEST OK")
        else:
            err("self-test failed — this run proves nothing about the journal")

    findings = []
    counts = scan(files, hist, args.max_hours, findings.append, args.suggest)
    for f in findings:
        print(f)
    print(f"\n{counts['checked']} owner-attributed quotes checked · "
          f"{counts['grounded']} grounded · {counts['absent']} absent · "
          f"{counts['time']} re-dated · {counts['short']} too short to check · "
          f"{counts['retracted']} retracted in the journal")

    if not counts["checked"]:
        # Zero checked quotes across the whole corpus would mean the extractor died,
        # not that the journal is honest — the same shape of blindness as the stale
        # archive.  Positive control on the scan itself.
        err("extracted no checkable owner quotes at all — extractor is broken")

    bad = counts["absent"] + counts["time"]
    if errors or bad:
        return 1
    if args.self_test and not st_ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
