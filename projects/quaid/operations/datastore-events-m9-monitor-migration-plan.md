# Datastore Events M9 Monitor Migration Plan

Status: M9 selected monitor-write migrations complete; M10 handoff criteria recorded
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Precondition

Do not implement any M9 sub-milestone until:

1. M8 completes the selected project-docs supervisor maintenance migration.
2. W4 smoke confirms the M8 listener-owned path is authoritative and stable.
3. W6 confirms no unrelated monitor/write paths were touched by M8.
4. The specific M9 sub-milestone has a focused implementation plan reviewed by
   the relevant domain owner before code lands.

This document started as a planning record and now also records closure for the
selected M9 monitor-write migrations. It does not approve new runtime work
beyond the completed slices listed below.

## M9 Goal

M9 migrates remaining monitor writes incrementally so monitors become producers
and datastores own writes.

The order is intentional risk gradient:

1. docs registration and index request events
2. project file and document changed events
3. lifecycle and session observed events
4. extraction completed and fact batch events
5. evolution, snippet, and journal events

Do not reorder without recording why and getting the relevant domain review.

## M9 Closure Status

The selected M9 monitor-write migrations are complete:

- M9.1 DocsDB registration/stale-index listener authority closed at
  `9ac2a07b3`.
- M9.2 project-doc worker apply/index request routing closed at
  `b5a4dbabe` + `959899295`, with diagnostic follow-up `2ff5aa51`.
- M9.3 session-log ingest request routing and active `session.ingest_log`
  helper convergence closed at `ce02408f2` + `e23dfc17f` and `7c2522ab5`;
  ack-only lifecycle events remain core-owned acknowledgements.
- M9.4 MemoryDB extraction fact/source publish helper and request routing closed
  through `65dbab41d` + `045883370`, `cd7cb61f7` + `98e7b21f5`, and
  `9acb2da60` + `41f4aacf8`.
- M9.5 EvolutionDB/NoteDB snippet/journal helper and request routing closed
  through `99a947426` + `7fd0771dc`, `c9aac7ab6`, and `126659a91`.

Deferred items are not M9 blockers unless separately selected by Solomon with a
new reviewed plan: direct `extract_from_transcript()` / CLI request routing,
default lifecycle-triggered transcript ingest and daemon automation,
source-window selector ownership, SessionDB ownership of `session.ingest_log`,
`datastore.notedb` / `core.plugins.notedb_contract` compatibility-alias
retirement, `notedb.core` plugin-id rename, and `.ego` integration. The
`datastore.evolutiondb` runtime package and `core.plugins.evolutiondb_contract`
module renames were completed in M10 Slice 1 and Slice 2. The extraction-side
project-log queue ownership path and the `PROJECT.log` rotation lock hardening
are implemented and tracked in
`projects/quaid/operations/project-log-single-writer-plan.md`.

Direct extraction request-mode routing is now tracked as M11 in
`projects/quaid/operations/datastore-events-m11-direct-extraction-request-plan.md`.
The M11 first Python-API-only runtime slice closed at `6eebe1a59` +
`539061237`: direct Python callers can opt into the existing MemoryDB and
EvolutionDB request modes, while defaults remain direct. Hidden CLI
operator/debug exposure closed at `6e413c648`: operators can pass the existing
request-mode controls explicitly, while normal help output and defaults remain
unchanged. Default request routing and broader deferred items remain
future-plan-gated.

M12 runtime slices closed at `e81244e32` and `3f245ba9e` + `1a92dd7c`:
EvolutionDB snippet and journal writes now have separate private helper
internals and additive snippet-only / journal-only request event surfaces behind
the existing combined extraction route.

M13 split extraction routing closed at `516732b88` + `9437788d`: explicit
`snippet_journal_write_mode="request"` extraction now routes through ordered
snippet and journal request events and aggregates back into the existing
`snippet_journal_metrics` shape. Default direct behavior and combined event
compatibility remain unchanged.

M14 SessionDB manifest metadata closed at `f0574902b` + `522f16e28`:
SessionDB is now listed as first-party manifest/contract metadata for durable
transcript/provenance ownership, while MemoryDB continues to own
`session.ingest_log.request.v1` and the `session_chunks` recall selector.
Lifecycle persistence, source-window enrichment, and SessionDB ownership of
`session.ingest_log` remain future-plan-gated.

M15 SessionDB ingest helper ownership closed at `379be9a47`: the session-ingest
payload helper internals now live in `core.plugins.sessiondb_contract`, while
`core.plugins.memorydb_contract` remains the MemoryDB-owned compatibility wrapper
for active/request session ingest. MemoryDB ownership of
`session.ingest_log.request.v1` and the `session_chunks` recall selector remains
unchanged.

M16 SessionDB request ownership closed at `40ff6c8ed` + `23c0e7228`:
`session.ingest_log.request.v1` metadata and request registration now belong to
SessionDB, while MemoryDB `session_chunks` recall ownership and source-window
behavior remain unchanged.

M17 active `session.ingest_log` import cleanup closed at `93b3561f5`: the active
handler now imports the SessionDB helper directly, while MemoryDB compatibility
wrappers and the MemoryDB `session_chunks` recall/write projection remain
unchanged.

M18 active `session.ingest_log` failHard cleanup closed at `fd7cc4b38`: the
active handler no longer catches unexpected SessionDB helper/import exceptions
locally; they now reach `process_events()` failHard machinery, while structured
failed envelopes and normal success behavior remain unchanged.

M19 SessionDB source-window metadata enrichment closed at `cf9eddd26` +
`e4c4ec0d5`: SessionDB `expand_microchunk()` now supplies provenance-only
`source_window_header` metadata for dated microchunks, while MemoryDB consumes
it under the existing source-window output policy. MemoryDB `session_chunks`
selector ownership remains unchanged, and SessionDB/source-window selector
ownership remains deferred.

M20 SessionDB lifecycle observation metadata closed at `bc58b8a06` +
`195fc7678`: ack-only lifecycle events with concrete `session_id` now persist
SessionDB metadata observations, while lifecycle events without `session_id`
remain acknowledged without persistence. Lifecycle-triggered transcript ingest,
daemon work, and lifecycle automation remain deferred.

M21 daemon lifecycle observation bridge closed at `f6b661ea0` + `b591b7d3` +
`f90602cb`: existing daemon reset/compaction/timeout/session_end signals now
record metadata-only SessionDB lifecycle observations through the M20 contract
seam, while rolling signals remain excluded. M22 explicit opt-in
lifecycle-to-daemon signal file bridge closed at `4fbecd088` + `90a0fb2de`:
lifecycle events with `payload.daemon_signal.enabled=true`, concrete
`session_id`, and a real transcript path can write existing daemon signals
through `core.extraction_daemon.write_signal()`, while plain lifecycle events
remain acknowledgement plus observation only. Default lifecycle-triggered
transcript ingest, new daemon automation, and recall/source-window policy
changes remain deferred.

M23 SessionDB ingest wrapper retirement closed at `bfe5836b` + `4a3824d88`:
the obsolete `core.plugins.memorydb_contract` wrappers for SessionDB-owned
`session.ingest_log` helper/handler/registrar were removed, while MemoryDB
`session_chunks` recall/write ownership, SessionDB request ownership, active
ingest behavior, source-window policy, lifecycle/daemon behavior, and non-session
MemoryDB contract surfaces remain unchanged. M24 default terminal
`session.agent_end` lifecycle-to-daemon `session_end` signal bridge closed at
`058737670`: plain terminal agent-end lifecycle events with concrete `session_id`
and real `payload.transcript_path` now write the existing daemon `session_end`
signal through `core.extraction_daemon.write_signal()`, while M22 explicit
opt-in remains canonical. M25 default `session.timeout`
lifecycle-to-daemon `timeout` signal bridge closed at `32ba63569`: plain timeout
lifecycle events with concrete `session_id` and real `payload.transcript_path`
now write the existing daemon `timeout` signal through
`core.extraction_daemon.write_signal()`. M26 default `session.compaction`
lifecycle-to-daemon `compaction` signal bridge closed at `2f35f279`: plain
compaction lifecycle events with concrete `session_id` and real
`payload.transcript_path` now write the existing daemon `compaction` signal
through `core.extraction_daemon.write_signal()`, while M22 explicit opt-in,
M24 agent-end, and M25 timeout precedence remain canonical. M27 default
`session.reset` lifecycle-to-daemon `reset` signal bridge closed at
`acd05eaab`: plain reset lifecycle events with concrete `session_id` and real
`payload.reset_transcript_path` now write the existing daemon `reset` signal
through `core.extraction_daemon.write_signal()`, while live
`payload.transcript_path` remains ack+observation only and OpenClaw reset hooks
plus daemon reset backup/cursor ownership remain unchanged. M28 event-bus
lifecycle signal wake/start parity closed at `5152a928`: after an existing M22
explicit or M24-M27 default lifecycle bridge writes a compatible daemon signal,
the event-bus path now wakes/starts the daemon only through
`core.extraction_daemon.ensure_alive()`. M29 facade compaction lifecycle emitter
closed at `a4a4d4238`: facade `processLifecycleEvent()` may emit only
`session.compaction` for explicit `CompactionSignal` inputs with concrete session
id and existing transcript path through the existing `emitEvent`/`execEvents`
immediate path, while M26 remains the daemon compaction signal writer and M28
remains the daemon wake owner. Reset, timeout, agent-end emitter wiring,
OpenClaw hook migration, daemon restart/stop automation, and
recall/source-window policy changes remain deferred.

## Shared Rules For Every M9 Slice

- One write path family per sub-milestone.
- Delete the replaced direct path in the same implementation patch unless
  Solomon/Hermes approves a real alpha-user compatibility shim with an owner and
  removal condition.
- No shadow-mode or dual-run code may remain after the slice is complete.
- Preserve operator-visible CLI/status output unless the sub-milestone plan
  explicitly approves a change.
- Preserve `failHard=true` behavior: listener/emission failures raise instead
  of falling back to the removed direct path.
- Use W4 validation before moving to the next sub-milestone.
- If three patches in a row chase the same event-delivery class of bugs, stop
  and revisit the architecture before continuing.

## M9.1 Docs Registration And Index Request Events

Scope:

- docs registration/index monitor writes after M8's selected supervisor path
- docs registry/index effects only

Out of scope:

- project file/document change events
- lifecycle/session events
- extraction/fact writes
- evolution/snippet/journal writes
- docs RAG search/recall output changes unless explicitly reviewed

Required review:

- W6 implementation review
- W8 static validation
- W3 review if docs recallability, indexing cadence, row metadata, or result
  shape can change
- W4 validation covering docs registration and recallability

## M9.2 Project File And Document Changed Events

Tracking doc: `datastore-events-m9-2-project-doc-change-plan.md`.

Status:

- Project-doc worker selected apply/index request slice complete at
  `b5a4dbabe` + `959899295`.
- Known failHard warning-order diagnostic gap closed at `2ff5aa51`.
- W4 final live PASS, W6 review closure, and W8 static/runtime closure are
  recorded for the consolidated M9.2 head.
- `docs.project_update.request.v1` is implemented for the selected
  project-doc worker apply/index operation. Project-log queue commit ownership
  and broader worker lifecycle/state ownership remain deferred/non-target.

Scope:

- project file/document change monitor writes
- project docs monitor freshness semantics and status output

Out of scope:

- worker `execute_update_once` internals unless explicitly selected
- docs RAG recall/search behavior
- lifecycle/session and extraction paths

Required validation:

- focused project-docs monitor tests
- W4 validation covering project-doc updates
- W3 review if any recall-visible docs behavior can change

## M9.3 Lifecycle And Session Observed Events

Tracking doc: `datastore-events-m9-3-lifecycle-session-plan.md`.

Status:

- First session-log ingest slice complete at `ce02408f2` + `e23dfc17f`.
- Active `session.ingest_log` follow-up complete at `7c2522ab5`.
- W4 live PASS, W6 review, and W8 static/runtime closure recorded for both
  session-log ingest slices.
- Ack-only lifecycle disposition recorded at `1e84e60ed` + `e329d13b1`: the
  remaining lifecycle events stay core-owned active acknowledgements unless a
  future W3/W6-reviewed persistence contract selects new runtime behavior.
- No additional M9.3 runtime migration is selected in this milestone.

Scope:

- lifecycle/session observed writes
- session cursor, reset, compaction, timeout, and close/finalize behavior

Out of scope:

- extraction fact writes
- docs registration/index
- adapter-specific delivery surfaces except where needed to preserve the same
  lifecycle event inputs

Required validation:

- focused lifecycle/session tests
- W4 validation across CC/CDX/OC lifecycle milestones

## M9.4 Extraction Completed And Fact Batch Events

Tracking doc: `datastore-events-m9-4-extraction-fact-plan.md`.

Status:

- First synchronous helper slice complete at `65dbab41d` + `045883370`.
- Pre-slice publish-default seam cleanup complete at `cd7cb61f7` + `98e7b21f5`.
- Daemon final rolling flush request-event slice complete at `9acb2da60` +
  `41f4aacf8`.
- W4 live/source-proof PASS, W3 recall PASS/no findings where required, W6
  concerns closed, and W8 static/runtime closure recorded for all completed
  M9.4 runtime pairs.
- `memory.extraction_publish.request.v1` is implemented for the selected daemon
  final rolling flush producer. Direct `extract_from_transcript()` / CLI request
  routing remains deferred.

Scope:

- extraction completed/fact batch writes behind datastore listeners
- memory facts, edges, source/session evidence projections, provenance, and
  dedup behavior for the selected extraction write family

Out of scope:

- unrelated docs monitor writes
- evolution/snippet/journal writes unless explicitly part of the selected
  extraction batch contract

Required validation:

- focused extraction and memory write tests
- W3 benchmark smoke if recall data shape changes materially
- W4 validation covering extraction milestones

## M9.5 Evolution, Snippet, And Journal Events

Tracking doc: `datastore-events-m9-5-evolution-snippet-journal-plan.md`.

Status:

- Draft plan opened for EvolutionDB-owned snippet/journal write migration.
- First synchronous helper seam complete at `99a947426` + `7fd0771dc`, with
  governance clarification recorded at `8e28a8d68`.
- W4 live/source-proof PASS, W3 recall/identity-context PASS, W6
  approved-with-concerns documented, and W8 static/runtime closure recorded.
- Request-event routing for snippet/journal writes is complete at `c9aac7ab6`
  with test-only diagnostic coverage at `126659a91`.
- W4 live PASS, W3 recall/identity-context PASS, W6 review closure, and W8
  static/runtime closure are recorded for the request-event pair.
- `evolution.snippet_journal_write.request.v1` is implemented for the selected
  daemon final rolling flush producer. Direct `extract_from_transcript()` / CLI
  request routing remains deferred.

Scope:

- evolution/snippet/journal writes behind `evolutiondb` listeners
- janitor distillation and identity-file update behavior for the selected path

Out of scope:

- runtime `datastore.notedb` module rename; that was out of M9 scope and later
  completed in M10 Slice 1
- `.ego` export/import behavior
- memory/docs write migrations from earlier M9 slices

Required validation:

- focused evolution/snippet/journal tests
- W4 validation covering identity/context lifecycle

## M10 Handoff Criteria

M9 is ready to hand off to M10 only when:

- each M9 sub-milestone has passed W4 validation before the next one starts
- no migrated monitor path retains its old direct production write path
- W6 confirms the series did not accumulate a B032-style symptom chain
- M10 runtime rename planning starts in
  `projects/quaid/operations/datastore-events-m10-evolutiondb-rename-plan.md`

Post-M10 disposition: M10 Slice 1 and Slice 2 completed the runtime package and
contract-module rename while retaining installed-alpha compatibility aliases.
Alias retirement and the `notedb.core` plugin-id rename remain future,
operator-gated work.
