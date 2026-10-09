#!/usr/bin/env python3
"""card_header.py — the nightly's gate on the card header shape (board #209, PINBOX).

Usage: python3 tools/card_header.py {check|fix|quarantine} FILE [FILE ...]
  check  rc 0 = every WRITTEN header is well-formed; rc 5 = malformed (one line per
         card on stdout); cards with no header at all are a warning on stderr.
  fix    rewrite FILE in place: a dashed card label (`C13-01`) is joined (`C1301`)
         everywhere in the file; a key-LABELLED compound header line is split into one
         `key: value` line per key; then the same check (rc 5 if something is left).
  quarantine  move every card whose header is still malformed, whole, under a
         `## DROPPED (malformed header …)` trailer (the converter does not read it), with its
         reason — the night runs it before publish, so 1 bad header no longer holds a batch.

THE ONE SHAPE (stated, with this example, in every nightly prompt that writes cards):
right under the `### C<n>: <title>` heading, one `key: value` per line —

    type: problem→solution
    level: observation
    tags: [nav, gotcha]

`type` is one of spec §4.2's nine types, `level` the one its row names (or
`assumption`), `tags` a `[a, b]` list; `scope`, `evidence`, `source`, `date` may
follow, each on its own line.

WHY. No prompt ever stated the header shape, so it was whatever the model wrote that
night: a fenced YAML block (2026-08-24, 10-04), bare lines (08-25, 10-05), nothing
(08-27), `level (type)` (09-20), `type: X · level: Y · …` (09-26+) and, on 2026-10-06,
`type: X; level: Y; tags: [...]` on all 145 candidates. Each new shape reached the
morning review unseen and was patched into `convert_inbox2.py` after the fact. Now the
shape is stated at the source, and the stage that receives the model's output checks
it: `nightly_distill.sh` runs `fix` over every consolidate output, and
`nightly_batch.py publish` refuses (rc 5) a batch whose header is still malformed — the
night fails loudly and the batch stays in carry instead of reaching the inbox.

WHAT IS NORMALISED AND WHAT IS NOT. A compound line whose every field is LABELLED by a
known key (`; ` or ` · ` before `<key>:`) is split — nothing is guessed, the labels say
where each value goes, and every value is kept verbatim. Nothing else is rewritten. What
is REFUSED is decided by the CONVERTER's own rule (`convert_inbox2.normalise_type` against
the body template), so the night refuses exactly what the morning could not convert
without guessing (flag `T`) and nothing stricter: keys still joined on one line, an
unlabelled value (`type: fact, level observation`), a type outside §4.2 (`gotcha`), a
level that contradicts the type, a `level (type)` whose halves disagree, tags that are
neither `[a, b]` nor `a, b`. (`observation (fact)` and `tags: a, b` pass — the converter
resolves both deterministically — although the prompts ask for the one shape above.)
A card with no header at all is still convertible from its body template
(`convert_inbox2.derive_type`), so it is a warning, not a refusal.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from convert_inbox2 import (LABEL, META_KEYS, META_LINE_RE, TRAILERS, derive_type,  # noqa: E402
                            normalise_type)

RC_MALFORMED = 5

# The header lines as every nightly prompt shows them (4-space markdown code block).
PROMPT_EXAMPLE = ("    type: problem→solution\n"
                  "    level: observation\n"
                  "    tags: [nav, gotcha]")

CARD_RE = re.compile(r"^#{2,3} +([A-Z]\d+[a-z]?)\b")
SECTION_RE = re.compile(r"^## +\S")
FENCE_OPEN_RE = re.compile(r"^\s*```ya?ml\s*$")
_KEYS = "|".join(sorted(META_KEYS, key=len, reverse=True))
_SEP = r"\s*[;·]\s*"
# a separator followed by a known key: the footprint of several keys on one line
JOINED_RE = re.compile(r"%s(?:%s):" % (_SEP, _KEYS))
FIELD_RE = re.compile(r"(?:^|%s)(%s):[ \t]*(.*?)(?=%s(?:%s):|$)" % (_SEP, _KEYS, _SEP, _KEYS))


def _is_meta(line):
    m = META_LINE_RE.match(line)
    return bool(m and m.group(1) in META_KEYS)


def _cards(lines):
    """-> [(label, heading index, end index)] for every card block of `lines`."""
    starts = [(i, CARD_RE.match(ln).group(1)) for i, ln in enumerate(lines)
              if CARD_RE.match(ln)]
    out = []
    for n, (i, label) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        for j in range(i + 1, end):
            if SECTION_RE.match(lines[j]) and not lines[j].startswith("###"):
                end = j
                break
        out.append((label, i, end))
    return out


def _header(lines, i, end):
    """-> (kind, [line indices]) of the card's header: 'fence', 'lines' or 'none'."""
    j = i + 1
    while j < end and not lines[j].strip():
        j += 1
    if j < end and FENCE_OPEN_RE.match(lines[j]):
        k = j + 1
        idx = []
        while k < end and lines[k].strip() != "```":
            idx.append(k)
            k += 1
        return "fence", idx
    idx = []
    while j < end and lines[j].strip():
        if _is_meta(lines[j]):
            idx.append(j)
        elif idx and lines[j][:1] in (" ", "\t"):
            idx.append(j)          # YAML continuation (a nested links: block)
        else:
            break
        j += 1
    return ("lines" if idx else "none"), idx


def split_compound(line):
    """-> [`key: value`, ...] when `line` is a key-labelled compound header line, else None."""
    if not _is_meta(line) or not JOINED_RE.search(line):
        return None
    fields = FIELD_RE.findall(line.rstrip())
    keys = [k for k, _ in fields]
    if len(fields) < 2 or len(set(keys)) != len(keys):
        return None
    return [f"{k}: {v.strip()}" for k, v in fields]


def normalise_text(text):
    """-> (text, [labels of changed cards]). Only labelled compound lines are touched."""
    lines = text.split("\n")
    changed = []
    for label, i, end in reversed(_cards(lines)):
        kind, idx = _header(lines, i, end)
        if kind != "lines":
            continue
        hit = False
        for j in reversed(idx):
            parts = split_compound(lines[j])
            if parts:
                lines[j:j + 1] = parts
                hit = True
        if hit:
            changed.append(label)
    return "\n".join(lines), list(reversed(changed))


TAG_LIST_RE = re.compile(r"^\[.*\]$|^[\w-]+(?:\s*,\s*[\w-]+)*$")


def _problems(meta_lines, prose):
    """Why a written header is malformed. The type/level rule is the CONVERTER's own
    (`convert_inbox2.normalise_type` against the body template): the night refuses
    exactly what the morning review could not convert without guessing (flag `T`), so
    this gate never invents a requirement the converter does not have."""
    meta, why = {}, []
    for ln in meta_lines:
        m = META_LINE_RE.match(ln)
        if not m:
            continue
        if JOINED_RE.search(ln):
            why.append(f"several header keys joined on one line ({ln.strip()!r}) — "
                       f"one `key: value` per line")
        meta.setdefault(m.group(1), m.group(2).strip().strip("'\""))
    if "type" not in meta:
        why.append("the header has no `type:` line")
    elif not why:
        body_type = derive_type(prose)[0]
        typ, _lvl, note = normalise_type(meta["type"], body_type, meta.get("level") or None)
        if typ is None:
            why.append(note)
    tags = meta.get("tags")
    if tags is not None and not TAG_LIST_RE.match(tags):
        why.append(f"tags {tags!r} is not a `[a, b]` list")
    return why


def _trailer_cut(lines):
    """Index of the first review/plumbing heading (convert_inbox2.TRAILERS): the converter reads
    no card past it, so neither does this gate (PDRAIN, #209 — a candidate already MOVED to
    `## DROPPED` must not refuse the batch it was moved out of)."""
    heads = tuple(t.lstrip("\n") for t in TRAILERS)
    for j, ln in enumerate(lines):
        if ln.startswith(heads):
            return j
    return len(lines)


# A card heading the converter cannot read is a silent zero at the review (PDRAIN2, #209): the
# batch tool's CAND_RE stops at a word boundary, so `### C13-01:` or `### C7.1:` publish as
# label C13 / C7, while convert_inbox2.CARD_RE (label, optional `(…)`, then `:`) reads no card.
CONVERTER_HEAD_RE = re.compile(r"^#{2,3} +" + LABEL + r":")
UNREADABLE_LABEL = ("card label not readable by the converter (convert_inbox2.CARD_RE wants "
                    "`### <letter><digits>[a-z]: title`, optional `(…)` before the colon)")
DASHED_RE = re.compile(r"^#{2,3} +([A-Z]\d+-\d+[a-z]?)(?= *[:(])", re.M)


def normalise_labels(text):
    """-> (text, [(old, new)]). A dashed card label `C13-01` becomes `C1301` — the digits
    joined, nothing guessed — everywhere in the file, so references in Flagged/Dropped follow.
    A label whose joined form already exists is left alone (the check then refuses it)."""
    renamed = []
    for old in dict.fromkeys(DASHED_RE.findall(text)):
        new = old.replace("-", "")
        if re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(new), text):
            continue
        text = re.sub(r"(?<![\w-])%s(?![\w-])" % re.escape(old), new, text)
        renamed.append((old, new))
    return text, renamed


def _malformed(lines):
    """-> [(label, start, end, why)] for every card before the trailers whose header is malformed."""
    cut = _trailer_cut(lines)
    out = []
    for label, i, end in _cards(lines):
        if i >= cut:
            break
        if not CONVERTER_HEAD_RE.match(lines[i]):
            out.append((label, i, end, UNREADABLE_LABEL))
            continue
        kind, idx = _header(lines, i, end)
        if kind == "none":
            continue
        body_from = (idx[-1] + 2 if kind == "fence" else idx[-1] + 1)
        why = _problems([lines[j] for j in idx], "\n".join(lines[body_from:end]))
        if why:
            out.append((label, i, end, "; ".join(why)))
    return out


# QUARANTINE, NOT REFUSAL OF THE WHOLE BATCH (PDRAIN, #209). Until 2026-10-06 one card with a
# malformed header made `publish` refuse the batch (rc 5) — and a refused DURABLE output is
# re-published unchanged every following night, refused every night: 4 headers held 25 cards and
# their MARK ids in the carry queue for good (drain 2026-10-06, cons-05: `decision→rationale` with
# `level: derived`). The rule is unchanged — nothing is guessed — but the malformed card is MOVED,
# whole, under this trailer heading (which the converter does not read), so the review sees it
# with its reason and the batch's other cards publish.
QUARANTINE_HEAD = "## DROPPED (malformed header — NOT guessed; fix the header by hand or discard)"


def quarantine_text(text):
    """-> (text, [(label, why)]): malformed cards moved under QUARANTINE_HEAD."""
    lines = text.split("\n")
    bad = _malformed(lines)
    if not bad:
        return text, []
    moved = []
    for label, i, end, why in reversed(bad):
        block = lines[i:end]
        del lines[i:end]
        moved.insert(0, (label, why, block))
    out = "\n".join(lines).rstrip("\n") + "\n\n" + QUARANTINE_HEAD + "\n\n"
    for label, why, block in moved:
        out += "\n".join(block).rstrip("\n") + "\nMALFORMED HEADER: " + why + "\n\n"
    return out, [(label, why) for label, why, _b in moved]


def check_text(text):
    """-> ['<label>: <why>; <why>', ...] for every card whose WRITTEN header is malformed."""
    return [f"{label}: {why}" for label, _i, _e, why in _malformed(text.split("\n"))]


def missing_headers(text):
    lines = text.split("\n")
    return [label for label, i, end in _cards(lines) if _header(lines, i, end)[0] == "none"]


def _write(path, text):
    import khms_paths as P
    P.require_may_write([path], "normalize candidate headers")
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".card_header-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    os.replace(tmp, path)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "quarantine":
        rc = 0
        for path in argv[1:]:
            new, moved = quarantine_text(open(path, encoding="utf-8").read())
            if moved:
                _write(path, new)
            print(f"card_header quarantine {path}: {len(moved)} card(s) with a malformed header "
                  f"moved, whole, under '{QUARANTINE_HEAD}'"
                  + "".join(f"\n  {lab}: {why}" for lab, why in moved))
        return rc
    if len(argv) < 2 or argv[0] not in ("check", "fix"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    rc = 0
    for path in argv[1:]:
        text = open(path, encoding="utf-8").read()
        if argv[0] == "fix":
            text, renamed = normalise_labels(text)
            if renamed:
                _write(path, text)
                print(f"card_header fix {path}: {len(renamed)} dashed card label(s) joined: "
                      + ", ".join(f"{a}->{b}" for a, b in renamed))
            new, changed = normalise_text(text)
            if changed:
                _write(path, new)
                text = new
            print(f"card_header fix {path}: {len(changed)} header(s) split into one key "
                  f"per line" + (f" ({', '.join(changed)})" if changed else ""))
        probs = check_text(text)
        miss = missing_headers(text)
        if miss:
            print(f"card_header {path}: WARNING {len(miss)} card(s) without a header "
                  f"(converted from the body template): {', '.join(miss)}", file=sys.stderr)
        for p in probs:
            print(f"card_header {path}: MALFORMED HEADER {p}")
        if probs:
            rc = RC_MALFORMED
        elif argv[0] == "check":
            print(f"card_header check {path}: OK")
    return rc


if __name__ == "__main__":
    sys.exit(main())
