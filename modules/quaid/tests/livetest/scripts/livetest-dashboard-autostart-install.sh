#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVETEST_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DASH_SCRIPT="$SCRIPT_DIR/livetest-dashboard.sh"

LABEL="${LABEL:-ai.quaid.livetest.dashboard}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8766}"

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/quaid"
OUT_LOG="$LOG_DIR/livetest-dashboard.stdout.log"
ERR_LOG="$LOG_DIR/livetest-dashboard.stderr.log"

usage() {
  cat <<'EOF'
livetest-dashboard-autostart-install.sh

Install and load a user LaunchAgent so the Quaid live-test dashboard starts
automatically at login/system start.

Usage:
  tests/livetest/scripts/livetest-dashboard-autostart-install.sh [options]

Options:
  --host <host>     Bind host (default: 0.0.0.0)
  --port <port>     Port (default: 8766)
  --label <label>   LaunchAgent label (default: ai.quaid.livetest.dashboard)
  -h, --help        Show help

Environment overrides:
  HOST=0.0.0.0
  PORT=8766
  LABEL=ai.quaid.livetest.dashboard
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --label)
      LABEL="${2:-}"
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

if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "Invalid --port value: $PORT" >&2
  exit 1
fi

mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR"

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>${DASH_SCRIPT}</string>
      <string>--host</string>
      <string>${HOST}</string>
      <string>--port</string>
      <string>${PORT}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${LIVETEST_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${OUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${ERR_LOG}</string>
  </dict>
</plist>
EOF

GUI_TARGET="gui/$(id -u)"

launchctl bootout "$GUI_TARGET" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true

if ! launchctl bootstrap "$GUI_TARGET" "$PLIST_PATH" >/dev/null 2>&1; then
  launchctl load "$PLIST_PATH"
fi

launchctl enable "$GUI_TARGET/$LABEL" >/dev/null 2>&1 || true
launchctl kickstart -k "$GUI_TARGET/$LABEL" >/dev/null 2>&1 || true

echo "[dashboard-autostart] installed: $PLIST_PATH"
echo "[dashboard-autostart] label: $LABEL"
echo "[dashboard-autostart] URL: http://${HOST}:${PORT}/dashboard.html"
echo "[dashboard-autostart] logs: $OUT_LOG | $ERR_LOG"
