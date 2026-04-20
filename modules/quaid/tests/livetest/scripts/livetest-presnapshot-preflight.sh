#!/usr/bin/env bash
# livetest-presnapshot-preflight.sh — Update live-test base snapshot tooling
#
# Runs the slow platform-tool maintenance path against a fresh clone of the
# current base image. If platform upgrades changed the clone, promotes that disk
# back into the locked base image. If nothing changed, destroys the clone and
# leaves the base untouched.
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
    echo "[dry-run] if that exits 20, would run:"
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

if [[ "$preflight_rc" -eq 20 ]]; then
    echo ""
    echo "Platform updates changed the run VM. Promoting run disk to base snapshot..."
    "$SCRIPT_DIR/livetest-refresh-base.sh" \
        --base "$BASE_IMAGE" \
        --name "$RUN_NAME" \
        --config "$CONFIG_PATH"
    echo ""
    echo "Base snapshot refreshed from upgraded run VM."
elif [[ "$preflight_rc" -eq 0 ]]; then
    echo ""
    echo "No platform updates were applied. Base snapshot already current."
else
    echo ""
    echo "Presnapshot preflight failed with exit code $preflight_rc." >&2
    echo "Leaving '$RUN_NAME' available for inspection." >&2
    exit "$preflight_rc"
fi

echo ""
echo "Cleaning up temporary run VM..."
"$SCRIPT_DIR/livetest-vm.sh" stop \
    --name "$RUN_NAME" \
    --config "$CONFIG_PATH"

echo ""
echo "Presnapshot preflight complete."
