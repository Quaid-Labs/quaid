# 011: Four-Zone Boundary Audit (Adapter / Core-Orchestrator / Datastore / Ingest)

Date: 2026-02-23
Status: Accepted (phases 1-5 complete; follow-up hardening queued)

## Context
Quaid currently uses a 5+1 zone architecture:
- `adapter`: host/runtime integration (OpenClaw hooks/tools/events)
- `core/orchestrator`: routing/policies/coordination and datastore-agnostic interfaces
- `datastore`: durable storage and retrieval engines
- `ingest`: extraction/normalization pipelines that write to datastores
- `orchestrator`: strategy selection and cross-zone flow control (separate from adapter)
- `lifecycle`: maintenance/quality flows (janitor, decay, contradiction sweeps, review jobs)

During rapid refactors, `adaptors/openclaw/adapter.ts` accumulated logic that belonged to `ingest` and `core`.

## Findings

### Fixed in this pass
1. **Adapter-owned extraction pipeline removed**
- Problem: `adapter.ts` contained a full extraction prompt, chunking/merge logic, and direct write loop.
- Fix: adapter now calls canonical ingest entrypoint `ingest/extract.py` through `callExtractPipeline(...)`.
- Ownership now:
  - extraction logic: `ingest/extract.py` (ingest)
  - memory writes/edges: `datastore/memorydb/memory_graph.py` via ingest
  - adapter: transcript assembly + trigger + notification dispatch

2. **Router heuristic drift removed from adapter defaults**
- Problem: adapter had hardcoded `isProjectOrTechnicalQuery(...)` and `inferProjectFromQuery(...)` heuristics.
- Fix: removed heuristic functions and defaulted adapter to neutral recall defaults (`general`, `personal`, `vector_basic+graph` when not routed).
- Routing now relies on orchestrator prepass (`routeRecallPlan`) instead of brittle adapter heuristics.

3. **TS/JS runtime drift guard restored in dev**
- Added `scripts/build-runtime.mjs` and `npm run build:runtime` in dev plugin.
- Upgraded `check-runtime-pairs.mjs` to generated-artifact verification (strict).
- Fixed porcelain path parsing bug in strict checker.

### Fixed in this pass (phase 2/3)
4. **Orchestrator moved out of adapter tree**
- Problem: orchestration lived under `adaptors/openclaw/knowledge/orchestrator.ts`, which blurred adapter vs core boundaries.
- Fix: moved to `orchestrator/default-orchestrator.ts`; adapter now imports this core/orchestrator surface.

5. **Project catalog parsing moved from adapter to core**
- Problem: adapter owned project metadata parsing and filtering.
- Fix: introduced `core/project-catalog.ts` and adapter now consumes `createProjectCatalogReader(...)`.

6. **Janitor RAG/project maintenance routed through lifecycle contract**
- Problem: janitor task 7 directly orchestrated project events, docs registry sync, and RAG reindex internals.
- Fix: added `core/lifecycle/janitor_lifecycle.py` registry (`LifecycleRegistry` + `RoutineContext`) and moved datastore-specific maintenance into routine `rag`.
- Result: janitor calls lifecycle routines by name and consumes returned metrics/logs/errors.

### Remaining boundary leaks (phase 4 queue)
1. **Doc/project ingestion still partly lives in adapter**
- File: `adaptors/openclaw/adapter.ts` (`updateDocsFromTranscript`, staleness/update flow)
- Desired: move to ingest service/module, adapter calls a single ingest entrypoint.

2. **Datastore bridge calls embedded in adapter tool handlers**
- Direct bridge calls (`callPython("search"|"store"|"create-edge")`) are still adapter-local.
- Desired: adapter invokes core interfaces (`writeData`, `recall`) only; python bridge wiring remains an adapter implementation detail hidden behind thin providers.

## Decision
We will enforce these rules:
1. Adapter must not own extraction prompts, chunking, or write loops.
2. Adapter must not contain query-classification heuristics for datastore routing.
3. Core defines recall/write contracts and datastore metadata.
4. Ingest owns transcript-to-fact transformation and normalization.
5. Datastore owns persistence and retrieval behavior only.
6. Lifecycle owns cross-cutting maintenance jobs (janitor) and calls other zones through stable interfaces.

## Implementation Notes (this pass)
- Adapter extraction now delegates to `ingest/extract.py`.
- `ingest/extract.py` now preserves per-fact `source` -> `source_type`, passes `privacy/keywords/session_id`, and creates edges for all successful writes with `fact_id`.
- Janitor task 7 now executes via lifecycle routine registry (`core/lifecycle/janitor_lifecycle.py`) instead of direct inline datastore orchestration.
- Project metadata recommendation parsing now lives in `core/project-catalog.ts`.

## Next Refactor Queue
1. Extract a core-owned `DocIngestionOrchestrator` and move `updateDocsFromTranscript` out of adapter.
2. Expand janitor lifecycle registry beyond RAG to docs maintenance and workspace audit routines.
3. Introduce a single core facade per zone boundary:
- adapter -> core: `runRecall`, `runIngest`, `runDocIngest`, `runJanitorSignals`
- core -> datastore: `read/write` contracts only
