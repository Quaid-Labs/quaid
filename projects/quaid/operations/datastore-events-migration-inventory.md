# Datastore Events Migration Inventory

Status: M0 approved; M1-M10 milestone records now live in the adjacent
`datastore-events-m*.md` files. This inventory remains the baseline boundary
snapshot, not the active implementation checklist.
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`
Branch strategy: local branch `datastore-refactor-m0` from dev HEAD. Alpha/user bugs interrupt this branch; public push/release stays with W8 and requires operator approval.

## M0 Scope

This inventory freezes the current datastore boundary before event/broker migration work.
It records observed production paths only. It does not change runtime behavior.

M0 goals from the migration plan:

- inventory current producers, monitors, write paths, recall paths, janitor maintenance registrations, and direct datastore imports
- define canonical datastore ids
- identify hardcoded store catalogs in TypeScript and Python
- identify monitor code that writes directly to memory/docs/evolution state

## Canonical Datastore Ids

Current canonical ids for this arc:

- `memorydb` - memory graph, facts, edges, source/session evidence projections, archive, domain metadata
- `docsdb` - project docs registry, document/RAG index, project-log queues, docs update policy
- `evolutiondb` - canonical target id for the current `datastore.notedb` implementation

Current open inventory decision before M2:

- `sessiondb` now exists as durable transcript/session evidence storage and has a CLI namespace. The original plan predates this split. M2 must decide whether `sessiondb` receives a first-party datastore manifest or remains an internal provenance datastore bridged by `core.services.session_memory_bridge`.

Runtime rename rule:

- Do not rename `datastore.notedb` imports or paths in M0. Planning/docs may
  use `evolutiondb`; runtime module rename was out of M0 scope and later tracked
  by the dedicated rename milestone.

## Current Producers And Monitors

Adapters and hook surfaces:

- `adaptors/openclaw/adapter.ts` and generated JS observe OpenClaw lifecycle, prompt, message, reset, compact, and session-end surfaces.
- `core/interface/hooks.py` observes Claude Code and Codex hook input, emits extraction signals, refreshes split rules/context artifacts, and relays deferred notices.
- Adapter plugin manifests declare hook/event surfaces but do not yet own dynamic runtime activation.

Daemon and runtime monitors:

- `core/extraction_daemon.py` watches extraction signals, transcript cursors, rolling buffers, no-content/stale signal state, and timeout/session-end processing.
- `core/runtime/events.py` provides the existing queue-backed event bus, event registry, queue/history files, handler registration, and immediate/queued dispatch CLI.
- `core/project_docs_supervisor.py` and `core/project_docs_worker.py` own project-doc worker process reconciliation and project-doc update execution.
- `core/docs_updater_hook.py` runs post-extraction docs update classification and dispatch.
- `core/lifecycle/workspace_audit.py` monitors core markdown/runtime files for janitor bloat and drift.
- `core/lifecycle/janitor.py` owns host/instance janitor orchestration and scheduled maintenance.

Ingest producers:

- `ingest/extract.py` transforms transcript text into facts, edges, snippets, journal entries, and source/session evidence.
- `ingest/session_logs_ingest.py` indexes session logs through the core session-memory bridge.
- `ingest/plugin_contract.py` declares ingest plugin surfaces but production write flow still calls existing ingest functions.

Docs/project producers:

- `datastore/docsdb/docs_cli.py` owns datastore-side docs CLI dispatch for `list`, `check`, and `changelog`.
- `datastore/docsdb/project_log_queue.py` persists project-log write intent for project-docs workers.
- `datastore/docsdb/project_updater.py` appends project logs and updates project docs.
- `datastore/docsdb/updater.py` checks and updates stale docs.
- `datastore/docsdb/rag.py` indexes/searches docs and registers RAG maintenance.
- `datastore/docsdb/registry.py` owns docs/project registry persistence.
- `datastore/docsdb/system_context.py` builds DocsDB-owned linked-project system-context metadata.

## Current Write Paths

MemoryDB write paths:

- `core/services/memory_service.py` wraps `datastore.facade` for `store`, `store_source_chunk(s)`, `store_session_chunk(s)`, `create_edge`, forget, stats, and domain APIs.
- `ingest/extract.py` stores facts through the memory service and creates edges through the same port.
- `ingest/extract.py` writes source/session evidence through `core.services.session_memory_bridge`, which projects SessionDB microchunks into MemoryDB source chunks.
- `core/extraction_daemon.py` still imports `datastore.memorydb.memory_graph` directly for some extraction/health/cursor-adjacent behavior.
- `core/lifecycle/datastore_runtime.py` re-exports memory maintenance functions and `get_graph()` for janitor/core callers.

SessionDB write paths:

- `core/services/session_memory_bridge.py` registers in-memory bridge routes for `sessiondb` and `memorydb`.
- `datastore/sessiondb/session_store.py` stores session transcripts, microchunks, pair ids, and memory-chunk attachments.
- `core/session_cli.py` exposes `expand-microchunk` and `expand-chunk` through the session bridge.

DocsDB write paths:

- `core/project_registry.py` composes project registry operations by importing `datastore.docsdb.registry` and `datastore.docsdb.rag`.
- `core/docs/updater.py` and `core/docs_cli.py` compose docs update/list/check/changelog behavior around DocsDB modules.
- `core/docs_updater_hook.py` imports `datastore.docsdb.updater` for post-extraction docs updates.
- `datastore/docsdb/project_log_queue.py`, `project_updater.py`, `registry.py`, `rag.py`, and `updater.py` own DocsDB persistence semantics.

EvolutionDB / NoteDB write paths:

- `ingest/extract.py` writes snippet and journal outputs through `datastore.notedb.soul_snippets`.
- `core/lifecycle/soul_snippets.py` wraps `datastore.notedb` for lifecycle callers.
- `datastore/notedb/soul_snippets.py` writes `*.snippets.md`, `journal/*.journal.md`, and registers snippet/journal janitor routines.

Archive and compatibility write paths:

- `lib/archive.py` is a compatibility shim over `datastore.memorydb.archive_store`.

## Current Recall And Query Paths

TypeScript facade/orchestration:

- `core/facade.ts` exposes the product recall entry points, formats injected context, and calls `createKnowledgeEngine`.
- `core/knowledge-engine.ts` coordinates recall planning, store fanout, result merge, and router failure policy.
- `core/knowledge-stores.ts` now owns the central TypeScript knowledge-store metadata/guidance consumed by `knowledge-engine`.
- `orchestrator/default-orchestrator.ts` is a re-export facade for the core knowledge engine.

Python/datastore recall:

- `datastore/memorydb/memory_graph.py` owns vector, graph, docs/session mixed-store planning, temporal filters, relation-chain traversal, RRF merge, and CLI recall.
- `datastore/docsdb/rag.py` owns docs index/search.
- `datastore/sessiondb/session_store.py` owns raw session transcript/microchunk expansion and lookup.
- `core/services/memory_service.py` wraps the datastore facade for core callers.

Current aliases and store names:

- `vector_basic` and `vector_technical` remain compatibility/routing aliases over the canonical `vector` store plus domain policy.
- `source_chunks` is normalized to `session_chunks` in TypeScript store normalization.
- Python recall also accepts mixed store plans such as `vector`, `docs`, `graph`, and `session_chunks`.

## Janitor And Maintenance Registrations

Core/janitor:

- `core/lifecycle/janitor.py` orchestrates janitor tasks, approvals, host apply, supervisor requests, and plugin contract maintenance surfaces.
- `core/lifecycle/janitor_lifecycle.py` is the lifecycle maintenance registry.
- `core/lifecycle/datastore_runtime.py` bridges core/janitor to datastore maintenance operations.

Registered datastore maintenance:

- `core/plugins/memorydb_contract.py` handles memorydb lifecycle/domain/tool-runtime hooks.
- `core/plugins/docsdb_contract.py` handles docsdb visible workspace, misc bootstrap, and project-doc monitor request hooks.
- `core/plugins/notedb_contract.py` is a minimal NoteDB contract.
- `datastore/memorydb/maintenance.py` registers memory graph maintenance.
- `datastore/memorydb/memory_graph.py` exposes datastore cleanup and graph/memory operations.
- `datastore/docsdb/rag.py` registers RAG/docs maintenance.
- `datastore/notedb/soul_snippets.py` registers snippets and journal maintenance.

Adapter maintenance:

- `adaptors/codex/maintenance.py`, `adaptors/claude_code/maintenance.py`, and OpenClaw adapter maintenance paths are loaded through lifecycle/plugin mechanisms where configured.

## Existing Event Bus

`core/runtime/events.py` is the current event bus. It is queue-backed, adapter-agnostic, and in scope for M1 so the migration does not duplicate or conflict with it.

Current mechanics:

- `EVENT_REGISTRY` defines eleven event names with `fireable`, `processable`, `listenable`, and `delivery_mode`.
- Events are persisted under the runtime root in `.runtime/events/queue.json` and `.runtime/events/history.jsonl`.
- `emit_event()` appends pending events; `_main emit --dispatch auto` immediately processes active events and leaves passive events queued.
- `process_events()` dispatches pending events through `EVENT_HANDLERS`.
- `register_event_handler()` can register or force-replace handlers at runtime.
- failHard is consulted on JSON read/write/chmod failures and handler failures.
- `validate_declared_event_contract()` maps adapter-native hook names to canonical runtime events for plugin manifest validation.

Current event names and datastore/state impact:

| Event | Current handler | Delivery | Datastore/state impact |
| --- | --- | --- | --- |
| `session.new` | `_handle_session_lifecycle` | active | acknowledges lifecycle only; no datastore write |
| `session.reset` | `_handle_session_lifecycle` | active | acknowledges lifecycle only; no datastore write |
| `session.compaction` | `_handle_session_lifecycle` | active | acknowledges lifecycle only; no datastore write |
| `session.timeout` | `_handle_session_lifecycle` | active | acknowledges lifecycle only; no datastore write |
| `session.agent_start` | `_handle_session_lifecycle` | active | acknowledges lifecycle only; no datastore write |
| `session.agent_end` | `_handle_session_lifecycle` | active | acknowledges lifecycle only; no datastore write |
| `notification.delayed` | `_handle_delayed_notification` | passive | queues deferred notices through runtime note storage |
| `memory.force_compaction` | `_handle_force_compaction` | passive | queues deferred compaction notice through runtime note storage |
| `docs.ingest_transcript` | `_handle_docs_ingest_transcript` | active | calls `core.ingest_runtime.run_docs_ingest`; affects DocsDB/project-doc ingestion state |
| `session.ingest_log` | `_handle_session_ingest_log` | active | calls `core.ingest_runtime.run_session_logs_ingest`; affects SessionDB and projected MemoryDB evidence through the session-memory bridge |
| `janitor.run_completed` | `_handle_janitor_run_completed` | active | queues user-facing janitor summary/digest notices based on janitor metrics; does not write MemoryDB facts directly |

M1 implication:

- The event envelope/broker facade should either wrap this module or deliberately evolve it. It must not introduce a parallel event registry or queue without an explicit replacement plan.
- Existing event names are not the final datastore migration taxonomy. For example, `docs.ingest_transcript` and `session.ingest_log` are active request-like handlers today, while the migration plan distinguishes domain events from request events with correlation ids.

## Direct Datastore Imports Outside `datastore/`

Observed production imports from non-datastore modules:

- `config.py` -> `datastore.memorydb.domain_defaults`
- `core/docs/updater.py` -> `datastore.docsdb`, `datastore.docsdb.project_updater`, `datastore.docsdb.rag`, `datastore.docsdb.registry`
- `core/docs_cli.py` -> `datastore.docsdb`
- `core/docs_updater_hook.py` -> `datastore.docsdb.updater`
- `core/extraction_daemon.py` -> `datastore.memorydb.memory_graph`
- `core/lifecycle/datastore_runtime.py` -> `datastore.docsdb.rag`, `datastore.memorydb.maintenance_ops`, `datastore.memorydb.memory_graph`, `datastore.notedb.soul_snippets`
- `core/lifecycle/soul_snippets.py` -> `datastore.notedb`
- `core/plugins/docsdb_contract.py` -> `datastore.docsdb.registry`, `datastore.docsdb.system_context`
- `core/plugins/memorydb_contract.py` -> `datastore.memorydb.domain_registry`, `datastore.memorydb.maintenance`, `datastore.memorydb.system_context`
- `core/project_registry.py` -> `datastore.docsdb.rag`, `datastore.docsdb.registry`
- `core/services/memory_service.py` -> `datastore.facade`, `datastore.memorydb.identity_defaults`
- `core/services/session_memory_bridge.py` -> `datastore.sessiondb.session_store`
- `lib/archive.py` -> `datastore.memorydb.archive_store`
- `scripts/e2e-domain-contract.py` -> `datastore.memorydb.memory_graph`
- `scripts/sync-tools-domain-block.py` -> `datastore.memorydb.domain_registry`

Boundary note:

- Several of these are current allowlisted core composition points. M0 records them; later milestones decide which move behind broker/manifest contracts.

## Hardcoded Store Catalogs And Capability Lists

TypeScript:

- `core/knowledge-stores.ts` is now the central TS knowledge-store registry for recall router guidance, routable keys, defaults, and compatibility alias normalization.
- `core/facade.ts` still has hardcoded recall bridge store sets and alias handling, including `vector`, `vector_basic`, `vector_technical`, `graph`, `project`, and `session_chunks`.
- `core/facade.ts` still maps `project` to bridge-side `docs` and normalizes legacy vector aliases into domain filters.
- M4 adds request-contract metadata for `journal` as `recall.journal.request.v1` under `evolutiondb`. Runtime facade routing is still unchanged: `core/facade.ts` `bridgeOnlyStores` does not include `journal`, so some explicit bridge-only paths do not treat `journal` as bridge-routable even though the knowledge registry can route to it through the knowledge engine. Resolve the runtime side before M6 behavior activation.

Python:

- `datastore/memorydb/memory_graph.py` owns Python recall store planning and mixed-store behavior.
- Python recall planning uses store names such as `vector`, `docs`, `graph`, and `session_chunks`, plus relation-chain and graph-aware planner metadata.
- `core/services/datastore_bridge.py` has callback registry names `sessiondb` and `memorydb`.
- `core/services/session_memory_bridge.py` hardcodes bridge routes for `sessiondb` and `memorydb`.
- `core/datastore_cli_registry.py` statically maps CLI namespaces `docs` and `session` to core CLI dispatch modules.

## Monitor Code That Still Writes Directly

Direct-write monitor paths to migrate in later milestones:

- `core/extraction_daemon.py` processes extraction/session signals and still reaches MemoryDB/session state through direct/core bridge calls rather than emitted domain events.
- `core/docs_updater_hook.py` runs post-extraction docs updates directly.
- `core/project_docs_supervisor.py` and `core/project_docs_worker.py` reconcile and execute docs worker updates directly.
- `core/lifecycle/workspace_audit.py` performs janitor workspace checks directly.
- `core/lifecycle/janitor.py` invokes datastore maintenance directly or through current plugin/lifecycle bridges.
- DocsDB project/file freshness and registration paths still write inside DocsDB modules rather than via domain event listeners.
- Evolution/snippet/journal writes from extraction still call NoteDB functions directly.

## M0 Follow-Up Decisions

1. `sessiondb` was not added to first-party datastore manifests in M2.
   - Current disposition: keep it as internal transcript/provenance plumbing
     through `core.services.session_memory_bridge` until a dedicated sessiondb
     manifest or source-window slice is reviewed.
   - Tracking doc: `datastore-events-m2-manifest-registry.md`.

2. User-facing recall store names are treated as capability/selectors rather
   than datastore implementation ids.
   - Current disposition: M4/M5 activate explicit docs/vector request slices;
     M6.1 keeps TypeScript routing metadata behavior-preserving; M6.2a and M6.3
     remain plan-approved but runtime-blocked.
   - Tracking docs: `datastore-events-m4-recall-request-contract.md`,
     `datastore-events-m5-explicit-vector-recall-plan.md`,
     `datastore-events-m6-routed-recall-capability-plan.md`, and
     `datastore-events-m6-memory-selector-plan.md`.

3. `evolutiondb` is the canonical datastore id while the M0 runtime module was
   `datastore.notedb`.
   - Current disposition: M10 Slice 1 made `datastore.evolutiondb` canonical
     with `datastore.notedb` as an installed-alpha compatibility alias. M10
     Slice 2 made `core.plugins.evolutiondb_contract` canonical with
     `core.plugins.notedb_contract` as the matching compatibility alias.
     `notedb.core` plugin-id rename and alias removal remain separately
     reviewed, operator-gated future work.

## Current Use

- Use this file as the frozen baseline for producer/write-path inventory.
- Use the milestone-specific documents for current implementation state,
  validation gates, and behavior-slice boundaries.
- Runtime behavior changes remain governed by the active milestone preconditions,
  especially W4 full-livetest or explicit Solomon/Hermes override gates for M6/M7.
