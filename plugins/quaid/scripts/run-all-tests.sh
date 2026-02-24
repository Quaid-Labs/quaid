#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
REPO_ROOT="$(cd "${ROOT_DIR}/../.." && pwd)"

MODE="quick"

usage() {
  cat <<USAGE
Usage: $(basename "$0") [--quick|--full]

Modes:
  --quick  Fast checks + isolated parallel Python unit tests (default)
  --full   Quick suite plus TS full suite + Python integration/regression
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) MODE="quick"; shift ;;
    --full) MODE="full"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

run_stage() {
  local name="$1"
  shift
  echo
  echo "================================================================"
  echo "[tests] ${name}"
  echo "================================================================"
  "$@"
}

run_optional_repo_checks() {
  if [[ -f "${REPO_ROOT}/scripts/check-docs-consistency.mjs" ]]; then
    run_stage "Docs consistency check" node "${REPO_ROOT}/scripts/check-docs-consistency.mjs"
  fi
  if [[ -f "${REPO_ROOT}/scripts/release-verify.mjs" ]]; then
    run_stage "Release consistency check" node "${REPO_ROOT}/scripts/release-verify.mjs"
  fi
}

# Best-effort TS dependency probe for clean dev workspaces.
has_vitest() {
  node -e "require.resolve('vitest/package.json')" >/dev/null 2>&1
}

# 1) Build/syntax checks
run_stage "Runtime TS/JS pair sync check (strict)" npm run check:runtime-pairs:strict
run_stage "Python compile check" python3 -m compileall -q .
run_stage "JavaScript syntax check" node --check adaptors/openclaw/index.js
run_stage "JavaScript syntax check (timeout manager)" node --check core/session-timeout.js
run_optional_repo_checks

# 2) Deterministic TypeScript integration coverage
if has_vitest; then
  mkdir -p "$ROOT_DIR/../logs"
  run_stage "TypeScript integration suite" npm run test:integration
else
  echo
  echo "================================================================"
  echo "[tests] TypeScript integration suite"
  echo "================================================================"
  echo "[tests] SKIP: vitest not installed in this clean workspace"
  echo "[tests] Hint: run bootstrap/install in test workspace for TS suites"
fi

# 3) Python unit tests (parallel, isolated per-file with timeout diagnostics)
run_stage "Python unit suite (parallel isolated)" python3 scripts/run_pytests.py --mode unit --workers 4 --timeout 120

if [[ "$MODE" == "full" ]]; then
  # Full TS unit/integration suite
  if has_vitest; then
    run_stage "TypeScript full suite" npm run test:run
  else
    echo
    echo "================================================================"
    echo "[tests] TypeScript full suite"
    echo "================================================================"
    echo "[tests] SKIP: vitest not installed in this clean workspace"
  fi
  # Python integration and regression suites in isolated mode
  run_stage "Python integration suite (parallel isolated)" python3 scripts/run_pytests.py --mode integration --workers 2 --timeout 180
  run_stage "Python regression suite (parallel isolated)" python3 scripts/run_pytests.py --mode regression --workers 4 --timeout 600

  # Bootstrap-driven end-to-end auth matrix (gateway/runtime wiring).
  E2E_MATRIX_SCRIPT="${QUAID_E2E_MATRIX_SCRIPT:-$ROOT_DIR/scripts/run-quaid-e2e-matrix.sh}"
  E2E_PATHS="${QUAID_E2E_PATHS:-openai-oauth,openai-api,anthropic-api}"
  if [[ -x "$E2E_MATRIX_SCRIPT" ]]; then
    if [[ -n "${QUAID_E2E_EXPECT:-}" ]]; then
      run_stage "Bootstrap E2E auth matrix" "$E2E_MATRIX_SCRIPT" --paths "$E2E_PATHS" --expect "$QUAID_E2E_EXPECT"
    else
      run_stage "Bootstrap E2E auth matrix" "$E2E_MATRIX_SCRIPT" --paths "$E2E_PATHS"
    fi
  else
    echo
    echo "================================================================"
    echo "[tests] Bootstrap E2E auth matrix"
    echo "================================================================"
    echo "[tests] SKIP: matrix script not found/executable at $E2E_MATRIX_SCRIPT"
    echo "[tests] Hint: set QUAID_E2E_MATRIX_SCRIPT to plugins/quaid/scripts/run-quaid-e2e-matrix.sh"
    echo "[tests] Hint: set QUAID_E2E_PATHS to override auth lanes"
  fi
fi

echo
echo "[tests] PASS (${MODE})"
