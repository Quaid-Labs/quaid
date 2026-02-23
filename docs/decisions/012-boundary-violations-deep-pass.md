# 012: Deep Boundary Pass (Post-Refactor)

Date: 2026-02-23
Status: Accepted (phase 3 applied; phase 4 queued)

## Scope
Deep audit of boundary ownership after the orchestrator split and janitor lifecycle extraction.

## Current Zone Model
- `adapter`: host integration and tool wiring
- `core/orchestrator`: routing policy and coordination surfaces
- `datastore`: persistence/index/search engines
- `ingestor`: transcript/log -> structured writes
- `lifecycle`: scheduled maintenance execution and quality control

## Findings

### High Priority (now resolved in this pass)
1. Adapter-owned Python datastore bridge calls in handlers
- File: `plugins/quaid/adapters/openclaw/adapter.ts`
- Previous evidence: direct `callPython("search"|"store"|"create-edge"|...)` calls.
- Resolution: introduced `plugins/quaid/core/datastore-bridge.ts` and migrated handler calls to `datastoreBridge`.
- Risk: adapter knows datastore verbs and payload shape, reducing portability.
- Follow-up: move to richer core ports (`runRecall`, `runWrite`, `runStats`, `runForget`) with host-agnostic envelopes.

2. Adapter-owned transcript docs update logic
- File: `plugins/quaid/adapters/openclaw/adapter.ts`
- Previous evidence: full stale-doc check + transcript update orchestration in adapter.
- Resolution: moved orchestration into `plugins/quaid/docs_ingest.py`; adapter now calls `callDocsIngestPipeline(...)`.
- Risk: ingestion policy drift by adapter/runtime.
- Follow-up: emit lifecycle event instead of direct ingest invocation from adapter.

### Medium Priority (now resolved in this pass)
3. Lifecycle/datastore direct adapter imports for runtime/provider resolution
- Files: `plugins/quaid/janitor.py`, `plugins/quaid/llm_clients.py`
- Previous evidence: direct `get_adapter()` calls for workspace/log paths and provider checks.
- Resolution: added `plugins/quaid/lib/runtime_context.py` port and rewired lifecycle/datastore/ingestor modules:
  - `janitor.py`
  - `llm_clients.py`
  - `docs_rag.py`
  - `docs_registry.py`
  - `project_updater.py`
  - `soul_snippets.py`
  - `workspace_audit.py`
  - `memory_graph.py`
  - `extract.py`

4. Datastore modules resolve runtime home paths via adapter
- Resolved via `lib/runtime_context.py` in the modules listed above.

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
- Added core datastore bridge and migrated adapter handler calls:
  - `plugins/quaid/core/datastore-bridge.ts`
  - adapter now routes memory bridge operations via `datastoreBridge`.
- Added docs ingest pipeline entrypoint and adapter integration:
  - `plugins/quaid/docs_ingest.py`
  - adapter uses `callDocsIngestPipeline(...)`.
- Added tests for new boundary surfaces:
  - `plugins/quaid/tests/datastore-bridge.test.ts`
  - `plugins/quaid/tests/test_docs_ingest.py`

## Boundary Scan Snapshot (post-pass)
- `adapter.ts`: only one `callPython(...)` remains (bridge implementation); no direct call sites in handlers.
- Remaining direct `get_adapter()` imports are concentrated in adapter-facing infrastructure modules:
  - `mcp_server.py` (host RPC entrypoint)
  - `notify.py`, `events.py`, `logger.py` (transport/runtime wiring)
  - `config.py`, `enable_wal.py`, `docs_updater.py` (runtime path/config helpers)
  - `lib/*` internals including `runtime_context.py` (intentional boundary root)
These are currently expected and not lifecycle/datastore boundary violations.

## Next Actions
1. Convert adapter-triggered docs ingest into lifecycle event dispatch + handler.
2. Expand janitor lifecycle registry beyond `rag` (workspace/docs/snippets/journal).
3. Add janitor task parallelization (after boundary enforcement completes).
