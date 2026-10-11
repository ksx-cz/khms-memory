# AGENTS.md — set up KHMS memory for yourself, in one pass

You are an agent that has been told: *"set up your memory per this repo."* This file is the
whole job. Work top to bottom. Everything you need is in this repository; nothing here needs
network access, a database, or a service.

Before you start, know the two rules that outrank convenience:

1. **You may not write into `memory/know/` without your operator's approval.** Background
   stages propose; approval is a separate, human-or-strong-model step. This is what keeps a
   fabricated card from becoming a remembered fact.
2. **Cards are immutable once approved.** You will be tempted to "just fix" one. Don't. Write
   a new card that supersedes it. The wrong old card is evidence about how you were wrong.

---

## Step 1 — Decide where memory lives, and set `KHMS_ROOT`

Keep a code clone and a separate data directory. `KHMS_CODE` below is a shell
convenience naming the clone; tools resolve data through `KHMS_ROOT` or registration,
never by copying tools into the base. Install Python 3 and PyYAML for view generation.

```bash
git clone https://github.com/ksx-cz/khms-memory.git "$HOME/khms"
export KHMS_CODE="$HOME/khms"
export KHMS_ROOT="$HOME/.agent-memory"
"$KHMS_CODE/tools/khms_init.sh" --register "$KHMS_ROOT"
python3 "$KHMS_CODE/tools/build_views.py"
python3 "$KHMS_CODE/tools/mark_ledger.py" sync
"$KHMS_CODE/tools/recall.sh" --lexical "checksum mismatch"
```

The ledger sync establishes MARK coverage from the journal before the first hook
check. Run it again after journal changes to refresh coverage; it approves no cards.

An empty base is a valid first install. Recall prints its lexical channel and
"nothing on record" (exit 1 for no matches, not an installation failure). No model,
embedding service or network call is needed for this check. Persist `KHMS_CODE` in
your shell profile; registration lets tools find the base without `KHMS_ROOT`.
See [README.md](README.md#safe-code-updates) before updating the clone.

`khms_init.sh` is idempotent and copies no code: it resolves the configuration every tool will
use (environment, `KHMS_CONFIG_FILE`, `$KHMS_ROOT/config/khms.env`), refuses a reader
deployment (`KHMS_READ_ONLY=1`, rc 4) or an invalid configuration (rc 3) before writing, then
creates the directories, the ID counter in the configured shared state and an empty tag
registry. It writes nothing into `memory/know/`.

**Version history belongs to the storage service.** Do not create local snapshots
or pre-write copies. Before deployment, verify that the configured server retains
file versions and test restoring an isolated fixture by content hash. Recovery is
per file: reconcile the corresponding review receipt, ledger and counter, then
rebuild views/indexes. Unsynchronized changes and expired versions cannot be
promised recoverable. Existing pinned experiment evidence is not deleted by tools.

Resulting layout — memorize it, you will refer to it constantly:

```
$KHMS_ROOT/
  MEMORY.md                 generated index; keep this in your context always (≤80 lines)
  memory/know/K-*.md        the cards — one claim per file, immutable
  memory/views/             generated: topics/<tag>.md, by-type/<type>.md, tags.md, recent.md
  memory/inbox/             proposals awaiting review (temp labels, no IDs yet)
  memory/inbox/.staging/    what each pipeline stage was actually handed (forensics)
  memory/archive/know/      superseded and condensed cards — "fog", still greppable
  journal/YYYY-MM-DD.md     what happened today, written as it happens
  config/khms.env           optional settings; no executable code
  state/                    agent logs and caches (default for a private base)
  state/shared/next_id      card ID counter (default shared-state location)
```

## Step 2 — Learn the card format (this is the part you must not improvise)

One card = one file = one claim. Read [spec/khms-spec.md](spec/khms-spec.md) §4 once in full;
this is the operational summary.

```markdown
---
id: K-NNNNN                     # K-NNNNN, sequential, assigned at approval, never reused
type: problem→solution          # shape of the knowledge — see the table below
level: observation              # observation | derived | assumption
status: active                  # active | challenged | refuted | superseded | condensed
tags: [sensors, gotcha]         # flat keys, from memory/views/tags.md
scope: device:weather-station   # where the knowledge holds (generality tree)
evidence: measured              # observations only: measured | observed | reported
source: 'journal/2026-03-04.md line 22; log excerpt "checksum mismatch"'   # observations only
date: 2026-03-04                # when the knowledge was established
links:
  derived_from: []              # required non-empty for level: derived
  supports: []
  contradicts: []
  supersedes: null
  refuted_by: []
---
SYMPTOM: ...
CAUSE: ...
FIX: ...
VERIFIED: ...
```

| `type` | use when | body template | level |
|---|---|---|---|
| `action→outcome` | an attempt had a result (worked, failed, partial) | `WHEN:` `THEN:` | observation |
| `problem→solution` | a symptom was diagnosed and the fix verified | `SYMPTOM:` `CAUSE:` `FIX:` `VERIFIED:` | observation |
| `fact` | static reality with exact values (IDs, addresses, configs, external findings) | free statement | observation |
| `requirement` | someone stated a need | `WHO:` `WHAT:` `WHY:` `DONE-CRITERIA:` | observation |
| `decision→rationale` | an alternative was chosen | `DECIDED:` `WHY:` `REJECTED:` | observation |
| `principle` | ≥2 independent observations share a shape | `HOLDS:` `LIMITS:` `IMPLICATIONS:` | derived |
| `policy` | a way of working is adopted | `RULE:` `LIMITS:` `IMPLICATIONS:` | derived |
| `goal→method` | a repeatable method exists | `GOAL:` `METHOD:` `PREREQUISITES:` `COST:` | derived |
| `overview` | a topic needs an anchor and a map | short narrative + links | derived |

Ids in this repository are placeholders: `K-NNNNN` in prose, the `K-9xxxx` band in the
tests, and the fictional low ids of [examples/](examples/). A real base allocates from
its own counter upward, so nothing here can be mistaken for a card of anybody's actual
memory.

Four rules that decide most edge cases:

- **Descriptive and normative never share a card.** What happened is an observation; what to
  do about it is a `derived` card linked by `derived_from`. A recommendation can die without
  killing the fact under it, and one fact can feed two competing recommendations.
- **Observations need `evidence` and `source`; derived cards need non-empty `derived_from`.**
  `build_views.py` exits non-zero if you break this, so a broken card cannot ship silently.
- **`measured` means you did it and saw the result.** Something a person told you, or a vendor
  document says, or a paper claims, is `reported` — even when you are sure it is true.
- **One card, one claim.** If the body needs "and also", it is two cards.

Working examples of every shape: [examples/](examples/).

## Step 3 — Bootstrap the two cards the system needs about itself

The empty-base smoke check above does not approve cards. Prepare candidate cards
in `memory/inbox/` describing the spec and bootstrap digest, with valid provenance:
a derived policy requires non-empty `derived_from` pointing to approved observations.
Do not paste the spec into `memory/know/` or invent evidence to satisfy the schema.
Have your operator review the candidates, then use `approve_inbox.py` as described
in Step 6. Until then, keep the spec and digest from this clone in context.

```bash
python3 "$KHMS_CODE/tools/build_views.py"  # validates cards, generates views + MEMORY.md
```

`MEMORY.md` is generated — never edit it by hand, and never edit anything in `memory/views/`.
Its §1 is the body of the bootstrap-digest card, so the retrieval rules travel with the index
that is always in your context.

## Step 4 — Capture during work: the MARK convention

You will not remember at 23:00 what you decided at 09:00, and a summary written at the end of
a session is a summary of what you still remember, which is the wrong sample. So capture as it
happens, cheaply, into `journal/YYYY-MM-DD.md`:

```
MARK[decision] switched to polling the sensor at 1 Hz — bus contention above that (sess a1b2)
MARK[failed]   tried raising the timeout to 5 s; same checksum errors (sess a1b2)
MARK[req]      operator wants the daily report to state the battery percentage (sess a1b2)
MARK[solved]   checksum errors were a loose ground, not a timing problem (sess a1b2)
```

One line per decision, stated requirement, failed attempt, solved problem. Kinds are open;
those four cover most of it. Write a **full card inline** only when the knowledge is `measured`
*and* immediately reusable; everything else is the nightly sweep's job, and the MARKs tell it
where to dig. A MARK costs you one line; a lost decision costs a re-derivation.

## Step 5 — Retrieval: a floor you install and a ceiling you owe

**The floor** is mechanical. Hooks call `khms_hook.py` on session start, on every operator
message, and before file edits and shell commands; it scores the text
against the card base and injects at most a couple of cards, under a budget. It runs whether or
not you remember it exists. Install it per [claude-code/README.md](claude-code/README.md).

**The ceiling is your duty, and the floor does not discharge it:**

- Run `tools/recall.sh "<symptom or error string or identifier>"` **before you root-cause
  anything**. Paste the artifact you actually have — the error text, the odd number, the
  parameter name. This is deliberately not tag-based: classifying the situation correctly is
  exactly what fails when you are lost.
- Run `tools/recall.sh "<what you are about to propose>"` **before you state a hypothesis or
  propose a change**. The proposal is the moment when repeating a documented dead end is most
  expensive, and the hook cannot fire on what you have not yet said.
- Run `tools/precheck.sh <tag> [tag...]` **before a risky action** (deleting things, changing
  configuration, writing to hardware, deploying). Zero model tokens: it prints active policies,
  gotchas, and refuted dead ends for those tags. The hook also runs it automatically for a
  named class of dangerous commands — that automation is a backstop, not a substitute.
- **An injected card is a pointer, not an answer.** If it is relevant, open the file and follow
  its links (`derived_from`, `supports`, `contradicts`, and any `[[K-…]]` in the body).
- **"Nothing on record" is a real and useful answer.** Say it rather than inventing coverage.

Retrieval ladder, cheapest first — go down it in order:
`MEMORY.md` (already in context) → `memory/views/topics/<tag>.md` → `precheck.sh` →
`recall.sh` / grep `memory/know/` → fog (`memory/archive/`) → **external sources** (vendor
docs, manuals, papers, web). The last rung is *mandatory* when you are about to assert a fact
no `measured` card covers. External findings come back as `reported` observations with the URL
and a locating quote in `source`.

## Step 6 — The write path: propose → consolidate → approve

Nothing writes into `memory/know/` except the approval step. The pipeline exists so that the
expensive attention is spent only where it is needed.

1. **Nightly sweep** (`tools/nightly_distill.sh`, cron ~03:30). Deterministic inputs first, at
   zero model cost: `preprocess_transcripts.py` turns session transcripts into a digest of
   message texts, tool names and errors; the journal files touched since the last run; the
   day's git log. A cheap model extracts candidate cards (over-generation is fine), each
   carrying a `**QUOTES:**` block whose lines must occur *verbatim* in a named source.
2. **Mechanical grounding check** (`tools/verify_quotes.py`, zero model cost). Greps every
   quote against the source it names, and flags every specific (number, identifier, log string)
   that no quote backs. Its report is an input to the next stage. This is the difference
   between asking a model not to fabricate and checking that it did not.
3. **Consolidate** (mid-tier model). Enforces the grounding report mechanically (unfound quote
   → the claim goes; unbacked specific → the specific goes; no quotes → the card goes),
   deduplicates, enforces schema, links candidates to existing cards, flags contradictions and
   status-change proposals. Writes `memory/inbox/DATE.md`. Still no IDs, still no authority.

   **Merge pressure, because a pipeline that only produces never merges.** Before the model is
   asked what a candidate replaces, `tools/nearest_cards.py` TELLS it which active cards rank
   nearest (zero model tokens, one `NEAREST:` line per candidate). After it answers,
   `tools/verify_relations.py` CHECKS the answer: every candidate must carry exactly one
   `RELATION:` line naming an existing card it supersedes / supports / contradicts, or saying
   in one sentence why its nearest is unrelated. It also enforces a per-night CAP
   (`NIGHTLY_MAX_CARDS`, `WEEKLY_MAX_CARDS`) that is a review-capacity number, not a knowledge
   one. Nothing is deleted: a candidate that fails is MOVED, whole, into
   `## DROPPED (no valid RELATION)`, and the overflow into `## DEFERRED`, both at the end of
   the same file. Ask for this early — measured in the reference deployment, a base without it
   held 17 cards about one measured parameter and 8 superseded edges in total.
4. **Morning review** (you, in a full session, at the start of the day). Read the `## Flagged`
   section with real attention; approve or fix the rest quickly. Then:

   ```bash
   "$KHMS_CODE/tools/approve_inbox.py" "$KHMS_ROOT/memory/inbox/$(date +%F).md"
   "$KHMS_CODE/tools/build_views.py"
   ```

   `approve_inbox.py` STOPS at `## DEFERRED` and `## DROPPED` — the candidates the relation
   gate withheld are reported and not imported, and rescuing one means moving its block back
   under `## Cards` by hand. It then allocates the sequential IDs from `KHMS_SHARED_STATE/next_id`,
   rewrites temp cross-references, and writes the cards. It exits non-zero if any card failed to parse —
   treat a partial import as a failure, never as a finished one. It also **refuses the whole
   run, before allocating any ID, when a card says it corrects something without naming what**
   (`tools/khms_lint.py`): add the `contradicts` / `supersedes` / `refuted_by` edge it asks for
   and re-run. A correction only prose can see is one your retrieval will never reach. Finish
   with a two-sentence report to your operator in their language.
5. **Weekly synthesis** (`tools/weekly_synthesis.sh`, after a successful night, due every seven calendar days) + weekly review.
   Proposes `principle` cards where three or more observations share a shape, puts contradicting
   patterns side by side with the cheapest experiment that would discriminate them, and ranks
   condensation candidates. Condensing is not deleting: the absorbing pattern must carry the
   information, and the absorbed card moves to `memory/archive/know/`, still greppable.

**Review is also where you analyze your own mistakes.** Not just the cards: what went wrong
that day, why, and what mechanism — not what intention — would have prevented it. If the same
mistake recurs, the previous countermeasure was wrong; choose a different one, of a different
kind. "I will be more careful" is not a countermeasure. A countermeasure is a step that either
happened or did not, and that leaves a trace showing which.

## Step 7 — Verify the install

```bash
"$KHMS_CODE/tools/build_views.py"                 # -> "OK: N cards …"; non-zero = schema break
"$KHMS_CODE/tools/verify_relations.py" --self-test  # -> self-test passes
"$KHMS_CODE/tools/recall.sh" --lexical checksum mismatch    # -> ranked cards, or "nothing on record"
"$KHMS_CODE/tools/precheck.sh" sensors            # -> policies/gotchas, or "nothing on record"
echo '{"hook_event_name":"UserPromptSubmit","prompt":"why do the checksums keep failing","session_id":"t1"}' \
  | python3 "$KHMS_CODE/tools/khms_hook.py"       # -> JSON with additionalContext, or nothing
tail -3 "$(python3 "$KHMS_CODE/tools/khms_paths.py" KHMS_INJECT_LOG)"            # -> one line per hook firing, with the reason
```

The audit log is not decoration. A hook that silently stops firing looks exactly like a quiet
day, so every firing records what it searched, what the top hit scored, and why anything above
the bar was *not* injected (`dedup`, `cap`, `threshold`). Kill switch, when the injections are
in your way: `touch "$(python3 "$KHMS_CODE/tools/khms_paths.py" KHMS_HOOKS_OFF_FILE)"` (or `KHMS_HOOKS_OFF=1`).

## Step 8 — Calibrate, and keep the numbers out of the rules

Keep a frozen golden set of the misses that actually happened — the query as it was typed and
the card that should have come back — and score it with `tools/eval/run_eval.py --prod`, which
runs the same path `recall.sh` does. Rows are gated (a regression fails the run) or open debt
(counted and named, not fatal); the shipped rows score against `examples/`, so you can see the
harness work before you have a base of your own.

Start by measuring what the automatic recall is actually worth to you —
[docs/measuring-injection.md](docs/measuring-injection.md) has the tools
(`inject_cited.py`, `khms_debt.py`), the experiment file that switches injection off per event,
and the traps in comparing a before and an after. In the reference deployment that measurement
took two events out of the wiring.

Every number in this system is configuration, not law: injection thresholds, the rate cap, the
dedup TTL, evidence weights, the belief slope. They are listed in one place —
[spec/khms-spec.md](spec/khms-spec.md) §7 — with the values a real deployment converged on.
Change them from measurements in your own `.inject.log` and `.recall.log`, and record the
change as a card. Never encode a calibrated number as a rule, and never store a live tunable's
current *value* in a card body: the card cannot notice when the value moves, so it silently
becomes misinformation. Store where the value lives, not what it is.

## What "done" looks like

- `$KHMS_ROOT` exists with the layout of Step 1; `build_views.py` exits 0.
- `MEMORY.md` is generated and is in your context; you have never edited it by hand.
- The spec and the bootstrap digest exist as cards, tagged `core`.
- Hooks are wired and `.inject.log` is growing.
- Today's journal has MARK lines in it.
- Your first inbox has been produced by a nightly run, reviewed by a human, and approved.
