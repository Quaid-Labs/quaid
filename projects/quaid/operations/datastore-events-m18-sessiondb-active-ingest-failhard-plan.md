# Datastore Events M18 SessionDB Active Ingest FailHard Plan

Status: draft plan; no runtime implementation yet
Owner: W1 runtime/datastore, W3 recall and source-window review
Plan source: `projects/quaid/operations/datastore-events-m17-sessiondb-active-ingest-import-cleanup-plan.md`

## Precondition

Do not implement runtime code for M18 until:

1. M17 active `session.ingest_log` import cleanup is closed through W4/W3/W6/W8.
2. W3 reviews the selected slice because active `session.ingest_log` writes
   SessionDB transcript rows, projects MemoryDB `session_chunks`, and feeds
   source-window expansion.
3. W6 reviews the failHard boundary because this slice changes how unexpected
   SessionDB helper exceptions surface from the active event handler.
4. W8 confirms static coverage includes active session ingest exception paths,
   request session ingest, datastore manifests/contracts, session memory bridge,
   store recall, source-window guards, and boundary checks.

This document selects one narrow failHard cleanup slice only. It does not approve
changing active event delivery mode, adding new event names, changing active or
request payload schemas, changing normal processed/failed envelopes for
validation or helper-returned failed statuses, removing MemoryDB compatibility
wrappers, changing `session.ingest_log.request.v1` ownership, adding recall
selectors to SessionDB, changing MemoryDB `session_chunks` projection ownership,
changing source-window behavior, lifecycle persistence, data migration, CLI
behavior, default request routing, public push, or release actions.

## Goal

M17 made active `session.ingest_log` import the SessionDB helper directly but
intentionally preserved the pre-existing handler-local bare
`except Exception: return {"status": "failed", "error": str(e)}` shape.

M18 selects the next failHard cleanup: remove the handler-local catch around the
SessionDB helper call so unexpected helper/import exceptions propagate to
`process_events()`' existing event-level failure machinery. This keeps normal
validation and helper-returned failed-status behavior unchanged while preventing
the active handler from converting unexpected exceptions into an ordinary handler
return before `process_events()` sees the exception.

This is not a source-window, recall, lifecycle, request-route, or wrapper-removal
slice.

## Current Boundary

Current post-M17 path:

1. `core.runtime.events._handle_session_ingest_log()` validates
   `payload.session_id` before helper invocation and returns
   `{"status": "failed", "error": "payload.session_id is required"}` when it is
   missing.
2. The handler imports `core.plugins.sessiondb_contract.run_session_ingest_payload()`
   inside the function and calls it.
3. If the helper returns a dict with status `failed` or `error`, the handler
   returns `{"status": "failed", "result": result}`.
4. If the helper raises unexpectedly, the handler catches `Exception` and returns
   `{"status": "failed", "error": str(e)}`. `process_events()` then treats that
   as a handler-reported failed status and, under failHard, raises a generic
   event failure from the returned envelope instead of the original exception.
5. `process_events()` already has event-level exception handling: if a handler
   raises, it marks the event failed in fail-soft mode and raises under
   failHard mode.

## Selected First Slice: Active Handler Exception Cleanup Only

Implement one runtime cleanup slice only:

1. Remove the handler-local `try/except Exception` wrapper around the SessionDB
   helper import/call in `core.runtime.events._handle_session_ingest_log()`.
2. Keep the in-function SessionDB helper import selected by M17. Do not move it
   to module scope and do not import the MemoryDB wrapper.
3. Keep missing-`payload.session_id` validation unchanged and before helper
   invocation.
4. Keep helper-returned failed/error status handling unchanged:
   `{"status": "failed", "result": result}` still returns from the handler and
   is handled by `process_events()` as a handler-reported failed status.
5. Let unexpected helper/import exceptions propagate to `process_events()`.
   Under failHard=true, `process_events()` raises through its existing exception
   path with the original exception as the cause. Under failHard=false,
   `process_events()` marks the event failed and records the exception text in
   the existing event-level failed envelope.
6. Do not change `process_events()` exception machinery in this slice.
7. Preserve all `core.plugins.memorydb_contract` compatibility wrappers.
8. Preserve M16 `session.ingest_log.request.v1` SessionDB ownership and request
   behavior.
9. Preserve SessionDB transcript row shape, MemoryDB `session_chunks`
   projection, microchunk linkage, source kind, source-window expansion inputs,
   recall selector ownership, ranking, and planner behavior.

## Non-Targets

- no new event names
- no active `session.ingest_log` delivery-mode change
- no active `session.ingest_log` payload schema change
- no normal successful active processed envelope change
- no missing-`session_id` failed envelope change
- no helper-returned failed/error envelope change
- no request broker ownership or response-shape change
- no removal of MemoryDB compatibility wrappers
- no change to MemoryDB `session_chunks` recall/write ownership
- no SessionDB recall selector or source-window selector ownership
- no MemoryDB projection, chunking, ranking, scoring, source-window, or recall
  result changes
- no lifecycle persistence for ack-only lifecycle events
- no transcript row schema or data migration
- no daemon scheduling, signal-shape, CLI, default request routing, `.ego`,
  alias retirement, or plugin-id rename changes

## FailHard Policy

- `failHard=true`: unexpected SessionDB helper/import exceptions from active
  `session.ingest_log` must not be caught and returned by the active handler.
  They must reach `process_events()` and raise from the event-level failHard
  path with the original exception chained as the cause.
- `failHard=false`: unexpected helper/import exceptions may be converted by
  `process_events()` into the existing failed event envelope, but they must not
  report the session log as indexed.
- Do not add fallback to the MemoryDB wrapper or direct
  `core.ingest_runtime.run_session_logs_ingest()` import after the SessionDB
  helper is selected.
- Do not wrap SessionDB helper execution and MemoryDB compatibility wrapper
  behavior in a broad shared try/except that can downgrade helper failures into
  routing success.

## Required Tests Before W4

Add or preserve focused tests proving:

- Active `session.ingest_log` still calls
  `core.plugins.sessiondb_contract.run_session_ingest_payload()` directly and
  does not call `core.plugins.memorydb_contract.run_session_ingest_payload()`.
- Missing `payload.session_id` still returns the same active failed envelope and
  does not call the helper.
- Helper-returned `{"status": "failed"}` still produces the same handler-reported
  failed result shape in fail-soft mode and still raises through the existing
  handler-reported failed-status path under failHard.
- Unexpected helper exceptions in fail-soft mode are handled by
  `process_events()`' event-level exception path: the event is marked failed,
  the exception text is recorded, and the event is not reported as processed.
- Unexpected helper exceptions in failHard mode raise from `process_events()`
  with the original helper exception chained as `__cause__`.
- Source assertions prove `_handle_session_ingest_log()` no longer contains a
  handler-local `except Exception` path and still does not import
  `run_session_logs_ingest()` directly.
- `session.ingest_log.request.v1` still registers under SessionDB and still
  returns exactly one SessionDB broker response row.
- Active and request session ingest still write SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- Source-window expansion tests still pass with no changed expansion metadata
  policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow session-ingest smoke:

- `_handle_session_ingest_log()` has no handler-local bare `except Exception`
  path around the SessionDB helper call.
- Active `session.ingest_log` still indexes a real transcript and projects
  MemoryDB `session_chunks` evidence with the same visible success result shape.
- Active helper exception smoke under failHard surfaces loudly through
  `process_events()` and does not route through MemoryDB wrapper fallback.
- `session.ingest_log.request.v1` still registers under SessionDB and returns
  broker response datastore id `sessiondb`.
- Source-window expansion for session evidence is unchanged.

## Deferred Decisions

- request/active compatibility-wrapper removal from `core.plugins.memorydb_contract`
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- lifecycle persistence for ack-only lifecycle events
- source-window metadata enrichment and selector ownership
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
