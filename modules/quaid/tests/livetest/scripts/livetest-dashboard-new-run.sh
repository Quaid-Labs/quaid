#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIVETEST_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

TARGET_LOG="$LIVETEST_DIR/dashboard.log"
TEMPLATE_LOG="$LIVETEST_DIR/dashboard_template.log"
LEGACY_TEMPLATE_LOG="$LIVETEST_DIR/current_run.log.example"

FORCE=0
TITLE="${TITLE:-}"

usage() {
  cat <<'EOF'
livetest-dashboard-new-run.sh

Create/reset dashboard.log for a new live-test run by copying dashboard_template.log.

Usage:
  tests/livetest/scripts/livetest-dashboard-new-run.sh [options]

Options:
  --title "<run title>"  Override the first line in dashboard.log
  --force                Overwrite dashboard.log if it already exists
  -h, --help             Show help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --title)
      TITLE="${2:-}"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
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

if [[ ! -f "$TEMPLATE_LOG" && -f "$LEGACY_TEMPLATE_LOG" ]]; then
  cp "$LEGACY_TEMPLATE_LOG" "$TEMPLATE_LOG"
  echo "[dashboard-new-run] created template from legacy file: $TEMPLATE_LOG"
fi

if [[ ! -f "$TEMPLATE_LOG" ]]; then
  echo "[dashboard-new-run] missing template: $TEMPLATE_LOG" >&2
  exit 1
fi

if [[ -f "$TARGET_LOG" && "$FORCE" -ne 1 ]]; then
  echo "[dashboard-new-run] dashboard log already exists: $TARGET_LOG" >&2
  echo "[dashboard-new-run] re-run with --force to overwrite." >&2
  exit 1
fi

cp "$TEMPLATE_LOG" "$TARGET_LOG"
rm -f "$LIVETEST_DIR/.dashboard-timing.json" "$LIVETEST_DIR"/.dashboard-timing.json.tmp.*

if [[ -n "$TITLE" ]]; then
  PYTHON_BIN="${QUAID_PYTHON_BIN:-python3}"
  "$PYTHON_BIN" - "$TARGET_LOG" "$TITLE" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
title = sys.argv[2].strip()
text = path.read_text(encoding="utf-8")
lines = text.splitlines()
if lines:
    lines[0] = title
else:
    lines = [title]
path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
PY
fi

echo "[dashboard-new-run] ready: $TARGET_LOG"
