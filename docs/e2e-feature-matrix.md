# Quaid E2E Feature Matrix

This matrix tracks what `modules/quaid/scripts/run-quaid-e2e.sh` validates today and what should be expanded next.
For staged rollout and admission criteria, see `docs/e2e-roadmap.md`.

## Automated Now

1. Gateway/model smoke (`suite=smoke|full|core`)
   - Verifies gateway can complete a real model call.
2. Session timeout manager + command signal wiring (`suite=integration|full|core`)
   - Runs `session-timeout-manager` and chat-flow integration tests.
3. Live lifecycle hook paths (`suite=live|full|core`)
   - Validates timeout extraction event.
   - Validates `/compact` => `CompactionSignal`.
   - Validates `/reset` + `/new` => `ResetSignal`.
   - Asserts no stale `.processing` signal claims remain (after a short drain window).
   - Asserts no internal extraction/ranking prompts leak into `logs/quaid/session-messages`.
4. Memory regression flow (`suite=memory|full|core`)
   - Writes parent facts, runs `/compact` and `/new`, verifies recall still returns expected names.
   - Fails if answer includes stale/low-confidence hedge language.
5. Notification matrix (`suite=notify|full`)
   - Verifies behavior across `quiet`, `normal`, `debug`.
   - Detects fatal notify CLI wiring errors.
6. Ingestion stress (`suite=ingest|full|core`)
   - Injects mixed fact/journal/snippet/project content, runs compaction path, verifies extraction/project activity.
7. Janitor end-to-end (`suite=janitor|full|core`)
   - Optional seed path (`suite=seed` or implied by `full/core`) populates pending maintenance workload.
   - Runs janitor apply/dry-run and verifies `janitor_runs` + key maintenance side effects.
   - `suite=pre-benchmark` additionally enforces graduation invariant (`pending=0`, `approved=0`) with leftover-ID failure preview.
8. Failure diagnostics (all suites)
   - On error, runner prints pending signals, timeout logs, notify-worker tail, gateway status, and gateway logs.
9. Machine-readable outcome summary (all suites)
   - Writes run summary to `/tmp/quaid-e2e-last-summary.json` (override with `QUAID_E2E_SUMMARY_PATH`).
   - Appends summary history to `/tmp/quaid-e2e-summary-history.jsonl` (override with `QUAID_E2E_SUMMARY_HISTORY_PATH`).
   - Includes overall status, duration, suite/profile, stage-by-stage pass/skip/fail state, failure metadata, and runtime-budget status.
   - `modules/quaid/scripts/e2e-summary-check.py` validates status/duration and emits compact CI-friendly output.
   - `modules/quaid/scripts/e2e-budget-tune.py` recommends runtime budgets from history percentiles.
10. Runtime budget presets
   - `--runtime-budget-profile auto|off|quick|deep` controls wall-clock budget gating.
   - `--runtime-budget-seconds` overrides budget in seconds.
   - Budget overrun fails run with explicit `runtime_budget_exceeded` failure reason in summary.
11. Bootstrap collision recovery
   - If worktree bootstrap fails with a workspace "already exists" collision after wipe, runner performs one forced cleanup + retry automatically.
12. Resilience and concurrency (`suite=resilience` or `suite=nightly`)
   - Validates recovery after forced gateway restart mid-session.
   - Validates live turns under janitor pressure.
   - Validates cross-session interleaving under janitor pressure; cursor checks enforce identity when cursor files are present.
   - Validates gateway restart during janitor apply-write path (`cleanup`) still records completed `janitor_runs`.
   - Validates registry/index drift recovery (`doc_registry.last_indexed_at` refresh on seeded RAG doc).
   - Validates project-updater event consumption under concurrent live-turn pressure when `projects.definitions` are configured.
   - Supports bounded soak loops via `--resilience-loops` (nightly defaults to 2 iterations).
   - Nightly also runs bounded janitor-stage stress passes (`--stage-item-cap 2`, repeated apply runs) and checks `janitor_runs` integrity.
   - Nightly profile stratification: `--nightly-profile quick|deep` adjusts minimum resilience loops and janitor stress passes.
   - Includes failure-injection probe (intentional malformed `/v1/responses` payload + immediate recovery turn).
   - Includes auth-failure injection probe (intentional invalid bearer token request + immediate recovery turn).
   - Includes gateway-down injection probe (forced gateway stop, failed request, restart, and recovery turn).
   - Includes source-mapping drift fixture coverage via project-updater pressure path.
   - Includes carryover trend assertion across repeated janitor stress passes.

## Recommended Next Additions

1. Nightly profile stratification
   - Add explicit runtime budget presets per profile and expose expected wall-clock bounds in output.
2. Runtime-budget presets
   - Encode quick/deep expected wall-clock bounds and fail when exceeded.
3. Stage-specific budget tuning
   - Adjust quick/deep runtime budgets from observed nightly history per environment.

## Runner Modes

1. `--suite core`
   - Benchmark-ready correctness path: smoke + integration + live + memory + ingest + janitor (+ seed), skips notify matrix.
2. `--suite blocker`
   - High-signal lifecycle path: integration + live + memory + notify.
3. `--suite pre-benchmark`
   - Blocker coverage plus ingest + janitor (+ seed) for benchmark checkpoint readiness.
4. `--suite nightly`
   - Alias of `full`.
5. `--quick-bootstrap`
   - Skip OpenClaw source refresh/install for faster local loops.
6. `--reuse-workspace`
   - Reuse existing `~/quaid/e2e-test` when possible; fallback to clean bootstrap on mismatch.
7. `--runtime-budget-profile`, `--runtime-budget-seconds`
   - Enable explicit runtime regression gates for nightly and long suites.
