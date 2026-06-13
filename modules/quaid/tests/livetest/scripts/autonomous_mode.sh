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

# Check for an existing instance before detaching so we can report failure
# to the caller before disappearing into the background.
_resolve_target_pane_for_files() {
    local target="$1"
    local pane=""
    case "$target" in
        self)
            pane="${TMUX_PANE:-}"
            ;;
        [0-9]|[0-9][0-9])
            pane="main:${target}.0"
            ;;
        main:*|livetest:*)
            pane="$target"
            ;;
        *)
            pane="$(python3 - "$TARGETS_FILE" "$target" <<'PY'
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
            ;;
    esac
    if [[ -n "$pane" ]]; then
        tmux display-message -p -t "$pane" '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null && return 0
        printf '%s\n' "$pane"
        return 0
    fi
    printf '%s\n' "$target"
}

_safe_target_for_files() {
    # Preserve the historical trailing underscore used by documented pidfile
    # paths, while canonicalizing aliases/window numbers to main:N.0 first.
    local canonical="$1"
    printf '%s' "$canonical" | tr -c 'A-Za-z0-9_.-' '_'
    printf '_'
}

_legacy_safe_target_for_files() {
    local target="$1"
    printf '%s' "$target" | tr -c 'A-Za-z0-9_.-' '_'
    printf '_'
}

CANONICAL_TARGET="$(_resolve_target_pane_for_files "$WINDOW")"
_SAFE_TARGET_PRE="$(_safe_target_for_files "$CANONICAL_TARGET")"
_LEGACY_SAFE_TARGET_PRE="$(_legacy_safe_target_for_files "$WINDOW")"
_PID_FILE_PRE="/tmp/autonomous_mode_${_SAFE_TARGET_PRE}.pid"
_LEGACY_PID_FILE_PRE="/tmp/autonomous_mode_${_LEGACY_SAFE_TARGET_PRE}.pid"
for _CANDIDATE_PID_FILE_PRE in "$_PID_FILE_PRE" "$_LEGACY_PID_FILE_PRE"; do
    [[ -f "$_CANDIDATE_PID_FILE_PRE" ]] || continue
    _EXISTING_PID="$(cat "$_CANDIDATE_PID_FILE_PRE" 2>/dev/null || true)"
    if [[ -n "$_EXISTING_PID" ]] && kill -0 "$_EXISTING_PID" 2>/dev/null; then
        echo "autonomous_mode already running for target '$WINDOW' (pid=$_EXISTING_PID)" >&2
        echo "  pidfile: $_CANDIDATE_PID_FILE_PRE" >&2
        exit 1
    fi
done

# Auto-detach by default so loops survive transient launch shells.
# Use -f to force foreground mode (debug/manual sessions).
if [[ "${AUTONOMOUS_MODE_CHILD:-0}" != "1" ]] && [[ "$FOREGROUND" -ne 1 ]]; then
    LAUNCH_LOG="/tmp/autonomous_mode_${_SAFE_TARGET_PRE}.launcher.log"
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
CANONICAL_TARGET="$(_resolve_target_pane_for_files "$WINDOW")"
SAFE_TARGET="$(_safe_target_for_files "$CANONICAL_TARGET")"
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
  "canonical_target": $(_json_escape "$CANONICAL_TARGET"),
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

if ! ( set -o noclobber; echo "$$" > "$PID_FILE" ) 2>/dev/null; then
    OTHER_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OTHER_PID" ]] && kill -0 "$OTHER_PID" 2>/dev/null; then
        echo "autonomous_mode already running for target '$WINDOW' (pid=$OTHER_PID)" >&2
        exit 1
    fi
    # Stale pidfile — reclaim it.
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
