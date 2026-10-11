#!/usr/bin/env bash
# KHMS nightly sweep driver: preprocess (0 tokens) -> sweep -> QUOTE CHECK -> NEAREST (0
# tokens) -> consolidate -> QUOTE CHECK -> RELATION GATE (0 tokens) -> optional triage stage.
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
# STAGES: KHMS_AGENT (headless CLI, called as `<agent> -p <prompt> --model M --allowedTools ...`),
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
AGENT_BIN=$(command -v -- "$AGENT" 2>/dev/null || true)
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
# THE SWEEP'S OVERALL DEADLINE (K209H, REV209 F8), minutes after the run's start. Until then a
# multi-part sweep had no total bound: K parts x 3 attempts x SWEEP_TIMEOUT, on a service with no
# timeout. No sweep part and no retry attempt STARTS after it, and a started attempt is clipped to
# it (RETRY_DEADLINE_EPOCH in nightly_retry.sh). 150: the single-call sweep measured 9–38 min, so
# a healthy night is far inside it, and it leaves the consolidate budget (CARD_BUDGET_MIN, +180)
# half an hour of its own. A sweep stopped by it FAILS like any other (no stamp, window kept; since
# #173 its finished parts are KEPT and the next run continues the frozen list -- see THE FROZEN PREPARE
# LIST below), and
# every failed sweep is recorded in KHMS_SWEEP_FAILED, which a morning review ping reads aloud
# until a sweep succeeds — a part that always fails on a growing window surfaces instead of
# looping silently.
SWEEP_BUDGET_MIN=${SWEEP_BUDGET_MIN:-150}
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
# Worst case with BATCH_RETRY_MAX=2: a batch started just before CARD_DEADLINE (+225 since P209M) ends
# by +225+160 = +385 min, triage (TRIAGE_TIMEOUT 120m) by ~+505 = ~11:55 — inside the weekly's wait,
# which starts at 04:00 (+30) and lasts WEEKLY_NIGHT_WAIT_MIN 480 min, i.e. until +510 (12:00).
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
#     OPEN marks are the queue the MARK stage works from, oldest first, as far as the night's
#     card budget (MARK_PENDING_MAX + CARD_BUDGET_MIN below) reaches;
#   * the MARK stage: queued marks the sweep did not cover go to the model in chunks;
#   * the CARRY QUEUE (memory/ledger/carry/): consolidate runs in batches, every batch
#     file stays in the carry queue until its output is merged into the inbox, so a batch
#     the night did not reach (budget, failure, kill) is what the next night does FIRST.
# The night's TIME is the only bound, and what does not fit is carried, never dropped.
LEDGER_TOOL=$CODE/mark_ledger.py
BATCH_TOOL=$CODE/nightly_batch.py
CARRY=$ROOT/memory/ledger/carry
# MARK stage sizing — an ESTIMATE until the first uncapped night measures it: the sweep
# turned 31–137 candidates out of a whole digest in 9–38 min (3.5–5.7 candidates/min,
# nightly.log 2026-09-20..10-01); a chunk of 60 one-line marks is therefore ~15–20 min,
# and 40m is 2x that. Chunks not started before the budget stay OPEN in the ledger.
# MEASURED (P209N): the only night the stage ran (2026-10-03, 3 chunks of 60) took 10.1, 10.6 and
# 14.1 min -> 30m (rule above). On the other four uncapped nights it ran NO chunk: the sweep alone
# put 138-157 candidates over MARK_PENDING_MAX (see the backpressure note below).
MARK_CHUNK=${MARK_CHUNK:-60}
MARK_TIMEOUT=${MARK_TIMEOUT:-30m}
# Wall-clock budgets, minutes after the run's start (03:30): no NEW mark chunk after
# +90 (05:00), no NEW consolidate batch after +225 (07:15) — a started one is bounded by
# its own timeout. Triage (31–59 min measured, P209N) runs after, so a full night ends by ~08:30.
# CARD_BUDGET_MIN was 180 until P209M (#209, reviewed budget increase: the night may write
# more cards). Sized with MARK_PENDING_MAX below: the consolidation of MARK_PENDING_MAX candidates
# (9 batches of 25) at the measured 12.5-19.6 min per batch (P209N: 75-139 min for 6-9 batches)
# takes ~115-175 min; it starts after the sweep (12-26 min) and the MARK stage (chunks 10-14 min),
# i.e. at ~+65..+90, and its last batch must still be allowed to START: +225. A night that does
# not finish its batches carries them, and the carried candidates hold the next night's MARK
# stage back (backpressure) — the budget regulates itself instead of piling up carry.
MARK_BUDGET_MIN=${MARK_BUDGET_MIN:-90}
CARD_BUDGET_MIN=${CARD_BUDGET_MIN:-225}
# Attempts per batch/chunk (the sweep keeps nightly_retry.sh's default 3): a batch that
# fails twice is carried, and the next one is tried rather than spending the night on it.
BATCH_RETRY_MAX=${BATCH_RETRY_MAX:-2}
# BACKPRESSURE: the MARK stage only produces candidates while fewer than this many are
# already waiting for consolidation (carry queue + tonight's sweep). Without it a large
# backlog (1,893 OPEN marks since 2026-09-21 when this was built) would turn into an
# ever-growing carry queue — candidates nobody consolidates — and the OPEN count would
# shrink while the real backlog only moved. 125 = 5 batches = what ~100 min of
# consolidation at the measured ~1.2 cards/min gets through. The marks it does not reach
# stay OPEN, visibly, in the ledger.
# RAISED 125 -> 225 (P209M, #209, owner 2026-10-06 K-93929 remedy b). MEASURED (P209N, nights
# 10-02..10-06): the sweep ALONE yields 52-157 candidates (62, 68, 138, 52, 157), so at 125 the
# stage was skipped on 4 of 5 nights while the OPEN queue grew 2740 -> 4387. A MARK chunk of 60
# marks yields 13-28 candidates (10-03: 13, 28, 21) and accounts for all 60 ids (candidates or
# NOT CARDED verdicts). At 225 = 9 batches, a night with the largest measured sweep (157) still
# runs 4 chunks (157 -> ~178 -> ~199 -> ~220 -> stop) = ~240 marks; a 60-candidate night runs
# chunks until the MARK budget (+90) stops it. COST, measured per unit on the same nights
# (P209N per-stage table, tok_account): a MARK chunk ~$1.8 (10-03: $5.46 / 3), a consolidate batch
# ~$2.4-3.3 ($14.28/6, $19.90/7, $28.99/9, $22.77/7) — i.e. ~$4-5 per 60 marks taken off the queue,
# and ~+$14-17 a night at 4 chunks + 3 extra batches over the 125/180 setting ($30-39 measured).
MARK_PENDING_MAX=${MARK_PENDING_MAX:-225}

mkdir -p "$STAGING"
# The stage prompts: prompts/<name>.md with their {{...}} fields and private extension slots filled
# (khms_prompt.py), rendered ONCE before anything is written. A stage without its instructions
# produces nothing a review could trust, so a prompt that cannot be rendered stops the night here,
# loudly and unstamped.
render_prompt() { python3 "$CODE/khms_prompt.py" "$1" 2>>"$LOG"; }
if ! SWEEP_BASE=$(render_prompt nightly-sweep) || ! MARKS_BASE=$(render_prompt nightly-marks) \
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
MARK_DEADLINE=$((RUN_START_EPOCH + MARK_BUDGET_MIN * 60))
SWEEP_DEADLINE=$((RUN_START_EPOCH + ${SWEEP_BUDGET_S:-$((SWEEP_BUDGET_MIN * 60))}))   # _S: test seam
CARD_DEADLINE=$((RUN_START_EPOCH + CARD_BUDGET_MIN * 60))
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
  echo "$(date -Is) know-how ($1): ${ids:-0} MARK id(s) and ${cands:-0} candidate(s) dropped as not know-how (K-93929)" >> "$LOG"
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
run_triage_stage() {
  if [ ${#TRIAGE_CMD[@]} -gt 0 ]; then
    "${TRIAGE_CMD[@]}" >> "$LOG" 2>&1 \
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

# THE FROZEN PREPARE LIST (#173; first night on the shared code, 2026-10-11). A window whose sweep has
# more parts than one night's SWEEP_BUDGET_MIN covers (that night: 42 one-chunk parts at 6-7 min, cut by
# the 150-min deadline at part 23) used to be thrown away whole: nothing kept, nothing stamped, and the
# next window two nights long -- the same failure every night on a growing window. Now the run that
# collects a window FREEZES its list of prepare inputs before the first part starts:
#   $STAGING/.prepare-<SK>/FROZEN   since=, end= (the window), group_chunks=, parts=, index_sha256=
#                                   (sha256 of nightly-<SK>-prepare-chunks.txt, the ordered input list);
#   $STAGING/.prepare-<SK>/done-NNN one COMPLETION RECORD per finished part (written by rename after the
#                                   part's call and its reassembly): group_sha256 of its input list and
#                                   output_sha256 of nightly-<SK>-sweep-gNN.md ("absent" when none).
# SK is the night key of the run that collected the window; its staging inputs (digest, git log, chunks,
# part lists) stay where that run wrote them. The NEXT run finds the list here and, instead of collecting
# a new window, runs only the parts without a valid record -- a part killed mid-call has none and is
# redone from nothing, a finished part is never run twice -- and builds the sweep file from the part
# outputs in order. The stamp moves to the frozen END only when every part is complete and the window is
# committed to the carry queue (step 2e), so no input of the window is ever skipped; the run after that
# collects the next window from there. A list the stamp already covers is removed; a damaged one is set
# aside and its window collected again from the (unmoved) stamp.
SK=$NK
WINDOW_END=$RUN_START
RESUME=""
sha_of() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1; }
frozen_get() { sed -n "s/^$2=//p" "$1/FROZEN" 2>/dev/null | head -1; }
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
QUEUE_N=$(grep -c '^- M-' "$QUEUE" 2>/dev/null) || QUEUE_N=0
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
  printf 'sweep: timeout %s per call; deadline: no sweep part or retry attempt starts after %s (+%s s); failed sweeps so far: %s\n' \
    "$SWEEP_TIMEOUT" "$(date -d @"$SWEEP_DEADLINE" +%H:%M)" "$((SWEEP_DEADLINE - RUN_START_EPOCH))" \
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
  echo "  per batch: timeout $CONSOLIDATE_TIMEOUT, $BATCH_RETRY_MAX attempt(s); no new batch after $(date -d @"$CARD_DEADLINE" +%H:%M)"
  echo "  INPUTS block of a batch (verbatim shape):"
  consolidate_inputs "$CARRY/$NK/cons-01.md" "$NK" 1 "$STAGING/nightly-consout-$NK-01.md" | sed 's|^|    |'
  echo
  echo "--- MARK LEDGER / MARK stage (board #209) ---"
  ledger sync --dry 2>&1 | sed 's|^|  |' || true
  echo "  queue: $QUEUE_N OPEN mark(s) not held by any pending proposal -> $QUEUE"
  # `|| true`: an EMPTY queue is a normal night; under `set -o pipefail` grep's rc 1 used to end
  # the dry run here with rc 1, before the MARK-stage limits, quote sources and triage were printed.
  { grep -m3 '^- M-' "$QUEUE" || true; } | cut -c1-160 | sed 's|^|    |'
  echo "  MARK stage: chunks of $MARK_CHUNK, timeout $MARK_TIMEOUT, $BATCH_RETRY_MAX attempt(s), no new chunk after $(date -d @"$MARK_DEADLINE" +%H:%M) or once >= $MARK_PENDING_MAX candidates wait for consolidation"
  echo "  grounding journal source (window + every journal from $LFLOOR): $JVERIFY ($(stat -c%s "$JVERIFY") B)"
  echo "  know-how test (K-93929): consolidate prompt $(grep -c 'not know-how' "$CODE/prompts/nightly-consolidate.md" || true) line(s), MARK prompt $(grep -c 'not know-how' "$CODE/prompts/nightly-marks.md" || true) line(s) name the drop reason; each output's drops are logged as 'know-how (<stage>): …'"
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
  echo "--- agent: ${AGENT_BIN:-NOT EXECUTABLE ($AGENT) — the model stages would FAIL}; models sweep=$SWEEP_MODEL marks=$MARK_MODEL consolidate=$CONS_MODEL"
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
SWEEP_PROMPT="$SWEEP_BASE

INPUTS:
- No primary source chunks available. Produce no candidates.
OUTPUT FILE: $SWEEP"
# reassemble <target> <part files...> — the merge described at 2a, as a function since #209
# follow-up: every sweep PART has its own part files too.
reassemble() {
  python3 - "$@" <<'PYMERGE' >> "$LOG" 2>&1 || echo "$(date -Is) reassembly of ${1##*/} FAILED (non-fatal)" >> "$LOG"
import re, sys, pathlib
target, parts = pathlib.Path(sys.argv[1]), [pathlib.Path(p) for p in sys.argv[2:]]
CARD = re.compile(r"^#{2,3} +([A-Z]\d+[a-z]?)\b", re.M)
text = target.read_text(encoding="utf-8") if target.exists() else ""
have, added = set(CARD.findall(text)), 0
for p in sorted(parts):
    if not p.exists():
        # nullglob removes unmatched GLOBS, but the -final.md element is a
        # literal and survives when absent (crashed 2026-08-24: single-file
        # sweep night, FileNotFoundError). Missing part = nothing to merge.
        continue
    t = p.read_text(encoding="utf-8")
    marks = list(CARD.finditer(t))
    for i, m in enumerate(marks):
        if m.group(1) in have:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(t)
        text += ("\n" if text and not text.endswith("\n") else "") + t[m.start():end]
        have.add(m.group(1)); added += 1
if added:
    target.write_text(text, encoding="utf-8")
print(f"reassembly: +{added} cards from {len(parts)} part file(s); sweep now holds {len(have)} candidates")
PYMERGE
}
SWEEP_OK=1
FAILED_STAGES=""
if [ -z "$AGENT_BIN" ]; then
  # No model can run tonight: the sweep FAILS (no stamp, the window is swept again), queued marks
  # stay OPEN and carried batches stay in the carry queue; only work that needs no model call
  # (publishing an already durable batch output) still happens below.
  SWEEP_OK=0
  FAILED_STAGES="$FAILED_STAGES agent-unavailable"
  echo "$(date -Is) AGENT UNAVAILABLE: '$AGENT' (KHMS_AGENT) is not an executable command — every model stage of this night FAILS; nothing is stamped" >> "$LOG"
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
  # PREPARE LIST above). Part k writes nightly-<SK>-sweep-gNN.md (and reassembles its own -part files
  # into it); its completion record is renamed into place only after that, so a part killed at any
  # point before it has no record and starts again from nothing next time. A part with a valid record
  # is skipped. Only when EVERY part of the list is complete is $SWEEP built -- from scratch, part 1 as
  # it is, part k merged with its labels moved to k000+ (a clash is renamed by nightly_batch.py merge,
  # never dropped) -- so a repeated build gives the same file. The first failing part (call, deadline,
  # usage limit) stops the loop; what finished stays finished for the next run.
  _K=${#SWEEP_GROUP_FILES[@]}
  PREP_DIR=$STAGING/.prepare-$SK
  if [ -z "$RESUME" ]; then
    mkdir -p "$PREP_DIR"
    printf 'since=%s\nend=%s\ngroup_chunks=%s\nparts=%s\nindex_sha256=%s\n' "$SINCE" "$WINDOW_END" \
      "$SWEEP_GROUP_CHUNKS" "$_K" "$(sha_of "$SWEEP_SOURCE_INDEX")" > "$PREP_DIR/FROZEN.tmp"
    mv "$PREP_DIR/FROZEN.tmp" "$PREP_DIR/FROZEN"
    echo "$(date -Is) prepare: input list frozen in $PREP_DIR ($_K part(s), window $SINCE -> $WINDOW_END)" >> "$LOG"
  fi
  part_out() { echo "$STAGING/nightly-$SK-sweep-g$(printf %02d "$1").md"; }
  part_complete() {   # part_complete <record> <part list> <output> -> 0 when the record proves the part done
    local o=absent
    [ -f "$1" ] || return 1
    [ "$(sed -n 's/^group_sha256=//p' "$1")" = "$(sha_of "$2")" ] || return 1
    if [ -e "$3" ]; then o=$(sha_of "$3"); fi
    [ "$(sed -n 's/^output_sha256=//p' "$1")" = "$o" ]
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
      echo "$(date -Is) sweep part $_k/$_K NOT STARTED — the sweep deadline $(date -d @"$SWEEP_DEADLINE" +%H:%M) (+${SWEEP_BUDGET_MIN} min) has passed; the sweep is incomplete" >> "$LOG"
      break
    fi
    # no valid record: whatever an earlier, killed attempt left is not this part's output
    rm -f "$_rec" "$_gout" "${_gout%.md}-part"*.md "${_gout%.md}-final.md"
    echo "$(date -Is) sweep part $_k/$_K: $(grep -c . "$_gf") chunk(s) -> $_gout" >> "$LOG"
    if ! RETRY_DEADLINE_EPOCH=$SWEEP_DEADLINE retry_claude_call "sweep-$_k" "$SWEEP_TIMEOUT" "$LOG" "$STAGE_MARK" -- \
         "$AGENT" -p "$SWEEP_BASE

$(sweep_group_inputs "$_k" "$_K" "$_gf" "$_gout")" "${STAGE_ISOLATION[@]}" --model "$SWEEP_MODEL" --allowedTools $TOOLS; then
      SWEEP_OK=0
      echo "$(date -Is) sweep part $_k/$_K FAILED — the sweep is incomplete; parts after it are not started" >> "$LOG"
    fi
    python3 "$CODE/maxtok_check.py" --stage "sweep-$_k" --since "$STAGE_MARK" \
      --projects "$PROJ_DIR" >> "$LOG" 2>&1 || true
    [ "$SWEEP_OK" = "1" ] || break
    shopt -s nullglob
    _gparts=("${_gout%.md}-part"*.md "${_gout%.md}-final.md")
    shopt -u nullglob
    if [ ${#_gparts[@]} -gt 0 ]; then reassemble "$_gout" "${_gparts[@]}"; fi
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
    # every part is complete: build the sweep file from the part outputs, in order, from nothing
    : > "$SWEEP.tmp"
    for _k in $(seq 1 "$_K"); do
      _gout=$(part_out "$_k")
      if [ ! -s "$_gout" ]; then
        echo "$(date -Is) sweep part $_k/$_K produced no candidates" >> "$LOG"
      elif [ "$_k" = "1" ]; then
        cp "$_gout" "$SWEEP.tmp"
      elif ! python3 "$BATCH_TOOL" merge "$SWEEP.tmp" "$_gout" --relabel-base $((_k * 1000)) >> "$LOG" 2>&1; then
        SWEEP_OK=0
        echo "$(date -Is) sweep part $_k/$_K merge FAILED — its candidates are not in $SWEEP, so the sweep is incomplete (the parts stay complete; the next run builds the sweep file again)" >> "$LOG"
        break
      fi
    done
    if [ "$SWEEP_OK" = "1" ]; then mv "$SWEEP.tmp" "$SWEEP"; else rm -f "$SWEEP.tmp"; fi
  fi
else
RETRY_DEADLINE_EPOCH=$SWEEP_DEADLINE retry_claude_call sweep "$SWEEP_TIMEOUT" "$LOG" "$STAGE_MARK" -- \
  "$AGENT" -p "$SWEEP_PROMPT" "${STAGE_ISOLATION[@]}" --model "$SWEEP_MODEL" --allowedTools $TOOLS || SWEEP_OK=0
# max_tokens guard over the LAST attempt (STAGE_MARK is re-stamped by each attempt).
# Report, not gate: `|| true` keeps a check that is meant to be non-fatal from killing the run
# under `set -e` (already paid for once, with weeks of lost nights).
python3 "$CODE/maxtok_check.py" --stage sweep --since "$STAGE_MARK" \
  --projects "$PROJ_DIR" >> "$LOG" 2>&1 || true
fi

if [ "$SWEEP_OK" -ne 1 ]; then
  FAILED_STAGES="$FAILED_STAGES sweep"
  # One line per failed sweep; read aloud by a morning review ping, removed on success.
  echo "$RUN_START night=$NK window-since=$SINCE parts=${#SWEEP_GROUP_FILES[@]}${_kept:+ kept=$_kept} frozen=${PREP_DIR:-none} digest=${SIZE}B${SOURCE_FAILED:+ sources-failed=$SOURCE_FAILED}" >> "$SWEEP_FAILED_FILE"
  _nfail=$(grep -c . "$SWEEP_FAILED_FILE" 2>/dev/null || echo 1)
  if [ "$_nfail" -ge 2 ]; then
    echo "$(date -Is) SWEEP FAILED ${_nfail} NIGHTS IN A ROW (first: $(head -1 "$SWEEP_FAILED_FILE" | cut -d' ' -f1)) — the window since $SINCE keeps growing; a part that fails every night needs a human, see $SWEEP_FAILED_FILE" >> "$LOG"
  fi
  if [ -n "${PREP_DIR:-}" ] && [ -f "$PREP_DIR/FROZEN" ]; then
    echo "$(date -Is) sweep FAILED after retries — the window is not complete, so it is not consolidated tonight; ${_kept:-0} of ${#SWEEP_GROUP_FILES[@]} part(s) are KEPT in $PREP_DIR and the next run continues that frozen list (not stamped)" >> "$LOG"
  else
    echo "$(date -Is) sweep FAILED after retries — no candidates produced, consolidate skipped for tonight (see the retry lines above for what was tried); not stamped, so the next run's window still covers tonight" >> "$LOG"
  fi
else
if [ -s "$SWEEP_FAILED_FILE" ]; then
  echo "$(date -Is) sweep OK — clearing the record of $(grep -c . "$SWEEP_FAILED_FILE") earlier failed sweep(s) ($SWEEP_FAILED_FILE)" >> "$LOG"
fi
rm -f "$SWEEP_FAILED_FILE"

# 2a) REASSEMBLY. On a day too big for one pass the sweep splits its own output into part files and
# numbers the chunks A1.., B1.., C1.. — but it is launched with `--allowedTools Read Write Edit Glob
# Grep` (no Bash) and therefore CANNOT run the merge it writes for itself. Once that left 221 candidates
# scattered across five parts with the "final" file holding only two of the chunks (77 cards), so
# consolidating from it would have silently dropped 144 candidates. The driver has Bash; it does the
# merge. Parts are appended in lexical order AFTER whatever the sweep put in $SWEEP, and only cards
# whose temp label is not already present are taken, so a part that was already merged by hand — or
# by the sweep's own merge.py — cannot be duplicated.
shopt -s nullglob
PARTS=("$STAGING/nightly-$SK-sweep-part"*.md "$STAGING/nightly-$SK-sweep-final.md")
shopt -u nullglob
if [ ${#PARTS[@]} -gt 0 ]; then
  reassemble "$SWEEP" "${PARTS[@]}"
fi

# 2b) mechanical grounding check — non-fatal, its report is an INPUT to consolidate.
# Deliberately not fatal: a sweep with some ungrounded cards is still worth consolidating, and the
# consolidate stage is instructed to strip exactly what this report names.
# `|| true` is LOAD-BEARING: verify_quotes.py exits 1 whenever it finds any
# failure, and this script runs under `set -euo pipefail`. Without it the run
# died here — silently, before consolidation, before the timestamp, and before
# its own "FAILED" line could be written. Every nightly from 2026-07-28 onward
# was lost that way; the comment above already said "deliberately not fatal"
# while the code made it fatal.
# journal= is $JVERIFY since board #209: the window's journals PLUS every journal from the ledger
# floor on, because a MARK-stage candidate quotes a queued mark from its own (older) day.
run_quotecheck() {   # run_quotecheck <file> -> appends to $QREPORT
  python3 "$CODE/verify_quotes.py" "$1" \
    --source digest="$DIGEST" --source journal="$JVERIFY" --source gitlog="$GITLOG" \
    >> "$QREPORT" 2>&1 || true
}

# A prepare candidate must never claim journal grounding, even if a model ignores its prompt.
# Enforce before record-stage candidates join the shared list; legacy carry stays untouched.
python3 - "$SWEEP" <<'PYPREPARE'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
if p.exists():
    blocks = re.split(r"(?m)(?=^#{2,3} +[A-Z]\d+[a-z]?\b)", p.read_text(encoding="utf-8"))
    p.write_text("".join(b for b in blocks if not re.search(r"src=journal\s*::", b)), encoding="utf-8")
PYPREPARE
python3 "$CODE/verify_quotes.py" "$SWEEP" --source digest="$DIGEST" --source gitlog="$GITLOG" \
  >> "$QREPORT" 2>&1 || true

# 2c) THE MARK STAGE (board #209). Queued OPEN journal marks go to the
# model in chunks of $MARK_CHUNK: every id must end in a candidate (`MARKS:` line) or in a
# `## MARKS NOT CARDED` line with a reason. A chunk that fails is NOT merged (a cut-off
# candidate must not drag its ids into a drop) — its marks simply stay OPEN for tomorrow, and so
# do the chunks the budget did not reach. Their outputs are merged into the sweep file, so the
# consolidate batches see one candidate list.
MARKS_PROMPT_FILE=$CODE/prompts/nightly-marks.md
MCHUNKS=()
if [ -f "$MARKS_PROMPT_FILE" ] && [ "$QUEUE_N" -gt 0 ] && [ -n "$AGENT_BIN" ]; then
  mapfile -t MCHUNKS < <(ledger queue --chunk "$MARK_CHUNK" --outdir "$STAGING" \
                           --prefix "nightly-$NK-markchunk" 2>>"$LOG") || MCHUNKS=()
fi
echo "$(date -Is) === marks === ${#MCHUNKS[@]} chunk(s) of <= $MARK_CHUNK queued mark(s) owned by the record stage" >> "$LOG"
MK=0
for MC in "${MCHUNKS[@]}"; do
  [ -f "$MC" ] || continue
  MK=$((MK + 1))
  if limit_stop "MARK chunks $MK..${#MCHUNKS[@]} (their marks stay OPEN)"; then break; fi
  if [ "$(date +%s)" -ge "$MARK_DEADLINE" ]; then
    echo "$(date -Is) === marks === budget of ${MARK_BUDGET_MIN} min reached — $(( ${#MCHUNKS[@]} - MK + 1 )) chunk(s) left OPEN in the ledger for the next night" >> "$LOG"
    break
  fi
  PENDING=$(python3 "$BATCH_TOOL" count "$SWEEP" 2>/dev/null || echo 0)
  for CF in $(carry_batches); do
    PENDING=$((PENDING + $(python3 "$BATCH_TOOL" count "$CF" 2>/dev/null || echo 0)))
  done
  if [ "$PENDING" -ge "$MARK_PENDING_MAX" ]; then
    echo "$(date -Is) === marks === $PENDING candidate(s) already wait for consolidation (>= $MARK_PENDING_MAX) — $(( ${#MCHUNKS[@]} - MK + 1 )) chunk(s) left OPEN in the ledger; consolidation first" >> "$LOG"
    break
  fi
  MOUT=$STAGING/nightly-$NK-markout-$(printf %02d "$MK").md
  MPROMPT="$MARKS_BASE

INPUTS:
- MARK QUEUE chunk: $MC
- journal directory (grep around a mark's journal/<file>:<line> for context): $ROOT/journal
- knowledge base (grep before carding): $ROOT/memory/know
OUTPUT FILE: $MOUT"
  if RETRY_MAX_ATTEMPTS=$BATCH_RETRY_MAX retry_claude_call "marks-$MK" "$MARK_TIMEOUT" "$LOG" "$STAGE_MARK" -- \
       "$AGENT" -p "$MPROMPT" "${STAGE_ISOLATION[@]}" --model "$MARK_MODEL" --allowedTools $TOOLS; then
    if [ -s "$MOUT" ]; then
      python3 "$BATCH_TOOL" merge "$SWEEP" "$MOUT" --letter M --relabel-base $((MK * 1000)) >> "$LOG" 2>&1 \
        || echo "$(date -Is) marks-$MK merge FAILED (non-fatal) — its marks stay OPEN" >> "$LOG"
    else
      # rc 0 and no output is a failed chunk, not a quiet one (PDRAIN: 2 of 63 calls on 2026-10-06
      # printed a delegation and wrote nothing; until then only "coverage: 0/60" in the log said so)
      FAILED_STAGES="$FAILED_STAGES marks-$MK"
      echo "$(date -Is) marks-$MK exited 0 but wrote no output ($MOUT) — FAILED, its $(grep -c '^- M-' "$MC") mark(s) stay OPEN for the next night" >> "$LOG"
    fi
    ledger coverage "$MOUT" --queue "$MC" >> "$LOG" 2>&1 || true
    knowhow_line "marks-$MK" "$MOUT"
  else
    FAILED_STAGES="$FAILED_STAGES marks-$MK"
    echo "$(date -Is) marks-$MK FAILED — output not merged, its $(grep -c '^- M-' "$MC") mark(s) stay OPEN for the next night" >> "$LOG"
  fi
  python3 "$CODE/maxtok_check.py" --stage "marks-$MK" --since "$STAGE_MARK" \
    --projects "$PROJ_DIR" >> "$LOG" 2>&1 || true
done

run_quotecheck "$SWEEP"
echo "$(date -Is) quotecheck: $(tail -1 "$QREPORT")" >> "$LOG"

# 2d) === nearest === — the mechanical half of merge pressure, 0 API tokens.
# Every candidate is annotated with the three ACTIVE cards the retrieval layer
# ranks closest to it, so the consolidate stage is asked "what does this
# replace?" with the answer's candidates already in front of it — instead of
# being asked to link against whatever it happened to read, which is how the
# reference base accumulated 17 cards about one measured voltage.
# NON-FATAL (paid for once): a ranking bug must not cost the night's
# cards. Without NEAREST lines the consolidate stage still runs; only the
# `RELATION: new` check downstream loses its evidence, and step 4a refuses to
# run the gate in that case rather than dropping candidates for the tool's own
# reason.
echo "$(date -Is) === nearest === annotating $SWEEP" >> "$LOG"
python3 "$CODE/nearest_cards.py" "$SWEEP" >> "$LOG" 2>&1 \
  || echo "$(date -Is) === nearest === FAILED (non-fatal) — consolidate runs without NEAREST lines" >> "$LOG"

# 2e) SPLIT INTO THE CARRY QUEUE — ONE ATOMIC COMMIT (review P1-4). Tonight's candidates, the
# sweep's and MARK stage's `## MARKS NOT CARDED` verdicts and tonight's WINDOW are written into
# $CARRY/.split-$SK.tmp and committed by ONE rename to $CARRY/$SK. A kill before the rename leaves
# nothing (the window is not stamped and is swept again); a kill after it leaves everything,
# and the next run reads the window back from $CARRY/$SK/WINDOW. A split failure falls back to
# ONE batch holding the whole sweep file, committed the same way — never to nothing.
SPLIT_OK=1
if [ -s "$SWEEP" ]; then
  SPLIT_LIST=$STAGING/nightly-$SK-split.txt
  _bf=""; [ "${NIGHTLY_FAULT:-}" = "split-before-commit" ] && _bf=split-before-commit
  if NIGHTLY_BATCH_FAULT="$_bf" python3 "$BATCH_TOOL" split-run "$SWEEP" --size "$CONSOLIDATE_BATCH" \
       --carry "$CARRY" --run "$SK" --window "$WINDOW_END" > "$SPLIT_LIST" 2>>"$LOG"; then
    echo "$(date -Is) split: $(python3 "$BATCH_TOOL" count "$SWEEP") candidate(s) -> $(grep -c '/cons-' "$SPLIT_LIST") batch(es) of <= $CONSOLIDATE_BATCH committed in $CARRY/$SK" >> "$LOG"
  else
    fault_point split-before-commit
    if [ -d "$CARRY/$SK" ]; then
      echo "$(date -Is) split reported a failure AFTER its commit — $CARRY/$SK is complete, continuing" >> "$LOG"
    else
      SPLIT_OK=0
      rm -rf "$CARRY/.split-$SK.tmp"
      mkdir -p "$CARRY/.split-$SK.tmp"
      cp "$SWEEP" "$CARRY/.split-$SK.tmp/cons-01.md"
      echo "$WINDOW_END" > "$CARRY/.split-$SK.tmp/WINDOW"
      mv -T "$CARRY/.split-$SK.tmp" "$CARRY/$SK"
      echo "$(date -Is) split FAILED (non-fatal) — the whole sweep file is ONE carried batch in $CARRY/$SK" >> "$LOG"
    fi
  fi
fi

# THE WINDOW IS PERSISTED NOW, so the stamp moves here (board #209): every candidate of the
# window is in the committed run directory, and the queue lives in the ledger. Only when the
# commit really happened (or there was nothing to commit) — a stamp over an uncommitted window
# is exactly the loss this ordering exists to prevent. A failed SWEEP leaves the stamp alone.
# The window's END is this run's start -- or, for a resumed frozen list, the start of the run that
# collected it (WINDOW_END), so the next window begins exactly where the frozen one ended. Only now is
# the frozen list done with: its parts are committed with the window.
if [ "$DRYRUN" != "1" ] && { [ -d "$CARRY/$SK" ] || [ ! -s "$SWEEP" ]; }; then
  echo "$WINDOW_END" > "$STAMP"
  if [ -d "$STAGING/.prepare-$SK" ]; then
    rm -rf "$STAGING/.prepare-$SK"
    echo "$(date -Is) prepare: the frozen list $SK is complete and committed to $CARRY/$SK — window stamped at $WINDOW_END" >> "$LOG"
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
postprocess_batch() {
  local f="$1" d="$2" tag="${3//\//-}"
  local dg="$STAGING/nightly-$d-digest.txt" jv="$STAGING/nightly-$d-journals-ledger.md"
  local gl="$STAGING/nightly-$d-git.txt" qr="$STAGING/nightly-$NK-quotecheck-$tag.txt"
  local rr="$STAGING/nightly-$NK-relations-$tag.txt"
  [ -f "$jv" ] || jv="$STAGING/nightly-$d-journals.md"
  [ -f "$dg" ] || { dg=$DIGEST; gl=$GITLOG; jv=$JVERIFY; }
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
CB=0; CB_DONE=0; CB_LEFT=0; CB_LIMIT=0
PUBLISHED_UNITS=()
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
    echo "$(date -Is) consolidate budget of ${CARD_BUDGET_MIN} min reached — $CB_LEFT batch(es) stay in $CARRY for the next night" >> "$LOG"
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
       "$AGENT" -p "$CPROMPT" "${STAGE_ISOLATION[@]}" --model "$CONS_MODEL" --allowedTools $TOOLS; then
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
# stamp (never backwards) and the directory goes.
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
# night: it writes its own file (memory/inbox/DATE-triage.md), takes no night lock (it holds the
# clone's code-use lock, nested in this run's), and run_triage_stage keeps it non-fatal under
# `set -euo pipefail`. Unconfigured, the log says so. A quiet night runs it too (above).
run_triage_stage

# THE STAMP (board #209): written right after the sweep's candidates were persisted in the carry
# queue (step 2e), never when the SWEEP failed — then the window stays open so the NEXT run's
# SINCE still covers tonight. A failed MARK chunk leaves its marks OPEN in the ledger and a
# failed consolidate batch stays in the carry queue: both are tomorrow's work, neither is lost.
if [ -n "$FAILED_STAGES" ]; then
  if [ "$SWEEP_OK" -ne 1 ]; then
    WHERE="not stamped, so the next run's window still covers this one"
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
