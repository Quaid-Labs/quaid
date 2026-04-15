#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${1:-github}"
TARGET_BRANCH="main"
BRANCH="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)"
REMOTE_MAIN_REF="${REMOTE}/${TARGET_BRANCH}"
HEAD_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
STAGING_BRANCH="ci/main/${HEAD_SHA:0:12}"
WORKFLOW_NAME="Quaid CI"
WAIT_SECONDS="${QUAID_PUSH_MAIN_WAIT_SECONDS:-2400}"
POLL_SECONDS="${QUAID_PUSH_MAIN_POLL_SECONDS:-10}"

die() {
  echo "[push-main] ERROR: $*" >&2
  exit 1
}

cleanup_staging_branch() {
  git push "$REMOTE" ":${STAGING_BRANCH}" >/dev/null 2>&1 || true
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

echo "[push-main] pushing ${REMOTE} ${STAGING_BRANCH}"
git push "$REMOTE" "HEAD:${STAGING_BRANCH}"

trap cleanup_staging_branch EXIT

deadline=$(( $(date +%s) + WAIT_SECONDS ))
run_id=""
while (( $(date +%s) < deadline )); do
  run_id="$(gh run list --workflow "$WORKFLOW_NAME" --branch "$STAGING_BRANCH" --limit 20 --json databaseId,headSha --jq ".[] | select(.headSha == \"${HEAD_SHA}\") | .databaseId" | head -n1 | tr -d '[:space:]')"
  if [[ -n "$run_id" ]]; then
    break
  fi
  sleep "$POLL_SECONDS"
done

[[ -n "$run_id" ]] || die "timed out waiting for ${WORKFLOW_NAME} run on ${STAGING_BRANCH} (${HEAD_SHA})"

echo "[push-main] waiting for ${WORKFLOW_NAME} run ${run_id}"
while (( $(date +%s) < deadline )); do
  status="$(gh run view "$run_id" --json status --jq '.status' | tr -d '[:space:]')"
  if [[ "$status" == "completed" ]]; then
    conclusion="$(gh run view "$run_id" --json conclusion --jq '.conclusion' | tr -d '[:space:]')"
    [[ "$conclusion" == "success" ]] || die "${WORKFLOW_NAME} failed on ${STAGING_BRANCH} (run ${run_id}, conclusion=${conclusion:-unknown})"
    break
  fi
  sleep "$POLL_SECONDS"
done

final_conclusion="$(gh run view "$run_id" --json conclusion --jq '.conclusion' | tr -d '[:space:]')"
[[ "$final_conclusion" == "success" ]] || die "timed out waiting for successful ${WORKFLOW_NAME} on ${STAGING_BRANCH} (run ${run_id})"

echo "[push-main] promoting ${HEAD_SHA} to ${TARGET_BRANCH}"
git push "$REMOTE" "HEAD:${TARGET_BRANCH}"

echo "[push-main] deleting staging branch ${STAGING_BRANCH}"
cleanup_staging_branch
trap - EXIT

echo "[push-main] PASS"
