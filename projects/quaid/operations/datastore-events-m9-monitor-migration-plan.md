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
lifecycle persistence, SessionDB first-party manifest registration,
source-window metadata enrichment,
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
