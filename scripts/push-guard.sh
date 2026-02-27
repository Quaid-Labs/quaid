#!/usr/bin/env bash
set -euo pipefail

REMOTE_NAME="${1:-}"
REMOTE_URL="${2:-}"
RELEASE_MODE="${QUAID_RELEASE:-0}"
ALLOW_GITHUB_PUSH="${QUAID_ALLOW_GITHUB_PUSH:-0}"
ALLOW_MAIN_PUSH="${QUAID_ALLOW_MAIN_PUSH:-0}"

if [[ -z "$REMOTE_NAME" || -z "$REMOTE_URL" ]]; then
  echo "[push-guard] missing remote arguments from git pre-push hook" >&2
  exit 2
fi

# Default workflow: push work to local backup remote only.
if [[ "$REMOTE_NAME" == "checkpoint" || "$REMOTE_NAME" == "nas" || "$REMOTE_NAME" == "backup" ]]; then
  exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [[ "$branch" == "main" && "$ALLOW_MAIN_PUSH" != "1" ]]; then
  cat >&2 <<'EOF'
[push-guard] blocked push from main.
Policy: push from feature branches; reserve main pushes for explicit release intent.
Override once: QUAID_ALLOW_MAIN_PUSH=1 git push <remote> <refspec>
EOF
  exit 1
fi

if [[ "$REMOTE_URL" == *"github.com/Steadman-Labs/quaid"* && "$ALLOW_GITHUB_PUSH" != "1" ]]; then
  if [[ "$RELEASE_MODE" == "1" ]]; then
    exit 0
  fi
  cat >&2 <<'EOF'
[push-guard] blocked push to GitHub remote.
Default policy routes pushes to local NAS/checkpoint remote first.
Use:
  git push checkpoint <refspec>
For release push to GitHub, use:
  ./scripts/release-push.sh
Manual one-shot override:
  QUAID_RELEASE=1 git push origin <refspec>
EOF
  exit 1
fi

exit 0
