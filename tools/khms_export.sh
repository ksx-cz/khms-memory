#!/usr/bin/env bash
# Build a read-only export of the knowledge base for another machine: cards in, manifest on top.
#
# WHY A COPY AND NOT A QUERY ACROSS THE NETWORK. A device in the field is asked "why was this
# built this way" exactly when nobody can reach the coordinator's machine, and an answer that
# needs that machine is an answer it does not have. A base is megabytes of markdown; copying it
# is cheaper than any protocol for not copying it.
#
# WHAT IT PRODUCES
#   <target>/know/K-*.md          the active cards, verbatim
#   <target>/archive/know/K-*.md  the archived ones (only with --with-archive)
#   <target>/manifest.json        count, build time, source host, byte size
#
# A consumer reads this layout and should fail honestly when the directory or the manifest is
# missing rather than answer from nothing.
#
# IDEMPOTENT, AND THE RSYNC IS WHY. Running it twice over the same target is a
# no-op except for the manifest's timestamp: rsync copies only what changed and
# --delete removes cards that left the base (a card that was archived must stop
# being an active hit on the consumer). The --delete is bounded to a directory
# this script owns: it REFUSES a target that exists and is neither empty nor a
# previous export, so a mistyped path cannot delete somebody's files.
#
# USAGE
#   tools/khms_export.sh <target-dir> [--with-archive]     (the base: KHMS_ROOT, see khms_paths.sh)
#   tools/khms_export.sh --help
#
# DEPLOY (the second half, the deployment's own job): copy <target-dir>/ to the consumer,
#   e.g. rsync -a --delete <target-dir>/ <host>:<path>/
# The export holds this clone's shared code-use lock while it runs (an update waits for it).
set -euo pipefail

CODE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TARGET=""
WITH_ARCHIVE=0

usage() { sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
  case "$arg" in
    --with-archive) WITH_ARCHIVE=1 ;;
    -h|--help)      usage; exit 0 ;;
    -*)             echo "unknown option: $arg" >&2; exit 2 ;;
    *)              TARGET="$arg" ;;
  esac
done

# shellcheck source=khms_paths.sh
. "$CODE/khms_paths.sh"
khms_load || exit $?
python3 "$CODE/khms_health.py" || exit 3
BRAIN="$(khms_get KHMS_ROOT)"
KNOW="${BRAIN}/memory/know"
ARCHIVE="${BRAIN}/memory/archive/know"

if [[ -z "$TARGET" ]]; then
  echo "khms_export.sh: a target directory is required" >&2
  usage >&2
  exit 2
fi

if [[ ! -d "$KNOW" ]]; then
  echo "khms_export.sh: no cards at $KNOW" >&2
  exit 1
fi
khms_code_lock || exit $?

# THE GUARDS IN FRONT OF EVERY WRITE, all checked before the first one, on CANONICAL paths (symlinks and
# `..` resolved, so an alias cannot walk around them). Every path this script mutates -- the target, its
# know/, archive/, archive/know/ and manifest.json -- must
#   1. be writable for this deployment: a reader (KHMS_READ_ONLY=1) may build an independent copy OUTSIDE
#      the base, its config and the shared state, never inside them (khms_may_write);
#   2. not overlap a SOURCE tree (the base's know/ and archive/know/): not the tree, not inside it, not an
#      ancestor of it -- `rm -rf <target>/archive` with <target> = <base>/memory would delete the source
#      archive, and an rsync --delete into the source would empty it.
# And the target must not exist, be empty, or carry a manifest THIS tool wrote (its "tool" field names
# khms_export.sh, or build_kb_snapshot.sh, the name of its predecessor): a file merely called manifest.json
# proves nothing, so anything else is somebody's directory and is left alone.
canon() { realpath -m -- "$1"; }
overlaps() {   # overlaps A B -> rc 0 when A is B, inside B, or an ancestor of B (canonical paths)
  local a b; a="$(canon "$1")"; b="$(canon "$2")"
  [ "$a" = "$b" ] && return 0
  case "$a/" in "${b%/}/"*) return 0 ;; esac
  case "$b/" in "${a%/}/"*) return 0 ;; esac
  return 1
}
for p in "$TARGET" "$TARGET/know" "$TARGET/archive" "$TARGET/archive/know" "$TARGET/manifest.json"; do
  if ! khms_may_write "$p"; then
    echo "khms_export.sh: read-only deployment (KHMS_READ_ONLY=1): $(canon "$p") is inside the base," >&2
    echo "  its config or the shared state; export to a directory outside them. Nothing was written." >&2
    exit 4
  fi
  for src in "$KNOW" "$ARCHIVE"; do
    if overlaps "$p" "$src"; then
      echo "khms_export.sh: $(canon "$p") overlaps the source tree $(canon "$src") —" >&2
      echo "  refusing: an export never writes to or deletes from what it reads. Nothing was written." >&2
      exit 1
    fi
  done
done
if [[ -e "$TARGET" ]]; then
  if [[ ! -d "$TARGET" ]]; then
    echo "khms_export.sh: $TARGET exists and is not a directory" >&2
    exit 1
  fi
  if [[ -n "$(ls -A "$TARGET" 2>/dev/null)" ]] \
     && ! grep -Eq '"tool": "(khms_export\.sh|build_kb_snapshot\.sh)"' "$TARGET/manifest.json" 2>/dev/null; then
    echo "khms_export.sh: $TARGET is not empty and holds no manifest written by this tool —" >&2
    echo "  refusing to --delete inside a directory this script did not build." >&2
    exit 1
  fi
fi
TARGET="$(canon "$TARGET")"

mkdir -p "$TARGET/know"
rsync -a --delete --delete-excluded --include='K-[0-9][0-9][0-9][0-9][0-9].md' --exclude='*' "$KNOW/" "$TARGET/know/"

if [[ "$WITH_ARCHIVE" -eq 1 && -d "$ARCHIVE" ]]; then
  mkdir -p "$TARGET/archive/know"
  rsync -a --delete --delete-excluded --include='K-[0-9][0-9][0-9][0-9][0-9].md' --exclude='*' \
        "$ARCHIVE/" "$TARGET/archive/know/"
else
  rm -rf "$TARGET/archive"
fi

CARDS="$(find "$TARGET/know" -maxdepth 1 -name 'K-*.md' | wc -l | tr -d ' ')"
ARCHIVED=0
if [[ -d "$TARGET/archive/know" ]]; then
  ARCHIVED="$(find "$TARGET/archive/know" -maxdepth 1 -name 'K-*.md' | wc -l | tr -d ' ')"
fi
BYTES="$(du -sb "$TARGET" | cut -f1)"
BUILT="$(date -Is)"

# The manifest is written LAST and its mtime is what a consumer watches to
# notice a refresh (it can stat it before every search), so writing it
# before the cards would announce a snapshot that is still being copied.
cat > "$TARGET/manifest.json" <<JSON
{
  "built": "${BUILT}",
  "cards": ${CARDS},
  "archived_cards": ${ARCHIVED},
  "bytes": ${BYTES},
  "source_host": "$(hostname)",
  "source_dir": "${KNOW}",
  "tool": "khms_export.sh"
}
JSON

echo "kb export: ${CARDS} cards (+${ARCHIVED} archived), $((BYTES / 1024)) kB -> ${TARGET}"
