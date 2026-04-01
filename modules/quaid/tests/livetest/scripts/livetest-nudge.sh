#!/usr/bin/env bash
# livetest-nudge.sh — Periodically nudge a livetest agent window to keep working
#
# Used by the claude-dev coordinator to keep livetester agents active during a run.
# Call this manually at run start for each livetester window; kill the PIDs when done.
#
# Usage:
#   livetest-nudge.sh -w <window> [-t <seconds>] [-m <message>] [-r <run-label>]
#   livetest-nudge.sh -w livetest:CC-tester -r "Run 36"
#   livetest-nudge.sh -w livetest:OC-tester -t 600 -r "Run 36"
#
# Options:
#   -w <window>      Tmux window target. Required. Accepts livetest:NAME or main:N style.
#   -t <seconds>     Interval between nudges (default: 600 = 10 minutes, minimum: 30)
#   -r <run-label>   Run label included in the nudge message (e.g. "Run 36")
#   -m <message>     Custom message override. PID is always prepended.
#   -n <command>     Shell command to run on exit. $WINDOW and $PID are available.
#   -h               Show this help
#
# Default message (when -r is given):
#   "[livetest-nudge PID=X] <run-label> in progress. Check your milestone queue for
#    STATUS or ISSUE messages. If nothing pending, monitor and stay ready."
#
# Default message (no -r):
#   "[livetest-nudge PID=X] Livetest in progress. Check your milestone queue for
#    STATUS or ISSUE messages. If nothing pending, monitor and stay ready."
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
INTERVAL=600
RUN_LABEL=""
CUSTOM_MESSAGE=""
ON_EXIT_CMD=""

usage() {
    sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
    exit 0
}

while getopts "w:t:r:m:n:h" opt; do
    case "$opt" in
        w) WINDOW="$OPTARG" ;;
        t) INTERVAL="$OPTARG" ;;
        r) RUN_LABEL="$OPTARG" ;;
        m) CUSTOM_MESSAGE="$OPTARG" ;;
        n) ON_EXIT_CMD="$OPTARG" ;;
        h) usage ;;
        *) usage ;;
    esac
done

if [[ -z "$WINDOW" ]]; then
    echo "Error: -w <window> is required" >&2
    echo "Example: $0 -w livetest:CC-tester -r 'Run 36'" >&2
    exit 1
fi

if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [[ "$INTERVAL" -lt 30 ]]; then
    echo "Error: interval must be an integer >= 30" >&2
    exit 1
fi

PID=$$

# --- Resolve target to tmux pane (mirrors tmux-msg.sh) ---
case "$WINDOW" in
    livetest:*) PANE="$WINDOW" ;;
    main:*)     PANE="$WINDOW" ;;
    [0-9]|[0-9][0-9]) PANE="main:${WINDOW}.0" ;;
    *)          PANE="$WINDOW" ;;
esac

# --- Build message ---
if [[ -n "$CUSTOM_MESSAGE" ]]; then
    MESSAGE="[livetest-nudge PID=$PID] $CUSTOM_MESSAGE"
elif [[ -n "$RUN_LABEL" ]]; then
    MESSAGE="[livetest-nudge PID=$PID] $RUN_LABEL in progress. Check your milestone queue for STATUS or ISSUE messages. If nothing pending, monitor and stay ready."
else
    MESSAGE="[livetest-nudge PID=$PID] Livetest in progress. Check your milestone queue for STATUS or ISSUE messages. If nothing pending, monitor and stay ready."
fi

# --- PID file (one instance per target) ---
SAFE_TARGET="$(echo "$WINDOW" | tr -c 'A-Za-z0-9_.-' '_')"
PID_FILE="/tmp/livetest_nudge_${SAFE_TARGET}.pid"
LOG_FILE="/tmp/livetest_nudge_${SAFE_TARGET}.log"

if ! ( set -o noclobber; echo "$$" > "$PID_FILE" ) 2>/dev/null; then
    OTHER_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OTHER_PID" ]] && kill -0 "$OTHER_PID" 2>/dev/null; then
        echo "livetest-nudge already running for target '$WINDOW' (pid=$OTHER_PID)" >&2
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
log "livetest-nudge started"
log "  PID:      $PID"
log "  Window:   $WINDOW"
log "  Interval: ${INTERVAL}s"
log "  Message:  $MESSAGE"
log "  Stop with: kill $PID"

echo "livetest-nudge.sh started (PID=$PID, window=$WINDOW, interval=${INTERVAL}s)"
echo "Stop with: kill $PID"

while true; do
    RC=0
    TMUX_MSG_SENDER="livetest-nudge" \
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
