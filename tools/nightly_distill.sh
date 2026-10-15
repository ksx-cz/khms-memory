#!/usr/bin/env bash
# KHMS nightly driver (#237): preprocess (0 tokens) -> GENERATOR (transcript/git passages -> candidate MARK
# lines, grounded by a tool, committed to memory/ledger/generated/) -> PER-MARK EVALUATOR (journal MARKs and
# generated lines, one packet and one call each, time-sorted) -> QUOTE CHECK + NEAREST + RELATION GATE (0
# tokens) -> publish (carried pre-#237 batches are still consolidated) -> optional triage stage.
# Usage: nightly_distill.sh [--dry-run]   (run once a night by the deployment's scheduler; log: KHMS_NIGHT_LOG)
#
# MODEL CHOICE IS EVIDENCE, NOT FASHION. In the reference deployment the cheapest model tier
# produced structurally perfect cards with largely invented detail and was banned from this stage;
# the strongest tier extracted ~4x more candidates than the mid tier on the same window. The mid
# tier for every nightly stage is affordable only because the recall floor is mechanical, not
# model-dependent: every journal MARK must yield a candidate or an explicit "already carded" note
# (the coverage ledger below), verify_quotes.py greps all grounding, and the smart passes live in
# the weekly synthesis and the morning review. The consolidate stage is handed the SOURCES, and
# verify_quotes.py greps every card's quotes against them before consolidation, so ungrounded
# claims are caught by a tool rather than by whichever model happens to be reading.
# Writes proposals to memory/inbox/. Never touches memory/know/. See spec/khms-spec.md §9.2.
#
# WHERE THINGS ARE (khms_paths.sh; every key below can be set in the environment or the settings file):
#   code     this script's directory -- helpers and prompt templates; nothing is written here
#   data     KHMS_ROOT: journal/, memory/ (inbox, ledger, know)
#   shared   KHMS_SHARED_STATE: KHMS_NIGHTLY_LOCK, KHMS_NIGHT_STAMP, KHMS_SWEEP_FAILED
#   agent    KHMS_NIGHT_LOG (this clone's code-use lock lives in the clone's gitdir, held for a real run)
# SOURCES (each optional; an unconfigured one is said in the log, never replaced by a guess):
#   KHMS_TRANSCRIPTS     space-separated directories of agent session transcripts (*.jsonl), filtered
#                        to paths containing KHMS_PROJECT_SLUG when that is set
#   KHMS_GIT_REPOS       space-separated repositories whose commits since the stamp are an input
#   KHMS_CHAT_FETCH      argv run first to refresh a chat archive (its own config names the archive)
#   KHMS_CHAT_VERIFY     argv run with `--journal <journal dir>`: principal quotes checked against chat
#   journal/             always
# STAGES: KHMS_AGENT (headless CLI, an ABSOLUTE path -- a bare name only with KHMS_AGENT_FROM_PATH=1 --
#   called as `<agent> -p <prompt> --output-format json --model M --allowedTools ...`; each call logs the
#   model that served it, read from that JSON by agent_result.py),
#   KHMS_SWEEP_MODEL, KHMS_MARK_MODEL, KHMS_CONS_MODEL; KHMS_NIGHT_TRIAGE = argv of the optional
#   standing-base triage stage. Prompts: prompts/<stage>.md rendered by khms_prompt.py (private
#   extensions from KHMS_PROMPT_EXT). An agent CLI that is not executable FAILS the night's model
#   stages visibly: nothing is stamped and the next run covers the window again.
set -euo pipefail

CODE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=khms_paths.sh
. "$CODE/khms_paths.sh"
khms_load || exit 3
python3 "$CODE/khms_health.py" || exit 3
khms_require_writable "run the nightly sweep" || exit $?
ROOT=$(khms_get KHMS_ROOT)
AGENT=$(khms_get KHMS_AGENT)
# THE AGENT CLI (#232): an absolute KHMS_AGENT, or a bare name ONLY with KHMS_AGENT_FROM_PATH=1
# (khms_agent_resolve in khms_paths.sh). A refused or missing agent leaves AGENT_BIN empty, and every model
# stage of the night FAILS visibly below (nothing stamped) -- the PATH lookup that served an older model for
# two nights in October 2026 is not a fallback.
khms_agent_resolve || true
AGENT_BIN=$KHMS_AGENT_BIN
# The triage stage judges with the same CLI the night just verified (nightly_triage.sh resolves it the same
# way when it runs on its own).
if [ -n "$AGENT_BIN" ] && [ -z "${KHMS_CLAUDE_BIN:-}${NIGHTLY_CLAUDE_BIN:-}" ]; then export KHMS_CLAUDE_BIN=$AGENT_BIN; fi
SWEEP_MODEL=$(khms_get KHMS_SWEEP_MODEL)
MARK_MODEL=$(khms_get KHMS_MARK_MODEL)
CONS_MODEL=$(khms_get KHMS_CONS_MODEL)
LOCK=$(khms_get KHMS_NIGHTLY_LOCK)
STAMP=$(khms_get KHMS_NIGHT_STAMP)
# NIGHTLY_LOG / NIGHTLY_STAGING / NIGHTLY_DATE stay overridable: a dry run must not write into the
# record the morning review reads, and must assemble its inputs somewhere harmless instead of over
# tonight's real staging files, which are the forensic record of what each stage was handed.
LOG=${NIGHTLY_LOG:-$(khms_get KHMS_NIGHT_LOG)}
mkdir -p "$(dirname "$LOG")" "$(dirname "$LOCK")" "$(dirname "$STAMP")"
STAGING=${NIGHTLY_STAGING:-$ROOT/memory/inbox/.staging}
DATE=${NIGHTLY_DATE:-$(date +%F)}
# NIGHTLY_DRYRUN=1 assembles every deterministic input, then prints the stage INPUTS blocks
# VERBATIM and stops — no model call, no stamp, no inbox written. It exists because a defect that
# lives in one missing line of a prompt's input list is invisible by inspection otherwise.
DRYRUN=${NIGHTLY_DRYRUN:-0}
# ARGUMENTS ARE REFUSED UNLESS KNOWN. When the script read no arguments at all,
# `nightly_distill.sh --dry-run` was silently a REAL run: in the reference deployment a check meant
# as a dry run swept the live base, wrote the stamp, six carry batches and a NOT CARDED section
# into an already-reviewed inbox file. An ignored flag that turns "look" into "do" is a check that
# passes whether or not it did what it says. `--dry-run` means exactly NIGHTLY_DRYRUN=1; anything
# else exits 2 before the first write.
for _arg in "$@"; do
  case "$_arg" in
    --dry-run|--dry) DRYRUN=1 ;;
    -h|--help)
      echo "usage: nightly_distill.sh [--dry-run]   (env: NIGHTLY_DRYRUN=1 is the same as --dry-run)"
      exit 0 ;;
    *)
      echo "nightly_distill.sh: unknown argument '$_arg' — refusing to run (known: --dry-run, --help)" >&2
      exit 2 ;;
  esac
done
# THIS CLONE'S CODE-USE LOCK, held (shared) for the whole run so an update of the clone refuses
# instead of swapping code under a running night. A dry run writes nothing at all — not even the
# lock file — so it takes no lock, exactly as it takes no night lock below.
[ "$DRYRUN" = "1" ] || khms_code_lock || exit $?
# Prepare bounds primary source input per fresh session. The default is ONE readable chunk;
# operators may explicitly group more with SWEEP_GROUP_CHUNKS. Prompt/schema and generated
# output add context, so this is an input-chunk bound, not an absolute model context guarantee.
SWEEP_GROUP_CHUNKS=${SWEEP_GROUP_CHUNKS:-1}
[[ "$SWEEP_GROUP_CHUNKS" =~ ^[1-9][0-9]*$ ]] || { echo "SWEEP_GROUP_CHUNKS must be a positive integer" >&2; exit 2; }

# Wall-clock caps on the two model stages. UNBOUNDED WAS THE 2026-08-15 FAILURE: the sweep call
# went to sleep after writing its last chunk (21 s CPU across 77 min elapsed) and never returned,
# so the run held this flock indefinitely — and a held flock does not just lose ONE night, it makes
# every following night exit "already running" without doing anything. A hang here is therefore
# silent and cumulative, which is exactly the failure class the KB calls a check that passes
# whether or not the thing it guards is healthy. Caps are generous (a genuinely big day measured
# ~75 min of productive sweep on 2026-08-15) but finite, and a timeout kills the stage, releases
# the lock, and leaves a FAILED line in the log where the morning review can see it.
# MEASURED LIMITS (board #209, P209N, 2026-10-06) — ONE RULE for every per-call stage limit:
# limit = 2 x the longest call measured, rounded UP to 5 min. Source: the night's own claude -p
# transcripts priced/timed by tools/tok_account.py, plus logs/nightly.log, nights 2026-09-20..10-06
# (17 nights; table in plans/handoff-P209N.md). tools/test_nightly_stage_limits.py holds the
# measured maxima and fails when a default here drifts from the rule.
#   sweep        17 calls, 7.2-36.9 min (max 2026-09-23)            -> 75m   (was 90m)
#   consolidate  29 batches 2026-10-02..06, 4.8-38.9 min (max 10-05) -> 80m   (was 45m: 1.16x)
#   MARK chunk    3 chunks 2026-10-03, 10.1-14.1 min                 -> 30m   (was 40m, an estimate)
# A call that exceeds its limit is killed by timeout(1) and logged by nightly_retry.sh as
# "FAILED after N/N attempts (rc=124 = TIMEOUT)"; the night then ends "nightly INCOMPLETE".
SWEEP_TIMEOUT=${SWEEP_TIMEOUT:-75m}
# THE SWEEP'S OVERALL DEADLINE (K209H, REV209 F8), minutes after THE SWEEP STAGE'S OWN START (#232; until
# 2026-10-12 after the RUN's start). Until K209H a multi-part sweep had no total bound: K parts x 3 attempts x
# SWEEP_TIMEOUT, on a service with no timeout. No sweep part and no retry attempt STARTS after it, and a
# started attempt is clipped to it (RETRY_DEADLINE_EPOCH in nightly_retry.sh).
# WHY FROM ITS OWN START: since PMARKFIRST the MARK stage runs first and uses its whole 90 min plus the
# overrun of its last chunk (2026-10-12: 03:32:33 -> 05:13:18), so a deadline counted from 03:30 left the
# sweep ~47 of its 150 minutes and it stopped at part 6 of 31 (ISWEEP12). Each stage now owns its budget.
# 180 = THE RULE ABOVE on the worst measured DAY, not on one call: the largest digest (1109 KB, 2026-10-05)
# is ~44 one-chunk parts, and the p95 part on a healthy CLI is 121 s (2026-10-11-2: 51 parts, median 81 s,
# p95 121 s, claude-sonnet-5-5) -> 44 x 121 s = 89 min -> 2 x 89 = 178 -> 180. (The old 150 was sized for
# the single-call sweep, 9–38 min.) A sweep stopped by it FAILS like any other (no stamp, window kept; since
# #173 its finished parts are KEPT and the next run continues the frozen list -- see THE FROZEN PREPARE
# LIST below), and
# every failed sweep is recorded in KHMS_SWEEP_FAILED, which a morning review ping reads aloud
# until a sweep succeeds — a part that always fails on a growing window surfaces instead of
# looping silently.
SWEEP_BUDGET_MIN=${SWEEP_BUDGET_MIN:-180}
SWEEP_FAILED_FILE=$(khms_get KHMS_SWEEP_FAILED)
# PER BATCH since 2026-10-01 (board #209, see "NO CARD CAP" below). Until then it was one
# 100m call for the whole night (2026-08-24: 103 candidates, 70 cards written before a 60m
# timeout killed it MID-WRITE; ~55 cards/h measured that night). Measured on the capped
# nights 2026-09-20..10-01 (nightly.log, `=== nearest ===` -> `=== verify_relations ===`):
# 20 cards took 7.5, 9.3, 9.6, 12, 13, 14, 15, 16, 17, 20, 22 min — ~1.2 cards/min, worst
# 22 min. A batch of 25 is therefore ~10–28 min; 45m is ~1.6x the worst measured rate and
# a timed-out batch is NOT lost (it stays in the carry queue for the next night).
# RE-MEASURED on the first five uncapped nights (P209N): 29 batches took 4.8-38.9 min, and the
# slowest was a 12-candidate batch (2026-10-05, consout-2026-10-04-06) — duration does not follow
# batch size, so the rate-based 45m left 1.16x headroom. 80m = 2 x 38.9 rounded up (rule above).
# Worst case with BATCH_RETRY_MAX=2: a batch started just before CARD_DEADLINE ends 2 x 80m + 30 s later.
# THE NIGHT'S LENGTH SINCE #232 (every stage counts its budget from its own start): MARK <= 90 + one chunk
# (<= 2 x 30m) ~ +151, sweep exactly +180 (clipped) -> +331, consolidation 280 + its last batch 160 -> +772,
# triage <= 120m -> ~+892 min (~18:20) -- only when EVERY stage runs at twice its worst measured length. A
# measured heavy night is ~+2 +105 +89 +139 +59 = ~+394 (~10:05), a typical one ~08:00. The weekly started by
# its Sunday 04:00 timer waits WEEKLY_NIGHT_WAIT_MIN 480 min (until 12:00) and fails after it; the weekly
# started by the deployment AFTER a successful night does not wait at all.
CONSOLIDATE_TIMEOUT=${CONSOLIDATE_TIMEOUT:-80m}
CONSOLIDATE_BATCH=${CONSOLIDATE_BATCH:-25}

# MERGE PRESSURE, added 2026-09-04 (audit review-2026-09-02-khms.md §5, §6 A5).
# The pipeline produced ~50 cards a night at ~100 % approval and merged nothing:
# 17 cards on "SVD48V bus voltage", 14 (+3) on "minimum turning radius", 75
# confirmed contradictions in one 250-pair triage sample against 8 superseded +
# 7 refuted edges in the WHOLE base. Two mechanical stages close that:
#   `nearest_cards.py`  — before the model is asked what a candidate replaces,
#                         it is TOLD which active cards are nearest (0 tokens).
#   `verify_relations.py` — after it answers, the answer is checked and the cap
#                         is enforced regardless of what the model emitted.
#
# NO CARD CAP (board #209, owner 2026-10-01: „no daily card limit at all — simply process
# everything in the queue"). Until that day a 20-card NIGHTLY_MAX_CARDS sent the overflow
# to `## DEFERRED` in that night's inbox file, which nothing ever read again: the
# 2026-09-21 rear-camera checkerboard calibration (journal 17:43/17:46/18:00) was lost
# exactly that way, with no line anywhere saying so. What replaced the cap:
#   * the COVERAGE LEDGER (mark_ledger.py): every tracked journal MARK has an id and
#     stays OPEN until a card covers it or the coordinator dismisses it with a reason;
#     OPEN marks are the queue the MARK stage works from, oldest first, as far as its own
#     time budget (MARK_BUDGET_MIN below) reaches;
#   * the MARK stage: queued marks go to the model in chunks, FIRST in the night (before the
#     sweep), and every finished chunk is committed to the carry queue on its own;
#   * the CARRY QUEUE (memory/ledger/carry/): consolidate runs in batches, every batch
#     file stays in the carry queue until its output is merged into the inbox, so a batch
#     the night did not reach (budget, failure, kill) is what the next night does FIRST.
# The night's TIME bounds every stage (the MARK stage also has a dollar cap, MARK_BUDGET_USD below), and what does
# not fit is carried, never dropped.
LEDGER_TOOL=$CODE/mark_ledger.py
BATCH_TOOL=$CODE/nightly_batch.py
CARRY=$ROOT/memory/ledger/carry
# THE MARK STAGE IS A PER-MARK EVALUATOR (#237, owner 2026-10-13). Until then the stage sent chunks of 60
# bare queue lines to an agent with tools (MARK_CHUNK, MARK_TIMEOUT; 2026-10-13: 17 chunks, 44-103 turns and
# 3.9-8.5 min each) and a consolidate call per chunk merged its candidates. Now permark.py builds a packet
# per mark with 0 tokens (journal context, nearest cards, tonight's candidates, a transcript excerpt) and
# makes ONE call without tools per mark; its candidates are approve-ready and published without a
# consolidate call (`split-run --ready`). PERMARK_TIMEOUT_S bounds one call: a one-shot sonnet call of about
# 10k tokens in took 12.1 s on average (2026-10-13 triage judge, 42 calls in 509 s); 180 s is the design's
# bound, well above twice any measured one-shot call.
PERMARK_TIMEOUT_S=${PERMARK_TIMEOUT_S:-180}
# Wall-clock budgets, EACH COUNTED FROM THE START OF ITS OWN STAGE (#232): no NEW mark chunk later than
# MARK_BUDGET_MIN after the MARK stage starts, no NEW sweep part later than SWEEP_BUDGET_MIN after the sweep
# starts, no NEW consolidate batch later than CARD_BUDGET_MIN after the consolidation starts. Until
# 2026-10-12 the sweep (+150) and the consolidation (+225) counted from the run's start, so a long MARK stage
# ate their time (ISWEEP12: the sweep got ~47 min). A started chunk/part/batch is bounded by its own timeout.
# Triage (31–59 min measured, P209N) runs after. The night's worst case is above (CONSOLIDATE_TIMEOUT).
# CARD_BUDGET_MIN: THE RULE ABOVE on the longest measured consolidation STAGE: 139 min (P209N, 6-9 batches
# a night, 2026-10-02..06, 75-139 min; for scale 2026-10-11-2: 26 batches in 120 min, 2026-10-12: 7 in 48)
# -> 2 x 139 = 278 -> 280. (It was 180, then 225 from the run's start since P209M, i.e. what was left after
# the stages before it.) A night that does not finish its batches carries them; the carry queue is the next
# night's consolidation, done oldest first.
# MARK_BUDGET_MIN 150 (owner 2026-10-13 08:21 on #237: stay sequential, no parallel lanes, 150 min): the
# evaluator handles marks one after another, so each sees every decision made before it (the in-night
# duplicate rule) with no locking; 150 min at 12-20 s per mark is ~450-750 marks a night.
MARK_BUDGET_MIN=${MARK_BUDGET_MIN:-150}
# MARK_BUDGET_USD 100 (#243, owner 2026-10-15 08:54: "set the night's cap to $100"): the MARK stage's DOLLAR cap per
# night, next to its time budget. The night of 2026-10-14 ran the stage for 300 min and spent $63.10 in it (the
# CLI's total_cost_usd), and the account hit its limit soon after. permark.py starts no new mark once what the
# stage charged + the dearest mark so far would pass it; the rest stays OPEN for the next night exactly as at the
# time budget, and a cap stop is not a failure (rc 0, the night's verdict unchanged). 0 = no dollar cap.
# Read like the triage's judge bound (nightly_triage.sh): the environment, else the setting KHMS_MARK_MAX_USD
# (the deployment's settings file), else 100.
MARK_BUDGET_USD=${MARK_BUDGET_USD:-$(khms_get KHMS_MARK_MAX_USD)}
MARK_BUDGET_USD=${MARK_BUDGET_USD:-100}
CARD_BUDGET_MIN=${CARD_BUDGET_MIN:-280}
# Attempts per batch/chunk (the sweep keeps nightly_retry.sh's default 3): a batch that
# fails twice is carried, and the next one is tried rather than spending the night on it.
BATCH_RETRY_MAX=${BATCH_RETRY_MAX:-2}
# THE MARK STAGE RUNS FIRST (PMARKFIRST, #209, Diblin's specification 2026-10-11 10:11). Until then
# the MARK stage ran AFTER the sweep, inside the sweep's success branch, and stopped as soon as
# MARK_PENDING_MAX (225) candidates already waited for consolidation (tonight's sweep + carry).
# MEASURED: P209N, nights 10-02..10-06 — the sweep alone yielded 52-157 candidates and the stage
# ran on 1 of 5 nights while the OPEN queue grew 2740 -> 4387; 2026-10-11 manual night — "648
# candidate(s) already wait for consolidation (>= 225) — 42 chunk(s) left OPEN", no chunk at all,
# 2537 OPEN marks untouched; and a failed sweep skipped the stage entirely. Policy: no daily
# limit, everything in the queue is processed. So now: the stage is the night's FIRST model stage,
# independent of the sweep's outcome and of how many candidates wait; it has its own budget
# (MARK_BUDGET_MIN from its own start) and no candidate-count gate; every finished chunk is
# committed, on its own, to the carry queue as a run directory WITHOUT a WINDOW (it is not a
# window of the sweep, so it never moves the sweep's stamp). The sweep later rebuilds its own file
# from its parts — a MARK result merged only into that file could be lost, so it never goes there.

mkdir -p "$STAGING"
# The stage prompts: prompts/<name>.md with their {{...}} fields and private extension slots filled
# (khms_prompt.py), rendered ONCE before anything is written. A stage without its instructions
# produces nothing a review could trust, so a prompt that cannot be rendered stops the night here,
# loudly and unstamped.
render_prompt() { python3 "$CODE/khms_prompt.py" "$1" 2>>"$LOG"; }
if ! SWEEP_BASE=$(render_prompt nightly-generate) || ! MARKS_BASE=$(render_prompt nightly-marks) \
   || ! CONSOLIDATE_BASE=$(render_prompt nightly-consolidate); then
  echo "$(date -Is) nightly FAILED: a stage prompt could not be rendered (khms_prompt.py, see above) — nothing run, nothing stamped" >> "$LOG"
  exit 1
fi
read -r -a TRIAGE_CMD <<< "$(khms_get KHMS_NIGHT_TRIAGE)"
# THE LOCK AND THE NIGHT'S START (K209H, REV209 F3). Until 2026-10-02 this was `exec 9>"$LOCK"`,
# which TRUNCATES the file before `flock -n` decides — so a refused second launch (a manual run, a
# timer catch-up) re-stamped the lock file's mtime, and the SessionStart hook, which read the night's
# start from that mtime, offered the running night's units as "ready for review". Now the file is
# opened for APPEND (no truncation, no mtime change by a launch that does not win), and the night's
# start is the line the HOLDER writes after winning: `nightly <iso start> pid=<pid> nk=<key>`
# (nightly_state.py reads it — the hook and a review ping alike).
# NIGHTLY_LOCK_WAIT_S: the weekly synthesis holds this lock for the few seconds of its inbox
# snapshot (so it never reads a half-published night); a night that starts inside those seconds
# waits for it instead of exiting "already running" and losing the night. 0 = refuse at once.
NIGHTLY_LOCK_WAIT_S=${NIGHTLY_LOCK_WAIT_S:-120}
if [ "$DRYRUN" != "1" ]; then
  exec 9>>"$LOCK"
  if [ "$NIGHTLY_LOCK_WAIT_S" -gt 0 ] 2>/dev/null; then _lk=(-w "$NIGHTLY_LOCK_WAIT_S"); else _lk=(-n); fi
  flock "${_lk[@]}" 9 || { echo "$(date -Is) already running ($(head -c 200 "$LOCK" 2>/dev/null | head -1)), exit" >> "$LOG"; exit 0; }
  # A dry run takes no lock: it must never be able to make a real nightly exit
  # "already running", which is the failure mode a held flock causes for EVERY
  # following night, not just the one that held it.
fi

# Stamp the START, not the finish (see the write at the end of this script): the window this run
# covers ends when its inputs were collected, so recording the completion time silently drops
# everything that happened DURING the run — hours, on a slow night — from every future sweep.
RUN_START=$(date -Is)
RUN_START_EPOCH=$(date +%s)
# Only the holder writes the lock file (see THE LOCK above); the night key follows once claimed.
[ "$DRYRUN" = "1" ] || printf 'nightly %s pid=%s\n' "$RUN_START" "$$" > "$LOCK"
# MARK_DEADLINE, SWEEP_DEADLINE and CARD_DEADLINE are set when their stage STARTS, from that moment (#232).
SWEEP_BUDGET_SEC=${SWEEP_BUDGET_S:-$((SWEEP_BUDGET_MIN * 60))}   # _S: test seams
CARD_BUDGET_SEC=${CARD_BUDGET_S:-$((CARD_BUDGET_MIN * 60))}
# RUN IDENTITY (review P1-4, 2026-10-01). Everything this run writes — staging inputs, carry
# batches, published inbox units — is named by its NIGHT KEY, not by DATE: the first run of a
# date gets NK=DATE (the historic names), a second run of the same date gets DATE-2, then
# DATE-3, claimed with an atomic mkdir. Until then a second run on the same date reopened
# `<DATE>-cons-NN.md` with "w" and overwrote a batch the first run had left in the carry queue,
# and overwrote the staging inputs that batch is grounded against. A dry run claims nothing and
# uses DATE-dry, so it can never clobber a real run's files either.
if [ "$DRYRUN" = "1" ]; then
  NK="$DATE-dry"
else
  NK="$DATE"; _n=1
  # a key is free only when no run claimed it AND nothing of an older (pre-claim) run uses it
  until ! compgen -G "$CARRY/$NK-cons-*.md" >/dev/null && ! compgen -G "$ROOT/memory/inbox/$NK-b[0-9]*.md" >/dev/null \
        && mkdir "$STAGING/.run-$NK" 2>/dev/null; do
    _n=$((_n + 1)); NK="$DATE-$_n"
    [ "$_n" -lt 100 ] || { echo "$(date -Is) cannot claim a night key for $DATE (100 tried)" >> "$LOG"; exit 1; }
  done
  echo "$RUN_START pid=$$" > "$STAGING/.run-$NK/owner"
  printf 'nightly %s pid=%s nk=%s\n' "$RUN_START" "$$" "$NK" > "$LOCK"
fi
echo "$RUN_START nightly start (night key $NK)" >> "$LOG"
# THE AGENT, ONCE PER REAL RUN (#232): which file runs the stages and what it says its version is. A model
# alias (`sonnet`) maps to a different model in another CLI version, so the version is half of the answer;
# the other half is the `served-by:` line every model call writes (agent_result.py, from the CLI's JSON).
if [ -n "$AGENT_BIN" ]; then
  _ver=$(timeout 30 "$AGENT_BIN" --version 2>&1 | head -1 || true)
  echo "$(date -Is) agent: $AGENT_BIN ($KHMS_AGENT_WHY) version: ${_ver:-UNKNOWN (no answer to --version)}" >> "$LOG"
else
  echo "$(date -Is) $KHMS_AGENT_WHY" >> "$LOG"
fi
export RETRY_RESULT_TOOL=$CODE/agent_result.py
export RETRY_SERVED_FILE=$STAGING/.served-$NK
rm -f "$RETRY_SERVED_FILE"
# THE USAGE-LIMIT FLAG (PDRAIN, #209; P209N A-4): retry_claude_call writes it on the first
# "You've hit your … limit" answer and then refuses every later model call of this run (rc 75), and
# the stage loops below stop at it with ONE line instead of attempting every chunk and batch.
export RETRY_LIMIT_FLAG=$STAGING/.usage-limit-$NK
rm -f "$RETRY_LIMIT_FLAG"
limit_stop() {   # limit_stop <what is left> -> 0 (and one log line) when the usage limit was hit
  [ -s "$RETRY_LIMIT_FLAG" ] || return 1
  echo "$(date -Is) USAGE LIMIT — $1 not started ($(head -c 200 "$RETRY_LIMIT_FLAG")); they stay queued/carried for the next night" >> "$LOG"
  case " $FAILED_STAGES " in *" usage-limit "*) ;; *) FAILED_STAGES="$FAILED_STAGES usage-limit" ;; esac
  return 0
}
# THE KNOW-HOW TEST, MADE VISIBLE (P209M, knowledge, not progress records).
# The MARK and consolidate prompts drop a pure status/progress item with the reason `not know-how`;
# this line counts those verdicts per output, so the log shows the test working (or never firing).
knowhow_line() {   # knowhow_line <stage tag> <output file> -> one log line
  local ids cands
  ids=$(grep -cE '^- M-[0-9a-f]{8} :: not know-how' "$2" 2>/dev/null || true)
  cands=$(grep -oiE 'not know-how: *[0-9]+' "$2" 2>/dev/null | grep -oE '[0-9]+' | awk '{s+=$1} END {print s+0}' || true)   # pipefail: no match is not an error
  echo "$(date -Is) know-how ($1): ${ids:-0} MARK id(s) and ${cands:-0} candidate(s) dropped as not know-how" >> "$LOG"
}
# The ledger tool, never fatal: a ledger bug must not cost the night's cards (K-90490), but
# it must say so — a silent ledger failure would read as "nothing OPEN".
ledger() {
  if [ ! -f "$LEDGER_TOOL" ]; then
    echo "$(date -Is) mark ledger MISSING ($LEDGER_TOOL) — coverage not tracked tonight" >> "$LOG"
    return 1
  fi
  python3 "$LEDGER_TOOL" --root "$ROOT" "$@"
}
SINCE=$(cat "$STAMP" 2>/dev/null || echo "1970-01-01")
# run_triage_stage: the optional standing-base triage (KHMS_NIGHT_TRIAGE), non-fatal, said in the log
# either way. Called on EVERY real night that reaches its end -- the quiet one included (see 4b below).
# It gets this run's NIGHT KEY (NIGHTLY_NK, #237): its morning file and sweep JSON are named by it like
# every other artefact of the night, so a second night of a date neither overwrites the first one's
# triage nor re-judges the pairs it judged.
run_triage_stage() {
  if [ ${#TRIAGE_CMD[@]} -gt 0 ]; then
    NIGHTLY_NK="$NK" "${TRIAGE_CMD[@]}" >> "$LOG" 2>&1 \
      || echo "$(date -Is) triage stage FAILED (non-fatal) — cards proposal is unaffected; the pairs it did not judge stay eligible for the next night's full sweep" >> "$LOG"
  else
    echo "$(date -Is) triage stage: OFF (KHMS_NIGHT_TRIAGE not configured)" >> "$LOG"
  fi
  return 0
}
# A run directory in the carry queue holds the WINDOW its candidates came from (written inside
# the same atomic rename that commits the split, review P1-4): a run killed after that commit
# but before it could write the stamp must not have its window swept a second time.
for _w in "$CARRY"/*/WINDOW; do
  [ -f "$_w" ] || continue
  _wv=$(cat "$_w" 2>/dev/null || true)
  if [ -n "$_wv" ] && [ "$(date -d "$_wv" +%s 2>/dev/null || echo 0)" -gt "$(date -d "$SINCE" +%s 2>/dev/null || echo 0)" ]; then
    echo "$(date -Is) window: stamp $SINCE is older than committed run window $_wv (${_w%/WINDOW}) — sweeping from the window" >> "$LOG"
    SINCE=$_wv
    [ "$DRYRUN" = "1" ] || echo "$SINCE" > "$STAMP"
  fi
done

# THE GENERATED LINES (#237 step 2) carry their window the same way: memory/ledger/generated/<key>.md is
# committed by one rename with a `WINDOW:` line, and a run killed after that commit but before the stamp
# must not generate the same window twice.
GENERATED=$ROOT/memory/ledger/generated
_gw=$(python3 "$CODE/generated_marks.py" window "$GENERATED" 2>/dev/null || true)
if [ -n "$_gw" ] && [ "$(date -d "$_gw" +%s 2>/dev/null || echo 0)" -gt "$(date -d "$SINCE" +%s 2>/dev/null || echo 0)" ]; then
  echo "$(date -Is) window: stamp $SINCE is older than the committed generated window $_gw — generating from it" >> "$LOG"
  SINCE=$_gw
  [ "$DRYRUN" = "1" ] || echo "$SINCE" > "$STAMP"
fi

# THE FROZEN PREPARE LIST (#173; first night on the shared code, 2026-10-11). A window whose sweep has
# more parts than one night's SWEEP_BUDGET_MIN covers (that night: 42 one-chunk parts at 6-7 min, cut by
# the 150-min deadline at part 23) used to be thrown away whole: nothing kept, nothing stamped, and the
# next window two nights long -- the same failure every night on a growing window. Now the run that
# collects a window FREEZES its list of prepare inputs before the first part starts:
#   $STAGING/.prepare-<SK>/FROZEN   since=, end= (the window), group_chunks=, parts=, index_sha256=
#                                   (sha256 of nightly-<SK>-prepare-chunks.txt, the ordered input list),
#                                   and one `input=<sha256><TAB><path>` line per frozen input: every primary
#                                   chunk of that list and the window's own sources (digest, git log, their
#                                   chunk indices, the window's journals) -- their CONTENT, not only their
#                                   names (review FIX 2026-10-11: a list of paths let a chunk whose bytes
#                                   changed at the same path resume against an old part output);
#   $STAGING/.prepare-<SK>/done-NNN one COMPLETION RECORD per finished part (written by rename after the
#                                   part's call and its reassembly): group_sha256 of its input list and
#                                   output_sha256 of nightly-<SK>-sweep-gNN.md ("absent" when none).
# SK is the night key of the run that collected the window; its staging inputs (digest, git log, chunks,
# part lists) stay where that run wrote them. The NEXT run finds the list here and, instead of collecting
# a new window, runs only the parts without a valid record -- a part killed mid-call has none and is
# redone from nothing, a finished part is never run twice -- and builds the sweep file from the part
# outputs in order. The stamp moves to the frozen END only when every part is complete and the window is
# committed to the carry queue (step 2e), so no input of the window is ever skipped; the run after that
# collects the next window from there. A list the stamp already covers is removed; a damaged one -- an input
# missing, or an input whose content sha256 differs from the frozen one -- is set aside WHOLE and its window
# collected again from the (unmoved) stamp, before any completion record is consumed. Redoing only the part
# whose chunk changed is not done on purpose: the changed bytes no longer match the digest the chunk was cut
# from, nor the grounding the finished parts quoted, so no single part can be shown to be the window's;
# re-collecting from the stamp rebuilds the window from its real sources, skips nothing and reuses nothing.
# The ledger grounding (nightly-<SK>-journals-ledger.md) is NOT frozen: a resume refreshes it on purpose.
SK=$NK
WINDOW_END=$RUN_START
RESUME=""
sha_of() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
frozen_get() { sed -n "s/^$2=//p" "$1/FROZEN" 2>/dev/null | head -1; }
frozen_inputs() {  # frozen_inputs <key> -> every input path whose CONTENT the frozen record vouches for
  local f
  for f in digest.txt git.txt digest-chunks.txt git-chunks.txt journals.md; do echo "$STAGING/nightly-$1-$f"; done
  cut -f2 "$STAGING/nightly-$1-prepare-chunks.txt"
}
frozen_input_sha() {  # frozen_input_sha <dir> <path> -> the content sha256 frozen for that path, or nothing
  awk -F'\t' -v p="$2" '$1 ~ /^input=/ && $2 == p { sub(/^input=/, "", $1); print $1; exit }' "$1/FROZEN"
}
frozen_check() {   # frozen_check <dir> <key> -> 0, or 1 with the reason on stdout
  local d="$1" k="$2" idx="$STAGING/nightly-$2-prepare-chunks.txt" src chunk f
  local -a g
  [ -s "$idx" ] || { echo "its input list $idx is missing"; return 1; }
  [ "$(sha_of "$idx")" = "$(frozen_get "$d" index_sha256)" ] || { echo "its input list $idx changed"; return 1; }
  shopt -s nullglob; g=("$STAGING/nightly-$k-prepare-group"[0-9]*.txt); shopt -u nullglob
  [ "${#g[@]}" = "$(frozen_get "$d" parts)" ] || { echo "${#g[@]} part list(s) on disk, $(frozen_get "$d" parts) frozen"; return 1; }
  cat "${g[@]}" | cmp -s - "$idx" || { echo "its part lists differ from its input list"; return 1; }
  while IFS=$'\t' read -r src chunk; do
    [ -f "$chunk" ] || { echo "input chunk $chunk ($src) is missing"; return 1; }
  done < "$idx"
  for f in digest.txt git.txt digest-chunks.txt git-chunks.txt journals.md; do
    [ -e "$STAGING/nightly-$k-$f" ] || { echo "grounding input nightly-$k-$f is missing"; return 1; }
  done
  while IFS= read -r f; do
    src=$(frozen_input_sha "$d" "$f")
    [ -n "$src" ] || { echo "input $f has no content sha256 in the frozen record"; return 1; }
    [ "$(sha_of "$f")" = "$src" ] || { echo "input $f changed (its content sha256 is not the frozen one)"; return 1; }
  done < <(frozen_inputs "$k")
  return 0
}
shopt -s nullglob
for _pd in "$STAGING"/.prepare-*/; do
  _pd=${_pd%/}; _fk=${_pd##*/.prepare-}
  case "$_fk" in *.damaged-*) continue ;; esac
  if [ ! -f "$_pd/FROZEN" ]; then   # a freeze that died before its rename: nothing was kept under it
    [ "$DRYRUN" = "1" ] || rm -rf "$_pd"
    continue
  fi
  _fend=$(frozen_get "$_pd" end)
  if [ "$(date -d "$_fend" +%s 2>/dev/null || echo 0)" -le "$(date -d "$SINCE" +%s 2>/dev/null || echo 0)" ]; then
    echo "$(date -Is) prepare: frozen list $_fk (window end $_fend) is already covered by the stamp $SINCE — removed" >> "$LOG"
    [ "$DRYRUN" = "1" ] || rm -rf "$_pd"
    continue
  fi
  if ! _why=$(frozen_check "$_pd" "$_fk"); then
    _aside="$_pd.damaged-$(date +%Y%m%dT%H%M%S)"
    echo "$(date -Is) prepare: frozen list $_fk is DAMAGED ($_why) — set aside as $_aside; its window is collected again from the stamp $SINCE, so nothing is skipped (its finished parts are swept again)" >> "$LOG"
    [ "$DRYRUN" = "1" ] || mv "$_pd" "$_aside"
    continue
  fi
  RESUME=$_fk; SK=$_fk; WINDOW_END=$_fend
  SINCE=$(frozen_get "$_pd" since)
  SWEEP_GROUP_CHUNKS=$(frozen_get "$_pd" group_chunks)
  _fdone=("$_pd"/done-[0-9]*)
  echo "$(date -Is) prepare: resuming the frozen prepare list $SK (window $SINCE -> $WINDOW_END, $(frozen_get "$_pd" parts) part(s), ${#_fdone[@]} with a completion record) — no new window is collected tonight; the stamp moves to $WINDOW_END once the list is complete" >> "$LOG"
  break
done
shopt -u nullglob

# 0) THE RECORD THE JOURNALS QUOTE FROM. A journal that puts words in the principal's mouth can
# only be caught against the chat history, and in the reference deployment the local copy of it
# was once 39 days stale — a month was ungroundable, and the first diagnosis of a mis-quote
# concluded "never written" about a sentence that had been written. The tool to refresh it had
# existed the whole time; what was missing was that anything RAN it. So the refresh belongs to the
# pipeline that consumes journals. Both steps are optional, configured argv (KHMS_CHAT_FETCH,
# KHMS_CHAT_VERIFY; never shell text), and neither may abort the night (set -e above): a failed
# fetch must leave a FAILED line where the morning review sees it, not skip the sweep.
# NIGHTLY_SKIP_DISCORD=1 / NIGHTLY_SKIP_CHAT=1 skip the whole block (network fetch + verifier over
# live chat history) — for a test that must not touch a real chat archive or the network.
read -r -a CHAT_FETCH <<< "$(khms_get KHMS_CHAT_FETCH)"
read -r -a CHAT_VERIFY <<< "$(khms_get KHMS_CHAT_VERIFY)"
if [ "${NIGHTLY_SKIP_DISCORD:-0}" = "1" ] || [ "${NIGHTLY_SKIP_CHAT:-0}" = "1" ]; then
  echo "$(date -Is) chat archive refresh SKIPPED (NIGHTLY_SKIP_CHAT=1)" >> "$LOG"
else
if [ ${#CHAT_FETCH[@]} -eq 0 ]; then
  echo "$(date -Is) source chat: OFF (KHMS_CHAT_FETCH not configured)" >> "$LOG"
elif timeout 300 "${CHAT_FETCH[@]}" >> "$LOG" 2>&1; then
  echo "$(date -Is) chat archive refreshed" >> "$LOG"
else
  echo "$(date -Is) FAILED chat archive fetch (rc=$?) — quote grounding would run on a stale file" >> "$LOG"
fi
# The verifier is expected to refuse an archive older than it trusts by itself, so a silently
# failed fetch turns into a loud ERROR here rather than into a clean-looking scan of nothing.
QOUT=$STAGING/nightly-$NK-owner-quotes.txt
if [ ${#CHAT_VERIFY[@]} -eq 0 ]; then
  echo "$(date -Is) principal-quote check: OFF (KHMS_CHAT_VERIFY not configured)" >> "$LOG"
elif timeout 300 "${CHAT_VERIFY[@]}" --journal "$ROOT/journal" > "$QOUT" 2>&1; then
  echo "$(date -Is) owner quotes OK: $(tail -1 "$QOUT")" >> "$LOG"
else
  echo "$(date -Is) OWNER-QUOTE FINDINGS (see $QOUT):" >> "$LOG"
  grep -E '^(UNGROUNDED|ERROR)' "$QOUT" >> "$LOG" || true
fi
fi

# 1) deterministic inputs -- collected for a NEW window only; a resumed frozen list has its own (SK)
DIGEST=$STAGING/nightly-$SK-digest.txt
JSRC=$STAGING/nightly-$SK-journals.md
GITLOG=$STAGING/nightly-$SK-git.txt
JOURNAL=$ROOT/journal/$DATE.md
SRC_FAILED=$STAGING/nightly-$NK-source-failed.txt
: > "$SRC_FAILED"
read -r -a GIT_REPOS <<< "$(khms_get KHMS_GIT_REPOS)"
if [ -z "$RESUME" ]; then
: > "$DIGEST"
# SOURCE: agent session transcripts (KHMS_TRANSCRIPTS, space-separated directories; only paths
# containing KHMS_PROJECT_SLUG when it is set). Unconfigured = no transcript source tonight, said in
# the log; the night never falls back to some other workspace's transcripts.
#
# OFF IS NOT FAILED. A source that is not configured is OFF and says so. A CONFIGURED source that cannot be
# read tonight (a directory that is not there, a preprocessor that fails, a repository git cannot read) is
# a FAILED source: its part of the window was not collected, so nothing may be extracted from this window
# and nothing may be stamped -- otherwise the next night starts after data no night has read (#173 review
# F2: an unreadable git repository advanced the stamp and its commits were never read again). Each failure
# is one line in $SRC_FAILED; a failed source makes the sweep FAIL below (window kept, stamp untouched),
# while the queue and the carry from earlier, fully collected windows still proceed.
read -r -a TRANSCRIPT_DIRS <<< "$(khms_get KHMS_TRANSCRIPTS)"
SLUG=$(khms_get KHMS_PROJECT_SLUG)
if [ ${#TRANSCRIPT_DIRS[@]} -eq 0 ]; then
  echo "$(date -Is) source transcripts: OFF (KHMS_TRANSCRIPTS not configured)" >> "$LOG"
fi
for SRC in "${TRANSCRIPT_DIRS[@]}"; do
  if [ ! -d "$SRC" ]; then
    echo "transcripts: $SRC is not a directory" >> "$SRC_FAILED"
    continue
  fi
  find "$SRC" -path "*${SLUG}*" -name "*.jsonl" -newermt "$SINCE" -type f \
    || echo "transcripts: listing $SRC failed" >> "$SRC_FAILED"
done | sort -u | while read -r f; do
  # --since gives a message-level cut, not just file mtime — a long-running session's
  # already-swept prefix is skipped on the next night
  python3 "$CODE/preprocess_transcripts.py" --since "$SINCE" "$f" >> "$DIGEST" \
    || echo "transcripts: preprocessing $f failed" >> "$SRC_FAILED"
done
# Journal SOURCE = every journal file touched since the last successful run, concatenated.
# Passing journal/$DATE.md as the source was an off-by-one: at 03:30 the day being
# distilled is YESTERDAY, whose journal is complete — while $DATE.md is empty or missing.
# Every sweep 07-25..07-29 therefore ran with a nascent journal or none, and the MARK
# anchors never fired on time. $JOURNAL stays as the day-summary APPEND TARGET only.
: > "$JSRC"
{ find "$ROOT/journal" -name '2*.md' -newermt "$SINCE" -type f \
    || echo "journal: listing $ROOT/journal failed" >> "$SRC_FAILED"; } | sort | while read -r j; do
  printf '\n===== %s =====\n' "${j##*/}" >> "$JSRC"
  cat "$j" >> "$JSRC" || echo "journal: reading $j failed" >> "$SRC_FAILED"
done
# SOURCE: the commits of KHMS_GIT_REPOS since the stamp (`git log --since --stat`). One repository is
# the log exactly as `git log` prints it; several are each headed `===== <repo> =====`.
: > "$GITLOG"
if [ ${#GIT_REPOS[@]} -eq 0 ]; then
  echo "$(date -Is) source git: OFF (KHMS_GIT_REPOS not configured)" >> "$LOG"
fi
# A base that was never stamped has SINCE=1970-01-01, which `git log --since` reads as "nothing"
# (measured: no commit at all) — so a first night reads the whole history instead.
_gsince=(--since="$SINCE"); [ "$SINCE" = "1970-01-01" ] && _gsince=()
for _repo in "${GIT_REPOS[@]}"; do
  if [ ${#GIT_REPOS[@]} -gt 1 ]; then printf '===== %s =====\n' "$_repo" >> "$GITLOG"; fi
  git -C "$_repo" log "${_gsince[@]}" --stat --date=iso >> "$GITLOG" 2>/dev/null \
    || echo "git: $_repo cannot be read" >> "$SRC_FAILED"
done
# Prepare sees unmarked source records only. Keep unmarked passages before a MARK,
# but suppress its continuations through a blank separator, digest record or git commit.
# This is explicit source filtering, not semantic dedup of paraphrases.
python3 - "$DIGEST" "$GITLOG" <<'PYUNMARKED' || echo "prepare: unmarked source filtering failed" >> "$SRC_FAILED"
import pathlib, re, sys
for filename in sys.argv[1:]:
    path = pathlib.Path(filename)
    kept, marked = [], False
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if re.match(r"^commit [0-9a-f]+|^\[.*?\] (?:USER|ASSISTANT|TOOL):|^===== SESSION", line):
            marked = False
        if re.search(r"MARK\[[^]\s]+\]", line):
            marked = True
        if marked and not line.strip():
            marked = False
        if not marked:
            kept.append(line)
    path.write_text("".join(kept), encoding="utf-8")
PYUNMARKED
fi   # a new window's inputs
SOURCE_FAILED=""
if [ -s "$SRC_FAILED" ]; then
  SOURCE_FAILED=$(grep -c . "$SRC_FAILED")
  while read -r _l; do echo "$(date -Is) source FAILED: $_l" >> "$LOG"; done < "$SRC_FAILED"
fi
GITLOG_WHAT="\`git log --since --stat\` over ${GIT_REPOS[*]:-no repository}"

# 1a) THE COVERAGE LEDGER (board #209). Derive every tracked MARK's state (journal + cards +
# unreviewed inbox + carry + the coordinator's events) and write tonight's QUEUE: the OPEN
# marks no pending proposal holds yet, oldest first. A dry run writes no ledger state.
QUEUE=$STAGING/nightly-$NK-marks.md
: > "$QUEUE"
if [ "$DRYRUN" = "1" ]; then
  ledger sync --dry >> "$LOG" 2>&1 || echo "$(date -Is) mark ledger sync FAILED (dry)" >> "$LOG"
else
  ledger sync >> "$LOG" 2>&1 || echo "$(date -Is) mark ledger sync FAILED (non-fatal) — OPEN counts in the morning are stale" >> "$LOG"
fi
ledger queue --out "$QUEUE" >> "$LOG" 2>&1 \
  || { echo "$(date -Is) mark ledger queue FAILED (non-fatal) — no MARK stage tonight" >> "$LOG"; : > "$QUEUE"; }
QUEUE_N=$(grep -cE '^- [MG]-' "$QUEUE" 2>/dev/null) || QUEUE_N=0
[ "$DRYRUN" = "1" ] || mkdir -p "$CARRY" 2>/dev/null || true
carry_batches() {   # never fails: an empty carry queue is a normal night, not an error
  # Legacy flat batches (<night>-cons-NN.md, nights before KHMSFIX) and run directories
  # (<night key>/notcarded.md, <night key>/cons-NN.md, committed by split-run). A durable
  # consolidate output (*.out.md) is part of its batch, never a batch of its own.
  local f
  {
    for f in "$CARRY"/*-cons-[0-9]*.md "$CARRY"/[0-9]*/notcarded.md "$CARRY"/[0-9]*/cons-[0-9]*.md; do
      case "$f" in *.out.md) continue ;; esac
      if [ -f "$f" ]; then echo "$f"; fi
    done
  } | sort
  return 0
}
# batch_ident <batch file> -> BID (stable batch id), BD (its night key), BNN, KIND, UNIT (the
# inbox unit it is published as) and DUR (its durable consolidate output in the carry queue).
batch_ident() {
  local b="$1" bn="${1##*/}" dir="${1%/*}"
  DUR="${b%.md}.out.md"
  if [ "$dir" = "$CARRY" ]; then
    BD=${bn%%-cons-*}; BNN=${bn##*-cons-}; BNN=${BNN%.md}; KIND=cons; BID="$BD-cons-$BNN"
  else
    BD=${dir##*/}
    if [ "$bn" = "notcarded.md" ]; then
      BNN=00; KIND=notcarded; BID="$BD/notcarded"
    else
      BNN=${bn#cons-}; BNN=${BNN%.md}; KIND=cons; BID="$BD/cons-$BNN"
    fi
  fi
  UNIT="$ROOT/memory/inbox/$BD-b$BNN.md"
}
# Test seam for the kill tests (test_khmsfix.py): NIGHTLY_FAULT=<point> SIGKILLs the run at
# that point, exactly as an OOM or a reboot would. Unset in production.
fault_point() {
  if [ "${NIGHTLY_FAULT:-}" = "$1" ]; then
    echo "$(date -Is) FAULT INJECTED at $1 — SIGKILL" >> "$LOG"
    kill -9 $$
  fi
  return 0
}
if [ "$DRYRUN" != "1" ]; then
  for _t in "$CARRY"/.split-*.tmp; do
    [ -d "$_t" ] || continue
    echo "$(date -Is) carry: discarding uncommitted split $_t (its run died before the commit; its window was never stamped and is swept again)" >> "$LOG"
    rm -rf "$_t"
  done
fi
CARRY_N=$(carry_batches | wc -l)
# Journal source for GROUNDING: the window's journals plus every journal from the ledger floor
# on — a queued mark from an older day is quoted from its own file, and a verifier handed only
# tonight's window would call every such quote ABSENT and strip it.
# A resumed list is grounded against the journals as they are NOW (they only grow), in the frozen run's
# own file, which is where a batch of its carry directory is grounded later (consolidate_inputs).
JVERIFY=$STAGING/nightly-$NK-journals-ledger.md
if [ -n "$RESUME" ] && [ "$DRYRUN" != "1" ]; then JVERIFY=$STAGING/nightly-$SK-journals-ledger.md; fi
if [ -n "$RESUME" ]; then : > "$JVERIFY"; else cp "$JSRC" "$JVERIFY"; fi
LFLOOR=$(ledger floor 2>>"$LOG" || echo 1970-01-01)
find "$ROOT/journal" -name '2*.md' -type f | sort | while read -r j; do
  b=${j##*/}
  [[ "$b" < "$LFLOOR" ]] && continue
  grep -qxF "===== $b =====" "$JVERIFY" && continue
  printf '\n===== %s =====\n' "$b" >> "$JVERIFY"
  cat "$j" >> "$JVERIFY"
done

SIZE=$(stat -c%s "$DIGEST")
NO_WINDOW=0
if [ "$SIZE" -lt 200 ] && [ ! -s "$GITLOG" ] && [ -z "$RESUME" ]; then
  if [ "$QUEUE_N" -eq 0 ] && [ "$CARRY_N" -eq 0 ] && [ -z "$SOURCE_FAILED" ]; then
    echo "$(date -Is) nothing new (digest ${SIZE}B), queue empty, carry empty — nothing to distil" >> "$LOG"
    if [ "$DRYRUN" != "1" ]; then
      date -Is > "$STAMP"
      # A quiet night is quiet for DISTILLATION only: cards change without a journal line, and the
      # triage compares the whole standing base with itself every night (#208). A
      # configured triage runs here exactly as on a busy night (review F7).
      run_triage_stage
    fi
    echo "$(date -Is) nightly done (nothing to distil)" >> "$LOG"
    exit 0
  fi
  # Nothing new to SWEEP, but the queue or the carry is not empty: that work is the night.
  NO_WINDOW=1
  echo "$(date -Is) nothing new to sweep (digest ${SIZE}B) — continuing with $QUEUE_N queued mark(s) and $CARRY_N carried batch(es)" >> "$LOG"
fi
echo "$(date -Is) ledger: $QUEUE_N OPEN mark(s) queued, $CARRY_N consolidate batch(es) carried from earlier nights" >> "$LOG"
echo "$(date -Is) digest ${SIZE}B — swept whole (no size cap since #209)" >> "$LOG"

# `Edit` is LOAD-BEARING. Without it every one of the sweep's 9 Edit calls was
# DENIED — a headless `-p` run has nobody to grant permission — so the only writable path left was
# whole-file `Write`, and the stage re-emitted its entire card set eight times (33.7k -> 119.8k
# chars) before spending its last 24 minutes on Edits that could never be allowed. The prompts now
# mandate append-by-Edit; the tool list has to make that possible.
TOOLS="Read Write Edit Glob Grep"
# STAGE ISOLATION (PDRAIN, #209, 2026-10-06). HOME is the coordinator's home, so every stage call loaded
# ~/.claude/settings.json and the COORDINATOR's ~/.claude/CLAUDE.md ("I decide WHO the work goes to")
# plus its hooks (recall injection on the prompt, gates). In the 2026-10-06 drain 2 of 63 MARK calls
# answered by printing an `Agent({... subagent_type: "librarian" ...})` delegation as text and exited 0
# without writing their OUTPUT FILE (a probe: `claude -p "…who are you…"` -> "the coordinator"; with
# --setting-sources "" -> "NO"). A stage gets its prompt and nothing else; the one thing the user
# settings supplied that the stages rely on — effortLevel high (P209N: every stage call is
# sonnet · high) — is passed explicitly.
# OFFERED = PERMITTED (2026-10-07, owner: "fixni aby nepadala na permissions"). --allowedTools only
# pre-approves; the stage was still OFFERED Bash (and the account's claude.ai connectors), tried
# ls/wc/find/sed and got "This command requires approval" auto-rejected — 479 rejected calls
# 2026-10-06..07 in consolidate and MARK stages. --tools limits the offered set to $TOOLS and
# --strict-mcp-config drops MCP; probe: the stage lists exactly "Edit, Glob, Grep, Read, Write".
STAGE_ISOLATION=(--setting-sources "" --effort high --strict-mcp-config --tools $TOOLS)
QREPORT=$STAGING/nightly-$SK-quotecheck.txt
SWEEP=$STAGING/nightly-$SK-sweep.md
# Tonight's proposals: every consolidated batch is published as its OWN immutable inbox unit
# memory/inbox/<night key>-bNN.md (review P1-3); this staging file only concatenates the units
# this run has published so far, for the next batch's dedup.
PROPOSED_SO_FAR=$STAGING/nightly-$NK-proposed.md
RECORD=$ROOT/memory/ledger/published.jsonl

# The agent CLI writes a session transcript to KHMS_AGENT_TRANSCRIPTS/<cwd with / . _ replaced
# by ->/<session-uuid>.jsonl, so the stage that just ran is the newest JSONL under this directory.
# Used only by the max_tokens guard below.
AGENT_TRANSCRIPTS=$(khms_get KHMS_AGENT_TRANSCRIPTS)
PROJ_DIR="$AGENT_TRANSCRIPTS/$(pwd | tr '/._' '---')"
[ -d "$PROJ_DIR" ] || PROJ_DIR="$AGENT_TRANSCRIPTS"
STAGE_MARK=$STAGING/.stage-start

# 1b) CHUNK THE DIGEST — the Read tool refuses >256 kB whole and >25,000 tokens per slice, so a big
# digest is not "hard to read", it is UNREADABLE, silently: on 2026-08-25 (297,906 B) the sweep got
# both refusals and ended up having read lines 1-550 of 3,768 — 15 % of its input — while its
# output looked like a normal night. The same blindness hit 2026-08-22 (328 kB -> 6 digest quotes
# against 49 on a small night). Chunks are sized with >=30 % margin under both caps.
# Chunk failure is fatal to prepare: an unbounded monolith would break the input contract.
CHUNK_INDEX=$STAGING/nightly-$SK-digest-chunks.txt
GITLOG_INDEX=$STAGING/nightly-$SK-git-chunks.txt
SWEEP_SOURCE_INDEX=$STAGING/nightly-$SK-prepare-chunks.txt
if [ -z "$RESUME" ]; then
: > "$CHUNK_INDEX"
python3 "$CODE/digest_chunk.py" "$DIGEST" "$STAGING" "nightly-$NK-digest" \
  --index "$CHUNK_INDEX" >> "$LOG" 2>&1 \
  || { echo "digest: chunking failed (partial index is not a complete window)" >> "$SRC_FAILED";
       SOURCE_FAILED=$(grep -c . "$SRC_FAILED");
       echo "$(date -Is) digest chunking FAILED — prepare will not read a partial index or monolith" >> "$LOG"; }
# 1c) CHUNK THE GIT LOG — same Read-tool caps, same silent blindness, and it is a SPECIFIED
# nightly input (spec §9.2: "delta transcript digest, today's journal, the day's git log").
# Measured on the four most recent nights, `--stat` git logs are ALL over the 25,000-token
# slice cap: 08-24 65.5 kB (~43.5k tok), 08-25 44.9 kB (~29.9k), 08-26 95.9 kB (~63.7k),
# 08-27 158.4 kB (~105k) — the last one four times the cap. A stage handed the monolith can
# only Read a self-chosen prefix of it, which is how 2026-08-27 produced 7 `src=gitlog` quotes
# where 2026-08-26 produced 108 off the same kind of day. Same chunker, same >=30 % margin,
# same fail-closed chunking contract; no monolith fallback for prepare.
: > "$GITLOG_INDEX"
if [ -s "$GITLOG" ]; then
  python3 "$CODE/digest_chunk.py" "$GITLOG" "$STAGING" "nightly-$NK-git" \
    --index "$GITLOG_INDEX" >> "$LOG" 2>&1 \
    || { echo "gitlog: chunking failed (partial index is not a complete window)" >> "$SRC_FAILED";
         SOURCE_FAILED=$(grep -c . "$SRC_FAILED");
         echo "$(date -Is) gitlog chunking FAILED — prepare will not read a partial index or monolith" >> "$LOG"; }
fi
# 1d) Prepare groups own explicit source-labelled chunks, including git chunks.
# Always use the part machinery, even for one chunk. No full index or other source is
# attached to a part. Each CLI invocation starts fresh (no resume/continue arguments).
SWEEP_GROUP_FILES=()
: > "$SWEEP_SOURCE_INDEX"
while IFS= read -r _chunk; do printf 'digest\t%s\n' "$_chunk"; done < "$CHUNK_INDEX" >> "$SWEEP_SOURCE_INDEX"
while IFS= read -r _chunk; do printf 'gitlog\t%s\n' "$_chunk"; done < "$GITLOG_INDEX" >> "$SWEEP_SOURCE_INDEX"
fi   # a new window's chunking
if { [ -s "$DIGEST" ] && [ ! -s "$CHUNK_INDEX" ]; } || { [ -s "$GITLOG" ] && [ ! -s "$GITLOG_INDEX" ]; }; then
  SOURCE_FAILED="prepare chunking failed — no unbounded monolith fallback"
fi
if [ -n "$RESUME" ]; then
  # the frozen part lists, as the collecting run split them (frozen_check proved them complete)
  mapfile -t SWEEP_GROUP_FILES < <(ls "$STAGING/nightly-$SK-prepare-group"[0-9]*.txt | sort)
  echo "$(date -Is) prepare: frozen list $SK: $(wc -l < "$SWEEP_SOURCE_INDEX") primary source chunk(s) in ${#SWEEP_GROUP_FILES[@]} part(s), <= $SWEEP_GROUP_CHUNKS chunk(s) each" >> "$LOG"
elif [ -s "$SWEEP_SOURCE_INDEX" ]; then
  rm -f "$STAGING/nightly-$SK-prepare-group"[0-9]*.txt
  split -l "$SWEEP_GROUP_CHUNKS" -d -a 3 --additional-suffix=.txt "$SWEEP_SOURCE_INDEX" \
    "$STAGING/nightly-$SK-prepare-group"
  mapfile -t SWEEP_GROUP_FILES < <(ls "$STAGING/nightly-$SK-prepare-group"[0-9]*.txt | sort)
  echo "$(date -Is) prepare: $(wc -l < "$SWEEP_SOURCE_INDEX") primary source chunk(s) -> ${#SWEEP_GROUP_FILES[@]} fresh session(s), <= $SWEEP_GROUP_CHUNKS chunk(s) each" >> "$LOG"
fi
# A successful splitter is not proof of complete coverage: verify source identities in order.
PREPARE_GROUP_INDEX=$STAGING/nightly-$NK-prepare-group-index.txt
: > "$PREPARE_GROUP_INDEX"
if [ ${#SWEEP_GROUP_FILES[@]} -gt 0 ]; then
  cat "${SWEEP_GROUP_FILES[@]}" > "$PREPARE_GROUP_INDEX" || SOURCE_FAILED="prepare group reading failed"
fi
if ! cmp -s "$SWEEP_SOURCE_INDEX" "$PREPARE_GROUP_INDEX"; then
  echo "prepare: grouped chunk identities differ from source index" >> "$SRC_FAILED"
  SOURCE_FAILED=$(grep -c . "$SRC_FAILED")
fi
PREPARE_COMPLETED_INDEX=$STAGING/nightly-$NK-prepare-completed-chunks.txt
: > "$PREPARE_COMPLETED_INDEX"
PREPARE_COMPLETED=0
sweep_group_inputs() {
  local k="$1" n="$2" gf="$3" o="$4" source chunk
  echo "INPUTS:"
  echo "- prepare PART $k OF $n, a fresh session owning ONLY these <= $SWEEP_GROUP_CHUNKS primary source chunks:"
  while IFS=$'\t' read -r source chunk; do
    echo "- primary source chunk (quote only as src=$source):"
    echo "  * $chunk"
  done < "$gf"
  echo "- Read only the listed primary chunks. Do not open other source files, indices, or earlier part outputs."
  echo "OUTPUT FILE: $o"
}

# consolidate_inputs <batch file> <batch's night D> <index tonight, 1-based> <output file>
# The INPUTS block of ONE consolidate batch. A batch carried from an earlier night D is
# grounded against D's OWN staging inputs (they persist in $STAGING) — tonight's digest does
# not contain what D's candidates quote. Only tonight's first batch appends the day summary.
consolidate_inputs() {
  local b="$1" d="$2" k="$3" o="$4"
  local dg="$STAGING/nightly-$d-digest.txt" ci="$STAGING/nightly-$d-digest-chunks.txt"
  local gl="$STAGING/nightly-$d-git.txt" gi="$STAGING/nightly-$d-git-chunks.txt"
  local jv="$STAGING/nightly-$d-journals-ledger.md" qr="$STAGING/nightly-$d-quotecheck.txt"
  [ -f "$jv" ] || jv="$STAGING/nightly-$d-journals.md"
  [ -f "$dg" ] || { dg=$DIGEST; ci=$CHUNK_INDEX; gl=$GITLOG; gi=$GITLOG_INDEX; jv=$JVERIFY; qr=$QREPORT; }
  # a MARK-stage directory (<night key>-mNN, step 1e) has no inputs of its own but its own quote report;
  # it is grounded against the journals of the night that consolidates it (they only grow)
  [ -f "$STAGING/nightly-$d-quotecheck.txt" ] && qr="$STAGING/nightly-$d-quotecheck.txt"
  echo "INPUTS:"
  echo "- sweep candidates (THIS BATCH — consolidate exactly these): $b"
  echo "- already proposed tonight (earlier batches, READ-ONLY, dedup against it; may not exist yet): $PROPOSED_SO_FAR"
  echo "- transcript digest (source of truth, GREP it — too large to Read whole): $dg"
  echo "- transcript digest as readable chunks (Read these when you need context around a hit):"
  if [ -s "$ci" ]; then sed 's|^|  * |' "$ci"; else echo "  * none — digest small enough to Read: $dg"; fi
  echo "- journal (source of truth, grep it): $jv"
  if [ -s "$gi" ]; then
    echo "- the day's git log, SPLIT INTO READABLE CHUNKS (quote as src=gitlog):"
    sed 's|^|  * |' "$gi"
    echo "- the day's git log, whole file — GREP ONLY, never Read it whole: $gl"
  elif [ -s "$gl" ]; then
    echo "- the day's git log ($GITLOG_WHAT), quote as src=gitlog: $gl"
  else
    echo "- the day's git log: $gl — EMPTY (no commits in this window). Do not cite src=gitlog."
  fi
  if [ "$k" = "1" ]; then
    echo "- today's journal (append the day summary here, nothing else): $JOURNAL"
  else
    echo "- day summary: already appended by tonight's first batch — do NOT append to any journal"
  fi
  echo "- quote-verification report: $qr"
  echo "OUTPUT FILE: $o"
}

if [ "$DRYRUN" = "1" ]; then
  echo "=== DRY RUN — inputs assembled in $STAGING, no model call made ==="
  echo
  echo "--- deterministic inputs ---"
  for f in "$DIGEST" "$JSRC" "$GITLOG"; do
    printf '%-70s %10d B\n' "$f" "$(stat -c%s "$f" 2>/dev/null || echo 0)"
  done
  printf 'digest chunks: %s   gitlog chunks: %s\n' \
    "$(wc -l < "$CHUNK_INDEX" 2>/dev/null || echo 0)" \
    "$(wc -l < "$GITLOG_INDEX" 2>/dev/null || echo 0)"
  printf 'sweep parts: %s (one sweep call per part of <= %s chunks; 0 = one call, prompt as below)\n' \
    "${#SWEEP_GROUP_FILES[@]}" "$SWEEP_GROUP_CHUNKS"
  printf 'sweep: timeout %s per call; deadline: no sweep part or retry attempt starts later than %s s after the sweep stage starts; failed sweeps so far: %s\n' \
    "$SWEEP_TIMEOUT" "$SWEEP_BUDGET_SEC" \
    "$(grep -c . "$SWEEP_FAILED_FILE" 2>/dev/null || echo 0)"
  echo
  if [ -n "$RESUME" ]; then
    echo "prepare: RESUMING the frozen list $SK (window $SINCE -> $WINDOW_END) from $STAGING/.prepare-$SK — parts with a valid completion record are not called again"
  else
    echo "prepare: a new window $SINCE -> $RUN_START; its input list is frozen in $STAGING/.prepare-$SK before part 1"
  fi
  echo "prepare journal tokens: 0 (journal records belong to the MARK ledger/stage)"
  echo "--- SWEEP stage INPUTS block (verbatim, as the prompt receives it) ---"
  for _i in "${!SWEEP_GROUP_FILES[@]}"; do
    sweep_group_inputs "$((_i + 1))" "${#SWEEP_GROUP_FILES[@]}" "${SWEEP_GROUP_FILES[$_i]}" \
      "$STAGING/nightly-$SK-sweep-g$(printf %02d "$((_i + 1))").md"
  done
  echo
  echo "--- CONSOLIDATE stage: one call PER BATCH of $CONSOLIDATE_BATCH candidates (no card cap) ---"
  echo "  carried batches from earlier nights (done FIRST): $CARRY_N"
  carry_batches | sed 's|^|    * |'
  echo "  tonight's sweep is split into batches committed atomically as $CARRY/<night key>/cons-NN.md"
  echo "  each consolidated batch is published as its own immutable inbox unit memory/inbox/<night key>-bNN.md (record: $RECORD)"
  echo "  per batch: timeout $CONSOLIDATE_TIMEOUT, $BATCH_RETRY_MAX attempt(s); no new batch later than $((CARD_BUDGET_SEC / 60)) min after the consolidation starts"
  echo "  INPUTS block of a batch (verbatim shape):"
  consolidate_inputs "$CARRY/$NK/cons-01.md" "$NK" 1 "$STAGING/nightly-consout-$NK-01.md" | sed 's|^|    |'
  echo
  echo "--- MARK LEDGER / MARK stage (board #209) ---"
  ledger sync --dry 2>&1 | sed 's|^|  |' || true
  echo "  queue: $QUEUE_N OPEN mark(s) not held by any pending proposal -> $QUEUE"
  # `|| true`: an EMPTY queue is a normal night; under `set -o pipefail` grep's rc 1 used to end
  # the dry run here with rc 1, before the MARK-stage limits, quote sources and triage were printed.
  { grep -m3 -E '^- [MG]-' "$QUEUE" || true; } | cut -c1-160 | sed 's|^|    |'
  echo "  MARK stage: the per-mark evaluator (permark.py, #237) AFTER the generator: one packet and ONE call without tools per queued id (journal M- and generated G-, one time axis), timeout ${PERMARK_TIMEOUT_S}s per call, no new mark later than ${MARK_BUDGET_MIN} min after the stage starts, no new mark once \$${MARK_BUDGET_USD} would be passed (0 = no dollar cap)"
  echo "  its output is committed once as $CARRY/<night key>-m01/ with every batch's durable output (split-run --ready): published without a consolidate call"
  echo "  grounding journal source (window + every journal from $LFLOOR): $JVERIFY ($(stat -c%s "$JVERIFY") B)"
  echo "  know-how test: consolidate prompt $(grep -c 'not know-how' "$CODE/prompts/nightly-consolidate.md" || true) line(s), MARK prompt $(grep -c 'not know-how' "$CODE/prompts/nightly-marks.md" || true) line(s) name the drop reason; each output's drops are logged as 'know-how (<stage>): …'"
  echo
  echo "--- verify_quotes sources (must match the src= labels the stages may use) ---"
  echo "  digest=$DIGEST"
  echo "  journal=$JVERIFY"
  echo "  gitlog=$GITLOG"
  echo
  echo "--- merge-pressure stages (added 2026-09-04) ---"
  echo "  === nearest ===            python3 $CODE/nearest_cards.py $SWEEP"
  echo "  === verify_relations ===   per batch, before it is published:"
  echo "                             python3 $CODE/nearest_cards.py <batch output> --only-missing"
  echo "                             python3 $CODE/verify_relations.py <batch output>   (no --max: NO CARD CAP)"
  python3 "$CODE/verify_relations.py" --self-test || true
  echo
  echo "--- agent: ${AGENT_BIN:+$AGENT_BIN ($KHMS_AGENT_WHY)}${AGENT_BIN:-$KHMS_AGENT_WHY — the model stages would FAIL}; models sweep=$SWEEP_MODEL marks=$MARK_MODEL consolidate=$CONS_MODEL (aliases; each call logs the model that served it)"
  echo "--- TRIAGE stage (step 4b) — delegated dry run ---"
  if [ ${#TRIAGE_CMD[@]} -gt 0 ]; then
    NIGHTLY_DRYRUN=1 "${TRIAGE_CMD[@]}" || true
  else
    echo "  OFF (KHMS_NIGHT_TRIAGE not configured)"
  fi
  exit 0
fi

# 2) sweep (reads everything, over-generates — but must quote its sources)
#
# RETRY, GROWING DELAY (board #170, 2026-09-22): a single transient failure of this call
# (a 500 from the API, a dropped connection) used to kill the whole night — see
# nightly_retry.sh for the incident and the contract. `SWEEP_OK` records whether it
# eventually succeeded; the rest of the script decides what to skip, but never exits here.
source "$CODE/nightly_retry.sh"
FAILED_STAGES=""
# 2) THE GENERATOR (#237 step 2; the former sweep). The night's FIRST model stage again: its lines must be
# committed before the evaluator builds its queue, so they are judged on the same night, time-sorted among
# the journal MARKs. Each part reads its chunk(s) and writes MARK-shaped LINES (prompts/nightly-generate.md);
# generated_marks.py keeps the well-formed, know-how, grounded ones and commits them, with the window, to
# memory/ledger/generated/<SK>.md by one rename; the stamp moves then. The sweep's own consolidation is gone
# for them: the per-mark evaluator below is the one consolidation path.
SWEEP_OK=1
GEN_PARTS=()
# THE SWEEP'S OWN DEADLINE (#232): counted from here, after the MARK stage, not from the run's start.
SWEEP_DEADLINE=$(( $(date +%s) + SWEEP_BUDGET_SEC ))
echo "$(date -Is) === sweep === no new part after $(date -d @"$SWEEP_DEADLINE" +%H:%M:%S) (+$((SWEEP_BUDGET_SEC / 60)) min from this stage's start)" >> "$LOG"
if [ -z "$AGENT_BIN" ]; then
  # No model can run tonight: the sweep FAILS (no stamp, the window is swept again), queued marks
  # stay OPEN and carried batches stay in the carry queue; only work that needs no model call
  # (publishing an already durable batch output) still happens below.
  SWEEP_OK=0
  FAILED_STAGES="$FAILED_STAGES agent-unavailable"
  echo "$(date -Is) $KHMS_AGENT_WHY — every model stage of this night FAILS; nothing is stamped" >> "$LOG"
elif [ -n "$SOURCE_FAILED" ]; then
  # A configured source could not be read (step 1 above): the window is incomplete, so it is not
  # swept and not committed; the stamp stays where it was and the next night collects it again.
  SWEEP_OK=0
  FAILED_STAGES="$FAILED_STAGES source"
  echo "$(date -Is) sweep NOT RUN — $SOURCE_FAILED configured source(s) FAILED ($SRC_FAILED); the window since $SINCE stays open and nothing is stamped" >> "$LOG"
elif [ "$NO_WINDOW" = "1" ]; then
  # Nothing new since the stamp: no sweep, an empty candidate file, and the night goes to the
  # queue and the carry (step 1a decided there is something in one of them).
  : > "$SWEEP"
  echo "$(date -Is) sweep SKIPPED — nothing new in the window" >> "$LOG"
elif [ ${#SWEEP_GROUP_FILES[@]} -gt 0 ]; then
  # 2-parts) ONE SWEEP CALL PER PART, EACH PART A TRANSACTION AGAINST THE FROZEN LIST (see THE FROZEN
  # PREPARE LIST above). Part k writes its generated lines to nightly-<SK>-sweep-gNN.md; its completion
  # record is renamed into place only after that, so a part killed at any point before it has no record
  # and starts again from nothing next time. A part with a valid record is skipped. Only when EVERY part
  # of the list is complete are the parts' lines committed (step 2e, generated_marks.py build). The first
  # failing part (call, deadline, usage limit) stops the loop; what finished stays finished for the next run.
  _K=${#SWEEP_GROUP_FILES[@]}
  PREP_DIR=$STAGING/.prepare-$SK
  if [ -z "$RESUME" ]; then
    mkdir -p "$PREP_DIR"
    printf 'since=%s\nend=%s\ngroup_chunks=%s\nparts=%s\nindex_sha256=%s\n' "$SINCE" "$WINDOW_END" \
      "$SWEEP_GROUP_CHUNKS" "$_K" "$(sha_of "$SWEEP_SOURCE_INDEX")" > "$PREP_DIR/FROZEN.tmp"
    frozen_inputs "$SK" | while IFS= read -r _f; do
      printf 'input=%s\t%s\n' "$(sha_of "$_f")" "$_f"
    done >> "$PREP_DIR/FROZEN.tmp"
    mv "$PREP_DIR/FROZEN.tmp" "$PREP_DIR/FROZEN"
    echo "$(date -Is) prepare: input list frozen in $PREP_DIR ($_K part(s), window $SINCE -> $WINDOW_END)" >> "$LOG"
  fi
  part_out() { echo "$STAGING/nightly-$SK-sweep-g$(printf %02d "$1").md"; }
  part_complete() {   # part_complete <record> <part list> <output> -> 0 when the record proves the part done
    local o=absent
    [ -f "$1" ] || return 1
    [ "$(sed -n 's/^group_sha256=//p' "$1")" = "$(sha_of "$2")" ] || return 1
    if [ -e "$3" ]; then o=$(sha_of "$3"); fi
    [ "$(sed -n 's/^output_sha256=//p' "$1")" = "$o" ] || return 1
    # a part finished by the pre-#237 sweep holds candidate CARDS, not generated lines: it is redone
    ! grep -qE '^#{2,3} +[A-Z][0-9]+' "$3" 2>/dev/null
  }
  _kept=0
  for _k in $(seq 1 "$_K"); do
    _gf=${SWEEP_GROUP_FILES[$((_k - 1))]}
    _gout=$(part_out "$_k")
    _rec=$PREP_DIR/done-$(printf %03d "$_k")
    if part_complete "$_rec" "$_gf" "$_gout"; then
      _kept=$((_kept + 1))
      cat "$_gf" >> "$PREPARE_COMPLETED_INDEX"
      PREPARE_COMPLETED=$((PREPARE_COMPLETED + $(wc -l < "$_gf")))
      continue
    fi
    if limit_stop "sweep parts $_k..$_K"; then SWEEP_OK=0; break; fi
    if [ "$(date +%s)" -ge "$SWEEP_DEADLINE" ]; then
      SWEEP_OK=0
      echo "$(date -Is) sweep part $_k/$_K NOT STARTED — the sweep deadline $(date -d @"$SWEEP_DEADLINE" +%H:%M:%S) (+$((SWEEP_BUDGET_SEC / 60)) min from the sweep's start) has passed; the sweep is incomplete" >> "$LOG"
      break
    fi
    # no valid record: whatever an earlier, killed attempt left is not this part's output
    rm -f "$_rec" "$_gout" "${_gout%.md}-part"*.md "${_gout%.md}-final.md"
    echo "$(date -Is) sweep part $_k/$_K: $(grep -c . "$_gf") chunk(s) -> $_gout" >> "$LOG"
    if ! RETRY_DEADLINE_EPOCH=$SWEEP_DEADLINE retry_claude_call "sweep-$_k" "$SWEEP_TIMEOUT" "$LOG" "$STAGE_MARK" -- \
         "$AGENT_BIN" -p "$SWEEP_BASE

$(sweep_group_inputs "$_k" "$_K" "$_gf" "$_gout")" "${STAGE_ISOLATION[@]}" --output-format json --model "$SWEEP_MODEL" --allowedTools $TOOLS; then
      SWEEP_OK=0
      echo "$(date -Is) sweep part $_k/$_K FAILED — the sweep is incomplete; parts after it are not started" >> "$LOG"
    fi
    python3 "$CODE/maxtok_check.py" --stage "sweep-$_k" --since "$STAGE_MARK" \
      --projects "$PROJ_DIR" >> "$LOG" 2>&1 || true
    [ "$SWEEP_OK" = "1" ] || break
    _o=absent; if [ -e "$_gout" ]; then _o=$(sha_of "$_gout"); fi
    printf 'part=%s\ngroup_sha256=%s\noutput_sha256=%s\nat=%s\n' "$_k" "$(sha_of "$_gf")" "$_o" "$(date -Is)" > "$_rec.tmp"
    mv "$_rec.tmp" "$_rec"
    _kept=$((_kept + 1))
    cat "$_gf" >> "$PREPARE_COMPLETED_INDEX"
    PREPARE_COMPLETED=$((PREPARE_COMPLETED + $(wc -l < "$_gf")))
  done
  if [ "$PREPARE_COMPLETED" -ne "$(wc -l < "$SWEEP_SOURCE_INDEX")" ] \
     || ! cmp -s "$SWEEP_SOURCE_INDEX" "$PREPARE_COMPLETED_INDEX"; then
    SWEEP_OK=0
    echo "$(date -Is) prepare incomplete: $PREPARE_COMPLETED chunk(s) completed, expected $(wc -l < "$SWEEP_SOURCE_INDEX"); source identities must match exactly — $_kept of $_K part(s) kept with a completion record in $PREP_DIR; the next run continues this frozen list before it collects a new window" >> "$LOG"
  else
    # every part is complete: its generated lines are committed below (generated_marks.py build)
    for _k in $(seq 1 "$_K"); do
      _gout=$(part_out "$_k")
      if [ -s "$_gout" ]; then GEN_PARTS+=("$_gout"); else echo "$(date -Is) generator part $_k/$_K wrote no line" >> "$LOG"; fi
    done
  fi
else
  # no primary source chunk at all (an empty window that is not NO_WINDOW): nothing to generate, no call
  echo "$(date -Is) generator: no primary source chunk in the window — nothing to generate" >> "$LOG"
fi

if [ "$SWEEP_OK" -ne 1 ]; then
  FAILED_STAGES="$FAILED_STAGES sweep"
  # One line per failed sweep; read aloud by a morning review ping, removed on success. Its second field is
  # the REASON (#239): usage-limit when the account's limit stopped it (the ping then says "transient, re-run"),
  # failed otherwise -- on 2026-10-15 a limit night was announced as a failure that "will repeat".
  _sreason=failed _slimit=""
  if [ -s "$RETRY_LIMIT_FLAG" ]; then
    _sreason=usage-limit
    _slimit=" limit=\"$(head -1 "$RETRY_LIMIT_FLAG" | cut -d' ' -f2- | tr -d '"' | cut -c1-120)\""
  fi
  echo "$RUN_START reason=$_sreason night=$NK window-since=$SINCE parts=${#SWEEP_GROUP_FILES[@]}${_kept:+ kept=$_kept} frozen=${PREP_DIR:-none} digest=${SIZE}B${SOURCE_FAILED:+ sources-failed=$SOURCE_FAILED}$_slimit" >> "$SWEEP_FAILED_FILE"
  _nfail=$(grep -c . "$SWEEP_FAILED_FILE" 2>/dev/null || echo 1)
  if [ "$_nfail" -ge 2 ]; then
    echo "$(date -Is) SWEEP FAILED ${_nfail} NIGHTS IN A ROW (first: $(head -1 "$SWEEP_FAILED_FILE" | cut -d' ' -f1)) — the window since $SINCE keeps growing; a part that fails every night needs a human, see $SWEEP_FAILED_FILE" >> "$LOG"
  fi
  if [ -n "${PREP_DIR:-}" ] && [ -f "$PREP_DIR/FROZEN" ]; then
    echo "$(date -Is) sweep FAILED after retries — the window is not complete, so it is not consolidated tonight; ${_kept:-0} of ${#SWEEP_GROUP_FILES[@]} part(s) are KEPT in $PREP_DIR and the next run continues that frozen list (not stamped)" >> "$LOG"
  else
    echo "$(date -Is) sweep FAILED after retries — no sweep candidates produced (carried batches, the MARK stage's included, are still consolidated below; see the retry lines above for what was tried); not stamped, so the next run's window still covers tonight" >> "$LOG"
  fi
else
if [ -s "$SWEEP_FAILED_FILE" ]; then
  echo "$(date -Is) sweep OK — clearing the record of $(grep -c . "$SWEEP_FAILED_FILE") earlier failed sweep(s) ($SWEEP_FAILED_FILE)" >> "$LOG"
fi
rm -f "$SWEEP_FAILED_FILE"

# 2e) COMMIT THE GENERATED LINES — ONE RENAME (#237 step 2). generated_marks.py keeps every well-formed,
# know-how line whose span its source really holds, once, and writes them with the window into
# memory/ledger/generated/<SK>.md (tmp + rename). A kill before the rename leaves nothing (the window is not
# stamped and is generated again); a kill after it leaves the file, whose WINDOW the next run reads back
# (see THE GENERATED LINES above). A refusal because the file exists means an earlier run committed it.
GEN_OK=1
if [ ${#GEN_PARTS[@]} -gt 0 ]; then
  GEN_FILE=$GENERATED/$SK.md
  if [ -e "$GEN_FILE" ]; then
    echo "$(date -Is) generator: $GEN_FILE is already committed — not rebuilt" >> "$LOG"
  elif python3 "$CODE/generated_marks.py" build --out "$GEN_FILE" --window "$SINCE -> $WINDOW_END" \
         --source digest="$DIGEST" --source gitlog="$GITLOG" \
         --rejects "$STAGING/nightly-$SK-generate-rejects.txt" "${GEN_PARTS[@]}" >> "$LOG" 2>&1; then
    fault_point generate-after-commit
  else
    GEN_OK=0
    FAILED_STAGES="$FAILED_STAGES generate-commit"
    echo "$(date -Is) generator: committing the lines FAILED — the window is not stamped and is generated again" >> "$LOG"
  fi
fi

# THE WINDOW IS PERSISTED NOW, so the stamp moves here: every line of the window is in the committed file (or
# there was nothing to commit). The window's END is this run's start -- or, for a resumed frozen list, the
# start of the run that collected it (WINDOW_END). Only now is the frozen list done with.
if [ "$DRYRUN" != "1" ] && [ "$GEN_OK" = "1" ]; then
  echo "$WINDOW_END" > "$STAMP"
  if [ -d "$STAGING/.prepare-$SK" ]; then
    rm -rf "$STAGING/.prepare-$SK"
    echo "$(date -Is) prepare: the frozen list $SK is complete and its lines committed — window stamped at $WINDOW_END" >> "$LOG"
  fi
fi

fi   # SWEEP_OK

# 3) CONSOLIDATION, BATCH BY BATCH, OLDEST CARRY FIRST — AS A PER-BATCH TRANSACTION (review
# P1-2). Every step is keyed by the batch's stable id and is safe to repeat after a kill:
#   (a) the batch is already published (unit VERIFIED against its manifest line and its source)
#       -> only the carry cleanup is left; a conflict keeps batch + output in carry, no model call;
#   (b) its durable output $DUR exists -> publish it, no model call;
#   (c) otherwise one model call ($BATCH_RETRY_MAX attempts) into a staging file keyed by the
#       batch (a retry continues its own partial file), the 0-token post-checks over it, an
#       atomic rename to $DUR, then
#   publish = exclusive creation of the immutable inbox unit + a line in $RECORD, and only then
#   the batch and its $DUR leave the carry queue. Merging the same batch twice is impossible:
#   the unit's name IS the batch id, and an existing unit is never overwritten.
# The digest/gitlog/journal inputs ARE LOAD-BEARING (the stage was once asked to enforce
# grounding for a git log it had never been handed); consolidate_inputs()
# hands each batch the inputs of ITS OWN night.
# Runs even when tonight's sweep failed: carried batches from earlier nights do not depend on it.
# postprocess_batch <file> <night key>: grounding must survive the middle stage too (quotecheck,
# a report), every candidate whose journal quote is a tracked MARK gets its id, and merge
# pressure is ENFORCED by verify_relations (a candidate without exactly one valid RELATION is
# MOVED, whole, under `## DROPPED (no valid RELATION)`; NO `--max` since board #209). The gate
# is SKIPPED if the NEAREST annotation fails — a RELATION gate without NEAREST lines would drop
# candidates for the tool's own reason (a check failing for its own reason, not the candidate's).
postprocess_batch() {   # postprocess_batch <file> <night key> <tag> [digest source] [gitlog source]
  local f="$1" d="$2" tag="${3//\//-}"
  local dg="$STAGING/nightly-$d-digest.txt" jv="$STAGING/nightly-$d-journals-ledger.md"
  local gl="$STAGING/nightly-$d-git.txt" qr="$STAGING/nightly-$NK-quotecheck-$tag.txt"
  local rr="$STAGING/nightly-$NK-relations-$tag.txt"
  [ -f "$jv" ] || jv="$STAGING/nightly-$d-journals.md"
  [ -f "$dg" ] || { dg=$DIGEST; gl=$GITLOG; jv=$JVERIFY; }
  if [ -n "${4:-}" ]; then dg=$4; fi
  if [ -n "${5:-}" ]; then gl=$5; fi
  ledger attribute "$f" >> "$LOG" 2>&1 || echo "$(date -Is) mark attribution (post $tag) FAILED (non-fatal)" >> "$LOG"
  python3 "$CODE/verify_quotes.py" "$f" --source digest="$dg" --source journal="$jv" \
    --source gitlog="$gl" > "$qr" 2>&1 || true
  echo "$(date -Is) quotecheck(post $tag): $(tail -1 "$qr")" >> "$LOG"
  knowhow_line "$tag" "$f"
  echo "$(date -Is) === verify_relations === gate over $f (no cap)" >> "$LOG"
  if python3 "$CODE/nearest_cards.py" "$f" --only-missing >> "$LOG" 2>&1; then
    python3 "$CODE/verify_relations.py" "$f" > "$rr" 2>&1 || true   # report, not a gate
    echo "$(date -Is) === verify_relations === $(tail -1 "$rr")" >> "$LOG"
    grep -E '^!! MASS DROP' "$rr" >> "$LOG" || true
    grep -E '^[A-Z][0-9]+: (DROP|DEFER|FLAG)' "$rr" >> "$LOG" || true
  else
    echo "$(date -Is) === verify_relations === SKIPPED for $tag — nearest(post) FAILED, and a RELATION gate without NEAREST lines would drop candidates for the tool's own reason" >> "$LOG"
  fi
  return 0
}
batch_rb() {        # the relabel base a batch is published with (publish and verify agree)
  if [ "$KIND" = "cons" ]; then echo $(( (10#$BNN - 1) * 1000 )); else echo 0; fi
}
# THE HEADER GATE (PINBOX, #209). 2026-10-06 published 145 candidates whose header was
# `type: X; level: Y; tags: [...]` on one line — no prompt stated the shape and nothing between
# the model and the inbox looked at it. The prompts now state it; here a key-LABELLED compound
# line is split into one `key: value` per line (card_header.py fix — labelled, so not a guess),
# and `publish` refuses (rc 5, no unit) whatever is still malformed: the batch stays in carry and
# the night ends INCOMPLETE, loudly, instead of handing the review an unconvertible unit. The
# fix runs on the publish source in BOTH paths, a fresh output and a durable one from a killed
# night, so the durable output and its published unit stay byte-comparable for `published`.
publish_batch() {   # publish_batch <source file> -> 0 when the unit is verified for this batch
  # A header still malformed after normalising is QUARANTINED (PDRAIN, #209): the card moves, whole,
  # under a `## DROPPED (malformed header …)` trailer with its reason — until then publish refused
  # the whole batch, and a refused durable output was refused again every night, holding all its
  # cards and MARK ids in carry for good (drain 2026-10-06: 4 headers held 25 cards).
  if ! python3 "$CODE/card_header.py" fix "$1" >> "$LOG" 2>&1; then
    python3 "$CODE/card_header.py" quarantine "$1" >> "$LOG" 2>&1 || true
    echo "$(date -Is) card header gate: $1 — malformed header(s) moved to ## DROPPED (malformed header) for the review, nothing guessed (see the quarantine line above)" >> "$LOG"
  fi
  python3 "$BATCH_TOOL" publish "$1" --unit "$UNIT" --batch-id "$BID" --record "$RECORD" \
    --relabel-base "$(batch_rb)" >> "$LOG" 2>&1
}
finish_batch() {    # the carry cleanup — repeatable, the batch is already published
  local flat="${BID//\//-}"
  [ -f "$B" ] && mv "$B" "$STAGING/carried-done-$flat.md"
  [ -f "$DUR" ] && mv "$DUR" "$STAGING/carried-done-$flat.out.md"
  PUBLISHED_UNITS+=("$UNIT")
  python3 "$BATCH_TOOL" merge "$PROPOSED_SO_FAR" "$UNIT" >> "$LOG" 2>&1 || true
  return 0
}
# 2f) THE MARK STAGE — THE PER-MARK EVALUATOR (#237). Every OPEN, not-proposed tracked id -- journal MARKs
# (`M-`) and the generated lines (`G-`, step 2e, this window's and any earlier one still OPEN) on ONE time
# axis -- goes through permark.py: one packet, one call without tools, one decision, until MARK_BUDGET_MIN
# from this stage's start (no new mark after it). Its output is ONE transaction: post-checked (attribute,
# quote check, NEAREST, RELATION gate) and committed with ONE rename as $CARRY/<night key>-m01/ including
# every batch's durable output (split-run --ready), so the consolidation below publishes it WITHOUT a model
# call. A kill before the rename leaves its marks OPEN (they are evaluated again: it costs money, nothing
# is lost); a kill after it leaves the whole result in carry, where the ledger counts its ids as proposed.
# A deadline, the dollar cap (MARK_BUDGET_USD, #243) or the usage limit ends the evaluator early: what it decided
# is committed, the rest stays OPEN. The cap writes one line `MARK stage stopped at the $ cap: …` (exit 0).
mark_commit() {   # mark_commit <evaluator output> <run key> <chunk number> -> 0 when committed as $CARRY/<run key>
  local out="$1" key="$2" k="$3" rel="$STAGING/nightly-$2-carry.md"
  local gd="$STAGING/nightly-$NK-ground-digest.txt" gg="$STAGING/nightly-$NK-ground-git.txt"
  rm -f "$rel"
  python3 "$BATCH_TOOL" merge "$rel" "$out" --letter M --relabel-base $((k * 1000)) >> "$LOG" 2>&1 || return 1
  # A generated line is quoted from ITS window's source; the committed generated files hold every such span
  # verbatim (generated_marks.py checked them against that source), so they ground it on any later night.
  { cat "$DIGEST" 2>/dev/null; cat "$GENERATED"/*.md 2>/dev/null; } > "$gd" || true
  { cat "$GITLOG" 2>/dev/null; cat "$GENERATED"/*.md 2>/dev/null; } > "$gg" || true
  postprocess_batch "$rel" "$NK" "$key" "$gd" "$gg"
  fault_point "marks-before-commit-$k"
  if ! python3 "$BATCH_TOOL" split-run "$rel" --size "$CONSOLIDATE_BATCH" --carry "$CARRY" --run "$key" --ready \
       > /dev/null 2>>"$LOG"; then
    [ -d "$CARRY/$key" ] || return 1
    echo "$(date -Is) marks-$k: split reported a failure AFTER its commit — $CARRY/$key is complete" >> "$LOG"
  fi
  echo "$(date -Is) marks-$k committed: $(python3 "$BATCH_TOOL" count "$rel") candidate(s) and $(grep -cE '^- [MG]-[0-9a-f]{8} ::' "$rel" || true) NOT CARDED verdict(s) -> $CARRY/$key (ready: no consolidate call)" >> "$LOG"
  fault_point "marks-after-commit-$k"
  return 0
}
MARK_PHASE_START=$(date +%s)
MARK_DEADLINE=$((MARK_PHASE_START + ${MARK_BUDGET_S:-$((MARK_BUDGET_MIN * 60))}))   # _S: test seam
ledger sync >> "$LOG" 2>&1 || echo "$(date -Is) mark ledger sync (marks) FAILED (non-fatal)" >> "$LOG"
ledger queue --out "$QUEUE" >> "$LOG" 2>&1 \
  || { echo "$(date -Is) mark ledger queue FAILED (non-fatal) — no MARK stage tonight" >> "$LOG"; : > "$QUEUE"; }
QUEUE_N=$(grep -cE '^- [MG]-' "$QUEUE" 2>/dev/null) || QUEUE_N=0
QUEUE_G=$(grep -cE '^- G-' "$QUEUE" 2>/dev/null) || QUEUE_G=0
echo "$(date -Is) === marks === per-mark evaluator: $QUEUE_N queued id(s) ($QUEUE_G generated line(s)); no new mark after $(date -d @"$MARK_DEADLINE" +%H:%M) (+${MARK_BUDGET_MIN} min from this stage's start); no new mark once \$${MARK_BUDGET_USD} would be passed (0 = no dollar cap)" >> "$LOG"
MOUT=$STAGING/nightly-$NK-markout.md
MKEY=$NK-m01
if [ "$QUEUE_N" -gt 0 ] && [ -n "$AGENT_BIN" ] && ! limit_stop "the MARK stage ($QUEUE_N queued id(s) stay OPEN)"; then
  PRC=0
  python3 "$CODE/permark.py" --queue "$QUEUE" --out "$MOUT" --deadline "$MARK_DEADLINE" --max-usd "$MARK_BUDGET_USD" --agent "$AGENT_BIN" \
    --model "$MARK_MODEL" --timeout "$PERMARK_TIMEOUT_S" --log "$LOG" --jsonl "$STAGING/nightly-$NK-permark.jsonl" \
    --served-file "$RETRY_SERVED_FILE" --limit-flag "$RETRY_LIMIT_FLAG" > /dev/null 2>>"$LOG" || PRC=$?
  if [ "$PRC" = "75" ]; then limit_stop "the rest of the MARK queue" || true; fi
  if [ "$PRC" != "0" ] && [ "$PRC" != "75" ]; then
    FAILED_STAGES="$FAILED_STAGES marks"
    echo "$(date -Is) marks: the evaluator FAILED (rc=$PRC) — what it decided is still committed; the rest stays OPEN" >> "$LOG"
  fi
  if [ -s "$MOUT" ] && grep -qE '^(MARKS: |- [MG]-[0-9a-f]{8} ::)' "$MOUT"; then
    if ! mark_commit "$MOUT" "$MKEY" 1; then
      FAILED_STAGES="$FAILED_STAGES marks-commit"
      echo "$(date -Is) marks: commit to the carry queue FAILED — nothing of it is used, its marks stay OPEN for the next night" >> "$LOG"
    fi
    ledger coverage "$MOUT" --queue "$QUEUE" >> "$LOG" 2>&1 || true
    knowhow_line "marks" "$MOUT"
  else
    echo "$(date -Is) marks: the evaluator decided no mark tonight — the queue stays OPEN" >> "$LOG"
  fi
elif [ "$QUEUE_N" -gt 0 ] && [ -z "$AGENT_BIN" ]; then
  echo "$(date -Is) marks: NOT STARTED — no agent CLI; $QUEUE_N id(s) stay OPEN" >> "$LOG"
fi

CB=0; CB_DONE=0; CB_LEFT=0; CB_LIMIT=0
PUBLISHED_UNITS=()
# THE CONSOLIDATION'S OWN DEADLINE (#232): counted from here, after the MARK stage and the sweep.
CARD_DEADLINE=$(( $(date +%s) + CARD_BUDGET_SEC ))
echo "$(date -Is) === consolidate === $(carry_batches | wc -l) batch(es) in the carry queue; no new batch after $(date -d @"$CARD_DEADLINE" +%H:%M:%S) (+$((CARD_BUDGET_SEC / 60)) min from this stage's start)" >> "$LOG"
: > "$PROPOSED_SO_FAR"
mapfile -t CBATCHES < <(carry_batches)
for B in "${CBATCHES[@]}"; do
  [ -f "$B" ] || continue
  batch_ident "$B"
  # (a) published by an earlier run that died before its cleanup: nothing to redo — but only
  # when the unit is VERIFIED as this batch's output (manifest line: batch -> unit -> sha, and
  # the durable output / NOT CARDED file normalised the same way). The bare existence of the
  # unit file used to drop the batch from carry, so a
  # unit with different content silently replaced its output. rc 10 = not published yet (the
  # only code that lets the batch go on to publish or a model call); 3 conflict, 4 missing
  # artefact or a failed check: the batch AND its output stay in carry, the night is marked.
  VSRC=""
  if [ "$KIND" = "notcarded" ]; then VSRC=$B; elif [ -s "$DUR" ]; then VSRC=$DUR; fi
  VRC=0
  python3 "$BATCH_TOOL" published "$BID" --unit "$UNIT" --record "$RECORD" \
    ${VSRC:+--src "$VSRC"} --relabel-base "$(batch_rb)" >> "$LOG" 2>&1 || VRC=$?
  case "$VRC" in
    0)
      echo "$(date -Is) $BID already published as ${UNIT##*/} (verified) — carry cleanup only, no model call" >> "$LOG"
      finish_batch
      continue ;;
    10) ;;
    *)
      FAILED_STAGES="$FAILED_STAGES publish-verify-$BID"
      echo "$(date -Is) PUBLISH CONFLICT $BID (verify rc=$VRC): ${UNIT##*/} is not provably this batch's output — batch and its output stay in the carry queue, no model call; resolve by hand (see the verify line above)" >> "$LOG"
      continue ;;
  esac
  # NOT CARDED verdicts are published as they are — they are verdicts for the review
  if [ "$KIND" = "notcarded" ]; then
    if publish_batch "$B"; then finish_batch; else
      FAILED_STAGES="$FAILED_STAGES publish-$BID"
      echo "$(date -Is) publish of $BID FAILED — it stays in the carry queue" >> "$LOG"
    fi
    continue
  fi
  # (b) a durable output survived a kill: publish it, no model call
  if [ -s "$DUR" ]; then
    if publish_batch "$DUR"; then
      fault_point after-publish
      finish_batch; CB_DONE=$((CB_DONE + 1))
    else
      FAILED_STAGES="$FAILED_STAGES publish-$BID"
      echo "$(date -Is) publish of $BID FAILED — batch and its output stay in the carry queue" >> "$LOG"
    fi
    continue
  fi
  # a batch that needs a model call is skipped (not the loop: published/durable/NOT CARDED
  # batches above still publish without one); the skipped ones are named once after the loop
  if [ -s "$RETRY_LIMIT_FLAG" ]; then CB_LIMIT=$((CB_LIMIT + 1)); continue; fi
  if [ "$(date +%s)" -ge "$CARD_DEADLINE" ]; then
    CB_LEFT=$(carry_batches | wc -l)
    echo "$(date -Is) consolidate budget of $((CARD_BUDGET_SEC / 60)) min (from its own start) reached — $CB_LEFT batch(es) stay in $CARRY for the next night" >> "$LOG"
    break
  fi
  if [ -z "$AGENT_BIN" ]; then
    echo "$(date -Is) consolidate-$BD-$BNN NOT STARTED — no agent CLI; the batch stays in the carry queue" >> "$LOG"
    continue
  fi
  CB=$((CB + 1))
  BOUT=$STAGING/nightly-consout-$BD-$BNN.md
  CPROMPT="$CONSOLIDATE_BASE

$(consolidate_inputs "$B" "$BD" "$CB" "$BOUT")"
  if RETRY_MAX_ATTEMPTS=$BATCH_RETRY_MAX retry_claude_call "consolidate-$BD-$BNN" "$CONSOLIDATE_TIMEOUT" "$LOG" "$STAGE_MARK" -- \
       "$AGENT_BIN" -p "$CPROMPT" "${STAGE_ISOLATION[@]}" --output-format json --model "$CONS_MODEL" --allowedTools $TOOLS; then
    if [ -s "$BOUT" ]; then
      postprocess_batch "$BOUT" "$BD" "$BID"
      cp "$BOUT" "$DUR.tmp" && mv "$DUR.tmp" "$DUR"
      fault_point before-publish
      if publish_batch "$DUR"; then
        fault_point after-publish
        finish_batch; CB_DONE=$((CB_DONE + 1))
      else
        FAILED_STAGES="$FAILED_STAGES publish-$BID"
        echo "$(date -Is) publish of $BID FAILED — batch and its output stay in the carry queue" >> "$LOG"
      fi
    else
      FAILED_STAGES="$FAILED_STAGES consolidate-$BD-$BNN"
      echo "$(date -Is) consolidate-$BD-$BNN produced no output — the batch stays in the carry queue" >> "$LOG"
    fi
  else
    FAILED_STAGES="$FAILED_STAGES consolidate-$BD-$BNN"
    echo "$(date -Is) consolidate-$BD-$BNN FAILED — the batch stays in the carry queue for the next night (partial output kept at $BOUT, the next attempt continues it)" >> "$LOG"
  fi
  python3 "$CODE/maxtok_check.py" --stage "consolidate-$BD-$BNN" --since "$STAGE_MARK" \
    --projects "$PROJ_DIR" >> "$LOG" 2>&1 || true
done
[ "$CB_LIMIT" -eq 0 ] || limit_stop "$CB_LIMIT consolidate batch(es) (they stay in $CARRY)" || true
# A run directory whose batches are all published is closed: its window is folded into the
# stamp (never backwards) and the directory goes. A MARK-stage directory (<night key>-mNN) has no
# WINDOW, so closing it never moves the stamp.
for _d in "$CARRY"/[0-9]*/; do
  [ -d "$_d" ] || continue
  _d=${_d%/}
  if ! ls "$_d"/*.md >/dev/null 2>&1; then
    _wv=$(cat "$_d/WINDOW" 2>/dev/null || true)
    if [ -n "$_wv" ] && [ "$(date -d "$_wv" +%s 2>/dev/null || echo 0)" -gt "$(date -d "$(cat "$STAMP" 2>/dev/null || echo 1970-01-01)" +%s 2>/dev/null || echo 0)" ]; then
      echo "$_wv" > "$STAMP"
    fi
    rm -f "$_d/WINDOW"; rmdir "$_d" 2>/dev/null || true
  fi
done
CB_LEFT=$(carry_batches | wc -l)
echo "$(date -Is) consolidate: $CB_DONE batch(es) published as inbox units (${#PUBLISHED_UNITS[@]} unit(s) incl. NOT CARDED), $CB_LEFT left in the carry queue" >> "$LOG"

# 4a') LEDGER STATE FOR THE MORNING: OPEN count + oldest OPEN, read by the session-start hook and
# printed by approve_inbox.py.
ledger sync >> "$LOG" 2>&1 || echo "$(date -Is) mark ledger sync (post) FAILED (non-fatal)" >> "$LOG"

# 4b) TRIAGE STAGE (optional, KHMS_NIGHT_TRIAGE) — sweep the STANDING base for contradictions and
# triage what loses. Consolidation flags contradictions only among the night's own candidates; no
# other stage compares anything against the cards already standing. In the reference deployment,
# measured over ~2,300 cards: 7 superseded and 7 refuted across months of work containing far more
# than seven proven errors. The base does not have a deletion problem, it has a detection problem,
# and this is the stage that detects.
#
# ALWAYS ATTEMPTED when configured, even when sweep or consolidate exhausted their retries above:
# it compares the STANDING base against itself, not against tonight's candidates, so a failure
# upstream must not stop it. It runs BEFORE the final verdict so a triage failure cannot cost the
# night: it writes its own file (memory/inbox/<night key>-triage.md), takes no night lock (it holds the
# clone's code-use lock, nested in this run's), and run_triage_stage keeps it non-fatal under
# `set -euo pipefail`. Unconfigured, the log says so. A quiet night runs it too (above).
run_triage_stage
# THE NIGHT'S MODELS (#232): one line, per stage family, of the models that actually served (from each
# call's served-by line); the morning review ping repeats it with the triage judge's.
echo "$(date -Is) models served this night: $(python3 "$CODE/agent_result.py" --summary "$RETRY_SERVED_FILE")" >> "$LOG"

# THE STAMP (board #209): written right after the sweep's candidates were persisted in the carry
# queue (step 2e), never when the SWEEP failed — then the window stays open so the NEXT run's
# SINCE still covers tonight. A failed MARK chunk leaves its marks OPEN in the ledger and a
# failed consolidate batch stays in the carry queue: both are tomorrow's work, neither is lost.
if [ -n "$FAILED_STAGES" ]; then
  if [ "$SWEEP_OK" -ne 1 ]; then
    WHERE="not stamped, so the next run's window still covers this one; committed MARK chunks are in $CARRY or the inbox, failed ones stay OPEN"
  else
    WHERE="window stamped; failed MARK chunks stay OPEN in the ledger and failed batches stay in $CARRY — the next night continues from there"
  fi
  echo "$(date -Is) nightly INCOMPLETE — failed stages:$FAILED_STAGES — see the retry/FAILED lines above for what was tried and why; $WHERE" >> "$LOG"
  exit 1
fi

# `grep -c PAT f || echo 0` printed TWO lines whenever the count was zero — grep -c prints its "0"
# AND exits 1, so the fallback fired on top of it and every "nightly done" line since 2026-08-12
# read "(0\n0 cards)". It was always zero anyway: the consolidate output labels cards `### C<n>`,
# and `^id: ` only appears after the morning review adds the YAML front matter. Count the headings
# the stage actually writes, and never let the count expression fail the run under `set -e`.
CARDS=0
for _u in "${PUBLISHED_UNITS[@]}"; do
  _c=$(grep -cE '^#{2,3} +[A-Z][0-9]+' "$_u" 2>/dev/null) || _c=0
  CARDS=$((CARDS + _c))
done
echo "$(date -Is) nightly done -> ${#PUBLISHED_UNITS[@]} inbox unit(s) $(for _u in "${PUBLISHED_UNITS[@]}"; do printf '%s ' "${_u##*/}"; done)($CARDS cards; carry queue: $CB_LEFT batch(es))" >> "$LOG"
