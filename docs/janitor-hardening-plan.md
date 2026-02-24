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

1. Add per-task runtime budgets + structured timeout result reporting in janitor metrics.
2. Add deterministic contradiction/dedup/decay fixture E2E checks in `pre-benchmark`.
3. Add explicit backlog carryover counters in `janitor_runs` and summary notifications.
4. Add benchmark report section: task durations, item throughput, timeout/retry counts.

## E2E Coverage Mapping

1. `--suite blocker`
   - lifecycle + memory continuity + notification policy
2. `--suite pre-benchmark`
   - blocker + ingest + janitor + deterministic maintenance fixtures (next)
3. `--suite nightly`
   - full + resilience/load scenarios (restart, pressure, migration fixtures)
