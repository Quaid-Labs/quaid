# Datastore Events M15 SessionDB Ingest Helper Plan

Status: draft plan; no runtime implementation yet
Owner: W1 runtime/datastore, W3 recall and source-window review
Plan source: `projects/quaid/operations/datastore-events-m14-sessiondb-manifest-plan.md`

## Precondition

Do not implement runtime code for M15 until:

1. M14 SessionDB manifest metadata is closed through W4/W3/W6/W8.
2. W3 reviews the selected slice because session ingest writes SessionDB rows,
   projects MemoryDB `session_chunks`, and feeds source-window expansion.
3. W6 reviews the ownership boundary because this slice moves helper ownership
   without changing event ownership.
4. W8 confirms static coverage includes session ingest, event routing, datastore
   registry/contract, session memory bridge, source-window expansion, and
   boundary checks.

This document selects an internal helper-ownership prerequisite slice. It does
not approve moving `session.ingest_log.request.v1` out of MemoryDB ownership,
changing active `session.ingest_log`, adding SessionDB-specific request events,
adding activated SessionDB handlers, changing recall selectors, changing
source-window behavior, lifecycle persistence, data migration, CLI behavior,
default request routing, public push, or release actions.

## Goal

M15 addresses the post-M14 boundary mismatch where the first-party SessionDB
manifest now exists, but the session ingest payload normalization helper still
lives in `core.plugins.memorydb_contract` because M9.3 intentionally kept the
request event under MemoryDB ownership.

The selected first slice is narrow: introduce a SessionDB-owned helper module for
the existing session-ingest payload normalization and transcript ingest call, then
keep the existing MemoryDB request handler and active-event path delegating
through that helper. Observable event ownership and response envelopes stay
unchanged.

A future ownership-move plan, for example routing
`session.ingest_log.request.v1` through SessionDB ownership, will consume this
SessionDB-owned helper directly. This M15 slice creates the helper-ownership
prerequisite without making that ownership move.

This is not a route switch. MemoryDB still owns the existing request event and
the user-facing `session_chunks` recall projection.

## Current Boundary

Current post-M14 path:

1. `session.ingest_log.request.v1` is declared in the MemoryDB manifest and
   `MemoryDbDatastoreContract`.
2. `core.plugins.memorydb_contract.register_session_ingest_log_request_handler()`
   registers `handle_session_ingest_log_request()` under datastore id
   `memorydb`.
3. `core.runtime.events._handle_session_ingest_log()` imports
   `core.plugins.memorydb_contract.run_session_ingest_payload()` for active
   `session.ingest_log` processing.
4. `run_session_ingest_payload()` normalizes the payload and calls
   `core.ingest_runtime.run_session_logs_ingest()`.
5. `ingest.session_logs_ingest.run()` resolves transcript source, adapter-parses
   host JSONL when needed, stores SessionDB transcript rows through
   `core.services.session_memory_bridge`, and projects MemoryDB
   `session_chunks` evidence.
6. SessionDB now has manifest/contract metadata, but no activated handler and no
   ownership of the ingest request event.

## Selected First Slice: SessionDB Helper Extraction Only

Implement one runtime helper-extraction slice only:

1. Add `core.plugins.sessiondb_contract` with a `run_session_ingest_payload(payload)`
   helper that owns the existing payload normalization and call into
   `core.ingest_runtime.run_session_logs_ingest()`.
2. Keep the function signature and result shape identical to the current
   `core.plugins.memorydb_contract.run_session_ingest_payload(payload)` helper.
3. Keep `core.plugins.memorydb_contract.run_session_ingest_payload(payload)` as
   a thin compatibility/delegation wrapper to the new SessionDB helper for this
   slice, so existing active-event imports and monkeypatch tests can be migrated
   deliberately.
4. Wrapper shape: `memorydb_contract.run_session_ingest_payload(payload)` is a
   distinct function-level wrapper that imports and calls
   `core.plugins.sessiondb_contract.run_session_ingest_payload(payload)`. Do not
   re-export the SessionDB helper symbol by identity. Monkeypatches on either
   module affect only that module's symbol.
5. Keep `core.plugins.memorydb_contract.handle_session_ingest_log_request(event)`
   and `register_session_ingest_log_request_handler()` as the production request
   handler and registration surface. The registered datastore id remains
   `memorydb`.
6. Do not add `session.ingest_log.request.v1` to the SessionDB manifest or
   `SessionDbDatastoreContract` in this slice.
7. Do not add a new SessionDB-specific request event or register any activated
   SessionDB request handler.
8. Preserve active `session.ingest_log` envelope behavior exactly: successful
   ingest returns `{"status": "processed", "result": result}` through
   `process_events()`, failed/error helper results mark the event failed, and
   failHard behavior remains owned by `process_events()`.
9. Preserve request broker response behavior exactly: broker responses still use
   datastore id `memorydb` and the existing request envelope/result shape.
10. Preserve transcript source resolution, adapter JSONL parsing, participant
   metadata forwarding, SessionDB transcript row shape, MemoryDB projection,
   microchunk linkage, and `session_chunks` recall/source-window behavior.

## Non-Targets

- no manifest ownership change for `session.ingest_log.request.v1`
- no change to MemoryDB `session_chunks` recall selector ownership
- no new event names
- no activated SessionDB handlers or registration helpers
- no change to active `session.ingest_log` delivery mode or result envelope
- no daemon session-ingest callsite changes
- no transcript row schema or migration
- no MemoryDB projection, chunking, ranking, scoring, source-window, or recall
  result changes
- no lifecycle persistence for ack-only lifecycle events
- no SessionDB CLI syntax or output changes
- no default request routing, hidden/public CLI request-mode, `.ego`, alias
  retirement, or plugin-id rename changes

## FailHard Policy

- `failHard=true`: transcript resolution, adapter parsing, SessionDB storage,
  MemoryDB projection, or helper execution failure must continue to raise through
  the same active/request paths that raise today. Do not add fallback to the old
  MemoryDB helper body after the SessionDB helper is selected.
- `failHard=false`: existing fail-soft event and broker behavior may continue,
  but failures must remain loud and must not report the session log as indexed.
- Do not wrap SessionDB helper delegation and MemoryDB request registration in a
  broad shared try/except that can downgrade helper failures into registration or
  routing success.

## Required Tests Before W4

Add or preserve focused tests proving:

- `core.plugins.sessiondb_contract.run_session_ingest_payload()` preserves the
  current payload normalization and `run_session_logs_ingest()` call arguments.
- `core.plugins.memorydb_contract.run_session_ingest_payload()` delegates to the
  SessionDB helper without changing its public signature or result shape.
- Active `session.ingest_log` still produces the same processed/failed envelope
  and does not import/call `core.ingest_runtime.run_session_logs_ingest()` from
  `core.runtime.events`.
- `session.ingest_log.request.v1` broker registration still uses datastore id
  `memorydb` and does not register a SessionDB handler.
- MemoryDB manifest and contract still declare `session.ingest_log.request.v1`;
  SessionDB manifest and contract still do not.
- Request handler parity with direct session ingest still writes SessionDB rows
  and MemoryDB `session_chunks` with the same counts, metadata, and source kind.
- Source-window expansion tests still pass with no changed expansion metadata
  policy.
- failHard request/active failures raise through the same paths and do not fall
  back to a direct or old helper implementation.
- Helper failure in `sessiondb_contract.run_session_ingest_payload()` propagates
  through the `memorydb_contract.run_session_ingest_payload()` wrapper unchanged;
  MemoryDB request handler registration is not affected by helper failures.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow session-ingest smoke:

- `core.plugins.sessiondb_contract` is importable and owns the session ingest
  helper body.
- `session.ingest_log.request.v1` still registers under MemoryDB.
- Active and request session ingest still index a real transcript.
- SessionDB transcript rows and MemoryDB `session_chunks` projection remain
  present and recallable.
- Source-window expansion for session evidence is unchanged.

## Deferred Decisions

- whether SessionDB should ever own `session.ingest_log.request.v1`
- whether SessionDB should expose dedicated request handlers beyond generic
  metadata/maintenance surfaces
- lifecycle persistence for ack-only lifecycle events
- source-window metadata enrichment and selector ownership
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
