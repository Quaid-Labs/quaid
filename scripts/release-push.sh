#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REMOTE="${QUAID_RELEASE_REMOTE:-github}"
REMOTE_BRANCH="${1:-}"
LOCAL_BRANCH="${2:-$(git rev-parse --abbrev-ref HEAD)}"

if [[ -z "$REMOTE_BRANCH" ]]; then
  cat >&2 <<'EOF'
[release-push] usage: ./scripts/release-push.sh <github-branch> [local-branch]
Policy: GitHub pushes require an explicit target branch every time.
Example: ./scripts/release-push.sh main
EOF
  exit 1
fi

bash "$ROOT_DIR/scripts/release-check.sh"

if [[ "$LOCAL_BRANCH" == "main" ]]; then
  QUAID_ALLOW_MAIN_PUSH=1 QUAID_RELEASE=1 QUAID_ALLOW_GITHUB_PUSH=1 \
    git push "$REMOTE" "$LOCAL_BRANCH:$REMOTE_BRANCH"
else
  QUAID_RELEASE=1 QUAID_ALLOW_GITHUB_PUSH=1 \
    git push "$REMOTE" "$LOCAL_BRANCH:$REMOTE_BRANCH"
fi

echo "[release-push] pushed $LOCAL_BRANCH -> $REMOTE:$REMOTE_BRANCH"
