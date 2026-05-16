# Datastore Events M4 Explicit Recall Activation Plan

Status: M4.1 activation slice in progress; explicit `stores:["docs"]` CLI recall routes through the broker
Owner: W1 runtime/datastore with W3 recall-quality approval before code
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Decision

M4 has a request contract and synchronous broker fanin mechanics. The next
behavior-changing step must pick one explicit recall path and replace that path
cleanly instead of creating a permanent direct-path plus broker-path split.

First target:

```bash
quaid recall "<query>" '{"stores":["docs"],"project":"<project>"}'
```

Reason:

- It is Python-side, where the broker and datastore manifests already live.
- It is explicit, not routed/LLM-planned recall.
- It has a narrow request event: `recall.docs.request.v1`.
- It exercises the W3 `project` selector concern without changing the TypeScript
  facade or adapter auto-inject path.
- It can be validated with focused CLI output-shape tests before any live run.

Do not switch TypeScript facade recall first. `core/facade.ts` currently calls
the Python bridge through `datastoreBridge.recall(args)`. Switching that layer
first would mix a TS orchestration change with Python broker activation and make
recall-quality regressions harder to localize.

## Proposed Replacement Boundary

Current Python explicit docs/project recall path:

- `datastore/memorydb/memory_graph.py` parses `quaid recall`
- `_resolve_recall_store_request()` resolves `stores=["docs"]`
- `_run_recall_store_plan()` / docs-only branches perform DocsDB search and
  format output

Proposed replacement:

- register a `recall.docs.request.v1` request handler for `docsdb`
- handler receives the M4 payload shape with `selector: "docs"` and `store:
  "docs"` for the current public CLI syntax
- handler calls the DocsDB-owned search implementation and returns rows in the
  existing JSON/text output shape
- the explicit CLI branch calls `request_broker_event(...)` for docs/project
  explicit requests
- the old explicit docs/project direct branch is removed in the same patch
- `selector: "project"` remains a first-class contract route for later callers,
  but this slice does not add `stores:["project"]` as new public CLI syntax

If the handler cannot be activated without keeping the old branch for product
safety, stop and route the incompatibility to Hermes/Solomon instead of adding a
quiet compatibility path.

## Non-Targets

Do not migrate in this slice:

- routed/LLM-planned recall
- vector or graph recall
- session chunk recall
- TypeScript facade recall
- adapter auto-inject recall
- journal recall

## Invariants

- `selector` must preserve the user-facing selector (`project` stays `project`).
- `store` may be handler-local (`project` maps to `store: "docs"`).
- failHard must raise on missing handler, handler failure, malformed response,
  and output-shape mismatch.
- Non-failHard failure may return an error response only if it logs at error
  level and preserves current user-facing failure semantics.
- The activated path must not enqueue request events. Recall requests are
  synchronous fanin calls.
- Parity checks may be used in focused tests, but no permanent shadow mode is
  allowed in production.

## Validation Gate

Before implementation:

- W3 approves or redirects this first target. Status: W3 approved explicit
  Python CLI docs/project recall with parity invariants.

After implementation:

- focused CLI/broker tests for `stores:["docs"]` with project, docs, date, fanout,
  similarity floor, and fallback parity
- focused output-shape tests for docs-only JSON and text rendering
- failHard missing-handler and handler-failure tests
- W6 review for old-path deletion and failHard behavior
- W8 static validation
- W4 smoke/livetest only after W3 and W6/W8 are clear, because this changes
  user-visible recall behavior
