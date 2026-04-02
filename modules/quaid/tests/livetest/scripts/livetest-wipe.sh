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
    run_remote "kill OC extraction daemons" \
        "pkill -9 -f extraction_daemon.py 2>/dev/null; echo 'OC daemons killed (or none running)'"
    run_remote "uninstall OC plugin" \
        "echo y | openclaw plugins uninstall quaid 2>/dev/null; echo 'OC plugin uninstalled (or not installed)'"
    run_remote "wipe OC silo + extensions" \
        "rm -rf $WORKSPACE/$OC_INSTANCE && rm -rf ~/.openclaw/extensions/quaid && echo 'OC silo + extensions wiped'"
    run_remote "clear OC session transcripts" \
        "rm -rf ~/.openclaw/agents/main/sessions/ && echo 'OC sessions cleared'"
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
        "rm -rf $WORKSPACE/$CC_INSTANCE && echo 'CC silo wiped'"
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
        "rm -f ~/.claude/rules/quaid-projects.md && echo 'CC rules cleared'"
    # Derive safe project dir path for clearing CC conversation history
    CC_PROJ_SAFE="$(echo "$CC_PROJECT_DIR" | sed 's|^/||; s|/|-|g')"
    run_remote "clear CC project conversation history" \
        "rm -rf ~/.claude/projects/-${CC_PROJ_SAFE} && echo 'CC project history cleared'"
}

wipe_cdx() {
    echo "--- CDX wipe ---"
    run_remote "kill CDX extraction daemons" \
        "for pid in \$(pgrep -f extraction_daemon.py 2>/dev/null); do if ps eww \$pid 2>/dev/null | grep -q 'QUAID_INSTANCE=$CDX_INSTANCE'; then kill -9 \$pid 2>/dev/null; fi; done; echo 'CDX-instance daemons killed'"
    run_remote "wipe CDX silo" \
        "rm -rf $WORKSPACE/$CDX_INSTANCE && echo 'CDX silo wiped'"
    run_remote "clear Codex Quaid hooks" \
        "python3 - <<'PY'\nimport json\nfrom pathlib import Path\nhooks_path = Path.home() / '.codex' / 'hooks.json'\nif hooks_path.exists():\n    try:\n        data = json.loads(hooks_path.read_text())\n    except Exception:\n        data = {}\n    hooks = data.get('hooks') if isinstance(data, dict) else {}\n    if not isinstance(hooks, dict):\n        hooks = {}\n    cleaned = {}\n    for event, entries in hooks.items():\n        kept_entries = []\n        for entry in entries or []:\n            if not isinstance(entry, dict):\n                continue\n            subhooks = []\n            for hook in entry.get('hooks') or []:\n                cmd = str((hook or {}).get('command') or '')\n                if 'quaid' in cmd.lower():\n                    continue\n                subhooks.append(hook)\n            if subhooks:\n                new_entry = dict(entry)\n                new_entry['hooks'] = subhooks\n                kept_entries.append(new_entry)\n        if kept_entries:\n            cleaned[event] = kept_entries\n    if cleaned:\n        hooks_path.write_text(json.dumps({'hooks': cleaned}, indent=2) + '\\n')\n    else:\n        hooks_path.unlink(missing_ok=True)\nprint('Codex Quaid hooks cleared')\nPY"
    run_remote "clear Codex hook feature flag" \
        "python3 - <<'PY'\nfrom pathlib import Path\nconfig_path = Path.home() / '.codex' / 'config.toml'\nif not config_path.exists():\n    print('No Codex config.toml')\n    raise SystemExit(0)\nlines = config_path.read_text().splitlines()\nout = []\nin_features = False\nfeatures_lines = []\n\ndef flush_features():\n    global features_lines\n    if not features_lines:\n        return\n    kept = [line for line in features_lines if line.strip() != 'codex_hooks = true']\n    nonempty = [line for line in kept if line.strip()]\n    if nonempty:\n        out.append('[features]')\n        out.extend(kept)\n    features_lines = []\n\nfor line in lines:\n    stripped = line.strip()\n    if stripped.startswith('[') and stripped.endswith(']'):\n        if in_features:\n            flush_features()\n            in_features = False\n        if stripped == '[features]':\n            in_features = True\n            features_lines = []\n            continue\n        out.append(line)\n        continue\n    if in_features:\n        features_lines.append(line)\n    else:\n        out.append(line)\nif in_features:\n    flush_features()\nconfig_path.write_text('\\n'.join(out).rstrip() + '\\n')\nprint('Codex feature flag cleared')\nPY"
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
