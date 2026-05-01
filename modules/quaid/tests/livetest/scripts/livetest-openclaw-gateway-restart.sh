#!/usr/bin/env bash
# Robustly start or restart the OpenClaw gateway on the remote livetest host.
#
# This intentionally keys off port health, launchctl ownership, and the actual
# gateway process shape. `pkill -f openclaw-gateway` is not enough on the VM
# because the live process is usually the LaunchAgent-owned node entrypoint.

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  livetest-openclaw-gateway-restart.sh [--start|--restart|--hard] [--host <ssh-host>] [--config <path>] [--dry-run]

Modes:
  --start      Return immediately if gateway /health is responsive; otherwise start it.
  --restart    Force a real gateway restart. This is the default.
  --hard       Alias for --restart.

The helper prefers launchctl kickstart when the ai.openclaw.gateway agent is
loaded. If health still fails, it tears down the port owner and starts
`openclaw gateway` directly.
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

MODE="restart"
REMOTE_HOST=""
CONFIG_PATH="$CONFIG_DEFAULT"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --start) MODE="start"; shift ;;
        --restart|--hard) MODE="restart"; shift ;;
        --host) REMOTE_HOST="${2:-}"; shift 2 ;;
        --config) CONFIG_PATH="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Error: unknown option '$1'" >&2; usage >&2; exit 2 ;;
    esac
done

read_config() {
    python3 - "$CONFIG_PATH" "$1" <<'PY'
import json
import sys
path, key = sys.argv[1:3]
with open(path) as f:
    value = json.load(f)
for part in key.split("."):
    value = value.get(part, "") if isinstance(value, dict) else ""
print(value or "")
PY
}

if [[ -z "$REMOTE_HOST" ]]; then
    if [[ ! -f "$CONFIG_PATH" ]]; then
        echo "Error: config not found at '$CONFIG_PATH'" >&2
        exit 1
    fi
    REMOTE_HOST="$(read_config remote.host)"
fi

if [[ -z "$REMOTE_HOST" ]]; then
    echo "Error: remote host is required (--host or remote.host in config)" >&2
    exit 1
fi

echo "OpenClaw gateway $MODE"
echo "  Remote host : $REMOTE_HOST"
[[ "$DRY_RUN" == "1" ]] && echo "  Mode        : DRY RUN"

if [[ "$DRY_RUN" == "1" ]]; then
    echo "  Would run robust remote gateway ${MODE}: health check, launchctl kickstart, port-owner kill fallback, health wait."
    exit 0
fi

ssh "$REMOTE_HOST" "OPENCLAW_GATEWAY_MODE='$MODE' bash -s" <<'REMOTE'
set -euo pipefail

MODE="${OPENCLAW_GATEWAY_MODE:-restart}"
LABEL="ai.openclaw.gateway"
PORT="18789"
HEALTH_URL="http://localhost:${PORT}/health"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

health_ok() {
    curl --max-time 3 -sf "$HEALTH_URL" >/dev/null 2>&1
}

gateway_pids() {
    lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

openclaw_bin() {
    if command -v openclaw >/dev/null 2>&1; then
        command -v openclaw
    elif [[ -x /opt/homebrew/bin/openclaw ]]; then
        printf '%s\n' /opt/homebrew/bin/openclaw
    elif [[ -x /usr/local/bin/openclaw ]]; then
        printf '%s\n' /usr/local/bin/openclaw
    else
        return 1
    fi
}

launchctl_target() {
    printf 'gui/%s/%s\n' "$(id -u)" "$LABEL"
}

print_state() {
    local prefix="$1"
    local pids
    pids="$(gateway_pids | tr '\n' ' ' | sed 's/[[:space:]]*$//')"
    printf '%s health=%s pids=%s\n' "$prefix" "$(health_ok && echo ok || echo down)" "${pids:-none}"
    launchctl list | awk -v label="$LABEL" '$3 == label { print "launchctl pid="$1" status="$2" label="$3 }' || true
}

wait_for_no_listener() {
    local i
    for i in $(seq 1 20); do
        if [[ -z "$(gateway_pids)" ]]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

wait_for_health() {
    local i
    for i in $(seq 1 30); do
        if health_ok; then
            return 0
        fi
        sleep 2
    done
    return 1
}

force_kill_port_owner() {
    local pid
    for pid in $(gateway_pids); do
        kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 2
    for pid in $(gateway_pids); do
        kill -KILL "$pid" 2>/dev/null || true
    done
    wait_for_no_listener || true
}

start_direct() {
    local bin
    bin="$(openclaw_bin)" || {
        echo "openclaw not found in PATH, /opt/homebrew/bin, or /usr/local/bin" >&2
        return 127
    }
    nohup "$bin" gateway > /tmp/oc-gw.log 2>&1 &
}

print_state "before:"

if [[ "$MODE" == "start" ]] && health_ok; then
    echo "Gateway already healthy."
    exit 0
fi

target="$(launchctl_target)"
if launchctl print "$target" >/dev/null 2>&1; then
    echo "Restarting loaded LaunchAgent with launchctl kickstart -k $target"
    launchctl kickstart -k "$target" >/dev/null 2>&1 || true
    if wait_for_health; then
        print_state "after:"
        exit 0
    fi
fi

echo "LaunchAgent restart did not produce healthy gateway; forcing port-owner restart."
launchctl bootout "$target" >/dev/null 2>&1 || true
force_kill_port_owner
start_direct

if ! wait_for_health; then
    print_state "after-failed:"
    echo "Gateway did not become healthy after restart." >&2
    echo "--- /tmp/oc-gw.log tail ---" >&2
    tail -n 80 /tmp/oc-gw.log 2>/dev/null >&2 || true
    echo "--- ~/.openclaw/logs/gateway.err.log tail ---" >&2
    tail -n 80 ~/.openclaw/logs/gateway.err.log 2>/dev/null >&2 || true
    exit 1
fi

print_state "after:"
REMOTE
