#!/bin/bash
# Prune stale OpenClaw agent silos in the livetest harness only.
#
# This is intentionally not called by user-facing `quaid instances list`.
# Physical deletion of ~/.quaid/instances/* is too risky for production list paths.
# Use this script only from livetest cleanup after native `openclaw agents delete`
# has already removed the matching OpenClaw agent/workspace state.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUAID_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
QUAID_HOME_ARG="${QUAID_HOME:-$HOME/.quaid}"
DRY_RUN=0

usage() {
    cat <<USAGE
Usage: livetest-prune-openclaw-silos.sh [--home PATH] [--dry-run]

Options:
  --home PATH   Quaid home to inspect/prune. Default: \$QUAID_HOME or ~/.quaid
  --dry-run     List stale OpenClaw silos that would be hidden from enumeration;
                does not delete anything.

This script sets QUAID_LIVETEST_HARNESS=1 for the explicit prune call. Do not
use it as a general production cleanup command.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --home)
            QUAID_HOME_ARG="${2:?--home requires a path}"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown option '$1'" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ "$DRY_RUN" == "1" ]]; then
    QUAID_HOME="$QUAID_HOME_ARG" PYTHONPATH="$QUAID_ROOT" python3 - <<'PY'
from lib.instance import stale_openclaw_agent_instances

for name in stale_openclaw_agent_instances():
    print(name)
PY
    exit 0
fi

QUAID_HOME="$QUAID_HOME_ARG" QUAID_LIVETEST_HARNESS=1 PYTHONPATH="$QUAID_ROOT" python3 - <<'PY'
from lib.instance import prune_livetest_instance_residues, prune_stale_openclaw_agent_instances

seen = set()
for name in [*prune_stale_openclaw_agent_instances(), *prune_livetest_instance_residues()]:
    if name in seen:
        continue
    seen.add(name)
    print(name)
PY
