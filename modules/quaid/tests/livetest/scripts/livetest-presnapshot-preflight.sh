#!/usr/bin/env bash
# livetest-presnapshot-preflight.sh — Update live-test base snapshot tooling
#
# Runs the slow platform-tool maintenance path against a fresh clone of the
# current base image. If platform upgrades, Claude OAuth refresh, or final
# harness cleanup changed the clone, promotes that disk back into the locked
# base image. If nothing changed, destroys the clone and leaves the base
# untouched.
#
# Usage:
#   livetest-presnapshot-preflight.sh [options]
#
# Options:
#   --base <name>     Base VM name (default: quaid-livetest-base)
#   --name <name>     Temporary/run VM name (default: quaid-livetest-run)
#   --user <user>     SSH user for VM (default: admin)
#   --config <path>   Path to livetest-config.json (default: auto-detected)
#   --dry-run         Print operations without executing
#   -h, --help        Show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

BASE_IMAGE="quaid-livetest-base"
RUN_NAME="quaid-livetest-run"
SSH_USER="admin"
CONFIG_PATH="$CONFIG_DEFAULT"
DRY_RUN=0
PRESNAPSHOT_CLEANUP_CHANGED=0

read_config_value() {
    local key="$1"
    python3 - "$CONFIG_PATH" "$key" <<'PY'
import json
import sys

path, key = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)

cur = data
for part in key.split("."):
    if not isinstance(cur, dict):
        print("")
        raise SystemExit(0)
    cur = cur.get(part)
if cur is None and key == "remote.host":
    cur = data.get("remote_host")
print(str(cur or ""))
PY
}

normalize_ssh_target() {
    local host="$1"
    if [[ -z "$host" ]]; then
        echo ""
    elif [[ "$host" == *@* ]]; then
        echo "$host"
    else
        echo "${SSH_USER}@${host}"
    fi
}

run_presnapshot_cleanup() {
    local configured_host remote_host prune_script stale
    configured_host="$(read_config_value remote.host)"
    remote_host="$(normalize_ssh_target "$configured_host")"
    if [[ -z "$remote_host" ]]; then
        echo "Skipping presnapshot cleanup: remote.host is not set in $CONFIG_PATH" >&2
        return 0
    fi
    prune_script="~/quaidcode/dev/modules/quaid/tests/livetest/scripts/livetest-prune-openclaw-silos.sh"
    echo ""
    echo "Running presnapshot cleanup on $remote_host..."
    if ! ssh "$remote_host" "test -x $prune_script"; then
        echo "  prune helper not found on remote; skipping stale OpenClaw silo cleanup"
        return 0
    fi
    stale="$(ssh "$remote_host" "$prune_script --home ~/.quaid --dry-run" 2>/dev/null || true)"
    if [[ -z "$stale" ]]; then
        echo "  no stale OpenClaw silos found"
        return 0
    fi
    echo "  stale OpenClaw silos found:"
    printf '%s\n' "$stale" | sed 's/^/    /'
    ssh "$remote_host" "$prune_script --home ~/.quaid"
    PRESNAPSHOT_CLEANUP_CHANGED=1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE_IMAGE="$2"; shift 2 ;;
        --name) RUN_NAME="$2"; shift 2 ;;
        --user) SSH_USER="$2"; shift 2 ;;
        --config) CONFIG_PATH="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
            exit 0
            ;;
        *) echo "Error: unknown option '$1'" >&2; exit 1 ;;
    esac
done

echo "========================================"
echo " livetest-presnapshot-preflight"
echo " Base image : $BASE_IMAGE"
echo " Run VM     : $RUN_NAME"
echo " SSH user   : $SSH_USER"
echo " Config     : $CONFIG_PATH"
[[ "$DRY_RUN" == "1" ]] && echo " Mode       : DRY RUN"
echo "========================================"
echo ""

if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] would start a fresh clone from '$BASE_IMAGE' as '$RUN_NAME'"
    echo "[dry-run] would run:"
    echo "  $SCRIPT_DIR/livetest-preflight.sh --config $CONFIG_PATH --platform-upgrades-only"
    echo "[dry-run] that maintenance pass includes platform CLI upgrades and VM Claude/Codex OAuth refresh"
    echo "[dry-run] would run final presnapshot cleanup:"
    echo "  ssh <remote.host> ~/quaidcode/dev/modules/quaid/tests/livetest/scripts/livetest-prune-openclaw-silos.sh --home ~/.quaid"
    echo "[dry-run] if platform upgrades, OAuth refresh, or cleanup changed the clone, would run:"
    echo "  $SCRIPT_DIR/livetest-refresh-base.sh --base $BASE_IMAGE --name $RUN_NAME --config $CONFIG_PATH"
    echo "[dry-run] would then destroy '$RUN_NAME' and restore remote.host in config"
    exit 0
fi

"$SCRIPT_DIR/livetest-vm.sh" start \
    --base "$BASE_IMAGE" \
    --name "$RUN_NAME" \
    --user "$SSH_USER" \
    --config "$CONFIG_PATH"

set +e
"$SCRIPT_DIR/livetest-preflight.sh" \
    --config "$CONFIG_PATH" \
    --platform-upgrades-only
preflight_rc=$?
set -e

if [[ "$preflight_rc" -ne 20 && "$preflight_rc" -ne 0 ]]; then
    echo ""
    echo "Presnapshot preflight failed with exit code $preflight_rc." >&2
    echo "Leaving '$RUN_NAME' available for inspection." >&2
    exit "$preflight_rc"
fi

run_presnapshot_cleanup

if [[ "$preflight_rc" -eq 20 || "$PRESNAPSHOT_CLEANUP_CHANGED" -eq 1 ]]; then
    echo ""
    if [[ "$preflight_rc" -eq 20 ]]; then
        echo "Platform/OAuth maintenance changed the run VM. Promoting run disk to base snapshot..."
    else
        echo "Presnapshot cleanup changed the run VM. Promoting run disk to base snapshot..."
    fi
    "$SCRIPT_DIR/livetest-refresh-base.sh" \
        --base "$BASE_IMAGE" \
        --name "$RUN_NAME" \
        --config "$CONFIG_PATH"
    echo ""
    echo "Base snapshot refreshed from maintained run VM."
elif [[ "$preflight_rc" -eq 0 ]]; then
    echo ""
    echo "No platform updates, OAuth refresh, or presnapshot cleanup changes were applied. Base snapshot already current."
fi

echo ""
echo "Cleaning up temporary run VM..."
"$SCRIPT_DIR/livetest-vm.sh" stop \
    --name "$RUN_NAME" \
    --config "$CONFIG_PATH"

echo ""
echo "Presnapshot preflight complete."
