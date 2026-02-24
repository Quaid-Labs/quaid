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
2. Multi-owner isolation checks (pending implementation).
   - Seeded owner-scoped duplicate fixture now asserts janitor does not collapse distinct owners.
3. RAG integrity assertions with seeded anchors (pending implementation).
   - Added seeded anchor assertion: pre-benchmark now requires `doc_chunks` to contain the seeded RAG anchor text.
4. Project artifact assertions beyond queue movement (pending implementation).
   - Added PROJECT.md artifact assertion via docs update log delta (`projects/quaid/PROJECT.md`).
5. Resilience checks (gateway restart mid-flow, concurrency/backpressure, migration fixtures) (pending implementation).
   - Added gateway restart mid-session resilience check (nightly/`resilience` suite path).
   - Remaining: deeper backpressure + migration-fixture resilience.

## Backlog Order

Implement next in this order:
1. Backpressure and competing-maintenance resilience checks (janitor/docs updater pressure while live turns run).
