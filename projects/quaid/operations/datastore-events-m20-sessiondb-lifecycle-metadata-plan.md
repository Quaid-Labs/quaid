# Datastore Events M20 SessionDB Lifecycle Metadata Plan

Status: draft plan; no runtime implementation yet
Owner: W1 runtime/datastore, W6 boundary review
Plan source: `projects/quaid/operations/datastore-events-m19-sessiondb-source-window-metadata-plan.md`

## Precondition

Do not implement runtime code for M20 until:

1. M19 SessionDB source-window metadata is closed through W4/W3/W6/W8.
2. W6 reviews the lifecycle ownership boundary because this slice changes
   ack-only lifecycle events from pure core acknowledgements into optional
   SessionDB-owned provenance observations.
3. W8 confirms static coverage includes runtime event processing,
   session ingest active/request paths, SessionDB schema/helper tests, and
   boundary checks.
4. W4 is ready to live-check that lifecycle events still acknowledge normally
   and do not interfere with daemon/session ingest flows.

This document selects a narrow lifecycle metadata ownership slice only. It does
not approve lifecycle-triggered transcript ingestion, daemon signal-shape
changes, event-name changes, SessionDB recall selectors, source-window selector
ownership, MemoryDB compatibility-wrapper removal, request/default routing
changes, CLI behavior changes, `.ego` integration, public push, or release
actions.

## Goal

M14 through M19 moved SessionDB from metadata-only registration to owning session
transcript ingest helper/request ownership, active helper import, active failHard
propagation, and source-window provenance metadata. The remaining lifecycle
boundary is still ack-only: core runtime receives `session.new`,
`session.reset`, `session.compaction`, `session.timeout`, `session.agent_start`,
and `session.agent_end`, then `_handle_session_lifecycle()` returns an
acknowledgement without recording SessionDB-owned lifecycle provenance.

M20 selects the first persistence slice for those ack-only lifecycle events:
when a lifecycle event has a concrete `session_id`, SessionDB records a compact
lifecycle observation row, while core runtime keeps the same processed/failed
acknowledgement behavior and all ingest/recall/source-window flows remain
unchanged.

This is not a lifecycle automation slice. Persisting an observation must not
trigger transcript ingestion, compaction, reset handling, source-window expansion,
recall ranking, or daemon work.

## Current Boundary

Current post-M19 path:

1. `core.runtime.events.EVENT_REGISTRY` declares lifecycle events as active
   processable events.
2. `_handle_session_lifecycle()` returns `{"status": "acknowledged", "event":
   event.get("name")}` for all lifecycle event names.
3. `process_events()` records the active event as processed when the handler
   returns a non-failed acknowledgement.
4. SessionDB persists transcript rows only through session ingest paths; it does
   not record lifecycle-only observations.
5. Some lifecycle-event call sites and tests emit events without a `session_id`.
   Those events are legitimate queue/broker plumbing and must keep acknowledging
   without requiring SessionDB persistence.

## Selected First Slice: SessionDB Lifecycle Observation Metadata Only

Implement one runtime metadata slice only:

1. Add a SessionDB-owned lifecycle observation helper, for example in
   `datastore.sessiondb.session_store`, that records compact lifecycle metadata
   for events with a concrete session id.
2. Store lifecycle observations in a dedicated SessionDB table rather than
   mutating transcript rows or MemoryDB projections. Suggested table shape:
   owner id, session id, event name, event id or idempotency key, source,
   observed timestamp, optional reason, and a compact JSON metadata payload.
3. Keep lifecycle observation idempotent for the same runtime event. Reprocessing
   the same event id or idempotency key must not create duplicate rows.
4. Update `_handle_session_lifecycle()` to call the SessionDB helper only when a
   session id is present in the event envelope or payload. Preserve the existing
   acknowledgement status and event field. It may add passive metadata such as
   `persisted: true|false` and `datastore_id: "sessiondb"`, but must not change
   processed/failed semantics.
5. Preserve compatibility for lifecycle events without a session id. They remain
   acknowledged and are not persisted; this is an intentional compatibility path
   for existing queue/broker tests and host lifecycle plumbing.
6. Preserve active/request session ingest behavior and M16/M17/M18/M19 ownership
   boundaries. M20 must not alter `session.ingest_log`,
   `session.ingest_log.request.v1`, SessionDB source-window metadata, MemoryDB
   `session_chunks` projection, or MemoryDB compatibility wrappers.
7. Preserve event names, delivery modes, payload shapes, and operator-visible
   lifecycle acknowledgement behavior.

## Non-Targets

- no new lifecycle event names
- no lifecycle-triggered transcript ingest, compaction, reset cleanup, or daemon
  signal scheduling
- no change to `session.ingest_log` active/request payloads or result envelopes
- no request broker ownership or response-shape change
- no change to MemoryDB `session_chunks` recall/write ownership
- no SessionDB recall selector or source-window selector ownership
- no source-window selection, ranking, planner, token-budget, or output-ordering
  change
- no removal, warning, or deprecation from MemoryDB compatibility wrappers
- no CLI/default-routing behavior change
- no transcript row schema migration beyond the selected lifecycle observation
  table
- no `.ego` import/export integration
- no compatibility-alias retirement or `notedb.core` plugin-id rename

## FailHard Policy

- `failHard=true`: if a lifecycle event has a session id and SessionDB
  persistence fails, the failure must raise through the existing active event
  failHard path. Do not return an acknowledged success for a failed selected
  persistence write.
- `failHard=false`: persistence failures may preserve the previous
  acknowledgement envelope, but must log loudly and include passive metadata that
  persistence failed. Do not claim `persisted=true` when the write failed.
- Missing `session_id` is not a persistence failure in this slice. It is an
  explicit compatibility path and should return an acknowledgement with
  `persisted=false` or equivalent passive metadata.
- Do not wrap SessionDB persistence and unrelated lifecycle acknowledgement logic
  in a shared broad `try`/`except` that could convert selected persistence
  failures into silent success under failHard.

## Required Tests Before W4

Add or preserve focused tests proving:

- SessionDB creates the lifecycle observation table and records one row for a
  lifecycle event with `session_id`, owner id, event name, event id/idempotency
  key, source, and reason metadata.
- Reprocessing the same event id or idempotency key is idempotent and does not
  create duplicate lifecycle rows.
- `_handle_session_lifecycle()` preserves the existing acknowledgement shape for
  all six lifecycle event names and records SessionDB metadata only when a
  session id is present.
- Lifecycle events without `session_id` still acknowledge and do not call the
  SessionDB persistence helper.
- Under `failHard=true`, a selected SessionDB lifecycle persistence failure
  raises through `process_events()` with the original exception chained by the
  existing event-level failHard machinery.
- Under `failHard=false`, a selected persistence failure logs loudly, returns an
  acknowledgement with `persisted=false` (or equivalent passive metadata), and
  does not claim the observation was stored.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window metadata tests still pass; lifecycle metadata must not affect
  source-window output.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow lifecycle smoke:

- `session.reset` or `session.agent_start` with `session_id` acknowledges and
  records a SessionDB lifecycle observation row.
- Replaying the same event id or idempotency key does not duplicate rows.
- A lifecycle event without `session_id` still acknowledges without persistence.
- `session.ingest_log` active and `session.ingest_log.request.v1` request paths
  still ingest transcripts and project MemoryDB `session_chunks` evidence.
- M19 source-window recall for dated session evidence still renders the same
  `source_date: <date>` context header.

## Deferred Decisions

- lifecycle-triggered transcript ingestion or daemon work
- request/active compatibility-wrapper removal from `core.plugins.memorydb_contract`
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
