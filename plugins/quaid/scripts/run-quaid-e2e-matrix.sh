#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="${SCRIPT_DIR}/run-quaid-e2e.sh"
BOOTSTRAP_ROOT="${QUAID_BOOTSTRAP_ROOT:-${HOME}/quaid/bootstrap}"
PROFILE_PATH="${QUAID_E2E_PROFILE_PATH:-${BOOTSTRAP_ROOT}/profiles/runtime-profile.local.quaid.json}"

PATHS=("openai-oauth" "openai-api" "anthropic-oauth" "anthropic-api")
EXPECT_SPEC=""
EXTRA_ARGS=()

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Options:
  --paths <csv>       Auth paths to test (default: openai-oauth,openai-api,anthropic-oauth,anthropic-api)
  --expect <spec>     Expected results map, e.g. "openai-oauth=pass,openai-api=fail"
  --                 Forward remaining args to run-quaid-e2e.sh
  -h, --help         Show help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --paths)
      IFS=',' read -r -a PATHS <<< "$2"
      shift 2
      ;;
    --expect)
      EXPECT_SPEC="$2"
      shift 2
      ;;
    --)
      shift
      EXTRA_ARGS=("$@")
      break
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ ! -x "$RUNNER" ]]; then
  echo "Missing runner: $RUNNER" >&2
  exit 1
fi

RESULTS=()
EXPECTED=()
REASONS=()
EXCEPTION_FILES=()
FAILED=0

if [[ -n "$EXPECT_SPEC" ]]; then
  IFS=',' read -r -a PAIRS <<< "$EXPECT_SPEC"
  for pair in "${PAIRS[@]}"; do
    pair="$(echo "$pair" | xargs)"
    [[ -z "$pair" ]] && continue
    key="${pair%%=*}"
    value="${pair#*=}"
    EXPECTED+=("${key}=${value}")
  done
fi

lookup_value() {
  local key="$1"
  local default_value="$2"
  shift 2
  local item
  for item in "$@"; do
    if [[ "${item%%=*}" == "$key" ]]; then
      echo "${item#*=}"
      return 0
    fi
  done
  echo "$default_value"
}

classify_failure_reason() {
  local log_file="$1"
  if rg -n "No OpenClaw authProfiles matched selector" "$log_file" >/dev/null 2>&1; then
    echo "config-missing-auth-profile"
    return 0
  fi
  if rg -n "invalid x-api-key|authentication_error" "$log_file" >/dev/null 2>&1; then
    echo "auth-invalid-key"
    return 0
  fi
  if rg -n "No API key found for provider|Auth store: .*auth-profiles.json" "$log_file" >/dev/null 2>&1; then
    echo "auth-store-missing-credentials"
    return 0
  fi
  if rg -n "OAuth token refresh failed" "$log_file" >/dev/null 2>&1; then
    echo "auth-oauth-refresh-failed"
    return 0
  fi
  if rg -n "rate limit|rate_limit|quota|usage cap|usage.*cap" "$log_file" >/dev/null 2>&1; then
    echo "auth-usage-restricted"
    return 0
  fi
  if rg -n "LLM smoke HTTP 401|LLM smoke HTTP 403" "$log_file" >/dev/null 2>&1; then
    echo "gateway-auth-denied"
    return 0
  fi
  if rg -n "LLM smoke HTTP 5[0-9]{2}|internal error|api_error" "$log_file" >/dev/null 2>&1; then
    echo "provider-runtime-error"
    return 0
  fi
  if rg -n "Missing required integration test file|Test Files .*failed|Tests .*failed" "$log_file" >/dev/null 2>&1; then
    echo "integration-test-failure"
    return 0
  fi
  if rg -n "Gateway failed to listen|failed to listen" "$log_file" >/dev/null 2>&1; then
    echo "gateway-start-failure"
    return 0
  fi
  echo "unknown"
}

extract_exception_lines() {
  local log_file="$1"
  rg -n "ERROR:| error |Error:|Traceback|exception|All models failed|failed to load plugin|OAuth token refresh failed|No API key found|api_error" "$log_file" \
    | sed -E 's/\x1b\\[[0-9;]*m//g' \
    | head -n 60 || true
}

get_auth_expiry_hint() {
  local auth_path="$1"
  local profile_key=""
  case "$auth_path" in
    anthropic-oauth) profile_key="anthropic:manual" ;;
    anthropic-api) profile_key="anthropic:default" ;;
    *) echo ""; return 0 ;;
  esac
  if [[ ! -f "$PROFILE_PATH" ]]; then
    echo ""
    return 0
  fi
  python3 - "$PROFILE_PATH" "$profile_key" <<'PY'
import json
import sys
from datetime import date

profile_path, profile_key = sys.argv[1], sys.argv[2]
try:
    obj = json.load(open(profile_path, "r", encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)

meta = (((obj.get("openclaw") or {}).get("authProfileMetadata") or {}).get(profile_key) or {})
setup_at = str(meta.get("setupAt") or "").strip()
renew_after = int(meta.get("renewAfterDays") or 365)
if not setup_at:
    print("")
    raise SystemExit(0)
try:
    y, m, d = [int(x) for x in setup_at.split("-")]
    setup_date = date(y, m, d)
except Exception:
    print("")
    raise SystemExit(0)

days = (date.today() - setup_date).days
days_left = renew_after - days
if days_left <= 30:
    print(f"hint=possible-token-expiry setupAt={setup_at} ageDays={days} renewAfterDays={renew_after}")
else:
    print(f"hint=token-age setupAt={setup_at} ageDays={days} renewAfterDays={renew_after}")
PY
}

for auth_path in "${PATHS[@]}"; do
  auth_path="$(echo "$auth_path" | xargs)"
  if [[ -z "$auth_path" ]]; then
    continue
  fi
  echo "================================================================"
  echo "[matrix] Running e2e for auth path: ${auth_path}"
  echo "================================================================"
  log_file="$(mktemp -t "quaid-e2e-${auth_path}")"
  runner_args=(--auth-path "$auth_path")
  if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    runner_args+=("${EXTRA_ARGS[@]}")
  fi
  if "$RUNNER" "${runner_args[@]}" >"$log_file" 2>&1; then
    cat "$log_file"
    RESULTS+=("${auth_path}=pass")
    REASONS+=("${auth_path}=ok")
  else
    cat "$log_file"
    RESULTS+=("${auth_path}=fail")
    REASONS+=("${auth_path}=$(classify_failure_reason "$log_file")")
  fi
  exc_file="$(mktemp -t "quaid-e2e-${auth_path}-exceptions")"
  extract_exception_lines "$log_file" >"$exc_file"
  EXCEPTION_FILES+=("${auth_path}=${exc_file}")
  rm -f "$log_file"
done

bundle_file="/tmp/quaid-e2e-matrix-exceptions-$(date +%Y%m%d-%H%M%S).json"
python3 - "$bundle_file" "${EXCEPTION_FILES[@]-}" <<'PY'
import json
import os
import sys

out = {"paths": {}}
for item in sys.argv[2:]:
    if not item or "=" not in item:
        continue
    auth_path, file_path = item.split("=", 1)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f.readlines() if ln.strip()]
    except Exception:
        lines = []
    out["paths"][auth_path] = {
        "exception_count": len(lines),
        "exceptions": lines,
    }

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
    f.write("\n")
print(sys.argv[1])
PY
for pair in "${EXCEPTION_FILES[@]-}"; do
  [[ -z "$pair" ]] && continue
  rm -f "${pair#*=}" || true
done

echo
echo "[matrix] Results:"
echo "[matrix] Exception bundle: ${bundle_file}"
for auth_path in "${PATHS[@]}"; do
  auth_path="$(echo "$auth_path" | xargs)"
  [[ -z "$auth_path" ]] && continue
  status="$(lookup_value "$auth_path" "skipped" "${RESULTS[@]-}")"
  expected="$(lookup_value "$auth_path" "pass" "${EXPECTED[@]-}")"
  reason="$(lookup_value "$auth_path" "-" "${REASONS[@]-}")"
  expiry_hint=""
  if [[ "$status" == "fail" ]]; then
    expiry_hint="$(get_auth_expiry_hint "$auth_path")"
  fi
  if [[ "$status" != "$expected" ]]; then
    FAILED=1
    if [[ -n "$expiry_hint" ]]; then
      echo "  - ${auth_path}: ${status} (expected ${expected}) reason=${reason} ${expiry_hint}"
    else
      echo "  - ${auth_path}: ${status} (expected ${expected}) reason=${reason}"
    fi
  else
    if [[ -n "$expiry_hint" ]]; then
      echo "  - ${auth_path}: ${status} reason=${reason} ${expiry_hint}"
    else
      echo "  - ${auth_path}: ${status} reason=${reason}"
    fi
  fi
done

exit "$FAILED"
