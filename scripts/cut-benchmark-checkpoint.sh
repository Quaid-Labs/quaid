#!/usr/bin/env bash
set -euo pipefail

# Back-compat wrapper.
# Canonical script lives in benchmark workspace:
#   ~/quaid/benchmark/scripts/cut-benchmark-checkpoint.sh

exec "$HOME/quaid/benchmark/scripts/cut-benchmark-checkpoint.sh" "$@"
