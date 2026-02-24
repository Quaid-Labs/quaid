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
8. Failure diagnostics (all suites)
   - On error, runner prints pending signals, timeout logs, notify-worker tail, gateway status, and gateway logs.
9. Bootstrap collision recovery
   - If worktree bootstrap fails with a workspace "already exists" collision after wipe, runner performs one forced cleanup + retry automatically.

## Recommended Next Additions

1. Contradiction resolution assertions
   - Seed contradictory facts and assert janitor resolves status transitions deterministically.
2. Decay and dedup assertions
   - Seed stale + duplicate facts and assert expected confidence/status changes after janitor.
3. Projects artifact assertions
   - Assert specific project updater outputs (not just queue/log movement).
4. RAG integrity assertions
   - Assert specific seeded docs are present in `doc_chunks` and retrievable post-janitor.
5. Multi-owner isolation assertions
   - Seed multiple owners and ensure recall/maintenance boundaries remain owner-scoped.

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
