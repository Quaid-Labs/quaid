#!/bin/bash
# tmux-msg.sh — Send a direct message to a running Claude Code or Codex session via tmux
#
# For queue-backed routine traffic, prefer tmux-mailbox.sh. This script is the
# direct-delivery path for urgent interrupts, self-tests, and one-off nudges.
#
# Usage:
#   tmux-msg.sh <target> <message>
#   tmux-msg.sh 3 "check benchmark status"
#   tmux-msg.sh main:3.0 "run benchmark loop"
#   tmux-msg.sh self "reminder: check back in 20m"
#
# Targets:
#   0-99         tmux window index (main:<n>.0)
#   self         send to this script's own tmux pane ($TMUX_PANE)
#   main:N.0     explicit pane address
#   livetest:NAME explicit pane address
#   <alias>      alias from scripts/.tmux-targets.json (gitignored)
#
# Environment:
#   TMUX_MSG_SENDER               (required) sender identity label
#   TMUX_MSG_SOURCE               (optional) override source pane; auto-detected from current tmux pane if not set
#   THIS_IS_A_CRITICAL_MESSAGE    (optional) set to "true" to bypass draft detection and send immediately
#   TMUX_MSG_WAIT                 (optional) max seconds to wait for user to finish; default 60
#   TMUX_MSG_POLL                 (optional) poll interval in seconds; default 2
#
# Exit codes:
#   0  message delivered
#   1  error
#   2  skipped — user was still busy after TMUX_MSG_WAIT seconds
#
# Behaviour:
#   If user is watching the pane AND (pane is in copy mode OR has an active draft):
#     → poll every TMUX_MSG_POLL seconds until they finish, then send
#     → if still busy after TMUX_MSG_WAIT seconds, exit 2 (skip)
#   If user is not watching:
#     → send immediately; exit copy mode first if needed; C-u if stale input at cursor
#
# The C-u is ONLY issued when the user is not watching and there is stale input at
# the cursor (e.g. a previous agent message that wasn't submitted). It is never
# issued while the user is watching.

set -euo pipefail

TARGET="${1:?Usage: tmux-msg.sh <target> <message>}"
SENDER="${TMUX_MSG_SENDER:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGETS_FILE="${TMUX_MSG_TARGETS_FILE:-$SCRIPT_DIR/.tmux-targets.json}"
# Auto-detect sender pane from tmux; TMUX_MSG_SOURCE overrides if explicitly set
_detected_pane="$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || echo "unknown")"
SENDER_PANE="${TMUX_MSG_SOURCE:-$_detected_pane}"
POLL="${TMUX_MSG_POLL:-2}"
# THIS_IS_A_CRITICAL_MESSAGE=true bypasses draft detection (send immediately).
# Use only when you need to interrupt the user mid-sentence (e.g. INTERRUPT-level
# escalations). ISSUE messages do NOT need this — busy detection only fires when
# the user is actively typing in the input prompt, not during agent tool calls.
if [[ "${THIS_IS_A_CRITICAL_MESSAGE:-false}" == "true" ]]; then
    WAIT=0
else
    WAIT="${TMUX_MSG_WAIT:-60}"
fi
shift

if [ -z "$SENDER" ]; then
    echo "Error: TMUX_MSG_SENDER is required (example: TMUX_MSG_SENDER=codex TMUX_MSG_SOURCE=main:2.0 tmux-msg.sh <target> <message>)" >&2
    exit 1
fi

MESSAGE="[from $SENDER @ $SENDER_PANE] $*"

if [ -z "$MESSAGE" ]; then
    echo "Error: no message provided" >&2
    echo "Usage: tmux-msg.sh <target> <message>" >&2
    exit 1
fi

# Resolve target to tmux pane
case "$TARGET" in
    self)
        PANE="${TMUX_PANE:-main:2.0}"
        ;;
    [0-9]|[0-9][0-9])
        PANE="main:${TARGET}.0"
        ;;
    main:*)
        PANE="$TARGET"
        ;;
    livetest:*)
        PANE="$TARGET"
        ;;
    *)
        PANE="$(python3 - "$TARGETS_FILE" "$TARGET" <<'PY'
import json
import pathlib
import sys

cfg = pathlib.Path(sys.argv[1])
key = sys.argv[2]
if not cfg.is_file():
    print("")
    raise SystemExit(0)
try:
    data = json.loads(cfg.read_text(encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
if not isinstance(data, dict):
    print("")
    raise SystemExit(0)
value = data.get(key, "")
print(value if isinstance(value, str) else "")
PY
)"
        if [[ -z "$PANE" ]]; then
            echo "Error: unknown target '$TARGET'" >&2
            echo "Valid: 0-99, self, main:N.0, livetest:NAME, or alias in $TARGETS_FILE" >&2
            exit 1
        fi
        ;;
esac

# Verify pane exists
if ! tmux has-session -t "${PANE%%.*}" 2>/dev/null; then
    echo "Error: tmux pane '$PANE' not found" >&2
    exit 1
fi

# --- Helpers ---

_pane_in_copy_mode() {
    local m
    m="$(tmux display-message -p -t "$PANE" '#{pane_in_mode}' 2>/dev/null || echo "0")"
    [[ "$m" == "1" ]]
}

_user_viewing() {
    local a
    a="$(tmux display-message -p -t "$PANE" '#{window_active}' 2>/dev/null || echo "0")"
    [[ "$a" == "1" ]]
}

_pane_has_draft() {
    local cursor_y pane_height raw_block candidate last_line
    cursor_y="$(tmux display-message -p -t "$PANE" '#{cursor_y}' 2>/dev/null || echo "")"
    pane_height="$(tmux display-message -p -t "$PANE" '#{pane_height}' 2>/dev/null || echo "")"
    [[ "$cursor_y" =~ ^[0-9]+$ ]] || return 1
    [[ "$pane_height" =~ ^[0-9]+$ ]] || return 1
    # Only treat as draft when cursor is on the bottom line of the pane.
    # Tool call rendering moves the cursor mid-screen; the user input prompt
    # is always at the very bottom. This avoids false positives from agents
    # running tool calls (which render output above the input line).
    last_line=$(( pane_height - 1 ))
    [[ "$cursor_y" -ge "$last_line" ]] || return 1

    raw_block="$(tmux capture-pane -p -t "$PANE" -S 0 -E "$cursor_y" 2>/dev/null || true)"

    # Find the last visible prompt line before the cursor and treat everything
    # from there to the cursor line as the current composer block. Long agent
    # messages wrap, so checking only the cursor line misses drafts whose bottom
    # line is a continuation without the prompt prefix.
    local -a lines=()
    local line prompt_idx=-1 prompt_mark="" i
    while IFS= read -r line; do
        lines+=("$line")
    done <<< "$raw_block"
    for ((i = 0; i < ${#lines[@]}; i++)); do
        for mark in "❯ " "› " "> " "$ " "% "; do
            if [[ "${lines[$i]}" == "$mark"* ]]; then
                prompt_idx="$i"
                prompt_mark="$mark"
                break
            fi
        done
    done
    [[ "$prompt_idx" -ge 0 ]] || return 1

    candidate=""
    for ((i = prompt_idx; i < ${#lines[@]}; i++)); do
        line="${lines[$i]}"
        if [[ "$i" -eq "$prompt_idx" ]]; then
            line="${line#"$prompt_mark"}"
        fi
        candidate+="$line"
    done

    # Trim whitespace
    candidate="${candidate#"${candidate%%[![:space:]]*}"}"
    candidate="${candidate%"${candidate##*[![:space:]]}"}"
    case "$candidate" in
        ""|"Press up to edit queued messages"|"Type a message"|"Enter a message")
            return 1
            ;;
    esac
    [[ -n "$candidate" ]]
}

# Returns true if the user is busy (watching + in copy mode or mid-draft).
# When this is true, we should wait rather than interrupt.
_user_busy() {
    _user_viewing || return 1
    _pane_in_copy_mode && return 0
    _pane_has_draft && return 0
    return 1
}

_submit_message() {
    tmux send-keys -l -t "$PANE" -- "$MESSAGE"
    sleep 0.3
    # Use several enter-equivalent keys for compatibility across pane types.
    tmux send-keys -t "$PANE" Enter
    sleep 0.05
    tmux send-keys -t "$PANE" C-m
    sleep 0.05
    tmux send-keys -t "$PANE" C-j
}

# --- Wait for user to finish ---

if [[ "$WAIT" -gt 0 ]] && _user_busy; then
    waited=0
    while _user_busy; do
        if [[ "$waited" -ge "$WAIT" ]]; then
            echo "Skipped: pane '$PANE' still busy after ${WAIT}s" >&2
            exit 2
        fi
        sleep "$POLL"
        waited=$(( waited + POLL ))
    done
fi

# --- Send ---

# If pane is in copy mode and user isn't watching, exit it before sending
if _pane_in_copy_mode; then
    tmux send-keys -t "$PANE" -X cancel 2>/dev/null || true
    sleep 0.1
fi

# Clear stale input only when user is NOT watching and cursor is past position 0
if ! _user_viewing; then
    CX="$(tmux display-message -p -t "$PANE" '#{cursor_x}' 2>/dev/null || echo "0")"
    if [[ "$CX" =~ ^[0-9]+$ ]] && [[ "$CX" -gt 0 ]]; then
        tmux send-keys -t "$PANE" C-u
        sleep 0.05
    fi
fi

_submit_message

# Some panes intermittently keep the text in the prompt buffer even after the
# initial submit sequence. Retry a few times; if the draft is still present,
# fail the send instead of pretending it worked.
for _attempt in 1 2 3; do
    sleep 0.2
    _pane_has_draft || break
    tmux send-keys -t "$PANE" Enter
    sleep 0.05
    tmux send-keys -t "$PANE" C-m
    sleep 0.05
    tmux send-keys -t "$PANE" C-j
done

if _pane_has_draft; then
    echo "Error: message still present as draft in pane '$PANE' after submit retries" >&2
    exit 1
fi

echo "Sent to $PANE: $MESSAGE"
