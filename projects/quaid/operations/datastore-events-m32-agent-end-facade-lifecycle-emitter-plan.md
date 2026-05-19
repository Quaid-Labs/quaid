# Datastore Events M32 Agent-End Facade Lifecycle Emitter Plan

Status: runtime agent-end facade emitter slice complete; OpenClaw hook migration deferred
Owner: W1 facade/runtime, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m31-timeout-facade-lifecycle-emitter-plan.md`

## Precondition

Runtime code for M32 was gated on:

1. M31 timeout facade lifecycle emitter was closed through W4/W3/W6/W8.
2. W3 reviewed the selected agent-end emitter slice because facade-originated
   terminal lifecycle events can write daemon `session_end` signals, wake the
   daemon through the existing M24/M28 path, project MemoryDB `session_chunks`,
   and change when terminal transcript evidence becomes recall-visible.
3. W6 reviewed the facade-to-runtime terminal lifecycle boundary because
   agent-end/session-end behavior overlaps with OpenClaw `agent_end`,
   `session_end`, transcript-update fallback, direct adapter signal writers, and
   daemon session finalization ownership.
4. W8 confirmed static coverage includes facade tests, runtime-pair checks,
   runtime lifecycle agent-end tests, extraction-daemon session-end signal tests,
   source-window/session bridge checks, and boundary checks.
5. W4 was ready to live-check that a facade-originated agent-end lifecycle event
   emits one existing `session.agent_end` event only when given an explicit live
   transcript path, writes one compatible daemon `session_end` signal through the
   existing M24 path, and wakes through the M28 `ensure_alive()` path without
   moving OpenClaw hook, transcript-update fallback, or daemon ownership into the
   facade.

This document records the completed narrow agent-end facade emitter slice only.
It did not approve OpenClaw hook migration, adapter direct `session_end` signal
removal, session start emitters, reset/compaction/timeout behavior changes,
daemon restart/stop behavior, transcript-update fallback migration, new
lifecycle event names, new daemon signal types, request/default routing changes,
SessionDB recall selectors, source-window selector ownership, broad
compatibility-alias retirement, CLI behavior changes, `.ego` integration, public
push, or release actions.

## Goal

M29, M30, and M31 implemented the first three facade lifecycle emitters:
`CompactionSignal` emits `session.compaction` with an explicit existing live
transcript path, `ResetSignal` emits `session.reset` only with an explicit
existing reset-preserved transcript path, and `TimeoutSignal` emits
`session.timeout` with an explicit existing live transcript path while rejecting
reset-preserved paths as timeout evidence.

M32 selected the final narrow facade-emitter slice for the four-event lifecycle
family: implemented facade `processLifecycleEvent()` for `AgentEndSignal` only
when the caller explicitly supplies a concrete `sessionId` and an existing live
transcript path, exposed at the facade boundary as `context.transcriptPath`,
`context.transcript_path`, `context.sessionFile`, or `context.session_file`. It
emits the existing `session.agent_end` runtime event through the existing
`emitEvent()` / `execEvents()` immediate path with payload `transcript_path`. The
existing M24 runtime handler remains the only owner of writing the daemon
`session_end` signal, and the existing M28 wake helper remains the only owner of
waking the daemon after that signal is queued.

M32 was not OpenClaw hook migration. It did not wire OpenClaw `agent_end`,
`session_end`, transcript-update fallback, session-index reset handling, or
before-agent hooks to `processLifecycleEvent()`. It did not remove current
adapter direct signal writers. It only added the facade terminal lifecycle emitter
contract for future callers that already hold the correct active transcript
path.

## Current Boundary

Pre-M32 path:

1. `createQuaidFacade().emitEvent()` delegates to the runtime events CLI through
   `deps.execEvents("emit", ...)`, normalizes payload/source/dispatch arguments,
   parses JSON output, and raises on malformed output.
2. `createQuaidFacade().processLifecycleEvent()` supports `CompactionSignal`,
   `ResetSignal`, and `TimeoutSignal` only. Compaction and timeout require a
   concrete `sessionId` and existing live `transcriptPath`; reset requires
   explicit `resetTranscriptPath` / `reset_transcript_path` and intentionally
   rejects live transcript fields as reset evidence.
3. Before M32, `AgentEndSignal` inputs were unsupported by `processLifecycleEvent()`.
   They returned passive no-op metadata under fail-soft or raised under failHard,
   and they did not emit runtime events.
4. `_handle_session_lifecycle()` in `core.runtime.events` already handles
   `session.agent_end` events. M24 selects only plain `session.agent_end` with
   concrete `session_id` and existing `payload.transcript_path`, then writes the
   existing daemon `session_end` signal through
   `core.extraction_daemon.write_signal()`.
5. M28 wakes the daemon through `core.extraction_daemon.ensure_alive()` only after
   the selected event-bus lifecycle signal write succeeds.
6. OpenClaw adapter paths currently own host-specific terminal behavior,
   including `session_end` hook handling, transcript-update fallback paths,
   direct `session_end` signal writing, duplicate suppression, and adapter-side
   daemon wake. M32 did not migrate or alter those paths.
7. Daemon session-end processing owns extraction/finalization from compatible
   `session_end` signal files and metadata-only daemon lifecycle observations.
   M32 did not move daemon signal processing, transcript ownership, cursor
   behavior, or finalization into the facade.
8. M30 reset remains stricter than agent-end, timeout, and compaction. Facade
   reset emission requires a real reset-preserved transcript path; live
   transcript paths must remain no-op for reset.
9. MemoryDB remains the owner of `session_chunks` recall/write projection and
   final source-window output policy. SessionDB `capabilities.recall` remains
   `[]`.

## Selected First Slice: Facade Agent-End Lifecycle Event Emitter

Implemented one runtime slice only:

1. Extended the existing `processLifecycleEvent()` facade method to support
   `signal.label === "AgentEndSignal"` in addition to the already-closed M29
   `CompactionSignal`, M30 `ResetSignal`, and M31 `TimeoutSignal` paths. Kept the
   implementation as explicit parallel branches; did not generalize compaction,
   reset, timeout, and agent-end into a broad lifecycle event passthrough.
2. Support only `CompactionSignal`, `ResetSignal`, `TimeoutSignal`, and
   `AgentEndSignal` in this slice. Session-start, unknown, or malformed signals
   must not emit runtime events; they return passive no-op metadata under
   fail-soft and raise under failHard.
3. Require an explicit concrete session id from caller context, for example
   `context.sessionId` or `context.session_id`. Do not infer session id from
   transcript contents, filenames, adapter-global state, OpenClaw session-index
   state, recent-reset markers, timeout cursor state, or daemon scanner state in
   this helper.
4. Require an explicit existing live transcript path from caller context,
   specifically `context.transcriptPath`, `context.transcript_path`,
   `context.sessionFile`, or `context.session_file`, and verify it exists before
   emitting. This mirrors M24's runtime `payload.transcript_path` eligibility.
5. Do not use `context.resetTranscriptPath` or `context.reset_transcript_path` for
   agent-end. Reset-preserved transcript paths belong to the M30 `ResetSignal`
   branch only. An `AgentEndSignal` with only reset-specific transcript context
   must return passive no-op metadata under fail-soft and raise under failHard.
6. Emit exactly one existing runtime event name for terminal agent-end:
   `session.agent_end`. Do not add `session.end`, `agent.end`, `session_end`, or
   other aliases as event names.
7. Build a compact terminal payload only: `session_id`, `transcript_path`,
   optional `reason`, optional `adapter`/`source`, and lifecycle provenance such
   as `lifecycle_signal_label`, `lifecycle_signal_source`,
   `lifecycle_signal_signature`, and `lifecycle_message_index` when present.
   Optional terminal provenance such as `agent_end_source` may be copied from an
   explicit scalar context field. Do not include transcript text, facts, recall
   rows, daemon signal contents, cursor state, source-window rows, or
   context-refresh contents.
8. Dispatch through the existing `emitRuntimeEvent("session.agent_end", payload,
   "immediate")` helper so runtime event handling, M24 signal writing, M28 wake
   behavior, and daemon session-end processing remain owned by the existing
   runtime path.
9. Preserve M29 compaction emitter behavior exactly. The existing
   `CompactionSignal` path keeps its live `transcriptPath` requirement,
   `session.compaction` event name, payload fields, fail-soft/failHard behavior,
   and `emitRuntimeEvent(..., "immediate")` dispatch.
10. Preserve M30 reset emitter behavior exactly. The existing `ResetSignal` path
    keeps its `resetTranscriptPath` / `reset_transcript_path` requirement,
    `session.reset` event name, live-transcript rejection guard, payload fields,
    fail-soft/failHard behavior, and `emitRuntimeEvent(..., "immediate")`
    dispatch.
11. Preserve M31 timeout emitter behavior exactly. The existing `TimeoutSignal`
    path keeps its live transcript path requirement, reset-path rejection guard,
    `session.timeout` event name, payload fields, fail-soft/failHard behavior,
    and `emitRuntimeEvent(..., "immediate")` dispatch.
12. Preserved `emitEvent()` behavior exactly. Do not add a second events CLI path,
    direct Python process invocation, direct `write_signal()` call, direct
    `ensure_alive()` call, or direct daemon session-finalization call in the
    facade helper.
13. Preserved M22/M24/M25/M26/M27/M28 runtime behavior exactly. M32 must not
    change runtime event eligibility, signal shapes, dedupe rules, wake metadata,
    agent-end default bridge behavior, timeout marker behavior, reset
    backup/cursor handling, compaction context refresh, or failHard behavior in
    `core.runtime.events` or `core.extraction_daemon`.
14. Did not wire OpenClaw hooks, `agent_end`, `session_end`, transcript-update
    fallback, before-agent hooks, timeout manager paths, idle scanners,
    session-index reset paths, compaction hook paths, or reset hook paths to
    `processLifecycleEvent()` in this slice. Current adapter direct signal
    writers may continue unchanged.
15. Preserved lifecycle duplicate-suppression helpers. The existing
    `detectLifecycleSignal()` / `shouldProcessLifecycleSignal()` /
    `markLifecycleSignalFromHook()` contract currently models reset/compaction
    hook signals; M32 does not require expanding those helper types to agent-end
    unless a concrete hook caller is added in a future approved slice.
16. Preserved generated runtime-pair discipline. Edited `core/facade.ts`, ran
    `npm run build:runtime` so the paired generated runtime file is derived, and
    validated with `npm run check:runtime-pairs`.

## Non-Targets

- no session-start or before-agent-start facade emitter behavior
- no agent-end emission from reset-specific `context.resetTranscriptPath` /
  `context.reset_transcript_path`
- no new lifecycle event names or aliases such as `session_end`, `session.end`,
  or `agent.end`
- no changes to M29 compaction facade emitter behavior
- no changes to M30 reset facade emitter behavior or live-transcript rejection
  guard
- no changes to M31 timeout facade emitter behavior or reset-path rejection guard
- no changes to M22 explicit signal mapping or validation
- no changes to M24/M25/M26/M27 default signal eligibility or signal shape
- no changes to M28 wake selection or wake metadata
- no direct `write_signal()` call from `core/facade.ts`
- no direct `ensure_alive()`, `start_daemon()`, `stop_daemon()`, restart helper,
  `subprocess`, pidfile, or manual daemon process management from `core/facade.ts`
- no datastore imports in `core/facade.ts` for this helper
- no migration or alteration of OpenClaw `agent_end`, `session_end`,
  transcript-update fallback, before-agent hooks, timeout manager paths, adapter
  direct signal writing, or adapter-side daemon wake
- no daemon signal priority, polling, locking, cursor, rolling buffer, timeout
  classifier, timeout marker contents, reset backup, compaction context refresh,
  session-end finalization, or transcript ownership behavior change
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

- `failHard=true`: selected facade agent-end emitter failures must raise loudly
  through the existing facade/dependency path. Unsupported signal labels,
  malformed signal objects, missing session id, missing/empty/nonexistent live
  transcript path, and agent-end inputs that provide only reset-specific
  transcript fields are selected facade failures under failHard. Do not catch and
  convert a failed `emitRuntimeEvent()` / `execEvents()` call into
  acknowledgement success.
- `failHard=false`: unsupported or invalid facade lifecycle inputs may return a
  passive no-op result, but must not claim a runtime event was emitted, must not
  call `emitRuntimeEvent()` / `execEvents()`, and must log or surface enough
  metadata for operator diagnosis.
- Runtime `write_signal()`, daemon session-end extraction/finalization, and
  daemon wake failures remain governed by existing M24/M28/daemon rules after
  the event is emitted. M32 must not catch those runtime failures in facade code
  or relabel them as facade success.
- Do not fall back to reset-specific transcript fields if the live agent-end
  transcript path is missing or invalid.
- Do not fall back to OpenClaw hook state, transcript-update fallback discovery,
  adapter direct signal writing, manual signal-file writes, direct daemon wake,
  or direct extraction when facade event emission fails.
- Do not wrap lifecycle detection, duplicate suppression, event emission, daemon
  signal writing, daemon wake, daemon session-end finalization, transcript-update
  fallback handling, and adapter hook acknowledgement in one shared broad
  `try`/`catch` that could hide selected failures under failHard.

## Required Tests Before W4

Add or preserve focused tests proving:

- `processLifecycleEvent()` for a valid `AgentEndSignal` with concrete
  `sessionId` and existing live `transcriptPath` / `transcript_path` /
  `sessionFile` / `session_file` calls `execEvents("emit", args)` exactly once
  with `--name session.agent_end`, `--dispatch immediate`, and a compact payload
  containing `session_id` and `transcript_path`.
- The emitted agent-end payload preserves compact lifecycle provenance fields
  from the signal (`label`, `source`, `signature`, optional `messageIndex`) and
  optional caller context fields without transcript text, daemon signal contents,
  cursor state, facts, recall rows, or source-window rows.
- An `AgentEndSignal` with only `resetTranscriptPath` / `reset_transcript_path`,
  even when that file exists, does not call `execEvents()` under fail-soft and
  raises under failHard.
- Missing session id, missing live transcript path, empty live transcript path,
  and nonexistent live transcript path do not call `execEvents()`; they return
  passive no-op metadata under fail-soft and raise under failHard.
- Session-start, unknown, or malformed signals do not emit runtime events and
  follow the same fail-soft no-op / failHard raise contract.
- The M29 `CompactionSignal` happy path and no-op behavior remain unchanged.
- The M30 `ResetSignal` happy path, reset-specific path requirement, and
  live-transcript rejection behavior remain unchanged.
- The M31 `TimeoutSignal` happy path, live transcript path requirement, and
  reset-path rejection behavior remain unchanged.
- `emitEvent()` malformed-output and dependency failure behavior remains
  unchanged; selected facade agent-end emission must not swallow those errors.
- Existing lifecycle detection and duplicate-suppression tests still pass; M32
  must not require widening those helpers for agent-end unless a concrete hook
  caller is approved.
- Runtime lifecycle tests for M22/M24/M25/M26/M27/M28 still pass, especially M24
  default agent-end signal writing, M24 no-path compatibility no-op, M24 explicit
  precedence, cross-path `session_end` dedupe, and M28 wake metadata.
- Existing daemon session-end tests still pass, including signal dedupe,
  extraction/finalization, lifecycle observation, and missing transcript
  retry/finalization behavior where covered.
- Source assertions or grep checks prove `core/facade.ts` does not call
  `write_signal`, `ensure_alive`, `start_daemon`, `stop_daemon`, restart helpers,
  `subprocess`, pidfile helpers, datastore imports, manual signal-file helpers,
  daemon session-end finalization helpers, transcript-update fallback code, or
  adapter hook migration code for this slice.
- Runtime-pair checks pass after deriving paired files from `core/facade.ts`.
- Active/request session ingest parity and M19 source-window metadata tests still
  pass; facade agent-end emitter wiring must not affect recall policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow facade agent-end emitter smoke:

- Call `createQuaidFacade().processLifecycleEvent()` with a valid
  `AgentEndSignal`, concrete session id, and existing live transcript path. It
  should emit one `session.agent_end` event through the events CLI with immediate
  dispatch.
- Confirm the runtime path writes one compatible daemon `session_end` signal and
  wakes through `core.extraction_daemon.ensure_alive()` via the existing M24/M28
  behavior.
- Confirm an `AgentEndSignal` with only reset-specific transcript path does not
  call the runtime events path and does not queue a `session_end` signal.
- Confirm missing/nonexistent live transcript path and malformed signal inputs do
  not call the runtime events path.
- Confirm `CompactionSignal`, `ResetSignal`, and `TimeoutSignal` still emit
  through the M29/M30/M31 behavior, including reset's live-transcript no-op guard
  and timeout's reset-path no-op guard.
- Confirm OpenClaw `agent_end`, `session_end`, transcript-update fallback paths,
  timeout manager paths, and adapter direct signal writing remain outside facade
  ownership.
- Confirm no daemon restart/stop, manual signal-file writing, direct daemon wake
  from facade, daemon session-end finalization from facade, or
  recall/source-window policy change is observed.

## Deferred Decisions

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

## Implementation Record

Runtime closed at `b015b5dbab4d07bc2ca6db15920d240fbc4acd15`
(`refactor(datastore): emit agent-end lifecycle from facade`) after the approved
plan commit `2b7f22d276d6791b4bc79e39347fb1d504f3786b`.

Implemented behavior:

- Extended `createQuaidFacade().processLifecycleEvent()` with an explicit
  `AgentEndSignal` branch alongside the M29 `CompactionSignal`, M30
  `ResetSignal`, and M31 `TimeoutSignal` branches. The implementation remains
  four explicit label branches followed by the existing unsupported-label
  no-op/failHard path; it is not a generalized lifecycle event passthrough.
- Requires an explicit concrete session id from caller context
  (`sessionId`/`session_id`) and an explicit existing live transcript path from
  caller context (`transcriptPath`/`transcript_path`/`sessionFile`/
  `session_file`). Missing, empty, or nonexistent live paths do not emit runtime
  events under fail-soft and raise under failHard.
- Preserves cross-emitter field discipline at the facade layer. The
  `AgentEndSignal` branch reads only live transcript fields; it does not read
  `ctx.resetTranscriptPath` or `ctx.reset_transcript_path`. A reset-path-only
  agent-end input remains passive no-op under fail-soft and raises under
  failHard.
- Emits exactly the existing runtime event name `session.agent_end` through the
  existing `emitRuntimeEvent()` / `execEvents()` path with dispatch `immediate`
  and payload field `transcript_path`. The daemon signal name `session_end` and
  event aliases such as `session.end` or `agent.end` are not emitted from the
  facade.
- Builds a compact agent-end payload only: `session_id`, `transcript_path`,
  `lifecycle_signal_label`, optional `lifecycle_signal_source`, optional
  `lifecycle_signal_signature`, optional nonnegative `lifecycle_message_index`,
  optional caller context `reason`, `adapter`, and `source`, and optional
  `agent_end_source`. It does not include transcript text, facts, recall rows,
  daemon signal contents, cursor state, source-window rows, or context-refresh
  contents.
- Preserves M29 `CompactionSignal` behavior verbatim. The existing branch keeps
  its live transcript path inputs, compact payload fields, fail-soft/failHard
  behavior, and `emitRuntimeEvent("session.compaction", ..., "immediate")`
  dispatch unchanged.
- Preserves M30 `ResetSignal` behavior verbatim, including the load-bearing
  live-transcript rejection guard. The existing branch keeps its
  `resetTranscriptPath` / `reset_transcript_path` requirement,
  `session.reset` event name, payload fields, fail-soft/failHard behavior, and
  `emitRuntimeEvent("session.reset", ..., "immediate")` dispatch unchanged.
- Preserves M31 `TimeoutSignal` behavior verbatim, including the reset-path
  rejection guard. The existing branch keeps its live transcript path inputs,
  compact payload fields, fail-soft/failHard behavior, and
  `emitRuntimeEvent("session.timeout", ..., "immediate")` dispatch unchanged.
- Leaves M24 as the only owner of writing daemon `session_end` signals from
  event-bus `session.agent_end` events and M28 as the only owner of daemon wake
  after event-bus signal queueing. `core/facade.ts` does not call
  `write_signal`, `ensure_alive`, `start_daemon`, `stop_daemon`, restart
  helpers, `subprocess`, pidfile helpers, datastore imports, manual signal-file
  helpers, daemon session-end finalization helpers, transcript-update fallback
  code, or direct daemon wake helpers.
- Preserves M22/M24/M25/M26/M27/M28 runtime behavior by scope: no
  `core.runtime.events` or daemon files changed, so lifecycle event eligibility,
  signal shape, dedupe, wake metadata, agent-end default bridge behavior,
  timeout marker behavior, reset backup/cursor handling, daemon polling, and
  failHard behavior remain owned by the existing runtime path.
- Preserves OpenClaw terminal lifecycle ownership. No adapter files changed;
  current OpenClaw `agent_end`, `session_end`, transcript-update fallback,
  before-agent hooks, direct `session_end` signal writing, duplicate
  suppression, and adapter-side daemon wake behavior remain unchanged.
- Preserves lifecycle duplicate-suppression helper ownership. Existing
  `detectLifecycleSignal()`, `shouldProcessLifecycleSignal()`,
  `markLifecycleSignalFromHook()`, and related history helpers remain unchanged;
  they continue to model reset/compaction hook signals only.
- Preserves generated runtime-pair discipline. `core/facade.ts` was edited, and
  the paired generated `core/facade.js` was derived with
  `npm run build:runtime`; `npm run check:runtime-pairs` passed.
- Preserves MemoryDB `session_chunks` recall/write ownership, SessionDB
  `capabilities.recall=[]`, M19 source-window metadata/output policy, active and
  request ingest parity, daemon polling/processing ownership, CLI/default
  routing, broad compatibility aliases, and `.ego` deferral.

Test coverage added or preserved:

- `processLifecycleEvent emits agent-end through existing events path` proves a
  valid `AgentEndSignal` with concrete session id and existing live transcript
  path calls `execEvents("emit", args)` exactly once with `--name
  session.agent_end`, `--dispatch immediate`, and compact lifecycle provenance
  payload fields including `transcript_path` and optional `agent_end_source`.
- The agent-end happy-path facade test asserts the emitted payload does not
  contain transcript text.
- `processLifecycleEvent rejects reset transcript agent-end inputs without
  emitting` proves an `AgentEndSignal` with only `resetTranscriptPath`, even when
  that file exists, returns `status="ignored"` / `event_emitted=false` under
  fail-soft, raises under failHard, logs the live-transcript requirement, and
  does not call `execEvents()`.
- The invalid-input tests preserve unsupported-label no-op behavior by moving
  the unsupported example to `SessionStartSignal`, while reflecting that
  `AgentEndSignal` is now a selected label.
- `processLifecycleEvent emits compaction through existing events path`,
  `processLifecycleEvent emits reset through existing events path`, and
  `processLifecycleEvent emits timeout through existing events path` preserve
  M29, M30, and M31 happy-path behavior. The M30 live-transcript reset rejection
  test and M31 reset-path timeout rejection test remain unchanged.
- `processLifecycleEvent preserves emitEvent failure behavior` proves malformed
  events CLI output still raises through the shared `emitRuntimeEvent()` path and
  is not relabeled as facade success.
- The source-boundary test proves `processLifecycleEvent` emits only the selected
  `session.compaction`, `session.reset`, `session.timeout`, and
  `session.agent_end` events with `immediate` dispatch; explicitly forbids
  `session_end`, `session.end`, and `agent.end` event aliases; does not
  reference forbidden daemon/process APIs; specifically asserts the
  `ResetSignal` source slice does not read live transcript fields; specifically
  asserts the `TimeoutSignal` source slice does not read reset-specific
  transcript fields; and specifically asserts the `AgentEndSignal` source slice
  does not read reset-specific transcript fields.
- Existing facade lifecycle detection/dedupe tests, M24/M28 runtime lifecycle
  tests, extraction-daemon session-end/lifecycle/write-signal tests,
  source-window/session bridge tests, session-timeout-manager tests,
  runtime-pair checks, docs consistency, boundary checks, eslint with existing
  facade warnings only, and unit-wrapper lanes remain green.

Validation chain:

- W4 R201 live/source-proof PASS on `b015b5dba`: installed `facade.ts` and
  generated `facade.js` were deployed; W4 verified the new `AgentEndSignal`
  branch, live transcript path discipline, reset-path-only no-op behavior, happy
  `session.agent_end` immediate emission with `transcript_path`, M29/M30/M31
  preservation, absence of forbidden facade daemon/datastore APIs, and M24/M28
  backend composition.
- W3 runtime/recall APPROVED with no findings on `b015b5dba`: explicit agent-end
  facade emission can only change terminal signal timing through the existing
  M24/M28 path, while MemoryDB `session_chunks`, SessionDB `recall=[]`, M19
  source-window policy, and active/request ingest ownership stay unchanged.
- W6 runtime APPROVED on `b015b5dba` with one LOW informational note for optional
  AgentEndSignal validation-branch test specificity. W6 verified all 16 plan
  steps, TS/JS pair derivation, M29/M30/M31 branch preservation, event-name
  discipline, cross-emitter rejection, OpenClaw/daemon ownership isolation, and
  B-code cleanliness.
- W8 STATIC PASS/runtime HOLD CLOSED on `b015b5dba`: W8 verified exact runtime
  paths, Solomon attribution, runtime-pair build/check, facade tests, full
  `test_events.py`, extraction-daemon session-end/lifecycle/write-signal
  selector, session-timeout-manager tests, store-recall sessiondb/source-window
  selector, docs consistency, boundary checks, eslint with existing facade
  warnings only, unit-wrapper `140/140`, and repo cleanliness.
