#!/usr/bin/env python3
"""Approve-stage helper: turn reviewed candidate cards (TMP ids) into know/ cards (K ids).

Usage: approve_inbox.py [--dry-run] file.md [file2.md ...]
- Parses bare card blocks (--- yaml --- body), tolerates ```markdown fences.
- Reserves sequential K-ids through khms_alloc.py (the one locked allocator), rewrites TMP cross-references in links.
- Writes memory/know/K-*.md; prints the TMP→K mapping.
Run ONLY after human/smart review of the input files (P6). Validate with build_views.py afterwards.
"""
import re
import hashlib
import json
import subprocess
import sys
import os

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_views
import khms_lint  # noqa: E402
import khms_alloc  # noqa: E402
import khms_lock as L
import khms_paths as P  # noqa: E402

BRAIN = P.ROOT
KNOW = os.path.join(BRAIN, "memory", "know")


def unit_of(approve_file):
    """The inbox unit an approve/converted file was made from:
    memory/inbox/2026-10-02-b01.approve.md -> (stem 2026-10-02-b01, its .md path)."""
    base = os.path.basename(approve_file)
    stem = re.sub(r"\.(approve|converted)(\.[^.]+)?\.md$|\.md$", "", base)
    return stem, os.path.join(BRAIN, "memory", "inbox", stem + ".md")


def write_review_marker(approve_file):
    """Close the review of the unit `approve_file` came from.  An existing unit goes through
    mark_ledger.close_unit_review — the same path as `mark_ledger.py mark-reviewed` — so its
    NOT CARDED verdicts are recorded with the close (#209 A4: without that the night queued
    those marks again, 235 of them on 2 nights)."""
    stem, unit = unit_of(approve_file)
    if not re.match(r"^\d{4}-\d{2}-\d{2}", stem):
        return None
    if os.path.exists(unit):
        import mark_ledger
        marker, _ = mark_ledger.close_unit_review(
            mark_ledger.Paths(BRAIN), unit, "approve_inbox",
            {"approve": os.path.basename(approve_file)}, prog="approve")
        return marker
    marker = os.path.join(P.REVIEW_MARKERS, f".reviewed-{stem}-cards")
    L.atomic_text(marker, "")
    return marker


def ledger_report():
    """The morning review's coverage line (board #209): re-derive the MARK ledger now
    that the approved cards (with their `marks:` ids) and the `.reviewed-*` markers
    exist, and return the OPEN count + the oldest OPEN mark.  Marks the reviewed
    proposal did not card are back in the queue from this moment; dismissing one
    needs a reason: tools/mark_ledger.py dismiss <id> --reason "…" (or
    --from-inbox <file> to accept the night's own NOT CARDED verdicts).
    Never fatal — the cards are already written — but never silent either."""
    try:
        import mark_ledger
    except ImportError:
        return ("\nMARK LEDGER: not installed (optional night-pipeline component, mark_ledger.py) "
                "-- coverage line skipped")
    try:
        paths = mark_ledger.Paths(BRAIN)
        floor = mark_ledger.floor_of(paths, None)
        skipped = {}          # status-kind lines the ledger leaves out -- counted, not silent (#236)
        with mark_ledger.ledger_lock(paths):
            marks = mark_ledger.derive(paths, floor, skipped=skipped)
            summary = mark_ledger.summarize(marks, floor, paths, skipped)
            mark_ledger.write_state(paths, marks, summary)
        return ("\n" + mark_ledger.status_line(summary)
                + "\n  an OPEN mark with no card and no reason goes back to the night queue; dismiss only with a reason:"
                + " tools/mark_ledger.py dismiss <id> --reason \"…\" | --from-inbox <inbox>")
    except Exception as exc:                                   # noqa: BLE001
        return f"\nMARK LEDGER: recomputation failed ({exc}) -- run tools/mark_ledger.py sync by hand"


# WITHHELD SECTIONS. `verify_relations.py` does not delete a candidate it rejects: it
# MOVES the whole block — yaml fence, body, quotes and all — into `## DEFERRED` or
# `## DROPPED (no valid RELATION)` at the end of the file, so a human can rescue it by
# moving it back. That is only true as long as this stage refuses to read those
# sections; a parser that scans the whole file would approve exactly the candidates the
# gate rejected, and the gate would be worse than nothing.
TRAILER_RE = re.compile(r"^## +(DEFERRED|DROPPED)\b.*$", re.M | re.I)

# THE `**LINKS:**` BODY LINE IS A REAL CHANNEL, and it has to be parsed here or the
# prompt that asks for it is lying. The consolidate stage writes links as one line in
# the body — it cannot edit frontmatter reliably across a whole file — and unless this
# stage folds that line into `links:`, every edge a stage spotted is dropped silently:
# no error, no warning, just a knowledge graph that never grows an edge. Only the five
# link types exist; anything else on the line (a plausible-looking `related=`) is
# reported and dropped, because inventing a sixth link type here would put edges into
# cards that nothing else can read.
LINKS_LINE_RE = re.compile(r"^\s*\*\*LINKS:\*\*\s*(.+?)\s*$", re.M)
LINK_KV_RE = re.compile(r"(\w+)\s*=\s*\[([^\]]*)\]")
LINK_KEYS = ("derived_from", "supports", "contradicts", "refuted_by", "supersedes")


def fold_links_line(meta, body, unknown=None):
    """-> body without the LINKS line, with its edges merged into meta['links']."""
    m = LINKS_LINE_RE.search(body)
    if not m:
        return body
    links = meta.get("links") or {}
    if not isinstance(links, dict):
        links = {}
    for key, vals in LINK_KV_RE.findall(m.group(1)):
        ids = khms_lint.split_link_ids(vals)      # commas and whitespace separate ids (#241)
        if key not in LINK_KEYS:
            if unknown is not None:
                unknown.append((str(meta.get("id", "?")), key))
            continue
        if key == "supersedes":
            links["supersedes"] = ids[0] if ids else links.get("supersedes")
            continue
        cur = list(links.get(key) or [])
        for v in ids:
            if v not in cur:
                cur.append(v)
        links[key] = cur
    meta["links"] = links
    return (body[:m.start()] + body[m.end():]).strip()


def split_link_lists(meta):
    """Split every whitespace-separated link list of one card into its ids, in place (#241).

    `supports: [K-90001 K-90002]` is YAML for ONE string; read as one id it reached
    the base as an edge to nothing, with the two real edges gone, and --dry-run passed
    it (K-96380; until then only a reviewer's eye caught it). Splitting is unambiguous: an
    id never contains whitespace. What is still not an id after the split is refused by
    build_views.card_problems before any id is allocated, naming the card and the field.
    A scalar (`supersedes: K-1`) stays a scalar when it holds one id.
    """
    links = meta.get("links")
    holders = [links] if isinstance(links, dict) else []
    holders.append(meta)                      # a top-level stray link key, nested later
    for holder in holders:
        for key in khms_lint.LINK_LIST_KEYS:
            val = holder.get(key)
            if val is None or (holder is meta and key not in LINK_KEYS):
                continue
            ids = khms_lint.split_link_ids(val)
            if isinstance(val, list):
                holder[key] = ids
            else:
                holder[key] = ids[0] if len(ids) == 1 else (ids or None)


def cut_trailers(text):
    """-> (text above the first withheld section, heading of that section or None)."""
    m = TRAILER_RE.search(text)
    return (text[:m.start()], m.group(0).strip()) if m else (text, None)


CARD_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*?)(?=\n---\s*\n|\Z)", re.S | re.M)

FRONT_ORDER = ["id", "type", "level", "status", "tags", "scope", "evidence",
               "source", "date", "links"]


def dump_card(meta, body):
    front = {}
    for k in FRONT_ORDER:
        if k in meta:
            front[k] = meta[k]
    for k, v in meta.items():
        if k not in front and k != "body":
            front[k] = v
    fm = yaml.safe_dump(front, sort_keys=False, allow_unicode=True,
                        default_flow_style=None, width=1000).strip()
    return f"---\n{fm}\n---\n{body.strip()}\n"


def _write_card(path, meta, body):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dump_card(meta, body))
    os.replace(tmp, path)


def mark_superseded(edges):
    """Close the other half of a `supersedes` edge on the TARGET card.

    A supersedes edge written on the new card alone leaves the old one
    `status: active`, so retrieval keeps serving it as current — the failure of
    a correction that is never reached (measured in the source deployment: old
    cards recited over their own correction for days) in its second form.  The
    morning review used to close it BY HAND, typing `status: superseded` +
    `superseded_by:` into the old card after `approve_inbox.py` had run.  This is
    that hand edit, done by the tool that knows both ids.

    Deliberately narrow: only `supersedes`, only a target that exists in know/,
    only one that is still `active` or `challenged` — a challenged card is the
    best supersede target there is (weekly prompt), and skipping it once left a
    replaced decision served as live.  `supports` and `contradicts` change no
    status here, because no existing tool has ever done so and this is not the
    place to invent a status policy.
    """
    changed, notes = 0, []
    for new_id, targets in edges:
        for tgt in targets:
            path = os.path.join(KNOW, f"{tgt}.md")
            if not os.path.exists(path):
                notes.append(f"   {new_id} supersedes {tgt}: no such card in "
                             f"know/ — target NOT marked")
                continue
            text = open(path, encoding="utf-8").read()
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
            if not m:
                notes.append(f"   {tgt}: unparseable card — NOT marked")
                continue
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError as e:
                notes.append(f"   {tgt}: bad yaml ({str(e)[:60]}) — NOT marked")
                continue
            status = str(meta.get("status", "active"))
            links = meta.get("links") or {}
            if not isinstance(links, dict):
                links = {}
            # The pointer lives in `links.superseded_by` only — that is the key the
            # build gate and retrieval read. A TOP-LEVEL `superseded_by` is a hand
            # edit in the wrong place (7 cards on 2026-10-03, review FRVSUP F2);
            # dump_card would re-emit it beside the nested one, so it is dropped
            # whenever this tool writes the card.
            had_stray = "superseded_by" in meta
            stray = meta.pop("superseded_by", None)
            if status == "superseded" and not links.get("superseded_by"):
                # Already superseded by hand but with no pointer: the back-link
                # is still owed — 33 such cards existed on 2026-10-02 (owner:
                # "at ukazujou na ty co je nahradily"). The status is left alone.
                pass
            elif status == "superseded" and had_stray:
                # Closed already; only the stray top-level copy goes. The nested
                # pointer is authoritative and is not overwritten.
                _write_card(path, meta, m.group(2))
                changed += 1
                notes.append(f"   {tgt}: stray top-level superseded_by {stray} "
                             f"removed (links.superseded_by "
                             f"{links.get('superseded_by')} kept)")
                continue
            elif status not in ("active", "challenged"):
                notes.append(f"   {tgt}: status is '{status}', not active — "
                             f"left as it is; review decides")
                continue
            meta["status"] = "superseded"
            links["superseded_by"] = new_id
            meta["links"] = links
            _write_card(path, meta, m.group(2))
            changed += 1
            notes.append(f"   {tgt}: status -> superseded, superseded_by "
                         f"{new_id}" + (f" (stray top-level superseded_by {stray} "
                                        f"removed)" if had_stray else ""))
    return changed, notes


def mirror_contradicts(new_ids):
    """Close the back half of every `contradicts` edge an approved card brought in (#265).

    `contradicts` is symmetric (spec §4.10) and build_views fails on a half the target does not carry;
    before this, a card approved with `contradicts: [K-old]` left K-old unaware of the conflict (153
    such edges in the reference deployment on 2026-10-27). The edit is apply_triage's mirror — only the
    target's links.contradicts line moves — limited to edges that touch the cards of this run. A target
    whose list layout the writer cannot edit is reported, not guessed; the build then names it."""
    import apply_triage as AT          # late: only a writing approve needs the triage writer
    edges, edits, refusals = AT.mirror_plan(BRAIN, sources=new_ids)
    if refusals:
        print(f"\ncontradicts mirror: NOT written ({len(refusals)} refusal(s)) — "
              f"apply_triage.py --mirror-contradicts names them:")
        for r in refusals:
            print(f"   {r}")
        return
    n = AT.write_mirror(BRAIN, edits, "approve cards")
    if edges:
        print(f"\ncontradicts mirror: {len(edges)} back half(s) written on {n} card(s)")
        for src, tgt in edges:
            print(f"   {tgt}: links.contradicts += {src}")


def parse_cards(text, skipped=None, unknown_links=None):
    cards = []
    if skipped is None:
        skipped = []
    if unknown_links is None:
        unknown_links = []

    def try_card(front, body):
        try:
            meta = yaml.safe_load(front)
        except yaml.YAMLError as e:
            import re as _re
            ident = _re.search(r"^id: (\S+)", front, _re.M)
            skipped.append((ident.group(1) if ident else "?", str(e).split("\n")[0]))
            print(f"SKIP (bad yaml): {str(e)[:120]} :: {front[:80]!r}", file=sys.stderr)
            return
        if isinstance(meta, dict) and "id" in meta and "type" in meta:
            body = re.split(r"\n## (?:STATS|NEW-TAGS)\b", body)[0].strip()
            # drop trailing markdown commentary lines (e.g. "*(reviewer note)*")
            body = re.sub(r"\n\*\(.*?\)\*\s*$", "", body, flags=re.S).strip()
            body = fold_links_line(meta, body, unknown_links)
            split_link_lists(meta)
            cards.append((meta, body))

    # pass 1: fenced cards (nightly-inbox format): ```yaml front \n---\n body ```
    def _take_fenced(m):
        inner = m.group(1)
        fm = re.match(r"(.*?)\n---\n(.*)", inner, re.S)
        if fm and re.search(r"^id: ", fm.group(1), re.M):
            try_card(fm.group(1).strip(), fm.group(2).strip())
            return ""            # remove from text so pass 2 can't re-match
        return inner
    text = re.sub(r"```yaml\n(.*?)```", _take_fenced, text, flags=re.S)
    text = re.sub(r"```(?:markdown)?\n?", "", text)

    # pass 2: bare card blocks (consolidator/extractor format)
    for m in CARD_RE.finditer(text):
        front = m.group(1)
        if not re.search(r"^id: ", front, re.M):
            continue             # a markdown '---' separator, not a card
        try_card(front, m.group(2).strip())
    return cards


def _main_locked():
    import khms_health
    khms_health.require_healthy(BRAIN)
    dry = "--dry-run" in sys.argv
    if not dry:
        P.require_writable("approve cards")
    files = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not files:
        print(__doc__)
        sys.exit(2)
    identity = hashlib.sha256()
    for path in files:
        _, unit = unit_of(path)
        identity.update(os.path.realpath(unit).encode() + b"\0" + open(path, 'rb').read())
        if os.path.isfile(unit):
            identity.update(open(unit, 'rb').read())
    receipt = os.path.join(P.SHARED_STATE, 'approvals', identity.hexdigest() + '.json')
    previous = json.loads(open(receipt).read()) if os.path.isfile(receipt) else None
    units = sorted({os.path.realpath(unit_of(path)[1]) for path in files})
    approval_dir = os.path.dirname(receipt)
    if os.path.isdir(approval_dir):
        for other in os.listdir(approval_dir):
            other_path = os.path.join(approval_dir, other)
            if not other.endswith('.json') or other_path == receipt:
                continue
            old = json.loads(open(other_path).read())
            if not old.get('complete') and set(old.get('units', [])) & set(units):
                print('REFUSED: this inbox unit has a partial approval with different input; '
                      'reconcile receipt ' + other_path, file=sys.stderr)
                sys.exit(1)
    input_hashes = {path: hashlib.sha256(open(path, 'rb').read()).hexdigest()
                    for path in set(files + units) if os.path.isfile(path)}

    def inputs_unchanged():
        if any(hashlib.sha256(open(path, 'rb').read()).hexdigest() != digest
               for path, digest in input_hashes.items()):
            raise RuntimeError('approval input changed during transaction; no reviewed marker written')

    if previous and previous.get('complete'):
        print('already approved: receipt ' + identity.hexdigest())
        return
    reviewed = []
    for path in files:
        stem, unit = unit_of(path)
        marker = os.path.join(P.REVIEW_MARKERS, f'.reviewed-{stem}-cards')
        if os.path.isfile(unit) and os.path.isfile(marker):
            unit_hash = hashlib.sha256(open(unit, 'rb').read()).hexdigest()
            reviewed.append(f'sha256={unit_hash}' in open(marker).read().splitlines())
        else:
            reviewed.append(False)
    if all(reviewed):
        print('already reviewed: identical inbox unit hash')
        return
    if any(reviewed):
        print('REFUSED: mixed reviewed and unreviewed units; separate the inputs', file=sys.stderr)
        sys.exit(1)
    all_cards = []
    skipped = []
    withheld, unknown_links = [], []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            head, trailer = cut_trailers(fh.read())
        if trailer:
            n = len(parse_cards(open(f, encoding="utf-8").read())) - len(parse_cards(head))
            withheld.append((f, trailer, n))
        all_cards += parse_cards(head, skipped, unknown_links)
    for f, trailer, n in withheld:
        print(f"withheld: {n} candidate(s) under '{trailer}' in {f} — they were moved "
              f"there by the relation gate, not deleted. To approve one, move its block "
              f"back under '## Cards' first.")
    for cid, key in unknown_links:
        print(f"LINKS line: dropped '{key}=' on {cid} — the only link types are "
              f"{', '.join(LINK_KEYS)}. Say the rest in prose, where a human reads it.")
    if not all_cards:
        print("no cards found")
        sys.exit(1)
    # Two inputs whose temp labels collide (two published units both starting at C1) would
    # map two cards to ONE K-id and write only the second. Refuse before any id is spent.
    seen_ids, dup = {}, []
    for meta, _ in all_cards:
        k = str(meta.get("id"))
        if k in seen_ids:
            dup.append(k)
        seen_ids[k] = True
    if dup:
        print(f"\n!! REFUSED: temp id(s) {', '.join(sorted(set(dup)))} occur more than once "
              f"across {files} — approve those files in separate runs. NOTHING was written.")
        sys.exit(1)
    # A card that fails to parse used to be one line on stderr and an exit code
    # of 0, so a partial import read as a complete one: on 2026-08-05, 12 of 69
    # cards were dropped because a quoted section title inside `source:` put a
    # second colon in the value, and the run still ended "OK". Silence about
    # what was lost is the failure; say it loudly and exit non-zero.
    if skipped:
        print(f"\n!! {len(skipped)} card(s) FAILED to parse and were NOT written:")
        for cid, err in skipped:
            print(f"   {cid}: {err}")
        print("   Fix them and re-run with ONLY those cards — the ones below are "
              "already written.")

    schema = []
    for meta, _ in all_cards:
        view = dict(meta)
        links = dict(meta.get('links') or {}) if isinstance(meta.get('links'), dict) else {}
        stray = meta.get('derived_from')
        if stray:
            links['derived_from'] = list(links.get('derived_from') or []) + (stray if isinstance(stray, list) else [stray])
        view['links'] = links
        schema += [f"{meta.get('id')}: {problem}" for problem in build_views.card_problems(view)[0]]
    if schema or skipped:
        print('REFUSED: invalid input; no ids allocated: ' + '; '.join(schema), file=sys.stderr)
        sys.exit(1)

    # A correction with no edge is a correction that will never be reached: the
    # retrieval layer follows links, and a card opening with "CORRECTED …" that
    # carried none once left the cards it corrects being recited to the principal
    # as current. Refuse BEFORE any id is allocated, so a
    # refused run leaves the counter and know/ exactly as they were and the
    # reviewer re-runs the whole file after adding the edge.
    known = khms_lint.known_card_ids(KNOW) | {str(m["id"]) for m, _ in all_cards}
    # … and (2026-09-20) a candidate that re-states a sentence the tombstone ledger has
    # retracted — the nightly re-deriving it from the journal, or a quote of it — is refused
    # the same way (khms_lint R1/R2), so an erratum cannot be undone by the next sweep.
    retracted = khms_lint.load_retracted(BRAIN)
    for flag in khms_lint.flag_batch([(str(m["id"]), m, b) for m, b in all_cards]):
        print("FLAG (reviewer decides): " + flag)
    problems = khms_lint.lint_batch(
        [(str(m["id"]), m, b) for m, b in all_cards], known, retracted,
        {t["ts"] for t in retracted})
    if problems:
        print(f"\n!! REFUSED: {len(problems)} card(s) failed lint (a correction without its "
              f"edge, a retracted claim re-stated, or inconsistent errata). NOTHING was "
              f"written, no ids were allocated.")
        for p in problems:
            print(f"   {p}")
        sys.exit(1)


    # One locked reservation for the whole batch, persisted BEFORE any card is written (khms_alloc.py):
    # a second writer on the same base -- another agent, another clone -- can never receive these ids.
    if dry:
        cur = khms_alloc.peek("card")
    else:
        inputs_unchanged()
        L.dirty(BRAIN)
        if previous:
            cur = previous['first']
        else:
            cur = khms_alloc.reserve("card", len(all_cards))[0]
            L.atomic_text(receipt, json.dumps({'first': cur, 'complete': False, 'units': units}) + '\n')
    mapping = {}
    for meta, _ in all_cards:
        mapping[str(meta["id"])] = f"K-{cur:05d}"
        cur += 1

    def remap(v):
        if isinstance(v, list):
            return [mapping.get(str(x), str(x)) for x in v]
        if v is None:
            return None
        return mapping.get(str(v), str(v))

    written = 0
    supersede_edges = []
    for meta, body in all_cards:
        old = str(meta["id"])
        meta["id"] = mapping[old]
        links = meta.get("links") or {}
        # Belt and braces (2026-08-25): a link written as a TOP-LEVEL key —
        # `supports: [K-xxxxx]` instead of `links: {supports: [...]}` — is not an
        # error anywhere; it is silently dropped, because build_views.py reads
        # links only from meta["links"]. Eight of that day's 120 cards carried
        # one, and a `derived` card whose derived_from sat outside `links`
        # failed validation with "derived card with empty derived_from".
        # convert_inbox.py nests them; this pops whatever still arrives that way
        # (hand-edited cards, other producers) so nothing reaches know/ loose.
        for k in ("derived_from", "supports", "contradicts", "refuted_by"):
            stray = meta.pop(k, None)
            vals = list(links.get(k) or [])
            if stray is not None:
                for v in (stray if isinstance(stray, list) else [stray]):
                    if v not in vals:
                        vals.append(v)
            links[k] = remap(vals)
        stray_ss = meta.pop("supersedes", None)
        links["supersedes"] = remap(links.get("supersedes") or stray_ss)
        meta["links"] = links
        ss = links["supersedes"]
        targets = [str(t) for t in (ss if isinstance(ss, list) else [ss])
                   if t and str(t).startswith("K-")]
        if targets:
            supersede_edges.append((meta["id"], targets))
        # frontmatter dump: keep key order stable & readable
        out = dump_card(meta, body)
        path = os.path.join(KNOW, f"{meta['id']}.md")
        if dry:
            print(f"DRY {old} -> {meta['id']}")
        else:
            if os.path.exists(path) and open(path, encoding="utf-8").read() != out:
                print(f"ERROR: {path} differs from the pending approval — reconcile receipt", file=sys.stderr)
                sys.exit(1)
            L.atomic_text(path, out)
            written += 1
    if not dry:
        n_marked, notes = mark_superseded(supersede_edges)
        if notes:
            print(f"\nsupersedes edges: {n_marked} target card(s) marked "
                  f"superseded")
            for n in notes:
                print(n)
        mirror_contradicts(set(mapping.values()))
        # THE MARKER IS THE RECORD OF THE FACT, and there are two facts per date, not one:
        # the card batch (this) and the contradiction triage (apply_triage.py writes
        # `-triage`). They shared `.reviewed-DATE` until 2026-09-03, so approving cards
        # silenced the triage ping and `2026-09-02-triage.md` went unread — 0 verdicts
        # applied, no trace. Written per input file, from the date in its name.
        # REVIEW IS PER UNIT, NOT PER DATE (Diblin review of #209 A, P1-3): the marker is
        # named after the inbox UNIT the approve file came from (DATE, DATE-weekly, or a
        # published batch <night>-bNN) and carries the unit's sha256, so a later batch is a
        # new unit and anything appended to this one after the review un-reviews it.
        inputs_unchanged()
        for f in files:
            write_review_marker(f)
        mpath = os.path.join(BRAIN, "memory", "inbox", ".staging", "last-mapping.txt")
        os.makedirs(os.path.dirname(mpath), exist_ok=True)
        with open(mpath, "a", encoding="utf-8") as mf:
            for o, n in mapping.items():
                mf.write(f"{o} {n}\n")
    print(f"{'would write' if dry else 'wrote'} {len(all_cards)} cards "
          f"({files}); counter -> {cur}")
    for o, n in list(mapping.items())[:10]:
        print(f"  {o} -> {n}")
    if len(mapping) > 10:
        print(f"  … +{len(mapping)-10} more")
    if not dry:
        print(ledger_report())
    # Non-zero when anything was dropped: a caller that only checks the exit
    # code must not read a partial import as a finished one.
    if skipped:
        sys.exit(1)
    if not dry:
        L.atomic_text(receipt, json.dumps({'first': cur - len(all_cards), 'complete': True, 'units': units}) + '\n')


def main():
    if '--dry-run' in sys.argv:
        return _main_locked()
    P.require_writable('approve cards')
    with L.lock('cards', BRAIN):
        result = _main_locked()
    # A failed build leaves views-dirty, so stale generated data is never reported fresh.
    subprocess.run([sys.executable, os.path.join(P.CODE, 'build_views.py')], check=True)
    return result


if __name__ == "__main__":
    main()
