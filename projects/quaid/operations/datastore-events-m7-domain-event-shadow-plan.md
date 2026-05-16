# Datastore Events M7 Domain Event Shadow Plan

Status: implementation submitted; pending W6/W8/W4 validation
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Precondition

Do not implement this milestone until:

1. M6 routed recall capability work is closed or explicitly deferred by
   Solomon/Hermes.
2. W4 records the required full-livetest gate as green for the active M6 stack,
   or Solomon/Hermes explicitly overrides it.
3. W6 reviews and approves the implementation patch before runtime code lands.
4. W8 static validation is available for the affected supervisor/docs tests.

This document is planning only. It does not change runtime behavior.

## M7 Goal

M7 starts moving monitors toward producer-only behavior without changing the
source of truth yet:

- a monitor emits a typed domain event
- the current direct write path remains authoritative
- a datastore listener runs in shadow mode and records what it would do
- tests compare event payload, direct result, and shadow intent

The old direct write path is not removed in M7. Removal is the M8 step for the
same selected path after shadow parity is proven.

## Selected First Slice

Use the project-docs supervisor docs registration/freshness tick as the first
shadowed monitor path.

Current source-of-truth path:

- `core/project_docs_supervisor.py` periodically calls
  `project_docs.auto_register_project_docs()`
- the same supervisor loop periodically calls
  `project_docs.index_one_stale_registered_doc()`
- `core/project_docs.py` delegates those operations into
  `core.docs.updater`
- `core.docs.updater` composes DocsDB-owned registry/RAG operations

Why this path:

- It matches the migration plan's preferred low-risk docs
  registration/freshness area.
- It is supervisor-owned monitor work, not adapter or recall behavior.
- It already has focused tests around supervisor ticking, auto-register, stale
  indexing, and failHard propagation.
- It is lower risk than project-doc worker `execute_update_once`, which mixes
  shadow git snapshots, PROJECT.log queue commits, doc editing, registry sync,
  indexing, cursor advancement, and user-visible notices.

## Event Boundary

Introduce one domain event for the supervisor tick:

```text
docs.project_maintenance_observed
```

The implementation uses the `docs.*` prefix to match the active
`core/runtime/events.py` naming convention used by `docs.ingest_transcript`.

Submitted payload shape:

```json
{
  "project": "optional-project-name-or-null",
  "observed_at": "ISO-8601 timestamp",
  "source": "project-docs-supervisor",
  "tick_kind": "auto_register_and_stale_index",
  "auto_register_interval_seconds": 300.0,
  "stale_index_interval_seconds": 60.0,
  "direct_result": {
    "auto_register_ran": true,
    "stale_index_ran": true,
    "registered": 1,
    "indexed_one": true,
    "errors": []
  },
  "dry_run": false
}
```

Payload rules:

- Include the direct result because M7 shadow mode compares listener intent
  against the authoritative direct path.
- Do not include document bodies, raw diffs, credentials, or local-only process
  environment.
- If a future implementation splits auto-register and stale-index ticks into
  separate events, W6 should review that split before code. The first slice
  should prefer one small event unless tests show the combined tick obscures
  failHard or parity.

## Shadow Listener Boundary

Register a DocsDB shadow listener for
`docs.project_maintenance_observed`.

The listener must:

- validate the event payload
- record shadow intent under hidden project-docs runtime state or broker trace
  state
- report what it would handle:
  - auto-register project docs
  - index one stale registered doc
- not call `auto_register_project_docs`
- not call `index_one_stale_registered_doc`
- not write docs registry rows
- not index docs chunks
- not update visible project files

The shadow output is diagnostic only. Direct supervisor calls remain the source
of truth throughout M7.

## Not In Scope

- removing the direct supervisor calls
- project-doc worker `execute_update_once`
- project-log queue commits
- `PROJECT.md` / `TOOLS.md` / `AGENTS.md` editing
- docs RAG search/recall
- adapter hooks or auto-inject
- janitor request handling
- lifecycle/session/extraction event migration

## FailHard Policy

- Event emission failure under `failHard=true` must raise and expose the error.
- Shadow listener validation failure under `failHard=true` must raise.
- With `failHard=false`, emission/listener failures may log loudly and preserve
  the direct supervisor write path.
- Do not let shadow-listener failure silently suppress direct supervisor work in
  fail-soft mode.
- Do not add fallback routing that bypasses broker failHard checks.

## Tests Before W4 Smoke

Focused tests should prove:

- supervisor tick emits `docs.project_maintenance_observed` when the
  auto-register/stale-index interval fires
- event payload includes direct result metrics and omits document bodies and
  environment secrets
- direct calls to `auto_register_project_docs` and
  `index_one_stale_registered_doc` still run in M7
- shadow listener records would-handle intent without calling DocsDB write/index
  functions
- shadow listener failure raises under `failHard=true`
- shadow listener failure logs loudly and leaves direct supervisor work intact
  under `failHard=false`
- no project-doc worker `execute_update_once` path emits this event in M7

## Plan Review Record

The M7 domain-event shadow plan is approved as a plan. Runtime implementation is
now submitted for validation; runtime code still requires the preconditions
above and fresh review of the implementation patch before landing.

Reviewed plan commit:

- `fce24801b` drafted the M7 shadow plan for the project-docs supervisor
  auto-register/stale-index tick.

Review status:

- W3 confirmed from recall-quality scope because direct project-docs supervisor
  behavior remains authoritative and docs RAG search/recall plus project-doc
  worker update paths are explicitly out of scope.
- W6 approved the shadow-first boundary and recorded an implementation-phase
  note to align the final event name with the existing event registry naming
  pattern.
- W8 passed docs-static validation for the plan.

Implementation guardrails carried forward:

- shadow traces must not change project/docs recall inputs, row metadata,
  indexing cadence, or failHard behavior without fresh W3 review
- shadow listener failure must not suppress the authoritative direct path in
  fail-soft mode
- no project-doc worker `execute_update_once` path should emit this event in M7

## Pre-Implementation Guard Record

Closed guard tests:

- Pre-existing `test_supervisor_runs_docs_rag_refresh_ticks` pins that the
  supervisor docs-maintenance tick directly calls
  `auto_register_project_docs` and `index_one_stale_registered_doc` in order
  before any M7 shadow event is added.
- `e11c4942c` pins that project-doc worker `execute_update_once` does not call
  the supervisor-only docs maintenance tick primitives
  `auto_register_project_docs` or `index_one_stale_registered_doc`. This keeps
  the future M7 shadow event scoped to the supervisor tick instead of the
  worker apply path.
- `0d194cadb` pins the supervisor auto-register tick failure policy before
  shadowing: fail-soft logs loudly and continues to the stale-index tick;
  failHard re-raises the original error and does not run stale indexing.
- `1622431f8` pins the sibling stale-index tick failure policy before
  shadowing: fail-soft logs loudly after auto-register completes; failHard
  re-raises the original stale-index error.

Remaining future behavior-slice coverage:

- W6 implementation review of the finalized event name, event payload, and
  shadow-only listener boundary
- W8 static validation for the event and supervisor tests
- W4 smoke proving supervisor docs maintenance still works and shadow traces are
  present in the installed runtime

## W4 Smoke After Code

After W6/W8 approve an implementation patch, W4 smoke should cover:

- supervisor starts normally
- docs auto-registration still happens
- stale registered docs still index
- project docs worker updates still work through the existing path
- event/shadow traces are present for the supervisor maintenance tick
- no failHard violations during normal CC/CDX/OC docs/project milestones

## M8 Handoff Criteria

M7 is ready to hand off to M8 only when:

- direct result and shadow intent agree in focused tests
- W4 smoke confirms the selected monitor path still works
- W6 confirms no unrelated monitor/write paths were touched
- `datastore-events-m8-authoritative-docs-listener-plan.md` is reviewed as the
  concrete plan to remove only this selected direct write path and make the
  DocsDB listener authoritative
