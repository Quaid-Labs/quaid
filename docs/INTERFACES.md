# Interface Contract (MCP / CLI / Adapter)

This document defines Quaid's external interface model for host adapters and runtime orchestration.

## Goals

- Keep core behavior adapter-independent.
- Make capabilities discoverable (not assumed).
- Keep MCP and CLI surfaces aligned enough that automation can be ported between them.
- Support new adapters without changing core contracts.

## Surfaces

- `OpenClaw adapter` (`modules/quaid/adaptors/openclaw/adapter.ts`): richest integration path, lifecycle hooks + tools.
- `MCP server` (`modules/quaid/core/interface/mcp_server.py`): host-agnostic RPC tools.
- `CLI wrapper` (`modules/quaid/quaid`): operational entrypoint to Python modules.

## Capability Discovery

### Knowledge Datastore Read Capabilities

- Source: `core/knowledge-stores.ts`
- Discovery:
  - Adapter memory tool guidance (`memory_recall` tool description)
  - Orchestrator internals use registry keys/options

### Event Capabilities

- Source: `modules/quaid/core/runtime/events.py` (`get_event_registry()`)
- Discovery:
  - MCP: `memory_event_capabilities`
  - CLI: `quaid event capabilities`
  - OpenClaw adapter: no direct event-capability tool (use MCP/CLI surfaces)

Each event includes `delivery_mode`:
- `active`: safe to trigger and process immediately.
- `passive`: must remain async/deferred (queued), not immediate.

This allows orchestrators/agents to adapt strategy based on supported events and delivery constraints.

## Write Contract

- Source: `core/data-writers.ts`
- API:
  - `createDataWriteEngine()`
  - `writeData({ datastore, action, payload, meta })`
  - `writeDataBatch(...)`
- Current adapter bindings:
  - `vector` / `store_fact`
  - `graph` / `create_edge`

New datastores should register DataWriters rather than adding direct write calls throughout adapters.

## Identity Contract (Multi-User Foundation)

Source:
- `modules/quaid/core/runtime/identity_runtime.py`
- `modules/quaid/datastore/memorydb/identity_defaults.py`
- `modules/quaid/core/services/memory_service.py`

Rules:
1. Core allows exactly one active identity resolver registration and one active privacy policy registration.
2. In `identity.mode=multi_user`, write paths fail fast if required identity envelope fields are missing.
3. In `identity.mode=multi_user`, read paths fail fast unless a privacy policy is registered.
4. Core bootstraps datastore-owned default hooks (`memorydb-default`) automatically so unhooked deployments fail loudly only for true contract violations, not missing wiring.

Required write envelope fields in multi-user mode:
- `source_channel`
- `source_conversation_id`
- `source_author_id`

Required read context in multi-user mode:
- `viewer_entity_id`

Optional scoped read context:
- `participant_entity_ids`
- `subject_entity_id`
- `source_channel` / `source_conversation_id` / `source_author_id`

Docs/project registry forward-compat surface:
- `datastore/docsdb/registry.py` now supports additive identity/source context fields on `doc_registry` entries (`source_*`, `speaker_entity_id`, `subject_entity_id`, `conversation_id`, `visibility_scope`, `sensitivity`, `participant_entity_ids`, `provenance_confidence`).
- These fields are schema-seeded now for future multi-user routing without destructive migrations.

## Event Contract

- Source: `modules/quaid/core/runtime/events.py`
- Core functions:
  - `emit_event(name, payload, source, session_id, owner_id, priority)`
  - `list_events(status, limit)`
  - `process_events(limit, names)`
  - `register_event_handler(name, handler)`
  - `get_event_registry()`

### Built-in events

- `session.new`
- `session.reset`
- `session.compaction`
- `session.timeout`
- `session.agent_start`
- `session.agent_end`
- `notification.delayed`
- `memory.force_compaction`

## MCP vs CLI mapping

### Event operations

- Emit:
  - MCP: `memory_event_emit`
  - CLI: `quaid event emit --name ... --payload ...`
  - Dispatch control: `dispatch=auto|immediate|queued` (`--dispatch` in CLI)
  - `auto` behavior: active events process immediately, passive events stay queued
- List:
  - MCP: `memory_event_list`
  - CLI: `quaid event list`
- Process:
  - MCP: `memory_event_process`
  - CLI: `quaid event process`
- Capabilities:
  - MCP: `memory_event_capabilities`
  - CLI: `quaid event capabilities`

## Adapter requirements (future adapters)

For adapter parity, a new adapter should support:

1. Emitting lifecycle events into Quaid event bus (`session.*`).
2. Providing a tool surface for:
   - read recall
   - write note/store
   - optional event controls (recommended via MCP/CLI passthrough, not adapter-specific tools)
3. Respecting capability discovery (do not assume all event handlers/features exist).

## Known gaps

- MCP and OpenClaw tool schemas are still partly hand-maintained (not fully generated from registry metadata).
- CLI currently routes through Python modules directly; not all surfaces are normalized as a single API package.
- Event processing uses a queue-backed local store today; distributed/remote event transport is not yet implemented.
