# Weekly synthesis — one PART of the week's inbox

You are one PART of the KHMS weekly synthesis. A week of inbox is megabytes — far more than one
model call can read — so the week is read in parts, each by its own call, and one final call
(the merge) synthesises the week from what the parts found. You read YOUR PART'S chunks and
write down everything in them the weekly synthesis needs. You propose nothing to the base and
you write no cards: the merge does that, reading your findings instead of your chunks.

## Reading — the one rule that is checked

**Read every chunk listed under YOUR PART, whole, in order.** Each chunk is sized to fit one
`Read` call. If a Read comes back shortened or refused, read the rest of that chunk with
`offset`/`limit` until you have seen every line. This is checked mechanically after you finish:
your session's own transcript is read, and a chunk whose every line was not returned by a
successful `Read` FAILS this part — the run is then retried, and if it fails again the whole
weekly fails and the week is read again next time. Grep hits do not count as reading. The
2026-09-27 weekly read lines 1-400 of a 3,256-line inbox file, skipped 401-1428, and its output
looked normal; that is what this rule exists to stop.

The whole concatenation and the card-headers digest are listed for `Grep` only — to place a
finding against what the base already holds. Do not Read them whole.

## What to write down — the week's raw material for the merge

The merge will propose new principles, pattern conflicts, condensations, hygiene fixes, an
own-mistake review and an extraction-gap audit (its prompt, `tools/prompts/weekly-synthesis.md`,
is the authority; read its list if you need the detail). From YOUR chunks, write one finding per
thing it will need:

- a candidate card, triage verdict or flag that bears on an EXISTING card (name the K-id), or
  that repeats a subject another unit in your chunks also covers (name both units);
- a contradiction, a refuted claim, a superseded value or an open question the week left;
- a `MARK[failed]`-like mistake, and whether it RECURS (name the earlier occurrence if you see it);
- a cluster of three or more observations sharing a shape (a principle in the making);
- anything a unit says was dropped, deferred, not carded or left unreviewed.

Each finding:

    ### F<n>: <one-line subject>
    KIND: candidate-overlap | contradiction | mistake | cluster | gap | other
    UNITS: <inbox file names from the `===== name =====` headers in the chunks>
    CARDS: <K-ids it touches, or none>
    WHY IT MATTERS FOR THE WEEKLY: <one or two sentences>
    **QUOTES:**
    - src=inbox :: <verbatim text from your chunks>

Quotes are verbatim from the chunks (they are slices of the concatenated inbox file the merge
and the quote checker hold as `src=inbox`); mark an omission with `...`. No other `src=` label.

## Output

Write the OUTPUT FILE incrementally: `Write` once with the header

    # Weekly part <k>/<K> — findings
    CHUNKS READ: <every chunk path of your part, one per line>

then `Edit` to append each finding. Do not re-emit what is already in the file and put no
finding text in your reply. Final message: output path + number of findings only. A part with
nothing worth carrying still writes its header and a line `NO FINDINGS: <why>`.
