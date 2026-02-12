#!/usr/bin/env bash
# Quaid Memory Plugin — One-Line Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/rekall-inc/quaid/main/install.sh | bash
set -euo pipefail

VERSION="${QUAID_VERSION:-latest}"
REPO="rekall-inc/quaid"
TMPDIR="${TMPDIR:-/tmp}"
INSTALL_DIR="$TMPDIR/quaid-install-$$"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[quaid]${NC} $*"; }
ok()    { echo -e "${GREEN}[quaid]${NC} $*"; }
error() { echo -e "${RED}[quaid]${NC} $*" >&2; }

cleanup() { rm -rf "$INSTALL_DIR" 2>/dev/null || true; }
trap cleanup EXIT

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

# --- Extract ---
info "Extracting..."
tar xzf "$INSTALL_DIR/quaid.tar.gz" -C "$INSTALL_DIR"

# Find the extracted directory (could be quaid-release or quaid-YYYYMMDD)
RELEASE_DIR=$(find "$INSTALL_DIR" -maxdepth 1 -type d -name "quaid*" | head -1)
if [[ -z "$RELEASE_DIR" || ! -f "$RELEASE_DIR/setup-quaid.mjs" ]]; then
    error "Archive doesn't contain setup-quaid.mjs"
    exit 1
fi

# --- Run guided installer ---
ok "Downloaded. Starting guided installer..."
echo ""
exec node "$RELEASE_DIR/setup-quaid.mjs"
