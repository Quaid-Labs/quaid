# Datastore Events M22 Lifecycle Daemon Signal Bridge Plan

Status: runtime opt-in bridge slice complete; default automation deferred
Owner: W1 runtime/daemon, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m21-daemon-lifecycle-observation-plan.md`

## Precondition

Runtime code for M22 was gated on:

1. M21 daemon lifecycle observation bridge is closed through W4/W3/W6/W8.
2. W6 reviews the runtime-event-to-daemon boundary because this slice would let
   selected lifecycle events enqueue existing daemon signal files.
3. W3 reviews the recall/source-window boundary because queued daemon signals can
   extract transcripts and project MemoryDB `session_chunks` evidence.
4. W8 confirms static coverage includes lifecycle event processing, daemon signal
   writing/deduplication, active/request session ingest, source-window guards,
   and boundary checks.
5. W4 is ready to live-check that an explicitly requested lifecycle signal bridge
   preserves normal daemon extraction/projection behavior and does not duplicate
   adapter hook signals.

This document records the completed narrow explicit bridge only. It does not approve default
lifecycle-triggered extraction, new daemon signal types, new lifecycle event
names, changes to adapter hook signal paths, request/default routing changes,
SessionDB recall selectors, source-window selector ownership, MemoryDB
compatibility-wrapper removal, CLI behavior changes, `.ego` integration, public
push, or release actions.

## Goal

M20 records lifecycle observations for event-bus lifecycle events with concrete
`session_id`. M21 records metadata-only observations when the daemon processes
existing lifecycle signal files. The remaining lifecycle automation boundary is
still intentionally closed: a normal `session.reset`, `session.compaction`,
`session.timeout`, or `session.agent_end` event does not itself enqueue daemon
extraction work.

M22 selected a first explicitly requested bridge between those two surfaces. If a
lifecycle event payload opts in and supplies a concrete transcript path, the
runtime now writes the corresponding existing daemon signal type through the same
signal-file path used by adapter hooks. Normal lifecycle events without the
explicit opt-in remain acknowledgement-plus-observation only.

This is not default automation. M22 does not infer extraction work from lifecycle
event names alone, does not wake or start the daemon in the first slice, and does
not change which transcripts adapter hooks already signal.

## Current Boundary

Pre-M22 path:

1. `_handle_session_lifecycle()` records SessionDB lifecycle observations for
   event-bus lifecycle events with `session_id`, preserving acknowledgement
   semantics.
2. Adapter hooks and daemon scanners write extraction signal files directly via
   `core.extraction_daemon.write_signal()`.
3. `process_signal()` handles existing `reset`, `compaction`, `timeout`,
   `session_end`, and `rolling` signal files, extracts transcript content, and
   projects SessionDB transcript rows plus MemoryDB `session_chunks` evidence.
4. M21 records metadata-only SessionDB lifecycle observations for daemon
   `reset`/`compaction`/`timeout`/`session_end` signals; `rolling` remains
   excluded.
5. No event-bus lifecycle handler enqueued daemon signal files.

## Selected First Slice: Explicit Opt-In Signal File Bridge

Implemented one runtime slice only:

1. Added a small helper for lifecycle-event daemon signal requests, for example
   `_maybe_queue_lifecycle_daemon_signal(event, *, session_id)`, as a private
   module-level function in `core.runtime.events` near
   `_handle_session_lifecycle()`. Do not create a new dedicated helper module
   for this single bridge. The helper must not live in `datastore.*` and must
   not import datastore modules.
2. The bridge is opt-in only. It runs only when the lifecycle event payload
   contains an explicit `daemon_signal` object with `enabled: true`. A plain
   lifecycle event without that object or with `enabled` false keeps the exact
   M20/M21 acknowledgement and observation behavior.
   - Payload schema: `payload.daemon_signal = {"enabled": bool, "transcript_path": str,
     "reason": str, "source": str}`. Only `enabled=true` selects the bridge.
     `transcript_path`, `reason`, and `source` are optional strings; unknown
     fields under `daemon_signal` are ignored, not rejected, and are not
     propagated to the signal metadata.
3. Require a concrete `session_id` and transcript path. The transcript path may
   be supplied as `payload.daemon_signal.transcript_path` or, if absent there,
   as `payload.transcript_path`. Missing, empty, or nonexistent transcript paths
   are selected request failures, not silent no-ops.
4. Mapped only existing lifecycle events to existing daemon signal types:
   `session.reset -> reset`, `session.compaction -> compaction`,
   `session.timeout -> timeout`, and `session.agent_end -> session_end`.
   `session.new` and `session.agent_start` do not queue daemon signals in this
   slice. Do not add new event names and do not add a `session.session_end`
   event. Implement this mapping as a module-level constant such as
   `LIFECYCLE_EVENT_TO_DAEMON_SIGNAL = {"session.reset": "reset",
   "session.compaction": "compaction", "session.timeout": "timeout",
   "session.agent_end": "session_end"}` so tests can assert the mapping
   directly. `session.new`, `session.agent_start`, and `rolling` must be absent
   from the constant.
5. Uses `core.extraction_daemon.write_signal()` through an in-function import
   inside the private helper. Do not write signal files by hand. Do not add a
   direct dependency from lifecycle handling to datastore implementations. Do
   not import or call daemon process lifecycle helpers such as start, wake, stop,
   or restart functions.
6. Preserves daemon signal idempotency by relying on the existing `write_signal()`
   dedupe rules for compatible same-session/same-type signals. Do not generate a
   new daemon signal UUID outside `write_signal()`.
7. Records compact signal metadata only: reason/source fields from
   `payload.daemon_signal`, the lifecycle event id/name, and the adapter label if
   provided. Do not put transcript text, extracted facts, recall rows, or
   source-window rows in the signal metadata.
8. Preserves `_handle_session_lifecycle()` acknowledgement shape. Successful
   explicit queueing may add passive fields such as `daemon_signal_queued: true`,
   `daemon_signal_type`, and `signal_name`, but must not change `status` or
   `event`. Non-opt-in events must not gain those fields.
   - Success envelope: add `daemon_signal_queued: true`,
     `daemon_signal_type: <mapped daemon signal type string>`, and
     `signal_name: <signal file basename or daemon-side identifier returned by
     write_signal()>`.
   - Fail-soft envelope: add `daemon_signal_queued: false` and
     `daemon_signal_error: <operator-readable string>`.
   - Plain lifecycle events without opt-in must not gain any
     `daemon_signal_*` fields or `signal_name`.
9. Does not wake, start, stop, or restart the daemon in this first slice. The
   selected bridge writes the existing signal file only; daemon process lifecycle
   automation remains future-plan-gated.
10. Preserves M21 daemon observation behavior. When the daemon later processes the
    queued signal, M21 observation recording should work exactly as it does for
    adapter-written signals.

## Non-Targets

- no default lifecycle-triggered transcript ingest
- no daemon start/wake/restart behavior
- no new event names or signal types
- no changes to adapter hook signal-writing behavior
- no changes to daemon signal priority, polling, locking, cursor, rolling buffer,
  timeout classifier, reset backup, or transcript ownership behavior
- no change to `session.ingest_log` active/request payloads or result envelopes
- no request broker ownership or response-shape change
- no change to MemoryDB `session_chunks` recall/write ownership
- no SessionDB recall selector or source-window selector ownership
- no source-window selection, ranking, planner, token-budget, or output-ordering
  change
- no removal, warning, or deprecation from MemoryDB compatibility wrappers
- no CLI/default-routing behavior change
- no SessionDB transcript or lifecycle table migration
- no `.ego` import/export integration
- no compatibility-alias retirement or `notedb.core` plugin-id rename

## FailHard Policy

- `failHard=true`: if a lifecycle event explicitly requests daemon signal
  queueing and the request is malformed, transcript path validation fails, or
  `write_signal()` fails, the failure must raise through the existing active
  event failHard path. Do not return an acknowledged success for a failed
  selected signal request.
- `failHard=false`: selected queueing failures may preserve lifecycle
  acknowledgement semantics, but must log loudly and must include passive
  metadata such as `daemon_signal_queued: false` and an operator-readable error.
- Missing `session_id` for a normal lifecycle event remains the M20 compatibility
  path and is not a signal-queueing failure unless `daemon_signal.enabled` is
  explicitly true.
- Do not fall back to writing signal files manually if `write_signal()` fails.
- Do not wrap SessionDB lifecycle observation persistence, daemon signal writing,
  and unrelated lifecycle acknowledgement logic in a shared broad `try`/`except`
  that could turn selected failures into silent success under failHard.
- Boundary check: M22 runtime must not add datastore imports to
  `core.runtime.events`, must not add `core/runtime/events.py` to any datastore
  composition allowlist, and must route signal creation through
  `core.extraction_daemon.write_signal()` only.

## Required Tests Before W4

Add or preserve focused tests proving:

- A lifecycle event with `daemon_signal.enabled=true`, concrete `session_id`, and
  existing transcript path writes exactly one existing daemon signal through
  `core.extraction_daemon.write_signal()` with the mapped signal type.
- Plain lifecycle events without the explicit opt-in do not call `write_signal()`
  and preserve the M20/M21 acknowledgement/observation envelope.
- `session.new`, `session.agent_start`, and `rolling`-equivalent payloads do not
  queue daemon signals in this slice.
- Tests assert the `LIFECYCLE_EVENT_TO_DAEMON_SIGNAL` constant exactly and prove
  excluded lifecycle events are absent from it.
- Re-emitting the same explicit lifecycle signal request uses `write_signal()`
  dedupe behavior and does not create duplicate pending signal files.
- Cross-path dedupe is explicit: if an adapter hook writes a compatible signal
  directly through `write_signal()` and a lifecycle bridge targets the same
  `session_id` plus mapped signal type, `read_pending_signals()` must show
  exactly one pending signal file.
- Missing/nonexistent transcript path under failHard raises with the original
  exception chained; fail-soft logs loudly and returns `daemon_signal_queued=false`
  without claiming a queued signal.
- `_handle_session_lifecycle()` continues to persist SessionDB lifecycle
  observations for concrete sessions and continues to acknowledge missing-session
  lifecycle events without persistence.
- M21 daemon observation tests still pass; a signal written by this bridge is
  observed by the daemon when processed.
- Source assertions or boundary checks prove the lifecycle helper does not
  import `datastore.*`, does not manually create signal files, and does not call
  daemon start/wake/restart helpers.
- Existing daemon signal tests for reset backup handling, timeout classification,
  rolling flushes, stale-sweep recovery, session-log ingest request routing, and
  signal prioritization still pass.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window metadata tests still pass; lifecycle-triggered signal files
  must not affect source-window output policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow explicit bridge smoke:

- Emit or process a `session.agent_end` or `session.compaction` event with
  `daemon_signal.enabled=true`, concrete `session_id`, and a real transcript
  path. It should acknowledge, record the M20 lifecycle observation, and write
  one compatible daemon signal file.
- Processing that signal through the daemon should extract/project exactly as
  the pre-M22 daemon signal path did and record the M21 daemon lifecycle
  observation.
- Replaying the same explicit request should not duplicate signal files.
- A plain lifecycle event without `daemon_signal.enabled=true` should not write a
  signal file.
- M19 source-window recall for dated session evidence should still render the
  same `source_date: <date>` context header.

## Deferred Decisions

- default terminal `session.agent_end` lifecycle-to-daemon signal bridge closed
  in M24 at `058737670`; broader reset/compaction/timeout
  lifecycle-triggered transcript ingestion remains deferred
- daemon start/wake/restart automation from lifecycle events
- SessionDB ingest wrapper retirement closed in M23 at `bfe5836b` +
  `4a3824d88`
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration

## Implementation Record

Runtime closed in `4fbecd088` (`refactor(datastore): bridge opt-in lifecycle signals to daemon`) with test-only follow-up `90a0fb2de` (`test(datastore): cover M22 daemon signal write failures`).

Implemented behavior:

- `core.runtime.events.LIFECYCLE_EVENT_TO_DAEMON_SIGNAL` maps only `session.reset -> reset`, `session.compaction -> compaction`, `session.timeout -> timeout`, and `session.agent_end -> session_end`. `session.new`, `session.agent_start`, and `rolling` remain excluded.
- `_maybe_queue_lifecycle_daemon_signal()` is a private helper next to `_handle_session_lifecycle()`. It imports `core.extraction_daemon.write_signal()` in-function, does not import `datastore.*`, does not manually write signal files, and does not call daemon start/wake/stop/restart helpers.
- The bridge is selected only by `payload.daemon_signal.enabled is True`. Plain lifecycle events keep the M20 acknowledgement plus SessionDB observation envelope and do not gain `daemon_signal_*` fields or `signal_name`.
- Opt-in payloads require a concrete `session_id` and an existing transcript path from `payload.daemon_signal.transcript_path` or `payload.transcript_path`. Missing session id, missing transcript path, nonexistent transcript path, and `write_signal()` failures are selected request failures.
- Success adds only passive bridge metadata to the existing acknowledgement result: `daemon_signal_queued=True`, `daemon_signal_type=<mapped signal type>`, and `signal_name=<write_signal result basename>`. Fail-soft selected failures log loudly and return `daemon_signal_queued=False` plus `daemon_signal_error`.
- Signal metadata is compact: bridge provenance, lifecycle event id/name, and optional `reason`/`source` from `payload.daemon_signal`. Unknown `daemon_signal` fields are ignored and not propagated. Transcript text, extracted facts, recall rows, and source-window rows are not added to signal metadata.
- Cross-path dedupe is delegated to existing `write_signal()` behavior. Adapter-written and lifecycle-bridge-written compatible same-session/same-type pending signals collapse to one pending signal file.
- Under `failHard=true`, selected bridge failures raise through `process_events()` with the original exception chained. Under `failHard=false`, selected bridge failures log loudly and preserve lifecycle acknowledgement semantics without claiming a queued signal.
- M20 lifecycle observation recording remains independent: fail-soft SessionDB observation failures do not block an explicitly requested daemon signal, and daemon signal failures do not claim lifecycle observation failure.
- M21 daemon observation behavior is unchanged; when the daemon later processes a bridge-written signal, it follows the same observation path as adapter-written signals.
- MemoryDB `session_chunks` recall/write ownership, SessionDB `capabilities.recall=[]`, M19 source-window metadata/output policy, M16 request ownership, M17/M18 active ingest behavior, MemoryDB compatibility wrappers, daemon polling/priority/locking/cursor/rolling behavior, CLI/default routing, and daemon process lifecycle all remain unchanged.

Test coverage added or preserved:

- exact mapping constant and excluded event assertions;
- explicit opt-in success path writes one existing daemon signal through `write_signal()` and records the expected passive envelope fields;
- non-opt-in lifecycle events do not call `write_signal()` and have no bridge fields;
- `session.new` and `session.agent_start` remain excluded even with opt-in payloads;
- missing transcript path, nonexistent transcript path, missing session id, and monkeypatched `write_signal()` exceptions cover fail-soft logging/envelope behavior and failHard exception chaining;
- cross-path adapter-hook plus lifecycle-bridge dedupe produces exactly one pending signal;
- source assertions prove no datastore import, no manual signal write helper, and no daemon start/wake/restart helper call;
- existing event, daemon signal, source-window, SessionDB bridge, registry, docs consistency, boundary, and unit-wrapper lanes remain green.

Validation chain:

- W4 R201 PASS on `4fbecd088`: opt-in bridge source proof and live smoke passed;  `90a0fb2de` was test-only and required no fresh live deploy.
- W3 APPROVED `4fbecd088` with no runtime/recall findings; W3 APPROVED `90a0fb2de` as test-only with no behavior delta.
- W6 APPROVED-WITH-CONCERNS on `4fbecd088`, then APPROVED `90a0fb2de` after the explicit `write_signal()` exception coverage gap was closed; the discarded-return note remains informational.
- W8 STATIC PASS/runtime HOLD for `4fbecd088` + `90a0fb2de`; runtime hold was only awaiting final closure recording.
