# Datastore Events M4 Recall Request Contract

Status: implemented; request contract and synchronous fanin available, behavior activation remains per-slice
Owner: W1 runtime/datastore, with W3 recall-quality review before behavior activation
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Scope

M4 starts the recall request-event migration by defining the request contract and
store-selector routing metadata. It does not switch production recall output.

Solomon direction on 2026-05-15 still applies: migrated paths should not keep
legacy direct callpaths. For M4 that means:

- existing production recall remains the source of truth until a later milestone
  replaces a specific path
- temporary shadow/parity checks may exist only as proof tooling
- once an explicit recall path is activated through the broker/datastore
  contract, the corresponding old direct path should be deleted in the same
  milestone unless Solomon approves an alpha compatibility shim

## Contract Surface

`core.contracts.recall` maps current user-facing recall selectors to manifested
datastore request handlers.

Current request events:

| Selector family | Event type | Datastore | Handler store |
| --- | --- | --- | --- |
| `vector`, `vector_basic`, `vector_technical` | `recall.memory.request.v1` | `memorydb` | matching vector store |
| `graph` | `recall.graph.request.v1` | `memorydb` | `graph` |
| `session_chunks` / `source_chunks` | `recall.memory.request.v1` | `memorydb` | `session_chunks` |
| `docs` | `recall.docs.request.v1` | `docsdb` | `docs` |
| `project` | `recall.docs.request.v1` | `docsdb` | `docs` |
| `project_context` | `recall.project_context.request.v1` | `docsdb` | `project_context` |
| `journal` | `recall.journal.request.v1` | `evolutiondb` | `journal` |

The request payload shape for each selected route is:

```json
{
  "query": "user query",
  "limit": 5,
  "selector": "project",
  "store": "docs",
  "datastore_id": "docsdb",
  "options": {}
}
```

`selector` preserves the normalized user-facing selector for diagnostics and
result labeling. `store` is the handler-local store key the datastore handler
will receive when that request path is activated. For example, `project` remains
`project` at the contract boundary while the docs datastore handler receives
`store: "docs"`.

If a recall request event is manually dispatched before M4 handler activation,
the event fails closed with `request handler not activated in M4`. It must not be
silently marked processed by the generic no-handler path.

## Registry Alignment

The M4 contract is validated against:

- `core.datastore_registry` first-party manifests
- `core.runtime.events.EVENT_REGISTRY`
- M3 `core.contracts.datastore` first-party handler declarations

M4 adds the missing `recall.journal.request.v1` metadata to `evolutiondb` because
`journal` is already a routed recall selector in the TypeScript knowledge-store
registry. This is metadata-only; it does not activate a journal handler.

## Broker Request Mechanics

M4 also adds synchronous broker request/fanin mechanics:

- `register_request_handler(event_type, handler, datastore_id=...)`
- `request_broker_event(event_type, payload, ...)`

This is separate from the existing queue-backed `emit` / `dispatch` path. Recall
requests need immediate responses and correlation ids; they must not be queued
and later marked processed with no response body.

The request broker fans out to all registered in-process handlers for the event
type and returns aggregate response metadata. Missing handlers or nacked/failed
handler responses fail loudly under `failHard=true`. With `failHard=false`, they
return a failed/partial response and log at error level.

The initial M4 contract slice registered no first-party recall datastore
handler. M4.1 then activated only the explicit Python CLI `stores:["docs"]`
path through `recall.docs.request.v1`; other recall request handlers remain
unactivated until their own reviewed migration slices.

## Activation Gate

M4 contract metadata is safe for W1 to implement. Activating recall behavior is
not W1-only:

- W3 must review the request mapping before any recall output changes.
- W8 static validation must cover the contract tests.
- W6 must review the boundary for direct-path leakage and failHard handling.
- W4/live validation is required before switching explicit or routed recall.

The next proposed implementation slice is explicit Python CLI
`stores:["vector"]` recall, tracked in
`datastore-events-m5-explicit-vector-recall-plan.md`. It should replace that
direct path end-to-end instead of leaving a permanent dual-run path.
