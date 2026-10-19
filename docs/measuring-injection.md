# Measuring the retrieval floor — how to find out whether the hook is paying for itself

Every parameter in `khms_hook.py` is configuration, not law (spec §7). This file is about the
number underneath all of them, which is easy never to ask for: **of everything the hook
injected, how much was used?**

In the reference deployment that question went unasked for months. When it was finally
measured, the answer was uncomfortable enough to change the wiring:

| what was measured | number |
|---|---|
| injections in 30 days | 2755 — 2044 before tool calls, 502 on operator messages, 209 after tool results |
| share of the cards the agent actually cited that came from an injection | **≤ 16 %** |
| relevance of what was injected, judged by hand on a day's sample | **~50 %** |
| injections whose card appeared in one of the next three assistant messages | **6.6 %** overall: 23.2 % on operator messages, 2.8 % before tool calls, **0 of 36** after tool results |
| hook calls per day that loaded the whole card base and then dropped the call on a budget gate | **2324** (~172 ms each) |
| share of the retrieval log that was the hook recording its own non-retrievals | **87 %** |

None of that says retrieval does not work. It says these particular EVENTS, at these
thresholds, on a base of this size, mostly did not — and that the cost was being paid anyway,
on every message and every tool call.

## The tools

- **`tools/inject_cited.py`** — injected → cited, per event and per day. It reads
  `tools/.inject.log` and your session transcripts and asks, for every injection, whether one
  of the next three assistant messages names the card. Point `--transcripts` (or
  `KHMS_TRANSCRIPTS`) at the directory your harness writes them to.
  Read its docstring before quoting a number from it: the citation rate is an UPPER bound
  where sessions overlap, a FLOOR on influence (a card that changed a decision without being
  named is invisible), and records it cannot attribute to a session are reported separately
  rather than counted as unused. Folding "we could not tell" into "it was not used" is how a
  measurement turns into an argument.
- **`tools/khms_debt.py`** — the other half of the same question: how much of the base has
  never been injected and has never been a recall top hit. In the reference deployment 43 % of
  cards had never been either.
- **`tools/.inject.log`** — every decision, including the silences and the reason
  (`dedup`, `cap`, `threshold`). Without it none of the above is computable after the fact.

## Running an experiment

`tools/khms_experiment.json` (example: `tools/khms_experiment.example.json`) switches card
injection off per event, and the hook stamps each log line with the regime that produced it,
so a week's records say which week they belong to:

```json
{
  "since": "2026-03-01",
  "name": "no-ups",
  "inject": {"UserPromptSubmit": false, "PostToolUse": false,
             "PostToolUseFailure": false, "PreToolUse": true},
  "review_on": "2026-03-08",
  "why": "one line, so that in a week you know what you were asking"
}
```

Rules that make it an experiment rather than a change of mind:

1. **Fail open in both directions.** A missing, empty or broken file means "behave exactly as
   before". Deleting the file is therefore the complete rollback, and no code changes back.
2. **Write the baseline down BEFORE the switch, with its window.** A comparison against a
   number you reconstruct afterwards from file mtimes is not a comparison.
3. **Name the review date in the file itself**, and the decision rule with it. "We will look at
   it in a week" without a rule becomes "we got used to it".
4. **Check that your metric has a baseline at all.** In the reference deployment the primary
   metric — a marker written into the journal on every correction — had only come into use two
   days before the experiment started, so a week of it would have measured journalling habits
   rather than errors. The fix was a second, objective metric that does not depend on the agent
   writing anything down: corrections in the operator's own messages, counted from the message
   archive. If your metric can go to zero because a channel went quiet, it is not measuring
   what you think.
5. **Two metrics that are complements, read together.** One of them counts what the agent
   admits; the other counts what the operator had to correct. A day with many of the first and
   none of the second is usually a day that ran in a channel the second cannot see — not a
   clean day.

## What the reference deployment did with the answer

`PostToolUse` and `PostToolUseFailure` came OUT of the wiring (see
[claude-code/README.md](../claude-code/README.md)) — 0 of 36 of their injections were ever
cited, and an error string being "a near-perfect query" turned out to be an argument, not a
measurement. `UserPromptSubmit` injection went off for one week as an experiment, with the
directive lines kept: the experiment removes the CARDS, not the duty to say where an answer
came from. `PreToolUse` stayed on, and so did the automatic `precheck.sh` for the risky-command
class, which is not part of the experiment — that path exists for the command that is about to
delete something, and its value does not depend on citation counts.

**Before you copy any of this: those numbers are one deployment's, on a base of ~2400 cards,
with one operator.** The method transfers. The conclusion does not.

## Finding: the slot budget, not the switch — a worked example of blaming the wrong thing

Halfway through the week without operator-prompt injection, the operator hit a problem whose
answer had been in the base for weeks: one card, one line, written after the same problem two
months earlier. An hour and two agents went into re-deriving it. The obvious suspect was the
experiment — injection was off, so of course the card did not arrive.

It was not the experiment. The prompt was replayed through the live hook with the experiment
config pointed at a nonexistent file (`KHMS_EXPERIMENT=/nonexistent`), state and logs redirected
to a scratch directory, and the pre-experiment policy produced this:

```
… | UserPromptSubmit | q=<the operator's sentence> | nhits=50 | top=K-NNNNN:17.8
  | INJECT [K-NNNNN,K-NNNNN] chars=375
    skipped=K-NNNNN:threshold 17.8<18,K-NNNNN:slots-full,…
    mode=hybrid+rescue
```

The wanted card is in that line — as `slots-full`. The dense channel HAD it, the two-slot budget
had no room for it, and the pre-experiment hook would not have surfaced it either. Three things
follow, and all three are the reason `.inject.log` records its silences:

1. **The experiment must not be credited with this miss** at the review date. Without the replay
   it would have been, and the wrong parameter would have been changed.
2. **A skip reason is worth more than a hit.** `slots-full` distinguishes "retrieval never found
   it" from "retrieval found it and the budget dropped it" — different defects with different
   fixes, indistinguishable from the outside.
3. **The narrow lever did not fix its own motivating case.** The obvious response — reserve a
   slot for the dense channel's best card (`DENSE_RESERVED_SLOT` in the hook, default off) — was
   built and measured: on the record it changes about one in seven operator-prompt decisions,
   always by replacing the second card rather than adding one, and *on this incident it does
   nothing at all*, because the dense channel also ranked the wanted card third, 0.017 of a
   cosine behind its top hit. What surfaced the card was widening the budget (`MAX_PRIMARY = 3`).
   A lever that is inert on the example that motivated it is a lever chosen from a story rather
   than from a measurement.

Note also what the replay had to be careful about: an earlier reading of the same lever ("give
the slot to the dense channel's rank-1 card") was implemented first and measured at 1 changed
decision in two days instead of 6 — because rank 1 was usually already among the picks. The
shipped rule is "the dense channel's best hit THAT THE LEXICAL PICKS DO NOT ALREADY CONTAIN".
Two readings of one sentence, one of them inert, and only the measurement told them apart.

## Keep a golden set of your real misses

`tools/eval/run_eval.py --prod` scores a frozen set of (query, expected card) pairs through the
exact path `recall.sh` runs, and prints one line. Every row should be a MISS THAT ACTUALLY
HAPPENED — the query as it was typed, and the id of the card that should have come back. That
file becomes the record of every hole retrieval has fallen into, and the only thing that can
tell an improvement from a story.

Rows carry `gate: true` (a regression here fails the run) or `gate: false` (open debt: counted,
named, not fatal). Keep that distinction honest. A case nothing currently fixes must not be
gated, because a permanently red gate is a gate nobody reads — and a case something was supposed
to fix must be gated the day it is claimed fixed.
