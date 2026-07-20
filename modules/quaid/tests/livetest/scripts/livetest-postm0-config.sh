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
#   cc  -> ~/.quaid/shared/config/claude-code/config.json
#   oc  -> ~/.quaid/shared/config/openclaw/config.json
#   cdx -> ~/.quaid/shared/config/codex/config.json
#
# Overrides written (safe for all platforms, all milestones):
#   livetest.enableExtractionBufferLog: true
#   capture.chunk_tokens: 1500
#   claude-code only: models.fastReasoning: claude-haiku-4-5-20251001
#   claude-code only: models.deepReasoning: claude-sonnet-4-6
#   openclaw only: models.fastReasoning: claude-haiku-4-5
#   openclaw only: models.deepReasoning: claude-sonnet-4-6
#   codex only: models.llmProvider: openai
#   codex only: models.deepReasoning: gpt-5.4
#
# Per-platform is the correct layer: platform config supersedes global, and
# per-instance can override platform later (e.g. M2 Part C timeout flip on one
# instance only). Writing to global risks contaminating other lanes.
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
if platform in {'claude-code', 'openclaw'}:
    models = overrides.setdefault('models', {})
    models['deepReasoning'] = 'claude-sonnet-4-6'
    if platform == 'claude-code':
        models['fastReasoning'] = 'claude-haiku-4-5-20251001'
    elif platform == 'openclaw':
        models['fastReasoning'] = 'claude-haiku-4-5'
elif platform == 'codex':
    models = overrides.setdefault('models', {})
    models['llmProvider'] = 'openai'
    models['deepReasoning'] = 'gpt-5.4'
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
print(f'  models.llmProvider={merged.get(\"models\",{}).get(\"llmProvider\")}')
print(f'  models.fastReasoning={merged.get(\"models\",{}).get(\"fastReasoning\")}')
print(f'  models.deepReasoning={merged.get(\"models\",{}).get(\"deepReasoning\")}')
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
    echo "[postm0] registering OpenClaw OpenAI model auth from VM Codex OAuth..."
    ssh "$REMOTE_HOST" 'bash -s' <<'REMEOF'
set -euo pipefail
source ~/.zprofile 2>/dev/null || true
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
if ! command -v openclaw >/dev/null 2>&1; then
    echo '  error: openclaw CLI not found; cannot register OpenClaw model auth' >&2
    exit 1
fi
TOKEN="$(
python3 <<'PYEOF'
import json
import pathlib

auth_path = pathlib.Path.home() / ".codex" / "auth.json"
try:
    payload = json.loads(auth_path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"failed reading {auth_path}: {exc}") from exc
tokens = payload.get("tokens", {})
if not isinstance(tokens, dict):
    tokens = {}
access_token = str(tokens.get("access_token", "")).strip()
if not access_token:
    raise SystemExit(f"missing tokens.access_token in {auth_path}")
print(access_token)
PYEOF
)"
printf '%s\n' "$TOKEN" | openclaw models auth paste-token --provider openai
echo '  registered OpenClaw openai provider models from ~/.codex/auth.json'
REMEOF

    echo
    echo "[postm0] restarting OpenClaw gateway so it loads the M0-installed Quaid extension..."
    "$SCRIPT_DIR/livetest-openclaw-gateway-restart.sh" --restart --host "$REMOTE_HOST" --config "$CONFIG_PATH"
fi

echo
echo "[postm0] verification — resolved layered config per instance:"
ssh "$REMOTE_HOST" "python3 <<'PYEOF'
import json
import pathlib
import sys

home = pathlib.Path.home()
quaid_home = home / '.quaid'
global_path = quaid_home / 'shared' / 'config' / 'global' / 'config.json'
platform_root = quaid_home / 'shared' / 'config'
instances_root = quaid_home / 'instances'

def load(path):
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise SystemExit(f'failed reading {path}: {exc}') from exc
    return data if isinstance(data, dict) else {}

def merge(base, over):
    out = json.loads(json.dumps(base))
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out

def value(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None

def infer_platform(instance_name, instance_cfg):
    adapter = value(instance_cfg.get('adapter'), 'type')
    if adapter:
        return str(adapter)
    if instance_name.startswith('claude-code'):
        return 'claude-code'
    if instance_name.startswith('openclaw'):
        return 'openclaw'
    if instance_name.startswith('codex'):
        return 'codex'
    return ''

failures = []
instance_dirs = sorted(p for p in instances_root.glob('*') if p.is_dir())
if not instance_dirs:
    failures.append(f'no instances found under {instances_root}')

for inst_dir in instance_dirs:
    inst_cfg_path = inst_dir / 'config.json'
    inst_cfg = load(inst_cfg_path)
    platform = infer_platform(inst_dir.name, inst_cfg)
    merged = load(global_path)
    if platform:
        merged = merge(merged, load(platform_root / platform / 'config.json'))
    merged = merge(merged, inst_cfg)

    capture = merged.get('capture', {})
    livetest = merged.get('livetest', {})
    models = merged.get('models', {})
    chunk_tokens = value(capture, 'chunk_tokens', 'chunkTokens')
    buffer_log = value(livetest, 'enableExtractionBufferLog', 'enable_extraction_buffer_log')
    provider = value(models, 'llmProvider', 'llm_provider')
    fast = value(models, 'fastReasoning', 'fast_reasoning')
    deep = value(models, 'deepReasoning', 'deep_reasoning')

    print(
        f'  {inst_dir.name}: platform={platform or \"?\"} '
        f'capture.chunk_tokens={chunk_tokens} '
        f'livetest.enableExtractionBufferLog={buffer_log} '
        f'models.llmProvider={provider} '
        f'models.fastReasoning={fast} '
        f'models.deepReasoning={deep}'
    )

    if chunk_tokens != 1500:
        failures.append(f'{inst_dir.name}: expected capture.chunk_tokens=1500, got {chunk_tokens!r}')
    if buffer_log is not True:
        failures.append(f'{inst_dir.name}: expected livetest.enableExtractionBufferLog=true, got {buffer_log!r}')
    if platform == 'claude-code':
        if fast != 'claude-haiku-4-5-20251001':
            failures.append(f'{inst_dir.name}: expected CC fastReasoning=claude-haiku-4-5-20251001, got {fast!r}')
        if deep != 'claude-sonnet-4-6':
            failures.append(f'{inst_dir.name}: expected CC deepReasoning=claude-sonnet-4-6, got {deep!r}')
    elif platform == 'openclaw':
        if fast != 'claude-haiku-4-5':
            failures.append(f'{inst_dir.name}: expected OC fastReasoning=claude-haiku-4-5, got {fast!r}')
        if deep != 'claude-sonnet-4-6':
            failures.append(f'{inst_dir.name}: expected OC deepReasoning=claude-sonnet-4-6, got {deep!r}')
    elif platform == 'codex':
        if provider != 'openai':
            failures.append(f'{inst_dir.name}: expected CDX llmProvider=openai, got {provider!r}')
        if deep != 'gpt-5.4':
            failures.append(f'{inst_dir.name}: expected CDX deepReasoning=gpt-5.4, got {deep!r}')

if failures:
    print('postm0 config verification failed:', file=sys.stderr)
    for failure in failures:
        print(f'  - {failure}', file=sys.stderr)
    raise SystemExit(1)
PYEOF
"

echo
echo "[postm0] done. Remember: per-instance config can still override platform;"
echo "         check this if a later step appears to ignore the override."
