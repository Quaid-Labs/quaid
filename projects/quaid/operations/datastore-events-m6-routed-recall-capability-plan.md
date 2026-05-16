# Datastore Events M6 Routed Recall Capability Plan

Status: M6.1 implemented/validated; behavior activation blocked on W4 full-livetest gate
Owner: W1 runtime/datastore with W3 recall-quality approval before code
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Precondition

M5 explicit recall activation is not enough by itself to start M6 behavior
changes. The migration plan requires W4 full livetest across CC/CDX/OC before
migrating routed recall.

Until that gate is recorded, M6 work should stay at planning, metadata, and
test-design level unless Solomon explicitly overrides the gate.

## Decision Needed

M6 should not be a broad recall rewrite. The first routed-recall slice should be
the smallest capability-registry replacement that removes duplicated store
catalogs without changing recall quality:

- keep the current LLM router prompt shape and store defaults
- keep current store execution functions and result merge/ranking behavior
- replace scattered hardcoded store eligibility lists with core-owned registry
  metadata
- validate planner-selected stores against the same registry metadata

This is a routing/catalog cleanup milestone, not a new broker activation for
graph, session chunks, journal, or mixed recall.

## Current Hardcoded Surfaces

Known surfaces to replace or explicitly leave alone:

- `core/knowledge-stores.ts`
  - already owns the TypeScript store registry and router descriptions
  - currently does not expose execution surface, bridge eligibility, handler
    store, or alias metadata for all consumers
- `core/knowledge-engine.ts`
  - uses `getRoutableDatastoreKeys()` and
    `renderRoutableKnowledgeDatastoreRouterGuidance()` from the core registry
  - still owns a hardcoded `descriptors` map for store execution
  - still has `_vectorStores` as a separate set for candidate-pool behavior
- `core/facade.ts`
  - still owns `bridgeOnlyStores = new Set([...])`
  - still normalizes `vector_basic` / `vector_technical` to `vector` and
    `project` to `docs` before bridge calls
  - still excludes `journal` from bridge-only explicit recall even though the
    knowledge-store registry lists `journal` as routable/default-enabled
- `core/contracts/recall.py`
  - owns Python request-route metadata for selectors, aliases, event types,
    datastore ids, and handler stores
  - is currently Python-only and not consumed by the TypeScript router/facade
- `core/datastore_registry.py`
  - owns first-party datastore manifests and recall capabilities
  - is currently Python-only and not consumed by TypeScript routed recall

## Proposed M6.1 Slice

Add enough metadata to the TypeScript core store registry for current TypeScript
consumers to stop carrying duplicate catalogs:

- `routable`: whether the LLM router may select the store
- `bridgeEligible`: whether explicit non-routed facade recall may use the
  Python bridge path directly
- `handlerStore`: bridge/datastore-local store name when different from the
  user-facing selector
- `aliases`: accepted compatibility names such as `source_chunks`
- `defaultDomain`: domain filter applied by `vector_basic` and
  `vector_technical`
- `usesCandidatePool`: whether prior vector rows should seed this store

Then update consumers to read this metadata:

- `getRoutableDatastoreKeys()` derives from `routable`
- facade `bridgeOnlyStores` derives from `bridgeEligible`
- facade bridge-store normalization derives from `handlerStore`
- router/planner validation rejects only stores absent from registry or not
  routable

Working-system rule: this slice must be behavior-preserving. It should not
change which stores the router sees, which explicit stores use the bridge, or
how result ranking/merge works.

## Not In Scope For M6.1

- no broker activation for routed/default recall
- no broker activation for mixed `vector+docs`
- no broker activation for `graph`
- no broker activation for `session_chunks` / `source_chunks`
- no broker activation for `journal`
- no change to `recall.memory.request.v1` handler scope from M5
- no change to router cost/latency ordering
- no change to vector_basic/vector_technical domain defaults
- no change to graph candidate-pool seeding
- no change to project-row preservation
- no change to auto-inject prompt timing or adapter behavior

These are later behavior slices after W3 review.

## Required Parity Invariants

M6.1 must preserve:

- flat default stores: `vector_basic`, `journal`, `project`
- expand-graph default stores: `vector_basic`, `graph`, `journal`, `project`
- router-visible stores: all current routable stores except aggregate `vector`
  and explicit-only `session_chunks`
- router guidance wording and cost/latency order unless W3 approves a prompt
  change
- `source_chunks` alias normalization to `session_chunks`
- `vector_basic` default domain `{ personal: true }`
- `vector_technical` default domain `{ technical: true }`
- `project` bridge handler store mapping to `docs`
- `graph` depth/domain/project option behavior
- `session_chunks` max chunk token options
- failHard behavior when a store execution path fails

## Test Plan

Before any behavior-changing code:

- W3 reviews this plan and either approves M6.1 as the first slice or redirects.

For M6.1 code:

- TypeScript unit tests that registry metadata reproduces current:
  - routable store list
  - flat and expand-graph defaults
  - bridge-only explicit store set
  - handler-store normalization for `vector_basic`, `vector_technical`,
    `project`, and `source_chunks`
- knowledge-engine tests proving routed store selection remains unchanged for
  representative router outputs
- facade tests proving explicit bridge routing remains unchanged
- failHard test proving store execution errors still raise when enabled
- W3 recall-quality review
- W6 boundary review
- W8 static validation
- W4 full livetest before any routed-recall behavior slice beyond M6.1

## Open Questions Before Code

- Should the TypeScript registry eventually be generated from Python
  `core.contracts.recall` / `core.datastore_registry`, or should M6 keep a
  TypeScript core registry as the canonical runtime source for adapter-facing
  recall?
- Should `journal` become bridge-eligible, or should it remain routed through
  the TypeScript journal file scanner until the `evolutiondb` broker slice?
- Should invalid router-selected stores fail immediately under failHard, or
  continue the current repair/validation behavior where invalid store names are
  ignored until no valid stores remain?

## W3 Resolution For M6.1

W3 approved M6.1 metadata-only code with conservative answers:

- Do not generate the TypeScript registry from Python contracts in this slice.
  Keep `core/knowledge-stores.ts` as the adapter-facing source for current
  TypeScript recall runtime metadata.
- Do not make `journal` bridge-eligible. Keep journal recall on the current
  TypeScript journal scanner path until a separate `evolutiondb`/journal slice
  is reviewed.
- Preserve current invalid-router-store behavior. Invalid router-selected store
  names continue to be filtered/repair-handled as today and fail only when the
  current code would fail. Do not tighten failHard semantics in M6.1.
- Add exact snapshot/parity tests for rendered router guidance and default store
  order, not only set equality.

## M6.1 Implementation Record

M6.1 landed as a metadata-only TypeScript runtime cleanup:

- `e5164fcd8` `refactor(core): route recall metadata through store registry`
- `bad9138da` `test(core): pin flat recall store defaults`

Implemented behavior-preserving changes:

- `core/knowledge-stores.ts` owns `routable`, `bridgeEligible`,
  `handlerStore`, `aliases`, `defaultDomain`, and `usesCandidatePool`
  metadata for the current TypeScript recall runtime.
- `core/knowledge-engine.ts` consumes registry metadata for vector-store
  detection, default domains, and graph candidate-pool seeding.
- `core/facade.ts` consumes registry metadata for explicit bridge eligibility
  and handler-store normalization.
- `journal` remains routable through the current TypeScript journal scanner but
  is not bridge-eligible.
- `source_chunks` remains an alias for `session_chunks`.
- No broker activation was added for routed/default, mixed, graph,
  `session_chunks`, or journal recall.

Validation:

- W3 approved recall-routing parity.
- W6 approved behavior preservation and boundary scope.
- W8 static passed runtime pairs, affected TypeScript tests, docs consistency,
  and diff checks.
- Local affected suite: `tests/knowledge-orchestrator.test.ts` and
  `tests/facade.test.ts` passed with 148 tests.

## W4 Full-Livetest Gate Before M6.2

Do not start routed/default recall behavior activation beyond M6.1 until W4
records a full livetest pass across CC, CDX, and OC on a stack that includes
M4/M5 explicit recall activation and M6.1 metadata cleanup.

Gate expectations:

- Normal M2/M3 extraction and auto-inject behavior remains unchanged.
- Explicit `stores:["docs"]` recall still uses the M4 broker path and preserves
  docs-only JSON/text output.
- Explicit `stores:["vector"]` recall still uses the M5 broker path and
  preserves vector JSON/text output, fast mode, chunk attachment, and temporal
  filters.
- Mixed `vector+docs`, default/routed recall, graph recall, session/source
  chunk recall, and journal recall remain on their existing non-M6 broker paths.
- Router-visible stores and router guidance remain unchanged.
- `journal` remains non-bridge and still uses the TypeScript journal scanner.
- No failHard violations or provider/deferred-notice regressions appear during
  the full run.

If W4 finds a recall-quality or runtime regression, stop M6 behavior planning
and route the issue to W1 or W3 according to normal ownership. If W4 passes,
the next M6 behavior slice still requires a fresh W3 plan review before code.

## Candidate M6.2 Slice: Routed Store Execution Request Boundary

Status: draft only. Do not implement until:

1. W4 records the full-livetest gate above as green, or Solomon explicitly
   overrides it.
2. W3 reviews and approves this M6.2 mapping before code.
3. W6 reviews the replacement boundary before live validation.

### Replacement Target

The remaining TypeScript runtime execution catalog lives in
`core/knowledge-engine.ts` `_executeStores()`:

- descriptor functions for `vector`, `vector_basic`, `vector_technical`,
  `graph`, `journal`, `project`, and `session_chunks`
- result accumulation, dedup, source-type boosts, sorting, and project-row
  preservation
- failHard propagation for store execution failures

M6.2 should not rewrite merge/ranking. The first behavior slice should replace
only the execution call boundary for the smallest routed/default descriptor
with reviewed broker-adjacent behavior and leave the `_executeStores()`
merge/ranking frame intact.

### Proposed First Behavior Slice: M6.2a Project Descriptor Only

Activate broker/request execution only for the routed/default `project`
descriptor:

- `project` through the docs request path, preserving `project`, `docs`,
  `dateFrom`, and `dateTo` filters and project-row preservation after merge.
- The request payload must preserve `selector: "project"` and `store: "docs"`
  at the contract boundary. The M4 explicit docs CLI slice used
  `selector: "docs"` for public `stores:["docs"]`; that hardcoded selector is
  not acceptable for routed/default project-descriptor activation.
- This slice does not add `stores:["project"]` as public CLI syntax. It only
  migrates the internal routed/default project descriptor.

Keep these descriptors on the current path in M6.2a:

- `vector_basic` and `vector_technical`: these were not reviewed as explicit
  broker-active selectors. M5 approved aggregate explicit `stores:["vector"]`
  and required the memory handler to nack `vector_basic` and
  `vector_technical`. Leave personal/technical vector recall on the current
  `deps.recallMemory` descriptor path until a separate M6.2b/M6.3 memory-scope
  plan is approved by W3.
- `graph`: graph traversal, candidate-pool semantics, and graph metadata
  preservation remain too coupled to ranking and require a separate W3 review.
- `session_chunks` / `source_chunks`: session evidence windows remain
  explicit-only and require a separate sessiondb/source-window slice.
- `journal`: stays on the TypeScript journal scanner until an
  `evolutiondb`/journal broker slice is reviewed.
- aggregate `vector`: not router-visible; keep it on its current explicit path
  unless a later explicit-aggregate slice needs it.

### Deferred M6.2b/M6.3 Memory Selector Slice

Before `vector_basic` or `vector_technical` can use broker/request execution,
W3 must review a separate memory-handler scope change:

- Option A: update the memory request handler to accept exactly `vector`,
  `vector_basic`, and `vector_technical`, preserving selector-specific default
  domains.
- Option B: map routed `vector_basic`/`vector_technical` to selector `vector`
  with explicit domain defaults, accepting the diagnostic/result-label tradeoff
  only if W3 approves it.

Either option must continue to nack `graph`, `session_chunks`, `source_chunks`,
`journal`, and unknown selectors. Do not bundle this with M6.2a.

### Required Behavior Invariants

M6.2a must preserve:

- router prompt text and router-visible store list
- flat and expand-graph default store order
- invalid router-output repair/filter behavior
- `vector_basic` default domain `{ personal: true }` by leaving it on the
  current path
- `vector_technical` default domain `{ technical: true }` by leaving it on the
  current path
- `project` docs/project/date filter semantics
- `project` result row shape, including `via`/category/source metadata
- result dedup keys, source-type boosts, sorting, and final limit behavior
- project-row preservation when `project` is selected
- failHard behavior: missing handler, handler failure, malformed response, and
  nacked response must raise when `retrieval.fail_hard=true`
- fail-soft behavior: degraded request execution must log loudly and return the
  same user-visible failure shape as the current path where applicable
- no fallback to the old direct descriptor after a broker request failure in the
  migrated descriptor

### Tests Before W4 Smoke

Add focused tests before any live deployment:

- routed/default flat recall still selects `vector_basic`, `journal`, and
  `project` in the same order
- expand-graph default still selects `vector_basic`, `graph`, `journal`, and
  `project`
- routed `project` broker request preserves `selector: "project"`,
  `store: "docs"`, docs/project/date filters, output shape, `via`, and
  project-row preservation
- `vector_basic`, `vector_technical`, `graph`, `journal`, `session_chunks`,
  and aggregate `vector` descriptors do not use the M6.2a broker path
- malformed/nacked/missing-handler request responses raise under failHard and
  do not silently fall back to the old direct descriptor
- mixed default results keep existing dedup/sort/source-type boost behavior

### Pre-Activation Guard Record

The following test-only commits pin the current descriptor baseline before any
M6.2a behavior code:

- `ffb272512` pins project docs/project/date filters, project row
  `via`/category/source metadata, and project-row preservation in mixed-store
  merge.
- `2193dc0d8` pins current project descriptor failure policy: fail-soft logs and
  preserves vector rows; failHard rethrows.
- `7034003f1` pins deferred `vector_basic`/`vector_technical` direct
  `recallMemory` descriptors and selector-specific default domains.
- `120ea77af` pins non-target `graph` direct memory recall with vector
  candidate-pool seeding and non-target `journal` direct journal-store recall.
- `069dea3b6` pins aggregate `vector` direct memory recall with store, domain,
  project, and date filters preserved.
- `7412d26fc` pins `source_chunks` alias normalization to the direct
  `session_chunks` memory descriptor with chunk-token options preserved.

Future M6.2a code must keep these tests green and add broker-specific coverage
for `selector: "project"` / `store: "docs"` threading, missing/nacked/malformed
broker responses, and no fallback to the old project descriptor after broker
failure.

### W4 Smoke After Code

After W3/W6/W8 approve a code slice, W4 smoke should cover:

- default/routed recall with project doc results
- default/routed recall with personal facts still present through the unchanged
  vector_basic path
- expand-graph recall confirming graph still works and remains on the old path
- journal result availability when journal data exists
- explicit `stores:["docs"]` and `stores:["vector"]` still use the M4/M5 paths
- negative case proving missing/nacked broker request surfaces loudly under
  failHard
