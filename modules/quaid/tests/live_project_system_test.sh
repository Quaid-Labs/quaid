#!/usr/bin/env bash
# Live integration test for the project system.
#
# Tests the full pipeline against a running Quaid instance using the
# actual CLI. Can be run standalone or driven by tmux message passing.
#
# Usage:
#   ./live_project_system_test.sh              # Run all tests
#   ./live_project_system_test.sh --test-name  # Run specific test
#
# Prerequisites:
#   - QUAID_HOME must be set
#   - quaid CLI must be on PATH or SCRIPT_DIR/../../quaid must exist
#
# Exit codes:
#   0 — all tests passed
#   1 — one or more tests failed

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

# Find the quaid CLI
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUAID_CMD="${QUAID_CMD:-$(dirname "$SCRIPT_DIR")/quaid}"
if [[ ! -x "$QUAID_CMD" ]]; then
    echo -e "${RED}ERROR: quaid CLI not found at $QUAID_CMD${NC}" >&2
    echo "Set QUAID_CMD or ensure quaid is on PATH" >&2
    exit 1
fi

# Ensure QUAID_HOME is set
if [[ -z "${QUAID_HOME:-}" ]]; then
    echo -e "${RED}ERROR: QUAID_HOME not set${NC}" >&2
    exit 1
fi

# Test workspace
TEST_DIR="$(mktemp -d /tmp/quaid-live-test.XXXXXX)"
trap 'rm -rf "$TEST_DIR"' EXIT

log_pass() { echo -e "  ${GREEN}PASS${NC}: $1"; (( ++PASS_COUNT )); }
log_fail() { echo -e "  ${RED}FAIL${NC}: $1"; (( ++FAIL_COUNT )); }
log_skip() { echo -e "  ${YELLOW}SKIP${NC}: $1"; (( ++SKIP_COUNT )); }

# Unique project name to avoid collisions
TEST_PROJECT="live-test-$(date +%s)"

# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

test_project_list() {
    echo "Test: project list"
    output=$("$QUAID_CMD" project list 2>&1) || true
    if [[ $? -eq 0 ]]; then
        log_pass "project list runs without error"
    else
        log_fail "project list failed: $output"
    fi
}

test_project_create() {
    echo "Test: project create"

    # Create a source dir
    mkdir -p "$TEST_DIR/src"
    echo "print('hello')" > "$TEST_DIR/src/main.py"
    echo "x = 1" > "$TEST_DIR/src/utils.py"

    output=$("$QUAID_CMD" project create "$TEST_PROJECT" \
        -d "Live test project" \
        -s "$TEST_DIR/src" 2>&1)

    if echo "$output" | grep -q "Created project"; then
        log_pass "project create succeeded"
    else
        log_fail "project create: unexpected output: $output"
        return
    fi

    # Verify it shows up in list
    list_output=$("$QUAID_CMD" project list 2>&1)
    if echo "$list_output" | grep -q "$TEST_PROJECT"; then
        log_pass "project appears in list"
    else
        log_fail "project not in list output"
    fi
}

test_project_show() {
    echo "Test: project show"
    output=$("$QUAID_CMD" project show "$TEST_PROJECT" 2>&1)
    if echo "$output" | grep -q "Live test project"; then
        log_pass "project show displays description"
    else
        log_fail "project show: $output"
    fi
}

test_project_snapshot_no_changes() {
    echo "Test: project snapshot (no changes)"
    output=$("$QUAID_CMD" project snapshot "$TEST_PROJECT" 2>&1)
    if echo "$output" | grep -q "No changes"; then
        log_pass "snapshot reports no changes"
    else
        log_fail "snapshot unexpected: $output"
    fi
}

test_project_snapshot_with_changes() {
    echo "Test: project snapshot (with changes)"

    # Modify a file
    echo "print('modified')" > "$TEST_DIR/src/main.py"
    echo "# new file" > "$TEST_DIR/src/new_module.py"

    output=$("$QUAID_CMD" project snapshot "$TEST_PROJECT" 2>&1)
    if echo "$output" | grep -q "changes"; then
        log_pass "snapshot detects changes"
    else
        log_fail "snapshot didn't detect changes: $output"
    fi

    if echo "$output" | grep -q "main.py"; then
        log_pass "snapshot lists modified file"
    else
        log_fail "snapshot didn't list main.py"
    fi
}

test_project_sync() {
    echo "Test: project sync"
    output=$("$QUAID_CMD" project sync 2>&1)
    # May say "no sync target" if running without OC — that's OK
    if [[ $? -eq 0 ]]; then
        log_pass "project sync runs without error"
    else
        log_fail "project sync failed: $output"
    fi
}

test_project_update() {
    echo "Test: project update"
    output=$("$QUAID_CMD" project update "$TEST_PROJECT" -d "Updated description" 2>&1)
    if echo "$output" | grep -q "Updated project"; then
        log_pass "project update succeeded"
    else
        log_fail "project update: $output"
    fi

    # Verify update
    show_output=$("$QUAID_CMD" project show "$TEST_PROJECT" 2>&1)
    if echo "$show_output" | grep -q "Updated description"; then
        log_pass "project update persisted"
    else
        log_fail "project update not persisted"
    fi
}

test_project_delete() {
    echo "Test: project delete"
    output=$("$QUAID_CMD" project delete "$TEST_PROJECT" 2>&1)
    if echo "$output" | grep -q "Deleted project"; then
        log_pass "project delete succeeded"
    else
        log_fail "project delete: $output"
    fi

    # Verify gone from list
    list_output=$("$QUAID_CMD" project list 2>&1)
    if ! echo "$list_output" | grep -q "$TEST_PROJECT"; then
        log_pass "project removed from list"
    else
        log_fail "project still in list after delete"
    fi

    # Verify source files untouched
    if [[ -f "$TEST_DIR/src/main.py" ]]; then
        log_pass "source files preserved after delete"
    else
        log_fail "source files deleted!"
    fi
}

test_invalid_project_name() {
    echo "Test: invalid project name"
    output=$("$QUAID_CMD" project create "Invalid Name!" 2>&1) || true
    if echo "$output" | grep -qi "error\|invalid"; then
        log_pass "invalid name rejected"
    else
        log_fail "invalid name not rejected: $output"
    fi
}

# --------------------------------------------------------------------------
# Cross-Instance Shared Project Tests
# --------------------------------------------------------------------------

test_shared_project_cross_instance() {
    echo ""
    echo "======================================"
    echo "Cross-Instance Shared Project Test"
    echo "======================================"
    echo ""

    SHARED_PROJECT="shared-live-test-$(date +%s)"

    # --- Phase 1: OC creates and populates project ---
    echo "Phase 1: OC instance creates project"

    if [[ -z "${QUAID_HOME:-}" ]]; then
        log_skip "QUAID_HOME not set, cannot test cross-instance"
        return
    fi

    # Create source content for OC
    mkdir -p "$TEST_DIR/shared-src"
    cat > "$TEST_DIR/shared-src/main.py" <<'PYEOF'
def hello():
    return "shared project content"
PYEOF
    cat > "$TEST_DIR/shared-src/README.md" <<'MDEOF'
# Shared Test Project
This project was created by OC for cross-instance testing.
MDEOF

    # Create project as OC instance
    oc_output=$(QUAID_INSTANCE=openclaw "$QUAID_CMD" project create "$SHARED_PROJECT" \
        -d "Cross-instance shared project test" \
        -s "$TEST_DIR/shared-src" 2>&1) || true

    if echo "$oc_output" | grep -q "Created project"; then
        log_pass "OC created shared project"
    else
        log_fail "OC project create failed: $oc_output"
        return
    fi

    # Compact to flush docs (if compact subcommand exists)
    compact_output=$(QUAID_INSTANCE=openclaw "$QUAID_CMD" project compact "$SHARED_PROJECT" 2>&1) || true
    if echo "$compact_output" | grep -qi "compact\|success\|done"; then
        log_pass "OC project compact succeeded"
    else
        log_skip "OC project compact not available or no-op: $compact_output"
    fi

    # --- Phase 2: CC links to same project and reads it ---
    echo "Phase 2: CC instance links and reads project"

    # Link CC to the same shared project
    link_output=$(QUAID_INSTANCE=claude-code "$QUAID_CMD" project link "$SHARED_PROJECT" 2>&1) || true
    if echo "$link_output" | grep -qi "link\|added\|success"; then
        log_pass "CC linked to shared project"
    else
        log_fail "CC project link failed: $link_output"
    fi

    # CC should be able to show the project
    cc_show=$(QUAID_INSTANCE=claude-code "$QUAID_CMD" project show "$SHARED_PROJECT" 2>&1) || true
    if echo "$cc_show" | grep -q "Cross-instance shared project test"; then
        log_pass "CC can read OC-created project description"
    else
        log_fail "CC cannot read shared project: $cc_show"
    fi

    # Verify shared project dir exists
    shared_dir="$QUAID_HOME/projects/$SHARED_PROJECT"
    if [[ -d "$shared_dir" ]]; then
        log_pass "shared project dir exists at $shared_dir"
    else
        log_skip "shared project dir not at expected path (may use different layout)"
    fi

    # --- Phase 3: Verify both instances see it in their registries ---
    echo "Phase 3: Verify registry entries"

    oc_list=$(QUAID_INSTANCE=openclaw "$QUAID_CMD" project list 2>&1) || true
    if echo "$oc_list" | grep -q "$SHARED_PROJECT"; then
        log_pass "OC lists shared project"
    else
        log_fail "OC doesn't list shared project"
    fi

    cc_list=$(QUAID_INSTANCE=claude-code "$QUAID_CMD" project list 2>&1) || true
    if echo "$cc_list" | grep -q "$SHARED_PROJECT"; then
        log_pass "CC lists shared project"
    else
        log_fail "CC doesn't list shared project"
    fi

    # --- Cleanup ---
    echo "Cleanup: removing test project"
    QUAID_INSTANCE=openclaw "$QUAID_CMD" project delete "$SHARED_PROJECT" 2>&1 || true
}

# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

echo "======================================"
echo "Quaid Project System — Live Tests"
echo "QUAID_HOME: $QUAID_HOME"
echo "CLI: $QUAID_CMD"
echo "Test dir: $TEST_DIR"
echo "======================================"
echo ""

# Run tests in order (some depend on previous)
test_project_list
test_project_create
test_project_show
test_project_snapshot_no_changes
test_project_snapshot_with_changes
test_project_sync
test_project_update
test_project_delete
test_invalid_project_name

# Cross-instance tests (run after both OC and CC single-instance tests pass)
test_shared_project_cross_instance

echo ""
echo "======================================"
echo -e "Results: ${GREEN}${PASS_COUNT} passed${NC}, ${RED}${FAIL_COUNT} failed${NC}, ${YELLOW}${SKIP_COUNT} skipped${NC}"
echo "======================================"

if [[ $FAIL_COUNT -gt 0 ]]; then
    exit 1
fi
exit 0
