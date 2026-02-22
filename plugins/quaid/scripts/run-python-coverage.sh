#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="fast"
FAIL_UNDER="${FAIL_UNDER:-45}"
VENV_DIR="${ROOT_DIR}/.venv-cov"

usage() {
  cat <<USAGE
Usage: $(basename "$0") [--fast|--full] [--fail-under N]

Modes:
  --fast  Excludes heavy historical regression packs (default)
  --full  Includes all Python tests
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fast) MODE="fast"; shift ;;
    --full) MODE="full"; shift ;;
    --fail-under) FAIL_UNDER="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pytest coverage mcp

if [[ "$MODE" == "full" ]]; then
  TEST_FILES=$(find tests -maxdepth 1 -type f -name 'test_*.py' | sort)
else
  TEST_FILES=$(find tests -maxdepth 1 -type f -name 'test_*.py' \
    ! -name 'test_golden_recall.py' \
    ! -name 'test_batch2_data_quality.py' \
    ! -name 'test_batch3_smart_retrieval.py' \
    ! -name 'test_batch4_decay_traversal.py' \
    ! -name 'test_chunk1_improvements.py' \
    ! -name 'test_chunk2_improvements.py' \
    ! -name 'test_coverage_gaps.py' | sort)
fi

echo "[coverage:py] mode=${MODE} fail_under=${FAIL_UNDER}"
python -m coverage erase
python -m coverage run -m pytest -q ${TEST_FILES}
python -m coverage report -m --omit='tests/*' --fail-under "${FAIL_UNDER}"
