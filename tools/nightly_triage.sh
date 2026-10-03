#!/usr/bin/env bash
# KHMS nightly TRIAGE stage: sweep the standing base for contradictions, triage the losing
# side at CLAIM level, and write ONE proposal file to memory/inbox/DATE-triage.md.
#
# WHY IT IS A SEPARATE STAGE AND A SEPARATE FILE:
#  - the consolidate stage already "flags contradictions" (spec §9.2), but only inside the
#    night's own candidate set. Nothing has ever compared a new card against the 2295 cards
#    already standing, which is how a correction can sit in the base for days while the wrong
#    card stays active and unmarked. Measured 2026-08-31: 7 refuted cards out of 2295.
#  - a separate output file means a failure here cannot damage the card proposals, and
#    khms_review_ping.sh's today-date glob picks it up on its own.
#
# IT PROPOSES. It never writes to memory/know/, never regenerates MEMORY.md or views/.
set -euo pipefail

# ARGUMENTS (#208 shadow, 2026-10-01): `--dry-run` is NIGHTLY_DRYRUN=1, anything else exits 2
# before any work — the same contract as nightly_distill.sh (an unknown flag that runs a live
# night anyway is a check that cannot fail).
for _a in "$@"; do
  case "$_a" in
    --dry-run|--dry) NIGHTLY_DRYRUN=1 ;;
    *) echo "usage: nightly_triage.sh [--dry-run]   (env: NIGHTLY_DRYRUN=1 is the same)" >&2
       echo "refused: unknown argument '$_a'" >&2; exit 2 ;;
  esac
done

# WHERE THINGS ARE (khms_paths.sh): data under KHMS_ROOT (memory/know read, memory/inbox written),
# the night's stamp KHMS_NIGHT_STAMP (read: the incremental window), log NIGHTLY_LOG / TRIAGE_LOG else
# KHMS_NIGHT_LOG, the tools beside this script. TRIAGE_OUT / NIGHTLY_STAGING / NIGHTLY_DATE stay
# overridable so a test can run the stage for real against a throwaway base.
. "$(dirname "$(readlink -f "$0")")/khms_paths.sh"
khms_load || exit 3
CODE=$KHMS_CODE
BRAIN=$(khms_get KHMS_ROOT)
DATE=${NIGHTLY_DATE:-$(date +%F)}
STAMP=$(khms_get KHMS_NIGHT_STAMP)
LOG=${TRIAGE_LOG:-${NIGHTLY_LOG:-$(khms_get KHMS_NIGHT_LOG)}}
mkdir -p "$(dirname "$LOG")"
STAGING=${NIGHTLY_STAGING:-$BRAIN/memory/inbox/.staging}
OUT=${TRIAGE_OUT:-$BRAIN/memory/inbox/$DATE-triage.md}
DRYRUN=${NIGHTLY_DRYRUN:-0}
# Full sweep on Sundays (the weekly window), incremental on the other six nights: an
# incremental run still compares the touched cards against the WHOLE base, which is the
# property that matters; the weekly full run is what catches conflicts between two cards that
# are both old.
LIMIT=${TRIAGE_LIMIT:-250}
# THE JUDGE: who adjudicates the pairs -- a Claude model through the agent CLI (`claude -p --model
# sonnet|haiku`) or the LOCAL model (KHMS_LLM_URL, $0). In the reference deployment a measured gate over
# 28 reviewed nights picked the local model, and the operator chose Sonnet as the judge of record
# anyway; the settings default is therefore sonnet (KHMS_JUDGE). The triage gate (khms_triage_gate.py)
# stays in front either way.
export KHMS_JUDGE=${KHMS_JUDGE:-$(khms_get KHMS_JUDGE)}
# THE PAID JUDGE'S BOUND: an ESTIMATED per-night bound, not a guaranteed hard cap (khms_contradict.py
# JudgeBudget: every attempt reserves its estimated worst case first; a known cost replaces the
# reservation, an unknown one keeps it; a cost above its estimate is a BREACH and stops every later
# paid call of the night). All runs of the date share one ledger. KHMS_JUDGE_MAX_USD; empty = the
# stage default $15 (~3.4x the measured cost of a 250-pair night in the reference deployment). Pairs
# the bound refuses are BUDGET in the morning file -- never sent, never "no contradiction".
TRIAGE_JUDGE_MAX_USD=${TRIAGE_JUDGE_MAX_USD:-$(khms_get KHMS_JUDGE_MAX_USD)}
TRIAGE_JUDGE_MAX_USD=${TRIAGE_JUDGE_MAX_USD:-15}
# THE NIGHT ALWAYS GETS A MORNING FILE when a fallback exists: when the Claude judge judged NOTHING
# (the sweep died, the CLI failed on every batch, the bound refused the first call), the sweep is run
# again with the LOCAL judge and that is the night's file; the Claude attempt is kept beside it as
# *.<judge>.json/md. Without a local model (KHMS_LLM_URL empty) there is no fallback: the night's
# triage FAILS loudly instead. A Claude night that judged at least one pair keeps its own file.
export KHMS_LLM_URL=${KHMS_LLM_URL:-$(khms_get KHMS_LLM_URL)}
export KHMS_LLM_MODEL=${KHMS_LLM_MODEL:-$(khms_get KHMS_LLM_MODEL)}
FALLBACK_JUDGE=""
if [ -n "$KHMS_LLM_URL" ] && [ "$KHMS_JUDGE" != local ]; then FALLBACK_JUDGE=local; fi
export KHMS_CLAUDE_BIN=${KHMS_CLAUDE_BIN:-${NIGHTLY_CLAUDE_BIN:-$(khms_get KHMS_AGENT)}}
# The shadow judge of the reference deployment (a second judge evaluated on the same pairs) is an
# evaluation pilot, not part of KHMS: it ran only while the judge of record was the local model.
SINCE=$(cat "$STAMP" 2>/dev/null || echo "1970-01-01")
if [ "$(date +%u)" = "7" ] || [ "${TRIAGE_FULL:-0}" = "1" ]; then MODE=(); else MODE=(--since "$SINCE"); fi

mkdir -p "$STAGING"
SWEEP_JSON=$STAGING/triage-$DATE-sweep.json
JUDGE_LEDGER=$STAGING/triage-$DATE-judge-spend.jsonl
SWEEP_MD=$STAGING/triage-$DATE-sweep.md
PROV_MD=$STAGING/triage-$DATE-provenance.md

if [ "$DRYRUN" = "1" ]; then
  echo "=== DRY RUN — triage stage inputs, no model call, no inbox write ==="
  echo "  brain          : $BRAIN"
  echo "  date           : $DATE"
  echo "  mode           : ${MODE[*]:-full sweep (all cards)}"
  echo "  since stamp    : $SINCE"
  echo "  cards in know/ : $(ls "$BRAIN"/memory/know/K-*.md | wc -l)"
  echo "  embed index    : $(ls -la "$BRAIN"/memory/.embed/bge.npz 2>/dev/null | awk '{print $5" B, "$6" "$7" "$8}')"
  echo "  adjudicate top : $LIMIT pairs"
  echo "  judge          : $KHMS_JUDGE$([ "$KHMS_JUDGE" = local ] || echo " (claude -p --model $KHMS_JUDGE via $KHMS_CLAUDE_BIN)") — judge of record"
  if [ "$KHMS_JUDGE" != local ]; then
    echo "  judge bound    : \$$TRIAGE_JUDGE_MAX_USD/night, estimated bound (not a guaranteed hard cap); spend ledger $JUDGE_LEDGER ($(python3 -c '
import json, sys
try:
    t = sum(float(json.loads(l)["charged_usd"]) for l in open(sys.argv[1]) if l.strip())
    print(f"${t:.4f} charged so far tonight")
except FileNotFoundError:
    print("nothing charged yet tonight")
except Exception as e:
    print(f"UNREADABLE: {e} - the sweep will refuse every paid call")' "$JUDGE_LEDGER" 2>&1))"
    if [ -n "$FALLBACK_JUDGE" ]; then
      echo "  judge fallback : local, when $KHMS_JUDGE judges nothing (sweep died, every call failed, bound hit first)"
    else
      echo "  judge fallback : NONE (no KHMS_LLM_URL) — a night the $KHMS_JUDGE judge cannot judge FAILS"
    fi
  fi
  echo "  llm            : ${KHMS_LLM_URL:-none (KHMS_LLM_URL empty)}${KHMS_LLM_URL:+ $KHMS_LLM_MODEL}"
  echo "  sweep json     -> $SWEEP_JSON"
  echo "  provenance     -> $PROV_MD"
  echo "  PROPOSAL FILE  -> $OUT"
  echo "  shadow judge   : not part of KHMS"
  echo
  echo "--- blocking only (deterministic, zero tokens) ---"
  python3 "$CODE/khms_contradict.py" --pairs-only --limit 3 "${MODE[@]}" \
    >/dev/null 2>"$STAGING/triage-$DATE-dryrun.err" || true
  cat "$STAGING/triage-$DATE-dryrun.err"
  echo
  echo "--- provenance flags (deterministic, zero tokens) ---"
  python3 "$CODE/khms_contradict.py" --provenance --limit 5 2>/dev/null | head -20
  echo
  echo "=== DRY RUN END — memory/know/ untouched, no inbox file written ==="
  exit 0
fi

echo "$(date -Is) triage start (mode=${MODE[*]:-full}, limit=$LIMIT, judge=$KHMS_JUDGE$([ "$KHMS_JUDGE" = local ] || echo ", bound \$$TRIAGE_JUDGE_MAX_USD/night estimated, ledger $JUDGE_LEDGER"))" >> "$LOG"

python3 "$CODE/khms_contradict.py" --provenance --limit 60 > "$PROV_MD" 2>>"$LOG" || true

# One blocking call with its own deadline. A hung model must cost this stage and nothing else.
#
# TWO DEADLINES, DELIBERATELY, AND THE INNER ONE IS SHORTER (2026-09-20). `timeout` here is the
# outer one: it kills, and a killed run can say nothing about itself. The tool's own
# `--deadline` is the inner one: it reaches the end of the run by itself, writes the report,
# and names every batch it could not get an answer for. A run that ends on the INNER deadline
# produces a usable file; one that ends on the outer deadline produces whatever the last
# checkpoint held. Keep the inner one below the outer with room for the report to be written.
TRIAGE_DEADLINE_S=${TRIAGE_DEADLINE_S:-6600}     # 110 m, inside the 120 m below
rc=0
# census <sweep json> -> "<judged> <planned>" (khms_triage_report.judgement_census, the one
# definition of "judged" the proposal uses); "? ?" when unreadable.
census() {
  python3 - "$1" <<PY
import json, sys
sys.path.insert(0, "$CODE")
try:
    import khms_triage_report as R
    d = json.load(open(sys.argv[1]))
    c = R.judgement_census(d["results"], d.get("meta", {}))
    print(f"{c['judged']} {c['planned']}")
except Exception:                                           # noqa: BLE001
    print("? ?")
PY
}
run_sweep() {   # run_sweep <judge>
  KHMS_JUDGE="$1" timeout --signal=TERM --kill-after=60s "${TRIAGE_TIMEOUT:-120m}" \
  python3 "$CODE/khms_contradict.py" "${MODE[@]}" \
    --limit "$LIMIT" --batch 6 --top 12 --deadline "$TRIAGE_DEADLINE_S" \
    --max-usd "$TRIAGE_JUDGE_MAX_USD" --spend-ledger "$JUDGE_LEDGER" \
    --out "$SWEEP_MD" --json "$SWEEP_JSON" \
    >> "$LOG" 2>&1
}
JUDGE_USED=$KHMS_JUDGE
run_sweep "$KHMS_JUDGE" || rc=$?
if [ "$KHMS_JUDGE" != "local" ]; then
  if [ -s "$SWEEP_JSON" ]; then CEN=$(census "$SWEEP_JSON"); else CEN="? ?"; fi
  read -r C_JUDGED C_PLANNED <<< "$CEN"
  if [ -z "$FALLBACK_JUDGE" ]; then
    :   # no local model configured: whatever the Claude judge left is the night (judged or FAILED below)
  elif [ "$rc" -ne 0 ] || [ ! -s "$SWEEP_JSON" ] || { [ "$C_JUDGED" = "0" ] && [ "$C_PLANNED" != "0" ]; }; then
    echo "$(date -Is) triage FALLBACK: judge $KHMS_JUDGE judged nothing (rc=$rc$([ "$rc" -eq 124 ] && echo ' = TIMEOUT'), judged $C_JUDGED of $C_PLANNED planned) —" \
         "the night's judge is local; the $KHMS_JUDGE attempt is kept as" \
         "${SWEEP_JSON%.json}.$KHMS_JUDGE.json / ${SWEEP_MD%.md}.$KHMS_JUDGE.md" >> "$LOG"
    if [ -e "$SWEEP_JSON" ]; then mv -f "$SWEEP_JSON" "${SWEEP_JSON%.json}.$KHMS_JUDGE.json"; fi
    if [ -e "$SWEEP_MD" ]; then mv -f "$SWEEP_MD" "${SWEEP_MD%.md}.$KHMS_JUDGE.md"; fi
    rc=0
    JUDGE_USED=local
    echo "$(date -Is) triage start (fallback, judge=local)" >> "$LOG"
    run_sweep local || rc=$?
  fi
fi
if [ "$rc" -ne 0 ] || [ ! -s "$SWEEP_JSON" ]; then
  echo "$(date -Is) triage sweep FAILED (rc=$rc$([ "$rc" -eq 124 ] && echo ' = TIMEOUT'))" >> "$LOG"
  # WHAT SURVIVED IS NOW SAID OUT LOUD. Before 2026-09-20 an interrupted sweep left nothing and
  # the log said only that it failed; the morning then had no way to tell "the model found
  # nothing" from "the run never got to look". The sweep now checkpoints as it goes, so say
  # how far it got — the file is NOT promoted to a proposal on its own (that is a review
  # decision, and memory/inbox/ is not this stage's to fill with half an answer).
  if [ -s "$SWEEP_JSON" ]; then
    # WHAT "ADJUDICATED" MEANS HERE (2026-09-20, second correction of the same day). The
    # sweep's own `progress.adjudicated` is the number of pairs it RECORDED, and a pair it
    # marked DEADLINE is recorded precisely because it was never sent. So this line asks
    # khms_triage_report.judgement_census — one definition of "judged", shared with the
    # proposal the morning reads — instead of re-deriving a looser one in the shell.
    echo "$(date -Is) triage PARTIAL kept: $(python3 - "$SWEEP_JSON" <<PY
import json, sys
sys.path.insert(0, "$CODE")
try:
    import khms_triage_report as R
    d = json.load(open(sys.argv[1]))
    c = R.judgement_census(d["results"], d.get("meta", {}))
    print(f"{c['judged']}/{c['planned']} planned pairs JUDGED, "
          f"{c['not_judged'] + c['no_record']} carry no judgement, "
          f"partial={c['partial']}, outcomes={c['by_outcome']}")
except Exception as e:                                      # noqa: BLE001
    print(f"unreadable ({e})")
PY
)  -> $SWEEP_JSON / $SWEEP_MD" >> "$LOG"
    echo "$(date -Is) triage PARTIAL: to turn it into a proposal by hand, run" \
         "tools/khms_triage_report.py $SWEEP_JSON --provenance $PROV_MD --out $OUT" >> "$LOG"
  else
    echo "$(date -Is) triage left NO partial report at all — that is a defect in the sweep," \
         "not an empty base" >> "$LOG"
  fi
  exit 1
fi

FALLBACK_ARGS=()
if [ "$JUDGE_USED" != "$KHMS_JUDGE" ]; then FALLBACK_ARGS=(--fallback-from "$KHMS_JUDGE"); fi
python3 "$CODE/khms_triage_report.py" "$SWEEP_JSON" \
  --provenance "$PROV_MD" --out "$OUT" "${FALLBACK_ARGS[@]}" >> "$LOG" 2>&1
# (that command logs its own census line: "wrote … — judged N/M planned · …")

# A SWEEP CAN EXIT 0 HAVING JUDGED NOTHING. When the run's inner deadline arrives, every pair
# left is recorded as DEADLINE and the tool finishes normally — measured 2026-09-20: 18 pairs
# planned, 0 judged, rc=0. The proposal now says so on its own first screen; this line puts
# the same fact in nightly.log, because "triage done" next to an empty night is the defect
# this whole file exists to stop.
read -r N_JUDGED N_PLANNED <<< "$(census "$SWEEP_JSON")"
if [ "$N_JUDGED" = "0" ] && [ "${N_PLANNED:-0}" != "0" ]; then
  echo "$(date -Is) triage WARNING: the sweep judged NOTHING — 0 of $N_PLANNED planned pairs" \
       "came back judged. $OUT is a record of a night that did not happen, not of a base" \
       "without contradictions." >> "$LOG"
fi

echo "$(date -Is) triage done ($N_JUDGED of $N_PLANNED planned pairs judged, judge $JUDGE_USED$([ "$JUDGE_USED" = "$KHMS_JUDGE" ] || echo " = FALLBACK from $KHMS_JUDGE")) -> $OUT" >> "$LOG"
