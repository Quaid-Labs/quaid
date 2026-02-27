# Quaid Testing Infrastructure

This document defines the current test stack, execution commands, and pass/fail rubric.

## Goals
- Keep local/PR validation deterministic and fast.
- Keep live-provider behavior checked via e2e smoke flows.
- Separate model-drift risk from blocking correctness checks.
- Prevent single hanging test from stalling the entire suite.

## Test Layers

### 1) Python Unit Tier (blocking)
- Purpose: validate routing, storage, recall, graph behavior without live LLM drift.
- Notes:
  - Default `pytest` run executes this tier only.
  - Integration and historical regression packs are marker-gated.
  - Suite now uses `faulthandler_timeout` diagnostics for stall traces.

### 2) Python Integration Tier (opt-in)
- Marker: `integration`
- Includes cross-module/process tests (for example MCP process orchestration).

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
  - Python compile check (`compileall`)
  - Node syntax checks on key JS runtime files

### 6) E2E Runtime (smoke)
- Purpose: validate real bootstrap/gateway/runtime orchestration and janitor path.
- Scripts:
  - `modules/quaid/scripts/run-quaid-e2e.sh`
  - `modules/quaid/scripts/run-quaid-e2e-matrix.sh`
- Scope:
  - Gateway stop/start, e2e workspace bootstrap, integration tests, janitor run, restore to `~/quaid/test`.
- Bootstrap coupling:
  - E2E runners live in `modules/quaid/scripts` and call bootstrap scripts via `QUAID_BOOTSTRAP_ROOT` (default `~/quaid/bootstrap`).
  - Optional local env file: `modules/quaid/.env.e2e` (template: `modules/quaid/scripts/e2e.env.example`).
- Notification safety:
  - E2E should run with Quaid notifications set to `quiet` to prevent Telegram/DM spam during automation.
  - Raise notification level only when explicitly testing notification UX.
- Auth/key model:
  - In host mode (OpenClaw), Quaid uses gateway-managed auth profiles.
  - Standalone MCP/CLI tests can use local env keys (`.env`) when needed.

## Standard Commands

### Quick combined suite (recommended local default)
```bash
cd ~/quaid/test/modules/quaid
npm run test:all
```

### Python unit-only (default pytest mode)
```bash
cd ~/quaid/test/modules/quaid
python3 -m pytest -q
```

### Python integration-only
```bash
cd ~/quaid/test/modules/quaid
python3 -m pytest -q -o addopts= -m integration
```

### Python regression-only
```bash
cd ~/quaid/test/modules/quaid
python3 -m pytest -q -o addopts= -m regression
```

### Parallel isolated Python runner (recommended for CI/local)
```bash
cd ~/quaid/test/modules/quaid
python3 scripts/run_pytests.py --mode unit --workers 4 --timeout 120
```

### OpenClaw adapter-only partition
```bash
cd ~/quaid/test/modules/quaid
npm run test:adapter:openclaw
```

### Coverage (TypeScript + Python)
```bash
cd ~/quaid/test/modules/quaid
npm run test:coverage:all
```

Python-only coverage (fast profile):
```bash
cd ~/quaid/test/modules/quaid
npm run test:coverage:py
```

Python-only coverage (full profile):
```bash
cd ~/quaid/test/modules/quaid
npm run test:coverage:py:full
```

### Full combined suite
```bash
cd ~/quaid/test/modules/quaid
npm run test:all:full
```

### E2E single provider
```bash
cd ~/quaid/test/modules/quaid
./scripts/run-quaid-e2e.sh --auth-path openai-oauth
```

### E2E with explicit notification level
```bash
cd ~/quaid/test/modules/quaid
./scripts/run-quaid-e2e.sh --auth-path openai-oauth --notify-level quiet
```

### E2E provider matrix
```bash
cd ~/quaid/test/modules/quaid
./scripts/run-quaid-e2e-matrix.sh --paths openai-oauth,openai-api,anthropic-api -- --skip-janitor
```
Per-path timeout defaults to `1200` seconds. Override with `QUAID_E2E_PATH_TIMEOUT_SEC=<seconds>` when needed.

Default full-suite matrix lanes intentionally exclude `anthropic-oauth` (known unstable refresh behavior).
Current default lanes in `run-all-tests.sh`:
- `openai-oauth`
- `openai-api`
- `anthropic-api`

Override with:
```bash
QUAID_E2E_PATHS="openai-oauth,openai-api,anthropic-api" npm run test:all:full
```

## Pass/Fail Rubric

### Blocking pass criteria
- Python unit tier passes.
- TypeScript integration suite passes.
- Build/syntax checks pass.

### Extended pass criteria
- Python integration tier passes.
- Python regression tier passes.

### E2E pass criteria
- Bootstrap to `~/quaid/e2e-test` succeeds.
- Integration tests in e2e workspace pass.
- Janitor run exits successfully.
- Janitor verification passes:
  - `janitor_runs` exists.
  - New run row recorded.
  - Run status is `completed`.
  - Apply mode shows observable work (`memories_processed` or `actions_taken` or status-bucket deltas).
- Cleanup/restore succeeds:
  - `~/quaid/e2e-test` removed on success (unless `--keep-on-success`).
  - Workspace restored to `~/quaid/test`.
  - Gateway health recovered.
- Summary integrity guard:
  - E2E summary must not report `status=success` while any stage remains `running`.
  - If a stage is still `running` at summary time, runner marks the run failed with `failure.reason=incomplete_stage_status`.

### Janitor E2E mode guidance
- Use `--janitor-dry-run` for non-interactive hardening sweeps.
- Reason: apply mode can legitimately wait on approval-policy `ask` requests, which is not a regression by itself.
- Use apply mode for release/benchmark validation only when approval policy is explicitly controlled.

## Determinism Policy
- Blocking tests must not depend on live model text exactness.
- Live provider tests should assert invariants (pipeline success, recorded actions), not exact extraction phrasing.

## Coverage Policy
- TypeScript coverage enforces minimum thresholds in Vitest config.
- Initial threshold floor is intentionally conservative and should ratchet upward over time.
- Python coverage runs in an isolated venv (`scripts/run-python-coverage.sh`) and reports source-only coverage (`--omit='tests/*'`).

## Bootstrap Ownership
- Runtime/bootstrap orchestration remains in `~/quaid/bootstrap` (machine-local operational repo).
- Quaid now keeps E2E entrypoints in `modules/quaid/scripts` so full test runs can execute from dev/test directly.
- `~/quaid/dev` must not store local secrets or host-specific credential material.
