#!/usr/bin/env bash
# autonomous_mode.sh — Periodically nudge a tmux agent to keep working
#
# Usage:
#   autonomous_mode.sh [options]
#   autonomous_mode.sh -w 1 -t 300
#   autonomous_mode.sh -w codex-bench -t 600 -m "Keep going on the bug bash"
#
# Options:
#   -w <window>    Tmux window target (number or alias, passed to tmux-msg.sh). Required.
#   -t <seconds>   Interval between messages (default: 300 = 5 minutes, minimum: 30)
#   -m <message>   Custom message. PID is always prepended. See default below.
#   -n <command>   Shell command to run on exit (e.g. send a notification).
#                  $WINDOW and $PID are available in the command string.
#   -h             Show this help
#
# Default message:
#   "If you're in the middle of something ignore this. Otherwise if you have
#    more work to do, keep going. If you finished your overall task, then kill
#    this process: kill <PID>"
#
# Stop it:
#   kill <PID>     (PID is printed on start and included in every message)

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMUX_MSG="$SCRIPT_DIR/tmux-msg.sh"

if [[ ! -x "$TMUX_MSG" ]]; then
    echo "Error: missing executable $TMUX_MSG" >&2
    exit 1
fi

# Defaults
WINDOW=""
INTERVAL=300
CUSTOM_MESSAGE=""
ON_EXIT_CMD=""

usage() {
    sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
    exit 0
}

while getopts "w:t:m:n:h" opt; do
    case "$opt" in
        w) WINDOW="$OPTARG" ;;
        t) INTERVAL="$OPTARG" ;;
        m) CUSTOM_MESSAGE="$OPTARG" ;;
        n) ON_EXIT_CMD="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done

if [[ -z "$WINDOW" ]]; then
    echo "Error: -w <window> is required" >&2
    echo "Example: $0 -w 1 -t 300" >&2
    exit 1
fi

if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [[ "$INTERVAL" -lt 30 ]]; then
    echo "Error: interval must be an integer >= 30" >&2
    exit 1
fi

PID=$$

# --- Resolve target to tmux pane (mirrors tmux-msg.sh) ---
case "$WINDOW" in
    codex-dev)    PANE="main:1.0" ;;
    codex-pr)     PANE="main:2.0" ;;
    codex-bench)  PANE="main:3.0" ;;
    claude|monitor) PANE="main:4.0" ;;
    [0-9]|[0-9][0-9]) PANE="main:${WINDOW}.0" ;;
    main:*)       PANE="$WINDOW" ;;
    *)            PANE="" ;;
esac

if [[ -z "$CUSTOM_MESSAGE" ]]; then
    MESSAGE="[autonomous-mode PID=$PID] If you're in the middle of something ignore this. Otherwise if you have more work to do, keep going. If you finished your overall task, then kill this process: kill $PID"
else
    MESSAGE="[autonomous-mode PID=$PID] $CUSTOM_MESSAGE"
fi

# --- PID file (one instance per target) ---
SAFE_TARGET="$(echo "$WINDOW" | tr -c 'A-Za-z0-9_.-' '_')"
PID_FILE="/tmp/autonomous_mode_${SAFE_TARGET}.pid"
LOG_FILE="/tmp/autonomous_mode_${SAFE_TARGET}.log"

if ! ( set -o noclobber; echo "$$" > "$PID_FILE" ) 2>/dev/null; then
    OTHER_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OTHER_PID" ]] && kill -0 "$OTHER_PID" 2>/dev/null; then
        echo "autonomous_mode already running for target '$WINDOW' (pid=$OTHER_PID)" >&2
        exit 1
    fi
    rm -f "$PID_FILE"
    if ! ( set -o noclobber; echo "$$" > "$PID_FILE" ) 2>/dev/null; then
        echo "Error: failed to claim pidfile $PID_FILE" >&2
        exit 1
    fi
fi

# --- Logging ---
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE"; }

# --- Cleanup ---
cleanup() {
    trap - EXIT INT TERM HUP
    rm -f "$PID_FILE"
    local pid
    for pid in $(jobs -p 2>/dev/null); do
        kill "$pid" 2>/dev/null || true
    done
    if [[ -n "${ON_EXIT_CMD:-}" ]]; then
        log "running on-exit command"
        eval "$ON_EXIT_CMD" >> "$LOG_FILE" 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM HUP

# --- Main loop ---
log "autonomous_mode started"
log "  PID:      $PID"
log "  Window:   $WINDOW"
log "  Interval: ${INTERVAL}s"
log "  Message:  $MESSAGE"
log "  Stop with: kill $PID"

echo "autonomous_mode.sh started (PID=$PID, window=$WINDOW, interval=${INTERVAL}s)"
echo "Stop with: kill $PID"

while true; do
    # tmux-msg.sh owns the full decision matrix (copy mode, draft, user watching)
    RC=0
    TMUX_MSG_SENDER="autonomous-mode" \
    TMUX_MSG_SOURCE="script" \
    "$TMUX_MSG" "$WINDOW" "$MESSAGE" >> "$LOG_FILE" 2>&1 || RC=$?
    if [[ "$RC" == "0" ]]; then
        log "nudge sent to $WINDOW"
    elif [[ "$RC" == "2" ]]; then
        log "skipped: user draft or copy mode on $WINDOW"
    else
        log "send failed (rc=$RC) on $WINDOW"
    fi
    sleep "$INTERVAL" &
    wait $! 2>/dev/null || break
done
