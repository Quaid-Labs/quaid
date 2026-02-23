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
- Resolution:
  - janitor now emits `janitor.run_completed` lifecycle event with metrics/change payload
  - event handler in `events.py` owns delayed notification queueing for summary/digest
  - `notify.py` exposes formatter helpers so delivery transport stays outside janitor
  - adapter runtime Python execution moved into adapter-local bridge module (`adapters/openclaw/python-bridge.ts`)
    so `adapter.ts` no longer defines/exports raw bridge command execution logic
  - `api.stats()` now routes via datastore interface function `memory_graph.stats()` rather than direct
    `get_graph().get_stats()` call in API layer

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
  - removed import-time cycle (`docs_updater -> events -> docs_ingest -> docs_updater`)
    by loading docs-updater entrypoints lazily via `importlib` wrappers in `docs_ingest.py`
- Delayed notification flow unified through event bus:
  - `plugins/quaid/events.py` added `queue_delayed_notification(...)`
  - `plugins/quaid/janitor.py` now uses event bus for delayed messages
  - `plugins/quaid/adapters/openclaw/adapter.ts` no longer flushes delayed notification files
  - removed legacy `flushDelayedNotificationsToRequestQueue(...)` helper from delayed-request bridge
  - updated delayed-request integration test to validate canonical queue/resolve/clear lifecycle
- MCP API boundary enforced:
  - `plugins/quaid/api.py` now exposes `stats()` and richer `recall(...)` options
  - `plugins/quaid/mcp_server.py` uses API entrypoints only for recall/stats
  - `projects_search` in `plugins/quaid/mcp_server.py` now routes through `api.projects_search_docs(...)`
    instead of directly constructing `DocsRAG` datastore calls
  - `memory_extract` in `plugins/quaid/mcp_server.py` now routes through
    `api.extract_transcript(...)` instead of direct ingestor imports
  - API no longer re-exports datastore model internals (`Node`, `Edge`) from
    `plugins/quaid/api.py`, tightening facade boundaries for external callers
- Orchestrator store implementation moved out:
  - `plugins/quaid/orchestrator/default-orchestrator.ts` uses injected store callbacks
  - `plugins/quaid/adapters/openclaw/adapter.ts` supplies journal/project store handlers
  - removed unused legacy deps (`path`, `fs`, `callDocsRag`) from orchestrator facade contract to tighten boundary surface
- Janitor lifecycle ownership expanded:
  - `plugins/quaid/janitor_lifecycle.py` is now orchestration-only: it loads module-owned routine registrars
  - datastore/workspace modules now own registration + maintenance logic:
    - `plugins/quaid/workspace_audit.py` registers `workspace`
    - `plugins/quaid/docs_updater.py` registers `docs_staleness` and `docs_cleanup`
    - `plugins/quaid/soul_snippets.py` registers `snippets` and `journal`
    - `plugins/quaid/docs_rag.py` registers `rag`
    - `plugins/quaid/memory_graph.py` registers `datastore_cleanup`
  - `plugins/quaid/janitor.py` now dispatches those tasks through lifecycle registry instead of inline task bodies
  - docs write policy checks are preserved via `allow_doc_apply` callback passed through routine context
  - added lifecycle tests for workspace/docs/snippets/journal in `plugins/quaid/tests/test_janitor_lifecycle.py`
  - janitor datastore imports now route through `plugins/quaid/datastore_maintenance.py`
    as a single datastore maintenance facade over `memory_graph` primitives
  - workspace audit datastore writes now route through the same facade (`store_memory`)
  - ingestor/clustering modules now route datastore graph access through the facade:
    - `plugins/quaid/extract.py`
    - `plugins/quaid/semantic_clustering.py`
  - ingestor write path in `plugins/quaid/extract.py` now uses datastore facade writes
    (`store_memory`/`create_edge`) instead of importing API write wrappers, removing
    ingestor -> api coupling and the api <-> extract cycle
  - API search now routes through datastore interface function `memory_graph.search(...)`
    instead of `api.py` calling `get_graph().search_hybrid(...)`
  - memory-store maintenance ownership consolidated into one datastore routine:
    - added `plugins/quaid/memory_graph_pipeline.py` with unified
      `memory_graph_maintenance` routine
    - `plugins/quaid/janitor.py` memory tasks now dispatch to that single routine
      via `options.subtask` (review/temporal/dedup_review/duplicates/
      contradictions/contradictions_resolve/decay/decay_review)
    - `plugins/quaid/janitor_lifecycle.py` now registers only the unified
      `memory_graph_pipeline` maintenance routine for memory-store tasks
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
  - `plugins/quaid/tests/test_events.py` janitor completion event routing
  - `plugins/quaid/tests/test_janitor_lifecycle.py` lifecycle parallel run surface
- Added lifecycle dry-run parallelization path:
  - `LifecycleRegistry.run_many(...)` in `plugins/quaid/janitor_lifecycle.py`
  - janitor uses parallel prepass for workspace/docs/snippets/journal during `--task all --dry-run`
  - `docs_rag` lifecycle routine now honors dry-run by skipping reindex mutation path

## Boundary Scan Snapshot (post-pass)
- `adapter.ts`: only one `callPython(...)` remains (bridge implementation); no direct handler call-site leakage.
- No remaining direct `get_adapter()` usage outside `plugins/quaid/lib/*`.
- Direct `get_adapter()` imports live only in boundary-root internals:
  - `lib/runtime_context.py` (intended adapter access port)
  - `lib/adapter.py` (adapter implementation)
  - `lib/config.py`, `lib/archive.py`, `lib/embeddings.py`, `lib/providers.py` (adapter-proximate library internals)
These are expected and not lifecycle/datastore boundary leaks.
- No direct `memory_graph` recall/stats bypass in MCP server.
- No direct ingestor import in MCP server extract path (API boundary now enforced).
- No direct graph-object `search_hybrid(...)` call in API layer.
- No ingestor datastore writes routed through API wrappers (`extract.py` now uses datastore facade directly).
- No direct delayed-notification file writes from janitor.
- No docs/events command ownership in `memory_graph.py` CLI.

## Next Actions
1. Expand safe parallel execution beyond dry-run prepass once mutation isolation is formalized per routine scope.
