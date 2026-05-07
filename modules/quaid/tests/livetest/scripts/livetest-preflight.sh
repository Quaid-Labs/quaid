#!/usr/bin/env bash
# livetest-preflight.sh — Pre-run safety checks, wipe, and platform prep
#
# Run this before every live test run. It:
#   1. Verifies the remote host is not the same machine as the local host
#   2. Verifies SSH connectivity to the remote
#   3. Verifies remote Homebrew Python 3.10+ is present
#   4. Upgrades platform CLIs on the remote
#   5. Wipes Quaid from the remote (full wipe by default)
#   6. Syncs the latest dev tree to the remote
#   7. Seeds shared auth and OC Matrix channel config on the remote
#   8. Starts platform services on the remote
#
# The remote host will have Quaid wiped and reinstalled repeatedly during a run.
# It may be running broken or unstable code at any point. It must be a dedicated
# machine separate from the one running the coordinator and tester agents.
#
# Usage:
#   livetest-preflight.sh [options]
#   livetest-preflight.sh                         # full preflight (dev tree)
#   livetest-preflight.sh --wipe-platform cc      # CC-only wipe (OC is live)
#   livetest-preflight.sh --skip-wipe             # skip wipe, just check + start services
#   livetest-preflight.sh --dry-run               # print commands without executing
#   livetest-preflight.sh --config path/to/livetest-config.json
#   livetest-preflight.sh --with-platform-upgrades # slow presnapshot-only CLI upgrade path
#   livetest-preflight.sh --platform-upgrades-only # safety checks + CLI/OAuth maintenance, exit 20 if changed
#   livetest-preflight.sh --release-verify v0.3.1  # release verification mode
#
# Options:
#   --wipe-platform <all|oc|cc|cdx>  Wipe scope (default: all)
#   --skip-wipe                      Skip the wipe step
#   --skip-platform-start            Skip starting platform services
#   --skip-platform-version-check    Skip non-blocking platform drift warning
#   --with-platform-upgrades         Apply platform CLI upgrades during preflight.
#                                    Intended for presnapshot maintenance, not per-run loops.
#   --platform-upgrades-only         Run safety checks + platform CLI/OAuth maintenance, then exit.
#                                    Returns 20 when upgrades changed the run VM.
#   --dry-run                        Print commands without executing them
#   --config <path>                  Path to livetest-config.json (default: auto-detected)
#   --release-verify <tag>           Release verification mode: step 6 clones the tagged
#                                    GitHub release instead of rsyncing the dev tree.
#                                    Use for post-release validation only. Default mode
#                                    always pulls from dev.
#   -h, --help                       Show this help
#
# Environment:
#   LIVETEST_CC_OAUTH_MIN_TTL_SECONDS  Minimum remaining lifetime required for
#                                      coordinator Claude OAuth before copying
#                                      it to the VM. Default: 5400 (90 min).
#   LIVETEST_CODEX_OAUTH_MIN_TTL_SECONDS  Minimum remaining access-token
#                                      lifetime required for coordinator Codex
#                                      OAuth before copying it to the VM and
#                                      seeding OpenClaw access-only auth.
#                                      Default: 5400 (90 min).
#
# Exit codes:
#   0  All checks passed and prep complete
#   1  Error (safety check failed, SSH unreachable, wipe failed, etc.)
#
# Set LIVETEST_WIPE_YES=1 to skip the wipe confirmation prompt.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DEFAULT="$(dirname "$SCRIPT_DIR")/livetest-config.json"

# --- Defaults ---
WIPE_PLATFORM="all"
SKIP_WIPE=0
SKIP_PLATFORM_START=0
SKIP_PLATFORM_VERSION_CHECK=0
RUN_PLATFORM_UPGRADES=0
PLATFORM_UPGRADES_ONLY=0
DRY_RUN=0
CONFIG_PATH="$CONFIG_DEFAULT"
RELEASE_VERIFY=""   # empty = dev mode (default); set to a tag like v0.3.1 for release verification
CC_OAUTH_MIN_TTL_SECONDS="${LIVETEST_CC_OAUTH_MIN_TTL_SECONDS:-5400}"
CODEX_OAUTH_MIN_TTL_SECONDS="${LIVETEST_CODEX_OAUTH_MIN_TTL_SECONDS:-5400}"

# --- Parse args ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wipe-platform)        WIPE_PLATFORM="$2"; shift 2 ;;
        --skip-wipe)            SKIP_WIPE=1; shift ;;
        --skip-platform-start)  SKIP_PLATFORM_START=1; shift ;;
        --skip-platform-version-check) SKIP_PLATFORM_VERSION_CHECK=1; shift ;;
        --with-platform-upgrades) RUN_PLATFORM_UPGRADES=1; shift ;;
        --platform-upgrades-only) RUN_PLATFORM_UPGRADES=1; PLATFORM_UPGRADES_ONLY=1; shift ;;
        --dry-run)              DRY_RUN=1; shift ;;
        --config)               CONFIG_PATH="$2"; shift 2 ;;
        --release-verify)       RELEASE_VERIFY="$2"; shift 2 ;;
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
if [[ ! "$CC_OAUTH_MIN_TTL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "Error: LIVETEST_CC_OAUTH_MIN_TTL_SECONDS must be an integer number of seconds" >&2
    exit 1
fi
if [[ ! "$CODEX_OAUTH_MIN_TTL_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "Error: LIVETEST_CODEX_OAUTH_MIN_TTL_SECONDS must be an integer number of seconds" >&2
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

if [[ -z "$REMOTE_HOST" ]]; then
    echo "Error: remote.host must be set in $CONFIG_PATH" >&2
    exit 1
fi

PASS="PASS"
FAIL="FAIL"

validate_claude_oauth_credentials() {
    local creds_path="$1"
    local label="$2"
    local min_ttl_seconds="${3:-0}"
    python3 - "$creds_path" "$label" "$min_ttl_seconds" <<'PYEOF'
import datetime
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1]).expanduser()
label = sys.argv[2]
try:
    min_ttl_seconds = int(float(sys.argv[3] or 0))
except Exception:
    raise SystemExit(f"{label}: invalid minimum TTL seconds {sys.argv[3]!r}")
if not path.exists():
    raise SystemExit(f"{label}: missing {path}")

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"{label}: invalid JSON in {path}: {exc}") from exc

oauth = payload.get("claudeAiOauth")
if not isinstance(oauth, dict):
    raise SystemExit(f"{label}: claudeAiOauth block missing in {path}")

access_token = str(oauth.get("accessToken") or "").strip()
refresh_token = str(oauth.get("refreshToken") or "").strip()
raw_expires = oauth.get("expiresAt")
if not access_token:
    raise SystemExit(f"{label}: accessToken missing in {path}")
if not refresh_token:
    raise SystemExit(f"{label}: refreshToken missing in {path}")
if raw_expires in (None, ""):
    raise SystemExit(f"{label}: expiresAt missing in {path}")

try:
    if isinstance(raw_expires, (int, float)):
        expires_at = datetime.datetime.fromtimestamp(float(raw_expires) / 1000.0, tz=datetime.timezone.utc)
    else:
        raw_text = str(raw_expires).strip()
        if raw_text.isdigit():
            expires_at = datetime.datetime.fromtimestamp(int(raw_text) / 1000.0, tz=datetime.timezone.utc)
        else:
            expires_at = datetime.datetime.fromisoformat(raw_text.replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
except Exception as exc:
    raise SystemExit(f"{label}: cannot parse expiresAt={raw_expires!r} in {path}: {exc}") from exc

now = datetime.datetime.now(datetime.timezone.utc)
if expires_at <= now:
    raise SystemExit(f"{label}: expired at {expires_at.isoformat()} ({path})")
remaining = (expires_at - now).total_seconds()
if min_ttl_seconds > 0 and remaining < min_ttl_seconds:
    raise SystemExit(
        f"{label}: expires too soon at {expires_at.isoformat()} "
        f"(remaining {int(remaining // 60)}m, required {int(min_ttl_seconds // 60)}m; {path})"
    )

print(f"{label}: valid until {expires_at.isoformat()} (TTL {remaining / 3600.0:.1f}h, remaining {int(remaining // 60)}m)")
PYEOF
}

sha256_file() {
    python3 - "$1" <<'PYEOF'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1]).expanduser()
print(hashlib.sha256(path.read_bytes()).hexdigest())
PYEOF
}

validate_codex_oauth_credentials() {
    local auth_path="$1"
    local label="$2"
    local min_ttl_seconds="${3:-0}"
    python3 - "$auth_path" "$label" "$min_ttl_seconds" <<'PYEOF'
import base64
import datetime
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1]).expanduser()
label = sys.argv[2]
try:
    min_ttl_seconds = int(float(sys.argv[3] or 0))
except Exception:
    raise SystemExit(f"{label}: invalid minimum TTL seconds {sys.argv[3]!r}")
if not path.exists():
    raise SystemExit(f"{label}: missing {path}")

try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"{label}: invalid JSON in {path}: {exc}") from exc

tokens = payload.get("tokens")
if not isinstance(tokens, dict):
    raise SystemExit(f"{label}: tokens block missing in {path}")

access_token = str(tokens.get("access_token") or "").strip()
refresh_token = str(tokens.get("refresh_token") or "").strip()
if not access_token:
    raise SystemExit(f"{label}: access_token missing in {path}")
if not refresh_token:
    raise SystemExit(f"{label}: refresh_token missing in {path}")

parts = access_token.split(".")
if len(parts) < 2:
    raise SystemExit(f"{label}: access_token is not a JWT in {path}")
try:
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
except Exception as exc:
    raise SystemExit(f"{label}: cannot decode access_token JWT in {path}: {exc}") from exc

raw_exp = claims.get("exp")
if not isinstance(raw_exp, (int, float)):
    raise SystemExit(f"{label}: access_token exp missing in {path}")
expires_at = datetime.datetime.fromtimestamp(float(raw_exp), tz=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
if expires_at <= now:
    raise SystemExit(f"{label}: access_token expired at {expires_at.isoformat()} ({path})")
remaining = (expires_at - now).total_seconds()
if min_ttl_seconds > 0 and remaining < min_ttl_seconds:
    raise SystemExit(
        f"{label}: access_token expires too soon at {expires_at.isoformat()} "
        f"(remaining {int(remaining // 60)}m, required {int(min_ttl_seconds // 60)}m; {path})"
    )

print(f"{label}: access token valid until {expires_at.isoformat()} (TTL {remaining / 3600.0:.1f}h, remaining {int(remaining // 60)}m)")
PYEOF
}

validate_remote_claude_oauth_credentials() {
    local expected_sha="${1:-}"
    ssh "$REMOTE_HOST" python3 - "$CC_OAUTH_MIN_TTL_SECONDS" "$expected_sha" <<'PYEOF'
import datetime
import hashlib
import json
import pathlib
import sys

path = pathlib.Path.home() / ".claude" / ".credentials.json"
label = "remote CC OAuth"
try:
    min_ttl_seconds = int(float(sys.argv[1] or 0))
except Exception:
    raise SystemExit(f"{label}: invalid minimum TTL seconds {sys.argv[1]!r}")
expected_sha = str(sys.argv[2] or "").strip().lower()
if not path.exists():
    raise SystemExit(f"{label}: missing {path}")

raw = path.read_bytes()
actual_sha = hashlib.sha256(raw).hexdigest()
if expected_sha and actual_sha != expected_sha:
    raise SystemExit(
        f"{label}: remote credential hash mismatch at {path} "
        f"(remote {actual_sha[:12]}, local {expected_sha[:12]})"
    )

try:
    payload = json.loads(raw.decode("utf-8"))
except Exception as exc:
    raise SystemExit(f"{label}: invalid JSON in {path}: {exc}") from exc

oauth = payload.get("claudeAiOauth")
if not isinstance(oauth, dict):
    raise SystemExit(f"{label}: claudeAiOauth block missing in {path}")

access_token = str(oauth.get("accessToken") or "").strip()
refresh_token = str(oauth.get("refreshToken") or "").strip()
raw_expires = oauth.get("expiresAt")
if not access_token:
    raise SystemExit(f"{label}: accessToken missing in {path}")
if not refresh_token:
    raise SystemExit(f"{label}: refreshToken missing in {path}")
if raw_expires in (None, ""):
    raise SystemExit(f"{label}: expiresAt missing in {path}")

try:
    if isinstance(raw_expires, (int, float)):
        expires_at = datetime.datetime.fromtimestamp(float(raw_expires) / 1000.0, tz=datetime.timezone.utc)
    else:
        raw_text = str(raw_expires).strip()
        if raw_text.isdigit():
            expires_at = datetime.datetime.fromtimestamp(int(raw_text) / 1000.0, tz=datetime.timezone.utc)
        else:
            expires_at = datetime.datetime.fromisoformat(raw_text.replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
except Exception as exc:
    raise SystemExit(f"{label}: cannot parse expiresAt={raw_expires!r} in {path}: {exc}") from exc

now = datetime.datetime.now(datetime.timezone.utc)
if expires_at <= now:
    raise SystemExit(f"{label}: expired at {expires_at.isoformat()} ({path})")
remaining = (expires_at - now).total_seconds()
if min_ttl_seconds > 0 and remaining < min_ttl_seconds:
    raise SystemExit(
        f"{label}: expires too soon at {expires_at.isoformat()} "
        f"(remaining {int(remaining // 60)}m, required {int(min_ttl_seconds // 60)}m; {path})"
    )

print(
    f"{label}: valid until {expires_at.isoformat()} "
    f"(TTL {remaining / 3600.0:.1f}h, remaining {int(remaining // 60)}m, sha {actual_sha[:12]})"
)
PYEOF
}

validate_remote_codex_oauth_credentials() {
    local expected_sha="${1:-}"
    ssh "$REMOTE_HOST" python3 - "$CODEX_OAUTH_MIN_TTL_SECONDS" "$expected_sha" <<'PYEOF'
import base64
import datetime
import hashlib
import json
import pathlib
import sys

path = pathlib.Path.home() / ".codex" / "auth.json"
label = "remote Codex OAuth"
try:
    min_ttl_seconds = int(float(sys.argv[1] or 0))
except Exception:
    raise SystemExit(f"{label}: invalid minimum TTL seconds {sys.argv[1]!r}")
expected_sha = str(sys.argv[2] or "").strip().lower()
if not path.exists():
    raise SystemExit(f"{label}: missing {path}")

raw = path.read_bytes()
actual_sha = hashlib.sha256(raw).hexdigest()
if expected_sha and actual_sha != expected_sha:
    raise SystemExit(
        f"{label}: remote auth hash mismatch at {path} "
        f"(remote {actual_sha[:12]}, local {expected_sha[:12]})"
    )

try:
    payload = json.loads(raw.decode("utf-8"))
except Exception as exc:
    raise SystemExit(f"{label}: invalid JSON in {path}: {exc}") from exc

tokens = payload.get("tokens")
if not isinstance(tokens, dict):
    raise SystemExit(f"{label}: tokens block missing in {path}")

access_token = str(tokens.get("access_token") or "").strip()
refresh_token = str(tokens.get("refresh_token") or "").strip()
if not access_token:
    raise SystemExit(f"{label}: access_token missing in {path}")
if not refresh_token:
    raise SystemExit(f"{label}: refresh_token missing in {path}")

parts = access_token.split(".")
if len(parts) < 2:
    raise SystemExit(f"{label}: access_token is not a JWT in {path}")
try:
    body = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
except Exception as exc:
    raise SystemExit(f"{label}: cannot decode access_token JWT in {path}: {exc}") from exc

raw_exp = claims.get("exp")
if not isinstance(raw_exp, (int, float)):
    raise SystemExit(f"{label}: access_token exp missing in {path}")
expires_at = datetime.datetime.fromtimestamp(float(raw_exp), tz=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
if expires_at <= now:
    raise SystemExit(f"{label}: access_token expired at {expires_at.isoformat()} ({path})")
remaining = (expires_at - now).total_seconds()
if min_ttl_seconds > 0 and remaining < min_ttl_seconds:
    raise SystemExit(
        f"{label}: access_token expires too soon at {expires_at.isoformat()} "
        f"(remaining {int(remaining // 60)}m, required {int(min_ttl_seconds // 60)}m; {path})"
    )

print(
    f"{label}: access token valid until {expires_at.isoformat()} "
    f"(TTL {remaining / 3600.0:.1f}h, remaining {int(remaining // 60)}m, sha {actual_sha[:12]})"
)
PYEOF
}

abort_if_errors() {
    local context="${1:-Preflight}"
    if [[ "$ERRORS" -gt 0 ]]; then
        echo ""
        echo "$context aborted: $ERRORS check(s) failed." >&2
        exit 1
    fi
}

CC_OAUTH_REMOTE_REFRESHED=0
CC_OAUTH_EXPECTED_SHA=""
CODEX_OAUTH_REMOTE_REFRESHED=0
CODEX_OAUTH_EXPECTED_SHA=""

copy_claude_oauth_credentials_to_remote() {
    local heading="${1:-[7/8] Copying CC OAuth credentials to remote...}"
    local cc_enabled local_creds local_status local_sha remote_status remote_tmp ttl_summary

    echo ""
    echo "$heading"
    CC_OAUTH_REMOTE_REFRESHED=0
    CC_OAUTH_EXPECTED_SHA=""

    cc_enabled="$(read_config platforms.cc.enabled)"
    if [[ "$cc_enabled" != "True" && "$cc_enabled" != "true" ]]; then
        echo "  (skipped — CC platform not enabled in config)"
        return 0
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would validate coordinator Claude OAuth has >=$((CC_OAUTH_MIN_TTL_SECONDS / 60))m remaining"
        echo "  [dry-run] would scp ~/.claude/.credentials.json to $REMOTE_HOST:~/.claude/.credentials.json"
        return 0
    fi

    local_creds="$HOME/.claude/.credentials.json"
    if [[ ! -f "$local_creds" ]]; then
        echo "  $FAIL  $local_creds not found — CC cannot create real sessions without coordinator Claude auth"
        ERRORS=$((ERRORS + 1))
        return 0
    fi

    local_status="$(validate_claude_oauth_credentials "$local_creds" "local CC OAuth" "$CC_OAUTH_MIN_TTL_SECONDS" 2>&1 || true)"
    if [[ "$local_status" != local\ CC\ OAuth:\ valid\ until* ]]; then
        echo "  $FAIL  $local_status"
        echo "         Refresh coordinator Claude auth before preflight, then rerun."
        echo "         Override only for emergency short runs: LIVETEST_CC_OAUTH_MIN_TTL_SECONDS=0"
        ERRORS=$((ERRORS + 1))
        return 0
    fi

    echo "  $PASS  $local_status"
    local_sha="$(sha256_file "$local_creds")"
    CC_OAUTH_EXPECTED_SHA="$local_sha"
    remote_tmp="$(ssh "$REMOTE_HOST" 'mkdir -p "$HOME/.claude" && umask 077 && mktemp "$HOME/.claude/.credentials.json.tmp.XXXXXX"')"
    scp "$local_creds" "$REMOTE_HOST:$remote_tmp"
    ssh "$REMOTE_HOST" python3 - "$remote_tmp" <<'PYEOF'
import os
import pathlib
import sys

tmp = pathlib.Path(sys.argv[1]).expanduser()
dest = pathlib.Path.home() / ".claude" / ".credentials.json"
dest.parent.mkdir(parents=True, exist_ok=True)
os.replace(tmp, dest)
dest.chmod(0o600)
try:
    dir_fd = os.open(str(dest.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
except Exception:
    pass
PYEOF
    if ! remote_status="$(validate_remote_claude_oauth_credentials "$local_sha" 2>&1)"; then
        echo "  $FAIL  $remote_status"
        ERRORS=$((ERRORS + 1))
    elif [[ "$remote_status" != remote\ CC\ OAuth:\ valid\ until* ]]; then
        echo "  $FAIL  $remote_status"
        ERRORS=$((ERRORS + 1))
    else
        echo "  $PASS  $remote_status"
        ttl_summary="$(printf '%s\n' "$remote_status" | sed -n 's/.*TTL \([^,)]*\).*/\1/p')"
        [[ -z "$ttl_summary" ]] && ttl_summary="unknown"
        echo "  $PASS  refreshed VM Claude OAuth, $ttl_summary TTL (~/.claude/.credentials.json)"
        CC_OAUTH_REMOTE_REFRESHED=1
    fi

    # Ensure any stale ANTHROPIC_API_KEY is removed from settings.json (it overrides .credentials.json).
    ssh "$REMOTE_HOST" python3 << 'PYEOF'
import json, pathlib
p = pathlib.Path.home() / '.claude' / 'settings.json'
if p.exists():
    d = json.loads(p.read_text())
    env = d.get('env', {})
    if 'ANTHROPIC_API_KEY' in env:
        del env['ANTHROPIC_API_KEY']
        d['env'] = env
        p.write_text(json.dumps(d, indent=2))
        print('  removed stale ANTHROPIC_API_KEY from settings.json')
PYEOF
}

copy_codex_oauth_credentials_to_remote() {
    local heading="${1:-[7b/8] Copying Codex OAuth credentials to remote...}"
    local cdx_enabled oc_enabled local_auth local_status local_sha remote_status remote_tmp ttl_summary

    echo ""
    echo "$heading"
    CODEX_OAUTH_REMOTE_REFRESHED=0
    CODEX_OAUTH_EXPECTED_SHA=""

    cdx_enabled="$(read_config platforms.cdx.enabled)"
    oc_enabled="$(read_config platforms.oc.enabled)"
    if [[ "$cdx_enabled" != "True" && "$cdx_enabled" != "true" && "$oc_enabled" != "True" && "$oc_enabled" != "true" ]]; then
        echo "  (skipped — neither CDX nor OC platform is enabled in config)"
        return 0
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would validate coordinator Codex OAuth has >=$((CODEX_OAUTH_MIN_TTL_SECONDS / 60))m access-token TTL remaining"
        echo "  [dry-run] would scp ~/.codex/auth.json to $REMOTE_HOST:~/.codex/auth.json"
        return 0
    fi

    local_auth="$HOME/.codex/auth.json"
    if [[ ! -f "$local_auth" ]]; then
        echo "  $FAIL  $local_auth not found — CDX and OC openai-codex cannot share fresh OAuth state"
        ERRORS=$((ERRORS + 1))
        return 0
    fi

    local_status="$(validate_codex_oauth_credentials "$local_auth" "local Codex OAuth" "$CODEX_OAUTH_MIN_TTL_SECONDS" 2>&1 || true)"
    if [[ "$local_status" != local\ Codex\ OAuth:\ access\ token\ valid\ until* ]]; then
        echo "  $FAIL  $local_status"
        echo "         Refresh coordinator Codex auth before preflight, then rerun."
        echo "         Override only for emergency short runs: LIVETEST_CODEX_OAUTH_MIN_TTL_SECONDS=0"
        ERRORS=$((ERRORS + 1))
        return 0
    fi

    echo "  $PASS  $local_status"
    local_sha="$(sha256_file "$local_auth")"
    CODEX_OAUTH_EXPECTED_SHA="$local_sha"
    remote_tmp="$(ssh "$REMOTE_HOST" 'mkdir -p "$HOME/.codex" && umask 077 && mktemp "$HOME/.codex/auth.json.tmp.XXXXXX"')"
    scp "$local_auth" "$REMOTE_HOST:$remote_tmp"
    ssh "$REMOTE_HOST" python3 - "$remote_tmp" <<'PYEOF'
import os
import pathlib
import sys

tmp = pathlib.Path(sys.argv[1]).expanduser()
dest = pathlib.Path.home() / ".codex" / "auth.json"
dest.parent.mkdir(parents=True, exist_ok=True)
os.replace(tmp, dest)
dest.chmod(0o600)
try:
    dir_fd = os.open(str(dest.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
except Exception:
    pass
PYEOF
    if ! remote_status="$(validate_remote_codex_oauth_credentials "$local_sha" 2>&1)"; then
        echo "  $FAIL  $remote_status"
        ERRORS=$((ERRORS + 1))
    elif [[ "$remote_status" != remote\ Codex\ OAuth:\ access\ token\ valid\ until* ]]; then
        echo "  $FAIL  $remote_status"
        ERRORS=$((ERRORS + 1))
    else
        echo "  $PASS  $remote_status"
        ttl_summary="$(printf '%s\n' "$remote_status" | sed -n 's/.*TTL \([^,)]*\).*/\1/p')"
        [[ -z "$ttl_summary" ]] && ttl_summary="unknown"
        echo "  $PASS  refreshed VM Codex OAuth, $ttl_summary access-token TTL (~/.codex/auth.json)"
        CODEX_OAUTH_REMOTE_REFRESHED=1
    fi
}

verify_claude_oauth_seed_persisted() {
    local heading="${1:-Verifying CC OAuth seed persisted...}"
    local cc_enabled local_creds local_sha remote_status

    echo ""
    echo "$heading"

    cc_enabled="$(read_config platforms.cc.enabled)"
    if [[ "$cc_enabled" != "True" && "$cc_enabled" != "true" ]]; then
        echo "  (skipped — CC platform not enabled in config)"
        return 0
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would verify remote ~/.claude/.credentials.json still matches coordinator credentials"
        return 0
    fi

    local_sha="$CC_OAUTH_EXPECTED_SHA"
    if [[ -z "$local_sha" ]]; then
        local_creds="$HOME/.claude/.credentials.json"
        if [[ ! -f "$local_creds" ]]; then
            echo "  $FAIL  $local_creds not found — cannot verify remote CC OAuth seed"
            ERRORS=$((ERRORS + 1))
            return 0
        fi
        local_sha="$(sha256_file "$local_creds")"
    fi
    if ! remote_status="$(validate_remote_claude_oauth_credentials "$local_sha" 2>&1)"; then
        echo "  $FAIL  $remote_status"
        ERRORS=$((ERRORS + 1))
    else
        echo "  $PASS  $remote_status"
    fi
}

verify_codex_oauth_seed_persisted() {
    local heading="${1:-Verifying Codex OAuth seed persisted...}"
    local cdx_enabled oc_enabled local_auth local_sha remote_status

    echo ""
    echo "$heading"

    cdx_enabled="$(read_config platforms.cdx.enabled)"
    oc_enabled="$(read_config platforms.oc.enabled)"
    if [[ "$cdx_enabled" != "True" && "$cdx_enabled" != "true" && "$oc_enabled" != "True" && "$oc_enabled" != "true" ]]; then
        echo "  (skipped — neither CDX nor OC platform is enabled in config)"
        return 0
    fi
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would verify remote ~/.codex/auth.json still matches coordinator credentials"
        return 0
    fi

    local_sha="$CODEX_OAUTH_EXPECTED_SHA"
    if [[ -z "$local_sha" ]]; then
        local_auth="$HOME/.codex/auth.json"
        if [[ ! -f "$local_auth" ]]; then
            echo "  $FAIL  $local_auth not found — cannot verify remote Codex OAuth seed"
            ERRORS=$((ERRORS + 1))
            return 0
        fi
        local_sha="$(sha256_file "$local_auth")"
    fi
    if ! remote_status="$(validate_remote_codex_oauth_credentials "$local_sha" 2>&1)"; then
        echo "  $FAIL  $remote_status"
        ERRORS=$((ERRORS + 1))
    else
        echo "  $PASS  $remote_status"
    fi
}

echo "========================================"
echo " livetest-preflight"
echo " Remote host : $REMOTE_HOST"
echo " Wipe scope  : $WIPE_PLATFORM"
if [[ -n "$RELEASE_VERIFY" ]]; then
    echo " Mode        : RELEASE VERIFICATION ($RELEASE_VERIFY)"
else
    echo " Mode        : dev (default)"
fi
[[ "$DRY_RUN" == "1" ]] && echo " Dry-run     : YES"
echo "========================================"
echo ""

ERRORS=0

# --- Check 1: Remote ≠ local ---
echo "[1/8] Verifying remote is not this machine..."

LOCAL_HOSTNAME="$(hostname -s 2>/dev/null || hostname)"
LOCAL_IP="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "")"

# Resolve the remote host to an IP
REMOTE_IP=""
if [[ "$DRY_RUN" == "0" ]]; then
    # Strip user@ prefix if present (e.g. admin@192.168.64.5 -> 192.168.64.5)
    REMOTE_HOST_ADDR="${REMOTE_HOST##*@}"
    REMOTE_IP="$(python3 -c "import socket; print(socket.gethostbyname('$REMOTE_HOST_ADDR'))" 2>/dev/null || true)"
fi

SAFE=1

# Check: hostname match
if [[ "$REMOTE_HOST" == "localhost" || "$REMOTE_HOST" == "127.0.0.1" || "$REMOTE_HOST" == "::1" ]]; then
    echo "  $FAIL  remote.host is localhost — the remote must be a separate machine"
    SAFE=0
fi

# Check: IP match
if [[ -n "$REMOTE_IP" && -n "$LOCAL_IP" && "$REMOTE_IP" == "$LOCAL_IP" ]]; then
    echo "  $FAIL  remote IP ($REMOTE_IP) matches local IP ($LOCAL_IP) — this would wipe your own machine"
    SAFE=0
fi

# Check: hostname match (covers shortname vs fqdn aliases, etc.)
if [[ "$DRY_RUN" == "0" && "$SAFE" == "1" ]]; then
    REMOTE_HOSTNAME="$(ssh "$REMOTE_HOST" 'hostname -s 2>/dev/null || hostname' 2>/dev/null || echo "")"
    if [[ -n "$REMOTE_HOSTNAME" && "$REMOTE_HOSTNAME" == "$LOCAL_HOSTNAME" ]]; then
        echo "  $FAIL  remote hostname ($REMOTE_HOSTNAME) matches local hostname ($LOCAL_HOSTNAME)"
        echo "         The remote must be a different machine from the coordinator."
        SAFE=0
    fi
fi

if [[ "$SAFE" == "1" ]]; then
    if [[ "$DRY_RUN" == "0" ]]; then
        echo "  $PASS  remote ($REMOTE_HOST / ${REMOTE_IP:-unknown ip}) ≠ local ($LOCAL_HOSTNAME / ${LOCAL_IP:-unknown ip})"
    else
        echo "  [dry-run] would verify remote ≠ local"
    fi
else
    ERRORS=$((ERRORS + 1))
fi

# --- Check 2: SSH connectivity ---
echo ""
echo "[2/8] Verifying SSH connectivity to $REMOTE_HOST..."

if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] would ssh $REMOTE_HOST 'echo ok'"
else
    SSH_RESULT="$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$REMOTE_HOST" 'echo ok' 2>&1 || true)"
    if [[ "$SSH_RESULT" == "ok" ]]; then
        echo "  $PASS  SSH connected to $REMOTE_HOST"
    else
        echo "  $FAIL  SSH to $REMOTE_HOST failed: $SSH_RESULT"
        echo "         Check key-based auth and that the host is reachable."
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- Check 3: Homebrew Python 3.10+ on remote ---
echo ""
echo "[3/8] Verifying remote Homebrew Python 3.10+..."

if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] would verify /opt/homebrew/bin/python3 >= 3.10 on $REMOTE_HOST"
else
    PY_CHECK_RESULT="$(ssh "$REMOTE_HOST" '
if [ ! -x /opt/homebrew/bin/python3 ]; then
  echo MISSING
  exit 0
fi
/opt/homebrew/bin/python3 - <<'"'"'"'"'"'"'"'"'PY'"'"'"'"'"'"'"'"'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
' 2>&1 || true)"
    PY_CHECK_LAST_LINE="$(printf '%s\n' "$PY_CHECK_RESULT" | tail -n 1 | tr -d '\r')"
    if [[ "$PY_CHECK_LAST_LINE" == "MISSING" ]]; then
        echo "  $FAIL  /opt/homebrew/bin/python3 not found on remote"
        echo "         Install Homebrew Python 3.12+ on the VM base image and retry."
        ERRORS=$((ERRORS + 1))
    elif [[ "$PY_CHECK_LAST_LINE" =~ ^[0-9]+\.[0-9]+$ ]]; then
        PY_MAJOR="${PY_CHECK_LAST_LINE%%.*}"
        PY_MINOR="${PY_CHECK_LAST_LINE##*.}"
        if (( PY_MAJOR > 3 || (PY_MAJOR == 3 && PY_MINOR >= 10) )); then
            echo "  $PASS  remote Homebrew Python $PY_CHECK_LAST_LINE"
        else
            echo "  $FAIL  /opt/homebrew/bin/python3 is too old ($PY_CHECK_LAST_LINE)"
            echo "         Install Homebrew Python 3.12+ on the VM base image and retry."
            ERRORS=$((ERRORS + 1))
        fi
    else
        echo "  $FAIL  could not verify /opt/homebrew/bin/python3 on remote: $PY_CHECK_RESULT"
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- Abort early if safety checks failed (before any write operations) ---
echo "  checking for preflight errors (ERRORS=$ERRORS)..."
if [[ "$ERRORS" -gt 0 ]]; then
    echo ""
    echo "Preflight aborted: $ERRORS check(s) failed." >&2
    exit 1
fi

# --- Step 4: Platform CLI version handling ---
echo ""
echo "[4/8] Checking platform CLIs on remote..."

if [[ "$SKIP_PLATFORM_VERSION_CHECK" == "1" && "$RUN_PLATFORM_UPGRADES" != "1" ]]; then
    echo "  (skipped — --skip-platform-version-check)"
elif [[ "$DRY_RUN" == "1" ]]; then
    if [[ "$RUN_PLATFORM_UPGRADES" == "1" ]]; then
        echo "  [dry-run] would upgrade claude, codex, openclaw to latest on $REMOTE_HOST"
        echo "  [dry-run] would compare before/after versions"
        if [[ "$PLATFORM_UPGRADES_ONLY" == "1" ]]; then
            echo "  [dry-run] would refresh VM Claude OAuth credentials for base snapshot"
            echo "  [dry-run] would refresh VM Codex OAuth credentials for base snapshot"
            echo "  [dry-run] would exit after platform/OAuth maintenance check"
            exit 0
        fi
    else
        echo "  [dry-run] would check remote platform CLI versions and warn on drift"
    fi
else
    local_npm_latest_version() {
        local pkg="$1"
        local latest
        latest="$(PKG_NAME="$pkg" python3 - <<'PYEOF' 2>/dev/null | tail -n 1 | tr -d '\r' || true
import os
import subprocess

pkg = os.environ.get("PKG_NAME", "")
try:
    proc = subprocess.run(
        ["npm", "view", pkg, "version"],
        capture_output=True,
        text=True,
        timeout=15,
    )
except Exception:
    print("__UNKNOWN__")
    raise SystemExit(0)
if proc.returncode != 0:
    print("__UNKNOWN__")
else:
    print((proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else "__UNKNOWN__")
PYEOF
)"
        if [[ -z "$latest" ]]; then
            echo "__UNKNOWN__"
        else
            echo "$latest"
        fi
    }

    remote_pkg_version() {
        local pkg="$1"
        ssh "$REMOTE_HOST" "set -euo pipefail; export PATH=\"/opt/homebrew/bin:\$HOME/.local/bin:\$PATH\"; eval \"\$(/opt/homebrew/bin/brew shellenv 2>/dev/null)\" 2>/dev/null || true; PKG_NAME='$pkg' python3 - <<'PYEOF'
import json
import os
from pathlib import Path
import shutil

pkg = os.environ.get(\"PKG_NAME\", \"\")
binary_by_pkg = {
    \"@anthropic-ai/claude-code\": \"claude\",
    \"@openai/codex\": \"codex\",
    \"openclaw\": \"openclaw\",
}


def package_parts(name: str) -> list[str]:
    return [part for part in name.split(\"/\") if part]


def read_version(package_dir: Path) -> str:
    try:
        data = json.loads((package_dir / \"package.json\").read_text(encoding=\"utf-8\"))
    except Exception:
        return \"\"
    if str(data.get(\"name\") or \"\") != pkg:
        return \"\"
    return str(data.get(\"version\") or \"\").strip()


def candidate_dirs() -> list[Path]:
    parts = package_parts(pkg)
    candidates: list[Path] = []
    for root in (
        Path(\"/opt/homebrew/lib/node_modules\"),
        Path(\"/usr/local/lib/node_modules\"),
        Path.home() / \".npm-global\" / \"lib\" / \"node_modules\",
        Path.home() / \".nvm\" / \"versions\" / \"node\",
    ):
        if root.name == \"node\":
            try:
                for version_root in root.iterdir():
                    candidates.append(version_root / \"lib\" / \"node_modules\" / Path(*parts))
            except Exception:
                pass
        else:
            candidates.append(root / Path(*parts))

    binary = binary_by_pkg.get(pkg, \"\")
    binary_path = shutil.which(binary) if binary else \"\"
    if binary_path:
        real = Path(os.path.realpath(binary_path))
        for parent in [real.parent, *real.parents]:
            package_json = parent / \"package.json\"
            if package_json.is_file():
                candidates.append(parent)
            if parent.name == \"node_modules\":
                candidates.append(parent / Path(*parts))

    seen: set[str] = set()
    deduped: list[Path] = []
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


for package_dir in candidate_dirs():
    version = read_version(package_dir)
    if version:
        print(version)
        raise SystemExit(0)
print(\"__MISSING__\")
PYEOF" 2>/dev/null | tr -d '\r'
    }

    remote_openclaw_version() {
        local npm_ver
        npm_ver="$(remote_pkg_version "openclaw")"
        if [[ "$npm_ver" != "__MISSING__" && -n "$npm_ver" ]]; then
            echo "$npm_ver"
            return
        fi
        ssh "$REMOTE_HOST" "set -euo pipefail; export PATH=\"/opt/homebrew/bin:\$HOME/.local/bin:\$PATH\"; eval \"\$(/opt/homebrew/bin/brew shellenv 2>/dev/null)\" 2>/dev/null || true; if ! command -v openclaw >/dev/null 2>&1; then echo '__MISSING__'; exit 0; fi; python3 - <<'PYEOF'
import os, re, shutil
binary = shutil.which(\"openclaw\") or \"\"
if not binary:
    print(\"__MISSING__\")
    raise SystemExit(0)
real = os.path.realpath(binary)
match = re.search(r\"/Cellar/openclaw/([^/]+)/\", real)
if match:
    print(match.group(1))
else:
    print(\"__UNKNOWN__\")
PYEOF" 2>/dev/null | tr -d '\r'
    }

    BEFORE_CLAUDE="$(remote_pkg_version "@anthropic-ai/claude-code")"
    BEFORE_CODEX="$(remote_pkg_version "@openai/codex")"
    BEFORE_OPENCLAW="$(remote_openclaw_version)"
    LATEST_CLAUDE="$(local_npm_latest_version "@anthropic-ai/claude-code")"
    LATEST_CODEX="$(local_npm_latest_version "@openai/codex")"
    LATEST_OPENCLAW="$(local_npm_latest_version "openclaw")"

    if [[ "$RUN_PLATFORM_UPGRADES" != "1" ]]; then
        drift=0
        echo "  remote version -> npm latest:"
        echo "    claude   : $BEFORE_CLAUDE -> $LATEST_CLAUDE"
        echo "    codex    : $BEFORE_CODEX -> $LATEST_CODEX"
        echo "    openclaw : $BEFORE_OPENCLAW -> $LATEST_OPENCLAW"
        if [[ "$LATEST_CLAUDE" != "__UNKNOWN__" && "$BEFORE_CLAUDE" != "__MISSING__" && "$BEFORE_CLAUDE" != "$LATEST_CLAUDE" ]]; then
            drift=1
        fi
        if [[ "$LATEST_CODEX" != "__UNKNOWN__" && "$BEFORE_CODEX" != "__MISSING__" && "$BEFORE_CODEX" != "$LATEST_CODEX" ]]; then
            drift=1
        fi
        if [[ "$LATEST_OPENCLAW" != "__UNKNOWN__" && "$BEFORE_OPENCLAW" != "__MISSING__" && "$BEFORE_OPENCLAW" != "$LATEST_OPENCLAW" ]]; then
            drift=1
        fi
        if [[ "$drift" -eq 1 ]]; then
            echo "  WARN  platform CLI drift detected; per-run preflight will not upgrade it."
            echo "        Run $SCRIPT_DIR/livetest-presnapshot-preflight.sh --config $CONFIG_PATH"
            echo "        to update the base snapshot before the next overnight loop."
        else
            echo "  $PASS  no platform CLI drift detected"
        fi
    else

        upgrade_remote_npm_cli() {
            local label="$1"
            local package_name="$2"
            local before_version="$3"
            local latest_version="$4"
            local should_update=1

            if [[ "$before_version" != "__MISSING__" && "$latest_version" != "__UNKNOWN__" && "$before_version" == "$latest_version" ]]; then
                should_update=0
            fi

            if [[ "$should_update" -eq 0 ]]; then
                echo "  ${label} already at ${before_version} (latest), skipping update"
                return
            fi

            printf "  upgrading %-12s ... " "${label}"
            update_output=""
            local install_spec="${package_name}@latest"
            if [[ "$latest_version" != "__UNKNOWN__" && -n "$latest_version" ]]; then
                install_spec="${package_name}@${latest_version}"
            fi
            if update_output="$(ssh "$REMOTE_HOST" "set -euo pipefail; export PATH=\"/opt/homebrew/bin:\$HOME/.local/bin:\$PATH\"; eval \"\$(/opt/homebrew/bin/brew shellenv 2>/dev/null)\" 2>/dev/null || true; npm install -g '${install_spec}'" 2>&1)"; then
                local after_version
                after_version="$(remote_pkg_version "$package_name")"
                if [[ "$latest_version" != "__UNKNOWN__" && "$after_version" != "$latest_version" ]]; then
                    echo "WARN: upgrade verification failed (continuing)"
                    echo "    expected ${latest_version}, found ${after_version}"
                    printf '%s\n' "$update_output" | tail -3 | sed 's/^/    /'
                    return
                fi
                echo "done"
            else
                echo "WARN: upgrade failed (continuing)"
                printf '%s\n' "$update_output" | tail -3 | sed 's/^/    /'
            fi
        }

        upgrade_remote_npm_cli "claude" "@anthropic-ai/claude-code" "$BEFORE_CLAUDE" "$LATEST_CLAUDE"
        upgrade_remote_npm_cli "codex" "@openai/codex" "$BEFORE_CODEX" "$LATEST_CODEX"

    oc_output=""
    oc_rc=0
    should_update_openclaw=1
    if [[ "$BEFORE_OPENCLAW" == "__MISSING__" ]]; then
        should_update_openclaw=0
    elif [[ "$BEFORE_OPENCLAW" != "__UNKNOWN__" && "$LATEST_OPENCLAW" != "__UNKNOWN__" && "$BEFORE_OPENCLAW" == "$LATEST_OPENCLAW" ]]; then
        should_update_openclaw=0
    fi

    if [[ "$BEFORE_OPENCLAW" == "__MISSING__" ]]; then
        echo "  openclaw not found, skipping update"
    elif [[ "$should_update_openclaw" -eq 0 ]]; then
        echo "  openclaw already at ${BEFORE_OPENCLAW} (latest), skipping update"
    else
        printf "  upgrading %-12s ... " "openclaw"
        # Guard step-4 against implicit errexit behavior inside command substitutions.
        # Preflight must continue cleanly to diffing + classification even when OpenClaw
        # update times out or fails.
        set +e
        oc_output="$("$SCRIPT_DIR/openclaw-cli-safe.sh" \
            --timeout "${OPENCLAW_PREFLIGHT_UPDATE_TIMEOUT_S:-${OPENCLAW_CLI_TIMEOUT_S:-120}}" \
            --label "openclaw-preflight-update" \
            --on-timeout "ssh \"$REMOTE_HOST\" 'pkill -f openclaw-update >/dev/null 2>&1 || true; pkill -f openclaw-completion >/dev/null 2>&1 || true; pkill -f openclaw-agent >/dev/null 2>&1 || true; pkill -f openclaw-agents >/dev/null 2>&1 || true'" \
            -- ssh "$REMOTE_HOST" 'set -euo pipefail; export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"; eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)" 2>/dev/null || true; if ! command -v openclaw >/dev/null 2>&1; then echo "__OPENCLAW_MISSING__"; exit 0; fi; openclaw update --yes' 2>&1)"
        oc_rc=$?
        set -e

        if [[ "$oc_rc" -eq 0 ]]; then
            if [[ "$oc_output" == *"__OPENCLAW_MISSING__"* ]]; then
                echo "not found, skipping"
            else
                echo "done"
            fi
        else
            if [[ "$oc_rc" -eq 124 ]]; then
                echo "skipped/timeout (continuing)"
            elif [[ "$oc_output" == *"__OPENCLAW_MISSING__"* ]]; then
                echo "not found, skipping"
            else
                echo "WARN: upgrade failed (continuing)"
                printf '%s\n' "$oc_output" | tail -3 | sed 's/^/    /'
            fi
        fi
    fi

    AFTER_CLAUDE="$(remote_pkg_version "@anthropic-ai/claude-code")"
    AFTER_CODEX="$(remote_pkg_version "@openai/codex")"
    AFTER_OPENCLAW="$(remote_openclaw_version)"

    updates_applied=0
    [[ "$BEFORE_CLAUDE" != "$AFTER_CLAUDE" ]] && updates_applied=1
    [[ "$BEFORE_CODEX" != "$AFTER_CODEX" ]] && updates_applied=1
    if [[ "$BEFORE_OPENCLAW" != "$AFTER_OPENCLAW" ]]; then
        updates_applied=1
    else
        oc_norm="$(printf '%s' "$oc_output" | tr '[:upper:]' '[:lower:]')"
        if [[ "$AFTER_OPENCLAW" == "__UNKNOWN__" \
          && "$oc_rc" -eq 0 \
          && "$oc_output" != *"__OPENCLAW_MISSING__"* \
          && "$oc_norm" != *"already up to date"* \
          && "$oc_norm" != *"already up-to-date"* \
          && "$oc_norm" != *"already latest"* ]]; then
            updates_applied=1
        fi
    fi

    echo "  version diff (before -> after):"
    echo "    claude   : $BEFORE_CLAUDE -> $AFTER_CLAUDE"
    echo "    codex    : $BEFORE_CODEX -> $AFTER_CODEX"
    echo "    openclaw : $BEFORE_OPENCLAW -> $AFTER_OPENCLAW"

    if [[ "$PLATFORM_UPGRADES_ONLY" == "1" ]]; then
        copy_claude_oauth_credentials_to_remote "[4b/8] Refreshing CC OAuth credentials for base snapshot..."
        copy_codex_oauth_credentials_to_remote "[4c/8] Refreshing Codex OAuth credentials for base snapshot..."
        if [[ "$ERRORS" -gt 0 ]]; then
            echo ""
            echo "Presnapshot preflight failed during OAuth refresh." >&2
            exit 1
        fi
    fi

    if [[ "$updates_applied" -eq 1 ]]; then
        echo ""
        if [[ "$PLATFORM_UPGRADES_ONLY" == "1" ]]; then
            echo "Platform updates were applied. Presnapshot wrapper should refresh the base image."
            exit 20
        else
            echo "Preflight halted after step 4 because one or more platform updates were applied."
            echo "Refresh the locked base image from this run VM, then re-run preflight:"
            echo "  $SCRIPT_DIR/livetest-refresh-base.sh --config $CONFIG_PATH"
            echo ""
            echo "After base refresh completes, run preflight again. Steps 5-8 are intentionally skipped on this pass."
            exit 0
        fi
    fi

    if [[ "$PLATFORM_UPGRADES_ONLY" == "1" ]]; then
        if [[ "$CC_OAUTH_REMOTE_REFRESHED" == "1" || "$CODEX_OAUTH_REMOTE_REFRESHED" == "1" ]]; then
            if [[ "$CC_OAUTH_REMOTE_REFRESHED" == "1" ]]; then
                echo "Claude OAuth credentials were refreshed. Presnapshot wrapper should refresh the base image."
            fi
            if [[ "$CODEX_OAUTH_REMOTE_REFRESHED" == "1" ]]; then
                echo "Codex OAuth credentials were refreshed. Presnapshot wrapper should refresh the base image."
            fi
            exit 20
        fi
        echo "No platform updates or credential refresh were applied. Base snapshot is current."
        exit 0
    fi
fi
fi

# --- Step 4d: Ensure openclaw gateway can resolve matrix-js-sdk ---
# OpenClaw's gateway imports matrix-js-sdk at runtime but the npm distribution
# has not bundled it inside its own node_modules in observed versions
# (>=2026.5.2). Without this dependency present in
# /opt/homebrew/lib/node_modules/openclaw/node_modules/matrix-js-sdk the
# matrix channel crashes with "Cannot find package 'matrix-js-sdk'" and
# auto-restarts indefinitely, breaking M1 SUP-02 (matrix canary) and every
# subsequent OC milestone that relies on the bot reply path.
#
# This step idempotently checks the openclaw global module dir for
# matrix-js-sdk and installs the pinned version when missing. It runs whether
# or not openclaw was upgraded above, because a fresh openclaw install also
# lacks the dep.
oc_enabled_for_matrix="$(read_config platforms.oc.enabled)"
if [[ "$oc_enabled_for_matrix" == "True" || "$oc_enabled_for_matrix" == "true" ]]; then
    OPENCLAW_MATRIX_JS_SDK_PIN="${OPENCLAW_MATRIX_JS_SDK_PIN:-41.4.0}"
    echo "[4d/8] Ensuring openclaw gateway has matrix-js-sdk@${OPENCLAW_MATRIX_JS_SDK_PIN}..."
    matrix_check="$(ssh "$REMOTE_HOST" "set -e; export PATH=\"/opt/homebrew/bin:\$HOME/.local/bin:\$PATH\"; eval \"\$(/opt/homebrew/bin/brew shellenv 2>/dev/null)\" 2>/dev/null || true; OC_ROOT=\"\$(npm root -g 2>/dev/null)/openclaw\"; if [[ ! -d \"\$OC_ROOT\" ]]; then echo MISSING_OC; exit 0; fi; PKG=\"\$OC_ROOT/node_modules/matrix-js-sdk/package.json\"; if [[ ! -f \"\$PKG\" ]]; then echo MISSING; exit 0; fi; node -e 'console.log(require(process.argv[1]).version)' \"\$PKG\" 2>/dev/null || echo UNKNOWN" 2>&1)"

    if [[ "$matrix_check" == "MISSING_OC" ]]; then
        echo "  openclaw global module not present; skipping matrix-js-sdk install"
    elif [[ "$matrix_check" == "$OPENCLAW_MATRIX_JS_SDK_PIN" ]]; then
        echo "  matrix-js-sdk already at ${OPENCLAW_MATRIX_JS_SDK_PIN}, skipping install"
    else
        case "$matrix_check" in
            MISSING)  echo "  matrix-js-sdk not found in openclaw global module; installing ${OPENCLAW_MATRIX_JS_SDK_PIN}..." ;;
            UNKNOWN)  echo "  matrix-js-sdk present but version unreadable; reinstalling ${OPENCLAW_MATRIX_JS_SDK_PIN}..." ;;
            *)        echo "  matrix-js-sdk currently ${matrix_check}; reinstalling pinned ${OPENCLAW_MATRIX_JS_SDK_PIN}..." ;;
        esac
        install_output=""
        if install_output="$(ssh "$REMOTE_HOST" "set -e; export PATH=\"/opt/homebrew/bin:\$HOME/.local/bin:\$PATH\"; eval \"\$(/opt/homebrew/bin/brew shellenv 2>/dev/null)\" 2>/dev/null || true; OC_ROOT=\"\$(npm root -g)/openclaw\"; cd \"\$OC_ROOT\" && npm install --no-audit --no-fund 'matrix-js-sdk@${OPENCLAW_MATRIX_JS_SDK_PIN}'" 2>&1)"; then
            echo "  matrix-js-sdk@${OPENCLAW_MATRIX_JS_SDK_PIN} installed"
        else
            echo "  WARN: matrix-js-sdk install failed (continuing — OC matrix lane will be down):"
            printf '%s\n' "$install_output" | tail -5 | sed 's/^/    /'
        fi
    fi
fi

echo "  [4/8] platform CLI check complete"

# --- Step 5: Wipe ---
echo ""
echo "[5/8] Wiping Quaid on remote ($WIPE_PLATFORM)..."

if [[ "$SKIP_WIPE" == "1" ]]; then
    echo "  (skipped — --skip-wipe)"
else
    WIPE_ARGS=("--platform" "$WIPE_PLATFORM" "--config" "$CONFIG_PATH")
    [[ "$DRY_RUN" == "1" ]] && WIPE_ARGS+=("--dry-run")

    echo "  invoking livetest-wipe.sh --platform $WIPE_PLATFORM ..."
    LIVETEST_WIPE_YES=1 "$SCRIPT_DIR/livetest-wipe.sh" "${WIPE_ARGS[@]}"
    echo "  [5/8] wipe complete"
fi

# --- Step 6: Code sync to remote (after wipe so install source is not deleted) ---
echo ""
if [[ -n "$RELEASE_VERIFY" ]]; then
    echo "[6/8] Release verification mode — removing dev tree from remote (guards clean install)..."
    # The dev tree (~/quaidcode/dev/modules/quaid/) left from prior dev runs triggers
    # setup-quaid.mjs's dev-machine guard. Remove it so the release install runs clean.
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would rm -rf ~/quaidcode on remote"
    else
        ssh "$REMOTE_HOST" 'rm -rf ~/quaidcode && echo "  dev tree removed" || echo "  ~/quaidcode not present (ok)"'
        echo "  The coordinator will install via:"
        echo "    curl -fsSL https://raw.githubusercontent.com/quaid-labs/quaid/main/install.sh \\"
        echo "      | QUAID_VERSION=$RELEASE_VERIFY bash -s -- --agent --all-platforms"
        echo "  install.sh downloads the release tarball directly from GitHub releases."
    fi
else
    echo "[6/8] Syncing latest Quaid code to remote..."
    LOCAL_DEV="$HOME/quaidcode/dev"
    LOCAL_HEAD="$(cd "$LOCAL_DEV" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "  [dry-run] would sync local dev ($LOCAL_HEAD) to remote"
    else
        echo "  Building runtime artifacts (local HEAD: $LOCAL_HEAD)..."
        (cd "$LOCAL_DEV/modules/quaid" && npm run build:runtime --silent)
        echo "  rsyncing dev tree to remote..."
        rsync -a --checksum \
            --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
            --exclude='.git/' --exclude='logs/' --exclude='.env*' --exclude='.tmp/' \
            --exclude='*MagicMock*' --exclude='<MagicMock*' --exclude='~/' \
            --exclude='.ci-local-logs/' --exclude='.pytest-home/' --exclude='.pytest_cache/' \
            --exclude='.ruff_cache/' --exclude='pytest-home/' \
            --exclude='release-promote-compatibility-work-*/' \
            --exclude='modules/quaid/tmp-lifecycle-*/' \
            "$LOCAL_DEV/" "$REMOTE_HOST:~/quaidcode/dev/" 2>&1 | tail -3
        # Do not pre-seed ~/.quaid/plugins/quaid before M0 install.
        # A plugin/runtime copy in place before the installer runs can cause the
        # OpenClaw gateway to auto-provision an instance and make the installer
        # think Quaid is already installed. Sync only the dev tree here; let M0
        # install own the runtime/plugin deployment step.
        echo "  $PASS  remote dev tree synced (local HEAD: $LOCAL_HEAD)"
    fi
fi

# --- Step 7: Copy coordinator CC OAuth credentials to remote ---
# Copies the coordinator's ~/.claude/.credentials.json to the remote so CC uses
# fresh OAuth rather than a potentially-expired/revoked API key.
# Fail early if the coordinator copy is missing or expired; otherwise W4 only
# discovers the problem when the first real CC session hits 401 before any hooks fire.
# Do NOT inject ANTHROPIC_API_KEY — it overrides credentials.json and may be stale.
copy_claude_oauth_credentials_to_remote "[7/8] Copying CC OAuth credentials to remote..."
abort_if_errors "Preflight"

# --- Step 7a: Copy coordinator Codex OAuth credentials to remote ---
# CDX and OC's openai-codex provider share the same ChatGPT OAuth account. Keep
# the VM's Codex CLI auth fresh before deriving OC auth-profiles from it.
copy_codex_oauth_credentials_to_remote "[7a/8] Copying Codex OAuth credentials to remote..."
abort_if_errors "Preflight"

# --- Step 7b: Seed shared Quaid auth credentials for installer ---
# M0 expects ~/.quaid/shared/auth/credentials.json to exist on the run VM.
# Source anthropic token from platforms.cc.auth_token_file when configured;
# otherwise use the historical local fallback. Source Codex auth from the VM's
# freshly-copied ~/.codex/auth.json, hydrate OC auth-profiles with access-only
# credentials so OpenClaw cannot rotate CDX's refresh token behind its back, and
# pin the livetest OC agent to the matching openai-codex provider.
echo ""
echo "[7b/8] Seeding Quaid shared auth credentials on remote..."
if [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] would read platforms.cc.auth_token_file (or fallback token path) and the freshly-copied $REMOTE_HOST:~/.codex/auth.json"
    echo "            then write $REMOTE_HOST:~/.quaid/shared/auth/credentials.json, seed ~/.openclaw/agents/main/agent/auth-profiles.json,"
    echo "            using access-only openai-codex credentials, and pin ~/.openclaw/openclaw.json agents.defaults.model.primary to openai-codex/gpt-5.4"
else
    LOCAL_SHARED_TOKEN_FILE="$(read_config platforms.cc.auth_token_file)"
    LOCAL_SHARED_TOKEN_FILE="${LOCAL_SHARED_TOKEN_FILE/#\~/$HOME}"
    if [[ -z "$LOCAL_SHARED_TOKEN_FILE" ]]; then
        LOCAL_SHARED_TOKEN_FILE="$HOME/quaidcode/anthtoken-yuni.md"
        echo "  WARN  platforms.cc.auth_token_file unset — falling back to $LOCAL_SHARED_TOKEN_FILE"
    fi
    if [[ ! -f "$LOCAL_SHARED_TOKEN_FILE" ]]; then
        echo "  WARN  $LOCAL_SHARED_TOKEN_FILE not found — shared auth credentials not updated"
    else
        IFS= read -r SHARED_TOKEN < "$LOCAL_SHARED_TOKEN_FILE" || true
        if [[ -z "${SHARED_TOKEN:-}" ]]; then
            echo "  WARN  $LOCAL_SHARED_TOKEN_FILE first line is empty — shared auth credentials not updated"
        else
            LOCAL_SHARED_TOKEN_TMP=""
            cleanup_local_shared_token_tmp() {
                if [[ -n "${LOCAL_SHARED_TOKEN_TMP:-}" ]]; then
                    rm -f "$LOCAL_SHARED_TOKEN_TMP"
                fi
            }
            trap cleanup_local_shared_token_tmp EXIT

            LOCAL_SHARED_TOKEN_TMP="$(mktemp "${TMPDIR:-/tmp}/quaid-shared-token.XXXXXX")"
            chmod 600 "$LOCAL_SHARED_TOKEN_TMP"
            printf '%s\n' "$SHARED_TOKEN" > "$LOCAL_SHARED_TOKEN_TMP"
            REMOTE_SHARED_TOKEN_TMP="$(ssh "$REMOTE_HOST" 'mkdir -p ~/.quaid/shared/auth && umask 077 && token_file="$(mktemp ~/.quaid/shared/auth/.shared-token.XXXXXX)" && chmod 600 "$token_file" && printf "%s\n" "$token_file"')"
            scp "$LOCAL_SHARED_TOKEN_TMP" "$REMOTE_HOST:$REMOTE_SHARED_TOKEN_TMP"
            ssh "$REMOTE_HOST" python3 - "$REMOTE_SHARED_TOKEN_TMP" <<'PYEOF'
import base64
import json
import os
import pathlib
import sys
from typing import Optional

token_path = pathlib.Path(sys.argv[1])
try:
    lines = token_path.read_text(encoding="utf-8").splitlines()
    anthropic_token = lines[0].strip() if lines else ""
finally:
    try:
        token_path.unlink(missing_ok=True)
    except Exception:
        pass
if not anthropic_token:
    raise SystemExit("empty token from temp file")

codex_auth_path = pathlib.Path.home() / ".codex" / "auth.json"
codex_token = ""
codex_refresh = ""
codex_account_id = ""
if codex_auth_path.exists():
    try:
        codex_auth = json.loads(codex_auth_path.read_text(encoding="utf-8"))
        codex_tokens = codex_auth.get("tokens", {})
        if not isinstance(codex_tokens, dict):
            codex_tokens = {}
        codex_token = str(codex_tokens.get("access_token", "")).strip()
        codex_refresh = str(codex_tokens.get("refresh_token", "")).strip()
        codex_account_id = str(codex_tokens.get("account_id", "")).strip()
    except Exception as err:
        print(f"  WARN  failed reading {codex_auth_path}: {err}")

def _jwt_expiry_ms(token: str) -> Optional[int]:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        exp = json.loads(decoded.decode("utf-8")).get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            return int(exp * 1000)
    except Exception:
        return None
    return None

out_path = pathlib.Path.home() / ".quaid" / "shared" / "auth" / "credentials.json"
payload = {
    "credentials": {
        "anthropic_oauth": {
            "token": anthropic_token,
        }
    }
}
if codex_token:
    payload["credentials"]["codex_oauth"] = {
        "token": codex_token,
    }
out_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, out_path)
except Exception:
    try:
        tmp_path.unlink(missing_ok=True)
    except Exception:
        pass
    raise
out_path.chmod(0o600)
print(f"  wrote {out_path}")

profiles_path = pathlib.Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
if codex_token:
    profiles_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        profiles = json.loads(profiles_path.read_text(encoding="utf-8")) if profiles_path.exists() else {}
    except Exception:
        profiles = {}
    expires = _jwt_expiry_ms(codex_token)
    # OpenClaw and Codex CLI run on the same VM but do not share refresh-token
    # writeback. Give OpenClaw only the long-lived access token so it cannot
    # rotate and invalidate the Codex CLI refresh token mid-run.
    credential = {
        "type": "token",
        "provider": "openai-codex",
        "token": codex_token,
        "managedBy": "quaid-preflight-access-only",
    }
    if expires:
        credential["expires"] = expires
    if codex_account_id:
        credential["accountId"] = codex_account_id

    profiles.setdefault("version", 1)
    profiles.setdefault("profiles", {})["openai-codex:default"] = credential
    profiles.setdefault("lastGood", {})["openai-codex"] = "openai-codex:default"
    profiles.setdefault("order", {})["openai-codex"] = ["openai-codex:default"]
    profiles_tmp = profiles_path.with_name(f".{profiles_path.name}.{os.getpid()}.tmp")
    fd = os.open(str(profiles_tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(profiles, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(profiles_tmp, profiles_path)
    except Exception:
        try:
            profiles_tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    profiles_path.chmod(0o600)
    print(f"  wrote {profiles_path}")

    openclaw_path = pathlib.Path.home() / ".openclaw" / "openclaw.json"
    if openclaw_path.exists():
        cfg = json.loads(openclaw_path.read_text(encoding="utf-8"))
        agents = cfg.setdefault("agents", {})
        if not isinstance(agents, dict):
            agents = {}
            cfg["agents"] = agents
        defaults = agents.setdefault("defaults", {})
        if not isinstance(defaults, dict):
            defaults = {}
            agents["defaults"] = defaults
        model_cfg = defaults.setdefault("model", {})
        if not isinstance(model_cfg, dict):
            model_cfg = {}
            defaults["model"] = model_cfg
        model_cfg["primary"] = "openai-codex/gpt-5.4"
        model_cfg["fallbacks"] = ["openai-codex/gpt-5.4-mini"]
        allowed_models = defaults.setdefault("models", {})
        if not isinstance(allowed_models, dict):
            allowed_models = {}
            defaults["models"] = allowed_models
        allowed_models.setdefault("openai-codex/gpt-5.4", {})
        allowed_models.setdefault("openai-codex/gpt-5.4-mini", {})
        openclaw_tmp = openclaw_path.with_name(f".{openclaw_path.name}.{os.getpid()}.tmp")
        with open(openclaw_tmp, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(openclaw_tmp, openclaw_path)
        print(f"  wrote {openclaw_path} agent model openai-codex/gpt-5.4")
    else:
        print(f"  WARN  {openclaw_path} missing — OC agent model default not updated")
else:
    print(f"  WARN  {codex_auth_path} missing or unreadable — codex_oauth/auth-profiles not updated")
PYEOF
            rm -f "$LOCAL_SHARED_TOKEN_TMP"
            LOCAL_SHARED_TOKEN_TMP=""
            echo "  $PASS  shared auth credentials copied to remote ~/.quaid/shared/auth/credentials.json"
        fi
    fi
fi

# --- Step 7c: Seed OC Matrix channel + helper config ---
echo ""
echo "[7c/8] Seeding OC Matrix config on remote..."
OC_ENABLED="$(read_config platforms.oc.enabled)"
if [[ "$OC_ENABLED" != "True" && "$OC_ENABLED" != "true" ]]; then
    echo "  (skipped — OC platform not enabled in config)"
elif [[ "$DRY_RUN" == "1" ]]; then
    echo "  [dry-run] would read $REMOTE_HOST:~/matrix-local/matrix-credentials.json + matrix-room.json"
    echo "            then write ~/.openclaw/openclaw.json channels.matrix and scripts/.matrix-config"
else
    MATRIX_SEED_OUTPUT="$(ssh "$REMOTE_HOST" python3 <<'PYEOF'
import json
import os
import pathlib
import urllib.parse


def _read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _extract_nested(mapping, *path):
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return current


def _extract_first(mapping, *candidates):
    for candidate in candidates:
        if isinstance(candidate, tuple):
            value = _extract_nested(mapping, *candidate)
        else:
            value = mapping.get(candidate) if isinstance(mapping, dict) else ""
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _merge_allowlist(existing, *values):
    merged = []
    seen = set()
    for source in (existing or []), values:
        for raw in source:
            text = str(raw or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
    return merged


def _is_private_homeserver(raw: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return False
    host = (parsed.hostname or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    if host.startswith("192.168.") or host.startswith("10.") or host.startswith("172.16.") or host.startswith("172.17.") or host.startswith("172.18.") or host.startswith("172.19.") or host.startswith("172.2") or host.startswith("172.30.") or host.startswith("172.31."):
        return True
    return False


home = pathlib.Path.home()
creds_path = home / "matrix-local" / "matrix-credentials.json"
room_path = home / "matrix-local" / "matrix-room.json"
openclaw_path = home / ".openclaw" / "openclaw.json"

if not creds_path.exists():
    raise SystemExit(f"missing Matrix credentials file: {creds_path}")
if not room_path.exists():
    raise SystemExit(f"missing Matrix room file: {room_path}")
if not openclaw_path.exists():
    raise SystemExit(f"missing OpenClaw config: {openclaw_path}")

creds = _read_json(creds_path)
room_cfg = _read_json(room_path)
openclaw_cfg = _read_json(openclaw_path)

homeserver = _extract_first(
    room_cfg,
    "homeserver",
    "homeserverUrl",
    "baseUrl",
    ("matrix", "homeserver"),
) or _extract_first(
    creds,
    "homeserver",
    "homeserverUrl",
    "baseUrl",
    ("matrix", "homeserver"),
    ("server", "url"),
) or "http://127.0.0.1:8008"
openclaw_user_id = _extract_first(
    room_cfg,
    "openclaw_user_id",
    "openclawUserId",
    "bot_user_id",
    "botUserId",
) or "@openclaw-bot:localhost"
sender_user_id = _extract_first(
    room_cfg,
    "sender_user_id",
    "senderUserId",
    "test_user_id",
    "testUserId",
) or "@quaid-test-bot:localhost"
room_id = _extract_first(
    room_cfg,
    "room_id",
    "roomId",
    "id",
    ("room", "room_id"),
    ("room", "roomId"),
)

if not room_id:
    raise SystemExit(f"could not extract Matrix room id from {room_path}")
if sender_user_id == openclaw_user_id:
    raise SystemExit(
        f"Matrix sender user must differ from OpenClaw bot user: {sender_user_id}"
    )


def _matrix_localpart(user_id: str) -> str:
    head = str(user_id or "").split(":", 1)[0].strip()
    return head[1:] if head.startswith("@") else head


def _account_token(data: dict, account: str) -> str:
    row = data.get(account)
    if isinstance(row, dict):
        for key in ("access_token", "accessToken", "token"):
            value = str(row.get(key) or "").strip()
            if value:
                return value
    return ""


openclaw_account = _matrix_localpart(openclaw_user_id) or "openclaw-bot"
sender_account = _matrix_localpart(sender_user_id) or "quaid-test-bot"
access_token = _account_token(creds, openclaw_account) or _extract_first(
    creds,
    ("openclaw-bot", "access_token"),
    ("openclaw-bot", "accessToken"),
    ("openclaw-bot", "token"),
    "access_token",
    "accessToken",
    "token",
    ("tokens", "access_token"),
    ("tokens", "accessToken"),
    ("auth", "access_token"),
    ("auth", "accessToken"),
)
sender_access_token = _account_token(creds, sender_account) or _extract_first(
    creds,
    ("quaid-test-bot", "access_token"),
    ("quaid-test-bot", "accessToken"),
    ("quaid-test-bot", "token"),
    ("sender", "access_token"),
    ("sender", "accessToken"),
    ("sender", "token"),
    ("test-bot", "access_token"),
    ("test-bot", "accessToken"),
    ("test-bot", "token"),
)
if not access_token:
    raise SystemExit(
        f"could not extract OpenClaw bot Matrix access token for {openclaw_user_id} from {creds_path}"
    )
if not sender_access_token:
    raise SystemExit(
        f"could not extract Matrix sender access token for {sender_user_id} from {creds_path}"
    )
if sender_access_token == access_token:
    raise SystemExit(
        "Matrix sender token resolves to the OpenClaw bot token; tests must send as the external tester account"
    )

plugins = openclaw_cfg.setdefault("plugins", {})
allow = plugins.setdefault("allow", [])
if not isinstance(allow, list):
    allow = []
    plugins["allow"] = allow
if "matrix" not in allow:
    allow.append("matrix")

entries = plugins.setdefault("entries", {})
if not isinstance(entries, dict):
    entries = {}
    plugins["entries"] = entries
matrix_entry = entries.get("matrix")
if not isinstance(matrix_entry, dict):
    matrix_entry = {}
entries["matrix"] = matrix_entry
matrix_entry["enabled"] = True

channels = openclaw_cfg.setdefault("channels", {})
if not isinstance(channels, dict):
    channels = {}
    openclaw_cfg["channels"] = channels
matrix_cfg = channels.get("matrix")
if not isinstance(matrix_cfg, dict):
    matrix_cfg = {}
    channels["matrix"] = matrix_cfg

matrix_cfg["enabled"] = True
matrix_cfg["homeserver"] = homeserver.rstrip("/")
matrix_cfg["accessToken"] = access_token
matrix_cfg["userId"] = openclaw_user_id
if _is_private_homeserver(homeserver):
    network_cfg = matrix_cfg.get("network")
    if not isinstance(network_cfg, dict):
        network_cfg = {}
        matrix_cfg["network"] = network_cfg
    network_cfg["dangerouslyAllowPrivateNetwork"] = True
matrix_cfg.pop("allowPrivateNetwork", None)
matrix_cfg["autoJoin"] = "allowlist"
matrix_cfg["autoJoinAllowlist"] = _merge_allowlist(matrix_cfg.get("autoJoinAllowlist"), room_id)
matrix_cfg["groupPolicy"] = "allowlist"
matrix_cfg["groupAllowFrom"] = _merge_allowlist(
    matrix_cfg.get("groupAllowFrom"),
    sender_user_id,
)
dm_cfg = matrix_cfg.get("dm")
if not isinstance(dm_cfg, dict):
    dm_cfg = {}
matrix_cfg["dm"] = dm_cfg
dm_cfg["policy"] = "allowlist"
dm_cfg["allowFrom"] = _merge_allowlist(
    dm_cfg.get("allowFrom"),
    sender_user_id,
)
groups_cfg = matrix_cfg.get("groups")
if not isinstance(groups_cfg, dict):
    groups_cfg = {}
matrix_cfg["groups"] = groups_cfg
room_entry = groups_cfg.get(room_id)
if not isinstance(room_entry, dict):
    room_entry = {}
groups_cfg[room_id] = room_entry
room_entry["enabled"] = True
room_entry.pop("allow", None)
room_entry["requireMention"] = False
room_tools = room_entry.get("tools")
if not isinstance(room_tools, dict):
    room_tools = {}
room_entry["tools"] = room_tools
room_tools["deny"] = _merge_allowlist(room_tools.get("deny"), "write")

helper_paths = [
    home / "quaidcode" / "dev" / "modules" / "quaid" / "tests" / "livetest" / "scripts" / ".matrix-config",
    home / "quaidcode" / "util" / "scripts" / ".matrix-config",
]


def _write_text_atomic(path: pathlib.Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = ""
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = ""
    if current == content:
        return False
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
    path.chmod(0o600)
    return True


openclaw_json = json.dumps(openclaw_cfg, indent=2) + "\n"
openclaw_changed = _write_text_atomic(openclaw_path, openclaw_json)

helper_payload = (
    f"MATRIX_HOMESERVER={homeserver.rstrip('/')}\n"
    f"MATRIX_ACCESS_TOKEN={sender_access_token}\n"
    f"MATRIX_ROOM_ID={room_id}\n"
    f"MATRIX_SENDER_USER_ID={sender_user_id}\n"
    f"MATRIX_BOT_USER_ID={openclaw_user_id}\n"
)
helper_changed = False
for helper_path in helper_paths:
    helper_changed = _write_text_atomic(helper_path, helper_payload) or helper_changed

print(json.dumps({
    "openclaw_changed": openclaw_changed,
    "helper_changed": helper_changed,
    "homeserver": homeserver.rstrip("/"),
    "room_id": room_id,
    "sender_user_id": sender_user_id,
    "openclaw_user_id": openclaw_user_id,
}))
PYEOF
)"
    MATRIX_OPENCLAW_CHANGED="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1]).get("openclaw_changed") else "0")' "$MATRIX_SEED_OUTPUT")"
    MATRIX_HELPER_CHANGED="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1]).get("helper_changed") else "0")' "$MATRIX_SEED_OUTPUT")"
    MATRIX_ROOM_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("room_id",""))' "$MATRIX_SEED_OUTPUT")"
    MATRIX_HOMESERVER="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("homeserver",""))' "$MATRIX_SEED_OUTPUT")"
    MATRIX_SENDER_USER_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("sender_user_id",""))' "$MATRIX_SEED_OUTPUT")"
    if [[ "$MATRIX_OPENCLAW_CHANGED" == "1" || "$MATRIX_HELPER_CHANGED" == "1" ]]; then
        echo "  $PASS  OC Matrix config seeded (homeserver=$MATRIX_HOMESERVER room=$MATRIX_ROOM_ID sender=$MATRIX_SENDER_USER_ID)"
    else
        echo "  $PASS  OC Matrix config already present (homeserver=$MATRIX_HOMESERVER room=$MATRIX_ROOM_ID sender=$MATRIX_SENDER_USER_ID)"
    fi
    if [[ "$MATRIX_OPENCLAW_CHANGED" == "1" ]]; then
        ssh "$REMOTE_HOST" 'launchctl kickstart -k "gui/$(id -u)/ai.openclaw.gateway" >/dev/null 2>&1 || (export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; OPENCLAW_BIN="$(command -v openclaw || true)"; if [ -z "$OPENCLAW_BIN" ] && [ -x /opt/homebrew/bin/openclaw ]; then OPENCLAW_BIN=/opt/homebrew/bin/openclaw; fi; if [ -n "$OPENCLAW_BIN" ]; then "$OPENCLAW_BIN" gateway restart >/dev/null 2>&1; fi) || true'
        echo "  $PASS  requested OpenClaw gateway restart to pick up channels.matrix changes"
    fi
fi

# --- Step 8: Platform services ---
echo ""
echo "[8/8] Starting platform services on remote..."

# In release verify mode, clear stale quaid plugin references from openclaw.json before
# starting the gateway. The wipe removes the quaid plugin runtime but leaves the OC gateway
# config intact, causing "plugin not found: quaid" on startup if the references remain.
if [[ -n "$RELEASE_VERIFY" && "$SKIP_PLATFORM_START" != "1" && "$DRY_RUN" != "1" ]]; then
    echo "  [release-verify] clearing stale quaid plugin references from remote openclaw.json..."
    ssh "$REMOTE_HOST" python3 <<'PYEOF'
import json, pathlib
p = pathlib.Path.home() / '.openclaw' / 'openclaw.json'
if not p.exists():
    print('  ~/.openclaw/openclaw.json not found — skipping')
else:
    d = json.loads(p.read_text())
    changed = False
    plugins = d.get('plugins', {})
    entries = plugins.get('entries', {})
    if 'quaid' in entries:
        del entries['quaid']
        changed = True
    allow = plugins.get('allow', [])
    if 'quaid' in allow:
        allow.remove('quaid')
        changed = True
    slots = plugins.get('slots', {})
    if slots.get('memory') == 'quaid':
        del slots['memory']
        changed = True
    if changed:
        p.write_text(json.dumps(d, indent=2))
        print('  cleared stale quaid references from ~/.openclaw/openclaw.json')
    else:
        print('  ~/.openclaw/openclaw.json has no stale quaid references — no changes needed')
PYEOF
fi

if [[ "$SKIP_PLATFORM_START" == "1" ]]; then
    echo "  (skipped — --skip-platform-start)"
else
    START_ARGS=("--config" "$CONFIG_PATH")
    [[ "$DRY_RUN" == "1" ]] && START_ARGS+=("--dry-run")

    echo "  invoking livetest-platform-start.sh..."
    "$SCRIPT_DIR/livetest-platform-start.sh" "${START_ARGS[@]}"
    echo "  [8/8] platform start complete"
fi

verify_claude_oauth_seed_persisted "[8b/8] Verifying CC OAuth seed persisted..."
verify_codex_oauth_seed_persisted "[8c/8] Verifying Codex OAuth seed persisted..."
abort_if_errors "Preflight"

# --- Done ---
echo ""
echo "========================================"
echo " Preflight complete — remote is clean"
if [[ -n "$RELEASE_VERIFY" ]]; then
    echo " Mode     : RELEASE VERIFICATION ($RELEASE_VERIFY)"
    echo " Install  : curl | bash via install.sh (coordinator-driven)"
    echo " Ready for coordinator to run M0 install, then brief testers at M1."
else
    echo " Ready to start Run M0."
fi
echo "========================================"
