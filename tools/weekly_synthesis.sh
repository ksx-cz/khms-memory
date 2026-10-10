#!/usr/bin/env bash
# KHMS weekly synthesis driver: headers digest (0 tokens) -> the week's inbox read in PARTS -> one
# synthesis MERGE call -> quote check -> NEAREST + RELATION gate (0 tokens).
# This stage reasons over existing cards, so its failure mode is misattribution (crediting a rule to
# the wrong card), which is just as checkable as any other quote — verify_quotes.py resolves
# src=K-xxxxx to the card itself. Writes memory/inbox/DATE-weekly.md. Never touches memory/know/.
# See spec/khms-spec.md §9.4.
# Usage: weekly_synthesis.sh   (no arguments; run daily by the deployment's scheduler; due every 7 days; log: KHMS_NIGHT_LOG)
#
# Locations and models come from khms_paths.sh: KHMS_ROOT (data), KHMS_WEEKLY_LOCK + KHMS_NIGHTLY_LOCK +
# KHMS_WEEK_STAMP (shared state), KHMS_NIGHT_LOG, KHMS_AGENT, KHMS_WEEK_PART_MODEL, KHMS_WEEK_MODEL,
# KHMS_AGENT_TRANSCRIPTS (where the agent CLI writes the session transcripts the read-coverage check
# reads). The run holds this clone's code-use lock for its WHOLE life, not only the night lock's
# snapshot seconds: an update of the clone must never swap code under a running weekly.
# WEEKLY_LOG / WEEKLY_DATE / WEEKLY_TRANSCRIPT_ROOT stay overridable for tests.
set -euo pipefail

CODE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
# shellcheck source=khms_paths.sh
. "$CODE/khms_paths.sh"
khms_load || exit 3
python3 "$CODE/khms_health.py" || exit 3
khms_require_writable "run the weekly synthesis" || exit $?
ROOT=$(khms_get KHMS_ROOT)
khms_code_lock || exit $?
AGENT=$(khms_get KHMS_AGENT)
LOG=${WEEKLY_LOG:-$(khms_get KHMS_NIGHT_LOG)}
mkdir -p "$(dirname "$LOG")"
WSTAMP=$(khms_get KHMS_WEEK_STAMP)
# mtime is the completion clock (also used by the review ping), never the inbox date.
# Check cheaply before locking, then again under the lock: another run may have finished.
weekly_due() {
  local completed now
  [ -f "$WSTAMP" ] || return 0
  completed=$(stat -c %Y -- "$WSTAMP") || return 0
  now=$(date +%s)
  [ "$((now - completed))" -ge 604800 ]
}
weekly_skip() {
  echo "$(date -Is) weekly not due: last success is younger than 7 days ($WSTAMP)" >> "$LOG"
  exit 0
}
weekly_due || weekly_skip
STAGING=$ROOT/memory/inbox/.staging
mkdir -p "$STAGING"
DATE=${WEEKLY_DATE:-$(date +%F)}

# MERGE PRESSURE. This stage reasons over the WHOLE base, so of the two producers it is the one
# most likely to write down again what the base already says (the reference base once held 17
# cards about one measured voltage). The answer to that is the RELATION gate below, not a count.
#
# NO CARD CAP (the principal's rule in the reference deployment: no daily card limit at all —
# simply process everything in the queue). An earlier WEEKLY_MAX_CARDS = 10 moved every candidate
# past ten into `## DEFERRED` — a section nothing ever reads again. Every candidate the synthesis
# finds is now a proposal; ranking still orders them, it no longer cuts them.

# ONE WEEKLY AT A TIME, AND NEVER INSIDE A HALF-PUBLISHED NIGHT. A weekly scheduled while the
# nightly still publishes its units one batch at a time once started inside the night and read
# whatever was published by then. Now: (1) a second weekly refuses to start (own flock,
# KHMS_WEEKLY_LOCK); (2) the weekly WAITS for the nightly's flock (KHMS_NIGHTLY_LOCK, the same
# lock the night holds for its whole run) and holds
# it only for the seconds of its inbox SNAPSHOT — window selection, concatenation, chunking — so it
# reads either all of a night or none of it, and a night that starts during those seconds waits
# for it (NIGHTLY_LOCK_WAIT_S) instead of being lost. The parts and the merge then work from the
# snapshot alone. A night still running after WEEKLY_NIGHT_WAIT_MIN fails the weekly, loudly
# (no output, no stamp: the window is read again by the next weekly).
WEEKLY_NIGHT_WAIT_MIN=${WEEKLY_NIGHT_WAIT_MIN:-480}
# THE WEEK IN PARTS. A real week is 0.9-2.5 MB of inbox in the reference deployment — far past
# what one model call can read, and a single weekly call once read about half of a 366 kB week
# while its output looked normal. The
# week's chunks are now read in PARTS of WEEKLY_PART_CHUNKS chunks, each by its own call that
# writes findings; one MERGE call then synthesises the week from the part findings and the card
# headers. Every call must PROVE it read all of its input: read_coverage.py checks the call's own
# transcript (each required file returned whole by successful Read calls). A part or a merge that
# fails the call or the check is retried (WEEKLY_PART_ATTEMPTS); one that still fails FAILS THE
# RUN — no DATE-weekly.md, no stamp — and the next weekly's window (weekly_inbox_window.py: from
# the previous weekly's day) covers the week again. Nothing is "handed" and assumed read.
# 6 chunks: digest_chunk.py sizes a chunk at <= 17,000 estimated tokens (an over-estimate), so a
# part is <= ~100k tokens of input — one context, no compaction, room left to write.
WEEKLY_PART_CHUNKS=${WEEKLY_PART_CHUNKS:-6}
WEEKLY_PART_TIMEOUT=${WEEKLY_PART_TIMEOUT:-45m}
WEEKLY_MERGE_TIMEOUT=${WEEKLY_MERGE_TIMEOUT:-120m}
WEEKLY_PART_ATTEMPTS=${WEEKLY_PART_ATTEMPTS:-2}
# Same account-limit vocabulary as nightly_retry.sh; quota exhaustion is not transient.
RETRY_LIMIT_RE=${RETRY_LIMIT_RE:-"hit your [a-z0-9 -]*limit|usage limit reached"}
# Parts only extract findings with quotes (the nightly extraction's kind of work, mid tier); the
# merge is the synthesis and runs on the strong tier (KHMS_WEEK_MODEL).
WEEKLY_PART_MODEL=${WEEKLY_PART_MODEL:-$(khms_get KHMS_WEEK_PART_MODEL)}
WEEKLY_MODEL=$(khms_get KHMS_WEEK_MODEL)
# Where the agent CLI writes a session transcript (<root>/<project>/<session-id>.jsonl); the
# coverage check finds each call's transcript by the session id this script gives it.
TRANSCRIPT_ROOT=${WEEKLY_TRANSCRIPT_ROOT:-$(khms_get KHMS_AGENT_TRANSCRIPTS)}
WLOCK=$(khms_get KHMS_WEEKLY_LOCK)
mkdir -p "$(dirname "$WLOCK")"

exec 7>>"$WLOCK"
flock -n 7 || { echo "$(date -Is) weekly already running, exit" >> "$LOG"; exit 0; }
weekly_due || weekly_skip

echo "$(date -Is) weekly start" >> "$LOG"
# render_prompt <name>: prompts/<name>.md with its fields and private extension slots filled
# (khms_prompt.py). A render failure fails the weekly: a call without its instructions is not a week.
render_prompt() {
  python3 "$CODE/khms_prompt.py" "$1" 2>>"$LOG" || weekly_fail "prompt $1 could not be rendered (khms_prompt.py)"
}
RUN_PROVEN_WHY=""
weekly_fail() {   # weekly_fail <why> — the run fails visibly and leaves no output that could pass for a week
  local line
  line="$(date -Is) weekly FAILED: $1 — no $DATE-weekly.md, not stamped; the next weekly reads this window again"
  echo "$line" >> "$LOG"
  echo "$line" >&2
  if [ -f "$OUT" ]; then mv -f "$OUT" "$STAGING/weekly-$DATE-FAILED-$(date +%H%M%S).md"; fi
  exit "${2:-1}"
}
FINAL_OUT=$ROOT/memory/inbox/$DATE-weekly.md
# The model and review gates write privately; publish only the completed proposal.
OUT=$STAGING/weekly-$DATE-merge.md
AGENT_BIN=$(command -v -- "$AGENT" 2>/dev/null || true)
[ -n "$AGENT_BIN" ] || weekly_fail "AGENT UNAVAILABLE: '$AGENT' (KHMS_AGENT) is not an executable command"

NLOCK=$(khms_get KHMS_NIGHTLY_LOCK)
mkdir -p "$(dirname "$NLOCK")"
exec 8>>"$NLOCK"
if ! flock -n 8; then
  echo "$(date -Is) weekly waits for the nightly ($(head -1 "$NLOCK" 2>/dev/null | cut -c1-120)) — up to ${WEEKLY_NIGHT_WAIT_MIN} min" >> "$LOG"
  flock -w $((WEEKLY_NIGHT_WAIT_MIN * 60)) 8 \
    || weekly_fail "the nightly still holds $NLOCK after ${WEEKLY_NIGHT_WAIT_MIN} min — a half-published night is not read"
fi
printf 'weekly %s pid=%s\n' "$(date -Is)" "$$" > "$NLOCK"     # the holder line (nightly_state.py)

# 0-token headers digest: one line per card
HDR=$STAGING/weekly-$DATE-headers.txt
PYTHONPATH="$KHMS_CODE${PYTHONPATH:+:$PYTHONPATH}" WEEKLY_KNOW="$ROOT/memory/know" python3 - > "$HDR" <<'EOF'
import os, re, yaml
import khms_files as glob
for p in sorted(glob.glob(os.path.join(os.environ["WEEKLY_KNOW"], "K-*.md"))):
    text = open(p, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m: continue
    meta = yaml.safe_load(m.group(1))
    body = m.group(2).strip().splitlines()
    title = body[0][:100] if body else ""
    print(f"{meta['id']}|{meta['type']}|{meta['level']}|{meta['status']}|{','.join(meta.get('tags') or [])}|{meta.get('scope','')}|{title}")
EOF

# THE WEEK'S INBOX, BY DATE. This line was once `ls …/inbox/2*.md | tail -7` — seven FILES, i.e.
# about a day and a half at four to twenty files a day, cut in collation order; twice five of the
# window's days never reached the stage and nothing said so. weekly_inbox_window.py selects every inbox file whose
# NAME carries a date from the previous weekly's day to DATE (both inclusive; see its docstring)
# and prints one summary line naming the window, its empty days and any undated file it skipped.
# A failure here is fatal and logged: a weekly over an unknown input is the defect being fixed.
INBOX_LIST=$STAGING/weekly-$DATE-inbox-window.txt
INBOX_WINDOW=$(python3 "$CODE/weekly_inbox_window.py" "$ROOT/memory/inbox" "$DATE" \
  2>&1 > "$INBOX_LIST") \
  || weekly_fail "inbox window selection: $INBOX_WINDOW"
echo "$(date -Is) $INBOX_WINDOW" >> "$LOG"
mapfile -t INBOX_FILES < "$INBOX_LIST"
INBOXES="${INBOX_FILES[*]}"

# The week's inbox files, concatenated into ONE file so the verifier can be given them as a
# source. Same defect class as the nightly's once-missing git log: this stage is told to read the
# week's inboxes "for extraction gaps", but verify_quotes.py was only ever handed `headers=`, so a
# quote drawn from an inbox could only come back `FAIL quote names unknown source` — six times on
# one card, once, for a source the stage had been
# given and told to use. A stage may cite exactly the sources the verifier holds; if we hand it an
# input, we hand the checker the same input.
INBOXCAT=$STAGING/weekly-$DATE-inboxes.md
: > "$INBOXCAT"
for f in "${INBOX_FILES[@]}"; do
  printf '\n===== %s =====\n' "${f##*/}" >> "$INBOXCAT"
  cat "$f" >> "$INBOXCAT"
done

# The concatenation, chunked so every piece fits one Read. FATAL since K209H: the parts read the
# chunks and nothing else, and a monolith fallback could not be proven read.
INBOX_INDEX=$STAGING/weekly-$DATE-inboxes-chunks.txt
: > "$INBOX_INDEX"
rm -f "$STAGING/weekly-$DATE-inboxes-part"[0-9]*.txt "$STAGING/weekly-$DATE-group"[0-9]*.txt
if [ -s "$INBOXCAT" ]; then
  python3 "$CODE/digest_chunk.py" "$INBOXCAT" "$STAGING" "weekly-$DATE-inboxes" \
    --index "$INBOX_INDEX" >> "$LOG" 2>&1 || weekly_fail "inbox chunking"
  [ -s "$INBOX_INDEX" ] || weekly_fail "inbox chunking wrote no chunk index"
fi
# The snapshot is complete: the night may run again.
flock -u 8
exec 8>&-

# Same Read-tool caps as the nightly (>256 kB whole / >25,000 tokens per slice): the headers digest
# of a grown base passes 300 kB, i.e. this stage's PRIMARY input becomes unreadable exactly the way
# the nightly digest did. Chunked with >=30 % margin. Fatal since K209H for the same reason as the
# inbox: the merge must prove it read every header chunk.
HDR_INDEX=$STAGING/weekly-$DATE-headers-chunks.txt
: > "$HDR_INDEX"
python3 "$CODE/digest_chunk.py" "$HDR" "$STAGING" "weekly-$DATE-headers" \
  --index "$HDR_INDEX" >> "$LOG" 2>&1 || weekly_fail "headers chunking"
[ -s "$HDR_INDEX" ] || weekly_fail "headers chunking wrote no chunk index"

STAGE_MARK=$STAGING/.weekly-stage-start

# run_proven <label> <timeout> <required-list-file> <output file> <prompt> <model>
# One model call that must PROVE it read every file of <required-list-file>: its session id is
# fixed here, its transcript is found by that id, and read_coverage.py checks it. The output file
# must exist and be non-empty. Tried WEEKLY_PART_ATTEMPTS times; returns 1 when no attempt passed.
run_proven() {
  local label="$1" tmo="$2" req="$3" out="$4" prompt="$5" model="$6"
  local attempt=1 sid rc tr cov said
  local call_out=$STAGING/.weekly-call-output
  while [ "$attempt" -le "$WEEKLY_PART_ATTEMPTS" ]; do
    sid=$(python3 -c 'import uuid; print(uuid.uuid4())')
    rm -f "$out"
    : > "$STAGE_MARK"
    rc=0
    # `Edit` is in the list for the same reason as in nightly_distill.sh: a headless run cannot be
    # granted permission interactively, so without Edit in this list the stage's only writable path
    # is whole-file Write — which is what made the nightly sweep re-emit its entire output eight times.
    # --setting-sources "" (PDRAIN, #209): a stage must not load the coordinator's ~/.claude/CLAUDE.md
    # and hooks (the nightly MARK stage answered as the coordinator and delegated instead of writing);
    # effort high is what the user settings supplied until then.
    timeout --signal=TERM --kill-after=60s "$tmo" /usr/bin/env "$AGENT_BIN" -p "$prompt" \
      --session-id "$sid" --setting-sources "" --effort high --model "$model" \
      --strict-mcp-config --tools Read Write Edit Glob Grep \
      --allowedTools Read Write Edit Glob Grep > "$call_out" 2>&1 || rc=$?
    cat "$call_out" >> "$LOG"
    # A quota response may be returned with CLI success; text is authoritative here.
    if grep -qiE "$RETRY_LIMIT_RE" "$call_out"; then
      RUN_PROVEN_WHY="$label USAGE LIMIT on attempt $attempt/$WEEKLY_PART_ATTEMPTS (rc=$rc): $(grep -m1 -iE "$RETRY_LIMIT_RE" "$call_out" | head -c 200) — not retried; no further model calls"
      echo "$(date -Is) $RUN_PROVEN_WHY" >> "$LOG"
      rm -f "$out"
      return 75
    fi
    said=$(grep -v '^[[:space:]]*$' "$call_out" | tail -1 | cut -c1-200 || true)
    tr=$(find "$TRANSCRIPT_ROOT" -name "$sid.jsonl" -type f 2>/dev/null | head -1)
    # Report, not gate: `|| true` so this cannot kill the script under `set -e`.
    [ -n "$tr" ] && python3 "$CODE/maxtok_check.py" --stage "$label" "$tr" >> "$LOG" 2>&1 || true
    if [ "$rc" -ne 0 ]; then
      RUN_PROVEN_WHY="$label attempt $attempt/$WEEKLY_PART_ATTEMPTS: model call FAILED (rc=$rc$([ "$rc" -eq 124 ] && echo ' = TIMEOUT'))${said:+: $said}"
      echo "$(date -Is) $RUN_PROVEN_WHY" >> "$LOG"
    elif [ -z "$tr" ]; then
      RUN_PROVEN_WHY="$label attempt $attempt/$WEEKLY_PART_ATTEMPTS: no transcript $sid.jsonl under $TRANSCRIPT_ROOT — reading cannot be proven"
      echo "$(date -Is) $RUN_PROVEN_WHY" >> "$LOG"
    else
      cov=0
      python3 "$CODE/read_coverage.py" --transcript "$tr" --label "$label" --list "$req" >> "$LOG" 2>&1 || cov=$?
      if [ "$cov" -ne 0 ]; then
        RUN_PROVEN_WHY="$label attempt $attempt/$WEEKLY_PART_ATTEMPTS: did not read all of its input (READ-COVERAGE above)"
        echo "$(date -Is) $RUN_PROVEN_WHY" >> "$LOG"
      elif [ ! -s "$out" ]; then
        RUN_PROVEN_WHY="$label attempt $attempt/$WEEKLY_PART_ATTEMPTS: wrote no output ($out)"
        echo "$(date -Is) $RUN_PROVEN_WHY" >> "$LOG"
      else
        return 0
      fi
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

# The parts: the week's chunks, in order, WEEKLY_PART_CHUNKS per part.
PART_FILES=()
if [ -s "$INBOX_INDEX" ]; then
  split -l "$WEEKLY_PART_CHUNKS" -d -a 3 --additional-suffix=.txt "$INBOX_INDEX" \
    "$STAGING/weekly-$DATE-group"
  mapfile -t GROUPS_ < <(ls "$STAGING/weekly-$DATE-group"[0-9]*.txt | sort)
  _K=${#GROUPS_[@]}
  echo "$(date -Is) weekly: $(grep -c . "$INBOX_INDEX") inbox chunk(s) -> $_K part(s) of <= $WEEKLY_PART_CHUNKS, each proven read" >> "$LOG"
  for _k in $(seq 1 "$_K"); do
    _gf=${GROUPS_[$((_k - 1))]}
    _pout=$STAGING/weekly-$DATE-part$(printf %02d "$_k").md
    _prompt="$(render_prompt weekly-part)

WEEKLY PART $_k OF $_K
INPUTS:
- YOUR PART: chunks of this week's inbox files, concatenated — READ EVERY ONE, WHOLE, IN ORDER:
$(sed 's|^|  * |' "$_gf")
- this week's inbox files, concatenated, whole file — GREP ONLY, never Read it whole: $INBOXCAT
- card headers digest, whole file — GREP ONLY: $HDR
- this week's inbox window: $INBOX_WINDOW
OUTPUT FILE: $_pout"
    echo "$(date -Is) weekly part $_k/$_K: $(grep -c . "$_gf") chunk(s) -> $_pout" >> "$LOG"
    run_proven "weekly-part-$_k" "$WEEKLY_PART_TIMEOUT" "$_gf" "$_pout" "$_prompt" "$WEEKLY_PART_MODEL" \
      || { _rc=$?; weekly_fail "part $_k/$_K failed ($_gf) — last: $RUN_PROVEN_WHY" "$_rc"; }
    PART_FILES+=("$_pout")
  done
else
  echo "$(date -Is) weekly: the window holds no inbox text — no parts" >> "$LOG"
fi

# The merge: the headers chunks and every part's findings, all proven read.
MERGE_REQ=$STAGING/weekly-$DATE-merge-required.txt
{ cat "$HDR_INDEX"; for p in "${PART_FILES[@]}"; do echo "$p"; done; } > "$MERGE_REQ"
if [ ${#PART_FILES[@]} -gt 0 ]; then
  PARTS_INPUT="this week's inbox, READ IN ${#PART_FILES[@]} PART(S) by earlier calls that each proved they read
  their chunks — their FINDINGS files, READ EVERY ONE WHOLE (they are your view of the week):
$(for p in "${PART_FILES[@]}"; do echo "  * $p"; done)
- this week's inbox files, concatenated, whole file — GREP ONLY (quote from it as src=inbox): $INBOXCAT"
else
  PARTS_INPUT="this week's inbox: EMPTY window (no inbox text to read) — $INBOXCAT"
fi
HDR_INPUT="card headers digest, SPLIT INTO READABLE CHUNKS — READ EVERY ONE, IN ORDER (the
  monolith below is too large for the Read tool; quote all of them as src=headers):
$(sed 's|^|  * |' "$HDR_INDEX")
- card headers digest, whole file — GREP ONLY, never Read it whole: $HDR"

MERGE_PROMPT="$(render_prompt weekly-synthesis)

INPUTS:
- $HDR_INPUT
- condense candidates: $ROOT/memory/views/condense-candidates.md
- this week's inbox window: $INBOX_WINDOW
- $PARTS_INPUT
- this week's inbox files, individually (names only — their text is in the parts and the concatenation): ${INBOXES:-none}
OUTPUT FILE: $OUT"
echo "$(date -Is) weekly merge: $(grep -c . "$HDR_INDEX") header chunk(s) + ${#PART_FILES[@]} part file(s) -> $OUT" >> "$LOG"
run_proven "weekly-merge" "$WEEKLY_MERGE_TIMEOUT" "$MERGE_REQ" "$OUT" "$MERGE_PROMPT" "$WEEKLY_MODEL" \
  || { _rc=$?; weekly_fail "the merge failed — last: $RUN_PROVEN_WHY" "$_rc"; }

# Grounding check: src=K-xxxxx quotes are resolved to the cards themselves, so a
# misattributed claim is caught by a tool rather than at review by eye.
# `|| true` is LOAD-BEARING: verify_quotes.py exits 1 on any finding and this script
# runs under `set -euo pipefail` — same silent-death class that ate the nightlies of
# 2026-07-28/29. The report is an input to review, not a gate.
python3 "$CODE/verify_quotes.py" "$OUT" --source headers="$HDR" \
  --source inbox="$INBOXCAT" \
  > "${OUT%.md}-quotecheck.txt" 2>&1 || true
echo "$(date -Is) weekly quotecheck: $(tail -1 "${OUT%.md}-quotecheck.txt")" >> "$LOG"

# === nearest === + === verify_relations === — the nightly's merge-pressure gate,
# applied here too. Without it the weekly once wrote proposals that named no existing
# card at all (0 `RELATION:` lines across 5 cards), and nothing between this stage and
# know/ ever asked what they replace.
#
# ONE ASYMMETRY WITH THE NIGHTLY, DELIBERATE. The nightly annotates its SWEEP file
# first, so its consolidate stage can SEE the NEAREST ids while it writes; the weekly
# has no intermediate candidate file, so the annotation can only run afterwards — and
# it runs WITHOUT `--only-missing`, i.e. it always recomputes. `--only-missing` exists
# to protect a line carried through from the sweep; here there is no such line, so the
# only thing it could protect is a NEAREST line the model wrote about ITSELF, which is
# precisely the evidence its own `RELATION: new` would then be judged against.
#
# NON-FATAL throughout. This script runs under `set -euo pipefail` and
# verify_relations.py exits 1 whenever it drops anything, so an un-guarded call would
# kill the weekly for doing its job — the shape that once ate two nightlies. Hence the
# `if` context and the `|| true`s. The gate is SKIPPED if the annotation fails: a
# RELATION check with no NEAREST lines drops every `new` candidate for the TOOL's reason
# instead of the candidate's.
WRREPORT=$STAGING/weekly-$DATE-relations.txt
echo "$(date -Is) weekly === nearest === annotating $OUT" >> "$LOG"
if python3 "$CODE/nearest_cards.py" "$OUT" >> "$LOG" 2>&1; then
  echo "$(date -Is) weekly === verify_relations === gate over $OUT (no --max: NO CARD CAP)" >> "$LOG"
  python3 "$CODE/verify_relations.py" "$OUT" \
    > "$WRREPORT" 2>&1 || true   # report, not a gate
  echo "$(date -Is) weekly === verify_relations === $(tail -1 "$WRREPORT")" >> "$LOG"
  grep -E '^!! MASS DROP' "$WRREPORT" >> "$LOG" || true
  grep -E '^[A-Z][0-9]+: (DROP|DEFER|FLAG)' "$WRREPORT" >> "$LOG" || true
else
  echo "$(date -Is) weekly === verify_relations === SKIPPED — nearest FAILED, and a RELATION gate without NEAREST lines would drop candidates for the tool's own reason" >> "$LOG"
fi

# Completion stamp — a review ping alerts when this goes stale (>7 d + 1 h), so a weekly
# that died no longer hides behind "no proposal file, nothing pending".
mv -f "${OUT%.md}-quotecheck.txt" "${FINAL_OUT%.md}-quotecheck.txt"
mv -f "$OUT" "$FINAL_OUT"
mkdir -p "$(dirname "$WSTAMP")"
date -Is > "$WSTAMP"
echo "$(date -Is) weekly done -> $FINAL_OUT" >> "$LOG"
