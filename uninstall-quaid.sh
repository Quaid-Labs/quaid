#!/usr/bin/env bash
# Quaid Memory Plugin — Uninstaller
# Removes Quaid plugin files and optionally restores from backup.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[quaid]${NC} $*"; }
ok()    { echo -e "${GREEN}[quaid]${NC} $*"; }
warn()  { echo -e "${YELLOW}[quaid]${NC} $*"; }
error() { echo -e "${RED}[quaid]${NC} $*" >&2; }

# --- Bootstrap PATH ---
if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

# --- Detect workspace ---
WORKSPACE="${CLAWDBOT_WORKSPACE:-}"
if [[ -z "$WORKSPACE" ]]; then
    for dir in "$HOME/.openclaw/workspace" "$HOME/clawd" "$HOME/.config/openclaw"; do
        if [[ -d "$dir" ]] && ( [[ -f "$dir/SOUL.md" ]] || [[ -d "$dir/plugins" ]] || [[ -f "$dir/IDENTITY.md" ]] ); then
            WORKSPACE="$dir"
            break
        fi
    done
fi
if [[ -z "$WORKSPACE" ]]; then
    error "Could not find OpenClaw workspace."
    error "Set CLAWDBOT_WORKSPACE and try again."
    exit 1
fi

PLUGIN_DIR="$WORKSPACE/plugins/quaid"
BACKUP_DIR="$HOME/.quaid-backups"

echo ""
echo -e "${BOLD}Quaid Uninstaller${NC}"
echo -e "Workspace: ${BLUE}$WORKSPACE${NC}"
echo ""

# --- Check if Quaid is installed ---
if [[ ! -d "$PLUGIN_DIR" ]]; then
    error "Quaid plugin not found at $PLUGIN_DIR"
    error "Nothing to uninstall."
    exit 1
fi

# --- List available backups ---
RESTORE_FROM=""
if [[ -d "$BACKUP_DIR" ]]; then
    BACKUPS=()
    while IFS= read -r d; do
        [[ -d "$d" ]] && BACKUPS+=("$d")
    done < <(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d | sort -r)

    # Also check for old-style workspace backups
    while IFS= read -r d; do
        [[ -d "$d" ]] && BACKUPS+=("$d")
    done < <(find "$WORKSPACE" -mindepth 1 -maxdepth 1 -type d -name ".quaid-backup-*" | sort -r)

    if [[ ${#BACKUPS[@]} -gt 0 ]]; then
        echo -e "${BOLD}Available backups:${NC}"
        for i in "${!BACKUPS[@]}"; do
            local_dir="${BACKUPS[$i]}"
            file_count=$(find "$local_dir" -type f | wc -l | tr -d ' ')
            dirname=$(basename "$local_dir")
            echo -e "  ${GREEN}$((i+1)))${NC} $dirname ($file_count files)"
        done
        echo ""
        echo -n "Restore from backup? Enter number (or 'n' to skip): "
        read -r choice
        if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 1 ]] && [[ "$choice" -le ${#BACKUPS[@]} ]]; then
            RESTORE_FROM="${BACKUPS[$((choice-1))]}"
            ok "Will restore from: $(basename "$RESTORE_FROM")"
        else
            info "Skipping backup restore."
        fi
    else
        info "No backups found."
    fi
else
    info "No backup directory found at $BACKUP_DIR"
fi

echo ""

# --- Confirm ---
echo -e "${YELLOW}This will:${NC}"
echo "  - Remove the Quaid plugin directory ($PLUGIN_DIR)"
echo "  - Remove Quaid config (config/memory.json)"
echo "  - Remove Quaid project docs (projects/quaid/)"
if [[ -n "$RESTORE_FROM" ]]; then
    echo -e "  - ${GREEN}Restore workspace files from backup${NC}"
fi
echo ""
echo -e "  ${RED}NOTE: memory.db will NOT be deleted (your memories are safe)${NC}"
echo -e "  To also delete your memory database: rm $WORKSPACE/data/memory.db"
echo ""
echo -n "Continue? [y/N] "
read -r confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    info "Cancelled."
    exit 0
fi

echo ""

# --- Restore from backup ---
if [[ -n "$RESTORE_FROM" ]]; then
    info "Restoring files from backup..."
    restored=0
    for f in "$RESTORE_FROM"/*; do
        [[ ! -f "$f" ]] && continue
        fname=$(basename "$f")
        case "$fname" in
            memory.json)
                cp "$f" "$WORKSPACE/config/memory.json" 2>/dev/null && ((restored++)) || true
                ;;
            memory.db)
                # Don't restore DB by default — it's not deleted
                ;;
            *)
                cp "$f" "$WORKSPACE/$fname" 2>/dev/null && ((restored++)) || true
                ;;
        esac
    done
    ok "Restored $restored files from backup."
fi

# --- Remove plugin ---
info "Removing plugin directory..."
rm -rf "$PLUGIN_DIR"
ok "Removed $PLUGIN_DIR"

# --- Remove config ---
if [[ -f "$WORKSPACE/config/memory.json" ]] && [[ -z "$RESTORE_FROM" ]]; then
    rm -f "$WORKSPACE/config/memory.json"
    ok "Removed config/memory.json"
fi

# --- Remove project docs ---
if [[ -d "$WORKSPACE/projects/quaid" ]]; then
    rm -rf "$WORKSPACE/projects/quaid"
    ok "Removed projects/quaid/"
fi

# --- Remove journal files (but not archive) ---
if [[ -d "$WORKSPACE/journal" ]]; then
    info "Removing snippet files..."
    find "$WORKSPACE/journal" -name "*.snippets.md" -delete 2>/dev/null || true
    ok "Removed snippet files (journal entries preserved)"
fi

# --- Remove janitor logs ---
if [[ -d "$WORKSPACE/logs" ]]; then
    rm -f "$WORKSPACE/logs/janitor"*.log 2>/dev/null || true
    ok "Cleaned janitor logs"
fi

echo ""
ok "Quaid has been uninstalled."
echo ""
info "Your memory database is still at: $WORKSPACE/data/memory.db"
info "Your backups are at: $BACKUP_DIR"
echo ""
echo -e "To reinstall later:"
echo -e "  ${BOLD}curl -fsSL https://raw.githubusercontent.com/steadman-labs/quaid/main/install.sh | bash${NC}"
echo ""
