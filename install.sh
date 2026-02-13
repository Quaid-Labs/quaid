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
if [[ "$NODE_MAJOR" -lt 16 ]]; then
    error "Node.js 16+ is required (found v$(node --version 2>/dev/null || echo 'unknown'))."
    error "Update: https://nodejs.org"
    exit 1
fi

if ! command -v clawdbot &>/dev/null && ! command -v openclaw &>/dev/null; then
    error "OpenClaw is required. Install it first:"
    error "  npm install -g openclaw"
    error "  openclaw setup"
    exit 1
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
# setup-quaid.mjs uses CLAWDBOT_WORKSPACE or cwd. Auto-detect if not set.
if [[ -z "${CLAWDBOT_WORKSPACE:-}" ]]; then
    for dir in "$HOME/.openclaw/workspace" "$HOME/clawd" "$HOME/.config/openclaw"; do
        if [[ -d "$dir" ]] && ( [[ -f "$dir/SOUL.md" ]] || [[ -d "$dir/plugins" ]] || [[ -f "$dir/IDENTITY.md" ]] ); then
            export CLAWDBOT_WORKSPACE="$dir"
            info "Detected workspace: $dir"
            break
        fi
    done
    # If still not found, check if clawdbot/openclaw can tell us
    if [[ -z "${CLAWDBOT_WORKSPACE:-}" ]]; then
        if command -v clawdbot &>/dev/null; then
            WS=$(clawdbot config get workspace 2>/dev/null || true)
            if [[ -n "$WS" && -d "$WS" ]]; then
                export CLAWDBOT_WORKSPACE="$WS"
                info "Detected workspace from gateway: $WS"
            fi
        fi
    fi
    # Last resort: default to ~/clawd
    if [[ -z "${CLAWDBOT_WORKSPACE:-}" ]]; then
        export CLAWDBOT_WORKSPACE="$HOME/clawd"
        info "Using default workspace: $HOME/clawd"
    fi
fi

# --- Run guided installer ---
ok "Downloaded. Starting guided installer..."
echo ""
cd "$CLAWDBOT_WORKSPACE" 2>/dev/null || true
node "$RELEASE_DIR/setup-quaid.mjs"
