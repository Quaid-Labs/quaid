#!/usr/bin/env bash
# livetest-refresh-base.sh — Promote current run VM disk to locked base image
#
# Use after preflight step-4 applies platform CLI updates. This copies the run VM
# disk over the base VM disk (with lock/unlock chmod dance) so future preflights
# do not re-pay update time.
#
# Usage:
#   livetest-refresh-base.sh [options]
#
# Options:
#   --base <name>     Base VM name (default: quaid-livetest-base)
#   --name <name>     Run VM name (default: quaid-livetest-run)
#   --config <path>   Path to livetest-config.json (accepted for symmetry/logging)
#   --dry-run         Print operations without executing
#   -h, --help        Show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

BASE_IMAGE="quaid-livetest-base"
RUN_NAME="quaid-livetest-run"
CONFIG_PATH="$CONFIG_DEFAULT"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base) BASE_IMAGE="$2"; shift 2 ;;
        --name) RUN_NAME="$2"; shift 2 ;;
        --config) CONFIG_PATH="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
            exit 0
            ;;
        *) echo "Error: unknown option '$1'" >&2; exit 1 ;;
    esac
done

die() { echo "Error: $*" >&2; exit 1; }
vm_exists() { tart list 2>/dev/null | awk 'NR>1{print $2}' | grep -qx "$1" 2>/dev/null; }
vm_state() { tart list 2>/dev/null | awk -v n="$1" '$2==n{print $NF}'; }

RUN_DISK="$HOME/.tart/vms/$RUN_NAME/disk.img"
BASE_DISK="$HOME/.tart/vms/$BASE_IMAGE/disk.img"

echo "========================================"
echo " livetest-refresh-base"
echo " Run VM      : $RUN_NAME"
echo " Base VM     : $BASE_IMAGE"
echo " Config      : $CONFIG_PATH"
[[ "$DRY_RUN" == "1" ]] && echo " Mode        : DRY RUN"
echo "========================================"

vm_exists "$RUN_NAME" || die "Run VM '$RUN_NAME' not found."
vm_exists "$BASE_IMAGE" || die "Base VM '$BASE_IMAGE' not found."
[[ -f "$RUN_DISK" ]] || die "Run VM disk not found: $RUN_DISK"
[[ -f "$BASE_DISK" ]] || die "Base VM disk not found: $BASE_DISK"

if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] would stop running VMs: $RUN_NAME, $BASE_IMAGE (if running)"
    echo "[dry-run] would chmod +w '$BASE_DISK'"
    echo "[dry-run] would copy '$RUN_DISK' -> '$BASE_DISK'"
    echo "[dry-run] would chmod -w '$BASE_DISK'"
    exit 0
fi

if [[ "$(vm_state "$RUN_NAME")" == "running" ]]; then
    echo "Stopping run VM '$RUN_NAME'..."
    tart stop "$RUN_NAME"
fi
if [[ "$(vm_state "$BASE_IMAGE")" == "running" ]]; then
    echo "Stopping base VM '$BASE_IMAGE'..."
    tart stop "$BASE_IMAGE"
fi

base_unlocked=0
cleanup() {
    if [[ "$base_unlocked" -eq 1 ]]; then
        chmod -w "$BASE_DISK" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

echo "Unlocking base disk..."
chmod +w "$BASE_DISK"
base_unlocked=1

echo "Copying run disk into base..."
cp -f "$RUN_DISK" "$BASE_DISK"
sync

echo "Re-locking base disk..."
chmod -w "$BASE_DISK"
base_unlocked=0
trap - EXIT

echo ""
echo "Base image refreshed from run VM."
echo "Next: rerun preflight."
