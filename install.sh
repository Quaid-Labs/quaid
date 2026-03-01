#!/usr/bin/env bash
# Quaid Memory Plugin — One-Line Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/steadman-labs/quaid/main/install.sh | bash
set -euo pipefail

VERSION="${QUAID_VERSION:-latest}"
REPO="steadman-labs/quaid"
_TMPDIR="${TMPDIR:-/tmp}"
INSTALL_DIR="$_TMPDIR/quaid-install-$$"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[quaid]${NC} $*"; }
ok()    { echo -e "${GREEN}[quaid]${NC} $*"; }
error() { echo -e "${RED}[quaid]${NC} $*" >&2; }

cleanup() { rm -rf "$INSTALL_DIR" 2>/dev/null || true; }
trap cleanup EXIT

# --- Bootstrap PATH (curl|bash doesn't load shell profiles) ---
if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

# --- Pre-checks ---
if ! command -v node &>/dev/null; then
    error "Node.js is required. Install it first:"
    if [[ "$(uname)" == "Darwin" ]]; then
        error "  brew install node"
    else
        error "  sudo apt install nodejs  # Debian/Ubuntu"
        error "  sudo dnf install nodejs  # Fedora/RHEL"
    fi
    error "  # or visit https://nodejs.org"
    exit 1
fi

NODE_MAJOR=$(node -e "console.log(process.versions.node.split('.')[0])" 2>/dev/null || echo "0")
if [[ "$NODE_MAJOR" -lt 18 ]]; then
    error "Node.js 18+ is required (found v$(node --version 2>/dev/null || echo 'unknown'))."
    error "Update: https://nodejs.org"
    exit 1
fi

# --- Detect mode: standalone vs OpenClaw ---
QUAID_MODE="standalone"
if [[ -n "${QUAID_HOME:-}" ]]; then
    QUAID_MODE="standalone"
elif [[ -n "${CLAWDBOT_WORKSPACE:-}" ]]; then
    QUAID_MODE="openclaw"
elif command -v clawdbot &>/dev/null || command -v openclaw &>/dev/null; then
    QUAID_MODE="openclaw"
else
    info "OpenClaw not detected. Installing in standalone mode."
    info "  (To use with OpenClaw, install it first: npm install -g openclaw)"
fi

# --- Download ---
info "Downloading Quaid..."
mkdir -p "$INSTALL_DIR"

if [[ "$VERSION" == "latest" ]]; then
    DOWNLOAD_URL="https://github.com/$REPO/releases/latest/download/quaid-release.tar.gz"
else
    DOWNLOAD_URL="https://github.com/$REPO/releases/download/$VERSION/quaid-release.tar.gz"
fi

if command -v curl &>/dev/null; then
    curl -fsSL "$DOWNLOAD_URL" -o "$INSTALL_DIR/quaid.tar.gz"
elif command -v wget &>/dev/null; then
    wget -qO "$INSTALL_DIR/quaid.tar.gz" "$DOWNLOAD_URL"
else
    error "curl or wget required"
    exit 1
fi

if [[ ! -f "$INSTALL_DIR/quaid.tar.gz" ]]; then
    error "Download failed. Check your internet connection."
    error "URL: $DOWNLOAD_URL"
    exit 1
fi

# --- Extract ---
info "Extracting..."
tar xzf "$INSTALL_DIR/quaid.tar.gz" -C "$INSTALL_DIR"

# Find setup-quaid.mjs (could be flat or in a subdirectory)
if [[ -f "$INSTALL_DIR/setup-quaid.mjs" ]]; then
    RELEASE_DIR="$INSTALL_DIR"
else
    RELEASE_DIR=$(find "$INSTALL_DIR" -maxdepth 1 -type d -name "quaid*" | head -1)
fi
if [[ -z "$RELEASE_DIR" || ! -f "$RELEASE_DIR/setup-quaid.mjs" ]]; then
    error "Archive doesn't contain setup-quaid.mjs"
    exit 1
fi

# --- Detect workspace ---
WORKSPACE=""
if [[ -n "${QUAID_WORKSPACE:-}" ]]; then
    WORKSPACE="$QUAID_WORKSPACE"
    info "Using QUAID_WORKSPACE: $WORKSPACE"
elif [[ -n "${QUAID_HOME:-}" ]]; then
    WORKSPACE="$QUAID_HOME"
    info "Using QUAID_HOME: $WORKSPACE"
elif [[ -n "${CLAWDBOT_WORKSPACE:-}" ]]; then
    WORKSPACE="$CLAWDBOT_WORKSPACE"
    info "Using CLAWDBOT_WORKSPACE: $WORKSPACE"
elif command -v clawdbot &>/dev/null || command -v openclaw &>/dev/null; then
    # Try to detect from CLI
    WORKSPACE=$(clawdbot config get workspace 2>/dev/null || openclaw config get workspace 2>/dev/null || echo "")
    if [[ -n "$WORKSPACE" ]]; then
        info "Detected workspace from CLI: $WORKSPACE"
    fi
fi
# Fallback to OpenClaw config file if CLI lookup is unavailable/empty.
if [[ -z "$WORKSPACE" && -f "$HOME/.openclaw/openclaw.json" ]]; then
    WORKSPACE="$(python3 - <<'PY'
import json, os
cfg = os.path.expanduser("~/.openclaw/openclaw.json")
try:
    data = json.load(open(cfg, "r", encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
for candidate in [data.get("workspace"), ((data.get("agents") or {}).get("defaults") or {}).get("workspace")]:
    if isinstance(candidate, str) and candidate.strip():
        print(candidate.strip())
        raise SystemExit(0)
print("")
PY
)"
    if [[ -n "$WORKSPACE" ]]; then
        info "Detected workspace from ~/.openclaw/openclaw.json: $WORKSPACE"
    fi
fi
# Last resort: standalone default
WORKSPACE="${WORKSPACE:-$HOME/quaid}"

# Create workspace directory if it doesn't exist (standalone fresh installs)
if [[ ! -d "$WORKSPACE" ]]; then
    info "Creating workspace directory: $WORKSPACE"
    mkdir -p "$WORKSPACE"
fi

# Export canonical env vars for setup-quaid.mjs
export QUAID_HOME="$WORKSPACE"
export CLAWDBOT_WORKSPACE="$WORKSPACE"
if [[ "$QUAID_MODE" == "openclaw" ]]; then
    info "Mode: OpenClaw (workspace: $WORKSPACE)"
else
    info "Mode: Standalone (home: $WORKSPACE)"
fi

# --- Run guided installer ---
ok "Downloaded. Starting guided installer..."
echo ""
cd "$WORKSPACE" 2>/dev/null || true
node "$RELEASE_DIR/setup-quaid.mjs"
