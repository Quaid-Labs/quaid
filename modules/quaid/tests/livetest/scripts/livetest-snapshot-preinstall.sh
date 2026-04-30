#!/usr/bin/env bash
# livetest-snapshot-preinstall.sh — Snapshot run VM after preflight, before install
#
# Creates a preinstall snapshot clone from the current run VM, then restarts the
# run VM and updates livetest-config remote.host to the fresh run IP.
#
# Usage:
#   livetest-snapshot-preinstall.sh [options]
#
# Options:
#   --name <name>      Run VM name (default: quaid-livetest-run)
#   --snapshot <name>  Snapshot VM name (default: quaid-livetest-preinstall)
#   --user <user>      VM SSH user (default: admin)
#   --config <path>    Path to livetest-config.json
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
BOOT_TIMEOUT=180
DRY_RUN=0
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) RUN_NAME="$2"; shift 2 ;;
        --snapshot) SNAPSHOT_NAME="$2"; shift 2 ;;
        --user) SSH_USER="$2"; shift 2 ;;
        --config) CONFIG_PATH="$2"; shift 2 ;;
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
    home / ".codex" / "auth.json",
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
import base64

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

codex_auth = home / ".codex" / "auth.json"
codex_payload = json.loads(codex_auth.read_text(encoding="utf-8"))
tokens = codex_payload.get("tokens")
if not isinstance(tokens, dict):
    raise SystemExit(f"{label}: missing tokens block in {codex_auth}")
access_token = str(tokens.get("access_token") or "").strip()
refresh_token = str(tokens.get("refresh_token") or "").strip()
if not access_token:
    raise SystemExit(f"{label}: missing Codex access_token in {codex_auth}")
if not refresh_token:
    raise SystemExit(f"{label}: missing Codex refresh_token in {codex_auth}")
parts = access_token.split(".")
if len(parts) < 2:
    raise SystemExit(f"{label}: Codex access_token is not a JWT in {codex_auth}")
body = parts[1] + "=" * (-len(parts[1]) % 4)
claims = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
raw_exp = claims.get("exp")
if not isinstance(raw_exp, (int, float)):
    raise SystemExit(f"{label}: missing Codex access_token exp in {codex_auth}")
codex_expires_at = datetime.datetime.fromtimestamp(float(raw_exp), tz=datetime.timezone.utc)
if codex_expires_at <= datetime.datetime.now(datetime.timezone.utc):
    raise SystemExit(f"{label}: Codex access_token expired at {codex_expires_at.isoformat()} in {codex_auth}")
print(f"{label}: verified preinstall state")
PYEOF
}

flush_remote_state() {
    local remote_host="$1"
    ssh "${SSH_OPTS[@]}" "$remote_host" 'sync && sleep 1 && sync'
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
echo " livetest-snapshot-preinstall"
echo " Run VM      : $RUN_NAME"
echo " Snapshot VM : $SNAPSHOT_NAME"
echo " SSH user    : $SSH_USER"
echo " Config      : $CONFIG_PATH"
[[ "$DRY_RUN" == "1" ]] && echo " Mode        : DRY RUN"
echo "========================================"

vm_exists "$RUN_NAME" || die "Run VM '$RUN_NAME' not found."
[[ -f "$CONFIG_PATH" ]] || die "Config not found: $CONFIG_PATH"

if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] would stop '$RUN_NAME' if running"
    echo "[dry-run] would delete existing '$SNAPSHOT_NAME' if present"
    echo "[dry-run] would clone '$RUN_NAME' -> '$SNAPSHOT_NAME'"
    echo "[dry-run] would verify setup-quaid.mjs + shared auth before stop and after restart"
    echo "[dry-run] would flush guest filesystems before stopping '$RUN_NAME'"
    echo "[dry-run] would restart '$RUN_NAME' and patch config remote.host"
    exit 0
fi

if [[ "$(vm_state "$RUN_NAME")" == "running" ]]; then
    CURRENT_VM_IP="$(wait_for_ip "$RUN_NAME")" || die "Run VM '$RUN_NAME' did not report an IP before snapshot."
    CURRENT_REMOTE_HOST="${SSH_USER}@${CURRENT_VM_IP}"
    wait_for_ssh "$CURRENT_VM_IP" || die "SSH not ready on $CURRENT_REMOTE_HOST before snapshot"
    verify_preinstall_state "$CURRENT_REMOTE_HOST" "before-snapshot" || die "Run VM is missing required preinstall state before snapshot"
    echo "Flushing guest state before snapshot..."
    flush_remote_state "$CURRENT_REMOTE_HOST" || die "Could not flush guest state before snapshot"
    echo "Stopping run VM '$RUN_NAME'..."
    tart stop "$RUN_NAME"
fi

if vm_exists "$SNAPSHOT_NAME"; then
    if [[ "$(vm_state "$SNAPSHOT_NAME")" == "running" ]]; then
        tart stop "$SNAPSHOT_NAME"
    fi
    tart delete "$SNAPSHOT_NAME"
fi

echo "Cloning run -> preinstall snapshot..."
tart clone "$RUN_NAME" "$SNAPSHOT_NAME"

echo "Restarting run VM..."
tart run --no-graphics "$RUN_NAME" >/dev/null 2>&1 &

echo "Waiting for run VM IP + SSH..."
VM_IP="$(wait_for_ip "$RUN_NAME")" || die "Run VM did not get an IP within ${BOOT_TIMEOUT}s"
wait_for_ssh "$VM_IP" || die "SSH not ready on ${SSH_USER}@${VM_IP}"

patch_config_host "$CONFIG_PATH" "${SSH_USER}@${VM_IP}"
verify_preinstall_state "${SSH_USER}@${VM_IP}" "after-restart" || die "Run VM lost required preinstall state across snapshot/restart"

echo ""
echo "Preinstall snapshot ready: $SNAPSHOT_NAME"
echo "Run VM back online at: ${SSH_USER}@${VM_IP}"
echo "Config remote.host updated."
