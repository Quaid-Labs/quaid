#!/usr/bin/env bash
# livetest-preflight.sh — Pre-run safety checks, wipe, and platform prep
#
# Run this before every live test run. It:
#   1. Verifies the remote host is not the same machine as the local host
#   2. Verifies SSH connectivity to the remote
#   3. Verifies remote Homebrew Python 3.10+ is present
#   4. Upgrades platform CLIs on the remote
#   5. Wipes Quaid from the remote (full wipe by default)
#   6. Syncs the latest dev tree to the remote
#   7. Injects the CC API key into remote ~/.claude/settings.json
#   8. Starts platform services on the remote
#
# The remote host will have Quaid wiped and reinstalled repeatedly during a run.
# It may be running broken or unstable code at any point. It must be a dedicated
# machine separate from the one running the coordinator and tester agents.
#
# Usage:
#   livetest-preflight.sh [options]
#   livetest-preflight.sh                         # full preflight (dev tree)
#   livetest-preflight.sh --wipe-platform cc      # CC-only wipe (OC is live)
#   livetest-preflight.sh --skip-wipe             # skip wipe, just check + start services
#   livetest-preflight.sh --dry-run               # print commands without executing
#   livetest-preflight.sh --config path/to/livetest-config.json
#   livetest-preflight.sh --release-verify v0.3.0-alpha  # release verification mode
#
# Options:
#   --wipe-platform <all|oc|cc|cdx>  Wipe scope (default: all)
#   --skip-wipe                      Skip the wipe step
#   --skip-platform-start            Skip starting platform services
#   --dry-run                        Print commands without executing them
#   --config <path>                  Path to livetest-config.json (default: auto-detected)
#   --release-verify <tag>           Release verification mode: step 6 clones the tagged
#                                    GitHub release instead of rsyncing the dev tree.
#                                    Use for post-release validation only. Default mode
#                                    always pulls from dev.
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
RELEASE_VERIFY=""   # empty = dev mode (default); set to a tag like v0.3.0-alpha for release verification

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wipe-platform)        WIPE_PLATFORM="$2"; shift 2 ;;
        --skip-wipe)            SKIP_WIPE=1; shift ;;
        --skip-platform-start)  SKIP_PLATFORM_START=1; shift ;;
        --dry-run)              DRY_RUN=1; shift ;;
        --config)               CONFIG_PATH="$2"; shift 2 ;;
        --release-verify)       RELEASE_VERIFY="$2"; shift 2 ;;
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
if [[ -n "$RELEASE_VERIFY" ]]; then
    echo " Mode        : RELEASE VERIFICATION ($RELEASE_VERIFY)"
else
    echo " Mode        : dev (default)"
fi
[[ "$DRY_RUN" == "1" ]] && echo " Dry-run     : YES"
echo "========================================"
echo ""

ERRORS=0

# --- Check 1: Remote ≠ local ---
echo "[1/8] Verifying remote is not this machine..."

LOCAL_HOSTNAME="$(hostname -s 2>/dev/null || hostname)"
LOCAL_IP="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "")"

# Resolve the remote host to an IP
REMOTE_IP=""
if [[ "$DRY_RUN" == "0" ]]; then
    # Strip user@ prefix if present (e.g. admin@192.168.64.5 -> 192.168.64.5)
    REMOTE_HOST_ADDR="${REMOTE_HOST##*@}"
    REMOTE_IP="$(python3 -c "import socket; print(socket.gethostbyname('$REMOTE_HOST_ADDR'))" 2>/dev/null || true)"
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
echo "[2/8] Verifying SSH connectivity to $REMOTE_HOST..."

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

# --- Check 3: Homebrew Python 3.10+ on remote ---
echo ""
echo "[3/8] Verifying remote Homebrew Python 3.10+..."

if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] would verify /opt/homebrew/bin/python3 >= 3.10 on $REMOTE_HOST"
else
    PY_CHECK_RESULT="$(ssh "$REMOTE_HOST" '
if [ ! -x /opt/homebrew/bin/python3 ]; then
  echo MISSING
  exit 0
fi
/opt/homebrew/bin/python3 - <<'"'"'"'"'"'"'"'"'PY'"'"'"'"'"'"'"'"'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
' 2>&1 || true)"
    PY_CHECK_LAST_LINE="$(printf '%s\n' "$PY_CHECK_RESULT" | tail -n 1 | tr -d '\r')"
    if [[ "$PY_CHECK_LAST_LINE" == "MISSING" ]]; then
        echo "  $FAIL  /opt/homebrew/bin/python3 not found on remote"
        echo "         Install Homebrew Python 3.12+ on the VM base image and retry."
        ERRORS=$((ERRORS + 1))
    elif [[ "$PY_CHECK_LAST_LINE" =~ ^[0-9]+\.[0-9]+$ ]]; then
        PY_MAJOR="${PY_CHECK_LAST_LINE%%.*}"
        PY_MINOR="${PY_CHECK_LAST_LINE##*.}"
        if (( PY_MAJOR > 3 || (PY_MAJOR == 3 && PY_MINOR >= 10) )); then
            echo "  $PASS  remote Homebrew Python $PY_CHECK_LAST_LINE"
        else
            echo "  $FAIL  /opt/homebrew/bin/python3 is too old ($PY_CHECK_LAST_LINE)"
            echo "         Install Homebrew Python 3.12+ on the VM base image and retry."
            ERRORS=$((ERRORS + 1))
        fi
    else
        echo "  $FAIL  could not verify /opt/homebrew/bin/python3 on remote: $PY_CHECK_RESULT"
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- Step 4: Upgrade platform CLIs ---
echo ""
echo "[4/8] Upgrading platform CLIs on remote to latest..."

if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] would upgrade claude, codex, openclaw to latest on $REMOTE_HOST"
else
    ssh "$REMOTE_HOST" bash -s << 'REMOTE_UPGRADE'
set -euo pipefail
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" 2>/dev/null || true

upgrade_cli() {
    local name="$1" cmd="$2"
    printf "  upgrading %-12s ... " "$name"
    if output=$(eval "$cmd" 2>&1); then
        local ver
        ver="$($name --version 2>/dev/null | head -1 || echo "?")"
        echo "done ($ver)"
    else
        echo "WARN: upgrade failed (continuing)"
        echo "$output" | tail -3 | sed 's/^/    /'
    fi
}

run_openclaw_safe() {
    local timeout_s="${OPENCLAW_CLI_TIMEOUT_S:-45}"
    local done_token="__OPENCLAW_PRECHECK_DONE__${RANDOM}${RANDOM}"
    local tmp_out
    tmp_out="$(mktemp -t openclaw-preflight.XXXXXX)"

    (
        set +e
        openclaw update --yes
        cmd_rc=$?
        printf '%s:%s\n' "$done_token" "$cmd_rc"
        exit 0
    ) >"$tmp_out" 2>&1 &

    local cmd_pid=$!
    local elapsed=0
    while kill -0 "$cmd_pid" 2>/dev/null; do
        if (( elapsed >= timeout_s )); then
            local pgid
            pgid="$(ps -o pgid= -p "$cmd_pid" 2>/dev/null | tr -d ' ' || true)"
            if [[ -n "$pgid" ]]; then
                kill -TERM "-$pgid" 2>/dev/null || true
                sleep 1
                kill -KILL "-$pgid" 2>/dev/null || true
            fi
            kill -TERM "$cmd_pid" 2>/dev/null || true
            sleep 1
            kill -KILL "$cmd_pid" 2>/dev/null || true
            pkill -f 'openclaw-update' 2>/dev/null || true
            pkill -f 'openclaw-completion' 2>/dev/null || true
            pkill -f 'openclaw-agent' 2>/dev/null || true
            pkill -f 'openclaw-agents' 2>/dev/null || true
            wait "$cmd_pid" 2>/dev/null || true
            echo "timeout after ${timeout_s}s; cleaned stuck OpenClaw workers"
            grep -v "^${done_token}:" "$tmp_out" | tail -3 | sed 's/^/    /' || true
            rm -f "$tmp_out"
            return 124
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    wait "$cmd_pid" 2>/dev/null || true

    local done_line
    done_line="$(grep "^${done_token}:" "$tmp_out" | tail -1 || true)"
    if [[ -z "$done_line" ]]; then
        echo "failed (missing completion sentinel)"
        grep -v "^${done_token}:" "$tmp_out" | tail -3 | sed 's/^/    /' || true
        rm -f "$tmp_out"
        return 1
    fi
    local cmd_rc="${done_line##*:}"
    if [[ "$cmd_rc" == "0" ]]; then
        rm -f "$tmp_out"
        return 0
    fi
    grep -v "^${done_token}:" "$tmp_out" | tail -3 | sed 's/^/    /' || true
    rm -f "$tmp_out"
    return "$cmd_rc"
}

upgrade_cli "claude"   "npm install -g @anthropic-ai/claude-code@latest --prefer-offline 2>/dev/null || npm install -g @anthropic-ai/claude-code@latest"
upgrade_cli "codex"    "npm install -g @openai/codex@latest --prefer-offline 2>/dev/null || npm install -g @openai/codex@latest"
# openclaw: try built-in updater, fall back to no-op if not available
if command -v openclaw &>/dev/null; then
    printf "  upgrading %-12s ... " "openclaw"
    if run_openclaw_safe >/dev/null 2>&1; then
        echo "done (updated)"
    else
        echo "skipped/timeout (continuing)"
    fi
else
    echo "  openclaw      ... not found, skipping"
fi
REMOTE_UPGRADE
fi
echo "  [4/8] CLI upgrade SSH session complete"

# --- Abort early if safety checks failed ---
echo "  checking for preflight errors (ERRORS=$ERRORS)..."
if [[ "$ERRORS" -gt 0 ]]; then
    echo ""
    echo "Preflight aborted: $ERRORS check(s) failed." >&2
    exit 1
fi

# --- Step 5: Wipe ---
echo ""
echo "[5/8] Wiping Quaid on remote ($WIPE_PLATFORM)..."

if [[ "$SKIP_WIPE" == "1" ]]; then
    echo "  (skipped — --skip-wipe)"
else
    WIPE_ARGS=("--platform" "$WIPE_PLATFORM" "--config" "$CONFIG_PATH")
    [[ "$DRY_RUN" == "1" ]] && WIPE_ARGS+=("--dry-run")

    echo "  invoking livetest-wipe.sh --platform $WIPE_PLATFORM ..."
    LIVETEST_WIPE_YES=1 "$SCRIPT_DIR/livetest-wipe.sh" "${WIPE_ARGS[@]}"
    echo "  [5/8] wipe complete"
fi

# --- Step 6: Code sync to remote (after wipe so install source is not deleted) ---
echo ""
if [[ -n "$RELEASE_VERIFY" ]]; then
    echo "[6/8] Release verification mode — removing dev tree from remote (guards clean install)..."
    # The dev tree (~/quaidcode/dev/modules/quaid/) left from prior dev runs triggers
    # setup-quaid.mjs's dev-machine guard. Remove it so the release install runs clean.
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would rm -rf ~/quaidcode on remote"
    else
        ssh "$REMOTE_HOST" 'rm -rf ~/quaidcode && echo "  dev tree removed" || echo "  ~/quaidcode not present (ok)"'
        echo "  The coordinator will install via:"
        echo "    curl -fsSL https://raw.githubusercontent.com/quaid-labs/quaid/main/install.sh \\"
        echo "      | QUAID_VERSION=$RELEASE_VERIFY bash -s -- --agent --all-platforms"
        echo "  install.sh downloads the release tarball directly from GitHub releases."
    fi
else
    echo "[6/8] Syncing latest Quaid code to remote..."
    LOCAL_DEV="$HOME/quaidcode/dev"
    LOCAL_HEAD="$(cd "$LOCAL_DEV" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would sync local dev ($LOCAL_HEAD) to remote"
    else
        echo "  Building runtime artifacts (local HEAD: $LOCAL_HEAD)..."
        (cd "$LOCAL_DEV/modules/quaid" && npm run build:runtime --silent)
        echo "  rsyncing dev tree to remote..."
        rsync -a --checksum \
            --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
            --exclude='.git/' --exclude='logs/' --exclude='.env*' --exclude='.tmp/' \
            --exclude='*MagicMock*' --exclude='<MagicMock*' --exclude='~/' \
            "$LOCAL_DEV/" "$REMOTE_HOST:~/quaidcode/dev/" 2>&1 | tail -3
        # Do not pre-seed ~/.quaid/plugins/quaid before M0 install.
        # A plugin/runtime copy in place before the installer runs can cause the
        # OpenClaw gateway to auto-provision an instance and make the installer
        # think Quaid is already installed. Sync only the dev tree here; let M0
        # install own the runtime/plugin deployment step.
        echo "  $PASS  remote dev tree synced (local HEAD: $LOCAL_HEAD)"
    fi
fi

# --- Step 7: Copy coordinator CC OAuth credentials to remote ---
# Copies the coordinator's ~/.claude/.credentials.json to the remote so CC uses
# fresh OAuth rather than a potentially-expired/revoked API key.
# The coordinator's running CC session always has a valid auto-refreshed token.
# Do NOT inject ANTHROPIC_API_KEY — it overrides credentials.json and may be stale.
echo ""
echo "[7/8] Copying CC OAuth credentials to remote..."

CC_ENABLED="$(read_config platforms.cc.enabled)"
if [[ "$CC_ENABLED" != "True" && "$CC_ENABLED" != "true" ]]; then
    echo "  (skipped — CC platform not enabled in config)"
elif [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] would scp ~/.claude/.credentials.json to $REMOTE_HOST:~/.claude/.credentials.json"
else
    LOCAL_CREDS="$HOME/.claude/.credentials.json"
    if [[ ! -f "$LOCAL_CREDS" ]]; then
        echo "  WARN  $LOCAL_CREDS not found — CC will use whatever credentials are in base image"
    else
        ssh "$REMOTE_HOST" 'mkdir -p ~/.claude'
        scp "$LOCAL_CREDS" "$REMOTE_HOST:~/.claude/.credentials.json"
        echo "  $PASS  CC OAuth credentials copied to remote ~/.claude/.credentials.json"
        # Ensure any stale ANTHROPIC_API_KEY is removed from settings.json (it overrides .credentials.json)
        ssh "$REMOTE_HOST" python3 << 'PYEOF'
import json, pathlib
p = pathlib.Path.home() / '.claude' / 'settings.json'
if p.exists():
    d = json.loads(p.read_text())
    env = d.get('env', {})
    if 'ANTHROPIC_API_KEY' in env:
        del env['ANTHROPIC_API_KEY']
        d['env'] = env
        p.write_text(json.dumps(d, indent=2))
        print('  removed stale ANTHROPIC_API_KEY from settings.json')
PYEOF
    fi
fi

# --- Step 8: Platform services ---
echo ""
echo "[8/8] Starting platform services on remote..."

# In release verify mode, clear stale quaid plugin references from openclaw.json before
# starting the gateway. The wipe removes the quaid plugin runtime but leaves the OC gateway
# config intact, causing "plugin not found: quaid" on startup if the references remain.
if [[ -n "$RELEASE_VERIFY" && "$SKIP_PLATFORM_START" != "1" && "$DRY_RUN" != "1" ]]; then
    echo "  [release-verify] clearing stale quaid plugin references from remote openclaw.json..."
    ssh "$REMOTE_HOST" python3 <<'PYEOF'
import json, pathlib
p = pathlib.Path.home() / '.openclaw' / 'openclaw.json'
if not p.exists():
    print('  ~/.openclaw/openclaw.json not found — skipping')
else:
    d = json.loads(p.read_text())
    changed = False
    plugins = d.get('plugins', {})
    entries = plugins.get('entries', {})
    if 'quaid' in entries:
        del entries['quaid']
        changed = True
    allow = plugins.get('allow', [])
    if 'quaid' in allow:
        allow.remove('quaid')
        changed = True
    slots = plugins.get('slots', {})
    if slots.get('memory') == 'quaid':
        del slots['memory']
        changed = True
    if changed:
        p.write_text(json.dumps(d, indent=2))
        print('  cleared stale quaid references from ~/.openclaw/openclaw.json')
    else:
        print('  ~/.openclaw/openclaw.json has no stale quaid references — no changes needed')
PYEOF
fi

if [[ "$SKIP_PLATFORM_START" == "1" ]]; then
    echo "  (skipped — --skip-platform-start)"
else
    START_ARGS=("--config" "$CONFIG_PATH")
    [[ "$DRY_RUN" == "1" ]] && START_ARGS+=("--dry-run")

    echo "  invoking livetest-platform-start.sh..."
    "$SCRIPT_DIR/livetest-platform-start.sh" "${START_ARGS[@]}"
    echo "  [8/8] platform start complete"
fi

# --- Done ---
echo ""
echo "========================================"
echo " Preflight complete — remote is clean"
if [[ -n "$RELEASE_VERIFY" ]]; then
    echo " Mode     : RELEASE VERIFICATION ($RELEASE_VERIFY)"
    echo " Install  : curl | bash via install.sh (coordinator-driven)"
    echo " Ready for coordinator to run M0 install, then brief testers at M1."
else
    echo " Ready to start Run M0."
fi
echo "========================================"
