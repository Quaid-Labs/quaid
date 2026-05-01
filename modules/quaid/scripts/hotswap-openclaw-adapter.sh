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
  ssh "$HOST" "mkdir -p $remote_adapter_dir $remote_core_dir"
  scp "$LOCAL_ADAPTER_TS" "$HOST:$remote_adapter_dir/adapter.ts"
  scp "$LOCAL_ADAPTER_JS" "$HOST:$remote_adapter_dir/adapter.js"
  scp "$LOCAL_TIMEOUT_TS" "$HOST:$remote_core_dir/session-timeout.ts"
  scp "$LOCAL_TIMEOUT_JS" "$HOST:$remote_core_dir/session-timeout.js"
done

ssh "$HOST" 'export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"; if command -v openclaw >/dev/null 2>&1; then openclaw gateway restart; elif [ -x /opt/homebrew/bin/openclaw ]; then /opt/homebrew/bin/openclaw gateway restart; else echo "openclaw not found" >&2; exit 127; fi'
ssh "$HOST" 'export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"; if command -v openclaw >/dev/null 2>&1; then openclaw gateway status || true; elif [ -x /opt/homebrew/bin/openclaw ]; then /opt/homebrew/bin/openclaw gateway status || true; fi'

echo "Applied: files synced and gateway restart requested on $HOST"
