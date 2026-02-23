# 012: Deep Boundary Pass (Post-Refactor)

Date: 2026-02-23
Status: Accepted (tracking queue)

## Scope
Deep audit of boundary ownership after the orchestrator split and janitor lifecycle extraction.

## Current Zone Model
- `adapter`: host integration and tool wiring
- `core/orchestrator`: routing policy and coordination surfaces
- `datastore`: persistence/index/search engines
- `ingestor`: transcript/log -> structured writes
- `lifecycle`: scheduled maintenance execution and quality control

## Findings

### High Priority
1. Adapter still owns Python datastore bridge wiring
- File: `plugins/quaid/adapters/openclaw/adapter.ts`
- Evidence: direct `callPython("search"|"store"|"create-edge"|...)` calls.
- Risk: adapter knows datastore verbs and payload shape, reducing portability.
- Target: move bridge verbs behind core ports (`runRecall`, `runWrite`, `runStats`, `runForget`).

2. Adapter still owns doc-ingestion orchestration hook
- File: `plugins/quaid/adapters/openclaw/adapter.ts`
- Evidence: `updateDocsFromTranscript(...)` is still called from adapter events.
- Risk: ingestion policy drift by adapter/runtime.
- Target: move to ingestor/lifecycle entrypoint, adapter emits only trigger metadata.

### Medium Priority
3. Lifecycle still couples to adapter for path/provider resolution
- Files: `plugins/quaid/janitor.py`, `plugins/quaid/llm_clients.py`
- Evidence: direct `get_adapter()` calls for workspace/log paths and provider checks.
- Target: introduce environment/provider ports (`runtime_context`, `llm_provider_port`) so lifecycle does not import adapter directly.

4. Datastore modules resolve runtime home paths via adapter
- Files: `plugins/quaid/docs_rag.py`, `plugins/quaid/docs_registry.py`, `plugins/quaid/project_updater.py`, `plugins/quaid/soul_snippets.py`
- Target: pass resolved roots from core/lifecycle instead of adapter lookups in each module.

### Low Priority
5. Mixed ownership in notification/report routing policy
- Files: `plugins/quaid/janitor.py`, `plugins/quaid/notify.py`, `plugins/quaid/events.py`
- Target: lifecycle emits report events; adapter decides immediate vs delayed transport.

## What Was Completed This Pass
- Janitor Task 7 extracted into lifecycle registry:
  - `plugins/quaid/janitor_lifecycle.py`
  - `janitor.py` now executes `LifecycleRegistry.run("rag", RoutineContext(...))`
- Added lifecycle contract tests:
  - `plugins/quaid/tests/test_janitor_lifecycle.py`
- Added E2E matrix path timeout guard to prevent hanging full suites:
  - `plugins/quaid/scripts/run-quaid-e2e-matrix.sh`

## Next Actions
1. Add core bridge ports and remove direct `callPython(...)` usage from adapter handlers.
2. Move `updateDocsFromTranscript` into ingestor/lifecycle and expose one adapter trigger method.
3. Add runtime/provider context object injected into lifecycle/datastore modules (no direct adapter imports).
