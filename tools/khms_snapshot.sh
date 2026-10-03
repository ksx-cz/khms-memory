#!/usr/bin/env bash
# Local, hardlinked snapshots of a KHMS base — the version control a base is allowed to have.
#
# WHY NOT GIT. The reference deployment's owner keeps the knowledge base out of every git
# repository, private ones included: it lives on a synced drive and locally. So there is no
# repository, no remote, no commits.
# What the audit of 2026-09-02 actually asked for is the two things a repository would have
# given: a way to see WHAT CHANGED since the last known-good state, and a way to GET A FILE
# BACK after a tool wrote nonsense into it (that audit's own incident: khms_dedup.py rewrote
# eleven cards, and the only reason it was recoverable was a snapshot somebody had taken by
# hand that morning). Both are answerable without git.
#
# HOW. rsync --link-dest against the previous snapshot: unchanged files become hardlinks, so a
# daily snapshot of a 90 MB base costs the bytes that actually changed, and every snapshot is
# still a COMPLETE tree you can read, diff and copy from with cat/cp — no tool needed to open
# it. That property is the point: a backup you need a working program to read is a backup that
# fails exactly when the program is what broke.
#
# WHERE. SNAP_ROOT (KHMS_SNAPSHOT_ROOT, default ~/.khms-snapshots) is OUTSIDE the base,
# deliberately: a base that is live-synced to a file-sync service would otherwise multiply the
# sync payload by the number of snapshots; outside it, they are local only and the sync service
# keeps its own file versions.
#
# COMMANDS
#   khms_snapshot.sh [--tag LABEL]        take a snapshot  (the `git commit`)
#   khms_snapshot.sh --list               one line per snapshot  (the `git log`)
#   khms_snapshot.sh --diff [SNAP|latest] brain vs a snapshot  (the `git status`)
#   khms_snapshot.sh --restore SNAP PATH  copy one file/dir back  (the `git checkout --`)
#   khms_snapshot.sh --prune [--dry]      30 daily / 52 weekly / then monthly
#
# ENVIRONMENT (all optional; the tests use them, cron does not)
#   BRAIN_SNAPSHOT_BRAIN   base to snapshot         (default: KHMS_ROOT, see khms_paths.sh)
#   BRAIN_SNAPSHOT_ROOT    where snapshots live     (default: KHMS_SNAPSHOT_ROOT, else ~/.khms-snapshots)
#   BRAIN_SNAPSHOT_NOW     "now" for naming/prune   (any `date -d` string; default: real now)
#
# EXIT CODES: 0 ok · 2 usage · 3 refused (unsafe path) · 4 rsync failed
set -uo pipefail

SELF_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
. "$SELF_DIR/khms_paths.sh"
if [[ -n "${BRAIN_SNAPSHOT_BRAIN:-}" ]]; then
  DEFAULT_BRAIN="$(khms_get KHMS_ROOT 2>/dev/null || true)"
else
  khms_load || exit 3
  DEFAULT_BRAIN="$(khms_get KHMS_ROOT)"
fi
DEFAULT_ROOT="$(khms_get KHMS_SNAPSHOT_ROOT 2>/dev/null || true)"
DEFAULT_ROOT="${DEFAULT_ROOT:-$HOME/.khms-snapshots}"
BRAIN="${BRAIN_SNAPSHOT_BRAIN:-$DEFAULT_BRAIN}"
SNAP_ROOT="${BRAIN_SNAPSHOT_ROOT:-$DEFAULT_ROOT}"

# THE EXCLUDE LIST IS THE SPEC, not an optimisation. Every entry is either a log the snapshot
# would otherwise duplicate hourly (.inject.log alone is 6 MB and grows every tool call), a
# cache that regenerates itself (__pycache__, .embed, .pytest_cache), a sync database that is
# meaningless outside its own client (.sync_*.db*), or a payload that is not knowledge at all
# (tools/chore-home is an npm cache). archive/transcripts is NOT here on purpose — it is the
# evidence record, and a version history of the evidence is worth its bytes.
EXCLUDES=(
  ".inject.log"
  ".recall.log"
  ".precheck.log"
  ".hook-state/"
  ".sync_*.db*"
  "tools/chore-home"
  "tools/chore-runs"
  "tools/chore-sandbox"
  "archive/transcripts"
  "__pycache__"
  "memory/.embed"
  ".pytest_cache"
  "tools/*.log"
  "logs"
)

usage() { sed -n '2,38p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die()   { local msg="$1"; echo "khms_snapshot.sh: $msg" >&2; exit "${2:-2}"; }

now_epoch() {
  if [[ -n "${BRAIN_SNAPSHOT_NOW:-}" ]]; then date -d "$BRAIN_SNAPSHOT_NOW" +%s
  else date +%s; fi
}

# A snapshot directory name is <YYYY-MM-DDTHHMM[SS]>[-<label>]. Seconds appear only when two
# snapshots land in the same minute (approve_inbox + apply_triage in one morning do exactly
# that), so the common name stays short and the rare one stays unique.
snap_stamp() { date -d "@$1" +%Y-%m-%dT%H%M; }
snap_label() { local n="$1"; [[ "$n" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4,6}-(.*)$ ]] && echo "${BASH_REMATCH[1]}" || echo ""; }
snap_time()  {
  local n="$1"
  [[ "$n" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})? ]] || return 1
  date -d "${BASH_REMATCH[1]} ${BASH_REMATCH[2]}:${BASH_REMATCH[3]}:${BASH_REMATCH[4]:-00}" +%s
}

# Newest-last list of snapshot directory names (lexical sort == chronological for this format).
list_snaps() {
  [[ -d "$SNAP_ROOT" ]] || return 0
  find "$SNAP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{4,6}' | sort
}
newest_snap() { list_snaps | tail -1; }

# Does a path relative to the brain root fall under the exclude list? This is the ONE place
# the rules live; --diff filters its output through it so "what the snapshot skipped" and
# "what the diff ignores" cannot drift apart — a drift that would make --diff report
# differences the snapshot was never going to hold.
is_excluded() {
  local rel="$1" pat p
  for pat in "${EXCLUDES[@]}"; do
    p="${pat%/}"
    # shellcheck disable=SC2053   # glob match on the right-hand side is intended
    if [[ "$rel" == $p || "$rel" == */$p || "$rel" == $p/* || "$rel" == */$p/* ]]; then
      return 0
    fi
  done
  return 1
}

rsync_excludes() { local pat; for pat in "${EXCLUDES[@]}"; do printf -- '--exclude=%s\n' "$pat"; done; }

# --------------------------------------------------------------------------- metadata
# n_files / bytes are counted over the snapshot itself, so they describe what is ON DISK to
# read back, not what rsync thought it was doing. changed_vs_prev counts files whose link
# count is 1: with --link-dest, an unchanged file is a hardlink to the previous snapshot
# (nlink >= 2), so nlink == 1 is exactly "this file's content is new in this snapshot".
# That number is this system's `git diff --stat` for the night, and it costs one find.
write_meta() {
  local snap="$1" tag="$2" prev="$3" ts="$4"
  local n bytes changed
  n="$(find "$snap" -type f ! -name .snapshot.json 2>/dev/null | wc -l)"
  bytes="$(find "$snap" -type f ! -name .snapshot.json -printf '%s\n' 2>/dev/null | awk '{s+=$1} END{print s+0}')"
  changed="$(find "$snap" -type f ! -name .snapshot.json -links 1 2>/dev/null | wc -l)"
  printf '{"ts":"%s","tag":"%s","prev":"%s","n_files":%s,"bytes":%s,"changed_vs_prev":%s}\n' \
    "$ts" "$tag" "$prev" "$n" "$bytes" "$changed" > "$snap/.snapshot.json"
}

# --------------------------------------------------------------------------- take
cmd_take() {
  local tag="${1:-}" epoch stamp name prev dest rc
  [[ -d "$BRAIN" ]] || die "brain not found: $BRAIN"
  # MEASURED 2026-09-04, in this script's own acceptance run: a --restore against a throwaway
  # brain wrote its pre-restore snapshot into the REAL snapshot root and moved `latest` onto a
  # one-file tree. `latest` is what --diff compares against and what the next --link-dest uses,
  # so that single stray snapshot would have made the next night's snapshot a full copy and
  # every --diff a lie. A foreign brain therefore needs a foreign root, and says so.
  if [[ "$BRAIN" != "$DEFAULT_BRAIN" && "$SNAP_ROOT" == "$DEFAULT_ROOT" ]]; then
    die "refusing to snapshot $BRAIN into the real snapshot root; set BRAIN_SNAPSHOT_ROOT too" 3
  fi
  mkdir -p "$SNAP_ROOT" || die "cannot create $SNAP_ROOT"

  # One snapshot at a time. Cron (03:15, 06:30) and the two card writers can all fire within
  # the same minute; two rsyncs into names derived from the same clock is how you get a
  # half-written "previous" tree used as the next --link-dest.
  exec 9>"$SNAP_ROOT/.lock"
  flock -w 900 9 || die "another snapshot is running (lock held >900 s)" 4

  epoch="$(now_epoch)"
  stamp="$(snap_stamp "$epoch")"
  name="$stamp${tag:+-$tag}"
  if [[ -e "$SNAP_ROOT/$name" ]]; then
    stamp="$(date -d "@$epoch" +%Y-%m-%dT%H%M%S)"
    name="$stamp${tag:+-$tag}"
    local n=1
    while [[ -e "$SNAP_ROOT/$name" ]]; do name="$stamp${tag:+-$tag}-$n"; n=$((n+1)); done
  fi
  prev="$(newest_snap)"
  dest="$SNAP_ROOT/$name"

  local args=(-a --delete)
  [[ -n "$prev" ]] && args+=(--link-dest="$SNAP_ROOT/$prev")
  mapfile -t exc < <(rsync_excludes)
  rsync "${args[@]}" "${exc[@]}" "$BRAIN/" "$dest/"
  rc=$?
  # 23/24 = "some files vanished or could not be read while we copied". The brain is LIVE:
  # another agent's temp file disappearing mid-run is normal and is not a failed snapshot.
  # Anything else is.
  if [[ $rc -ne 0 && $rc -ne 23 && $rc -ne 24 ]]; then
    echo "rsync failed rc=$rc — snapshot $name is INCOMPLETE" >&2
    exit 4
  fi
  [[ $rc -ne 0 ]] && echo "note: rsync rc=$rc (source files changed during the copy)" >&2

  write_meta "$dest" "$tag" "$prev" "$(date -d "@$epoch" -Is)"
  ln -sfn "$name" "$SNAP_ROOT/latest"
  echo "snapshot $name  <- $BRAIN  (prev: ${prev:-none})"
  cat "$dest/.snapshot.json"
}

# --------------------------------------------------------------------------- list
cmd_list() {
  local s meta n
  n=0
  while read -r s; do
    [[ -n "$s" ]] || continue
    n=$((n+1))
    meta="$SNAP_ROOT/$s/.snapshot.json"
    if [[ -f "$meta" ]]; then
      python3 - "$s" "$meta" <<'PY'
import json, sys
name, path = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(path))
except Exception as exc:                                        # noqa: BLE001
    print(f"{name:34s}  (unreadable .snapshot.json: {exc})"); sys.exit()
print(f"{name:34s}  tag={d.get('tag') or '-':<16s} "
      f"files={d.get('n_files','?'):>6} "
      f"changed={d.get('changed_vs_prev','?'):>6} "
      f"bytes={d.get('bytes',0)/1e6:8.1f}M  {d.get('ts','')}")
PY
    else
      printf '%-34s  (no .snapshot.json — pre-tool snapshot)\n' "$s"
    fi
  done < <(list_snaps)
  [[ $n -eq 0 ]] && echo "(no snapshots in $SNAP_ROOT)"
  local cur; cur="$(readlink "$SNAP_ROOT/latest" 2>/dev/null || true)"
  [[ -n "$cur" ]] && echo "latest -> $cur"
  return 0
}

# --------------------------------------------------------------------------- diff
# `git status`: what does the brain hold now that the snapshot does not, and vice versa.
# diff -rq does not descend into a directory that exists on only one side, so the excluded
# 1.1 GB never gets walked; its "Only in" line is dropped by is_excluded below.
cmd_diff() {
  local which="${1:-latest}" snap
  if [[ "$which" == latest ]]; then
    snap="$(readlink -f "$SNAP_ROOT/latest" 2>/dev/null)"
    [[ -n "$snap" && -d "$snap" ]] || die "no 'latest' snapshot in $SNAP_ROOT"
  else
    snap="$SNAP_ROOT/$which"
    [[ -d "$snap" ]] || die "no such snapshot: $which"
  fi
  local added=0 removed=0 changed=0 special=0 line rel
  local -a out=()
  while IFS= read -r line; do
    case "$line" in
      "Only in $BRAIN"*|"Only in $BRAIN/"*)
        rel="${line#Only in $BRAIN}"; rel="${rel#/}"; rel="${rel%%:*}/${line##*: }"
        rel="${rel#/}"
        is_excluded "$rel" && continue
        added=$((added+1)); out+=("+ $rel") ;;
      "Only in $snap"*)
        rel="${line#Only in $snap}"; rel="${rel#/}"; rel="${rel%%:*}/${line##*: }"
        rel="${rel#/}"
        [[ "$rel" == ".snapshot.json" ]] && continue
        is_excluded "$rel" && continue
        removed=$((removed+1)); out+=("- $rel") ;;
      "Files "*" differ")
        rel="${line#Files }"; rel="${rel%% and *}"; rel="${rel#$BRAIN/}"
        is_excluded "$rel" && continue
        changed=$((changed+1)); out+=("M $rel") ;;
      *)
        # sockets/fifos (tools/.embed-bge.sock) and other non-regular files: diff cannot
        # compare them and says so. Counted, shown, never silently swallowed.
        special=$((special+1)); out+=("? $line") ;;
    esac
  done < <(diff -rq "$BRAIN" "$snap" 2>/dev/null)
  printf '%s\n' "${out[@]}" | grep -v '^$' || true
  echo "--- brain vs $(basename "$snap"): added=$added removed=$removed changed=$changed special=$special"
  return 0
}

# --------------------------------------------------------------------------- restore
# `git checkout -- <path>`: one file or directory, out of one snapshot, back into the brain.
# It takes a pre-restore snapshot FIRST, because a restore is itself a write that can be the
# wrong one — restoring yesterday's card over today's correction is the same class of loss the
# tool exists to undo, and the undo of the undo has to exist before the undo runs.
cmd_restore() {
  khms_require_writable "restore into the base" || exit 3
  local which="${1:-}" rel="${2:-}" snap src dst real
  [[ -n "$which" && -n "$rel" ]] || die "usage: --restore <snapshot|latest> <path-relative-to-brain>"
  if [[ "$which" == latest ]]; then
    snap="$(readlink -f "$SNAP_ROOT/latest" 2>/dev/null)"
    [[ -n "$snap" && -d "$snap" ]] || die "no 'latest' snapshot in $SNAP_ROOT"
  else
    snap="$SNAP_ROOT/$which"; [[ -d "$snap" ]] || die "no such snapshot: $which"
  fi
  # REFUSE ANYTHING THAT LEAVES THE BRAIN, before touching a byte. An absolute path or a `..`
  # would let a restore write into ~/.claude or /etc with the caller believing it was scoped
  # to the base; the check is on the RESOLVED path, so a symlink inside the brain cannot
  # smuggle the write out either.
  case "$rel" in
    /*)     die "refusing an absolute path: $rel" 3 ;;
    *..*)   die "refusing a path containing '..': $rel" 3 ;;
  esac
  src="$snap/$rel"
  [[ -e "$src" ]] || die "not in $(basename "$snap"): $rel"
  dst="$BRAIN/$rel"
  real="$(readlink -m "$dst")"
  [[ "$real" == "$BRAIN/"* ]] || die "refusing to write outside the brain: $real" 3

  echo "pre-restore snapshot first:"
  ( cmd_take "pre-restore" ) || die "pre-restore snapshot failed — nothing restored" 4

  mkdir -p "$(dirname "$dst")"
  if [[ -d "$src" ]]; then
    rsync -a "$src/" "$dst/"
  else
    # --remove-destination unlinks the target before writing: without it, cp would write
    # THROUGH the existing inode, and an inode the brain shares with anything else would be
    # rewritten behind its back.
    cp -a --remove-destination "$src" "$dst"
  fi
  echo "restored $rel  <- $(basename "$snap")"
}

# --------------------------------------------------------------------------- prune
# Keep everything for 30 days, then one per ISO week for a year, then one per month. The
# tiers exist because the questions change with age: this week you ask "what did the nightly
# do to this card last night", and a year out you ask "what did the base look like in March".
# NEVER removed: whatever `latest` points at, and the pre-audit-impl tag — the last tree from
# before this whole implementation, which is the rollback point for it.
cmd_prune() {
  local dry="${1:-}" now keep_reason latest_target
  now="$(now_epoch)"
  latest_target="$(readlink "$SNAP_ROOT/latest" 2>/dev/null || true)"
  local -A claimed=()
  local -a removed=() kept=()
  local s ts age_d label key
  # newest first: the first snapshot to claim a bucket is the one kept
  while read -r s; do
    [[ -n "$s" ]] || continue
    ts="$(snap_time "$s" 2>/dev/null)" || { kept+=("$s  KEEP unparsable-name"); continue; }
    age_d=$(( (now - ts) / 86400 ))
    label="$(snap_label "$s")"
    keep_reason=""
    if [[ "$s" == "$latest_target" ]]; then keep_reason="latest"
    elif [[ "$label" == "pre-audit-impl" ]]; then keep_reason="tagged-pre-audit-impl"
    elif (( age_d <= 30 )); then keep_reason="within-30d"
    fi
    if (( age_d <= 30 )); then
      key=""
    elif (( age_d <= 364 )); then
      key="W:$(date -d "@$ts" +%G-%V)"
    else
      key="M:$(date -d "@$ts" +%Y-%m)"
    fi
    if [[ -n "$key" ]]; then
      if [[ -z "${claimed[$key]:-}" ]]; then
        claimed[$key]="$s"
        [[ -z "$keep_reason" ]] && keep_reason="first-in-$key"
      else
        # forced keeps (latest / tagged) still hold their bucket, so the tier does not also
        # keep a second snapshot from the same week just because the first was special
        claimed[$key]="${claimed[$key]}"
      fi
    fi
    if [[ -n "$keep_reason" ]]; then
      kept+=("$s  KEEP $keep_reason")
    else
      removed+=("$s")
    fi
  done < <(list_snaps | sort -r)

  local x
  for x in "${kept[@]:-}"; do [[ -n "$x" ]] && echo "$x"; done
  for x in "${removed[@]:-}"; do
    [[ -n "$x" ]] || continue
    if [[ "$dry" == "--dry" ]]; then
      echo "$x  WOULD REMOVE"
    else
      echo "$x  REMOVE"
      rm -rf -- "${SNAP_ROOT:?}/$x"
    fi
  done
  echo "--- prune: kept=${#kept[@]} removed=${#removed[@]}${dry:+ (dry run)}"
  return 0
}

# --------------------------------------------------------------------------- argv
# Take and prune compose (`--tag post-review --prune` is the morning cron line); the three
# read-only commands are exclusive. `--prune` alone prunes and takes nothing — a cleanup must
# never be able to create the thing it is cleaning up after.
main() {
  local tag="" do_prune=0 dry="" mode="" want_take=0 diff_arg="latest" r1="" r2=""
  [[ $# -eq 0 ]] && want_take=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help) usage; exit 0 ;;
      --list)    [[ -n "$mode" ]] && die "--list cannot be combined with --$mode"; mode=list; shift ;;
      --diff)    [[ -n "$mode" ]] && die "--diff cannot be combined with --$mode"; mode=diff; shift
                 if [[ $# -gt 0 && "$1" != --* ]]; then diff_arg="$1"; shift; fi ;;
      --restore) [[ -n "$mode" ]] && die "--restore cannot be combined with --$mode"; mode=restore
                 r1="${2:-}"; r2="${3:-}"; shift $(( $# < 3 ? $# : 3 )) ;;
      --prune)   do_prune=1; shift ;;
      --dry)     dry="--dry"; shift ;;
      --tag)     tag="${2:-}"; [[ -n "$tag" ]] || die "--tag needs a label"; want_take=1; shift 2 ;;
      *)         die "unknown option: $1" ;;
    esac
  done
  case "$mode" in
    list)    cmd_list; return ;;
    diff)    cmd_diff "$diff_arg"; return ;;
    restore) cmd_restore "$r1" "$r2"; return ;;
  esac
  [[ -n "$dry" && $do_prune -eq 0 ]] && die "--dry only means something with --prune"
  (( want_take )) && cmd_take "$tag"
  (( do_prune ))  && cmd_prune "$dry"
  return 0
}
main "$@"
