#!/usr/bin/env bash
# Create (or complete) a KHMS data base. Idempotent and non-copying.
#
# Usage: khms_init.sh [--register] <base-dir>        (or: KHMS_ROOT=<base-dir> khms_init.sh [--register])
#
# What it does, and nothing else:
#   * creates the data directories (memory/, journal/, plans/, archive/, reports/), the config directory
#     and the state directories beneath <base-dir>;
#   * creates the card counter in the shared state ONLY when there is none -- an existing counter, existing
#     cards, settings, tag registry and journal are never touched; a new counter starts above the highest
#     card id already on disk;
#   * writes a commented settings template to <base-dir>/config/khms.env only when that file is absent;
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
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    -*) echo "khms_init: unknown option $a" >&2; exit 2 ;;
    *) [ -z "$ROOT" ] || { echo "khms_init: one base directory, please" >&2; exit 2; }; ROOT="$a" ;;
  esac
done
ROOT="${ROOT:-${KHMS_ROOT:-}}"
[ -n "$ROOT" ] || { echo "khms_init: name the base directory (argument or KHMS_ROOT)" >&2; exit 2; }
[ "${KHMS_READ_ONLY:-0}" = 1 ] && { echo "khms_init: refusing on a read-only deployment (KHMS_READ_ONLY=1)" >&2; exit 4; }
ROOT="$(realpath -m -- "$ROOT")"
REPO="$(dirname "$CODE")"
case "$ROOT/" in "$REPO/"*)
  echo "khms_init: refusing to put a base inside the code checkout ($REPO); data never lives in a clone" >&2
  exit 3 ;;
esac

mkdir -p "$ROOT"/memory/{know,views/topics,views/by-type,inbox/.staging,archive/know,tombstones} \
         "$ROOT"/{journal,plans,archive,reports,config} "$ROOT"/state/shared

created=()
COUNTER="$ROOT/state/shared/next_id"
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

if [ ! -e "$ROOT/config/khms.env" ]; then
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
