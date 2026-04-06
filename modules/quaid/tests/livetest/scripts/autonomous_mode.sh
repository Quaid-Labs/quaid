#!/usr/bin/env bash
# autonomous_mode.sh — Periodically nudge a tmux agent to keep working
#
# Usage:
#   autonomous_mode.sh [options]
#   autonomous_mode.sh -w 1 -t 300
#   autonomous_mode.sh -w main:3.0 -t 600 -m "Keep going on the bug bash"
#
# Options:
#   -w <window>    Tmux target (prefer main:N.0 pane or window number), passed to tmux-msg.sh. Required.
#   -t <seconds>   Interval between messages (default: 300 = 5 minutes, minimum: 30)
#   -m <message>   Custom message. PID is always prepended. See default below.
#   -n <command>   Shell command to run on exit (e.g. send a notification).
#                  $WINDOW and $PID are available in the command string.
#   -f             Foreground mode (do not auto-detach). Default: auto-detach.
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
FOREGROUND=0

usage() {
    sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
    exit 0
}

while getopts "w:t:m:n:fh" opt; do
    case "$opt" in
        w) WINDOW="$OPTARG" ;;
        t) INTERVAL="$OPTARG" ;;
        m) CUSTOM_MESSAGE="$OPTARG" ;;
        n) ON_EXIT_CMD="$OPTARG" ;;
        f) FOREGROUND=1 ;;
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

# Auto-detach by default so loops survive transient launch shells.
# Use -f to force foreground mode (debug/manual sessions).
if [[ "${AUTONOMOUS_MODE_CHILD:-0}" != "1" ]] && [[ "$FOREGROUND" -ne 1 ]]; then
    SAFE_TARGET_PRE="$(echo "$WINDOW" | tr -c 'A-Za-z0-9_.-' '_')"
    LAUNCH_LOG="/tmp/autonomous_mode_${SAFE_TARGET_PRE}.launcher.log"
    cmd=( "$0" -w "$WINDOW" -t "$INTERVAL" -f )
    if [[ -n "$CUSTOM_MESSAGE" ]]; then
        cmd+=( -m "$CUSTOM_MESSAGE" )
    fi
    if [[ -n "$ON_EXIT_CMD" ]]; then
        cmd+=( -n "$ON_EXIT_CMD" )
    fi
    AUTONOMOUS_MODE_CHILD=1 nohup "${cmd[@]}" >> "$LAUNCH_LOG" 2>&1 < /dev/null &
    child_pid=$!
    echo "autonomous_mode.sh detached child started (pid=$child_pid, window=$WINDOW, interval=${INTERVAL}s)"
    echo "Stop with: kill $child_pid"
    exit 0
fi

PID=$$

if [[ -z "$CUSTOM_MESSAGE" ]]; then
    MESSAGE="[autonomous-mode PID=$PID] If you're in the middle of something ignore this. Otherwise if you have more work to do, keep going. If you finished your overall task, then kill this process: kill $PID"
else
    MESSAGE="[autonomous-mode PID=$PID] $CUSTOM_MESSAGE"
fi

# --- PID file (one instance per target) ---
SAFE_TARGET="$(echo "$WINDOW" | tr -c 'A-Za-z0-9_.-' '_')"
PID_FILE="/tmp/autonomous_mode_${SAFE_TARGET}.pid"
LOG_FILE="/tmp/autonomous_mode_${SAFE_TARGET}.log"
TRACE_LOG="/tmp/autonomous_mode_${SAFE_TARGET}.trace.log"
STATUS_FILE="/tmp/autonomous_mode_${SAFE_TARGET}.status.json"

STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
LOOP_COUNT=0
LAST_SEND_RC=""
LAST_OUTCOME="not_started"
STOP_REASON="running"
EXIT_CODE=0

_json_escape() {
    python3 - "$1" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1]))
PY
}

write_status() {
    local state="$1"
    local tmp_file="${STATUS_FILE}.tmp.$$"
    local now
    now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    cat > "$tmp_file" <<EOF
{
  "script": "autonomous_mode.sh",
  "state": $(_json_escape "$state"),
  "target": $(_json_escape "$WINDOW"),
  "pid": $PID,
  "interval_seconds": $INTERVAL,
  "started_at": $(_json_escape "$STARTED_AT"),
  "updated_at": $(_json_escape "$now"),
  "loop_count": $LOOP_COUNT,
  "last_send_rc": $(_json_escape "$LAST_SEND_RC"),
  "last_outcome": $(_json_escape "$LAST_OUTCOME"),
  "stop_reason": $(_json_escape "$STOP_REASON"),
  "exit_code": $EXIT_CODE,
  "pid_file": $(_json_escape "$PID_FILE"),
  "log_file": $(_json_escape "$LOG_FILE"),
  "trace_log": $(_json_escape "$TRACE_LOG")
}
EOF
    mv "$tmp_file" "$STATUS_FILE"
}

is_running_autonomous_target() {
    local candidate_pid="$1"
    [[ "$candidate_pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$candidate_pid" 2>/dev/null || return 1
    local cmdline
    cmdline="$(ps -p "$candidate_pid" -o command= 2>/dev/null || true)"
    [[ -n "$cmdline" ]] || return 1
    [[ "$cmdline" == *"autonomous_mode.sh"* ]] || return 1
    [[ "$cmdline" == *"-w $WINDOW"* ]] || return 1
    return 0
}

if ! ( set -o noclobber; echo "$$" > "$PID_FILE" ) 2>/dev/null; then
    OTHER_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OTHER_PID" ]] && is_running_autonomous_target "$OTHER_PID"; then
        echo "autonomous_mode already running for target '$WINDOW' (pid=$OTHER_PID)" >&2
        exit 1
    fi
    # Stale pidfile (dead PID or PID reused by unrelated process): reclaim it.
    rm -f "$PID_FILE"
    if ! ( set -o noclobber; echo "$$" > "$PID_FILE" ) 2>/dev/null; then
        echo "Error: failed to claim pidfile $PID_FILE" >&2
        exit 1
    fi
fi

# --- Logging ---
ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$LOG_FILE"; }
trace() { echo "[$(ts)] $*" >> "$TRACE_LOG"; }

# --- Cleanup ---
cleanup() {
    EXIT_CODE=$?
    trap - EXIT INT TERM HUP
    if [[ "$STOP_REASON" == "running" ]]; then
        STOP_REASON="process_exit"
    fi
    trace "cleanup start exit_code=$EXIT_CODE pid=$$ stop_reason=$STOP_REASON"
    rm -f "$PID_FILE"
    local pid
    for pid in $(jobs -p 2>/dev/null); do
        trace "cleanup killing child pid=$pid"
        kill "$pid" 2>/dev/null || true
    done
    if [[ -n "${ON_EXIT_CMD:-}" ]]; then
        log "running on-exit command"
        eval "$ON_EXIT_CMD" >> "$LOG_FILE" 2>&1 || true
    fi
    write_status "stopped"
    trace "cleanup complete exit_code=$EXIT_CODE stop_reason=$STOP_REASON"
}
trap cleanup EXIT
trap 'STOP_REASON="signal:INT"; EXIT_CODE=130; exit 130' INT
trap 'STOP_REASON="signal:TERM"; EXIT_CODE=143; exit 143' TERM
trap 'STOP_REASON="signal:HUP"; EXIT_CODE=129; exit 129' HUP

# --- Main loop ---
log "autonomous_mode started"
log "  PID:      $PID"
log "  Window:   $WINDOW"
log "  Interval: ${INTERVAL}s"
log "  Message:  $MESSAGE"
log "  Stop with: kill $PID"
trace "startup pid=$PID ppid=$PPID window=$WINDOW interval=$INTERVAL pid_file=$PID_FILE log_file=$LOG_FILE"
write_status "running"

echo "autonomous_mode.sh started (PID=$PID, window=$WINDOW, interval=${INTERVAL}s)"
echo "Stop with: kill $PID"

while true; do
    LOOP_COUNT=$((LOOP_COUNT + 1))
    LAST_OUTCOME="send_start"
    write_status "running"
    trace "loop begin pid=$PID window=$WINDOW"
    # tmux-msg.sh owns the full decision matrix (copy mode, draft, user watching)
    RC=0
    trace "send start target=$WINDOW"
    TMUX_MSG_SENDER="autonomous-mode" \
    TMUX_MSG_SOURCE="script" \
    "$TMUX_MSG" "$WINDOW" "$MESSAGE" >> "$LOG_FILE" 2>&1 || RC=$?
    LAST_SEND_RC="$RC"
    trace "send end rc=$RC target=$WINDOW"
    if [[ "$RC" == "0" ]]; then
        LAST_OUTCOME="sent"
        log "nudge sent to $WINDOW"
    elif [[ "$RC" == "2" ]]; then
        LAST_OUTCOME="skipped_busy"
        log "skipped: user draft or copy mode on $WINDOW"
    else
        LAST_OUTCOME="send_failed"
        log "send failed (rc=$RC) on $WINDOW"
    fi
    write_status "running"
    trace "sleep start interval=$INTERVAL"
    LAST_OUTCOME="sleeping"
    write_status "sleeping"
    sleep "$INTERVAL" &
    wait $! 2>/dev/null || {
        wait_rc=$?
        STOP_REASON="sleep_wait_interrupted:$wait_rc"
        LAST_OUTCOME="sleep_interrupted"
        write_status "stopping"
        trace "sleep wait interrupted rc=$wait_rc"
        break
    }
    trace "sleep complete interval=$INTERVAL"
done
