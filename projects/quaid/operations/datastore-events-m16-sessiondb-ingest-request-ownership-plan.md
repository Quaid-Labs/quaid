# Datastore Events M16 SessionDB Ingest Request Ownership Plan

Status: runtime ownership slice complete; lifecycle/source-window decisions deferred
Owner: W1 runtime/datastore, W3 recall and source-window review
Plan source: `projects/quaid/operations/datastore-events-m15-sessiondb-ingest-helper-plan.md`

## Precondition

Do not implement runtime code for M16 until:

1. M15 SessionDB ingest helper ownership is closed through W4/W3/W6/W8.
2. W3 reviews the selected slice because the request writes SessionDB rows,
   projects MemoryDB `session_chunks`, and feeds source-window expansion.
3. W6 reviews the ownership boundary because this slice moves the request-event
   owner from MemoryDB metadata/registration to SessionDB metadata/registration.
4. W8 confirms static coverage includes session ingest request routing, datastore
   manifests/contracts, extraction-daemon request validation, session memory
   bridge, source-window expansion, and boundary checks.

This document records the completed request-ownership move only. It does not
approve changing active `session.ingest_log` delivery, adding new event names,
changing request payload or result envelopes, adding recall selectors to
SessionDB, changing MemoryDB `session_chunks` projection ownership, changing
source-window behavior, lifecycle persistence, data migration, CLI behavior,
default request routing, public push, or release actions.

## Goal

M16 addressed the post-M15 boundary mismatch where SessionDB owns the ingest
helper body, but `session.ingest_log.request.v1` is still declared and registered
under MemoryDB. The selected slice moved only the existing request event's
first-party ownership metadata and request-handler registration to SessionDB.

This is not a new route, event, or envelope. Producers still emit the same
`session.ingest_log.request.v1` event with the same payload. The handler still
runs the M15 SessionDB helper and returns the same ingest result shape. The
observable request broker response changed only in datastore ownership:
`datastore_id` becomes `sessiondb` instead of `memorydb`.

MemoryDB continues to own the user-facing `session_chunks` recall selector and
projection. SessionDB owns transcript/provenance persistence for the ingest
request; MemoryDB remains the recall projection owner.

## Current Boundary

Current post-M15 path:

1. `session.ingest_log.request.v1` is declared in the MemoryDB manifest and
   `MemoryDbDatastoreContract`.
2. `core.plugins.memorydb_contract.register_session_ingest_log_request_handler()`
   registers `handle_session_ingest_log_request()` under datastore id
   `memorydb`.
3. The handler calls `memorydb_contract.run_session_ingest_payload()`, a distinct
   compatibility wrapper that delegates to
   `core.plugins.sessiondb_contract.run_session_ingest_payload()`.
4. `core.extraction_daemon._request_session_logs_ingest()` imports the MemoryDB
   registrar and validates that the broker response row uses datastore id
   `memorydb`.
5. `core.runtime.events._handle_session_ingest_log()` still imports the MemoryDB
   helper wrapper for active `session.ingest_log` processing.
6. MemoryDB manifest/contract still own `session_chunks` recall capability;
   SessionDB manifest/contract have no recall selector.

## Selected First Slice: Request Ownership Move Only

Implemented one runtime ownership slice only:

1. Add `handle_session_ingest_log_request(event)` and
   `register_session_ingest_log_request_handler()` to
   `core.plugins.sessiondb_contract`.
2. The new SessionDB handler unwraps `event["payload"]` exactly like the current
   MemoryDB handler and delegates to
   `sessiondb_contract.run_session_ingest_payload(payload)`.
3. The new SessionDB registrar registers `session.ingest_log.request.v1` with
   datastore id `sessiondb` and `force=True`.
4. Move `session.ingest_log.request.v1` from the MemoryDB first-party manifest
   request_handlers list to the SessionDB first-party manifest request_handlers
   list.
5. Move the corresponding `DatastoreHandlerSpec` from
   `MemoryDbDatastoreContract` to `SessionDbDatastoreContract`, with replacement
   target `core.plugins.sessiondb_contract.handle_session_ingest_log_request`.
6. Update SessionDB manifest `capabilities.writes` from `[]` to only the
   SessionDB stores written by the existing ingest path: `sessions`,
   `transcript_chunks`, `message_pairs`, and `microchunks`. Do not add
   `message_pair_attachments`, recall selectors, or MemoryDB projection writes
   in this slice.
   Runtime source proof before this addendum found no current
   `message_pair_attachments` table or write path in
   `datastore.sessiondb.session_store`; it is declarative future metadata, not a
   store written by `run_session_logs_ingest()` today.
7. Keep MemoryDB manifest `capabilities.recall` including `session_chunks` and
   keep MemoryDB manifest `capabilities.writes` including `session_chunks`.
8. Update `core.extraction_daemon._request_session_logs_ingest()` to import the
   SessionDB registrar and update its broker-response validation/error text to
   expect exactly one `sessiondb` response.
9. Preserve `memorydb_contract.run_session_ingest_payload(payload)` as the
   distinct compatibility wrapper from M15.
10. Preserve `memorydb_contract.handle_session_ingest_log_request(event)` and
    `memorydb_contract.register_session_ingest_log_request_handler()` as
    compatibility wrappers only. They must delegate to the SessionDB handler and
    registrar; they must not register `session.ingest_log.request.v1` under
    datastore id `memorydb` after this slice.
    Wrapper shape: each MemoryDB compatibility wrapper is a distinct
    function-level wrapper that imports and calls the corresponding
    `core.plugins.sessiondb_contract` handler or registrar. Do not re-export the
    SessionDB symbols by identity. Monkeypatches on either module affect only
    that module's symbol.
    Wrapper logging policy: MemoryDB compatibility wrappers are silent. They do
    not emit deprecation, info, or warning logs on call; their purpose is
    module-import-path compatibility only.
11. Preserve active `session.ingest_log` behavior exactly. Do not change
    `core.runtime.events._handle_session_ingest_log()` in this slice; it may keep
    importing through the MemoryDB helper wrapper.
12. Preserve request payload and handler result shape exactly. Broker callers see
    the same result object under `responses[0].result`; only
    `responses[0].datastore_id` changes from `memorydb` to `sessiondb`.

## Non-Targets

- no new event names
- no change to `session.ingest_log.request.v1` payload schema
- no change to request broker top-level envelope shape
- no change to handler result shape
- no active `session.ingest_log` delivery or envelope change
- no change to MemoryDB `session_chunks` recall selector ownership
- no SessionDB recall selector or source-window selector ownership
- no MemoryDB projection, chunking, ranking, scoring, source-window, or recall
  result changes
- no lifecycle persistence for ack-only lifecycle events
- no daemon scheduling or signal-shape changes beyond the request registrar and
  response datastore-id validation
- no transcript row schema or data migration
- no SessionDB CLI syntax or output changes
- no default request routing, hidden/public CLI request-mode, `.ego`, alias
  retirement, or plugin-id rename changes

## FailHard Policy

- `failHard=true`: SessionDB request registration, broker validation, transcript
  resolution, adapter parsing, SessionDB storage, MemoryDB projection, or helper
  execution failure must raise through the same active/request paths that raise
  today. Do not fall back to MemoryDB request registration after SessionDB
  ownership is selected.
- `failHard=false`: existing fail-soft event and broker behavior may continue,
  but failures must remain loud and must not report the session log as indexed.
- Do not wrap SessionDB request registration and helper execution in a broad
  shared try/except that can downgrade helper failures into registration success
  or routing success.
- Do not register both MemoryDB and SessionDB handlers for
  `session.ingest_log.request.v1` in this slice. The broker response must contain
  exactly one response row, owned by SessionDB.

## Required Tests Before W4

Add or preserve focused tests proving:

- SessionDB manifest and contract declare `session.ingest_log.request.v1`;
  MemoryDB manifest and contract no longer declare it.
- SessionDB `capabilities.writes` lists only `sessions`, `transcript_chunks`,
  `message_pairs`, and `microchunks`; SessionDB `capabilities.recall` remains
  `[]`; MemoryDB still owns `session_chunks` recall/write capability.
- `sessiondb_contract.register_session_ingest_log_request_handler()` registers
  `session.ingest_log.request.v1` under datastore id `sessiondb`.
- `memorydb_contract.register_session_ingest_log_request_handler()` delegates to
  the SessionDB registrar and does not register a MemoryDB request handler.
- Request broker calls to `session.ingest_log.request.v1` return exactly one
  response row with datastore id `sessiondb` and the same result shape as before.
- `core.extraction_daemon._request_session_logs_ingest()` imports/registers the
  SessionDB handler, expects a `sessiondb` response, and rejects missing,
  malformed, failed, or non-SessionDB responses without fallback.
- Active `session.ingest_log` still produces the same processed/failed envelope
  and still does not import/call `core.ingest_runtime.run_session_logs_ingest()`
  from `core.runtime.events`.
- Request handler parity with direct session ingest still writes SessionDB rows
  and MemoryDB `session_chunks` with the same counts, metadata, and source kind.
- Source-window expansion tests still pass with no changed expansion metadata
  policy.
- failHard request/active failures raise through the same paths and do not fall
  back to MemoryDB request ownership or a direct helper implementation.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow session-ingest smoke:

- `session.ingest_log.request.v1` registers under SessionDB and returns broker
  response datastore id `sessiondb`.
- MemoryDB manifest/contract no longer declare the request event; SessionDB
  manifest/contract do.
- MemoryDB still owns `session_chunks` recall/write capability; SessionDB recall
  remains empty.
- Request session ingest indexes a real transcript and projects MemoryDB
  `session_chunks` evidence.
- Active `session.ingest_log` behavior remains unchanged.
- Source-window expansion for session evidence is unchanged.

## Implementation Record

Runtime ownership slice closed at `40ff6c8ed` (`refactor(datastore): move
session ingest request to SessionDB`) with metadata/test follow-up `23c0e7228`
(`test(datastore): align SessionDB ingest event metadata`).

Implemented behavior:

- Added `core.plugins.sessiondb_contract.handle_session_ingest_log_request()`
  and `register_session_ingest_log_request_handler()`; the registrar registers
  `session.ingest_log.request.v1` under datastore id `sessiondb`.
- Moved `session.ingest_log.request.v1` from MemoryDB manifest/contract metadata
  to SessionDB manifest/contract metadata.
- Updated SessionDB `capabilities.writes` to the current transcript-store write
  set: `sessions`, `transcript_chunks`, `message_pairs`, and `microchunks`.
  `message_pair_attachments` remains declarative store metadata and is not in
  writes.
- Preserved MemoryDB `session_chunks` recall and write capability.
- Updated `core.extraction_daemon._request_session_logs_ingest()` to import the
  SessionDB registrar and validate exactly one `sessiondb` broker response.
- Preserved `core.plugins.memorydb_contract` helper, handler, and registrar as
  silent distinct compatibility wrappers that delegate to SessionDB; they do not
  register a MemoryDB handler.
- Left active `session.ingest_log` behavior unchanged; the active handler still
  imports through the MemoryDB helper wrapper.
- Updated the `session.ingest_log.request.v1` event registry description to
  record SessionDB-owned transcript ingest with MemoryDB `session_chunks`
  projection.
- Did not change event name, payload schema, request/active result shape,
  source-window behavior, recall ranking/planning, lifecycle persistence, daemon
  signal shape, CLI behavior, default routing, or public release state.

Tests added or preserved:

- Manifest and contract ownership move: SessionDB declares the request event;
  MemoryDB no longer declares it.
- Capability split: SessionDB writes only current transcript stores and recall
  remains `[]`; MemoryDB retains `session_chunks` recall/write capability.
- SessionDB registrar ownership and MemoryDB compatibility wrapper delegation
  without dual registration.
- Request broker integration returns exactly one `sessiondb` response row with
  unchanged result shape.
- Extraction-daemon request validation rejects missing, malformed, failed, and
  non-SessionDB responses without falling back to direct ingest.
- Event capability metadata pins both SessionDB request ownership and MemoryDB
  `session_chunks` projection.
- Direct/request/active parity still writes SessionDB rows and MemoryDB
  `session_chunks`; session-memory bridge, store-recall, and session-log lanes
  continue to pass.

Validation:

- W4 R201 live/source-proof PASS on `40ff6c8ed`: SessionDB owns the request
  handler; exactly one broker registration row exists; MemoryDB manifest and
  contract no longer declare the request event; SessionDB manifest and contract
  do; extraction daemon expects `sessiondb`; active `session.ingest_log` remains
  unchanged; MemoryDB keeps `session_chunks`; prior M9.x/M10/M11/M12/M13/M14/M15
  routes remain intact. W4 source-proofed the `23c0e7228` metadata follow-up
  without requiring a fresh live smoke.
- W3 runtime/recall APPROVED after `23c0e7228` closed the stale event-capability
  description finding: MemoryDB retains `session_chunks` recall/write
  projection, SessionDB recall remains `[]`, source-window/recall/ranking/planner
  behavior is unchanged, and lifecycle/new-event/CLI/default-routing behavior is
  unchanged.
- W6 APPROVED with no concerns for both `40ff6c8ed` and `23c0e7228`: no dual
  registration, no fallback to MemoryDB request registration, silent distinct
  wrappers, capability split clean, no broad shared try/except, and no B-code
  concerns introduced.
- W8 static PASS/runtime HOLD closed for the corrected pair: focused session
  ingest/event capability selectors, extraction-daemon request validation,
  registry/contract lanes, affected runtime lane, adjacent session-memory/store
  recall/session-log lane, py_compile, ruff, diff/docs checks, boundary check,
  and unit wrapper all passed.

## Deferred Decisions

- active `session.ingest_log` import cleanup away from the MemoryDB wrapper is
  tracked as M17 in
  `projects/quaid/operations/datastore-events-m17-sessiondb-active-ingest-import-cleanup-plan.md`
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- lifecycle persistence for ack-only lifecycle events
- source-window metadata enrichment and selector ownership
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
