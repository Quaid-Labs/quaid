# Benchmark Operations Guide

This guide documents the handoff between `dev` hardening and benchmark execution.
It focuses on deterministic checkpoint cuts and janitor/E2E gates that must pass before smoke/full benchmark runs.

## Ownership Boundaries

1. `dev` owns code + correctness fixes.
2. `benchmark` owns checkpoint cutting + benchmark orchestration scripts.
3. E2E in `dev` is the admission gate before requesting a new checkpoint cut.

Canonical checkpoint script location:
- `~/quaid/benchmark/scripts/cut-benchmark-checkpoint.sh`

## Required Pre-Cut Gates (Run In `~/quaid/dev/modules/quaid`)

1. Boundary check:
```bash
python3 scripts/check-boundaries.py
```
2. Janitor benchmark lane:
```bash
bash scripts/run-quaid-e2e.sh --suite janitor-parallel-bench --quick-bootstrap --reuse-workspace --janitor-timeout 420
```
3. Targeted janitor tests:
```bash
PYTHONPATH=. pytest -q tests/test_janitor_benchmark_review_gate.py tests/test_maintenance_parallelism.py
```

Expected `janitor-parallel-bench` artifacts:
- `/tmp/quaid-e2e-last-summary.json` with `"status": "success"`
- `/tmp/quaid-e2e-janitor-parallel-bench.json` with:
  - `seeded_counts_after.* == 0`
  - `janitor_stats_errors == []`
  - threshold block present

## Cutting A Checkpoint (Run In `~/quaid/benchmark`)

Dry run:
```bash
./scripts/cut-benchmark-checkpoint.sh --dry-run
```

Actual cut:
```bash
./scripts/cut-benchmark-checkpoint.sh
```

Output metadata:
- `~/quaid/benchmark-checkpoint/.quaid-checkpoint-meta`
  - `source_branch`
  - `source_sha`
  - `cut_at_utc`

## Failure Triage

If E2E `janitor-parallel-bench` fails:
1. Read `/tmp/quaid-e2e-last-summary.json` `failure.stage` + `failure.reason`.
2. Check janitor tail from E2E diagnostics (`janitor stdout/stderr tail`).
3. Check `/tmp/quaid-e2e-janitor-parallel-bench.json`:
   - threshold violations
   - seeded carryover counts
   - `janitor_stats_errors`
4. Fix in `dev`, rerun the same lane, then recut checkpoint from `benchmark`.

## Budget Tuning (Current Baseline)

History on this machine currently has no successful `nightly` samples, so tuned budgets are not yet reliable for nightly lanes.
For `janitor-parallel-bench` (3 successful samples), use these provisional caps:

1. Total runtime budget: `479s`
2. Stage budget `bootstrap`: `48s`
3. Stage budget `janitor`: `394s`

Example:
```bash
QUAID_E2E_RUNTIME_BUDGET_SECONDS=479 \
QUAID_E2E_STAGE_BUDGETS_JSON='{"bootstrap":48,"janitor":394}' \
bash modules/quaid/scripts/run-quaid-e2e.sh --suite janitor-parallel-bench
```

Regenerate recommendations any time sample count changes:
```bash
cd ~/quaid/dev/modules/quaid
python3 scripts/e2e-budget-tune.py --suite janitor-parallel-bench --min-samples 3
python3 scripts/e2e-budget-tune.py --suite janitor-parallel-bench --stage bootstrap --min-samples 3
python3 scripts/e2e-budget-tune.py --suite janitor-parallel-bench --stage janitor --min-samples 3
```

## Notes

1. `janitor-parallel-bench` intentionally validates deterministic benchmark invariants (carryover depletion + threshold gates), not every semantic status-delta transition.
2. Benchmark-mode janitor now includes a pre-graduate catch-up review pass to prevent same-cycle pending leakage.
