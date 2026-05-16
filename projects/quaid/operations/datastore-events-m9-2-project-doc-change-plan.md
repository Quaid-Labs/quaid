# Datastore Events M9.2 Project Doc Change Plan

Status: draft plan; runtime implementation blocked until M9.1 validation completes
Owner: W1 runtime/datastore
Plan source: `projects/quaid/operations/datastore-events-m9-monitor-migration-plan.md`

## Precondition

Do not implement runtime code for M9.2 until:

1. M9.1 docs registration/index request migration passes W3/W4/W6/W8.
2. W4 confirms docs registration, stale indexing, and docs recall/search remain
   stable in the installed environment after M9.1.
3. W6 confirms M9.1 did not leave fallback calls to the removed selected helper
   path or touch unrelated monitor/write families.
4. W3 reviews this M9.2 implementation plan because project-doc change writes
   can affect docs recallability, indexing cadence, row metadata, and result
   shape.

This document is planning only. It does not approve runtime implementation.

## M9.2 Goal

M9.2 migrates project-doc worker document-change writes so the worker becomes a
producer/coordinator and DocsDB owns the selected docs write/index operation.

The project-doc worker should continue to own:

- update locks
- request retention and worker state
- progress phase reporting
- shadow snapshot/cursor reads
- PROJECT.log cursor advancement
- deferred user notices
- worker lifecycle and process supervision

DocsDB should own the selected document update and indexing primitives for the
chosen slice.

## Current Boundary

Current post-M9.1 worker path in `core/project_docs.py`:

1. `execute_update_once()` acquires the project update lock and records state.
2. `_commit_queued_project_logs()` drains durable project-log queue items and
   writes visible `PROJECT.log` / `PROJECT.md` history.
3. `_read_project_log_since()` reads the visible `PROJECT.log` cursor window.
4. `snapshot_project()` and `committed_shadow_snapshot_since_cursor()` collect
   source-file change context.
5. `core.docs_updater_hook.update_project_docs()` edits project docs based on
   source diffs and project-log context.
6. `sync_project_docs_registry()` syncs visible docs registry rows.
7. `core.docs.updater.update_registered_docs()` indexes registered docs.
8. `core.docs.updater.index_project_logs()` indexes project logs.
9. Worker state, cursors, progress, and notices are finalized.

Current lower DocsDB primitives include:

- `datastore.docsdb.project_log_queue` for durable project-log intent
- `datastore.docsdb.project_updater.append_project_logs()` for visible
  append-only project history writes
- `datastore.docsdb.project_updater.refresh_project_md()` through
  `core.docs.updater.sync_project_visible_docs()`
- `datastore.docsdb.updater` classification/update/indexing primitives through
  the core docs updater wrapper

## Proposed First Slice

First slice target: move the project-doc worker's selected docs apply/index
operation behind a DocsDB event/request handler while leaving worker lock,
state, cursor, and notification ownership unchanged.

Selected operation:

1. receive project name, snapshots, project-log entries, dry-run flag, and
   request metadata from the worker
2. run the same docs update path currently reached through
   `core.docs_updater_hook.update_project_docs()`
3. sync visible project docs registry rows
4. update registered docs for that project
5. index that project's project logs
6. return the same metrics payloads the worker currently stores

The implementation patch must delete the replaced direct worker calls to the
selected apply/index functions in the same patch. Do not add a fallback from the
worker back to the old direct path after listener failure.

## Deferred Sub-Slices

Defer these until separate review:

- moving `_commit_queued_project_logs()` behind a DocsDB listener
- moving project-log queue drain/mark-committed ownership
- changing `PROJECT.log` append format or history-only behavior
- changing shadow snapshot or cursor semantics
- changing worker request scheduling or process lifecycle
- changing docs recall/search query behavior

The first slice may still pass project-log entries as input context. It must not
change how queued project-log writes are committed.

## Event Contract

The implementation may add a new request-style event, for example
`docs.project_update.request.v1`, if review confirms the synchronous worker
transaction needs request/fanin semantics.

Required payload fields:

- `project`
- `source: project-docs-worker`
- `request_id`
- `dry_run`
- `snapshots`
- `project_log_entries`
- `project_log_offset`

Payload must not include:

- raw credentials
- environment dumps
- unrelated transcript/session bodies
- unbounded file contents outside the selected snapshot/log context already
  used by the current docs update path

Required response fields:

- `metrics`
- `registry_sync`
- `indexed_docs`
- `indexed_project_logs`
- `errors`

The response shape must be directly consumable by the existing worker state
fields without changing operator-facing progress/status output.

## Non-Targets

- docs RAG recall/search ranking or result formatting
- project-doc worker locking/state/cursor ownership
- project-log queue commit semantics
- supervisor docs maintenance tick behavior from M9.1
- lifecycle/session/extraction/evolution/snippet/journal events
- memory/vector/graph recall paths
- public CLI syntax changes

## FailHard Policy

- `failHard=true`: listener/request validation or execution failure must raise
  through the worker update path. The worker must not fall back to the removed
  direct apply/index path.
- `failHard=false`: failure may preserve current worker error-state behavior,
  but it must log loudly, increment errors, preserve request/state visibility,
  and not silently report a fresh project.

## Parity Invariants

Implementation must preserve:

- update lock behavior and lock-busy request retention
- dry-run behavior
- project scoping and validation
- docs update classification/gating behavior
- PROJECT.log context passed to the docs updater
- visible docs registry sync semantics
- registered-doc indexing scope and protected `PROJECT.log` handling
- project-log index timing and count
- worker metrics/state/progress field names
- deferred notice message shape
- fail-soft continuation/error accounting where current code continues
- failHard re-raise behavior

## Required Tests Before W4

Add or preserve focused tests proving:

- selected worker apply/index path uses the new DocsDB handler/request and no
  longer calls the replaced direct functions
- worker lock/state/cursor/progress fields remain owned by `execute_update_once`
- project-log queue commit stays on the existing path for this slice
- dry-run does not write or index
- fail-soft listener failure records an error state and logs loudly
- failHard listener failure raises and does not fall back to direct calls
- docs registry sync and index counts are preserved in worker state
- project-doc worker `execute_update_once` remains out of supervisor
  maintenance event paths

## W4 Smoke

After W3/W6/W8 review, W4 should smoke:

- project-doc worker update from a real project change
- docs update applies to the same visible files as before
- docs recall/search can find newly updated/indexed docs
- project-log queue commits still produce visible history
- worker state/progress/status remain operator-readable
- failHard surfaces listener/request failure

## Handoff Criteria

M9.2 is complete only when:

- the selected worker apply/index direct calls are removed
- W4 confirms project-doc updates and recall/search remain stable
- W3 confirms no recall-visible docs behavior changed, or explicitly approves
  any observed change
- W6 confirms no unrelated monitor/write path migrated in the same patch
- W8 static validation passes focused worker, events, registry, contract, and
  docs consistency suites
