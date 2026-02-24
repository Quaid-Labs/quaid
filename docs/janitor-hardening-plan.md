# Janitor Hardening Plan

Scope: improve both benchmark stability (repeatable throughput/latency) and live runtime health (resilience, bounded failure impact).

## Primary Risks Observed

1. Long-running LLM maintenance batches can stall throughput and amplify tail latency.
2. Gateway/service instability can cascade into janitor retries and backlog growth.
3. Some maintenance outcomes are difficult to verify deterministically (contradiction/dedup/decay).
4. Backlog pressure can build without clear per-stage budget enforcement.
5. Failure modes are not always isolated by task class (memory vs non-memory maintenance).

## Hardening Priorities

### P0: Throughput and Safety Guardrails

1. Per-task wall-clock budget + timeout enforcement.
   - Enforce a max runtime per maintenance routine to prevent one routine from monopolizing the run.
2. Adaptive batch sizing.
   - Dynamically shrink LLM batch size when timeout/error rate rises; expand when healthy.
3. Retry policy normalization.
   - Use bounded retries with jitter for transient LLM/gateway errors; fail-fast for permanent schema/data errors.
4. Task isolation by class.
   - Keep memory pipeline fail-fast semantics, but continue non-memory tasks independently.
5. Backlog protection.
   - Cap processed items per run per task; carry remainder forward explicitly.

### P1: Determinism and Benchmark Health

1. Deterministic janitor fixture suite.
   - Seed known contradiction/dedup/decay cases and assert exact post-run statuses.
2. Run-to-run drift checks.
   - Compare janitor outputs across two runs on same fixture DB; fail if unexpected drift appears.
3. Benchmark-mode behavior contract.
   - Keep transient LLM batch failures non-fatal for benchmark mode while surfacing structured warnings.
4. Task duration histograms.
   - Capture per-task p50/p95/p99 duration and item throughput for benchmark regressions.

### P2: Live System Resilience

1. Janitor heartbeat + phase checkpoints.
   - Persist progress checkpoints so interrupted runs resume safely without replaying completed phases.
2. Dead-letter queues for malformed records.
   - Route unrecoverable items to task-specific dead-letter storage for later analysis.
3. Leader lock hardening.
   - Ensure single janitor leader and stale-lock recovery with owner PID + heartbeat timestamp.
4. Notification degradation policy.
   - Never block janitor completion on notification failures.

## Concrete Next Implementations

1. Evaluate and increase safe janitor parallelism for benchmark throughput.
   - Profile task-level critical path and identify routines eligible for parallel execution.
   - Add bounded worker-pool execution for independent maintenance routines with deterministic ordering constraints.
   - Add correctness gates: no regression in pending/approved invariants, no cross-owner contamination, no duplicate run-record writes.
2. Evaluate maintenance sub-task parallelism inside datastore routines.
   - Focus on review/dedup/contradiction/decay batch concurrency where reads are independent.
   - Keep write phases serialized where needed to preserve sqlite integrity and deterministic outcomes.
3. Add per-task runtime budgets + structured timeout result reporting in janitor metrics.
4. Add deterministic contradiction/dedup/decay fixture E2E checks in `pre-benchmark`.
5. Add explicit backlog carryover counters in `janitor_runs` and summary notifications.
6. Add benchmark report section: task durations, item throughput, timeout/retry counts.

## Implemented In This Pass

1. Pending-review coverage guard in datastore routine (`review_pending_memories`)
   - Tracks decision coverage across `id` and `merge_ids`.
   - Runs a targeted retry for omitted IDs.
   - Hard-fails with explicit missing IDs if coverage is still incomplete.
   - Emits `review_coverage_ratio` and carryover values.
2. Deterministic regression coverage test
   - Added test for incomplete first-pass decisions that are recovered by targeted retry.
   - Added test for unrecoverable incomplete decisions that hard-fail.
3. Janitor stage hardening hooks
   - Stage item-cap plumbing (`max_items`) from janitor orchestrator into datastore maintenance routines.
   - Carryover counters surfaced from datastore routines and included in janitor metrics/report.
   - Stage budget reporting (budget vs elapsed) with warnings for overruns.
4. Janitor checkpoint/heartbeat scaffolding
   - Per-task checkpoint file with heartbeat/current stage/completed stages.
   - Resume-aware skip for already-completed memory stages in `task=all`.
   - Checkpoint status persisted as running/completed/failed.
5. Run-record enrichment (`janitor_runs`)
   - Added structured JSON columns for skipped tasks, carryover, stage budgets, checkpoint path, task summary.
6. Benchmark smoke blocker fix + guardrails
   - Fixed contradictions carryover query to use `contradictions.status='pending'` (schema-correct; removes `review_status` crash).
   - Added regression test for contradictions carryover query path.
   - Added benchmark-mode review gate in janitor Task 2: fail memory pipeline immediately when review coverage is incomplete or review carryover is non-zero.
   - Added unit tests covering benchmark-mode review gate trigger/no-trigger behavior.
7. LLM batch parallelism for datastore-owned maintenance reviews
   - Added bounded parallel batch dispatch helper in `datastore/memorydb/maintenance_ops.py`.
   - Parallelized Opus-heavy routines while preserving deterministic apply order:
     - `review_pending_memories`
     - `resolve_contradictions_with_opus`
     - `review_dedup_rejections`
     - `review_decayed_memories`
   - Added env controls:
     - `QUAID_JANITOR_LLM_PARALLELISM`
     - `QUAID_JANITOR_LLM_PARALLELISM_<TASK>`
   - Defaults: `1` in normal mode, `2` in benchmark mode.

## E2E Coverage Mapping

1. `--suite blocker`
   - lifecycle + memory continuity + notification policy
2. `--suite pre-benchmark`
   - blocker + ingest + janitor + benchmark-mode fail-fast review probe
   - seeded contradiction fixture assertion (must transition out of pending)
   - seeded dedup + decay-review fixture assertions (must transition out of unreviewed/pending)
   - seeded multi-owner isolation assertion (owner-scoped memories must not collapse across owners)
   - seeded RAG anchor assertion (anchor text must appear in doc_chunks after janitor)
3. `--suite nightly`
   - full + resilience/load scenarios (restart, pressure, migration fixtures)
