# Datastore Events M8 Authoritative Docs Listener Plan

Status: implemented and validated
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

M7 was implemented and validated in `a17ceb244` + `29552057a`. M8 completed
W3/W4/W6/W8 validation on stack `b405e5813+90de45f09+f4a9e21dc`.

## M8 Goal

M8 removes the direct supervisor write path selected in M7 and makes the DocsDB
listener authoritative for that one path.

The selected path is only the project-docs supervisor docs maintenance tick:

- auto-register visible project docs
- index one stale registered project doc

M8 should not broaden the migration to other monitor or write paths.

## Implementation Record

Implemented by `b405e5813`:

- `core/project_docs_supervisor.py` emits the selected maintenance tick with
  `requested_operations` and no longer performs the replaced direct
  auto-register or stale-index writes for that tick.
- `core/plugins/docsdb_contract.py` handles
  `docs.project_maintenance_observed` authoritatively through DocsDB-owned
  listener execution.
- Listener output keeps the M7 operator metrics shape under
  `listener_result.direct_result`.
- Tests pin the direct-call tripwires, failHard/fail-soft listener behavior,
  project scope preservation, and project-doc worker non-target boundary.

Follow-up `90de45f09` records the selected event in the DocsDB datastore
manifest/contract metadata and adds the registry guard for accepted non-request
domain events.

Validation:

- W4 smoke confirmed the listener-owned supervisor maintenance path was
  authoritative and stable.
- W3 confirmed no docs recall/search behavior changed.
- W6 confirmed no unrelated monitor/write paths were touched.
- W8 static validation passed for the affected event, DocsDB, registry, and
  supervisor coverage.

`f4a9e21dc` drafted the M9.1 follow-on plan after M8 validation closed. M9.1
later moved the listener internals from `core.project_docs` wrappers to the
DocsDB-owned helper path; that later slice does not reopen M8.

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

Implementation choice: reuse `docs.project_maintenance_observed` and replace
the supervisor-provided `direct_result` payload with
`requested_operations: {auto_register, stale_index}`. The authoritative DocsDB
listener writes the result metrics under `listener_result.direct_result`.

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
