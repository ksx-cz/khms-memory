#!/usr/bin/env bash
# khms_review_ping.sh -- the morning review ping.
# When the night/week pipeline has left unreviewed proposals in memory/inbox/, or a scheduled run did not
# finish, tell the agent that does the review, in one message. Run it from the deployment's scheduler after
# the night (it waits for a night that is still running, see below).
#
# WHERE THINGS ARE (khms_paths.sh): the base KHMS_ROOT; review markers KHMS_SHARED_STATE/reviewed; stamps
# KHMS_NIGHT_STAMP / KHMS_WEEK_STAMP; failed sweeps KHMS_SWEEP_FAILED.
# DELIVERY (optional) -- KHMS_PING_SEND: argv that reads the ping text on stdin and delivers it. rc 0 =
#   delivered, rc 3 = the recipient is not there (logged, not an error), anything else = a failed delivery.
#   For a terminal agent in tmux: `khms_ping_tmux.sh <session>`. Empty = the text is only printed.
# PROJECT LINES (optional) -- KHMS_PING_EXTRA: argv run with no input; every non-empty stdout line is one
#   project alarm that rides in the same message, and a project with nothing to say prints nothing. A
#   command that fails is an alarm itself (a check that did not answer looks like a check that found nothing).
# WORDING: the PING_* keys of khms_messages.py (a deployment's messages file may override them).
# Usage: khms_review_ping.sh [--dry-run]   (KHMS_PING_DRYRUN=1 is the same: print, never deliver)
set -uo pipefail

CODE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=khms_paths.sh
. "$CODE/khms_paths.sh"
khms_load || exit $?
BRAIN="$(khms_get KHMS_ROOT)"
INBOX="$BRAIN/memory/inbox"
MARKERS="$(khms_get KHMS_SHARED_STATE)/reviewed"
STAMP="$(khms_get KHMS_NIGHT_STAMP)"
WSTAMP="$(khms_get KHMS_WEEK_STAMP)"
SFAIL="$(khms_get KHMS_SWEEP_FAILED)"
PRINCIPAL="$(khms_get KHMS_PRINCIPAL)"
read -r -a PING_SEND <<< "$(khms_get KHMS_PING_SEND)"
read -r -a PING_EXTRA <<< "$(khms_get KHMS_PING_EXTRA)"
M() { python3 "$CODE/khms_messages.py" get "$@"; }
TODAY=${KHMS_PING_TODAY:-$(date +%F)}
# What a running contradiction sweep looks like in the process table. Overridable so the
# test can point it at its own stub instead of pattern-matching the live machine.
SWEEP_PATTERN=${KHMS_PING_PGREP:-khms_contradict.py}

# --- IS THE NIGHT STILL RUNNING? (K209H, #209; REV209 F2) ----------------------------------
# Until 2026-10-02 this script ran at 04:45 and judged the inbox as if the night were over. With
# the caps gone the night runs to ~07:00, so on 2026-10-02 it asked for the review of the first
# three of nine units and said "the sweep did not finish" about a triage that had not started yet.
# It now asks the SAME source the SessionStart hook asks (nightly_state.py — the nightly's
# flock and its holder line; one function, not a copy) and, while the night runs, WAITS for it
# (KHMS_PING_NIGHT_WAIT_MIN, polled every KHMS_PING_POLL_S). A night still running at the
# deadline is SAID, and what it wrote since its start is named as in progress, never as the review.
# A dry run does not wait unless asked: an agent checking the ping mid-night needs the answer now.
NS="$CODE/nightly_state.py"
if [ "${1:-}" = "--dry-run" ] || [ "${KHMS_PING_DRYRUN:-}" = "1" ]; then
  NIGHT_WAIT_MIN=${KHMS_PING_NIGHT_WAIT_MIN:-0}
else
  NIGHT_WAIT_MIN=${KHMS_PING_NIGHT_WAIT_MIN:-240}
fi
NIGHT_POLL_S=${KHMS_PING_POLL_S:-60}
night_state() {
  NIGHT_LINE="$(python3 "$NS" --brain "$BRAIN" status 2>&1)"
  NIGHT_RC=$?
}
night_state
if [ "$NIGHT_RC" = 0 ] && [ "$NIGHT_WAIT_MIN" -gt 0 ] 2>/dev/null; then
  echo "$(date -Is) nightly still running ($NIGHT_LINE) — the review ping waits up to ${NIGHT_WAIT_MIN} min for it"
  _deadline=$(( $(date +%s) + NIGHT_WAIT_MIN * 60 ))
  while [ "$NIGHT_RC" = 0 ] && [ "$(date +%s)" -lt "$_deadline" ]; do
    sleep "$NIGHT_POLL_S"
    night_state
  done
  echo "$(date -Is) night state after the wait: $NIGHT_LINE"
fi
NIGHT_RUNNING=0
NIGHT_NOTE=""
WIP=""
if [ "$NIGHT_RC" = 0 ]; then
  NIGHT_RUNNING=1
  read -r _ _ns_epoch NIGHT_HM NIGHT_NK <<< "$NIGHT_LINE"
  # shellcheck disable=SC2046
  WIP="$(cd "$INBOX" 2>/dev/null && python3 "$NS" --brain "$BRAIN" wip "$INBOX" $(ls -- *.md 2>/dev/null))"
  NIGHT_NOTE="$(M PING_NIGHT_RUNNING hm="$NIGHT_HM" nk="${NIGHT_NK:-?}" wip="${WIP:-$(M PING_NONE)}")"
  NIGHT_NOTE="$(printf '%s' "$NIGHT_NOTE" | tr '\n' ' ')"
elif [ "$NIGHT_RC" != 1 ]; then
  NIGHT_NOTE="$(M PING_NIGHT_UNKNOWN line="$NIGHT_LINE")"
fi
is_wip() { [ -n "$WIP" ] && printf '%s\n' "$WIP" | grep -qxF "$1"; }

# --- A TRIAGE FILE THAT JUDGED NOTHING (added 2026-09-20, #149) -------------------------
# Until today this script knew TWO states of the morning triage: the file is missing (said
# out loud, since 2026-09-02) and the file is there (silence, once its review marker exists).
# MEASURED the same day: a sweep can write a complete-looking proposal in which ZERO of the
# planned pairs carry a verdict — the endpoint hung, every pair came back UNANSWERED or was
# never sent at all — and the old proposal header called that "12 pairs adjudicated". Such a
# file passed here in perfect silence, which is the one morning I most need to be woken for:
# the night did not happen and nothing else will say so. (A check whose passing
# condition is met whether or not the guarded thing is healthy is not a check).
#
# So THREE states, and silence is allowed to mean only the third:
#   missing · present but judged nothing · present and judged its plan.
# The number is read from the proposal's own census line, which `khms_triage_report.py` has
# printed since 2026-09-20 and which the triage tests pin to this regex. A file
# without that line (every proposal written before today) yields NOTHING here and is treated
# exactly as it was — an old file must not start ringing a new alarm.
triage_census() {
  sed -nE 's/^Sweep: \*\*([0-9]+) of ([0-9]+) planned pairs were actually judged\*\*.*/\1 \2/p' \
      "$1" 2>/dev/null | head -1
}

# What is pending in the inbox? ANY *.md there is unreviewed by convention: the
# review stage moves processed files to .staging/done-*, so the inbox holds only
# open proposals. Scanning everything (not just $TODAY's files) closes two gaps
# found 2026-07-29: a proposal produced yesterday and never reviewed stopped
# pinging at midnight, and files outside the DATE/DATE-weekly naming (e.g.
# DATE-session-drafts.md from a session wrap-up) fell through what was an
# allowlist of two literal names.
# CORRECTED 2026-09-01. The convention above is not the one the review actually
# follows: nothing moves processed files out of the inbox — the review writes
# a .reviewed-<DATE> marker, which is also what the SessionStart hook reads. The two
# mechanisms therefore disagreed, and this one listed 28 files as "unreviewed"
# on a morning when 25 of them had markers dating back nine days. A pending
# alarm that is wrong nine times out of ten is not read, and this one is the
# only thing standing between a missed nightly and silence.
#
# So: ONE authority for "reviewed", the marker, shared with the hook. Derived
# artefacts (.converted.md, .approve.md) are outputs of the review, never
# proposals, and are skipped — listing them was pure noise.
#
# ONE MARKER PER DATE WAS ONE TOO FEW (measured 2026-09-02). The nightly writes TWO
# independent proposals — the card batch (DATE.md) and the contradiction triage
# (DATE-triage.md) — and both were covered by a single `.reviewed-DATE`. The card review
# touched it, this script fell silent, and `2026-09-02-triage.md` was never read: 0 of its
# verdicts applied, and nothing anywhere said so. So the two are now marked separately
# (`-cards`, `-triage`) and reported separately. The bare `.reviewed-DATE` is still honoured
# for dates BEFORE today, because that is what the nine days of existing markers are.
PENDING=""
DATES="$(for f in "$INBOX"/*.md; do
           [ -f "$f" ] || continue
           b="$(basename "$f")"
           case "$b" in *.converted.md|*.approve.md|*.applied.md|*.preview.md) continue ;; esac
           printf '%s\n' "$b" | sed -E 's/^([0-9]{4}-[0-9]{2}-[0-9]{2})(-weekly)?.*/\1\2/'
         done | sort -u)"
# Today's triage is expected even when its file is absent — that absence is the finding — but only
# while the night runs a triage stage at all (KHMS_NIGHT_TRIAGE configured). A stage that is OFF is
# not a sweep that failed: the night's own log says OFF, and so does this ping (by saying nothing).
# Triage files already in the inbox are offered for review either way.
TRIAGE_ON=0
[ -n "$(khms_get KHMS_NIGHT_TRIAGE)" ] && TRIAGE_ON=1
if [ "$TRIAGE_ON" = 1 ]; then
  case "$DATES" in *"$TODAY"*) ;; *) DATES="$DATES
$TODAY" ;; esac
fi

# CARD REVIEW IS PER UNIT (Diblin review of #209 A, P1-3, 2026-10-01). A date can hold several
# units — DATE.md, DATE-weekly.md and every published nightly batch <night>-bNN.md — and a
# unit counts as reviewed only while its marker still describes it (sha256, or a legacy marker
# not older than the file). mark_ledger.py owns that rule so the hook, the ledger and this ping
# agree; if it cannot run, the old per-date marker check below is the fallback.
ML="$CODE/mark_ledger.py"
UNREV_UNITS=""
UNITS_OK=0
if UNREV_UNITS="$(python3 "$ML" --root "$BRAIN" units --unreviewed 2>/dev/null)"; then
  UNITS_OK=1
  UNREV_UNITS="$(printf '%s\n' "$UNREV_UNITS" | sed -n 's|^UNREVIEWED memory/inbox/||p')"
fi
for d in $DATES; do
  legacy=0
  if [ -f "$MARKERS/.reviewed-$d" ] && [ "$d" != "$TODAY" ]; then legacy=1; fi
  state=""
  # --- card proposals (DATE.md / DATE-weekly.md)
  if [ "$UNITS_OK" = 1 ]; then
    case "$d" in
      *-weekly) units_re="^${d}\.md\$" ;;
      *) units_re="^${d}((-[0-9]+)?-b[0-9]{2,})?\.md\$" ;;
    esac
    have_units="$(ls "$INBOX" 2>/dev/null | grep -E "$units_re" || true)"
    if [ -n "$have_units" ]; then
      pend="$(printf '%s\n' "$UNREV_UNITS" | grep -E "$units_re" | while read -r _u; do
                if [ -n "$_u" ] && ! is_wip "$_u"; then printf '%s ' "$_u"; fi; done | sed 's/ $//' || true)"
      if [ -n "$pend" ]; then
        state="cards: PENDING ($pend)"
      else
        state="cards: reviewed"
      fi
    fi
  elif [ -f "$INBOX/$d.md" ] && ! is_wip "$d.md"; then
    if [ "$legacy" = 1 ] || [ -f "$MARKERS/.reviewed-$d-cards" ]; then
      state="cards: reviewed"
    else
      state="cards: PENDING"
    fi
  fi
  # --- contradiction triage (DATE-triage.md); weekly units have none
  case "$d" in
    *-weekly) ;;
    *)
      if [ -f "$INBOX/$d-triage.md" ] && ! is_wip "$d-triage.md"; then
        rev=0
        if [ "$legacy" = 1 ] || [ -f "$MARKERS/.reviewed-$d-triage" ]; then rev=1; fi
        cen="$(triage_census "$INBOX/$d-triage.md")"
        judged="${cen%% *}"; planned="${cen##* }"
        ratio=""
        [ -n "$cen" ] && ratio=" (judged $judged of $planned planned pairs)"
        if [ -z "$cen" ] && [ "$d" = "$TODAY" ]; then
          ratio=" (no judged census in this file — written by a pre-2026-09-20 reporter)"
        fi
        # The zero-judgement alarm fires for TODAY whatever the review marker says — the fix
        # is to RE-RUN the sweep, and marking it reviewed does not make the night happen. For
        # older dates it fires only while the file is still unreviewed, so it cannot become
        # the alarm that rings every morning forever and is therefore ignored.
        if [ -n "$cen" ] && [ "$judged" -eq 0 ] 2>/dev/null \
           && { [ "$d" = "$TODAY" ] || [ "$rev" = 0 ]; }; then
          t="triage: JUDGED NOTHING (0 of $planned planned pairs came back judged — the file \
exists and decided nothing; re-run the sweep, this is not a result to review)"
        elif [ "$rev" = 1 ]; then
          t="triage: reviewed$ratio"
        else
          t="triage: PENDING$ratio"
        fi
      elif [ "$TRIAGE_ON" = 1 ] && [ "$d" = "$TODAY" ] && [ ! -f "$MARKERS/.reviewed-$d-triage" ]; then
        # NOT the same thing as "the triage found nothing": the file is either still being
        # written by a live sweep or the sweep died. Say which — on 2026-09-02 this script
        # reported neither and the missing file passed for a clean night.
        pid="$(pgrep -f "$SWEEP_PATTERN" | head -1)"
        if [ "$NIGHT_RUNNING" = 1 ]; then
          t="triage: not-yet-written (the nightly is still running since $NIGHT_HM — the triage is its last stage)"
        elif [ -n "$pid" ]; then
          el="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')"
          t="triage: not-yet-written (khms_contradict running pid $pid, elapsed ${el:-?}s)"
        else
          t="triage: not-yet-written (no khms_contradict process — the sweep did not finish)"
        fi
      else
        t=""
      fi
      [ -n "$t" ] && state="${state:+$state, }$t"
      ;;
  esac
  case "$state" in
    *PENDING*|*not-yet-written*|*"JUDGED NOTHING"*)
      PENDING="${PENDING:+$PENDING + }$d [$state]" ;;
  esac
done

# ALSO ping when the nightly did not RUN. Until 2026-07-29 this script only
# looked for the proposal file, so a nightly that died produced no file, and no
# file meant no ping — the failure hid behind the check meant to catch it. That
# is exactly the "a success signal at one layer is not the thing itself" lesson
# the base already carries, applied to its own watchdog.
#
# nightly_distill.sh writes the night stamp (KHMS_NIGHT_STAMP) only after it completes.
# Threshold -mmin +1200 (20 h): a healthy stamp is ~40 min old at 04:17, a missed
# night makes it ~24.6 h — so the FIRST missed night already fires. The original
# `-mtime +1` needs age ≥ 48 h and silently skipped the first miss.
if [ "$NIGHT_RUNNING" != 1 ] && { [ ! -f "$STAMP" ] || [ -n "$(find "$STAMP" -mmin +1200 2>/dev/null)" ]; }; then
  LAST=$([ -f "$STAMP" ] && cat "$STAMP" || M PING_NEVER)
  PENDING="${PENDING:+$PENDING + }$(M PING_NIGHT_MISSED last="$LAST")"
fi
if [ -n "$NIGHT_NOTE" ]; then PENDING="${PENDING:+$PENDING + }$NIGHT_NOTE"; fi

# A SWEEP THAT KEEPS FAILING (K209H, REV209 F8). A failed sweep writes no stamp, so the next night
# sweeps the same window plus a day — and a part that fails deterministically fails again, every
# night, on a window that only grows. nightly_distill.sh appends one line per failed sweep to
# KHMS_SWEEP_FAILED and removes the file on the next success; here it is said every morning
# until it is gone.
if [ -s "$SFAIL" ]; then
  _sn=$(grep -c . "$SFAIL")
  _sfirst=$(head -1 "$SFAIL" | cut -d' ' -f1)
  _slast=$(tail -1 "$SFAIL" | cut -c1-200)
  PENDING="${PENDING:+$PENDING + }$(M PING_SWEEP_FAILED n="$_sn" first="$_sfirst" last="$_slast")"
fi

# Same watchdog for the weekly (stamps KHMS_WEEK_STAMP on success).
# 10140 min = 7 d + 1 h: a failed Sunday fires from Monday 04:17 on; a healthy
# stamp never crosses the threshold before its next refresh.
if [ ! -f "$WSTAMP" ] || [ -n "$(find "$WSTAMP" -mmin +10140 2>/dev/null)" ]; then
  WLAST=$([ -f "$WSTAMP" ] && cat "$WSTAMP" || M PING_NEVER)
  PENDING="${PENDING:+$PENDING + }$(M PING_WEEK_MISSED last="$WLAST")"
fi

# ---------------------------------------------------------------------------
# PROJECT LINES (KHMS_PING_EXTRA). A project puts its own morning alarms into the same message -- whether a
# device runs the version it should, whether a log kept every boot -- without KHMS knowing anything about
# the project. The command READS, it never measures: deciding what an alarm is belongs to the project's own
# tested check. Each output line is one alarm; they are joined onto ONE line (see delivery below).
EXTRA=""
if [ ${#PING_EXTRA[@]} -gt 0 ]; then
  if _xout="$("${PING_EXTRA[@]}" 2>&1 </dev/null)"; then
    EXTRA="$(printf '%s\n' "$_xout" | sed '/^[[:space:]]*$/d' | paste -sd'\t' - | sed 's/\t/  ||  /g')"
  else
    _xrc=$?
    EXTRA="$(M PING_EXTRA_FAILED rc="$_xrc" cmd="${PING_EXTRA[*]}" out="$(printf '%s' "$_xout" | tail -1 | cut -c1-200)")"
  fi
fi

[ -z "$PENDING$EXTRA" ] && exit 0

# The conversion step names the TOOL, not the goal. "prevod do YAML frontmatter" is a
# goal, and a goal is what every morning since 2026-08-17 has met by writing a fresh
# throwaway converter ("recurring friction — KHMS morning review still needs
# a format converter"). convert_inbox2.py does it deterministically, for all
# three inbox shapes the consolidate stage produces, and validates its own output with
# approve_inbox.py's own parser before claiming success. Read its report, fix what it
# flags, approve.
# A night that produced a file and no judgements needs a DIFFERENT action from an unreviewed
# proposal: re-run the sweep first, review second. Reviewing it as if it were a result is how
# "twelve pairs adjudicated, zero contradictions" became a clean bill of health on 2026-09-20.
ZERO_NOTE=""
case "$PENDING" in
  *"JUDGED NOTHING"*)
    ZERO_NOTE="$(M PING_ZERO_NOTE)" ;;
esac

MSG=""
if [ -n "$PENDING" ]; then
MSG="$(M PING_REVIEW pending="$PENDING" zero_note="$ZERO_NOTE" principal="$PRINCIPAL")"
fi

# The project lines ride in the SAME message, on ONE line. Not out of tidiness:
# a terminal agent is reached by typing the text and pressing Enter, and an embedded
# newline is taken by the CLI's paste detection as a literal newline — which is how
# the 2026-07-29 ping sat unsent in the input box for hours. One message, one line.
# When the inbox is clean the project lines are the WHOLE message, with their own header,
# so the text never claims there are proposals to review when there are none.
if [ -n "$EXTRA" ]; then
  if [ -n "$MSG" ]; then
    MSG="$MSG  ||  $EXTRA"
  else
    MSG="$(M PING_EXTRA_ONLY extra="$EXTRA" principal="$PRINCIPAL")"
  fi
fi

# --dry-run: print what would be sent and exit. Use this when testing from inside
# a session -- a live delivery types into the agent's OWN input box while it is busy,
# which is exactly the situation where delivery is flakiest.
#
# KHMS_PING_DRYRUN=1 IS THE SAME SWITCH, REACHABLE WITHOUT OWNING $1.
# The flag can only be given by whoever writes the argv, and the callers that most need the
# brake -- an agent auditing the inbox from inside the very session the live path types into --
# reach this script through wrappers and env. An earlier version ignored the variable and
# went all the way to the delivery.
if [ "${1:-}" = "--dry-run" ] || [ "${KHMS_PING_DRYRUN:-}" = "1" ]; then
  echo "DRY-RUN pending: $PENDING"
  echo "DRY-RUN extra: ${EXTRA:-$(M PING_EXTRA_CLEAN)}"
  # ...AND WHAT WOULD BE SENT. A dry run that printed only the pending list while
  # promising "what would be sent" could not show the one thing it exists to show:
  # a broken message would have dry-run clean every time.
  echo "DRY-RUN message:"
  printf '%s\n' "$MSG"
  exit 0
fi

if [ ${#PING_SEND[@]} -eq 0 ]; then
  echo "$(date -Is) delivery OFF (KHMS_PING_SEND not configured); the ping text:"
  printf '%s\n' "$MSG"
  exit 0
fi
src=0
printf '%s\n' "$MSG" | "${PING_SEND[@]}" || src=$?
case "$src" in
  0) echo "$(date -Is) pinged: ${PENDING:-$(M PING_INBOX_CLEAN)}${EXTRA:+ + $EXTRA}" ;;
  3) echo "$(date -Is) not delivered: the recipient is not there (${PING_SEND[*]} rc=3), skipped" ;;
  *) echo "$(date -Is) delivery FAILED rc=$src (${PING_SEND[*]})"; exit 1 ;;
esac
