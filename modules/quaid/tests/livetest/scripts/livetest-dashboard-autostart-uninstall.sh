#!/usr/bin/env bash
set -euo pipefail

LABEL="${LABEL:-ai.quaid.livetest.dashboard}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/${LABEL}.plist"
GUI_TARGET="gui/$(id -u)"

usage() {
  cat <<'EOF'
livetest-dashboard-autostart-uninstall.sh

Unload and remove the Quaid live-test dashboard LaunchAgent.

Usage:
  tests/livetest/scripts/livetest-dashboard-autostart-uninstall.sh [--label <label>]

Options:
  --label <label>  LaunchAgent label (default: ai.quaid.livetest.dashboard)
  -h, --help       Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)
      LABEL="${2:-}"
      PLIST_PATH="$LAUNCH_AGENTS_DIR/${LABEL}.plist"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

launchctl bootout "$GUI_TARGET" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl disable "$GUI_TARGET/$LABEL" >/dev/null 2>&1 || true

if [[ -f "$PLIST_PATH" ]]; then
  rm -f "$PLIST_PATH"
  echo "[dashboard-autostart] removed: $PLIST_PATH"
else
  echo "[dashboard-autostart] plist not present: $PLIST_PATH"
fi

echo "[dashboard-autostart] unloaded label: $LABEL"
