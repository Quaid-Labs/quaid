#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE="${1:-github}"
TARGET_BRANCH="canary"
BRANCH="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD)"
REMOTE_MAIN_REF="${REMOTE}/main"
REMOTE_CANARY_REF="${REMOTE}/${TARGET_BRANCH}"

die() {
  echo "[push-canary] ERROR: $*" >&2
  exit 1
}

if [[ "$BRANCH" != "$TARGET_BRANCH" ]]; then
  die "current branch is '$BRANCH'; only '$TARGET_BRANCH' may be pushed with this script"
fi

if [[ "$REMOTE" == "main" || "$TARGET_BRANCH" == "main" ]]; then
  die "refusing to push to main"
fi

if [[ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]]; then
  die "worktree is dirty; commit or stash changes before pushing canary"
fi

cd "$ROOT_DIR"

if git rev-parse --verify "$REMOTE_MAIN_REF" >/dev/null 2>&1 && git rev-parse --verify "$REMOTE_CANARY_REF" >/dev/null 2>&1; then
  history_scan="$(git log --format='%an%x09%ae%x09%cn%x09%ce' "${REMOTE_MAIN_REF}..${REMOTE_CANARY_REF}" | rg -n '(^|	)(Clawdbot|clawdbot@testbench\.local)(	|$)' || true)"
  if [[ -n "$history_scan" ]]; then
    die "target ${REMOTE_CANARY_REF} still contains Clawdbot/local-email commit attribution; rewrite canary history before pushing new commits"
  fi
fi

scan_patterns=(
  'owner'
  'owner'
  'user:owner'
  '<redacted>'
  '<telegram-id>'
  'alfie\.local'
  '/Users/owner/'
  '/Users/solomon/'
  'clawdbot@testbench\.local'
  '\bSolomon\b'
)
scan_files=()
while IFS= read -r file; do
  case "$file" in
    README.md|NOTICE|scripts/push-canary.sh)
      continue
      ;;
  esac
  scan_files+=("$file")
done < <(git ls-files)

pattern_regex="$(printf '%s\n' "${scan_patterns[@]}" | paste -sd'|' -)"
tmp_scan="$(mktemp /tmp/quaid-push-canary-scan.XXXXXX)"

echo "[push-canary] privacy scan"
if ((${#scan_files[@]})) && rg -n -S -e "$pattern_regex" "${scan_files[@]}" >"$tmp_scan" 2>/dev/null; then
  cat "$tmp_scan" >&2
  rm -f "$tmp_scan"
  die "tracked tree still contains blocked private/local markers"
fi
rm -f "$tmp_scan"

echo "[push-canary] ownership / attribution"
node scripts/release-owner-check.mjs

echo "[push-canary] docs consistency"
node scripts/check-docs-consistency.mjs

echo "[push-canary] runtime ts/js pairs"
(
  cd modules/quaid
  node scripts/check-runtime-pairs.mjs --strict
)

echo "[push-canary] pushing ${REMOTE} ${TARGET_BRANCH}"
git push "$REMOTE" "HEAD:${TARGET_BRANCH}"

echo "[push-canary] PASS"
