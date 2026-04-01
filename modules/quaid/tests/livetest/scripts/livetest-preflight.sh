#!/usr/bin/env bash
# livetest-preflight.sh — Pre-run safety checks, wipe, and platform prep
#
# Run this before every live test run. It:
#   1. Verifies the remote host is not the same machine as the local host
#   2. Verifies SSH connectivity to the remote
#   3. Wipes Quaid from the remote (full wipe by default)
#   4. Starts platform services on the remote (OC gateway)
#
# The remote host will have Quaid wiped and reinstalled repeatedly during a run.
# It may be running broken or unstable code at any point. It must be a dedicated
# machine separate from the one running the coordinator and tester agents.
#
# Usage:
#   livetest-preflight.sh [options]
#   livetest-preflight.sh                         # full preflight
#   livetest-preflight.sh --wipe-platform cc      # CC-only wipe (OC is live)
#   livetest-preflight.sh --skip-wipe             # skip wipe, just check + start services
#   livetest-preflight.sh --dry-run               # print commands without executing
#   livetest-preflight.sh --config path/to/livetest-config.json
#
# Options:
#   --wipe-platform <all|oc|cc|cdx>  Wipe scope (default: all)
#   --skip-wipe                      Skip the wipe step
#   --skip-platform-start            Skip starting platform services
#   --dry-run                        Print commands without executing them
#   --config <path>                  Path to livetest-config.json (default: auto-detected)
#   -h, --help                       Show this help
#
# Exit codes:
#   0  All checks passed and prep complete
#   1  Error (safety check failed, SSH unreachable, wipe failed, etc.)
#
# Set LIVETEST_WIPE_YES=1 to skip the wipe confirmation prompt.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

# --- Defaults ---
WIPE_PLATFORM="all"
SKIP_WIPE=0
SKIP_PLATFORM_START=0
DRY_RUN=0
CONFIG_PATH="$CONFIG_DEFAULT"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wipe-platform)        WIPE_PLATFORM="$2"; shift 2 ;;
        --skip-wipe)            SKIP_WIPE=1; shift ;;
        --skip-platform-start)  SKIP_PLATFORM_START=1; shift ;;
        --dry-run)              DRY_RUN=1; shift ;;
        --config)               CONFIG_PATH="$2"; shift 2 ;;
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

PASS="PASS"
FAIL="FAIL"

echo "========================================"
echo " livetest-preflight"
echo " Remote host : $REMOTE_HOST"
echo " Wipe scope  : $WIPE_PLATFORM"
[[ "$DRY_RUN" == "1" ]] && echo " Mode        : DRY RUN"
echo "========================================"
echo ""

ERRORS=0

# --- Check 1: Remote ≠ local ---
echo "[1/4] Verifying remote is not this machine..."

LOCAL_HOSTNAME="$(hostname -s 2>/dev/null || hostname)"
LOCAL_IP="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "")"

# Resolve the remote host to an IP
REMOTE_IP=""
if [[ "$DRY_RUN" == "0" ]]; then
    REMOTE_IP="$(python3 -c "import socket; print(socket.gethostbyname('$REMOTE_HOST'))" 2>/dev/null || true)"
fi

SAFE=1

# Check: hostname match
if [[ "$REMOTE_HOST" == "localhost" || "$REMOTE_HOST" == "127.0.0.1" || "$REMOTE_HOST" == "::1" ]]; then
    echo "  $FAIL  remote.host is localhost — the remote must be a separate machine"
    SAFE=0
fi

# Check: IP match
if [[ -n "$REMOTE_IP" && -n "$LOCAL_IP" && "$REMOTE_IP" == "$LOCAL_IP" ]]; then
    echo "  $FAIL  remote IP ($REMOTE_IP) matches local IP ($LOCAL_IP) — this would wipe your own machine"
    SAFE=0
fi

# Check: hostname match (covers shortname vs fqdn aliases, etc.)
if [[ "$DRY_RUN" == "0" && "$SAFE" == "1" ]]; then
    REMOTE_HOSTNAME="$(ssh "$REMOTE_HOST" 'hostname -s 2>/dev/null || hostname' 2>/dev/null || echo "")"
    if [[ -n "$REMOTE_HOSTNAME" && "$REMOTE_HOSTNAME" == "$LOCAL_HOSTNAME" ]]; then
        echo "  $FAIL  remote hostname ($REMOTE_HOSTNAME) matches local hostname ($LOCAL_HOSTNAME)"
        echo "         The remote must be a different machine from the coordinator."
        SAFE=0
    fi
fi

if [[ "$SAFE" == "1" ]]; then
    if [[ "$DRY_RUN" == "0" ]]; then
        echo "  $PASS  remote ($REMOTE_HOST / ${REMOTE_IP:-unknown ip}) ≠ local ($LOCAL_HOSTNAME / ${LOCAL_IP:-unknown ip})"
    else
        echo "  [dry-run] would verify remote ≠ local"
    fi
else
    ERRORS=$((ERRORS + 1))
fi

# --- Check 2: SSH connectivity ---
echo ""
echo "[2/4] Verifying SSH connectivity to $REMOTE_HOST..."

if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] would ssh $REMOTE_HOST 'echo ok'"
else
    SSH_RESULT="$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE_HOST" 'echo ok' 2>&1 || true)"
    if [[ "$SSH_RESULT" == "ok" ]]; then
        echo "  $PASS  SSH connected to $REMOTE_HOST"
    else
        echo "  $FAIL  SSH to $REMOTE_HOST failed: $SSH_RESULT"
        echo "         Check key-based auth and that the host is reachable."
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- Abort early if safety checks failed ---
if [[ "$ERRORS" -gt 0 ]]; then
    echo ""
    echo "Preflight aborted: $ERRORS check(s) failed." >&2
    exit 1
fi

# --- Step 3: Wipe ---
echo ""
echo "[3/4] Wiping Quaid on remote ($WIPE_PLATFORM)..."

if [[ "$SKIP_WIPE" == "1" ]]; then
    echo "  (skipped — --skip-wipe)"
else
    WIPE_ARGS=("--platform" "$WIPE_PLATFORM" "--config" "$CONFIG_PATH")
    [[ "$DRY_RUN" == "1" ]] && WIPE_ARGS+=("--dry-run")

    LIVETEST_WIPE_YES=1 "$SCRIPT_DIR/livetest-wipe.sh" "${WIPE_ARGS[@]}"
fi

# --- Step 4: Platform services ---
echo ""
echo "[4/4] Starting platform services on remote..."

if [[ "$SKIP_PLATFORM_START" == "1" ]]; then
    echo "  (skipped — --skip-platform-start)"
else
    START_ARGS=("--config" "$CONFIG_PATH")
    [[ "$DRY_RUN" == "1" ]] && START_ARGS+=("--dry-run")

    "$SCRIPT_DIR/livetest-platform-start.sh" "${START_ARGS[@]}"
fi

# --- Done ---
echo ""
echo "========================================"
echo " Preflight complete — remote is clean"
echo " Ready to start Run M0."
echo "========================================"
