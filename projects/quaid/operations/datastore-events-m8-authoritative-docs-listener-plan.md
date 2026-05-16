# Datastore Events M8 Authoritative Docs Listener Plan

Status: draft plan; blocked until M7 shadow parity is implemented and validated
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Precondition

Do not implement this milestone until:

1. M7 is implemented for the selected project-docs supervisor maintenance tick.
2. Focused M7 tests prove the direct supervisor result and DocsDB shadow intent
   agree.
3. W4 smoke confirms the supervisor still auto-registers docs, indexes stale
   docs, and leaves project-doc worker updates untouched.
4. W6 approves the M8 implementation patch before runtime code lands.
5. W8 static validation covers the affected event, DocsDB, and supervisor tests.
6. W3 reviews the implementation if any docs recall indexing cadence, recall
   inputs, row metadata, or project/docs result shape can change.

This document is planning only. It does not approve runtime implementation.

## M8 Goal

M8 removes the direct supervisor write path selected in M7 and makes the DocsDB
listener authoritative for that one path.

The selected path is only the project-docs supervisor docs maintenance tick:

- auto-register visible project docs
- index one stale registered project doc

M8 should not broaden the migration to other monitor or write paths.

## Replacement Boundary

Current M7 shape:

- `core/project_docs_supervisor.py` emits the finalized M7 event.
- The supervisor still directly calls
  `project_docs.auto_register_project_docs()`.
- The supervisor still directly calls
  `project_docs.index_one_stale_registered_doc()`.
- The DocsDB listener records shadow intent only.

Target M8 shape:

- `core/project_docs_supervisor.py` emits the finalized event for the same
  maintenance tick.
- The supervisor no longer calls
  `project_docs.auto_register_project_docs()` directly for this selected tick.
- The supervisor no longer calls
  `project_docs.index_one_stale_registered_doc()` directly for this selected
  tick.
- The DocsDB listener performs the authoritative auto-register and stale-index
  work.
- Listener output records the same effect metrics that M7 compared in shadow
  mode.

The M8 patch must delete the replaced direct supervisor calls in the same patch
that makes the listener authoritative. Do not leave a compatibility dual-run
path after authority moves.

## Event Payload

M7's shadow payload may include `direct_result` because the direct path remains
authoritative there. M8 cannot require `direct_result` from the supervisor after
the direct path is removed.

Implementation review must choose one of these shapes explicitly:

- reuse the finalized M7 event name but change the payload to represent intent
  plus listener result, not direct result
- introduce a new authoritative event name that preserves the same tick identity
  without carrying `direct_result`

Either way, the payload must not include document bodies, raw diffs, credentials,
or local process environment.

## Not In Scope

- project-doc worker `execute_update_once`
- project-log queue commits
- `PROJECT.md` / `TOOLS.md` / `AGENTS.md` editing
- docs RAG search/recall behavior
- adapter hooks or auto-inject
- janitor request handling
- lifecycle/session/extraction event migration
- any memory/vector/graph recall path

## FailHard Policy

- Event emission failure under `failHard=true` must raise.
- Authoritative listener validation or execution failure under `failHard=true`
  must raise.
- With `failHard=false`, listener failures may log loudly and report failure
  status, but must not claim successful docs maintenance.
- Do not reintroduce a fallback direct supervisor write path to hide listener
  failures after M8 authority moves.

## Tests Before W4 Smoke

Focused tests should prove:

- supervisor emits the finalized M8 event for the selected maintenance tick
- supervisor direct-call trip-wires prove it no longer calls
  `auto_register_project_docs` or `index_one_stale_registered_doc`
- DocsDB listener calls the authoritative auto-register and stale-index
  primitives exactly once for the tick
- listener result metrics preserve the M7 shadow parity fields needed by
  operators
- listener failure raises under `failHard=true`
- listener failure logs loudly and reports failure under `failHard=false`
- project-doc worker `execute_update_once` still does not emit or handle this
  supervisor maintenance event
- docs RAG recall/search tests are unchanged

## W4 Smoke After Code

After W6/W8 approve an implementation patch, W4 smoke should cover:

- supervisor starts normally
- docs auto-registration still happens through the listener path
- stale registered docs still index through the listener path
- project docs worker updates still work through the existing worker path
- event/listener traces show authoritative handling, not shadow-only handling
- no failHard violations during normal CC/CDX/OC docs/project milestones

## M9 Handoff Criteria

M8 is ready to hand off only when:

- the selected supervisor direct write path has been removed
- W4 smoke confirms the listener-owned path is authoritative and stable
- W6 confirms no unrelated monitor/write paths were touched
- W3 confirms no recall-visible docs behavior changed, or explicitly approves
  any observed recall-visible change
- `datastore-events-m9-monitor-migration-plan.md` is reviewed as the concrete
  plan for the next producer migration slices
