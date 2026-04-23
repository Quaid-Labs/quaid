#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
REPO_ROOT="$(cd "${ROOT_DIR}/../.." && pwd)"
PROGRESS_INTERVAL="${QUAID_GATE_PROGRESS_INTERVAL:-30}"

MODE="quick"

usage() {
  cat <<USAGE
Usage: $(basename "$0") [--quick|--full]

Modes:
  --quick  Fast checks + isolated parallel Python unit tests (default)
  --full   Quick suite plus TS full suite + Python integration/regression (live tests separate)
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

cpu_count() {
  python3 - <<'PY'
import os
print(os.cpu_count() or 4)
PY
}

calc_workers() {
  local cap="$1"
  local divisor="$2"
  python3 - "$cap" "$divisor" <<'PY'
import os, sys
cap = int(sys.argv[1])
divisor = max(1, int(sys.argv[2]))
cpu = os.cpu_count() or 4
print(max(1, min(cap, max(1, cpu // divisor))))
PY
}

run_stage() {
  local name="$1"
  shift
  echo
  echo "================================================================"
  echo "[tests] ${name}"
  echo "================================================================"
  local __start __end
  __start=$(date +%s)
  "$@"
  __end=$(date +%s)
  echo "[tests] ${name}: duration=$((__end - __start))s"
}

run_parallel_group() {
  local group="$1"
  shift
  local slug
  slug="$(printf '%s' "$group" | tr -c '[:alnum:]' '-')"
  local log_dir
  log_dir="$(mktemp -d "${TMPDIR:-/tmp}/quaid-test-${slug}.XXXXXX")"
  local -a names=()
  local -a logs=()
  local -a pids=()
  local spec name cmd log pid

  echo
  echo "================================================================"
  echo "[tests] ${group}"
  echo "================================================================"
  for spec in "$@"; do
    name="${spec%%::*}"
    cmd="${spec#*::}"
    log="$log_dir/${#names[@]}.log"
    names+=("$name")
    logs+=("$log")
    echo "[tests] ${group}: start ${name}"
    ( set -uo pipefail; __start=$(date +%s); eval "$cmd"; __rc=$?; __end=$(date +%s); echo; echo "[tests] ${group}: ${name} duration=$((__end - __start))s rc=${__rc}"; exit "$__rc" ) >"$log" 2>&1 &
    pids+=("$!")
  done

  local last_progress
  last_progress=$(date +%s)
  while true; do
    local -a running=()
    for i in "${!pids[@]}"; do
      pid="${pids[$i]}"
      if kill -0 "$pid" >/dev/null 2>&1; then
        running+=("${names[$i]}")
      fi
    done
    if [[ "${#running[@]}" -eq 0 ]]; then
      break
    fi
    sleep 1
    local now
    now=$(date +%s)
    if (( now - last_progress >= PROGRESS_INTERVAL )); then
      echo "[tests] ${group}: still running: ${running[*]}"
      last_progress="$now"
    fi
  done

  local failed=0
  local rc=0
  for i in "${!pids[@]}"; do
    name="${names[$i]}"
    log="${logs[$i]}"
    if wait "${pids[$i]}"; then
      rc=0
    else
      rc=$?
      failed=1
    fi
    echo
    echo "================================================================"
    echo "[tests] ${group}: ${name} output (rc=${rc})"
    echo "================================================================"
    cat "$log"
  done
  rm -rf "$log_dir"

  if [[ "$failed" -ne 0 ]]; then
    echo "[tests] ${group}: FAIL" >&2
    return 1
  fi
  echo "[tests] ${group}: PASS"
}

# Best-effort TS dependency probe for clean dev workspaces.
has_vitest() {
  node -e "require.resolve('vitest/package.json')" >/dev/null 2>&1
}

plugin_contract_health_smoke() {
  python3 - <<PY
import json
import os
import subprocess
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="quaid-plugin-health-") as tmp:
    tmp_path = Path(tmp)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "adapter": {"type": "standalone"},
                # Health smoke only needs config/bootstrap viability.
                # Disable plugin runtime loading to avoid host-workspace assumptions.
                "plugins": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    out = subprocess.check_output(
        ["python3", "core/runtime/plugin_health.py"],
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": ".",
            "QUAID_HOME": str(tmp_path),
            "OPENCLAW_WORKSPACE": str(tmp_path),
            "QUAID_INSTANCE": "smoke-test",
        },
    )
    payload = json.loads(out)
    if not isinstance(payload, dict):
        raise SystemExit("plugin_health output was not a JSON object")
    if "enabled" not in payload:
        raise SystemExit("plugin_health output missing 'enabled'")
    if "plugins" not in payload:
        raise SystemExit("plugin_health output missing 'plugins'")
    print(json.dumps({"enabled": bool(payload["enabled"]), "plugin_count": len(payload.get("plugins", {}))}))
PY
}

optional_repo_check_specs=()
if [[ -f "${REPO_ROOT}/scripts/check-docs-consistency.mjs" ]]; then
  optional_repo_check_specs+=("Docs consistency check::node '${REPO_ROOT}/scripts/check-docs-consistency.mjs'")
fi
if [[ -f "${REPO_ROOT}/scripts/release-verify.mjs" ]]; then
  optional_repo_check_specs+=("Release consistency check::node '${REPO_ROOT}/scripts/release-verify.mjs'")
fi

PYTEST_UNIT_WORKERS="${QUAID_PYTEST_UNIT_WORKERS:-$(calc_workers 8 2)}"
PYTEST_INTEGRATION_WORKERS="${QUAID_PYTEST_INTEGRATION_WORKERS:-$(calc_workers 4 2)}"
PYTEST_REGRESSION_WORKERS="${QUAID_PYTEST_REGRESSION_WORKERS:-$(calc_workers 4 2)}"
CPU_COUNT="$(cpu_count)"
echo "[tests] cpu_count=${CPU_COUNT} pytest_unit_workers=${PYTEST_UNIT_WORKERS} pytest_integration_workers=${PYTEST_INTEGRATION_WORKERS} pytest_regression_workers=${PYTEST_REGRESSION_WORKERS} ts_shards=${QUAID_TS_SHARDS:-2}"

# 1) Build generated runtime first; pair sync checks depend on current artifacts.
run_stage "Runtime build" npm run build:runtime

static_specs=(
  "Runtime TS/JS pair sync check (strict)::npm run check:runtime-pairs:strict"
  "Boundary import check::npm run check:boundaries"
  "Python compile check::python3 -m compileall -q ."
  "JavaScript syntax check::node --check adaptors/openclaw/index.js"
  "JavaScript syntax check (timeout manager)::node --check core/session-timeout.js"
  "TypeScript lint::npm run lint:ts"
  "Python lint::npm run lint:py"
  "Plugin contract health smoke::plugin_contract_health_smoke"
)
if [[ "${#optional_repo_check_specs[@]}" -gt 0 ]]; then
  static_specs+=("${optional_repo_check_specs[@]}")
fi
run_parallel_group "Static checks" "${static_specs[@]}"

# 2) Deterministic TypeScript integration coverage and Python unit tests are isolated.
if has_vitest; then
  mkdir -p "$ROOT_DIR/../logs"
  run_parallel_group "Quick suites" \
    "TypeScript integration suite::npm run test:integration" \
    "Python unit suite (parallel isolated)::python3 scripts/run_pytests.py --mode unit --workers ${PYTEST_UNIT_WORKERS} --timeout 120" \
    "CI smoke Python suite::python3 -m pytest -q tests/test_events.py tests/test_docs_updater.py tests/test_project_updater.py tests/test_adapter.py tests/test_claude_code_auth_provider.py tests/test_memory_graph_singleton.py"
else
  echo
  echo "================================================================"
  echo "[tests] Quick suites"
  echo "================================================================"
  echo "[tests] SKIP TypeScript integration suite: vitest not installed in this clean workspace"
  run_parallel_group "Quick Python suites" \
    "Python unit suite (parallel isolated)::python3 scripts/run_pytests.py --mode unit --workers ${PYTEST_UNIT_WORKERS} --timeout 120" \
    "CI smoke Python suite::python3 -m pytest -q tests/test_events.py tests/test_docs_updater.py tests/test_project_updater.py tests/test_adapter.py tests/test_claude_code_auth_provider.py tests/test_memory_graph_singleton.py"
fi

if [[ "$MODE" == "full" ]]; then
  if has_vitest; then
    run_parallel_group "Full suites" \
      "TypeScript full suite::scripts/run-vitest-sharded.sh" \
      "Python integration suite (parallel isolated)::python3 scripts/run_pytests.py --mode integration --workers ${PYTEST_INTEGRATION_WORKERS} --timeout 180" \
      "Python regression suite (parallel isolated)::python3 scripts/run_pytests.py --mode regression --workers ${PYTEST_REGRESSION_WORKERS} --timeout 600"
  else
    run_parallel_group "Full Python suites" \
      "Python integration suite (parallel isolated)::python3 scripts/run_pytests.py --mode integration --workers ${PYTEST_INTEGRATION_WORKERS} --timeout 180" \
      "Python regression suite (parallel isolated)::python3 scripts/run_pytests.py --mode regression --workers ${PYTEST_REGRESSION_WORKERS} --timeout 600"
    echo "[tests] SKIP TypeScript full suite: vitest not installed in this clean workspace"
  fi

  echo
  echo "================================================================"
  echo "[tests] Live validation"
  echo "================================================================"
  echo "[tests] NOTE: legacy e2e automation is deprecated and no longer part of test:all:full"
  echo "[tests] Use modules/quaid/tests/livetest/livetest-guide/ for authoritative host validation"
fi

echo
echo "[tests] PASS (${MODE})"
