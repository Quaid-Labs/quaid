# Datastore Events M24 Default Agent-End Signal Plan

Status: runtime default terminal bridge slice complete; broader automation deferred
Owner: W1 runtime/daemon, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m22-lifecycle-daemon-signal-bridge-plan.md`

## Precondition

Runtime code for M24 was gated on:

1. M23 SessionDB ingest wrapper retirement is closed through W4/W3/W6/W8.
2. W3 reviews the selected slice because a default lifecycle-triggered daemon
   signal can extract transcripts, project MemoryDB `session_chunks`, and affect
   recall-visible source-window evidence.
3. W6 reviews the runtime-event-to-daemon boundary because this slice changes the
   default behavior of one lifecycle event path.
4. W8 confirms static coverage includes lifecycle event processing, daemon signal
   writing/deduplication, active/request session ingest, source-window guards,
   and boundary checks.
5. W4 is ready to live-check that a plain terminal lifecycle event can enqueue
   exactly one existing daemon signal and that duplicate adapter-hook signals are
   still deduped.

This document records the completed narrow default terminal bridge slice only.
It did not approve reset/compaction/timeout default automation, daemon
start/wake/restart behavior, new signal types, new lifecycle event names,
request/default routing changes, SessionDB recall selectors, source-window
selector ownership, broad compatibility-alias retirement, CLI behavior changes,
`.ego` integration, public push, or release actions.

## Goal

M22 added an explicit lifecycle-to-daemon signal bridge: lifecycle events only
queue daemon work when `payload.daemon_signal.enabled=true` is present. That
protected alpha stability while proving the event-bus-to-daemon boundary,
`write_signal()` dedupe, failHard behavior, and M21 daemon observation path.

The next lowest-risk default behavior was narrower than the full deferred
lifecycle automation item: only `session.agent_end` now infers an existing
`session_end` daemon signal when the event already carries a concrete
`session_id` and a real transcript path. Terminal agent-end events are the
natural point to flush transcript evidence, while reset, compaction, timeout,
rolling, and daemon process lifecycle automation remain future-plan-gated.

M24 selected this one default bridge only. It keeps M22's explicit opt-in bridge
for all four mapped lifecycle events and adds a default path for terminal
`session.agent_end` only.

This is not broad lifecycle automation. It does not wake or start the daemon. It
only writes the existing `session_end` signal file through
`core.extraction_daemon.write_signal()` and lets the daemon's normal polling and
signal-processing path do the work.

## Current Boundary

Pre-M24 path:

1. `_handle_session_lifecycle()` records SessionDB lifecycle observations for
   event-bus lifecycle events with `session_id`, preserving M20 acknowledgement
   semantics.
2. `_maybe_queue_lifecycle_daemon_signal()` may write existing daemon signals
   only when `payload.daemon_signal.enabled is True`.
3. M22 maps explicit opt-in `session.reset`, `session.compaction`,
   `session.timeout`, and `session.agent_end` events to existing daemon signal
   types `reset`, `compaction`, `timeout`, and `session_end`.
4. Plain lifecycle events without `daemon_signal.enabled=true` remain
   acknowledgement plus SessionDB observation only, even when they carry a
   transcript path.
5. Adapter hooks and daemon scanners may already write compatible daemon signal
   files directly through `core.extraction_daemon.write_signal()`.
6. M21 records metadata-only SessionDB lifecycle observations when the daemon
   later processes `reset`, `compaction`, `timeout`, and `session_end` signals.
   Those observations persist directly to SessionDB through
   `core.plugins.sessiondb_contract.record_session_lifecycle_observation()` and
   do not republish through `process_events()`.
7. MemoryDB remains the owner of `session_chunks` recall/write projection and
   final source-window output policy. SessionDB `capabilities.recall` remains
   `[]`.

## Selected First Slice: Default Terminal Agent-End Signal Only

Implemented one runtime slice only:

1. Added a private helper in `core.runtime.events`, near
   `_maybe_queue_lifecycle_daemon_signal()`, that determines whether a plain
   lifecycle event is eligible for default daemon signal queueing. A suggested
   name is `_maybe_queue_default_agent_end_signal(event, *, session_id)`. The
   helper must not live in `datastore.*` and must not import datastore modules.
2. The default bridge is selected only for `event.name == "session.agent_end"`.
   It maps to the existing daemon signal type `session_end` only. Did not add
   default queueing for `session.reset`, `session.compaction`,
   `session.timeout`, `session.new`, `session.agent_start`, or rolling.
3. Preserved the M22 explicit opt-in bridge exactly. If
   `payload.daemon_signal.enabled is True`, the M22 helper remains the selected
   path and keeps its existing four-event mapping, validation, passive envelope
   fields, and failHard behavior. The M24 default helper must not run a second
   queueing attempt after the explicit M22 bridge runs.
4. The default path requires a concrete non-empty `session_id` and an existing
   transcript path from `payload.transcript_path`. Did not read
   `payload.daemon_signal.transcript_path` for the default path; that field
   belongs to the explicit M22 bridge.
5. Missing, empty, or nonexistent `payload.transcript_path` for a plain
   `session.agent_end` event is not a failure in this slice. It preserves the
   M20 acknowledgement plus observation behavior and does not add daemon signal
   fields. This compatibility rule prevents older alpha lifecycle emitters that
   do not know about transcript paths from turning terminal events into failures.
   It is also the explicit opt-out contract for emitters that need ack-only
   terminal lifecycle behavior: omit `payload.transcript_path` unless the event
   should queue the default daemon `session_end` signal.
6. Uses `core.extraction_daemon.write_signal()` through an in-function import
   inside the default helper. Did not write signal files by hand. Did not import or
   call daemon process lifecycle helpers such as start, wake, stop, or restart.
7. Preserved idempotency by relying on existing `write_signal()` dedupe rules for
   compatible same-session/same-type signals. If an adapter hook already wrote a
   `session_end` signal for the same session, the default bridge must collapse
   to the same pending signal file instead of creating a duplicate.
8. Records compact signal metadata only: bridge provenance, lifecycle event id,
   lifecycle event name, and optional adapter/source fields already present in
   the lifecycle payload. Did not put transcript text, extracted facts, recall
   rows, or source-window rows in signal metadata.
9. Successful default queueing may add passive fields to the acknowledgement
   result: `daemon_signal_queued: true`, `daemon_signal_type: "session_end"`,
   `signal_name: <write_signal result basename>`, and
   `daemon_signal_default: true`. It must not change `status` or `event`.
10. Under fail-soft, a selected default queueing failure from `write_signal()` may
    preserve lifecycle acknowledgement semantics, but must log loudly and return
    `daemon_signal_queued: false`, `daemon_signal_default: true`, and
    `daemon_signal_error: <operator-readable string>`. It must not claim a
    signal was queued.
11. Under failHard, a selected default queueing failure from `write_signal()` must
    raise through `process_events()` with the original exception chained. Do not
    catch it and return acknowledgement success.
12. Preserved M21 daemon observation behavior. When the daemon later processes the
    default-written signal, it should follow the same observation path as
    adapter-written and explicit-M22 bridge signals.
13. M24 is infrastructure for event-bus lifecycle emitters that already know the
    active transcript path or will be migrated to provide it. Current adapter
    hook paths may continue to write `session_end` signals directly through
    `write_signal()`; this slice does not require migrating those hooks to
    event-bus lifecycle emission.

## Non-Targets

- no default queueing for reset, compaction, timeout, session.new,
  session.agent_start, or rolling
- no daemon start/wake/restart behavior
- no new lifecycle event names or daemon signal types
- no changes to adapter hook signal-writing behavior
- no changes to daemon signal priority, polling, locking, cursor, rolling buffer,
  timeout classifier, reset backup, or transcript ownership behavior
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

- `failHard=true`: if a default `session.agent_end` queueing attempt is selected
  and `write_signal()` fails, the failure must raise through the existing active
  event failHard path with the original exception chained. Do not return an
  acknowledged success for a failed selected default signal request.
- `failHard=false`: selected default queueing failures may preserve lifecycle
  acknowledgement semantics, but must log loudly and must include passive
  metadata with `daemon_signal_queued=false` and an operator-readable error.
- Missing `session_id` or missing/nonexistent `payload.transcript_path` on plain
  lifecycle events is a compatibility no-op for the default path, not a selected
  queueing failure. Explicit M22 opt-in payloads keep their stricter validation.
- Do not fall back to writing signal files manually if `write_signal()` fails.
- Do not wrap SessionDB lifecycle observation persistence, default daemon signal
  writing, explicit M22 daemon signal writing, and unrelated lifecycle
  acknowledgement logic in a shared broad `try`/`except` that could turn
  selected failures into silent success under failHard.
- Boundary check: M24 runtime must not add datastore imports to
  `core.runtime.events`, must not add `core/runtime/events.py` to any datastore
  composition allowlist, and must route signal creation through
  `core.extraction_daemon.write_signal()` only.

## Required Tests Before W4

Add or preserve focused tests proving:

- A plain `session.agent_end` lifecycle event with concrete `session_id` and an
  existing `payload.transcript_path` writes exactly one existing `session_end`
  daemon signal through `core.extraction_daemon.write_signal()`.
- Successful default queueing adds only passive fields:
  `daemon_signal_queued=true`, `daemon_signal_type="session_end"`,
  `signal_name`, and `daemon_signal_default=true`, while preserving `status` and
  `event`.
- Plain `session.reset`, `session.compaction`, `session.timeout`, `session.new`,
  and `session.agent_start` lifecycle events do not use the default bridge, even
  when they carry `payload.transcript_path`.
- Plain `session.agent_end` events with missing session id, missing transcript
  path, empty transcript path, or nonexistent transcript path preserve the M20
  acknowledgement/observation behavior and do not gain daemon signal fields.
- Explicit M22 opt-in behavior still wins: `payload.daemon_signal.enabled=true`
  keeps the M22 helper, envelope fields, and stricter validation, and the M24
  default helper does not attempt a second queue.
- Cross-path dedupe is explicit: if an adapter hook writes a compatible
  `session_end` signal directly through `write_signal()` and a default
  `session.agent_end` bridge targets the same session, `read_pending_signals()`
  must show exactly one pending signal file.
- Monkeypatched `write_signal()` failures cover fail-soft logging/envelope
  behavior and failHard exception chaining for the selected default path.
- `_handle_session_lifecycle()` continues to persist SessionDB lifecycle
  observations for concrete sessions and continues to acknowledge missing-session
  lifecycle events without persistence.
- M21 daemon observation tests still pass; a signal written by this default
  bridge is observed by the daemon when processed.
- Source assertions or boundary checks prove the default helper does not import
  `datastore.*`, does not manually create signal files, and does not call daemon
  start/wake/restart helpers.
- Existing daemon signal tests for reset backup handling, timeout
  classification, rolling flushes, stale-sweep recovery, session-log ingest
  request routing, and signal prioritization still pass.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window metadata tests still pass; default terminal lifecycle signal
  files must not affect source-window output policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow default terminal bridge smoke:

- Emit or process a plain `session.agent_end` event with concrete `session_id`
  and a real `payload.transcript_path`. It should acknowledge, record the M20
  lifecycle observation, and write one compatible `session_end` daemon signal
  file.
- Processing that signal through the daemon should extract/project exactly as
  the pre-M24 daemon `session_end` signal path did and record the M21 daemon
  lifecycle observation.
- Replaying the same event or pairing it with an adapter-written compatible
  `session_end` signal should not duplicate pending signal files.
- Plain reset/compaction/timeout lifecycle events without explicit M22 opt-in
  should not write signal files.
- A plain `session.agent_end` event without transcript path should keep the
  M20 acknowledgement/observation behavior and should not gain daemon signal
  fields.
- M19 source-window recall for dated session evidence should still render the
  same `source_date: <date>` context header.

## Deferred Decisions

- default timeout lifecycle-to-daemon signal bridge closed in M25 at
  `32ba63569`; default compaction lifecycle-to-daemon signal bridge closed in
  M26 at `2f35f279`; default reset lifecycle-to-daemon signal bridge closed in
  M27 at `acd05eaab`
- event-bus lifecycle signal wake/start parity closed in M28 at
  `5152a928`; daemon restart/stop automation remains deferred
- facade compaction lifecycle emitter closed in M29 at `a4a4d4238`;
  reset facade lifecycle emitter closed in M30 at `9f43c696`;
  timeout facade lifecycle emitter closed in M31 at `815b938`;
  agent-end facade lifecycle emitter is tracked as M32 in
  `projects/quaid/operations/datastore-events-m32-agent-end-facade-lifecycle-emitter-plan.md`;
  OpenClaw hook migration remains deferred
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- broad compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration

## Implementation Record

Runtime default terminal bridge slice closed at `058737670`
(`refactor(datastore): default agent-end lifecycle signal`). The approved plan
and guard addendum are `024f28726` and `fcac4fb8e`.

Implemented behavior:

- Added `core.runtime.events._default_agent_end_transcript_path()` as a
  side-effect-free eligibility helper for the default terminal path. It selects
  only plain `session.agent_end` events with a non-empty `session_id` and an
  existing `payload.transcript_path`; it returns `None` for M22 explicit opt-in
  payloads, non-agent-end lifecycle events, missing session ids, missing paths,
  empty paths, and nonexistent paths.
- Added `core.runtime.events._maybe_queue_default_agent_end_signal()` as the
  writer helper. It imports `core.extraction_daemon.write_signal()` in-function,
  writes the existing daemon `session_end` signal type only, and does not import
  datastore modules, manually write signal files, or call daemon start/wake/stop/
  restart helpers.
- Preserved explicit M22 bridge precedence: when `payload.daemon_signal.enabled`
  is `True`, `_handle_session_lifecycle()` routes to the existing M22 helper and
  does not run the M24 default helper.
- Preserved the M24 opt-out/compatibility contract: plain `session.agent_end`
  events without a real `payload.transcript_path` keep the M20 acknowledgement
  plus lifecycle-observation behavior and do not gain daemon signal fields.
- Added only passive default-bridge envelope fields on successful default
  queueing: `daemon_signal_queued=True`, `daemon_signal_type="session_end"`,
  `signal_name=<write_signal result basename>`, and
  `daemon_signal_default=True`. The handler does not change `status` or `event`.
- Under fail-soft, selected default `write_signal()` failures log loudly and add
  `daemon_signal_queued=False`, `daemon_signal_default=True`, and
  `daemon_signal_error=<operator-readable string>`. Under failHard, selected
  default `write_signal()` failures raise through `process_events()` with the
  original exception chained.
- Preserved fail-soft independence between M20 lifecycle observation persistence
  and daemon signal writing: a SessionDB observation failure does not block an
  otherwise-selected default agent-end signal.
- Preserved existing daemon semantics by delegating idempotency and cross-path
  dedupe to `write_signal()`. Adapter-written and default-agent-end same-session
  `session_end` signals collapse to one pending signal file under the existing
  same-session/same-type compatible dedupe rules.
- Preserved M21 daemon observation behavior by writing a standard `session_end`
  signal; daemon processing and observation recording remain on the pre-existing
  path. No daemon polling, priority, locking, cursor, rolling, timeout
  classifier, reset backup, or transcript ownership behavior changed.
- Preserved MemoryDB `session_chunks` recall/write ownership, SessionDB
  `capabilities.recall=[]`, M19 source-window metadata/output policy, M16
  request ownership, M17/M18 active ingest behavior, M20 lifecycle observation
  semantics, M22 explicit opt-in bridge behavior, CLI/default routing, broad
  compatibility aliases, and adapter hook direct `write_signal()` paths.

Test coverage added or preserved:

- Default success path writes exactly one `session_end` signal for plain
  `session.agent_end` with concrete `session_id` and existing
  `payload.transcript_path`, and asserts the passive envelope fields plus compact
  signal metadata.
- Negative default-selection coverage proves plain `session.reset`,
  `session.compaction`, `session.timeout`, `session.new`, and
  `session.agent_start` do not default-queue daemon signals even with
  `payload.transcript_path`.
- No-op compatibility coverage proves missing session id, missing transcript
  path, empty transcript path, and nonexistent transcript path preserve the
  acknowledgement/observation shape and add no daemon signal fields.
- Explicit M22 precedence coverage proves `payload.daemon_signal.enabled=True`
  uses the M22 explicit bridge, uses `payload.daemon_signal.transcript_path`, and
  does not set `daemon_signal_default`.
- Cross-path dedupe coverage proves an adapter-written `session_end` signal and
  the M24 default bridge for the same session result in one pending signal file.
- Monkeypatched `write_signal()` failure coverage proves fail-soft logging and
  passive failure metadata plus failHard exception chaining.
- Source-boundary assertions cover both M22 and M24 helpers: in-function
  `write_signal()` imports are present, while `datastore.*`, manual signal-file
  helpers, and daemon process lifecycle calls are absent.
- Existing event, extraction-daemon signal, source-window, session-memory bridge,
  docs consistency, boundary, and unit-wrapper lanes remained green.

Validation chain:

- W4 R201 PASS on `058737670`: default plain `session.agent_end` queued one
  `session_end` signal; no-path and nonexistent-path plain events no-op;
  explicit M22 opt-in wins; non-agent-end lifecycle events remain excluded; no
  daemon wake/start/restart or recall/source-window policy change observed.
- W3 runtime/recall APPROVED with no findings: default selection, M22
  precedence, no-path compatibility no-op, non-agent-end exclusions,
  `write_signal()`-only signal creation, and recall/source-window boundaries were
  verified.
- W6 runtime APPROVED with one LOW informational note: a dedicated M24
  write-then-daemon-process round-trip test could make the explicit Step 12
  coverage direct, but existing daemon `session_end` tests and M22 parity make
  the invariant functionally covered.
- W8 STATIC PASS/runtime HOLD for `058737670`: focused M24 selector, full
  `test_events.py`, extraction-daemon selector, source-window selector,
  `test_session_memory_bridge.py`, py_compile, ruff, diff/docs, boundary, and
  unit wrapper 140/140 all passed.
