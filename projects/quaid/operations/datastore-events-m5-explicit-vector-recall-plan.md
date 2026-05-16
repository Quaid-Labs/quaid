# Datastore Events M5 Explicit Vector Recall Plan

Status: proposed next slice, not implemented
Owner: W1 runtime/datastore with W3 recall-quality approval before code
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Decision Needed

M4.1 activated the narrow explicit docs-only CLI path:

```bash
quaid recall "<query>" '{"stores":["docs"],"project":"<project>"}'
```

The next M5 behavior-changing slice should stay equally small. Recommended
target:

```bash
quaid recall "<query>" '{"stores":["vector"]}'
```

This is the smallest MemoryDB request activation because it avoids graph
traversal, session chunk expansion, journal file scanning, TypeScript facade
routing, and adapter auto-inject.

## Why Not Mixed Or Routed Recall Yet

Do not activate these in this slice:

- `stores:["vector","docs"]` mixed recall
- `stores:["graph"]`
- `stores:["session_chunks"]` or `stores:["source_chunks"]`
- `stores:["vector_basic"]` or `stores:["vector_technical"]`
- archive recall (`archive:true`)
- session-filtered recall (`session_id`)
- routed/default recall where the planner chooses stores
- TypeScript facade recall
- adapter auto-inject recall

Reasons:

- Mixed recall currently depends on `_run_recall_store_plan()` ranking and merge
  behavior. Activating it with vector-only would obscure regressions.
- `graph` and `session_chunks` have separate traversal/window semantics and
  should get their own W3-reviewed slices.
- `vector_basic` and `vector_technical` are facade/router concepts that require
  domain-default parity review before activation.
- Archive and session-filtered recall branches currently run before the
  vector-only branch and have different output/source semantics; they must stay
  on their existing paths.
- Routed/default recall changes planner behavior and belongs after explicit
  capability slices prove the broker path.

## Replacement Boundary

Current Python explicit vector recall path:

- `datastore/memorydb/memory_graph.py` parses `quaid recall`
- `_resolve_recall_store_request()` resolves `stores:["vector"]`
- the vector-only branch builds `recall_kwargs`
- `recall(query, ...)` returns rows or `(rows, meta)` depending on JSON mode
- `_build_recall_json_payload()` / `_print_recall_results()` render output

Proposed replacement:

- register a `recall.memory.request.v1` request handler for `memorydb`
- handler receives payload with `selector:"vector"` and `store:"vector"`
- because `recall.memory.request.v1` is also used by excluded selectors
  (`vector_basic`, `vector_technical`, and `session_chunks`), the handler must
  validate both fields and nack any payload where `selector!="vector"` or
  `store!="vector"`; do not silently process other memory selectors through the
  vector path
- handler calls the existing vector recall implementation with the same
  explicit CLI options
- CLI branch calls `request_broker_event(...)` only for explicit
  `stores:["vector"]`
- old explicit vector-only direct call in the CLI branch is deleted in the same
  patch
- archive/session-filtered, mixed/vector+docs, vector_basic/vector_technical,
  session_chunks, graph, and routed/default recall remain on the existing path
  until their own slices

## Required Parity Invariants

The brokered vector-only path must preserve current behavior for:

- `limit`
- `owner`
- `min_similarity`
- `domain_filter`
- `domain_boost`
- `project`
- `date_from` / `date_to`
- `temporal_dimension`
- `current_session_id`
- `compaction_time`
- `timeout_ms`
- `candidate_pool`
- `planner_profile`
- `planned_queries` / `planner_meta` when already present
- `fast` overrides (`use_multi_pass=false`, `use_reranker=false`,
  `max_turns=1`, `use_routing=false`)
- `include_chunks`, `max_chunk_tokens`, and `max_total_chunk_tokens`
- JSON payload contract and meta shape
- text `_print_recall_results()` shape

The request must be synchronous fanin only. It must not enqueue request events.

## Failure Policy

- `failHard=true`: raise on missing handler, handler exception, nacked/failed
  response, malformed response, invalid row shape, and invalid meta/output
  shape.
- `failHard=false`: log at error level and preserve current CLI failure
  semantics.
- Do not add a fallback that calls the old vector direct branch after a broker
  failure. That would preserve a legacy callpath after migration.

## Suggested Tests

- explicit vector-only JSON recall preserves rows/meta shape
- explicit vector-only text recall preserves `_print_recall_results()` shape
- fast-mode vector recall preserves fast overrides
- date/project/domain filters are forwarded
- include_chunks options are forwarded and output shape remains valid
- missing handler failHard/non-failHard
- handler exception failHard/non-failHard
- malformed handler response failHard
- mixed `stores:["vector","docs"]` does not use the vector broker handler in this
  slice
- `stores:["vector"]` with `archive:true` or `session_id` does not use the vector
  broker handler
- direct handler calls with `selector:"vector_basic"`,
  `selector:"vector_technical"`, or `selector:"session_chunks"` nack rather than
  processing as vector recall

## Validation Gate

Before implementation:

- W3 approves or redirects this target and invariants.

After implementation:

- focused vector CLI/broker tests
- W3 recall-quality review
- W6 review for old-path deletion and failHard behavior
- W8 static validation
- W4 smoke with explicit vector JSON/text and mixed vector+docs non-broker check
