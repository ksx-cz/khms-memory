#!/usr/bin/env bash
# khms_ping_tmux.sh <tmux-session> -- deliver a message read on stdin into a terminal agent running in tmux.
# A delivery adapter for khms_review_ping.sh: set KHMS_PING_SEND="<this file> <session>".
# Exit: 0 delivered · 2 usage · 3 no such session (the recipient is not there; the caller logs it) · 1 tmux failed.
#
# WHY TEXT AND ENTER ARE TWO STEPS. -l types the text literally (no tmux key-name interpretation). Text and
# Enter must NOT arrive as one input burst: the agent's paste detection then takes the Enter as a literal
# newline and the message sits in the input box unsubmitted -- that is how an early ping was found hours old
# and unsent. Hence the pause, and a verify-and-retry: if the message's first word is still visible at the
# bottom of the pane after submit, press Enter once more (a no-op if it did send). For the same reason the
# message is one line: an embedded newline would be typed as a newline, so newlines are folded to spaces.
set -uo pipefail
SESSION="${1:-}"
if [ -z "$SESSION" ] || [ $# -ne 1 ]; then
  echo "usage: khms_ping_tmux.sh <tmux-session>   (message on stdin)" >&2; exit 2
fi
TMUX_BIN=${KHMS_TMUX_BIN:-tmux}
MSG="$(tr '\n' ' ' | sed 's/ *$//')"
[ -n "$MSG" ] || { echo "khms_ping_tmux.sh: empty message, nothing sent" >&2; exit 2; }
MARK="${MSG%% *}"
"$TMUX_BIN" has-session -t "$SESSION" 2>/dev/null || { echo "$(date -Is) no tmux session '$SESSION'"; exit 3; }
"$TMUX_BIN" send-keys -t "$SESSION" -l "$MSG" || exit 1
sleep "${KHMS_TMUX_PAUSE_S:-1}"
"$TMUX_BIN" send-keys -t "$SESSION" Enter || exit 1
sleep "$(( ${KHMS_TMUX_PAUSE_S:-1} * 2 ))"
if "$TMUX_BIN" capture-pane -p -t "$SESSION" | tail -n 6 | grep -qF -- "$MARK"; then
  "$TMUX_BIN" send-keys -t "$SESSION" Enter
  echo "$(date -Is) Enter retry (message still visible in input area)"
fi
exit 0
