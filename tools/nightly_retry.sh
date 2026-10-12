#!/usr/bin/env bash
# nightly_retry.sh — shared retry-with-backoff wrapper for the nightly pipeline's model
# calls. Sourced by nightly_distill.sh (and by its own tests); not meant to be run
# standalone.
#
# WHY (owner, board #170, 2026-09-22): the sweep stage's `claude -p ...` call got one
# API 500 from the far side, the CLI exited nonzero, and nightly_distill.sh's
# `[ "$rc" -eq 0 ] || exit 1` killed the ENTIRE night on that single transient error —
# no consolidate, no triage, nothing in the log but "sweep FAILED (rc=1)". A one-shot
# outage on the far side must not cost a whole night of the record. This is UNRELATED to
# the OOM-kill class of failure (rc=137, board #147) — that is a resource problem, not a
# transient-error problem, and retrying it would only thrash the machine again; this
# wrapper retries on ANY nonzero exit because the CLI does not expose a machine-readable
# "this was transient" signal, and a fixed, bounded number of attempts with a growing
# delay is safe either way (a truly broken stage still fails, just after the budget below
# instead of on the first blip).
#
# retry_claude_call <stage-name> <per-attempt-timeout> <log-file> <stage-mark-file> -- <cmd...>
#   Runs <cmd...> under `timeout --signal=TERM --kill-after=60s <per-attempt-timeout>`,
#   up to RETRY_MAX_ATTEMPTS times (default 3), with a GROWING delay between attempts
#   (RETRY_BASE_DELAY_S, default 30s, tripled each retry: 30s, 90s, ...). Every attempt's
#   start re-stamps <stage-mark-file> (nightly_distill.sh's max_tokens guard reads
#   "since" it, so it always checks the attempt that actually ran).
#
#   Returns 0 the moment an attempt exits 0. After the LAST attempt still fails, returns
#   that attempt's exit code and does NOT exit or abort anything itself — every line
#   (per-attempt retry note, and the final FAILED line) goes to <log-file>, so a run that
#   exhausts every attempt still says so in its own output rather than dying silently. The
#   CALLER decides what "still failed" means for the rest of the pipeline (skip the
#   dependent stage, keep running the independent ones — see nightly_distill.sh).
# _retry_clip <timeout spec> <seconds left> -> the smaller of the two, as a timeout(1) spec.
_retry_clip() {
  local spec="$1" left="$2" n unit secs
  n="${spec%[smhd]}"; unit="${spec#"$n"}"
  case "$unit" in m) secs=$((n * 60)) ;; h) secs=$((n * 3600)) ;; d) secs=$((n * 86400)) ;; *) secs=$n ;; esac
  if [ "$secs" -gt "$left" ] 2>/dev/null; then echo "${left}s"; else echo "$spec"; fi
}
# THE ACCOUNT'S USAGE LIMIT IS NOT TRANSIENT (PDRAIN, #209; P209N review A-4). On 2026-10-04 the
# weekly limit answered every call in ~3 s with "You've hit your weekly limit · resets Oct 8, 3am";
# the wrapper still slept and retried every stage, every remaining batch was attempted twice, and the
# triage judge made 84 failing calls. A limit message now ends the stage at once with rc 75, and when
# RETRY_LIMIT_FLAG names a file (nightly_distill.sh sets one per night) the first hit writes it and
# every later retry_claude_call of the run returns 75 WITHOUT calling the model.
RETRY_LIMIT_RE=${RETRY_LIMIT_RE:-"hit your [a-z0-9 -]*limit|usage limit reached"}
RETRY_RC_LIMIT=75
retry_claude_call() {
  local stage="$1" tmo="$2" log="$3" stage_mark="$4"
  shift 4
  [ "${1:-}" = "--" ] || { echo "retry_claude_call: expected -- before the command" >&2; return 2; }
  shift
  local max_attempts="${RETRY_MAX_ATTEMPTS:-3}"
  local delay="${RETRY_BASE_DELAY_S:-30}"
  local attempt=1 rc=0 left
  # RETRY_DEADLINE_EPOCH (K209H, REV209 F8): an overall wall-clock bound for the caller's stage.
  # No attempt STARTS after it, and a started attempt's timeout is clipped to it, so a stage made
  # of several calls (the multi-part sweep) cannot outlive its budget by K x attempts x timeout.
  # Unset = the old contract exactly.
  local deadline="${RETRY_DEADLINE_EPOCH:-}"
  local flag="${RETRY_LIMIT_FLAG:-}" out
  while :; do
    if [ -n "$flag" ] && [ -s "$flag" ]; then
      echo "$(date -Is) $stage NOT STARTED (attempt $attempt/$max_attempts): the account's usage limit was hit earlier in this run ($(head -c 200 "$flag"))" >> "$log"
      return "$RETRY_RC_LIMIT"
    fi
    if [ -n "$deadline" ]; then
      left=$(( deadline - $(date +%s) ))
      if [ "$left" -le 0 ]; then
        echo "$(date -Is) $stage NOT STARTED (attempt $attempt/$max_attempts): the stage deadline $(date -d @"$deadline" +%H:%M) has passed" >> "$log"
        return 124
      fi
      tmo=$(_retry_clip "$tmo" "$left")
    fi
    : > "$stage_mark"
    rc=0
    out=$(mktemp "${TMPDIR:-/tmp}/retry-out.XXXXXX")
    timeout --signal=TERM --kill-after=60s "$tmo" "$@" > "$out" 2>&1 || rc=$?
    # RETRY_RESULT_TOOL (#232): when set (nightly_distill.sh sets agent_result.py), every attempt's output is
    # logged through it -- the call's answer, then ONE `<stage> served-by: <model>` line read from the CLI's
    # JSON result (UNKNOWN when there is none), appended to RETRY_SERVED_FILE too. Unset = logged verbatim.
    if [ -n "${RETRY_RESULT_TOOL:-}" ] && [ -f "$RETRY_RESULT_TOOL" ]; then
      python3 "$RETRY_RESULT_TOOL" --stage "$stage" --attempt "$attempt/$max_attempts" --rc "$rc" \
        ${RETRY_SERVED_FILE:+--served-file "$RETRY_SERVED_FILE"} "$out" >> "$log" 2>&1 || cat "$out" >> "$log"
    else
      cat "$out" >> "$log"
    fi
    if [ "$rc" -ne 0 ] && grep -qiE "$RETRY_LIMIT_RE" "$out"; then
      echo "$(date -Is) $stage USAGE LIMIT on attempt $attempt/$max_attempts (rc=$rc): $(grep -m1 -iE "$RETRY_LIMIT_RE" "$out" | head -c 200) — not retried$([ -n "$flag" ] && echo "; no further model call starts in this run ($flag)")" >> "$log"
      if [ -n "$flag" ]; then echo "$(date -Is) $stage: $(grep -m1 -iE "$RETRY_LIMIT_RE" "$out" | head -c 200)" > "$flag"; fi
      rm -f "$out"
      return "$RETRY_RC_LIMIT"
    fi
    rm -f "$out"
    if [ "$rc" -eq 0 ]; then
      if [ "$attempt" -gt 1 ]; then
        echo "$(date -Is) $stage RECOVERED on attempt $attempt/$max_attempts after a transient failure" >> "$log"
      fi
      return 0
    fi
    if [ "$attempt" -ge "$max_attempts" ]; then
      echo "$(date -Is) $stage FAILED after $attempt/$max_attempts attempts (rc=$rc$([ "$rc" -eq 124 ] && echo ' = TIMEOUT'))" >> "$log"
      return "$rc"
    fi
    echo "$(date -Is) $stage attempt $attempt/$max_attempts FAILED (rc=$rc) — retrying in ${delay}s (transient-failure retry)" >> "$log"
    sleep "$delay"
    delay=$((delay * 3))
    attempt=$((attempt + 1))
  done
}
