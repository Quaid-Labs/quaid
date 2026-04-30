#!/usr/bin/env bash
# livetest-wipe.sh — Wipe Quaid from the remote host before a live test run
#
# Always runs via SSH to the remote host — NEVER touches the local machine.
# Reads connection and workspace details from livetest-config.json.
#
# Usage:
#   livetest-wipe.sh [options]
#   livetest-wipe.sh                        # full wipe (all silos)
#   livetest-wipe.sh --platform cc          # CC-only wipe (OC is live, leave it)
#   livetest-wipe.sh --dry-run              # print commands, do not run them
#   livetest-wipe.sh --config path/to/livetest-config.json
#
# Options:
#   --platform <all|oc|cc|cdx>  Which silo(s) to wipe (default: all)
#   --dry-run                   Print SSH commands without executing them
#   --config <path>             Path to livetest-config.json (default: auto-detected)
#   -h, --help                  Show this help
#
# Full wipe (--platform all):
#   1. Kill all extraction daemons
#   2. Uninstall OC plugin
#   3. Remove entire Quaid workspace + extensions dir
#   4. Clear OC session transcripts
#   5. Clear CC adapter rules and project history
#
# CC-only wipe (--platform cc):
#   Use when OC is already running and you only need to re-install CC.
#   1. Kill CC extraction daemons only (by QUAID_INSTANCE env)
#   2. Remove CC silo only
#   3. Clear CC hooks from ~/.claude/settings.json
#   4. Clear CC adapter rules and project history
#
# Safety: this script will print the remote host before doing anything and
# ask for confirmation unless LIVETEST_WIPE_YES=1 is set.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

# --- Defaults ---
PLATFORM="all"
DRY_RUN=0
CONFIG_PATH="$CONFIG_DEFAULT"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform) PLATFORM="$2"; shift 2 ;;
        --dry-run)  DRY_RUN=1; shift ;;
        --config)   CONFIG_PATH="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/{ s/^# //; s/^#//; p }' "$0"
            exit 0
            ;;
        *) echo "Error: unknown option '$1'" >&2; exit 1 ;;
    esac
done

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Error: config not found at '$CONFIG_PATH'" >&2
    echo "Copy livetest-config.template.json to livetest-config.json and fill it in." >&2
    exit 1
fi

# --- Read config ---
read_config() {
    python3 -c "
import sys, json
with open('$CONFIG_PATH') as f:
    c = json.load(f)
key = '$1'
parts = key.split('.')
val = c
for p in parts:
    val = val.get(p, '')
    if val == '':
        break
print(val)
"
}

REMOTE_HOST="$(read_config remote.host)"
WORKSPACE="$(read_config remote.workspace)"
OC_INSTANCE="$(read_config platforms.oc.instance_name)"
CC_INSTANCE="$(read_config platforms.cc.instance_name)"
CDX_INSTANCE="$(read_config platforms.cdx.instance_name)"
CC_PROJECT_DIR="$(read_config platforms.cc.project_dir)"

if [[ -z "$REMOTE_HOST" || -z "$WORKSPACE" ]]; then
    echo "Error: remote.host and remote.workspace must be set in $CONFIG_PATH" >&2
    exit 1
fi

# --- Safety banner ---
echo "livetest-wipe.sh"
echo "  Remote host : $REMOTE_HOST"
echo "  Workspace   : $WORKSPACE"
echo "  Platform    : $PLATFORM"
[[ "$DRY_RUN" == "1" ]] && echo "  Mode        : DRY RUN (no commands will execute)"
echo ""

if [[ "${LIVETEST_WIPE_YES:-0}" != "1" && "$DRY_RUN" == "0" ]]; then
    read -r -p "Wipe $PLATFORM on $REMOTE_HOST? [y/N] " confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo "Aborted." >&2
        exit 1
    fi
fi

# --- Executor ---
run_remote() {
    local desc="$1"
    local cmd="$2"
    echo "  >> $desc"
    if [[ "$DRY_RUN" == "0" ]]; then
        ssh "$REMOTE_HOST" "$cmd"
    else
        echo "     ssh $REMOTE_HOST '$cmd'"
    fi
}

# --- Wipe functions ---

wipe_oc() {
    echo "--- OC wipe ---"
    run_remote "kill OC-instance extraction daemons" \
        "for pid in \$(pgrep -f extraction_daemon.py 2>/dev/null); do if ps eww \$pid 2>/dev/null | grep -q 'QUAID_INSTANCE=$OC_INSTANCE'; then kill -9 \$pid 2>/dev/null; fi; done; echo 'OC-instance daemons killed'"
    run_remote "wipe OC silo + extension + native memory state" \
        "python3 - <<'PYEOF'
from pathlib import Path
import json
import os
import shutil

workspace = Path(os.path.expanduser('$WORKSPACE'))
oc_instance = '$OC_INSTANCE'
home = Path.home()
cfg_path = home / '.openclaw' / 'openclaw.json'

targets = [
    workspace / 'instances' / oc_instance,
    home / '.openclaw' / 'extensions' / 'quaid',
    home / '.openclaw' / 'agents' / 'main' / 'sessions',
    home / '.openclaw' / 'workspace' / 'memory',
    home / '.openclaw' / 'workspace' / 'git-commits',
]

workspace_candidates = []
try:
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
except Exception:
    cfg = {}

agents = cfg.get('agents') if isinstance(cfg, dict) else {}
defaults = agents.get('defaults') if isinstance(agents, dict) else {}
default_workspace = defaults.get('workspace') if isinstance(defaults, dict) else None
if isinstance(default_workspace, str) and default_workspace.strip():
    workspace_candidates.append(default_workspace.strip())

agent_list = agents.get('list') if isinstance(agents, dict) else []
if isinstance(agent_list, list):
    for entry in agent_list:
        if not isinstance(entry, dict):
            continue
        raw = entry.get('workspace')
        if isinstance(raw, str) and raw.strip():
            workspace_candidates.append(raw.strip())

for raw in workspace_candidates:
    resolved = Path(os.path.expanduser(raw))
    if not resolved.is_absolute():
        resolved = (cfg_path.parent / resolved).resolve()
    targets.append(resolved / 'memory')
    targets.append(resolved / 'git-commits')

seen = set()
for target in targets:
    key = str(target)
    if key in seen:
        continue
    seen.add(key)
    shutil.rmtree(target, ignore_errors=True)

if isinstance(cfg, dict):
    plugins = cfg.get('plugins')
    if isinstance(plugins, dict):
        entries = plugins.get('entries')
        if isinstance(entries, dict):
            entries.pop('quaid', None)
        installs = plugins.get('installs')
        if isinstance(installs, dict):
            installs.pop('quaid', None)
        install_records = plugins.get('installRecords')
        if isinstance(install_records, dict):
            install_records.pop('quaid', None)
        allow = plugins.get('allow')
        if isinstance(allow, list):
            plugins['allow'] = [item for item in allow if str(item).strip() != 'quaid']
        slots = plugins.get('slots')
        if isinstance(slots, dict) and slots.get('memory') == 'quaid':
            slots.pop('memory', None)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg_path.with_name(f'.{cfg_path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(cfg, indent=2) + '\\n', encoding='utf-8')
        tmp.replace(cfg_path)

print('OC silo, sessions, extension, native memory state, and stale quaid plugin refs wiped')
PYEOF"
}

wipe_cc() {
    echo "--- CC wipe ---"
    if [[ "$PLATFORM" == "all" ]]; then
        run_remote "kill CC extraction daemons" \
            "pkill -9 -f extraction_daemon.py 2>/dev/null; echo 'CC daemons killed (or none running)'"
    else
        # CC-only: kill only the CC instance daemon, leave others
        run_remote "kill CC-instance extraction daemons" \
            "for pid in \$(pgrep -f extraction_daemon.py 2>/dev/null); do if ps eww \$pid 2>/dev/null | grep -q 'QUAID_INSTANCE=$CC_INSTANCE'; then kill -9 \$pid 2>/dev/null; fi; done; echo 'CC-instance daemons killed'"
    fi
    run_remote "wipe CC silo" \
        "rm -rf $WORKSPACE/instances/$CC_INSTANCE && echo 'CC silo wiped'"
    run_remote "clear CC hooks from settings.json" \
        "python3 -c \"
import json; from pathlib import Path
p = Path.home() / '.claude/settings.json'
if p.exists():
    d = json.loads(p.read_text())
    for ev, entries in list(d.get('hooks', {}).items()):
        d['hooks'][ev] = [e for e in entries if 'quaid' not in str(e).lower()]
    p.write_text(json.dumps(d, indent=2))
print('CC hooks cleared')
\""
    run_remote "clear CC adapter rules" \
        "bash -c 'rm -f ~/.claude/rules/quaid-*.md ~/.claude/rules/quaid-projects.md ~/.claude/rules/quaid-projects.md.bak' && echo 'CC rules cleared'"
    # Wipe entire ~/.claude/projects/ directory. Previous logic derived a single
    # target from CC_PROJECT_DIR but macOS normalizes /tmp → /private/tmp in the
    # project-dir-sanitized form Claude uses, so the targeted rm missed the
    # actual directory (e.g. '-private-tmp-cc-livetest' vs '-tmp-cc-livetest').
    # Stray project dirs from other sessions on the same VM also contaminate
    # the post-install hook. Only the livetest uses CC on the VM, so full clear
    # is safe.
    run_remote "clear all CC project conversation history" \
        "rm -rf ~/.claude/projects && echo 'CC project history cleared (entire projects/ dir)'"
}

wipe_cdx() {
    echo "--- CDX wipe ---"
    run_remote "kill CDX extraction daemons" \
        "for pid in \$(pgrep -f extraction_daemon.py 2>/dev/null); do if ps eww \$pid 2>/dev/null | grep -q 'QUAID_INSTANCE=$CDX_INSTANCE'; then kill -9 \$pid 2>/dev/null; fi; done; echo 'CDX-instance daemons killed'"
    run_remote "wipe CDX silo" \
        "rm -rf $WORKSPACE/instances/$CDX_INSTANCE && echo 'CDX silo wiped'"
    run_remote "clear Codex Quaid hooks" \
        "python3 -c \"from pathlib import Path; p=Path.home()/'.codex'/'hooks.json'; p.unlink(missing_ok=True); print('Codex Quaid hooks cleared')\""
    run_remote "clear Codex config JSON" \
        "python3 -c \"from pathlib import Path; p=Path.home()/'.codex'/'config.json'; p.unlink(missing_ok=True); print('Codex config.json cleared')\""
    run_remote "clear Codex hook feature flag" \
        "python3 -c \"
from pathlib import Path
import re
p = Path.home() / '.codex' / 'config.toml'
if not p.exists():
    print('No Codex config.toml')
else:
    txt = p.read_text()
    txt = re.sub(r'codex_hooks\s*=\s*true\s*\n?', '', txt)
    txt = re.sub(r'\[features\]\s*\n(?=\[|\Z)', '', txt)
    p.write_text(txt)
    print('Codex feature flag cleared')
\""
}

wipe_shared() {
    echo "--- Shared workspace ---"
    run_remote "wipe shared Quaid workspace root" \
        "rm -rf $WORKSPACE && echo 'Workspace wiped: $WORKSPACE'"
}

# --- Dispatch ---
case "$PLATFORM" in
    all)
        wipe_oc
        wipe_cc
        wipe_cdx
        wipe_shared
        ;;
    oc)
        wipe_oc
        ;;
    cc)
        wipe_cc
        ;;
    cdx)
        wipe_cdx
        ;;
    *)
        echo "Error: unknown platform '$PLATFORM' (valid: all, oc, cc, cdx)" >&2
        exit 1
        ;;
esac

echo ""
echo "Wipe complete ($PLATFORM on $REMOTE_HOST)."
