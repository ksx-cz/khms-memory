#!/usr/bin/env python3
"""Card lint — the checks that must hold BEFORE a card enters know/.

Usage: python3 tools/khms_lint.py <know-dir | card.md ...>   (also imported by approve_inbox and build_views)

One rule today, and it exists because of a measured failure:

  A card whose body says it CORRECTS something must carry a graph edge naming
  what it corrects.

In the source deployment two cards measured that a sensor branch "has no pull-ups,
keep the device disconnected".  A month later the operator corrected that in
person and a new card was written, opening with the word CORRECTED — but with
`links: {contradicts: [], supersedes: null}`.  Four days later the old cards were
recited to the operator as current, because nothing in the base connected them to
their own correction: the retrieval layer can only follow edges, and there was no
edge.  The correcting card's body already carried the lesson as prose ("cards must
be read with their date") — and writing a lesson down is not a mechanism.  The
mechanism is this refusal plus the reverse injection in the hook.

The vocabulary is the spec's (spec/ §4.10): derived_from / supports /
contradicts / supersedes / refuted_by.  There is no `refines` link; a correction
uses `contradicts` (mutual conflict, symmetric) or `supersedes` (replacement,
body must then read SUPERSEDES K-x BECAUSE …).

Escape hatch, deliberately visible in the record (a measure is a step that can
be seen to have been taken): a card that genuinely corrects something
never carded writes

    NO-CORRECTION-TARGET: <why nothing in the base can be pointed at>

in its body.  That is a statement in an immutable card, not a flag in a script.
"""
import os
import math
import khms_files
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_patterns as KP  # noqa: E402

# The spec's link vocabulary that can express "this card corrects that one".
BACK_EDGES = ("contradicts", "supersedes", "refuted_by")

ESCAPE_RE = re.compile(r"^\s*NO-CORRECTION-TARGET:\s*\S", re.M)
CARD_ID_RE = re.compile(r"\bK-\d{5}\b")

# --- link lists: one element = ONE id (#241) ---------------------------------
# The link keys a card's `links:` may hold (spec §4.10, plus the back pointer the
# supersede writer adds).
LINK_LIST_KEYS = ("derived_from", "supports", "contradicts", "refuted_by", "supersedes",
                  "superseded_by")
# A link element is a card id (`K-` and five or more digits) or a temp label of the
# batch being approved: an upper-case letter, then letters/digits in dash-joined runs,
# holding at least one digit (`C12`, `M2045`, `C25b`, `TMP-LEG-4`). Prose, a
# bracketed `[[K-…]]`, a trailing comma and two ids joined by a space match neither.
LINK_ID_RE = re.compile(r"K-\d{5,}|(?=\S*\d)[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
# Whitespace and commas between ids are separators. Commas are YAML's own; whitespace
# is what the per-mark writer emitted on 2026-10-15 (`supports=[K-a K-b]`), and with
# only commas splitting, `K-a K-b` was ONE id that passed every check (K-96380).
LINK_SEP_RE = re.compile(r"[\s,]+")


def split_link_ids(val):
    """-> list of link elements from a list, a scalar or None, split on whitespace and
    commas, de-duplicated in order. Every reader of a link list goes through this, so
    `[K-1 K-2]` and `[K-1, K-2]` are the same two edges everywhere."""
    vals = val if isinstance(val, list) else ([] if val is None else [val])
    out = []
    for v in vals:
        if v is None:
            continue
        for part in LINK_SEP_RE.split(str(v).strip()):
            if part and part not in out:
                out.append(part)
    return out


def link_problems(links):
    """-> problems of a card's `links` dict: every element must be ONE id (LINK_ID_RE).
    Texts carry no card id; the caller prefixes its own label."""
    if not isinstance(links, dict):
        return []
    problems = []
    for key in LINK_LIST_KEYS:
        val = links.get(key)
        for v in (val if isinstance(val, list) else ([] if val is None else [val])):
            if v is None or not LINK_ID_RE.fullmatch(str(v)):
                problems.append(f"links.{key}: {str(v)!r} is not a card id (K-NNNNN) or a temp "
                                f"label -- one id per element, ids separated by commas")
    return problems

# Correction language, in tiers, because the word list alone does not
# separate "this card corrects the record" from "we fixed a bolt".  Measured on
# the 2187-card base of 2026-08-28: the flat word list fires on 476 cards (22 %
# of everything, 270 of them on the Czech stem "oprav*", which in a robotics
# base overwhelmingly means *repair*).  A lint that fires on a fifth of all
# cards gets switched off, and a switched-off check measures nothing, so:
#
#   BLOCK — the KHMS body markers, upper-case, as the spec and the card corpus
#           actually write them (spec §4.2: "SUPERSEDES K-x BECAUSE"; a
#           correcting card opens with "CORRECTED (owner: …)").  Unambiguous.
#   CONTEXT — a phrase that states a claim is false ("was wrong", "no longer
#           true", "supersedes"), only on a line that ALSO names a specific card:
#           a K-id or a definite reference ("this/the/that/its card", plus the
#           deployment's forms in LINT_RECORD_REF).  "K-xxxxx was wrong" fires.
#   WEAK — a word that names correcting as an activity as often as it performs
#           one ("corrected"; deployment repair stems in LINT_WEAK_<n>): only on a
#           line with a DEFINITE reference.  "corrected the cron line (K-xxxxx)"
#           does not fire; "the card corrected …" does.
#
# Narrowed 2026-10-13 (#237), measured on the 5840-card base.  Escape set = the 136
# cards with a NO-CORRECTION-TARGET line (a refusal a human judged false; the old
# rule fired on 124).  True set = cards with a backward edge whose body uses
# correction words (473; the old rule fired on 291 with the edge removed).  The
# escapes came from four shapes:
#   * a generic record word on the line ("card", "knowledge base", deployment
#     words in the retired LINT_RECORD_CTX) — a line that mentions cards at all,
#     not one that names the card being corrected;
#   * the lint's own vocabulary as a subject: the escape token NO-CORRECTION-TARGET
#     (it contains CORRECTION), field names in code spans (`supersedes:`), pipeline
#     annotations in HTML comments, verbatim source quotes (`- src=… :: …`, evidence
#     of what a source said, not this card's claim);
#   * the noun "correction" / repair stems naming the topic of a KHMS card;
#   * and, the other way round, the K-id arm of the old context never matched: it
#     was upper-case and the lines are lower-cased first, so "K-xxxxx was wrong"
#     passed unrefused.
# Measured after (same base, deployment repair stems moved to LINT_WEAK_): escapes 124 -> 34,
# true set 291 -> 299 (285 of the old 291 kept, 14 corrections the dead K-id arm missed),
# cards with neither edge nor escape 35 -> 32.  The 6 lost: each corrects only through a
# noun ("flagged for correction"), a generic record word or a quoted source.
BLOCK_PATTERNS = (
    ("CORRECTED", re.compile(r"\bCORRECT(ED|ION|IONS|S)\b")),
    # ACTIVE voice only: "SUPERSEDES K-x" is the replacing card speaking. The
    # passive "SUPERSEDED 2026-03-04 by …" is the OLD card annotating itself,
    # which is a different (and legitimate) shape — it must not be refused.
    ("SUPERSEDES", re.compile(r"\bSUPERSED(ES|ING)\b")),
)
CONTEXT_PATTERNS = (
    ("contrary to", re.compile(r"\bcontrary to\b")),
    ("was wrong", re.compile(r"\b(was|were|is|are) wrong\b")),
    ("no longer true", re.compile(r"\bno longer (true|holds|valid|the case)\b")),
    ("supersedes", re.compile(r"\bsupersed(es|ing)\b")),
)
WEAK_PATTERNS = (
    # the verb only: the noun "correction" names the topic ("a correction is reached by
    # an edge"); the upper-case marker CORRECTION in the BLOCK tier still counts.
    ("corrected", re.compile(r"\bcorrect(ed|s)\b")),
)
# Matched on norm()ed (lower-case, diacritics stripped) lines.
DEFINITE_REF = re.compile(r"\b(?:this|the|that|its) card\b" + KP.extra("LINT_RECORD_REF"))
RECORD_REF = re.compile(r"\bk-\d{5}\b|" + DEFINITE_REF.pattern)
# Deployment vocabulary (private config, khms_patterns.py) is appended in file order.
BLOCK_PATTERNS = BLOCK_PATTERNS + tuple(KP.labelled("LINT_BLOCK_"))
CONTEXT_PATTERNS = CONTEXT_PATTERNS + tuple(KP.labelled("LINT_CONTEXT_"))
WEAK_PATTERNS = WEAK_PATTERNS + tuple(KP.labelled("LINT_WEAK_"))
if KP.extra("LINT_RECORD_CTX"):
    # Retired key: generic record words were an escape source (above). Say so instead
    # of ignoring a configured value silently.
    print("khms_lint: LINT_RECORD_CTX is no longer read (#237); put definite card "
          "references in LINT_RECORD_REF", file=sys.stderr)

# Text that talks ABOUT correction without being one (see above).
ESCAPE_TOKEN_RE = re.compile(r"NO-CORRECTION-TARGET", re.I)
CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
SOURCE_QUOTE_RE = re.compile(r"^[ \t]*-[ \t]*src=.*$", re.M)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)   # pipeline annotations ("dropped: …")


def norm(s):
    """Same normalisation khms_search uses: NFKD, drop combining marks, lower."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def correction_terms(body):
    """Which correction phrases the body uses, in the order they are listed.

    ERRATUM blocks are excluded first: "the sentence … was wrong at the time of writing"
    corrects THIS card's own text, not another card, so it owes no backward edge.
    The escape token, code spans, HTML comments and verbatim source quotes are excluded: they
    mention correction vocabulary, they do not state a correction by this card."""
    body, _, _ = strip_errata(body or "")
    body = SOURCE_QUOTE_RE.sub("", ESCAPE_TOKEN_RE.sub(" ", HTML_COMMENT_RE.sub(" ", body)))
    body = CODE_SPAN_RE.sub(" ", body)
    terms = [name for name, rx in BLOCK_PATTERNS if rx.search(body)]
    for line in body.splitlines():
        nl = norm(line)
        if not RECORD_REF.search(nl):
            continue
        tiers = CONTEXT_PATTERNS + (WEAK_PATTERNS if DEFINITE_REF.search(nl) else ())
        for name, rx in tiers:
            if rx.search(nl) and name not in terms:
                terms.append(name)
    return terms


def backward_targets(meta_or_links):
    """Card ids this card points at with a correction edge.

    Accepts either a `links` dict or a whole card `meta`. A stray TOP-LEVEL
    `contradicts:` counts here: convert_inbox/approve_inbox nest it a few lines
    later (test_link_placement.py), and refusing a card for an edge it does have,
    merely written one level too high, would be the wrong refusal.
    """
    d = meta_or_links or {}
    links = d.get("links") if isinstance(d.get("links"), dict) else {}
    out = []
    for k in BACK_EDGES:
        for src in (links, d):
            v = src.get(k)
            if not v:
                continue
            for item in (v if isinstance(v, list) else [v]):
                item = str(item).strip()
                if item and item.lower() not in ("null", "none", "~") and item not in out:
                    out.append(item)
    return out


def known_card_ids(know_dir):
    try:
        return {f[:-3] for f in khms_files.listdir(know_dir)
                if f.startswith("K-") and f.endswith(".md")}
    except OSError:
        return set()


def check_correction_edges(card_id, meta, body, known_ids=None):
    """None when the card is fine, else the refusal message (one string).

    `known_ids` — ids the edge may point at (know/ plus the ids being approved in
    this same batch).  None disables the existence half of the check.
    """
    terms = correction_terms(body)
    if not terms:
        return None
    if ESCAPE_RE.search(body or ""):
        return None
    targets = backward_targets(meta)
    quoted = ", ".join(f"'{t}'" for t in terms[:4])
    if not targets:
        return (
            f"{card_id}: the body uses correction language ({quoted}) but carries no "
            f"backward edge. A card that corrects another card MUST name it: add "
            f"links.contradicts: [K-xxxxx] (mutual conflict) or links.supersedes: "
            f"K-xxxxx (replacement — body must then contain 'SUPERSEDES K-xxxxx "
            f"BECAUSE: …'), or links.refuted_by: [K-xxxxx]. Without that edge the "
            f"corrected card is still served alone and the correction is never "
            f"reached (measured: old cards recited over their own correction). "
            f"If nothing in the base can be pointed at, say so in the body: "
            f"'NO-CORRECTION-TARGET: <reason>'.")
    if known_ids is not None:
        missing = [t for t in targets if t not in known_ids]
        if missing:
            return (
                f"{card_id}: correction language ({quoted}) with a backward edge that "
                f"points at nothing: {', '.join(missing)} not in know/ and not in this "
                f"batch. Fix the id or point the edge at a card that exists.")
    return None


# --------------------------------------------------------------- errata (R1, R2)
# Second rule family, 2026-09-20 (package KBFIX, issue #136). A claim that was FALSE WHEN
# WRITTEN — not a belief overtaken by events — is corrected in place by
# tools/khms_erratum.py, which (declare) puts the retracted wording into the tombstone
# ledger and (apply) replaces it in the card, leaving an ERRATUM block that quotes it.
# Between the two steps, and for every card that quotes the same sentence, R1 fails; R2
# keeps frontmatter, body and ledger telling the same story afterwards.
#
#   R1  no card's LIVE text (ERRATUM blocks and [ERRATUM TS-…] markers excluded) may contain
#       a claim the ledger has retracted, compared after khms_triage's normalisation
#       (lower-case, punctuation to spaces, single spaces) so line wrapping and quoting do
#       not hide a match.
#   R2  every `errata:` entry has its ERRATUM block and its inline marker in the body, every
#       ERRATUM block is declared in `errata:`, and every TS id stands in the ledger under
#       this card's id.
#
# Both are wired into approve_inbox.py (before an id is spent) and build_views.py (over the
# whole base, loud) — the mechanism lives in the artefact that would otherwise fail
# (writing a lesson down is not a mechanism), not in a rule someone has to remember.

ERRATUM_BLOCK_RE = re.compile(r"^ERRATUM (TS-\d{5}) \(.*?(?=\n\s*\n|\Z)", re.S | re.M)
ERRATUM_MARK_RE = re.compile(r"\[ERRATUM\s+(TS-\d{5})\]")   # \s+: survives a re-wrap
MIN_CLAIM_NORM = 24     # normalised characters; a shorter "claim" is not a sentence


def strip_errata(body):
    """-> (live_text, block_ids, mark_ids): the body without its ERRATUM blocks and inline
    markers, plus the TS ids each carried."""
    body = body or ""
    block_ids = ERRATUM_BLOCK_RE.findall(body)
    live = ERRATUM_BLOCK_RE.sub("", body)
    mark_ids = ERRATUM_MARK_RE.findall(live)
    live = ERRATUM_MARK_RE.sub("", live)
    return live, block_ids, mark_ids


def norm_claim(s):
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def load_retracted(brain):
    """-> [{ts, was, date, claim, nclaim}] from memory/tombstones/LEDGER.md (empty when
    there is no ledger). Lazy import: khms_triage is regex-only but not needed by the
    correction-edge rule."""
    import khms_triage
    path = os.path.join(brain, "memory", "tombstones", "LEDGER.md")
    return [{"ts": t["ts"], "was": t["was"], "date": t["date"], "claim": t["claim"],
             "nclaim": norm_claim(t["claim"])} for t in khms_triage.load_ledger(path)]


def check_retracted_claims(card_id, body, retracted):
    """R1. None when clean, else one message naming the tombstone and the fix."""
    if not retracted:
        return None
    live, _, _ = strip_errata(body)
    nbody = norm_claim(live)
    hits = [t for t in retracted
            if len(t["nclaim"]) >= MIN_CLAIM_NORM and t["nclaim"] in nbody]
    if not hits:
        return None
    t = hits[0]
    own = t["was"] == card_id
    return (
        f"{card_id}: still states a claim retracted as {t['ts']} on {t['date']} "
        f"(declared false on {t['was']}): \"{t['claim'][:90]}…\". "
        + (f"Apply the erratum: khms_erratum.py apply --card {card_id} --ts {t['ts']} "
           f"--now \"<corrected wording>\"." if own else
           f"This card repeats {t['was']}'s retracted sentence (a quote, or the nightly "
           f"re-deriving it) — reword it or quote the corrected sentence instead.")
        + (f" (+{len(hits) - 1} more retracted claim(s) in this card)" if len(hits) > 1 else ""))


def check_errata_consistency(card_id, meta, body, ledger_ids=None, ledger_owner=None):
    """R2. `ledger_ids`: TS ids the ledger holds (None disables that half);
    `ledger_owner`: {ts: card id} to verify the tombstone was declared on THIS card."""
    meta = meta or {}
    entries = meta.get("errata") or []
    if entries and not isinstance(entries, list):
        return f"{card_id}: `errata:` must be a list of {{ts, date, by}} entries"
    head_ids = []
    for e in entries:
        if not isinstance(e, dict) or not re.fullmatch(r"TS-\d{5}", str(e.get("ts", ""))):
            return f"{card_id}: `errata:` entry without a TS-nnnnn `ts`: {e!r}"
        for k in ("date", "by"):
            if not e.get(k):
                return f"{card_id}: `errata:` entry {e.get('ts')} lacks `{k}`"
        head_ids.append(str(e["ts"]))
    _, block_ids, mark_ids = strip_errata(body)
    if not head_ids and not block_ids and not mark_ids:
        return None
    if sorted(set(head_ids)) != sorted(set(block_ids)):
        return (f"{card_id}: errata in frontmatter {sorted(set(head_ids))} != ERRATUM blocks "
                f"in body {sorted(set(block_ids))}")
    if sorted(set(mark_ids)) != sorted(set(block_ids)):
        return (f"{card_id}: [ERRATUM TS] markers {sorted(set(mark_ids))} != ERRATUM blocks "
                f"{sorted(set(block_ids))} — the corrected sentence must carry its marker")
    if ledger_ids is not None:
        missing = [t for t in head_ids if t not in ledger_ids]
        if missing:
            return f"{card_id}: errata {missing} are not in the tombstone ledger"
    if ledger_owner:
        foreign = [t for t in head_ids if ledger_owner.get(t) not in (None, card_id)]
        if foreign:
            return (f"{card_id}: errata {foreign} were declared on "
                    f"{[ledger_owner[t] for t in foreign]}, not on this card")
    return None


_NUM_RE = re.compile(r"(?<![\w.#/@:\-])-?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")
_UNIT_SUFFIX = {"", "mm", "cm", "km", "m", "rad", "deg", "°", "%", "khz", "mhz", "ghz",
                "hz", "ms", "µs", "us", "ns", "s", "mv", "kv", "v", "ma", "a", "mw", "kw",
                "w", "px"}
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
# An inline `[ERRATUM TS-…]` marker stands where a corrected sentence ended: what follows
# it is the next sentence (K-92603 "…holds the measured one) [ERRATUM TS-00407] (0.567 m
# being…" read 0.567 as rear_tyre_track's number).
_SENT_END = re.compile(r"[.;!?](?:\s|$)|\n|—|\s-\s|\[ERRATUM\s+TS-\d{5}\]")
# Another parameter's assignment ends the key's reach even when the index does not hold
# that name: K-90260 "desired_linear_vel (… nav2_params.yaml), min_turning_radius=1.3"
# read 1.3 as desired_linear_vel's (the repo key is minimum_turning_radius).
_OTHER_NAME_RE = re.compile(r"`?\b[A-Za-z]\w*_\w*`?\s*[=:]")
# A pointer to the file that holds the value, in the same paragraph — the value is
# then quoted next to its source and is still current, so it is not flagged.
_POINTER_RE = re.compile(r"[\w.\-/~]*\w\.(?:ya?ml|xacro|json|py|cpp|hpp|h)\b")
_WINDOW = 80
# The key is a TERM, not a subject, when an operator follows it: `kingpin_track/2 =
# 1.79`, `k_y·e_y = 0`, `curvature_fit_arc_m > 0` — a formula or a condition states no
# value of the key (2 of 4 false flags in the first 30-sample of round 2).
_TERM_RE = re.compile(r"\s*(?:[/*+^·×<>≤≥]|!=)")


def _card_numbers(text):
    """[(raw, float)] for the plain numbers in text — not dates, ids, versions, times,
    hashes; a number glued to letters counts only when the letters are a unit."""
    out = []
    for m in _NUM_RE.finditer(text):
        raw, after = m.group(0), text[m.end():]
        if re.match(r"[-.:/]\d|\d", after):          # 2026-10-08, 1.2.3, 10:30, 3/4
            continue
        if re.match(r"[A-Za-zµ°%]*", after).group(0).lower() not in _UNIT_SUFFIX:
            continue                                   # 3D, 0x1f, 5abc
        out.append((raw, float(raw.replace(",", "."))))
    return out


_FACTORS = (1.0, 1000.0, 0.001, 100.0, 180.0 / math.pi, math.pi / 180.0)


def _same_value(raw, card, repo):
    """The card's number is the repo's value at the card's own precision, allowing the
    conversions a card uses (m↔mm, s↔ms, fraction↔%, rad↔deg): "30°" is 0.5236 rad,
    "0.7 m" is 0.700, "0.6570" is not 0.700."""
    mant, _e, exp = raw.lower().partition("e")
    decimals = len(re.split(r"[.,]", mant)[1]) if re.search(r"[.,]", mant) else 0
    tol = 0.5 * 10 ** (int(exp or 0) - decimals) + 1e-12
    return any(abs(abs(card) - abs(repo) * k) <= tol for k in _FACTORS)


def _key_hits(text, index, max_words=8):
    """[(name, start, end)] of repo key names in text: consecutive words joined by a
    single '_', ' ' or '-'; not part of a longer identifier (no '_' on either side).
    A two-word name counts only written as the identifier (`max_gap`, not "max gap"):
    in prose, "set to", "max gap", "not measured", "turning radius" are English far
    more often than they are the key (measured on the base, see test file). Three and
    more words may be prose ("rear tyre track" is rear_tyre_track)."""
    toks = [(m.group(0).lower(), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    hits, i = [], 0
    while i < len(toks):
        best = None
        if i == 0 or text[toks[i][1] - 1:toks[i][1]] != "_":
            words, ident = [toks[i][0]], True
            for j in range(i + 1, min(i + max_words, len(toks))):
                sep = text[toks[j - 1][2]:toks[j][1]]
                if sep not in ("_", " ", "-"):
                    break
                words.append(toks[j][0])
                ident = ident and sep == "_"
                name = "_".join(words)
                seg = text[toks[i][1]:toks[j][2]]
                if (name in index and text[toks[j][2]:toks[j][2] + 1] != "_"
                        and (ident or len(words) >= 3)
                        and not (ident and seg.isupper())):   # WHEELBASE_M = code const
                    best = (name, toks[i][1], toks[j][2], j)
        if best:
            hits.append(best[:3])
            i = best[3] + 1
        else:
            i += 1
    return hits


def _statements(body):
    """The card's own claim split into statements: paragraphs, each `- ` item on its
    own; the `**QUOTES:**` tail is evidence quoted with its `src=`, not read, and
    neither is an ERRATUM block — it quotes the retracted wording by design (R1/R2)."""
    claim = ERRATUM_BLOCK_RE.sub("", body or "")
    claim = re.split(r"^\*\*QUOTES:?\*\*", claim, maxsplit=1, flags=re.M)[0]
    units = []
    for para in re.split(r"\n\s*\n", claim):
        units += re.split(r"\n(?=\s*[-*] )", para)
    return units


def check_repo_values(card_id, meta, body, index=None):
    """→ a FLAG message when a statement names a repo key and states a number for it
    in the same sentence; else None. index: khms_repo_values.load_index() by default.

      * the number is NOT the repo's value → "card says X, repo says Y (file:line)" —
        a stale-value card, flagged even when it cites the file;
      * the number IS the repo's value → "value lives in file:line" — unless the same
        statement already points at that file (the value is quoted next to its source).
    A key named with no number after it is a pointer and is fine; a number with no key
    (a measurement: "median 17 Hz on /imu") is never flagged."""
    if index is None:
        import khms_repo_values
        index = khms_repo_values.load_index()
    if not index:
        return None
    found = []
    for unit in _statements(body):
        hits = _key_hits(unit, index)
        pointers = {os.path.basename(p) for p in _POINTER_RE.findall(unit)}
        for n, (name, _s, end) in enumerate(hits):
            stop = hits[n + 1][1] if n + 1 < len(hits) else len(unit)
            window = re.sub(r"^\[\d+\]", "", unit[end:min(stop, end + _WINDOW)])  # key[2]=
            if _TERM_RE.match(window):
                continue
            cuts = [m.start() for m in (_SENT_END.search(window),
                                        _OTHER_NAME_RE.search(window)) if m]
            nums = _card_numbers(window[:min(cuts)] if cuts else window)
            if not nums:
                continue
            entries = index[name]
            match = [e for e in entries for r, v in nums if _same_value(r, v, e.num)]
            if match:
                e = match[0]
                if os.path.basename(e.path) not in pointers:
                    found.append(f"{name} — value lives in {e.path}:{e.line}")
            else:
                repo = ", ".join(f"{e.raw} ({e.path}:{e.line})" for e in entries[:3])
                more = f" +{len(entries) - 3} more" if len(entries) > 3 else ""
                found.append(f"{name} — card says {nums[0][0]}, repo says {repo}{more}"
                             f" — stale value?")
    if not found:
        return None
    uniq = list(dict.fromkeys(found))
    more = f" (+{len(uniq) - 3} more)" if len(uniq) > 3 else ""
    return (f"{card_id}: states a value the repo holds: " + "; ".join(uniq[:3]) + more
            + " — point to the file:key instead of the number (reviewer decides)")


def flag_batch(cards, index=None):
    """cards: [(id, meta, body)] → [flag message]. Flags are reported, never refused."""
    return [msg for cid, meta, body in cards
            for msg in (check_repo_values(cid, meta, body, index),) if msg]


def lint_batch(cards, known_ids=None, retracted=None, ledger_ids=None):
    """cards: [(id, meta, body)] → [refusal message]. `retracted` / `ledger_ids` from
    load_retracted(); None runs only the correction-edge rule."""
    problems = []
    for cid, meta, body in cards:
        for msg in (check_correction_edges(cid, meta, body, known_ids),
                    check_retracted_claims(cid, body, retracted),
                    check_errata_consistency(cid, meta, body, ledger_ids)):
            if msg:
                problems.append(msg)
    return problems


def _main(argv):
    """CLI: khms_lint.py <know-dir-or-card.md ...> — audit cards already on disk."""
    import khms_files as glob
    import yaml
    argv = list(argv or [])
    values = "--values" in argv
    argv = [a for a in argv if a != "--values"]
    import khms_repo_values
    repo_index = khms_repo_values.load_index() if values else {}
    flagged = 0
    paths = []
    for a in argv:
        paths += sorted(glob.glob(os.path.join(a, "K-*.md"))) if os.path.isdir(a) else [a]
    if not paths:
        print(__doc__)
        return 2
    known = {os.path.basename(p)[:-3] for p in paths}
    # ... and every card that already lives beside them: a single-file lint of a
    # superseding card used to report its (existing) target as 'points at nothing'
    # (measured once in the source deployment), because only the batch was 'known'.
    known |= known_card_ids(os.path.dirname(os.path.abspath(paths[0])))
    # An edge may legitimately point into the fog: superseded and condensed cards
    # move to memory/archive/know/. Counting those as "points at nothing" turned
    # a first audit run into 37 false alarms.
    for p in list(paths):
        arch = os.path.join(os.path.dirname(os.path.dirname(p)), "archive", "know")
        if os.path.isdir(arch):
            known |= {f[:-3] for f in khms_files.listdir(arch) if f.endswith(".md")}
            break
    # the ledger sits beside the base: <brain>/memory/tombstones/LEDGER.md
    brain = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(paths[0]))))
    retracted = load_retracted(brain)
    ledger_ids = {t["ts"] for t in retracted}
    ledger_owner = {t["ts"]: t["was"] for t in retracted}
    bad = bad_err = 0
    for p in paths:
        with open(p, encoding="utf-8") as f:
            text = f.read()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            continue
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        cid = os.path.basename(p)[:-3]
        msg = check_correction_edges(cid, meta, m.group(2), known)
        if msg:
            bad += 1
            print(msg)
        for msg in (check_retracted_claims(cid, m.group(2), retracted),
                    check_errata_consistency(cid, meta, m.group(2), ledger_ids, ledger_owner)):
            if msg:
                bad_err += 1
                print(msg)
        if values:
            flag = check_repo_values(cid, meta, m.group(2), repo_index)
            if flag:
                flagged += 1
                print("FLAG " + flag)
    if values:
        print(f"khms_lint: {flagged} flagged / {len(repo_index)} keys indexed")
    print(f"khms_lint: {bad} card(s) with unlinked correction language / {len(paths)} checked")
    print(f"khms_lint: {bad_err} card(s) stating a retracted claim or with inconsistent errata "
          f"({len(retracted)} tombstone(s) in the ledger)")
    return 1 if (bad or bad_err) else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
