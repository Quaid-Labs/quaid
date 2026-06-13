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
#   2. Instance identity is explicitly pinned in project settings, bound via
#      Quaid's instance-binding files, or matches CC's path-derived fallback
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
  REMOTE_HOST="$(python3 - "$CONFIG_PATH" <<'PY'
import json
import sys
from pathlib import Path

cfg = json.loads(Path(sys.argv[1]).read_text())
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
import hashlib
import json
import re
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
digest = hashlib.sha256(resolved_project_dir.encode("utf-8", "surrogatepass")).hexdigest()[:12]
readable = re.sub(r"[^a-z0-9]+", "-", Path(resolved_project_dir).name.lower()).strip("-") or "project"
readable = readable[:39].strip("-") or "project"
adapter_id = "claude-code"
derived_slug = f"{readable}-{digest}"
derived_instance = f"{adapter_id}-{derived_slug}"
legacy_slug = re.sub(r"[^a-z0-9]+", "-", resolved_project_dir.lower()).strip("-")
session_dir_name = resolved_project_dir.replace("/", "-")
session_root = home / ".claude" / "projects" / session_dir_name
hook_trace_path = home / ".quaid" / "instances" / instance_id / "logs" / "quaid-hook-trace.jsonl"


def bound_instance_for_project():
    binding_root = home / ".quaid" / "shared" / "instance-bindings" / adapter_id
    candidate_slugs = [derived_slug]
    if legacy_slug and legacy_slug not in candidate_slugs:
        candidate_slugs.append(legacy_slug)

    for candidate_slug in candidate_slugs:
        path = binding_root / f"{candidate_slug}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"binding_error={path}:{exc}")
            continue
        bound_instance = str(data.get("instance") or "").strip()
        stored_project = str(data.get("project_dir") or "").strip()
        if not bound_instance:
            print(f"binding_instance=(invalid empty instance from {path})")
            continue
        if not stored_project:
            print(f"binding_instance=(skipped missing project_dir from {path})")
            continue
        try:
            if Path(stored_project).expanduser().resolve() != Path(resolved_project_dir):
                print(f"binding_instance=(skipped project mismatch from {path})")
                continue
        except Exception as exc:
            print(f"binding_instance=(skipped unresolved project from {path}: {exc})")
            continue
        config_path = home / ".quaid" / "instances" / bound_instance / "config.json"
        if not config_path.is_file():
            print(f"binding_instance=(skipped missing config for {bound_instance} from {path})")
            continue
        return bound_instance, path
    return "", None

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
if project_settings_path.is_file():
    data = json.loads(project_settings_path.read_text())
    actual_instance = str(((data.get("env") or {}).get("QUAID_INSTANCE")) or "").strip()
    if actual_instance:
        print(f"project_instance={actual_instance}")
    else:
        print(f"project_instance=(missing; derived fallback {derived_instance})")
    if actual_instance and actual_instance != instance_id:
        failures.append(
            f"project settings QUAID_INSTANCE mismatch: expected {instance_id} got {actual_instance}"
        )
else:
    print(f"project_instance=(path-derived fallback {derived_instance})")

binding_instance, binding_path = bound_instance_for_project()
if binding_instance:
    print(f"binding_instance={binding_instance} ({binding_path})")
else:
    print("binding_instance=(none)")

binding_matches_instance = binding_instance == instance_id
if derived_instance != instance_id and not project_settings_path.is_file() and not binding_matches_instance:
    failures.append(
        f"missing project settings and path-derived instance mismatch: expected {instance_id} got {derived_instance}"
    )
elif derived_instance != instance_id and project_settings_path.is_file() and not binding_matches_instance:
    data = json.loads(project_settings_path.read_text())
    actual_instance = str(((data.get("env") or {}).get("QUAID_INSTANCE")) or "").strip()
    if not actual_instance:
        failures.append(
            f"project settings omit QUAID_INSTANCE and path-derived instance mismatch: expected {instance_id} got {derived_instance}"
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

case "$REMOTE_HOST" in
  localhost|127.0.0.1|::1)
    python3 - "$PROJECT_DIR" "$INSTANCE_ID" "$MAX_AGE_MIN" <<<"$remote_python"
    ;;
  *)
    ssh "$REMOTE_HOST" python3 - "$PROJECT_DIR" "$INSTANCE_ID" "$MAX_AGE_MIN" <<<"$remote_python"
    ;;
esac
