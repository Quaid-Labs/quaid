#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${1:-github}"
TARGET_BRANCH="main"
BRANCH="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)"
REMOTE_MAIN_REF="${REMOTE}/${TARGET_BRANCH}"

die() {
  echo "[push-main] ERROR: $*" >&2
  exit 1
}

if [[ "$BRANCH" != "$TARGET_BRANCH" ]]; then
  die "current branch is '$BRANCH'; only '$TARGET_BRANCH' may be pushed with this script"
fi

if [[ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]]; then
  die "worktree is dirty; commit or stash changes before pushing main"
fi

cd "$ROOT_DIR"

if git rev-parse --verify "$REMOTE_MAIN_REF" >/dev/null 2>&1; then
  history_scan="$(git log --format='%an%x09%ae%x09%cn%x09%ce' "${REMOTE_MAIN_REF}..HEAD" | rg -n '(^|\t)(Clawdbot|clawdbot@testbench\.local)(\t|$)' || true)"
  if [[ -n "$history_scan" ]]; then
    die "push range still contains Clawdbot/local-email commit attribution; rewrite history before pushing main"
  fi
fi

echo "[push-main] privacy scan"
node scripts/privacy-audit.mjs --tree-only

echo "[push-main] ownership / attribution"
node scripts/release-owner-check.mjs

echo "[push-main] docs consistency"
node scripts/check-docs-consistency.mjs

echo "[push-main] runtime ts/js pairs"
(
  cd modules/quaid
  node scripts/check-runtime-pairs.mjs --strict
)

echo "[push-main] pushing ${REMOTE} ${TARGET_BRANCH}"
git push "$REMOTE" "HEAD:${TARGET_BRANCH}"

echo "[push-main] PASS"
