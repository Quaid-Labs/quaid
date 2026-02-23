# 012: Deep Boundary Pass (Post-Refactor)

Date: 2026-02-23
Status: Accepted (phase 5 applied)

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
  - `config.py`
  - `docs_updater.py`
  - `enable_wal.py`
  - `events.py`
  - `logger.py`
  - `mcp_server.py`
  - `notify.py`

4. Datastore modules resolve runtime home paths via adapter
- Resolved via `lib/runtime_context.py` in the modules listed above.

### Low Priority
5. Mixed ownership in notification/report routing policy
- Files: `plugins/quaid/janitor.py`, `plugins/quaid/notify.py`, `plugins/quaid/events.py`
- Target: lifecycle emits report events; adapter decides immediate vs delayed transport.

### Additional High Priority (resolved in this pass)
6. Split delayed-notification pipelines
- Previous state:
  - janitor wrote `logs/janitor/delayed-notifications.json`
  - events queued to `.quaid/runtime/notes/delayed-llm-requests.json`
- Resolution:
  - janitor now queues delayed messages through event bus (`events.queue_delayed_notification(...)`)
  - adapter no longer flushes `delayed-notifications.json` into request queue
  - canonical delayed path is now event bus -> delayed request queue only

7. MCP server bypassed API/core boundaries
- Previous state:
  - advanced recall path imported `memory_graph.recall` directly
  - stats path called `get_graph().get_stats()` directly
- Resolution:
  - MCP recall now routes advanced options through `api.recall(...)`
  - stats now routes through `api.stats()`

8. Core project catalog used runtime process execution
- Previous state:
  - `core/project-catalog.ts` executed `python3 docs_registry.py ...`
- Resolution:
  - project catalog now reads canonical config (`config/memory.json`) directly
  - no process execution in core project catalog path

9. Orchestrator carried adapter/datastore implementation logic for stores
- Previous state:
  - journal/project store retrieval internals lived in orchestrator
- Resolution:
  - orchestrator now consumes injected store callbacks (`recallJournalStore`, `recallProjectStore`)
  - openclaw adapter provides concrete implementations

10. Datastore CLI mixed in docs/events concerns
- Previous state:
  - `memory_graph.py` CLI had `event` subcommands and docs-RAG portion of `search-all`
- Resolution:
  - removed event CLI surface from `memory_graph.py`
  - `search-all` reduced to datastore-only unified memory search

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
  - adapter now dispatches `docs.ingest_transcript` through event bus (`plugins/quaid/events.py`) with immediate active processing
  - docs ingestion executes in events handler (`_handle_docs_ingest_transcript`) instead of direct adapter subprocess ownership
- Delayed notification flow unified through event bus:
  - `plugins/quaid/events.py` added `queue_delayed_notification(...)`
  - `plugins/quaid/janitor.py` now uses event bus for delayed messages
  - `plugins/quaid/adapters/openclaw/adapter.ts` no longer flushes delayed notification files
  - removed legacy `flushDelayedNotificationsToRequestQueue(...)` helper from delayed-request bridge
  - updated delayed-request integration test to validate canonical queue/resolve/clear lifecycle
- MCP API boundary enforced:
  - `plugins/quaid/api.py` now exposes `stats()` and richer `recall(...)` options
  - `plugins/quaid/mcp_server.py` uses API entrypoints only for recall/stats
- Orchestrator store implementation moved out:
  - `plugins/quaid/orchestrator/default-orchestrator.ts` uses injected store callbacks
  - `plugins/quaid/adapters/openclaw/adapter.ts` supplies journal/project store handlers
  - removed unused legacy deps (`path`, `fs`, `callDocsRag`) from orchestrator facade contract to tighten boundary surface
- Janitor lifecycle ownership expanded:
  - `plugins/quaid/janitor_lifecycle.py` now registers `workspace`, `snippets`, and `journal` routines in addition to `rag`
  - `plugins/quaid/janitor.py` now dispatches those tasks through lifecycle registry instead of inline task bodies
  - added lifecycle tests for workspace/snippets/journal in `plugins/quaid/tests/test_janitor_lifecycle.py`
- Core project catalog no longer shells to python:
  - `plugins/quaid/core/project-catalog.ts`
- Docs/project update notifications now emit delayed event bus messages:
  - `plugins/quaid/docs_updater.py`
  - `plugins/quaid/project_updater.py`
- Added/updated test coverage:
  - `plugins/quaid/tests/knowledge-orchestrator.test.ts`
  - `plugins/quaid/tests/test_mcp_server.py`
- Added tests for new boundary surfaces:
  - `plugins/quaid/tests/datastore-bridge.test.ts`
  - `plugins/quaid/tests/test_docs_ingest.py`

## Boundary Scan Snapshot (post-pass)
- `adapter.ts`: only one `callPython(...)` remains (bridge implementation); no direct handler call-site leakage.
- No remaining direct `get_adapter()` usage outside `plugins/quaid/lib/*`.
- Direct `get_adapter()` imports live only in boundary-root internals:
  - `lib/runtime_context.py` (intended adapter access port)
  - `lib/adapter.py` (adapter implementation)
  - `lib/config.py`, `lib/archive.py`, `lib/embeddings.py`, `lib/providers.py` (adapter-proximate library internals)
These are expected and not lifecycle/datastore boundary leaks.
- No direct `memory_graph` recall/stats bypass in MCP server.
- No direct delayed-notification file writes from janitor.
- No docs/events command ownership in `memory_graph.py` CLI.

## Next Actions
1. Move doc staleness/cleanup task ownership into janitor lifecycle registry.
2. Add janitor task parallelization (after boundary enforcement completes).
