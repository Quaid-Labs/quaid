#!/bin/bash
# tmux-msg.sh — Send a message to a running Claude Code or Codex session via tmux
#
# Usage:
#   tmux-msg.sh <target> <message>
#   tmux-msg.sh 3 "check benchmark status"
#   tmux-msg.sh codex-bench "run r19 with split provider"
#   tmux-msg.sh self "reminder: check back in 20m"
#
# Targets:
#   0-99         tmux window index (main:<n>.0)
#   self         send to this script's own tmux pane ($TMUX_PANE)
#   codex-dev    window 1
#   codex-pr     window 2
#   codex-bench  window 3
#   claude       window 4
#   main:N.0     explicit pane address
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
    codex-dev)
        PANE="main:1.0"
        ;;
    codex-pr)
        PANE="main:2.0"
        ;;
    codex-bench)
        PANE="main:3.0"
        ;;
    claude|monitor)
        PANE="main:4.0"
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
        echo "Error: unknown target '$TARGET'" >&2
        echo "Valid: 0-99, self, codex-dev, codex-pr, codex-bench, claude, monitor, main:N.0, livetest:NAME" >&2
        exit 1
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
    local cursor_y cursor_x pane_height raw_line candidate last_line
    cursor_y="$(tmux display-message -p -t "$PANE" '#{cursor_y}' 2>/dev/null || echo "")"
    cursor_x="$(tmux display-message -p -t "$PANE" '#{cursor_x}' 2>/dev/null || echo "")"
    pane_height="$(tmux display-message -p -t "$PANE" '#{pane_height}' 2>/dev/null || echo "")"
    [[ "$cursor_y" =~ ^[0-9]+$ ]] || return 1
    [[ "$cursor_x" =~ ^[0-9]+$ ]] || return 1
    [[ "$pane_height" =~ ^[0-9]+$ ]] || return 1
    # Only treat as draft when cursor is on the bottom line of the pane.
    # Tool call rendering moves the cursor mid-screen; the user input prompt
    # is always at the very bottom. This avoids false positives from agents
    # running tool calls (which render output above the input line).
    last_line=$(( pane_height - 1 ))
    [[ "$cursor_y" -ge "$last_line" ]] || return 1
    raw_line="$(tmux capture-pane -p -t "$PANE" -S "$cursor_y" -E "$cursor_y" 2>/dev/null || true)"
    candidate="${raw_line:0:$cursor_x}"
    # Require the cursor line to START WITH a known prompt prefix.
    # Tool output lines may contain "> " or "$ " mid-line; only the actual
    # input prompt will have it at position 0.
    local stripped=0
    for mark in "❯ " "› " "> " "$ " "% "; do
        if [[ "$raw_line" == "$mark"* ]]; then
            candidate="${candidate#"$mark"}"
            stripped=1
            break
        fi
    done
    [[ "$stripped" -eq 1 ]] || return 1
    # Trim whitespace
    candidate="${candidate#"${candidate%%[![:space:]]*}"}"
    candidate="${candidate%"${candidate##*[![:space:]]}"}"
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

tmux send-keys -t "$PANE" "$MESSAGE"
sleep 0.3
# Use both Enter and C-m for compatibility across pane types
tmux send-keys -t "$PANE" Enter
sleep 0.05
tmux send-keys -t "$PANE" C-m

echo "Sent to $PANE: $MESSAGE"
