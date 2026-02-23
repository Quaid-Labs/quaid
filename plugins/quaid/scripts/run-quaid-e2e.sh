#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOOTSTRAP_ROOT="${QUAID_BOOTSTRAP_ROOT:-${HOME}/quaid/bootstrap}"
ENV_FILE="${QUAID_E2E_ENV_FILE:-${PLUGIN_ROOT}/.env.e2e}"

E2E_WS="${HOME}/quaid/e2e-test"
DEV_WS="${HOME}/quaid/dev"

PROFILE_TEST="${QUAID_E2E_PROFILE_TEST:-${BOOTSTRAP_ROOT}/profiles/runtime-profile.local.quaid.json}"
PROFILE_SRC="${QUAID_E2E_PROFILE_SRC:-${BOOTSTRAP_ROOT}/profiles/runtime-profile.local.quaid.json}"
TMP_PROFILE_BASE="$(mktemp /tmp/quaid-e2e-profile.XXXXXX)"
TMP_PROFILE="${TMP_PROFILE_BASE}.json"
mv "$TMP_PROFILE_BASE" "$TMP_PROFILE"

AUTH_PATH="openai-oauth"
KEEP_ON_SUCCESS=false
RUN_JANITOR=true
RUN_LLM_SMOKE=true
JANITOR_TIMEOUT_SECONDS=240
JANITOR_MODE="apply"
NOTIFY_LEVEL="quiet"
E2E_SKIP_EXIT_CODE=20

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Options:
  --auth-provider <id>   Legacy provider selector (openai|anthropic)
  --auth-path <id>       Auth path for bootstrap profile (openai-oauth|openai-api|anthropic-oauth|anthropic-api; default: openai-oauth)
  --keep-on-success      Do not delete ~/quaid/e2e-test after successful run
  --skip-janitor         Skip janitor phase
  --skip-llm-smoke       Skip gateway LLM smoke call
  --janitor-timeout <s>  Janitor timeout seconds (default: 240)
  --janitor-dry-run      Run janitor in dry-run mode (default is apply)
  --notify-level <lvl>   Quaid notify level for e2e (quiet|normal|verbose|debug, default: quiet)
  --env-file <path>      Optional .env file to source before running (default: plugins/quaid/.env.e2e)
  -h, --help             Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auth-provider) AUTH_PROVIDER="$2"; shift 2 ;;
    --auth-path) AUTH_PATH="$2"; shift 2 ;;
    --keep-on-success) KEEP_ON_SUCCESS=true; shift ;;
    --skip-janitor) RUN_JANITOR=false; shift ;;
    --skip-llm-smoke) RUN_LLM_SMOKE=false; shift ;;
    --janitor-timeout) JANITOR_TIMEOUT_SECONDS="$2"; shift 2 ;;
    --janitor-dry-run) JANITOR_MODE="dry-run"; shift ;;
    --notify-level) NOTIFY_LEVEL="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

skip_e2e() {
  local reason="$1"
  echo "[e2e] SKIP_REASON:${reason}" >&2
  echo "[e2e] SKIP: bootstrap e2e prerequisites are not available in this environment." >&2
  echo "[e2e] To enable e2e auth-path tests:" >&2
  echo "[e2e]   1) Set QUAID_BOOTSTRAP_ROOT (default: ~/quaid/bootstrap)." >&2
  echo "[e2e]   2) Copy plugins/quaid/scripts/e2e.env.example to plugins/quaid/.env.e2e and set keys as needed." >&2
  echo "[e2e]   3) Required keys by path:" >&2
  echo "[e2e]      - openai-api: OPENAI_API_KEY" >&2
  echo "[e2e]      - anthropic-api: ANTHROPIC_API_KEY" >&2
  echo "[e2e]      - openai-oauth / anthropic-oauth: valid bootstrap auth profiles/tokens" >&2
  exit "$E2E_SKIP_EXIT_CODE"
}

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

if [[ "$AUTH_PATH" == "openai-oauth" || "$AUTH_PATH" == "openai-api" || "$AUTH_PATH" == "anthropic-oauth" || "$AUTH_PATH" == "anthropic-api" ]]; then
  true
else
  case "${AUTH_PROVIDER:-}" in
    openai) AUTH_PATH="openai-oauth" ;;
    anthropic) AUTH_PATH="anthropic-api" ;;
    *)
      echo "Invalid auth selection. Use --auth-path openai-oauth|openai-api|anthropic-oauth|anthropic-api (or legacy --auth-provider openai|anthropic)." >&2
      exit 1
      ;;
  esac
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

cleanup() {
  local exit_code="$1"
  rm -f "$TMP_PROFILE"
  if [[ "$exit_code" -eq 0 && "$KEEP_ON_SUCCESS" != true ]]; then
    echo "[e2e] Success, removing ${E2E_WS}"
    rm -rf "$E2E_WS"
  fi
  restore_test_gateway
  return "$exit_code"
}

trap 'cleanup $?' EXIT

echo "[e2e] Stopping any running gateway..."
openclaw gateway stop || true

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

echo "[e2e] Bootstrapping e2e workspace: ${E2E_WS}"
"${BOOTSTRAP_ROOT}/scripts/bootstrap-local.sh" \
  --profile "$TMP_PROFILE" \
  --wipe \
  --auth-path "$AUTH_PATH" \
  --worktree-source "$DEV_WS" \
  --worktree-test-branch "e2e-runtime"

echo "[e2e] Starting gateway on e2e workspace..."
start_gateway_safe
if ! wait_for_gateway_listen 40; then
  echo "[e2e] Gateway failed to listen on 127.0.0.1:18789 in e2e workspace" >&2
  openclaw gateway status || true
  exit 1
fi

if [[ "$RUN_LLM_SMOKE" == true ]]; then
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
else
  echo "[e2e] Skipping LLM smoke (--skip-llm-smoke)."
fi

echo "[e2e] Syncing latest dev overlay into e2e workspace..."
rsync -a --delete \
  --exclude ".git" \
  --exclude "node_modules" \
  --exclude "logs" \
  --exclude "data" \
  --exclude "__pycache__" \
  "${DEV_WS}/plugins/quaid/" "${E2E_WS}/plugins/quaid/"

echo "[e2e] Running Quaid integration tests..."
for required in \
  "${E2E_WS}/plugins/quaid/tests/session-timeout-manager.test.ts" \
  "${E2E_WS}/plugins/quaid/tests/chat-flow.integration.test.ts"; do
  if [[ ! -f "$required" ]]; then
    echo "[e2e] Missing required integration test file: $required" >&2
    echo "[e2e] Ensure test files are committed in ${DEV_WS} before running e2e." >&2
    exit 1
  fi
done
(cd "${E2E_WS}/plugins/quaid" && npx vitest run tests/session-timeout-manager.test.ts tests/chat-flow.integration.test.ts --reporter=verbose)

if [[ "$RUN_JANITOR" == true ]]; then
echo "[e2e] Running janitor (${JANITOR_MODE})..."
python3 - "$E2E_WS" "$JANITOR_TIMEOUT_SECONDS" "$JANITOR_MODE" <<'PY'
import json
import sqlite3
import subprocess
import sys

ws = sys.argv[1]
timeout_seconds = int(sys.argv[2])
mode = sys.argv[3]
db_path = f"{ws}/data/memory.db"

def fetch_counts(conn):
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM nodes GROUP BY status"
    ).fetchall()
    out = {"pending": 0, "approved": 0, "active": 0}
    for status, count in rows:
        if status in out:
            out[status] = count
    return out

with sqlite3.connect(db_path) as conn:
    before_counts = fetch_counts(conn)
    has_runs_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='janitor_runs'"
    ).fetchone() is not None
    before_run_id = 0
    if has_runs_table:
        before_run_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM janitor_runs").fetchone()[0]

cmd = ["python3", "plugins/quaid/core/lifecycle/janitor.py", "--task", "all"]
if mode == "dry-run":
    cmd.append("--dry-run")
else:
    cmd.append("--apply")

try:
    subprocess.run(cmd, cwd=ws, check=True, timeout=timeout_seconds)
except subprocess.TimeoutExpired:
    print(f"[e2e] Janitor run timed out after {timeout_seconds}s", file=sys.stderr)
    raise SystemExit(1)

with sqlite3.connect(db_path) as conn:
    after_counts = fetch_counts(conn)
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

if not run_row:
    if mode == "dry-run":
        print("[e2e] Janitor dry-run completed (no janitor_runs record required).")
        raise SystemExit(0)
    print("[e2e] ERROR: no janitor_runs record was written", file=sys.stderr)
    raise SystemExit(1)

run_id, task_name, status, memories_processed, actions_taken, completed_at = run_row
summary = {
    "mode": mode,
    "run_id": run_id,
    "task": task_name,
    "status": status,
    "memories_processed": memories_processed,
    "actions_taken": actions_taken,
    "before": before_counts,
    "after": after_counts,
    "completed_at": completed_at,
}
print("[e2e] Janitor verification:")
print(json.dumps(summary, indent=2))

if status != "completed":
    print(f"[e2e] ERROR: janitor run status was {status}", file=sys.stderr)
    raise SystemExit(1)

if mode == "apply":
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
PY
else
  echo "[e2e] Skipping janitor (--skip-janitor)."
fi

echo "[e2e] E2E run complete."
