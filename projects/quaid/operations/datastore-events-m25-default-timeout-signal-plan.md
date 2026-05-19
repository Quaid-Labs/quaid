# Datastore Events M25 Default Timeout Signal Plan

Status: runtime default timeout bridge slice complete; broader reset/compaction automation deferred
Owner: W1 runtime/daemon, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m24-default-agent-end-signal-plan.md`

## Precondition

Runtime code for M25 was gated on:

1. M24 default terminal `session.agent_end` bridge is closed through
   W4/W3/W6/W8.
2. W3 reviews the selected timeout slice because a default lifecycle-triggered
   daemon `timeout` signal can extract transcripts, project MemoryDB
   `session_chunks`, write context-refresh timeout markers, and affect
   recall-visible source-window evidence.
3. W6 reviews the runtime-event-to-daemon boundary because this slice expands
   default behavior for one more lifecycle event path.
4. W8 confirms static coverage includes lifecycle event processing, daemon
   signal writing/deduplication, timeout signal processing, active/request
   session ingest, source-window guards, and boundary checks.
5. W4 is ready to live-check that a plain timeout lifecycle event can enqueue
   exactly one existing daemon timeout signal and that duplicate adapter-hook
   timeout signals are still deduped.

This document records the completed narrow default timeout bridge slice only. It
does not approve reset/compaction default automation, daemon start/wake/restart
behavior, new signal types, new lifecycle event names, request/default routing
changes, SessionDB recall selectors, source-window selector ownership, broad
compatibility-alias retirement, CLI behavior changes, `.ego` integration, public
push, or release actions.

## Goal

M24 added the first default lifecycle-to-daemon signal bridge: plain terminal
`session.agent_end` events with concrete `session_id` and real
`payload.transcript_path` now queue the existing daemon `session_end` signal.
That proved the default-bridge boundary while keeping reset, compaction, and
timeout default automation deferred.

M25 selected the next narrow default bridge: plain `session.timeout` events may
queue the existing daemon `timeout` signal when the event already carries a
concrete `session_id` and a real `payload.transcript_path`. Timeout is selected
before reset/compaction because the daemon already treats timeout as its own
signal type with a specific post-processing marker, and this slice can preserve
all reset-backup and compaction-context-refresh behavior unchanged.

M25 was not broad lifecycle automation. It does not wake or start the daemon. It
only writes the existing `timeout` signal file through
`core.extraction_daemon.write_signal()` and lets the daemon's normal polling and
signal-processing path do the work.

## Current Boundary

Pre-M25 path:

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
5. Plain `session.timeout` events without `daemon_signal.enabled=true` remain
   acknowledgement plus SessionDB observation only, even when they carry a
   transcript path.
6. Adapter hooks and daemon scanners may already write compatible daemon signal
   files directly through `core.extraction_daemon.write_signal()`.
7. M21 records metadata-only SessionDB lifecycle observations when the daemon
   later processes `reset`, `compaction`, `timeout`, and `session_end` signals.
   Those observations persist directly to SessionDB through
   `core.plugins.sessiondb_contract.record_session_lifecycle_observation()` and
   do not republish through `process_events()`.
8. Daemon `timeout` signal processing may write the existing context-refresh
   timeout marker after the lifecycle observation and before signal finalization;
   M25 must not change that daemon-side ordering.
9. MemoryDB remains the owner of `session_chunks` recall/write projection and
   final source-window output policy. SessionDB `capabilities.recall` remains
   `[]`.

## Selected First Slice: Default Timeout Signal Only

Implemented one runtime slice only:

1. Added a private helper in `core.runtime.events`, near the M22/M24 lifecycle
   daemon-signal helpers, that determines whether a plain lifecycle event is
   eligible for default timeout queueing. A suggested name is
   `_maybe_queue_default_timeout_signal(event, *, session_id)`. The helper must
   not live in `datastore.*` and must not import datastore modules.
2. The default bridge is selected only for `event.name == "session.timeout"`.
   It maps to the existing daemon signal type `timeout` only. Do not add default
   queueing for `session.reset`, `session.compaction`, `session.new`,
   `session.agent_start`, rolling, or any new event name.
3. Preserved the M22 explicit opt-in bridge exactly. If
   `payload.daemon_signal.enabled is True`, the M22 helper remains the selected
   path and keeps its existing four-event mapping, validation, passive envelope
   fields, and failHard behavior. The M25 default helper must not run a second
   queueing attempt after the explicit M22 bridge runs.
4. Preserved the M24 default terminal agent-end bridge exactly. Plain
   `session.agent_end` events keep the M24 helper, envelope fields, and
   failHard/fail-soft behavior. M25 must not generalize the M24 helper in a way
   that changes terminal agent-end behavior.
5. The default timeout path requires a concrete non-empty `session_id` and an
   existing transcript path from `payload.transcript_path`. Do not read
   `payload.daemon_signal.transcript_path` for the default path; that field
   belongs to the explicit M22 bridge.
6. Missing, empty, or nonexistent `payload.transcript_path` for a plain
   `session.timeout` event is not a failure in this slice. It preserves the M20
   acknowledgement plus observation behavior and does not add daemon signal
   fields. This is the explicit opt-out contract for emitters that need
   ack-only timeout lifecycle behavior: omit `payload.transcript_path` unless
   the event should queue the default daemon `timeout` signal.
7. Uses `core.extraction_daemon.write_signal()` through an in-function import
   inside the default helper. Do not write signal files by hand. Do not import or
   call daemon process lifecycle helpers such as start, wake, stop, or restart.
8. Preserves idempotency by relying on existing `write_signal()` dedupe rules for
   compatible same-session/same-type signals. If an adapter hook already wrote a
   `timeout` signal for the same session, the default bridge must collapse to the
   same pending signal file instead of creating a duplicate.
9. Records compact signal metadata only: bridge provenance, lifecycle event id,
   lifecycle event name, and optional adapter/source fields already present in
   the lifecycle payload. Do not put transcript text, extracted facts, recall
   rows, context-refresh marker contents, or source-window rows in signal
   metadata.
10. Successful default queueing may add passive fields to the acknowledgement
    result: `daemon_signal_queued: true`, `daemon_signal_type: "timeout"`,
    `signal_name: <write_signal result basename>`, and
    `daemon_signal_default: true`. It must not change `status` or `event`.
11. Under fail-soft, a selected default timeout queueing failure from
    `write_signal()` may preserve lifecycle acknowledgement semantics, but must
    log loudly and return `daemon_signal_queued: false`,
    `daemon_signal_default: true`, and
    `daemon_signal_error: <operator-readable string>`. It must not claim a
    signal was queued.
12. Under failHard, a selected default timeout queueing failure from
    `write_signal()` must raise through `process_events()` with the original
    exception chained. Do not catch it and return acknowledgement success.
13. Preserved M21 daemon observation behavior. When the daemon later processes the
    default-written timeout signal, it should follow the same observation path as
    adapter-written and explicit-M22 timeout signals, including the existing
    context-refresh timeout-marker ordering in `_finalize_no_payload_signal()`.
14. M25 is infrastructure for event-bus timeout emitters that already know the
    active transcript path or will be migrated to provide it. Current adapter
    hook paths may continue to write `timeout` signals directly through
    `write_signal()`; this slice does not require migrating those hooks to
    event-bus lifecycle emission.

## Non-Targets

- no default queueing for reset, compaction, session.new, session.agent_start,
  rolling, or new event names
- no changes to the M24 default `session.agent_end` bridge
- no daemon start/wake/restart behavior
- no new lifecycle event names or daemon signal types
- no changes to adapter hook signal-writing behavior
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

- `failHard=true`: if a default `session.timeout` queueing attempt is selected
  and `write_signal()` fails, the failure must raise through the existing active
  event failHard path with the original exception chained. Do not return an
  acknowledged success for a failed selected default signal request.
- `failHard=false`: selected default timeout queueing failures may preserve
  lifecycle acknowledgement semantics, but must log loudly and must include
  passive metadata with `daemon_signal_queued=false` and an operator-readable
  error.
- Missing `session_id` or missing/nonexistent `payload.transcript_path` on plain
  lifecycle events is a compatibility no-op for the default path, not a selected
  queueing failure. Explicit M22 opt-in payloads keep their stricter validation.
- Do not fall back to writing signal files manually if `write_signal()` fails.
- Do not wrap SessionDB lifecycle observation persistence, default timeout signal
  writing, M24 default agent-end signal writing, explicit M22 daemon signal
  writing, and unrelated lifecycle acknowledgement logic in a shared broad
  `try`/`except` that could turn selected failures into silent success under
  failHard.
- Boundary check: M25 runtime must not add datastore imports to
  `core.runtime.events`, must not add `core/runtime/events.py` to any datastore
  composition allowlist, and must route signal creation through
  `core.extraction_daemon.write_signal()` only.

## Required Tests Before W4

Add or preserve focused tests proving:

- A plain `session.timeout` lifecycle event with concrete `session_id` and an
  existing `payload.transcript_path` writes exactly one existing `timeout`
  daemon signal through `core.extraction_daemon.write_signal()`.
- Successful default queueing adds only passive fields:
  `daemon_signal_queued=true`, `daemon_signal_type="timeout"`, `signal_name`,
  and `daemon_signal_default=true`, while preserving `status` and `event`.
- Plain `session.reset`, `session.compaction`, `session.new`, and
  `session.agent_start` lifecycle events do not use the timeout default bridge,
  even when they carry `payload.transcript_path`. M24 `session.agent_end` keeps
  its existing default `session_end` behavior and is not remapped to timeout.
- Plain `session.timeout` events with missing session id, missing transcript
  path, empty transcript path, or nonexistent transcript path preserve the M20
  acknowledgement/observation behavior and do not gain daemon signal fields.
- Explicit M22 opt-in behavior still wins: `payload.daemon_signal.enabled=true`
  keeps the M22 helper, envelope fields, stricter validation, and
  `payload.daemon_signal.transcript_path` handling, and the M25 default helper
  does not attempt a second queue.
- M24 explicit regression coverage still proves plain `session.agent_end` with a
  real transcript path queues `session_end`, not `timeout`.
- Cross-path dedupe is explicit: if an adapter hook writes a compatible `timeout`
  signal directly through `write_signal()` and a default `session.timeout` bridge
  targets the same session, `read_pending_signals()` must show exactly one
  pending signal file.
- Monkeypatched `write_signal()` failures cover fail-soft logging/envelope
  behavior and failHard exception chaining for the selected default timeout
  path.
- `_handle_session_lifecycle()` continues to persist SessionDB lifecycle
  observations for concrete sessions and continues to acknowledge missing-session
  lifecycle events without persistence.
- M21 daemon observation tests still pass; a timeout signal written by this
  default bridge is observed by the daemon when processed and preserves the
  existing timeout-marker finalization ordering.
- Source assertions or boundary checks prove the default helper does not import
  `datastore.*`, does not manually create signal files, and does not call daemon
  start/wake/restart helpers.
- Existing daemon signal tests for reset backup handling, compaction context
  refresh, timeout classification/markers, rolling flushes, stale-sweep recovery,
  session-log ingest request routing, and signal prioritization still pass.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window metadata tests still pass; default timeout lifecycle signal
  files must not affect source-window output policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow default timeout bridge smoke:

- Emit or process a plain `session.timeout` event with concrete `session_id` and
  a real `payload.transcript_path`. It should acknowledge, record the M20
  lifecycle observation, and write one compatible `timeout` daemon signal file.
- Processing that signal through the daemon should extract/project exactly as
  the pre-M25 daemon `timeout` signal path did, record the M21 daemon lifecycle
  observation, and preserve the existing context-refresh timeout-marker
  behavior.
- Replaying the same event or pairing it with an adapter-written compatible
  `timeout` signal should not duplicate pending signal files.
- Plain reset/compaction lifecycle events without explicit M22 opt-in should not
  write signal files.
- Plain `session.agent_end` should retain the M24 default `session_end` signal
  behavior, and explicit M22 opt-in should still win over all default paths.
- A plain `session.timeout` event without transcript path should keep the M20
  acknowledgement/observation behavior and should not gain daemon signal fields.
- M19 source-window recall for dated session evidence should still render the
  same `source_date: <date>` context header.

## Deferred Decisions

- default lifecycle-triggered reset/compaction transcript ingestion
- daemon start/wake/restart automation from lifecycle events
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- broad compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration


## Implementation Record

Runtime default timeout bridge slice closed at `32ba63569`
(`refactor(datastore): default timeout lifecycle signal`). The approved plan is
`df4b8f19d`.

Implemented behavior:

- Added `core.runtime.events._default_timeout_transcript_path()` as a
  side-effect-free eligibility helper for the default timeout path. It selects
  only plain `session.timeout` events with a non-empty `session_id` and an
  existing `payload.transcript_path`; it returns `None` for M22 explicit opt-in
  payloads, non-timeout lifecycle events, missing session ids, missing paths,
  empty paths, and nonexistent paths.
- Added `core.runtime.events._maybe_queue_default_timeout_signal()` as the
  writer helper. It imports `core.extraction_daemon.write_signal()` in-function,
  writes the existing daemon `timeout` signal type only, and does not import
  datastore modules, manually write signal files, or call daemon start/wake/stop/
  restart helpers.
- Preserved explicit M22 bridge precedence: when `payload.daemon_signal.enabled`
  is `True`, `_handle_session_lifecycle()` routes to the existing M22 helper and
  does not run the M25 default helper.
- Preserved the M24 default terminal agent-end bridge: plain
  `session.agent_end` events still queue `session_end`, not `timeout`, and the
  M24 helper behavior and envelope fields remain unchanged.
- Preserved the M25 opt-out/compatibility contract: plain `session.timeout`
  events without a real `payload.transcript_path` keep the M20 acknowledgement
  plus lifecycle-observation behavior and do not gain daemon signal fields.
- Added only passive default-bridge envelope fields on successful default
  timeout queueing: `daemon_signal_queued=True`,
  `daemon_signal_type="timeout"`, `signal_name=<write_signal result basename>`,
  and `daemon_signal_default=True`. The handler does not change `status` or
  `event`.
- Under fail-soft, selected default timeout `write_signal()` failures log loudly
  and add `daemon_signal_queued=False`, `daemon_signal_default=True`, and
  `daemon_signal_error=<operator-readable string>`. Under failHard, selected
  default timeout `write_signal()` failures raise through `process_events()` with
  the original exception chained.
- Preserved fail-soft independence between M20 lifecycle observation persistence
  and daemon signal writing: a SessionDB observation failure does not block an
  otherwise-selected default timeout signal.
- Preserved existing daemon semantics by delegating idempotency and cross-path
  dedupe to `write_signal()`. Adapter-written and default-timeout same-session
  `timeout` signals collapse to one pending signal file under the existing
  same-session/same-type compatible dedupe rules.
- Preserved M21 daemon observation and timeout marker behavior by writing a
  standard `timeout` signal; daemon processing, observation recording, and
  `_finalize_no_payload_signal()` timeout-marker ordering remain on the
  pre-existing path. No daemon polling, priority, locking, cursor, rolling,
  timeout classifier, reset backup, compaction context refresh, or transcript
  ownership behavior changed.
- Preserved MemoryDB `session_chunks` recall/write ownership, SessionDB
  `capabilities.recall=[]`, M19 source-window metadata/output policy, M16
  request ownership, M17/M18 active ingest behavior, M20 lifecycle observation
  semantics, M22 explicit opt-in bridge behavior, M24 default terminal bridge
  behavior, CLI/default routing, broad compatibility aliases, and adapter hook
  direct `write_signal()` paths.

Test coverage added or preserved:

- Default success path writes exactly one `timeout` signal for plain
  `session.timeout` with concrete `session_id` and existing
  `payload.transcript_path`, and asserts the passive envelope fields plus compact
  signal metadata.
- Negative default-selection coverage proves plain `session.reset`,
  `session.compaction`, `session.new`, and `session.agent_start` do not
  default-queue daemon signals even with `payload.transcript_path`.
- No-op compatibility coverage proves missing session id, missing transcript
  path, empty transcript path, and nonexistent transcript path preserve the
  acknowledgement/observation shape and add no daemon signal fields.
- Explicit M22 precedence coverage proves `payload.daemon_signal.enabled=True`
  uses the M22 explicit bridge, uses `payload.daemon_signal.transcript_path`, and
  does not set `daemon_signal_default`.
- M24 regression coverage proves plain `session.agent_end` still queues
  `session_end`, not `timeout`.
- Cross-path dedupe coverage proves an adapter-written `timeout` signal and the
  M25 default bridge for the same session result in one pending signal file.
- Monkeypatched `write_signal()` failure coverage proves fail-soft logging and
  passive failure metadata plus failHard exception chaining.
- Source-boundary assertions cover M22, M24, and M25 helpers: in-function
  `write_signal()` imports are present, while `datastore.*`, manual signal-file
  helpers, and daemon process lifecycle calls are absent.
- Existing event, extraction-daemon timeout/lifecycle/write-signal,
  source-window, session-memory bridge, docs consistency, boundary, and
  unit-wrapper lanes remained green.

Validation chain:

- W4 R201 PASS on `32ba63569`: default plain `session.timeout` queued one
  `timeout` signal; no-path and nonexistent-path plain events no-op; explicit
  M22 opt-in wins; M24 `session.agent_end` still queues `session_end`; other
  lifecycle events remain excluded; no daemon wake/start/restart or
  recall/source-window policy change observed; M21 daemon-side timeout-marker
  behavior remains on the existing path.
- W3 runtime/recall APPROVED with no findings: default selection, M22
  precedence, M24 preservation, no-path compatibility no-op, non-timeout
  exclusions, `write_signal()`-only signal creation, and recall/source-window
  boundaries were verified.
- W6 runtime APPROVED with one LOW informational note: a dedicated M25
  write-then-daemon-process round-trip test could make the explicit Step 13
  coverage direct, but existing daemon timeout marker-ordering tests and M24
  parity make the invariant functionally covered.
- W8 STATIC PASS/runtime HOLD for `32ba63569`: focused default-timeout selector,
  full `test_events.py`, extraction-daemon timeout/lifecycle selector,
  source-window selector, `test_session_memory_bridge.py`, py_compile, ruff,
  diff/show, docs consistency, boundary, and unit wrapper 140/140 all passed.
