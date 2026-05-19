# Datastore Events M27 Default Reset Signal Plan

Status: runtime default reset bridge slice complete; daemon automation deferred
Owner: W1 runtime/daemon, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m26-default-compaction-signal-plan.md`

## Precondition

Runtime code for M27 was gated on:

1. M26 default compaction bridge is closed through W4/W3/W6/W8.
2. W3 reviews the selected reset slice because a default lifecycle-triggered
   daemon `reset` signal can extract transcripts, project MemoryDB
   `session_chunks`, and affect recall-visible source-window evidence.
3. W6 reviews the runtime-event-to-daemon boundary because reset has
   backup/cursor retargeting and session-teardown semantics that are stricter
   than M24/M25/M26.
4. W8 confirms static coverage includes lifecycle event processing, daemon
   signal writing/deduplication, reset backup/cursor handling, active/request
   session ingest, source-window guards, and boundary checks.
5. W4 is ready to live-check that only a reset lifecycle event with an explicit
   reset-preserved transcript path can enqueue exactly one existing daemon
   `reset` signal and that OpenClaw adapter reset hooks remain unchanged.

This document records the completed narrow default reset bridge slice only. It
does not approve daemon start/wake/restart behavior, OpenClaw hook migration,
live `payload.transcript_path` reset default queueing, new signal types, new
lifecycle event names, request/default routing changes, SessionDB recall
selectors, source-window selector ownership, broad compatibility-alias
retirement, CLI behavior changes, `.ego` integration, public push, or release
actions.

## Goal

M24, M25, and M26 proved three narrow default lifecycle-to-daemon signal
bridges: terminal `session.agent_end -> session_end`, idle
`session.timeout -> timeout`, and `session.compaction -> compaction`. Those
bridges write existing daemon signal files through
`core.extraction_daemon.write_signal()` and leave daemon polling/processing
unchanged.

M27 selected the reset bridge, but reset is deliberately narrower than the M24 to
M26 pattern. A live session transcript path after reset can point at a new or
reused post-reset conversation, while reset extraction must prefer the
pre-reset preserved transcript or reset backup. Therefore a plain
`session.reset` event is eligible for the M27 default bridge only when it carries
a concrete `session_id` and a real reset-specific preserved transcript path in
`payload.reset_transcript_path`.

M27 was not broad reset automation. It does not infer reset backups from a live
`payload.transcript_path`, does not migrate OpenClaw `before_reset` or
session-index reset hooks, does not wake or start the daemon, and does not
change daemon reset backup/cursor retargeting. It only writes the existing
`reset` signal file through `core.extraction_daemon.write_signal()` when the
event-bus emitter explicitly provides the reset-preserved transcript path.

## Current Boundary

Pre-M27 path:

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
6. M26 added default queueing only for plain `session.compaction` events with a
   concrete `session_id` and an existing `payload.transcript_path`.
7. Plain `session.reset` events without `daemon_signal.enabled=true` remained
   acknowledgement plus SessionDB observation only, even when they carry a live
   `payload.transcript_path`.
8. OpenClaw adapter-level reset paths already write compatible daemon `reset`
   signals directly. The `before_reset` hook snapshots the transcript before
   teardown, may retarget extraction to the transcript session id, may set
   `bypass_recent_reset_dedup`, and then calls its adapter signal writer. The
   session-index reset paths also handle key transitions, reused filenames,
   orphan backup waits, and recent-reset duplicate suppression. M27 did not
   migrate or alter those adapter paths.
9. The daemon reset processor already owns reset backup discovery, cursor
   retargeting, duplicate extraction avoidance, rolling buffer cleanup, and
   plain-session rebasing after reset backup. M27 did not move that ownership
   into `core.runtime.events`.
10. M21 records metadata-only SessionDB lifecycle observations when the daemon
    later processes `reset`, `compaction`, `timeout`, and `session_end` signals.
    Those observations persist directly to SessionDB through
    `core.plugins.sessiondb_contract.record_session_lifecycle_observation()` and
    do not republish through `process_events()`.
11. MemoryDB remains the owner of `session_chunks` recall/write projection and
    final source-window output policy. SessionDB `capabilities.recall` remains
    `[]`.

## Selected First Slice: Default Reset Signal With Reset-Specific Path Only

Implemented one runtime slice only:

1. Added a private helper in `core.runtime.events`, near the M22/M24/M25/M26
   lifecycle daemon-signal helpers, that determines whether a plain lifecycle
   event is eligible for default reset queueing. A suggested name is
   `_maybe_queue_default_reset_signal(event, *, session_id)`. Kept the helper
   parallel to the existing default helpers; do not generalize M24/M25/M26.
2. The default bridge is selected only for `event.name == "session.reset"`. It
   maps to the existing daemon signal type `reset` only. Did not add default
   queueing for `session.new`, `session.agent_start`, rolling, or any new event
   name.
3. Preserved the M22 explicit opt-in bridge exactly. If
   `payload.daemon_signal.enabled is True`, the M22 helper remains the selected
   path and keeps its existing four-event mapping, validation,
   `payload.daemon_signal.transcript_path` handling, passive envelope fields,
   and failHard behavior. The M27 default helper must not run a second queueing
   attempt after the explicit M22 bridge runs.
4. Preserved the M24 default terminal agent-end bridge exactly. Plain
   `session.agent_end` events keep the M24 helper, envelope fields, and
   failHard/fail-soft behavior.
5. Preserved the M25 default timeout bridge exactly. Plain `session.timeout`
   events keep the M25 helper, envelope fields, and failHard/fail-soft behavior.
6. Preserved the M26 default compaction bridge exactly. Plain
   `session.compaction` events keep the M26 helper, envelope fields,
   `supports_compaction_control` discipline, and failHard/fail-soft behavior.
7. The default reset path requires a concrete non-empty `session_id` and an
   existing reset-specific transcript path from `payload.reset_transcript_path`.
   This path must represent the pre-reset preserved transcript or reset backup
   selected by the lifecycle emitter. Do not read `payload.transcript_path` for
   the M27 default path, because that field can point at a live post-reset file.
   Do not read `payload.daemon_signal.transcript_path`; that field belongs to
   the explicit M22 bridge.
8. Missing, empty, or nonexistent `payload.reset_transcript_path` for a plain
   `session.reset` event is not a failure in this slice. It preserves the M20
   acknowledgement plus observation behavior and does not add daemon signal
   fields. This is the explicit opt-out contract for emitters that need ack-only
   reset lifecycle behavior: omit `payload.reset_transcript_path` unless the
   event should queue the default daemon `reset` signal.
9. A plain `session.reset` event with only `payload.transcript_path` must remain
   ack plus observation only. This guard is load-bearing: it prevents default
   reset queueing from extracting a reused live transcript that no longer
   represents the pre-reset conversation.
10. Uses `core.extraction_daemon.write_signal()` through an in-function import
    inside the default helper. Did not write signal files by hand. Did not import
    or call daemon process lifecycle helpers such as start, wake, stop, or
    restart.
11. Preserves idempotency by relying on existing `write_signal()` dedupe rules
    for compatible same-session/same-type signals. If an adapter hook already
    wrote a compatible `reset` signal for the same session, the default bridge
    must collapse to the same pending signal file instead of creating a
    duplicate. Did not implement OpenClaw's recent-reset marker suppression in
    `core.runtime.events`.
12. Records compact signal metadata only: bridge provenance, lifecycle event id,
    lifecycle event name, optional adapter/source/reason fields already present
    in the lifecycle payload, and optional reset path provenance such as
    `reset_transcript_source` when provided as a scalar payload field. Do not put
    transcript text, extracted facts, recall rows, reset backup contents, cursor
    state, or source-window rows in signal metadata.
13. Successful default queueing may add passive fields to the acknowledgement
    result: `daemon_signal_queued: true`, `daemon_signal_type: "reset"`,
    `signal_name: <write_signal result basename>`, and
    `daemon_signal_default: true`. It must not change `status` or `event`.
14. Under fail-soft, a selected default reset queueing failure from
    `write_signal()` may preserve lifecycle acknowledgement semantics, but must
    log loudly and return `daemon_signal_queued: false`,
    `daemon_signal_default: true`, and
    `daemon_signal_error: <operator-readable string>`. It must not claim a
    signal was queued.
15. Under failHard, a selected default reset queueing failure from
    `write_signal()` must raise through `process_events()` with the original
    exception chained. Do not catch it and return acknowledgement success.
16. Preserved M21 daemon observation behavior. When the daemon later processes the
    default-written reset signal, it should follow the same observation path as
    adapter-written and explicit-M22 reset signals.
17. Preserved daemon reset backup/cursor semantics. M27 must not change daemon
    reset backup discovery, stale rolling-buffer cleanup, cursor rebasing, reset
    rename classification, or duplicate extraction safeguards. Existing daemon
    reset tests must remain the source of truth for those invariants.
18. M27 is infrastructure for event-bus reset emitters that already know the
    reset-preserved transcript path or will be migrated to provide it. Current
    OpenClaw reset hooks and session-index paths may continue to write `reset`
    signals directly through their existing adapter writer; this slice does not
    require migrating those hooks to event-bus lifecycle emission.

## Non-Targets

- no default reset queueing from live `payload.transcript_path`
- no default queueing for session.new, session.agent_start, rolling, or new event
  names
- no changes to the M24 default `session.agent_end` bridge
- no changes to the M25 default `session.timeout` bridge
- no changes to the M26 default `session.compaction` bridge
- no daemon start/wake/restart behavior
- no new lifecycle event names or daemon signal types
- no changes to adapter hook signal-writing behavior
- no migration or alteration of OpenClaw `before_reset`, session-end reset,
  session-index reset, orphan-backup wait, recent-reset marker, or duplicate
  suppression behavior
- no reset-backup discovery, cursor retargeting, or transcript ownership logic in
  `core.runtime.events`
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

- `failHard=true`: if a default `session.reset` queueing attempt is selected and
  `write_signal()` fails, the failure must raise through the existing active
  event failHard path with the original exception chained. Do not return an
  acknowledged success for a failed selected default signal request.
- `failHard=false`: selected default reset queueing failures may preserve
  lifecycle acknowledgement semantics, but must log loudly and must include
  passive metadata with `daemon_signal_queued=false` and an operator-readable
  error.
- Missing `session_id` or missing/nonexistent `payload.reset_transcript_path` on
  plain reset lifecycle events is a compatibility no-op for the default path,
  not a selected queueing failure. A plain reset event with only
  `payload.transcript_path` is also a compatibility no-op. Explicit M22 opt-in
  payloads keep their stricter validation.
- Do not fall back to live `payload.transcript_path` if the reset-specific path
  is missing or invalid.
- Do not fall back to writing signal files manually if `write_signal()` fails.
- Do not wrap SessionDB lifecycle observation persistence, default reset signal
  writing, M26 default compaction signal writing, M25 default timeout signal
  writing, M24 default agent-end signal writing, explicit M22 daemon signal
  writing, and unrelated lifecycle acknowledgement logic in a shared broad
  `try`/`except` that could turn selected failures into silent success under
  failHard.
- Boundary check: M27 runtime must not add datastore imports to
  `core.runtime.events`, must not add `core/runtime/events.py` to any datastore
  composition allowlist, and must route signal creation through
  `core.extraction_daemon.write_signal()` only.

## Required Tests Before W4

Add or preserve focused tests proving:

- A plain `session.reset` lifecycle event with concrete `session_id` and an
  existing `payload.reset_transcript_path` writes exactly one existing `reset`
  daemon signal through `core.extraction_daemon.write_signal()`.
- Successful default queueing adds only passive fields:
  `daemon_signal_queued=true`, `daemon_signal_type="reset"`, `signal_name`, and
  `daemon_signal_default=true`, while preserving `status` and `event`.
- A plain `session.reset` event with only `payload.transcript_path`, even when
  that file exists, preserves M20 acknowledgement/observation behavior and does
  not write a daemon signal.
- Plain `session.new` and `session.agent_start` lifecycle events do not use the
  reset default bridge, even when they carry `payload.reset_transcript_path`.
  M24 `session.agent_end` keeps its existing default `session_end` behavior, M25
  `session.timeout` keeps its existing default `timeout` behavior, and M26
  `session.compaction` keeps its existing default `compaction` behavior.
- Plain `session.reset` events with missing session id, missing reset transcript
  path, empty reset transcript path, or nonexistent reset transcript path
  preserve the M20 acknowledgement/observation behavior and do not gain daemon
  signal fields.
- Explicit M22 opt-in behavior still wins: `payload.daemon_signal.enabled=true`
  keeps the M22 helper, envelope fields, stricter validation, and
  `payload.daemon_signal.transcript_path` handling, and the M27 default helper
  does not attempt a second queue.
- Cross-path dedupe is explicit: if an adapter hook writes a compatible `reset`
  signal directly through `write_signal()` and a default `session.reset` bridge
  targets the same session, `read_pending_signals()` must show exactly one
  pending signal file.
- Monkeypatched `write_signal()` failures cover fail-soft logging/envelope
  behavior and failHard exception chaining for the selected default reset path.
- `_handle_session_lifecycle()` continues to persist SessionDB lifecycle
  observations for concrete sessions and continues to acknowledge missing-session
  lifecycle events without persistence.
- M21 daemon observation tests still pass; a reset signal written by this default
  bridge is observed by the daemon when processed.
- Existing daemon reset tests still pass, including reset backup re-extraction,
  stale rolling-buffer cursor cleanup, cursor rebasing from backup to plain
  session file, and missing transcript retry/finalization behavior.
- Source assertions or boundary checks prove the default helper does not import
  `datastore.*`, does not manually create signal files, does not call daemon
  start/wake/restart helpers, and does not implement OpenClaw recent-reset marker
  suppression.
- Existing daemon signal tests for compaction signal processing, compaction
  context refresh, timeout classification/markers, rolling flushes, stale-sweep
  recovery, session-log ingest request routing, and signal prioritization still
  pass.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window metadata tests still pass; default reset lifecycle signal
  files must not affect source-window output policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow default reset bridge smoke:

- Emit or process a plain `session.reset` event with concrete `session_id` and a
  real `payload.reset_transcript_path` pointing at a preserved pre-reset
  transcript or reset backup. It should acknowledge, record the M20 lifecycle
  observation, and write one compatible `reset` daemon signal file.
- Emit or process a plain `session.reset` event with only a real
  `payload.transcript_path`. It should keep acknowledgement plus observation
  behavior and should not write a signal file.
- Processing a default-written reset signal through the daemon should preserve
  the pre-M27 reset behavior for backup discovery, cursor handling, rolling
  buffer cleanup, and M21 daemon lifecycle observation.
- Replaying the same event or pairing it with an adapter-written compatible
  `reset` signal should not duplicate pending signal files.
- Plain `session.agent_end` should retain the M24 default `session_end` signal
  behavior, plain `session.timeout` should retain the M25 default `timeout`
  behavior, plain `session.compaction` should retain the M26 default
  `compaction` behavior, and explicit M22 opt-in should still win over all
  default paths.
- Plain `session.reset` without reset transcript path should keep the M20
  acknowledgement/observation behavior and should not gain daemon signal fields.
- OpenClaw `before_reset` and session-index reset hook paths should remain
  direct adapter-hook signal writers; this smoke should not require migrating
  those hooks to event-bus lifecycle emission.
- M19 source-window recall for dated session evidence should still render the
  same `source_date: <date>` context header.

## Deferred Decisions

- event-bus lifecycle signal wake/start parity closed in M28 at
  `5152a928`; daemon restart/stop automation remains deferred
- facade compaction lifecycle emitter closed in M29 at `a4a4d4238`;
  reset facade lifecycle emitter closed in M30 at `9f43c696`;
  timeout, agent-end emitter wiring and OpenClaw hook migration remain deferred
- whether event-bus reset emitters should later be wired from OpenClaw hooks or
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

Runtime closed at `acd05eaab06f8434e6f45a795daf60cac1a92d90`
(`refactor(datastore): default reset lifecycle signal`) after the approved plan
commit `0dba22fd978d080d587f654d0db175983bbf1282`.

Implemented behavior:

- Added `_default_reset_transcript_path()` as the side-effect-free eligibility
  helper for plain `session.reset` events. It selects only events without
  `payload.daemon_signal.enabled=true`, with non-empty `session_id`, and with an
  existing `payload.reset_transcript_path`.
- Added `_maybe_queue_default_reset_signal()` as the writer helper. It uses an
  in-function import of `core.extraction_daemon.write_signal()`, writes only the
  existing daemon `reset` signal type, and does not import datastore modules,
  manually create signal files, call daemon start/wake/stop/restart helpers, or
  implement OpenClaw recent-reset marker suppression.
- Preserved the load-bearing reset path discipline: M27 never reads live
  `payload.transcript_path` for default reset queueing. A plain `session.reset`
  event with only live `payload.transcript_path` remains acknowledgement plus
  observation only.
- Preserved M22 explicit precedence: `payload.daemon_signal.enabled=true` keeps
  the M22 helper, including `payload.daemon_signal.transcript_path` handling,
  and the M27 default helper does not run a second queueing attempt.
- Preserved M24, M25, and M26 defaults exactly: plain `session.agent_end` still
  queues `session_end`, plain `session.timeout` still queues `timeout`, and
  plain `session.compaction` still queues `compaction`.
- Preserved the no-op compatibility contract: plain `session.reset` events with
  missing session id, missing reset transcript path, empty reset transcript path,
  or nonexistent reset transcript path remain acknowledgement plus observation
  only and do not gain daemon signal fields.
- Records compact metadata only:
  `bridge="event_lifecycle_default_reset_bridge"`, lifecycle event id, lifecycle
  event name, and optional payload `adapter`/`source`/`reason`/
  `reset_transcript_source`. It does not put transcript text, facts, recall rows,
  reset backup contents, cursor state, or source-window rows in signal metadata.
- Adds passive success envelope fields only: `daemon_signal_queued=True`,
  `daemon_signal_type="reset"`, `signal_name`, and
  `daemon_signal_default=True`; `status` and `event` remain unchanged.
- Preserves fail-soft/failHard behavior: selected `write_signal()` failures log
  loudly and return `daemon_signal_queued=False`, `daemon_signal_default=True`,
  and `daemon_signal_error` under fail-soft; failHard raises through
  `process_events()` with the original exception chained.
- Preserves SessionDB observation independence. SessionDB lifecycle observation
  failure under fail-soft does not block selected default reset signal writing;
  daemon signal failure does not claim observation failure.
- Preserves cross-path dedupe through existing `write_signal()` same-session and
  same-type rules. Adapter-hook reset and M27 default reset collapse to one
  pending signal file; adapter-specific metadata such as
  `bypass_recent_reset_dedup` survives through the existing metadata merge.
- Preserves M21 daemon observation behavior by writing a standard daemon `reset`
  signal. Daemon reset backup discovery, cursor retargeting, duplicate
  extraction avoidance, rolling buffer cleanup, reset rename classification,
  plain-session rebasing, and missing transcript retry/finalization remain
  unchanged.
- Preserves OpenClaw reset adapter-hook behavior. M27 does not migrate
  `before_reset`, session-end reset, session-index reset, orphan-backup wait,
  recent-reset marker, duplicate-suppression, or direct adapter signal-writing
  behavior.
- Preserves MemoryDB `session_chunks` recall/write ownership, SessionDB
  `capabilities.recall=[]`, M19 source-window metadata/output policy, M16
  request ownership, M17/M18 active ingest, M20 lifecycle observation, M22
  explicit opt-in bridge, M24/M25/M26 defaults, CLI/default routing, and broad
  compatibility aliases.

Test coverage added or preserved:

- `test_session_lifecycle_default_reset_signal_writes_existing_signal` proves the
  happy path writes one `reset` signal from `payload.reset_transcript_path` and
  records the passive envelope plus compact bridge metadata.
- `test_session_lifecycle_default_reset_signal_ignores_live_transcript_path`
  pins the load-bearing guard: a plain `session.reset` with only live
  `payload.transcript_path` does not call `write_signal()` and does not gain
  daemon signal fields.
- `test_session_lifecycle_default_signal_excludes_unselected_events` keeps
  `session.new` and `session.agent_start` excluded from default queueing even
  when they carry `payload.reset_transcript_path`.
- `test_session_lifecycle_default_reset_signal_noop_without_valid_inputs` covers
  missing session id, missing reset transcript path, empty reset transcript path,
  and nonexistent reset transcript path no-op behavior.
- `test_session_lifecycle_explicit_daemon_signal_wins_over_default_reset` proves
  M22 explicit opt-in uses `payload.daemon_signal.transcript_path` and suppresses
  the M27 default path.
- `test_session_lifecycle_default_reset_write_failure_respects_failhard` covers
  fail-soft logging/envelope and failHard exception chaining for selected
  `write_signal()` failures.
- `test_session_lifecycle_default_reset_signal_dedupes_with_adapter_signal`
  proves adapter-hook plus M27 default reset collapse to one pending signal and
  preserve adapter reset metadata through the existing metadata merge.
- `test_session_lifecycle_daemon_signal_helper_preserves_boundaries` now covers
  five in-function `write_signal` imports (M22, M27, M24, M25, M26) and asserts
  no datastore/manual-signal/daemon-process-control/recent-reset marker logic in
  the helper sources.
- `test_session_lifecycle_persistence_failure_does_not_block_failsoft_default_reset_signal`
  proves SessionDB observation failure does not block selected M27 signal writing
  under fail-soft.
- Existing event, extraction-daemon reset/lifecycle/write-signal,
  source-window/session-memory bridge, docs consistency, boundary, and unit
  wrapper lanes remain green.

Validation chain:

- W4 R201 live/source-proof PASS on `acd05eaab`: default plain `session.reset`
  with `payload.reset_transcript_path` queued one `reset` signal; plain reset
  with only live `payload.transcript_path` no-oped; missing/empty/nonexistent
  reset path no-oped; M22 explicit precedence, M24/M25/M26 defaults, and
  new/agent_start exclusions were preserved; no OpenClaw hook migration, reset
  backup/cursor move, daemon wake/start/restart, datastore import, manual signal
  file, or recall/source-window change was observed.
- W3 runtime/recall APPROVED with no findings on `acd05eaab`: recall/source-window
  ownership unchanged, SessionDB recall remains `[]`, M19 source-window policy
  unchanged, and active/request ingest ownership unchanged.
- W6 runtime APPROVED with no findings on `acd05eaab`: the
  `payload.reset_transcript_path` discipline, live-transcript rejection guard,
  OpenClaw recent-reset marker isolation, daemon reset processor isolation, and
  B-code checks were clean.
- W8 STATIC PASS/runtime HOLD on `acd05eaab`: focused default-reset selector, full
  `tests/test_events.py`, extraction-daemon reset/lifecycle/write-signal
  selector, source-window/session bridge selector, py_compile, ruff, diff/show
  checks, docs consistency, boundary, and unit wrapper 140/140 passed.
