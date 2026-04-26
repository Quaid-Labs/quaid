#!/usr/bin/env bash
# livetest-session-init.sh - Create the canonical local livetest tmux layout.
#
# Usage:
#   livetest-session-init.sh [--config PATH] [--force] [--skip-nudges] [--run-label LABEL] [--dry-run]
#
# Creates one local tmux window per enabled lane:
#   - left pane:  local tester agent from tester.cli
#   - right pane: SSH shell to remote.host
#
# This script only manages the local tmux control surface. It does not install,
# wipe, or modify the remote host.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

CONFIG_PATH="$CONFIG_DEFAULT"
FORCE=0
SKIP_NUDGES=0
DRY_RUN=0
RUN_LABEL="livetest"

usage() {
    sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --skip-nudges) SKIP_NUDGES=1; shift ;;
        --run-label) RUN_LABEL="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Error: unknown option '$1'" >&2; exit 1 ;;
    esac
done

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Error: config not found at '$CONFIG_PATH'" >&2
    echo "Copy livetest-config.template.json to livetest-config.json and fill it in." >&2
    exit 1
fi

read_config() {
    python3 - "$CONFIG_PATH" "$1" <<'PY'
import json
import sys
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = cfg
for part in sys.argv[2].split("."):
    if not isinstance(value, dict):
        value = ""
        break
    value = value.get(part, "")
    if value == "":
        break
if isinstance(value, bool):
    print("true" if value else "false")
else:
    print(value)
PY
}

shell_quote() {
    printf "%q" "$1"
}

REMOTE_HOST="$(read_config remote.host)"
SESSION="$(read_config tmux.livetest_session)"
TESTER_CLI="$(read_config tester.cli)"

SESSION="${SESSION:-livetest}"
TESTER_CLI="${TESTER_CLI:-codex --yolo}"

if [[ -z "$REMOTE_HOST" ]]; then
    echo "Error: remote.host must be set in $CONFIG_PATH" >&2
    exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
    echo "Error: tmux is required on the local coordinator machine" >&2
    exit 1
fi

lanes=()
for lane in CC OC CDX; do
    key="$(printf '%s' "$lane" | tr '[:upper:]' '[:lower:]')"
    enabled="$(read_config "platforms.$key.enabled")"
    if [[ "$enabled" == "true" || "$enabled" == "True" ]]; then
        lanes+=("$lane")
    fi
done

if [[ "${#lanes[@]}" -eq 0 ]]; then
    echo "Error: no platforms are enabled in $CONFIG_PATH" >&2
    exit 1
fi

session_exists() {
    tmux has-session -t "$SESSION" 2>/dev/null
}

window_exists() {
    local name="$1"
    session_exists || return 1
    tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -Fxq "$name"
}

window_count() {
    tmux list-windows -t "$SESSION" 2>/dev/null | wc -l | tr -d ' '
}

ensure_session_for_force_kill() {
    if session_exists && [[ "$(window_count)" == "1" ]]; then
        tmux new-window -d -t "$SESSION:" -n "__livetest_init_tmp__" "${SHELL:-/bin/zsh}"
    fi
}

create_lane_window() {
    local lane="$1"
    local ssh_cmd
    ssh_cmd="ssh $(shell_quote "$REMOTE_HOST")"

    if window_exists "$lane"; then
        if [[ "$FORCE" != "1" ]]; then
            echo "Error: $SESSION:$lane already exists. Use --force to recreate livetest lane windows." >&2
            exit 1
        fi
        ensure_session_for_force_kill
        tmux kill-window -t "$SESSION:$lane"
    fi

    if ! session_exists; then
        tmux new-session -d -s "$SESSION" -n "$lane" "$TESTER_CLI"
    else
        tmux new-window -d -t "$SESSION:" -n "$lane" "$TESTER_CLI"
    fi

    tmux split-window -h -t "$SESSION:$lane.0" "$ssh_cmd"
    tmux select-pane -t "$SESSION:$lane.0"
}

start_nudge() {
    local lane="$1"
    local target="$SESSION:$lane.0"
    local safe_target log_file
    safe_target="$(printf '%s' "$target" | tr -c 'A-Za-z0-9_.-' '_')"
    log_file="/tmp/livetest_session_init_${safe_target}.log"
    "$SCRIPT_DIR/livetest-nudge.sh" -w "$target" -r "$RUN_LABEL" >>"$log_file" 2>&1 &
    local nudge_pid="$!"
    disown "$nudge_pid" 2>/dev/null || true
    echo "  nudge $target pid=$nudge_pid log=$log_file"
}

echo "livetest-session-init.sh"
echo "  Config     : $CONFIG_PATH"
echo "  Session    : $SESSION"
echo "  Remote     : $REMOTE_HOST"
echo "  Tester CLI : $TESTER_CLI"
echo "  Lanes      : ${lanes[*]}"
echo "  Force      : $FORCE"
echo "  Nudges     : $([[ "$SKIP_NUDGES" == "1" ]] && echo skipped || echo enabled)"

if [[ "$DRY_RUN" == "1" ]]; then
    echo ""
    echo "[dry-run] would create/recreate local tmux windows and SSH panes"
    exit 0
fi

for lane in "${lanes[@]}"; do
    create_lane_window "$lane"
done

if session_exists && window_exists "__livetest_init_tmp__"; then
    tmux kill-window -t "$SESSION:__livetest_init_tmp__"
fi

echo ""
echo "Created local livetest panes:"
for lane in "${lanes[@]}"; do
    echo "  $SESSION:$lane.0 tester"
    echo "  $SESSION:$lane.1 ssh $REMOTE_HOST"
done

if [[ "$SKIP_NUDGES" != "1" ]]; then
    echo ""
    echo "Starting nudge loops:"
    for lane in "${lanes[@]}"; do
        start_nudge "$lane"
    done
fi

echo ""
echo "Next:"
echo "  1. Send TESTER.SKILL.md plus TESTER.<lane>.md to each tester pane."
echo "  2. Verify right-hand SSH panes are connected before briefing M0."
