# Datastore Events M6.3 Memory Selector Plan

Status: draft for W3 review; no runtime implementation
Owner: W1 runtime/datastore with W3 recall-quality approval before code
Plan source: `~/quaidcode/util/docs/arch_refactor.md` and
`datastore-events-m6-routed-recall-capability-plan.md`

## Precondition

Do not implement this slice until:

1. W4 records the M6.1 full-livetest gate as green, or Solomon/Hermes
   explicitly overrides it.
2. M6.2a project-descriptor broker activation is either completed and
   validated, or W3 explicitly approves doing memory selectors first.
3. W3 approves this memory selector mapping before code.
4. W6 reviews the replacement boundary before live validation.

This document is planning only. It does not change runtime behavior.

## Problem

`vector_basic` and `vector_technical` are routed/default recall selectors in the
TypeScript orchestrator, but they are not separate concrete datastores. The
lower runtime already stores both in MemoryDB vector recall. The architectural
direction is:

- canonical concrete store: `vector`
- selector-specific behavior: domain policy
- no permanent legacy direct callpath after a selector is migrated

M5 activated only explicit Python CLI `stores:["vector"]`. It intentionally
nacks `vector_basic` and `vector_technical` because those selectors need
separate recall-quality review for domain defaults and routed/default behavior.

## Recommended Contract

Use a canonical vector handler store while preserving the routed selector for
diagnostics and result labeling:

| Routed selector | Request event | Payload selector | Payload store | Required domain policy |
| --- | --- | --- | --- | --- |
| `vector_basic` | `recall.memory.request.v1` | `vector_basic` | `vector` | `{ personal: true }` unless explicitly overridden by existing opts |
| `vector_technical` | `recall.memory.request.v1` | `vector_technical` | `vector` | `{ technical: true }` unless explicitly overridden by existing opts |

Rationale:

- `store:"vector"` reflects the concrete datastore handler and avoids keeping
  legacy aliases as primary datastore truth.
- `selector:"vector_basic"` / `selector:"vector_technical"` preserves current
  router diagnostics, result labels, and W3 review visibility.
- Domain policy stays explicit in request options instead of hidden inside the
  datastore handler.

If W3 prefers a different mapping, it should be called out before code. In
particular, mapping both `selector` and `store` to `vector` would be leaner but
would lose selector-level diagnostics unless the original selector is carried in
another explicit field.

## Replacement Boundary

Current routed/default path:

- `core/knowledge-engine.ts` descriptor `vector_basic` calls
  `deps.recallMemory(..., { stores:["vector_basic"], domain:{personal:true},
  ... })`.
- descriptor `vector_technical` calls `deps.recallMemory(...,
  { stores:["vector_technical"], domain:{technical:true}, ... })`.
- facade/bridge normalization maps both selectors to handler store `vector`
  before Python bridge calls.
- MemoryDB ultimately runs vector recall with the domain defaults.

Proposed replacement:

- Update recall request metadata so `vector_basic` and `vector_technical`
  route to `recall.memory.request.v1` with handler store `vector`.
- Register or extend the MemoryDB memory request handler to accept exactly:
  - `selector:"vector", store:"vector"`
  - `selector:"vector_basic", store:"vector"`
  - `selector:"vector_technical", store:"vector"`
- The handler must nack `graph`, `session_chunks`, `source_chunks`, `journal`,
  `docs`, `project`, and unknown selectors.
- The TypeScript descriptors call the request broker only for
  `vector_basic`/`vector_technical` in this slice.
- The old direct `deps.recallMemory` descriptor calls for the migrated
  selectors are deleted in the same patch.

Do not add a shadow fallback to the old direct descriptor after a broker
failure. A migrated path either succeeds through the broker or fails according
to failHard/fail-soft policy.

## Not In Scope

- M6.2a project descriptor.
- Explicit Python CLI `stores:["vector_basic"]` or
  `stores:["vector_technical"]`.
- aggregate explicit `stores:["vector"]` beyond the existing M5 path.
- mixed `vector+docs` recall.
- `graph` traversal, candidate-pool expansion, or graph metadata.
- `session_chunks` / `source_chunks`.
- `journal`.
- router prompt text, router-visible stores, or default store order.
- result merge, dedup, sorting, source-type boosts, or project-row
  preservation.

## Required Behavior Invariants

The brokered `vector_basic` and `vector_technical` descriptors must preserve:

- routed/default store selection order:
  - flat: `vector_basic`, `journal`, `project`
  - expand-graph: `vector_basic`, `graph`, `journal`, `project`
- selector-specific domain defaults:
  - `vector_basic` -> `{ personal: true }`
  - `vector_technical` -> `{ technical: true }`
- explicit caller domain overrides where the current path permits them
- `domainBoost`
- `project`
- `dateFrom` / `dateTo`
- `fast`
- candidate-pool behavior: neither selector consumes a candidate pool
- result row shape, including `via`/category/source metadata
- downstream result merge, dedup, source-type boost, and final limit behavior
- failHard behavior for missing handler, handler exception, malformed response,
  nacked response, and invalid output shape
- fail-soft behavior: log loudly and preserve current partial-result semantics
  for other stores in the same `_executeStores()` fanout

## Tests Before W4 Smoke

Before live validation, add focused tests proving:

- request payload for `vector_basic` preserves `selector:"vector_basic"`,
  `store:"vector"`, and `{ personal:true }` domain policy
- request payload for `vector_technical` preserves
  `selector:"vector_technical"`, `store:"vector"`, and `{ technical:true }`
  domain policy
- explicit domain overrides still win where the current descriptor path allows
  overrides
- `graph`, `journal`, `session_chunks`, `source_chunks`, `project`, and
  aggregate `vector` do not use this broker path
- missing handler raises under failHard and logs/fails loudly under fail-soft
- nacked handler raises under failHard and does not fall back to
  `deps.recallMemory`
- malformed response raises under failHard
- handler exception raises under failHard
- mixed default results preserve current merge/dedup/source-type boost behavior

## W4 Smoke After Code

After W3/W6/W8 approve an implementation slice, W4 smoke should cover:

- default flat routed recall still returns personal vector facts through
  `vector_basic`
- technical-vector routed recall path where fixture data exists
- expand-graph recall still uses graph on the existing path and preserves
  candidate-pool behavior
- journal recall still uses the TypeScript journal scanner
- project recall still follows the M6.2a project decision for the current
  stack
- explicit `stores:["vector"]` still follows the M5 path
- negative failHard case for a nacked memory selector
