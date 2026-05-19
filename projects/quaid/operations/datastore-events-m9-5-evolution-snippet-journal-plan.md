# Datastore Events M9.5 Evolution Snippet Journal Plan

Status: first synchronous helper seam complete; request-event slice planned
Owner: W1 runtime/datastore, W3 recall and identity-context review
Plan source: `projects/quaid/operations/datastore-events-m9-monitor-migration-plan.md`

## Precondition

Do not implement runtime code for M9.5 until:

1. M9.4 extraction publish request routing is closed through W4/W3/W6/W8.
2. W3 reviews the selected slice because journal content and identity-context
   markdown can be recall-visible and user-visible.
3. W6 reviews the ownership boundary so extraction remains the producer and
   EvolutionDB/NoteDB owns snippet and journal persistence.
4. W8 confirms focused static lanes cover extraction orchestration,
   EvolutionDB/NoteDB writes, event contracts, and boundary checks.

This document records the proposed migration sequence only. It does not approve
runtime code.

## Goal

M9.5 should move the selected snippet and journal write family behind
EvolutionDB ownership while preserving normal product behavior.

The selected write family is:

- pending identity snippets written to `*.snippets.md`
- journal entries written to `journal/*.journal.md`
- write counters, duplicate-skip behavior, target filenames, and failure
  diagnostics for those files

Extraction remains the producer of structured snippet and journal payloads.
EvolutionDB owns how those payloads are persisted into the current NoteDB
markdown implementation.

## Current Boundary

Current extraction publish path:

1. `ingest.extract.extract_from_transcript()` builds `raw_snippets` and
   `raw_journal` while extracting facts.
2. `ingest.extract.apply_extracted_payloads()` routes MemoryDB facts through the
   M9.4 MemoryDB helper/request path.
3. The same function normalizes snippet and journal payloads and directly calls
   `core.lifecycle.soul_snippets.write_snippet_entry()` and
   `core.lifecycle.soul_snippets.write_journal_entry()`.
4. `core.lifecycle.soul_snippets` is a thin wrapper over
   `datastore.notedb.soul_snippets`.
5. `datastore.notedb.soul_snippets` owns the actual markdown file writes,
   duplicate detection, journal archiving, snippet review, journal
   distillation, and janitor routine registration.

Current EvolutionDB registry state:

- `evolutiondb` is the canonical datastore id.
- The runtime module remains `datastore.notedb.soul_snippets`.
- `notedb` remains a runtime alias.
- The first-party contract currently declares journal recall, datastore
  validate/explain, and maintenance request handlers, but no snippet/journal
  write request.

## Design Constraints

- Do not rename `datastore.notedb` modules in M9. Runtime package renaming waits
  for the dedicated M10 rename milestone.
- Do not wrap all of `apply_extracted_payloads()` as an EvolutionDB operation.
  MemoryDB fact publish and DocsDB project-log queueing are separate owners.
- Do not move snippet or journal persistence into MemoryDB.
- Do not move project-log queueing into EvolutionDB.
- Do not change `.ego` import/export behavior.
- Do not change journal recall ranking, scoring, source-window policy, or
  planner behavior.
- Do not change snippet review or journal distillation behavior unless the
  selected runtime slice explicitly targets that maintenance path.
- Preserve current file formats, target paths, duplicate-skip semantics, trigger
  labels, date handling, and identity-file projection behavior.

## Selected First Slice

The preferred first runtime slice is a synchronous EvolutionDB-owned helper
seam, not a request event.

Implementation shape:

- Add an EvolutionDB contract helper in `core.plugins.notedb_contract`.
- Delegate from that helper to `datastore.notedb.soul_snippets`.
- Route `apply_extracted_payloads()` snippet and journal writes through the
  contract helper instead of directly loading `core.lifecycle.soul_snippets`.
- Keep `apply_extracted_payloads()` as the orchestration entrypoint.
- Keep MemoryDB publish and DocsDB project-log side effects in their current
  owners and order.
- Keep `core.lifecycle.soul_snippets` available for lifecycle callers while the
  selected extraction write path moves to the EvolutionDB contract seam.

This mirrors the successful M9.4 pattern: establish a datastore-owned helper and
prove parity before introducing a broker/request event.

## First-Slice Design Pins

The first runtime slice uses the existing `core.plugins.notedb_contract` module
name intentionally. `evolutiondb` is the canonical datastore id, but the runtime
package remains `datastore.notedb` until M10. Keeping the contract module aligned
with the runtime package avoids a misleading half-rename in M9. A future M10
rename may introduce `evolutiondb_contract` or move the module when the runtime
package rename happens.

`core.lifecycle.soul_snippets` remains available for lifecycle callers in the
first slice. That is a partial migration by design:

- selected extraction snippet/journal writes route through the EvolutionDB
  contract seam
- existing lifecycle/janitor callers keep using the lifecycle wrapper
- snippet review and journal distillation maintenance registration remains in
  `datastore.notedb.soul_snippets`

Moving lifecycle callers to the same contract seam is deferred unless a later
reviewed M9.5 slice selects that maintenance path. The helper-first extraction
slice must not alter janitor routine registration.

The selected extraction slice may dispatch snippets and journal entries through
one combined helper call. That call must preserve the existing sequence inside
the helper: snippet writes first, journal writes second. The extraction
orchestrator may also attach an additive `snippet_journal_metrics` key to its
result for operator/debug counters, provided all existing result keys and
side-effect ordering remain unchanged.

## Candidate Helper Contract

Candidate helper:

- `core.plugins.notedb_contract.run_snippet_journal_write_payload(payload)`

Candidate payload:

- `source`: expected producer, initially `extraction-apply-payloads`
- `owner_id`
- `session_id`
- `label`
- `trigger`
- `date_str`
- `time_str`
- `snippets`: mapping of filename to list of snippet strings
- `journal`: mapping of filename to journal text
- `write_snippets`
- `write_journal`
- `dry_run`

Candidate response:

- `status`: `ok` or `failed`
- `snippet_files_seen`
- `snippet_items_seen`
- `snippet_files_written`
- `snippet_items_written`
- `snippet_files_skipped`
- `journal_files_seen`
- `journal_files_written`
- `journal_files_skipped`
- `target_files`: object with `snippets` and `journal` arrays of logical
  target filenames, for example
  `{"snippets": ["SOUL.snippets.md"], "journal": ["SOUL.journal.md"]}`
- `errors`

The response should expose only the counters and target metadata the extraction
orchestrator or operator diagnostics consume. Internal markdown parsing,
archive, and janitor mechanics stay covered by EvolutionDB/NoteDB tests rather
than becoming response-envelope validation.

## Future Request Event Slice

The next proposed runtime slice is a request event for the selected extraction
snippet/journal write path. This section is a plan only; it does not approve
runtime code until W3/W6/W8 review closes.

Candidate event:

- `evolution.snippet_journal_write.request.v1`

Candidate owner:

- EvolutionDB, via `core.plugins.notedb_contract` and the existing
  `datastore.notedb.soul_snippets` implementation.

Candidate producer:

- `ingest.extract.apply_extracted_payloads()` only, for the selected extraction
  snippet/journal write path when an explicit request mode is selected.

Implementation shape:

- Add the request event to the core runtime event registry with
  `delivery_mode="request"` and `fireable=True`.
- Add the handler spec to the EvolutionDB/NoteDB contract and datastore
  manifest under the existing `evolutiondb` owner.
- Register the request handler from `core.plugins.notedb_contract`.
- Keep the handler thin: validate the request envelope, then delegate to the
  existing `run_snippet_journal_write_payload()` helper.
- Keep the helper behind the current `core.lifecycle.soul_snippets` seam. The
  request handler must not import `datastore.notedb` directly.
- Add an explicit `snippet_journal_write_mode` (or equivalently named) argument
  to `apply_extracted_payloads()`, defaulting to `direct`.
- In direct mode, preserve the current synchronous helper path for
  `extract_from_transcript()` / CLI callers.
- In request mode, `apply_extracted_payloads()` should send exactly one broker
  request and validate the response before continuing to project-log queueing
  and `publish_complete`.

Request-slice rules:

- The request handler must delegate to the same helper selected above.
- `apply_extracted_payloads()` should keep an explicit mode flag if broker
  routing is added. Do not use environment sniffing or daemon direct broker
  bypass.
- Broker, handler, validator, or markdown write failure must not fall back to
  direct NoteDB writes.
- Direct `extract_from_transcript()` / CLI behavior should remain direct unless
  a separate reviewed slice explicitly selects request routing for it.
- The response validator should check only the fields the extraction
  orchestrator consumes: `status`, snippet/journal counters, `target_files`,
  and `errors`. Do not validate internal markdown archive mechanics, duplicate
  parser state, or filesystem implementation details in the broker envelope.
- `target_files` remain logical target filenames, not physical filesystem paths.
- FailHard warning discipline applies to validator failures as well as writer
  failures: warn before raising so operators see the broker/handler failure
  reason in live logs.
- The request path may return `snippet_journal_metrics` to the orchestrator, but
  it must remain additive and must not replace existing `result["snippets"]` or
  `result["journal"]` shapes.

Request-slice non-targets:

- no `datastore.notedb` package rename
- no split into separate snippet-only and journal-only events in this slice
- no direct daemon broker bypass around `apply_extracted_payloads()`
- no request routing for direct `extract_from_transcript()` / CLI callers
- no change to lifecycle/janitor callers of `core.lifecycle.soul_snippets`
- no snippet review or journal distillation maintenance routing changes
- no MemoryDB fact publish, DocsDB project-log, session ingest, lifecycle ack,
  recall planner, ranking, scoring, or source-window changes

Required tests before W4:

- event registry, datastore manifest, and contract handler spec expose
  `evolution.snippet_journal_write.request.v1` under EvolutionDB/NoteDB.
- direct mode still calls the synchronous helper path and keeps CLI/direct
  behavior unchanged.
- request mode calls the broker once and does not fall back to direct helper
  writes after broker, handler, validator, or writer failure.
- request/direct parity for snippet and journal file contents, including
  duplicate inputs.
- request/direct parity for `snippet_journal_metrics`, logical `target_files`,
  and existing `result["snippets"]` / `result["journal"]` shapes.
- failHard broker/handler/validator failure warns before raising and preserves
  exception identity or contextual cause as appropriate.
- fail-soft write failure logs, reports `status="failed"` or populated
  `errors`, and does not claim written counters.
- snippet writes still happen before journal writes inside the helper.
- `publish_complete` still fires after snippet, journal, and project-log side
  effects.
- W3-selected journal recall or identity-context checks still see the same
  persisted content after request-mode writes.

W4 smoke requirements:

- installed extraction writes at least one snippet and one journal entry through
  `evolution.snippet_journal_write.request.v1`.
- the expected `*.snippets.md` and `journal/*.journal.md` files are visible in
  the installed runtime home with the same content as the direct helper path.
- duplicate snippet/journal payloads do not create duplicate visible entries.
- failHard request-mode write failure stops without direct fallback.
- identity/context lifecycle still reads the written content as before.
- M9.2 DocsDB, M9.3 session ingest, and M9.4 MemoryDB extraction publish routes
  remain healthy.

## Non-Targets

- no `datastore.notedb` package rename
- no `.ego` export/import behavior
- no MemoryDB fact, edge, source-chunk, dedup, ranking, recall, or source-window
  changes
- no DocsDB project-log queueing changes
- no session ingest changes
- no lifecycle ack persistence
- no snippet review model prompt changes
- no journal distillation model prompt changes
- no janitor scheduling changes unless a later selected slice targets
  maintenance routing
- no new durable schema for snippets or journal entries
- no public/user CLI output change unless explicitly reviewed

## FailHard Policy

- `failHard=true`: helper, request dispatch, handler validation, markdown file
  read/write, archive, duplicate-check, or projection failure must raise through
  the caller. Do not route around the selected EvolutionDB write path.
- `failHard=false`: degraded behavior may remain only where current behavior
  already degrades, and it must log loudly without claiming snippets or journal
  entries were written.
- Warnings must be emitted before failHard raises so live operators can see the
  failing filename, trigger, and exception reason.
- Exceptions should preserve identity with bare `raise` where current code
  relies on it, or use contextual `RuntimeError(... ) from exc` where the helper
  boundary needs a stable message.

## Parity Invariants

Implementation must preserve:

- target path resolution for `SOUL.md`, `USER.md`, `ENVIRONMENT.md`, and
  `*.snippets.md`
- journal path resolution under `journal/*.journal.md`
- snippet duplicate detection and skip behavior
- journal duplicate detection and sequence-number behavior
- journal max-entry archive behavior
- trigger derivation from extraction labels
- date and time defaults used by current snippet and journal writers
- generated USER snippet projection cleanup/reconciliation behavior
- return booleans from the underlying NoteDB writers for existing direct callers
  of `core.lifecycle.soul_snippets` and `datastore.notedb.soul_snippets`; the
  extraction orchestrator may consume the richer helper counters instead
- extraction result shape for `result["snippets"]` and `result["journal"]`
- `publish_complete` trace ordering from M9.4 after snippet, journal, and
  project-log side effects
- snippet/journal write ordering relative to MemoryDB publish and project-log
  queueing
- lifecycle wrapper behavior for existing callers
- maintenance registration for snippet review and journal distillation

## Required Tests Before W4

Add or preserve tests proving:

- helper/direct parity for snippet and journal file contents.
- helper/direct parity for duplicate snippet and duplicate journal inputs.
- `apply_extracted_payloads()` routes snippet and journal writes through the
  EvolutionDB contract seam without moving MemoryDB or DocsDB writes.
- failHard snippet write failure raises after logging and does not fall back to
  direct NoteDB writes.
- fail-soft snippet write failure logs and does not claim written counters.
- failHard journal write failure raises after logging and does not fall back to
  direct NoteDB writes.
- fail-soft journal write failure logs and does not claim written counters.
- `publish_complete` still fires after snippet, journal, and project-log side
  effects.
- M9.2 DocsDB, M9.3 session ingest, and M9.4 MemoryDB extraction publish routes
  remain registered.
- W3-selected journal recall or identity-context checks still see the same
  persisted content.

## W4 Smoke

W4 should smoke the runtime patch only after W3/W6/W8 review:

- normal extraction writes at least one snippet and one journal entry through
  the selected EvolutionDB seam.
- the expected `*.snippets.md` and `journal/*.journal.md` files are visible in
  the installed runtime home.
- duplicate snippet/journal payloads do not create duplicate visible entries.
- failHard write failure stops the selected path without fallback.
- identity/context lifecycle still reads the written content as before.
- M9.2 DocsDB, M9.3 session ingest, and M9.4 MemoryDB extraction publish routes
  remain healthy.

## First Runtime Slice Closure

The synchronous EvolutionDB/NoteDB helper seam closed at:

- `99a947426` `refactor(datastore): route snippet journal writes through EvolutionDB`
- `7fd0771dc` `fix(datastore): keep EvolutionDB helper behind core seam`

Implemented shape:

- `core.plugins.notedb_contract.run_snippet_journal_write_payload()` is the
  selected helper seam for extraction snippet/journal writes.
- `ingest.extract.apply_extracted_payloads()` still owns orchestration and now
  sends normalized `result["snippets"]` and `result["journal"]` through that
  helper.
- The helper calls `core.lifecycle.soul_snippets`, which preserves the
  allowlisted core lifecycle seam to the existing `datastore.notedb`
  implementation.
- `core.lifecycle.soul_snippets` remains available for lifecycle/janitor callers.
- No request event was added in this slice.
- MemoryDB fact publish, DocsDB project-log queueing, lifecycle maintenance,
  and journal recall behavior are unchanged.
- `snippet_journal_metrics` is additive; existing extraction result keys and
  side-effect ordering remain unchanged.

Closure evidence:

- W4 live PASS for `99a947426`; W4 source-proof PASS for `7fd0771dc`.
- W3 recall/identity-context review PASS/no findings for the corrected pair.
- W6 APPROVED-WITH-CONCERNS; governance clarifications were recorded in
  `8e28a8d68`.
- W8 static PASS and runtime hold close recorded for the pair after
  `7fd0771dc` fixed the initial boundary-check hold.
- W8 docs-gate PASS for `8e28a8d68`.

## Deferred Decisions

- Whether to split snippet and journal writes into separate request events.
- Whether direct `extract_from_transcript()` / CLI should ever use request mode.
- Whether snippet review and journal distillation maintenance should get their
  own request handlers beyond the existing maintenance contract.
- Runtime `datastore.evolutiondb` package rename, deferred to M10.
- `.ego` import/export integration, deferred to a separate product milestone.
