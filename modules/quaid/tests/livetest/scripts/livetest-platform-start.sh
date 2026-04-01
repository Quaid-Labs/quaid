#!/usr/bin/env bash
# livetest-platform-start.sh — Start platform services on the remote host
#
# Always runs via SSH to the remote host — NEVER touches the local machine.
# Reads connection details from livetest-config.json.
#
# Usage:
#   livetest-platform-start.sh [options]
#   livetest-platform-start.sh --platform oc        # start OC gateway + verify health
#   livetest-platform-start.sh --platform all       # start all platform services
#   livetest-platform-start.sh --dry-run
#
# Options:
#   --platform <all|oc>   Which platform services to start (default: all)
#   --dry-run             Print SSH commands without executing them
#   --config <path>       Path to livetest-config.json (default: auto-detected)
#   -h, --help            Show this help
#
# What this starts:
#   OC: openclaw gateway (if not already running), waits up to 60s for health
#
# CC and CDX do not need pre-start services — they are started by the tester
# agent as part of the interactive session.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

# --- Defaults ---
PLATFORM="all"
DRY_RUN=0
CONFIG_PATH="$CONFIG_DEFAULT"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform) PLATFORM="$2"; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        --config)   CONFIG_PATH="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
            exit 0
            ;;
        *) echo "Error: unknown option '$1'" >&2; exit 1 ;;
    esac
done

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Error: config not found at '$CONFIG_PATH'" >&2
    echo "Copy livetest-config.template.json to livetest-config.json and fill it in." >&2
    exit 1
fi

# --- Read config ---
read_config() {
    python3 -c "
import sys, json
with open('$CONFIG_PATH') as f:
    c = json.load(f)
key = '$1'
parts = key.split('.')
val = c
for p in parts:
    val = val.get(p, '')
    if val == '':
        break
print(val)
"
}

REMOTE_HOST="$(read_config remote.host)"

if [[ -z "$REMOTE_HOST" ]]; then
    echo "Error: remote.host must be set in $CONFIG_PATH" >&2
    exit 1
fi

echo "livetest-platform-start.sh"
echo "  Remote host : $REMOTE_HOST"
echo "  Platform    : $PLATFORM"
[[ "$DRY_RUN" == "1" ]] && echo "  Mode        : DRY RUN (no commands will execute)"
echo ""

# --- Executor ---
run_remote() {
    local desc="$1"
    local cmd="$2"
    echo "  >> $desc"
    if [[ "$DRY_RUN" == "0" ]]; then
        ssh "$REMOTE_HOST" "$cmd"
    else
        echo "     ssh $REMOTE_HOST '$cmd'"
    fi
}

# --- OC gateway ---
start_oc() {
    echo "--- OC gateway ---"
    run_remote "start gateway if not running" \
        "pgrep -f openclaw-gateway > /dev/null 2>&1 && echo 'Gateway already running' || (nohup openclaw gateway > /tmp/oc-gw.log 2>&1 & echo 'Gateway started')"

    if [[ "$DRY_RUN" == "1" ]]; then
        echo "     [dry-run] would wait for gateway health at http://localhost:18789/health"
        return
    fi

    echo "  >> waiting for gateway health (up to 60s)..."
    local i
    for i in $(seq 1 30); do
        if ssh "$REMOTE_HOST" 'curl -sf http://localhost:18789/health > /dev/null 2>&1'; then
            echo "  Gateway ready (${i}x2s elapsed)"
            return
        fi
        sleep 2
    done

    echo "Error: OC gateway did not become healthy after 60s on $REMOTE_HOST" >&2
    echo "Check /tmp/oc-gw.log on the remote host." >&2
    exit 1
}

# --- Dispatch ---
case "$PLATFORM" in
    all|oc)
        start_oc
        ;;
    cc|cdx)
        echo "No pre-start services needed for $PLATFORM — tester agent starts the session directly."
        ;;
    *)
        echo "Error: unknown platform '$PLATFORM' (valid: all, oc, cc, cdx)" >&2
        exit 1
        ;;
esac

echo ""
echo "Platform services ready ($PLATFORM on $REMOTE_HOST)."
