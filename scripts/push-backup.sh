#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

REMOTE="${1:-checkpoint}"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [[ -z "$BRANCH" ]]; then
  echo "[push-backup] failed to resolve current branch" >&2
  exit 2
fi

git push "$REMOTE" "HEAD:$BRANCH"
echo "[push-backup] pushed $BRANCH to $REMOTE"
