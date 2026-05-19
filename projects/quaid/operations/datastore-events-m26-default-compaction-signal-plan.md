# Datastore Events M26 Default Compaction Signal Plan

Status: draft plan; no runtime implementation yet
Owner: W1 runtime/daemon, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m25-default-timeout-signal-plan.md`

## Precondition

Do not implement runtime code for M26 until:

1. M25 default timeout bridge is closed through W4/W3/W6/W8.
2. W3 reviews the selected compaction slice because a default
   lifecycle-triggered daemon `compaction` signal can extract transcripts,
   project MemoryDB `session_chunks`, and affect recall-visible source-window
   evidence.
3. W6 reviews the runtime-event-to-daemon boundary because this slice expands
   default behavior for one more lifecycle event path and must not generalize the
   M24/M25 helpers.
4. W8 confirms static coverage includes lifecycle event processing, daemon
   signal writing/deduplication, compaction signal processing, active/request
   session ingest, source-window guards, and boundary checks.
5. W4 is ready to live-check that a plain compaction lifecycle event can enqueue
   exactly one existing daemon compaction signal and that duplicate adapter-hook
   compaction signals are still deduped.

This document is a plan only. It does not approve runtime code until W3/W6/W8
review this scope and W4 is ready to validate the installed runtime. It does
not approve reset default automation, daemon start/wake/restart behavior, new
signal types, new lifecycle event names, request/default routing changes,
SessionDB recall selectors, source-window selector ownership, broad
compatibility-alias retirement, CLI behavior changes, `.ego` integration, public
push, or release actions.

## Goal

M24 and M25 proved two narrow default lifecycle-to-daemon signal bridges:
terminal `session.agent_end -> session_end` and idle `session.timeout ->
timeout`. Both write existing daemon signal files through
`core.extraction_daemon.write_signal()` and leave daemon polling/processing
unchanged.

M26 selects the next narrow default bridge: plain `session.compaction` events may
queue the existing daemon `compaction` signal when the event already carries a
concrete `session_id` and a real `payload.transcript_path`. Compaction is
selected before reset because reset carries backup/cursor retargeting and
session-teardown semantics that should remain isolated until a separate plan.

M26 is not broad lifecycle automation. It does not wake or start the daemon. It
does not arm or change compaction context-refresh behavior. It only writes the
existing `compaction` signal file through `core.extraction_daemon.write_signal()`
and lets the daemon's normal polling and signal-processing path do the work.

## Current Boundary

Post-M25 path:

1. `_handle_session_lifecycle()` records SessionDB lifecycle observations for
   event-bus lifecycle events with `session_id`, preserving M20 acknowledgement
   semantics.
2. `_maybe_queue_lifecycle_daemon_signal()` may write existing daemon signals
   only when `payload.daemon_signal.enabled is True`.
3. M22 maps explicit opt-in `session.reset`, `session.compaction`,
   `session.timeout`, and `session.agent_end` events to existing daemon signal
   types `reset`, `compaction`, `timeout`, and `session_end`.
4. M24 added default queueing only for plain `session.agent_end` events with a
   concrete `session_id` and an existing `payload.transcript_path`.
5. M25 added default queueing only for plain `session.timeout` events with a
   concrete `session_id` and an existing `payload.transcript_path`.
6. Plain `session.compaction` events without `daemon_signal.enabled=true` remain
   acknowledgement plus SessionDB observation only, even when they carry a
   transcript path.
7. Adapter hooks and daemon scanners may already write compatible daemon signal
   files directly through `core.extraction_daemon.write_signal()`.
8. OpenClaw's adapter-level `before_compaction` hook has its own payload
   filtering, duplicate suppression, and context-refresh arming behavior. M26
   must not migrate or alter that adapter hook path.
9. M21 records metadata-only SessionDB lifecycle observations when the daemon
   later processes `reset`, `compaction`, `timeout`, and `session_end` signals.
   Those observations persist directly to SessionDB through
   `core.plugins.sessiondb_contract.record_session_lifecycle_observation()` and
   do not republish through `process_events()`.
10. MemoryDB remains the owner of `session_chunks` recall/write projection and
    final source-window output policy. SessionDB `capabilities.recall` remains
    `[]`.

## Selected First Slice: Default Compaction Signal Only

Implement one runtime slice only:

1. Add a private helper in `core.runtime.events`, near the M22/M24/M25 lifecycle
   daemon-signal helpers, that determines whether a plain lifecycle event is
   eligible for default compaction queueing. A suggested name is
   `_maybe_queue_default_compaction_signal(event, *, session_id)`. The helper
   must not live in `datastore.*` and must not import datastore modules.
2. The default bridge is selected only for `event.name == "session.compaction"`.
   It maps to the existing daemon signal type `compaction` only. Do not add
   default queueing for `session.reset`, `session.new`, `session.agent_start`,
   rolling, or any new event name.
3. Preserve the M22 explicit opt-in bridge exactly. If
   `payload.daemon_signal.enabled is True`, the M22 helper remains the selected
   path and keeps its existing four-event mapping, validation, passive envelope
   fields, and failHard behavior. The M26 default helper must not run a second
   queueing attempt after the explicit M22 bridge runs.
4. Preserve the M24 default terminal agent-end bridge exactly. Plain
   `session.agent_end` events keep the M24 helper, envelope fields, and
   failHard/fail-soft behavior.
5. Preserve the M25 default timeout bridge exactly. Plain `session.timeout`
   events keep the M25 helper, envelope fields, and failHard/fail-soft behavior.
6. The default compaction path requires a concrete non-empty `session_id` and an
   existing transcript path from `payload.transcript_path`. Do not read
   `payload.daemon_signal.transcript_path` for the default path; that field
   belongs to the explicit M22 bridge.
7. Missing, empty, or nonexistent `payload.transcript_path` for a plain
   `session.compaction` event is not a failure in this slice. It preserves the
   M20 acknowledgement plus observation behavior and does not add daemon signal
   fields. This is the explicit opt-out contract for emitters that need ack-only
   compaction lifecycle behavior: omit `payload.transcript_path` unless the event
   should queue the default daemon `compaction` signal.
8. Use `core.extraction_daemon.write_signal()` through an in-function import
   inside the default helper. Do not write signal files by hand. Do not import or
   call daemon process lifecycle helpers such as start, wake, stop, or restart.
9. Preserve idempotency by relying on existing `write_signal()` dedupe rules for
   compatible same-session/same-type signals. If an adapter hook already wrote a
   `compaction` signal for the same session, the default bridge must collapse to
   the same pending signal file instead of creating a duplicate.
10. Pass `supports_compaction_control` only from an explicit boolean
    `payload.supports_compaction_control` field. If the field is absent or not a
    boolean, pass `False`. Do not infer adapter compaction-control capability in
    `core.runtime.events`; adapter capability discovery remains outside this
    event-bus helper.
11. Record compact signal metadata only: bridge provenance, lifecycle event id,
    lifecycle event name, and optional adapter/source fields already present in
    the lifecycle payload. Do not put transcript text, extracted facts, recall
    rows, context-refresh marker contents, or source-window rows in signal
    metadata.
12. Successful default queueing may add passive fields to the acknowledgement
    result: `daemon_signal_queued: true`, `daemon_signal_type: "compaction"`,
    `signal_name: <write_signal result basename>`, and
    `daemon_signal_default: true`. It must not change `status` or `event`.
13. Under fail-soft, a selected default compaction queueing failure from
    `write_signal()` may preserve lifecycle acknowledgement semantics, but must
    log loudly and return `daemon_signal_queued: false`,
    `daemon_signal_default: true`, and
    `daemon_signal_error: <operator-readable string>`. It must not claim a
    signal was queued.
14. Under failHard, a selected default compaction queueing failure from
    `write_signal()` must raise through `process_events()` with the original
    exception chained. Do not catch it and return acknowledgement success.
15. Preserve M21 daemon observation behavior. When the daemon later processes the
    default-written compaction signal, it should follow the same observation path
    as adapter-written and explicit-M22 compaction signals.
16. M26 is infrastructure for event-bus compaction emitters that already know the
    active transcript path or will be migrated to provide it. Current adapter
    hook paths may continue to write `compaction` signals directly through
    `write_signal()`; this slice does not require migrating those hooks to
    event-bus lifecycle emission.

## Non-Targets

- no default queueing for reset, session.new, session.agent_start, rolling, or
  new event names
- no changes to the M24 default `session.agent_end` bridge
- no changes to the M25 default `session.timeout` bridge
- no daemon start/wake/restart behavior
- no new lifecycle event names or daemon signal types
- no changes to adapter hook signal-writing behavior
- no changes to OpenClaw `before_compaction` hook payload filtering, duplicate
  suppression, context-refresh arming, or direct signal writing
- no changes to daemon signal priority, polling, locking, cursor, rolling buffer,
  timeout classifier, timeout marker contents, reset backup, compaction context
  refresh, or transcript ownership behavior
- no change to `session.ingest_log` active/request payloads or result envelopes
- no request broker ownership or response-shape change
- no change to MemoryDB `session_chunks` recall/write ownership
- no SessionDB recall selector or source-window selector ownership
- no source-window selection, ranking, planner, token-budget, or output-ordering
  change
- no SessionDB transcript or lifecycle table migration
- no CLI/default-routing behavior change
- no broad compatibility-alias retirement or `notedb.core` plugin-id rename
- no `.ego` import/export integration
- no public push or release action

## FailHard Policy

- `failHard=true`: if a default `session.compaction` queueing attempt is selected
  and `write_signal()` fails, the failure must raise through the existing active
  event failHard path with the original exception chained. Do not return an
  acknowledged success for a failed selected default signal request.
- `failHard=false`: selected default compaction queueing failures may preserve
  lifecycle acknowledgement semantics, but must log loudly and must include
  passive metadata with `daemon_signal_queued=false` and an operator-readable
  error.
- Missing `session_id` or missing/nonexistent `payload.transcript_path` on plain
  lifecycle events is a compatibility no-op for the default path, not a selected
  queueing failure. Explicit M22 opt-in payloads keep their stricter validation.
- Do not fall back to writing signal files manually if `write_signal()` fails.
- Do not wrap SessionDB lifecycle observation persistence, default compaction
  signal writing, M25 default timeout signal writing, M24 default agent-end
  signal writing, explicit M22 daemon signal writing, and unrelated lifecycle
  acknowledgement logic in a shared broad `try`/`except` that could turn selected
  failures into silent success under failHard.
- Boundary check: M26 runtime must not add datastore imports to
  `core.runtime.events`, must not add `core/runtime/events.py` to any datastore
  composition allowlist, and must route signal creation through
  `core.extraction_daemon.write_signal()` only.

## Required Tests Before W4

Add or preserve focused tests proving:

- A plain `session.compaction` lifecycle event with concrete `session_id` and an
  existing `payload.transcript_path` writes exactly one existing `compaction`
  daemon signal through `core.extraction_daemon.write_signal()`.
- Successful default queueing adds only passive fields:
  `daemon_signal_queued=true`, `daemon_signal_type="compaction"`, `signal_name`,
  and `daemon_signal_default=true`, while preserving `status` and `event`.
- Plain `session.reset`, `session.new`, and `session.agent_start` lifecycle
  events do not use the compaction default bridge, even when they carry
  `payload.transcript_path`. M24 `session.agent_end` keeps its existing default
  `session_end` behavior and M25 `session.timeout` keeps its existing default
  `timeout` behavior.
- Plain `session.compaction` events with missing session id, missing transcript
  path, empty transcript path, or nonexistent transcript path preserve the M20
  acknowledgement/observation behavior and do not gain daemon signal fields.
- Explicit M22 opt-in behavior still wins: `payload.daemon_signal.enabled=true`
  keeps the M22 helper, envelope fields, stricter validation, and
  `payload.daemon_signal.transcript_path` handling, and the M26 default helper
  does not attempt a second queue.
- `payload.supports_compaction_control=True` is passed to `write_signal()` only
  when the field is explicitly boolean true; absent/non-boolean values pass
  `False`.
- Cross-path dedupe is explicit: if an adapter hook writes a compatible
  `compaction` signal directly through `write_signal()` and a default
  `session.compaction` bridge targets the same session, `read_pending_signals()`
  must show exactly one pending signal file.
- Monkeypatched `write_signal()` failures cover fail-soft logging/envelope
  behavior and failHard exception chaining for the selected default compaction
  path.
- `_handle_session_lifecycle()` continues to persist SessionDB lifecycle
  observations for concrete sessions and continues to acknowledge missing-session
  lifecycle events without persistence.
- M21 daemon observation tests still pass; a compaction signal written by this
  default bridge is observed by the daemon when processed.
- Source assertions or boundary checks prove the default helper does not import
  `datastore.*`, does not manually create signal files, and does not call daemon
  start/wake/restart helpers.
- Existing daemon signal tests for reset backup handling, compaction signal
  processing, compaction context refresh, timeout classification/markers,
  rolling flushes, stale-sweep recovery, session-log ingest request routing, and
  signal prioritization still pass.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window metadata tests still pass; default compaction lifecycle
  signal files must not affect source-window output policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow default compaction bridge smoke:

- Emit or process a plain `session.compaction` event with concrete `session_id`
  and a real `payload.transcript_path`. It should acknowledge, record the M20
  lifecycle observation, and write one compatible `compaction` daemon signal
  file.
- Processing that signal through the daemon should extract/project exactly as
  the pre-M26 daemon `compaction` signal path did and record the M21 daemon
  lifecycle observation.
- Replaying the same event or pairing it with an adapter-written compatible
  `compaction` signal should not duplicate pending signal files.
- Plain reset lifecycle events without explicit M22 opt-in should not write
  signal files.
- Plain `session.agent_end` should retain the M24 default `session_end` signal
  behavior, plain `session.timeout` should retain the M25 default `timeout`
  behavior, and explicit M22 opt-in should still win over all default paths.
- A plain `session.compaction` event without transcript path should keep the M20
  acknowledgement/observation behavior and should not gain daemon signal fields.
- M19 source-window recall for dated session evidence should still render the
  same `source_date: <date>` context header.

## Deferred Decisions

- default lifecycle-triggered reset transcript ingestion
- daemon start/wake/restart automation from lifecycle events
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- broad compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
