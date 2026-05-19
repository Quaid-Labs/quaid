# Datastore Events M31 Timeout Facade Lifecycle Emitter Plan

Status: runtime timeout facade emitter slice complete; agent-end emitter deferred
Owner: W1 facade/runtime, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m30-reset-facade-lifecycle-emitter-plan.md`

## Precondition

Runtime code for M31 was gated on:

1. M30 reset facade lifecycle emitter was closed through W4/W3/W6/W8.
2. W3 reviewed the selected timeout emitter slice because facade-originated
   timeout lifecycle events can write daemon `timeout` signals, wake the daemon
   through the existing M25/M28 path, write context-refresh timeout markers, and
   change when MemoryDB `session_chunks` evidence becomes recall-visible.
3. W6 reviewed the facade-to-runtime timeout boundary because timeout has
   daemon-side marker and cursor behavior that must remain owned by the existing
   daemon processor, not by the facade.
4. W8 confirmed static coverage includes facade tests, runtime-pair checks,
   runtime lifecycle timeout tests, extraction-daemon timeout/marker tests,
   source-window/session bridge checks, and boundary checks.
5. W4 was ready to live-check that a facade-originated timeout lifecycle event
   emits one existing `session.timeout` event only when given an explicit live
   transcript path, writes one compatible daemon `timeout` signal through the
   existing M25 path, and wakes through the M28 `ensure_alive()` path without
   moving timeout marker, cursor, or daemon ownership into the facade.

This document records the completed narrow timeout facade emitter slice only. It
does not approve agent-end
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

M31 selected the next narrow facade-emitter slice: implemented facade
`processLifecycleEvent()` for `TimeoutSignal` only when the caller explicitly
supplies a concrete `sessionId` and an existing live transcript path, exposed at
the facade boundary as `context.transcriptPath`, `context.transcript_path`,
`context.sessionFile`, or `context.session_file`. It emits the existing
`session.timeout` runtime event through the existing `emitEvent()` /
`execEvents()` immediate path with payload `transcript_path`. The existing M25
runtime handler remains the only owner of writing the daemon `timeout` signal,
and the existing M28 wake helper remains the only owner of waking the daemon
after that signal is queued.

M31 was not timeout manager migration. It did not move idle-timeout detection,
context-refresh timeout marker writing, timeout cursor logic, transcript
classification, adapter direct signal-writing, or daemon timeout finalization
into the facade. It only added the facade timeout emitter contract for future
callers that already hold the correct active transcript path.

## Current Boundary

Pre-M31 path:

1. `createQuaidFacade().emitEvent()` delegates to the runtime events CLI through
   `deps.execEvents("emit", ...)`, normalizes payload/source/dispatch arguments,
   parses JSON output, and raises on malformed output.
2. `createQuaidFacade().processLifecycleEvent()` supported `CompactionSignal` and
   `ResetSignal` only. `CompactionSignal` requires a concrete `sessionId` and
   existing live `transcriptPath`, emits `session.compaction` with dispatch
   `immediate`, and returns fail-soft no-op metadata or raises under failHard for
   invalid inputs. `ResetSignal` requires explicit `resetTranscriptPath` /
   `reset_transcript_path`, emits `session.reset`, and intentionally rejects live
   transcript fields as reset evidence.
3. `TimeoutSignal` inputs were unsupported by `processLifecycleEvent()`.
   Under M30 they returned passive no-op metadata under fail-soft or raised under
   failHard, and they do not emit runtime events.
4. `_handle_session_lifecycle()` in `core.runtime.events` already handles
   `session.timeout` events. M25 selects only plain `session.timeout` with
   concrete `session_id` and existing `payload.transcript_path`, then writes the
   existing daemon `timeout` signal through `core.extraction_daemon.write_signal()`.
5. M28 wakes the daemon through `core.extraction_daemon.ensure_alive()` only after
   the selected event-bus lifecycle signal write succeeds.
6. Daemon timeout processing owns timeout classification, cursor handling,
   duplicate avoidance, extraction/finalization, and context-refresh timeout
   marker writing. M31 did not move that ownership into the facade.
7. Adapter hook or timeout-manager paths may continue to write compatible daemon
   `timeout` signals directly through `write_signal()` when they own that host
   integration. M31 did not migrate or remove those paths.
8. M30 reset remains stricter than timeout and compaction. Facade reset emission
   requires a real reset-preserved transcript path; live transcript paths must
   remain no-op for reset.
9. MemoryDB remains the owner of `session_chunks` recall/write projection and
   final source-window output policy. SessionDB `capabilities.recall` remains
   `[]`.

## Selected First Slice: Facade Timeout Lifecycle Event Emitter

Implemented one runtime slice only:

1. Extended the existing `processLifecycleEvent()` facade method to support
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
8. Dispatches through the existing `emitRuntimeEvent("session.timeout", payload,
   "immediate")` helper so runtime event handling, M25 signal writing, M28 wake
   behavior, and daemon timeout finalization remain owned by the existing runtime
   path.
9. Preserved M29 compaction emitter behavior exactly. The existing
   `CompactionSignal` path keeps its live `transcriptPath` requirement,
   `session.compaction` event name, payload fields, fail-soft/failHard behavior,
   and `emitRuntimeEvent(..., "immediate")` dispatch.
10. Preserved M30 reset emitter behavior exactly. The existing `ResetSignal` path
    keeps its `resetTranscriptPath` / `reset_transcript_path` requirement,
    `session.reset` event name, live-transcript rejection guard, payload fields,
    fail-soft/failHard behavior, and `emitRuntimeEvent(..., "immediate")`
    dispatch.
11. Preserved `emitEvent()` behavior exactly. Do not add a second events CLI path,
    direct Python process invocation, direct `write_signal()` call, direct
    timeout marker write, or direct `ensure_alive()` call in the facade helper.
12. Preserved M22/M24/M25/M26/M27/M28 runtime behavior exactly. M31 must not
    change runtime event eligibility, signal shapes, dedupe rules, wake metadata,
    timeout marker behavior, cursor handling, reset backup/cursor handling,
    compaction context refresh, or failHard behavior in `core.runtime.events` or
    `core.extraction_daemon`.
13. Did not wire OpenClaw hooks, timeout manager paths, idle scanners,
    session-index reset paths, compaction hook paths, reset hook paths, or
    agent-end behavior to `processLifecycleEvent()` in this slice. Current
    adapter direct signal writers may continue unchanged.
14. Preserved lifecycle duplicate-suppression helpers. The existing
    `detectLifecycleSignal()` / `shouldProcessLifecycleSignal()` /
    `markLifecycleSignalFromHook()` contract currently models reset/compaction
    hook signals; M31 does not require expanding those helper types to timeout
    unless a concrete timeout hook caller is added in a future approved slice.
15. Preserved generated runtime-pair discipline. Edited `core/facade.ts`, ran
    `npm run build:runtime` so the paired generated runtime file was derived, and
    validated with `npm run check:runtime-pairs`.

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

- agent-end lifecycle facade emitter closed in M32 at `b015b5dba`
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

Runtime closed at `815b93895c46a19166b6526c002acba155dcf084`
(`refactor(datastore): emit timeout lifecycle from facade`) after the approved
plan commit `cbd2d874a76245dca9a3d4ce9a1e66ea8b415b6f`.

Implemented behavior:

- Extended `createQuaidFacade().processLifecycleEvent()` with an explicit
  `TimeoutSignal` branch alongside the M29 `CompactionSignal` and M30
  `ResetSignal` branches. The implementation remains three explicit label
  branches followed by the existing unsupported-label no-op/failHard path; it is
  not a generalized lifecycle event passthrough.
- Requires an explicit concrete session id from caller context
  (`sessionId`/`session_id`) and an explicit existing live transcript path from
  caller context (`transcriptPath`/`transcript_path`/`sessionFile`/
  `session_file`). Missing, empty, or nonexistent live paths do not emit runtime
  events under fail-soft and raise under failHard.
- Preserves the cross-emitter field discipline at the facade layer. The
  `TimeoutSignal` branch reads only live transcript fields; it does not read
  `ctx.resetTranscriptPath` or `ctx.reset_transcript_path`. A reset-path-only
  timeout input remains passive no-op under fail-soft and raises under failHard.
- Emits exactly the existing runtime event name `session.timeout` through the
  existing `emitRuntimeEvent()` / `execEvents()` path with dispatch `immediate`
  and payload field `transcript_path`.
- Builds a compact timeout payload only: `session_id`, `transcript_path`,
  `lifecycle_signal_label`, optional `lifecycle_signal_source`, optional
  `lifecycle_signal_signature`, optional nonnegative `lifecycle_message_index`,
  optional caller context `reason`, `adapter`, and `source`, and optional
  `timeout_source`. It does not include transcript text, facts, recall rows,
  timeout marker contents, cursor state, source-window rows, or context-refresh
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
- Leaves M25 as the only owner of writing daemon `timeout` signals and M28 as
  the only owner of daemon wake after event-bus signal queueing.
  `core/facade.ts` does not call `write_signal`, `ensure_alive`, `start_daemon`,
  `stop_daemon`, restart helpers, `subprocess`, pidfile helpers, datastore
  imports, manual signal-file helpers, timeout marker writers, timeout cursor
  helpers, timeout classifier/finalization helpers, or direct daemon wake
  helpers.
- Preserves M22/M24/M25/M26/M27/M28 runtime behavior by scope: no
  `core.runtime.events` or daemon files changed, so lifecycle event eligibility,
  signal shape, dedupe, wake metadata, timeout marker behavior, cursor handling,
  daemon polling, and failHard behavior remain owned by the existing runtime
  path.
- Preserves adapter hook and timeout-manager ownership. No adapter files changed;
  current OpenClaw hook paths, timeout manager paths, direct timeout signal
  writing, and adapter-side daemon wake behavior remain unchanged.
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

- `processLifecycleEvent emits timeout through existing events path` proves a
  valid `TimeoutSignal` with concrete session id and existing live transcript
  path calls `execEvents("emit", args)` exactly once with `--name
  session.timeout`, `--dispatch immediate`, and compact lifecycle provenance
  payload fields including `transcript_path` and optional `timeout_source`.
- The timeout happy-path facade test asserts the emitted payload does not contain
  transcript text.
- `processLifecycleEvent rejects reset transcript timeout inputs without
  emitting` proves a `TimeoutSignal` with only `resetTranscriptPath`, even when
  that file exists, returns `status="ignored"` / `event_emitted=false` under
  fail-soft, raises under failHard, logs the live-transcript requirement, and
  does not call `execEvents()`.
- The invalid-input tests preserve unsupported-label no-op behavior by moving
  the unsupported example to `AgentEndSignal`, while reflecting that
  `TimeoutSignal` is now a selected label.
- `processLifecycleEvent emits compaction through existing events path` and
  `processLifecycleEvent emits reset through existing events path` preserve M29
  and M30 happy-path behavior. The M30 live-transcript reset rejection test
  remains unchanged.
- `processLifecycleEvent preserves emitEvent failure behavior` proves malformed
  events CLI output still raises through the shared `emitRuntimeEvent()` path and
  is not relabeled as facade success.
- The source-boundary test proves `processLifecycleEvent` emits only the
  selected `session.compaction`, `session.reset`, and `session.timeout` events
  with `immediate` dispatch, does not reference forbidden daemon/process APIs,
  specifically asserts the `ResetSignal` source slice does not read live
  transcript fields, and specifically asserts the `TimeoutSignal` source slice
  does not read reset-specific transcript fields.
- Existing facade lifecycle detection/dedupe tests, M25/M28 runtime lifecycle
  tests, extraction-daemon timeout/lifecycle/write-signal tests,
  source-window/session bridge tests, session-timeout-manager tests,
  runtime-pair checks, docs consistency, boundary checks, eslint with existing
  facade warnings only, and unit-wrapper lanes remain green.

Validation chain:

- W4 R201 live/source-proof PASS on `815b93895`: installed `facade.ts` and
  generated `facade.js` were deployed; W4 verified the new `TimeoutSignal`
  branch, live transcript path discipline, reset-path-only no-op behavior, happy
  `session.timeout` immediate emission with `transcript_path`, M29/M30
  preservation, absence of forbidden facade daemon/datastore APIs, and M25/M28
  backend composition.
- W3 runtime/recall APPROVED with no findings on `815b93895`: explicit timeout
  facade emission can only change timing through the existing M25/M28 path,
  while MemoryDB `session_chunks`, SessionDB `recall=[]`, M19 source-window
  policy, and active/request ingest ownership stay unchanged.
- W6 runtime APPROVED on `815b93895` with one LOW informational note for
  optional TimeoutSignal validation-branch test specificity. W6 verified all 15
  plan steps, TS/JS pair derivation, M29 and M30 branch preservation, the
  load-bearing cross-emitter rejection guard, daemon timeout ownership
  isolation, lifecycle dedupe helper non-widening, and B-code cleanliness.
- W8 STATIC PASS/runtime HOLD CLOSED on `815b93895`: exact changed paths were
  `core/facade.ts`, generated `core/facade.js`, and `tests/facade.test.ts`;
  Solomon attribution, runtime-pair build/check, facade tests, full
  `tests/test_events.py`, extraction-daemon timeout/lifecycle/write-signal
  selector, source-window/session bridge selector, session-timeout-manager, docs
  consistency, boundary, eslint with existing facade warnings only, and unit
  wrapper 140/140 passed.
