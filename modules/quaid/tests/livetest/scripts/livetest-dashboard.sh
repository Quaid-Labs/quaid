#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVETEST_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"

usage() {
  cat <<'EOF'
livetest-dashboard.sh

Serve the lightweight live-test dashboard.

Usage:
  tests/livetest/scripts/livetest-dashboard.sh [--host 0.0.0.0] [--port 8765]

Environment overrides:
  HOST=0.0.0.0
  PORT=8765
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

LOG_PATH="$LIVETEST_DIR/current_run.log"
EXAMPLE_PATH="$LIVETEST_DIR/current_run.log.example"

if [[ ! -f "$LOG_PATH" && -f "$EXAMPLE_PATH" ]]; then
  cp "$EXAMPLE_PATH" "$LOG_PATH"
  echo "[dashboard] created $LOG_PATH from example"
fi

echo "[dashboard] serving: $LIVETEST_DIR"
echo "[dashboard] URL: http://$HOST:$PORT/dashboard.html"
echo "[dashboard] data file: $LOG_PATH"

exec python3 -m http.server "$PORT" --bind "$HOST" --directory "$LIVETEST_DIR"
