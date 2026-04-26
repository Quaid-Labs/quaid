#!/usr/bin/env bash
# verify-cc-session-capture.sh — Verify Claude Code created a real transcript for CC lane
#
# Usage:
#   verify-cc-session-capture.sh [--remote HOST] [--config PATH] [--project-dir DIR] [--instance ID] [--max-age-min N]
#
# Defaults:
#   --config      tests/livetest/livetest-config.json
#   --project-dir /tmp/cc-livetest
#   --instance    claude-code-private-tmp-cc-livetest
#   --max-age-min 5
#
# Checks:
#   1. ~/.claude/settings.json contains Quaid CC hooks
#   2. <project-dir>/.claude/settings.json contains the expected QUAID_INSTANCE
#   3. ~/.claude/projects/<encoded-project-dir> has a fresh *.jsonl transcript
#   4. The instance hook trace file exists (best-effort; absence is reported)
#
# Exit codes:
#   0 = pass
#   1 = one or more checks failed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

REMOTE_HOST=""
CONFIG_PATH="$CONFIG_DEFAULT"
PROJECT_DIR="/tmp/cc-livetest"
INSTANCE_ID="claude-code-private-tmp-cc-livetest"
MAX_AGE_MIN=5

usage() {
  sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote) REMOTE_HOST="$2"; shift 2 ;;
    --config) CONFIG_PATH="$2"; shift 2 ;;
    --project-dir) PROJECT_DIR="$2"; shift 2 ;;
    --instance) INSTANCE_ID="$2"; shift 2 ;;
    --max-age-min) MAX_AGE_MIN="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Error: unknown option '$1'" >&2; exit 1 ;;
  esac
done

if [[ -z "$REMOTE_HOST" ]]; then
  if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Error: config not found at '$CONFIG_PATH'" >&2
    exit 1
  fi
  REMOTE_HOST="$(python3 - <<PY
import json
from pathlib import Path
cfg = json.loads(Path(${CONFIG_PATH@Q}).read_text())
print(str(((cfg.get("remote") or {}).get("host")) or "").strip())
PY
)"
fi

if [[ -z "$REMOTE_HOST" ]]; then
  echo "Error: remote host is required (--remote or config remote.host)" >&2
  exit 1
fi

echo "verify-cc-session-capture.sh"
echo "  Remote host : $REMOTE_HOST"
echo "  Project dir : $PROJECT_DIR"
echo "  Instance    : $INSTANCE_ID"
echo "  Fresh window: ${MAX_AGE_MIN}m"
echo ""

remote_python="$(cat <<'PY'
import glob
import json
import sys
from pathlib import Path

project_dir = sys.argv[1]
instance_id = sys.argv[2]
max_age_min = int(sys.argv[3])

required_hooks = {"SessionStart", "UserPromptSubmit", "PreCompact", "SessionEnd"}
failures = []

home = Path.home()
global_settings_path = home / ".claude" / "settings.json"
project_settings_path = Path(project_dir) / ".claude" / "settings.json"
resolved_project_dir = str(Path(project_dir).resolve())
session_dir_name = resolved_project_dir.replace("/", "-")
session_root = home / ".claude" / "projects" / session_dir_name
hook_trace_path = home / ".quaid" / "instances" / instance_id / "logs" / "quaid-hook-trace.jsonl"

print(f"global_settings={global_settings_path}")
if not global_settings_path.is_file():
    failures.append(f"missing global Claude settings: {global_settings_path}")
else:
    data = json.loads(global_settings_path.read_text())
    hooks = set((data.get("hooks") or {}).keys())
    print(f"global_hook_keys={sorted(hooks)}")
    missing = sorted(required_hooks - hooks)
    if missing:
        failures.append(f"missing Claude hooks in ~/.claude/settings.json: {missing}")

print(f"project_settings={project_settings_path}")
if not project_settings_path.is_file():
    failures.append(f"missing project settings: {project_settings_path}")
else:
    data = json.loads(project_settings_path.read_text())
    actual_instance = str(((data.get("env") or {}).get("QUAID_INSTANCE")) or "").strip()
    print(f"project_instance={actual_instance or '(missing)'}")
    if actual_instance != instance_id:
        failures.append(
            f"project settings QUAID_INSTANCE mismatch: expected {instance_id} got {actual_instance or '(missing)'}"
        )

print(f"session_root={session_root}")
if not session_root.is_dir():
    failures.append(f"missing Claude session directory: {session_root}")
else:
    fresh_cutoff = max_age_min * 60
    now = __import__("time").time()
    fresh = []
    for path in sorted(session_root.glob("*.jsonl")):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age <= fresh_cutoff:
            fresh.append((path, age))
    if not fresh:
        failures.append(
            f"no fresh session jsonl under {session_root} within {max_age_min} minutes"
        )
    else:
        shown = ", ".join(f"{p.name}:{int(age)}s" for p, age in fresh[:5])
        print(f"fresh_sessions={shown}")

print(f"hook_trace={hook_trace_path}")
if hook_trace_path.is_file():
    try:
        lines = hook_trace_path.read_text(encoding="utf-8").splitlines()
        preview = lines[-3:]
        print("hook_trace_tail=")
        for line in preview:
            print(line)
    except OSError:
        failures.append(f"could not read hook trace: {hook_trace_path}")
else:
    failures.append(f"missing hook trace file: {hook_trace_path}")

if failures:
    print("")
    print("FAIL")
    for failure in failures:
        print(f"- {failure}")
    raise SystemExit(1)

print("")
print("PASS")
PY
)"

ssh "$REMOTE_HOST" "python3 - ${PROJECT_DIR@Q} ${INSTANCE_ID@Q} ${MAX_AGE_MIN@Q}" <<<"$remote_python"
