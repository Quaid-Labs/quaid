# Quaid Testing Infrastructure

This document defines the current test stack, execution commands, and pass/fail rubric.

## Goals
- Keep local/PR validation deterministic and fast.
- Keep host/runtime behavior checked via the live-test workflow.
- Separate model-drift risk from blocking correctness checks.
- Prevent single hanging test from stalling the entire suite.
- Do not treat the legacy e2e lane as release truth.

## Test Layers

### 1) Python Unit Tier (blocking)
- Purpose: validate routing, storage, recall, graph behavior without live LLM drift.
- Notes:
  - Default `pytest` run executes this tier only.
  - Integration and historical regression packs are marker-gated.
  - Suite now uses `faulthandler_timeout` diagnostics for stall traces.

### 2) Python Integration Tier (opt-in)
- Marker: `integration`
- Includes cross-module/process tests (for example adapter/daemon orchestration).

### 3) Python Regression Tier (opt-in)
- Marker: `regression`
- Includes larger historical packs (chunk/batch regression suites, golden recall).
- Kept for deep validation, removed from default fast loop.

### 4) TypeScript Integration (blocking)
- Deterministic integration suite run via `npm run test:integration`.
- Includes delayed-request lifecycle coverage (queue -> flush/surface -> resolve/clear) for adapter-managed janitor escalation flow.

### 4b) Adapter-Specific Partition (OpenClaw, expandable)
- Purpose: keep provider/host adapter tests isolated from core-memory tests.
- Current partition:
  - Python: `python3 scripts/run_pytests.py --mode adapter_openclaw`
  - TypeScript: `npm run test:adapter:openclaw:ts`
- Combined command:
  - `npm run test:adapter:openclaw`
- Notes:
  - Python partition is marker-based (`pytest.mark.adapter_openclaw`).
  - Selection runs with `-m adapter_openclaw`, so files can contain mixed tests while
    only adapter-marked cases execute in the adapter lane.
  - Future adapters should add parallel suites (`adapter_codex`, `adapter_claude_code`, etc.)
    without mixing assertions into core-memory tiers.

### 5) Build/Syntax (blocking)
- Purpose: catch syntax/build breakage early.
- Checks:
  - Runtime build (`npm run build:runtime`)
  - Boundary import check (`npm run check:boundaries`)
  - TypeScript lint (`npm run lint:ts`)
  - Python lint (`npm run lint:py`)
  - Python compile check (`compileall`)
  - Node syntax checks on key JS runtime files

### 6) Live Validation (authoritative)
- Purpose: validate real install, host integration, extraction, retrieval, and maintenance behavior on the supported hosts.
- Source of truth:
  - `modules/quaid/tests/livetest/livetest-guide/`
- Notes:
  - Live validation is the release-truth lane for OpenClaw, Claude Code, and Codex.
  - Compatibility rows are written from accepted live clears, not from legacy e2e automation.

### 7) Legacy E2E (deprecated)
- `modules/quaid/scripts/run-quaid-e2e.sh`
- `modules/quaid/scripts/run-quaid-e2e-matrix.sh`
- Status:
  - Deprecated and intentionally not part of `npm run test:all:full`
  - Kept only as historical reference until removed or rebuilt

## Standard Commands

### Quick combined suite (recommended local default)
```bash
cd <test-workspace>/modules/quaid
npm run test:all
```

### Python unit-only (default pytest mode)
```bash
cd <test-workspace>/modules/quaid
python3 -m pytest -q
```

### Python integration-only
```bash
cd <test-workspace>/modules/quaid
python3 -m pytest -q -o addopts= -m integration
```

### Python regression-only
```bash
cd <test-workspace>/modules/quaid
python3 -m pytest -q -o addopts= -m regression
```

### Parallel isolated Python runner (recommended for CI/local)
```bash
cd <test-workspace>/modules/quaid
python3 scripts/run_pytests.py --mode unit --workers 4 --timeout 120
```

### OpenClaw adapter-only partition
```bash
cd <test-workspace>/modules/quaid
npm run test:adapter:openclaw
```

### Coverage (TypeScript + Python)
```bash
cd <test-workspace>/modules/quaid
npm run test:coverage:all
```

Python-only coverage (fast profile):
```bash
cd <test-workspace>/modules/quaid
npm run test:coverage:py
```

Python-only coverage (full profile):
```bash
cd <test-workspace>/modules/quaid
npm run test:coverage:py:full
```

### Full combined suite
```bash
cd <test-workspace>/modules/quaid
npm run test:all:full
```
This no longer runs legacy e2e automation. Use the live-test guide separately for host validation.

## Projects System Live Testing

Full protocol in `operations/projects-testing.md`. Run order:

1. **OC CRUD** — create, register doc, search, show, delete
2. **CC CRUD** (local testbench) — same sequence against CC instance
3. **Cross-platform** — global registry check, CC registers doc to OC project, CC reads it back

### Quick reference
```bash
# OC
export QUAID_HOME=~/.quaid QUAID_VISIBLE_HOME=~/quaid QUAID_INSTANCE=openclaw
quaid project create <name> && quaid project list

# CC
export QUAID_HOME=~/.quaid QUAID_VISIBLE_HOME=~/quaid QUAID_INSTANCE=claude-code
quaid project create <name> && quaid project list

# Global (either machine)
quaid global-registry list
```

Pass criteria: OC CRUD clean, CC CRUD clean, global registry shows both instances, CC can register and search docs in OC-owned project.

---

## Pass/Fail Rubric

### Blocking pass criteria
- Python unit tier passes.
- TypeScript integration suite passes.
- Build/syntax checks pass.

### Extended pass criteria
- Python integration tier passes.
- Python regression tier passes.

### Live validation pass criteria
- The current live suite in `modules/quaid/tests/livetest/livetest-guide/` passes on the supported hosts.
- The cleared runtime SHA is recorded and matched against the intended release target.
- Compatibility rows are written from that accepted clear.

## Determinism Policy
- Blocking tests must not depend on live model text exactness.
- Live provider tests should assert invariants (pipeline success, recorded actions), not exact extraction phrasing.

## Coverage Policy
- TypeScript coverage enforces minimum thresholds in Vitest config.
- Initial threshold floor is intentionally conservative and should ratchet upward over time.
- Python coverage runs in an isolated venv (`scripts/run-python-coverage.sh`) and reports source-only coverage (`--omit='tests/*'`).

## Bootstrap Ownership
- Runtime/bootstrap orchestration remains in the machine-local bootstrap repo (path set via `QUAID_BOOTSTRAP_ROOT`).
- Legacy e2e entrypoints remain in `modules/quaid/scripts` only as historical reference until removal or replacement.
- `paths.devRoot` must not store local secrets or host-specific credential material.
