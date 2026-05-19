# Datastore Events M31 Timeout Facade Lifecycle Emitter Plan

Status: draft plan; no runtime implementation yet
Owner: W1 facade/runtime, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m30-reset-facade-lifecycle-emitter-plan.md`

## Precondition

Do not implement runtime code for M31 until:

1. M30 reset facade lifecycle emitter is closed through W4/W3/W6/W8.
2. W3 reviews the selected timeout emitter slice because facade-originated
   timeout lifecycle events can write daemon `timeout` signals, wake the daemon
   through the existing M25/M28 path, write context-refresh timeout markers, and
   change when MemoryDB `session_chunks` evidence becomes recall-visible.
3. W6 reviews the facade-to-runtime timeout boundary because timeout has
   daemon-side marker and cursor behavior that must remain owned by the existing
   daemon processor, not by the facade.
4. W8 confirms static coverage includes facade tests, runtime-pair checks,
   runtime lifecycle timeout tests, extraction-daemon timeout/marker tests,
   source-window/session bridge checks, and boundary checks.
5. W4 is ready to live-check that a facade-originated timeout lifecycle event
   emits one existing `session.timeout` event only when given an explicit live
   transcript path, writes one compatible daemon `timeout` signal through the
   existing M25 path, and wakes through the M28 `ensure_alive()` path without
   moving timeout marker, cursor, or daemon ownership into the facade.

This document is a plan only. Do not implement from it until W3/W6/W8 approve
and W4 is ready to validate the runtime slice. It does not approve agent-end
emitter wiring, OpenClaw hook migration, adapter timeout-signal removal, daemon
restart/stop behavior, timeout marker writes in the facade, cursor ownership in
facade code, new lifecycle event names, new daemon signal types, request/default
routing changes, SessionDB recall selectors, source-window selector ownership,
broad compatibility-alias retirement, CLI behavior changes, `.ego` integration,
public push, or release actions.

## Goal

M29 and M30 implemented the first two facade lifecycle emitters:
`CompactionSignal` emits `session.compaction` with an explicit existing live
transcript path, while `ResetSignal` emits `session.reset` only with an explicit
existing reset-preserved transcript path. Timeout follows the compaction-style
field contract, not the reset-specific contract: M25 default timeout queueing is
selected only for plain `session.timeout` events that carry a concrete
`session_id` and an existing `payload.transcript_path`.

M31 selects the next narrow facade-emitter slice: implement facade
`processLifecycleEvent()` for `TimeoutSignal` only when the caller explicitly
supplies a concrete `sessionId` and an existing live transcript path, exposed at
the facade boundary as `context.transcriptPath`, `context.transcript_path`,
`context.sessionFile`, or `context.session_file`. It emits the existing
`session.timeout` runtime event through the existing `emitEvent()` /
`execEvents()` immediate path with payload `transcript_path`. The existing M25
runtime handler remains the only owner of writing the daemon `timeout` signal,
and the existing M28 wake helper remains the only owner of waking the daemon
after that signal is queued.

M31 is not timeout manager migration. It does not move idle-timeout detection,
context-refresh timeout marker writing, timeout cursor logic, transcript
classification, adapter direct signal-writing, or daemon timeout finalization
into the facade. It only adds the facade timeout emitter contract for future
callers that already hold the correct active transcript path.

## Current Boundary

Post-M30 path:

1. `createQuaidFacade().emitEvent()` delegates to the runtime events CLI through
   `deps.execEvents("emit", ...)`, normalizes payload/source/dispatch arguments,
   parses JSON output, and raises on malformed output.
2. `createQuaidFacade().processLifecycleEvent()` supports `CompactionSignal` and
   `ResetSignal` only. `CompactionSignal` requires a concrete `sessionId` and
   existing live `transcriptPath`, emits `session.compaction` with dispatch
   `immediate`, and returns fail-soft no-op metadata or raises under failHard for
   invalid inputs. `ResetSignal` requires explicit `resetTranscriptPath` /
   `reset_transcript_path`, emits `session.reset`, and intentionally rejects live
   transcript fields as reset evidence.
3. `TimeoutSignal` inputs are currently unsupported by `processLifecycleEvent()`.
   Under M30 they return passive no-op metadata under fail-soft or raise under
   failHard, and they do not emit runtime events.
4. `_handle_session_lifecycle()` in `core.runtime.events` already handles
   `session.timeout` events. M25 selects only plain `session.timeout` with
   concrete `session_id` and existing `payload.transcript_path`, then writes the
   existing daemon `timeout` signal through `core.extraction_daemon.write_signal()`.
5. M28 wakes the daemon through `core.extraction_daemon.ensure_alive()` only after
   the selected event-bus lifecycle signal write succeeds.
6. Daemon timeout processing owns timeout classification, cursor handling,
   duplicate avoidance, extraction/finalization, and context-refresh timeout
   marker writing. M31 must not move that ownership into the facade.
7. Adapter hook or timeout-manager paths may continue to write compatible daemon
   `timeout` signals directly through `write_signal()` when they own that host
   integration. M31 must not migrate or remove those paths.
8. M30 reset remains stricter than timeout and compaction. Facade reset emission
   requires a real reset-preserved transcript path; live transcript paths must
   remain no-op for reset.
9. MemoryDB remains the owner of `session_chunks` recall/write projection and
   final source-window output policy. SessionDB `capabilities.recall` remains
   `[]`.

## Selected First Slice: Facade Timeout Lifecycle Event Emitter

Implement one runtime slice only:

1. Extend the existing `processLifecycleEvent()` facade method to support
   `signal.label === "TimeoutSignal"` in addition to the already-closed M29
   `CompactionSignal` and M30 `ResetSignal` paths. Keep the implementation as
   explicit parallel branches; do not generalize compaction, reset, and timeout
   into a broad lifecycle event passthrough.
2. Support only `CompactionSignal`, `ResetSignal`, and `TimeoutSignal` in this
   slice. Agent-end, unknown, or malformed signals must not emit runtime events;
   they return passive no-op metadata under fail-soft and raise under failHard.
3. Require an explicit concrete session id from caller context, for example
   `context.sessionId` or `context.session_id`. Do not infer session id from
   transcript contents, filenames, adapter-global state, timeout cursor state, or
   daemon timeout scanner state in this helper.
4. Require an explicit existing live transcript path from caller context,
   specifically `context.transcriptPath`, `context.transcript_path`,
   `context.sessionFile`, or `context.session_file`, and verify it exists before
   emitting. This mirrors M25's runtime `payload.transcript_path` eligibility.
5. Do not use `context.resetTranscriptPath` or `context.reset_transcript_path` for
   timeout. Reset-preserved transcript paths belong to the M30 ResetSignal branch
   only. A `TimeoutSignal` with only reset-specific transcript context must
   return passive no-op metadata under fail-soft and raise under failHard.
6. Emit exactly one existing runtime event name for timeout: `session.timeout`.
   Do not add new event names or aliases.
7. Build a compact timeout payload only: `session_id`, `transcript_path`,
   optional `reason`, optional `adapter`/`source`, and lifecycle provenance such
   as `lifecycle_signal_label`, `lifecycle_signal_source`,
   `lifecycle_signal_signature`, and `lifecycle_message_index` when present.
   Optional timeout provenance such as `timeout_source` may be copied from an
   explicit scalar context field. Do not include transcript text, facts, recall
   rows, timeout marker contents, cursor state, source-window rows, or
   context-refresh contents.
8. Dispatch through the existing `emitRuntimeEvent("session.timeout", payload,
   "immediate")` helper so runtime event handling, M25 signal writing, M28 wake
   behavior, and daemon timeout finalization remain owned by the existing runtime
   path.
9. Preserve M29 compaction emitter behavior exactly. The existing
   `CompactionSignal` path keeps its live `transcriptPath` requirement,
   `session.compaction` event name, payload fields, fail-soft/failHard behavior,
   and `emitRuntimeEvent(..., "immediate")` dispatch.
10. Preserve M30 reset emitter behavior exactly. The existing `ResetSignal` path
    keeps its `resetTranscriptPath` / `reset_transcript_path` requirement,
    `session.reset` event name, live-transcript rejection guard, payload fields,
    fail-soft/failHard behavior, and `emitRuntimeEvent(..., "immediate")`
    dispatch.
11. Preserve `emitEvent()` behavior exactly. Do not add a second events CLI path,
    direct Python process invocation, direct `write_signal()` call, direct
    timeout marker write, or direct `ensure_alive()` call in the facade helper.
12. Preserve M22/M24/M25/M26/M27/M28 runtime behavior exactly. M31 must not
    change runtime event eligibility, signal shapes, dedupe rules, wake metadata,
    timeout marker behavior, cursor handling, reset backup/cursor handling,
    compaction context refresh, or failHard behavior in `core.runtime.events` or
    `core.extraction_daemon`.
13. Do not wire OpenClaw hooks, timeout manager paths, idle scanners,
    session-index reset paths, compaction hook paths, reset hook paths, or
    agent-end behavior to `processLifecycleEvent()` in this slice. Current
    adapter direct signal writers may continue unchanged.
14. Preserve lifecycle duplicate-suppression helpers. The existing
    `detectLifecycleSignal()` / `shouldProcessLifecycleSignal()` /
    `markLifecycleSignalFromHook()` contract currently models reset/compaction
    hook signals; M31 does not require expanding those helper types to timeout
    unless a concrete timeout hook caller is added in a future approved slice.
15. Preserve generated runtime-pair discipline. Edit `core/facade.ts`, then run
    `npm run build:runtime` so the paired generated runtime file is derived, and
    validate with `npm run check:runtime-pairs`.

## Non-Targets

- no agent-end emitter behavior
- no timeout emission from reset-specific `context.resetTranscriptPath` /
  `context.reset_transcript_path`
- no timeout marker writes in `core/facade.ts`
- no timeout cursor reads/writes, timeout transcript classification, daemon
  timeout scanner logic, or timeout finalization behavior in `core/facade.ts`
- no changes to M29 compaction facade emitter behavior
- no changes to M30 reset facade emitter behavior or live-transcript rejection
  guard
- no changes to M22 explicit signal mapping or validation
- no changes to M24/M25/M26/M27 default signal eligibility or signal shape
- no changes to M28 wake selection or wake metadata
- no direct `write_signal()` call from `core/facade.ts`
- no direct `ensure_alive()`, `start_daemon()`, `stop_daemon()`, restart helper,
  `subprocess`, pidfile, or manual daemon process management from `core/facade.ts`
- no datastore imports in `core/facade.ts` for this helper
- no migration or alteration of OpenClaw hook paths, timeout manager paths,
  adapter direct timeout-signal writing, or adapter-side daemon wake
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

- `failHard=true`: selected facade timeout emitter failures must raise loudly
  through the existing facade/dependency path. Unsupported signal labels,
  malformed signal objects, missing session id, missing/empty/nonexistent live
  transcript path, and timeout inputs that provide only reset-specific transcript
  fields are selected facade failures under failHard. Do not catch and convert a
  failed `emitRuntimeEvent()` / `execEvents()` call into acknowledgement success.
- `failHard=false`: unsupported or invalid facade lifecycle inputs may return a
  passive no-op result, but must not claim a runtime event was emitted, must not
  call `emitRuntimeEvent()` / `execEvents()`, and must log or surface enough
  metadata for operator diagnosis.
- Runtime `write_signal()`, daemon timeout marker, and daemon wake failures
  remain governed by existing M25/M28/daemon rules after the event is emitted.
  M31 must not catch those runtime failures in facade code or relabel them as
  facade success.
- Do not fall back to reset-specific transcript fields if the live timeout
  transcript path is missing or invalid.
- Do not fall back to timeout cursor discovery, timeout marker writing, adapter
  direct signal writing, manual signal-file writes, direct daemon wake, or direct
  extraction when facade event emission fails.
- Do not wrap lifecycle detection, duplicate suppression, event emission, daemon
  signal writing, daemon wake, timeout marker writing, cursor handling, and
  adapter hook acknowledgement in one shared broad `try`/`catch` that could hide
  selected failures under failHard.

## Required Tests Before W4

Add or preserve focused tests proving:

- `processLifecycleEvent()` for a valid `TimeoutSignal` with concrete `sessionId`
  and existing live `transcriptPath` / `transcript_path` / `sessionFile` /
  `session_file` calls `execEvents("emit", args)` exactly once with `--name
  session.timeout`, `--dispatch immediate`, and a compact payload containing
  `session_id` and `transcript_path`.
- The emitted timeout payload preserves compact lifecycle provenance fields from
  the signal (`label`, `source`, `signature`, optional `messageIndex`) and
  optional caller context fields without transcript text, timeout marker
  contents, cursor state, facts, recall rows, or source-window rows.
- A `TimeoutSignal` with only `resetTranscriptPath` / `reset_transcript_path`,
  even when that file exists, does not call `execEvents()` under fail-soft and
  raises under failHard.
- Missing session id, missing live transcript path, empty live transcript path,
  and nonexistent live transcript path do not call `execEvents()`; they return
  passive no-op metadata under fail-soft and raise under failHard.
- Agent-end, unknown, or malformed signals do not emit runtime events and follow
  the same fail-soft no-op / failHard raise contract.
- The M29 `CompactionSignal` happy path and no-op behavior remain unchanged.
- The M30 `ResetSignal` happy path, reset-specific path requirement, and
  live-transcript rejection behavior remain unchanged.
- `emitEvent()` malformed-output and dependency failure behavior remains
  unchanged; selected facade timeout emission must not swallow those errors.
- Existing lifecycle detection and duplicate-suppression tests still pass; M31
  must not require widening those helpers for timeout unless a concrete timeout
  hook caller is approved.
- Runtime lifecycle tests for M22/M24/M25/M26/M27/M28 still pass, especially M25
  timeout default signal writing, M25 no-path no-op, M25 explicit precedence,
  and M28 wake metadata.
- Existing daemon timeout tests still pass, including timeout classification,
  context-refresh timeout marker writing, cursor handling, missing transcript
  retry/finalization, and signal dedupe behavior.
- Source assertions or grep checks prove `core/facade.ts` does not call
  `write_signal`, `ensure_alive`, `start_daemon`, `stop_daemon`, restart helpers,
  `subprocess`, pidfile helpers, datastore imports, timeout marker writers,
  timeout cursor helpers, or adapter hook migration code for this slice.
- Runtime-pair checks pass after deriving paired files from `core/facade.ts`.
- Active/request session ingest parity and M19 source-window metadata tests still
  pass; facade timeout emitter wiring must not affect recall policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow facade timeout emitter smoke:

- Call `createQuaidFacade().processLifecycleEvent()` with a valid
  `TimeoutSignal`, concrete session id, and existing live transcript path. It
  should emit one `session.timeout` event through the events CLI with immediate
  dispatch.
- Confirm the runtime path writes one compatible daemon `timeout` signal and
  wakes through `core.extraction_daemon.ensure_alive()` via the existing M25/M28
  behavior.
- Confirm a `TimeoutSignal` with only reset-specific transcript path does not
  call the runtime events path and does not queue a timeout signal.
- Confirm missing/nonexistent live transcript path and malformed signal inputs do
  not call the runtime events path.
- Confirm `CompactionSignal` and `ResetSignal` still emit through the M29/M30
  behavior, including reset's live-transcript no-op guard, and agent-end labels
  remain unsupported in this slice.
- Confirm timeout marker writing, timeout cursor handling, OpenClaw hook paths,
  timeout manager paths, and adapter direct signal writing remain outside facade
  ownership.
- Confirm no daemon restart/stop, manual signal-file writing, direct daemon wake
  from facade, timeout marker write from facade, or recall/source-window policy
  change is observed.

## Deferred Decisions

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
