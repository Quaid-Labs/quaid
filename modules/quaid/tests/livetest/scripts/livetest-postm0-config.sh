#!/usr/bin/env bash
# livetest-postm0-config.sh — apply post-M0 config overrides to per-platform
# config and restart daemons so the overrides take effect.
#
# Usage:
#   livetest-postm0-config.sh <platform|all> [<platform>...]
#
#   livetest-postm0-config.sh cc              # just CC
#   livetest-postm0-config.sh oc cc cdx       # all three explicitly
#   livetest-postm0-config.sh all             # every installed platform
#
# Platforms map to config dirs:
#   cc  -> ~/.quaid/shared/config/claude_code/config.json
#   oc  -> ~/.quaid/shared/config/openclaw/config.json
#   cdx -> ~/.quaid/shared/config/codex/config.json
#
# Overrides written (safe for all platforms, all milestones):
#   livetest.enableExtractionBufferLog: true
#   capture.chunk_tokens: 1500
#
# Per-platform is the correct layer: platform config supersedes global, and
# per-instance can override platform later (e.g. M4 idle-timeout flip on one
# platform only). Writing to global risks contaminating other lanes.
#
# After the merge, the daemon for every installed instance is restarted so the
# new values are loaded.
#
# Remote host is discovered from livetest-config.json (same file preflight uses).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${LIVETEST_CONFIG_PATH:-$SCRIPT_DIR/../livetest-config.json}"

if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "error: livetest-config.json not found at $CONFIG_PATH" >&2
    exit 1
fi

REMOTE_HOST="$(python3 -c "import json,sys; print(json.load(open('$CONFIG_PATH'))['remote']['host'])")"
if [[ -z "$REMOTE_HOST" ]]; then
    echo "error: remote.host missing in $CONFIG_PATH" >&2
    exit 1
fi

if [[ $# -eq 0 ]]; then
    echo "usage: $0 <platform|all> [<platform>...]" >&2
    exit 1
fi

declare -a platforms
if [[ "$1" == "all" ]]; then
    platforms=(openclaw claude-code codex)
else
    for arg in "$@"; do
        case "$arg" in
            oc|openclaw)                platforms+=(openclaw) ;;
            cc|claude-code|claude_code) platforms+=(claude-code) ;;
            cdx|codex)                  platforms+=(codex) ;;
            *)
                echo "error: unknown platform '$arg'" >&2
                exit 1
                ;;
        esac
    done
fi

for platform in "${platforms[@]}"; do
    echo "[postm0] merging overrides into $platform platform config..."
    ssh "$REMOTE_HOST" "python3 - '$platform' <<'PYEOF'
import json, os, pathlib, sys
platform = sys.argv[1]
p = pathlib.Path.home() / '.quaid' / 'shared' / 'config' / platform / 'config.json'
if not p.exists():
    print(f'  SKIP: {p} does not exist (platform not installed yet)')
    raise SystemExit(0)
existing = json.loads(p.read_text())
overrides = {
    'livetest': {'enableExtractionBufferLog': True},
    'capture': {'chunk_tokens': 1500},
}
def merge(base, over):
    out = json.loads(json.dumps(base))
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out
merged = merge(existing, overrides)
p.write_text(json.dumps(merged, indent=2))
print(f'  merged {p}')
print(f'  capture.chunk_tokens={merged.get(\"capture\",{}).get(\"chunk_tokens\")}')
print(f'  livetest.enableExtractionBufferLog={merged.get(\"livetest\",{}).get(\"enableExtractionBufferLog\")}')
PYEOF
"
done

echo
echo "[postm0] restarting daemons on each installed instance..."

ssh "$REMOTE_HOST" "bash <<'REMEOF'
set -e
source ~/.zprofile 2>/dev/null || true
QCLI=~/.quaid/plugins/quaid/quaid
if [[ ! -x \"\$QCLI\" ]]; then
    QCLI=~/.openclaw/extensions/quaid/quaid
fi
if [[ ! -x \"\$QCLI\" ]]; then
    echo '  error: could not find quaid CLI wrapper' >&2
    exit 1
fi
for inst_dir in ~/.quaid/instances/*/; do
    inst=\$(basename \"\$inst_dir\")
    echo \"  restarting daemon for instance: \$inst\"
    QUAID_HOME=~/.quaid QUAID_INSTANCE=\"\$inst\" \"\$QCLI\" daemon stop  2>&1 | tail -1 || true
    sleep 1
    QUAID_HOME=~/.quaid QUAID_INSTANCE=\"\$inst\" \"\$QCLI\" daemon start 2>&1 | tail -1
done
REMEOF
"

if printf '%s\n' "${platforms[@]}" | grep -qx 'openclaw'; then
    echo
    echo "[postm0] restarting OpenClaw gateway so it loads the M0-installed Quaid extension..."
    "$SCRIPT_DIR/livetest-openclaw-gateway-restart.sh" --restart --host "$REMOTE_HOST" --config "$CONFIG_PATH"
fi

echo
echo "[postm0] verification — resolved chunk_tokens per instance:"
ssh "$REMOTE_HOST" "bash <<'REMEOF'
source ~/.zprofile 2>/dev/null || true
QCLI=~/.quaid/plugins/quaid/quaid
if [[ ! -x \"\$QCLI\" ]]; then
    QCLI=~/.openclaw/extensions/quaid/quaid
fi
for inst_dir in ~/.quaid/instances/*/; do
    inst=\$(basename \"\$inst_dir\")
    val=\$(QUAID_HOME=~/.quaid QUAID_INSTANCE=\"\$inst\" \"\$QCLI\" config show 2>/dev/null | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d.get(\"capture\",{}).get(\"chunk_tokens\",\"?\"))' 2>/dev/null || echo '?')
    echo \"  \$inst: capture.chunk_tokens=\$val\"
done
REMEOF
"

echo
echo "[postm0] done. Remember: per-instance config can still override platform;"
echo "         check this if a later step appears to ignore the override."
