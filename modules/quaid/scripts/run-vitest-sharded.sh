#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SHARDS="${QUAID_TS_SHARDS:-2}"
PROGRESS_INTERVAL="${QUAID_GATE_PROGRESS_INTERVAL:-30}"

if ! [[ "$SHARDS" =~ ^[0-9]+$ ]] || [[ "$SHARDS" -lt 1 ]]; then
  echo "[vitest-sharded] ERROR: QUAID_TS_SHARDS must be a positive integer, got '$SHARDS'" >&2
  exit 2
fi

if [[ "$SHARDS" -eq 1 || "$#" -gt 0 ]]; then
  exec npx vitest run "$@"
fi

log_dir="$(mktemp -d "${TMPDIR:-/tmp}/quaid-vitest-shards.XXXXXX")"
db_dir="$log_dir/db"
mkdir -p "$db_dir"
pids=()
logs=()
labels=()

cleanup() {
  local rc=$?
  if [[ "$rc" -ne 0 ]]; then
    for pid in "${pids[@]:-}"; do
      kill "$pid" >/dev/null 2>&1 || true
    done
  fi
  rm -rf "$log_dir"
}
trap cleanup EXIT

echo "[vitest-sharded] shards=$SHARDS args=$*"
for shard in $(seq 1 "$SHARDS"); do
  label="shard-${shard}-${SHARDS}"
  log="$log_dir/${label}.log"
  labels+=("$label")
  logs+=("$log")
  echo "[vitest-sharded] start $label"
  (
    set -euo pipefail
    export TEST_DB_PATH="$db_dir/shard-${shard}.db"
    export MEMORY_DB_PATH="$TEST_DB_PATH"
    npx vitest run --shard="${shard}/${SHARDS}" "$@"
  ) >"$log" 2>&1 &
  pids+=("$!")
done

last_progress=$(date +%s)
while true; do
  running=()
  for idx in "${!pids[@]}"; do
    pid="${pids[$idx]}"
    if kill -0 "$pid" >/dev/null 2>&1; then
      running+=("${labels[$idx]}")
    fi
  done
  if [[ "${#running[@]}" -eq 0 ]]; then
    break
  fi
  sleep 1
  now=$(date +%s)
  if (( now - last_progress >= PROGRESS_INTERVAL )); then
    echo "[vitest-sharded] still running: ${running[*]}"
    last_progress="$now"
  fi
done

failed=0
for idx in "${!pids[@]}"; do
  pid="${pids[$idx]}"
  label="${labels[$idx]}"
  log="${logs[$idx]}"
  if wait "$pid"; then
    rc=0
  else
    rc=$?
    failed=1
  fi
  echo
  echo "================================================================"
  echo "[vitest-sharded] ${label} output (rc=${rc})"
  echo "================================================================"
  cat "$log"
done

if [[ "$failed" -ne 0 ]]; then
  echo "[vitest-sharded] FAIL"
  exit 1
fi

echo "[vitest-sharded] PASS shards=$SHARDS"
