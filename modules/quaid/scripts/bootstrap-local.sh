#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROFILE="${ROOT_DIR}/profiles/runtime-profile.local.quaid.json"
WIPE=false
AUTH_PROVIDER=""
AUTH_PATH=""
USE_WORKTREE=true
WORKTREE_SOURCE=""
WORKTREE_SOURCE_EXPLICIT=false
WORKTREE_BRANCH=""
WORKTREE_TEST_BRANCH="test-runtime"
WORKTREE_DETACH=false
WORKTREE_REMOTE="origin"
BACKUP_ROOT="${HOME}/quaid/backups"
PREBOOTSTRAP_MAX_BACKUPS="${QUAID_PREBOOTSTRAP_MAX_BACKUPS:-3}"
OPENCLAW_SOURCE="${HOME}/quaid/openclaw-source"
OPENCLAW_REPO_URL="${OPENCLAW_REPO_URL:-https://github.com/openclaw/openclaw.git}"
OPENCLAW_REF="${OPENCLAW_REF:-}"
OPENCLAW_REFRESH=true
OPENCLAW_INSTALL=true
WORKTREE_EXCLUDE_PATTERNS=(
  "node_modules/"
  "dist/"
  "build/"
  ".pytest_cache/"
  "__pycache__/"
  "*.pyc"
  ".venv/"
  "venv/"
  ".env"
  ".env.*"
  "logs/"
  "data/"
  "plugins/quaid/.state/"
)

is_git_repo_or_worktree() {
  local dir="$1"
  git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

ensure_worktree_local_excludes() {
  local workspace="$1"
  local git_dir
  git_dir="$(git -C "$workspace" rev-parse --git-dir)"
  local exclude_file="${git_dir}/info/exclude"
  mkdir -p "$(dirname "$exclude_file")"
  touch "$exclude_file"
  for pattern in "${WORKTREE_EXCLUDE_PATTERNS[@]}"; do
    if ! rg -qx --fixed-strings "$pattern" "$exclude_file" >/dev/null 2>&1; then
      echo "$pattern" >> "$exclude_file"
    fi
  done
  echo "Ensured worktree-local excludes in: $exclude_file"
}

prune_prebootstrap_backups() {
  local pre_dir="${BACKUP_ROOT}/prebootstrap"
  [[ -d "$pre_dir" ]] || return 0
  local keep_count="$PREBOOTSTRAP_MAX_BACKUPS"
  if ! [[ "$keep_count" =~ ^[0-9]+$ ]]; then
    keep_count=3
  fi
  if [[ "$keep_count" -lt 1 ]]; then
    keep_count=1
  fi
  backup_dirs=()
  while IFS= read -r line; do
    backup_dirs+=("$line")
  done < <(find "$pre_dir" -mindepth 1 -maxdepth 1 -type d -print | sort)
  local total="${#backup_dirs[@]}"
  if [[ "$total" -le "$keep_count" ]]; then
    return 0
  fi
  local delete_n=$((total - keep_count))
  for ((i=0; i<delete_n; i++)); do
    rm -rf "${backup_dirs[$i]}"
  done
  echo "Pruned prebootstrap backups: removed ${delete_n}, kept ${keep_count}"
}

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Options:
  --profile <path>       Profile JSON (default: profiles/runtime-profile.local.quaid.json)
  --wipe                 Move existing runtime workspace to timestamped backup first
  --auth-provider <id>   Auth provider to apply: anthropic|openai
  --auth-path <id>       Auth path to apply: openai-oauth|openai-api|anthropic-oauth|anthropic-api
  --no-worktree          Skip git worktree provisioning for runtime workspace
  --worktree-source <p>  Source repo for runtime worktree (default: sibling dev, fallback: <workspace>-dev-clean, then <workspace>-dev)
  --worktree-branch <b>  Base branch/ref for runtime worktree (default: source repo current branch, fallback: main)
  --worktree-test-branch <b> Branch to use inside runtime workspace (default: test-runtime)
  --detach-worktree      Leave runtime workspace detached (legacy behavior)
  --worktree-remote <r>  Remote to fetch before creating worktree (default: origin)
  --openclaw-source <p>  OpenClaw source checkout path (default: ~/quaid/openclaw-source)
  --openclaw-ref <ref>   OpenClaw git ref/tag/sha to checkout (e.g. v2026.3.7)
  --no-openclaw-refresh  Skip refreshing OpenClaw source checkout
  --no-openclaw-install  Skip installing OpenClaw CLI from source
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --wipe) WIPE=true; shift ;;
    --auth-provider) AUTH_PROVIDER="$2"; shift 2 ;;
    --auth-path) AUTH_PATH="$2"; shift 2 ;;
    --no-worktree) USE_WORKTREE=false; shift ;;
    --worktree-source) WORKTREE_SOURCE="$2"; WORKTREE_SOURCE_EXPLICIT=true; shift 2 ;;
    --worktree-branch) WORKTREE_BRANCH="$2"; shift 2 ;;
    --worktree-test-branch) WORKTREE_TEST_BRANCH="$2"; shift 2 ;;
    --detach-worktree) WORKTREE_DETACH=true; shift ;;
    --worktree-remote) WORKTREE_REMOTE="$2"; shift 2 ;;
    --openclaw-source) OPENCLAW_SOURCE="$2"; shift 2 ;;
    --openclaw-ref) OPENCLAW_REF="$2"; shift 2 ;;
    --no-openclaw-refresh) OPENCLAW_REFRESH=false; shift ;;
    --no-openclaw-install) OPENCLAW_INSTALL=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ -n "$AUTH_PROVIDER" ]] && [[ "$AUTH_PROVIDER" != "anthropic" ]] && [[ "$AUTH_PROVIDER" != "openai" ]]; then
  echo "Invalid --auth-provider: $AUTH_PROVIDER (expected: anthropic|openai)" >&2
  exit 1
fi
if [[ -n "$AUTH_PATH" ]] && [[ "$AUTH_PATH" != "openai-oauth" ]] && [[ "$AUTH_PATH" != "openai-api" ]] && [[ "$AUTH_PATH" != "anthropic-oauth" ]] && [[ "$AUTH_PATH" != "anthropic-api" ]]; then
  echo "Invalid --auth-path: $AUTH_PATH (expected: openai-oauth|openai-api|anthropic-oauth|anthropic-api)" >&2
  exit 1
fi
if [[ -n "$AUTH_PROVIDER" ]] && [[ -n "$AUTH_PATH" ]]; then
  echo "Use either --auth-provider or --auth-path, not both." >&2
  exit 1
fi

if [[ ! -f "$PROFILE" ]]; then
  echo "Profile not found: $PROFILE" >&2
  exit 1
fi

WORKSPACE="$(python3 -c 'import json,sys,pathlib; raw=json.load(open(sys.argv[1], encoding="utf-8"))["runtime"]["workspace"]; print(pathlib.Path(str(raw)).expanduser())' "$PROFILE")"
if [[ -z "$WORKTREE_SOURCE" ]]; then
  WORKSPACE_PARENT="$(dirname "$WORKSPACE")"
  if is_git_repo_or_worktree "${WORKSPACE_PARENT}/dev"; then
    WORKTREE_SOURCE="${WORKSPACE_PARENT}/dev"
  elif is_git_repo_or_worktree "${WORKSPACE}-dev-clean"; then
    WORKTREE_SOURCE="${WORKSPACE}-dev-clean"
  elif is_git_repo_or_worktree "${WORKSPACE}-dev"; then
    WORKTREE_SOURCE="${WORKSPACE}-dev"
  else
    WORKTREE_SOURCE="${WORKSPACE_PARENT}/dev"
  fi
fi

if [[ -z "$WORKTREE_BRANCH" ]]; then
  src_branch="$(git -C "$WORKTREE_SOURCE" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
  if [[ -n "$src_branch" && "$src_branch" != "HEAD" ]]; then
    WORKTREE_BRANCH="$src_branch"
  else
    WORKTREE_BRANCH="main"
  fi
fi

if [[ "$USE_WORKTREE" == true ]] && [[ "$WORKTREE_SOURCE_EXPLICIT" != true ]]; then
  if [[ "$(basename "$WORKSPACE")" == *benchmark* ]]; then
    USE_WORKTREE=false
    echo "Skipping auto-worktree for benchmark workspace: $WORKSPACE"
  elif ! is_git_repo_or_worktree "$WORKTREE_SOURCE"; then
    USE_WORKTREE=false
    echo "Skipping auto-worktree; source repo not found: $WORKTREE_SOURCE"
  fi
fi

is_same_worktree_source() {
  local candidate="$1"
  if [[ ! -e "$candidate/.git" ]]; then
    return 1
  fi
  local ws_common=""
  local src_common=""
  ws_common="$(git -C "$candidate" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  src_common="$(git -C "$WORKTREE_SOURCE" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  [[ -n "$ws_common" && -n "$src_common" && "$ws_common" == "$src_common" ]]
}

if [[ "$WIPE" == true ]] && [[ -d "$WORKSPACE" ]]; then
  TS="$(date +%Y%m%d-%H%M%S)"
  BKP="${BACKUP_ROOT}/prebootstrap/${TS}"
  mkdir -p "$(dirname "$BKP")"
  prune_prebootstrap_backups
  mv "$WORKSPACE" "$BKP"
  if [[ "$USE_WORKTREE" != true ]]; then
    mkdir -p "$WORKSPACE"
  fi
  echo "Moved existing workspace to: $BKP"

  # Clear OpenClaw runtime session history/state so /new|/reset tests start clean.
  OPENCLAW_HOME="${HOME}/.openclaw"
  if [[ -d "$OPENCLAW_HOME" ]]; then
    find "$OPENCLAW_HOME/agents" -type d -name sessions -prune -exec rm -rf {} + 2>/dev/null || true
    rm -rf "$OPENCLAW_HOME/completions" "$OPENCLAW_HOME/delivery-queue" 2>/dev/null || true
    mkdir -p "$OPENCLAW_HOME/completions" "$OPENCLAW_HOME/delivery-queue" 2>/dev/null || true
    echo "Cleared OpenClaw session history/state under: $OPENCLAW_HOME"
  fi

  # Clear Quaid temp state that survives workspace wipes.
  rm -f /tmp/memory-notes-*.json /tmp/memory-injection-*.log /tmp/extraction-details-*.json /tmp/quaid-recovery-last-run-* 2>/dev/null || true
  echo "Cleared Quaid temp state under: /tmp"
fi

ensure_openclaw_source() {
  if [[ "$OPENCLAW_INSTALL" != true ]]; then
    return 0
  fi

  mkdir -p "$(dirname "$OPENCLAW_SOURCE")"
  if [[ ! -d "${OPENCLAW_SOURCE}/.git" ]]; then
    echo "Cloning OpenClaw source into: $OPENCLAW_SOURCE"
    git clone "$OPENCLAW_REPO_URL" "$OPENCLAW_SOURCE"
  elif [[ "$OPENCLAW_REFRESH" == true ]]; then
    echo "Refreshing OpenClaw source in: $OPENCLAW_SOURCE"
    git -C "$OPENCLAW_SOURCE" fetch --all --prune
    if git -C "$OPENCLAW_SOURCE" show-ref --verify --quiet "refs/remotes/origin/main"; then
      git -C "$OPENCLAW_SOURCE" checkout main >/dev/null 2>&1 || true
      git -C "$OPENCLAW_SOURCE" pull --ff-only origin main || true
    else
      git -C "$OPENCLAW_SOURCE" pull --ff-only || true
    fi
  fi

  if [[ -n "$OPENCLAW_REF" ]]; then
    echo "Checking out OpenClaw ref: $OPENCLAW_REF"
    git -C "$OPENCLAW_SOURCE" fetch --all --tags --prune
    git -C "$OPENCLAW_SOURCE" checkout "$OPENCLAW_REF"
  fi
}

install_openclaw_cli() {
  if [[ "$OPENCLAW_INSTALL" != true ]]; then
    echo "Skipping OpenClaw CLI install (--no-openclaw-install)."
    return 0
  fi
  if [[ ! -f "${OPENCLAW_SOURCE}/package.json" ]]; then
    echo "OpenClaw source missing package.json at ${OPENCLAW_SOURCE}; installing latest from npm."
    npm install -g openclaw
  else
    echo "Building OpenClaw source before global install..."
    (cd "$OPENCLAW_SOURCE" && npm install)
    if (cd "$OPENCLAW_SOURCE" && npm run --silent build >/dev/null 2>&1); then
      echo "OpenClaw source build completed."
    else
      echo "WARN: OpenClaw source build failed; falling back to npm latest package."
      npm install -g openclaw
      echo "OpenClaw CLI version: $(openclaw --version 2>/dev/null || echo unknown)"
      return 0
    fi
    echo "Installing OpenClaw CLI from source: $OPENCLAW_SOURCE"
    npm install -g "$OPENCLAW_SOURCE"
  fi
  echo "OpenClaw CLI version: $(openclaw --version 2>/dev/null || echo unknown)"
}

ensure_openclaw_source
install_openclaw_cli

if [[ "$USE_WORKTREE" == true ]]; then
  if ! is_git_repo_or_worktree "$WORKTREE_SOURCE"; then
    echo "Worktree source is not a git repo: $WORKTREE_SOURCE" >&2
    exit 1
  fi

  if [[ -d "$WORKSPACE" ]] && ! is_same_worktree_source "$WORKSPACE"; then
    echo "Workspace exists but is not a worktree from $WORKTREE_SOURCE: $WORKSPACE" >&2
    echo "Re-run with --wipe to archive it before bootstrap." >&2
    exit 1
  fi

  if [[ ! -d "$WORKSPACE" ]]; then
    git -C "$WORKTREE_SOURCE" worktree prune >/dev/null 2>&1 || true
    git -C "$WORKTREE_SOURCE" fetch "$WORKTREE_REMOTE" "$WORKTREE_BRANCH" >/dev/null 2>&1 || true
    if git -C "$WORKTREE_SOURCE" show-ref --verify --quiet "refs/heads/${WORKTREE_TEST_BRANCH}"; then
      git -C "$WORKTREE_SOURCE" worktree add --force "$WORKSPACE" "$WORKTREE_TEST_BRANCH"
    else
      git -C "$WORKTREE_SOURCE" worktree add --force -b "$WORKTREE_TEST_BRANCH" "$WORKSPACE" "$WORKTREE_BRANCH"
    fi
    echo "Provisioned runtime workspace as worktree: $WORKSPACE <= $WORKTREE_SOURCE (${WORKTREE_TEST_BRANCH})"
  else
    echo "Runtime workspace already attached to worktree source: $WORKSPACE"
  fi

  if [[ "$WORKTREE_DETACH" == true ]]; then
    git -C "$WORKSPACE" checkout --detach >/dev/null 2>&1 || true
    echo "Workspace set to detached HEAD (--detach-worktree)."
  else
    if git -C "$WORKSPACE" show-ref --verify --quiet "refs/heads/${WORKTREE_TEST_BRANCH}"; then
      git -C "$WORKSPACE" checkout "$WORKTREE_TEST_BRANCH" >/dev/null 2>&1 || true
    else
      git -C "$WORKSPACE" checkout -b "$WORKTREE_TEST_BRANCH" "$WORKTREE_BRANCH" >/dev/null 2>&1 || true
    fi
    echo "Workspace on commit branch: ${WORKTREE_TEST_BRANCH}"
  fi

  # Always align runtime workspace to the source branch tip from local dev first.
  # Falling back to remote is acceptable when local branch isn't present.
  if git -C "$WORKTREE_SOURCE" show-ref --verify --quiet "refs/heads/${WORKTREE_BRANCH}"; then
    git -C "$WORKSPACE" reset --hard "$WORKTREE_BRANCH" >/dev/null 2>&1 || true
  else
    git -C "$WORKTREE_SOURCE" fetch "$WORKTREE_REMOTE" "$WORKTREE_BRANCH" >/dev/null 2>&1 || true
    if git -C "$WORKSPACE" show-ref --verify --quiet "refs/remotes/${WORKTREE_REMOTE}/${WORKTREE_BRANCH}"; then
      git -C "$WORKSPACE" reset --hard "${WORKTREE_REMOTE}/${WORKTREE_BRANCH}" >/dev/null 2>&1 || true
    fi
  fi
  echo "Aligned runtime workspace to: ${WORKTREE_BRANCH}"
fi

if is_git_repo_or_worktree "$WORKSPACE"; then
  ensure_worktree_local_excludes "$WORKSPACE"
fi

APPLY_ARGS=(--profile "$PROFILE")
if [[ -n "$AUTH_PROVIDER" ]]; then
  APPLY_ARGS+=(--auth-provider "$AUTH_PROVIDER")
fi
if [[ -n "$AUTH_PATH" ]]; then
  APPLY_ARGS+=(--auth-path "$AUTH_PATH")
fi
python3 "${ROOT_DIR}/scripts/apply-runtime-profile.py" "${APPLY_ARGS[@]}"

PLUGIN_DIR="${WORKSPACE}/plugins/quaid"
MODULE_DIR="${WORKSPACE}/modules/quaid"
ACTIVE_PLUGIN_DIR="${PLUGIN_DIR}"

# Link plugins/quaid -> modules/quaid BEFORE the installer, so that
# openclaw plugins enable can find the runtime files during preflight.
if [[ -f "${MODULE_DIR}/package.json" ]]; then
  mkdir -p "${WORKSPACE}/plugins"
  if [[ -d "${PLUGIN_DIR}" ]] && [[ ! -L "${PLUGIN_DIR}" ]] && [[ ! -f "${PLUGIN_DIR}/package.json" ]]; then
    STALE_DIR_BACKUP="${PLUGIN_DIR}.stale.$(date +%Y%m%d-%H%M%S)"
    mv "${PLUGIN_DIR}" "${STALE_DIR_BACKUP}"
    echo "Moved stale plugin shim dir: ${PLUGIN_DIR} -> ${STALE_DIR_BACKUP}"
  fi
  ln -sfn ../modules/quaid "${PLUGIN_DIR}"
  ACTIVE_PLUGIN_DIR="${MODULE_DIR}"
  echo "Linked plugin dir before installer: ${PLUGIN_DIR} -> ../modules/quaid"
fi

INSTALL_OWNER_NAME="${QUAID_BOOTSTRAP_OWNER_NAME:-Solomon Steadman}"
INSTALLER_MJS="${WORKTREE_SOURCE}/setup-quaid.mjs"
if [[ ! -f "${INSTALLER_MJS}" ]] && [[ -f "${WORKSPACE}/setup-quaid.mjs" ]]; then
  INSTALLER_MJS="${WORKSPACE}/setup-quaid.mjs"
fi
if [[ -f "${INSTALLER_MJS}" ]]; then
  echo "Running canonical installer: ${INSTALLER_MJS}"
  (
    cd "$(dirname "${INSTALLER_MJS}")"
    QUAID_INSTALL_AGENT=1 \
    QUAID_INSTALL_SKIP_BIN_SHIM=1 \
    node "${INSTALLER_MJS}" --agent --workspace "${WORKSPACE}" --owner-name "${INSTALL_OWNER_NAME}"
  )
else
  echo "WARN: setup-quaid.mjs not found (source=${WORKTREE_SOURCE}, workspace=${WORKSPACE}); skipping installer run."
fi

if [[ -f "${MODULE_DIR}/package.json" ]]; then
  echo "Installing Quaid module test dependencies in: ${MODULE_DIR}"
  if [[ -f "${MODULE_DIR}/package-lock.json" ]]; then
    (cd "${MODULE_DIR}" && npm ci)
  else
    (cd "${MODULE_DIR}" && npm install)
  fi
fi

# Repoint global quaid CLI shim to the active runtime workspace.
# This prevents stale links to old sandboxes (for example ~/clawd).
BIN_DIR="${HOME}/bin"
mkdir -p "${BIN_DIR}"
ln -sfn "${ACTIVE_PLUGIN_DIR}/quaid" "${BIN_DIR}/quaid"
echo "Updated CLI shim: ${BIN_DIR}/quaid -> ${ACTIVE_PLUGIN_DIR}/quaid"

echo "Local bootstrap complete for workspace: $WORKSPACE"
