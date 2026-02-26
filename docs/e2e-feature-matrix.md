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
   - Optional strict mode: `QUAID_E2E_NOTIFY_REQUIRE_DELIVERY=true` requires active channel context and successful normal/debug sends.
6. Ingestion stress (`suite=ingest|full|core`)
   - Injects mixed fact/journal/snippet/project content, runs compaction path, verifies extraction/project activity.
7. Janitor end-to-end (`suite=janitor|full|core`)
   - Optional seed path (`suite=seed` or implied by `full/core`) populates pending maintenance workload.
   - Seed path bootstraps memorydb schema before fixture insertion so clean/new workspaces do not fail with missing `nodes` table.
   - Runs janitor apply/dry-run and verifies `janitor_runs` + key maintenance side effects.
   - `suite=pre-benchmark` additionally enforces graduation invariant (`pending=0`, `approved=0`) with leftover-ID failure preview.
8. Failure diagnostics (all suites)
   - On error, runner prints pending signals, timeout logs, notify-worker tail, gateway status, and gateway logs.
9. Machine-readable outcome summary (all suites)
   - Writes run summary to `/tmp/quaid-e2e-last-summary.json` (override with `QUAID_E2E_SUMMARY_PATH`).
   - Appends summary history to `/tmp/quaid-e2e-summary-history.jsonl` (override with `QUAID_E2E_SUMMARY_HISTORY_PATH`).
   - Includes overall status, duration, stage-by-stage pass/skip/fail state, per-stage durations, failure metadata, and runtime-budget status.
   - `modules/quaid/scripts/e2e-summary-check.py` validates status/duration and emits compact CI-friendly output.
   - `modules/quaid/scripts/e2e-budget-tune.py` recommends runtime budgets from history percentiles (`--stage` for per-stage tuning).
   - Nightly mode also emits `/tmp/quaid-e2e-budget-recommendation.json` (override `QUAID_E2E_BUDGET_RECOMMENDATION_PATH`) from accumulated history.
10. Runtime budget presets
   - `--runtime-budget-profile auto|off|quick|deep` controls wall-clock budget gating.
   - `--runtime-budget-seconds` overrides budget in seconds.
   - Budget overrun fails run with explicit `runtime_budget_exceeded` failure reason in summary.
   - `QUAID_E2E_STAGE_BUDGETS_JSON='{"bootstrap":120,"notify_matrix":300}'` enables per-stage duration gates.
   - Nightly auto-tuning can populate stage budgets from history (`QUAID_E2E_AUTO_STAGE_BUDGETS=true`).
11. Bootstrap collision recovery
   - If worktree bootstrap fails with a workspace "already exists" collision after wipe, runner performs one forced cleanup + retry automatically.
12. Workspace teardown reliability
   - Successful runs now retry workspace deletion (`~/quaid/e2e-test`) to handle transient file-creation races during shutdown.
13. Resilience and concurrency (`suite=resilience` or `suite=nightly`)
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
   - Includes timeout injection probe (non-routable upstream request with short timeout + recovery turn).
   - Includes timeout injection + recovery while janitor pressure probe is active.
   - Includes malformed-upstream-response probe (invalid JSON response parse failure + recovery turn).
   - Includes source-mapping drift fixture coverage via project-updater pressure path.
   - Includes carryover trend assertion across repeated janitor stress passes.
14. Janitor parallel benchmark reporting (`suite=janitor-parallel-bench`)
   - Emits `/tmp/quaid-e2e-janitor-parallel-bench.json` (override `QUAID_E2E_JANITOR_PARALLEL_REPORT_PATH`).
   - Captures seeded before/after carryover signals plus janitor task duration/change/error data from `janitor-stats.json`.
   - Enforces threshold gates for benchmark automation:
     - `QUAID_E2E_JPB_MAX_ERRORS` (default `0`)
     - `QUAID_E2E_JPB_MAX_WARNINGS` (default `-1`, disabled)
     - `QUAID_E2E_JPB_MAX_CONTRADICTIONS_PENDING_AFTER` (default `0`)
     - `QUAID_E2E_JPB_MAX_DEDUP_UNREVIEWED_AFTER` (default `0`)
     - `QUAID_E2E_JPB_MAX_DECAY_PENDING_AFTER` (default `0`)
     - `QUAID_E2E_JPB_MAX_STAGING_EVENTS_AFTER` (default `0`)

## Recommended Next Additions

1. Stage-specific budget rollout
   - Enforce stage-specific budget gates in nightly after collecting enough per-stage history.
2. CI artifact plumbing
   - Upload nightly budget recommendation JSON and history snapshot as CI artifacts for long-horizon tuning.
3. Timeout-lane pressure assertions
   - Add explicit regression checks for timeout-failure behavior under active janitor pressure.

## Runner Modes

1. `--suite core`
   - Benchmark-ready correctness path: smoke + integration + live + memory + ingest + janitor (+ seed), skips notify matrix.
2. `--suite blocker`
   - High-signal lifecycle path: integration + live + memory + notify.
3. `--suite pre-benchmark`
   - Blocker coverage plus ingest + janitor (+ seed) for benchmark checkpoint readiness.
4. `--suite nightly`
   - Alias of `full`.
5. `--suite nightly-strict-notify`
   - Nightly/full coverage with strict notification-delivery enforcement enabled.
6. `--suite janitor-parallel-bench`
   - Janitor-focused benchmark lane (seed + pre-benchmark guards + benchmark-mode parallel LLM settings).
7. `--quick-bootstrap`
   - Skip OpenClaw source refresh/install for faster local loops.
8. `--reuse-workspace`
   - Reuse existing `~/quaid/e2e-test` when possible; fallback to clean bootstrap on mismatch.
9. `--runtime-budget-profile`, `--runtime-budget-seconds`
   - Enable explicit runtime regression gates for nightly and long suites.
