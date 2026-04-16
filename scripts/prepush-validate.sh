#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP_FILE="$ROOT_DIR/.git/.quaid-prepush-validation.stamp"
FULL_MODE=0

usage() {
  cat <<'USAGE'
Usage: ./scripts/prepush-validate.sh [--full]

Runs pre-push validation and records a stamp bound to the current HEAD.

Options:
  --full    Also run the full local gate (`npm run test:all:full`)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)
      FULL_MODE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[prepush-validate] ERROR: unknown argument '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cd "$ROOT_DIR"
HEAD_SHA="$(git rev-parse HEAD)"
STAMP_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
MODE_LABEL="quick"
if [[ "$FULL_MODE" == "1" ]]; then
  MODE_LABEL="full"
fi

echo "[prepush-validate] docs consistency"
node scripts/check-docs-consistency.mjs

echo "[prepush-validate] release consistency"
node scripts/release-verify.mjs

(
  cd modules/quaid

  echo "[prepush-validate] runtime ts/js pairs"
  node scripts/check-runtime-pairs.mjs --strict

  echo "[prepush-validate] boundaries"
  npm run check:boundaries

  echo "[prepush-validate] lint ts"
  npm run lint:ts

  echo "[prepush-validate] lint py"
  npm run lint:py

  echo "[prepush-validate] ts unit"
  npm run test:run

  echo "[prepush-validate] py unit"
  python3 scripts/run_pytests.py --mode unit --workers 4 --timeout 120

  echo "[prepush-validate] ts integration"
  npm run test:integration

  echo "[prepush-validate] ci smoke py"
  python3 -m pytest -q \
    tests/test_events.py \
    tests/test_docs_updater.py \
    tests/test_project_updater.py \
    tests/test_adapter.py \
    tests/test_claude_code_auth_provider.py \
    tests/test_memory_graph_singleton.py

  if [[ "$FULL_MODE" == "1" ]]; then
    echo "[prepush-validate] full local gate"
    npm run test:all:full
  fi
)

cat > "$STAMP_FILE" <<EOF
head_sha=$HEAD_SHA
mode=$MODE_LABEL
timestamp_utc=$STAMP_TS
EOF

echo "[prepush-validate] PASS head=$HEAD_SHA mode=$MODE_LABEL stamp=$STAMP_FILE"
