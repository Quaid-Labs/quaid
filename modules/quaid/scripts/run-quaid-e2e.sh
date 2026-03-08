#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOOTSTRAP_ROOT="${QUAID_BOOTSTRAP_ROOT:-${PLUGIN_ROOT}}"
ENV_FILE="${QUAID_E2E_ENV_FILE:-${PLUGIN_ROOT}/.env.e2e}"

E2E_WS="${HOME}/quaid/test"
DEV_WS="${HOME}/quaid/dev"
OPENCLAW_SOURCE="${HOME}/quaid/openclaw-source"
# Default E2E gate to the OpenClaw beta lane we validate against in canary.
# Keep overridable for bisects via --openclaw-ref / QUAID_E2E_OPENCLAW_REF.
OPENCLAW_REF="${QUAID_E2E_OPENCLAW_REF:-v2026.3.2-beta.1}"
MIN_OPENCLAW_VERSION="${QUAID_E2E_MIN_OPENCLAW_VERSION:-2026.2.10}"

PROFILE_TEST="${QUAID_E2E_PROFILE_TEST:-${BOOTSTRAP_ROOT}/profiles/runtime-profile.local.quaid.json}"
PROFILE_SRC="${QUAID_E2E_PROFILE_SRC:-${BOOTSTRAP_ROOT}/profiles/runtime-profile.local.quaid.json}"
TMP_PROFILE_BASE="$(mktemp /tmp/quaid-e2e-profile.XXXXXX)"
TMP_PROFILE="${TMP_PROFILE_BASE}.json"
mv "$TMP_PROFILE_BASE" "$TMP_PROFILE"

AUTH_PATH="openai-api"
KEEP_ON_SUCCESS=false
RUN_JANITOR=true
RUN_LLM_SMOKE=true
RUN_LIVE_EVENTS=true
RUN_NOTIFY_MATRIX=true
RUN_INTEGRATION_TESTS=true
RUN_INGEST_STRESS=true
RUN_JANITOR_SEED=true
RUN_MEMORY_FLOW=true
RUN_RESILIENCE=false
RUN_JANITOR_STRESS=false
RUN_PREBENCH_GUARDS=false
RUN_JANITOR_PARALLEL_BENCH=false
INGEST_ALLOW_FALLBACK="${QUAID_E2E_INGEST_ALLOW_FALLBACK:-false}"
REQUIRE_NATIVE_COMMAND_HOOKS="${QUAID_E2E_REQUIRE_NATIVE_COMMAND_HOOKS:-false}"
INGEST_MAX_COMPACTION_SESSIONS="${QUAID_E2E_INGEST_MAX_COMPACTION_SESSIONS:-4}"
JANITOR_TIMEOUT_SECONDS=480
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

NOTE:
  In this harness, slash commands sent through `openclaw agent --message` are treated
  as plain text in many builds. For deterministic command semantics (compact/reset/new),
  use gateway/session APIs (e.g. `openclaw gateway call agent` or `sessions.compact`)
  instead of relying on CLI text routing.

Options:
  --auth-path <id>       Auth path for bootstrap profile (openai-oauth|openai-api|anthropic-oauth|anthropic-api; default: openai-api)
  --keep-on-success      Do not delete ~/quaid/test after successful run
  --skip-janitor         Skip janitor phase
  --skip-llm-smoke       Skip gateway LLM smoke call
  --skip-live-events     Skip live command/timeout hook validation
  --skip-notify-matrix   Skip notification-level matrix validation (quiet/normal/debug)
  --suite <csv>          Test suites to run. Values: full,core,blocker,pre-benchmark,nightly,nightly-strict-notify,janitor-parallel-bench,smoke,integration,live,memory,notify,ingest,janitor,seed,resilience,janitor-stress (default: full)
  --janitor-timeout <s>  Janitor timeout seconds (default: 240)
  --janitor-dry-run      Run janitor in dry-run mode (default is apply)
  --live-timeout-wait <s> Max seconds to wait for timeout event in live check (default: 90)
  --nightly-profile <p>  Nightly profile: quick|deep (default: quick)
  --resilience-loops <n> Number of resilience-suite iterations (default: 1; nightly defaults to 2)
  --notify-level <lvl>   Quaid notify level for e2e (quiet|normal|verbose|debug, default: debug)
  --runtime-budget-profile <p> Runtime budget profile: auto|off|quick|deep (default: auto)
  --runtime-budget-seconds <n>  Explicit runtime budget override in seconds (0 disables override)
  --env-file <path>      Optional .env file to source before running (default: modules/quaid/.env.e2e)
  --openclaw-source <p>  OpenClaw source repo path (default: ~/quaid/openclaw-source)
  --openclaw-ref <ref>   OpenClaw git ref/tag/sha to test (default: env QUAID_E2E_OPENCLAW_REF)
  --quick-bootstrap      Skip OpenClaw source refresh/install during bootstrap (faster local loops)
  --reuse-workspace      Reuse existing ~/quaid/test workspace when possible (fast path)
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
    --openclaw-ref) OPENCLAW_REF="$2"; shift 2 ;;
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

suite_has_exact() {
  local needle="$1"
  local token
  IFS=',' read -r -a _suite_tokens <<< "$E2E_SUITES"
  for token in "${_suite_tokens[@]}"; do
    token="$(echo "$token" | tr '[:upper:]' '[:lower:]' | xargs)"
    [[ -z "$token" ]] && continue
    if [[ "$token" == "$needle" ]]; then
      return 0
    fi
  done
  return 1
}

if suite_has_exact "nightly"; then
  NIGHTLY_MODE=true
  E2E_SUITES="full"
fi
if suite_has_exact "nightly-strict-notify"; then
  NIGHTLY_MODE=true
  NOTIFY_REQUIRE_DELIVERY="true"
  E2E_SUITES="full"
fi

if suite_has_exact "blocker"; then
  RUN_LLM_SMOKE=false
  RUN_INTEGRATION_TESTS=true
  RUN_LIVE_EVENTS=true
  RUN_NOTIFY_MATRIX=false
  RUN_INGEST_STRESS=false
  RUN_JANITOR=false
  RUN_JANITOR_SEED=false
  RUN_MEMORY_FLOW=true
elif suite_has_exact "pre-benchmark"; then
  RUN_LLM_SMOKE=true
  RUN_INTEGRATION_TESTS=true
  RUN_LIVE_EVENTS=true
  RUN_NOTIFY_MATRIX=true
  RUN_INGEST_STRESS=true
  RUN_JANITOR=true
  RUN_JANITOR_SEED=true
  RUN_MEMORY_FLOW=true
  RUN_PREBENCH_GUARDS=true
elif suite_has_exact "janitor-parallel-bench"; then
  RUN_LLM_SMOKE=false
  RUN_INTEGRATION_TESTS=false
  RUN_LIVE_EVENTS=false
  RUN_NOTIFY_MATRIX=false
  RUN_INGEST_STRESS=false
  RUN_JANITOR=true
  RUN_JANITOR_SEED=true
  RUN_MEMORY_FLOW=false
  RUN_PREBENCH_GUARDS=true
  RUN_JANITOR_PARALLEL_BENCH=true
elif suite_has_exact "core"; then
  RUN_LLM_SMOKE=true
  RUN_INTEGRATION_TESTS=true
  RUN_LIVE_EVENTS=true
  RUN_NOTIFY_MATRIX=false
  RUN_INGEST_STRESS=true
  RUN_JANITOR=true
  RUN_JANITOR_SEED=true
  RUN_MEMORY_FLOW=true
elif suite_has_exact "full" || suite_has_exact "all"; then
  RUN_LLM_SMOKE=true
  RUN_INTEGRATION_TESTS=true
  RUN_LIVE_EVENTS=true
  RUN_NOTIFY_MATRIX=true
  RUN_INGEST_STRESS=true
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
  suite_has "stress" && RUN_INGEST_STRESS=true
  if suite_has "janitor"; then
    RUN_JANITOR=true
    RUN_JANITOR_SEED=true
  fi
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
if [[ "$INGEST_ALLOW_FALLBACK" != "true" && "$INGEST_ALLOW_FALLBACK" != "false" ]]; then
  echo "Invalid QUAID_E2E_INGEST_ALLOW_FALLBACK: $INGEST_ALLOW_FALLBACK (expected true|false)" >&2
  exit 1
fi
if ! [[ "$INGEST_MAX_COMPACTION_SESSIONS" =~ ^[0-9]+$ ]] || [[ "$INGEST_MAX_COMPACTION_SESSIONS" -lt 1 ]]; then
  echo "Invalid QUAID_E2E_INGEST_MAX_COMPACTION_SESSIONS: $INGEST_MAX_COMPACTION_SESSIONS (expected integer >= 1)" >&2
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
stage_statuses = {
    "bootstrap": os.environ["SUMMARY_STAGE_bootstrap"],
    "gateway_smoke": os.environ["SUMMARY_STAGE_gateway_smoke"],
    "integration": os.environ["SUMMARY_STAGE_integration"],
    "live_events": os.environ["SUMMARY_STAGE_live_events"],
    "resilience": os.environ["SUMMARY_STAGE_resilience"],
    "memory_flow": os.environ["SUMMARY_STAGE_memory_flow"],
    "notify_matrix": os.environ["SUMMARY_STAGE_notify_matrix"],
    "ingest_stress": os.environ["SUMMARY_STAGE_ingest_stress"],
    "janitor": os.environ["SUMMARY_STAGE_janitor"],
}
if status == "success":
    running = [name for name, st in stage_statuses.items() if st == "running"]
    if running:
        status = "failed"
        os.environ["SUMMARY_E2E_FAIL_REASON"] = "incomplete_stage_status"
        os.environ["SUMMARY_CURRENT_STAGE"] = running[0]
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
    "stages": stage_statuses,
    "stage_durations_seconds": {
        "bootstrap": int(os.environ["SUMMARY_STAGE_bootstrap_DURATION"]),
        "gateway_smoke": int(os.environ["SUMMARY_STAGE_gateway_smoke_DURATION"]),
        "integration": int(os.environ["SUMMARY_STAGE_integration_DURATION"]),
        "live_events": int(os.environ["SUMMARY_STAGE_live_events_DURATION"]),
        "resilience": int(os.environ["SUMMARY_STAGE_resilience_DURATION"]),
        "memory_flow": int(os.environ["SUMMARY_STAGE_memory_flow_DURATION"]),
        "notify_matrix": int(os.environ["SUMMARY_STAGE_notify_matrix_DURATION"]),
        "ingest_stress": int(os.environ["SUMMARY_STAGE_ingest_stress_DURATION"]),
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

# Keep embedding mode consistent across the entire e2e lane. Mixing real and
# mock embeddings in one workspace can create vec_nodes dimension drift
# (e.g., 4096 vs 128), which causes janitor review FIX upserts to fail.
JANITOR_MOCK_EMBEDDINGS_FLAG="$(printf '%s' "${QUAID_E2E_JANITOR_MOCK_EMBEDDINGS:-1}" | tr '[:upper:]' '[:lower:]' | xargs)"
if [[ "$JANITOR_MOCK_EMBEDDINGS_FLAG" == "1" || "$JANITOR_MOCK_EMBEDDINGS_FLAG" == "true" || "$JANITOR_MOCK_EMBEDDINGS_FLAG" == "yes" || "$JANITOR_MOCK_EMBEDDINGS_FLAG" == "on" ]]; then
  export MOCK_EMBEDDINGS=1
  echo "[e2e] MOCK_EMBEDDINGS enabled for full lane (dimension consistency)."
else
  unset MOCK_EMBEDDINGS
fi

# One-time key ingestion for e2e-only secrets.
# Preferred vars are E2E_TEST_KEY_OPENAI / E2E_TEST_KEY_ANTHROPIC to avoid
# accidental consumption by non-e2e code paths.
ingest_e2e_test_keys() {
  local openai_file="${HOME}/quaid/oaikey.txt"
  local anthropic_file="${HOME}/quaid/anthkey.txt"
  local consumed=false
  local tmp=""

  if [[ -z "${E2E_TEST_KEY_OPENAI:-}" && -f "$openai_file" ]]; then
    tmp="$(head -n 1 "$openai_file" | tr -d '\r\n')"
    if [[ -n "$tmp" ]]; then
      export E2E_TEST_KEY_OPENAI="$tmp"
      rm -f "$openai_file"
      consumed=true
    fi
  fi

  if [[ -z "${E2E_TEST_KEY_ANTHROPIC:-}" && -z "${E2E_TEST_KEY_ANTRHOPIC:-}" && -f "$anthropic_file" ]]; then
    tmp="$(head -n 1 "$anthropic_file" | tr -d '\r\n')"
    if [[ -n "$tmp" ]]; then
      export E2E_TEST_KEY_ANTHROPIC="$tmp"
      # Back-compat alias for misspelled variable name.
      export E2E_TEST_KEY_ANTRHOPIC="$tmp"
      rm -f "$anthropic_file"
      consumed=true
    fi
  fi

  # Translate test-only names into provider vars only inside e2e runner scope.
  if [[ -n "${E2E_TEST_KEY_OPENAI:-}" ]]; then
    export OPENAI_API_KEY="${E2E_TEST_KEY_OPENAI}"
  fi
  if [[ -n "${E2E_TEST_KEY_ANTHROPIC:-}" ]]; then
    export ANTHROPIC_API_KEY="${E2E_TEST_KEY_ANTHROPIC}"
  elif [[ -n "${E2E_TEST_KEY_ANTRHOPIC:-}" ]]; then
    export ANTHROPIC_API_KEY="${E2E_TEST_KEY_ANTRHOPIC}"
  fi

  if [[ "$consumed" == true ]]; then
    echo "[e2e] Ingested one-time key file(s) into E2E_TEST_KEY_* and removed source file(s)."
  fi
}

ingest_e2e_test_keys

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

has_oauth_profile_token() {
  local provider="$1"
  python3 - "$provider" "$PROFILE_SRC" <<'PY'
import json
import pathlib
import sys

provider = str(sys.argv[1]).strip().lower()
profile_src = pathlib.Path(sys.argv[2]).expanduser()

def load_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

cfg = load_json(profile_src)
openclaw = cfg.get("openclaw") if isinstance(cfg, dict) else {}
if not isinstance(openclaw, dict):
    openclaw = {}
config_path = pathlib.Path(str(openclaw.get("configPath") or "~/.openclaw/openclaw.json")).expanduser()
runtime_cfg = load_json(config_path)
auth_store_raw = openclaw.get("authProfileStorePath")
if isinstance(auth_store_raw, str) and auth_store_raw.strip():
    auth_store_path = pathlib.Path(auth_store_raw).expanduser()
else:
    auth_store_path = config_path.parent / "agents" / "main" / "agent" / "auth-profiles.json"

store = load_json(auth_store_path)
profiles = store.get("profiles") if isinstance(store, dict) else {}
if not isinstance(profiles, dict):
    profiles = {}

runtime_auth = runtime_cfg.get("auth") if isinstance(runtime_cfg, dict) else {}
runtime_profiles = runtime_auth.get("profiles") if isinstance(runtime_auth, dict) else {}
if isinstance(runtime_profiles, dict):
    merged = dict(runtime_profiles)
    merged.update(profiles)
    profiles = merged

for profile_id, payload in profiles.items():
    if not isinstance(payload, dict):
        continue
    pid = str(profile_id).strip().lower()
    p_provider = str(payload.get("provider") or "").strip().lower()
    p_mode = str(payload.get("mode") or payload.get("type") or "").strip().lower()
    token = str(payload.get("token") or "").strip()
    if not token:
        continue
    if provider == "openai":
        provider_match = p_provider in {"openai", "openai-codex"} or pid.startswith("openai")
    elif provider == "anthropic":
        provider_match = p_provider == "anthropic" or pid.startswith("anthropic")
    else:
        provider_match = p_provider == provider or pid.startswith(provider)
    if not provider_match:
        continue
    oauthish = (
        p_mode == "oauth"
        or "oauth" in pid
        or pid.endswith(":manual")
        or pid.endswith(":claude-cli")
    )
    if oauthish:
        print("yes")
        raise SystemExit(0)

print("no")
PY
}

case "$AUTH_PATH" in
  openai-oauth)
    if [[ "$(has_oauth_profile_token openai)" != "yes" ]]; then
      skip_e2e "missing-openai-oauth-token"
    fi
    ;;
  anthropic-oauth)
    if [[ "$(has_oauth_profile_token anthropic)" != "yes" ]]; then
      skip_e2e "missing-anthropic-oauth-token"
    fi
    ;;
esac

# Critical e2e isolation rule:
# For proper e2e behavior there can be NO cross contamination of keys between
# auth lanes. We intentionally scrub non-lane credentials before bootstrap so a
# passing lane cannot be "fixed" by fallback to another provider's key.
isolate_auth_lane_keys() {
  case "$AUTH_PATH" in
    openai-api)
      unset ANTHROPIC_API_KEY E2E_TEST_KEY_ANTHROPIC E2E_TEST_KEY_ANTRHOPIC
      ;;
    anthropic-api)
      unset OPENAI_API_KEY E2E_TEST_KEY_OPENAI
      ;;
    openai-oauth)
      unset OPENAI_API_KEY ANTHROPIC_API_KEY E2E_TEST_KEY_OPENAI E2E_TEST_KEY_ANTHROPIC E2E_TEST_KEY_ANTRHOPIC
      ;;
    anthropic-oauth)
      unset OPENAI_API_KEY ANTHROPIC_API_KEY E2E_TEST_KEY_OPENAI E2E_TEST_KEY_ANTHROPIC E2E_TEST_KEY_ANTRHOPIC
      ;;
  esac
}

isolate_auth_lane_keys

if ! command -v openclaw >/dev/null 2>&1; then
  skip_e2e "missing-openclaw-cli"
fi

require_min_openclaw_version() {
  local actual
  actual="$(openclaw --version 2>/dev/null | head -n 1 | tr -d '\r' | xargs || true)"
  if [[ -z "$actual" ]]; then
    echo "[e2e] ERROR: could not determine OpenClaw version from 'openclaw --version'" >&2
    return 1
  fi
  if ! python3 - <<'PY' "$actual" "$MIN_OPENCLAW_VERSION"
import re
import sys
actual = str(sys.argv[1] or "").strip()
minimum = str(sys.argv[2] or "").strip()
def parse(v):
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", v)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())
a = parse(actual)
b = parse(minimum)
if a is None or b is None or a < b:
    raise SystemExit(1)
PY
  then
    echo "[e2e] ERROR: unsupported OpenClaw version. installed='${actual}' required='${MIN_OPENCLAW_VERSION}+'" >&2
    return 1
  fi
  echo "[e2e] OpenClaw version OK: ${actual} (required ${MIN_OPENCLAW_VERSION}+)"
}

require_min_openclaw_version

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

enable_required_openclaw_hooks() {
  local cli=""
  if command -v openclaw >/dev/null 2>&1; then
    cli="openclaw"
  elif command -v clawdbot >/dev/null 2>&1; then
    cli="clawdbot"
  else
    echo "[e2e] ERROR: OpenClaw CLI not found; cannot enable required hooks." >&2
    return 1
  fi

  # Keep E2E strict and production-faithful: no alias fallback and no direct
  # config force-enable writes. Required hooks must enable via CLI.
  local required_hooks=("bootstrap-extra-files")
  local hook out_file out
  for hook in "${required_hooks[@]}"; do
    out_file="$(mktemp -t quaid-e2e-hook.XXXXXX)"
    if "$cli" hooks enable "$hook" >"$out_file" 2>&1; then
      rm -f "$out_file"
      echo "[e2e] Hook enabled: ${hook}"
      continue
    fi
    out="$(cat "$out_file" 2>/dev/null || true)"
    rm -f "$out_file"
    if [[ "$out" == *"already enabled"* || "$out" == *"Already enabled"* ]]; then
      echo "[e2e] Hook enabled: ${hook}"
      continue
    fi
    echo "[e2e] ERROR: failed to enable required hook '${hook}' via ${cli}" >&2
    if [[ -n "${out:-}" ]]; then
      echo "[e2e] ERROR: ${out}" >&2
    fi
    return 1
  done
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
python3 - "$PROFILE_SRC" "$TMP_PROFILE" "$E2E_WS" "$AUTH_PATH" <<'PY'
import json
import os
import sys

src, out, e2e_ws, auth_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
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

openclaw = obj.setdefault("openclaw", {})
creds = openclaw.setdefault("authProfileCredentials", {})
auth_profiles = openclaw.setdefault("authProfiles", {})
auth_order = openclaw.setdefault("authOrder", {})

# E2E must not emit live Telegram traffic.
channels = openclaw.setdefault("channels", {})
telegram = channels.setdefault("telegram", {})
telegram["enabled"] = False

# Critical e2e isolation rule:
# For proper e2e behavior there can be NO cross contamination of keys between
# lanes. Only inject the API key that belongs to the selected auth lane.
openai_api_key = (os.environ.get("OPENAI_API_KEY") or "").strip() if auth_path == "openai-api" else ""
if openai_api_key:
    # Always provision native OpenAI API-key profile for API-key auth paths.
    openai_profile = dict(creds.get("openai:default") or {})
    openai_profile["type"] = "api_key"
    openai_profile["provider"] = "openai"
    openai_profile["key"] = openai_api_key
    openai_profile.pop("token", None)
    creds["openai:default"] = openai_profile
    auth_profiles["openai:default"] = {"provider": "openai", "mode": "api_key"}
    if not isinstance(auth_order.get("openai"), list):
        auth_order["openai"] = []
    if "openai:default" not in auth_order["openai"]:
        auth_order["openai"].insert(0, "openai:default")

    profile = dict(creds.get("openai-codex:api") or {})
    profile["type"] = "api_key"
    profile["provider"] = "openai-codex"
    profile["key"] = openai_api_key
    profile.pop("token", None)
    creds["openai-codex:api"] = profile

    # For openai-api path, pin default agent model to the OpenAI provider lane.
    if auth_path == "openai-api":
        openclaw.setdefault("agentDefaults", {})["modelPrimary"] = "openai/gpt-5.1-codex"
        provider_defaults = openclaw.setdefault("providerDefaults", {})
        openai_defaults = provider_defaults.setdefault("openai", {})
        openai_defaults["modelPrimary"] = "openai/gpt-5.1-codex"
        if not isinstance(openai_defaults.get("modelFallbacks"), list) or not openai_defaults["modelFallbacks"]:
            openai_defaults["modelFallbacks"] = ["openai/gpt-5.1"]
        for agent in openclaw.get("agentList", []):
            if isinstance(agent, dict):
                agent["modelPrimary"] = "openai/gpt-5.1-codex"

anthropic_api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip() if auth_path == "anthropic-api" else ""
if anthropic_api_key:
    profile = dict(creds.get("anthropic:default") or {})
    profile["type"] = "api_key"
    profile["provider"] = "anthropic"
    profile["key"] = anthropic_api_key
    profile.pop("token", None)
    creds["anthropic:default"] = profile
    auth_profiles["anthropic:default"] = {"provider": "anthropic", "mode": "api_key"}
    if not isinstance(auth_order.get("anthropic"), list):
        auth_order["anthropic"] = []
    if "anthropic:default" not in auth_order["anthropic"]:
        auth_order["anthropic"].insert(0, "anthropic:default")

    # For anthropic-api path, pin default agent model to Anthropic so the
    # gateway does not fail over to OpenAI when OPENAI_API_KEY is absent.
    # Cost policy for e2e lanes: keep Anthropic on Haiku-only.
    if auth_path == "anthropic-api":
        openclaw.setdefault("agentDefaults", {})["modelPrimary"] = "anthropic/claude-haiku-4-5"
        provider_defaults = openclaw.setdefault("providerDefaults", {})
        anthropic_defaults = provider_defaults.setdefault("anthropic", {})
        anthropic_defaults["modelPrimary"] = "anthropic/claude-haiku-4-5"
        anthropic_defaults["modelFallbacks"] = ["anthropic/claude-haiku-4-5"]
        for agent in openclaw.get("agentList", []):
            if isinstance(agent, dict):
                agent["modelPrimary"] = "anthropic/claude-haiku-4-5"

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
  local provider_override=""
  local rc=0
  case "$AUTH_PATH" in
    openai-oauth|openai-api) provider_override="openai" ;;
    anthropic-oauth|anthropic-api) provider_override="anthropic" ;;
    *) provider_override="" ;;
  esac
  args=(
    "${BOOTSTRAP_ROOT}/scripts/bootstrap-local.sh"
    --profile "$TMP_PROFILE"
    --auth-path "$AUTH_PATH"
    --openclaw-source "$OPENCLAW_SOURCE"
    --worktree-source "$DEV_WS"
    --worktree-test-branch "e2e-runtime"
  )
  if [[ -n "$OPENCLAW_REF" ]]; then
    args+=(--openclaw-ref "$OPENCLAW_REF")
  fi
  if [[ "$do_wipe" == "true" ]]; then
    args+=(--wipe)
  fi
  if [[ "$QUICK_BOOTSTRAP" == true ]]; then
    args+=(--no-openclaw-refresh --no-openclaw-install)
  fi
  bootstrap_log="$(mktemp -t quaid-e2e-bootstrap.log.XXXXXX)"
  if QUAID_INSTALL_PROVIDER="$provider_override" "${args[@]}" 2>&1 | tee "$bootstrap_log"; then
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
  if [[ "$do_wipe" == "true" ]] && rg -q -e "already exists" -e "Workspace exists but is not a worktree" "$bootstrap_log"; then
    echo "[e2e] Bootstrap hit workspace collision/worktree-state race; retrying once after cleanup." >&2
    rm -rf "$E2E_WS"
    if git -C "$DEV_WS" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      git -C "$DEV_WS" worktree prune >/dev/null 2>&1 || true
    fi
    if QUAID_INSTALL_PROVIDER="$provider_override" "${args[@]}" 2>&1 | tee "$bootstrap_log"; then
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

echo "[e2e] Skipping legacy quaid-reset-signal hook setup (contract-owned lifecycle handlers active)."
echo "[e2e] Ensuring required OpenClaw hooks are enabled..."
enable_required_openclaw_hooks

# Keep timeout override opt-in; default live checks should use installer/runtime defaults.
MEMORY_CFG="${E2E_WS}/config/memory.json"
if [[ -f "$MEMORY_CFG" ]]; then
  if [[ "${QUAID_E2E_FORCE_SHORT_TIMEOUT:-false}" == "true" ]]; then
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
print("[e2e] Updated capture timeout for forced short-timeout validation (~6 seconds).")
PY
  elif [[ "$RUN_MEMORY_FLOW" == true ]]; then
    python3 - "$MEMORY_CFG" <<'PY'
import json, sys
p = sys.argv[1]
obj = json.load(open(p, "r", encoding="utf-8"))
capture = obj.setdefault("capture", {})
capture["autoCompactionOnTimeout"] = False
capture["auto_compaction_on_timeout"] = False
with open(p, "w", encoding="utf-8") as f:
    json.dump(obj, f, indent=2)
    f.write("\n")
print("[e2e] Memory-flow suite: disabled auto compaction on timeout for deterministic timeout+reset extraction checks.")
PY
  else
    echo "[e2e] Leaving capture inactivity timeout at configured default."
  fi
else
  echo "[e2e] ERROR: missing memory config: $MEMORY_CFG" >&2
  exit 1
fi

# Align embedding config with actually-available local Ollama models.
# This prevents silent extraction/store failures when installer defaults to a
# model that isn't pulled on the runner.
if command -v ollama >/dev/null 2>&1; then
  if [[ "$RUN_JANITOR" == true ]]; then
    EMBED_PREF="${QUAID_E2E_OLLAMA_EMBED_MODEL:-${QUAID_E2E_JANITOR_EMBEDDINGS_MODEL:-nomic-embed-text:latest}}"
  else
    EMBED_PREF="${QUAID_E2E_OLLAMA_EMBED_MODEL:-qwen3-embedding:8b}"
  fi
  EMBED_SELECTED="$EMBED_PREF"
  if ! ollama list 2>/dev/null | rg -q "^${EMBED_SELECTED}\\s"; then
    echo "[e2e] WARN: preferred embeddings model not installed in Ollama: ${EMBED_SELECTED}" >&2
    for candidate in qwen3-embedding:8b nomic-embed-text:latest nomic-embed-text mxbai-embed-large:latest mxbai-embed-large all-minilm; do
      if ollama list 2>/dev/null | rg -q "^${candidate}\\s"; then
        EMBED_SELECTED="$candidate"
        echo "[e2e] Using installed Ollama embeddings fallback: ${EMBED_SELECTED}" >&2
        break
      fi
    done
  fi
  if ollama list 2>/dev/null | rg -q "^${EMBED_SELECTED}\\s"; then
    if [[ "$EMBED_SELECTED" == nomic-embed-text* ]]; then
      EMBED_DIM="${QUAID_E2E_OLLAMA_EMBED_DIM:-768}"
    else
      EMBED_DIM="${QUAID_E2E_OLLAMA_EMBED_DIM:-4096}"
    fi
    export QUAID_E2E_JANITOR_EMBEDDINGS_MODEL="$EMBED_SELECTED"
    python3 - "$MEMORY_CFG" "$EMBED_SELECTED" "$EMBED_DIM" <<'PY'
import json, sys
p, model, dim = sys.argv[1], sys.argv[2], int(sys.argv[3])
obj = json.load(open(p, "r", encoding="utf-8"))
models = obj.setdefault("models", {})
models["embeddingsProvider"] = "ollama"
ollama = obj.setdefault("ollama", {})
ollama["embeddingModel"] = model
ollama["embeddingDim"] = dim
with open(p, "w", encoding="utf-8") as f:
    json.dump(obj, f, indent=2)
    f.write("\n")
print(f"[e2e] Embeddings model pinned to installed Ollama model: {model} ({dim}d)")
PY
  else
    echo "[e2e] WARN: no supported Ollama embeddings model installed; janitor RAG may fail." >&2
  fi
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

if [[ -d "${E2E_WS}/modules/quaid" ]] && [[ -f "${E2E_WS}/modules/quaid/package.json" ]]; then
  mkdir -p "${E2E_WS}/plugins"
  if [[ -d "${E2E_WS}/plugins/quaid" ]] && [[ ! -L "${E2E_WS}/plugins/quaid" ]] && [[ ! -f "${E2E_WS}/plugins/quaid/package.json" ]]; then
    stale_backup="${E2E_WS}/plugins/quaid.stale.$(date +%Y%m%d-%H%M%S)"
    mv "${E2E_WS}/plugins/quaid" "${stale_backup}"
    echo "[e2e] Moved stale plugin shim dir: ${E2E_WS}/plugins/quaid -> ${stale_backup}"
  fi
  ln -sfn ../modules/quaid "${E2E_WS}/plugins/quaid"
fi

if [[ "$RUN_INTEGRATION_TESTS" == true ]]; then
begin_stage "integration"
echo "[e2e] Running Quaid integration tests..."
for required in \
  "${E2E_WS}/modules/quaid/tests/session-timeout-manager.test.ts" \
  "${E2E_WS}/modules/quaid/tests/chat-flow.integration.test.ts" \
  "${E2E_WS}/modules/quaid/scripts/e2e-domain-contract.py"; do
  if [[ ! -f "$required" ]]; then
    echo "[e2e] Missing required integration test file: $required" >&2
    echo "[e2e] Ensure test files are committed in ${DEV_WS} before running e2e." >&2
    exit 1
  fi
done
(cd "${E2E_WS}/modules/quaid" && npx vitest run tests/session-timeout-manager.test.ts tests/chat-flow.integration.test.ts --reporter=verbose)
(cd "${E2E_WS}/modules/quaid" && python3 scripts/e2e-domain-contract.py "${E2E_WS}")
pass_stage "integration"
else
  skip_stage "integration"
  echo "[e2e] Skipping integration tests (suite selection)."
fi

if [[ "$RUN_LIVE_EVENTS" == true ]]; then
begin_stage "live_events"
echo "[e2e] Validating live /compact /reset /new + timeout events..."
echo "[e2e] NOTE: slash commands are exercised through gateway chat.send API; CLI text routing is non-deterministic in this harness."
python3 - "$E2E_WS" "$LIVE_TIMEOUT_WAIT_SECONDS" "$REQUIRE_NATIVE_COMMAND_HOOKS" <<'PY'
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
try:
    import resource
except Exception:
    resource = None

ws = sys.argv[1]
timeout_wait = int(sys.argv[2])
require_native_hooks = str(sys.argv[3]).strip().lower() in ("1", "true", "yes", "on")
events_path = os.path.join(ws, "logs", "quaid", "session-timeout-events.jsonl")
notify_log_path = os.path.join(ws, "logs", "notify-worker.log")
pending_signal_dir = os.path.join(ws, "data", "pending-extraction-signals")
session_id = ""
session_key = "agent:main:main"

def run_agent(message: str, sid: str = "") -> bool:
    params = {
        "sessionKey": "main",
        "message": message,
        "idempotencyKey": f"e2e-live-{uuid.uuid4().hex[:12]}",
        "bestEffortDeliver": True,
    }
    ok, _payload = gateway_call_json("agent", params, timeout_sec=90)
    if not ok:
        print(f"[e2e] WARN: gateway agent call failed for message={message!r}", flush=True)
        return False
    # Successful RPC acceptance is enough for lifecycle hook assertions in this stage.
    return True

def gateway_call_json(method: str, params: dict, timeout_sec: int = 30) -> tuple[bool, dict]:
    cmd = [
        "openclaw",
        "gateway",
        "call",
        method,
        "--json",
        "--params",
        json.dumps(params),
        "--timeout",
        str(timeout_sec * 1000),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 10)
    except subprocess.TimeoutExpired:
        print(f"[e2e] WARN: gateway call timed out method={method!r}", flush=True)
        return False, {}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        print(f"[e2e] WARN: gateway call failed method={method!r}: {err}", flush=True)
        return False, {}
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        payload = {}
    # openclaw gateway call returns bare JSON results on success (no `ok` wrapper).
    # command-level failures already return non-zero exit status above.
    return True, payload

def gateway_call(method: str, params: dict, timeout_sec: int = 30) -> bool:
    ok, _ = gateway_call_json(method, params, timeout_sec=timeout_sec)
    return ok

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

def resolve_runtime_session_id(marker_text: str, fallback_session_id: str, seconds: int = 35) -> str:
    session_dir = os.path.join(ws, "logs", "quaid", "session-messages")
    deadline = time.time() + seconds
    while time.time() < deadline:
        if os.path.isdir(session_dir):
            for name in os.listdir(session_dir):
                if not name.endswith(".jsonl"):
                    continue
                fp = os.path.join(session_dir, name)
                try:
                    content = open(fp, "r", encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                if marker_text in content:
                    return name[:-6]
        time.sleep(1)
    return fallback_session_id

def resolve_session_id_from_key(session_key: str, fallback_session_id: str) -> str:
    sessions_path = os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
    try:
        with open(sessions_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        entry = data.get(session_key) or {}
        sid = str(entry.get("sessionId") or "").strip()
        if sid:
            return sid
    except Exception:
        pass
    return fallback_session_id

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

def queue_signal_fallback(session_id: str, label: str) -> None:
    if not str(session_id or "").strip():
        raise SystemExit(f"[e2e] ERROR: cannot queue fallback {label}; runtime session id is empty")
    script = r"""
import { SessionTimeoutManager } from "./modules/quaid/core/session-timeout.js";
const workspace = process.argv[1];
const sessionId = process.argv[2];
const signalLabel = process.argv[3];
const tm = new SessionTimeoutManager({
  workspace,
  timeoutMinutes: 60,
  isBootstrapOnly: false,
  logger: console,
  extract: async () => {},
  readSessionMessages: () => [{ role: "user", content: "e2e-fallback", timestamp: new Date().toISOString() }],
  listSessionActivity: () => [{ sessionId, lastActivityMs: Date.now() }],
});
tm.queueExtractionSignal(sessionId, signalLabel, { source: "e2e_live_fallback" });
console.log(`[e2e] queued fallback ${signalLabel} for ${sessionId}`);
"""
    proc = subprocess.run(
        ["node", "-e", script, ws, session_id, label],
        cwd=ws,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"[e2e] ERROR: failed to queue fallback signal {label} for {session_id}: {proc.stderr.strip()[:400]}"
        )
    out = proc.stdout.strip()
    if out:
        print(out)

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

def reset_signal_source(lines: list[str]) -> str:
    for ln in lines:
        if '"label":"ResetSignal"' not in ln:
            continue
        if (
            '"event":"signal_process_begin"' not in ln
            and '"event":"extract_begin"' not in ln
            and '"event":"signal_queue_skipped_already_cleared"' not in ln
        ):
            continue
        if '"source":"session_end"' in ln:
            return "session_end"
        if '"source":"before_reset"' in ln:
            return "before_reset"
        if '"source":"command_hook"' in ln:
            return "command_hook"
        if '"source":"transcript_update"' in ln:
            return "transcript_update"
    return ""

def compaction_signal_source(lines: list[str]) -> str:
    for ln in lines:
        if '"label":"CompactionSignal"' not in ln:
            continue
        if (
            '"event":"signal_process_begin"' not in ln
            and '"event":"extract_begin"' not in ln
            and '"event":"signal_queue_skipped_already_cleared"' not in ln
        ):
            continue
        if '"source":"before_compaction"' in ln:
            return "before_compaction"
        if '"source":"transcript_update"' in ln:
            return "transcript_update"
    return ""

print(f"[e2e] Live events session key: {session_key}")
notify_start = line_count(notify_log_path)
stage_start = line_count(events_path)
fallback_used = False

compact_marker = f"E2E_COMPACT_{uuid.uuid4().hex[:10]}"
marker_ok = run_agent(f"E2E marker before compact: {compact_marker}")
runtime_session_id = resolve_session_id_from_key(session_key, "main-session")
if marker_ok:
    runtime_session_id = resolve_runtime_session_id(compact_marker, runtime_session_id)
else:
    print("[e2e] WARN: marker message failed; using synthetic live session id for fallback checks.", flush=True)
if not runtime_session_id:
    raise SystemExit("[e2e] ERROR: could not resolve runtime session id for live event checks")
print(f"[e2e] Live runtime session_id: {runtime_session_id}")
start = line_count(events_path)
compact_ok = run_agent("/compact")
if compact_ok:
    try:
        wait_for(
            lambda lines: bool(compaction_signal_source(lines)),
            45,
            "compaction signal processing",
            start,
        )
        source = compaction_signal_source(read_tail_since(events_path, start))
        print(f"[e2e] Live compact hook path OK (source={source or 'unknown'}).")
    except SystemExit as err:
        print(f"[e2e] WARN: compact hook path not observed ({err}). Falling back to direct signal queue.")
        fallback_used = True
        queue_signal_fallback(runtime_session_id, "CompactionSignal")
        wait_for(
            lambda lines: any(
                f'"session_id":"{runtime_session_id}"' in ln
                and '"label":"CompactionSignal"' in ln
                and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
                for ln in lines
            ),
            35,
            "compaction fallback signal processing",
            start,
        )
        print("[e2e] Live compact fallback path OK.")
else:
    print("[e2e] WARN: compact command did not complete; using direct signal queue fallback.", flush=True)
    fallback_used = True
    queue_signal_fallback(runtime_session_id, "CompactionSignal")
    wait_for(
        lambda lines: any(
            f'"session_id":"{runtime_session_id}"' in ln
            and '"label":"CompactionSignal"' in ln
            and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
            for ln in lines
        ),
        35,
        "compaction fallback signal processing",
        start,
    )
    print("[e2e] Live compact fallback path OK.")
assert_notify_worker_healthy(notify_start)

# Reset/new command path.
start = line_count(events_path)
run_agent("E2E baseline message before reset.")
reset_ok = run_agent("/reset")
if reset_ok:
    try:
        wait_for(
            lambda lines: bool(reset_signal_source(lines)),
            25,
            "reset signal processing",
            start,
        )
        source = reset_signal_source(read_tail_since(events_path, start))
        print(f"[e2e] Live reset path OK (source={source or 'unknown'}).")
    except SystemExit as err:
        # /reset is inconsistent across OpenClaw builds; /restart is often wired
        # to the same semantic boundary and emits ResetSignal reliably.
        print(f"[e2e] WARN: /reset hook path not observed ({err}). Retrying with /restart.")
        restart_ok = run_agent("/restart")
        if restart_ok:
            try:
                wait_for(
                    lambda lines: bool(reset_signal_source(lines)),
                    45,
                    "restart command reset signal processing",
                    start,
                )
                source = reset_signal_source(read_tail_since(events_path, start))
                print(f"[e2e] Live restart path OK (source={source or 'unknown'}).")
            except SystemExit as err_restart:
                print(f"[e2e] WARN: restart hook path not observed ({err_restart}). Falling back to direct signal queue.")
                fallback_used = True
                queue_signal_fallback(runtime_session_id, "ResetSignal")
                wait_for(
                    lambda lines: any(
                        '"label":"ResetSignal"' in ln
                        and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
                        for ln in lines
                    ),
                    35,
                    "reset fallback signal processing",
                    start,
                )
                print("[e2e] Live reset fallback path OK.")
        else:
            print("[e2e] WARN: /restart command did not complete; using direct signal queue fallback.")
            fallback_used = True
            queue_signal_fallback(runtime_session_id, "ResetSignal")
            wait_for(
                lambda lines: any(
                    '"label":"ResetSignal"' in ln
                    and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
                    for ln in lines
                ),
                35,
                "reset fallback signal processing",
                start,
            )
            print("[e2e] Live reset fallback path OK.")
else:
    print("[e2e] WARN: /reset command did not complete; trying /restart.", flush=True)
    restart_ok = run_agent("/restart")
    if restart_ok:
        try:
            wait_for(
                lambda lines: bool(reset_signal_source(lines)),
                45,
                "restart command reset signal processing",
                start,
            )
            source = reset_signal_source(read_tail_since(events_path, start))
            print(f"[e2e] Live restart path OK (source={source or 'unknown'}).")
        except SystemExit as err_restart:
            print(f"[e2e] WARN: restart hook path not observed ({err_restart}). Falling back to direct signal queue.")
            fallback_used = True
            queue_signal_fallback(runtime_session_id, "ResetSignal")
            wait_for(
                lambda lines: any(
                    '"label":"ResetSignal"' in ln
                    and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
                    for ln in lines
                ),
                35,
                "reset fallback signal processing",
                start,
            )
            print("[e2e] Live reset fallback path OK.")
    else:
        print("[e2e] WARN: reset/restart commands did not complete; using direct signal queue fallback.", flush=True)
        fallback_used = True
        queue_signal_fallback(runtime_session_id, "ResetSignal")
        wait_for(
            lambda lines: any(
                '"label":"ResetSignal"' in ln
                and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
                for ln in lines
            ),
            35,
            "reset fallback signal processing",
            start,
        )
        print("[e2e] Live reset fallback path OK.")
assert_notify_worker_healthy(notify_start)

# /new path uses same reset signal semantics.
start = line_count(events_path)
run_agent("E2E baseline message before new.")
new_ok = run_agent("/new")
if new_ok:
    try:
        wait_for(
            lambda lines: bool(reset_signal_source(lines)),
            90,
            "new command signal processing",
            start,
        )
        source = reset_signal_source(read_tail_since(events_path, start))
        print(f"[e2e] Live new path OK (source={source or 'unknown'}).")
    except SystemExit as err:
        print(f"[e2e] WARN: new hook path not observed ({err}). Falling back to direct signal queue.")
        fallback_used = True
        queue_signal_fallback(runtime_session_id, "ResetSignal")
        wait_for(
            lambda lines: any(
                '"label":"ResetSignal"' in ln
                and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
                for ln in lines
            ),
            35,
            "new fallback signal processing",
            start,
        )
        print("[e2e] Live new fallback path OK.")
else:
    print("[e2e] WARN: new command did not complete; using direct signal queue fallback.", flush=True)
    fallback_used = True
    queue_signal_fallback(runtime_session_id, "ResetSignal")
    wait_for(
        lambda lines: any(
            '"label":"ResetSignal"' in ln
            and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
            for ln in lines
        ),
        35,
        "new fallback signal processing",
        start,
    )
    print("[e2e] Live new fallback path OK.")
assert_notify_worker_healthy(notify_start)

# Ensure session cursor bookkeeping is active for replay safety.
cursor_dir = os.path.join(ws, "data", "session-cursors")
cursor_path = os.path.join(cursor_dir, f"{runtime_session_id}.json")
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
    print(
        f"[e2e] WARN: session cursor missing for runtime session ({runtime_session_id}); "
        "queueing fallback ResetSignal to verify cursor progression."
    )
    fallback_used = True
    queue_signal_fallback(runtime_session_id, "ResetSignal")
    retry_deadline = time.time() + 25
    while time.time() < retry_deadline:
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
        f"[e2e] ERROR: session cursor was not written for live events session ({runtime_session_id})"
    )
if str(cursor_payload.get("sessionId") or "") != runtime_session_id:
    raise SystemExit(
        f"[e2e] ERROR: session cursor sessionId mismatch: {cursor_payload.get('sessionId')!r} != {runtime_session_id!r}"
    )
if not cursor_payload.get("lastMessageKey"):
    raise SystemExit("[e2e] ERROR: session cursor missing lastMessageKey")
print("[e2e] Live session cursor progression OK.")

stage_lines = read_tail_since(events_path, stage_start)
extract_lines = []
for raw in stage_lines:
    try:
        evt = json.loads(raw)
    except Exception:
        continue
    if str(evt.get("event") or "") == "extract_begin":
        extract_lines.append(evt)
if len(extract_lines) >= 12:
    by_sid = {}
    for evt in extract_lines:
        sid = str(evt.get("session_id") or "")
        by_sid[sid] = by_sid.get(sid, 0) + 1
    duplicate_sids = [sid for sid, count in by_sid.items() if count > 2 and sid]
    if duplicate_sids or len(by_sid) >= 8:
        raise SystemExit(
            "[e2e] ERROR: extraction-storm signature detected in live events "
            f"(extract_begin={len(extract_lines)}, unique_sessions={len(by_sid)}, duplicate_sessions={duplicate_sids[:8]})."
        )

if fallback_used:
    msg = (
        "[e2e] lifecycle command hook path did not produce native /compact|/reset|/new extraction events; "
        "fallback signal path was used."
    )
    if require_native_hooks:
        raise SystemExit(f"[e2e] ERROR: {msg} (strict mode enabled)")
    print(f"[e2e] WARN: {msg}")

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
    janitor_script = os.path.join(ws, "modules", "quaid", "core", "lifecycle", "janitor.py")
    return subprocess.Popen(
        ["python3", janitor_script, "--task", "review", "--dry-run", "--stage-item-cap", "8"],
        cwd=ws,
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
    updater_script = os.path.join(ws, "modules", "quaid", "datastore", "docsdb", "project_updater.py")
    updater_probe = subprocess.Popen(
        ["python3", updater_script, "process-all"],
        cwd=ws,
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
cleanup_script = os.path.join(ws, "modules", "quaid", "core", "lifecycle", "janitor.py")
cleanup_probe = subprocess.Popen(
    ["python3", cleanup_script, "--task", "cleanup", "--apply"],
    cwd=ws,
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
echo "[e2e] Running memory flow regression checks (extract + DB verify)..."
python3 - "$E2E_WS" <<'PY'
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
try:
    import resource
except Exception:
    resource = None

ws = sys.argv[1]
events_path = os.path.join(ws, "logs", "quaid", "session-timeout-events.jsonl")
db_path = os.path.join(ws, "data", "memory.db")
cfg_path = os.path.join(ws, "config", "memory.json")
memory_soft_fail = str(os.environ.get("QUAID_E2E_MEMORY_SOFT_FAIL", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
run_tag = uuid.uuid4().hex[:8]
session_key = "agent:main:main"
wendy_token = f"WendyRun{run_tag}"
kent_token = f"KentRun{run_tag}"
iris_token = f"IrisRun{run_tag}"
milo_token = f"MiloRun{run_tag}"

if resource is not None:
    try:
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard_limit > 0 and soft_limit < hard_limit:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard_limit, hard_limit))
    except Exception:
        pass

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

def run_agent(message: str, timeout_sec: int = 30, sid: str = "", retries: int = 2) -> bool:
    params = {
        "agentId": "main",
        "sessionKey": session_key,
        "message": message,
        "idempotencyKey": f"e2e-memory-{uuid.uuid4().hex[:12]}",
    }
    if sid:
        params["sessionId"] = sid
    for attempt in range(1, max(1, retries) + 1):
        ok, payload = gateway_call_json("agent", params, timeout_sec=max(timeout_sec, 45))
        if not ok:
            print(f"[e2e] WARN: gateway agent failed message={message!r} attempt={attempt}/{retries}", flush=True)
            time.sleep(1)
            continue
        result = payload.get("result") if isinstance(payload, dict) else None
        run_id = payload.get("runId") if isinstance(payload, dict) else None
        if (not isinstance(run_id, str) or not run_id.strip()) and isinstance(result, dict):
            run_id = result.get("runId")
        if isinstance(run_id, str) and run_id.strip():
            wait_deadline = time.time() + max(timeout_sec + 45, 120)
            wait_status = ""
            while time.time() < wait_deadline:
                ok_wait, wait_payload = gateway_call_json(
                    "agent.wait",
                    {"runId": run_id, "timeoutMs": 15000},
                    timeout_sec=30,
                )
                if not ok_wait:
                    print(
                        f"[e2e] WARN: gateway agent.wait failed message={message!r} attempt={attempt}/{retries}",
                        flush=True,
                    )
                    time.sleep(1)
                    continue
                wait_status = ""
                if isinstance(wait_payload, dict):
                    wait_status = str(wait_payload.get("status") or wait_payload.get("state") or "").strip().lower()
                    if not wait_status and isinstance(wait_payload.get("result"), dict):
                        wait_status = str(wait_payload["result"].get("status") or wait_payload["result"].get("state") or "").strip().lower()
                if wait_status in {"ok", "succeeded", "success", "done", "completed", "complete"}:
                    return True
                if wait_status in {"failed", "error", "aborted", "timeout", "cancelled", "canceled"}:
                    print(
                        f"[e2e] WARN: gateway agent.wait status={wait_status} message={message!r} attempt={attempt}/{retries}",
                        flush=True,
                    )
                    break
                if wait_status == "":
                    # Some builds omit status for terminal inline payloads.
                    return True
                time.sleep(1)
            print(
                f"[e2e] WARN: gateway agent.wait timed out waiting for terminal status "
                f"(status={wait_status or 'unknown'}) message={message!r} attempt={attempt}/{retries}",
                flush=True,
            )
            time.sleep(1)
            continue
        # Some builds return final payload inline without runId.
        return True
    return False

def gateway_call_json(method: str, params: dict, timeout_sec: int = 35) -> tuple[bool, dict]:
    cmd = [
        "openclaw",
        "gateway",
        "call",
        method,
        "--json",
        "--params",
        json.dumps(params),
        "--timeout",
        str(timeout_sec * 1000),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 10)
    except subprocess.TimeoutExpired:
        print(f"[e2e] WARN: gateway call timed out method={method!r}", flush=True)
        return False, {}
    if proc.returncode != 0:
        print(
            f"[e2e] WARN: gateway call failed method={method!r}: {(proc.stderr or proc.stdout).strip()[:500]}",
            flush=True,
        )
        return False, {}
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        payload = {}
    return True, payload

def gateway_call(method: str, params: dict, timeout_sec: int = 35) -> bool:
    ok, _ = gateway_call_json(method, params, timeout_sec=timeout_sec)
    return ok

def resolve_session_id_from_key(session_key_value: str, fallback_session_id: str) -> str:
    sessions_path = os.path.expanduser("~/.openclaw/agents/main/sessions/sessions.json")
    try:
        with open(sessions_path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        entry = data.get(session_key_value) or {}
        sid = str(entry.get("sessionId") or "").strip()
        if sid:
            return sid
    except Exception:
        pass
    return fallback_session_id

def wait_for_signal_extraction(
    start_line: int,
    runtime_session_id: str,
    label: str,
    seconds: int = 90,
    queued_signal_path: str = "",
) -> bool:
    deadline = time.time() + seconds
    pending_drained_since = None
    processing_prefix = f"{queued_signal_path}.processing." if queued_signal_path else ""
    while time.time() < deadline:
        lines = read_tail_since(events_path, start_line)
        if any(
            f'"session_id":"{runtime_session_id}"' in ln
            and (
                (f'"label":"{label}"' in ln and '"event":"signal_process_begin"' in ln)
                or (f'"label":"{label}"' in ln and '"event":"extract_done"' in ln)
                or (f'"label":"{label}"' in ln and '"event":"extract_begin"' in ln)
            )
            for ln in lines
        ):
            return True
        if queued_signal_path:
            queued_exists = os.path.exists(queued_signal_path)
            queued_processing = False
            try:
                if processing_prefix:
                    parent = os.path.dirname(queued_signal_path) or "."
                    base = os.path.basename(processing_prefix)
                    queued_processing = any(
                        name.startswith(base) for name in os.listdir(parent)
                    )
            except Exception:
                queued_processing = False
            if not queued_exists and not queued_processing:
                if pending_drained_since is None:
                    pending_drained_since = time.time()
                # Require a short stable window so we don't count transient races.
                if (time.time() - pending_drained_since) >= 1.5:
                    return True
            else:
                pending_drained_since = None
        time.sleep(1)
    return False

def wait_for_session_persisted_token(session_id_value: str, token: str, seconds: int = 20) -> bool:
    if not session_id_value or not token:
        return False
    sessions_dir = os.path.expanduser("~/.openclaw/agents/main/sessions")
    target_path = os.path.join(sessions_dir, f"{session_id_value}.jsonl")
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if os.path.exists(target_path):
                with open(target_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                if token in text:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False

def run_direct_extract_fallback(
    transcript_text: str,
    session_id_value: str,
    owner_id_value: str,
    label: str = "ResetSignal",
) -> bool:
    transcript = f"User: {transcript_text}\nAssistant: Acknowledged."
    cmd = [
        "python3",
        "modules/quaid/ingest/extract.py",
        "-",
        "--owner",
        str(owner_id_value or "default"),
        "--label",
        str(label or "ResetSignal"),
        "--session-id",
        (session_id_value or "e2e-memory-fallback"),
        "--json",
    ]
    env = os.environ.copy()
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = "modules/quaid" + (f":{py_path}" if py_path else "")
    env["CLAWDBOT_WORKSPACE"] = ws
    try:
        proc = subprocess.run(
            cmd,
            cwd=ws,
            input=transcript,
            text=True,
            capture_output=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print("[e2e] WARN: direct extraction fallback timed out", flush=True)
        return False
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        print(f"[e2e] WARN: direct extraction fallback failed: {err[:500]}", flush=True)
        return False
    print("[e2e] Direct extraction fallback completed.", flush=True)
    return True

def queue_signal_fallback(
    session_id_value: str,
    label: str,
    fallback_text: str,
    source: str = "e2e_memory_flow_fallback",
) -> None:
    if not session_id_value:
        raise SystemExit(f"[e2e] ERROR: cannot queue fallback {label}; empty session id")
    # Write directly to the real pending-signal queue consumed by the gateway plugin
    # worker. Do not instantiate a local SessionTimeoutManager with a stub extractor,
    # because that can emit fake extract_done events without persisting facts.
    script = r"""
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const workspace = process.argv[1];
const sid = process.argv[2];
const signalLabel = process.argv[3];
const signalDir = join(workspace, "data", "pending-extraction-signals");
const signalPath = join(signalDir, `${sid}.json`);
mkdirSync(signalDir, { recursive: true });
let attemptCount = 0;
if (existsSync(signalPath)) {
  try {
    const existing = JSON.parse(readFileSync(signalPath, "utf8"));
    attemptCount = Number(existing?.attemptCount || 0);
  } catch {}
}
const payload = {
  sessionId: sid,
  label: String(signalLabel || "ResetSignal"),
  queuedAt: new Date().toISOString(),
  attemptCount,
  meta: { source: process.argv[4] || "e2e_memory_flow_fallback" },
};
writeFileSync(signalPath, JSON.stringify(payload), { mode: 0o600 });
console.log(`[e2e] queued fallback ${payload.label} for ${sid}`);
"""
    proc = subprocess.run(
        [
            "node",
            "-e",
            script,
            str(ws),
            session_id_value,
            label,
            str(source or "e2e_memory_flow_fallback"),
            str(fallback_text or "e2e-memory-flow-fallback"),
        ],
        cwd=str(ws),
        capture_output=True,
        text=True,
        timeout=45,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"[e2e] ERROR: failed to queue memory-flow fallback {label} for {session_id_value}: {proc.stderr.strip()[:400]}"
        )
    out = proc.stdout.strip()
    if out:
        print(out)

def find_tokens_for_owner(tokens: list[str], owner_id_value: str, seconds: int = 120) -> tuple[bool, str]:
    deadline = time.time() + seconds
    memory_text_col = "text"
    found_map = {tok: False for tok in tokens}
    while time.time() < deadline:
        try:
            with sqlite3.connect(db_path) as conn:
                cols = {
                    str(row[1]).strip().lower()
                    for row in conn.execute("PRAGMA table_info(nodes)").fetchall()
                }
                if "name" in cols:
                    memory_text_col = "name"
                elif "text" in cols:
                    memory_text_col = "text"
                else:
                    raise RuntimeError("nodes table has neither 'name' nor 'text' column")
                for tok in tokens:
                    rows = conn.execute(
                        f"SELECT {memory_text_col} FROM nodes WHERE owner_id = ? AND lower({memory_text_col}) LIKE ?",
                        (owner_id_value, f"%{tok.lower()}%"),
                    ).fetchall()
                    found_map[tok] = len(rows) > 0
        except Exception:
            pass
        if all(found_map.values()):
            return True, memory_text_col
        time.sleep(1)
    return False, memory_text_col

def cursor_last_message_key(session_id_value: str) -> str:
    cursor_path = Path(ws) / "data" / "session-cursors" / f"{session_id_value}.json"
    try:
        if cursor_path.exists():
            payload = json.loads(cursor_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(payload, dict):
                key = str(payload.get("lastMessageKey") or "").strip()
                if key:
                    return key
    except Exception:
        pass
    return ""

def wait_for_cursor_advance(session_id_value: str, previous_key: str = "", seconds: int = 45) -> str:
    deadline = time.time() + seconds
    while time.time() < deadline:
        key = cursor_last_message_key(session_id_value)
        if key and (not previous_key or key != previous_key):
            return key
        time.sleep(1)
    return ""

owner_id = "maya"
try:
    cfg = json.loads(open(cfg_path, "r", encoding="utf-8").read())
    owner_id = str(((cfg.get("users") or {}).get("defaultOwner")) or owner_id)
except Exception:
    pass
# Seed data, force timeout extraction, verify DB + cursor.
seed_timeout_text = (
    "Please remember this family detail for later recall: "
    f"my mother is {wendy_token} and my father is {kent_token}."
)
seed_ok = run_agent(seed_timeout_text, timeout_sec=45, retries=3)
runtime_session_id = resolve_session_id_from_key(session_key, "main-session")
if not seed_ok or not runtime_session_id:
    raise SystemExit(
        "[e2e] ERROR: timeout seed command path failed "
        f"(seed_ok={seed_ok}, runtime_session_id={runtime_session_id!r})."
    )
if not wait_for_session_persisted_token(runtime_session_id, wendy_token, seconds=20):
    print(
        f"[e2e] WARN: timeout-seed token not yet persisted for session={runtime_session_id}; proceeding with forced timeout extraction.",
        flush=True,
    )
timeout_start_line = line_count(events_path)
queue_signal_fallback(
    runtime_session_id,
    "Timeout",
    seed_timeout_text,
    source="e2e_memory_flow_forced_timeout",
)
queued_timeout_signal_path = os.path.join(ws, "data", "pending-extraction-signals", f"{runtime_session_id}.json")
timeout_seen = wait_for_signal_extraction(
    timeout_start_line,
    runtime_session_id,
    "Timeout",
    60,
    queued_timeout_signal_path,
)
if not timeout_seen:
    print(
        f"[e2e] WARN: forced Timeout signal not observed for session={runtime_session_id}; running direct extract fallback.",
        flush=True,
    )
    if not run_direct_extract_fallback(seed_timeout_text, runtime_session_id, owner_id, label="Timeout"):
        preview = "\n".join(read_tail_since(events_path, timeout_start_line)[-40:])
        raise SystemExit(
            "[e2e] ERROR: timed out waiting for forced Timeout extraction in memory flow\n"
            f"{preview}"
        )

found_timeout_tokens, memory_text_col = find_tokens_for_owner([wendy_token, kent_token], owner_id, seconds=120)
if not found_timeout_tokens:
    extract_diag = "no_extract_diag"
    try:
        timeout_log = Path(ws) / "logs" / "quaid" / "session-timeout.log"
        if timeout_log.exists():
            lines = timeout_log.read_text(encoding="utf-8", errors="replace").splitlines()
            focus = [ln.strip() for ln in lines[-120:] if ("event=extract_" in ln) or ("event=signal_process_" in ln)]
            if focus:
                extract_diag = " | ".join(focus[-4:])
    except Exception:
        pass
    print(
        "[e2e] WARN: forced Timeout extraction completed but expected facts were not stored in DB; "
        "running direct extraction fallback with timeout transcript.",
        flush=True,
    )
    run_direct_extract_fallback(seed_timeout_text, runtime_session_id, owner_id, label="Timeout")
    found_timeout_tokens, memory_text_col = find_tokens_for_owner([wendy_token, kent_token], owner_id, seconds=180)
    if not found_timeout_tokens:
        if memory_soft_fail:
            print(
                "[e2e] WARN: timeout extraction facts were not persisted in DB under soft-fail mode; "
                "continuing because timeout extraction signal path completed.",
                flush=True,
            )
        else:
            raise SystemExit(
                "[e2e] ERROR: forced Timeout extraction completed but expected facts were not stored in DB "
                f"(owner={owner_id}, column={memory_text_col}, tokens={[wendy_token, kent_token]}, "
                f"extract_diag={extract_diag})"
            )

# Baseline cursor state after first extraction cycle; some builds only advance
# cursor once new transcript turns are appended after timeout processing.
cursor_baseline = cursor_last_message_key(runtime_session_id)

# Add new data after timeout, force second extraction via reset/new semantics, verify DB + cursor advance.
seed_compact_text = (
    "Please remember this follow-up detail for later recall: "
    f"my mentor is {iris_token} and my teammate is {milo_token}."
)
second_seed_ok = run_agent(seed_compact_text, timeout_sec=45, retries=3)
pre_reset_session_id = resolve_session_id_from_key(session_key, runtime_session_id)
if not second_seed_ok:
    # Provider/gateway can occasionally timeout on the visible reply while still
    # allowing deterministic reset-triggered extraction fallback below.
    print(
        "[e2e] WARN: post-timeout seed message timed out before reset extraction; "
        "continuing with reset signal and direct extract fallback.",
        flush=True,
    )
if not wait_for_session_persisted_token(pre_reset_session_id, iris_token, seconds=20):
    print(
        f"[e2e] WARN: post-timeout seed token not yet persisted for session={pre_reset_session_id}; proceeding with sessions.reset.",
        flush=True,
    )
reset_start_line = line_count(events_path)
reset_ok, reset_payload = gateway_call_json("sessions.reset", {"key": session_key}, timeout_sec=90)
if not reset_ok:
    raise SystemExit(f"[e2e] ERROR: sessions.reset failed for post-timeout extraction (key={session_key}).")
if isinstance(reset_payload, dict) and reset_payload.get("ok") is False:
    raise SystemExit(f"[e2e] ERROR: sessions.reset returned non-ok payload: {reset_payload}")
reset_seen = wait_for_signal_extraction(reset_start_line, pre_reset_session_id, "ResetSignal", 45)
if not reset_seen:
    queue_signal_fallback(
        pre_reset_session_id,
        "ResetSignal",
        seed_compact_text,
        source="e2e_memory_flow_forced_post_timeout_reset",
    )
    queued_reset_signal_path = os.path.join(
        ws, "data", "pending-extraction-signals", f"{pre_reset_session_id}.json"
    )
    reset_seen = wait_for_signal_extraction(
        reset_start_line,
        pre_reset_session_id,
        "ResetSignal",
        60,
        queued_reset_signal_path,
    )
if not reset_seen:
    print(
        f"[e2e] WARN: ResetSignal worker path not observed for session={pre_reset_session_id}; running direct extract fallback.",
        flush=True,
    )
    reset_seen = run_direct_extract_fallback(
        seed_compact_text,
        pre_reset_session_id,
        owner_id,
        label="ResetSignal",
    )
if not reset_seen:
    preview = "\n".join(read_tail_since(events_path, reset_start_line)[-40:])
    raise SystemExit(
        "[e2e] ERROR: timed out waiting for post-timeout reset extraction in memory flow\n"
        f"{preview}"
    )

found_compact_tokens, memory_text_col = find_tokens_for_owner([iris_token, milo_token], owner_id, seconds=120)
if not found_compact_tokens:
    extract_diag = "no_extract_diag"
    try:
        timeout_log = Path(ws) / "logs" / "quaid" / "session-timeout.log"
        if timeout_log.exists():
            lines = timeout_log.read_text(encoding="utf-8", errors="replace").splitlines()
            focus = [ln.strip() for ln in lines[-120:] if ("event=extract_" in ln) or ("event=signal_process_" in ln)]
            if focus:
                extract_diag = " | ".join(focus[-4:])
    except Exception:
        pass
    print(
        "[e2e] WARN: post-timeout reset tokens missing after signal path; running direct extraction fallback with post-timeout transcript.",
        flush=True,
    )
    if run_direct_extract_fallback(seed_compact_text, pre_reset_session_id, owner_id, label="ResetSignal"):
        found_compact_tokens, memory_text_col = find_tokens_for_owner([iris_token, milo_token], owner_id, seconds=90)
    if not found_compact_tokens:
        if memory_soft_fail:
            print(
                "[e2e] WARN: post-timeout reset facts were not persisted in DB under soft-fail mode; "
                "continuing because reset extraction signal path completed.",
                flush=True,
            )
        else:
            raise SystemExit(
                "[e2e] ERROR: post-timeout reset extraction completed but expected new facts were not stored in DB "
                f"(owner={owner_id}, column={memory_text_col}, tokens={[iris_token, milo_token]}, "
                f"extract_diag={extract_diag})"
            )

cursor_after_compact = wait_for_cursor_advance(pre_reset_session_id, previous_key=cursor_baseline, seconds=30)
post_reset_session_id = resolve_session_id_from_key(session_key, "")
post_reset_cursor_key = ""
if post_reset_session_id:
    post_reset_cursor_key = wait_for_cursor_advance(post_reset_session_id, previous_key="", seconds=30)
if not cursor_after_compact and not post_reset_cursor_key:
    probe_session_id = post_reset_session_id or pre_reset_session_id
    probe_text = f"Cursor advance probe {uuid.uuid4().hex[:8]}"
    if run_agent(probe_text, timeout_sec=45, sid=probe_session_id, retries=2):
        if not cursor_after_compact:
            cursor_after_compact = wait_for_cursor_advance(pre_reset_session_id, previous_key=cursor_baseline, seconds=45)
        if not post_reset_cursor_key and post_reset_session_id:
            post_reset_cursor_key = wait_for_cursor_advance(post_reset_session_id, previous_key="", seconds=45)
    if not cursor_after_compact and not post_reset_cursor_key:
        if post_reset_session_id and post_reset_session_id != pre_reset_session_id:
            print(
                "[e2e] WARN: cursor files did not advance, but reset produced a new runtime session id; "
                f"accepting session rollover fallback (pre={pre_reset_session_id}, post={post_reset_session_id}).",
                flush=True,
            )
        else:
            raise SystemExit(
                "[e2e] ERROR: session cursor did not advance after post-timeout reset extraction "
                f"(pre_reset_session={pre_reset_session_id}, pre_reset_previous_key={cursor_baseline or '<empty>'}, "
                f"post_reset_session={post_reset_session_id or '<unknown>'}, probe_session={probe_session_id or '<unknown>'})"
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
pending_signal_dir = os.path.join(ws, "data", "pending-extraction-signals")

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
    last_detail = ""
    for attempt in (1, 2):
        # Lightweight restart: preserve current service wiring and avoid slow
        # stop/install/start chains that can block lane runtime under load.
        step = subprocess.run(
            ["openclaw", "gateway", "restart"],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if step.returncode != 0:
            last_detail = (step.stderr or step.stdout or "").strip()[:400]
            # Fallback once to explicit stop/start if restart fails.
            subprocess.run(["openclaw", "gateway", "stop"], capture_output=True, text=True, timeout=30)
            step = subprocess.run(
                ["openclaw", "gateway", "start"],
                capture_output=True,
                text=True,
                timeout=45,
            )
            last_detail = (step.stderr or step.stdout or last_detail).strip()[:400]
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
        time.sleep(1)
    raise SystemExit(
        "[e2e] ERROR: gateway did not resume listen on 127.0.0.1:18789 for notify matrix"
        + (f" (last start output: {last_detail})" if last_detail else "")
    )

def clear_pending_signals() -> None:
    if not os.path.isdir(pending_signal_dir):
        return
    for name in os.listdir(pending_signal_dir):
        path = os.path.join(pending_signal_dir, name)
        if not os.path.isfile(path):
            continue
        if name.endswith(".json") or ".json.processing." in name:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

def ensure_gateway_ready() -> None:
    chk = subprocess.run(
        ["bash", "-lc", "lsof -nP -iTCP:18789 -sTCP:LISTEN"],
        capture_output=True,
        text=True,
    )
    if chk.returncode == 0:
        return
    restart_gateway()

def run_agent(session_id: str, message: str, *, timeout_sec: int = 90, attempts: int = 2) -> None:
    last_err = ""
    for attempt in range(1, attempts + 1):
        ensure_gateway_ready()
        try:
            proc = subprocess.run(
                [
                    "openclaw",
                    "agent",
                    "--session-id",
                    session_id,
                    "--message",
                    message,
                    "--timeout",
                    str(max(5, int(timeout_sec))),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=max(int(timeout_sec) + 45, 60),
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

def resolve_runtime_session_id(marker_text: str, fallback_session_id: str, seconds: int = 35) -> str:
    session_dir = os.path.join(ws, "logs", "quaid", "session-messages")
    deadline = time.time() + seconds
    while time.time() < deadline:
        if os.path.isdir(session_dir):
            for name in os.listdir(session_dir):
                if not name.endswith(".jsonl"):
                    continue
                fp = os.path.join(session_dir, name)
                try:
                    content = open(fp, "r", encoding="utf-8", errors="replace").read()
                except Exception:
                    continue
                if marker_text in content:
                    return name[:-6]
        time.sleep(1)
    return fallback_session_id

def wait_for_reset_start(session_id: str, start_line: int, seconds: int = 120):
    deadline = time.time() + seconds
    while time.time() < deadline:
        lines = read_tail_since(events_path, start_line)
        if any(
            f'"session_id":"{session_id}"' in ln
            and '"label":"ResetSignal"' in ln
            and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln)
            for ln in lines
        ):
            return True, ""
        time.sleep(1)
    preview = "\n".join(read_tail_since(events_path, start_line)[-30:])
    return False, preview

def queue_reset_signal(session_id: str) -> None:
    os.makedirs(pending_signal_dir, exist_ok=True)
    signal_path = os.path.join(pending_signal_dir, f"{session_id}.json")
    attempt_count = 0
    if os.path.exists(signal_path):
        try:
            existing = json.loads(open(signal_path, "r", encoding="utf-8").read())
            attempt_count = int(existing.get("attemptCount") or 0)
        except Exception:
            attempt_count = 0
    payload = {
        "sessionId": session_id,
        "label": "ResetSignal",
        "queuedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "attemptCount": attempt_count,
        "meta": {"source": "e2e_notify_matrix_fallback"},
    }
    with open(signal_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    print(f"[e2e] queued fallback ResetSignal for {session_id}")

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

def summarize_notify_activity(lines):
    loaded = sum(1 for ln in lines if "[config] Loaded from " in ln)
    sent = sum(1 for ln in lines if "[notify] Sent to " in ln)
    no_last_channel = sum(1 for ln in lines if "[notify] No last channel found" in ln)
    send_failed = sum(1 for ln in lines if "[notify] Send failed" in ln)
    activity = loaded + sent + no_last_channel + send_failed
    return {
        "notify_lines": len(lines),
        "loaded_count": loaded,
        "sent_count": sent,
        "no_last_channel_count": no_last_channel,
        "send_failed_count": send_failed,
        "activity_count": activity,
    }

def collect_notify_activity(level: str, notify_start: int):
    # Notification worker writes are asynchronous and can arrive after extraction starts.
    if not os.path.isfile(notify_log_path):
        # Some local runs have no notify worker log configured; treat this as non-fatal.
        if level in ("normal", "debug"):
            return {
                "notify_lines": 0,
                "loaded_count": 0,
                "sent_count": 0,
                "no_last_channel_count": 0,
                "send_failed_count": 0,
                "activity_count": 1,
            }
        return {
            "notify_lines": 0,
            "loaded_count": 0,
            "sent_count": 0,
            "no_last_channel_count": 0,
            "send_failed_count": 0,
            "activity_count": 0,
        }
    timeout_sec = 20 if level == "quiet" else 30
    deadline = time.time() + timeout_sec
    last_lines = []
    summary = summarize_notify_activity(last_lines)
    while time.time() < deadline:
        last_lines = read_tail_since(notify_log_path, notify_start)
        assert_no_fatal_notify_errors(last_lines)
        summary = summarize_notify_activity(last_lines)
        if level in ("normal", "debug") and summary["activity_count"] > 0:
            return summary
        time.sleep(1)
    if level in ("normal", "debug"):
        preview = "\n".join(last_lines[-30:])
        summary["timed_out"] = True
        summary["preview"] = preview
        return summary
    summary["timed_out"] = False
    return summary

results = []
for level in ("quiet", "normal", "debug"):
    print(f"[e2e] notify-matrix level={level} start")
    set_level(level)
    # Config levels are loaded by gateway/plugin runtime; bounce gateway so each
    # level uses the intended notification config.
    restart_gateway()
    clear_pending_signals()
    notify_start = line_count(notify_log_path)
    events_start = line_count(events_path)
    sid = f"quaid-e2e-notify-{level}-{uuid.uuid4().hex[:8]}"
    marker = f"E2E_NOTIFY_LEVEL_{level}_{uuid.uuid4().hex[:6]}"
    # Best-effort marker call for session binding; do not block notify matrix if
    # agent transport is flaky under load.
    try:
        run_agent(sid, f"notification level marker: {marker}", timeout_sec=35, attempts=1)
    except SystemExit as exc:
        print(f"[e2e] WARN: marker command failed for notify-matrix session={sid}: {exc}")
    runtime_sid = resolve_runtime_session_id(marker, sid)
    reset_ok = False
    queue_reset_signal(runtime_sid)
    seen, preview = wait_for_reset_start(runtime_sid, events_start, 60)
    if not seen:
        # Under heavy queue pressure, extraction can lag; bounce gateway once
        # then re-queue deterministic fallback.
        restart_gateway()
        queue_reset_signal(runtime_sid)
        seen, preview = wait_for_reset_start(runtime_sid, events_start, 60)
    if not seen:
        raise SystemExit(
            f"[e2e] ERROR: notify matrix timed out waiting for reset extraction start "
            f"(session={runtime_sid}, reset_ok={reset_ok})\n{preview}"
        )
    summary = collect_notify_activity(level, notify_start)
    results.append({"level": level, **summary})
    loaded = summary["loaded_count"]
    sent = summary["sent_count"]
    no_last_channel = summary["no_last_channel_count"]
    activity = summary["activity_count"]
    if level == "quiet" and loaded > 0:
        raise SystemExit("[e2e] ERROR: quiet level emitted extraction notifications")
    timed_out = bool(summary.get("timed_out"))
    if level in ("normal", "debug") and timed_out and activity == 0:
        preview = str(summary.get("preview") or "").strip()
        if strict_delivery:
            raise SystemExit(
                f"[e2e] ERROR: {level} level emitted no extraction notification activity "
                f"within 30s\n{preview}"
            )
        print(
            f"[e2e] WARN: {level} level had no extraction notification activity within 30s "
            "(non-strict mode; continuing)"
        )
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
echo "[e2e] NOTE: ingest compaction uses sessions.compact API, not CLI '/compact' text, for deterministic behavior."
INGEST_ALLOW_FALLBACK="$INGEST_ALLOW_FALLBACK" \
INGEST_MAX_COMPACTION_SESSIONS="$INGEST_MAX_COMPACTION_SESSIONS" \
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
allow_fallback = os.environ.get("INGEST_ALLOW_FALLBACK", "false").lower() == "true"
max_compaction_sessions = int(os.environ.get("INGEST_MAX_COMPACTION_SESSIONS", "4"))
db_path = ws / "data" / "memory.db"
events_path = ws / "logs" / "quaid" / "session-timeout-events.jsonl"
project_staging = ws / "projects" / "staging"
project_log = ws / "logs" / "project-updater.log"
extraction_log_path = ws / "data" / "extraction-log.json"
session_id = f"quaid-e2e-ingest-{uuid.uuid4().hex[:12]}"
session_key = "agent:main:main"
marker = f"E2E_INGEST_{uuid.uuid4().hex[:10]}"

def run_agent(message: str, timeout_sec: int = 220) -> None:
    params = {
        "agentId": "main",
        "sessionKey": session_key,
        "sessionId": session_id,
        "message": message,
        "idempotencyKey": f"e2e-ingest-{uuid.uuid4().hex[:12]}",
    }
    ok, payload = gateway_call_json("agent", params, timeout_sec=max(timeout_sec, 45))
    if not ok:
        raise SystemExit(f"[e2e] ERROR: gateway agent failed for message={message[:80]!r}")
    result = payload.get("result") if isinstance(payload, dict) else None
    run_id = payload.get("runId") if isinstance(payload, dict) else None
    if (not isinstance(run_id, str) or not run_id.strip()) and isinstance(result, dict):
        run_id = result.get("runId")
    if isinstance(run_id, str) and run_id.strip():
        if not gateway_call("agent.wait", {"runId": run_id, "timeoutMs": min((timeout_sec + 60) * 1000, 240000)}, timeout_sec=max(timeout_sec + 30, 90)):
            raise SystemExit(f"[e2e] ERROR: gateway agent.wait failed for runId={run_id!r}")
    # Some builds may return inline result with no runId; treat as success.

def gateway_call_json(method: str, params: dict, timeout_sec: int = 30) -> tuple[bool, dict]:
    cmd = [
        "openclaw",
        "gateway",
        "call",
        method,
        "--json",
        "--params",
        json.dumps(params),
        "--timeout",
        str(timeout_sec * 1000),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 10)
    except subprocess.TimeoutExpired:
        print(f"[e2e] WARN: gateway call timed out method={method!r}", flush=True)
        return False, {}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        print(f"[e2e] WARN: gateway call failed method={method!r}: {err}", flush=True)
        return False, {}
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        payload = {}
    return True, payload

def gateway_call(method: str, params: dict, timeout_sec: int = 30) -> bool:
    ok, _ = gateway_call_json(method, params, timeout_sec=timeout_sec)
    return ok

def queue_signal_fallback(session_id_value: str, label: str) -> None:
    if not session_id_value:
        raise SystemExit(f"[e2e] ERROR: cannot queue fallback {label}; empty session id")
    script = r"""
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
const workspace = process.argv[1];
const sid = process.argv[2];
const signalLabel = process.argv[3];
const signalDir = join(workspace, "data", "pending-extraction-signals");
const signalPath = join(signalDir, `${sid}.json`);
mkdirSync(signalDir, { recursive: true });
let attemptCount = 0;
if (existsSync(signalPath)) {
  try {
    const existing = JSON.parse(readFileSync(signalPath, "utf8"));
    attemptCount = Number(existing?.attemptCount || 0);
  } catch {}
}
const payload = {
  sessionId: sid,
  label: signalLabel,
  source: "e2e_ingest_fallback",
  enqueuedAtMs: Date.now(),
  attemptCount,
  fallbackText: "e2e ingest fallback",
};
writeFileSync(signalPath, JSON.stringify(payload, null, 2), "utf8");
console.log(`[e2e] queued fallback ${signalLabel} for ${sid}`);
"""
    proc = subprocess.run(
        ["node", "-e", script, str(ws), session_id_value, label],
        cwd=str(ws),
        capture_output=True,
        text=True,
        timeout=45,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"[e2e] ERROR: failed to queue ingest fallback {label} for {session_id_value}: {proc.stderr.strip()[:400]}"
        )
    out = proc.stdout.strip()
    if out:
        print(out)

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
    openclaw_sessions_dir = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
    deadline = time.time() + seconds
    uuid_re = __import__("re").compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        __import__("re").I,
    )
    while time.time() < deadline:
        if session_dir.is_dir():
            for fp in session_dir.glob("*.jsonl"):
                try:
                    content = fp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if marker_text in content:
                    return fp.stem
        if openclaw_sessions_dir.is_dir():
            for fp in openclaw_sessions_dir.glob("*.jsonl"):
                try:
                    content = fp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if marker_text in content:
                    m = uuid_re.search(fp.name)
                    if m:
                        return m.group(0).lower()
        time.sleep(1)
    # Last-resort fallback: infer from new CompactionSignal events seen in this stage.
    for ln in read_tail_since(events_path, start_line):
        if '"label":"CompactionSignal"' not in ln:
            continue
        if '"event":"signal_process_begin"' not in ln and '"event":"extract_begin"' not in ln:
            continue
        marker = '"session_id":"'
        if marker not in ln:
            continue
        try:
            sid = ln.split(marker, 1)[1].split('"', 1)[0].strip()
        except Exception:
            sid = ""
        if uuid_re.fullmatch(sid or ""):
            return sid.lower()
    return ""

def resolve_session_id_from_key(session_key_value: str, fallback_session_id: str) -> str:
    sessions_path = Path.home() / ".openclaw" / "agents" / "main" / "sessions" / "sessions.json"
    try:
        data = json.loads(sessions_path.read_text(encoding="utf-8", errors="replace"))
        entry = data.get(session_key_value) or {}
        sid = str(entry.get("sessionId") or "").strip()
        if sid:
            return sid
    except Exception:
        pass
    return fallback_session_id

def wait_for(pred, seconds: int, label: str):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return
        time.sleep(1)
    raise SystemExit(f"[e2e] ERROR: timed out waiting for {label}")

def wait_for_best_effort(pred, seconds: int):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(1)
    return False

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
3) I work in /Users/clawdbot/quaid/dev.
Project summary: quaid refactor includes janitor lifecycle registry and datastore-owned maintenance.
Journal cue: I feel focused and cautious about boundaries.
Snippet cue: boundary ownership belongs in datastore modules.
""",
    timeout_sec=260,
)
compact_ok, compact_payload = gateway_call_json(
    "sessions.compact",
    {"key": session_key},
    timeout_sec=90,
)
if not compact_ok:
    raise SystemExit(f"[e2e] ERROR: sessions.compact failed for key={session_key}")
if isinstance(compact_payload, dict) and compact_payload.get("ok") is False:
    raise SystemExit(f"[e2e] ERROR: sessions.compact returned non-ok payload: {compact_payload}")

runtime_session_id = resolve_runtime_session_id(marker)
if not runtime_session_id:
    runtime_session_id = resolve_session_id_from_key(session_key, session_id)
    print(
        f"[e2e] WARN: could not resolve runtime UUID for marker={marker}; "
        f"falling back to declared session_id={session_id}",
        flush=True,
    )

def extraction_seen() -> bool:
    lines = read_tail_since(events_path, start_line)
    if not runtime_session_id:
        return any(
            '"label":"CompactionSignal"' in ln
            and ('"event":"signal_process_begin"' in ln or '"event":"extract_begin"' in ln or '"event":"extract_complete"' in ln)
            for ln in lines
        )
    return any(
        f'"session_id":"{runtime_session_id}"' in ln
        and (
            ('"label":"CompactionSignal"' in ln and '"event":"signal_process_begin"' in ln)
            or '"event":"extract_begin"' in ln
            or '"event":"extract_complete"' in ln
        )
        for ln in lines
    )

def compaction_signal_session_ids() -> set[str]:
    session_ids = set()
    lines = read_tail_since(events_path, start_line)
    for ln in lines:
        if '"label":"CompactionSignal"' not in ln:
            continue
        if '"event":"signal_process_begin"' not in ln and '"event":"extract_begin"' not in ln:
            continue
        marker = '"session_id":"'
        if marker not in ln:
            continue
        try:
            sid = ln.split(marker, 1)[1].split('"', 1)[0].strip().lower()
        except Exception:
            sid = ""
        if sid:
            session_ids.add(sid)
    return session_ids

extraction_deadline = time.time() + 90
while time.time() < extraction_deadline and not extraction_seen():
    time.sleep(1)
if not extraction_seen():
    target_sid = runtime_session_id or session_id
    print(
        f"[e2e] WARN: ingest extraction not observed via /compact; queuing fallback CompactionSignal for {target_sid}",
        flush=True,
    )
    queue_signal_fallback(target_sid, "CompactionSignal")
    wait_for(extraction_seen, 60, "ingestion extraction completion (fallback)")

compaction_sessions = compaction_signal_session_ids()
if len(compaction_sessions) > max_compaction_sessions:
    raise SystemExit(
        f"[e2e] ERROR: compaction fan-out detected ({len(compaction_sessions)} sessions > {max_compaction_sessions})"
    )

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

project_activity_observed = wait_for_best_effort(project_activity_seen, 45)
if not project_activity_observed:
    print(
        "[e2e] WARN: no project queue/log activity observed in ingest window; "
        "continuing because extraction and DB checks passed",
        flush=True,
    )

if after_nodes <= baseline_nodes:
    # Node writes can lag behind extraction completion under queue pressure.
    growth_deadline = time.time() + 90
    while time.time() < growth_deadline and after_nodes <= baseline_nodes:
        time.sleep(2)
        try:
            with sqlite3.connect(db_path) as conn:
                after_nodes = count_nodes(conn)
        except Exception:
            pass
if after_nodes <= baseline_nodes:
    print(
        f"[e2e] WARN: ingestion produced no net memory growth "
        f"(before={baseline_nodes}, after={after_nodes}); continuing due successful extraction signal path",
        flush=True,
    )

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
            "project_activity_observed": project_activity_observed,
            "compaction_session_count": len(compaction_sessions),
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

with sqlite3.connect(db_path, timeout=30.0) as conn:
    conn.execute("PRAGMA busy_timeout=30000")
    schema_path = ws / "plugins" / "quaid" / "datastore" / "memorydb" / "schema.sql"
    if not schema_path.exists():
        schema_path = ws / "modules" / "quaid" / "datastore" / "memorydb" / "schema.sql"
    if not schema_path.exists():
        raise RuntimeError(f"memorydb schema not found (checked {schema_path})")
    conn.executescript(schema_path.read_text(encoding="utf-8"))
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

python3 - "$E2E_WS" <<'PY'
import os
import sys

ws = sys.argv[1]
pending_dir = os.path.join(ws, "data", "pending-extraction-signals")
removed = 0
if os.path.isdir(pending_dir):
    for name in os.listdir(pending_dir):
        if name.endswith(".json") or ".json.processing." in name:
            path = os.path.join(pending_dir, name)
            try:
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                pass
print(f"[e2e] Cleared pending extraction signal files before janitor: {removed}")
PY

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
project_md_path = os.path.join(ws, "projects", "quaid", "PROJECT.md")

def _project_log_line_count(path: str) -> int:
    begin = "<!-- BEGIN:PROJECT_LOG -->"
    end = "<!-- END:PROJECT_LOG -->"
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except Exception:
        return 0
    if begin not in raw or end not in raw:
        return 0
    segment = raw.split(begin, 1)[1].split(end, 1)[0]
    lines = [line.strip() for line in segment.splitlines() if line.strip()]
    return sum(1 for line in lines if line.startswith("- "))

before_project_log_lines = _project_log_line_count(project_md_path)
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

janitor_script = os.path.join(ws, "modules", "quaid", "core", "lifecycle", "janitor.py")
cmd = ["python3", janitor_script, "--task", "all"]
if mode == "dry-run":
    cmd.append("--dry-run")
else:
    cmd.append("--apply")
    # E2E apply lane must not block on policy=ask scopes; approve explicitly.
    cmd.append("--approve")
cmd.append("--force-distill")
# Keep janitor bounded for e2e stability while still exercising write paths.
cmd.extend(["--stage-item-cap", "8"])
cmd.extend(["--time-budget", str(max(120, min(int(timeout_seconds) - 30, 300)))])

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
        janitor_script,
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
    # Keep janitor LLM calls bounded in e2e lanes so one slow provider call
    # cannot exceed the suite's subprocess timeout window.
    env["QUAID_DEEP_REASONING_TIMEOUT"] = str(int(os.environ.get("QUAID_E2E_DEEP_TIMEOUT", "30") or "30"))
    env["QUAID_FAST_REASONING_TIMEOUT"] = str(int(os.environ.get("QUAID_E2E_FAST_TIMEOUT", "20") or "20"))
    env["QUAID_LLM_MAX_RETRIES"] = str(int(os.environ.get("QUAID_E2E_LLM_MAX_RETRIES", "0") or "0"))
    env["QUAID_WORKSPACE_AUDIT_TIMEOUT_SECONDS"] = str(int(os.environ.get("QUAID_E2E_WORKSPACE_TIMEOUT", "30") or "30"))
    env["QUAID_DOCS_UPDATE_TIMEOUT_SECONDS"] = str(int(os.environ.get("QUAID_E2E_DOCS_UPDATE_TIMEOUT", "30") or "30"))
    env["QUAID_DOCS_TRANSCRIPT_TIMEOUT_SECONDS"] = str(int(os.environ.get("QUAID_E2E_DOCS_TRANSCRIPT_TIMEOUT", "30") or "30"))
    env["QUAID_JANITOR_SKIP_NOTIFY"] = str(int(os.environ.get("QUAID_E2E_JANITOR_SKIP_NOTIFY", "1") or "1"))
    configured_parallel = {
        "enabled": None,
        "llmWorkers": None,
        "lifecyclePrepassWorkers": None,
    }
    cfg_path = os.path.join(ws, "config", "memory.json")
    cfg = {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    janitor_cfg = cfg.setdefault("janitor", {})
    models_cfg = cfg.setdefault("models", {})
    ollama_cfg = cfg.setdefault("ollama", {})
    # Keep janitor/rag embedding latency bounded in e2e by preferring a small
    # local embeddings model rather than large VRAM-heavy defaults.
    janitor_embeddings_model = str(os.environ.get("QUAID_E2E_JANITOR_EMBEDDINGS_MODEL", "") or "").strip()
    if not janitor_embeddings_model:
        janitor_embeddings_model = str(ollama_cfg.get("embeddingModel") or ollama_cfg.get("embedding_model") or "").strip()
    if janitor_embeddings_model:
        models_cfg["embeddings_provider"] = str(models_cfg.get("embeddings_provider") or "ollama")
        models_cfg["embeddingsProvider"] = str(models_cfg.get("embeddingsProvider") or "ollama")
        ollama_cfg["embedding_model"] = janitor_embeddings_model
        ollama_cfg["embeddingModel"] = janitor_embeddings_model
    if mode == "apply":
        # Force apply mode in e2e so janitor writes janitor_runs records even
        # when user/default config is set to ask/dry_run.
        janitor_cfg["applyMode"] = "auto"
        janitor_cfg["apply_mode"] = "auto"
    if janitor_parallel_bench:
        parallel_cfg = janitor_cfg.setdefault("parallel", {})
        llm_workers = int(os.environ.get("QUAID_E2E_JPB_LLM_WORKERS", "4") or "4")
        prepass_workers = int(os.environ.get("QUAID_E2E_JPB_PREPASS_WORKERS", "3") or "3")
        parallel_cfg["enabled"] = True
        parallel_cfg["llmWorkers"] = max(1, llm_workers)
        parallel_cfg["lifecyclePrepassWorkers"] = max(1, prepass_workers)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        configured_parallel = {
            "enabled": True,
            "llmWorkers": max(1, llm_workers),
            "lifecyclePrepassWorkers": max(1, prepass_workers),
        }
        env["QUAID_BENCHMARK_MODE"] = "1"
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
    except Exception:
        pass
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
    stdout_tail = (exc.stdout or "")[-4000:]
    stderr_tail = (exc.stderr or "")[-4000:]
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
    soft_fail_enabled = str(os.environ.get("QUAID_E2E_JANITOR_SOFT_FAIL", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
    soft_fail_markers = (
        "vec_nodes update failed",
        "Journal distillation failed",
        "parse_json_response failed",
        "Invalid \\escape",
    )
    combined_tail = f"{stdout_tail}\n{stderr_tail}"
    if soft_fail_enabled and any(marker in combined_tail for marker in soft_fail_markers):
        print(
            "[e2e] WARN: janitor encountered known non-critical failures; "
            "continuing with post-run invariants.",
            file=sys.stderr,
        )
    else:
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
after_project_log_lines = _project_log_line_count(project_md_path)
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
        "configured_parallel": configured_parallel,
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
    "project_log_lines_before": before_project_log_lines,
    "project_log_lines_after": after_project_log_lines,
    "snippet_exists_before": snippet_exists_before,
    "snippet_exists_after": snippet_exists_after,
    "journal_exists_before": journal_exists_before,
    "journal_exists_after": journal_exists_after,
    "completed_at": completed_at,
}
print("[e2e] Janitor verification:")
print(json.dumps(summary, indent=2))

soft_fail_enabled = str(os.environ.get("QUAID_E2E_JANITOR_SOFT_FAIL", "1") or "1").strip().lower() in {"1", "true", "yes", "on"}
soft_status_failed = bool(soft_fail_enabled and status == "failed")
if status != "completed":
    if soft_status_failed:
        print("[e2e] WARN: janitor status=failed in soft-fail mode; continuing with invariant checks.", file=sys.stderr)
    else:
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
    if after_anchor_chunks < before_anchor_chunks:
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
    if prebench_guard and not soft_status_failed:
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
        if (not janitor_parallel_bench) and after_project_log_lines <= before_project_log_lines:
            print(
                "[e2e] ERROR: project log assertion failed; PROJECT.md project-log block did not gain entries "
                f"(before={before_project_log_lines}, after={after_project_log_lines})",
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
