# Datastore Events M9.1 Docs Registration And Index Request Plan

Status: draft plan; blocked until M8 validation completes
Owner: W1 runtime/datastore
Plan source: `projects/quaid/operations/datastore-events-m9-monitor-migration-plan.md`

## Precondition

Do not implement runtime code for M9.1 until:

1. M8 authoritative docs listener stack passes W4 smoke.
2. W6 confirms the M8 stack did not touch unrelated monitor/write paths.
3. W3 confirms no docs recall/search behavior changed in M8, or explicitly
   approves any observed recall-visible change.
4. W8 static validation passes the M8 stack.
5. This M9.1 plan has W3/W6 review if the implementation can affect docs
   recallability, indexing cadence, row metadata, or result shape.

This document is planning only. It does not approve runtime implementation.

## M9.1 Goal

M9.1 migrates the remaining docs registration and stale-doc index request
surface behind a datastore-owned listener/request contract. After M8, the
supervisor's periodic docs maintenance tick is listener-owned, but the selected
DocsDB listener still delegates to `core.project_docs` helper functions that
wrap the actual docs registry/index primitives.

M9.1 should make the DocsDB-owned boundary explicit without changing recall or
indexing behavior.

## Current Boundary

Current post-M8 path:

- `core/project_docs_supervisor.py` emits `docs.project_maintenance_observed`
  with `requested_operations`.
- `core/plugins/docsdb_contract.py` handles the event.
- The handler calls `core.project_docs.auto_register_project_docs()`.
- The handler calls `core.project_docs.index_one_stale_registered_doc()`.
- Those helpers call DocsDB-owned primitives:
  - `core.docs.updater.sync_project_visible_docs()` via
    `sync_project_docs_registry()`
  - `core.docs.updater.index_one_stale_registered_doc()`

The `core.project_docs` helper layer remains useful for worker paths, status,
project runtime context, and project-doc worker update transactions. M9.1
should not blindly remove those helpers globally.

## Proposed First Slice

First slice target: move the authoritative listener implementation from calling
`core.project_docs.auto_register_project_docs()` and
`core.project_docs.index_one_stale_registered_doc()` to a DocsDB-owned helper
that performs the same selected operations for the listener.

The helper can live in `core/plugins/docsdb_contract.py` or a DocsDB-owned module
if that keeps imports legal. It must preserve the same primitives and ordering:

1. materialize queued projects for the requested project/all-project scope
2. sync visible project docs registry for each selected project
3. index one stale registered doc for the selected project/all-project scope

Do not migrate project-doc worker `execute_update_once` in this slice. That
worker owns a larger transaction: shadow snapshots, updater application,
registry sync, registered-doc indexing, project-log queue drain, state writes,
and notifications. It needs a separate plan if selected later.

## Replacement Boundary

M9.1 implementation must delete the replaced direct listener-to-`core.project_docs`
maintenance calls in the same patch that adds the datastore-owned helper. Do not
leave a fallback path from the DocsDB listener back to the old selected helper
calls after listener failure.

Allowed remaining `core.project_docs` calls after M9.1:

- project validation/runtime-context helpers needed to preserve current scoping
- project-doc worker transaction internals
- project status/diff/CLI surfaces
- worker lifecycle and supervisor process management

Not allowed after the selected slice:

- DocsDB listener calling `core.project_docs.auto_register_project_docs()` for
  the selected event path
- DocsDB listener calling `core.project_docs.index_one_stale_registered_doc()`
  for the selected event path
- compatibility dual-run of old and new registration/index request paths

## Event Contract

Reuse `docs.project_maintenance_observed` unless implementation review finds a
specific reason a new event is needed. Keep payload shape from M8:

- `source: project-docs-supervisor`
- `tick_kind: auto_register_and_stale_index`
- `requested_operations.auto_register: bool`
- `requested_operations.stale_index: bool`
- no document bodies, raw diffs, credentials, or environment dumps

Listener output must keep the M8 operator metrics shape under
`listener_result.direct_result`:

- `auto_register_ran`
- `stale_index_ran`
- `registered`
- `indexed_one`
- `errors`

## Non-Targets

- docs RAG recall/search ranking or result formatting
- `DocsRAG.search()` or recall request handlers
- project-doc worker `execute_update_once`
- project-log queue commits
- `PROJECT.md`, `TOOLS.md`, or `AGENTS.md` editing
- lifecycle/session/extraction/evolution/snippet/journal events
- memory/vector/graph recall paths
- public CLI syntax changes

## FailHard Policy

- `failHard=true`: listener validation or execution failure must raise through
  event dispatch and supervisor failed-count handling.
- `failHard=false`: listener may continue from auto-register failure to
  stale-index if both were requested, but it must log loudly, report failed
  status, and keep errors in `listener_result.direct_result.errors`.
- No fallback to the removed direct helper calls after M9.1 authority moves.

## Parity Invariants

Implementation must preserve:

- all-project vs project-scoped behavior for registration and stale indexing
- queued project materialization before registry sync
- visible-doc registry sync semantics, including unregistering visible docs that
  disappear only after updater apply
- protected root docs and `PROJECT.log` handling
- stale-doc index order and one-doc-per-tick behavior
- fail-soft continuation from auto-register failure to stale-index attempt
- failHard re-raise behavior
- event result metrics shape
- operator-visible supervisor startup and status behavior

## Required Tests Before W4

Add or preserve focused tests proving:

- M8 authoritative event happy path still calls the same DocsDB registry/index
  primitives exactly once for requested operations
- selected listener path no longer calls
  `core.project_docs.auto_register_project_docs()` or
  `core.project_docs.index_one_stale_registered_doc()`
- all-project and project-scoped request behavior are preserved if project scope
  is supported in the helper
- fail-soft auto-register failure logs and still attempts stale-index
- fail-soft stale-index failure logs and reports failed status
- failHard auto-register failure raises and does not attempt stale-index
- failHard stale-index failure raises after auto-register succeeds
- project-doc worker `execute_update_once` tests remain green and do not use the
  supervisor maintenance event path

## W4 Smoke

After W3/W6/W8 review, W4 should smoke:

- supervisor starts normally
- docs auto-registration still happens through the listener-owned path
- stale registered docs still index through the listener-owned path
- docs recall/search can find newly indexed docs
- project-doc worker updates still work through the existing worker path
- failHard raises on listener validation or execution failure

## Handoff Criteria

M9.1 is complete only when:

- the selected listener-to-core helper calls are removed
- W4 confirms registration/indexing still work in an installed environment
- W3 confirms no recall-visible docs behavior changed, or explicitly approves
  any observed change
- W6 confirms no unrelated monitor/write path migrated in the same patch
- W8 static validation passes the event, registry, contract, and project-docs
  suites
