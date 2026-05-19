# Datastore Events M30 Reset Facade Lifecycle Emitter Plan

Status: runtime reset facade emitter slice complete; timeout/agent-end emitters deferred
Owner: W1 facade/runtime, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m29-compaction-facade-lifecycle-emitter-plan.md`

## Precondition

Runtime code for M30 was gated on:

1. M29 facade compaction lifecycle emitter was closed through W4/W3/W6/W8.
2. W3 reviewed the selected reset emitter slice because facade-originated reset
   lifecycle events can write daemon `reset` signals and wake the daemon through
   the existing M27/M28 path, changing when reset-preserved evidence becomes
   recall-visible through MemoryDB `session_chunks`.
3. W6 reviewed the facade-to-runtime reset boundary because reset has stricter
   transcript ownership than compaction: a live transcript path can refer to a
   post-reset conversation, while reset extraction must use the pre-reset
   preserved transcript or reset backup.
4. W8 confirmed static coverage includes facade tests, runtime-pair checks,
   runtime lifecycle reset tests, daemon reset backup/cursor tests,
   source-window/session bridge checks, and boundary checks.
5. W4 was ready to live-check that a facade-originated reset lifecycle event emits
   one existing `session.reset` event only when given an explicit reset-preserved
   transcript path, writes one compatible daemon `reset` signal through the
   existing M27 path, and wakes through the M28 `ensure_alive()` path without
   migrating OpenClaw reset hooks.

This document records the completed narrow reset facade emitter slice only. It
does not approve live
`transcriptPath` reset queueing, reset backup discovery in the facade, timeout
emitter wiring, agent-end emitter wiring, OpenClaw hook migration, adapter
reset-signal removal, daemon restart/stop behavior, new lifecycle event names,
new daemon signal types, request/default routing changes, SessionDB recall
selectors, source-window selector ownership, broad compatibility-alias
retirement, CLI behavior changes, `.ego` integration, public push, or release
actions.

## Goal

M29 retired the `processLifecycleEvent()` facade stub only for
`CompactionSignal`. M30 selected the next facade-emitter slice, but reset must not
reuse the compaction field contract. M27 intentionally made default reset
queueing stricter than compaction: runtime `session.reset` events are eligible
for the default daemon `reset` signal only when they carry a concrete
`session_id` and a real `payload.reset_transcript_path` representing pre-reset
preserved evidence.

M30 therefore implemented facade `processLifecycleEvent()` for `ResetSignal` only
when the caller explicitly supplies a concrete `sessionId` and an existing
reset-preserved transcript path, exposed at the facade boundary as
`context.resetTranscriptPath` or `context.reset_transcript_path`. It emits the
existing `session.reset` runtime event through the existing `emitEvent()` /
`execEvents()` immediate path with payload `reset_transcript_path`. The existing
M27 runtime handler remains the only owner of writing the daemon `reset` signal,
and the existing M28 wake helper remains the only owner of waking the daemon
after that signal is queued.

M30 was not OpenClaw reset hook migration. It did not move `before_reset`,
session-index reset handling, orphan-backup waits, recent-reset marker logic,
duplicate suppression, reset backup discovery, cursor retargeting, or direct
adapter signal-writing into the facade. It only added the facade reset emitter
contract for future callers that already hold the correct reset-preserved
transcript path.

## Current Boundary

Pre-M30 path:

1. `createQuaidFacade().emitEvent()` delegates to the runtime events CLI through
   `deps.execEvents("emit", ...)`, normalizes payload/source/dispatch arguments,
   parses JSON output, and raises on malformed output.
2. `createQuaidFacade().processLifecycleEvent()` supported only
   `CompactionSignal`. It requires a concrete `sessionId` and existing live
   `transcriptPath`, emits `session.compaction` with dispatch `immediate`, and
   returns fail-soft no-op metadata or raises under failHard for unsupported or
   invalid inputs.
3. `ResetSignal` inputs were unsupported by `processLifecycleEvent()`.
   Under M29 they returned passive no-op metadata under fail-soft or raised under
   failHard, and they do not read live `transcriptPath` as reset evidence.
4. `_handle_session_lifecycle()` in `core.runtime.events` already handles
   `session.reset` events. M27 selects only plain `session.reset` with concrete
   `session_id` and existing `payload.reset_transcript_path`, then writes the
   existing daemon `reset` signal through `core.extraction_daemon.write_signal()`.
   A reset event with only live `payload.transcript_path` remains ack plus
   observation only.
5. M28 wakes the daemon through `core.extraction_daemon.ensure_alive()` only after
   the selected event-bus lifecycle signal write succeeds.
6. OpenClaw adapter reset paths currently own `before_reset`, session-end reset,
   session-index reset, transcript snapshotting, orphan-backup waits,
   recent-reset marker suppression, duplicate suppression, reset signal direct
   writing, and adapter-side daemon wake. M30 did not migrate or alter those
   paths.
7. The daemon reset processor owns reset backup discovery, cursor retargeting,
   duplicate extraction avoidance, rolling-buffer cleanup, plain-session rebasing,
   and missing transcript retry/finalization. M30 did not move that ownership
   into the facade.
8. MemoryDB remains the owner of `session_chunks` recall/write projection and
   final source-window output policy. SessionDB `capabilities.recall` remains
   `[]`.

## Selected First Slice: Facade Reset Lifecycle Event Emitter

Implemented one runtime slice only:

1. Extended the existing `processLifecycleEvent()` facade method to support
   `signal.label === "ResetSignal"` in addition to the already-closed M29
   `CompactionSignal` path. Keep the implementation as explicit parallel
   branches; do not generalize compaction and reset into a broad lifecycle event
   passthrough.
2. Support only `ResetSignal` and existing `CompactionSignal` in this slice.
   Timeout, agent-end, unknown, or malformed signals must not emit runtime
   events; they return passive no-op metadata under fail-soft and raise under
   failHard.
3. Require an explicit concrete session id from caller context, for example
   `context.sessionId` or `context.session_id`. Do not infer session id from
   transcript contents, filenames, adapter-global state, or session-index reset
   state in this helper.
4. Require an explicit reset-preserved transcript path from caller context,
   specifically `context.resetTranscriptPath` or `context.reset_transcript_path`,
   and verify it exists before emitting. This path must represent the pre-reset
   preserved transcript or reset backup selected by the caller.
5. Do not use live `context.transcriptPath`, `context.transcript_path`,
   `context.sessionFile`, or `context.session_file` for the reset path. A
   `ResetSignal` with only live transcript context must return passive no-op
   metadata under fail-soft and raise under failHard. This guard is load-bearing:
   it prevents facade reset emission from extracting a reused post-reset
   transcript.
6. Emit exactly one existing runtime event name for reset: `session.reset`. Do
   not add new event names or aliases.
7. Build a compact reset payload only: `session_id`, `reset_transcript_path`,
   optional `reason`, optional `adapter`/`source`, and lifecycle provenance such
   as `lifecycle_signal_label`, `lifecycle_signal_source`,
   `lifecycle_signal_signature`, and `lifecycle_message_index` when present.
   Optional reset path provenance such as `reset_transcript_source` may be
   copied from an explicit scalar context field. Do not include transcript text,
   facts, recall rows, reset backup contents, cursor state, source-window rows,
   or context-refresh contents.
8. Dispatches through the existing `emitRuntimeEvent("session.reset", payload,
   "immediate")` helper so runtime event handling, M27 signal writing, and M28
   wake behavior remain owned by `core.runtime.events`.
9. Preserved M29 compaction emitter behavior exactly. The existing
   `CompactionSignal` path keeps its live `transcriptPath` requirement,
   `session.compaction` event name, payload fields, fail-soft/failHard behavior,
   and `emitRuntimeEvent(..., "immediate")` dispatch.
10. Preserved `emitEvent()` behavior exactly. Do not add a second events CLI path,
    direct Python process invocation, direct `write_signal()` call, or direct
    `ensure_alive()` call in the facade helper.
11. Preserved M22/M24/M25/M26/M27/M28 runtime behavior exactly. M30 must not
    change runtime event eligibility, signal shapes, dedupe rules, wake metadata,
    reset backup/cursor handling, or failHard behavior in `core.runtime.events`
    or `core.extraction_daemon`.
12. Did not wire OpenClaw hooks, session-index reset paths, orphan-backup waits,
    timeout manager, compaction hook paths, or agent-end behavior to
    `processLifecycleEvent()` in this slice. Current adapter direct signal
    writers may continue unchanged.
13. Preserved lifecycle duplicate-suppression helpers. If a future caller wants to
    suppress duplicate reset emit attempts, it must use the existing
    `shouldProcessLifecycleSignal()` / `markLifecycleSignalFromHook()` contract;
    M30 does not add new dedupe storage or recent-reset marker handling.
14. Preserved generated runtime-pair discipline. Edited `core/facade.ts`, ran
    `npm run build:runtime` so the paired generated runtime file was derived, and
    validated with `npm run check:runtime-pairs`.

## Non-Targets

- no reset emission from live `context.transcriptPath`, `context.transcript_path`,
  `context.sessionFile`, or `context.session_file`
- no reset backup discovery or reset backup path inference in `core/facade.ts`
- no timeout emitter behavior
- no agent-end emitter behavior
- no new lifecycle event names or daemon signal types
- no changes to M29 compaction facade emitter behavior
- no changes to M22 explicit signal mapping or validation
- no changes to M24/M25/M26/M27 default signal eligibility or signal shape
- no changes to M28 wake selection or wake metadata
- no direct `write_signal()` call from `core/facade.ts`
- no direct `ensure_alive()`, `start_daemon()`, `stop_daemon()`, restart helper,
  `subprocess`, pidfile, or manual daemon process management from `core/facade.ts`
- no datastore imports in `core/facade.ts` for this helper
- no migration or alteration of OpenClaw `before_reset`, session-end reset,
  session-index reset, orphan-backup wait, recent-reset marker, duplicate
  suppression, or adapter direct reset-signal writing
- no changes to daemon reset backup discovery, cursor retargeting, duplicate
  extraction avoidance, rolling-buffer cleanup, plain-session rebasing, or
  missing transcript retry/finalization
- no changes to daemon signal priority, polling, locking, cursor, rolling buffer,
  timeout classifier, timeout marker contents, compaction context refresh, or
  transcript ownership behavior
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

- `failHard=true`: selected facade reset emitter failures must raise loudly
  through the existing facade/dependency path. Unsupported signal labels,
  malformed signal objects, missing session id, missing/empty/nonexistent
  `resetTranscriptPath` / `reset_transcript_path`, and reset inputs that provide
  only live transcript fields are selected facade failures under failHard. Do not
  catch and convert a failed `emitRuntimeEvent()` / `execEvents()` call into
  acknowledgement success.
- `failHard=false`: unsupported or invalid facade lifecycle inputs may return a
  passive no-op result, but must not claim a runtime event was emitted, must not
  call `emitRuntimeEvent()` / `execEvents()`, and must log or surface enough
  metadata for operator diagnosis.
- Runtime `write_signal()` and daemon wake failures remain governed by existing
  M27/M28 rules after the event is emitted. M30 must not catch those runtime
  failures in facade code or relabel them as facade success.
- Do not fall back to live transcript fields if the reset-specific path is
  missing or invalid.
- Do not fall back to reset backup discovery, adapter direct signal writing,
  manual signal-file writes, direct daemon wake, or direct extraction when facade
  event emission fails.
- Do not wrap lifecycle detection, duplicate suppression, event emission, daemon
  signal writing, daemon wake, reset backup discovery, and adapter hook
  acknowledgement in one shared broad `try`/`catch` that could hide selected
  failures under failHard.

## Required Tests Before W4

Add or preserve focused tests proving:

- `processLifecycleEvent()` for a valid `ResetSignal` with concrete `sessionId`
  and existing `resetTranscriptPath` / `reset_transcript_path` calls
  `execEvents("emit", args)` exactly once with `--name session.reset`,
  `--dispatch immediate`, and a compact payload containing `session_id` and
  `reset_transcript_path`.
- The emitted reset payload preserves compact lifecycle provenance fields from
  the signal (`label`, `source`, `signature`, optional `messageIndex`) and
  optional caller context fields without transcript text, reset backup contents,
  cursor state, facts, recall rows, or source-window rows.
- A `ResetSignal` with only live `transcriptPath` / `transcript_path` /
  `sessionFile` / `session_file`, even when that file exists, does not call
  `execEvents()` under fail-soft and raises under failHard.
- Missing session id, missing reset transcript path, empty reset transcript path,
  and nonexistent reset transcript path do not call `execEvents()`; they return
  passive no-op metadata under fail-soft and raise under failHard.
- Timeout, agent-end, unknown, or malformed signals do not emit runtime events
  and follow the same fail-soft no-op / failHard raise contract.
- The M29 `CompactionSignal` happy path and no-op behavior remain unchanged.
- `emitEvent()` malformed-output and dependency failure behavior remains
  unchanged; selected facade reset emission must not swallow those errors.
- Existing lifecycle detection and duplicate-suppression tests still pass.
- Runtime lifecycle tests for M22/M24/M25/M26/M27/M28 still pass, especially M27
  reset default signal writing, M27 live-transcript no-op, and M28 wake metadata.
- Existing daemon reset tests still pass, including reset backup re-extraction,
  stale rolling-buffer cursor cleanup, cursor rebasing from backup to plain
  session file, and missing transcript retry/finalization behavior.
- Source assertions or grep checks prove `core/facade.ts` does not call
  `write_signal`, `ensure_alive`, `start_daemon`, `stop_daemon`, restart helpers,
  `subprocess`, pidfile helpers, datastore imports, reset backup discovery, or
  OpenClaw recent-reset marker logic for this slice.
- Runtime-pair checks pass after deriving paired files from `core/facade.ts`.
- Active/request session ingest parity and M19 source-window metadata tests still
  pass; facade reset emitter wiring must not affect recall policy.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow facade reset emitter smoke:

- Call `createQuaidFacade().processLifecycleEvent()` with a valid `ResetSignal`,
  concrete session id, and existing reset-preserved transcript path. It should
  emit one `session.reset` event through the events CLI with immediate dispatch.
- Confirm the runtime path writes one compatible daemon `reset` signal and wakes
  through `core.extraction_daemon.ensure_alive()` via the existing M27/M28
  behavior.
- Confirm a `ResetSignal` with only a live transcript path does not call the
  runtime events path and does not queue a reset signal.
- Confirm missing/nonexistent reset transcript path and malformed signal inputs do
  not call the runtime events path.
- Confirm `CompactionSignal` still emits `session.compaction` with the M29
  behavior, and timeout/agent-end labels remain unsupported in this slice.
- Confirm OpenClaw reset hooks and session-index reset paths remain direct
  adapter-hook signal writers and no adapter hook migration occurred.
- Confirm no daemon restart/stop, manual signal-file writing, direct daemon wake
  from facade, reset backup discovery in facade, or recall/source-window policy
  change is observed.

## Deferred Decisions

- timeout facade lifecycle emitter closed in M31 at `815b938`; it requires
  explicit live transcript path discipline and does not use reset-preserved
  transcript paths as timeout evidence
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


## Implementation Record

Runtime closed at `9f43c69667bd02b2d314993bf0e2c4845d30099b`
(`refactor(datastore): emit reset lifecycle from facade`) after the approved
plan commit `6ba859d1bc164ce9bf82dc9b4d96c27c325353ac`.

Implemented behavior:

- Extended `createQuaidFacade().processLifecycleEvent()` with an explicit
  `ResetSignal` branch alongside the M29 `CompactionSignal` branch. The
  implementation remains two explicit label branches followed by the existing
  unsupported-label no-op/failHard path; it is not a generalized lifecycle event
  passthrough.
- Requires an explicit concrete session id from caller context
  (`sessionId`/`session_id`) and an explicit existing reset-preserved transcript
  path from caller context (`resetTranscriptPath`/`reset_transcript_path`).
  Missing, empty, or nonexistent reset paths do not emit runtime events under
  fail-soft and raise under failHard.
- Preserves the load-bearing M27 reset discipline at the facade layer. The
  `ResetSignal` branch reads only `ctx.resetTranscriptPath` /
  `ctx.reset_transcript_path`; it does not read live `ctx.transcriptPath`,
  `ctx.transcript_path`, `ctx.sessionFile`, or `ctx.session_file`. A live-only
  reset input remains passive no-op under fail-soft and raises under failHard.
- Emits exactly the existing runtime event name `session.reset` through the
  existing `emitRuntimeEvent()` / `execEvents()` path with dispatch `immediate`
  and payload field `reset_transcript_path`.
- Builds a compact reset payload only: `session_id`, `reset_transcript_path`,
  `lifecycle_signal_label`, optional `lifecycle_signal_source`, optional
  `lifecycle_signal_signature`, optional nonnegative `lifecycle_message_index`,
  optional caller context `reason`, `adapter`, and `source`, and optional
  `reset_transcript_source`. It does not include transcript text, facts, recall
  rows, reset backup contents, cursor state, source-window rows, or
  context-refresh contents.
- Preserves M29 `CompactionSignal` behavior at logic level inside the new
  explicit branch wrapper: live transcript path inputs, compact payload fields,
  fail-soft/failHard behavior, and `emitRuntimeEvent("session.compaction", ...,
  "immediate")` dispatch remain unchanged.
- Leaves M27 as the only owner of writing daemon `reset` signals and M28 as the
  only owner of daemon wake after event-bus signal queueing. `core/facade.ts`
  does not call `write_signal`, `ensure_alive`, `start_daemon`, `stop_daemon`,
  restart helpers, `subprocess`, pidfile helpers, datastore imports, manual
  signal-file helpers, reset backup discovery, or direct daemon wake helpers.
- Preserves M22/M24/M25/M26/M27/M28 runtime behavior by scope: no
  `core.runtime.events` or daemon files changed, so lifecycle event eligibility,
  signal shape, dedupe, wake metadata, reset backup/cursor handling, daemon
  polling, and failHard behavior remain owned by the existing runtime path.
- Preserves OpenClaw reset adapter-hook ownership. No adapter files changed;
  current `before_reset`, session-end reset, session-index reset,
  orphan-backup wait, recent-reset marker, duplicate suppression, direct reset
  signal writing, and adapter-side daemon wake behavior remain unchanged.
- Preserves lifecycle duplicate-suppression helper ownership. Existing
  `detectLifecycleSignal()`, `shouldProcessLifecycleSignal()`,
  `markLifecycleSignalFromHook()`, and related history helpers remain unchanged.
- Preserves generated runtime-pair discipline. `core/facade.ts` was edited, and
  the paired generated `core/facade.js` was derived with
  `npm run build:runtime`; `npm run check:runtime-pairs` passed.
- Preserves MemoryDB `session_chunks` recall/write ownership, SessionDB
  `capabilities.recall=[]`, M19 source-window metadata/output policy, active and
  request ingest parity, daemon polling/processing ownership, CLI/default
  routing, broad compatibility aliases, and `.ego` deferral.

Test coverage added or preserved:

- `processLifecycleEvent emits reset through existing events path` proves a
  valid `ResetSignal` with concrete session id and existing reset-preserved
  transcript path calls `execEvents("emit", args)` exactly once with `--name
  session.reset`, `--dispatch immediate`, and compact lifecycle provenance
  payload fields including `reset_transcript_path` and optional
  `reset_transcript_source`.
- The reset happy-path facade test asserts the emitted payload does not contain
  transcript text.
- `processLifecycleEvent rejects live transcript reset inputs without emitting`
  proves a `ResetSignal` with only live `transcriptPath` / `sessionFile`, even
  when that file exists, returns `status="ignored"` / `event_emitted=false` under
  fail-soft, raises under failHard, and does not call `execEvents()`.
- The invalid-input tests preserve unsupported-label no-op behavior and selected
  invalid-input failHard behavior while reflecting that `ResetSignal` is now a
  selected label.
- `processLifecycleEvent emits compaction through existing events path` and the
  invalid compaction path preserve M29 `CompactionSignal` happy-path and no-op
  behavior.
- `processLifecycleEvent preserves emitEvent failure behavior` proves malformed
  events CLI output still raises through the shared `emitRuntimeEvent()` path and
  is not relabeled as facade success.
- The source-boundary test proves `processLifecycleEvent` emits only the
  selected `session.compaction` and `session.reset` events with `immediate`
  dispatch, does not reference forbidden daemon/process APIs, and specifically
  asserts the `ResetSignal` source slice does not read live transcript fields.
- Existing facade lifecycle detection/dedupe tests, M27/M28 runtime lifecycle
  tests, source-window/session bridge tests, session-timeout-manager tests,
  runtime-pair checks, docs consistency, boundary checks, eslint with existing
  facade warnings only, and unit-wrapper lanes remain green.

Validation chain:

- W4 R201 live/source-proof PASS on `9f43c696`: installed `facade.ts` and
  generated `facade.js` were deployed; W4 verified the new `ResetSignal` branch,
  reset-specific path discipline, missing/nonexistent reset path no-ops, happy
  `session.reset` immediate emission with `reset_transcript_path`, M29
  compaction preservation, absence of forbidden facade daemon/datastore APIs, and
  M27/M28 backend composition.
- W3 runtime/recall APPROVED with no findings on `9f43c696`: explicit
  reset-preserved facade emission can only change timing through the existing
  M27/M28 path, while MemoryDB `session_chunks`, SessionDB `recall=[]`, M19
  source-window policy, and active/request ingest ownership stay unchanged.
- W6 runtime APPROVED on `9f43c696` with one LOW informational note for optional
  ResetSignal validation-branch test specificity. W6 verified all 14 plan steps,
  TS/JS pair derivation, M29 compaction branch logic preservation, the
  load-bearing live-transcript-rejection guard, OpenClaw/daemon reset processor
  isolation, and B-code cleanliness.
- W8 STATIC PASS/runtime HOLD CLOSED on `9f43c696`: exact changed paths were
  `core/facade.ts`, generated `core/facade.js`, and `tests/facade.test.ts`;
  Solomon attribution, runtime-pair build/check, facade tests, full
  `tests/test_events.py`, source-window/session bridge selector,
  session-timeout-manager, docs consistency, boundary, eslint with existing
  facade warnings only, and unit wrapper 140/140 passed.
