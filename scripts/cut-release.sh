#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VERSION="$(node -p "require('./modules/quaid/package.json').version")"
TAG="v${VERSION}"
NOTES_FILE="docs/releases/${TAG}.md"
POST_FILE="docs/releases/${TAG}-release-post.md"
TARBALL_PATH="$ROOT_DIR/release/quaid-release.tar.gz"
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
DRY_RUN=0
ALLOW_CANARY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --allow-canary)
      ALLOW_CANARY=1
      shift
      ;;
    *)
      echo "[cut-release] unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ ! "$VERSION" =~ ^([0-9]+)\.([0-9]+)\. ]]; then
  echo "[cut-release] could not derive release line from version '$VERSION'" >&2
  exit 1
fi

RELEASE_BRANCH="release/${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
HEAD_SHA="$(git rev-parse HEAD)"

die() {
  echo "[cut-release] $*" >&2
  exit 1
}

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[cut-release] DRY-RUN'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

ref_sha() {
  local ref="$1"
  git rev-parse --verify "$ref" 2>/dev/null || true
}

release_body_file() {
  if [[ -f "$POST_FILE" ]]; then
    printf '%s\n' "$POST_FILE"
  else
    printf '%s\n' "$NOTES_FILE"
  fi
}

if [[ -n "$(git status --short)" ]]; then
  die "worktree is dirty; commit or stash changes before cutting a release"
fi

if [[ ! -f "$NOTES_FILE" ]]; then
  die "missing release notes: $NOTES_FILE"
fi

if [[ ! -f "$POST_FILE" ]]; then
  echo "[cut-release] release post missing, will use release notes body: $NOTES_FILE"
fi

case "$CURRENT_BRANCH" in
  main|release/*)
    ;;
  canary)
    [[ "$ALLOW_CANARY" -eq 1 ]] || die "refusing to cut a release from canary without --allow-canary"
    ;;
  *)
    die "release cuts must run from main or release/* (or canary with --allow-canary during transition); current branch is $CURRENT_BRANCH"
    ;;
esac

echo "[cut-release] release target"
echo "  version: $VERSION"
echo "  tag:     $TAG"
echo "  branch:  $RELEASE_BRANCH"
echo "  head:    $HEAD_SHA"
echo "  source:  $CURRENT_BRANCH"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "  mode:    dry-run"
fi

echo "[cut-release] release checks"
bash "$ROOT_DIR/scripts/release-check.sh"

echo "[cut-release] build tarball"
bash "$ROOT_DIR/scripts/build-release-tarball.sh"
[[ -f "$TARBALL_PATH" ]] || die "expected tarball missing: $TARBALL_PATH"

LOCAL_BRANCH_SHA="$(ref_sha "refs/heads/$RELEASE_BRANCH")"
REMOTE_BRANCH_SHA="$(ref_sha "refs/remotes/github/$RELEASE_BRANCH")"

if [[ -n "$LOCAL_BRANCH_SHA" && "$LOCAL_BRANCH_SHA" != "$HEAD_SHA" ]]; then
  if git merge-base --is-ancestor "$LOCAL_BRANCH_SHA" "$HEAD_SHA"; then
    echo "[cut-release] fast-forward local $RELEASE_BRANCH -> $HEAD_SHA"
    run_cmd git branch -f "$RELEASE_BRANCH" "$HEAD_SHA" >/dev/null
  else
    die "local $RELEASE_BRANCH points to $LOCAL_BRANCH_SHA, not an ancestor of release HEAD $HEAD_SHA"
  fi
elif [[ -z "$LOCAL_BRANCH_SHA" ]]; then
  echo "[cut-release] create local $RELEASE_BRANCH at $HEAD_SHA"
  run_cmd git branch "$RELEASE_BRANCH" "$HEAD_SHA" >/dev/null
fi

if [[ -n "$REMOTE_BRANCH_SHA" && "$REMOTE_BRANCH_SHA" != "$HEAD_SHA" ]]; then
  if git merge-base --is-ancestor "$REMOTE_BRANCH_SHA" "$HEAD_SHA"; then
    echo "[cut-release] remote github/$RELEASE_BRANCH will fast-forward from $REMOTE_BRANCH_SHA"
  else
    die "remote github/$RELEASE_BRANCH points to $REMOTE_BRANCH_SHA, not an ancestor of release HEAD $HEAD_SHA"
  fi
fi

TAG_SHA="$(git rev-parse "${TAG}^{commit}" 2>/dev/null || true)"
if [[ -n "$TAG_SHA" && "$TAG_SHA" != "$HEAD_SHA" ]]; then
  die "tag $TAG already exists at $TAG_SHA, not release HEAD $HEAD_SHA"
elif [[ -z "$TAG_SHA" ]]; then
  echo "[cut-release] create annotated tag $TAG"
  run_cmd git tag -a "$TAG" -m "Quaid $TAG" "$HEAD_SHA"
fi

if [[ "$CURRENT_BRANCH" == "main" ]]; then
  echo "[cut-release] push main"
  run_cmd git push github main
elif [[ "$CURRENT_BRANCH" == release/* ]]; then
  echo "[cut-release] source branch is release branch; no separate main push"
elif [[ "$CURRENT_BRANCH" == "canary" ]]; then
  echo "[cut-release] transitional canary source; not pushing canary as part of release"
fi

echo "[cut-release] push release branch"
run_cmd git push github "$RELEASE_BRANCH:$RELEASE_BRANCH"

echo "[cut-release] push tag"
run_cmd git push github "$TAG"

PRERELEASE_FLAG=()
if [[ "$VERSION" == *alpha* || "$VERSION" == *beta* || "$VERSION" == *rc* ]]; then
  PRERELEASE_FLAG=(--prerelease)
fi

RELEASE_BODY_FILE="$(release_body_file)"

if gh release view "$TAG" --repo Quaid-Labs/quaid >/dev/null 2>&1; then
  RELEASE_TARGET="$(gh release view "$TAG" --repo Quaid-Labs/quaid --json targetCommitish -q .targetCommitish 2>/dev/null || true)"
  if [[ -n "$RELEASE_TARGET" && "$RELEASE_TARGET" != "$HEAD_SHA" && "$RELEASE_TARGET" != "$TAG" ]]; then
    die "GitHub release $TAG already exists with target '$RELEASE_TARGET', not release HEAD $HEAD_SHA"
  fi
  echo "[cut-release] GitHub release $TAG already exists; uploading tarball asset"
  run_cmd gh release upload "$TAG" "$TARBALL_PATH" --repo Quaid-Labs/quaid --clobber
else
  echo "[cut-release] create GitHub release $TAG"
  run_cmd gh release create "$TAG" \
    "$TARBALL_PATH" \
    --repo Quaid-Labs/quaid \
    --title "$TAG" \
    --notes-file "$RELEASE_BODY_FILE" \
    --target "$HEAD_SHA" \
    "${PRERELEASE_FLAG[@]}"
fi

echo "[cut-release] PASS tag=$TAG branch=$RELEASE_BRANCH"
echo "[cut-release] restore working branch: git switch $CURRENT_BRANCH"
