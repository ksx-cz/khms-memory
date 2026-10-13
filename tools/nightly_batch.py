#!/usr/bin/env python3
"""nightly_batch.py — split / merge the nightly's candidate files (board #209).

Usage: python3 tools/nightly_batch.py {split|merge|split-run} ...   (run by nightly_distill.sh)

WHY.  With the 20-card cap gone (owner, 2026-10-01: "no daily card limit at
all — simply process everything in the queue") a night can hand the
consolidate stage 130+ candidates.  Measured on the capped nights 2026-09-20 ..
2026-10-01, consolidate needed 7.5–22 min for 20 cards (~1.2 cards/min), so
one call over 130 candidates would run ~110 min — past its own timeout, and a
timeout used to throw the whole night's proposal away.  So consolidate now runs
in BATCHES, every batch file lives in the persistent carry queue
(memory/ledger/carry/) until its output has been merged into the inbox, and a
batch the night did not reach — deadline, failure, or a kill — is simply the
first thing the next night does.  Nothing is dropped and nothing is redone.

    split SWEEP --size N --outdir D --prefix P   -> D/P-01.md ... (+ prints paths)
          the sweep's non-candidate sections ride with batch 01, except
          `## MARKS NOT CARDED`, written to D/P-notcarded.md for a direct merge
    merge OUT PART [--relabel-base N] [--letter L]
          structured merge: PART's candidates after OUT's, then every section
          merged under ONE heading (convert_inbox2.py stops reading cards at
          the first trailer heading, so a second `## Flagged` in the middle
          would hide every card after it)
    count FILE                                   -> number of candidates

THE PER-BATCH TRANSACTION (Independent review of #209 part A, 2026-10-01, P1-2/P1-4).
"Merge into the inbox, then move the batch out of carry" was two writes; a kill
between them left the batch to be consolidated again and merged again (a label
clash was renamed, never recognised).  Now every step is idempotent and keyed
by a stable BATCH ID, so a run killed anywhere repeats nothing that finished:

    split-run SWEEP --carry C --run NK [--window TS] [--ready]
          writes the batches (+ NOT CARDED + WINDOW) into C/.split-NK.tmp/ and
          COMMITS them with one rename to C/NK/ — a kill before the rename
          leaves no batch (and no stamp), a kill after it leaves all of them
          plus the window the next run resumes from.  Refuses an existing C/NK.
          Without --window no WINDOW file is written: a MARK-stage chunk
          (C/<night key>-mNN, PMARKFIRST) is not a window of the sweep and must
          never move the sweep's stamp.
          --ready (#237): the input is ALREADY consolidated (the per-mark evaluator writes
          approve-ready candidates), so each cons-NN.md gets its durable output
          cons-NN.out.md in the same temp directory and the same rename; the
          night then publishes the batch from it without a model call.
    publish SRC --unit U --batch-id ID --record R [--relabel-base N]
          creates the inbox unit U from SRC with an exclusive link (never an
          overwrite), then records ID in R.  U already there with the same
          content = no-op; with different content = refusal (rc 3); a card
          with a malformed header (card_header.py) = refusal (rc 5), no unit.
    published ID --unit U --record R [--src S --relabel-base N]
          rc 0 VERIFIED (unit + manifest + sha, and S's normalised content when
          given), 10 ABSENT, 3 CONFLICT, 4 MISSING/error — see verify().  The
          recovery path of nightly_distill.sh takes a batch out of carry only on
          rc 0 and spends a model call only on rc 10 (review khmsfix-2, 2026-10-01:
          the bare existence of the unit file used to count as published).

One batch = one immutable inbox unit, so approving a unit reviews exactly
that content and a later batch is a new, unreviewed unit (review P1-3).
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

CAND_RE = re.compile(r"^#{2,3} +([A-Z])(\d+)([a-z]?)\b")
SECTION_RE = re.compile(r"^## +(.+?)\s*$")
NOT_CARDED = "## MARKS NOT CARDED"


# TRAILER SECTIONS KEEP THEIR CARDS (PDRAIN, #209). `## DROPPED (no valid RELATION)` and
# `## DEFERRED` (tools/verify_relations.py) hold candidates the gate MOVED out of the proposal, and
# the converter never reads past such a heading (convert_inbox2.TRAILERS). Until 2026-10-06 this
# parser took every `### C<n>` line for a card wherever it stood, so publish/merge pulled every
# gate-dropped candidate back under `## Cards` — the relation gate was undone by the very step that
# published its result. A card line inside a trailer section now stays in that section.
TRAILER_HEADS = ("## flagged", "## dropped", "## already carded", "## suspected",
                 "## condense-confirmed", "## experiments", "## stats", "## new-tags",
                 "## deferred", "## marks not carded")


def is_trailer(heading):
    h = heading.strip().lower()
    return any(h.startswith(t) for t in TRAILER_HEADS)


def parse(text):
    """-> (preamble, [card blocks], [(heading, content)])."""
    pre, cards, sections = [], [], []
    cur = None          # ("card", lines) | ("sec", heading, lines)
    in_trailer = False
    for line in text.splitlines(keepends=True):
        if CAND_RE.match(line) and not in_trailer:
            cur = ["card", [line]]
            cards.append(cur)
            continue
        sm = SECTION_RE.match(line)
        if sm and not line.startswith("###"):
            if sm.group(1).strip().lower().rstrip(":") == "cards":
                cur = None          # the cards container heading itself
                in_trailer = False
                continue
            cur = ["sec", line.rstrip("\n"), []]
            sections.append(cur)
            in_trailer = is_trailer(line)
            continue
        if cur is None:
            pre.append(line)
        elif cur[0] == "card":
            cur[1].append(line)
        else:
            cur[2].append(line)
    return ("".join(pre), ["".join(c[1]) for c in cards],
            [(s[1], "".join(s[2])) for s in sections])


def sec_key(heading):
    h = heading.lstrip("#").strip().lower()
    h = re.sub(r"\s*\(.*$", "", h)
    return re.sub(r"[:\s]+$", "", h)


def render(pre, cards, sections):
    out = pre.rstrip("\n") + "\n\n" if pre.strip() else ""
    out += "## Cards\n\n"
    for c in cards:
        out += c.rstrip("\n") + "\n\n"
    for head, body in sections:
        out += head + "\n" + body.strip("\n") + "\n\n"
    return out.rstrip("\n") + "\n"


def relabel(cards, base, letter=None):
    """Card labels -> `<letter or own><base + n>`; references inside the part
    (LINKS lines, prose) to its OWN labels are rewritten with them."""
    if not base and not letter:
        return cards
    mapping = {}
    for c in cards:
        m = CAND_RE.match(c)
        old = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        new = f"{letter or m.group(1)}{base + int(m.group(2))}{m.group(3)}"
        mapping.setdefault(old, new)
    if not mapping:
        return cards
    pat = re.compile(r"(?<![\w-])(" + "|".join(sorted(map(re.escape, mapping),
                                                       key=len, reverse=True))
                     + r")(?![\w-])")
    return [pat.sub(lambda m: mapping[m.group(1)], c) for c in cards]


def merge_text(out_text, part_text, base=0, letter=None):
    p0, c0, s0 = parse(out_text) if out_text else ("", [], [])
    p1, c1, s1 = parse(part_text)
    c1 = relabel(c1, base, letter)
    have = {}
    for c in c0:
        m = CAND_RE.match(c)
        have[f"{m.group(1)}{m.group(2)}{m.group(3)}"] = True
    for c in c1:
        m = CAND_RE.match(c)
        lab = f"{m.group(1)}{m.group(2)}{m.group(3)}"
        # a label clash would make convert_inbox2 / approve map two cards to one
        # TMP id; it must never cost a candidate, so the newcomer is renamed
        n = 1
        while lab in have:
            lab2 = f"{m.group(1)}{int(m.group(2)) + 9000 + n}{m.group(3)}"
            if lab2 not in have:
                c = c.replace(f"{m.group(1)}{m.group(2)}{m.group(3)}", lab2, 1)
                lab = lab2
                break
            n += 1
        have[lab] = True
        c0.append(c)
    order, merged = [], {}
    for head, body in s0 + s1:
        k = sec_key(head)
        if k not in merged:
            merged[k] = [head, ""]
            order.append(k)
        if body.strip():
            merged[k][1] = merged[k][1].rstrip("\n") + "\n" + body.strip("\n") + "\n"
    return render(p0 or p1, c0, [(merged[k][0], merged[k][1]) for k in order])


def cmd_split(a):
    text = open(a.sweep, encoding="utf-8").read()
    pre, cards, sections = parse(text)
    os.makedirs(a.outdir, exist_ok=True)
    rest = [(h, b) for h, b in sections if sec_key(h) != sec_key(NOT_CARDED)]
    notc = [(h, b) for h, b in sections if sec_key(h) == sec_key(NOT_CARDED)]
    paths = []
    n = max(1, a.size)
    for k in range(0, len(cards), n):
        idx = k // n + 1
        p = os.path.join(a.outdir, f"{a.prefix}-{idx:02d}.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(render(f"# consolidate batch {idx} of {a.prefix} "
                            f"(candidates {k + 1}-{min(k + n, len(cards))} of {len(cards)})\n",
                            cards[k:k + n], rest if idx == 1 else []))
        paths.append(p)
    if notc:
        p = os.path.join(a.outdir, f"{a.prefix}-notcarded.md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(render("", [], [(NOT_CARDED, "".join(b for _h, b in notc))]))
        print(f"NOTCARDED {p}")
    for p in paths:
        print(p)
    print(f"split: {len(cards)} candidate(s) -> {len(paths)} batch(es) of <= {n}",
          file=sys.stderr)
    return 0


def cmd_split_run(a):
    """Atomic split: everything into a temp dir, then ONE rename commits it."""
    text = open(a.sweep, encoding="utf-8").read()
    _pre, cards, sections = parse(text)
    final = os.path.join(a.carry, a.run)
    tmp = os.path.join(a.carry, f".split-{a.run}.tmp")
    if os.path.exists(final):
        print(f"split-run: {final} exists — a run key is never reused, refusing", file=sys.stderr)
        return 3
    if os.path.exists(tmp):                    # a split killed before its commit: discard
        for f in os.listdir(tmp):
            os.unlink(os.path.join(tmp, f))
        os.rmdir(tmp)
    os.makedirs(tmp)
    rest = [(h, b) for h, b in sections if sec_key(h) != sec_key(NOT_CARDED)]
    notc = [(h, b) for h, b in sections if sec_key(h) == sec_key(NOT_CARDED)]
    n = max(1, a.size)
    names = []
    for k in range(0, len(cards), n):
        idx = k // n + 1
        name = f"cons-{idx:02d}.md"
        with open(os.path.join(tmp, name), "x", encoding="utf-8") as fh:
            fh.write(render(f"# consolidate batch {idx} of run {a.run} "
                            f"(candidates {k + 1}-{min(k + n, len(cards))} of {len(cards)})\n",
                            cards[k:k + n], rest if idx == 1 else []))
        names.append(name)
        if a.ready:                        # the batch IS its output (#237): no consolidate call
            with open(os.path.join(tmp, f"cons-{idx:02d}.out.md"), "x", encoding="utf-8") as fh:
                fh.write(render(f"# ready output of batch {idx} of run {a.run} — written by the per-mark "
                                f"evaluator, published without a consolidate call\n",
                                cards[k:k + n], rest if idx == 1 else []))
    if notc:
        with open(os.path.join(tmp, "notcarded.md"), "x", encoding="utf-8") as fh:
            fh.write(render("", [], [(NOT_CARDED, "".join(b for _h, b in notc))]))
    if a.window is not None:               # a MARK-stage chunk has no window (see the docstring)
        with open(os.path.join(tmp, "WINDOW"), "x", encoding="utf-8") as fh:
            fh.write(a.window.strip() + "\n")
    if os.environ.get("NIGHTLY_BATCH_FAULT") == "split-before-commit":
        print("split-run: FAULT INJECTED before commit", file=sys.stderr)
        os._exit(137)
    os.rename(tmp, final)
    for nm in (["notcarded.md"] if notc else []) + names:
        print(os.path.join(final, nm))
    print(f"split: {len(cards)} candidate(s) -> {len(names)} batch(es) of <= {n} "
          f"committed in {final}", file=sys.stderr)
    return 0


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_records(path, batch_id=None):
    """-> {batch: record} (the LAST line per batch), or with `batch_id` the list of ALL
    lines of that batch.  A missing record file is an empty record; any other read error
    RAISES — an unreadable manifest must never look like "nothing published yet"."""
    out, lines = {}, []
    try:
        fh = open(path, encoding="utf-8")
    except FileNotFoundError:
        return [] if batch_id is not None else {}
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or not r.get("batch"):
                continue
            out[r["batch"]] = r
            if r["batch"] == batch_id:
                lines.append(r)
    return lines if batch_id is not None else out


# verify() states.  Only ABSENT lets the nightly spend a model call on the batch, and only
# VERIFIED lets it take the batch out of the carry queue (review khmsfix-2 P1, 2026-10-01).
ABSENT, VERIFIED, CONFLICT, MISSING = "absent", "verified", "conflict", "missing"


def _append_record(record, batch_id, unit, digest):
    os.makedirs(os.path.dirname(record) or ".", exist_ok=True)
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "batch": batch_id, "unit": os.path.basename(unit), "sha256": digest},
            ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _link_unit(unit, content):
    """Exclusive creation of `unit` with `content` -> True, False when it already exists."""
    d = os.path.dirname(unit) or "."
    tmp = os.path.join(d, f".{os.path.basename(unit)}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.link(tmp, unit)                     # atomic, fails if the unit exists
    except FileExistsError:
        return False
    finally:
        os.unlink(tmp)
    return True


def expected_content(src, base=0):
    """The unit text a batch's source publishes as: normalised and relabelled exactly as
    publish() writes it, so a durable output is comparable with its published unit."""
    return merge_text("", open(src, encoding="utf-8").read(), base)


def verify(batch_id, unit, record, src=None, base=0, repair=True):
    """-> (state, message): is `unit` provably THIS batch's output?

    Binds batch id -> unit name -> sha256 (the manifest line in `record`) and, when the
    batch's source is at hand (`src`: its durable consolidate output, or the NOT CARDED
    batch file), the source's normalised content -> the same sha.  Existence of a file is
    never enough.  With `repair`, the two states a kill can legitimately leave are
    completed, both proven against `src`:
      * a unit without its manifest line (killed between the link and the record) whose
        content IS the source's -> the manifest line is written;
      * a manifest line without its unit whose sha IS the source's -> the unit is
        re-created from the source (nothing in the tools archives or moves units).
    Errors (unreadable files) RAISE: the caller reports them, it never treats them as
    ABSENT."""
    name = os.path.basename(unit)
    recs = read_records(record, batch_id)
    claimed = sorted(b for b, r in read_records(record).items()
                     if r.get("unit") == name and b != batch_id)
    if claimed:
        return CONFLICT, (f"verify: {name} is recorded as the unit of {', '.join(claimed)}, "
                          f"not of {batch_id}")
    if len({(r.get("unit"), r.get("sha256")) for r in recs}) > 1:
        return CONFLICT, f"verify: {batch_id} has contradicting manifest lines in {record}"
    rec = recs[-1] if recs else None
    exp = None
    if src:
        if not os.path.exists(src):
            return MISSING, f"verify: source {src} of {batch_id} is named but does not exist"
        content = expected_content(src, base)
        exp = sha256_text(content)
    have = sha256_text(open(unit, encoding="utf-8").read()) if os.path.lexists(unit) else None
    if rec is not None:
        if rec.get("unit") != name:
            return CONFLICT, (f"verify: the manifest of {batch_id} names unit "
                              f"{rec.get('unit')}, not {name}")
        want = rec.get("sha256")
        if not want:
            return CONFLICT, f"verify: the manifest line of {batch_id} carries no sha256"
        if exp is not None and exp != want:
            return CONFLICT, (f"verify: the source {src} of {batch_id} differs from what its "
                              f"manifest recorded as published (sha {exp[:12]} != {want[:12]})")
        if have is not None:
            if have != want:
                return CONFLICT, (f"verify: {name} was changed after {batch_id} was published "
                                  f"(sha {have[:12]} != manifest {want[:12]})")
            return VERIFIED, f"verify: {batch_id} published as {name}, content verified"
        if exp is not None and repair:
            if not _link_unit(unit, content):
                return verify(batch_id, unit, record, src, base, repair=False)
            return VERIFIED, (f"verify: {batch_id} was recorded but {name} was missing — "
                              f"re-created from {src} (sha verified)")
        return MISSING, (f"verify: {batch_id} is recorded as {name} but the unit is missing "
                         f"and no verified source can restore it")
    if have is None:
        return ABSENT, f"verify: {batch_id} not published yet"
    if exp is None:
        return CONFLICT, (f"verify: {name} exists without a manifest line of {batch_id} and "
                          f"there is no source to verify it against")
    if have != exp:
        return CONFLICT, (f"verify: {name} exists with DIFFERENT content than {batch_id}'s "
                          f"output and no manifest (sha {have[:12]} != {exp[:12]})")
    if repair:
        _append_record(record, batch_id, unit, have)
        return VERIFIED, (f"verify: {name} matched {batch_id}'s output without a manifest "
                          f"line (a kill between link and record) — manifest completed")
    return VERIFIED, f"verify: {name} matches {batch_id}'s output (manifest line missing)"


def is_published(batch_id, unit, record, src=None, base=0):
    """True only when `unit` is VERIFIED as this batch's output (no repair)."""
    try:
        return verify(batch_id, unit, record, src, base, repair=False)[0] == VERIFIED
    except (OSError, UnicodeDecodeError):
        return False


RC_HEADER = 5         # publish refused: a malformed card header (card_header.py)


def publish(src, unit, batch_id, record, base=0):
    """-> (rc, message).  Exclusive create of `unit` + a manifest line; idempotent.
    rc 0 = the unit is verified as this batch's output; rc 3 = conflict or missing
    artefact, nothing was overwritten and the batch stays in carry; rc 5 = a card header
    is malformed (card_header.check_text), no unit was created, the batch stays in carry."""
    state, msg = verify(batch_id, unit, record, src, base)
    if state == VERIFIED:
        return 0, msg.replace("verify:", "publish:", 1) + " — no-op"
    if state != ABSENT:
        return 3, msg.replace("verify:", "publish:", 1) + " — never overwritten; the batch stays in carry"
    content = expected_content(src, base)
    # The header gate (PINBOX, #209): a card whose written header is malformed never
    # reaches the inbox — 2026-10-06 published 145 `type: X; level: Y; tags: [...]`
    # candidates nobody could convert. nightly_distill.sh normalises the labelled
    # shapes first (card_header.py fix); what is still malformed here fails the night.
    import card_header
    bad = card_header.check_text(content)
    if bad:
        return RC_HEADER, (f"publish: {batch_id} REFUSED — {len(bad)} card(s) with a malformed "
                           f"header, no unit created, the batch stays in carry: "
                           + " | ".join(bad))
    if not _link_unit(unit, content):
        return publish(src, unit, batch_id, record, base)
    if os.environ.get("NIGHTLY_BATCH_FAULT") == "publish-after-link":
        print("publish: FAULT INJECTED after link, before the manifest line", file=sys.stderr)
        os._exit(137)
    _append_record(record, batch_id, unit, sha256_text(content))
    return 0, f"publish: {batch_id} -> {unit} ({len(parse(content)[1])} candidate(s))"


def cmd_publish(a):
    rc, msg = publish(a.src, a.unit, a.batch_id, a.record, a.relabel_base)
    print(msg, file=sys.stderr if rc else sys.stdout)
    return rc


RC_PUBLISHED = {VERIFIED: 0, ABSENT: 10, CONFLICT: 3, MISSING: 4}


def cmd_published(a):
    """rc 0 VERIFIED, 10 ABSENT, 3 CONFLICT, 4 MISSING or an error.  ABSENT has its own
    code so that no failure (a crash is rc 1, argparse rc 2) can pass for "not yet
    published" and buy the batch a second model call."""
    try:
        state, msg = verify(a.batch_id, a.unit, a.record, a.src, a.relabel_base)
    except Exception as e:                     # noqa: BLE001 — reported, never ABSENT
        print(f"verify: {a.batch_id}: check FAILED ({type(e).__name__}: {e}) — the batch "
              f"stays in carry", file=sys.stderr)
        return 4
    print(msg, file=sys.stdout if state in (VERIFIED, ABSENT) else sys.stderr)
    return RC_PUBLISHED[state]


def cmd_merge(a):
    out_text = open(a.out, encoding="utf-8").read() if os.path.exists(a.out) else ""
    part = open(a.part, encoding="utf-8").read()
    new = merge_text(out_text, part, a.relabel_base, a.letter)
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new)
    os.replace(tmp, a.out)
    _p, c, _s = parse(new)
    print(f"merge: {a.part} -> {a.out} (now {len(c)} candidate(s))")
    return 0


def cmd_count(a):
    try:
        _p, c, _s = parse(open(a.file, encoding="utf-8").read())
    except OSError:
        c = []
    print(len(c))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("split")
    s.add_argument("sweep")
    s.add_argument("--size", type=int, default=25)
    s.add_argument("--outdir", required=True)
    s.add_argument("--prefix", required=True)
    s.set_defaults(fn=cmd_split)
    s = sub.add_parser("merge")
    s.add_argument("out")
    s.add_argument("part")
    s.add_argument("--relabel-base", type=int, default=0)
    s.add_argument("--letter", default=None)
    s.set_defaults(fn=cmd_merge)
    s = sub.add_parser("split-run")
    s.add_argument("sweep")
    s.add_argument("--size", type=int, default=25)
    s.add_argument("--carry", required=True)
    s.add_argument("--run", required=True)
    s.add_argument("--window", default=None)
    s.add_argument("--ready", action="store_true",
                   help="also write each batch's durable output cons-NN.out.md (the batch is already "
                        "consolidated: the per-mark evaluator's output, #237) in the same commit")
    s.set_defaults(fn=cmd_split_run)
    s = sub.add_parser("publish")
    s.add_argument("src")
    s.add_argument("--unit", required=True)
    s.add_argument("--batch-id", required=True)
    s.add_argument("--record", required=True)
    s.add_argument("--relabel-base", type=int, default=0)
    s.set_defaults(fn=cmd_publish)
    s = sub.add_parser("published")
    s.add_argument("batch_id")
    s.add_argument("--unit", required=True)
    s.add_argument("--record", required=True)
    s.add_argument("--src", default=None,
                   help="the batch's durable output (or NOT CARDED file) to verify against")
    s.add_argument("--relabel-base", type=int, default=0)
    s.set_defaults(fn=cmd_published)
    s = sub.add_parser("count")
    s.add_argument("file")
    s.set_defaults(fn=cmd_count)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
