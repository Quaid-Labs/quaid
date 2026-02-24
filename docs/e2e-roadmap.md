# Quaid E2E Roadmap

This roadmap stages E2E expansion by risk and release impact. It is designed to run in an execution loop: implement, run, fix, repeat.

## E2E Admission Rule

Only add an E2E test when the behavior depends on cross-boundary integration that unit/integration tests cannot credibly validate in isolation.

A candidate must include at least one of:
1. Gateway/runtime process behavior (hook loading, restart behavior, CLI wiring, auth/runtime config).
2. Multi-subsystem orchestration (adapter -> core lifecycle -> datastore maintenance -> notifications).
3. Persistent state interactions across turns/sessions (cursor replay protection, queued signals, janitor side effects).
4. End-user observable behavior requiring full pipeline execution (real command hook path, timeout extraction path, notification delivery policy).

If a case can be covered with deterministic in-process unit tests alone, keep it out of E2E.

## Stage 1: Blocker

Goal: catch regressions that break core lifecycle behavior (commands, timeout, extraction, memory continuity, notifications).

Runner:
- `modules/quaid/scripts/run-quaid-e2e.sh --suite blocker`

Coverage requirements:
1. Command/timeout lifecycle hook parity
   - `/compact`, `/reset`, `/new`, timeout all trigger expected extraction signals.
2. Session cursor progression integrity
   - Cursor file is written for active session and advances correctly (no replay-only processing).
3. Memory continuity flow
   - Facts survive `/compact` + `/new` and are recallable without stale/low-confidence hedge language.
4. Notification policy matrix
   - `quiet|normal|debug` levels respected; no fatal notify-worker CLI errors.
5. Internal prompt non-leakage
   - Extraction/ranking prompts do not leak into persisted user session message logs.

## Stage 2: Pre-Benchmark

Goal: validate benchmark-relevant core behavior and maintenance workflows before checkpoint cut.

Runner:
- `modules/quaid/scripts/run-quaid-e2e.sh --suite pre-benchmark`

Coverage requirements:
1. Stage 1 blocker suite (full pass).
2. Ingestion stress path
   - Fact/journal/snippet/project paths produce expected extraction + project activity.
3. Janitor end-to-end
   - Seeded maintenance workload processed; janitor run recorded and key side effects present.
   - Pre-benchmark invariant check: after janitor apply, `pending==0` and `approved==0` (fails with leftover ID preview).
4. Janitor task safety
   - Benchmark-mode fail-fast gate is implemented in janitor: review stage fails immediately when coverage < 100% or review carryover > 0.
   - `pre-benchmark` now probes this path directly via benchmark-mode dry-run review + forced stage cap and requires non-zero exit.
5. Deterministic maintenance outcomes
   - Seeded contradiction fixture transition assertion is implemented.
   - Seeded dedup/decay fixture transition assertions are implemented.

## Stage 3: Nightly

Goal: broad system hardening with heavier, slower, multi-boundary checks.

Runner:
- `modules/quaid/scripts/run-quaid-e2e.sh --suite nightly`

Coverage requirements:
1. Full suite pass (`nightly` currently aliases `full`).
2. Multi-owner isolation checks.
   - Seeded owner-scoped duplicate fixture now asserts janitor does not collapse distinct owners.
3. RAG integrity assertions with seeded anchors.
   - Added seeded anchor assertion: pre-benchmark now requires `doc_chunks` to contain the seeded RAG anchor text.
4. Project artifact assertions beyond queue movement.
   - Added PROJECT.md artifact assertion via docs update log delta (`projects/quaid/PROJECT.md`).
5. Resilience checks (gateway restart mid-flow, concurrency/backpressure, migration fixtures).
   - Added gateway restart mid-session resilience check (nightly/`resilience` suite path).
   - Added concurrent pressure probe (janitor review dry-run while live turn executes).
   - Added cross-session concurrency matrix probe (two session IDs interleaved under janitor pressure; warns when cursor files are unavailable in non-live suites).
   - Added gateway restart during janitor run-write probe (cleanup apply run must still record a completed `janitor_runs` row).
   - Added migration-fixture resilience probe (legacy `janitor_runs` schema auto-migrates before run write).
   - Added registry/index drift fixture probe (`doc_registry.last_indexed_at` for seeded doc must refresh after janitor RAG).
   - Added registry/doc-chunk path-mismatch drift probe (both relative and absolute registry paths must refresh).
   - Added source-mapping drift fixture (`doc_registry.source_files` includes missing path) exercised under project-updater pressure path when projects are configured.
   - Added bounded soak mode for resilience checks (`--resilience-loops`, nightly defaults to 2 iterations).
   - Added bounded janitor-stage stress profile (nightly includes extra capped janitor apply passes with run-record integrity checks).
   - Added janitor carryover trend assertion across stress passes (final carryover must not exceed initial stress carryover).
   - Added nightly profile stratification (`--nightly-profile quick|deep`):
     quick => baseline nightly loops/stress; deep => heavier resilience loops + stress passes.
   - Added failure-injection overlap probe (malformed `/v1/responses` payload must fail, followed by successful normal turn).
6. Nightly outcome summary export.
   - Runner now writes machine-readable run summary to `/tmp/quaid-e2e-last-summary.json` (override with `QUAID_E2E_SUMMARY_PATH`).
   - Summary includes stage status map, suite/profile metadata, timing, and failure stage/line/reason.
7. Runtime budget presets.
   - Added profile-based runtime budgets (`quick|deep`) with enforcement.
   - `--runtime-budget-profile auto` maps to nightly profile; non-nightly defaults to `off`.
   - `--runtime-budget-seconds` overrides the profile budget directly.
8. Budget tuning support from history.
   - Runner appends summary history JSONL for trend tracking.
   - Added `modules/quaid/scripts/e2e-budget-tune.py` to recommend budgets from p95 + configurable buffer.
9. Nightly budget recommendation emission.
   - Nightly suite now runs `e2e-budget-tune.py` automatically from summary history.
   - Emits recommendation JSON at `/tmp/quaid-e2e-budget-recommendation.json` (override with `QUAID_E2E_BUDGET_RECOMMENDATION_PATH`).
   - Non-fatal when history is insufficient (`QUAID_E2E_BUDGET_TUNE_MIN_SAMPLES`, `QUAID_E2E_BUDGET_TUNE_BUFFER_RATIO` control tuning).
10. Strict notification-delivery assertions.
   - Notify matrix now tracks sent/failure/no-channel counters per level.
   - Optional strict mode (`QUAID_E2E_NOTIFY_REQUIRE_DELIVERY=true`) fails when normal/debug have no active channel context or no successful sends.
11. Stage-specific budget tuning primitives.
   - E2E summary now records `stage_durations_seconds` for each stage.
   - `e2e-budget-tune.py --stage <name>` supports per-stage recommendation generation from summary history.

## Backlog Order

Implement next in this order:
1. Enforce stage-specific budget gates in nightly from accumulated history.
2. Upload nightly budget recommendation/history as CI artifacts for trend visibility.
3. Enable strict notification-delivery mode in a dedicated nightly lane with stable channel fixtures.
