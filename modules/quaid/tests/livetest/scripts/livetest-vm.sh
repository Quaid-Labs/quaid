#!/usr/bin/env bash
# livetest-vm.sh — Tart VM lifecycle manager for Quaid live tests
#
# Manages a disposable macOS VM that replaces a physical remote host (e.g. alfie)
# for each live test run. Each run clones a base snapshot, boots it, runs tests,
# then destroys the clone. The base retains credentials and pre-installed platform
# CLIs across runs.
#
# Usage:
#   livetest-vm.sh start [--base BASE] [--name NAME] [--user USER] [--config CONFIG]
#   livetest-vm.sh stop  [--name NAME] [--config CONFIG]
#   livetest-vm.sh ip    [--name NAME]
#   livetest-vm.sh status
#   livetest-vm.sh upgrade-base [--base BASE] [--user USER]
#   livetest-vm.sh setup-base [--source SOURCE] [--name NAME]
#
# Commands:
#   start          Clone base image, boot VM, patch livetest-config.json with VM IP
#   stop           Stop and destroy the run VM, restore original host in config
#   ip             Print IP of the named (or active) run VM
#   status         Show all quaid-livetest-* VMs and their state
#   upgrade-base   Boot base clone, wait for manual upgrades, save new base
#   setup-base     One-time setup: create base image from a Sequoia OCI image
#
# Options:
#   --base BASE    Base image name (default: quaid-livetest-base)
#   --name NAME    Run VM name (default: quaid-livetest-run)
#   --user USER    SSH user inside VM (default: admin)
#   --config PATH  Path to livetest-config.json (default: auto-detected)
#
# State file: tests/livetest/.livetest-vm.state (gitignored)
# When a VM is active, livetest-config.json's remote.host is patched to the VM IP.
# When stop is called, the original host is restored from the state file.
#
# Base image requirements:
#   - macOS Sequoia (arm64)
#   - openclaw, codex, claude CLIs installed and authenticated
#   - SSH enabled (System Settings > General > Sharing > Remote Login)
#   - Coordinator's SSH public key in ~/.ssh/authorized_keys
#   - QUAID_PYTHON_BIN set in ~/.zshrc or ~/.zprofile if needed
#
# See: tests/livetest/VM-SETUP.md for base image setup instructions.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVETEST_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DEFAULT="$LIVETEST_DIR/livetest-config.json"
STATE_FILE="$LIVETEST_DIR/.livetest-vm.state"

# Defaults
BASE_IMAGE="quaid-livetest-base"
RUN_NAME="quaid-livetest-run"
SSH_USER="admin"
CONFIG_PATH="$CONFIG_DEFAULT"
BOOT_TIMEOUT=180   # seconds to wait for VM SSH to become ready
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes)

# ── Argument parsing ────────────────────────────────────────────────────────

COMMAND="${1:-}"
if [[ -z "$COMMAND" ]]; then
    echo "Usage: livetest-vm.sh <start|stop|ip|status|upgrade-base|setup-base> [options]" >&2
    exit 1
fi
shift

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base)    BASE_IMAGE="$2"; shift 2 ;;
        --name)    RUN_NAME="$2"; shift 2 ;;
        --user)    SSH_USER="$2"; shift 2 ;;
        --config)  CONFIG_PATH="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
            exit 0
            ;;
        *) echo "Error: unknown option '$1'" >&2; exit 1 ;;
    esac
done

# ── Helpers ─────────────────────────────────────────────────────────────────

die() { echo "Error: $*" >&2; exit 1; }

vm_exists() { tart list 2>/dev/null | awk 'NR>1{print $2}' | grep -qx "$1" 2>/dev/null; }

vm_state() { tart list 2>/dev/null | awk -v n="$1" '$2==n{print $NF}'; }

vm_ip() {
    local name="$1"
    tart ip "$name" 2>/dev/null || true
}

# Retry tart ip until we get an address or timeout
wait_for_ip() {
    local name="$1" elapsed=0 ip=""
    echo "  Waiting for VM to get an IP address..." >&2
    while [[ $elapsed -lt $BOOT_TIMEOUT ]]; do
        ip="$(vm_ip "$name")"
        if [[ -n "$ip" && "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            echo "  VM IP: $ip" >&2
            echo "$ip"
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    return 1
}

# Retry SSH until it accepts connections or timeout
wait_for_ssh() {
    local ip="$1" user="$2" elapsed=0
    echo "  Waiting for SSH to become ready on $ip..."
    while [[ $elapsed -lt $BOOT_TIMEOUT ]]; do
        if ssh "${SSH_OPTS[@]}" "${user}@${ip}" 'echo ok' 2>/dev/null | grep -q ok; then
            echo "  SSH ready."
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    return 1
}

# Patch a JSON string field in livetest-config.json using python3
patch_config_host() {
    local config="$1" new_host="$2"
    python3 - "$config" "$new_host" <<'PYEOF'
import sys, json
path, new_host = sys.argv[1], sys.argv[2]
with open(path) as f:
    c = json.load(f)
c.setdefault("remote", {})["host"] = new_host
with open(path, "w") as f:
    json.dump(c, f, indent=2)
    f.write("\n")
PYEOF
}

read_config_host() {
    python3 -c "
import json
with open('$CONFIG_PATH') as f:
    c = json.load(f)
print(c.get('remote', {}).get('host', ''))
"
}

save_state() {
    local vm_name="$1" vm_ip="$2" orig_host="$3"
    python3 - "$STATE_FILE" "$vm_name" "$vm_ip" "$orig_host" <<'PYEOF'
import sys, json
from datetime import datetime, timezone
state_file, vm_name, vm_ip, orig_host = sys.argv[1:]
state = {
    "vm_name": vm_name,
    "vm_ip": vm_ip,
    "original_host": orig_host,
    "started_at": datetime.now(timezone.utc).isoformat()
}
with open(state_file, "w") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
PYEOF
}

load_state() {
    if [[ ! -f "$STATE_FILE" ]]; then
        die "No active VM state found (expected $STATE_FILE). Run 'start' first."
    fi
    python3 -c "
import json
with open('$STATE_FILE') as f:
    s = json.load(f)
print(s.get('$1', ''))
"
}

clear_state() { rm -f "$STATE_FILE"; }

# ── Commands ─────────────────────────────────────────────────────────────────

cmd_start() {
    # Validate base image exists
    if ! vm_exists "$BASE_IMAGE"; then
        die "Base image '$BASE_IMAGE' not found. Run 'setup-base' or 'tart clone' to create it."
    fi

    # Destroy any stale run VM
    if vm_exists "$RUN_NAME"; then
        echo "Stale run VM '$RUN_NAME' found — destroying it first..."
        local state
        state="$(vm_state "$RUN_NAME")"
        if [[ "$state" == "running" ]]; then
            tart stop "$RUN_NAME" 2>/dev/null || true
        fi
        tart delete "$RUN_NAME" 2>/dev/null || true
    fi

    # Validate config
    if [[ ! -f "$CONFIG_PATH" ]]; then
        die "Config not found at '$CONFIG_PATH'"
    fi

    ORIG_HOST="$(read_config_host)"
    echo ""
    echo "========================================"
    echo " livetest-vm start"
    echo " Base image  : $BASE_IMAGE"
    echo " Run VM      : $RUN_NAME"
    echo " SSH user    : $SSH_USER"
    echo " Config      : $CONFIG_PATH"
    echo "========================================"
    echo ""

    # Clone base → run VM
    echo "[1/4] Cloning $BASE_IMAGE → $RUN_NAME..."
    tart clone "$BASE_IMAGE" "$RUN_NAME"
    echo "  Clone complete."
    chmod +w "${HOME}/.tart/vms/${RUN_NAME}/disk.img"

    # Boot VM (headless)
    echo ""
    echo "[2/4] Booting $RUN_NAME (headless)..."
    tart run --no-graphics "$RUN_NAME" &
    TART_PID=$!
    echo "  VM process started (pid $TART_PID)."

    # Wait for IP
    echo ""
    echo "[3/4] Waiting for network..."
    VM_IP="$(wait_for_ip "$RUN_NAME")" || die "VM did not get an IP within ${BOOT_TIMEOUT}s. Check tart and VM network setup."

    # Wait for SSH
    echo ""
    echo "[4/4] Waiting for SSH..."
    wait_for_ssh "$VM_IP" "$SSH_USER" || die "SSH not ready within ${BOOT_TIMEOUT}s on $SSH_USER@$VM_IP. Check SSH is enabled in the base image."

    # Patch config with user@IP so ssh commands work without extra config
    local ssh_host="${SSH_USER}@${VM_IP}"
    patch_config_host "$CONFIG_PATH" "$ssh_host"
    save_state "$RUN_NAME" "$VM_IP" "$ORIG_HOST"

    echo ""
    echo "========================================"
    echo " VM ready"
    echo " Name  : $RUN_NAME"
    echo " IP    : $VM_IP"
    echo " SSH   : ssh ${SSH_USER}@${VM_IP}"
    echo " Config: remote.host patched to ${SSH_USER}@${VM_IP}"
    echo "========================================"
    echo ""
    echo "$VM_IP"
}

cmd_stop() {
    local vm_name="$RUN_NAME" orig_host=""
    local state_vm="" state_vm_ip=""

    # Try to load state for vm name / original host
    if [[ -f "$STATE_FILE" ]]; then
        state_vm="$(load_state vm_name)"
        state_vm_ip="$(load_state vm_ip)"
        if [[ -n "$state_vm" && "$state_vm" != "$RUN_NAME" ]]; then
            echo "  WARN  ignoring stale VM state for '$state_vm' while stopping '$RUN_NAME'" >&2
        else
            [[ -n "$state_vm" ]] && vm_name="$state_vm"
            orig_host="$(load_state original_host)"
        fi
    fi

    echo ""
    echo "========================================"
    echo " livetest-vm stop"
    echo " VM     : $vm_name"
    echo " Config : $CONFIG_PATH"
    echo "========================================"
    echo ""

    if vm_exists "$vm_name"; then
        local state
        state="$(vm_state "$vm_name")"
        if [[ "$state" == "running" ]]; then
            echo "Stopping $vm_name..."
            tart stop "$vm_name"
        fi
        echo "Deleting $vm_name..."
        tart delete "$vm_name"
        echo "  Done."
    else
        echo "  VM '$vm_name' not found — already gone."
    fi

    # Restore original host in config
    if [[ -f "$CONFIG_PATH" && -n "$orig_host" ]]; then
        local current_host expected_host_a expected_host_b
        current_host="$(read_config_host)"
        expected_host_a="$state_vm_ip"
        expected_host_b="${SSH_USER}@${state_vm_ip}"
        if [[ -n "$state_vm_ip" && ( "$current_host" == "$expected_host_a" || "$current_host" == "$expected_host_b" ) ]]; then
            patch_config_host "$CONFIG_PATH" "$orig_host"
            echo "  Config remote.host restored to '$orig_host'."
        else
            echo "  WARN  not restoring remote.host from stale state (current='$current_host' state_vm_ip='${state_vm_ip:-}')"
        fi
    fi

    clear_state
    echo ""
    echo "VM stopped and destroyed. Run VM is clean."
}

cmd_ip() {
    local vm_name="$RUN_NAME"
    if [[ -f "$STATE_FILE" ]]; then
        local state_vm
        state_vm="$(load_state vm_name)"
        [[ -n "$state_vm" ]] && vm_name="$state_vm"
    fi

    local ip
    ip="$(vm_ip "$vm_name")" || true
    if [[ -z "$ip" ]]; then
        die "Could not get IP for '$vm_name'. Is it running? (tart list)"
    fi
    echo "$ip"
}

cmd_status() {
    echo ""
    echo "Quaid live test VMs:"
    echo ""
    tart list 2>/dev/null | head -1
    tart list 2>/dev/null | awk 'NR>1 && /quaid-livetest/' || echo "  (none)"
    echo ""

    if [[ -f "$STATE_FILE" ]]; then
        echo "Active run state ($STATE_FILE):"
        cat "$STATE_FILE"
        echo ""
    fi
}

cmd_upgrade_base() {
    local upgrade_name="${BASE_IMAGE}-upgrade-$(date +%Y%m%d%H%M%S)"

    if ! vm_exists "$BASE_IMAGE"; then
        die "Base image '$BASE_IMAGE' not found."
    fi

    echo ""
    echo "========================================"
    echo " livetest-vm upgrade-base"
    echo " Source base : $BASE_IMAGE"
    echo " Upgrade VM  : $upgrade_name"
    echo " SSH user    : $SSH_USER"
    echo "========================================"
    echo ""
    echo "Cloning $BASE_IMAGE → $upgrade_name..."
    tart clone "$BASE_IMAGE" "$upgrade_name"

    echo "Booting $upgrade_name..."
    tart run --no-graphics "$upgrade_name" &

    VM_IP="$(wait_for_ip "$upgrade_name")" || die "VM did not get an IP within ${BOOT_TIMEOUT}s"
    wait_for_ssh "$VM_IP" "$SSH_USER" || die "SSH not ready on $SSH_USER@$VM_IP"

    echo ""
    echo "========================================"
    echo " VM is running. SSH in and perform upgrades:"
    echo ""
    echo "   ssh ${SSH_USER}@${VM_IP}"
    echo ""
    echo " Typical upgrade steps:"
    echo "   openclaw update"
    echo "   npm install -g @openai/codex@latest"
    echo "   # verify claude --version"
    echo ""
    echo " When done, come back here and press ENTER to save as new base."
    echo "========================================"
    echo ""
    read -r -p "Press ENTER when upgrades are complete (Ctrl-C to abort): "

    echo ""
    echo "Stopping upgrade VM..."
    tart stop "$upgrade_name"

    # Rotate: delete old base, clone upgrade → new base
    echo "Saving as new base ($BASE_IMAGE)..."
    if vm_exists "$BASE_IMAGE"; then
        local old_base="${BASE_IMAGE}-before-$(date +%Y%m%d%H%M%S)"
        echo "  Renaming old base to $old_base (kept for safety)..."
        # tart has no rename; clone then delete
        tart clone "$BASE_IMAGE" "$old_base"
        tart delete "$BASE_IMAGE"
    fi
    tart clone "$upgrade_name" "$BASE_IMAGE"
    tart delete "$upgrade_name"

    echo ""
    echo "New base saved as '$BASE_IMAGE'."
    echo "(Old base kept as '$old_base' — delete when confirmed good.)"
}

cmd_setup_base() {
    # One-time setup from OCI Sequoia base
    local source_image="${1:-ghcr.io/cirruslabs/macos-sequoia-base:latest}"

    echo ""
    echo "========================================"
    echo " livetest-vm setup-base"
    echo " Source  : $source_image"
    echo " Output  : $BASE_IMAGE"
    echo " SSH user: $SSH_USER"
    echo "========================================"
    echo ""

    # Check if destination already exists
    if vm_exists "$BASE_IMAGE"; then
        echo "Warning: '$BASE_IMAGE' already exists."
        read -r -p "Overwrite? (y/N): " confirm
        [[ "$confirm" =~ ^[Yy]$ ]] || die "Aborted."
        tart delete "$BASE_IMAGE"
    fi

    # Clone from OCI or local source
    echo "Cloning from $source_image → $BASE_IMAGE..."
    if [[ "$source_image" == ghcr.io/* || "$source_image" == oci://* ]]; then
        tart clone "$source_image" "$BASE_IMAGE"
    else
        tart clone "$source_image" "$BASE_IMAGE"
    fi

    echo ""
    echo "========================================"
    echo " Base VM created."
    echo ""
    echo " Next steps — boot and configure the VM:"
    echo ""
    echo "   tart run $BASE_IMAGE"
    echo ""
    echo " Inside the VM, complete:"
    echo "   1. Enable SSH: System Settings > General > Sharing > Remote Login"
    echo "   2. Add coordinator SSH key to ~/.ssh/authorized_keys:"
    echo "        mkdir -p ~/.ssh && chmod 700 ~/.ssh"
    echo "        echo '<your-pub-key>' >> ~/.ssh/authorized_keys"
    echo "        chmod 600 ~/.ssh/authorized_keys"
    echo "   3. Install platform CLIs:"
    echo "        brew install openclaw   # or follow openclaw install guide"
    echo "        npm install -g @openai/codex"
    echo "        # claude is pre-installed via OpenClaw or install separately"
    echo "   4. Log in to each platform CLI:"
    echo "        openclaw auth login"
    echo "        codex auth login   # or equivalent"
    echo "   5. Set up Quaid homes: mkdir -p ~/.quaid ~/quaid"
    echo "   6. Shut down the VM cleanly (sudo shutdown -h now)"
    echo ""
    echo " After setup, the VM is your credential snapshot."
    echo " Run 'livetest-vm.sh upgrade-base' to update CLIs without re-authenticating."
    echo "========================================"
}

# ── Dispatch ─────────────────────────────────────────────────────────────────

case "$COMMAND" in
    start)          cmd_start ;;
    stop)           cmd_stop ;;
    ip)             cmd_ip ;;
    status)         cmd_status ;;
    upgrade-base)   cmd_upgrade_base ;;
    setup-base)     cmd_setup_base "${1:-}" ;;
    *)
        echo "Error: unknown command '$COMMAND'" >&2
        echo "Valid commands: start, stop, ip, status, upgrade-base, setup-base" >&2
        exit 1
        ;;
esac
