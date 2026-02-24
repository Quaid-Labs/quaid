#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-$HOME/quaid/dev}"
TARGET_ROOT="${TARGET_ROOT:-$HOME/quaid/benchmark-checkpoint}"
DELETE_MODE=true
DRY_RUN=false

usage() {
  cat <<'USAGE'
Usage: cut-benchmark-checkpoint.sh [options]

Sync dev -> benchmark-checkpoint for benchmark reproducibility.

Options:
  --source <path>      Source tree (default: ~/quaid/dev)
  --target <path>      Target checkpoint tree (default: ~/quaid/benchmark-checkpoint)
  --no-delete          Do not delete extraneous files from target
  --dry-run            Preview rsync changes only
  -h, --help           Show this help

Environment:
  SOURCE_ROOT, TARGET_ROOT may be used instead of flags.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE_ROOT="$2"; shift 2 ;;
    --target) TARGET_ROOT="$2"; shift 2 ;;
    --no-delete) DELETE_MODE=false; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

SOURCE_ROOT="$(cd "$SOURCE_ROOT" && pwd)"
TARGET_ROOT="${TARGET_ROOT/#\~/$HOME}"

if [[ ! -d "$SOURCE_ROOT" ]]; then
  echo "Source does not exist: $SOURCE_ROOT" >&2
  exit 1
fi
if [[ ! -d "$SOURCE_ROOT/.git" ]]; then
  echo "Source is not a git repository: $SOURCE_ROOT" >&2
  exit 1
fi
if [[ "$SOURCE_ROOT" == "$TARGET_ROOT" ]]; then
  echo "Source and target must be different." >&2
  exit 1
fi

mkdir -p "$TARGET_ROOT"

delete_flag=()
if [[ "$DELETE_MODE" == "true" ]]; then
  delete_flag+=(--delete)
fi
if [[ "$DRY_RUN" == "true" ]]; then
  delete_flag+=(--dry-run)
fi

echo "[checkpoint-cut] source=$SOURCE_ROOT"
echo "[checkpoint-cut] target=$TARGET_ROOT"
echo "[checkpoint-cut] delete_mode=$DELETE_MODE dry_run=$DRY_RUN"

rsync -a "${delete_flag[@]}" \
  --exclude '.git/' \
  --exclude 'node_modules/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.venv/' \
  --exclude '.mypy_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '.DS_Store' \
  "$SOURCE_ROOT"/ "$TARGET_ROOT"/

if [[ "$DRY_RUN" == "true" ]]; then
  echo "[checkpoint-cut] dry-run complete (no files changed)."
  exit 0
fi

src_sha="$(git -C "$SOURCE_ROOT" rev-parse --short HEAD)"
src_branch="$(git -C "$SOURCE_ROOT" rev-parse --abbrev-ref HEAD)"
timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

cat > "$TARGET_ROOT/.quaid-checkpoint-meta" <<META
source_root=$SOURCE_ROOT
source_branch=$src_branch
source_sha=$src_sha
cut_at_utc=$timestamp
META

echo "[checkpoint-cut] complete"
echo "[checkpoint-cut] source_sha=$src_sha branch=$src_branch"
echo "[checkpoint-cut] metadata=$TARGET_ROOT/.quaid-checkpoint-meta"
