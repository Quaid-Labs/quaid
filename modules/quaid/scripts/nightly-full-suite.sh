#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${QUAID_REPO_ROOT:-${HOME}/quaid/dev}"
E2E_SCRIPT="${REPO_ROOT}/modules/quaid/scripts/run-quaid-e2e.sh"
LOG_DIR="${QUAID_NIGHTLY_LOG_DIR:-${HOME}/quaid/logs/nightly-e2e}"
TS="$(date +%Y%m%d-%H%M%S)"
RUN_LOG="${LOG_DIR}/nightly-${TS}.log"
SUMMARY_PATH="${LOG_DIR}/nightly-${TS}.summary.json"
HISTORY_PATH="${LOG_DIR}/nightly-summary-history.jsonl"
BUDGET_PATH="${LOG_DIR}/nightly-${TS}.budget.json"
LATEST_LOG_LINK="${LOG_DIR}/latest.log"
LATEST_SUMMARY_LINK="${LOG_DIR}/latest.summary.json"
KEEP_LOGS="${QUAID_NIGHTLY_KEEP_LOGS:-14}"

mkdir -p "${LOG_DIR}"

run_e2e() {
  local rc=0
  (
    cd "${REPO_ROOT}"
    export QUAID_E2E_SUMMARY_PATH="${SUMMARY_PATH}"
    export QUAID_E2E_SUMMARY_HISTORY_PATH="${HISTORY_PATH}"
    export QUAID_E2E_BUDGET_RECOMMENDATION_PATH="${BUDGET_PATH}"
    bash "${E2E_SCRIPT}" --suite nightly --quick-bootstrap --reuse-workspace
  ) 2>&1 | tee "${RUN_LOG}" || rc=$?
  return $rc
}

send_telegram_status() {
  local exit_code="$1"
  python3 - "$REPO_ROOT" "$SUMMARY_PATH" "$RUN_LOG" "$exit_code" <<'PY'
import json
import os
import sys
from pathlib import Path

repo = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
run_log = Path(sys.argv[3])
exit_code = int(sys.argv[4])

status = "failed"
duration = None
stage = ""
reason = ""
if summary_path.exists():
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        status = str(data.get("status") or status)
        duration = data.get("duration_seconds")
        failure = data.get("failure") if isinstance(data.get("failure"), dict) else {}
        stage = str(failure.get("stage") or "")
        reason = str(failure.get("reason") or "")
    except Exception:
        pass

parts = [f"[Quaid Nightly] status={status} exit={exit_code}"]
if duration is not None:
    parts.append(f"duration={duration}s")
if stage:
    parts.append(f"stage={stage}")
if reason:
    parts.append(f"reason={reason[:180]}")
parts.append(f"log={run_log}")
parts.append(f"summary={summary_path}")
msg = " | ".join(parts)

sys.path.insert(0, str(repo / "modules" / "quaid"))
try:
    os.environ.setdefault("QUAID_HOME", str(repo))
    os.environ.setdefault("CLAWDBOT_WORKSPACE", str(repo))
    from core.runtime.notify import notify_user

    ok = notify_user(msg, channel_override="telegram")
    if not ok:
        notify_user(msg)
except Exception:
    # Don't fail the nightly job on notify errors.
    pass
PY
}

rotate_logs() {
  local keep="$1"
  find "${LOG_DIR}" -maxdepth 1 -type f -name 'nightly-*.log' | sort | head -n -"${keep}" | xargs -r rm -f
  find "${LOG_DIR}" -maxdepth 1 -type f -name 'nightly-*.summary.json' | sort | head -n -"${keep}" | xargs -r rm -f
  find "${LOG_DIR}" -maxdepth 1 -type f -name 'nightly-*.budget.json' | sort | head -n -"${keep}" | xargs -r rm -f
}

rc=0
if ! run_e2e; then
  rc=$?
fi

ln -sfn "${RUN_LOG}" "${LATEST_LOG_LINK}"
[[ -f "${SUMMARY_PATH}" ]] && ln -sfn "${SUMMARY_PATH}" "${LATEST_SUMMARY_LINK}" || true
rotate_logs "${KEEP_LOGS}"
send_telegram_status "$rc" || true

exit "$rc"
