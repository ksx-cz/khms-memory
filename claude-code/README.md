# Implementing KHMS in Claude Code

Everything here is harness wiring. The memory itself is plain files and works without any of
it; hooks are what turn "the agent could search its memory" into "the agent's memory reaches
the agent whether or not it remembers to search".

## Dependencies

| Requirement | Used by | Notes |
|---|---|---|
| `python3` ≥ 3.8 | everything | no virtualenv needed |
| `PyYAML` | `build_views.py`, `approve_inbox.py`, `precheck.sh`, `weekly_synthesis.sh` | `pip install pyyaml` |
| `flock`, `timeout`, `find`, `date` (GNU coreutils/util-linux) | the pipeline drivers | present on any normal Linux; on macOS install `coreutils` and `flock` or run the pipeline in a container |
| `git` | optional | only to feed the day's commits into the sweep |
| a headless agent CLI (`claude -p …`) | the nightly and weekly drivers | set `KHMS_AGENT` if it is not `claude` |

`khms_hook.py` and `khms_search.py` are **stdlib only** on purpose: the hook runs on every
message and every tool call, so it must start fast and must never fail because an import broke.

## Wire the hooks

Set `KHMS_ROOT` in the harness environment, or initialize with `--register` under
that harness's HOME. Shell exports in a terminal do not configure an already
running harness. Resolve state/log paths with `khms_paths.py`; configuration may
place them outside the base. The example hook commands always execute clone code.


Merge [`settings.hooks.json`](settings.hooks.json) into `~/.claude/settings.json` (user-wide) or
`.claude/settings.json` (per project), replacing `PATH_TO_KHMS` with the absolute path to your code clone
(`KHMS_CODE` in the onboarding examples), never the data base. Hooks do
not expand `$VARS` in the command string, so the path is written out; that one absolute path is
the only one in the whole system.

| Event | Matcher | What the hook does |
|---|---|---|
| `SessionStart` | — | Reports an unreviewed inbox ("review it before other work"; silent when the inbox is clean) and puts the full reply directive into the session ONCE. |
| `UserPromptSubmit` | — | Scores the operator's message against the card base; injects up to two cards plus their correctors, appends the one-line directive pointer, and opens a gated turn for the retrieval-claim gate. |
| `PreToolUse` | `Bash` | Runs `precheck.sh` automatically for a named risky-command class (`rm -rf`, `rsync --delete`, `systemctl`, `git push --force`, `kubectl delete`, `terraform apply`, `DROP TABLE`, …). Otherwise scores the command text. |
| `PreToolUse` | `Edit\|Write` | Scores the file path and the edited text — the moment before a change is when a gotcha card is worth most. |
| `PreToolUse` | *your report tool* | The retrieval-claim gate: DENIES a report whose `base:` line cites card ids that no explicit recall of this turn backs. Set the matcher and `KHMS_REPORT_TOOL` to the tool your agent reports to its principal through. |

**`PostToolUse` and `PostToolUseFailure` are deliberately not wired.** They were, and the
argument for them was good — "an error string is a near-perfect query" — but the measurement
disagreed: **0 of 36** of their injections were ever cited in the next three assistant
messages, against 23.2 % for operator messages. An argument that survives only because nobody
measured it is the thing this whole system exists to prevent. Wire them back if your own
`.inject.log` says something different; the code path is unchanged and the switch is one line
in `tools/khms_experiment.json`. See [docs/measuring-injection.md](../docs/measuring-injection.md).

**The reply directive is a file, not a constant.** `tools/prompts/report_directive.md` holds
the three mandatory lines (`base:` / `verified:` / `if I were wrong:`). It enters the session
once at `SessionStart`; every prompt afterwards carries a single pointer line. As a constant it
was 1813 characters appended to every operator prompt — 46 times on one measured day, the same
paragraph each time.

**The retrieval-claim gate closes the loop on `base:`.** The mandatory line asks the agent to
say what it searched for. Until the gate existed, that line was satisfiable from the hook's own
injection — a check whose passing condition held whether or not a retrieval had happened, which
is exactly the failure shape the base has a card about. The gate denies two things and nothing
else: a substantial report with no `base:` line, and a `base:` line that cites card ids while no
`src=cli` recall since the turn began matches the query it claims to have run. A line that cites
no card, and every honest form ("did not search", "nothing on record"), always passes — the
thing being checked is a CITATION, not the wording. Every verdict, including the allows, goes
into `KHMS_CLAIM_LOG` (default `state/claim_gate.log` in a private base).

The hook returns `{"hookSpecificOutput": {"hookEventName": …, "additionalContext": …}}` — the
text lands in the model's context for that turn. Printing nothing means "no injection", which is
the common case and must stay cheap. Give each hook a `timeout` (15 s is generous; the scan is
~0.1 s at a few thousand cards).

## The budget, and why every part of it exists

Injection is not free — it consumes context, and an agent that gets three irrelevant cards per
message learns to ignore all of them. The parameters live at the top of `khms_hook.py`:

- **Per-event score thresholds.** Higher for operator messages (18) than for tool errors (15):
  an error string is a much sharper query than a sentence.
- **At most two primary cards**, plus the principle one of them supports and whatever corrects
  it, capped at 900 characters total.
- **Correction pointers are exempt from that cap** (at most two per card, 110 characters each).
  A served card always arrives with a one-line `! CORRECTED BY K-xxxxx: …` for every card whose
  `contradicts` / `supersedes` / `refuted_by` points at it, *whatever its status* — the corrected
  card is usually still `active`, because a correction lands before anyone re-statuses anything.
  They are exempt because they are not extra retrieval: they correct content that is being
  injected anyway, and a truncated correction is precisely the failure the pointer exists to
  prevent.
- **Rate cap: 3 injections per 10 minutes**, plus **one bypass ride** per window for a top hit
  that clears the operator-message bar. Without the bypass the cap spends the window
  first-come — and drops the best question of the hour because three routine ones preceded it.
- **Dedup TTL: 12 hours per card per session.** Not "once per session": sessions run for days,
  and a card injected on Monday stayed silent on Thursday for the query it was written for.
- **Query cooldown: 15 minutes** for the same text, so a retry loop does not re-inject.
- **The dense channel, if you have one.** `khms_recall_hybrid.py` fuses a lexical ranking
  with an embedding daemon's (reciprocal rank fusion, k=60); `recall.sh` uses it by default and
  the hook consults it on operator messages only. Two policies sit on top: a RESCUE that adds
  cards the lexical channel did not reach at all, and `DENSE_RESERVED_SLOT` (**off**), which
  gives the dense channel's best unheld card the last primary slot. Read
  [docs/measuring-injection.md](../docs/measuring-injection.md) before switching the second one
  on: it was measured to change about one operator-prompt decision in seven, and to do nothing
  at all on the incident that motivated it. With no daemon installed both are inert and recall
  is exactly the lexical behaviour, said out loud in the first line and in the recall log.
- **The budget is spent BEFORE the card base is loaded.** Every gate that needs no search
  result runs first. Measured: 2324 calls in one day each paid ~172 ms to load a 2467-card base
  and then died on the query cooldown or the rate cap — and each of them also wrote a
  "nothing found" line into the retrieval log, which is how 87 % of the log that is supposed to
  BE the record of retrieval came to be a record of non-retrievals.
- **Length damping** `score × min(1, (85/distinct_tokens)^0.5)`. The score is a sum of per-token
  idf, so without this, long grab-bag cards win queries they have nothing to do with. The pivot
  is the median distinct-token count of your own base — measure it, do not copy 85.

**On a young base, lower the thresholds.** The defaults above assume a few thousand cards: the
score is a sum of inverse-document-frequency weights, so on a base of twenty cards almost
nothing reaches 18 and the floor never fires. Start around 8–10, watch `.inject.log` for a week,
and raise them when injections start feeling like noise rather than help.

**Everything the hook decides goes into `KHMS_INJECT_LOG` (default `state/inject.log` in a private base)**, including the silences, with the
reason a card above the bar was not injected (`dedup`, `cap`, `threshold`). This is not
optional decoration: a hook that quietly stops firing is indistinguishable from a quiet day, and
"the good card was there and stayed silent" has to be diagnosable after the fact.

Kill switch, when injections are in the way: `touch "$(python3 "$KHMS_CODE/tools/khms_paths.py" KHMS_HOOKS_OFF_FILE)"`, or export
`KHMS_HOOKS_OFF=1`. Delete the file to re-enable. The hook also fails open on every exception:
a broken memory system must never break the session it is trying to help.

## Tuning `DOMAIN_RE`

`khms_hook.py` has one regex you must edit for your own work: `DOMAIN_RE`, the list of words
that mean "this text is about something the base might know". Tool events are only searched when
they match it or contain an error. Too broad and every keystroke triggers a scan; too narrow and
the floor never fires. Start from the tags in `memory/views/tags.md` that actually carry cards,
then check `.inject.log` after a day of work: mostly `silent (below threshold)` means the regex
is too broad, no lines at all means it is too narrow.

## Cron

```cron
# Daily: weekly follows a successful night and decides whether seven calendar days have elapsed.
30 3 * * * KHMS_ROOT=/path/to/base /bin/bash -c '/path/to/clone/tools/nightly_distill.sh && /path/to/clone/tools/weekly_synthesis.sh' >> /path/to/base/state/cron.log 2>&1
```

Do not add a second weekly timer: chaining avoids overlap with a long night and
retries a due weekly after the next successful night.

Cron gets a minimal environment: set `KHMS_ROOT` in the crontab line (as above), and set `HOME`
if your agent CLI stores credentials under it. Both drivers are single-instance (`flock`) and
both cap each model stage with `timeout` — an uncapped stage that hangs holds the lock forever,
and then every following night exits "already running" without doing anything, which turns one
lost night into all of them.

**Alert on staleness, not on failure.** A run that dies leaves no proposal file, and "no inbox
today" looks exactly like "a quiet day with nothing to distill". Check the age of
`KHMS_NIGHT_STAMP` and `KHMS_WEEK_STAMP`
(default `state/shared/last-nightly` and `state/shared/last-weekly`) (both written only on success) and say something
when they exceed a day and a week respectively.

## Verifying the wiring

```bash
echo '{"hook_event_name":"SessionStart","session_id":"t"}' | python3 "$KHMS_CODE/tools/khms_hook.py"
echo '{"hook_event_name":"UserPromptSubmit","prompt":"why does the checksum keep failing","session_id":"t"}' \
  | python3 "$KHMS_CODE/tools/khms_hook.py"
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"rm -rf ./build"},"session_id":"t"}' \
  | python3 "$KHMS_CODE/tools/khms_hook.py"
tail "$(python3 "$KHMS_CODE/tools/khms_paths.py" KHMS_INJECT_LOG)"
```

The third one should print the precheck block if any card carries the `filesystem` tag. Nothing
printed is a valid outcome for the first two on an empty base — check the log line, which always
exists.

Dry-run the pipeline without spending a single model token:

```bash
NIGHTLY_DRYRUN=1 "$KHMS_CODE/tools/nightly_distill.sh"
```

It assembles every deterministic input and prints the two stages' INPUTS blocks verbatim. Use it
whenever you change the driver: the most expensive pipeline defect in the reference deployment
was a single missing line in an INPUTS list — a stage was asked to enforce grounding against a
source it had never been handed — and it was invisible by inspection for two nights.

## Cost, and where it goes

Per day, in the reference deployment: the deterministic parts (transcript digest, journal
concatenation, quote verification, view generation, every hook firing) cost **zero model
tokens**. The two nightly model stages dominate; the weekly is one larger run. Watch the ratio
rather than the absolute: when the extract stage costs more than the consolidate stage by a wide
margin, its input is usually oversized rather than its job being hard.
