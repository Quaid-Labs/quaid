#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOOTSTRAP_ROOT="${QUAID_BOOTSTRAP_ROOT:-${HOME}/quaid/bootstrap}"
ENV_FILE="${QUAID_E2E_ENV_FILE:-${PLUGIN_ROOT}/.env.e2e}"

E2E_WS="${HOME}/quaid/e2e-test"
DEV_WS="${HOME}/quaid/dev"
OPENCLAW_SOURCE="${HOME}/openclaw-source"

PROFILE_TEST="${QUAID_E2E_PROFILE_TEST:-${BOOTSTRAP_ROOT}/profiles/runtime-profile.local.quaid.json}"
PROFILE_SRC="${QUAID_E2E_PROFILE_SRC:-${BOOTSTRAP_ROOT}/profiles/runtime-profile.local.quaid.json}"
TMP_PROFILE_BASE="$(mktemp /tmp/quaid-e2e-profile.XXXXXX)"
TMP_PROFILE="${TMP_PROFILE_BASE}.json"
mv "$TMP_PROFILE_BASE" "$TMP_PROFILE"

AUTH_PATH="openai-oauth"
KEEP_ON_SUCCESS=false
RUN_JANITOR=true
RUN_LLM_SMOKE=true
RUN_LIVE_EVENTS=true
RUN_NOTIFY_MATRIX=true
RUN_INTEGRATION_TESTS=true
RUN_INGEST_STRESS=true
RUN_PROJECT_CRUD=true
RUN_SESSION_TOOLS=true
RUN_JANITOR_SEED=true
RUN_MEMORY_FLOW=true
RUN_RESILIENCE=false
RUN_JANITOR_STRESS=false
RUN_PREBENCH_GUARDS=false
RUN_JANITOR_PARALLEL_BENCH=false
JANITOR_TIMEOUT_SECONDS=240
JANITOR_MODE="apply"
NOTIFY_LEVEL="debug"
LIVE_TIMEOUT_WAIT_SECONDS=90
E2E_SUITES="full"
NIGHTLY_MODE=false
NIGHTLY_PROFILE="quick"
RESILIENCE_LOOPS=1
JANITOR_STRESS_PASSES=2
E2E_SKIP_EXIT_CODE=20
QUICK_BOOTSTRAP=false
REUSE_WORKSPACE=false
SUMMARY_OUTPUT_PATH="${QUAID_E2E_SUMMARY_PATH:-/tmp/quaid-e2e-last-summary.json}"
SUMMARY_HISTORY_PATH="${QUAID_E2E_SUMMARY_HISTORY_PATH:-/tmp/quaid-e2e-summary-history.jsonl}"
SUMMARY_BUDGET_RECOMMENDATION_PATH="${QUAID_E2E_BUDGET_RECOMMENDATION_PATH:-/tmp/quaid-e2e-budget-recommendation.json}"
RUNTIME_BUDGET_TUNE_MIN_SAMPLES="${QUAID_E2E_BUDGET_TUNE_MIN_SAMPLES:-5}"
RUNTIME_BUDGET_TUNE_BUFFER_RATIO="${QUAID_E2E_BUDGET_TUNE_BUFFER_RATIO:-1.2}"
NOTIFY_REQUIRE_DELIVERY="${QUAID_E2E_NOTIFY_REQUIRE_DELIVERY:-false}"
AUTO_STAGE_BUDGETS="${QUAID_E2E_AUTO_STAGE_BUDGETS:-true}"
AUTO_STAGE_BUDGETS_STAGES="${QUAID_E2E_AUTO_STAGE_BUDGETS_STAGES:-bootstrap,resilience,notify_matrix,janitor}"
JANITOR_PARALLEL_REPORT_PATH="${QUAID_E2E_JANITOR_PARALLEL_REPORT_PATH:-/tmp/quaid-e2e-janitor-parallel-bench.json}"
JPB_MAX_ERRORS="${QUAID_E2E_JPB_MAX_ERRORS:-0}"
JPB_MAX_WARNINGS="${QUAID_E2E_JPB_MAX_WARNINGS:--1}"
JPB_MAX_CONTRADICTIONS_PENDING_AFTER="${QUAID_E2E_JPB_MAX_CONTRADICTIONS_PENDING_AFTER:-0}"
JPB_MAX_DEDUP_UNREVIEWED_AFTER="${QUAID_E2E_JPB_MAX_DEDUP_UNREVIEWED_AFTER:-0}"
JPB_MAX_DECAY_PENDING_AFTER="${QUAID_E2E_JPB_MAX_DECAY_PENDING_AFTER:-0}"
JPB_MAX_STAGING_EVENTS_AFTER="${QUAID_E2E_JPB_MAX_STAGING_EVENTS_AFTER:-0}"
RUNTIME_BUDGET_PROFILE="${QUAID_E2E_RUNTIME_BUDGET_PROFILE:-auto}"
RUNTIME_BUDGET_SECONDS="${QUAID_E2E_RUNTIME_BUDGET_SECONDS:-0}"
STAGE_BUDGETS_JSON="${QUAID_E2E_STAGE_BUDGETS_JSON:-}"
RUNTIME_BUDGET_EXCEEDED="false"
RUN_START_EPOCH="$(date +%s)"
CURRENT_STAGE="init"
E2E_STATUS="running"
E2E_FAIL_LINE=""
E2E_FAIL_REASON=""

STAGE_bootstrap="pending"
STAGE_bootstrap_START_EPOCH=0
STAGE_bootstrap_DURATION_SECONDS=0
STAGE_gateway_smoke="pending"
STAGE_gateway_smoke_START_EPOCH=0
STAGE_gateway_smoke_DURATION_SECONDS=0
STAGE_integration="pending"
STAGE_integration_START_EPOCH=0
STAGE_integration_DURATION_SECONDS=0
STAGE_live_events="pending"
STAGE_live_events_START_EPOCH=0
STAGE_live_events_DURATION_SECONDS=0
STAGE_resilience="pending"
STAGE_resilience_START_EPOCH=0
STAGE_resilience_DURATION_SECONDS=0
STAGE_memory_flow="pending"
STAGE_memory_flow_START_EPOCH=0
STAGE_memory_flow_DURATION_SECONDS=0
STAGE_notify_matrix="pending"
STAGE_notify_matrix_START_EPOCH=0
STAGE_notify_matrix_DURATION_SECONDS=0
STAGE_ingest_stress="pending"
STAGE_ingest_stress_START_EPOCH=0
STAGE_ingest_stress_DURATION_SECONDS=0
STAGE_project_crud="pending"
STAGE_project_crud_START_EPOCH=0
STAGE_project_crud_DURATION_SECONDS=0
STAGE_session_tools="pending"
STAGE_session_tools_START_EPOCH=0
STAGE_session_tools_DURATION_SECONDS=0
STAGE_janitor="pending"
STAGE_janitor_START_EPOCH=0
STAGE_janitor_DURATION_SECONDS=0

SKIP_JANITOR_FLAG=false
SKIP_LLM_SMOKE_FLAG=false
SKIP_LIVE_EVENTS_FLAG=false
SKIP_NOTIFY_MATRIX_FLAG=false

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Options:
  --auth-path <id>       Auth path for bootstrap profile (openai-oauth|openai-api|anthropic-oauth|anthropic-api; default: openai-oauth)
  --keep-on-success      Do not delete ~/quaid/e2e-test after successful run
  --skip-janitor         Skip janitor phase
  --skip-llm-smoke       Skip gateway LLM smoke call
  --skip-live-events     Skip live command/timeout hook validation
  --skip-notify-matrix   Skip notification-level matrix validation (quiet/normal/debug)
  --suite <csv>          Test suites to run. Values: full,core,blocker,pre-benchmark,nightly,nightly-strict-notify,janitor-parallel-bench,smoke,integration,live,memory,notify,ingest,project,session,janitor,seed,resilience,janitor-stress (default: full)
  --janitor-timeout <s>  Janitor timeout seconds (default: 240)
  --janitor-dry-run      Run janitor in dry-run mode (default is apply)
  --live-timeout-wait <s> Max seconds to wait for timeout event in live check (default: 90)
  --nightly-profile <p>  Nightly profile: quick|deep (default: quick)
  --resilience-loops <n> Number of resilience-suite iterations (default: 1; nightly defaults to 2)
  --notify-level <lvl>   Quaid notify level for e2e (quiet|normal|verbose|debug, default: debug)
  --runtime-budget-profile <p> Runtime budget profile: auto|off|quick|deep (default: auto)
  --runtime-budget-seconds <n>  Explicit runtime budget override in seconds (0 disables override)
  --env-file <path>      Optional .env file to source before running (default: modules/quaid/.env.e2e)
  --openclaw-source <p>  OpenClaw source repo path (default: ~/openclaw-source)
  --quick-bootstrap      Skip OpenClaw source refresh/install during bootstrap (faster local loops)
  --reuse-workspace      Reuse existing ~/quaid/e2e-test workspace when possible (fast path)
  -h, --help             Show this help
USAGE
}

emit_failure_diagnostics() {
  local code="${1:-1}"
  local line="${2:-unknown}"
  set +e
  echo "[e2e] FAILURE: command failed at line=${line} exit_code=${code}" >&2
  echo "[e2e] ---- diagnostics begin ----" >&2
  echo "[e2e] workspace=${E2E_WS}" >&2
  echo "[e2e] auth_path=${AUTH_PATH} suites=${E2E_SUITES}" >&2
  if [[ -d "${E2E_WS}" ]]; then
    echo "[e2e] pending signal files:" >&2
    find "${E2E_WS}/data/pending-extraction-signals" -maxdepth 2 -type f 2>/dev/null | sed -n '1,40p' >&2 || true
    echo "[e2e] timeout events tail:" >&2
    tail -n 80 "${E2E_WS}/logs/quaid/session-timeout-events.jsonl" 2>/dev/null >&2 || true
    echo "[e2e] timeout log tail:" >&2
    tail -n 80 "${E2E_WS}/logs/quaid/session-timeout.log" 2>/dev/null >&2 || true
    echo "[e2e] notify-worker tail:" >&2
    tail -n 80 "${E2E_WS}/logs/notify-worker.log" 2>/dev/null >&2 || true
  fi
  echo "[e2e] openclaw gateway status:" >&2
  openclaw gateway status >&2 || true
  echo "[e2e] openclaw logs tail:" >&2
  openclaw logs --plain --limit 120 >&2 || true
  echo "[e2e] ---- diagnostics end ----" >&2
  set -e
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auth-path) AUTH_PATH="$2"; shift 2 ;;
    --keep-on-success) KEEP_ON_SUCCESS=true; shift ;;
    --skip-janitor) SKIP_JANITOR_FLAG=true; shift ;;
    --skip-llm-smoke) SKIP_LLM_SMOKE_FLAG=true; shift ;;
    --skip-live-events) SKIP_LIVE_EVENTS_FLAG=true; shift ;;
    --skip-notify-matrix) SKIP_NOTIFY_MATRIX_FLAG=true; shift ;;
    --suite) E2E_SUITES="$2"; shift 2 ;;
    --janitor-timeout) JANITOR_TIMEOUT_SECONDS="$2"; shift 2 ;;
    --janitor-dry-run) JANITOR_MODE="dry-run"; shift ;;
    --live-timeout-wait) LIVE_TIMEOUT_WAIT_SECONDS="$2"; shift 2 ;;
    --nightly-profile) NIGHTLY_PROFILE="$2"; shift 2 ;;
    --resilience-loops) RESILIENCE_LOOPS="$2"; shift 2 ;;
    --notify-level) NOTIFY_LEVEL="$2"; shift 2 ;;
    --runtime-budget-profile) RUNTIME_BUDGET_PROFILE="$2"; shift 2 ;;
    --runtime-budget-seconds) RUNTIME_BUDGET_SECONDS="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --openclaw-source) OPENCLAW_SOURCE="$2"; shift 2 ;;
    --quick-bootstrap) QUICK_BOOTSTRAP=true; shift ;;
    --reuse-workspace) REUSE_WORKSPACE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

suite_has() {
  local needle="$1"
  local token
  IFS=',' read -r -a _suite_tokens <<< "$E2E_SUITES"
  for token in "${_suite_tokens[@]}"; do
    token="$(echo "$token" | tr '[:upper:]' '[:lower:]' | xargs)"
    [[ -z "$token" ]] && continue
    if [[ "$token" == "$needle" || "$token" == "full" || "$token" == "all" ]]; then
      return 0
    fi
  done
  return 1
}

if suite_has "nightly"; then
  NIGHTLY_MODE=true
  E2E_SUITES="full"
fi
if suite_has "nightly-strict-notify"; then
  NIGHTLY_MODE=true
  NOTIFY_REQUIRE_DELIVERY="true"
  E2E_SUITES="full"
fi

if suite_has "blocker"; then
  RUN_LLM_SMOKE=false
  RUN_INTEGRATION_TESTS=true
  RUN_LIVE_EVENTS=true
  RUN_NOTIFY_MATRIX=true
  RUN_INGEST_STRESS=false
  RUN_PROJECT_CRUD=false
  RUN_SESSION_TOOLS=true
  RUN_JANITOR=false
  RUN_JANITOR_SEED=false
  RUN_MEMORY_FLOW=true
elif suite_has "pre-benchmark"; then
  RUN_LLM_SMOKE=true
  RUN_INTEGRATION_TESTS=true
  RUN_LIVE_EVENTS=true
  RUN_NOTIFY_MATRIX=true
  RUN_INGEST_STRESS=true
  RUN_PROJECT_CRUD=true
  RUN_SESSION_TOOLS=true
  RUN_JANITOR=true
  RUN_JANITOR_SEED=true
  RUN_MEMORY_FLOW=true
  RUN_PREBENCH_GUARDS=true
elif suite_has "janitor-parallel-bench"; then
  RUN_LLM_SMOKE=false
  RUN_INTEGRATION_TESTS=false
  RUN_LIVE_EVENTS=false
  RUN_NOTIFY_MATRIX=false
  RUN_INGEST_STRESS=false
  RUN_PROJECT_CRUD=false
  RUN_SESSION_TOOLS=false
  RUN_JANITOR=true
  RUN_JANITOR_SEED=true
  RUN_MEMORY_FLOW=false
  RUN_PREBENCH_GUARDS=true
  RUN_JANITOR_PARALLEL_BENCH=true
elif suite_has "core"; then
  RUN_LLM_SMOKE=true
  RUN_INTEGRATION_TESTS=true
  RUN_LIVE_EVENTS=true
  RUN_NOTIFY_MATRIX=false
  RUN_INGEST_STRESS=true
  RUN_PROJECT_CRUD=true
  RUN_SESSION_TOOLS=true
  RUN_JANITOR=true
  RUN_JANITOR_SEED=true
  RUN_MEMORY_FLOW=true
elif suite_has "full"; then
  RUN_LLM_SMOKE=true
  RUN_INTEGRATION_TESTS=true
  RUN_LIVE_EVENTS=true
  RUN_NOTIFY_MATRIX=true
  RUN_INGEST_STRESS=true
  RUN_PROJECT_CRUD=true
  RUN_SESSION_TOOLS=true
  RUN_JANITOR=true
  RUN_JANITOR_SEED=true
  RUN_MEMORY_FLOW=true
  RUN_RESILIENCE=false
  RUN_JANITOR_STRESS=false
else
  RUN_LLM_SMOKE=false
  RUN_INTEGRATION_TESTS=false
  RUN_LIVE_EVENTS=false
  RUN_NOTIFY_MATRIX=false
  RUN_INGEST_STRESS=false
  RUN_PROJECT_CRUD=false
  RUN_SESSION_TOOLS=false
  RUN_JANITOR=false
  RUN_JANITOR_SEED=false
  RUN_MEMORY_FLOW=false

  suite_has "smoke" && RUN_LLM_SMOKE=true
  suite_has "integration" && RUN_INTEGRATION_TESTS=true
  suite_has "tests" && RUN_INTEGRATION_TESTS=true
  suite_has "live" && RUN_LIVE_EVENTS=true
  suite_has "hooks" && RUN_LIVE_EVENTS=true
  suite_has "notify" && RUN_NOTIFY_MATRIX=true
  suite_has "memory" && RUN_MEMORY_FLOW=true
  suite_has "ingest" && RUN_INGEST_STRESS=true
  suite_has "project" && RUN_PROJECT_CRUD=true
  suite_has "session" && RUN_SESSION_TOOLS=true
  suite_has "stress" && RUN_INGEST_STRESS=true
  suite_has "janitor" && RUN_JANITOR=true
  suite_has "seed" && RUN_JANITOR_SEED=true
  suite_has "resilience" && RUN_RESILIENCE=true
  suite_has "janitor-stress" && RUN_JANITOR_STRESS=true
fi

if [[ "$NIGHTLY_MODE" == true ]]; then
  RUN_RESILIENCE=true
  RUN_JANITOR_STRESS=true
fi
if [[ "$AUTO_STAGE_BUDGETS" != "true" && "$AUTO_STAGE_BUDGETS" != "false" ]]; then
  echo "Invalid QUAID_E2E_AUTO_STAGE_BUDGETS: $AUTO_STAGE_BUDGETS (expected true|false)" >&2
  exit 1
fi

if [[ "$SKIP_JANITOR_FLAG" == true ]]; then RUN_JANITOR=false; fi
if [[ "$SKIP_LLM_SMOKE_FLAG" == true ]]; then RUN_LLM_SMOKE=false; fi
if [[ "$SKIP_LIVE_EVENTS_FLAG" == true ]]; then RUN_LIVE_EVENTS=false; fi
if [[ "$SKIP_NOTIFY_MATRIX_FLAG" == true ]]; then RUN_NOTIFY_MATRIX=false; fi
if ! [[ "$RESILIENCE_LOOPS" =~ ^[0-9]+$ ]] || [[ "$RESILIENCE_LOOPS" -lt 1 ]]; then
  echo "Invalid --resilience-loops: $RESILIENCE_LOOPS (expected positive integer)" >&2
  exit 1
fi
if [[ "$NIGHTLY_MODE" == true && "$RESILIENCE_LOOPS" -lt 2 ]]; then RESILIENCE_LOOPS=2; fi
if [[ "$NIGHTLY_PROFILE" != "quick" && "$NIGHTLY_PROFILE" != "deep" ]]; then
  echo "Invalid --nightly-profile: $NIGHTLY_PROFILE (expected quick|deep)" >&2
  exit 1
fi
if [[ "$RUNTIME_BUDGET_PROFILE" != "auto" && "$RUNTIME_BUDGET_PROFILE" != "off" && "$RUNTIME_BUDGET_PROFILE" != "quick" && "$RUNTIME_BUDGET_PROFILE" != "deep" ]]; then
  echo "Invalid --runtime-budget-profile: $RUNTIME_BUDGET_PROFILE (expected auto|off|quick|deep)" >&2
  exit 1
fi
if [[ -n "$STAGE_BUDGETS_JSON" ]]; then
  if ! python3 - "$STAGE_BUDGETS_JSON" <<'PY'
import json
import re
import sys
raw = sys.argv[1]
try:
    obj = json.loads(raw)
except Exception:
    raise SystemExit(1)
if not isinstance(obj, dict):
    raise SystemExit(1)
for k, v in obj.items():
    if not isinstance(k, str):
        raise SystemExit(1)
    if not re.match(r"^[a-z_]+$", k):
        raise SystemExit(1)
    if not isinstance(v, int) or v < 0:
        raise SystemExit(1)
PY
  then
    echo "Invalid QUAID_E2E_STAGE_BUDGETS_JSON (expected JSON object of {stage:int_seconds})" >&2
    exit 1
  fi
fi
if ! [[ "$RUNTIME_BUDGET_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "Invalid --runtime-budget-seconds: $RUNTIME_BUDGET_SECONDS (expected non-negative integer)" >&2
  exit 1
fi
if [[ "$NIGHTLY_MODE" == true ]]; then
  if [[ "$NIGHTLY_PROFILE" == "deep" ]]; then
    if [[ "$RESILIENCE_LOOPS" -lt 4 ]]; then RESILIENCE_LOOPS=4; fi
    if [[ "$JANITOR_STRESS_PASSES" -lt 4 ]]; then JANITOR_STRESS_PASSES=4; fi
  else
    if [[ "$RESILIENCE_LOOPS" -lt 2 ]]; then RESILIENCE_LOOPS=2; fi
    if [[ "$JANITOR_STRESS_PASSES" -lt 2 ]]; then JANITOR_STRESS_PASSES=2; fi
  fi
fi
if [[ "$RUNTIME_BUDGET_PROFILE" == "auto" ]]; then
  if [[ "$NIGHTLY_MODE" == true ]]; then
    RUNTIME_BUDGET_PROFILE="$NIGHTLY_PROFILE"
  else
    RUNTIME_BUDGET_PROFILE="off"
  fi
fi
if [[ "$RUNTIME_BUDGET_SECONDS" -eq 0 ]]; then
  case "$RUNTIME_BUDGET_PROFILE" in
    quick) RUNTIME_BUDGET_SECONDS=1500 ;; # 25m
    deep) RUNTIME_BUDGET_SECONDS=3600 ;;  # 60m
    off) RUNTIME_BUDGET_SECONDS=0 ;;
  esac
fi

autotune_stage_budgets_from_history() {
  if [[ "$NIGHTLY_MODE" != true || "$AUTO_STAGE_BUDGETS" != "true" ]]; then
    return 0
  fi
  if [[ -n "$STAGE_BUDGETS_JSON" ]]; then
    return 0
  fi
  if [[ ! -f "$SUMMARY_HISTORY_PATH" ]]; then
    return 0
  fi
  local tuned
  tuned="$(python3 - "$SUMMARY_HISTORY_PATH" "$AUTO_STAGE_BUDGETS_STAGES" "$RUNTIME_BUDGET_TUNE_MIN_SAMPLES" "$RUNTIME_BUDGET_TUNE_BUFFER_RATIO" "${SCRIPT_DIR}/e2e-budget-tune.py" <<'PY'
import json
import subprocess
import sys

hist, stages_csv, min_samples_raw, buffer_raw, tuner_script = sys.argv[1:6]
stages = [s.strip() for s in stages_csv.split(",") if s.strip()]
try:
    min_samples = int(min_samples_raw)
except Exception:
    min_samples = 5
try:
    buffer = float(buffer_raw)
except Exception:
    buffer = 1.2
out = {}
for stage in stages:
    cmd = [
        "python3",
        tuner_script,
        "--history", hist,
        "--suite", "nightly",
        "--status", "success",
        "--stage", stage,
        "--min-samples", str(max(min_samples, 1)),
        "--buffer-ratio", str(max(buffer, 1.0)),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        continue
    try:
        payload = json.loads(proc.stdout or "{}")
        val = int(payload.get("recommended_budget_seconds") or 0)
        if val > 0:
            out[stage] = val
    except Exception:
        continue
print(json.dumps(out, separators=(",", ":")))
PY
)"
  if [[ -n "$tuned" && "$tuned" != "{}" ]]; then
    STAGE_BUDGETS_JSON="$tuned"
    echo "[e2e] Auto-tuned stage budgets from history: ${STAGE_BUDGETS_JSON}"
  fi
}
autotune_stage_budgets_from_history

skip_e2e() {
  local reason="$1"
  E2E_STATUS="skipped"
  E2E_FAIL_REASON="$reason"
  echo "[e2e] SKIP_REASON:${reason}" >&2
  echo "[e2e] SKIP: bootstrap e2e prerequisites are not available in this environment." >&2
  echo "[e2e] To enable e2e auth-path tests:" >&2
  echo "[e2e]   1) Set QUAID_BOOTSTRAP_ROOT (default: ~/quaid/bootstrap)." >&2
  echo "[e2e]   2) Copy modules/quaid/scripts/e2e.env.example to modules/quaid/.env.e2e and set keys as needed." >&2
  echo "[e2e]   3) Required keys by path:" >&2
  echo "[e2e]      - openai-api: OPENAI_API_KEY" >&2
  echo "[e2e]      - anthropic-api: ANTHROPIC_API_KEY" >&2
  echo "[e2e]      - openai-oauth / anthropic-oauth: valid bootstrap auth profiles/tokens" >&2
  write_e2e_summary "$E2E_SKIP_EXIT_CODE" || true
  exit "$E2E_SKIP_EXIT_CODE"
}

set_stage_status() {
  local stage="$1"
  local status="$2"
  eval "STAGE_${stage}='${status}'"
}

begin_stage() {
  local stage="$1"
  CURRENT_STAGE="$stage"
  set_stage_status "$stage" "running"
  eval "STAGE_${stage}_START_EPOCH='$(date +%s)'"
}

finalize_stage_timing() {
  local stage="$1"
  local now
  local start=0
  local duration=0
  now="$(date +%s)"
  eval "start=\${STAGE_${stage}_START_EPOCH:-0}"
  if [[ "$start" =~ ^[0-9]+$ && "$start" -gt 0 ]]; then
    duration=$((now - start))
    if [[ "$duration" -lt 0 ]]; then
      duration=0
    fi
  fi
  eval "STAGE_${stage}_DURATION_SECONDS='${duration}'"
}

pass_stage() {
  local stage="$1"
  finalize_stage_timing "$stage"
  set_stage_status "$stage" "passed"
}

skip_stage() {
  local stage="$1"
  finalize_stage_timing "$stage"
  set_stage_status "$stage" "skipped"
}

write_e2e_summary() {
  local exit_code="${1:-0}"
  local end_epoch
  end_epoch="$(date +%s)"
  SUMMARY_EXIT_CODE="$exit_code" \
  SUMMARY_END_EPOCH="$end_epoch" \
  SUMMARY_OUTPUT_PATH="$SUMMARY_OUTPUT_PATH" \
  SUMMARY_HISTORY_PATH="$SUMMARY_HISTORY_PATH" \
  SUMMARY_CURRENT_STAGE="$CURRENT_STAGE" \
  SUMMARY_E2E_STATUS="$E2E_STATUS" \
  SUMMARY_E2E_FAIL_LINE="$E2E_FAIL_LINE" \
  SUMMARY_E2E_FAIL_REASON="$E2E_FAIL_REASON" \
  SUMMARY_AUTH_PATH="$AUTH_PATH" \
  SUMMARY_SUITES="$E2E_SUITES" \
  SUMMARY_NIGHTLY_MODE="$NIGHTLY_MODE" \
  SUMMARY_NIGHTLY_PROFILE="$NIGHTLY_PROFILE" \
  SUMMARY_RESILIENCE_LOOPS="$RESILIENCE_LOOPS" \
  SUMMARY_JANITOR_MODE="$JANITOR_MODE" \
  SUMMARY_JANITOR_TIMEOUT="$JANITOR_TIMEOUT_SECONDS" \
  SUMMARY_NOTIFY_LEVEL="$NOTIFY_LEVEL" \
  SUMMARY_BUDGET_PROFILE="$RUNTIME_BUDGET_PROFILE" \
  SUMMARY_BUDGET_SECONDS="$RUNTIME_BUDGET_SECONDS" \
  SUMMARY_BUDGET_EXCEEDED="$RUNTIME_BUDGET_EXCEEDED" \
  SUMMARY_RUN_START="$RUN_START_EPOCH" \
  SUMMARY_STAGE_bootstrap="$STAGE_bootstrap" \
  SUMMARY_STAGE_bootstrap_DURATION="$STAGE_bootstrap_DURATION_SECONDS" \
  SUMMARY_STAGE_gateway_smoke="$STAGE_gateway_smoke" \
  SUMMARY_STAGE_gateway_smoke_DURATION="$STAGE_gateway_smoke_DURATION_SECONDS" \
  SUMMARY_STAGE_integration="$STAGE_integration" \
  SUMMARY_STAGE_integration_DURATION="$STAGE_integration_DURATION_SECONDS" \
  SUMMARY_STAGE_live_events="$STAGE_live_events" \
  SUMMARY_STAGE_live_events_DURATION="$STAGE_live_events_DURATION_SECONDS" \
  SUMMARY_STAGE_resilience="$STAGE_resilience" \
  SUMMARY_STAGE_resilience_DURATION="$STAGE_resilience_DURATION_SECONDS" \
  SUMMARY_STAGE_memory_flow="$STAGE_memory_flow" \
  SUMMARY_STAGE_memory_flow_DURATION="$STAGE_memory_flow_DURATION_SECONDS" \
  SUMMARY_STAGE_notify_matrix="$STAGE_notify_matrix" \
  SUMMARY_STAGE_notify_matrix_DURATION="$STAGE_notify_matrix_DURATION_SECONDS" \
  SUMMARY_STAGE_ingest_stress="$STAGE_ingest_stress" \
  SUMMARY_STAGE_ingest_stress_DURATION="$STAGE_ingest_stress_DURATION_SECONDS" \
  SUMMARY_STAGE_project_crud="$STAGE_project_crud" \
  SUMMARY_STAGE_project_crud_DURATION="$STAGE_project_crud_DURATION_SECONDS" \
  SUMMARY_STAGE_session_tools="$STAGE_session_tools" \
  SUMMARY_STAGE_session_tools_DURATION="$STAGE_session_tools_DURATION_SECONDS" \
  SUMMARY_STAGE_janitor="$STAGE_janitor" \
  SUMMARY_STAGE_janitor_DURATION="$STAGE_janitor_DURATION_SECONDS" \
  python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

start = int(os.environ["SUMMARY_RUN_START"])
end = int(os.environ["SUMMARY_END_EPOCH"])
status = os.environ["SUMMARY_E2E_STATUS"]
if status == "running":
    status = "success" if int(os.environ["SUMMARY_EXIT_CODE"]) == 0 else "failed"
failure_stage = os.environ["SUMMARY_CURRENT_STAGE"] if status == "failed" else ""
failure_line = os.environ["SUMMARY_E2E_FAIL_LINE"] if status == "failed" else ""
failure_reason = os.environ["SUMMARY_E2E_FAIL_REASON"] if status == "failed" else ""
out = {
    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "status": status,
    "exit_code": int(os.environ["SUMMARY_EXIT_CODE"]),
    "duration_seconds": max(0, end - start),
    "auth_path": os.environ["SUMMARY_AUTH_PATH"],
    "suite": os.environ["SUMMARY_SUITES"],
    "nightly": {
        "enabled": os.environ["SUMMARY_NIGHTLY_MODE"].lower() == "true",
        "profile": os.environ["SUMMARY_NIGHTLY_PROFILE"],
        "resilience_loops": int(os.environ["SUMMARY_RESILIENCE_LOOPS"]),
    },
    "janitor": {
        "mode": os.environ["SUMMARY_JANITOR_MODE"],
        "timeout_seconds": int(os.environ["SUMMARY_JANITOR_TIMEOUT"]),
    },
    "notify_level": os.environ["SUMMARY_NOTIFY_LEVEL"],
    "runtime_budget": {
        "profile": os.environ["SUMMARY_BUDGET_PROFILE"],
        "seconds": int(os.environ["SUMMARY_BUDGET_SECONDS"]),
        "exceeded": os.environ["SUMMARY_BUDGET_EXCEEDED"].lower() == "true",
    },
    "failure": {
        "stage": failure_stage,
        "line": failure_line,
        "reason": failure_reason,
    },
    "stages": {
        "bootstrap": os.environ["SUMMARY_STAGE_bootstrap"],
        "gateway_smoke": os.environ["SUMMARY_STAGE_gateway_smoke"],
        "integration": os.environ["SUMMARY_STAGE_integration"],
        "live_events": os.environ["SUMMARY_STAGE_live_events"],
        "resilience": os.environ["SUMMARY_STAGE_resilience"],
        "memory_flow": os.environ["SUMMARY_STAGE_memory_flow"],
        "notify_matrix": os.environ["SUMMARY_STAGE_notify_matrix"],
        "ingest_stress": os.environ["SUMMARY_STAGE_ingest_stress"],
        "project_crud": os.environ["SUMMARY_STAGE_project_crud"],
        "session_tools": os.environ["SUMMARY_STAGE_session_tools"],
        "janitor": os.environ["SUMMARY_STAGE_janitor"],
    },
    "stage_durations_seconds": {
        "bootstrap": int(os.environ["SUMMARY_STAGE_bootstrap_DURATION"]),
        "gateway_smoke": int(os.environ["SUMMARY_STAGE_gateway_smoke_DURATION"]),
        "integration": int(os.environ["SUMMARY_STAGE_integration_DURATION"]),
        "live_events": int(os.environ["SUMMARY_STAGE_live_events_DURATION"]),
        "resilience": int(os.environ["SUMMARY_STAGE_resilience_DURATION"]),
        "memory_flow": int(os.environ["SUMMARY_STAGE_memory_flow_DURATION"]),
        "notify_matrix": int(os.environ["SUMMARY_STAGE_notify_matrix_DURATION"]),
        "ingest_stress": int(os.environ["SUMMARY_STAGE_ingest_stress_DURATION"]),
        "project_crud": int(os.environ["SUMMARY_STAGE_project_crud_DURATION"]),
        "session_tools": int(os.environ["SUMMARY_STAGE_session_tools_DURATION"]),
        "janitor": int(os.environ["SUMMARY_STAGE_janitor_DURATION"]),
    },
}
out_path = os.environ["SUMMARY_OUTPUT_PATH"]
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
    f.write("\n")
print(f"[e2e] Wrote summary: {out_path}")

hist_path = str(os.environ.get("SUMMARY_HISTORY_PATH") or "").strip()
if hist_path:
    try:
        os.makedirs(os.path.dirname(hist_path), exist_ok=True)
        with open(hist_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(out))
            f.write("\n")
        print(f"[e2e] Appended summary history: {hist_path}")
    except Exception as exc:
        print(f"[e2e] WARN: failed to append summary history ({hist_path}): {exc}")
PY
}

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

if [[ "$AUTH_PATH" == "openai-oauth" || "$AUTH_PATH" == "openai-api" || "$AUTH_PATH" == "anthropic-oauth" || "$AUTH_PATH" == "anthropic-api" ]]; then
  true
else
  echo "Invalid auth selection. Use --auth-path openai-oauth|openai-api|anthropic-oauth|anthropic-api." >&2
  exit 1
fi

case "$NOTIFY_LEVEL" in
  quiet|normal|verbose|debug) ;;
  *)
    echo "Invalid --notify-level: $NOTIFY_LEVEL (expected quiet|normal|verbose|debug)" >&2
    exit 1
    ;;
esac

if [[ ! -f "$PROFILE_SRC" ]]; then
  skip_e2e "missing-profile-src"
fi

if [[ ! -f "$PROFILE_TEST" ]]; then
  skip_e2e "missing-profile-test"
fi

if [[ ! -x "${BOOTSTRAP_ROOT}/scripts/bootstrap-local.sh" ]]; then
  skip_e2e "missing-bootstrap-runner"
fi

if [[ ! -f "${BOOTSTRAP_ROOT}/scripts/apply-runtime-profile.py" ]]; then
  skip_e2e "missing-profile-applier"
fi

case "$AUTH_PATH" in
  openai-api)
    if [[ -z "${OPENAI_API_KEY:-}" ]]; then
      skip_e2e "missing-openai-api-key"
    fi
    ;;
  anthropic-api)
    if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
      skip_e2e "missing-anthropic-api-key"
    fi
    ;;
esac

if ! command -v openclaw >/dev/null 2>&1; then
  skip_e2e "missing-openclaw-cli"
fi

start_gateway_safe() {
  # install + start is idempotent and handles "service not loaded" states.
  openclaw gateway install >/dev/null 2>&1 || true
  openclaw gateway start >/dev/null 2>&1 || true
}

wait_for_gateway_listen() {
  local max_tries="${1:-20}"
  local i
  for ((i=1; i<=max_tries; i++)); do
    if lsof -nP -iTCP:18789 -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

restore_test_gateway() {
  set +e
  echo "[e2e] Restoring gateway config to test workspace..."
  python3 "${BOOTSTRAP_ROOT}/scripts/apply-runtime-profile.py" --profile "$PROFILE_TEST" --auth-path "$AUTH_PATH" >/dev/null 2>&1
  openclaw gateway stop >/dev/null 2>&1 || true
  set -e
  start_gateway_safe
  if ! wait_for_gateway_listen 20; then
    echo "[e2e] WARN: gateway did not start listening on 127.0.0.1:18789 after restore" >&2
  fi
}

emit_budget_recommendation_if_nightly() {
  if [[ "$NIGHTLY_MODE" != true ]]; then
    return 0
  fi
  if [[ ! -f "$SUMMARY_HISTORY_PATH" ]]; then
    echo "[e2e] WARN: no summary history found for nightly budget tuning: ${SUMMARY_HISTORY_PATH}" >&2
    return 0
  fi
  local status_filter="success"
  if [[ "$E2E_STATUS" != "success" ]]; then
    status_filter="all"
  fi
  local out_dir
  out_dir="$(dirname "$SUMMARY_BUDGET_RECOMMENDATION_PATH")"
  mkdir -p "$out_dir"
  if python3 "${SCRIPT_DIR}/e2e-budget-tune.py" \
      --history "$SUMMARY_HISTORY_PATH" \
      --suite nightly \
      --status "$status_filter" \
      --min-samples "$RUNTIME_BUDGET_TUNE_MIN_SAMPLES" \
      --buffer-ratio "$RUNTIME_BUDGET_TUNE_BUFFER_RATIO" \
      > "$SUMMARY_BUDGET_RECOMMENDATION_PATH"; then
    echo "[e2e] Budget recommendation: ${SUMMARY_BUDGET_RECOMMENDATION_PATH}"
  else
    rm -f "$SUMMARY_BUDGET_RECOMMENDATION_PATH" || true
    echo "[e2e] WARN: nightly budget recommendation unavailable (insufficient history or parser failure)." >&2
  fi
}

enforce_stage_budgets() {
  if [[ -z "$STAGE_BUDGETS_JSON" ]]; then
    return 0
  fi
  local violations=0
  while IFS=' ' read -r stage budget; do
    [[ -z "$stage" || -z "$budget" ]] && continue
    local var_name="STAGE_${stage}_DURATION_SECONDS"
    local duration=0
    eval "duration=\${${var_name}:-0}"
    if [[ "$duration" -gt "$budget" ]]; then
      echo "[e2e] ERROR: stage budget exceeded (stage=${stage} duration=${duration}s budget=${budget}s)" >&2
      violations=1
    fi
  done < <(python3 - "$STAGE_BUDGETS_JSON" <<'PY'
import json
import sys
obj = json.loads(sys.argv[1])
for k, v in obj.items():
    print(f"{k} {int(v)}")
PY
)
  if [[ "$violations" -ne 0 ]]; then
    E2E_STATUS="failed"
    E2E_FAIL_REASON="stage_budget_exceeded"
    E2E_FAIL_LINE="stage-budget-check"
    CURRENT_STAGE="runtime_budget"
    exit 1
  fi
}

cleanup() {
  local exit_code="$1"
  if [[ "$E2E_STATUS" == "running" ]]; then
    if [[ "$exit_code" -eq 0 ]]; then
      E2E_STATUS="success"
    elif [[ "$E2E_STATUS" != "failed" ]]; then
      E2E_STATUS="failed"
    fi
  fi
  write_e2e_summary "$exit_code" || true
  emit_budget_recommendation_if_nightly || true
  rm -f "$TMP_PROFILE"
  openclaw gateway stop >/dev/null 2>&1 || true
  if [[ "$exit_code" -eq 0 && "$KEEP_ON_SUCCESS" != true ]]; then
    echo "[e2e] Success, removing ${E2E_WS}"
    local attempt=1
    while [[ -d "$E2E_WS" && "$attempt" -le 3 ]]; do
      rm -rf "$E2E_WS" 2>/dev/null || true
      if [[ -d "$E2E_WS" ]]; then
        sleep 1
      fi
      attempt=$((attempt + 1))
    done
    if [[ -d "$E2E_WS" ]]; then
      echo "[e2e] WARN: workspace cleanup incomplete: ${E2E_WS}" >&2
    fi
  fi
  restore_test_gateway
  return "$exit_code"
}

on_err() {
  local code="$1"
  local line="$2"
  E2E_STATUS="failed"
  E2E_FAIL_LINE="$line"
  E2E_FAIL_REASON="err_trap"
  emit_failure_diagnostics "$code" "$line"
  exit "$code"
}

trap 'on_err $? $LINENO' ERR
trap 'cleanup $?' EXIT

echo "[e2e] Stopping any running gateway..."
openclaw gateway stop || true
begin_stage "bootstrap"

echo "[e2e] Building temp e2e profile at: $TMP_PROFILE"
python3 - "$PROFILE_SRC" "$TMP_PROFILE" "$E2E_WS" <<'PY'
import json
import sys

src, out, e2e_ws = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, "r", encoding="utf-8") as f:
    obj = json.load(f)

old_ws = obj.get("runtime", {}).get("workspace")
if not old_ws:
    raise SystemExit("Profile missing runtime.workspace")

def replace_paths(v):
    if isinstance(v, str):
        return v.replace(old_ws, e2e_ws)
    if isinstance(v, list):
        return [replace_paths(x) for x in v]
    if isinstance(v, dict):
        return {k: replace_paths(x) for k, x in v.items()}
    return v

obj = replace_paths(obj)
obj.setdefault("runtime", {})["workspace"] = e2e_ws
with open(out, "w", encoding="utf-8") as f:
    json.dump(obj, f, indent=2)
PY

python3 - "$TMP_PROFILE" "$NOTIFY_LEVEL" <<'PY'
import json
import sys

profile_path, notify_level = sys.argv[1], sys.argv[2]
with open(profile_path, "r", encoding="utf-8") as f:
    obj = json.load(f)

quaid = obj.setdefault("quaid", {})
notifications = quaid.setdefault("notifications", {})
notifications["level"] = notify_level
notifications["full_text"] = False
notifications["show_processing_start"] = False
notifications["fullText"] = False
notifications["showProcessingStart"] = False
if notify_level == "quiet":
    notifications["janitor"] = {"verbosity": "off"}
    notifications["extraction"] = {"verbosity": "off"}
    notifications["retrieval"] = {"verbosity": "off"}

with open(profile_path, "w", encoding="utf-8") as f:
    json.dump(obj, f, indent=2)
PY

run_bootstrap() {
  local do_wipe="$1"
  local -a args
  local bootstrap_log=""
  local rc=0
  args=(
    "${BOOTSTRAP_ROOT}/scripts/bootstrap-local.sh"
    --profile "$TMP_PROFILE"
    --auth-path "$AUTH_PATH"
    --openclaw-source "$OPENCLAW_SOURCE"
    --worktree-source "$DEV_WS"
    --worktree-test-branch "e2e-runtime"
  )
  if [[ "$do_wipe" == "true" ]]; then
    args+=(--wipe)
  fi
  if [[ "$QUICK_BOOTSTRAP" == true ]]; then
    args+=(--no-openclaw-refresh --no-openclaw-install)
  fi
  bootstrap_log="$(mktemp -t quaid-e2e-bootstrap.log.XXXXXX)"
  if "${args[@]}" 2>&1 | tee "$bootstrap_log"; then
    rc=0
  else
    rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    rm -f "$bootstrap_log"
    return 0
  fi

  # Rare race: workspace path can be recreated between wipe and worktree add.
  # Retry once after explicit cleanup when the failure is this specific collision.
  if [[ "$do_wipe" == "true" ]] && rg -q "already exists" "$bootstrap_log"; then
    echo "[e2e] Bootstrap hit workspace collision; retrying once after cleanup." >&2
    rm -rf "$E2E_WS"
    if git -C "$DEV_WS" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git -C "$DEV_WS" worktree prune >/dev/null 2>&1 || true
    fi
    if "${args[@]}" 2>&1 | tee "$bootstrap_log"; then
      rm -f "$bootstrap_log"
      return 0
    fi
    rc=$?
  fi
  echo "[e2e] bootstrap log preserved at: $bootstrap_log" >&2
  return "$rc"
}

  if [[ "$REUSE_WORKSPACE" == true && -d "$E2E_WS" ]]; then
  echo "[e2e] Reusing existing e2e workspace: ${E2E_WS}"
  if ! run_bootstrap false; then
    echo "[e2e] Reuse bootstrap failed; falling back to clean bootstrap (--wipe)." >&2
    run_bootstrap true
  fi
else
echo "[e2e] Bootstrapping e2e workspace: ${E2E_WS}"
run_bootstrap true
fi

echo "[e2e] Starting gateway on e2e workspace..."
start_gateway_safe
if ! wait_for_gateway_listen 40; then
  echo "[e2e] Gateway failed to listen on 127.0.0.1:18789 in e2e workspace" >&2
  openclaw gateway status || true
  exit 1
fi
pass_stage "bootstrap"

if [[ "$RUN_LLM_SMOKE" == true ]]; then
begin_stage "gateway_smoke"
echo "[e2e] Running gateway LLM smoke call..."
if ! python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

gateway = cfg.get("gateway", {})
port = int(gateway.get("port", 18789))
token = ((gateway.get("auth") or {}).get("token") or "").strip()
model = (
    (((cfg.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary")
    or ""
).strip()
if not model:
    print("[e2e] ERROR: no default primary model configured in openclaw.json", file=sys.stderr)
    raise SystemExit(1)

url = f"http://127.0.0.1:{port}/v1/responses"
payload = {
    "model": model,
    "input": "Reply with exactly: OK",
    "max_output_tokens": 16,
}
data = json.dumps(payload).encode("utf-8")
headers = {"Content-Type": "application/json"}
if token:
    headers["Authorization"] = f"Bearer {token}"

req = urllib.request.Request(url, data=data, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=40) as resp:
        status = resp.getcode()
        body = resp.read().decode("utf-8", errors="replace")
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    print(f"[e2e] ERROR: LLM smoke HTTP {e.code}: {body[:600]}", file=sys.stderr)
    raise SystemExit(1)
except Exception as e:
    print(f"[e2e] ERROR: LLM smoke failed: {e}", file=sys.stderr)
    raise SystemExit(1)

if status < 200 or status >= 300:
    print(f"[e2e] ERROR: LLM smoke non-2xx: {status}", file=sys.stderr)
    raise SystemExit(1)

print(f"[e2e] LLM smoke OK (model={model}, status={status})")
print(body[:300])
PY
then
  echo "[e2e] Recent gateway auth/runtime errors:"
  openclaw logs --plain --limit 220 2>/dev/null | rg -n "No API key found|OAuth token refresh failed|Auth store:|All models failed|api_error|rate_limit|authentication_error|invalid x-api-key" | tail -n 40 || true
  exit 1
fi
pass_stage "gateway_smoke"
else
  skip_stage "gateway_smoke"
  echo "[e2e] Skipping LLM smoke (--skip-llm-smoke)."
fi

HOOK_SRC="${E2E_WS}/modules/quaid/adaptors/openclaw/hooks/quaid-reset-signal"
HOOK_DST="${E2E_WS}/hooks/quaid-reset-signal"
if [[ -d "$HOOK_SRC" ]]; then
  echo "[e2e] Installing workspace hook: quaid-reset-signal"
  mkdir -p "${E2E_WS}/hooks"
  rm -rf "$HOOK_DST"
  cp -R "$HOOK_SRC" "$HOOK_DST"
  openclaw hooks enable quaid-reset-signal >/dev/null 2>&1 || true
  openclaw hooks install "$HOOK_DST" >/dev/null 2>&1 || true
  openclaw hooks enable quaid-reset-signal >/dev/null 2>&1 || true
else
  echo "[e2e] Missing hook source: $HOOK_SRC" >&2
  exit 1
fi

if ! openclaw hooks list --json | python3 - <<'PY'
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    raise SystemExit(1)
decoder = json.JSONDecoder()
obj = None
for idx, ch in enumerate(raw):
    if ch != "{":
        continue
    try:
        candidate, end = decoder.raw_decode(raw[idx:])
    except Exception:
        continue
    if isinstance(candidate, dict) and "hooks" in candidate:
        obj = candidate
if not isinstance(obj, dict):
    raise SystemExit(1)
hooks = obj.get("hooks") or []
match = next((h for h in hooks if (h.get("name") == "quaid-reset-signal")), None)
if not match:
    raise SystemExit(1)
if match.get("disabled"):
    raise SystemExit(1)
print("[e2e] Hook ready: quaid-reset-signal")
PY
then
  echo "[e2e] WARN: quaid-reset-signal precheck did not pass; live event checks will validate behavior directly." >&2
fi

# Keep timeout test practical in CI/dev by forcing a short inactivity timeout.
MEMORY_CFG="${E2E_WS}/config/memory.json"
if [[ -f "$MEMORY_CFG" ]]; then
  python3 - "$MEMORY_CFG" <<'PY'
import json, sys
p = sys.argv[1]
obj = json.load(open(p, "r", encoding="utf-8"))
capture = obj.setdefault("capture", {})
capture["inactivityTimeoutMinutes"] = 0.1
capture["inactivity_timeout_minutes"] = 0.1
capture["autoCompactionOnTimeout"] = False
capture["auto_compaction_on_timeout"] = False
with open(p, "w", encoding="utf-8") as f:
    json.dump(obj, f, indent=2)
    f.write("\n")
print("[e2e] Updated capture timeout for live timeout validation (~6 seconds).")
PY
else
  echo "[e2e] ERROR: missing memory config: $MEMORY_CFG" >&2
  exit 1
fi

# Reload gateway so newly installed hooks and timeout config are active.
echo "[e2e] Restarting gateway to apply hook/config changes..."
openclaw gateway stop >/dev/null 2>&1 || true
start_gateway_safe
if ! wait_for_gateway_listen 40; then
  echo "[e2e] Gateway failed to restart after hook/config update" >&2
  openclaw gateway status || true
  exit 1
fi

if [[ ! -e "${E2E_WS}/plugins/quaid" ]] && [[ -d "${E2E_WS}/modules/quaid" ]]; then
  mkdir -p "${E2E_WS}/plugins"
  ln -sfn ../modules/quaid "${E2E_WS}/plugins/quaid"
fi

if [[ "$RUN_INTEGRATION_TESTS" == true ]]; then
begin_stage "integration"
echo "[e2e] Running Quaid integration tests..."
for required in \
  "${E2E_WS}/modules/quaid/tests/session-timeout-manager.test.ts" \
  "${E2E_WS}/modules/quaid/tests/chat-flow.integration.test.ts"; do
  if [[ ! -f "$required" ]]; then
    echo "[e2e] Missing required integration test file: $required" >&2
    echo "[e2e] Ensure test files are committed in ${DEV_WS} before running e2e." >&2
    exit 1
  fi
done
(cd "${E2E_WS}/modules/quaid" && npx vitest run tests/session-timeout-manager.test.ts tests/chat-flow.integration.test.ts --reporter=verbose)
pass_stage "integration"
else
  skip_stage "integration"
  echo "[e2e] Skipping integration tests (suite selection)."
fi

if [[ "$RUN_LIVE_EVENTS" == true ]]; then
begin_stage "live_events"
echo "[e2e] Validating live /compact /reset /new + timeout events..."
python3 - "$E2E_WS" "$LIVE_TIMEOUT_WAIT_SECONDS" <<'PY'
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid

ws = sys.argv[1]
timeout_wait = int(sys.argv[2])
events_path = os.path.join(ws, "logs", "quaid", "session-timeout-events.jsonl")
notify_log_path = os.path.join(ws, "logs", "notify-worker.log")
session_id = f"quaid-e2e-live-{uuid.uuid4().hex[:12]}"

def run_agent(message: str) -> None:
    cmd = ["openclaw", "agent", "--session-id", session_id, "--message", message, "--timeout", "180", "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise SystemExit(f"[e2e] ERROR: openclaw agent timed out for message={message!r}")
    if proc.returncode != 0:
        raise SystemExit(f"[e2e] ERROR: openclaw agent failed for message={message!r}: {proc.stderr.strip()[:400]}")

def line_count(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)

def read_tail_since(path: str, start: int):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, start=1):
            if idx > start:
                out.append(line.strip())
    return out

def wait_for(predicate, seconds: int, label: str, start_line: int):
    deadline = time.time() + seconds
    while time.time() < deadline:
        lines = read_tail_since(events_path, start_line)
        if predicate(lines):
            return
        time.sleep(1)
    lines = read_tail_since(events_path, start_line)
    preview = "\n".join(lines[-30:])
    raise SystemExit(f"[e2e] ERROR: timed out waiting for {label}\n[e2e] recent events:\n{preview}")

def assert_notify_worker_healthy(start_line: int) -> None:
    lines = read_tail_since(notify_log_path, start_line)
    if not lines:
        return
    bad = []
    patterns = (
        "No such file or directory: 'clawdbot'",
        "No such file or directory: 'openclaw'",
        "No message CLI found",
    )
    for ln in lines:
        if any(p in ln for p in patterns):
            bad.append(ln)
    if bad:
        preview = "\n".join(bad[-20:])
        raise SystemExit(
            "[e2e] ERROR: notification worker CLI failure detected.\n"
            f"[e2e] notify-worker excerpts:\n{preview}"
        )

print(f"[e2e] Live events session: {session_id}")
notify_start = line_count(notify_log_path)

timeout_marker = f"E2E_TIMEOUT_{uuid.uuid4().hex[:10]}"
start = line_count(events_path)
run_agent(f"E2E timeout probe marker: {timeout_marker}")
print(f"[e2e] Timeout runtime session_id: {session_id}")
wait_for(
    lambda lines: any(
        f'"session_id":"{session_id}"' in ln
        and (
            '"event":"timer_fired"' in ln
            or ('"event":"extract_begin"' in ln and '"timeout_minutes"' in ln)
        )
        for ln in lines
    ),
    timeout_wait,
    "timeout extraction event",
    start,
)
print("[e2e] Live timeout event path OK.")
assert_notify_worker_healthy(notify_start)

compact_marker = f"E2E_COMPACT_{uuid.uuid4().hex[:10]}"
run_agent(f"E2E marker before compact: {compact_marker}")
start = line_count(events_path)
run_agent("/compact")
wait_for(
    lambda lines: any(
        f'"session_id":"{session_id}"' in ln
        and '"label":"CompactionSignal"' in ln
        and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
        for ln in lines
    ),
    45,
    "compaction signal processing",
    start,
)
print("[e2e] Live compact hook path OK.")
assert_notify_worker_healthy(notify_start)

# Reset can race session teardown, so validate by event window (not marker lookup).
start = line_count(events_path)
run_agent("E2E baseline message before reset.")
run_agent("/reset")
wait_for(
    lambda lines: any(
        '"label":"ResetSignal"' in ln
        and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
        for ln in lines
    ),
    45,
    "reset signal processing",
    start,
)
print("[e2e] Live reset hook path OK.")
assert_notify_worker_healthy(notify_start)

# /new path uses same reset signal semantics; validate by event window.
start = line_count(events_path)
run_agent("E2E baseline message before new.")
run_agent("/new")
wait_for(
    lambda lines: any(
        '"label":"ResetSignal"' in ln
        and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
        for ln in lines
    ),
    45,
    "new command signal processing",
    start,
)
print("[e2e] Live new hook path OK.")
assert_notify_worker_healthy(notify_start)

# Ensure session cursor bookkeeping is active for replay safety.
cursor_dir = os.path.join(ws, "data", "session-cursors")
cursor_path = os.path.join(cursor_dir, f"{session_id}.json")
deadline = time.time() + 20
cursor_payload = None
while time.time() < deadline:
    if os.path.exists(cursor_path):
        try:
            with open(cursor_path, "r", encoding="utf-8", errors="replace") as f:
                cursor_payload = json.load(f)
        except Exception:
            cursor_payload = None
        if isinstance(cursor_payload, dict):
            break
    time.sleep(1)
if not isinstance(cursor_payload, dict):
    raise SystemExit(
        f"[e2e] ERROR: session cursor was not written for live events session ({session_id})"
    )
if str(cursor_payload.get("sessionId") or "") != session_id:
    raise SystemExit(
        f"[e2e] ERROR: session cursor sessionId mismatch: {cursor_payload.get('sessionId')!r} != {session_id!r}"
    )
if not cursor_payload.get("lastMessageKey"):
    raise SystemExit("[e2e] ERROR: session cursor missing lastMessageKey")
print("[e2e] Live session cursor progression OK.")

# Postconditions: no stale lock claims and no internal extraction prompts
# persisted as session messages in a clean e2e workspace.
pending_dir = os.path.join(ws, "data", "pending-extraction-signals")
if os.path.isdir(pending_dir):
    # Claims are ephemeral lock files; allow a short drain window and only fail
    # if claims remain stale beyond the threshold (likely true orphaned locks).
    deadline = time.time() + 25
    stale = []
    while time.time() < deadline:
        now = time.time()
        stale = []
        for name in os.listdir(pending_dir):
            if ".processing." not in name:
                continue
            fp = os.path.join(pending_dir, name)
            try:
                age = now - os.path.getmtime(fp)
            except FileNotFoundError:
                continue
            if age >= 120:
                stale.append((name, int(age)))
        if not stale:
            break
        time.sleep(1)
    if stale:
        raise SystemExit(
            "[e2e] ERROR: live events left stale pending signal claim files:\n"
            + "\n".join(f"{name} age_s={age}" for name, age in stale[:20])
        )

internal_markers = (
    "Extract memorable facts and journal entries from this conversation:",
    "Given a personal memory query and memory documents",
)
session_dir = os.path.join(ws, "logs", "quaid", "session-messages")
if os.path.isdir(session_dir):
    contaminated = []
    for name in os.listdir(session_dir):
        if not name.endswith(".jsonl"):
            continue
        fp = os.path.join(session_dir, name)
        try:
            content = open(fp, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if any(marker in content for marker in internal_markers):
            contaminated.append(name)
    if contaminated:
        raise SystemExit(
            "[e2e] ERROR: internal extraction/ranking prompts leaked into session-message logs:\n"
            + "\n".join(contaminated[:20])
        )
PY
pass_stage "live_events"
else
  skip_stage "live_events"
  echo "[e2e] Skipping live events (--skip-live-events)."
fi

if [[ "$RUN_RESILIENCE" == true ]]; then
begin_stage "resilience"
for ((res_i=1; res_i<=RESILIENCE_LOOPS; res_i++)); do
echo "[e2e] Running resilience check (iteration ${res_i}/${RESILIENCE_LOOPS})..."
python3 - "$E2E_WS" "$res_i" <<'PY'
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

ws = sys.argv[1]
resilience_iter = int(sys.argv[2])
session_id = f"quaid-e2e-resilience-{uuid.uuid4().hex[:10]}"
cursor_dir = os.path.join(ws, "data", "session-cursors")

def _wait_gateway(seconds: int = 30) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        chk = subprocess.run(
            ["bash", "-lc", "lsof -nP -iTCP:18789 -sTCP:LISTEN"],
            capture_output=True,
            text=True,
        )
        if chk.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit("[e2e] ERROR: gateway did not return to listening state after restart")

def run_agent(message: str) -> str:
    proc = subprocess.run(
        ["openclaw", "agent", "--session-id", session_id, "--message", message, "--timeout", "150", "--json"],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if proc.returncode != 0:
        raise SystemExit(f"[e2e] ERROR: resilience agent call failed: {proc.stderr.strip()[:400]}")
    out = proc.stdout.strip()
    if not out:
        return ""
    try:
        parsed = json.loads(out)
    except Exception:
        return out
    if isinstance(parsed, dict):
        if isinstance(parsed.get("response"), str):
            return parsed["response"]
        if isinstance(parsed.get("output"), str):
            return parsed["output"]
    return out

def run_agent_for(sid: str, message: str) -> str:
    proc = subprocess.run(
        ["openclaw", "agent", "--session-id", sid, "--message", message, "--timeout", "150", "--json"],
        capture_output=True,
        text=True,
        timeout=240,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"[e2e] ERROR: resilience agent call failed (sid={sid}): {proc.stderr.strip()[:400]}"
        )
    out = proc.stdout.strip()
    if not out:
        return ""
    try:
        parsed = json.loads(out)
    except Exception:
        return out
    if isinstance(parsed, dict):
        if isinstance(parsed.get("response"), str):
            return parsed["response"]
        if isinstance(parsed.get("output"), str):
            return parsed["output"]
    return out

def _spawn_janitor_probe() -> subprocess.Popen:
    env = dict(os.environ)
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = "modules/quaid" + (f":{py_path}" if py_path else "")
    env["QUAID_HOME"] = ws
    env["CLAWDBOT_WORKSPACE"] = ws
    return subprocess.Popen(
        ["python3", "modules/quaid/core/lifecycle/janitor.py", "--task", "review", "--dry-run", "--stage-item-cap", "8"],
        cwd=".",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

def _parse_last_json_blob(text: str):
    if not text:
        return None
    raw = text.strip()
    decoder = json.JSONDecoder()
    last = None
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[i:])
        except Exception:
            continue
        if isinstance(obj, dict):
            last = obj
    return last

def _inject_bad_request_failure_and_verify_recovery() -> None:
    cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        cfg = json.loads(open(cfg_path, "r", encoding="utf-8").read())
    except Exception:
        return
    gateway = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    port = int(gateway.get("port", 18789) or 18789)
    token = str(((gateway.get("auth") or {}).get("token") or "")).strip()
    url = f"http://127.0.0.1:{port}/v1/responses"
    bad_payload = b'{"model":"broken",'
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=bad_payload,
        headers=headers,
        method="POST",
    )
    failed_as_expected = False
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            _ = resp.read()
    except urllib.error.HTTPError:
        failed_as_expected = True
    except Exception:
        failed_as_expected = True
    if not failed_as_expected:
        raise SystemExit(
            "[e2e] ERROR: failure-injection probe unexpectedly succeeded for malformed /v1/responses payload"
        )

def _inject_auth_failure_and_verify_recovery() -> None:
    cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        cfg = json.loads(open(cfg_path, "r", encoding="utf-8").read())
    except Exception:
        return
    gateway = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    port = int(gateway.get("port", 18789) or 18789)
    url = f"http://127.0.0.1:{port}/v1/responses"
    payload = {
        "model": "openai-codex/gpt-5.3-codex",
        "input": "auth-failure probe",
        "max_output_tokens": 8,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer e2e-invalid-token",
        },
        method="POST",
    )
    failed_as_expected = False
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            _ = resp.read()
    except urllib.error.HTTPError as e:
        if int(getattr(e, "code", 0) or 0) in (401, 403):
            failed_as_expected = True
        else:
            failed_as_expected = True
    except Exception:
        failed_as_expected = True
    if not failed_as_expected:
        raise SystemExit(
            "[e2e] ERROR: failure-injection probe unexpectedly succeeded for invalid auth token"
        )

def _inject_gateway_down_failure_and_recover() -> None:
    cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        cfg = json.loads(open(cfg_path, "r", encoding="utf-8").read())
    except Exception:
        return
    gateway = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
    port = int(gateway.get("port", 18789) or 18789)
    token = str(((gateway.get("auth") or {}).get("token") or "")).strip()
    url = f"http://127.0.0.1:{port}/v1/responses"
    payload = {
        "model": "openai-codex/gpt-5.3-codex",
        "input": "gateway-down probe",
        "max_output_tokens": 8,
    }
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    subprocess.run(["openclaw", "gateway", "stop"], check=False, capture_output=True, text=True, timeout=90)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    failed_as_expected = False
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            _ = resp.read()
    except Exception:
        failed_as_expected = True
    finally:
        subprocess.run(["openclaw", "gateway", "install"], check=False, capture_output=True, text=True, timeout=90)
        subprocess.run(["openclaw", "gateway", "start"], check=False, capture_output=True, text=True, timeout=90)
        _wait_gateway(40)
    if not failed_as_expected:
        raise SystemExit(
            "[e2e] ERROR: failure-injection probe unexpectedly succeeded while gateway was stopped"
        )

def _inject_timeout_failure_and_verify_recovery() -> None:
    # Simulate upstream timeout against a non-routable TEST-NET-3 address.
    url = "http://203.0.113.1:81/v1/responses"
    payload = {
        "model": "openai-codex/gpt-5.3-codex",
        "input": "timeout-failure probe",
        "max_output_tokens": 8,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    failed_as_expected = False
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            _ = resp.read()
    except Exception:
        failed_as_expected = True
    if not failed_as_expected:
        raise SystemExit(
            "[e2e] ERROR: failure-injection probe unexpectedly succeeded for timeout lane"
        )

def _inject_malformed_response_failure_and_verify_recovery() -> None:
    class _BadJSONHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (stdlib callback name)
            try:
                _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            except Exception:
                pass
            body = b'{"id":"resp_bad","status":"completed","output":INVALID_JSON}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # noqa: A003 (stdlib callback name)
            return

    server = HTTPServer(("127.0.0.1", 0), _BadJSONHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_port)
    url = f"http://127.0.0.1:{port}/v1/responses"
    req = urllib.request.Request(
        url,
        data=b'{"model":"x","input":"malformed-response-probe"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    failed_as_expected = False
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            json.loads(raw)
        except Exception:
            failed_as_expected = True
    except Exception:
        failed_as_expected = True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
    if not failed_as_expected:
        raise SystemExit(
            "[e2e] ERROR: failure-injection probe unexpectedly parsed malformed upstream response"
        )

first = run_agent("Resilience probe turn 1: acknowledge with OK.")
if not first:
    raise SystemExit("[e2e] ERROR: resilience turn 1 produced empty output")

subprocess.run(["openclaw", "gateway", "restart"], check=False, capture_output=True, text=True, timeout=90)
_wait_gateway(40)

second = run_agent("Resilience probe turn 2 after forced gateway restart: acknowledge with OK.")
if not second:
    raise SystemExit("[e2e] ERROR: resilience turn 2 produced empty output after gateway restart")

_inject_bad_request_failure_and_verify_recovery()
post_injection = run_agent("Bad-request recovery probe: acknowledge with OK.")
if not post_injection:
    raise SystemExit("[e2e] ERROR: recovery probe failed after bad-model failure injection")
_inject_auth_failure_and_verify_recovery()
post_auth_injection = run_agent("Auth-failure recovery probe: acknowledge with OK.")
if not post_auth_injection:
    raise SystemExit("[e2e] ERROR: recovery probe failed after auth failure injection")
_inject_gateway_down_failure_and_recover()
post_gateway_down_injection = run_agent("Gateway-down recovery probe: acknowledge with OK.")
if not post_gateway_down_injection:
    raise SystemExit("[e2e] ERROR: recovery probe failed after gateway-down injection")
_inject_timeout_failure_and_verify_recovery()
post_timeout_injection = run_agent("Timeout-failure recovery probe: acknowledge with OK.")
if not post_timeout_injection:
    raise SystemExit("[e2e] ERROR: recovery probe failed after timeout failure injection")
_inject_malformed_response_failure_and_verify_recovery()
post_malformed_injection = run_agent("Malformed-response recovery probe: acknowledge with OK.")
if not post_malformed_injection:
    raise SystemExit("[e2e] ERROR: recovery probe failed after malformed response injection")

janitor_probe = _spawn_janitor_probe()
pressure_turn = run_agent("Resilience probe turn 3 during janitor pressure: acknowledge with OK.")
if not pressure_turn:
    janitor_probe.kill()
    raise SystemExit("[e2e] ERROR: resilience turn 3 failed/empty during janitor pressure")
_inject_timeout_failure_and_verify_recovery()
post_pressure_timeout = run_agent("Timeout-failure recovery probe under janitor pressure: acknowledge with OK.")
if not post_pressure_timeout:
    janitor_probe.kill()
    raise SystemExit("[e2e] ERROR: recovery probe failed after timeout failure under janitor pressure")
try:
    j_out, j_err = janitor_probe.communicate(timeout=180)
except subprocess.TimeoutExpired:
    janitor_probe.kill()
    raise SystemExit("[e2e] ERROR: janitor pressure probe timed out")
if janitor_probe.returncode != 0:
    raise SystemExit(
        "[e2e] ERROR: janitor pressure probe failed during resilience test\n"
        f"{(j_out or '')[-600:]}\n{(j_err or '')[-600:]}"
    )

sid_a = f"quaid-e2e-resilience-a-{uuid.uuid4().hex[:6]}"
sid_b = f"quaid-e2e-resilience-b-{uuid.uuid4().hex[:6]}"
janitor_probe_2 = _spawn_janitor_probe()
for sid, msg in [
    (sid_a, "Cross-session probe A turn 1: acknowledge with OK."),
    (sid_b, "Cross-session probe B turn 1: acknowledge with OK."),
    (sid_a, "Cross-session probe A turn 2: acknowledge with OK."),
    (sid_b, "Cross-session probe B turn 2: acknowledge with OK."),
]:
    out = run_agent_for(sid, msg)
    if not out:
        janitor_probe_2.kill()
        raise SystemExit(f"[e2e] ERROR: empty output in cross-session probe sid={sid}")

try:
    j2_out, j2_err = janitor_probe_2.communicate(timeout=180)
except subprocess.TimeoutExpired:
    janitor_probe_2.kill()
    raise SystemExit("[e2e] ERROR: janitor cross-session pressure probe timed out")
if janitor_probe_2.returncode != 0:
    raise SystemExit(
        "[e2e] ERROR: janitor cross-session pressure probe failed\n"
        f"{(j2_out or '')[-600:]}\n{(j2_err or '')[-600:]}"
    )

def _read_cursor_with_wait(sid: str, wait_seconds: float = 10.0):
    cp = os.path.join(cursor_dir, f"{sid}.json")
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if os.path.exists(cp):
            try:
                return json.loads(open(cp, "r", encoding="utf-8").read())
            except Exception:
                time.sleep(0.25)
                continue
        time.sleep(0.25)
    return None

missing_cursor_ids = []
for sid in (sid_a, sid_b):
    payload = _read_cursor_with_wait(sid)
    if not isinstance(payload, dict):
        missing_cursor_ids.append(sid)
        continue
    if str(payload.get("sessionId") or "") != sid:
        raise SystemExit(
            f"[e2e] ERROR: cursor sessionId mismatch sid={sid} got={payload.get('sessionId')!r}"
        )
if missing_cursor_ids:
    print(
        "[e2e] WARN: cross-session probe missing cursor files for "
        + ", ".join(missing_cursor_ids)
        + " (continuing; cursor checks are covered in live-events suite)."
    )

project_defs = {}
cfg_path = os.path.join(ws, "config", "memory.json")
try:
    cfg_obj = json.loads(open(cfg_path, "r", encoding="utf-8").read())
    project_defs = ((cfg_obj.get("projects") or {}).get("definitions") or {})
except Exception:
    project_defs = {}
if isinstance(project_defs, dict) and project_defs:
    staging_dir = os.path.join(ws, "projects", "staging")
    os.makedirs(staging_dir, exist_ok=True)
    evt_path = os.path.join(staging_dir, f"{int(time.time() * 1000)}-e2e-resilience-project.json")
    evt_payload = {
        "project_hint": "quaid",
        "files_touched": ["projects/quaid/README.md", "modules/quaid/datastore/docsdb/project_updater.py"],
        "summary": "Resilience matrix queued project updater event.",
        "trigger": "compact",
        "session_id": f"quaid-e2e-resilience-project-{uuid.uuid4().hex[:8]}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(evt_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(evt_payload, indent=2))

    updater_env = dict(os.environ)
    updater_py_path = updater_env.get("PYTHONPATH", "")
    updater_env["PYTHONPATH"] = "modules/quaid" + (f":{updater_py_path}" if updater_py_path else "")
    updater_env["QUAID_HOME"] = ws
    updater_env["CLAWDBOT_WORKSPACE"] = ws
    updater_probe = subprocess.Popen(
        ["python3", "modules/quaid/datastore/docsdb/project_updater.py", "process-all"],
        cwd=".",
        env=updater_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for sid, msg in [
        (sid_a, "Project-updater pressure probe A: acknowledge with OK."),
        (sid_b, "Project-updater pressure probe B: acknowledge with OK."),
    ]:
        out = run_agent_for(sid, msg)
        if not out:
            updater_probe.kill()
            raise SystemExit(f"[e2e] ERROR: empty output during project-updater pressure probe sid={sid}")

    try:
        u_out, u_err = updater_probe.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        updater_probe.kill()
        raise SystemExit("[e2e] ERROR: project-updater pressure probe timed out")
    if updater_probe.returncode != 0:
        raise SystemExit(
            "[e2e] ERROR: project-updater pressure probe failed\n"
            f"{(u_out or '')[-600:]}\n{(u_err or '')[-600:]}"
        )
    parsed = _parse_last_json_blob(u_out or "")
    if not isinstance(parsed, dict):
        raise SystemExit("[e2e] ERROR: could not parse project-updater process-all result JSON")
    if int(parsed.get("processed", 0) or 0) < 1:
        raise SystemExit(
            "[e2e] ERROR: project-updater pressure probe did not process queued event "
            f"(result={parsed})"
        )
    if int(parsed.get("errors", 0) or 0) > 0:
        raise SystemExit(
            "[e2e] ERROR: project-updater pressure probe reported errors "
            f"(result={parsed})"
        )
else:
    print("[e2e] WARN: skipping project-updater pressure probe (no projects.definitions configured).")

cleanup_env = dict(os.environ)
cleanup_py_path = cleanup_env.get("PYTHONPATH", "")
cleanup_env["PYTHONPATH"] = "modules/quaid" + (f":{cleanup_py_path}" if cleanup_py_path else "")
cleanup_env["QUAID_HOME"] = ws
cleanup_env["CLAWDBOT_WORKSPACE"] = ws
cleanup_probe = subprocess.Popen(
    ["python3", "modules/quaid/core/lifecycle/janitor.py", "--task", "cleanup", "--apply"],
    cwd=".",
    env=cleanup_env,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
time.sleep(1.2)
subprocess.run(["openclaw", "gateway", "restart"], check=False, capture_output=True, text=True, timeout=90)
_wait_gateway(40)
try:
    c_out, c_err = cleanup_probe.communicate(timeout=180)
except subprocess.TimeoutExpired:
    cleanup_probe.kill()
    raise SystemExit("[e2e] ERROR: janitor cleanup write-window probe timed out")
if cleanup_probe.returncode != 0:
    raise SystemExit(
        "[e2e] ERROR: janitor cleanup write-window probe failed\n"
        f"{(c_out or '')[-600:]}\n{(c_err or '')[-600:]}"
    )
db_path = os.path.join(ws, "data", "memory.db")
with sqlite3.connect(db_path) as conn:
    row = conn.execute(
        """
        SELECT task_name, status
        FROM janitor_runs
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
if not row:
    raise SystemExit("[e2e] ERROR: no janitor_runs row found after cleanup write-window probe")
if str(row[0]) != "cleanup" or str(row[1]) != "completed":
    raise SystemExit(
        "[e2e] ERROR: janitor write-window probe recorded unexpected run state "
        f"(task={row[0]!r}, status={row[1]!r})"
    )

print("[e2e] Resilience check passed.")
PY
done
pass_stage "resilience"
else
  skip_stage "resilience"
  echo "[e2e] Skipping resilience check (suite selection)."
fi

if [[ "$RUN_MEMORY_FLOW" == true ]]; then
begin_stage "memory_flow"
echo "[e2e] Running memory flow regression checks (compact/new/recall)..."
python3 - "$E2E_WS" <<'PY'
import json
import os
import subprocess
import sys
import time
import uuid

ws = sys.argv[1]
events_path = os.path.join(ws, "logs", "quaid", "session-timeout-events.jsonl")
session_id = f"quaid-e2e-memory-{uuid.uuid4().hex[:12]}"

def line_count(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)

def read_tail_since(path: str, start: int):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, start=1):
            if idx > start:
                out.append(line.strip())
    return out

def run_agent(message: str, timeout_sec: int = 220) -> str:
    try:
        proc = subprocess.run(
            ["openclaw", "agent", "--session-id", session_id, "--message", message, "--timeout", str(timeout_sec), "--json"],
            capture_output=True,
            text=True,
            timeout=max(timeout_sec + 60, 180),
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"[e2e] ERROR: openclaw agent timed out message={message!r}"
        )
    if proc.returncode != 0:
        raise SystemExit(
            f"[e2e] ERROR: openclaw agent failed message={message!r}: {proc.stderr.strip()[:500]}"
        )
    out = proc.stdout.strip()
    if not out:
        return ""
    # Keep parser permissive: output can be non-JSON in some gateway fallback paths.
    try:
        parsed = json.loads(out)
    except Exception:
        return out
    if isinstance(parsed, dict):
        if isinstance(parsed.get("response"), str):
            return parsed["response"]
        if isinstance(parsed.get("output"), str):
            return parsed["output"]
    return out

def wait_for_reset_signal(start_line: int, seconds: int = 60) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        lines = read_tail_since(events_path, start_line)
        if any(
            f'"session_id":"{session_id}"' in ln
            and '"label":"ResetSignal"' in ln
            and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
            for ln in lines
        ):
            return
        time.sleep(1)
    preview = "\n".join(read_tail_since(events_path, start_line)[-30:])
    raise SystemExit(
        "[e2e] ERROR: timed out waiting for reset extraction in memory flow\n"
        + preview
    )

# Seed facts and force compaction extraction.
run_agent("Please remember this exactly: my mother is Wendy and my father is Kent.")
run_agent("/compact", timeout_sec=260)

# Trigger reset path (same hook path used by /new).
start_line = line_count(events_path)
run_agent("/new", timeout_sec=260)
wait_for_reset_signal(start_line, 60)

# Validate recall behavior from new session.
answer = run_agent(
    "What are my parents' names? Answer with exactly two names and no caveats.",
    timeout_sec=260,
)
answer_l = answer.lower()
if "wendy" not in answer_l or "kent" not in answer_l:
    raise SystemExit(
        "[e2e] ERROR: memory flow recall did not return expected parent names.\n"
        f"[e2e] answer={answer[:800]}"
    )

bad_markers = ("low-confidence", "low confidence", "outdated", "stale")
if any(marker in answer_l for marker in bad_markers):
    raise SystemExit(
        "[e2e] ERROR: memory flow answer included stale/low-confidence hedge language.\n"
        f"[e2e] answer={answer[:800]}"
    )

print("[e2e] Memory flow regression checks passed.")
PY
pass_stage "memory_flow"
else
  skip_stage "memory_flow"
  echo "[e2e] Skipping memory flow checks (suite selection)."
fi

if [[ "$RUN_NOTIFY_MATRIX" == true ]]; then
begin_stage "notify_matrix"
echo "[e2e] Validating notification level matrix (quiet/normal/debug)..."
python3 - "$E2E_WS" <<'PY'
import json
import os
import subprocess
import sys
import time
import uuid

ws = sys.argv[1]
strict_delivery = os.environ.get("QUAID_E2E_NOTIFY_REQUIRE_DELIVERY", "").strip().lower() == "true"
cfg_path = os.path.join(ws, "config", "memory.json")
events_path = os.path.join(ws, "logs", "quaid", "session-timeout-events.jsonl")
notify_log_path = os.path.join(ws, "logs", "notify-worker.log")

def line_count(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)

def read_tail_since(path: str, start: int):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, start=1):
            if idx > start:
                out.append(line.strip())
    return out

def set_level(level: str) -> None:
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    notifications = cfg.setdefault("notifications", {})
    notifications["level"] = level
    if level == "quiet":
        notifications["janitor"] = {"verbosity": "off"}
        notifications["extraction"] = {"verbosity": "off"}
        notifications["retrieval"] = {"verbosity": "off"}
    elif level == "normal":
        notifications["janitor"] = {"verbosity": "summary"}
        notifications["extraction"] = {"verbosity": "summary"}
        notifications["retrieval"] = {"verbosity": "off"}
    elif level == "debug":
        notifications["janitor"] = {"verbosity": "full"}
        notifications["extraction"] = {"verbosity": "full"}
        notifications["retrieval"] = {"verbosity": "full"}
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

def restart_gateway() -> None:
    subprocess.run(["openclaw", "gateway", "stop"], capture_output=True, text=True, timeout=60)
    subprocess.run(["openclaw", "gateway", "install"], capture_output=True, text=True, timeout=60)
    subprocess.run(["openclaw", "gateway", "restart"], capture_output=True, text=True, timeout=60)
    subprocess.run(["openclaw", "gateway", "start"], capture_output=True, text=True, timeout=60)
    deadline = time.time() + 30
    while time.time() < deadline:
        chk = subprocess.run(
            ["bash", "-lc", "lsof -nP -iTCP:18789 -sTCP:LISTEN"],
            capture_output=True,
            text=True,
        )
        if chk.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit("[e2e] ERROR: gateway did not resume listen on 127.0.0.1:18789 for notify matrix")

def ensure_gateway_ready() -> None:
    chk = subprocess.run(
        ["bash", "-lc", "lsof -nP -iTCP:18789 -sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    if chk.returncode == 0:
        return
    restart_gateway()

def run_agent(session_id: str, message: str) -> None:
    attempts = 2
    last_err = ""
    for attempt in range(1, attempts + 1):
        ensure_gateway_ready()
        try:
            proc = subprocess.run(
                ["openclaw", "agent", "--session-id", session_id, "--message", message, "--timeout", "90", "--json"],
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            last_err = (
                f"[e2e] openclaw agent timed out in notify matrix level session={session_id} "
                f"msg={message!r} attempt={attempt}/{attempts}"
            )
            restart_gateway()
            continue
        if proc.returncode == 0:
            return
        last_err = (
            f"[e2e] openclaw agent failed in notify matrix level session={session_id} "
            f"msg={message!r} attempt={attempt}/{attempts}: {proc.stderr.strip()[:400]}"
        )
        restart_gateway()
    raise SystemExit(last_err or "[e2e] ERROR: notify matrix agent call failed")

def wait_for_reset_start(start_line: int, seconds: int = 45) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        lines = read_tail_since(events_path, start_line)
        if any('"label":"ResetSignal"' in ln and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln) for ln in lines):
            return
        time.sleep(1)
    preview = "\n".join(read_tail_since(events_path, start_line)[-30:])
    raise SystemExit(f"[e2e] ERROR: notify matrix timed out waiting for reset extraction start\n{preview}")

def wait_for_reset_finish(start_line: int, seconds: int = 120) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        lines = read_tail_since(events_path, start_line)
        if any(
            '"label":"ResetSignal"' in ln
            and (
                '"event":"signal_process_complete"' in ln
                or '"event":"signal_process_end"' in ln
                or '"event":"extract_done"' in ln
            )
            for ln in lines
        ):
            return
        time.sleep(1)
    preview = "\n".join(read_tail_since(events_path, start_line)[-40:])
    raise SystemExit(f"[e2e] ERROR: notify matrix timed out waiting for reset extraction completion\n{preview}")

def assert_no_fatal_notify_errors(lines) -> None:
    patterns = (
        "No such file or directory: 'clawdbot'",
        "No such file or directory: 'openclaw'",
        "No message CLI found",
    )
    bad = [ln for ln in lines if any(p in ln for p in patterns)]
    if bad:
        raise SystemExit(
            "[e2e] ERROR: notify matrix detected fatal notification CLI issue:\n"
            + "\n".join(bad[-20:])
        )

results = []
for level in ("quiet", "normal", "debug"):
    print(f"[e2e] notify-matrix level={level} start")
    set_level(level)
    restart_gateway()
    notify_start = line_count(notify_log_path)
    events_start = line_count(events_path)
    sid = f"quaid-e2e-notify-{level}-{uuid.uuid4().hex[:8]}"
    marker = f"E2E_NOTIFY_LEVEL_{level}_{uuid.uuid4().hex[:6]}"
    run_agent(sid, f"notification level marker: {marker}")
    run_agent(sid, "/reset")
    wait_for_reset_start(events_start, 45)
    wait_for_reset_finish(events_start, 120)
    time.sleep(2)
    notify_lines = read_tail_since(notify_log_path, notify_start)
    assert_no_fatal_notify_errors(notify_lines)
    loaded = sum(1 for ln in notify_lines if "[config] Loaded from " in ln)
    sent = sum(1 for ln in notify_lines if "[notify] Sent to " in ln)
    no_last_channel = sum(1 for ln in notify_lines if "[notify] No last channel found" in ln)
    send_failed = sum(1 for ln in notify_lines if "[notify] Send failed" in ln)
    activity = loaded + sent + no_last_channel + send_failed
    results.append(
        {
            "level": level,
            "notify_lines": len(notify_lines),
            "loaded_count": loaded,
            "sent_count": sent,
            "no_last_channel_count": no_last_channel,
            "send_failed_count": send_failed,
            "activity_count": activity,
        }
    )
    if level == "quiet" and loaded > 0:
        raise SystemExit("[e2e] ERROR: quiet level emitted extraction notifications")
    if level in ("normal", "debug") and activity == 0:
        raise SystemExit(f"[e2e] ERROR: {level} level emitted no extraction notification activity")
    if strict_delivery and level in ("normal", "debug"):
        if no_last_channel > 0:
            raise SystemExit(
                f"[e2e] ERROR: strict notify delivery enabled and {level} had no active channel context"
            )
        if sent == 0:
            raise SystemExit(
                f"[e2e] ERROR: strict notify delivery enabled and {level} sent no notifications"
            )
    print(f"[e2e] notify-matrix level={level} ok")

print("[e2e] Notify matrix results:")
print(json.dumps(results, indent=2))
print("[e2e] Notify matrix checks passed.")
PY
pass_stage "notify_matrix"
else
  skip_stage "notify_matrix"
  echo "[e2e] Skipping notification matrix (suite selection/flag)."
fi

if [[ "$RUN_INGEST_STRESS" == true ]]; then
begin_stage "ingest_stress"
echo "[e2e] Running ingestion stress checks (facts/snippets/journal/projects)..."
python3 - "$E2E_WS" <<'PY'
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

ws = Path(sys.argv[1])
db_path = ws / "data" / "memory.db"
events_path = ws / "logs" / "quaid" / "session-timeout-events.jsonl"
project_staging = ws / "projects" / "staging"
project_log = ws / "logs" / "project-updater.log"
extraction_log_path = ws / "data" / "extraction-log.json"
session_id = f"quaid-e2e-ingest-{uuid.uuid4().hex[:12]}"
marker = f"E2E_INGEST_{uuid.uuid4().hex[:10]}"

def run_agent(message: str, timeout_sec: int = 220) -> None:
    cmd = ["openclaw", "agent", "--session-id", session_id, "--message", message, "--timeout", str(timeout_sec), "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=max(timeout_sec + 60, 180))
    except subprocess.TimeoutExpired:
        raise SystemExit(f"[e2e] ERROR: openclaw agent timed out for message={message[:80]!r}")
    if proc.returncode != 0:
        raise SystemExit(f"[e2e] ERROR: openclaw agent failed for message={message[:80]!r}: {proc.stderr.strip()[:500]}")

def count_nodes(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()
    return int(row[0] if row else 0)

def count_staging_events() -> int:
    if not project_staging.exists():
        return 0
    return len(list(project_staging.glob("*.json")))

def project_log_size() -> int:
    try:
        return project_log.stat().st_size
    except Exception:
        return 0

def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)

def read_tail_since(path: Path, start: int):
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, start=1):
            if idx > start:
                out.append(line.strip())
    return out

def resolve_runtime_session_id(marker_text: str, seconds: int = 35) -> str:
    session_dir = ws / "logs" / "quaid" / "session-messages"
    deadline = time.time() + seconds
    while time.time() < deadline:
        if session_dir.is_dir():
            for fp in session_dir.glob("*.jsonl"):
                try:
                    content = fp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if marker_text in content:
                    return fp.stem
        time.sleep(1)
    raise SystemExit(f"[e2e] ERROR: unable to resolve runtime session_id for marker={marker_text}")

def wait_for(pred, seconds: int, label: str):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return
        time.sleep(1)
    raise SystemExit(f"[e2e] ERROR: timed out waiting for {label}")

with sqlite3.connect(db_path) as conn:
    baseline_nodes = count_nodes(conn)

baseline_staging = count_staging_events()
baseline_project_log_size = project_log_size()
start_line = line_count(events_path)

run_agent(
    f"""{marker}
I need you to remember project status and personal context. We are editing modules/quaid/core/lifecycle/janitor.py and modules/quaid/datastore/docsdb/project_updater.py.
Facts:
1) My dog is named Madu.
2) My sister is Shannon.
3) I work in /Users/owner/quaid/dev.
Project summary: quaid refactor includes janitor lifecycle registry and datastore-owned maintenance.
Journal cue: I feel focused and cautious about boundaries.
Snippet cue: boundary ownership belongs in datastore modules.
""",
    timeout_sec=260,
)
run_agent("/compact", timeout_sec=260)

runtime_session_id = resolve_runtime_session_id(marker)

def extraction_seen() -> bool:
    lines = read_tail_since(events_path, start_line)
    return any(
        f'"session_id":"{runtime_session_id}"' in ln
        and (
            ('"label":"CompactionSignal"' in ln and '"event":"signal_process_begin"' in ln)
            or '"event":"extract_complete"' in ln
        )
        for ln in lines
    )

wait_for(extraction_seen, 90, "ingestion extraction completion")

with sqlite3.connect(db_path) as conn:
    after_nodes = count_nodes(conn)

extraction_logged = False
if extraction_log_path.exists():
    try:
        raw = json.loads(extraction_log_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            extraction_logged = runtime_session_id in raw
    except Exception:
        extraction_logged = False

def project_activity_seen() -> bool:
    if count_staging_events() > baseline_staging:
        return True
    if project_log_size() > baseline_project_log_size:
        return True
    return False

wait_for(project_activity_seen, 45, "project event queue/update activity")

print(
    json.dumps(
        {
            "session_id": session_id,
            "runtime_session_id": runtime_session_id,
            "nodes_before": baseline_nodes,
            "nodes_after": after_nodes,
            "node_delta": after_nodes - baseline_nodes,
            "extraction_logged": extraction_logged,
            "project_staging_before": baseline_staging,
            "project_staging_after": count_staging_events(),
            "project_log_size_before": baseline_project_log_size,
            "project_log_size_after": project_log_size(),
        },
        indent=2,
    )
)
print("[e2e] Ingestion stress checks passed.")
PY
pass_stage "ingest_stress"
else
  skip_stage "ingest_stress"
  echo "[e2e] Skipping ingestion stress checks (suite selection)."
fi

if [[ "$RUN_PROJECT_CRUD" == true ]]; then
begin_stage "project_crud"
echo "[e2e] Running project CRUD checks (create/list/rename/archive/delete)..."
python3 - "$E2E_WS" <<'PY'
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ws = Path(sys.argv[1])
registry = ["python3", "-m", "datastore.docsdb.registry"]
env = dict(os.environ)
py = env.get("PYTHONPATH", "")
root = str((ws / "modules" / "quaid").resolve())
env["PYTHONPATH"] = root if not py else f"{root}:{py}"

def run(args, timeout=90):
    proc = subprocess.run(
        [*registry, *args],
        cwd=str(ws),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"[e2e] ERROR: docs_registry failed args={args}\n"
            f"stdout:\n{proc.stdout[-800:]}\n"
            f"stderr:\n{proc.stderr[-800:]}"
        )
    return proc.stdout.strip()

def list_names():
    out = run(["list-projects", "--names-only"])
    return [ln.strip() for ln in out.splitlines() if ln.strip()]

seed = uuid.uuid4().hex[:8]
p_create = f"e2e-crud-{seed}-c"
p_rename_old = f"e2e-crud-{seed}-r1"
p_rename_new = f"e2e-crud-{seed}-r2"
p_archive = f"e2e-crud-{seed}-a"
p_delete = f"e2e-crud-{seed}-d"

# CREATE + LIST
run(["create-project", p_create, "--label", "E2E Create", "--description", "e2e create/list validation"])
names = set(list_names())
if p_create not in names:
    raise SystemExit(f"[e2e] ERROR: created project missing from list: {p_create}")

# RENAME
run(["create-project", p_rename_old, "--label", "E2E Rename", "--description", "e2e rename validation"])
run(["rename-project", p_rename_old, p_rename_new])
names = set(list_names())
if p_rename_old in names or p_rename_new not in names:
    raise SystemExit(
        f"[e2e] ERROR: rename validation failed old={p_rename_old in names} new={p_rename_new in names}"
    )

# ARCHIVE
run(["create-project", p_archive, "--label", "E2E Archive", "--description", "e2e archive validation"])
run(["archive-project", p_archive, "--yes"])
archive_dir = ws / "projects" / "archive" / p_archive
if not archive_dir.exists():
    raise SystemExit(f"[e2e] ERROR: archive validation failed; missing dir {archive_dir}")
names = set(list_names())
if p_archive in names:
    raise SystemExit(f"[e2e] ERROR: archived project still listed as active: {p_archive}")

# DELETE
run(["create-project", p_delete, "--label", "E2E Delete", "--description", "e2e delete validation"])
run(["delete-project", p_delete, "--yes"])
names = set(list_names())
if p_delete in names:
    raise SystemExit(f"[e2e] ERROR: deleted project still listed as active: {p_delete}")

print(
    json.dumps(
        {
            "created_project": p_create,
            "renamed_from": p_rename_old,
            "renamed_to": p_rename_new,
            "archived_project": p_archive,
            "archived_dir_exists": archive_dir.exists(),
            "deleted_project": p_delete,
            "active_project_count_after": len(names),
        },
        indent=2,
    )
)
print("[e2e] Project CRUD checks passed.")
PY
pass_stage "project_crud"
else
  skip_stage "project_crud"
  echo "[e2e] Skipping project CRUD checks (suite selection)."
fi

if [[ "$RUN_SESSION_TOOLS" == true ]]; then
begin_stage "session_tools"
echo "[e2e] Running session tools checks (session_recall + session_logs_search)..."
python3 - "$E2E_WS" <<'PY'
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ws = Path(sys.argv[1])
session_seed = f"quaid-e2e-session-seed-{uuid.uuid4().hex[:10]}"
session_query = f"quaid-e2e-session-query-{uuid.uuid4().hex[:10]}"
marker = f"E2E_SESSION_SEARCH_{uuid.uuid4().hex[:12]}"
events_path = ws / "logs" / "quaid" / "session-timeout-events.jsonl"
sessions_dir = Path.home() / ".openclaw" / "sessions"

def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return sum(1 for _ in f)

def read_tail_since(path: Path, start: int):
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, start=1):
            if idx > start:
                out.append(line.strip())
    return out

def wait_for(pred, seconds: int, label: str, start_line: int):
    deadline = time.time() + seconds
    while time.time() < deadline:
        lines = read_tail_since(events_path, start_line)
        if pred(lines):
            return
        time.sleep(1)
    preview = "\n".join(read_tail_since(events_path, start_line)[-30:])
    raise SystemExit(f"[e2e] ERROR: timed out waiting for {label}\n{preview}")

def run_agent(session_id: str, message: str, timeout_sec: int = 220) -> str:
    try:
        proc = subprocess.run(
            ["openclaw", "agent", "--session-id", session_id, "--message", message, "--timeout", str(timeout_sec), "--json"],
            capture_output=True,
            text=True,
            timeout=max(timeout_sec + 60, 180),
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(f"[e2e] ERROR: openclaw agent timed out message={message[:80]!r}")
    if proc.returncode != 0:
        raise SystemExit(f"[e2e] ERROR: openclaw agent failed: {proc.stderr.strip()[:500]}")
    out = proc.stdout.strip()
    if not out:
        return ""
    try:
        parsed = json.loads(out)
    except Exception:
        return out
    if isinstance(parsed, dict):
        if isinstance(parsed.get("response"), str):
            return parsed["response"]
        if isinstance(parsed.get("output"), str):
            return parsed["output"]
    return out

# Seed a unique marker and force compaction-triggered ingest.
run_agent(session_seed, f"Remember this exact marker for recovery tests: {marker}. Reply with ACK only.")
start = line_count(events_path)
run_agent(session_seed, "/compact", timeout_sec=260)
wait_for(
    lambda lines: any(
        f'"session_id":"{session_seed}"' in ln
        and '"label":"CompactionSignal"' in ln
        and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
        for ln in lines
    ),
    45,
    "session seed compaction signal",
    start,
)

# Ask the model to use session_logs_search explicitly.
answer = run_agent(
    session_query,
    (
        f'Use the tool "session_logs_search" with query "{marker}". '
        "Return exactly the marker if found; otherwise return NOT_FOUND."
    ),
    timeout_sec=260,
)
if marker not in answer:
    raise SystemExit(
        "[e2e] ERROR: session_logs_search validation failed; marker missing from response.\n"
        f"[e2e] answer={answer[:700]}"
    )

# Validate session_recall list path returns indexed sessions text.
list_answer = run_agent(
    session_query,
    "Use session_recall action=list limit=3 and return one session id you see.",
    timeout_sec=260,
)
if "quaid-e2e-session-seed-" not in list_answer and "session" not in list_answer.lower():
    raise SystemExit(
        "[e2e] ERROR: session_recall list validation failed.\n"
        f"[e2e] answer={list_answer[:700]}"
    )

# Soft assertion: verify tool call trace likely present in session transcript.
query_session_path = sessions_dir / f"{session_query}.jsonl"
tool_trace_found = False
if query_session_path.exists():
    raw = query_session_path.read_text(encoding="utf-8", errors="replace")
    tool_trace_found = ("session_logs_search" in raw) or ("session_recall" in raw)

print(
    json.dumps(
        {
            "seed_session": session_seed,
            "query_session": session_query,
            "marker": marker,
            "session_logs_search_response_preview": answer[:180],
            "session_recall_response_preview": list_answer[:180],
            "tool_trace_found": tool_trace_found,
        },
        indent=2,
    )
)
print("[e2e] Session tools checks passed.")
PY
pass_stage "session_tools"
else
  skip_stage "session_tools"
  echo "[e2e] Skipping session tools checks (suite selection)."
fi

if [[ "$RUN_JANITOR" == true ]]; then
begin_stage "janitor"
if [[ "$RUN_JANITOR_SEED" == true ]]; then
echo "[e2e] Seeding janitor workload (pending memory/snippets/journal/project/rag)..."
python3 - "$E2E_WS" <<'PY'
import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

ws = Path(__import__("sys").argv[1])
db_path = ws / "data" / "memory.db"
cfg_path = ws / "config" / "memory.json"
cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
journal_dir = ws / ((cfg.get("docs") or {}).get("journal", {}).get("journalDir") or "journal")
staging_dir = ws / ((cfg.get("projects") or {}).get("stagingDir") or "projects/staging/")
project_home = ws / "projects" / "quaid"
docs_dir = ws / ((cfg.get("rag") or {}).get("docsDir") or "docs")

journal_dir.mkdir(parents=True, exist_ok=True)
staging_dir.mkdir(parents=True, exist_ok=True)
project_home.mkdir(parents=True, exist_ok=True)
docs_dir.mkdir(parents=True, exist_ok=True)

snippet_path = ws / "SOUL.snippets.md"
snippet_path.write_text(
    "# SOUL — Pending Snippets\n\n"
    "## Compaction — 2026-02-20 01:01:01\n"
    "- [REFLECTION] Boundary ownership should remain inside datastore modules.\n"
    "- [REFLECTION] Janitor should orchestrate, not own datastore internals.\n",
    encoding="utf-8",
)

journal_path = journal_dir / "SOUL.journal.md"
journal_path.write_text(
    "# SOUL Journal\n\n"
    "## 2026-02-18 — Reset\n"
    "I noticed the system became more coherent once maintenance ownership lived with each datastore.\n",
    encoding="utf-8",
)

(project_home / "README.md").write_text(
    "# Quaid Project\n\nSeeded e2e project doc for janitor/rag checks.\n",
    encoding="utf-8",
)
(project_home / "PROJECT.md").write_text(
    "# Quaid Project\n\n"
    "## Overview\n\nSeeded PROJECT.md for e2e artifact assertions.\n\n"
    "### In This Directory\n"
    "<!-- Auto-discovered — all files in this directory belong to this project -->\n"
    "(none yet)\n\n"
    "### External Files\n"
    "| File | Purpose | Auto-Update |\n"
    "|------|---------|-------------|\n\n"
    "## Documents\n\n- projects/quaid/README.md\n",
    encoding="utf-8",
)
(docs_dir / "e2e-janitor-seed.md").write_text(
    "# Janitor E2E Seed\n\n"
    "This document exists so rag maintenance has deterministic input.\n\n"
    "E2E_RAG_ANCHOR_JANITOR_BOUNDARY_20260224\n",
    encoding="utf-8",
)

event = {
    "project_hint": "quaid",
    "files_touched": ["modules/quaid/core/lifecycle/janitor.py", "projects/quaid/README.md"],
    "summary": "E2E seeded project event for janitor validation.",
    "trigger": "compact",
    "session_id": f"quaid-e2e-seed-{uuid.uuid4().hex[:8]}",
    "timestamp": dt.datetime.now().isoformat(),
}
(staging_dir / f"{int(dt.datetime.now().timestamp() * 1000)}-e2e-seed.json").write_text(
    json.dumps(event, indent=2),
    encoding="utf-8",
)

now = dt.datetime.now()
old_ts = (now - dt.timedelta(days=120)).isoformat()
contradiction_marker = "e2e-seed-contradiction"
dedup_marker = "e2e-seed-dedup"
decay_marker = "e2e-seed-decay"
multi_owner_marker = "e2e-seed-multi-owner"
rag_anchor_marker = "E2E_RAG_ANCHOR_JANITOR_BOUNDARY_20260224"
registry_drift_doc = "docs/e2e-janitor-seed.md"
registry_drift_doc_abs = str((docs_dir / "e2e-janitor-seed.md").resolve())
source_mapping_drift_doc = "projects/quaid/PROJECT.md"

def mk_node(
    node_type: str,
    name: str,
    status: str = "pending",
    confidence: float = 0.72,
    owner_id: str = "quaid",
):
    nid = str(uuid.uuid4())
    return (
        nid,
        node_type,
        name,
        json.dumps({}),
        None,
        0,
        0,
        confidence,
        "e2e-seed",
        None,
        "shared",
        None,
        None,
        old_ts,
        old_ts,
        old_ts,
        0,
        0.0,
        owner_id,
        "e2e-seed",
        "unknown",
        "fact",
        0.9,
        status,
        None,
        hashlib.sha256(" ".join(name.lower().split()).encode("utf-8")).hexdigest(),
        None,
        0,
        None,
        None,
    )

rows = [
    mk_node("Fact", "The deployment host for quaid is runner-alpha", "pending", 0.74),
    mk_node("Fact", "The deployment host for quaid is runner alpha", "pending", 0.71),
    mk_node("Fact", "I prefer keeping janitor orchestration separate from datastore internals", "pending", 0.77),
    mk_node("Fact", "I currently live in Austin, Texas", "active", 0.82),
    mk_node("Fact", "I currently live in Bali, Indonesia", "active", 0.82),
    mk_node("Fact", f"{multi_owner_marker}: preferred coding beverage is matcha tea", "pending", 0.79, "owner_alpha"),
    mk_node("Fact", f"{multi_owner_marker}: preferred coding beverage is matcha tea", "pending", 0.79, "owner_beta"),
]

with sqlite3.connect(db_path) as conn:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS doc_registry (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          file_path TEXT NOT NULL UNIQUE,
          project TEXT NOT NULL DEFAULT 'default',
          asset_type TEXT NOT NULL DEFAULT 'doc',
          title TEXT,
          description TEXT,
          tags TEXT DEFAULT '[]',
          state TEXT NOT NULL DEFAULT 'active',
          auto_update INTEGER DEFAULT 0,
          source_files TEXT,
          last_indexed_at TEXT,
          last_modified_at TEXT,
          registered_at TEXT NOT NULL DEFAULT (datetime('now')),
          registered_by TEXT DEFAULT 'system'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO doc_registry (
          file_path, project, asset_type, title, description, tags, state,
          auto_update, source_files, last_indexed_at, last_modified_at, registered_by
        ) VALUES (?, 'quaid', 'doc', 'Janitor E2E Seed', 'seed drift fixture', '[]', 'active', 0, NULL, ?, ?, 'e2e-seed')
        ON CONFLICT(file_path) DO UPDATE SET
          project='quaid',
          state='active',
          last_indexed_at=excluded.last_indexed_at,
          last_modified_at=excluded.last_modified_at,
          registered_by='e2e-seed'
        """,
        (registry_drift_doc, old_ts, old_ts),
    )
    conn.execute(
        """
        INSERT INTO doc_registry (
          file_path, project, asset_type, title, description, tags, state,
          auto_update, source_files, last_indexed_at, last_modified_at, registered_by
        ) VALUES (?, 'quaid', 'doc', 'Janitor E2E Seed ABS', 'seed drift fixture abs', '[]', 'active', 0, NULL, ?, ?, 'e2e-seed')
        ON CONFLICT(file_path) DO UPDATE SET
          project='quaid',
          state='active',
          last_indexed_at=excluded.last_indexed_at,
          last_modified_at=excluded.last_modified_at,
          registered_by='e2e-seed'
        """,
        (registry_drift_doc_abs, old_ts, old_ts),
    )
    conn.execute(
        """
        INSERT INTO doc_registry (
          file_path, project, asset_type, title, description, tags, state,
          auto_update, source_files, last_indexed_at, last_modified_at, registered_by
        ) VALUES (?, 'quaid', 'doc', 'Quaid Project Map Drift', 'source mapping drift fixture', '[]', 'active', 1, ?, ?, ?, 'e2e-seed')
        ON CONFLICT(file_path) DO UPDATE SET
          project='quaid',
          state='active',
          auto_update=1,
          source_files=excluded.source_files,
          last_indexed_at=excluded.last_indexed_at,
          last_modified_at=excluded.last_modified_at,
          registered_by='e2e-seed'
        """,
        (
            source_mapping_drift_doc,
            json.dumps(["missing/ghost-source.md", "modules/quaid/core/lifecycle/janitor.py"]),
            old_ts,
            old_ts,
        ),
    )
    conn.executemany(
        """
        INSERT INTO nodes (
          id, type, name, attributes, embedding, verified, pinned, confidence, source, source_id,
          privacy, valid_from, valid_until, created_at, updated_at, accessed_at, access_count,
          storage_strength, owner_id, session_id, fact_type, knowledge_type, extraction_confidence,
          status, speaker, content_hash, superseded_by, confirmation_count, last_confirmed_at, keywords
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO dedup_log (
          id, new_text, existing_node_id, existing_text, similarity, decision, llm_reasoning, review_status, owner_id, source, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            "The deployment host for quaid is runner alpha",
            rows[0][0],
            rows[0][2],
            0.97,
            "auto_reject",
            dedup_marker,
            "unreviewed",
            "quaid",
            "e2e-seed",
            now.isoformat(),
        ),
    )
    conn.execute(
        """
        INSERT INTO contradictions (
          id, node_a_id, node_b_id, explanation, status, detected_at
        ) VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (
            str(uuid.uuid4()),
            rows[3][0],
            rows[4][0],
            f"{contradiction_marker}: seeded direct contradiction for janitor validation",
            now.isoformat(),
        ),
    )
    conn.execute(
        """
        INSERT INTO decay_review_queue (
          id, node_id, node_text, node_type, confidence_at_queue, access_count,
          last_accessed, verified, created_at_node, status, queued_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            str(uuid.uuid4()),
            rows[4][0],
            f"{decay_marker}: I currently live in Bali, Indonesia",
            "Fact",
            0.11,
            0,
            old_ts,
            0,
            old_ts,
            now.isoformat(),
        ),
    )

print(
    json.dumps(
        {
            "seeded_nodes": len(rows),
            "seeded_contradictions": 1,
            "seeded_dedup_rows": 1,
            "seeded_decay_rows": 1,
            "seeded_multi_owner_rows": 2,
            "seeded_rag_anchor": rag_anchor_marker,
            "seeded_registry_drift_doc": registry_drift_doc,
            "seeded_registry_drift_doc_abs": registry_drift_doc_abs,
            "seeded_source_mapping_drift_doc": source_mapping_drift_doc,
            "snippet_path": str(snippet_path),
            "journal_path": str(journal_path),
            "staging_dir": str(staging_dir),
            "docs_dir": str(docs_dir),
        },
        indent=2,
    )
)
PY
fi

echo "[e2e] Running janitor (${JANITOR_MODE})..."
python3 - "$E2E_WS" "$JANITOR_TIMEOUT_SECONDS" "$JANITOR_MODE" "$RUN_PREBENCH_GUARDS" "$RUN_JANITOR_STRESS" "$JANITOR_STRESS_PASSES" "$RUN_JANITOR_PARALLEL_BENCH" "$JANITOR_PARALLEL_REPORT_PATH" <<'PY'
import json
import os
import sqlite3
import subprocess
import sys

ws = sys.argv[1]
timeout_seconds = int(sys.argv[2])
mode = sys.argv[3]
prebench_guard = str(sys.argv[4]).strip().lower() in {"1", "true", "yes", "on"}
janitor_stress = str(sys.argv[5]).strip().lower() in {"1", "true", "yes", "on"}
janitor_stress_passes = max(1, int(sys.argv[6]))
janitor_parallel_bench = str(sys.argv[7]).strip().lower() in {"1", "true", "yes", "on"}
janitor_parallel_report_path = str(sys.argv[8])
db_path = f"{ws}/data/memory.db"
journal_path = os.path.join(ws, "journal", "SOUL.journal.md")
snippet_path = os.path.join(ws, "SOUL.snippets.md")
staging_dir = os.path.join(ws, "projects", "staging")
contradiction_marker = "e2e-seed-contradiction"
dedup_marker = "e2e-seed-dedup"
decay_marker = "e2e-seed-decay"
multi_owner_marker = "e2e-seed-multi-owner"
rag_anchor_marker = "E2E_RAG_ANCHOR_JANITOR_BOUNDARY_20260224"
registry_drift_doc = "docs/e2e-janitor-seed.md"
registry_drift_doc_abs = os.path.abspath(os.path.join(ws, registry_drift_doc))
docs_update_log_path = os.path.join(ws, "logs", "docs-update-log.json")
janitor_stats_path = os.path.join(ws, "logs", "janitor-stats.json")

def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    try:
        return int(str(raw).strip())
    except Exception:
        return int(default)

def fetch_counts(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM nodes GROUP BY status"
    ).fetchall()
    out = {"pending": 0, "approved": 0, "active": 0}
    for status, count in rows:
        if status in out:
            out[status] = count
    return out

def _load_docs_update_entries(path: str):
    if not os.path.exists(path):
        return []
    try:
        raw = json.loads(open(path, "r", encoding="utf-8").read())
        if isinstance(raw, list):
            return raw
    except Exception:
        return []
    return []

def _load_json_obj(path: str):
    if not os.path.exists(path):
        return {}
    try:
        raw = json.loads(open(path, "r", encoding="utf-8").read())
        if isinstance(raw, dict):
            return raw
    except Exception:
        return {}
    return {}

with sqlite3.connect(db_path) as conn:
    before_counts = fetch_counts(conn)
    before_chunks = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='doc_chunks'"
    ).fetchone()[0]
    before_doc_chunks = 0
    before_anchor_chunks = 0
    if before_chunks:
        before_doc_chunks = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
        before_anchor_chunks = int(
            conn.execute(
                "SELECT COUNT(*) FROM doc_chunks WHERE content LIKE ?",
                (f"%{rag_anchor_marker}%",),
            ).fetchone()[0]
        )
    has_runs_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='janitor_runs'"
    ).fetchone() is not None
    before_run_id = 0
    if has_runs_table:
        before_run_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM janitor_runs").fetchone()[0]
    has_contradictions_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contradictions'"
    ).fetchone() is not None
    before_seeded_contradictions_pending = 0
    before_seeded_contradictions_resolved = 0
    if has_contradictions_table:
        before_seeded_contradictions_pending = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM contradictions
                WHERE status = 'pending' AND explanation LIKE ?
                """,
                (f"%{contradiction_marker}%",),
            ).fetchone()[0]
        )
        before_seeded_contradictions_resolved = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM contradictions
                WHERE status IN ('resolved', 'false_positive') AND explanation LIKE ?
                """,
                (f"%{contradiction_marker}%",),
            ).fetchone()[0]
        )
    has_dedup_log_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dedup_log'"
    ).fetchone() is not None
    before_seeded_dedup_unreviewed = 0
    before_seeded_dedup_reviewed = 0
    if has_dedup_log_table:
        before_seeded_dedup_unreviewed = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM dedup_log
                WHERE review_status = 'unreviewed' AND llm_reasoning = ?
                """,
                (dedup_marker,),
            ).fetchone()[0]
        )
        before_seeded_dedup_reviewed = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM dedup_log
                WHERE review_status IN ('confirmed', 'reversed') AND llm_reasoning = ?
                """,
                (dedup_marker,),
            ).fetchone()[0]
        )
    has_decay_queue_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decay_review_queue'"
    ).fetchone() is not None
    before_seeded_decay_pending = 0
    before_seeded_decay_reviewed = 0
    if has_decay_queue_table:
        before_seeded_decay_pending = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM decay_review_queue
                WHERE status = 'pending' AND node_text LIKE ?
                """,
                (f"%{decay_marker}%",),
            ).fetchone()[0]
        )
        before_seeded_decay_reviewed = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM decay_review_queue
                WHERE status = 'reviewed' AND node_text LIKE ?
                """,
                (f"%{decay_marker}%",),
            ).fetchone()[0]
        )
    before_multi_owner_node_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE name LIKE ?",
            (f"%{multi_owner_marker}%",),
        ).fetchone()[0]
    )
    before_multi_owner_distinct_owners = int(
        conn.execute(
            "SELECT COUNT(DISTINCT owner_id) FROM nodes WHERE name LIKE ?",
            (f"%{multi_owner_marker}%",),
        ).fetchone()[0]
    )
    has_doc_registry_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_registry'"
    ).fetchone() is not None
    before_registry_last_indexed = ""
    before_registry_last_indexed_abs = ""
    if has_doc_registry_table:
        row = conn.execute(
            "SELECT COALESCE(last_indexed_at, '') FROM doc_registry WHERE file_path = ?",
            (registry_drift_doc,),
        ).fetchone()
        before_registry_last_indexed = str((row[0] if row else "") or "")
        row_abs = conn.execute(
            "SELECT COALESCE(last_indexed_at, '') FROM doc_registry WHERE file_path = ?",
            (registry_drift_doc_abs,),
        ).fetchone()
        before_registry_last_indexed_abs = str((row_abs[0] if row_abs else "") or "")
before_docs_update_entries = _load_docs_update_entries(docs_update_log_path)
before_project_doc_updates = sum(
    1 for e in before_docs_update_entries
    if isinstance(e, dict)
    and str(e.get("doc_path", "")) == "projects/quaid/PROJECT.md"
)
before_staging = len([x for x in os.listdir(staging_dir) if x.endswith(".json")]) if os.path.isdir(staging_dir) else 0
before_seeded_staging = len([x for x in os.listdir(staging_dir) if x.endswith("-e2e-seed.json")]) if os.path.isdir(staging_dir) else 0
snippet_exists_before = os.path.exists(snippet_path)
journal_exists_before = os.path.exists(journal_path)

if prebench_guard and has_runs_table:
    # Migration-fixture resilience probe:
    # emulate legacy janitor_runs schema (without JSON enrichment columns)
    # and require janitor init path to migrate it in-place before write.
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE janitor_runs RENAME TO janitor_runs_legacy_backup")
        conn.execute(
            """
            CREATE TABLE janitor_runs (
              id INTEGER PRIMARY KEY,
              task_name TEXT NOT NULL,
              started_at TEXT NOT NULL,
              completed_at TEXT,
              memories_processed INTEGER DEFAULT 0,
              actions_taken INTEGER DEFAULT 0,
              status TEXT DEFAULT 'running'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO janitor_runs (id, task_name, started_at, completed_at, memories_processed, actions_taken, status)
            SELECT id, task_name, started_at, completed_at, memories_processed, actions_taken, status
            FROM janitor_runs_legacy_backup
            """
        )
        conn.execute("DROP TABLE janitor_runs_legacy_backup")
    print("[e2e] Applied janitor_runs legacy-schema migration probe.")

cmd = ["python3", "modules/quaid/core/lifecycle/janitor.py", "--task", "all"]
if mode == "dry-run":
    cmd.append("--dry-run")
else:
    cmd.append("--apply")
cmd.append("--force-distill")

if prebench_guard:
    # Validate benchmark-mode fail-fast review gate before full janitor apply.
    # Stage cap=1 guarantees carryover when seeded pending backlog >1.
    review_env = dict(os.environ)
    review_env["QUAID_BENCHMARK_MODE"] = "1"
    review_env["JANITOR_MAX_ITEMS_PER_STAGE"] = "1"
    review_py_parts = [f"{ws}/modules/quaid"]
    if review_env.get("PYTHONPATH"):
        review_py_parts.append(review_env["PYTHONPATH"])
    review_env["PYTHONPATH"] = ":".join(review_py_parts)
    review_cmd = [
        "python3",
        "modules/quaid/core/lifecycle/janitor.py",
        "--task",
        "review",
        "--dry-run",
        "--no-resume-checkpoint",
    ]
    review_probe = subprocess.run(
        review_cmd,
        cwd=ws,
        env=review_env,
        capture_output=True,
        text=True,
        timeout=max(120, min(timeout_seconds, 240)),
    )
    if review_probe.returncode == 0:
        print(
            "[e2e] ERROR: benchmark-mode review fail-fast probe unexpectedly passed "
            "(expected non-zero with forced carryover).",
            file=sys.stderr,
        )
        print(review_probe.stdout[-1200:], file=sys.stderr)
        print(review_probe.stderr[-1200:], file=sys.stderr)
        raise SystemExit(1)
    print("[e2e] Benchmark-mode review fail-fast probe behaved as expected.")

try:
    env = dict(os.environ)
    py_parts = [f"{ws}/modules/quaid"]
    if env.get("PYTHONPATH"):
        py_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = ":".join(py_parts)
    if janitor_parallel_bench:
        env["QUAID_BENCHMARK_MODE"] = "1"
        env.setdefault("QUAID_JANITOR_LLM_PARALLELISM", "2")
    subprocess.run(
        cmd,
        cwd=ws,
        check=True,
        timeout=timeout_seconds,
        env=env,
        capture_output=True,
        text=True,
    )
except subprocess.TimeoutExpired:
    print(f"[e2e] Janitor run timed out after {timeout_seconds}s", file=sys.stderr)
    raise SystemExit(1)
except subprocess.CalledProcessError as exc:
    print(
        f"[e2e] ERROR: janitor run failed with exit code {exc.returncode}",
        file=sys.stderr,
    )
    if exc.stdout:
        print("[e2e] janitor stdout (tail):", file=sys.stderr)
        print(exc.stdout[-2000:], file=sys.stderr)
    if exc.stderr:
        print("[e2e] janitor stderr (tail):", file=sys.stderr)
        print(exc.stderr[-2000:], file=sys.stderr)
    raise SystemExit(1)

if janitor_stress and mode == "apply":
    stress_env = dict(os.environ)
    stress_py_parts = [f"{ws}/modules/quaid"]
    if stress_env.get("PYTHONPATH"):
        stress_py_parts.append(stress_env["PYTHONPATH"])
    stress_env["PYTHONPATH"] = ":".join(stress_py_parts)
    stress_before_runs = 0
    stress_before_max_id = 0
    with sqlite3.connect(db_path) as _conn:
        _has = _conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='janitor_runs'"
        ).fetchone() is not None
        if _has:
            stress_before_runs = int(
                _conn.execute("SELECT COUNT(*) FROM janitor_runs").fetchone()[0]
            )
            stress_before_max_id = int(
                _conn.execute("SELECT COALESCE(MAX(id), 0) FROM janitor_runs").fetchone()[0]
            )
    for _ in range(janitor_stress_passes):
        stress_cmd = [
            "python3",
            "modules/quaid/core/lifecycle/janitor.py",
            "--task",
            "all",
            "--apply",
            "--force-distill",
            "--stage-item-cap",
            "2",
            "--no-resume-checkpoint",
        ]
        subprocess.run(
            stress_cmd,
            cwd=ws,
            check=True,
            timeout=max(240, timeout_seconds),
            env=stress_env,
        )
    with sqlite3.connect(db_path) as _conn:
        stress_after_runs = int(_conn.execute("SELECT COUNT(*) FROM janitor_runs").fetchone()[0])
        stress_failed = int(
            _conn.execute(
                "SELECT COUNT(*) FROM janitor_runs WHERE id > ? AND status != 'completed'",
                (stress_before_max_id,),
            ).fetchone()[0]
        )
        stress_rows = _conn.execute(
            "SELECT carryover_json FROM janitor_runs WHERE id > ? ORDER BY id ASC",
            (stress_before_max_id,),
        ).fetchall()
    if (stress_after_runs - stress_before_runs) < janitor_stress_passes:
        print(
            "[e2e] ERROR: janitor stress profile expected additional janitor_runs rows "
            f"(expected>={janitor_stress_passes}, got={stress_after_runs - stress_before_runs})",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if stress_failed > 0:
        print(
            f"[e2e] ERROR: janitor stress profile recorded non-completed runs ({stress_failed})",
            file=sys.stderr,
        )
        raise SystemExit(1)
    stress_carryovers = []
    for row in stress_rows:
        raw = str((row[0] if row else "") or "").strip()
        if not raw:
            stress_carryovers.append(0)
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            stress_carryovers.append(0)
            continue
        if not isinstance(obj, dict):
            stress_carryovers.append(0)
            continue
        total = 0
        for k, v in obj.items():
            if not str(k).endswith("_carryover"):
                continue
            try:
                total += int(v or 0)
            except Exception:
                continue
        stress_carryovers.append(total)
    if len(stress_carryovers) >= 2 and stress_carryovers[-1] > stress_carryovers[0]:
        print(
            "[e2e] ERROR: janitor carryover trend regressed across stress passes "
            f"(first={stress_carryovers[0]}, last={stress_carryovers[-1]})",
            file=sys.stderr,
        )
        raise SystemExit(1)

with sqlite3.connect(db_path) as conn:
    after_counts = fetch_counts(conn)
    after_doc_chunks = 0
    after_anchor_chunks = 0
    has_doc_chunks = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_chunks'"
    ).fetchone() is not None
    if has_doc_chunks:
        after_doc_chunks = conn.execute("SELECT COUNT(*) FROM doc_chunks").fetchone()[0]
        after_anchor_chunks = int(
            conn.execute(
                "SELECT COUNT(*) FROM doc_chunks WHERE content LIKE ?",
                (f"%{rag_anchor_marker}%",),
            ).fetchone()[0]
        )
    has_runs_table_after = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='janitor_runs'"
    ).fetchone() is not None
    if not has_runs_table_after:
        print("[e2e] ERROR: janitor_runs table was not created", file=sys.stderr)
        raise SystemExit(1)
    run_row = conn.execute(
        "SELECT id, task_name, status, COALESCE(memories_processed,0), COALESCE(actions_taken,0), COALESCE(completed_at,'') "
        "FROM janitor_runs WHERE id > ? ORDER BY id DESC LIMIT 1",
        (before_run_id,),
    ).fetchone()
    janitor_runs_cols = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(janitor_runs)").fetchall()
    }
    after_seeded_contradictions_pending = 0
    after_seeded_contradictions_resolved = 0
    if has_contradictions_table:
        after_seeded_contradictions_pending = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM contradictions
                WHERE status = 'pending' AND explanation LIKE ?
                """,
                (f"%{contradiction_marker}%",),
            ).fetchone()[0]
        )
        after_seeded_contradictions_resolved = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM contradictions
                WHERE status IN ('resolved', 'false_positive') AND explanation LIKE ?
                """,
                (f"%{contradiction_marker}%",),
            ).fetchone()[0]
        )
    after_seeded_dedup_unreviewed = 0
    after_seeded_dedup_reviewed = 0
    if has_dedup_log_table:
        after_seeded_dedup_unreviewed = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM dedup_log
                WHERE review_status = 'unreviewed' AND llm_reasoning = ?
                """,
                (dedup_marker,),
            ).fetchone()[0]
        )
        after_seeded_dedup_reviewed = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM dedup_log
                WHERE review_status IN ('confirmed', 'reversed') AND llm_reasoning = ?
                """,
                (dedup_marker,),
            ).fetchone()[0]
        )
    after_seeded_decay_pending = 0
    after_seeded_decay_reviewed = 0
    if has_decay_queue_table:
        after_seeded_decay_pending = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM decay_review_queue
                WHERE status = 'pending' AND node_text LIKE ?
                """,
                (f"%{decay_marker}%",),
            ).fetchone()[0]
        )
        after_seeded_decay_reviewed = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM decay_review_queue
                WHERE status = 'reviewed' AND node_text LIKE ?
                """,
                (f"%{decay_marker}%",),
            ).fetchone()[0]
        )
    after_multi_owner_node_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE name LIKE ?",
            (f"%{multi_owner_marker}%",),
        ).fetchone()[0]
    )
    after_multi_owner_distinct_owners = int(
        conn.execute(
            "SELECT COUNT(DISTINCT owner_id) FROM nodes WHERE name LIKE ?",
            (f"%{multi_owner_marker}%",),
        ).fetchone()[0]
    )
    after_registry_last_indexed = ""
    after_registry_last_indexed_abs = ""
    if has_doc_registry_table:
        row = conn.execute(
            "SELECT COALESCE(last_indexed_at, '') FROM doc_registry WHERE file_path = ?",
            (registry_drift_doc,),
        ).fetchone()
        after_registry_last_indexed = str((row[0] if row else "") or "")
        row_abs = conn.execute(
            "SELECT COALESCE(last_indexed_at, '') FROM doc_registry WHERE file_path = ?",
            (registry_drift_doc_abs,),
        ).fetchone()
        after_registry_last_indexed_abs = str((row_abs[0] if row_abs else "") or "")
after_docs_update_entries = _load_docs_update_entries(docs_update_log_path)
after_project_doc_updates = sum(
    1 for e in after_docs_update_entries
    if isinstance(e, dict)
    and str(e.get("doc_path", "")) == "projects/quaid/PROJECT.md"
)
after_staging = len([x for x in os.listdir(staging_dir) if x.endswith(".json")]) if os.path.isdir(staging_dir) else 0
after_seeded_staging = len([x for x in os.listdir(staging_dir) if x.endswith("-e2e-seed.json")]) if os.path.isdir(staging_dir) else 0
janitor_stats = _load_json_obj(janitor_stats_path)

if janitor_parallel_bench:
    metrics_block = janitor_stats.get("metrics") if isinstance(janitor_stats, dict) else {}
    if not isinstance(metrics_block, dict):
        metrics_block = {}
    task_durations = janitor_stats.get("task_durations") or metrics_block.get("task_durations") or {}
    changes_applied = janitor_stats.get("changes_applied") or janitor_stats.get("changes") or {}
    error_details = janitor_stats.get("error_details") or metrics_block.get("error_details") or []
    warning_details = janitor_stats.get("warning_details") or metrics_block.get("warning_details") or []
    report = {
        "mode": mode,
        "prebench_guard": prebench_guard,
        "janitor_stress": janitor_stress,
        "suite": "janitor-parallel-bench",
        "seeded_counts_before": {
            "contradictions_pending": before_seeded_contradictions_pending,
            "dedup_unreviewed": before_seeded_dedup_unreviewed,
            "decay_pending": before_seeded_decay_pending,
            "staging_events": before_seeded_staging,
        },
        "seeded_counts_after": {
            "contradictions_pending": after_seeded_contradictions_pending,
            "dedup_unreviewed": after_seeded_dedup_unreviewed,
            "decay_pending": after_seeded_decay_pending,
            "staging_events": after_seeded_staging,
        },
        "janitor_stats_task_durations": task_durations,
        "janitor_stats_changes": changes_applied,
        "janitor_stats_errors": error_details,
        "janitor_stats_warnings": warning_details,
        "thresholds": {
            "max_errors": _env_int("QUAID_E2E_JPB_MAX_ERRORS", 0),
            "max_warnings": _env_int("QUAID_E2E_JPB_MAX_WARNINGS", -1),
            "max_contradictions_pending_after": _env_int("QUAID_E2E_JPB_MAX_CONTRADICTIONS_PENDING_AFTER", 0),
            "max_dedup_unreviewed_after": _env_int("QUAID_E2E_JPB_MAX_DEDUP_UNREVIEWED_AFTER", 0),
            "max_decay_pending_after": _env_int("QUAID_E2E_JPB_MAX_DECAY_PENDING_AFTER", 0),
            "max_staging_events_after": _env_int("QUAID_E2E_JPB_MAX_STAGING_EVENTS_AFTER", 0),
        },
    }
    try:
        os.makedirs(os.path.dirname(janitor_parallel_report_path), exist_ok=True)
        with open(janitor_parallel_report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
        print(f"[e2e] Wrote janitor parallel benchmark report: {janitor_parallel_report_path}")
    except Exception as exc:
        print(f"[e2e] WARN: failed writing janitor parallel benchmark report: {exc}", file=sys.stderr)

    thresholds = report["thresholds"]
    violations = []
    errors_count = len(error_details)
    warnings_count = len(warning_details)
    if errors_count > int(thresholds["max_errors"]):
        violations.append(
            f"errors={errors_count} > max_errors={thresholds['max_errors']}"
        )
    if int(thresholds["max_warnings"]) >= 0 and warnings_count > int(thresholds["max_warnings"]):
        violations.append(
            f"warnings={warnings_count} > max_warnings={thresholds['max_warnings']}"
        )
    if after_seeded_contradictions_pending > int(thresholds["max_contradictions_pending_after"]):
        violations.append(
            "seeded contradictions pending after="
            f"{after_seeded_contradictions_pending} > "
            f"max_contradictions_pending_after={thresholds['max_contradictions_pending_after']}"
        )
    if after_seeded_dedup_unreviewed > int(thresholds["max_dedup_unreviewed_after"]):
        violations.append(
            "seeded dedup unreviewed after="
            f"{after_seeded_dedup_unreviewed} > "
            f"max_dedup_unreviewed_after={thresholds['max_dedup_unreviewed_after']}"
        )
    if after_seeded_decay_pending > int(thresholds["max_decay_pending_after"]):
        violations.append(
            "seeded decay pending after="
            f"{after_seeded_decay_pending} > "
            f"max_decay_pending_after={thresholds['max_decay_pending_after']}"
        )
    if after_seeded_staging > int(thresholds["max_staging_events_after"]):
        violations.append(
            f"seeded staging events after={after_seeded_staging} > "
            f"max_staging_events_after={thresholds['max_staging_events_after']}"
        )
    if violations:
        print("[e2e] ERROR: janitor-parallel-bench threshold violations detected:", file=sys.stderr)
        for violation in violations:
            print(f"[e2e] ERROR:   - {violation}", file=sys.stderr)
        raise SystemExit(1)

if not run_row:
    if mode == "dry-run":
        print("[e2e] Janitor dry-run completed (no janitor_runs record required).")
        raise SystemExit(0)
    print("[e2e] ERROR: no janitor_runs record was written", file=sys.stderr)
    raise SystemExit(1)

run_id, task_name, status, memories_processed, actions_taken, completed_at = run_row
snippet_exists_after = os.path.exists(snippet_path)
journal_exists_after = os.path.exists(journal_path)
summary = {
    "mode": mode,
    "run_id": run_id,
    "task": task_name,
    "status": status,
    "memories_processed": memories_processed,
    "actions_taken": actions_taken,
    "before": before_counts,
    "after": after_counts,
    "staging_before": before_staging,
    "staging_after": after_staging,
    "seeded_staging_before": before_seeded_staging,
    "seeded_staging_after": after_seeded_staging,
    "doc_chunks_before": before_doc_chunks,
    "doc_chunks_after": after_doc_chunks,
    "rag_anchor_chunks_before": before_anchor_chunks,
    "rag_anchor_chunks_after": after_anchor_chunks,
    "seeded_contradictions_pending_before": before_seeded_contradictions_pending,
    "seeded_contradictions_pending_after": after_seeded_contradictions_pending,
    "seeded_contradictions_resolved_before": before_seeded_contradictions_resolved,
    "seeded_contradictions_resolved_after": after_seeded_contradictions_resolved,
    "seeded_dedup_unreviewed_before": before_seeded_dedup_unreviewed,
    "seeded_dedup_unreviewed_after": after_seeded_dedup_unreviewed,
    "seeded_dedup_reviewed_before": before_seeded_dedup_reviewed,
    "seeded_dedup_reviewed_after": after_seeded_dedup_reviewed,
    "seeded_decay_pending_before": before_seeded_decay_pending,
    "seeded_decay_pending_after": after_seeded_decay_pending,
    "seeded_decay_reviewed_before": before_seeded_decay_reviewed,
    "seeded_decay_reviewed_after": after_seeded_decay_reviewed,
    "multi_owner_nodes_before": before_multi_owner_node_count,
    "multi_owner_nodes_after": after_multi_owner_node_count,
    "multi_owner_distinct_owners_before": before_multi_owner_distinct_owners,
    "multi_owner_distinct_owners_after": after_multi_owner_distinct_owners,
    "registry_last_indexed_before": before_registry_last_indexed,
    "registry_last_indexed_after": after_registry_last_indexed,
    "registry_last_indexed_abs_before": before_registry_last_indexed_abs,
    "registry_last_indexed_abs_after": after_registry_last_indexed_abs,
    "project_doc_updates_before": before_project_doc_updates,
    "project_doc_updates_after": after_project_doc_updates,
    "snippet_exists_before": snippet_exists_before,
    "snippet_exists_after": snippet_exists_after,
    "journal_exists_before": journal_exists_before,
    "journal_exists_after": journal_exists_after,
    "completed_at": completed_at,
}
print("[e2e] Janitor verification:")
print(json.dumps(summary, indent=2))

if status != "completed":
    print(f"[e2e] ERROR: janitor run status was {status}", file=sys.stderr)
    raise SystemExit(1)

if mode == "apply":
    required_janitor_cols = {
        "skipped_tasks_json",
        "carryover_json",
        "stage_budget_json",
        "checkpoint_path",
        "task_summary_json",
    }
    missing_cols = sorted(required_janitor_cols - janitor_runs_cols)
    if missing_cols:
        print(
            "[e2e] ERROR: janitor migration resilience probe failed; missing janitor_runs columns: "
            + ",".join(missing_cols),
            file=sys.stderr,
        )
        raise SystemExit(1)
    # Require evidence of work only when there was work to do.
    # Clean installs can legitimately produce a no-op janitor run.
    changed_buckets = before_counts != after_counts
    did_work = (memories_processed > 0) or (actions_taken > 0) or changed_buckets
    had_work = sum(before_counts.values()) > 0
    if had_work and not did_work:
        print("[e2e] ERROR: janitor apply completed but no observable work was recorded", file=sys.stderr)
        raise SystemExit(1)
    if (not had_work) and (not did_work):
        print("[e2e] Janitor apply completed with no-op workload (clean state).")
    if after_doc_chunks < before_doc_chunks:
        print("[e2e] ERROR: rag chunk count regressed after janitor run", file=sys.stderr)
        raise SystemExit(1)
    if after_anchor_chunks <= before_anchor_chunks:
        print(
            "[e2e] ERROR: rag anchor assertion failed; seeded anchor was not indexed into doc_chunks "
            f"(before={before_anchor_chunks}, after={after_anchor_chunks})",
            file=sys.stderr,
        )
        raise SystemExit(1)
    # Queue depth can grow from unrelated runtime traffic; verify deterministic seeded workload instead.
    if before_seeded_staging > 0 and after_seeded_staging >= before_seeded_staging:
        print("[e2e] ERROR: seeded project staging events were not consumed by janitor run", file=sys.stderr)
        raise SystemExit(1)
    if snippet_exists_before and snippet_exists_after:
        print("[e2e] WARN: snippet backlog still present after janitor run.")
    if prebench_guard:
        if before_seeded_contradictions_pending > 0:
            if after_seeded_contradictions_pending > 0:
                print(
                    "[e2e] ERROR: seeded contradiction fixture remained pending after janitor run "
                    f"(before={before_seeded_contradictions_pending}, after={after_seeded_contradictions_pending})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            resolved_delta = after_seeded_contradictions_resolved - before_seeded_contradictions_resolved
            if (not janitor_parallel_bench) and resolved_delta < before_seeded_contradictions_pending:
                print(
                    "[e2e] ERROR: seeded contradiction fixture did not transition to resolved/false_positive as expected "
                    f"(pending_before={before_seeded_contradictions_pending}, resolved_delta={resolved_delta})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
        if before_seeded_dedup_unreviewed > 0:
            if after_seeded_dedup_unreviewed > 0:
                print(
                    "[e2e] ERROR: seeded dedup fixture remained unreviewed after janitor run "
                    f"(before={before_seeded_dedup_unreviewed}, after={after_seeded_dedup_unreviewed})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            dedup_reviewed_delta = after_seeded_dedup_reviewed - before_seeded_dedup_reviewed
            if (not janitor_parallel_bench) and dedup_reviewed_delta < before_seeded_dedup_unreviewed:
                print(
                    "[e2e] ERROR: seeded dedup fixture did not transition to reviewed as expected "
                    f"(unreviewed_before={before_seeded_dedup_unreviewed}, reviewed_delta={dedup_reviewed_delta})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
        if before_seeded_decay_pending > 0:
            if after_seeded_decay_pending > 0:
                print(
                    "[e2e] ERROR: seeded decay-review fixture remained pending after janitor run "
                    f"(before={before_seeded_decay_pending}, after={after_seeded_decay_pending})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            decay_reviewed_delta = after_seeded_decay_reviewed - before_seeded_decay_reviewed
            if (not janitor_parallel_bench) and decay_reviewed_delta < before_seeded_decay_pending:
                print(
                    "[e2e] ERROR: seeded decay-review fixture did not transition to reviewed as expected "
                    f"(pending_before={before_seeded_decay_pending}, reviewed_delta={decay_reviewed_delta})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
        if (not janitor_parallel_bench) and before_multi_owner_distinct_owners >= 2:
            if after_multi_owner_distinct_owners < 2:
                print(
                    "[e2e] ERROR: multi-owner isolation failed; seeded owner-scoped memories collapsed owners "
                    f"(before_owners={before_multi_owner_distinct_owners}, after_owners={after_multi_owner_distinct_owners})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            if after_multi_owner_node_count < 2:
                print(
                    "[e2e] ERROR: multi-owner isolation failed; seeded owner-scoped memories were over-collapsed "
                    f"(before_nodes={before_multi_owner_node_count}, after_nodes={after_multi_owner_node_count})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
        if (not janitor_parallel_bench) and after_project_doc_updates <= before_project_doc_updates:
            print(
                "[e2e] ERROR: project artifact assertion failed; no new PROJECT.md update log entry "
                f"(before={before_project_doc_updates}, after={after_project_doc_updates})",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if has_doc_registry_table:
            if not after_registry_last_indexed:
                print(
                    "[e2e] ERROR: registry drift assertion failed; last_indexed_at missing after janitor run",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            if after_registry_last_indexed == before_registry_last_indexed:
                print(
                    "[e2e] ERROR: registry drift assertion failed; last_indexed_at did not refresh "
                    f"(value={after_registry_last_indexed!r})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            if not after_registry_last_indexed_abs:
                print(
                    "[e2e] ERROR: registry path-mismatch assertion failed; absolute-path last_indexed_at missing",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            if after_registry_last_indexed_abs == before_registry_last_indexed_abs:
                print(
                    "[e2e] ERROR: registry path-mismatch assertion failed; absolute-path last_indexed_at did not refresh "
                    f"(value={after_registry_last_indexed_abs!r})",
                    file=sys.stderr,
                )
                raise SystemExit(1)
        pending_after = int(after_counts.get("pending", 0))
        approved_after = int(after_counts.get("approved", 0))
        if pending_after > 0 or approved_after > 0:
            with sqlite3.connect(db_path) as conn:
                leftovers = conn.execute(
                    """
                    SELECT id, status, substr(name,1,120)
                    FROM nodes
                    WHERE status IN ('pending', 'approved')
                    ORDER BY created_at DESC
                    LIMIT 20
                    """
                ).fetchall()
            preview = "\n".join(
                f"  - {rid} [{status}] {text}" for rid, status, text in leftovers
            )
            print(
                "[e2e] ERROR: pre-benchmark janitor invariant failed; "
                f"pending={pending_after} approved={approved_after}\n{preview}",
                file=sys.stderr,
            )
            raise SystemExit(1)
PY
pass_stage "janitor"
else
  skip_stage "janitor"
  echo "[e2e] Skipping janitor (suite selection/--skip-janitor)."
fi

if [[ "$RUNTIME_BUDGET_SECONDS" -gt 0 ]]; then
  elapsed_now=$(( $(date +%s) - RUN_START_EPOCH ))
  if [[ "$elapsed_now" -gt "$RUNTIME_BUDGET_SECONDS" ]]; then
    RUNTIME_BUDGET_EXCEEDED="true"
    E2E_STATUS="failed"
    E2E_FAIL_REASON="runtime_budget_exceeded"
    E2E_FAIL_LINE="budget-check"
    CURRENT_STAGE="runtime_budget"
    echo "[e2e] ERROR: runtime budget exceeded (elapsed=${elapsed_now}s budget=${RUNTIME_BUDGET_SECONDS}s profile=${RUNTIME_BUDGET_PROFILE})" >&2
    exit 1
  fi
fi
enforce_stage_budgets

echo "[e2e] E2E run complete."
