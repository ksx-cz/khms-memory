#!/usr/bin/env bash
# Dispatch an entrypoint from this KHMS clone using one resolved config and personal state.
set -euo pipefail
TOOLS="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
[[ -r "$TOOLS/khms_paths.py" ]] || { echo "khms_exec: missing resolver: $TOOLS" >&2; exit 3; }
if [[ "${KHMS_EXEC_PRESOLVED:-}" == 1 && -n "${KHMS_STATE:-}" ]]; then
  # The caller resolved and validated this identity once (a deployment's install step) and exports the
  # resolved KHMS_CONFIG_FILE and KHMS_STATE: skip the two resolver runs, ~30 ms on every hook call.
  STATE="$KHMS_STATE"
else
  # Validate the explicitly selected base/config before invoking any entrypoint.
  python3 "$TOOLS/khms_paths.py" --root >/dev/null
  STATE="$(python3 "$TOOLS/khms_paths.py" KHMS_STATE)"
fi
unset KHMS_EXEC_PRESOLVED              # the promise covers this dispatch only, not what the entrypoint starts
# Both retrieval and claim verification consume the same personal evidence files.
# An inherited historical log override must not route evidence to another agent.
export KHMS_RECALL_LOG="$STATE/recall.log"
export KHMS_RECALL_JSONL="$STATE/recall.jsonl"
name="${1:-}"; [ -n "$name" ] || { echo 'khms_exec: name an entrypoint' >&2; exit 3; }; shift
[[ "$name" != /* && "/$name/" != */../* && "/$name/" != */./* ]] || {
  echo 'khms_exec: entrypoint must be a contained relative path' >&2; exit 3;
}
entry="$(realpath -m "$TOOLS/$name")"
[[ "$entry" == "$TOOLS/"* ]] || { echo 'khms_exec: entrypoint escapes tools' >&2; exit 3; }
[[ -r "$entry" && -f "$entry" ]] || { echo "khms_exec: missing entrypoint: $TOOLS/$name" >&2; exit 3; }
case "$name" in
  embed_index.py|embed_server.py)
    interpreter="$(python3 "$TOOLS/khms_paths.py" KHMS_EMBED_PY)"
    exec "$interpreter" "$entry" "$@" ;;
  *.py) exec python3 "$entry" "$@" ;;
  *.sh) exec bash "$entry" "$@" ;;
  *) echo 'khms_exec: expected a Python or shell entrypoint' >&2; exit 3 ;;
esac
