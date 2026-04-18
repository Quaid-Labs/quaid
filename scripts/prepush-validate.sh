#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP_FILE="$ROOT_DIR/.git/.quaid-prepush-validation.stamp"
FULL_MODE=0
PROGRESS_INTERVAL="${QUAID_GATE_PROGRESS_INTERVAL:-30}"

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

run_parallel_group() {
  local group="$1"
  shift
  local slug
  slug="$(printf '%s' "$group" | tr -c '[:alnum:]' '-')"
  local log_dir
  log_dir="$(mktemp -d "${TMPDIR:-/tmp}/quaid-prepush-${slug}.XXXXXX")"
  local -a names=()
  local -a logs=()
  local -a pids=()
  local spec name cmd log pid

  for spec in "$@"; do
    name="${spec%%::*}"
    cmd="${spec#*::}"
    log="$log_dir/${#names[@]}.log"
    names+=("$name")
    logs+=("$log")
    echo "[prepush-validate] ${group}: start ${name}"
    ( set -uo pipefail; __start=$(date +%s); eval "$cmd"; __rc=$?; __end=$(date +%s); echo; echo "[prepush-validate] ${group}: ${name} duration=$((__end - __start))s rc=${__rc}"; exit "$__rc" ) >"$log" 2>&1 &
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
      echo "[prepush-validate] ${group}: still running: ${running[*]}"
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
    echo "[prepush-validate] ${group}: ${name} output (rc=${rc})"
    echo "================================================================"
    cat "$log"
  done
  rm -rf "$log_dir"

  if [[ "$failed" -ne 0 ]]; then
    echo "[prepush-validate] ${group}: FAIL" >&2
    return 1
  fi
  echo "[prepush-validate] ${group}: PASS"
}

cd "$ROOT_DIR"
HEAD_SHA="$(git rev-parse HEAD)"
STAMP_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
MODE_LABEL="quick"
if [[ "$FULL_MODE" == "1" ]]; then
  MODE_LABEL="full"
fi

PYTEST_UNIT_WORKERS="${QUAID_PYTEST_UNIT_WORKERS:-$(calc_workers 8 2)}"
CPU_COUNT="$(cpu_count)"
echo "[prepush-validate] cpu_count=${CPU_COUNT} pytest_unit_workers=${PYTEST_UNIT_WORKERS} ts_shards=${QUAID_TS_SHARDS:-2}"

if [[ "$FULL_MODE" == "1" ]]; then
  echo "[prepush-validate] full mode delegates to npm run test:all:full to avoid duplicate quick-suite execution"
  (
    cd modules/quaid
    __start=$(date +%s)
    npm run test:all:full
    __end=$(date +%s)
    echo "[prepush-validate] full local gate duration=$((__end - __start))s"
  )
else
  run_parallel_group "repo checks" \
    "docs consistency::node scripts/check-docs-consistency.mjs" \
    "release consistency::node scripts/release-verify.mjs"

  (
    cd modules/quaid

    run_parallel_group "static checks" \
      "runtime ts/js pairs::node scripts/check-runtime-pairs.mjs --strict" \
      "boundaries::npm run check:boundaries" \
      "lint ts::npm run lint:ts" \
      "lint py::npm run lint:py"

    run_parallel_group "primary suites" \
      "ts unit::scripts/run-vitest-sharded.sh" \
      "py unit::python3 scripts/run_pytests.py --mode unit --workers ${PYTEST_UNIT_WORKERS} --timeout 120" \
      "ts integration::npm run test:integration" \
      "ci smoke py::python3 -m pytest -q tests/test_events.py tests/test_docs_updater.py tests/test_project_updater.py tests/test_adapter.py tests/test_claude_code_auth_provider.py tests/test_memory_graph_singleton.py"
  )
fi

cat > "$STAMP_FILE" <<EOF_STAMP
head_sha=$HEAD_SHA
mode=$MODE_LABEL
timestamp_utc=$STAMP_TS
EOF_STAMP

echo "[prepush-validate] PASS head=$HEAD_SHA mode=$MODE_LABEL stamp=$STAMP_FILE"
