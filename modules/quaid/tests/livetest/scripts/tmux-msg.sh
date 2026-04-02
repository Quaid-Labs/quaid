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
#
# Exit codes:
#   0  message delivered
#   1  error
#
# Behaviour:
#   If pane is in copy mode:
#     → wait until copy mode exits
#   If pane has a draft:
#     → sample it every 5 seconds
#     → while the draft changes, keep waiting
#     → once the draft is unchanged across a 5 second interval, preserve it,
#       clear it, send the message, then restore the draft
#     → if the draft becomes empty on a check, send immediately
#   If pane stays in copy mode for 5 minutes, force-exit copy mode and proceed

set -euo pipefail

TARGET="${1:?Usage: tmux-msg.sh <target> <message>}"
SENDER="${TMUX_MSG_SENDER:-}"
# Auto-detect sender pane from tmux; TMUX_MSG_SOURCE overrides if explicitly set
_detected_pane="$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || echo "unknown")"
SENDER_PANE="${TMUX_MSG_SOURCE:-$_detected_pane}"
DRAFT_STABLE_SECS=5
COPY_MODE_FORCE_EXIT_SECS=300
# THIS_IS_A_CRITICAL_MESSAGE=true bypasses draft detection (send immediately).
if [[ "${THIS_IS_A_CRITICAL_MESSAGE:-false}" == "true" ]]; then
    BYPASS_WAIT=true
else
    BYPASS_WAIT=false
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
        PANE="${TMUX_PANE:-main:4.0}"
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

_pane_command() {
    tmux display-message -p -t "$PANE" '#{pane_current_command}' 2>/dev/null || echo ""
}

_pane_allows_ctrl_c_clear() {
    case "$(_pane_command)" in
        bash|zsh|sh|fish)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

_user_viewing() {
    local w p
    w="$(tmux display-message -p -t "$PANE" '#{window_active}' 2>/dev/null || echo "0")"
    p="$(tmux display-message -p -t "$PANE" '#{pane_active}' 2>/dev/null || echo "0")"
    [[ "$w" == "1" && "$p" == "1" ]]
}

_looks_like_placeholder_prompt() {
    local text
    text="$(_normalize_message_text "$1")"
    [[ -n "$text" ]] || return 1

    case "$text" in
        "Describe a task"*|\
        "Ask anything"*|\
        "What would you like"*|\
        "How can I help"*|\
        "Type a message"*|\
        "Enter a prompt"*)
            return 0
            ;;
    esac

    return 1
}

_current_draft_text() {
    local cursor_y cursor_x pane_height start_line raw_block candidate prompt_index i line mark stripped continuation_count
    cursor_y="$(tmux display-message -p -t "$PANE" '#{cursor_y}' 2>/dev/null || echo "")"
    cursor_x="$(tmux display-message -p -t "$PANE" '#{cursor_x}' 2>/dev/null || echo "")"
    pane_height="$(tmux display-message -p -t "$PANE" '#{pane_height}' 2>/dev/null || echo "")"
    [[ "$cursor_y" =~ ^[0-9]+$ ]] || return 1
    [[ "$cursor_x" =~ ^[0-9]+$ ]] || return 1
    [[ "$pane_height" =~ ^[0-9]+$ ]] || return 1

    start_line=$(( cursor_y > 3 ? cursor_y - 3 : 0 ))
    raw_block="$(tmux capture-pane -p -t "$PANE" -S "$start_line" -E "$cursor_y" 2>/dev/null || true)"
    [[ -n "$raw_block" ]] || return 1

    prompt_index=-1
    continuation_count=0
    candidate=""
    i=0
    while IFS= read -r line; do
        stripped=""
        for mark in "❯ " "› " "> " "$ " "% "; do
            if [[ "$line" == *"$mark"* ]]; then
                prompt_index=$i
                stripped="${line##*"$mark"}"
                break
            fi
        done
        if [[ "$prompt_index" -ge 0 ]]; then
            if [[ "$i" -eq "$prompt_index" ]]; then
                candidate+="$stripped"
            elif [[ -n "$candidate" ]]; then
                candidate+="$line"
                continuation_count=$(( continuation_count + 1 ))
            fi
        fi
        i=$(( i + 1 ))
    done <<< "$raw_block"

    [[ "$prompt_index" -ge 0 ]] || return 1
    # Trim whitespace
    candidate="${candidate#"${candidate%%[![:space:]]*}"}"
    candidate="${candidate%"${candidate##*[![:space:]]}"}"
    [[ -n "$candidate" ]] || return 1

    # Some UIs render placeholder helper text on the prompt line with the
    # cursor still at the prompt start. Ignore only obvious placeholders.
    # Wrapped drafts and injected inter-agent messages can also leave the
    # cursor near the prompt start, so do not discard those purely by cursor
    # position.
    if [[ "$cursor_x" -le 2 ]]; then
        if [[ "$continuation_count" -eq 0 ]] && _looks_like_placeholder_prompt "$candidate"; then
            return 1
        fi
    fi

    printf '%s\n' "$candidate"
}

_queued_draft_text() {
    local pane_height start raw_block in_queue line candidate
    pane_height="$(tmux display-message -p -t "$PANE" '#{pane_height}' 2>/dev/null || echo "")"
    [[ "$pane_height" =~ ^[0-9]+$ ]] || return 1

    start=$(( pane_height > 20 ? pane_height - 20 : 0 ))
    raw_block="$(tmux capture-pane -p -t "$PANE" -S "$start" -E -1 2>/dev/null || true)"
    [[ -n "$raw_block" ]] || return 1

    in_queue=0
    candidate=""
    while IFS= read -r line; do
        if [[ "$line" == *"Messages to be submitted after next tool call"* ]]; then
            in_queue=1
            continue
        fi

        if [[ "$in_queue" -eq 1 ]]; then
            if [[ "$line" == *"↳ "* ]]; then
                candidate+="${line##*↳ }"
                continue
            fi

            if [[ -n "$candidate" && "$line" == "    "* ]]; then
                candidate+="${line#"    "}"
                continue
            fi

            [[ -n "$candidate" ]] && break
        fi
    done <<< "$raw_block"

    [[ -n "$candidate" ]] || return 1
    printf '%s\n' "$candidate"
}

_draft_state() {
    local prompt queued
    prompt="$(_current_draft_text 2>/dev/null || true)"
    if [[ -n "$prompt" ]]; then
        printf 'prompt\t%s\n' "$prompt"
        return 0
    fi

    queued="$(_queued_draft_text 2>/dev/null || true)"
    if [[ -n "$queued" ]]; then
        printf 'queued\t%s\n' "$queued"
        return 0
    fi

    return 1
}

_pane_has_draft() {
    local draft
    draft="$(_current_draft_text 2>/dev/null || true)"
    [[ -n "$draft" ]]
}

_normalize_message_text() {
    local text
    text="$1"
    # Fold wrapped-line indentation and other display-only spacing so we can
    # reliably compare what tmux shows in the prompt against what we injected.
    text="${text//$'\n'/ }"
    text="$(printf '%s' "$text" | tr -s '[:space:]' ' ')"
    text="${text#"${text%%[![:space:]]*}"}"
    text="${text%"${text##*[![:space:]]}"}"
    printf '%s\n' "$text"
}

_text_looks_like_message() {
    local observed expected min_len prefix_len
    observed="$(_normalize_message_text "$1")"
    expected="$(_normalize_message_text "$2")"
    [[ -n "$observed" && -n "$expected" ]] || return 1

    if [[ "$observed" == "$expected" ]]; then
        return 0
    fi
    if [[ "$observed" == *"$expected"* || "$expected" == *"$observed"* ]]; then
        return 0
    fi

    min_len=${#observed}
    if [[ ${#expected} -lt "$min_len" ]]; then
        min_len=${#expected}
    fi
    if [[ "$min_len" -lt 24 ]]; then
        return 1
    fi

    prefix_len=48
    if [[ "$min_len" -lt "$prefix_len" ]]; then
        prefix_len="$min_len"
    fi

    [[ "${observed:0:prefix_len}" == "${expected:0:prefix_len}" ]]
}

_draft_matches_message() {
    local draft expected
    draft="$(_current_draft_text 2>/dev/null || true)"
    [[ -n "$draft" ]] || return 1
    _text_looks_like_message "$draft" "$MESSAGE"
}

_queued_matches_message() {
    local queued expected
    queued="$(_queued_draft_text 2>/dev/null || true)"
    [[ -n "$queued" ]] || return 1
    _text_looks_like_message "$queued" "$MESSAGE"
}

_message_still_pending() {
    _draft_matches_message || _queued_matches_message
}

_sleep_and_count() {
    local secs="$1"
    sleep "$secs"
    COPY_MODE_WAITED=$(( COPY_MODE_WAITED + secs ))
}

_wait_for_copy_mode_exit() {
    while _pane_in_copy_mode; do
        if [[ "$COPY_MODE_WAITED" -ge "$COPY_MODE_FORCE_EXIT_SECS" ]]; then
            tmux send-keys -t "$PANE" -X cancel 2>/dev/null || true
            sleep 0.1
            return 0
        fi
        _sleep_and_count "$DRAFT_STABLE_SECS"
    done
}

_clear_current_draft() {
    local before after max_len i attempt
    before="$(_current_draft_text 2>/dev/null || true)"
    [[ -n "$before" ]] || return 0

    tmux send-keys -t "$PANE" C-u
    for attempt in {1..10}; do
        sleep 0.1
        after="$(_current_draft_text 2>/dev/null || true)"
        [[ -z "$after" ]] && return 0
        [[ "$after" != "$before" ]] && break
    done

    tmux send-keys -t "$PANE" Escape
    sleep 0.1
    tmux send-keys -t "$PANE" C-u
    for attempt in {1..10}; do
        sleep 0.1
        after="$(_current_draft_text 2>/dev/null || true)"
        [[ -z "$after" ]] && return 0
        [[ "$after" != "$before" ]] && break
    done

    # Ctrl-C is only safe in shell-like panes. In agent panes it interrupts the
    # live conversation, which is worse than failing to preserve a draft.
    if _pane_allows_ctrl_c_clear; then
        tmux send-keys -t "$PANE" C-c
        for attempt in {1..10}; do
            sleep 0.1
            after="$(_current_draft_text 2>/dev/null || true)"
            [[ -z "$after" ]] && return 0
            [[ "$after" != "$before" ]] && break
        done

        tmux send-keys -t "$PANE" Escape
        sleep 0.1
        tmux send-keys -t "$PANE" C-c
        for attempt in {1..10}; do
            sleep 0.1
            after="$(_current_draft_text 2>/dev/null || true)"
            [[ -z "$after" ]] && return 0
        done
    fi

    max_len=${#before}
    if [[ ${#after} -gt "$max_len" ]]; then
        max_len=${#after}
    fi

    for ((i = 0; i < max_len + 16; i++)); do
        tmux send-keys -t "$PANE" BSpace
    done

    for attempt in {1..10}; do
        sleep 0.1
        after="$(_current_draft_text 2>/dev/null || true)"
        [[ -z "$after" ]] && return 0
    done

    return 1
}

_wait_for_stable_draft() {
    local first second first_kind first_text second_kind second_text
    while true; do
        COPY_MODE_WAITED=0
        _wait_for_copy_mode_exit

        first="$(_draft_state 2>/dev/null || true)"
        if [[ -z "$first" ]]; then
            PRESERVED_DRAFT=""
            return 0
        fi
        IFS=$'\t' read -r first_kind first_text <<< "$first"

        sleep "$DRAFT_STABLE_SECS"
        COPY_MODE_WAITED=0
        _wait_for_copy_mode_exit

        second="$(_draft_state 2>/dev/null || true)"
        if [[ -z "$second" ]]; then
            continue
        fi
        IFS=$'\t' read -r second_kind second_text <<< "$second"

        if [[ "$first" == "$second" ]]; then
            if [[ "$first_kind" == "prompt" ]]; then
                PRESERVED_DRAFT="$first_text"
                return 0
            fi
            continue
        fi
    done
}

# --- Wait for send window / preserve draft if needed ---

PRESERVED_DRAFT=""
COPY_MODE_WAITED=0

if [[ "$BYPASS_WAIT" != "true" ]]; then
    _wait_for_stable_draft
fi

# --- Send ---

if _pane_in_copy_mode; then
    tmux send-keys -t "$PANE" -X cancel 2>/dev/null || true
    sleep 0.1
fi

if [[ -n "$PRESERVED_DRAFT" ]]; then
    if ! _clear_current_draft; then
        echo "Error: failed to clear preserved draft safely in pane '$PANE'" >&2
        exit 1
    fi
elif ! _user_viewing; then
    CX="$(tmux display-message -p -t "$PANE" '#{cursor_x}' 2>/dev/null || echo "0")"
    if [[ "$CX" =~ ^[0-9]+$ ]] && [[ "$CX" -gt 0 ]]; then
        tmux send-keys -t "$PANE" C-u
        sleep 0.05
    fi
fi

tmux send-keys -t "$PANE" -l -- "$MESSAGE"
sleep 0.2
tmux send-keys -t "$PANE" Enter
sleep 0.2

# Some panes occasionally keep the injected text pending after the first
# submit, either in the live prompt or in Codex's queued-message buffer.
# Retry only while our exact message is still pending so we do not submit
# unrelated user input.
if _message_still_pending; then
    tmux send-keys -t "$PANE" C-m
    sleep 0.2
fi

if _message_still_pending; then
    tmux send-keys -t "$PANE" Enter
    sleep 0.2
fi

if _message_still_pending; then
    echo "Error: message still pending after submit retries in pane '$PANE'" >&2
    exit 1
fi

if [[ -n "$PRESERVED_DRAFT" ]]; then
    sleep 0.5
    tmux send-keys -t "$PANE" -l -- "$PRESERVED_DRAFT"
    sleep 0.1
fi

echo "Sent to $PANE: $MESSAGE"
