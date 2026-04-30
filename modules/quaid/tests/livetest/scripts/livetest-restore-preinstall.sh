#!/usr/bin/env bash
# livetest-restore-preinstall.sh — Restore run VM from preinstall snapshot + re-rsync dev tree
#
# Use when installer fails after preflight. This restores the run VM to the
# preinstall snapshot, boots it, updates livetest-config remote.host, and re-syncs
# the latest local dev tree to ~/quaidcode/dev on the VM.
#
# Usage:
#   livetest-restore-preinstall.sh [options]
#
# Options:
#   --name <name>      Run VM name (default: quaid-livetest-run)
#   --snapshot <name>  Snapshot VM name (default: quaid-livetest-preinstall)
#   --user <user>      VM SSH user (default: admin)
#   --config <path>    Path to livetest-config.json
#   --dev-root <path>  Local dev checkout root (default: ~/quaidcode/dev)
#   --timeout <sec>    Boot/SSH timeout (default: 180)
#   --dry-run          Print operations without executing
#   -h, --help         Show help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

RUN_NAME="quaid-livetest-run"
SNAPSHOT_NAME="quaid-livetest-preinstall"
SSH_USER="admin"
CONFIG_PATH="$CONFIG_DEFAULT"
DEV_ROOT="$HOME/quaidcode/dev"
BOOT_TIMEOUT=180
DRY_RUN=0
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) RUN_NAME="$2"; shift 2 ;;
        --snapshot) SNAPSHOT_NAME="$2"; shift 2 ;;
        --user) SSH_USER="$2"; shift 2 ;;
        --config) CONFIG_PATH="$2"; shift 2 ;;
        --dev-root) DEV_ROOT="$2"; shift 2 ;;
        --timeout) BOOT_TIMEOUT="$2"; shift 2 ;;
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
vm_ip() { tart ip "$1" 2>/dev/null || true; }

wait_for_ip() {
    local name="$1" elapsed=0 ip=""
    while [[ "$elapsed" -lt "$BOOT_TIMEOUT" ]]; do
        ip="$(vm_ip "$name")"
        if [[ -n "$ip" && "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "$ip"
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    return 1
}

wait_for_ssh() {
    local ip="$1" elapsed=0
    while [[ "$elapsed" -lt "$BOOT_TIMEOUT" ]]; do
        if ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" 'echo ok' 2>/dev/null | grep -q ok; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    return 1
}

verify_preinstall_state() {
    local remote_host="$1" label="$2"
    ssh "${SSH_OPTS[@]}" "$remote_host" python3 - "$label" <<'PYEOF'
import pathlib
import sys

label = sys.argv[1]
home = pathlib.Path.home()
required = [
    home / "quaidcode" / "dev" / "setup-quaid.mjs",
    home / ".claude" / ".credentials.json",
    home / ".quaid" / "shared" / "auth" / "credentials.json",
]
missing = [str(path) for path in required if not path.is_file()]
if missing:
    print(f"{label}: missing required preinstall files:", file=sys.stderr)
    for path in missing:
        print(f"  {path}", file=sys.stderr)
    raise SystemExit(1)

import datetime
import json

cc_creds = home / ".claude" / ".credentials.json"
payload = json.loads(cc_creds.read_text(encoding="utf-8"))
oauth = payload.get("claudeAiOauth")
if not isinstance(oauth, dict):
    raise SystemExit(f"{label}: missing claudeAiOauth in {cc_creds}")
raw_expires = oauth.get("expiresAt")
if raw_expires in (None, ""):
    raise SystemExit(f"{label}: missing Claude OAuth expiresAt in {cc_creds}")
if isinstance(raw_expires, (int, float)):
    expires_at = datetime.datetime.fromtimestamp(float(raw_expires) / 1000.0, tz=datetime.timezone.utc)
else:
    raw_text = str(raw_expires).strip()
    if raw_text.isdigit():
        expires_at = datetime.datetime.fromtimestamp(int(raw_text) / 1000.0, tz=datetime.timezone.utc)
    else:
        expires_at = datetime.datetime.fromisoformat(raw_text.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
if expires_at <= datetime.datetime.now(datetime.timezone.utc):
    raise SystemExit(f"{label}: Claude OAuth expired at {expires_at.isoformat()} in {cc_creds}")
print(f"{label}: verified preinstall state")
PYEOF
}

patch_config_host() {
    local config="$1" new_host="$2"
    python3 - "$config" "$new_host" <<'PYEOF'
import json, sys
path, host = sys.argv[1], sys.argv[2]
with open(path, "r", encoding="utf-8") as f:
    cfg = json.load(f)
cfg.setdefault("remote", {})["host"] = host
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PYEOF
}

echo "========================================"
echo " livetest-restore-preinstall"
echo " Snapshot VM : $SNAPSHOT_NAME"
echo " Run VM      : $RUN_NAME"
echo " SSH user    : $SSH_USER"
echo " Config      : $CONFIG_PATH"
echo " Dev root    : $DEV_ROOT"
[[ "$DRY_RUN" == "1" ]] && echo " Mode        : DRY RUN"
echo "========================================"

vm_exists "$SNAPSHOT_NAME" || die "Snapshot VM '$SNAPSHOT_NAME' not found."
[[ -f "$CONFIG_PATH" ]] || die "Config not found: $CONFIG_PATH"
[[ -d "$DEV_ROOT" ]] || die "Dev root not found: $DEV_ROOT"

if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] would stop/delete '$RUN_NAME', clone from '$SNAPSHOT_NAME', boot + wait SSH"
    echo "[dry-run] would patch config remote.host to new run IP"
    echo "[dry-run] would build runtime and rsync '$DEV_ROOT/' to ~/quaidcode/dev/"
    exit 0
fi

if vm_exists "$RUN_NAME"; then
    if [[ "$(vm_state "$RUN_NAME")" == "running" ]]; then
        tart stop "$RUN_NAME"
    fi
    tart delete "$RUN_NAME"
fi

echo "Restoring run VM from preinstall snapshot..."
tart clone "$SNAPSHOT_NAME" "$RUN_NAME"
tart run --no-graphics "$RUN_NAME" >/dev/null 2>&1 &

echo "Waiting for run VM IP + SSH..."
VM_IP="$(wait_for_ip "$RUN_NAME")" || die "Run VM did not get an IP within ${BOOT_TIMEOUT}s"
wait_for_ssh "$VM_IP" || die "SSH not ready on ${SSH_USER}@${VM_IP}"
REMOTE_HOST="${SSH_USER}@${VM_IP}"
patch_config_host "$CONFIG_PATH" "$REMOTE_HOST"

echo "Building runtime artifacts from local dev tree..."
(cd "$DEV_ROOT/modules/quaid" && npm run build:runtime --silent)

echo "Rsyncing latest dev tree to restored run VM..."
rsync -a --checksum \
    --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
    --exclude='.git/' --exclude='logs/' --exclude='.env*' --exclude='.tmp/' \
    --exclude='*MagicMock*' --exclude='<MagicMock*' --exclude='~/' \
    --exclude='.ci-local-logs/' --exclude='.pytest-home/' --exclude='.pytest_cache/' \
    --exclude='.ruff_cache/' --exclude='pytest-home/' \
    --exclude='release-promote-compatibility-work-*/' \
    --exclude='modules/quaid/tmp-lifecycle-*/' \
    "$DEV_ROOT/" "$REMOTE_HOST:~/quaidcode/dev/" 2>&1 | tail -3
verify_preinstall_state "$REMOTE_HOST" "after-restore" || die "Restored run VM is missing required preinstall state"

echo ""
echo "Restore complete."
echo "Run VM ready at: $REMOTE_HOST"
echo "Next: retry M0 install."
