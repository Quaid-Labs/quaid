#!/usr/bin/env bash
# =============================================================================
# Quaid Memory Plugin — Guided Installer
# =============================================================================
# 7-step interactive installer for the Quaid memory system.
# Run from your OpenClaw workspace root: bash setup-quaid.sh
#
# Prerequisites:
#   - OpenClaw gateway installed and configured
#   - Python 3.10+
#   - SQLite 3.35+ (for FTS5 + JSON support)
#   - Ollama running locally (for embeddings), or OpenAI API key for cloud embeddings
# =============================================================================

set -euo pipefail

# --- Constants ---
QUAID_VERSION="0.1.0-alpha"
MIN_PYTHON_VERSION="3.10"
MIN_SQLITE_VERSION="3.35"
# Gateway PR #13287 — required hooks for memory extraction
# These features land in OpenClaw after version 2026.2.9
MIN_GATEWAY_VERSION="2026.2.10"
HOOKS_PR_URL="https://github.com/openclaw/openclaw/pull/13287"
HOOKS_FORK_BRANCH="solstead/openclaw:plugin-memory-hooks"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="${CLAWDBOT_WORKSPACE:-$(pwd)}"
PLUGIN_DIR="${WORKSPACE_ROOT}/plugins/quaid"
CONFIG_DIR="${WORKSPACE_ROOT}/config"
DATA_DIR="${WORKSPACE_ROOT}/data"
JOURNAL_DIR="${WORKSPACE_ROOT}/journal"
LOGS_DIR="${WORKSPACE_ROOT}/logs"
PROJECTS_DIR="${WORKSPACE_ROOT}/projects"

# Colors (disable if not a terminal)
if [[ -t 1 ]]; then
    BOLD='\033[1m'
    GREEN='\033[0;32m'
    YELLOW='\033[0;33m'
    RED='\033[0;31m'
    CYAN='\033[0;36m'
    DIM='\033[2m'
    RESET='\033[0m'
else
    BOLD='' GREEN='' YELLOW='' RED='' CYAN='' DIM='' RESET=''
fi

# --- Helpers ---
info()  { echo -e "${GREEN}[+]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[!]${RESET} $*"; }
error() { echo -e "${RED}[x]${RESET} $*"; }
step()  { echo -e "\n${BOLD}${CYAN}=== Step $1 of 7 — $2 ===${RESET}\n"; }
ask()   { echo -en "${BOLD}$1${RESET} "; read -r REPLY; }
confirm() {
    local prompt="${1:-Continue?}"
    local default="${2:-y}"
    if [[ "$default" == "y" ]]; then
        echo -en "${BOLD}${prompt} [Y/n]${RESET} "
    else
        echo -en "${BOLD}${prompt} [y/N]${RESET} "
    fi
    read -r REPLY
    REPLY="${REPLY:-$default}"
    [[ "$REPLY" =~ ^[Yy] ]]
}

# --- Dependency installer helper ---
# Attempts to install a package via Homebrew. Returns 0 on success, 1 on failure.
# Usage: _try_brew_install "package-name" "Human-readable label"
_try_brew_install() {
    local package="$1"
    local label="${2:-$1}"

    if ! command -v brew &>/dev/null; then
        echo ""
        echo "  Homebrew is not installed. It's the easiest way to manage"
        echo "  dependencies on macOS."
        echo ""
        if confirm "Install Homebrew now?"; then
            info "Installing Homebrew..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            # Add Homebrew to PATH for this session
            if [[ -f /opt/homebrew/bin/brew ]]; then
                eval "$(/opt/homebrew/bin/brew shellenv)"
            elif [[ -f /usr/local/bin/brew ]]; then
                eval "$(/usr/local/bin/brew shellenv)"
            fi
            if ! command -v brew &>/dev/null; then
                error "Homebrew install finished but 'brew' is not on PATH."
                echo "  Close and reopen your terminal, then re-run this installer."
                return 1
            fi
            info "Homebrew installed"
        else
            echo ""
            echo -e "  ${BOLD}Install ${label} manually:${RESET}"
            echo "    https://brew.sh → install Homebrew"
            echo "    brew install ${package}"
            echo ""
            echo "  Then re-run this installer."
            return 1
        fi
    fi

    echo ""
    if confirm "Install ${label} via Homebrew? (brew install ${package})"; then
        info "Installing ${label}..."
        if brew install "$package" 2>&1 | tail -5; then
            info "${label} installed"
            return 0
        else
            error "brew install ${package} failed."
            echo "  Try running it manually: brew install ${package}"
            return 1
        fi
    else
        echo ""
        echo -e "  ${BOLD}Install manually:${RESET}  brew install ${package}"
        echo "  Then re-run this installer."
        return 1
    fi
}

# --- Gateway compatibility check ---
# Quaid requires before_compaction/before_reset hooks (PR #13287).
# If hooks are present: silently returns 0.
# If hooks are missing: patches the gateway automatically, or exits.
GATEWAY_DIR=""  # Set by check_gateway_hooks if found
PATCH_FILE="${SCRIPT_DIR}/gateway-hooks.patch"

_find_gateway() {
    # Try resolving the clawdbot/openclaw CLI symlink to find the gateway root
    for cli_name in clawdbot openclaw; do
        if command -v "$cli_name" &>/dev/null; then
            local resolved
            resolved=$(readlink -f "$(command -v "$cli_name")" 2>/dev/null || readlink "$(command -v "$cli_name")" 2>/dev/null || true)
            if [[ -n "$resolved" ]]; then
                local candidate_dir
                candidate_dir=$(dirname "$(dirname "$resolved")")
                if [[ -f "${candidate_dir}/package.json" ]]; then
                    echo "$candidate_dir"
                    return 0
                fi
            fi
        fi
    done
    # Fall back to common npm global paths
    for candidate in \
        "$(npm root -g 2>/dev/null)/openclaw" \
        "$(npm root -g 2>/dev/null)/clawdbot" \
        "/opt/homebrew/lib/node_modules/openclaw" \
        "/opt/homebrew/lib/node_modules/clawdbot" \
        "/usr/local/lib/node_modules/openclaw"
    do
        if [[ -d "$candidate" ]] && [[ -f "${candidate}/package.json" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

_gateway_has_hooks() {
    local gw_path="$1"
    # Check both dist/ (bundled) and src/ (source) for the hook features
    { grep -rq "runBeforeCompaction\|before_compaction" "${gw_path}/dist/" 2>/dev/null || \
      grep -rq "runBeforeCompaction\|before_compaction" "${gw_path}/src/" 2>/dev/null; } && \
    { grep -rq "extraBootstrapFiles" "${gw_path}/dist/" 2>/dev/null || \
      grep -rq "extraBootstrapFiles" "${gw_path}/src/" 2>/dev/null; }
}

check_gateway_hooks() {
    local gw_path
    gw_path=$(_find_gateway) || {
        echo ""
        error "Could not find an OpenClaw gateway installation on this machine."
        echo ""
        echo "  Quaid is a plugin for OpenClaw and needs the gateway to run."
        echo "  It hooks into conversation events (compaction, reset) to extract memories."
        echo ""
        echo -e "  ${BOLD}How to fix:${RESET}"
        echo "    1. Install OpenClaw:  npm install -g openclaw"
        echo "    2. Set it up:         openclaw setup"
        echo "    3. Re-run this installer"
        echo ""
        echo -e "  ${DIM}If you installed OpenClaw to a non-standard path, set the CLI${RESET}"
        echo -e "  ${DIM}on your PATH so this installer can find it.${RESET}"
        return 1
    }

    GATEWAY_DIR="$gw_path"
    local gw_version=""
    gw_version=$(python3 -c "import json; print(json.load(open('${gw_path}/package.json')).get('version',''))" 2>/dev/null || true)

    # If hooks are already present, we're done — no dialog, no output beyond the check line
    if _gateway_has_hooks "$gw_path"; then
        info "Gateway OK — hooks present (v${gw_version:-unknown})"
        return 0
    fi

    # --- Hooks missing — explain what's happening clearly ---
    echo ""
    echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "  ${BOLD}${YELLOW} Gateway Patch Required${RESET}"
    echo -e "  ${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    echo "  Your OpenClaw gateway is missing memory hooks that Quaid needs."
    echo "  These hooks let Quaid capture memories when conversations are"
    echo "  compacted or reset — without them, no memories get extracted."
    echo ""
    if [[ -n "$gw_version" ]]; then
        echo -e "  Your version:     ${YELLOW}${gw_version}${RESET}"
    fi
    echo -e "  Required version: ${GREEN}${MIN_GATEWAY_VERSION}+${RESET} (with PR #13287 features)"
    echo -e "  PR details:       ${CYAN}${HOOKS_PR_URL}${RESET}"
    echo ""
    echo "  The installer will now patch your gateway to add these hooks."
    echo "  This modifies OpenClaw source files in:"
    echo -e "    ${DIM}${gw_path}${RESET}"
    echo ""

    if ! confirm "Patch the gateway now?"; then
        echo ""
        echo "  No worries — Quaid can't work without these hooks though."
        echo ""
        echo -e "  ${BOLD}When you're ready, you have two options:${RESET}"
        echo ""
        echo "    Option A — Update OpenClaw (if PR #13287 has been merged):"
        echo "      npm install -g openclaw@latest"
        echo ""
        echo "    Option B — Install from the hooks branch directly:"
        echo "      npm install -g solstead/openclaw#plugin-memory-hooks"
        echo ""
        echo "  Then re-run this installer."
        return 1
    fi

    # Strategy depends on whether this is a git checkout (has src/) or npm bundle (dist/ only)
    local is_git=false
    if [[ -d "${gw_path}/.git" ]] || git -C "$gw_path" rev-parse --git-dir &>/dev/null 2>&1; then
        is_git=true
    fi

    echo ""

    if $is_git; then
        # ---- Git checkout: apply TS source patch, then rebuild ----
        info "Detected git checkout at ${gw_path}"
        echo "  Strategy: apply source patch → rebuild TypeScript"
        echo ""

        local patch_applied=false

        # Try 1: bundled patch file (fastest, cleanest)
        if [[ -f "$PATCH_FILE" ]] && [[ -d "${gw_path}/src" ]]; then
            info "Attempt 1: Applying bundled patch file..."
            echo -e "  ${DIM}(This adds 3 features: compaction hooks, reset hooks, bootstrap file globs)${RESET}"
            local patch_output
            if patch_output=$(cd "$gw_path" && git apply "$PATCH_FILE" 2>&1); then
                info "Patch applied cleanly."
                patch_applied=true
            else
                echo ""
                warn "Bundled patch didn't apply cleanly (your gateway may have diverged)."
                echo -e "  ${DIM}${patch_output}${RESET}"
                echo ""
            fi
        fi

        # Try 2: merge from fork
        if ! $patch_applied; then
            info "Attempt 2: Merging hooks from the Quaid fork branch..."
            echo -e "  ${DIM}(Fetching solstead/openclaw → plugin-memory-hooks branch)${RESET}"
            echo ""
            if ! _patch_git_merge "$gw_path"; then
                return 1
            fi
        fi

        # Rebuild
        echo ""
        info "Rebuilding gateway from patched source..."
        echo "  This compiles TypeScript → JavaScript. May take 30-60 seconds."
        echo ""
        local build_output
        if build_output=$(cd "$gw_path" && pnpm build 2>&1) || build_output=$(cd "$gw_path" && npm run build 2>&1); then
            info "Gateway rebuilt successfully."
        else
            echo ""
            error "Gateway build failed after patching."
            echo ""
            echo "  The source patch was applied, but the TypeScript build didn't succeed."
            echo "  This usually means a dependency mismatch or build tool issue."
            echo ""
            echo -e "  ${BOLD}How to fix:${RESET}"
            echo "    cd ${gw_path}"
            echo "    pnpm install        # reinstall dependencies"
            echo "    pnpm build          # retry the build"
            echo ""
            echo "  Build output:"
            echo -e "  ${DIM}${build_output}${RESET}"
            echo ""
            echo -e "  ${BOLD}Alternative — install pre-built from fork:${RESET}"
            echo "    npm install -g solstead/openclaw#plugin-memory-hooks"
            echo ""
            echo "  Then re-run this installer."
            echo ""
            echo -e "  If you're stuck, open an issue:  ${CYAN}${HOOKS_PR_URL}${RESET}"
            return 1
        fi
    else
        # ---- npm bundle: reinstall from fork ----
        info "Detected npm global install at ${gw_path}"
        echo "  Pre-built bundles use hashed filenames — can't patch in-place."
        echo "  Strategy: reinstall OpenClaw from the hooks branch."
        echo ""
        echo "  Running: npm install -g solstead/openclaw#plugin-memory-hooks"
        echo ""
        local install_output
        if install_output=$(npm install -g "solstead/openclaw#plugin-memory-hooks" 2>&1); then
            info "Gateway reinstalled with hooks."
        else
            echo ""
            error "Gateway reinstall failed."
            echo ""
            echo "  npm couldn't install the hooks branch. Common causes:"
            echo "    - Network issue (can't reach GitHub)"
            echo "    - Permission denied (try with sudo or fix npm prefix)"
            echo "    - Node.js version incompatibility"
            echo ""
            echo "  Install output:"
            echo -e "  ${DIM}${install_output}${RESET}"
            echo ""
            echo -e "  ${BOLD}How to fix:${RESET}"
            echo "    1. Check network:   curl -s https://github.com > /dev/null && echo OK"
            echo "    2. Try with sudo:   sudo npm install -g solstead/openclaw#plugin-memory-hooks"
            echo "    3. Or update npm:   npm install -g npm@latest"
            echo ""
            echo "  If PR #13287 has been merged into OpenClaw, you can also try:"
            echo "    npm install -g openclaw@latest"
            echo ""
            echo "  Then re-run this installer."
            echo ""
            echo -e "  Need help? PR: ${CYAN}${HOOKS_PR_URL}${RESET}"
            return 1
        fi
    fi

    # Verify the patch worked
    echo ""
    info "Verifying hooks are present..."
    if _gateway_has_hooks "$gw_path"; then
        echo ""
        echo -e "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
        echo -e "  ${BOLD}${GREEN} Gateway patched successfully!${RESET}"
        echo -e "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
        echo ""
        echo "  Memory hooks (before_compaction, before_reset) are now active."
        echo "  Extra bootstrap file loading (extraBootstrapFiles) is available."
        echo ""
        echo -e "  ${DIM}Note: When OpenClaw merges PR #13287, you can switch back to the${RESET}"
        echo -e "  ${DIM}official release: npm install -g openclaw@latest${RESET}"
        echo ""
        return 0
    else
        echo ""
        error "Patch appeared to succeed, but hooks were not detected afterward."
        echo ""
        echo "  This is unexpected — the patch or build may have partially failed."
        echo ""
        echo -e "  ${BOLD}What to try:${RESET}"
        echo ""
        echo "    Option A — Reinstall from the hooks branch:"
        echo "      npm install -g solstead/openclaw#plugin-memory-hooks"
        echo ""
        echo "    Option B — If PR #13287 has been merged:"
        echo "      npm install -g openclaw@latest"
        echo ""
        echo "    Option C — Manual inspection:"
        echo "      Check if these exist in the gateway source:"
        echo "        grep -r 'runBeforeCompaction' ${gw_path}/dist/"
        echo "        grep -r 'extraBootstrapFiles' ${gw_path}/dist/"
        echo ""
        echo "  Then re-run this installer."
        echo ""
        echo -e "  Need help? PR: ${CYAN}${HOOKS_PR_URL}${RESET}"
        return 1
    fi
}

# Helper: merge hooks from fork into a git checkout
_patch_git_merge() {
    local gw_path="$1"
    info "Adding Quaid fork as a git remote..."
    (
        cd "$gw_path"
        git remote add quaid-hooks https://github.com/solstead/openclaw.git 2>/dev/null || true
    )

    info "Fetching the hooks branch..."
    local fetch_output
    if ! fetch_output=$(cd "$gw_path" && git fetch quaid-hooks plugin-memory-hooks 2>&1); then
        echo ""
        error "Couldn't fetch the hooks branch from GitHub."
        echo ""
        echo "  This usually means a network issue or the fork is unavailable."
        echo ""
        echo "  Fetch output:"
        echo -e "  ${DIM}${fetch_output}${RESET}"
        echo ""
        echo -e "  ${BOLD}How to fix:${RESET}"
        echo "    1. Check network: curl -s https://github.com > /dev/null && echo OK"
        echo "    2. Try manually:  cd ${gw_path}"
        echo "       git remote add quaid-hooks https://github.com/solstead/openclaw.git"
        echo "       git fetch quaid-hooks plugin-memory-hooks"
        echo "       git merge quaid-hooks/plugin-memory-hooks"
        echo "       pnpm build"
        echo ""
        echo -e "  Need help? PR: ${CYAN}${HOOKS_PR_URL}${RESET}"
        return 1
    fi

    info "Merging hooks into your gateway checkout..."
    local merge_output
    if merge_output=$(cd "$gw_path" && git merge quaid-hooks/plugin-memory-hooks --no-edit 2>&1); then
        info "Merge successful — no conflicts."
        return 0
    fi

    echo ""
    warn "Merge had conflicts with your local changes."
    echo -e "  ${DIM}${merge_output}${RESET}"
    echo ""
    (cd "$gw_path" && git merge --abort 2>/dev/null || true)

    info "Trying cherry-pick instead (applies hook commits individually)..."
    local pick_output
    if pick_output=$(cd "$gw_path" && git cherry-pick quaid-hooks/plugin-memory-hooks~5..quaid-hooks/plugin-memory-hooks 2>&1); then
        info "Cherry-pick successful."
        return 0
    fi

    (cd "$gw_path" && git cherry-pick --abort 2>/dev/null || true)
    echo ""
    error "Automatic patching failed — your gateway has conflicting local changes."
    echo ""
    echo "  The installer tried two strategies:"
    echo "    1. git merge  — had conflicts"
    echo "    2. cherry-pick — also had conflicts"
    echo ""
    echo "  This usually means your gateway has custom modifications that overlap"
    echo "  with the hook changes. You'll need to resolve conflicts manually."
    echo ""
    echo -e "  ${BOLD}Manual steps:${RESET}"
    echo "    cd ${gw_path}"
    echo "    git remote add quaid-hooks https://github.com/solstead/openclaw.git"
    echo "    git fetch quaid-hooks plugin-memory-hooks"
    echo "    git merge quaid-hooks/plugin-memory-hooks"
    echo "    # Resolve any conflicts, then:"
    echo "    pnpm build"
    echo ""
    echo "  Then re-run this installer."
    echo ""
    echo -e "  Need help? PR: ${CYAN}${HOOKS_PR_URL}${RESET}"
    return 1
}

# --- Collected config values (populated during steps) ---
OWNER_NAME=""
OWNER_DISPLAY=""
PROVIDER=""
HIGH_MODEL=""
LOW_MODEL=""
API_KEY_ENV=""
BASE_URL=""
EMBED_MODEL=""
EMBED_DIM=""
SYS_MEMORY=true
SYS_JOURNAL=true
SYS_PROJECTS=true
SYS_WORKSPACE=true
NOTIFICATION_LEVEL="normal"

# =============================================================================
# Step 1: Welcome & Pre-flight
# =============================================================================
step1_preflight() {
    step 1 "Pre-flight checks"

    echo -e "${BOLD}"
    local bw=50  # inner width between ║ pipes
    local pad
    pad=$(printf '%*s' "$bw" '')
    echo "  ╔$(printf '═%.0s' $(seq 1 $bw))╗"
    echo "  ║${pad}║"
    printf "  ║   %-$((bw-3))s║\n" "Quaid v${QUAID_VERSION}"
    printf "  ║   %-$((bw-3))s║\n" "\"If I'm not me, then who am I?\""
    echo "  ║${pad}║"
    printf "  ║   %-$((bw-3))s║\n" "Long-term memory for your OpenClaw bot."
    echo "  ║${pad}║"
    echo "  ╚$(printf '═%.0s' $(seq 1 $bw))╝"
    echo -e "${RESET}"
    echo -e "  Workspace: ${CYAN}${WORKSPACE_ROOT}${RESET}"
    echo ""

    # --- OpenClaw must be installed and running ---
    info "Checking OpenClaw gateway..."
    if ! command -v clawdbot &>/dev/null && ! command -v openclaw &>/dev/null; then
        echo ""
        error "OpenClaw is not installed."
        echo ""
        echo "  Quaid is a plugin for OpenClaw and requires it to run."
        echo ""
        echo -e "  ${BOLD}Install OpenClaw first:${RESET}"
        echo "    npm install -g openclaw"
        echo "    openclaw setup"
        echo ""
        echo "  Then re-run this installer."
        exit 1
    fi

    # Check gateway is actually running (check health endpoint on default port)
    local gw_running=false
    if clawdbot status &>/dev/null </dev/null; then
        gw_running=true
    elif curl -sf http://localhost:18789/health &>/dev/null; then
        gw_running=true
    fi

    if ! $gw_running; then
        echo ""
        error "OpenClaw gateway is not running."
        echo ""
        echo "  Quaid needs the gateway to be running so it can read your configuration"
        echo "  and hook into conversation events."
        echo ""
        echo -e "  ${BOLD}Start the gateway:${RESET}"
        echo "    clawdbot gateway start"
        echo ""
        echo "  Then re-run this installer."
        exit 1
    fi
    info "OpenClaw gateway is running"

    # Check that OpenClaw onboarding has been completed (at least one agent configured)
    local has_agent=false
    if clawdbot config get agents 2>/dev/null </dev/null | strings | grep -q '"id"'; then
        has_agent=true
    fi

    if ! $has_agent; then
        echo ""
        error "OpenClaw setup has not been completed."
        echo ""
        echo "  Quaid needs at least one agent configured before it can install."
        echo "  Run the OpenClaw setup wizard first:"
        echo ""
        echo -e "  ${BOLD}  openclaw setup${RESET}"
        echo ""
        echo "  Then re-run this installer."
        exit 1
    fi
    info "OpenClaw onboarding complete"

    # Check Python version
    info "Checking Python 3.10+..."
    if ! command -v python3 &>/dev/null; then
        warn "Python 3 not found."
        if _try_brew_install "python@3.12" "Python"; then
            hash -r  # refresh PATH
        else
            error "Python ${MIN_PYTHON_VERSION}+ is required. Install it and re-run this installer."
            exit 1
        fi
    fi
    local py_version
    py_version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if python3 -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)"; then
        info "Python ${py_version} — OK"
    else
        warn "Python ${py_version} found, but ${MIN_PYTHON_VERSION}+ required."
        if _try_brew_install "python@3.12" "Python 3.12"; then
            hash -r
            py_version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
            if python3 -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)"; then
                info "Python ${py_version} — OK"
            else
                error "Python is still ${py_version} after upgrade. You may need to update your PATH."
                echo "  Try: export PATH=\"\$(brew --prefix python@3.12)/bin:\$PATH\""
                exit 1
            fi
        else
            exit 1
        fi
    fi

    # Check SQLite version (bundled with Python — upgrading Python via Homebrew usually fixes this)
    info "Checking SQLite 3.35+..."
    local sqlite_version
    sqlite_version=$(python3 -c "import sqlite3; print(sqlite3.sqlite_version)")
    if python3 -c "
import sqlite3
parts = [int(x) for x in sqlite3.sqlite_version.split('.')]
exit(0 if (parts[0], parts[1]) >= (3, 35) else 1)
"; then
        info "SQLite ${sqlite_version} — OK"
    else
        warn "SQLite ${sqlite_version} found, but ${MIN_SQLITE_VERSION}+ required (for FTS5 + JSON)."
        echo ""
        echo "  Python's sqlite3 module uses the system SQLite library."
        echo "  Installing Python via Homebrew links it to a modern SQLite."
        echo ""
        if _try_brew_install "python@3.12" "Python 3.12 (with modern SQLite)"; then
            hash -r
            sqlite_version=$(python3 -c "import sqlite3; print(sqlite3.sqlite_version)")
            if python3 -c "
import sqlite3
parts = [int(x) for x in sqlite3.sqlite_version.split('.')]
exit(0 if (parts[0], parts[1]) >= (3, 35) else 1)
"; then
                info "SQLite ${sqlite_version} — OK"
            else
                error "SQLite is still ${sqlite_version}. Your Python may not be using Homebrew's SQLite."
                echo "  Try: brew install sqlite && brew install python@3.12"
                exit 1
            fi
        else
            exit 1
        fi
    fi

    # Check FTS5 support
    if python3 -c "
import sqlite3
conn = sqlite3.connect(':memory:')
try:
    conn.execute('CREATE VIRTUAL TABLE t USING fts5(content)')
    conn.close()
except:
    exit(1)
" 2>/dev/null; then
        info "FTS5 support — OK"
    else
        warn "SQLite FTS5 extension not available."
        echo "  FTS5 is included in Homebrew's SQLite and in Homebrew Python."
        if _try_brew_install "sqlite" "SQLite (with FTS5)"; then
            # Also need to reinstall Python to pick up the new SQLite
            info "Reinstalling Python to link with new SQLite..."
            brew reinstall python@3.12 2>/dev/null || brew reinstall python 2>/dev/null || true
            hash -r
            if python3 -c "
import sqlite3
conn = sqlite3.connect(':memory:')
conn.execute('CREATE VIRTUAL TABLE t USING fts5(content)')
conn.close()
" 2>/dev/null; then
                info "FTS5 support — OK"
            else
                error "FTS5 still not available. You may need to reinstall Python from Homebrew."
                echo "  Try: brew install sqlite && brew reinstall python@3.12"
                exit 1
            fi
        else
            exit 1
        fi
    fi

    # Check git
    if ! command -v git &>/dev/null; then
        warn "Git not found."
        if _try_brew_install "git" "Git"; then
            if ! command -v git &>/dev/null; then
                error "Git still not found after install. Add it to your PATH and re-run."
                exit 1
            fi
        else
            error "Git is required for doc staleness tracking and project management."
            exit 1
        fi
    fi
    info "Git $(git --version | sed 's/git version //') — OK"

    # Check gateway compatibility (hooks from PR #13287)
    info "Checking OpenClaw gateway for memory hooks..."
    echo -e "  ${DIM}Quaid hooks into conversation events (compaction, reset) to extract memories.${RESET}"
    echo -e "  ${DIM}This requires specific gateway features from PR #13287.${RESET}"
    echo ""
    if ! check_gateway_hooks; then
        echo ""
        error "Gateway check failed. Setup cannot continue without memory hooks."
        echo "  Fix the issue above and re-run: bash setup-quaid.sh"
        exit 1
    fi

    # Check if plugin source exists (release tarball or existing install)
    if [[ -d "${SCRIPT_DIR}/plugins/quaid" ]]; then
        info "Plugin source found in release package"
    elif [[ -d "${SCRIPT_DIR}/quaid" ]]; then
        info "Plugin source found in release package (flat layout)"
    elif [[ -d "${PLUGIN_DIR}" ]] && [[ -n "$(ls -A "${PLUGIN_DIR}" 2>/dev/null)" ]]; then
        info "Plugin source found at existing install"
    else
        error "Plugin source not found."
        error "Expected at ${SCRIPT_DIR}/plugins/quaid/ or ${PLUGIN_DIR}/"
        exit 1
    fi

    echo ""
    info "Pre-flight checks passed."

    # --- Backup warning (only shown after all prereqs pass) ---
    echo ""
    # Only offer backup if there are existing files to back up
    local has_existing=false
    for f in SOUL.md USER.md MEMORY.md TOOLS.md AGENTS.md; do
        [[ -f "${WORKSPACE_ROOT}/${f}" ]] && has_existing=true && break
    done
    [[ -f "${CONFIG_DIR}/memory.json" ]] && has_existing=true
    [[ -f "${DATA_DIR}/memory.db" ]] && has_existing=true

    if $has_existing; then
        echo ""
        echo -e "  ${YELLOW}Note:${RESET} Quaid's nightly janitor will modify your workspace markdown"
        echo "  files (SOUL.md, USER.md, MEMORY.md, etc.) to keep them slim and"
        echo "  up-to-date. It is highly recommended to back up your workspace first."
        echo ""

        if confirm "Create a backup now?"; then
            local backup_dir="${WORKSPACE_ROOT}/.quaid-backup-$(date +%Y%m%d-%H%M%S)"
            mkdir -p "$backup_dir"
            local backed=0
            for f in SOUL.md USER.md MEMORY.md TOOLS.md AGENTS.md IDENTITY.md HEARTBEAT.md TODO.md; do
                if [[ -f "${WORKSPACE_ROOT}/${f}" ]]; then
                    cp "${WORKSPACE_ROOT}/${f}" "$backup_dir/"
                    backed=$((backed + 1))
                fi
            done
            [[ -f "${CONFIG_DIR}/memory.json" ]] && cp "${CONFIG_DIR}/memory.json" "$backup_dir/" && backed=$((backed + 1))
            [[ -f "${DATA_DIR}/memory.db" ]] && cp "${DATA_DIR}/memory.db" "$backup_dir/" && backed=$((backed + 1))
            info "Backed up ${backed} files to ${backup_dir}/"
        fi
    fi
}

# =============================================================================
# Step 2: Owner & Identity
# =============================================================================
step2_owner() {
    step 2 "Detecting owner"

    echo "  Quaid stores memories per-owner. Detecting your identity..."
    echo ""

    # Try git config (most reliable — most devs have this set)
    local git_name=""
    if command -v git &>/dev/null; then
        git_name=$(git config user.name 2>/dev/null || true)
    fi

    if [[ -n "$git_name" ]]; then
        OWNER_DISPLAY="$git_name"
        info "Detected from git: ${OWNER_DISPLAY}"
    elif [[ -n "${USER:-}" ]]; then
        # Fall back to OS username
        OWNER_DISPLAY="$USER"
        info "Using system user: ${OWNER_DISPLAY}"
    else
        ask "Could not auto-detect. What's your name?"
        OWNER_DISPLAY="${REPLY:-User}"
    fi

    OWNER_NAME=$(echo "$OWNER_DISPLAY" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
    info "Owner ID: ${OWNER_NAME}"

    if ! confirm "Use '${OWNER_DISPLAY}' as your owner name?"; then
        ask "Enter your preferred name:"
        OWNER_DISPLAY="${REPLY:-$OWNER_DISPLAY}"
        OWNER_NAME=$(echo "$OWNER_DISPLAY" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
        info "Owner ID: ${OWNER_NAME}"
    fi
}

# =============================================================================
# Step 3: Model Selection
# =============================================================================
step3_models() {
    step 3 "Choose your models"

    echo "  Quaid uses two LLM models:"
    echo "    - High reasoning: fact extraction, review, journal distillation"
    echo "    - Low reasoning:  reranking, pre-filters, intent classification"
    echo ""

    # Lookup functions (bash 3.2 compatible — no associative arrays)
    _low_model_for() {
        case "$1" in
            claude-opus-4-6|claude-sonnet-4-5) echo "claude-haiku-4-5" ;;
            gpt-4o)        echo "gpt-4o-mini" ;;
            gpt-5.2)       echo "gpt-5-mini" ;;
            gemini-2.5-pro) echo "gemini-2.0-flash" ;;
            gemini-3-pro)  echo "gemini-3-flash" ;;
            *)             echo "$1" ;;  # fallback: same model for both
        esac
    }

    _key_env_for() {
        case "$1" in
            anthropic)   echo "ANTHROPIC_API_KEY" ;;
            openai)      echo "OPENAI_API_KEY" ;;
            openrouter)  echo "OPENROUTER_API_KEY" ;;
            together)    echo "TOGETHER_API_KEY" ;;
            ollama)      echo "" ;;
            *)           echo "ANTHROPIC_API_KEY" ;;
        esac
    }

    _base_url_for() {
        case "$1" in
            openrouter)  echo "https://openrouter.ai/api/v1" ;;
            together)    echo "https://api.together.xyz/v1" ;;
            ollama)      echo "http://localhost:11434/v1" ;;
            *)           echo "" ;;  # anthropic/openai use SDK defaults
        esac
    }

    _api_format_for() {
        case "$1" in
            anthropic) echo "anthropic" ;;
            *)         echo "openai" ;;
        esac
    }

    echo "  Choose your LLM provider:"
    echo -e "    1) Anthropic (Claude)  ${GREEN}— Recommended${RESET}"
    echo -e "    2) OpenAI              ${DIM}— Experimental${RESET}"
    echo -e "    3) OpenRouter          ${DIM}— Experimental (multi-provider gateway)${RESET}"
    echo -e "    4) Together AI         ${DIM}— Experimental${RESET}"
    echo -e "    5) Ollama (local)      ${DIM}— Experimental (quality depends on model size)${RESET}"
    echo ""
    ask "Provider [1]:"

    local provider_choice="${REPLY:-1}"
    local provider_key=""
    case "$provider_choice" in
        1) provider_key="anthropic" ;;
        2) provider_key="openai" ;;
        3) provider_key="openrouter" ;;
        4) provider_key="together" ;;
        5) provider_key="ollama" ;;
        *) provider_key="anthropic" ;;
    esac

    PROVIDER="$(_api_format_for "$provider_key")"
    API_KEY_ENV="$(_key_env_for "$provider_key")"
    BASE_URL="$(_base_url_for "$provider_key")"

    if [[ "$provider_key" != "anthropic" ]]; then
        echo ""
        warn "Non-Anthropic providers are experimental in this alpha release."
        warn "Prompts are tuned for Claude — other models may produce lower quality results."
    fi

    # Model selection
    if [[ "$provider_key" == "anthropic" ]]; then
        HIGH_MODEL="claude-opus-4-6"
        LOW_MODEL="claude-haiku-4-5"
        info "Using Claude Opus 4.6 (high) + Claude Haiku 4.5 (low)"
    elif [[ "$provider_key" == "ollama" ]]; then
        echo ""
        ask "High reasoning model name (e.g. llama3.1:70b, qwen2.5:72b):"
        HIGH_MODEL="${REPLY:-llama3.1:70b}"
        LOW_MODEL="$(_low_model_for "$HIGH_MODEL")"
        ask "Low reasoning model name [${LOW_MODEL}]:"
        LOW_MODEL="${REPLY:-$LOW_MODEL}"
    else
        echo ""
        ask "High reasoning model name (e.g. gpt-4o, claude-opus-4-6):"
        HIGH_MODEL="${REPLY:-gpt-4o}"
        LOW_MODEL="$(_low_model_for "$HIGH_MODEL")"
        ask "Low reasoning model name [${LOW_MODEL}]:"
        LOW_MODEL="${REPLY:-$LOW_MODEL}"
    fi

    if [[ "$HIGH_MODEL" == "$LOW_MODEL" && "$provider_key" != "ollama" ]]; then
        warn "Using the same model for both high and low reasoning will increase costs."
    fi

    # API key check
    if [[ -n "$API_KEY_ENV" ]]; then
        echo ""
        if [[ -n "${!API_KEY_ENV:-}" ]]; then
            info "API key found in environment (${API_KEY_ENV})"
        else
            warn "API key not found in environment."
            echo "  Set it with: export ${API_KEY_ENV}=your-key-here"
            echo "  Or add it to ${WORKSPACE_ROOT}/.env"
            if ! confirm "Continue without API key? (you can set it later)"; then
                exit 0
            fi
        fi
    fi

    echo ""
    info "Models: ${HIGH_MODEL} (high) + ${LOW_MODEL} (low) via ${provider_key}"

    # --- Notification verbosity ---
    echo ""
    echo -e "  ${BOLD}Notification verbosity${RESET}"
    echo -e "  ${DIM}Notifications are sent on whatever channel you use to talk to your bot.${RESET}"
    echo ""
    echo "    1) Quiet   — Errors only"
    echo -e "    2) Normal  — Extraction + janitor summaries ${GREEN}(default)${RESET}"
    echo "    3) Verbose — Also shows memory recall"
    echo "    4) Debug   — Full details on everything"
    echo ""
    ask "Verbosity [2]:"
    case "${REPLY:-2}" in
        1) NOTIFICATION_LEVEL="quiet";   info "Notifications: quiet" ;;
        3) NOTIFICATION_LEVEL="verbose"; info "Notifications: verbose" ;;
        4) NOTIFICATION_LEVEL="debug";   info "Notifications: debug" ;;
        *) NOTIFICATION_LEVEL="normal";  info "Notifications: normal" ;;
    esac
}

# =============================================================================
# Step 4: Embedding Model
# =============================================================================
step4_embeddings() {
    step 4 "Embedding model"

    echo "  Embeddings power semantic search — turning text into vectors"
    echo "  so Quaid can find relevant memories by meaning, not just keywords."
    echo ""

    # Check if Ollama is installed and running
    local ollama_running=false
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        ollama_running=true
        info "Ollama is running"
    elif command -v ollama &>/dev/null; then
        # Installed but not running — try to start it
        warn "Ollama is installed but not running."
        if confirm "Start Ollama now?"; then
            info "Starting Ollama..."
            ollama serve &>/dev/null &
            sleep 2
            if curl -sf http://localhost:11434/api/tags &>/dev/null; then
                ollama_running=true
                info "Ollama started"
            else
                warn "Ollama didn't start in time. Trying 'brew services start ollama'..."
                brew services start ollama 2>/dev/null || true
                sleep 3
                if curl -sf http://localhost:11434/api/tags &>/dev/null; then
                    ollama_running=true
                    info "Ollama started via Homebrew services"
                else
                    warn "Could not start Ollama. You can start it manually later: ollama serve"
                fi
            fi
        fi
    else
        # Not installed at all
        warn "Ollama not found."
        echo ""
        echo "  Ollama runs embedding models locally — free, fast, and private."
        echo "  Without it, Quaid falls back to cloud embeddings (OpenAI API)."
        echo ""
        if confirm "Install Ollama now?"; then
            if command -v brew &>/dev/null; then
                info "Installing Ollama via Homebrew..."
                if brew install ollama 2>&1 | tail -3; then
                    info "Ollama installed"
                    info "Starting Ollama..."
                    brew services start ollama 2>/dev/null || ollama serve &>/dev/null &
                    sleep 3
                    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
                        ollama_running=true
                        info "Ollama is running"
                    else
                        warn "Ollama installed but not yet responding. It may need a moment."
                    fi
                else
                    warn "Homebrew install failed. Trying direct install..."
                fi
            fi

            if ! $ollama_running && ! command -v ollama &>/dev/null; then
                info "Installing Ollama via official installer..."
                curl -fsSL https://ollama.ai/install.sh | sh
                sleep 2
                if command -v ollama &>/dev/null; then
                    info "Ollama installed"
                    ollama serve &>/dev/null &
                    sleep 3
                    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
                        ollama_running=true
                        info "Ollama is running"
                    fi
                else
                    warn "Ollama install may have succeeded but 'ollama' is not on PATH."
                    echo "  Close and reopen your terminal, then re-run this installer."
                fi
            fi
        fi
    fi

    if $ollama_running; then
        # Check available system RAM to guide model selection
        local total_ram_gb=0
        local free_ram_gb=0
        if [[ "$(uname)" == "Darwin" ]]; then
            total_ram_gb=$(sysctl -n hw.memsize 2>/dev/null | awk '{printf "%.0f", $1/1024/1024/1024}')
            # On macOS, estimate free RAM from vm_stat
            local page_size
            page_size=$(vm_stat 2>/dev/null | head -1 | grep -oE '[0-9]+' || echo 16384)
            local free_pages
            free_pages=$(vm_stat 2>/dev/null | awk '/Pages free/ {gsub(/\./,"",$3); print $3}')
            local inactive_pages
            inactive_pages=$(vm_stat 2>/dev/null | awk '/Pages inactive/ {gsub(/\./,"",$3); print $3}')
            free_ram_gb=$(echo "${free_pages:-0} ${inactive_pages:-0} ${page_size}" | awk '{printf "%.0f", ($1+$2)*$3/1024/1024/1024}')
        else
            total_ram_gb=$(free -g 2>/dev/null | awk '/^Mem:/ {print $2}')
            free_ram_gb=$(free -g 2>/dev/null | awk '/^Mem:/ {print $7}')
        fi
        total_ram_gb="${total_ram_gb:-0}"
        free_ram_gb="${free_ram_gb:-0}"

        echo ""
        if [[ "$total_ram_gb" -gt 0 ]]; then
            info "System RAM: ${total_ram_gb}GB total, ~${free_ram_gb}GB available"
        fi
        echo ""
        echo "  Embedding models run persistently in RAM while Ollama is active."
        echo "  Choose a model that fits your available memory:"
        echo ""

        # Determine which models can fit (need ~2GB headroom beyond model size)
        local can_8b=false can_nomic=false can_mini=false
        local default_choice=3  # fallback to smallest

        if [[ "$free_ram_gb" -ge 8 ]] || [[ "$total_ram_gb" -ge 24 ]]; then
            can_8b=true; default_choice=1
        fi
        if [[ "$free_ram_gb" -ge 4 ]] || [[ "$total_ram_gb" -ge 12 ]]; then
            can_nomic=true
            [[ "$default_choice" -gt 2 ]] && default_choice=2
        fi
        if [[ "$free_ram_gb" -ge 2 ]] || [[ "$total_ram_gb" -ge 8 ]]; then
            can_mini=true
        fi

        if $can_8b; then
            echo -e "    1) qwen3-embedding:8b  (4096 dim, ${YELLOW}~6GB RAM${RESET})  ${GREEN}— Best quality${RESET}"
        else
            echo -e "    ${DIM}1) qwen3-embedding:8b  (4096 dim, ~6GB RAM)  — Not enough RAM${RESET}"
        fi
        if $can_nomic; then
            echo -e "    2) nomic-embed-text    (768 dim,  ${YELLOW}~1.5GB RAM${RESET})"
        else
            echo -e "    ${DIM}2) nomic-embed-text    (768 dim,  ~1.5GB RAM) — Not enough RAM${RESET}"
        fi
        if $can_mini; then
            echo -e "    3) all-minilm          (384 dim,  ${YELLOW}~500MB RAM${RESET})  — Minimal"
        else
            echo -e "    ${DIM}3) all-minilm          (384 dim,  ~500MB RAM) — Not enough RAM${RESET}"
        fi
        echo ""
        ask "Embedding model [${default_choice}]:"

        local choice="${REPLY:-$default_choice}"
        case "$choice" in
            1)
                if $can_8b; then
                    EMBED_MODEL="qwen3-embedding:8b"
                    EMBED_DIM=4096
                else
                    warn "Not enough RAM for qwen3-embedding:8b (~6GB). Falling back to nomic-embed-text."
                    if $can_nomic; then
                        EMBED_MODEL="nomic-embed-text"; EMBED_DIM=768
                    else
                        EMBED_MODEL="all-minilm"; EMBED_DIM=384
                    fi
                fi
                ;;
            2)
                if $can_nomic; then
                    EMBED_MODEL="nomic-embed-text"
                    EMBED_DIM=768
                else
                    warn "Not enough RAM for nomic-embed-text (~1.5GB). Using all-minilm."
                    EMBED_MODEL="all-minilm"; EMBED_DIM=384
                fi
                ;;
            *)  EMBED_MODEL="all-minilm"
                EMBED_DIM=384
                ;;
        esac

        # Check if model is already pulled, if not pull it
        if curl -sf http://localhost:11434/api/tags | python3 -c "
import sys, json
tags = json.load(sys.stdin).get('models', [])
names = [m.get('name','') for m in tags]
model = '${EMBED_MODEL}'
exit(0 if any(model in n for n in names) else 1)
" 2>/dev/null; then
            info "${EMBED_MODEL} already available"
        else
            echo ""
            info "Downloading ${EMBED_MODEL}... (this may take a few minutes)"
            if ollama pull "$EMBED_MODEL"; then
                info "${EMBED_MODEL} ready"
            else
                warn "Download failed. Run 'ollama pull ${EMBED_MODEL}' manually before using memory."
            fi
        fi
    else
        echo ""
        echo "  Without Ollama, Quaid will use OpenAI's API for embeddings."
        echo "  This costs ~\$0.02 per million tokens but requires an OpenAI API key."
        echo ""
        EMBED_MODEL="text-embedding-3-small"
        EMBED_DIM=1536
        info "Using OpenAI text-embedding-3-small (cloud, 1536 dim)"
    fi

    echo ""
    warn "Changing embedding models later requires re-embedding all stored facts."
    info "Embedding model: ${EMBED_MODEL} (dim=${EMBED_DIM})"
}

# =============================================================================
# Step 5: Systems Configuration
# =============================================================================
step5_systems() {
    step 5 "System configuration"

    echo "  Quaid has 4 subsystems. All are enabled by default."
    echo "  Disabling a system prevents it from running entirely."
    echo ""
    echo -e "    ${GREEN}✓${RESET} Memory     — Extract and recall facts from conversations"
    echo -e "    ${GREEN}✓${RESET} Journal    — Track and understand personality evolution"
    echo -e "    ${GREEN}✓${RESET} Projects   — Auto-update project docs from document changes"
    echo -e "    ${GREEN}✓${RESET} Workspace  — Monitor core markdown file health"
    echo ""
    echo -e "  ${DIM}Disable systems if they conflict with other plugins or you don't need them.${RESET}"
    echo -e "  ${DIM}Learn more: https://github.com/openclaw/quaid${RESET}"
    echo ""

    if confirm "Keep all systems enabled?" "y"; then
        info "All systems enabled"
        return
    fi

    echo ""
    echo "  Enter numbers to disable (e.g. '3 4' to disable Projects and Workspace):"
    echo ""
    echo "    1) Memory      2) Journal      3) Projects      4) Workspace"
    echo ""
    ask "Disable:"

    for num in $REPLY; do
        case "$num" in
            1) SYS_MEMORY=false;    warn "Memory disabled — no fact extraction or recall" ;;
            2) SYS_JOURNAL=false;   warn "Journal disabled — no personality tracking" ;;
            3) SYS_PROJECTS=false;  warn "Projects disabled — no doc auto-updates" ;;
            4) SYS_WORKSPACE=false; warn "Workspace disabled — no markdown monitoring" ;;
        esac
    done

    echo ""
    local enabled=""
    $SYS_MEMORY    && enabled="${enabled}Memory, "
    $SYS_JOURNAL   && enabled="${enabled}Journal, "
    $SYS_PROJECTS  && enabled="${enabled}Projects, "
    $SYS_WORKSPACE && enabled="${enabled}Workspace, "
    enabled="${enabled%, }"
    info "Enabled: ${enabled:-none}"
}

# =============================================================================
# Step 6: Install & Migrate
# =============================================================================
step6_install() {
    step 6 "Installing..."

    # Create directories
    info "Creating directories..."
    mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$JOURNAL_DIR" "$LOGS_DIR" "$PROJECTS_DIR"
    mkdir -p "${JOURNAL_DIR}/archive"

    # Copy plugin source if not already in place
    if [[ ! -d "${PLUGIN_DIR}" ]] || [[ -z "$(ls -A "${PLUGIN_DIR}" 2>/dev/null)" ]]; then
        info "Installing plugin source..."
        mkdir -p "${PLUGIN_DIR}"
        if [[ -d "${SCRIPT_DIR}/plugins/quaid" ]]; then
            cp -R "${SCRIPT_DIR}/plugins/quaid/"* "${PLUGIN_DIR}/"
        elif [[ -d "${SCRIPT_DIR}/quaid" ]]; then
            cp -R "${SCRIPT_DIR}/quaid/"* "${PLUGIN_DIR}/"
        else
            error "Plugin source not found. Expected at ${SCRIPT_DIR}/plugins/quaid/ or ${SCRIPT_DIR}/quaid/"
            exit 1
        fi
    else
        info "Plugin source already in place"
    fi

    # Initialize database
    info "Initializing database..."
    if [[ -f "${DATA_DIR}/memory.db" ]]; then
        warn "Database already exists at ${DATA_DIR}/memory.db"
        if confirm "Reinitialize? (existing data will be preserved, only missing tables added)"; then
            python3 -c "
import sqlite3
conn = sqlite3.connect('${DATA_DIR}/memory.db')
with open('${PLUGIN_DIR}/schema.sql') as f:
    conn.executescript(f.read())
conn.close()
print('[+] Database updated')
"
        fi
    else
        python3 -c "
import sqlite3
conn = sqlite3.connect('${DATA_DIR}/memory.db')
with open('${PLUGIN_DIR}/schema.sql') as f:
    conn.executescript(f.read())
conn.close()
print('[+] Database initialized')
"
    fi
    chmod 600 "${DATA_DIR}/memory.db"

    # Write config/memory.json
    info "Writing configuration..."
    _write_config

    # Create minimal workspace files if they don't exist
    for f in SOUL.md USER.md MEMORY.md; do
        if [[ ! -f "${WORKSPACE_ROOT}/${f}" ]]; then
            echo "# ${f%.md}" > "${WORKSPACE_ROOT}/${f}"
            info "Created ${f}"
        fi
    done

    # Create journal files if journal enabled
    if $SYS_JOURNAL; then
        for f in SOUL USER MEMORY; do
            local jf="${JOURNAL_DIR}/${f}.journal.md"
            if [[ ! -f "$jf" ]]; then
                echo "# ${f} Journal" > "$jf"
                info "Created ${jf}"
            fi
        done
    fi

    # Initialize git repo for workspace (required for doc staleness tracking)
    if [[ ! -d "${WORKSPACE_ROOT}/.git" ]]; then
        info "Initializing git repository..."
        git -C "$WORKSPACE_ROOT" init --quiet
        # Create .gitignore for runtime artifacts
        if [[ ! -f "${WORKSPACE_ROOT}/.gitignore" ]]; then
            cat > "${WORKSPACE_ROOT}/.gitignore" << 'GITIGNORE'
# Runtime data
data/*.db
data/*.db-*
logs/
.env
.env.*

# Python
__pycache__/
*.pyc
.pytest_cache/

# OS
.DS_Store
Thumbs.db

# Build
node_modules/
build/
GITIGNORE
        fi
        # Initial commit so git diff/log have a baseline
        git -C "$WORKSPACE_ROOT" add -A
        git -C "$WORKSPACE_ROOT" commit --quiet -m "Initial Quaid workspace"
        success "Git repository initialized"
    else
        info "Git repository already exists"
    fi

    # Create owner's Person node
    info "Creating owner node in memory graph..."
    (
        cd "$PLUGIN_DIR"
        python3 -c "
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE_ROOT}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from memory_graph import store
try:
    store('${OWNER_DISPLAY}', owner_id='${OWNER_NAME}', category='person', source='installer')
    print('[+] Person node created for ${OWNER_DISPLAY}')
except Exception as e:
    print(f'[!] Could not create Person node: {e}', file=sys.stderr)
" 2>&1
    ) || true

    # Migration check
    echo ""
    _check_migration

    # Install Quaid project reference docs (TOOLS.md, AGENTS.md, onboarding)
    if $SYS_PROJECTS; then
        local quaid_proj_dir="${PROJECTS_DIR}/quaid"
        mkdir -p "$quaid_proj_dir"
        local quaid_proj_src="${SCRIPT_DIR}/projects/quaid"
        for f in TOOLS.md AGENTS.md project_onboarding.md; do
            if [[ -f "${quaid_proj_src}/${f}" ]] && [[ ! -f "${quaid_proj_dir}/${f}" ]]; then
                cp "${quaid_proj_src}/${f}" "${quaid_proj_dir}/${f}"
            fi
        done
        if [[ ! -f "${quaid_proj_dir}/PROJECT.md" ]]; then
            cat > "${quaid_proj_dir}/PROJECT.md" << 'PROJEOF'
# Quaid Memory System

Persistent long-term memory plugin. Stores facts, relationships, and preferences
in a local SQLite graph database. Retrieved automatically via hybrid search.

## Key Files
- `TOOLS.md` — CLI commands and agent tools reference
- `AGENTS.md` — Instructions for how the agent should use memory
- `project_onboarding.md` — Guide for discovering and registering projects

## Systems
- **Memory** — Fact extraction, graph storage, hybrid recall
- **Journal** — Slow-path learning, personality evolution
- **Projects** — Documentation tracking, staleness detection, RAG search
- **Workspace** — Core markdown monitoring, nightly maintenance
PROJEOF
        fi
        # Register Quaid project in the docs registry
        (
            cd "$PLUGIN_DIR"
            python3 -c "
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE_ROOT}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from docs_registry import DocsRegistry
reg = DocsRegistry()
try:
    reg.create_project('quaid', label='Quaid Memory System', description='Memory plugin reference docs and agent instructions.')
except ValueError:
    pass
found = reg.auto_discover('quaid')
print(f'[+] Quaid project registered ({len(found)} docs)')
" 2>&1
        ) || true
    fi

    info "Installation complete!"
}

# Write the config file from collected values
_write_config() {
    local base_url_json="null"
    if [[ -n "$BASE_URL" ]]; then
        base_url_json="\"${BASE_URL}\""
    fi

    cat > "${CONFIG_DIR}/memory.json" <<JSONEOF
{
  "systems": {
    "memory": ${SYS_MEMORY},
    "journal": ${SYS_JOURNAL},
    "projects": ${SYS_PROJECTS},
    "workspace": ${SYS_WORKSPACE}
  },
  "models": {
    "provider": "${PROVIDER}",
    "apiKeyEnv": "${API_KEY_ENV}",
    "baseUrl": ${base_url_json},
    "lowReasoning": "${LOW_MODEL}",
    "highReasoning": "${HIGH_MODEL}",
    "lowReasoningContext": 200000,
    "highReasoningContext": 200000,
    "lowReasoningMaxOutput": 8192,
    "highReasoningMaxOutput": 16384,
    "batchBudgetPercent": 0.5
  },
  "capture": {
    "enabled": true,
    "strictness": "high",
    "skipPatterns": ["^(thanks|ok|sure|yes|no)$", "^(hi|hello|hey)\\\\b"]
  },
  "decay": {
    "enabled": true,
    "thresholdDays": 30,
    "ratePercent": 10,
    "minimumConfidence": 0.1,
    "protectVerified": true,
    "protectPinned": true,
    "reviewQueueEnabled": true,
    "mode": "exponential",
    "baseHalfLifeDays": 60,
    "accessBonusFactor": 0.15
  },
  "janitor": {
    "enabled": true,
    "dryRun": false,
    "taskTimeoutMinutes": 60,
    "opusReview": {
      "enabled": true,
      "batchSize": 50,
      "maxTokens": 4000
    },
    "dedup": {
      "similarityThreshold": 0.85,
      "highSimilarityThreshold": 0.95,
      "autoRejectThreshold": 0.98,
      "grayZoneLow": 0.88,
      "haikuVerifyEnabled": true
    },
    "contradiction": {
      "enabled": true,
      "timeoutMinutes": 60,
      "minSimilarity": 0.6,
      "maxSimilarity": 0.85
    }
  },
  "retrieval": {
    "defaultLimit": 5,
    "maxLimit": 8,
    "minSimilarity": 0.6,
    "notifyMinSimilarity": 0.85,
    "boostRecent": true,
    "boostFrequent": true,
    "maxTokens": 2000,
    "notifyOnRecall": true,
    "reranker": {
      "enabled": true,
      "provider": "anthropic",
      "topK": 20
    },
    "rrfK": 60,
    "rerankerBlend": 0.5,
    "compositeRelevanceWeight": 0.60,
    "compositeRecencyWeight": 0.20,
    "compositeFrequencyWeight": 0.15,
    "multiPassGate": 0.70,
    "mmrLambda": 0.7,
    "coSessionDecay": 0.6,
    "recencyDecayDays": 90,
    "useHyde": true,
    "traversal": {
      "useBeam": true,
      "beamWidth": 5,
      "maxDepth": 2,
      "hopDecay": 0.7
    }
  },
  "logging": {
    "enabled": true,
    "level": "info",
    "retentionDays": 30,
    "components": ["memory", "janitor"]
  },
  "notifications": {
    "level": "${NOTIFICATION_LEVEL}",
    "janitor": { "verbosity": null, "channel": "last_used" },
    "extraction": { "verbosity": null, "channel": "last_used" },
    "retrieval": { "verbosity": null, "channel": "last_used" },
    "fullText": true,
    "showProcessingStart": true
  },
  "docs": {
    "autoUpdateOnCompact": true,
    "maxDocsPerUpdate": 3,
    "stalenessCheckEnabled": true,
    "updateTimeoutSeconds": 120,
    "coreMarkdown": {
      "enabled": true,
      "monitorForBloat": true,
      "monitorForOutdated": true,
      "files": {
        "SOUL.md": { "purpose": "Personality and interaction style", "maxLines": 80 },
        "USER.md": { "purpose": "About the user", "maxLines": 150 },
        "MEMORY.md": { "purpose": "Core memories loaded every session", "maxLines": 100 }
      }
    },
    "journal": {
      "enabled": true,
      "snippetsEnabled": true,
      "mode": "distilled",
      "journalDir": "journal",
      "targetFiles": ["SOUL.md", "USER.md", "MEMORY.md"],
      "maxEntriesPerFile": 50,
      "maxTokens": 8192,
      "distillationIntervalDays": 7,
      "archiveAfterDistillation": true
    },
    "sourceMapping": {},
    "docPurposes": {}
  },
  "projects": {
    "enabled": true,
    "projectsDir": "projects/",
    "stagingDir": "projects/staging/",
    "definitions": {},
    "defaultProject": "quaid"
  },
  "users": {
    "defaultOwner": "${OWNER_NAME}",
    "identities": {
      "${OWNER_NAME}": {
        "channels": { "cli": ["*"] },
        "speakers": ["${OWNER_DISPLAY}", "${OWNER_NAME}", "The user"],
        "personNodeName": "${OWNER_DISPLAY}"
      }
    }
  },
  "database": {
    "path": "data/memory.db",
    "archivePath": "data/memory_archive.db",
    "walMode": true
  },
  "ollama": {
    "url": "http://localhost:11434",
    "embeddingModel": "${EMBED_MODEL}",
    "embeddingDim": ${EMBED_DIM}
  },
  "rag": {
    "docsDir": "docs",
    "chunkMaxTokens": 800,
    "chunkOverlapTokens": 100,
    "maxResults": 5,
    "searchLimit": 5,
    "minSimilarity": 0.3
  }
}
JSONEOF

    info "Config written to ${CONFIG_DIR}/memory.json"
}

# Check for existing markdown files and offer migration
_check_migration() {
    local files_found=()
    local total_lines=0

    for f in SOUL.md USER.md TOOLS.md MEMORY.md AGENTS.md; do
        local fpath="${WORKSPACE_ROOT}/${f}"
        if [[ -f "$fpath" ]]; then
            local lines
            lines=$(wc -l < "$fpath" | tr -d ' ')
            if [[ "$lines" -gt 5 ]]; then
                files_found+=("$f ($lines lines)")
                total_lines=$((total_lines + lines))
            fi
        fi
    done

    if [[ ${#files_found[@]} -eq 0 ]]; then
        return
    fi

    echo ""
    info "Found existing workspace files with content:"
    for desc in "${files_found[@]}"; do
        echo "    ${desc}"
    done
    echo ""
    echo "  Would you like to import facts from these files into memory?"
    echo "  This uses your configured model and may take 2-5 minutes."
    echo -e "  Estimated cost: ${YELLOW}~\$0.15-0.50${RESET} depending on content."
    echo ""

    if confirm "Import facts from existing files?"; then
        info "Starting migration... (this may take a few minutes)"
        (
            cd "$PLUGIN_DIR"
            python3 -c "
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE_ROOT}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')

from memory_graph import store
from llm_clients import call_high_reasoning, parse_json_response

files = [f for f in ['SOUL.md', 'USER.md', 'TOOLS.md', 'MEMORY.md', 'AGENTS.md']
         if os.path.exists(os.path.join('${WORKSPACE_ROOT}', f))]

total_facts = 0
for fname in files:
    fpath = os.path.join('${WORKSPACE_ROOT}', fname)
    with open(fpath) as f:
        content = f.read().strip()
    if len(content) < 50:
        continue

    print(f'[+] Extracting from {fname}...')
    prompt = f'''Extract factual information from this document. Return a JSON array of objects:
[{{\"fact\": \"...\", \"category\": \"fact|preference|belief|experience\"}}]

Only extract clear, specific facts about the user or their preferences.
Skip meta-information, instructions, and formatting.

Document ({fname}):
{content}'''

    response, _ = call_high_reasoning(prompt, max_tokens=4000)
    if response:
        parsed = parse_json_response(response)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and 'fact' in item:
                    store(item['fact'], owner_id='${OWNER_NAME}',
                          category=item.get('category', 'fact'),
                          source='migration')
                    total_facts += 1

print(f'[+] Migration complete: {total_facts} facts extracted and stored as pending.')
print(f'    Next janitor run will review and activate them.')
" 2>&1
        ) || warn "Migration had errors. You can retry later with 'quaid migrate'."
    else
        info "Skipped. Run 'quaid migrate' anytime to import later."
    fi
}

# =============================================================================
# Step 7: Validation & Next Steps
# =============================================================================
step7_validate() {
    step 7 "All done!"

    info "Running health checks..."
    echo ""

    local all_ok=true

    # Check database
    if [[ -f "${DATA_DIR}/memory.db" ]]; then
        local table_count
        table_count=$(python3 -c "
import sqlite3
conn = sqlite3.connect('${DATA_DIR}/memory.db')
tables = conn.execute(\"SELECT COUNT(*) FROM sqlite_master WHERE type='table'\").fetchone()[0]
print(tables)
conn.close()
")
        echo -e "  ${GREEN}✓${RESET} Database     — OK (${DATA_DIR}/memory.db, ${table_count} tables)"
    else
        echo -e "  ${RED}✗${RESET} Database     — MISSING"
        all_ok=false
    fi

    # Check embeddings
    if curl -sf http://localhost:11434/api/tags &>/dev/null; then
        echo -e "  ${GREEN}✓${RESET} Embeddings   — OK (${EMBED_MODEL}, dim=${EMBED_DIM})"
    else
        if [[ "$EMBED_MODEL" == "text-embedding-3-small" ]]; then
            echo -e "  ${YELLOW}~${RESET} Embeddings   — Cloud (${EMBED_MODEL}, requires API key)"
        else
            echo -e "  ${RED}✗${RESET} Embeddings   — Ollama not running"
            all_ok=false
        fi
    fi

    # Check LLM
    echo -e "  ${GREEN}✓${RESET} LLM (main)   — ${HIGH_MODEL} via ${PROVIDER}"
    echo -e "  ${GREEN}✓${RESET} LLM (fast)   — ${LOW_MODEL} via ${PROVIDER}"

    # Check config
    if [[ -f "${CONFIG_DIR}/memory.json" ]]; then
        echo -e "  ${GREEN}✓${RESET} Config       — OK (${CONFIG_DIR}/memory.json)"
    else
        echo -e "  ${RED}✗${RESET} Config       — MISSING"
        all_ok=false
    fi

    # Check owner
    echo -e "  ${GREEN}✓${RESET} Owner        — ${OWNER_DISPLAY} (${OWNER_NAME})"

    # Check systems
    local sys_list=""
    $SYS_MEMORY    && sys_list="${sys_list}Memory, "
    $SYS_JOURNAL   && sys_list="${sys_list}Journal, "
    $SYS_PROJECTS  && sys_list="${sys_list}Projects, "
    $SYS_WORKSPACE && sys_list="${sys_list}Workspace, "
    sys_list="${sys_list%, }"
    echo -e "  ${GREEN}✓${RESET} Systems      — ${sys_list}"

    # Smoke test: store and recall a fact
    echo ""
    info "Running smoke test (store + recall)..."
    local smoke_ok=false
    (
        cd "$PLUGIN_DIR"
        python3 -c "
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE_ROOT}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from memory_graph import store, recall

# Store a test fact
node_id = store('Quaid installer smoke test fact', owner_id='${OWNER_NAME}', category='fact', source='installer-test')
print(f'[+] Stored test fact (node {node_id})')

# Try to recall it (FTS only, no embedding needed)
results = recall('installer smoke test', owner_id='${OWNER_NAME}', limit=1)
if results:
    print(f'[+] Recalled: {results[0][\"name\"][:60]}...')
    print('SMOKE_OK')
else:
    print('[!] Recall returned no results (embeddings may not be ready yet)')
    print('SMOKE_PARTIAL')
" 2>&1
    ) | while IFS= read -r line; do
        echo "  $line"
        if [[ "$line" == *"SMOKE_OK"* ]]; then
            smoke_ok=true
        fi
    done

    echo ""
    if $all_ok; then
        echo -e "  ${BOLD}${GREEN}Quaid is installed!${RESET} Here's what happens next:"
    else
        echo -e "  ${BOLD}${YELLOW}Quaid is installed with some warnings.${RESET} Check items above."
    fi

    echo ""
    echo "  Next steps:"
    echo "    • Have a conversation → /compact or /reset → facts are extracted"
    echo "    • Run 'quaid doctor' anytime to check health"
    echo "    • The nightly janitor reviews, deduplicates, and maintains memories"
    echo "    • Run 'quaid stats' to see your memory database grow"
    echo ""
    echo -e "  ${DIM}Docs: https://github.com/openclaw/quaid${RESET}"
    echo ""
    echo -e "  ${DIM}Get your ass to Mars.${RESET}"
    echo ""
}

# =============================================================================
# Main
# =============================================================================
main() {
    step1_preflight
    step2_owner
    step3_models
    step4_embeddings
    step5_systems
    step6_install
    step7_validate
}

main "$@"
