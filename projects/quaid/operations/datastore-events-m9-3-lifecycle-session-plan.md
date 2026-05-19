# Datastore Events M9.3 Lifecycle And Session Plan

Status: session write migration complete; lifecycle ack-only disposition recorded
Owner: W1 runtime/datastore
Plan source: `projects/quaid/operations/datastore-events-m9-monitor-migration-plan.md`

## Precondition

Do not implement runtime code for M9.3 until:

1. M9.2 project-doc update migration passes W3/W4/W6/W8.
2. W4 confirms project-doc worker updates, docs recall/search, and worker
   status output remain stable after M9.2.
3. W6 confirms M9.2 did not leave fallback calls to the removed selected worker
   apply/index path or touch unrelated monitor/write families.
4. W3 reviews the M9.3 implementation plan because session log ingestion feeds
   `session_chunks` recall evidence and can affect recall-visible source-window
   behavior.

W3 approved the first runtime slice under the ownership constraints below. That
slice is complete at runtime commit `ce02408f2` plus test-fixture commit
`e23dfc17f` after W4 live validation, W6 review, and W8 static closure.
The active `session.ingest_log` follow-up is complete at `7c2522ab5` after W3
plan approval, W4 live validation, W6 review, and W8 static/runtime closure.
Ack-only lifecycle events were dispositioned in `1e84e60ed` with guardrail
clarification in `e329d13b1`: they remain core runtime acknowledgements unless
W3/W6 approve a concrete lifecycle persistence contract in a future slice.

## M9.3 Goal

M9.3 migrates lifecycle/session observed writes so runtime monitors become
producers and the owning datastore boundary performs session persistence and
projection.

Current lifecycle events such as `session.new`, `session.reset`,
`session.compaction`, `session.timeout`, `session.agent_start`, and
`session.agent_end` are ack-only in `core/runtime/events.py`. The current
session-facing datastore write is `session.ingest_log`, plus direct daemon calls
to `core.ingest_runtime.run_session_logs_ingest()` after extraction flushes.

The first behavior slice should target that session-log ingest write path, not
the ack-only lifecycle events.

## Current Boundary

Current session-log ingest path:

1. `core/extraction_daemon.py` directly calls
   `core.ingest_runtime.run_session_logs_ingest()` on both no-new-content
   session-end handling and normal final flush handling.
2. `core/runtime/events.py` also handles `session.ingest_log` by calling
   `core.ingest_runtime.run_session_logs_ingest()`.
3. `core/ingest_runtime.py` imports `ingest.session_logs_ingest.run()`.
4. `ingest/session_logs_ingest.py` resolves the transcript source, adapter-parses
   host JSONL when needed, then calls
   `core.services.session_memory_bridge.get_session_memory_bridge().store_session_transcript()`.
5. `core/services/session_memory_bridge.py` stores the transcript through
   `sessiondb` callback routes and projects microchunk evidence into `memorydb`
   session chunks.

The active event bus already exists. M9.3 must evolve or reuse it; it must not
introduce a parallel lifecycle queue or duplicate registry.

## SessionDB Manifest Boundary

M2 intentionally did not register `sessiondb` as a first-party datastore
manifest. SessionDB currently participates through
`core.services.session_memory_bridge` as transcript/provenance plumbing and
projects recallable evidence into `memorydb` session chunks.

Before any behavior patch, implementation review must choose one explicit
ownership model:

- add a first-party `sessiondb` manifest and contract for session transcript
  writes, with `memorydb` projection called from the datastore-owned handler, or
- keep the first slice owned by `memorydb`/the session-memory bridge and record
  that `sessiondb` manifest registration remains deferred to a dedicated
  source-window slice.

Do not silently add `sessiondb` to manifests in the same patch as a behavior
migration unless W3/W6 have reviewed that ownership decision.

## First-Slice Decision

W3 approved keeping this slice owned by the existing MemoryDB/session-memory
bridge boundary. The implementation uses a synchronous
`session.ingest_log.request.v1` request handler registered under MemoryDB. The
handler unwraps to the original `run_session_logs_ingest()` result shape, while
the daemon validates the broker envelope before logging status.

SessionDB remains unregistered as a first-party datastore in this slice. A
dedicated source-window/ownership slice must review and approve any future
SessionDB manifest registration.

Post-M14 update: the metadata-only SessionDB first-party manifest slice closed
at `f0574902b` + `522f16e28` and is recorded in
`projects/quaid/operations/datastore-events-m14-sessiondb-manifest-plan.md`.
The M14 closure keeps `session.ingest_log` ownership, lifecycle persistence, and
source-window behavior out of scope unless a later reviewed slice selects them.

Post-M15 update: the SessionDB ingest helper ownership prerequisite closed at
`379be9a47` and is recorded in
`projects/quaid/operations/datastore-events-m15-sessiondb-ingest-helper-plan.md`.
The request ownership move closed in M16 at `40ff6c8ed` + `23c0e7228` and is
recorded in
`projects/quaid/operations/datastore-events-m16-sessiondb-ingest-request-ownership-plan.md`.
M16 does not select lifecycle persistence or source-window enrichment. Active
`session.ingest_log` import cleanup away from the MemoryDB wrapper closed in M17
at `93b3561f5`.

## First-Slice Validation

Completed runtime:

- `ce02408f2` routes the daemon's selected session-log ingest callsites through
  `session.ingest_log.request.v1`.
- `e23dfc17f` aligns extraction-daemon test fixtures with the broker runtime
  path; it does not change production runtime code.

Validation recorded:

- W4 live PASS on R201: broker route, no direct fallback, fail-soft/failHard
  contracts, parse-before-store ordering, M9.2 stability, clean daemon restart
  after `.pyc` pruning.
- W6 APPROVED-WITH-CONCERNS: no blocking findings; optional participant/source
  metadata population remains intentionally deferred because the replaced daemon
  callsites did not previously supply those fields.
- W8 static PASS: full extraction daemon, adjacent session-log lanes,
  datastore/event/session bridge suites, py_compile, ruff, docs consistency, and
  unit wrapper.

Implementation note:

- The request handler accepts `source_channel`, `conversation_id`,
  `participant_ids`, and `participant_aliases` to preserve the full
  `run_session_logs_ingest()` contract for trusted producers.
- The migrated daemon callsites intentionally pass only the fields that the
  previous direct call supplied. Deriving richer source metadata from signals or
  adapter state is a separate source-window/metadata slice and needs W3 review.

## Proposed First Slice

First slice target: replace the direct daemon-to-ingest session-log write calls
with a broker/event request that preserves the current synchronous outcome and
failHard behavior.

Selected producer paths:

- no-new-content `session_end` path in `core/extraction_daemon.py`
- normal final flush path in `core/extraction_daemon.py`

Selected handler path:

- the datastore-owned session-log ingest handler that resolves transcript
  source, stores SessionDB transcript records, and projects session chunks into
  MemoryDB evidence.

The implementation may reuse `session.ingest_log` if it can preserve current
synchronous error propagation and result logging, or it may introduce a request
event such as `session.ingest_log.request.v1` if request/fanin semantics are
needed. The patch must delete the replaced direct daemon calls in the same patch
and must not fall back to the old direct call after broker/listener failure.

## Deferred Sub-Slices

Defer these until separate review:

- changing the ack-only lifecycle handlers for `session.new`, `session.reset`,
  `session.compaction`, `session.timeout`, `session.agent_start`, or
  `session.agent_end`
- moving extraction fact writes or fact-batch persistence
- changing cursor advancement, signal processing locks, staged payload flushes,
  or extraction buffer behavior
- changing session transcript chunking, microchunk linking, or recall result
  formatting
- changing `session_chunks` routed/default recall behavior
- changing adapter lifecycle delivery surfaces

Next M9.3 runtime work must begin with a focused plan for one deferred
sub-slice. Do not extend the first-slice patch into ack-only lifecycle handlers
or source metadata enrichment by follow-on code without that review.

## Next Slice Plan: Active `session.ingest_log`

The next narrow runtime slice should remove the remaining active-event direct
call from `core/runtime/events.py` without changing active event semantics.

Current remaining direct path:

- `core/runtime/events.py::_handle_session_ingest_log()` unwraps the active
  `session.ingest_log` payload and calls
  `core.ingest_runtime.run_session_logs_ingest()` directly.

Proposed ownership change:

- keep `session.ingest_log` as an active event in this slice
- delegate the storage work to the MemoryDB-owned session ingest contract path,
  reusing `core.plugins.memorydb_contract.handle_session_ingest_log_request()`
  or a small shared helper owned by that module
- preserve the existing active handler result envelope:
  - ingest result with `status` in `failed` or `error` returns
    `{"status": "failed", "result": result}`
  - successful ingest returns `{"status": "processed", "result": result}`
  - unexpected exceptions return `{"status": "failed", "error": str(exc)}`

Non-targets for this slice:

- do not change `session.ingest_log` to request delivery mode
- do not introduce a `sessiondb` manifest
- do not change daemon session-ingest callsites completed in the first slice
- do not enrich production daemon payloads with `source_channel`,
  `conversation_id`, `participant_ids`, or `participant_aliases`
- do not change lifecycle ack handlers or fact extraction writes

Required implementation tests:

- active `session.ingest_log` still produces the same processed/failed result
  shape that existing event processing expects
- active handler delegates through the MemoryDB-owned contract/helper rather
  than calling `run_session_logs_ingest()` from `core/runtime/events.py`
- handler-returned failed/error results still mark the event failed through
  `process_events()`
- failHard behavior remains owned by `process_events()` raising on failed
  handler results; the active handler must not add a fallback route
- request handler parity tests from the first slice remain passing

Review gates:

- W3 should confirm this does not alter recall-visible projection fields or
  source-window behavior.
- W6 should review the ownership boundary and result-envelope preservation.
- W8 should run `tests/test_events.py` plus the first-slice session ingest,
  session memory bridge, and extraction daemon lanes.
- W4 live smoke is only needed if runtime code changes land after this plan; it
  should verify active `session.ingest_log` processing plus the already-closed
  daemon broker path.

### Active Session-Ingest Follow-Up Closure

Completed runtime:

- `fb5e9b0f1` recorded the reviewed active `session.ingest_log` slice plan.
- `7c2522ab5` routes active `session.ingest_log` storage through the
  MemoryDB-owned shared helper `run_session_ingest_payload()`.
- `core/runtime/events.py` no longer imports or calls
  `run_session_logs_ingest()` directly.

Validation recorded:

- W3 approved the plan with recall-sensitive constraints: active delivery must
  remain active, no `sessiondb` manifest, no daemon callsite changes, no source
  metadata enrichment, and no lifecycle/fact/recall planner changes.
- W4 live PASS on R201: active and request paths converge on the shared
  MemoryDB helper; active processed/failed envelope semantics are preserved;
  failHard remains owned by `process_events()`; daemon request route remains
  unaffected.
- W6 APPROVED with no findings: shared-helper design resolved the plan decision
  and avoided active-handler-to-request-handler coupling.
- W8 static PASS and runtime hold closed: focused active/session-ingest tests,
  full event suite, session ingest/session memory bridge lanes, unit wrapper,
  py_compile, ruff, diff check, docs consistency, and route scan all passed.

Implementation note:

- The active handler keeps only active-envelope validation and wrapping in
  `core/runtime/events.py`. Payload normalization plus
  `run_session_logs_ingest()` delegation are centralized in
  `core/plugins/memorydb_contract.py`.
- Trusted active-event producer metadata (`source_channel`, `conversation_id`,
  `participant_ids`, `participant_aliases`, `message_count`, and `topic_hint`)
  remains forwarded through SessionDB rows and MemoryDB `session_chunks`.

## Event Contract Requirements

The first slice must preserve the current session ingest inputs:

- `session_id`
- `owner_id`
- `label`
- `session_file`
- `transcript_path`
- `source_channel`
- `conversation_id`
- `participant_ids`
- `participant_aliases`
- `message_count`
- `topic_hint`

Payload must not include credentials, environment dumps, unrelated transcript
bodies, or unbounded session files beyond the transcript path/source already
used by the current ingest path.

Handler response must preserve the current result fields the daemon logs and
tests assert:

- `status`
- `reason` when skipped
- `session_id`
- `source_kind`
- chunk/index counts supplied by the session-memory bridge result

## Next Decision Point: Ack-Only Lifecycle Events

The remaining M9.3 lifecycle events are currently ack-only active events:

- `session.new`
- `session.reset`
- `session.compaction`
- `session.timeout`
- `session.agent_start`
- `session.agent_end`

Current implementation:

- `core/runtime/events.py::_handle_session_lifecycle()` returns
  `{"status": "acknowledged", "event": event.get("name")}`.
- Each lifecycle capability remains `delivery_mode="active"`; this disposition
  does not convert lifecycle acknowledgements to request or passive events.
- Adapter/native aliases currently canonicalize to runtime event names before
  validation:
  - `before_agent_start` -> `session.agent_start`
  - `agent_end` -> `session.agent_end`
  - `session_end` -> `session.reset`
  - `before_compaction` -> `session.compaction`
  - `before_reset` -> `session.reset`
  - `command:new` -> `session.new`
  - `command:reset` -> `session.reset`
  - `command:restart` -> `session.reset`
  - `command:compact` -> `session.compaction`
  - `command:compaction` -> `session.compaction`
- No current handler writes SessionDB, MemoryDB, or any other datastore state.

Proposed disposition:

- Do not add datastore persistence in the ack-only lifecycle slice unless W3/W6
  first approve a concrete lifecycle persistence contract.
- Treat the current ack-only handlers as core runtime dispatch acknowledgements,
  not datastore write paths.
- If product requirements later need durable lifecycle audit/session state,
  plan that as a separate datastore-owned contract with explicit storage schema,
  replay semantics, and W4 lifecycle milestone validation.

Allowed next patch shape:

- docs-only decision record that M9.3 session write migration is complete and
  ack-only lifecycle events remain core-owned for now; or
- test-only/runtime-no-op guard coverage pinning that lifecycle aliases,
  delivery mode, and acknowledgement envelope remain unchanged.

Non-targets:

- no lifecycle datastore table or SessionDB manifest
- no change to active delivery mode
- no adapter delivery-surface changes
- no change to reset/compaction/timeout side effects
- no new persistence of transcript bodies, environment, credentials, or hook
  payloads
- no new lifecycle event names such as `session.fork` without their own
  reviewed product contract, alias mapping, and W4 lifecycle smoke plan

Required review before any runtime behavior change:

- W3 if lifecycle persistence can affect recall/session evidence or
  source-window behavior.
- W6 for ownership and envelope semantics.
- W4 for CC/CDX/OC lifecycle milestone smoke if runtime behavior changes.
- W8 for full event/static lanes.

Closure status:

- The selected M9.3 datastore write family is complete: daemon session-log
  ingest callsites route through `session.ingest_log.request.v1`, and the active
  `session.ingest_log` handler routes through the SessionDB helper after M17.
- No additional M9.3 runtime migration is selected for the ack-only lifecycle
  events in this milestone.
- `session.new`, `session.reset`, `session.compaction`, `session.timeout`,
  `session.agent_start`, and `session.agent_end` remain core-owned active
  acknowledgements.
- Future lifecycle persistence, SessionDB manifest registration, source-window
  metadata enrichment, or new lifecycle events such as `session.fork` require a
  separate reviewed plan and W4 lifecycle smoke.

## Non-Targets

- docs registration/index paths from M9.1 and M9.2
- project-doc worker updates
- docs RAG recall/search behavior
- memory fact extraction writes
- graph/vector recall ranking or result formatting
- `session_chunks` recall planner behavior
- lifecycle ack event semantics
- public CLI syntax changes

## FailHard Policy

- `failHard=true`: broker/listener validation, dispatch, transcript resolution,
  SessionDB storage, or MemoryDB projection failure must raise through the
  daemon path. Do not fall back to the removed direct call.
- `failHard=false`: the daemon may preserve current warning/log behavior, but
  the failure must be loud and must not report the session log as indexed.
- Queue/read/write failures in the event path remain failHard-sensitive per the
  existing broker contract.

## Parity Invariants

Implementation must preserve:

- no-new-content session-end behavior, including stale-signal cleanup before a
  failHard raise
- normal final flush logging and status handling
- transcript source resolution order: explicit transcript path, session file,
  adapter session path, then missing
- adapter parsing for host JSONL transcripts before SessionDB storage
- participant id and alias normalization
- source channel, conversation id, message count, and topic hint forwarding
- SessionDB transcript row shape
- MemoryDB session chunk projection and microchunk linkage
- `source_kind` and skip reason semantics
- `session_chunks` recallability for newly indexed transcripts

## Required Tests Before W4

Add or preserve focused tests proving:

- daemon no-new-content path uses the broker/event request and no longer calls
  `run_session_logs_ingest()` directly
- daemon final flush path uses the broker/event request and no longer calls
  `run_session_logs_ingest()` directly
- `session.ingest_log` or the new request handler preserves payload forwarding
  and result shape
- transcript-path JSONL still adapter-parses before SessionDB storage
- fail-soft handler failure logs loudly and does not claim indexed status
- failHard handler failure raises through the daemon path without fallback
- SessionDB transcript rows and MemoryDB session chunk projection remain
  recallable through `session_chunks`

## W4 Smoke

After W3/W6/W8 review, W4 should smoke:

- session end with no new content still processes/cleans signals correctly
- normal final flush indexes a real session transcript
- `session_chunks` recall finds newly indexed session evidence
- failHard surfaces handler validation or execution failure
- CC/CDX/OC lifecycle milestones still complete without new deferred-notice or
  compaction regressions

## Handoff Criteria

M9.3 first slice is complete only when:

- selected daemon direct session-log ingest calls are removed
- W4 confirms lifecycle/session milestones and `session_chunks` recall remain
  stable
- W3 confirms no recall-visible session evidence behavior changed, or
  explicitly approves any observed change
- W6 confirms no unrelated extraction/fact write path migrated in the same patch
- W8 static validation passes focused event, session ingest, session bridge, and
  extraction daemon suites
