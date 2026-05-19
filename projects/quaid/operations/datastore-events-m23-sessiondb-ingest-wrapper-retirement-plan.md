# Datastore Events M23 SessionDB Ingest Wrapper Retirement Plan

Status: draft plan; no runtime implementation yet
Owner: W1 runtime/datastore, W3 recall and source-window review
Plan source: `projects/quaid/operations/datastore-events-m22-lifecycle-daemon-signal-bridge-plan.md`

## Precondition

Do not implement runtime code for M23 until:

1. M22 explicit opt-in lifecycle-to-daemon signal bridge is closed through
   W4/W3/W6/W8.
2. W3 reviews the selected slice because `session.ingest_log` still writes
   SessionDB transcript rows, projects MemoryDB `session_chunks`, and feeds
   source-window expansion.
3. W6 reviews the compatibility boundary because this slice removes the last
   MemoryDB module import-path wrappers for SessionDB-owned session ingest.
4. W8 confirms static coverage includes active session ingest, request session
   ingest, extraction-daemon request routing, datastore manifests/contracts,
   session memory bridge, source-window guards, import/source assertions, and
   boundary checks.
5. W4 is ready to live-check that active and request session ingest still index a
   real transcript and project MemoryDB `session_chunks` evidence after wrapper
   retirement.

This document is a plan only. It does not approve runtime changes until the
preconditions above are satisfied and the plan is reviewed. It does not approve
removing MemoryDB `session_chunks` APIs, changing recall/source-window behavior,
changing lifecycle signal behavior, broad compatibility-alias retirement,
`notedb.core` plugin-id rename, `.ego` integration, public push, or release
actions.

## Goal

M15 introduced `core.plugins.sessiondb_contract.run_session_ingest_payload()` as
the SessionDB-owned helper while keeping MemoryDB wrappers for import-path
compatibility. M16 moved `session.ingest_log.request.v1` ownership and
registration to SessionDB. M17 moved active `session.ingest_log` to import the
SessionDB helper directly. M18 restored failHard propagation for unexpected
active helper exceptions.

After those closures, the remaining `core.plugins.memorydb_contract`
session-ingest wrappers are no longer used by in-repo production paths. They now widen
the ownership boundary by leaving a MemoryDB module surface that appears to own
SessionDB transcript ingest.

M23 selects one cleanup slice: retire only the obsolete MemoryDB
`session.ingest_log` compatibility wrappers, while preserving SessionDB-owned
active/request ingest behavior and MemoryDB-owned `session_chunks` recall/write
projection.

This is not broad compatibility-alias retirement. It is not a recall ownership
move. It is not a source-window selector change.

## Current Boundary

Current post-M22 path:

1. `core.plugins.sessiondb_contract.run_session_ingest_payload()` owns
   session-ingest payload normalization and delegates to
   `core.ingest_runtime.run_session_logs_ingest()`.
2. `core.plugins.sessiondb_contract.handle_session_ingest_log_request()` owns the
   request handler for `session.ingest_log.request.v1`.
3. `core.plugins.sessiondb_contract.register_session_ingest_log_request_handler()`
   registers the request handler under datastore id `sessiondb`.
4. `core.extraction_daemon._request_session_logs_ingest()` imports the SessionDB
   registrar and requires exactly one SessionDB broker response.
5. `core.runtime.events._handle_session_ingest_log()` imports the SessionDB
   helper directly and lets unexpected helper/import exceptions reach
   `process_events()` failHard machinery.
6. `core.plugins.memorydb_contract` still exposes three silent compatibility
   wrappers for the same SessionDB-owned surfaces:
   `run_session_ingest_payload()`, `handle_session_ingest_log_request()`, and
   `register_session_ingest_log_request_handler()`.
7. MemoryDB still owns `session_chunks` recall/write capability and final
   source-window output policy. SessionDB `capabilities.recall` remains `[]`.

## Selected First Slice: Session-Ingest Wrapper Retirement Only

Implement one runtime cleanup slice only:

1. Remove these obsolete SessionDB ingest compatibility wrappers from
   `core.plugins.memorydb_contract`:
   - `run_session_ingest_payload(payload)`
   - `handle_session_ingest_log_request(event)`
   - `register_session_ingest_log_request_handler()`
2. Do not add replacement shims, deprecation wrappers, warning wrappers, or
   fallback wrappers under MemoryDB. If W4 or W6 finds a real installed-alpha
   caller that still depends on these import paths, stop the runtime change and
   write a separate compatibility-shim plan with an owner and removal condition.
3. Preserve `core.plugins.sessiondb_contract` helper, handler, and registrar
   names and behavior exactly.
4. Preserve active `session.ingest_log` behavior exactly: event name, payload
   schema, missing-`session_id` failed envelope, helper-returned failed/error
   envelope, successful processed envelope, and failHard exception propagation
   stay unchanged.
5. Preserve `session.ingest_log.request.v1` behavior exactly: request event name,
   payload schema, SessionDB datastore id, broker response shape, and
   extraction-daemon broker validation stay unchanged.
6. Preserve SessionDB transcript row shape, MemoryDB `session_chunks`
   projection, microchunk linkage, source kind, source-window expansion inputs,
   recall selector ownership, ranking, planner behavior, and token budget.
7. Preserve MemoryDB-owned non-session-ingest contract functions, including
   extraction publish, domain sync, maintenance, and `session_chunks` public
   MemoryDB APIs. Do not remove `store_session_chunks()`,
   `list_session_chunks()`, `get_session_chunk()`, or any MemoryDB recall
   selector surface.
8. Do not change datastore manifests or capabilities in this slice except for
   tests/source assertions that continue to prove SessionDB owns
   `session.ingest_log.request.v1` and MemoryDB owns `session_chunks`.
9. Do not change lifecycle observation, daemon signal bridge, daemon polling,
   signal finalization, rolling behavior, CLI behavior, default routing,
   compatibility aliases outside these three wrappers, or `.ego` behavior.

## Non-Targets

- no new event names
- no active `session.ingest_log` delivery-mode change
- no active or request payload schema change
- no active processed/failed envelope change
- no request broker response-shape change
- no SessionDB helper, request handler, or registrar rename
- no MemoryDB `session_chunks` recall/write ownership change
- no SessionDB recall selector or source-window selector ownership
- no MemoryDB projection, chunking, ranking, scoring, source-window, or recall
  result changes
- no lifecycle observation or lifecycle-to-daemon signal bridge changes
- no transcript row schema or data migration
- no daemon scheduling, signal-shape, polling, cursor, lock, or rolling changes
- no default request routing, hidden/public CLI request-mode, broad
  compatibility-alias retirement, `notedb.core` rename, `.ego`, public push, or
  release actions

## FailHard Policy

- `failHard=true`: active/request session-ingest failures must continue to raise
  through the existing SessionDB helper, request broker, and `process_events()`
  paths. Do not add any fallback to a removed MemoryDB wrapper or direct
  `core.ingest_runtime.run_session_logs_ingest()` import.
- `failHard=false`: existing fail-soft event and broker behavior may continue,
  but failures must remain loud and must not report the session log as indexed.
- If any in-repo production path still imports one of the removed MemoryDB
  session-ingest wrappers, that is a code bug to fix at the import site, not a
  reason to keep a silent wrapper.
- Do not catch `ImportError` or `AttributeError` from removed wrapper imports and
  route around them to SessionDB. Hidden compatibility fallback would recreate
  the same ownership ambiguity.
- Do not wrap wrapper-retirement source assertions and runtime ingest behavior in
  a shared broad try/except that can mask a missing import or failed ingest as a
  successful event.

## Required Tests Before W4

Add or preserve focused tests proving:

- `core.plugins.memorydb_contract` no longer defines
  `run_session_ingest_payload`, `handle_session_ingest_log_request`, or
  `register_session_ingest_log_request_handler`.
- Production source no longer imports or references the removed MemoryDB
  session-ingest wrapper names. Scope the scan to production code; tests may
  reference the names only to assert absence.
- `core.plugins.sessiondb_contract` still defines and owns
  `run_session_ingest_payload`, `handle_session_ingest_log_request`, and
  `register_session_ingest_log_request_handler`.
- Active `session.ingest_log` still imports/calls
  `core.plugins.sessiondb_contract.run_session_ingest_payload()` directly and
  does not import or call MemoryDB session-ingest wrappers.
- Missing `payload.session_id`, helper-returned failed/error results, successful
  active results, fail-soft unexpected helper exceptions, and failHard unexpected
  helper exceptions preserve the M18 behavior.
- `session.ingest_log.request.v1` still registers under SessionDB and still
  returns exactly one SessionDB broker response row.
- `core.extraction_daemon._request_session_logs_ingest()` still imports/registers
  through SessionDB, expects a `sessiondb` response, and rejects missing,
  malformed, failed, or non-SessionDB responses without fallback.
- Active and request session ingest still write SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window expansion tests still pass with no changed expansion metadata
  or output policy.
- M20/M21/M22 lifecycle observation and daemon signal bridge tests still pass;
  wrapper retirement must not touch lifecycle or daemon paths.
- Boundary checks still pass without adding `core.plugins.memorydb_contract` or
  `core.runtime.events` datastore-composition exceptions.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow session-ingest smoke:

- `core.plugins.memorydb_contract` no longer exposes the three session-ingest
  wrapper names.
- `core.plugins.sessiondb_contract` remains importable and owns helper, handler,
  and registrar behavior.
- Active `session.ingest_log` indexes a real transcript and projects MemoryDB
  `session_chunks` evidence with the same visible result shape.
- `session.ingest_log.request.v1` still registers under SessionDB and returns
  broker response datastore id `sessiondb`.
- MemoryDB `session_chunks` recall/source-window evidence remains present and
  recallable.
- M22 opt-in lifecycle-to-daemon signal bridge and M21 daemon lifecycle
  observation behavior remain intact.

## Deferred Decisions

- default lifecycle-triggered transcript ingestion
- daemon start/wake/restart automation from lifecycle events
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- broad compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
