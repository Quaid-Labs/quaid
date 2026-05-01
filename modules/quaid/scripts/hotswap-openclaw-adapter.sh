#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Usage:
  scripts/hotswap-openclaw-adapter.sh --host <ssh-host> [--plugin-dir <remote-path>] [--apply]

Description:
  Copy local OpenClaw adapter/runtime files to a remote host without reinstalling Quaid.
  Default mode is dry-run (shows actions only). Use --apply to execute.

Defaults:
  Sync both ~/.openclaw/extensions/quaid (active OpenClaw extension)
  and ~/.quaid/plugins/quaid (installed Quaid plugin copy).

Options:
  --plugin-dir may be repeated. Supplying it replaces the default target list.
USAGE
}

HOST=""
DEFAULT_PLUGIN_DIRS=('~/.openclaw/extensions/quaid' '~/.quaid/plugins/quaid')
PLUGIN_DIRS=()
APPLY=0

remote_quote() {
  local value="$1"
  if [[ "$value" == "~" ]]; then
    printf "~"
    return
  fi
  if [[ "$value" == "~/"* ]]; then
    local rest="${value#~/}"
    printf "~/'%s'" "$(printf '%s' "$rest" | sed "s/'/'\\\\''/g")"
    return
  fi
  printf "'%s'" "$(printf '%s' "$value" | sed "s/'/'\\\\''/g")"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:-}"; shift 2 ;;
    --plugin-dir)
      PLUGIN_DIRS+=("${2:-}"); shift 2 ;;
    --apply)
      APPLY=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2 ;;
  esac
done

if [[ "${#PLUGIN_DIRS[@]}" -eq 0 ]]; then
  PLUGIN_DIRS=("${DEFAULT_PLUGIN_DIRS[@]}")
fi

if [[ -z "$HOST" ]]; then
  echo "Missing --host" >&2
  usage
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_ADAPTER_TS="$MOD_DIR/adaptors/openclaw/adapter.ts"
LOCAL_ADAPTER_JS="$MOD_DIR/adaptors/openclaw/adapter.js"
LOCAL_TIMEOUT_TS="$MOD_DIR/core/session-timeout.ts"
LOCAL_TIMEOUT_JS="$MOD_DIR/core/session-timeout.js"

for f in "$LOCAL_ADAPTER_TS" "$LOCAL_ADAPTER_JS" "$LOCAL_TIMEOUT_TS" "$LOCAL_TIMEOUT_JS"; do
  if [[ ! -f "$f" ]]; then
    echo "Missing local file: $f" >&2
    exit 2
  fi
done

echo "Target host: $HOST"
echo "Target plugin dirs:"
for plugin_dir in "${PLUGIN_DIRS[@]}"; do
  echo "- $plugin_dir"
done
echo "Files to sync:"
for plugin_dir in "${PLUGIN_DIRS[@]}"; do
  echo "- $LOCAL_ADAPTER_TS -> $plugin_dir/adaptors/openclaw/adapter.ts"
  echo "- $LOCAL_ADAPTER_JS -> $plugin_dir/adaptors/openclaw/adapter.js"
  echo "- $LOCAL_TIMEOUT_TS -> $plugin_dir/core/session-timeout.ts"
  echo "- $LOCAL_TIMEOUT_JS -> $plugin_dir/core/session-timeout.js"
done

echo ""
if [[ "$APPLY" -eq 0 ]]; then
  echo "DRY RUN only. Re-run with --apply to execute copy + gateway restart."
  exit 0
fi

for plugin_dir in "${PLUGIN_DIRS[@]}"; do
  remote_adapter_dir="$plugin_dir/adaptors/openclaw"
  remote_core_dir="$plugin_dir/core"
  quoted_adapter_dir="$(remote_quote "$remote_adapter_dir")"
  quoted_core_dir="$(remote_quote "$remote_core_dir")"
  ssh "$HOST" "mkdir -p $quoted_adapter_dir $quoted_core_dir"
  ssh "$HOST" "cat > $quoted_adapter_dir/adapter.ts" < "$LOCAL_ADAPTER_TS"
  ssh "$HOST" "cat > $quoted_adapter_dir/adapter.js" < "$LOCAL_ADAPTER_JS"
  ssh "$HOST" "cat > $quoted_core_dir/session-timeout.ts" < "$LOCAL_TIMEOUT_TS"
  ssh "$HOST" "cat > $quoted_core_dir/session-timeout.js" < "$LOCAL_TIMEOUT_JS"
done

GATEWAY_RESTART_HELPER="$MOD_DIR/tests/livetest/scripts/livetest-openclaw-gateway-restart.sh"
if [[ ! -x "$GATEWAY_RESTART_HELPER" ]]; then
  echo "Missing gateway restart helper: $GATEWAY_RESTART_HELPER" >&2
  exit 2
fi
"$GATEWAY_RESTART_HELPER" --host "$HOST" --restart

echo "Applied: files synced and gateway restart requested on $HOST"
