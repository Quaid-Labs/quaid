#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVETEST_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8766}"

usage() {
  cat <<'EOF'
livetest-dashboard.sh

Serve the lightweight live-test dashboard.

Usage:
  tests/livetest/scripts/livetest-dashboard.sh [--host 0.0.0.0] [--port 8765]

Environment overrides:
  HOST=0.0.0.0
  PORT=8766
EOF
}

_python_bin_ok() {
  local bin="${1:-}"
  [[ -n "$bin" ]] || return 1
  if [[ "$bin" == */* && ! -x "$bin" ]]; then
    return 1
  fi
  "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1
}

_resolve_python_bin() {
  local candidates=()
  [[ -n "${QUAID_PYTHON_BIN:-}" ]] && candidates+=("${QUAID_PYTHON_BIN}")
  candidates+=(
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "python3"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if _python_bin_ok "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf '%s\n' "python3"
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
PYTHON_BIN="$(_resolve_python_bin)"
export QUAID_PYTHON_BIN="$PYTHON_BIN"

if [[ ! -f "$LOG_PATH" && -f "$EXAMPLE_PATH" ]]; then
  cp "$EXAMPLE_PATH" "$LOG_PATH"
  echo "[dashboard] created $LOG_PATH from example"
fi

echo "[dashboard] serving: $LIVETEST_DIR"
echo "[dashboard] URL: http://$HOST:$PORT/dashboard.html"
echo "[dashboard] data file: $LOG_PATH"
echo "[dashboard] python: $PYTHON_BIN"

exec "$PYTHON_BIN" -m http.server "$PORT" --bind "$HOST" --directory "$LIVETEST_DIR"
