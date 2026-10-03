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
  while :; do
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
    timeout --signal=TERM --kill-after=60s "$tmo" "$@" >> "$log" 2>&1 || rc=$?
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
