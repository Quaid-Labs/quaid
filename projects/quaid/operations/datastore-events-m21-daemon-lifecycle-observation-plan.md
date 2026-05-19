# Datastore Events M21 Daemon Lifecycle Observation Plan

Status: runtime observation slice complete; lifecycle automation deferred
Owner: W1 daemon/datastore, W6 boundary review, W3 recall guard review
Plan source: `projects/quaid/operations/datastore-events-m20-sessiondb-lifecycle-metadata-plan.md`

## Precondition

Runtime code for M21 was gated on:

1. M20 SessionDB lifecycle observation metadata is closed through W4/W3/W6/W8.
2. W6 reviews the daemon-to-SessionDB boundary because this slice lets the
   daemon record lifecycle observations through the M20 SessionDB contract seam.
3. W3 reviews the recall/source-window boundary because daemon signal processing
   also owns transcript extraction and MemoryDB `session_chunks` projection.
4. W8 confirms static coverage includes extraction daemon signal processing,
   lifecycle event processing, SessionDB observation persistence, session ingest
   active/request paths, source-window guards, and boundary checks.
5. W4 is ready to live-check that daemon lifecycle signal processing still
   extracts and projects session evidence exactly as before.

This document records the completed narrow daemon lifecycle observation bridge only. It does
not approve new daemon signals, lifecycle-triggered transcript ingest beyond the
existing daemon signal paths, new event names, request/default routing changes,
SessionDB recall selectors, source-window selector ownership, MemoryDB
compatibility-wrapper removal, CLI behavior changes, `.ego` integration, public
push, or release actions.

## Goal

M20 records SessionDB lifecycle observations for lifecycle events that already
flow through `core.runtime.events._handle_session_lifecycle()`. The daemon has a
parallel lifecycle signal path: adapters and idle-session checks write extraction
signals such as `reset`, `compaction`, `timeout`, and `session_end`; the daemon
processes those signals directly to extract transcript content and project
MemoryDB `session_chunks` evidence.

M21 selected the first bridge between those existing daemon lifecycle signals and
M20's SessionDB lifecycle observation table. When the daemon is already handling
a concrete lifecycle signal for a concrete `session_id`, it records a metadata
observation through `core.plugins.sessiondb_contract.record_session_lifecycle_observation()`.
The daemon's extraction behavior, cursor behavior, signal lifecycle, session
logs ingest, recall output, and source-window behavior remain unchanged.

This is not lifecycle automation. M21 did not create new daemon work from
lifecycle observations, did not enqueue extra extraction signals, and did not
change which transcripts are extracted.

## Current Boundary

Pre-M21 path:

1. `core.runtime.events._handle_session_lifecycle()` records M20 lifecycle
   observations for event-bus lifecycle events with `session_id`.
2. `core.extraction_daemon.write_signal()` and adapter hook paths write daemon
   signal files for `reset`, `compaction`, `timeout`, `session_end`, and
   `rolling`.
3. `core.extraction_daemon.process_signal()` processes those files directly,
   manages cursors/rolling buffers, extracts transcript deltas, publishes through
   request-mode datastore paths, and requests SessionDB session-log ingest.
4. Daemon lifecycle signals were not recorded as M20 lifecycle
   observations unless another caller also emits a matching runtime event.
5. MemoryDB remains the owner of `session_chunks` recall/write projection and
   final source-window output policy. SessionDB `capabilities.recall=[]`.

## Selected First Slice: Daemon Signal Observation Metadata Only

Implemented one runtime metadata slice only:

1. Add a small daemon-local helper, for example
   `_record_daemon_lifecycle_observation(signal_data, *, session_id,
   signal_type, transcript_path, label)`, that builds an event-like dict and
   calls `core.plugins.sessiondb_contract.record_session_lifecycle_observation()`.
   Helper placement: keep `_record_daemon_lifecycle_observation()` as a private
   module-level function in `core.extraction_daemon` near the existing signal
   processing code. Do not create a new dedicated helper module for this single
   function. The daemon must call through the core plugin contract. Do not import
   `datastore.sessiondb.session_store` directly from `core.extraction_daemon`.
2. Record observations only for lifecycle signal types that already represent
   terminal or lifecycle boundaries: `reset`, `compaction`, `timeout`, and
   `session_end`. Do not record observations for `rolling` signals in this
   slice; rolling is a streaming extraction implementation detail, not a
   lifecycle boundary.
3. Map daemon signal types to existing lifecycle event names only:
   `reset -> session.reset`, `compaction -> session.compaction`,
   `timeout -> session.timeout`, and `session_end -> session.agent_end`.
   Mapping definition: implement this as a module-level constant such as
   `DAEMON_SIGNAL_TO_LIFECYCLE_EVENT = {"reset": "session.reset",
   "compaction": "session.compaction", "timeout": "session.timeout",
   "session_end": "session.agent_end"}` so tests can assert the mapping
   directly. `rolling` must be absent from the constant; absence is the test
   invariant for excluded signal types. Do not introduce a `session.session_end`
   or daemon-specific event name.
4. Preserve idempotency with a stable event id derived from existing daemon
   signal identity, preferably the signal file basename from `_signal_path` plus
   the signal type and session id. Event id composition: use a
   prefix-disambiguated daemon id such as
   `daemon-signal:{signal_file_basename}:{signal_type}:{session_id}`. The
   `daemon-signal:` prefix distinguishes daemon-origin observations from
   event-bus-origin observations, which use the event-bus event id directly, and
   prevents accidental primary-key collisions. Reprocessing the same signal file
   must not create duplicate lifecycle observation rows. Do not generate a new
   observation UUID at record time; use the already-existing signal identity.
5. Record compact metadata only: daemon signal type, transcript path, adapter,
   source, reason/meta fields already present on the signal, and enough
   provenance to identify that the observation came from daemon signal
   processing. Do not store transcript text, extracted facts, recall rows, or
   source-window rows in the lifecycle observation metadata.
6. Place the recording only after signal validation has confirmed a concrete
   `session_id`, selected lifecycle signal type, and active-instance transcript
   ownership. The call should happen before the existing `mark_signal_processed()`
   finalization for the selected signal so success and no-payload lifecycle
   paths can be observed, but it must not change cursor offsets, signal priority,
   lock behavior, rolling buffer state, retry behavior, or whether a signal is
   marked processed.
7. If observation recording succeeds, daemon logs may include a debug/info line,
   but operator-visible daemon status and extraction results must remain
   unchanged.
8. Preserve M20 runtime event behavior. `_handle_session_lifecycle()` remains the
   event-bus lifecycle handler and its missing-session/failHard behavior is not
   changed by this slice.

## Non-Targets

- no new event names or signal types
- no lifecycle-triggered transcript ingest beyond daemon signal processing that
  already exists before M21
- no extra daemon signal enqueueing, no daemon scheduling change, and no signal
  priority/order change
- no cursor, rolling buffer, stale-sweep, timeout classifier, reset backup, or
  transcript ownership behavior change
- no change to `session.ingest_log` active/request payloads or result envelopes
- no request broker ownership or response-shape change
- no change to MemoryDB `session_chunks` recall/write ownership
- no SessionDB recall selector or source-window selector ownership
- no source-window selection, ranking, planner, token-budget, or output-ordering
  change
- no removal, warning, or deprecation from MemoryDB compatibility wrappers
- no CLI/default-routing behavior change
- no SessionDB transcript table migration beyond existing M20 lifecycle
  observation rows
- no `.ego` import/export integration
- no compatibility-alias retirement or `notedb.core` plugin-id rename

## FailHard Policy

- `failHard=true`: if daemon lifecycle observation recording is selected for a
  concrete lifecycle signal and SessionDB persistence fails, the failure must
  raise through the existing daemon signal-processing failHard path. Do not mark
  the observation as persisted or silently continue under failHard.
- `failHard=false`: observation recording failures may preserve the previous
  daemon extraction/signal behavior, but must log loudly and must not claim the
  observation was stored.
- Missing `session_id` or non-lifecycle signal type is not a persistence failure
  in this slice. It is an out-of-scope signal and must not call the SessionDB
  helper.
- Do not wrap SessionDB observation recording and unrelated signal finalization,
  cursor advancement, or extraction publication in a shared broad `try`/`except`
  that could convert selected persistence failures into silent signal success
  under failHard.
- Do not fall back to writing lifecycle observations directly through
  `datastore.sessiondb.session_store` if the plugin contract helper fails.
- Boundary check: M21 runtime must not add `core/extraction_daemon.py` to the
  `scripts/check-boundaries.py` datastore composition allowlist. The daemon
  routes through `core/plugins/sessiondb_contract.py`, which is already
  allowlisted from M20. A boundary-check pass is the required guard.

## Required Tests Before W4

Add or preserve focused tests proving:

- Daemon processing for `reset`, `compaction`, `timeout`, and `session_end`
  records SessionDB lifecycle observations through
  `core.plugins.sessiondb_contract.record_session_lifecycle_observation()` with
  the mapped event names and deterministic event ids.
- `rolling` signal processing does not record lifecycle observations.
- Reprocessing the same daemon signal id/path is idempotent and does not create
  duplicate lifecycle observation rows.
- The daemon does not import `datastore.sessiondb.session_store` directly; source
  assertions or boundary checks prove it calls through `core.plugins.sessiondb_contract`.
  The runtime diff does not add `core/extraction_daemon.py` to the boundary
  allowlist.
- Missing-session or malformed lifecycle signals remain handled by the existing
  daemon signal validation paths and do not call the SessionDB observation
  helper.
- Under `failHard=true`, selected observation persistence failures raise through
  the existing daemon failHard path with the original exception chained.
- Under `failHard=false`, selected observation persistence failures log loudly
  while preserving the pre-M21 daemon signal/extraction behavior.
- Existing daemon signal tests for reset backup handling, timeout classification,
  rolling flushes, stale-sweep recovery, session-log ingest request routing, and
  signal prioritization still pass.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- M19 source-window metadata tests still pass; daemon lifecycle observations must
  not affect source-window output.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow daemon lifecycle smoke:

- A real or synthetic daemon `session_end` or `reset` signal for a concrete
  session still extracts/projection-ingests exactly as before and records one
  SessionDB lifecycle observation row.
- Replaying the same signal does not duplicate observation rows.
- A rolling signal or missing-session signal does not record a lifecycle
  observation.
- `session.ingest_log` active and `session.ingest_log.request.v1` request paths
  still ingest transcripts and project MemoryDB `session_chunks` evidence.
- M19 source-window recall for dated session evidence still renders the same
  `source_date: <date>` context header.

## Deferred Decisions

- explicit opt-in lifecycle-to-daemon signal file bridge closed in M22 at
  `4fbecd088` + `90a0fb2de`; default lifecycle-triggered transcript ingestion
  and daemon automation remain deferred
- request/active compatibility-wrapper removal from `core.plugins.memorydb_contract`
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration

## Implementation Record

Runtime closed in `f6b661ea0` (`refactor(datastore): observe daemon
lifecycle signals in SessionDB`) with failHard/test follow-up `b591b7d3`
(`test(datastore): tighten M21 daemon failHard guards`) and hardening follow-up
`f90602cb` (`fix(datastore): harden M21 daemon observation guards`).

Implemented behavior:

- `core.extraction_daemon.DAEMON_SIGNAL_TO_LIFECYCLE_EVENT` maps existing daemon
  lifecycle signal types to existing M20 lifecycle event names:
  `reset -> session.reset`, `compaction -> session.compaction`,
  `timeout -> session.timeout`, and `session_end -> session.agent_end`.
  `rolling` remains absent and is not recorded as a lifecycle observation.
- `core.extraction_daemon._record_daemon_lifecycle_observation()` builds compact
  event-like metadata and calls
  `core.plugins.sessiondb_contract.record_session_lifecycle_observation()`.
  The daemon does not import `datastore.sessiondb.session_store` directly for
  M21 observation writes and did not add `core/extraction_daemon.py` to the
  boundary allowlist.
- Daemon-origin observation ids are deterministic and prefix-disambiguated:
  `daemon-signal:{signal_file}:{signal_type}:{session_id}`. Reprocessing the
  same signal id/path is idempotent through the M20 `event_id` primary key and
  `INSERT OR IGNORE` behavior.
- Observation recording is placed before selected signal finalization side
  effects. `_finalize_no_payload_signal()` records before timeout markers,
  cursor writes, rolling-state clears, noop metrics, `mark_signal_processed()`,
  and lock release. The full-flush `process_signal()` path also records before
  final cursor/signal finalization.
- Under `failHard=true`, selected daemon observation persistence failures raise
  `RuntimeError("SessionDB daemon lifecycle observation failed while failHard is enabled")`
  with the original exception chained as `__cause__`; finalization side effects
  do not run after the failHard observation error. Under `failHard=false`, the
  daemon logs loudly and returns `persisted=False` for the observation attempt
  while preserving pre-M21 signal/extraction behavior.
- `datastore.sessiondb.session_store._session_db_path()` centralizes SessionDB
  parent-directory creation and all SessionDB connection entry points now use
  it, including lifecycle observation recording, lifecycle listing, transcript
  ingest/indexing, microchunk fetch/attach, source-window expansion, recent
  session listing, session loading, and lifecycle lock-path creation.
- `lib.config._workspace_root()` now logs a warning before falling back to
  `QUAID_HOME/instances/QUAID_INSTANCE` when an adapter lacks
  `instance_root()`, so the daemon/test compatibility path is observable.
- M20 event-bus lifecycle handling, M16 request ownership, M17/M18 active
  session ingest routing/failHard behavior, M19 source-window metadata,
  MemoryDB compatibility wrappers, MemoryDB `session_chunks` recall/write
  ownership, and SessionDB `capabilities.recall=[]` are unchanged.

Test coverage:

- Mapping tests assert the exact `DAEMON_SIGNAL_TO_LIFECYCLE_EVENT` constant and
  rolling absence.
- Helper tests assert prefix-disambiguated event ids, mapped lifecycle event
  names, plugin-contract routing, compact payload/provenance fields, missing
  session and rolling skip behavior, idempotent duplicate signal handling, and
  fail-soft/failHard persistence behavior.
- Ordering tests assert no-payload finalization records before timeout marker,
  cursor write, signal processed marker, and lock release, and assert
  failHard observation errors prevent those finalization side effects.
- Integration coverage drives `process_signal()` through a full compaction flush
  and asserts exactly one daemon lifecycle observation is attempted with the
  expected session id, signal type, and transcript path.
- Store/config hardening tests assert SessionDB connection entry points route
  through the parent-directory guard and the adapter-without-`instance_root()`
  fallback logs loudly.
- Existing extraction daemon, session lifecycle, SessionDB registry, M19
  source-window/session bridge, active/request ingest, boundary, ruff,
  py_compile, and unit-wrapper lanes continue to pass.

Validation record:

- W4 R201 live/source-proof PASS on `f6b661ea0`, `b591b7d3`, and `f90602cb`:
  daemon observations route through the SessionDB contract, event ids keep the
  `daemon-signal:` format, rolling remains excluded, observation ordering is
  before signal finalization, failHard wraps and chains original exceptions,
  fail-soft logs and continues, SessionDB parent-dir creation is centralized,
  and daemon stability remained clean.
- W3 runtime/recall APPROVED with no findings for the latest stack: daemon
  lifecycle observations are metadata-only, MemoryDB `session_chunks` ownership
  and final source-window policy are unchanged, SessionDB recall remains `[]`,
  M19 `source_window_header` behavior is untouched, and no selector, ranking,
  planner, token-budget, or output-ordering drift was found.
- W6 APPROVED after `f90602cb` closed the parent-directory guard scope concern,
  loud fallback warning, process-signal integration coverage gap, and duplicate
  signal-file derivation. The remaining B017/textual and pre-existing boundary
  allowlist notes were accepted as informational.
- W8 static PASS for the latest stack: focused daemon lifecycle/sessiondb
  observation selectors, full `tests/test_extraction_daemon.py`, adjacent
  lifecycle/source-window lanes, py_compile, ruff, diff/docs checks, boundary
  checks, and the unit wrapper all passed.
