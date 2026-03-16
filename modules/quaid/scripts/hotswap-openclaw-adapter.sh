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
  --plugin-dir ~/.openclaw/extensions/quaid
USAGE
}

HOST=""
PLUGIN_DIR='~/.openclaw/extensions/quaid'
APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      HOST="${2:-}"; shift 2 ;;
    --plugin-dir)
      PLUGIN_DIR="${2:-}"; shift 2 ;;
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

REMOTE_ADAPTER_DIR="$PLUGIN_DIR/adaptors/openclaw"
REMOTE_CORE_DIR="$PLUGIN_DIR/core"

echo "Target host: $HOST"
echo "Target plugin dir: $PLUGIN_DIR"
echo "Files to sync:"
echo "- $LOCAL_ADAPTER_TS -> $REMOTE_ADAPTER_DIR/adapter.ts"
echo "- $LOCAL_ADAPTER_JS -> $REMOTE_ADAPTER_DIR/adapter.js"
echo "- $LOCAL_TIMEOUT_TS -> $REMOTE_CORE_DIR/session-timeout.ts"
echo "- $LOCAL_TIMEOUT_JS -> $REMOTE_CORE_DIR/session-timeout.js"

echo ""
if [[ "$APPLY" -eq 0 ]]; then
  echo "DRY RUN only. Re-run with --apply to execute copy + gateway restart."
  exit 0
fi

ssh "$HOST" "mkdir -p $REMOTE_ADAPTER_DIR $REMOTE_CORE_DIR"
scp "$LOCAL_ADAPTER_TS" "$HOST:$REMOTE_ADAPTER_DIR/adapter.ts"
scp "$LOCAL_ADAPTER_JS" "$HOST:$REMOTE_ADAPTER_DIR/adapter.js"
scp "$LOCAL_TIMEOUT_TS" "$HOST:$REMOTE_CORE_DIR/session-timeout.ts"
scp "$LOCAL_TIMEOUT_JS" "$HOST:$REMOTE_CORE_DIR/session-timeout.js"

ssh "$HOST" "openclaw gateway restart"
ssh "$HOST" "openclaw gateway status || true"

echo "Applied: files synced and gateway restart requested on $HOST"
