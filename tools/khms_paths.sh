#!/usr/bin/env bash
# khms_paths.sh -- the shell half of the KHMS location resolver; khms_paths.py is the other half.
#
# Usage (sourced):   . "$(dirname "$(readlink -f "$0")")/khms_paths.sh"; khms_load || exit $?
#                    root="$(khms_get KHMS_ROOT)"; khms_require_writable "allocate ids" || exit $?
# Usage (executed):  khms_paths.sh KEY | --all | --root | --code-lock-file   (same output and exit codes as
#                    khms_paths.py)
#
# Both readers implement ONE contract, documented in khms_paths.py: root discovery ($KHMS_ROOT, then
# KHMS_ROOT in $KHMS_CONFIG_FILE, then ~/.config/khms/root, else an error), settings file selection,
# env > file > default precedence, and the KEY=VALUE grammar with ${NAME} references. Cycles, unresolved
# references, malformed lines, a missing explicitly selected file and a KHMS_READ_ONLY other than 0/1 fail
# with rc 3. The file is parsed line by line and never sourced or evaluated.

KHMS_CODE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

_KHMS_KEYS=(KHMS_ROOT KHMS_CONFIG_FILE KHMS_CONFIG_DIR KHMS_STATE KHMS_SHARED_STATE KHMS_SHARED KHMS_RUNTIME KHMS_READ_ONLY
  KHMS_RECALL_LOG KHMS_RECALL_JSONL KHMS_INJECT_LOG KHMS_PRECHECK_LOG KHMS_CLAIM_LOG KHMS_PROPOSAL_LOG KHMS_STOP_LOG
  KHMS_RECALL_INTENT KHMS_HOOK_STATE
  KHMS_HOOKS_OFF_FILE KHMS_LEN_CACHE KHMS_EXPERIMENT KHMS_GLOSSARY KHMS_DIRECTIVE KHMS_EMBED_DIR
  KHMS_EMBED_PY KHMS_DENSE_SOCKET KHMS_HF_HOME KHMS_TERM_GLOSSARY KHMS_REPO KHMS_REPO_INCLUDE KHMS_DAV_URL KHMS_DAV_USER KHMS_DAV_BASE KHMS_DAV_NETRC KHMS_TRANSCRIPTS KHMS_MESSAGES KHMS_PATTERNS
  KHMS_NIGHTLY_LOCK KHMS_WEEKLY_LOCK KHMS_NIGHT_STAMP KHMS_WEEK_STAMP KHMS_SWEEP_FAILED KHMS_NIGHT_LOG
  KHMS_AGENT KHMS_AGENT_FROM_PATH KHMS_AGENT_TRANSCRIPTS KHMS_SWEEP_MODEL KHMS_MARK_MODEL KHMS_CONS_MODEL
  KHMS_WEEK_PART_MODEL KHMS_WEEK_MODEL KHMS_PROJECT_SLUG KHMS_GIT_REPOS KHMS_CHAT_FETCH KHMS_CHAT_VERIFY
  KHMS_NIGHT_TRIAGE KHMS_SPEC KHMS_PRINCIPAL KHMS_PROMPT_EXT KHMS_ACCOUNT_DIR
  KHMS_PING_DEPUTY KHMS_REVIEW_DEPUTY_TRANSPORT KHMS_JUDGE_EXPERIMENT_CONFIG KHMS_METRICS_CONFIG
  KHMS_PING_SEND KHMS_PING_EXTRA KHMS_JUDGE KHMS_JUDGE_MAX_USD KHMS_LLM_URL KHMS_LLM_MODEL
  KHMS_REPORT_TOOL KHMS_REPORT_CHANNEL KHMS_REPORT_SERVER)
declare -gA _KHMS_DEFAULT=(
  [KHMS_CONFIG_FILE]="" [KHMS_CONFIG_DIR]='${KHMS_ROOT}/config' [KHMS_STATE]='${KHMS_ROOT}/state'
  [KHMS_SHARED]="0" [KHMS_RUNTIME]="" [KHMS_SHARED_STATE]='${KHMS_ROOT}/state/shared' [KHMS_READ_ONLY]="0"
  [KHMS_RECALL_LOG]='${KHMS_STATE}/recall.log' [KHMS_RECALL_JSONL]='${KHMS_STATE}/recall.jsonl'
  [KHMS_INJECT_LOG]='${KHMS_STATE}/inject.log' [KHMS_PRECHECK_LOG]='${KHMS_STATE}/precheck.log'
  [KHMS_CLAIM_LOG]='${KHMS_STATE}/claim_gate.log' [KHMS_PROPOSAL_LOG]='${KHMS_STATE}/proposal_gate.log'
  [KHMS_STOP_LOG]='${KHMS_STATE}/stop_gate.log' [KHMS_RECALL_INTENT]='${KHMS_STATE}/recall_intent.jsonl'
  [KHMS_HOOK_STATE]='${KHMS_STATE}/hook-state'
  [KHMS_HOOKS_OFF_FILE]='${KHMS_STATE}/hooks-off' [KHMS_LEN_CACHE]='${KHMS_STATE}/cardlen.json'
  [KHMS_EXPERIMENT]='${KHMS_CONFIG_DIR}/khms_experiment.json' [KHMS_GLOSSARY]="" [KHMS_DIRECTIVE]=""
  [KHMS_EMBED_DIR]='${KHMS_RUNTIME}/embed' [KHMS_EMBED_PY]="" [KHMS_DENSE_SOCKET]=""
  [KHMS_TERM_GLOSSARY]="" [KHMS_REPO]="" [KHMS_REPO_INCLUDE]="*" [KHMS_DAV_URL]="" [KHMS_DAV_USER]="" [KHMS_DAV_BASE]="" [KHMS_DAV_NETRC]="" [KHMS_HF_HOME]="" [KHMS_TRANSCRIPTS]=""
  [KHMS_MESSAGES]='${KHMS_CONFIG_DIR}/messages.env' [KHMS_PATTERNS]='${KHMS_CONFIG_DIR}/patterns.env'
  [KHMS_NIGHTLY_LOCK]='${KHMS_RUNTIME}/nightly.lock' [KHMS_WEEKLY_LOCK]='${KHMS_RUNTIME}/weekly.lock'
  [KHMS_NIGHT_STAMP]='${KHMS_SHARED_STATE}/last-nightly' [KHMS_WEEK_STAMP]='${KHMS_SHARED_STATE}/last-weekly'
  [KHMS_SWEEP_FAILED]='${KHMS_SHARED_STATE}/sweep-failed' [KHMS_NIGHT_LOG]='${KHMS_STATE}/nightly.log'
  [KHMS_AGENT]="claude" [KHMS_AGENT_FROM_PATH]="0"
  [KHMS_AGENT_TRANSCRIPTS]='${HOME}/.claude/projects' [KHMS_SWEEP_MODEL]="sonnet" [KHMS_MARK_MODEL]="sonnet"
  [KHMS_CONS_MODEL]="sonnet" [KHMS_WEEK_PART_MODEL]="sonnet" [KHMS_WEEK_MODEL]="opus"
  [KHMS_PROJECT_SLUG]="" [KHMS_GIT_REPOS]="" [KHMS_CHAT_FETCH]="" [KHMS_CHAT_VERIFY]="" [KHMS_NIGHT_TRIAGE]=""
  [KHMS_SPEC]="" [KHMS_PRINCIPAL]="the principal" [KHMS_PROMPT_EXT]='${KHMS_CONFIG_DIR}/prompts' [KHMS_ACCOUNT_DIR]=""
  [KHMS_REVIEW_DEPUTY_TRANSPORT]="" [KHMS_JUDGE_EXPERIMENT_CONFIG]="" [KHMS_METRICS_CONFIG]=""
  [KHMS_PING_DEPUTY]="" [KHMS_PING_SEND]="" [KHMS_PING_EXTRA]="" [KHMS_JUDGE]="sonnet" [KHMS_JUDGE_MAX_USD]=""
  [KHMS_LLM_URL]="" [KHMS_LLM_MODEL]="qwen3-coder-next:latest"
  [KHMS_REPORT_TOOL]="" [KHMS_REPORT_CHANNEL]=""
  [KHMS_REPORT_SERVER]="")
declare -gA _KHMS_RAW=()
_KHMS_ROOT_RESOLVED=""
_KHMS_CFG_RESOLVED=""

_khms_err() { echo "khms_paths: $*" >&2; return 3; }

_khms_parse() {   # _khms_parse FILE -> fills _KHMS_RAW; rc 3 on malformed/duplicate keys
  local file="$1" line s v key n=0
  local -A first_lines=()
  _KHMS_RAW=()
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    s="${line#"${line%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"
    [ -z "$s" ] && continue
    [ "${s:0:1}" = "#" ] && continue
    [[ "$s" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] \
      || { _khms_err "malformed line $n in $file (expected KEY=VALUE)"; return 3; }
    v="${BASH_REMATCH[2]}"
    key="${BASH_REMATCH[1]}"
    if [ -n "${first_lines[$key]:-}" ]; then
      _khms_err "duplicate key $key at line $n in $file (first defined at line ${first_lines[$key]})"
      return 3
    fi
    first_lines[$key]="$n"
    v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"
    if [ "${#v}" -ge 2 ] && [ "${v:0:1}" = "${v: -1}" ] && { [ "${v:0:1}" = '"' ] || [ "${v:0:1}" = "'" ]; }; then
      v="${v:1:${#v}-2}"
    fi
    _KHMS_RAW["$key"]="$v"
  done < "$file"
}

_khms_expand() {  # _khms_expand RAW STACK(space separated, first = requested key) -> expanded value
  local raw="$1" stack="$2" out="" rest="$1" name sub
  while [[ "$rest" =~ \$\{([A-Za-z_][A-Za-z0-9_]*)\} ]]; do
    name="${BASH_REMATCH[1]}"
    case " $stack " in *" $name "*)
      local first="${stack%% *}"
      _khms_err "$first refers to itself through ${stack// / -> } -> $name"; return 3 ;;
    esac
    out+="${rest%%"${BASH_REMATCH[0]}"*}"
    sub="$(_khms_resolve "$name" "$stack $name")" || return 3
    out+="$sub"
    rest="${rest#*"${BASH_REMATCH[0]}"}"
  done
  printf '%s' "$out$rest"
}

_khms_resolve() { # _khms_resolve KEY STACK
  local key="$1" stack="$2"
  if [ "$key" = KHMS_ROOT ] && [ -n "$_KHMS_ROOT_RESOLVED" ]; then printf '%s' "$_KHMS_ROOT_RESOLVED"; return 0; fi
  if [ "$key" = KHMS_CONFIG_FILE ] && [ -n "$_KHMS_ROOT_RESOLVED" ]; then printf '%s' "$_KHMS_CFG_RESOLVED"; return 0; fi
  if [ -n "${!key:-}" ]; then printf '%s' "${!key}"; return 0; fi
  if [ -n "${_KHMS_RAW[$key]:-}" ]; then _khms_expand "${_KHMS_RAW[$key]}" "$stack"; return $?; fi
  if [ "$key" = KHMS_RUNTIME ]; then
    python3 "$KHMS_CODE/khms_runtime.py" "$_KHMS_ROOT_RESOLVED"; return $?
  fi
  if [ -n "${_KHMS_DEFAULT[$key]+x}" ] && [ "$key" != KHMS_ROOT ]; then
    _khms_expand "${_KHMS_DEFAULT[$key]}" "$stack"; return $?
  fi
  _khms_err "${stack%% *} refers to \${$key}, which nothing defines"
}

khms_load() {     # discover root and settings file; rc 3 with a reason when impossible
  local cfg="${KHMS_CONFIG_FILE:-}" root="${KHMS_ROOT:-}" reg other
  _KHMS_RAW=(); _KHMS_ROOT_RESOLVED=""; _KHMS_CFG_RESOLVED=""
  if [ -n "$cfg" ]; then
    [ -f "$cfg" ] || { _khms_err "config file $cfg does not exist (named by KHMS_CONFIG_FILE)"; return 3; }
    [ -r "$cfg" ] || { _khms_err "cannot read config file $cfg"; return 3; }
    _khms_parse "$cfg" || return 3
  fi
  if [ -z "$root" ] && [ -n "$cfg" ] && [ -n "${_KHMS_RAW[KHMS_ROOT]:-}" ]; then
    root="$(_khms_expand "${_KHMS_RAW[KHMS_ROOT]}" "KHMS_ROOT")" || return 3
  fi
  if [ -z "$root" ] && [ -n "${HOME:-}" ]; then
    reg="$HOME/.config/khms/root"
    if [ -f "$reg" ]; then
      IFS= read -r root < "$reg" || true
      root="${root#"${root%%[![:space:]]*}"}"; root="${root%"${root##*[![:space:]]}"}"
    fi
  fi
  if [ -z "$root" ] && [ -n "$cfg" ]; then
    _khms_err "the settings file $cfg (KHMS_CONFIG_FILE) names no KHMS_ROOT and no base is registered" \
              "-- add KHMS_ROOT to it, set KHMS_ROOT, or create and register a base with:" \
              "khms_init.sh --register <dir>"
    return 3
  fi
  if [ -z "$root" ]; then
    _khms_err "no KHMS data root -- set KHMS_ROOT, name a settings file with KHMS_CONFIG_FILE," \
              "or create and register a base with: khms_init.sh --register <dir>"; return 3
  fi
  [ "${root:0:1}" = / ] || { _khms_err "KHMS_ROOT must be an absolute path, got: $root"; return 3; }
  if [ -z "$cfg" ] && [ -f "$root/config/khms.env" ]; then
    cfg="$root/config/khms.env"
    _khms_parse "$cfg" || return 3
    if [ -n "${_KHMS_RAW[KHMS_ROOT]:-}" ]; then
      other="$(_khms_expand "${_KHMS_RAW[KHMS_ROOT]}" "KHMS_ROOT")" || return 3
      local a="${other%"${other##*[!/]}"}" b="${root%"${root##*[!/]}"}"
      [ "${a:-/}" = "${b:-/}" ] \
        || { _khms_err "$cfg names KHMS_ROOT=$other but the discovered root is $root"; return 3; }
    fi
  fi
  _KHMS_ROOT_RESOLVED="$root"; _KHMS_CFG_RESOLVED="$cfg"
  local ro; ro="$(_khms_resolve KHMS_READ_ONLY KHMS_READ_ONLY)" || return 3
  case "$ro" in 0|1) ;; *) _khms_err "KHMS_READ_ONLY must be 0 or 1, got: $ro"; return 3 ;; esac
  if [ "$(_khms_resolve KHMS_SHARED KHMS_SHARED)" = 1 ]; then
    python3 "$KHMS_CODE/khms_paths.py" --root >/dev/null || return 3
  fi
}

khms_get() {      # khms_get KEY -> resolved value on stdout (khms_load first)
  [ -n "${1:-}" ] || { _khms_err "khms_get needs a KEY"; return 2; }
  [ -n "$_KHMS_ROOT_RESOLVED" ] || khms_load || return 3
  _khms_resolve "$1" "$1"
}

khms_require_writable() {   # khms_require_writable "<what>" -> rc 4 and a message when read-only
  local ro; ro="$(khms_get KHMS_READ_ONLY)" || return 3
  if [ "$ro" = 1 ]; then
    echo "khms: read-only deployment (KHMS_READ_ONLY=1): refusing to $1; nothing was written" >&2
    return 4
  fi
}

khms_may_write() {   # khms_may_write PATH -> rc 0 when this deployment may append to PATH (see khms_paths.py)
  local ro p k b; ro="$(khms_get KHMS_READ_ONLY)" || return 3
  [ "$ro" = 1 ] || return 0
  p="$(realpath -m -- "$1")"
  for k in KHMS_ROOT KHMS_CONFIG_DIR KHMS_SHARED_STATE KHMS_RUNTIME; do
    b="$(khms_get "$k")" || return 3; b="$(realpath -m -- "$b")"
    [ "$p" = "$b" ] && return 1
    case "$p" in "${b%/}/"*) return 1 ;; esac
  done
  return 0
}

# khms_agent_resolve: THE AGENT CLI, NEVER A SILENT PATH LOOKUP (#232). Sets KHMS_AGENT_BIN (absolute path, or
# "" when it cannot be used) and KHMS_AGENT_WHY (one line: how it was found, or why it was refused).
# Until 2026-10-12 the drivers ran `command -v "$KHMS_AGENT"` on the default bare name `claude`: under systemd
# PATH found the owner's stale CLI, whose `sonnet` alias served an older, ~5x slower model for two nights, and
# nothing said which binary ran. Now KHMS_AGENT must be an ABSOLUTE path; a bare name is refused with one line
# unless KHMS_AGENT_FROM_PATH=1 explicitly allows the lookup (a quick local install), and then the line says
# which file PATH gave. Model names stay what the deployment configures (aliases move with the CLI version,
# which is why every stage call logs the model that actually served it -- see agent_result.py).
khms_agent_resolve() {
  local a allow
  KHMS_AGENT_BIN=""; KHMS_AGENT_WHY=""
  a="$(khms_get KHMS_AGENT)" || return 3
  allow="$(khms_get KHMS_AGENT_FROM_PATH)" || return 3
  case "$a" in
    /*)
      if [ -f "$a" ] && [ -x "$a" ]; then
        KHMS_AGENT_BIN="$a"; KHMS_AGENT_WHY="KHMS_AGENT, -> $(readlink -f -- "$a")"; return 0
      fi
      KHMS_AGENT_WHY="AGENT UNAVAILABLE: KHMS_AGENT='$a' is not an executable file"; return 1 ;;
  esac
  if [ "$allow" != 1 ]; then
    KHMS_AGENT_WHY="AGENT REFUSED: KHMS_AGENT='$a' is not an absolute path; PATH would pick $(command -v -- "$a" 2>/dev/null || echo nothing), which can silently be another CLI and model -- set KHMS_AGENT=<absolute path>, or KHMS_AGENT_FROM_PATH=1 to allow the PATH lookup"
    return 4
  fi
  KHMS_AGENT_BIN="$(command -v -- "$a" 2>/dev/null || true)"
  case "$KHMS_AGENT_BIN" in
    /*) KHMS_AGENT_WHY="found through PATH (KHMS_AGENT='$a', KHMS_AGENT_FROM_PATH=1), -> $(readlink -f -- "$KHMS_AGENT_BIN")"; return 0 ;;
  esac
  KHMS_AGENT_BIN=""
  KHMS_AGENT_WHY="AGENT UNAVAILABLE: KHMS_AGENT='$a' (KHMS_AGENT_FROM_PATH=1) is not an executable command on PATH"
  return 1
}

khms_code_lock_file() {   # the code-use lock file of THIS clone -- from the clone alone, never from config
  # (the same rule as khms_paths.code_lock_file: <gitdir>/khms-code.lock, else <clone>/.khms-code.lock)
  local repo gd
  repo="$(dirname "$KHMS_CODE")"
  if [ -d "$repo/.git" ]; then
    printf '%s/khms-code.lock\n' "$repo/.git"
  elif [ -f "$repo/.git" ] && gd="$(sed -n '1s/^gitdir:[[:space:]]*//p' "$repo/.git")" && [ -n "$gd" ]; then
    case "$gd" in /*) ;; *) gd="$repo/$gd" ;; esac
    printf '%s/khms-code.lock\n' "$(readlink -f "$gd")"
  else
    printf '%s/.khms-code.lock\n' "$repo"
  fi
}

# khms_code_lock: hold a SHARED lock on this clone's code for the rest of the caller's life.
# Every long-running entrypoint (night, week, export, the embedding daemon) takes it first; an updater takes
# the same file EXCLUSIVELY (flock -x -n) and therefore refuses while any of them runs, without killing it.
# Shared locks never conflict with each other, and a child inherits the open descriptor, so a nested call
# (the night running a helper that takes the lock again) cannot deadlock. rc 5 when an update holds it.
khms_code_lock() {
  local f; f="$(khms_code_lock_file)" || return 3
  mkdir -p "${f%/*}" || { _khms_err "cannot create ${f%/*} for the code-use lock"; return 3; }
  [ -s "$f" ] || printf 'code=%s\n' "$KHMS_CODE" >> "$f"
  exec {KHMS_CODE_LOCK_FD}>>"$f"
  flock -s -n "$KHMS_CODE_LOCK_FD" || {
    echo "khms: the code clone $KHMS_CODE is being updated (exclusive lock $f is held) -- refusing to start" >&2
    exec {KHMS_CODE_LOCK_FD}>&-
    return 5
  }
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    ""|-h|--help) sed -n '2,6p' "${BASH_SOURCE[0]}"; [ -n "${1:-}" ]; exit $? ;;
  esac
  khms_load || exit 3
  case "$1" in
    --all) for k in "${_KHMS_KEYS[@]}"; do v="$(khms_get "$k")" || exit 3; printf '%s=%s\n' "$k" "$v"; done ;;
    --root) printf '%s\n' "$_KHMS_ROOT_RESOLVED" ;;
    --code-lock-file) khms_code_lock_file || exit 3 ;;
    KHMS_*) v="$(khms_get "$1")" || exit 3; printf '%s\n' "$v" ;;
    *) echo "khms_paths: unknown name $1" >&2; exit 2 ;;
  esac
fi
