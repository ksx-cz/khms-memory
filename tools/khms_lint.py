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
import re
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_patterns as KP  # noqa: E402

# The spec's link vocabulary that can express "this card corrects that one".
BACK_EDGES = ("contradicts", "supersedes", "refuted_by")

ESCAPE_RE = re.compile(r"^\s*NO-CORRECTION-TARGET:\s*\S", re.M)
CARD_ID_RE = re.compile(r"\bK-\d{5}\b")

# Correction language, in two tiers, because the word list alone does not
# separate "this card corrects the record" from "we fixed a bolt".  Measured on
# the 2187-card base of 2026-08-28: the flat word list fires on 476 cards (22 %
# of everything, 270 of them on the Czech stem "oprav*", which in a robotics
# base overwhelmingly means *repair*).  A lint that fires on a fifth of all
# cards gets switched off, and a switched-off check measures nothing, so:
#
#   BLOCK — the KHMS body markers, upper-case, as the spec and the card corpus
#           actually write them (spec §4.2: "SUPERSEDES K-x BECAUSE"; a
#           correcting card opens with "CORRECTED (owner: …)").  Unambiguous.
#   CONTEXT — everything else, only on a line that ALSO talks about the record
#           (a K-id, "card", "knowledge base", plus deployment vocabulary from
#           khms_patterns.py).  "We fixed the cable" does not fire; "correction
#           of card K-xxxxx" does.
#
# Same list of words the rule names, anchored so each means what the rule means.
# Calibrated together: 41 of 2187 cards (1.9 %), the motivating correction among them.
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
    ("corrected", re.compile(r"\bcorrect(ed|ion|ions|s)\b")),
    ("supersedes", re.compile(r"\bsupersed(es|ing)\b")),
)
# "this line is about the record itself", diacritics already stripped.
RECORD_CTX = re.compile(
    r"\bK-\d{5}\b|\bcard\b|\bknowledge base\b" + KP.extra("LINT_RECORD_CTX"))
# Deployment vocabulary (private config, khms_patterns.py) is appended in file order.
BLOCK_PATTERNS = BLOCK_PATTERNS + tuple(KP.labelled("LINT_BLOCK_"))
CONTEXT_PATTERNS = CONTEXT_PATTERNS + tuple(KP.labelled("LINT_CONTEXT_"))


def norm(s):
    """Same normalisation khms_search uses: NFKD, drop combining marks, lower."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def correction_terms(body):
    """Which correction phrases the body uses, in the order they are listed.

    ERRATUM blocks are excluded first: "the sentence … was wrong at the time of writing"
    corrects THIS card's own text, not another card, so it owes no backward edge."""
    body, _, _ = strip_errata(body or "")
    terms = [name for name, rx in BLOCK_PATTERNS if rx.search(body)]
    for line in body.splitlines():
        nl = norm(line)
        if not RECORD_CTX.search(nl):
            continue
        for name, rx in CONTEXT_PATTERNS:
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
        return {f[:-3] for f in os.listdir(know_dir)
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
    import glob
    import yaml
    paths = []
    for a in argv or []:
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
            known |= {f[:-3] for f in os.listdir(arch) if f.endswith(".md")}
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
    print(f"khms_lint: {bad} card(s) with unlinked correction language / {len(paths)} checked")
    print(f"khms_lint: {bad_err} card(s) stating a retracted claim or with inconsistent errata "
          f"({len(retracted)} tombstone(s) in the ledger)")
    return 1 if (bad or bad_err) else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
