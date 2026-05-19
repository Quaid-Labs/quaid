# Datastore Events M29 Compaction Facade Lifecycle Emitter Plan

Status: draft plan; no runtime implementation yet
Owner: W1 facade/runtime, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m28-lifecycle-signal-daemon-wake-plan.md`

## Precondition

Do not implement runtime code for M29 until:

1. M28 lifecycle daemon wake is closed through W4/W3/W6/W8.
2. W3 reviews the selected emitter slice because facade-originated lifecycle
   events can now write daemon `compaction` signals and wake the daemon through
   the existing M26/M28 path, changing when MemoryDB `session_chunks` evidence
   becomes recall-visible.
3. W6 reviews the facade-to-runtime boundary because M29 starts retiring the
   explicit `processLifecycleEvent()` facade stub and must not move adapter hook
   ownership into core.
4. W8 confirms static coverage includes facade tests, runtime lifecycle event
   tests, runtime-pair checks if TypeScript runtime files change, daemon signal
   tests, source-window/session bridge checks, and boundary checks.
5. W4 is ready to live-check that a facade-originated compaction lifecycle event
   emits one existing `session.compaction` event, writes one compatible daemon
   `compaction` signal through the existing M26 path, and wakes through the M28
   `ensure_alive()` path without migrating OpenClaw hooks.

This document is a plan only. Do not implement from it until W3/W6/W8 approve
and W4 is ready to validate the runtime slice. It does not approve reset emitter
wiring, `payload.reset_transcript_path` inference, agent-end emitter wiring,
timeout emitter wiring, OpenClaw hook migration, adapter direct-signal removal,
daemon restart/stop behavior, new lifecycle event names, new daemon signal
types, request/default routing changes, SessionDB recall selectors,
source-window selector ownership, broad compatibility-alias retirement, CLI
behavior changes, `.ego` integration, public push, or release actions.

## Goal

M24-M27 added default event-bus lifecycle-to-daemon signal bridges, and M28 added
wake parity after successful event-bus signal writes. Those slices made the
runtime event path capable of queueing and waking standard daemon work, but
adapter code still has no implemented facade method for emitting lifecycle
events through that path. `createQuaidFacade()` exposes `processLifecycleEvent()`
as a typed stub that currently throws `notImplemented("processLifecycleEvent")`.

M29 selects the narrowest emitter-contract slice: implement facade
`processLifecycleEvent()` for `CompactionSignal` only, and have it emit the
existing `session.compaction` event through `emitEvent()` / `execEvents()` when a
caller explicitly supplies a concrete `sessionId` and an existing live
`transcriptPath`. The existing M26 runtime handler remains the only owner of
writing the daemon `compaction` signal, and the existing M28 wake helper remains
the only owner of waking the daemon after that signal is queued.

M29 is not adapter migration. It does not change OpenClaw `before_compaction`,
transcript-update fallback, direct adapter signal-writing, duplicate suppression,
or compaction context-refresh arming. It only turns the facade stub into a small,
explicit compaction event emitter that future adapter/facade call sites can use
when they already have the correct transcript evidence.

## Current Boundary

Post-M28 path:

1. `createQuaidFacade().emitEvent()` delegates to the runtime events CLI through
   `deps.execEvents("emit", ...)`, normalizes payload/source/dispatch arguments,
   parses JSON output, and raises on malformed output.
2. `createQuaidFacade().processLifecycleEvent()` is still a stub that throws
   `notImplemented("processLifecycleEvent")`.
3. `detectLifecycleSignal()`, `shouldProcessLifecycleSignal()`, and
   `markLifecycleSignalFromHook()` already detect and dedupe `CompactionSignal`
   and `ResetSignal` candidates inside the facade, but they do not emit runtime
   lifecycle events.
4. `_handle_session_lifecycle()` in `core.runtime.events` already handles
   `session.compaction` events. M26 selects only plain `session.compaction` with
   concrete `session_id` and existing `payload.transcript_path`, then writes the
   existing daemon `compaction` signal through `core.extraction_daemon.write_signal()`.
5. M28 wakes the daemon through `core.extraction_daemon.ensure_alive()` only after
   the selected event-bus lifecycle signal write succeeds.
6. OpenClaw adapter hook paths currently own `before_compaction` behavior,
   transcript capture, duplicate suppression, context-refresh arming, direct
   signal writing, and adapter-side daemon wake. M29 must not migrate or alter
   those paths.
7. M27 reset remains stricter than compaction. Default reset queueing requires a
   real `payload.reset_transcript_path`; live `payload.transcript_path` alone
   must remain no-op for reset.
8. MemoryDB remains the owner of `session_chunks` recall/write projection and
   final source-window output policy. SessionDB `capabilities.recall` remains
   `[]`.

## Selected First Slice: Facade Compaction Lifecycle Event Emitter

Implement one runtime slice only:

1. Change the facade API type for `processLifecycleEvent()` from a `never` stub
   to an async method returning the parsed runtime event result object.
2. Support only `signal.label === "CompactionSignal"` in this slice. A reset,
   timeout, agent-end, unknown, or malformed signal must not emit a runtime event;
   it returns passive no-op metadata under fail-soft and raises under failHard.
3. Require an explicit concrete session id from the caller context, for example
   `context.sessionId` or an equivalent already-normalized field. Do not infer a
   session id from transcript contents or adapter-global state in this helper.
4. Require an explicit live transcript path from the caller context, for example
   `context.transcriptPath` or `context.sessionFile`, and verify it exists before
   emitting. Missing, empty, or nonexistent transcript paths return passive no-op
   metadata under fail-soft and raise under failHard via `deps.isFailHardEnabled()`.
5. Emit exactly one existing runtime event name: `session.compaction`. Do not add
   new event names or aliases.
6. Build a compact payload only: `session_id`, `transcript_path`, optional
   `reason`, optional `adapter`/`source`, and lifecycle provenance such as
   `lifecycle_signal_label`, `lifecycle_signal_source`, `lifecycle_signal_signature`,
   and `lifecycle_message_index` when present. Do not include transcript text,
   facts, recall rows, source-window rows, or context-refresh contents.
7. Dispatch through the existing `emitEvent("session.compaction", payload,
   "immediate")` facade path so runtime event handling, M26 signal writing, and
   M28 wake behavior remain owned by `core.runtime.events`.
8. Preserve `emitEvent()` behavior exactly. Do not add a second events CLI path,
   direct Python process invocation, direct `write_signal()` call, or direct
   `ensure_alive()` call in the facade helper.
9. Preserve M22/M24/M25/M26/M27/M28 runtime behavior exactly. M29 must not change
   runtime event eligibility, signal shapes, dedupe rules, wake metadata, or
   failHard behavior in `core.runtime.events`.
10. Do not implement reset emitter behavior in this slice. In particular, do not
    infer `payload.reset_transcript_path` from live `transcriptPath`, do not read
    reset backup files, and do not move OpenClaw reset hook/session-index reset
    ownership into the facade.
11. Do not wire OpenClaw hooks, transcript-update fallback, timeout manager, or
    agent-end behavior to `processLifecycleEvent()` in this slice. Current
    adapter direct signal writers may continue unchanged.
12. Preserve lifecycle duplicate-suppression helpers. If a future caller wants to
    suppress duplicate compaction emit attempts, it must use the existing
    `shouldProcessLifecycleSignal()` / `markLifecycleSignalFromHook()` contract;
    M29 does not add new dedupe storage.
13. Preserve generated runtime-pair discipline. Edit `core/facade.ts`, then run
    `npm run build:runtime` so the paired generated runtime file is derived, and
    validate with `npm run check:runtime-pairs`.

## Non-Targets

- no OpenClaw hook migration
- no adapter direct-signal removal
- no transcript-update fallback migration
- no reset emitter behavior
- no reset backup discovery or `payload.reset_transcript_path` inference
- no timeout emitter behavior
- no agent-end emitter behavior
- no new lifecycle event names or daemon signal types
- no changes to M22 explicit signal mapping or validation
- no changes to M24/M25/M26/M27 default signal eligibility or signal shape
- no changes to M28 wake selection or wake metadata
- no direct `write_signal()` call from `core/facade.ts`
- no direct `ensure_alive()`, `start_daemon()`, `stop_daemon()`, restart helper,
  `subprocess`, pidfile, or manual daemon process management from `core/facade.ts`
- no datastore imports in `core/facade.ts` for this helper
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

- `failHard=true`: selected facade lifecycle emitter failures must raise loudly
  through the existing facade/dependency path. Unsupported signal labels,
  malformed signal objects, missing session id, and missing/empty/nonexistent
  transcript path are selected facade failures under failHard. Do not catch and
  convert a failed `emitEvent()` / `execEvents()` call into acknowledgement
  success.
- `failHard=false`: unsupported or invalid facade lifecycle inputs may return a
  passive no-op result, but must not claim a runtime event was emitted, must not
  call `emitEvent()`, and must log or surface enough metadata for operator
  diagnosis.
- Runtime `write_signal()` and daemon wake failures remain governed by existing
  M26/M28 rules after the event is emitted. M29 must not catch those runtime
  failures in facade code or relabel them as facade success.
- Do not fall back to adapter direct signal writing, manual signal-file writes,
  direct daemon wake, reset backup discovery, or direct extraction when facade
  event emission fails.
- Do not wrap lifecycle detection, duplicate suppression, event emission, daemon
  signal writing, daemon wake, and adapter hook acknowledgement in one shared
  broad `try`/`catch` that could hide selected failures under failHard.

## Required Tests Before W4

Add or preserve focused tests proving:

- `processLifecycleEvent()` for a valid `CompactionSignal` with concrete
  `sessionId` and existing transcript path calls `emitEvent()` / `execEvents()`
  exactly once with `--name session.compaction`, `--dispatch immediate`, and a
  compact payload containing `session_id` and `transcript_path`.
- The emitted compaction payload preserves compact lifecycle provenance fields
  from the signal (`label`, `source`, `signature`, optional `messageIndex`) and
  optional caller context fields without transcript text or recall rows.
- Missing session id, missing transcript path, empty transcript path, and
  nonexistent transcript path do not call `execEvents()`; they return passive
  no-op metadata under fail-soft and raise under failHard.
- Reset signals remain unimplemented/not selected in M29. A `ResetSignal` must
  not emit `session.reset`, must not read live `transcriptPath` as
  `reset_transcript_path`, and must not call `execEvents()`; it returns no-op
  metadata under fail-soft and raises under failHard.
- Unknown or malformed signals do not emit runtime events and follow the same
  fail-soft no-op / failHard raise contract.
- `emitEvent()` malformed-output and dependency failure behavior remains
  unchanged; selected facade lifecycle emission must not swallow those errors.
- Existing lifecycle detection and duplicate-suppression tests still pass.
- Runtime lifecycle tests for M22/M24/M25/M26/M27/M28 still pass, especially
  M26 compaction signal writing and M28 wake metadata.
- Source assertions or grep checks prove `core/facade.ts` does not call
  `write_signal`, `ensure_alive`, `start_daemon`, `stop_daemon`, restart helpers,
  `subprocess`, pidfile helpers, or adapter hook migration code for this slice.
- Runtime-pair checks pass after deriving paired files from `core/facade.ts`.
- Active/request session ingest parity and M19 source-window metadata tests still
  pass; facade emitter wiring must not affect recall policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow facade compaction emitter smoke:

- Call `createQuaidFacade().processLifecycleEvent()` with a valid
  `CompactionSignal`, concrete session id, and existing transcript path. It
  should emit one `session.compaction` event through the events CLI with
  immediate dispatch.
- Confirm the runtime path writes one compatible daemon `compaction` signal and
  wakes through `core.extraction_daemon.ensure_alive()` via the existing M26/M28
  behavior.
- Confirm missing/nonexistent transcript path and malformed signal inputs do not
  call the runtime events path.
- Confirm a reset signal is not selected and does not queue a reset signal from a
  live transcript path.
- Confirm OpenClaw `before_compaction` direct signal-writing behavior remains
  unchanged and no adapter hook migration occurred.
- Confirm no daemon restart/stop, manual signal-file writing, direct daemon wake
  from facade, or recall/source-window policy change is observed.

## Deferred Decisions

- reset lifecycle facade emitter with explicit `payload.reset_transcript_path`
- timeout lifecycle facade emitter
- agent-end lifecycle facade emitter
- OpenClaw hook migration to facade lifecycle emitters
- adapter direct-signal retirement, if ever approved
- daemon restart/stop automation from lifecycle events
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- broad compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
