# Datastore Events M17 SessionDB Active Ingest Import Cleanup Plan

Status: runtime import-cleanup slice complete; wrapper removal deferred
Owner: W1 runtime/datastore, W3 recall and source-window review
Plan source: `projects/quaid/operations/datastore-events-m16-sessiondb-ingest-request-ownership-plan.md`

## Precondition

Do not implement runtime code for M17 until:

1. M16 SessionDB request ownership is closed through W4/W3/W6/W8.
2. W3 reviews the selected slice because active `session.ingest_log` writes
   SessionDB transcript rows, projects MemoryDB `session_chunks`, and feeds
   source-window expansion.
3. W6 reviews the import-boundary cleanup because this slice removes the active
   event's remaining MemoryDB wrapper dependency.
4. W8 confirms static coverage includes active session ingest, request session
   ingest, datastore manifests/contracts, session memory bridge, store recall,
   source-window guards, and boundary checks.

This document records one narrow import cleanup slice only. It does not approve
changing active event delivery mode, adding new event names, changing active or
request payload/result envelopes, removing MemoryDB compatibility wrappers,
changing `session.ingest_log.request.v1` ownership, adding recall selectors to
SessionDB, changing MemoryDB `session_chunks` projection ownership, changing
source-window behavior, lifecycle persistence, data migration, CLI behavior,
default request routing, public push, or release actions.

## Goal

M16 moved `session.ingest_log.request.v1` metadata and request registration to
SessionDB while intentionally leaving the active `session.ingest_log` handler
importing through `core.plugins.memorydb_contract.run_session_ingest_payload()`.

M17 selected the next cleanup prerequisite: make the active handler import the
SessionDB-owned helper directly. This aligns active-event helper ownership with
the M15/M16 SessionDB ownership boundary while preserving the active event name,
payload, processed/failed envelope, request ownership, and MemoryDB
`session_chunks` projection behavior.

This is not a route switch or compatibility removal. MemoryDB compatibility
wrappers remain for installed alpha import paths and older internal callers.

## Current Boundary

Pre-M17 path:

1. `core.plugins.sessiondb_contract.run_session_ingest_payload()` owns the
   session-ingest payload normalization and `core.ingest_runtime` delegation.
2. `session.ingest_log.request.v1` is declared and registered under SessionDB.
3. `core.plugins.memorydb_contract.run_session_ingest_payload()` remains a
   silent distinct compatibility wrapper to the SessionDB helper.
4. `core.runtime.events._handle_session_ingest_log()` still imported
   `run_session_ingest_payload()` from `core.plugins.memorydb_contract` for
   active `session.ingest_log` processing.
5. MemoryDB manifest/contract still own `session_chunks` recall/write
   capability; SessionDB manifest/contract have `capabilities.recall=[]`.

## Selected First Slice: Active Import Cleanup Only

Implemented one runtime cleanup slice only:

1. Update `core.runtime.events._handle_session_ingest_log()` to import
   `run_session_ingest_payload()` from `core.plugins.sessiondb_contract`
   directly.
   Import location: runtime source proof before implementation found the
   existing import is inside `_handle_session_ingest_log()`, not module-level.
   Update that existing in-function import in `core/runtime/events.py` from
   `from core.plugins.memorydb_contract import run_session_ingest_payload` to
   `from core.plugins.sessiondb_contract import run_session_ingest_payload`.
   Keep the import in the function; do not move it to module scope.
2. Keep the active `session.ingest_log` event name, delivery mode, payload
   schema, processed/failed result envelope, and `process_events()` behavior
   unchanged.
3. Keep the active handler's validation behavior unchanged: missing
   `payload.session_id` returns the same failed envelope before helper
   invocation.
4. Keep helper-result handling unchanged: helper result statuses `failed` or
   `error` still return `{"status": "failed", "result": result}`; successful
   results still return `{"status": "processed", "result": result}`.
5. Preserve the current exception behavior in the active handler for this slice.
   Do not broaden, narrow, or reframe the existing catch/failed-envelope shape
   unless a separate failHard cleanup plan selects it.
6. Preserve all `core.plugins.memorydb_contract` compatibility wrappers. Do not
   delete, rename, warn from, or deprecate the MemoryDB helper/handler/registrar
   wrappers in this slice.
7. Do not change `session.ingest_log.request.v1` manifest, contract, event
   registry, registrar, broker validation, or handler ownership. Those moved to
   SessionDB in M16 and must remain stable.
8. Preserve SessionDB transcript row shape, MemoryDB `session_chunks`
   projection, microchunk linkage, source kind, source-window expansion inputs,
   recall selector ownership, ranking, and planner behavior.

## Implementation Record

Runtime import-cleanup slice closed at `93b3561f5`
(`refactor(datastore): route active session ingest through SessionDB helper`).

Implemented behavior:

- Updated the existing in-function import inside
  `core.runtime.events._handle_session_ingest_log()` from
  `core.plugins.memorydb_contract.run_session_ingest_payload` to
  `core.plugins.sessiondb_contract.run_session_ingest_payload`.
- Preserved active `session.ingest_log` event name, delivery mode, payload
  schema, processed/failed envelope, missing-`session_id` validation, helper
  failed/error result handling, and the pre-existing exception/failed-envelope
  behavior.
- Preserved M16 `session.ingest_log.request.v1` SessionDB ownership, broker
  validation, manifest/contract metadata, and request handler behavior.
- Preserved all `core.plugins.memorydb_contract` compatibility wrappers.
- Preserved MemoryDB `session_chunks` recall/write projection, SessionDB
  `capabilities.recall=[]`, source-window inputs, recall ranking/planning,
  lifecycle behavior, CLI behavior, default routing, and public release state.

Tests added or preserved:

- Active `session.ingest_log` trip-wire coverage: the test monkeypatches
  `memorydb_contract.run_session_ingest_payload()` to raise if called and
  verifies the active path calls the SessionDB helper instead.
- Source assertion coverage: `core.runtime.events` does not import
  `core.plugins.memorydb_contract.run_session_ingest_payload` and does not
  directly import `run_session_logs_ingest`; it does import the SessionDB helper.
- Missing-`session_id` and helper failed-result active envelope tests still pin
  the existing processed/failed behavior.
- Existing request-path, manifest/contract, MemoryDB compatibility wrapper,
  session-memory bridge, store recall, and source-window guard lanes remain
  covered.

Validation:

- W4 R201 live/source-proof PASS on `93b3561f5`: active handler imports the
  SessionDB helper directly; active envelope shape is unchanged; M16 request
  ownership remains SessionDB; MemoryDB compatibility wrappers remain callable;
  MemoryDB `session_chunks` recall/write projection is unchanged; prior milestone
  routes remain intact.
- W3 runtime/recall APPROVED with no findings: active event behavior and
  request ownership are unchanged except for the helper import source;
  `session_chunks` projection, SessionDB recall `[]`, source-window, recall,
  ranking, planner, lifecycle, CLI, and default routing behavior are unchanged.
- W6 APPROVED with no concerns: one-line in-function import swap, no
  MemoryDB-call trip-wire implemented, in-function import location preserved,
  pre-existing bare-except behavior explicitly deferred, and no B-code concerns
  introduced.
- W8 static PASS/runtime HOLD pending final closure: focused session ingest
  selector, affected event/extraction/session-ingest/registry/contract lane,
  py_compile, ruff, diff/docs checks, boundary check, and unit wrapper all
  passed.

## Non-Targets

- no new event names
- no active `session.ingest_log` delivery-mode change
- no active `session.ingest_log` payload schema change
- no active processed/failed envelope change
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

- `failHard=true`: transcript resolution, adapter parsing, SessionDB storage,
  MemoryDB projection, or helper execution failure must raise or fail through
  the same active/request paths that raise or fail today. Do not fall back to
  the MemoryDB wrapper after the SessionDB helper import is selected.
- `failHard=false`: existing fail-soft event and broker behavior may continue,
  but failures must remain loud and must not report the session log as indexed.
- Do not wrap SessionDB helper import/execution and MemoryDB compatibility
  wrapper behavior in a broad shared try/except that can downgrade helper
  failures into routing success.
- Do not add a direct `core.ingest_runtime.run_session_logs_ingest()` import to
  `core.runtime.events`.

## Required Tests Before W4

Add or preserve focused tests proving:

- Active `session.ingest_log` imports/calls
  `core.plugins.sessiondb_contract.run_session_ingest_payload()` directly and
  does not call `core.plugins.memorydb_contract.run_session_ingest_payload()`.
  Verify the no-MemoryDB-call invariant with a trip-wire: monkeypatch
  `core.plugins.memorydb_contract.run_session_ingest_payload()` to raise
  `AssertionError` if invoked, then process active `session.ingest_log`. Assert
  the SessionDB helper was called and the MemoryDB trip-wire did not fire.
- Missing `payload.session_id` still returns the same active failed envelope and
  does not call either helper.
- Helper failure/result-status handling preserves the existing active
  processed/failed envelope shape.
- MemoryDB compatibility wrappers remain importable, silent, distinct callables,
  and delegate to SessionDB as established by M15/M16.
- `session.ingest_log.request.v1` still registers under SessionDB and still
  returns exactly one SessionDB broker response row.
- Active and request session ingest still write SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- Source-window expansion tests still pass with no changed expansion metadata
  policy.
- failHard request/active failures do not fall back to MemoryDB wrapper
  ownership or direct ingest-runtime imports.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow session-ingest smoke:

- `_handle_session_ingest_log()` imports the SessionDB helper directly and no
  longer imports `core.plugins.memorydb_contract.run_session_ingest_payload()`.
- Active `session.ingest_log` indexes a real transcript and projects MemoryDB
  `session_chunks` evidence with the same visible result shape.
- `session.ingest_log.request.v1` still registers under SessionDB and returns
  broker response datastore id `sessiondb`.
- MemoryDB compatibility wrappers remain callable and silent.
- Source-window expansion for session evidence is unchanged.

## Deferred Decisions

- SessionDB ingest wrapper retirement closed in M23 at `bfe5836b` +
  `4a3824d88`
- active handler exception/failed-envelope cleanup for the pre-existing bare
  `except Exception` path closed in M18 at `fd7cc4b38`
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- lifecycle persistence for ack-only lifecycle events
- source-window metadata enrichment and selector ownership
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
