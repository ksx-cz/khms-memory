#!/usr/bin/env bash
# khms_validity_daily.sh -- the daily card-validity JUDGE: N cards not yet checked, unattended (#219).
#
# One run = one batch in KHMS_VALIDITY_DIR/<date>/: a fresh read-only sandbox of the trees named by
# KHMS_VALIDITY_TREES (card_validity.py sandbox), `card_validity.py run` on N cards drawn past the checked
# ledger (seed = the date as YYYYMMDD), `proposals` with every erratum row dry-run, the rows split
# round-robin into part files proposals-part<k>.md for parallel verification, and ONE status line in the
# night log (KHMS_NIGHT_LOG):
#   VALIDITY <date>: sampled=… holds=… no-longer-holds=… cannot-check=… unverified=… knowhow-no=… cost=$… parts=… rows=…
#   VALIDITY <date>: WAITING — batch <d> not applied (no APPLIED marker); nothing judged
#   VALIDITY <date>: DONE — no eligible cards left (…)
#   VALIDITY <date>: FAILED <reason>
# Verification and apply are NOT this job: they belong to whoever reviews the base, and the morning ping
# (khms_review_ping.sh) names every batch whose parts wait. That reviewer writes <date>/APPLIED when the
# batch is applied; until then no new batch is judged, so batches never pile up.
#
# Why a job of its own and not a night stage: the night has its own budget and finishing window; a 500-card
# pass is ~30 min and ~$21 of paid judge calls on top of it.
#
# Usage: khms_validity_daily.sh [--n 500] [--max-usd 30] [--workers 4] [--parts 4]
#                               [--out-root DIR] [--log FILE]
#   --out-root and --log redirect a trial run away from the daily directory and the night log. The cards a
#   trial judges DO enter the checked ledger: they were checked.
# Exit: 0 = a batch written, or nothing to do (waiting / already ran today / DONE); 1 = FAILED (said in the
# log); 75 = another run holds the lock (nothing written); 2 = usage.
# KHMS_VALIDITY_TODAY overrides the date (tests).
set -uo pipefail

CODE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=khms_paths.sh
. "$CODE/khms_paths.sh"
khms_load || exit 3

N=500; MAX_USD=30; WORKERS=4; PARTS=4; OUT_ROOT=""; LOG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --n) N="$2"; shift 2 ;;
    --max-usd) MAX_USD="$2"; shift 2 ;;
    --workers) WORKERS="$2"; shift 2 ;;
    --parts) PARTS="$2"; shift 2 ;;
    --out-root) OUT_ROOT="$2"; shift 2 ;;
    --log) LOG="$2"; shift 2 ;;
    *) echo "khms_validity_daily: unknown argument $1" >&2; exit 2 ;;
  esac
done
DATE="${KHMS_VALIDITY_TODAY:-$(date +%F)}"
SEED="${DATE//-/}"
ROOT="$(khms_get KHMS_ROOT)"
KNOW="$ROOT/memory/know"
OUT_ROOT="${OUT_ROOT:-$(khms_get KHMS_VALIDITY_DIR)}"
OUT="$OUT_ROOT/$DATE"
LOG="${LOG:-$(khms_get KHMS_NIGHT_LOG)}"
RUNTIME="$(khms_get KHMS_RUNTIME)"
CV=(python3 "$CODE/card_validity.py")
mkdir -p "$(dirname "$LOG")"

say() {   # the one status line: night log + stdout (the scheduler's own log)
  printf '%s VALIDITY %s: %s\n' "$(date -Is)" "$DATE" "$1" >> "$LOG"
  printf 'VALIDITY %s: %s\n' "$DATE" "$1"
}
fail() { say "FAILED $1"; exit 1; }

[ -n "$RUNTIME" ] || fail "KHMS_RUNTIME is empty (the lock and the sandbox live there)"
mkdir -p "$RUNTIME" || fail "cannot create $RUNTIME"

# ONE RUN AT A TIME. Opened for append (a refused launch truncates nothing); the holder writes its line.
LOCK="$RUNTIME/validity-daily.lock"
exec {LFD}>>"$LOCK"
if ! flock -n "$LFD"; then
  echo "$(date -Is) khms_validity_daily: another run holds $LOCK ($(head -1 "$LOCK" 2>/dev/null)) -- not started" >&2
  exit 75
fi
printf 'validity %s pid=%s date=%s\n' "$(date -Is)" "$$" "$DATE" > "$LOCK"

# ONE BATCH A DAY. STATUS is written last, so a run that died midway is resumed (its draw is kept).
if [ -f "$OUT/STATUS" ]; then
  echo "VALIDITY $DATE: already ran today -- $(cat "$OUT/STATUS")"
  exit 0
fi

# NO NEW BATCH WHILE AN OLDER ONE WAITS. A batch waits from its first part file until its APPLIED marker.
waiting=()
for d in "$OUT_ROOT"/*/; do
  [ -d "$d" ] || continue
  b="$(basename "$d")"
  [ "$b" = "$DATE" ] && continue
  compgen -G "$d/proposals-part*.md" >/dev/null || continue
  [ -e "$d/APPLIED" ] || waiting+=("$b")
done
if [ ${#waiting[@]} -gt 0 ]; then
  say "WAITING — batch ${waiting[*]} not applied (no APPLIED marker in $OUT_ROOT/<date>/); nothing judged"
  exit 0
fi

khms_require_writable "run the daily card-validity judge" || fail "the base is read-only here"
khms_code_lock || fail "the code clone $CODE is being updated (code-use lock held)"
read -r -a TREES <<< "$(khms_get KHMS_VALIDITY_TREES)"
[ ${#TREES[@]} -gt 0 ] || fail "KHMS_VALIDITY_TREES is not configured (the trees the judge reads)"
AGENT="$(khms_get KHMS_AGENT)"
mkdir -p "$OUT" || fail "cannot create $OUT"

# THE POOL. A resumed batch keeps its draw (sample.json); a fresh one first asks whether anything is left.
if [ ! -f "$OUT/sample.json" ]; then
  pool="$("${CV[@]}" sample --know "$KNOW" --seed "$SEED" --n "$N" 2>&1)" || fail "sample: $(printf '%s' "$pool" | tail -1)"
  printf '%s\n' "$pool" > "$OUT/pool.log"
  eligible="$(printf '%s\n' "$pool" | sed -nE 's/^ELIGIBLE ([0-9]+) of ([0-9]+).*/\1/p')"
  [ -n "$eligible" ] || fail "sample printed no ELIGIBLE line: $(printf '%s' "$pool" | tail -1)"
  if [ "$eligible" = 0 ]; then
    rmdir "$OUT" 2>/dev/null
    say "DONE — no eligible cards left ($(printf '%s\n' "$pool" | sed -n 's/^ELIGIBLE //p'))"
    exit 0
  fi
fi

# THE SANDBOX: built fresh for every run (today's trees), removed at exit whatever happens.
SB="$RUNTIME/validity-sandbox"
drop_sandbox() { if [ -e "$SB" ]; then chmod -R u+w "$SB" 2>/dev/null; rm -rf "$SB"; fi; }
drop_sandbox
trap drop_sandbox EXIT
targs=()
for t in "${TREES[@]}"; do targs+=(--tree "$t"); done
"${CV[@]}" sandbox --out "$SB" "${targs[@]}" > "$OUT/sandbox.log" 2>&1 \
  || fail "sandbox: $(tail -1 "$OUT/sandbox.log")"

"${CV[@]}" run --know "$KNOW" --sandbox "$SB" --out "$OUT" --seed "$SEED" --n "$N" --workers "$WORKERS" \
  --max-usd "$MAX_USD" --claude-bin "$AGENT" >> "$OUT/run.log" 2>&1
rc=$?
case "$rc" in
  0) capnote="" ;;
  3) capnote=" STOPPED-AT-CAP \$$MAX_USD" ;;
  *) fail "judge run rc=$rc: $(tail -1 "$OUT/run.log")" ;;
esac
"${CV[@]}" proposals --know "$KNOW" --out "$OUT/proposals.md" --results "$DATE=$OUT/sonnet.jsonl" \
  --sandbox "$SB" --dry-run-errata --summary-of "$DATE" --parts "$PARTS" > "$OUT/proposals.log" 2>&1 \
  || fail "proposals rc=$?: $(tail -1 "$OUT/proposals.log")"

sampled="$(sed -nE 's/^(SAMPLED|RESUMED) ([0-9]+) .*/\2/p' "$OUT/run.log" | tail -1)"
verdicts="$(grep '^VERDICTS ' "$OUT/run.log" | tail -1)"
rows="$(sed -nE 's/^PROPOSALS ([0-9]+) .*/\1/p' "$OUT/proposals.log")"
parts=0
for f in "$OUT"/proposals-part*.md; do [ -f "$f" ] && parts=$((parts + 1)); done
line="sampled=${sampled:-?} ${verdicts#VERDICTS } parts=$parts rows=${rows:-?}$capnote"
printf '%s\n' "$line" > "$OUT/STATUS"
say "$line"
exit 0
