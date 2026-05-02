#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${1:-github}"
TARGET_BRANCH="main"
BRANCH="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)"
REMOTE_MAIN_REF="${REMOTE}/${TARGET_BRANCH}"
HEAD_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
STAGING_BRANCH="ci/main/${HEAD_SHA:0:12}"
WAIT_SECONDS="${QUAID_PUSH_MAIN_WAIT_SECONDS:-2400}"
POLL_SECONDS="${QUAID_PUSH_MAIN_POLL_SECONDS:-10}"
POST_PROMOTION_WAIT_SECONDS="${QUAID_PUSH_MAIN_POST_PROMOTION_WAIT_SECONDS:-2400}"
POST_PROMOTION_DISCOVERY_SECONDS="${QUAID_PUSH_MAIN_POST_PROMOTION_DISCOVERY_SECONDS:-90}"
PREVALIDATION_STAMP="$ROOT_DIR/.git/.quaid-prepush-validation.stamp"
REQUIRE_PREVALIDATION="${QUAID_PUSH_MAIN_REQUIRE_PREVALIDATION:-1}"
REQUIRE_PREVALIDATION_MODE="${QUAID_PUSH_MAIN_REQUIRE_PREVALIDATION_MODE:-any}"

die() {
  echo "[push-main] ERROR: $*" >&2
  exit 1
}

notify_operator() {
  local message="$1"
  local notifier="${QUAID_PUSH_MAIN_NOTIFY_CMD:-${HOME}/quaidcode/util/scripts/tg}"
  if [[ -x "$notifier" ]]; then
    "$notifier" "$message" >/dev/null 2>&1 || true
  fi
}

require_prepush_validation() {
  if [[ "$REQUIRE_PREVALIDATION" != "1" ]]; then
    echo "[push-main] prepush validation gate bypassed (QUAID_PUSH_MAIN_REQUIRE_PREVALIDATION=${REQUIRE_PREVALIDATION})"
    return
  fi

  [[ -f "$PREVALIDATION_STAMP" ]] || die "missing prepush validation stamp for HEAD ${HEAD_SHA}; run ./scripts/prepush-validate.sh [--full]"

  local stamp_head=""
  local stamp_mode=""
  local stamp_ts=""
  while IFS='=' read -r key value; do
    case "$key" in
      head_sha) stamp_head="$value" ;;
      mode) stamp_mode="$value" ;;
      timestamp_utc) stamp_ts="$value" ;;
    esac
  done < "$PREVALIDATION_STAMP"

  [[ -n "$stamp_head" ]] || die "invalid prepush validation stamp (${PREVALIDATION_STAMP}); run ./scripts/prepush-validate.sh again"
  [[ "$stamp_head" == "$HEAD_SHA" ]] || die "stale prepush validation stamp (stamp=${stamp_head} head=${HEAD_SHA}); run ./scripts/prepush-validate.sh again"

  case "$REQUIRE_PREVALIDATION_MODE" in
    any|quick)
      ;;
    full)
      [[ "$stamp_mode" == "full" ]] || die "prepush validation mode is '${stamp_mode:-unknown}'; full mode required. Run ./scripts/prepush-validate.sh --full"
      ;;
    *)
      die "invalid QUAID_PUSH_MAIN_REQUIRE_PREVALIDATION_MODE='${REQUIRE_PREVALIDATION_MODE}' (expected: any|quick|full)"
      ;;
  esac

  echo "[push-main] prepush validation stamp OK (head=${stamp_head} mode=${stamp_mode:-unknown} ts=${stamp_ts:-unknown})"
}

cleanup_staging_branch() {
  git push "$REMOTE" ":${STAGING_BRANCH}" >/dev/null 2>&1 || true
}

installer_gate_required() {
  git rev-parse --verify "$REMOTE_MAIN_REF" >/dev/null 2>&1 || return 1
  git diff --name-only "${REMOTE_MAIN_REF}...HEAD" | \
    rg -q '^(setup-quaid\.mjs|setup-quaid\.sh|install\.sh|install\.ps1|\.github/workflows/installer-openclaw-smoke\.yml|\.github/workflows/ci\.yml|scripts/push-main\.sh)$'
}

wait_for_workflow_success() {
  local workflow_name="$1"
  local branch_name="$2"
  local deadline="$3"
  local run_id=""

  while (( $(date +%s) < deadline )); do
    run_id="$(gh run list --workflow "$workflow_name" --branch "$branch_name" --limit 20 --json databaseId,headSha --jq ".[] | select(.headSha == \"${HEAD_SHA}\") | .databaseId" | head -n1 | tr -d '[:space:]')"
    if [[ -n "$run_id" ]]; then
      break
    fi
    sleep "$POLL_SECONDS"
  done

  [[ -n "$run_id" ]] || die "timed out waiting for ${workflow_name} run on ${branch_name} (${HEAD_SHA})"

  echo "[push-main] waiting for ${workflow_name} run ${run_id}"
  while (( $(date +%s) < deadline )); do
    local status=""
    local conclusion=""
    status="$(gh run view "$run_id" --json status --jq '.status' | tr -d '[:space:]')"
    if [[ "$status" == "completed" ]]; then
      conclusion="$(gh run view "$run_id" --json conclusion --jq '.conclusion' | tr -d '[:space:]')"
      [[ "$conclusion" == "success" ]] || die "${workflow_name} failed on ${branch_name} (run ${run_id}, conclusion=${conclusion:-unknown})"
      return 0
    fi
    sleep "$POLL_SECONDS"
  done

  local final_conclusion=""
  final_conclusion="$(gh run view "$run_id" --json conclusion --jq '.conclusion' | tr -d '[:space:]')"
  [[ "$final_conclusion" == "success" ]] || die "timed out waiting for successful ${workflow_name} on ${branch_name} (run ${run_id})"
}

post_promotion_runs() {
  gh run list \
    --branch "$TARGET_BRANCH" \
    --commit "$HEAD_SHA" \
    --limit 50 \
    --json databaseId,name,status,conclusion,url \
    --jq '.[] | [.databaseId,.name,.status,(.conclusion // ""),.url] | @tsv'
}

wait_for_post_promotion_gates() {
  local deadline="$1"
  local discover_deadline=$(( $(date +%s) + POST_PROMOTION_DISCOVERY_SECONDS ))
  local runs=""

  echo "[push-main] waiting for post-promotion ${TARGET_BRANCH} workflow runs"
  while (( $(date +%s) < discover_deadline )); do
    runs="$(post_promotion_runs || true)"
    if [[ -n "$runs" ]]; then
      break
    fi
    sleep "$POLL_SECONDS"
  done

  [[ -n "$runs" ]] || die "timed out waiting for post-promotion ${TARGET_BRANCH} workflow runs (${HEAD_SHA})"

  while (( $(date +%s) < deadline )); do
    runs="$(post_promotion_runs || true)"
    local failed=""
    local incomplete=0

    while IFS=$'\t' read -r run_id run_name run_status run_conclusion run_url; do
      [[ -n "$run_id" ]] || continue
      if [[ "$run_status" != "completed" ]]; then
        incomplete=1
        continue
      fi
      case "$run_conclusion" in
        success|skipped)
          ;;
        *)
          failed+="${run_name} run ${run_id} conclusion=${run_conclusion:-unknown} ${run_url}"$'\n'
          ;;
      esac
    done <<< "$runs"

    if [[ -n "$failed" ]]; then
      notify_operator "Quaid post-promotion gate failed on ${TARGET_BRANCH} ${HEAD_SHA}: ${failed}"
      die "post-promotion ${TARGET_BRANCH} workflow failed: ${failed//$'\n'/; }"
    fi

    if [[ "$incomplete" == "0" ]]; then
      echo "[push-main] post-promotion ${TARGET_BRANCH} workflow gates PASS"
      return 0
    fi

    sleep "$POLL_SECONDS"
  done

  notify_operator "Quaid post-promotion gate timed out on ${TARGET_BRANCH} ${HEAD_SHA}"
  die "timed out waiting for post-promotion ${TARGET_BRANCH} workflow gates (${HEAD_SHA})"
}

if [[ "$BRANCH" != "$TARGET_BRANCH" ]]; then
  die "current branch is '$BRANCH'; only '$TARGET_BRANCH' may be pushed with this script"
fi

if [[ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]]; then
  die "worktree is dirty; commit or stash changes before pushing main"
fi

cd "$ROOT_DIR"
require_prepush_validation

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
wait_for_workflow_success "Quaid CI" "$STAGING_BRANCH" "$deadline"
if installer_gate_required; then
  wait_for_workflow_success "Installer OpenClaw Smoke" "$STAGING_BRANCH" "$deadline"
else
  echo "[push-main] installer smoke gate not required for this changeset"
fi

echo "[push-main] promoting ${HEAD_SHA} to ${TARGET_BRANCH}"
git push "$REMOTE" "HEAD:${TARGET_BRANCH}"

post_promotion_deadline=$(( $(date +%s) + POST_PROMOTION_WAIT_SECONDS ))
wait_for_post_promotion_gates "$post_promotion_deadline"

echo "[push-main] deleting staging branch ${STAGING_BRANCH}"
cleanup_staging_branch
trap - EXIT

echo "[push-main] PASS"
