# Datastore Events M28 Lifecycle Signal Daemon Wake Plan

Status: runtime lifecycle daemon wake slice complete; restart/stop automation deferred
Owner: W1 runtime/daemon, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m27-default-reset-signal-plan.md`

## Precondition

Runtime code for M28 was gated on:

1. M27 default reset bridge was closed through W4/W3/W6/W8.
2. W3 reviewed the selected wake slice because lifecycle-triggered daemon wake
   can cause already-queued `reset`, `compaction`, `timeout`, and `session_end`
   signals to process sooner and therefore affect recall-visible MemoryDB
   `session_chunks` and source-window evidence timing.
3. W6 reviewed the runtime-event-to-daemon process boundary because M28 is the
   first event-bus lifecycle slice that may call daemon process lifecycle code
   after writing a signal.
4. W8 confirmed static coverage includes lifecycle event processing, daemon
   signal writing/deduplication, daemon ensure-alive behavior, active/request
   session ingest, source-window guards, and boundary checks.
5. W4 was ready to live-check that event-bus lifecycle signals still write
   exactly one compatible signal file and then wake/start the existing extraction
   daemon through the canonical daemon API, without adapter hook migration or
   daemon restart behavior.

This document records the completed narrow wake parity slice only. Runtime code
for M28 was gated on W3/W6/W8 approval and W4 live validation readiness. It
does not approve daemon restart/stop behavior, new signal types, new lifecycle event names, adapter hook
migration, event-bus emitter wiring from OpenClaw hooks, request/default routing
changes, SessionDB recall selectors, source-window selector ownership, broad
compatibility-alias retirement, CLI behavior changes, `.ego` integration, public
push, or release actions.

## Goal

M22 introduced the explicit event-bus lifecycle-to-daemon signal bridge. M24,
M25, M26, and M27 completed the four default signal-bearing lifecycle bridges:
`session.agent_end -> session_end`, `session.timeout -> timeout`,
`session.compaction -> compaction`, and `session.reset -> reset`. Those slices
write existing daemon signal files through `core.extraction_daemon.write_signal()`
and intentionally leave daemon polling, process lifecycle, and adapter hook paths
unchanged.

The remaining gap was wake parity. Existing hook and adapter signal writers act
as daemon wake points after signal creation so extraction resumes even if the
daemon was not already running. Before M28, the event-bus lifecycle bridge wrote
the same signal files but did not wake/start the daemon. M28 selected the narrow
wake parity slice: after a lifecycle event-bus bridge successfully queues a signal file, call
the canonical `core.extraction_daemon.ensure_alive()` path so the existing daemon
or supervisor owns process startup.

M28 was not broad daemon automation. It did not call restart/stop helpers, did
not manually spawn daemon processes, did not change signal shapes or daemon
polling/processing, and did not migrate OpenClaw hook writers to event-bus
emitters. It only added a wake/start attempt after successful event-bus
lifecycle signal queueing.

## Current Boundary

Pre-M28 path:

1. `_handle_session_lifecycle()` records SessionDB lifecycle observations for
   event-bus lifecycle events with `session_id`, preserving M20 acknowledgement
   semantics.
2. `_maybe_queue_lifecycle_daemon_signal()` writes existing daemon signals only
   when `payload.daemon_signal.enabled is True`. It maps M22 explicit opt-in
   events to existing daemon signal types.
3. `_maybe_queue_default_agent_end_signal()` writes the existing `session_end`
   signal for plain `session.agent_end` with concrete `session_id` and a real
   `payload.transcript_path`.
4. `_maybe_queue_default_timeout_signal()` writes the existing `timeout` signal
   for plain `session.timeout` with concrete `session_id` and a real
   `payload.transcript_path`.
5. `_maybe_queue_default_compaction_signal()` writes the existing `compaction`
   signal for plain `session.compaction` with concrete `session_id` and a real
   `payload.transcript_path`.
6. `_maybe_queue_default_reset_signal()` writes the existing `reset` signal for
   plain `session.reset` with concrete `session_id` and a real
   `payload.reset_transcript_path`. Live `payload.transcript_path` remains a
   no-op for the reset default path.
7. Before M28, these event-bus helpers did not call daemon start/wake/restart
   helpers after writing the signal file. They relied on the daemon's existing
   polling if it was already running.
8. Hook and adapter paths that write daemon signals already wake/start the daemon
   through their existing mechanisms. M28 did not migrate or alter those paths.
9. `core.extraction_daemon.ensure_alive()` is the canonical Python API for
   ensuring an instance daemon is running. It first uses the project-docs
   supervisor path when enabled, then falls back to locked `start_daemon()` only
   when needed. It returns the live daemon pid. It is already responsible for
   failHard behavior inside the daemon process lifecycle layer.
10. M21 records metadata-only SessionDB lifecycle observations when the daemon
    later processes `reset`, `compaction`, `timeout`, and `session_end` signals.
    Those observations persist directly to SessionDB through
    `core.plugins.sessiondb_contract.record_session_lifecycle_observation()` and
    do not republish through `process_events()`.
11. MemoryDB remains the owner of `session_chunks` recall/write projection and
    final source-window output policy. SessionDB `capabilities.recall` remains
    `[]`.

## Selected First Slice: Wake After Successful Event-Bus Lifecycle Signal

Implemented one runtime slice only:

1. Added a small private helper in `core.runtime.events`, near the lifecycle
   daemon-signal helpers, that wakes the daemon after a lifecycle bridge queues a
   signal.
2. The helper may run only after one of the existing event-bus lifecycle helpers
   returns a successful signal result with `daemon_signal_queued is True` and a
   `signal_name`. It must not run for ack-only/no-op lifecycle events, invalid
   transcript paths, missing session ids, or failed signal writes.
3. The selected event-bus paths were M22 explicit opt-in and M24/M25/M26/M27
   default bridges. This slice was wake parity for existing event-bus signal
   writers; it was not a new selection surface.
4. Preserved M22 explicit opt-in validation and signal mapping exactly. M28 did
   not weaken explicit `payload.daemon_signal.*` validation and did not run a
   default helper after M22 wins.
5. Preserved M24/M25/M26/M27 default eligibility exactly, including M27's
   reset-specific `payload.reset_transcript_path` discipline and live
   `payload.transcript_path` reset no-op guard.
6. Call `core.extraction_daemon.ensure_alive()` through an in-function import in
   the wake helper or immediately adjacent code. Do not import datastore modules
   and do not import adapter modules from `core.runtime.events`.
7. Do not call `start_daemon()`, `stop_daemon()`, restart helpers, `subprocess`,
   or any manual pidfile/process management from `core.runtime.events`.
   `ensure_alive()` remains the only process lifecycle API used by M28.
8. Do not manually create, rewrite, or move signal files. Signal creation remains
   exclusively owned by `core.extraction_daemon.write_signal()`.
9. On wake success, append passive metadata only. Suggested fields:
   `daemon_wake_attempted=true`, `daemon_wake_succeeded=true`, and
   `daemon_wake_pid=<pid>`. Do not change `status`, `event`,
   `daemon_signal_queued`, `daemon_signal_type`, or `signal_name`.
10. Wake failure must be represented separately from signal-write failure. If
    `write_signal()` succeeded but `ensure_alive()` failed under fail-soft, the
    result must continue to report `daemon_signal_queued=true` and include
    passive wake failure metadata such as `daemon_wake_attempted=true`,
    `daemon_wake_succeeded=false`, and `daemon_wake_error=<message>`.
11. Under failHard, a selected wake failure after a successful signal write must
    raise through `process_events()` with the original exception chained. Do not
    catch it and return acknowledgement success.
12. Did not change daemon polling cadence, priority, locking, cursor handling,
    rolling flush behavior, reset backup/cursor behavior, timeout marker
    finalization, compaction context refresh, or transcript ownership.
13. Did not add event-bus emitter wiring from OpenClaw hooks or any
    gateway/facade layer. M28 assumes event-bus lifecycle events already exist;
    current adapter hook paths may continue direct signal writing and their
    existing wake logic.
14. Preserved M21 daemon observation behavior. When the daemon processes a signal
    queued and woken by M28, it follows the same observation path as existing
    explicit/default lifecycle signals and adapter-written signals.

## Non-Targets

- no daemon restart/stop behavior
- no direct `start_daemon()` calls from `core.runtime.events`
- no direct `subprocess.Popen`, pidfile writes, pidfile deletion, or manual daemon
  process management from `core.runtime.events`
- no manual signal-file creation or fallback signal writing
- no new lifecycle event names or daemon signal types
- no changes to M22 explicit signal mapping or validation
- no changes to M24 default `session.agent_end` eligibility or signal shape
- no changes to M25 default `session.timeout` eligibility or signal shape
- no changes to M26 default `session.compaction` eligibility,
  `supports_compaction_control` discipline, or signal shape
- no changes to M27 default `session.reset` `payload.reset_transcript_path`
  discipline or live `payload.transcript_path` no-op guard
- no changes to adapter hook signal-writing or wake behavior
- no migration of OpenClaw `before_compaction`, `before_reset`, session-end reset,
  timeout, session-index reset, or agent-end behavior to event-bus emitters
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

- `failHard=true`: if a lifecycle event-bus bridge successfully writes a signal
  and the selected M28 daemon wake attempt fails, the failure must raise through
  the existing `process_events()` failHard path with the original exception
  chained. The signal file may remain queued; the wake failure is still a real
  product bug and must not be hidden.
- `failHard=false`: wake failures may preserve lifecycle acknowledgement and the
  already-queued signal result, but must log loudly and must include passive wake
  failure metadata. Do not claim `daemon_signal_queued=false` when the signal
  file was already written successfully.
- Signal-write failures remain governed by the existing M22/M24/M25/M26/M27
  failHard/fail-soft rules. M28 must not turn a selected signal-write failure
  into a wake failure or acknowledgement success.
- Ack-only/no-op lifecycle events are not selected wake attempts. Missing session
  id, missing transcript path, nonexistent transcript path, and M27 live
  `payload.transcript_path` reset no-op behavior must remain no-op for wake.
- Do not fall back to manual signal-file writes or manual daemon process spawning
  if `write_signal()` or `ensure_alive()` fails.
- Do not wrap SessionDB lifecycle observation persistence, signal writing,
  daemon wake, and unrelated lifecycle acknowledgement logic in a shared broad
  `try`/`except` that could turn selected failures into silent success under
  failHard.
- Boundary check: M28 runtime must not add datastore imports to
  `core.runtime.events`, must not add `core/runtime/events.py` to any datastore
  composition allowlist, and must route signal creation through
  `core.extraction_daemon.write_signal()` and wake through
  `core.extraction_daemon.ensure_alive()` only.

## Required Tests Before W4

Add or preserve focused tests proving:

- A successful explicit M22 lifecycle daemon signal write calls
  `core.extraction_daemon.ensure_alive()` exactly once after the signal write and
  appends passive wake success metadata without changing existing signal fields.
- A successful default M24 `session.agent_end` bridge calls `ensure_alive()` once
  after writing `session_end` and preserves M24 envelope fields.
- A successful default M25 `session.timeout` bridge calls `ensure_alive()` once
  after writing `timeout` and preserves M25 envelope fields.
- A successful default M26 `session.compaction` bridge calls `ensure_alive()` once
  after writing `compaction`, preserves strict `supports_compaction_control`
  behavior, and preserves M26 envelope fields.
- A successful default M27 `session.reset` bridge calls `ensure_alive()` once
  after writing `reset`, continues to read only `payload.reset_transcript_path`,
  and preserves the live `payload.transcript_path` no-op guard.
- Ack-only/no-op lifecycle events do not wake the daemon: missing session id,
  missing/empty/nonexistent transcript path, plain reset with only live
  `payload.transcript_path`, `session.new`, and `session.agent_start` must not
  call `ensure_alive()`.
- Wake failure after successful signal write is covered in fail-soft and
  failHard modes. Fail-soft keeps `daemon_signal_queued=true` and adds wake
  failure metadata; failHard raises with the original wake exception chained.
- Signal-write failure still follows the existing M22/M24/M25/M26/M27
  signal-write failure behavior and must not call `ensure_alive()`.
- SessionDB lifecycle observation persistence still runs independently of signal
  writing and daemon wake. A persistence failure under fail-soft must not block a
  selected signal write or wake when the event otherwise qualifies.
- Source assertions or boundary checks prove `core.runtime.events` imports
  `write_signal()` and `ensure_alive()` in-function, does not import
  `datastore.*`, does not call `start_daemon`, `stop_daemon`, restart helpers,
  `subprocess`, or manual signal-file helpers, and does not implement adapter
  hook migration logic.
- Existing daemon signal tests for reset, compaction, timeout, session_end,
  rolling flushes, stale-sweep recovery, signal prioritization, and daemon
  process lifecycle still pass.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window metadata tests still pass; daemon wake timing must not affect
  source-window output policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow lifecycle wake smoke:

- Emit or process one qualifying plain lifecycle event for each default bridge:
  `session.agent_end`, `session.timeout`, `session.compaction`, and
  `session.reset` with `payload.reset_transcript_path`. Each should acknowledge,
  record the SessionDB lifecycle observation, write one compatible daemon signal,
  and attempt daemon wake through `core.extraction_daemon.ensure_alive()`.
- Emit or process one explicit M22 opt-in lifecycle event. It should preserve M22
  signal mapping and validation, write one compatible daemon signal, and attempt
  daemon wake exactly once.
- Plain no-op lifecycle events should remain no-op for wake: missing transcript
  path, nonexistent transcript path, reset with only live `payload.transcript_path`,
  `session.new`, and `session.agent_start` should not call daemon wake and should
  not gain wake metadata.
- A monkeypatched or controlled wake failure should preserve the already-written
  signal file under fail-soft and surface a failHard exception under failHard.
- No daemon restart/stop, manual signal-file writing, adapter hook migration, or
  recall/source-window policy change should be observed.
- M19 source-window recall for dated session evidence should still render the
  same `source_date: <date>` context header.

## Deferred Decisions

- daemon restart/stop automation from lifecycle events
- facade compaction lifecycle emitter closed in M29 at `a4a4d4238`
- reset facade lifecycle emitter is tracked as M30 in
  `projects/quaid/operations/datastore-events-m30-reset-facade-lifecycle-emitter-plan.md`
- timeout and agent-end lifecycle emitters
- whether OpenClaw hooks should later be wired to facade lifecycle emitters or
  another facade/gateway layer
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- broad compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration


## Implementation Record

Runtime closed at `5152a92884697c64e326d4fdbb4c1c38aaebf61c`
(`refactor(datastore): wake lifecycle daemon after signal`) after the approved
plan commit `8ab3ef42d199e5a55f52fb9c8ef39a7211d277a5`.

Implemented behavior:

- Added `_wake_daemon_after_lifecycle_signal()` as the private wake helper for
  lifecycle bridge results. It requires `daemon_signal_queued is True` and a
  non-empty `signal_name` before attempting any daemon wake.
- Uses an in-function import of `core.extraction_daemon.ensure_alive()` and does
  not import datastore modules or adapter modules from `core.runtime.events`.
- Preserves all M22/M24/M25/M26/M27 signal helpers and eligibility rules exactly.
  M22 explicit opt-in remains canonical; M24 agent-end, M25 timeout, M26
  compaction, and M27 reset defaults keep their existing signal types and
  transcript-path requirements, including M27's `payload.reset_transcript_path`
  discipline and live `payload.transcript_path` no-op guard.
- Runs wake only after successful signal queueing. Ack-only/no-op lifecycle
  events, missing session ids, missing/empty/nonexistent transcript paths, plain
  reset events with only live `payload.transcript_path`, and selected
  `write_signal()` failures do not call `ensure_alive()` and do not gain wake
  metadata.
- Adds passive wake success metadata only: `daemon_wake_attempted=True`,
  `daemon_wake_succeeded=True`, and `daemon_wake_pid=<pid>`. It does not change
  `status`, `event`, `daemon_signal_queued`, `daemon_signal_type`, or
  `signal_name`.
- Keeps signal-write failure and wake failure separate. `_handle_session_lifecycle()`
  updates the result with the successful signal fields before opening the wake
  `try`/`except`, so a later wake failure cannot relabel the signal write as
  failed.
- Preserves fail-soft/failHard behavior for wake failures. Under fail-soft, a
  selected wake failure logs loudly, preserves `daemon_signal_queued=True`, and
  adds `daemon_wake_attempted=True`, `daemon_wake_succeeded=False`, and
  `daemon_wake_error`. Under failHard, the original wake exception raises through
  `process_events()` with the cause chained.
- Treats invalid `ensure_alive()` return values as wake failures. Non-integer,
  zero, or negative pids become wake errors rather than wake success; W4 live
  validation covered the invalid-pid fail-soft path, and W6 recorded an optional
  LOW note that a dedicated repo test could pin that defensive branch.
- Does not call `start_daemon()`, `stop_daemon()`, restart helpers, `subprocess`,
  pidfile management, manual signal-file helpers, or manual daemon spawning from
  `core.runtime.events`. `ensure_alive()` remains the only daemon process API
  used by M28.
- Does not change daemon polling cadence, signal priority, locking, cursor
  handling, rolling flush behavior, reset backup/cursor behavior, timeout marker
  finalization, compaction context refresh, transcript ownership, adapter hook
  signal-writing, or event-bus emitter wiring.
- Preserves M21 daemon observation behavior by waking only after standard daemon
  signals are already queued. Daemon processing and SessionDB lifecycle
  observation paths remain unchanged.
- Preserves MemoryDB `session_chunks` recall/write ownership, SessionDB
  `capabilities.recall=[]`, M19 source-window metadata/output policy, active and
  request ingest parity, CLI/default routing, broad compatibility aliases, and
  `.ego` deferral.

Test coverage added or preserved:

- Existing explicit M22 and default M24/M25/M26/M27 happy-path lifecycle tests now
  prove one successful signal write produces one `ensure_alive()` call and
  passive wake success metadata.
- No-op and excluded-event lifecycle tests use a failing wake monkeypatch to prove
  missing/invalid inputs, unselected event names, and M27 live-transcript reset
  no-op behavior do not call `ensure_alive()`.
- Selected `write_signal()` failure tests prove signal-write failure handling is
  unchanged and does not call daemon wake.
- `test_session_lifecycle_daemon_wake_failure_respects_failhard` covers wake
  failure after successful signal write under fail-soft and failHard. Fail-soft
  preserves `daemon_signal_queued=True`; failHard raises with the original wake
  exception chained.
- Persistence-failure tests prove SessionDB lifecycle observation failure under
  fail-soft does not block selected signal writing or wake when the event
  otherwise qualifies.
- Cross-path dedupe tests for the default bridges preserve one pending signal and
  now also prove successful wake metadata on the event-bus path.
- `test_session_lifecycle_daemon_signal_helper_preserves_boundaries` now covers
  five in-function `write_signal()` imports plus one in-function `ensure_alive()`
  import, and asserts no datastore imports, manual signal helpers, direct daemon
  process-control helpers, restart/stop logic, `subprocess`, or adapter migration
  logic in the helper sources.
- Existing event, extraction-daemon reset/compaction/timeout/session-end,
  source-window/session-memory bridge, docs consistency, boundary, and unit
  wrapper lanes remain green.

Validation chain:

- W4 R201 live/source-proof PASS on `5152a928`: M22 explicit plus M24 agent-end,
  M25 timeout, M26 compaction, and M27 reset paths each wrote exactly one signal
  and called `ensure_alive()` once; no-op lifecycle events did not wake; wake
  fail-soft preserved the queued signal; failHard raised; invalid pid returned
  from `ensure_alive()` surfaced as wake failure while preserving the signal.
- W3 runtime/recall APPROVED with no findings on `5152a928`: wake happens only
  after `daemon_signal_queued=true` with `signal_name`; signal eligibility,
  daemon processing, M27 reset discipline, SessionDB observation behavior,
  MemoryDB `session_chunks`, SessionDB `recall=[]`, M19 source-window policy, and
  active/request ingest ownership stayed unchanged.
- W6 runtime APPROVED on `5152a928` with one LOW informational note for optional
  invalid-pid repo-test specificity. W6 verified dual-axis signal-write versus
  wake-failure handling, failHard behavior, unchanged M22/M24/M25/M26/M27 signal
  helpers, boundary cleanliness, and B033 wake-parity rather than sync-tightening.
- W8 STATIC PASS/runtime HOLD CLOSED on `5152a928`: lifecycle selector, full
  `tests/test_events.py`, full `tests/test_extraction_daemon.py`,
  source-window/session bridge selector, py_compile, ruff, diff/show checks, docs
  consistency, boundary, and unit wrapper 140/140 passed.
