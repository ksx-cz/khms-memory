#!/usr/bin/env bash
# Create (or complete) a KHMS data base. Idempotent and non-copying.
#
# Usage: khms_init.sh [--register] <base-dir>        (or: KHMS_ROOT=<base-dir> khms_init.sh [--register])
#
# First it resolves the effective configuration exactly as every tool will (khms_paths.sh: the environment,
# an explicit KHMS_CONFIG_FILE, the base's own config/khms.env), with <base-dir> as the root. A reader
# deployment (KHMS_READ_ONLY=1 from any of them) is refused with rc 4 and an unresolvable configuration with
# rc 3 -- both before a single write. Without <base-dir> the root is the one the configuration names.
# What it does then, and nothing else:
#   * creates the data directories (memory/, journal/, plans/, archive/, reports/) beneath <base-dir> and
#     the CONFIGURED config, agent-state and shared-state directories (KHMS_CONFIG_DIR, KHMS_STATE,
#     KHMS_SHARED_STATE), wherever the configuration puts them;
#   * creates the card counter in the configured shared state -- the one the allocator reads -- ONLY when
#     there is none; an existing counter, existing cards, settings, tag registry and journal are never
#     touched; a new counter starts above the highest card id already on disk;
#   * writes a commented settings template to <base-dir>/config/khms.env only when that file is absent and
#     no other settings file is selected by KHMS_CONFIG_FILE;
#   * with --register, records <base-dir> in ~/.config/khms/root so tools find it without KHMS_ROOT. An
#     existing registration of a DIFFERENT base is refused, never replaced.
# It never copies code: every tool runs from this checkout and finds the base through khms_paths.
# Cards only ever arrive through review (approve_inbox.py); this script writes none.
set -euo pipefail

CODE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REGISTER=0
ROOT=""
for a in "$@"; do
  case "$a" in
    --register) REGISTER=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    -*) echo "khms_init: unknown option $a" >&2; exit 2 ;;
    *) [ -z "$ROOT" ] || { echo "khms_init: one base directory, please" >&2; exit 2; }; ROOT="$a" ;;
  esac
done
ROOT="${ROOT:-${KHMS_ROOT:-}}"
[ -n "$ROOT" ] || [ -n "${KHMS_CONFIG_FILE:-}" ] \
  || { echo "khms_init: name the base directory (argument, KHMS_ROOT, or KHMS_ROOT in KHMS_CONFIG_FILE)" >&2
       exit 2; }

# Resolve the configuration BEFORE any write; nothing below may touch the filesystem until this passed.
# shellcheck source=khms_paths.sh
. "$CODE/khms_paths.sh"
if [ -n "$ROOT" ]; then
  ROOT="$(realpath -m -- "$ROOT")"
  export KHMS_ROOT="$ROOT"
fi
khms_load || exit 3
khms_require_writable "initialize the base" || exit $?
if [ -n "${KHMS_CONFIG_FILE:-}" ] && [ -n "${_KHMS_RAW[KHMS_ROOT]:-}" ]; then
  # An explicitly selected settings file that names a DIFFERENT root would send every later tool without
  # KHMS_ROOT to that other base while this one is initialized: refuse instead of preparing a layout nobody
  # reads. The file's own KHMS_ROOT must resolve, too.
  named="$(unset KHMS_ROOT; khms_load && khms_get KHMS_ROOT)" || exit 3
  if [ "$(realpath -m -- "$named")" != "$(realpath -m -- "$(khms_get KHMS_ROOT)")" ]; then
    echo "khms_init: $KHMS_CONFIG_FILE names KHMS_ROOT=$named, not $(khms_get KHMS_ROOT) -- refusing" >&2
    exit 3
  fi
fi
resolve_dir() { local v; v="$(khms_get "$1")" || exit 3; realpath -m -- "$v"; }
ROOT="$(resolve_dir KHMS_ROOT)"
CONFIG_DIR="$(resolve_dir KHMS_CONFIG_DIR)"
STATE="$(resolve_dir KHMS_STATE)"
SHARED="$(resolve_dir KHMS_SHARED_STATE)"

REPO="$(dirname "$CODE")"
for d in "$ROOT" "$CONFIG_DIR" "$STATE" "$SHARED"; do
  case "$d/" in "$REPO/"*)
    echo "khms_init: refusing to put $d inside the code checkout ($REPO); data never lives in a clone" >&2
    exit 3 ;;
  esac
done

mkdir -p "$ROOT"/memory/{know,views/topics,views/by-type,inbox/.staging,archive/know,tombstones} \
         "$ROOT"/{journal,plans,archive,reports} "$CONFIG_DIR" "$STATE" "$SHARED"

created=()
COUNTER="$SHARED/next_id"
if [ ! -e "$COUNTER" ]; then
  hi=0
  for f in "$ROOT"/memory/know/K-*.md "$ROOT"/memory/archive/know/K-*.md; do
    [ -e "$f" ] || continue
    n="${f##*/K-}"; n="${n%.md}"; n=$((10#$n))
    [ "$n" -gt "$hi" ] && hi="$n"
  done
  echo "$((hi + 1))" > "$COUNTER"
  created+=("counter $COUNTER = $((hi + 1))")
fi

if [ ! -e "$ROOT/memory/views/tags.md" ]; then
  cat > "$ROOT/memory/views/tags.md" <<'EOF'
# Tag registry

| tag | group | description | aliases | cards |
|---|---|---|---|---|
| core | meta | always-loaded card, listed in MEMORY.md §2 | - | 0 |
| khms | meta | the memory system itself | memory | 0 |
| gotcha | meta | a trap: surfaced by precheck.sh, protected from condensation | - | 0 |
EOF
  created+=("tag registry")
fi

if [ -z "${KHMS_CONFIG_FILE:-}" ] && [ ! -e "$ROOT/config/khms.env" ]; then
  mkdir -p "$ROOT/config"
  cat > "$ROOT/config/khms.env" <<'EOF'
# KHMS settings for this base. KEY=VALUE, `#` comments, ${NAME} references; never sourced by a shell.
# Every key is optional: the documented default applies when a key is absent or empty
# (python3 <khms checkout>/tools/khms_paths.py --all prints the resolved values).
#
# KHMS_STATE=${KHMS_ROOT}/state                      this agent's logs and caches
# KHMS_SHARED_STATE=${KHMS_ROOT}/state/shared        allocator, writer locks, checkpoints
# KHMS_READ_ONLY=0                                   1 = a reader: no data writes, no id allocation
# KHMS_GLOSSARY=${KHMS_CONFIG_DIR}/glossary.txt       query expansion (absent = none)
# KHMS_EMBED_PY=/path/to/venv/bin/python3            enables the dense channel (absent = lexical only)
# KHMS_TRANSCRIPTS=/path/to/session/transcripts       source of the injection-usage report
EOF
  created+=("settings template config/khms.env")
fi

TODAY="$ROOT/journal/$(date +%F).md"
[ -e "$TODAY" ] || { printf '# %s\n\n' "$(date +%F)" > "$TODAY"; created+=("journal $(date +%F)"); }

if [ "$REGISTER" = 1 ]; then
  [ -n "${HOME:-}" ] || { echo "khms_init: --register needs HOME" >&2; exit 2; }
  REG="$HOME/.config/khms/root"
  if [ -e "$REG" ]; then
    cur="$(head -n1 "$REG")"
    if [ "$cur" != "$ROOT" ]; then
      echo "khms_init: $REG already registers $cur -- refusing to replace it with $ROOT" \
           "(remove that file yourself if the replacement is intended)" >&2
      exit 3
    fi
  else
    mkdir -p "$(dirname "$REG")"
    printf '%s\n' "$ROOT" > "$REG"
    created+=("registration $REG")
  fi
fi

echo "KHMS base ready: $ROOT"
if [ ${#created[@]} -eq 0 ]; then
  echo "  nothing created -- the base was already complete"
else
  for c in "${created[@]}"; do echo "  created: $c"; done
fi
cat <<EOF

Next:
  1. point the tools at it: export KHMS_ROOT="$ROOT"   (or run this again with --register)
  2. load the bootstrap cards: put candidate cards (see spec/) into $ROOT/memory/inbox/,
     review them, then run  $CODE/approve_inbox.py <file>  and  $CODE/build_views.py
  3. wire the hooks: see claude-code/README.md
EOF
