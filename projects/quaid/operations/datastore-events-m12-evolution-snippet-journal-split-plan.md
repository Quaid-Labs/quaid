# Datastore Events M12 EvolutionDB Snippet Journal Split Plan

Status: first helper-split runtime slice complete; separate request-event surface planned
Owner: W1 runtime/datastore, W3 recall and identity-context review
Plan source: `projects/quaid/operations/datastore-events-m9-5-evolution-snippet-journal-plan.md`

## Precondition

Do not implement runtime code for M12 until:

1. M9.5 EvolutionDB snippet/journal helper and request routing are closed
   through W4/W3/W6/W8.
2. M10 Slice 1 and Slice 2 rename work is closed, with compatibility aliases
   retained for installed alpha state.
3. M11 direct extraction request-mode opt-in work is closed, so direct Python and
   hidden CLI callers already use the same combined request surface as the
   daemon when explicitly selected.
4. W3 reviews the selected slice because snippets and journal files are visible
   identity/recall surfaces.
5. W6 reviews the ownership boundary because this slice touches EvolutionDB
   contract helpers, request envelopes, and extraction orchestration.
6. W8 confirms static coverage includes EvolutionDB helper tests, extraction
   request-mode tests, event/contract/manifest checks, and boundary checks.

This document records the first helper-split runtime slice and selects a second
event-surface-only slice for review. It does not approve extraction producer
routing changes, daemon routing changes, default behavior changes, public CLI
changes, alias retirement, plugin-id rename, or public push/release actions.

## Goal

M12 addresses the deferred M9.5 question of whether snippet and journal writes
should split into separate request surfaces.

The safe first step is not a new event. The safe first step is to split the
combined EvolutionDB helper internals into snippet-owned and journal-owned
sub-helpers while preserving the public combined helper and combined request
handler exactly.

Later slices may consider separate request events only after the helper split is
validated and reviewed. The next selected slice adds those event surfaces but
does not route extraction producers through them yet.

## Current Boundary

Current post-M11 path:

1. `ingest.extract.apply_extracted_payloads()` builds one payload containing
   `snippets`, `journal`, trigger/date/time metadata, write flags, and dry-run.
2. Direct mode calls `core.plugins.evolutiondb_contract.run_snippet_journal_write_payload()`.
3. Request mode sends one `evolution.snippet_journal_write.request.v1` broker
   request.
4. `handle_snippet_journal_write_request()` validates the combined payload and
   delegates to `run_snippet_journal_write_payload()`.
5. The helper writes snippet entries first, then journal entries, and returns a
   combined `snippet_journal_metrics` object.
6. Project-log queueing and `publish_complete` happen after the combined
   snippet/journal write block.

## Implementation Record

First runtime slice implemented by:

- `e81244e32` `refactor(datastore): split snippet journal helper internals`

Implemented behavior:

- `core.plugins.evolutiondb_contract` now has private mutation-style helpers:
  `_run_snippet_write_payload()` and `_run_journal_write_payload()`.
- Public `run_snippet_journal_write_payload()` still owns payload validation,
  result construction, common metadata parsing, helper ordering, orchestration,
  and final return.
- The snippet helper mutates only snippet-family metrics and
  `target_files["snippets"]`; the journal helper mutates only journal-family
  metrics and `target_files["journal"]`. Shared `status` and `errors` behavior
  remains the same as before the split.
- The combined helper still calls snippet before journal.
- `handle_snippet_journal_write_request()`,
  `evolution.snippet_journal_write.request.v1`, request envelopes,
  `snippet_journal_metrics`, extraction routing kwargs, visible markdown files,
  failHard/no-fallback behavior, project-log queueing, and `publish_complete`
  ordering are unchanged.
- Tests pin family-isolated counters and target files, write-flag skip behavior,
  snippet-before-journal helper ordering, failHard warn-before-raise, combined
  request handler envelope preservation, and request-mode no-fallback behavior.

Validation:

- W4 live/source-proof PASS on R201 for `e81244e32`.
- W3 runtime/recall APPROVED/no findings.
- W6 APPROVED/no concerns.
- W8 static PASS and runtime HOLD closed for the commit.

## Selected First Slice

Selected scope: split the internal helper implementation only.

Implementation shape:

- Keep `run_snippet_journal_write_payload(payload)` as the public direct helper.
- Keep `handle_snippet_journal_write_request(event)` as the only request handler.
- Keep `evolution.snippet_journal_write.request.v1` as the only request event.
- Add private helper functions inside `core.plugins.evolutiondb_contract`, for
  example:
  - `_run_snippet_write_payload(payload, result, soul_snippets)`
  - `_run_journal_write_payload(payload, result, soul_snippets)`
- Helper signature shape is mutation-style: helpers receive the shared combined
  `result` dict and mutate only their write family's metrics in place. The
  public combined helper owns result dict construction, common metadata parsing,
  orchestration, ordering, and final return.
- The public combined helper owns payload validation, common metadata parsing,
  result object creation, helper ordering, and final result shape.
- The snippet helper owns only snippet iteration, target-file projection,
  counters, direct `write_snippet_entry()` calls, and snippet write errors.
- The journal helper owns only journal iteration, target-file projection,
  counters, direct `write_journal_entry()` calls, and journal write errors.
- The combined helper must call the snippet helper before the journal helper.
- Do not move validation or orchestration into `ingest.extract`.
- Do not add new mode kwargs; existing `snippet_journal_write_mode` remains the
  only extraction routing control for this write family.

This slice should be reviewable as a mechanical helper extraction plus tests. It
must not change visible markdown paths, duplicate behavior, counters, target
files, errors, request envelopes, or event names.

## Future Request-Event Split

Separate request events were not selected in the first slice.

Selected second slice: add separate request event surfaces, but do not route
extraction through them yet.

Implementation shape:

- Add two new request event constants and registry entries:
  - `evolution.snippet_write.request.v1`
  - `evolution.journal_write.request.v1`
- Register both events under EvolutionDB ownership with `delivery_mode:
  request`, `fireable: true`, `processable: false`, and `listenable: true`.
- Add both event names to the EvolutionDB manifest `request_handlers` list and
  `EvolutionDbDatastoreContract.handler_specs`.
- Add thin handlers in `core.plugins.evolutiondb_contract`, for example
  `handle_snippet_write_request()` and `handle_journal_write_request()`, plus
  explicit registration helpers.
- Keep the existing combined handler and combined
  `evolution.snippet_journal_write.request.v1` event unchanged.
- The snippet handler accepts `source="extraction-apply-payloads"`, a `snippets`
  object, shared trigger/date/time/write/dry-run metadata, and no non-empty
  `journal` payload. It delegates through the public combined helper with
  `journal` forced to `{}`.
- The journal handler accepts `source="extraction-apply-payloads"`, a `journal`
  object, shared trigger/date/write/dry-run metadata, and no non-empty
  `snippets` payload. It delegates through the public combined helper with
  `snippets` forced to `{}`.
- Both new handlers return the same envelope shape as the combined handler:
  `{status: "ok", snippet_journal_metrics: ...}`. The metrics object remains the
  combined helper's existing shape, with the other write family counters at
  zero.
- Do not change `ingest.extract.apply_extracted_payloads()` in this slice.
- Do not change `snippet_journal_write_mode`; request mode still sends one
  combined `evolution.snippet_journal_write.request.v1` broker request.

This slice creates explicit EvolutionDB request surfaces and tests their
contracts, but it does not introduce partial-write producer behavior.

If a later slice routes extraction through separate events, it needs a separate
reviewed plan that covers:

- whether extraction request mode sends one combined request or two ordered
  broker requests
- how response validation preserves the existing additive
  `snippet_journal_metrics` shape for callers
- how snippet-before-journal ordering is preserved when both are requested
- failure behavior when the first request succeeds and the second fails
- W4 smoke proving visible snippet and journal content remain recallable and
  identity-visible

## Non-Targets

- no new event names in the first slice
- no request-envelope changes in the first slice
- no extraction mode kwarg changes
- no daemon routing changes
- no default request routing changes
- no public CLI behavior changes
- no project-log queueing or `publish_complete` ordering changes
- no snippet or journal markdown path, file format, trigger, date, duplicate,
  archive, or USER projection changes
- no journal recall ranking, scoring, planner, source-window, or source metadata
  policy changes
- no `.ego` import/export behavior change
- no alias retirement or `notedb.core` plugin-id rename

## FailHard Policy

- Snippet and journal write failures must preserve the existing failHard
  behavior: warn before raise under `failHard=true`; record failure metrics and
  continue according to current helper behavior under `failHard=false`.
- The snippet helper and journal helper must not share a broad `try`/`except`
  scope that can misclassify one write family's failure as the other's failure.
- Do not catch helper extraction errors and return success. Result status must
  remain `failed` when current behavior would mark it failed.
- Request-mode broker, handler, validator, or writer failure must not fall back
  to the synchronous helper after request mode is selected.

## Required Tests Before W4

Focused tests should prove:

- `run_snippet_journal_write_payload()` returns byte-for-byte equivalent result
  shape for mixed snippet/journal payloads before and after the helper split.
- Snippet-only payloads update only snippet counters, target files, and errors.
- Journal-only payloads update only journal counters, target files, and errors.
- Mixed payloads preserve snippet-before-journal helper ordering.
- Dry-run and `write_snippets` / `write_journal` flags preserve current skip
  counters and target-file projection.
- Soft snippet write failure records snippet error metrics without inventing
  journal errors.
- Soft journal write failure records journal error metrics without inventing
  snippet errors.
- failHard snippet and journal write failures each log before raising.
- The combined request handler still returns `{status, snippet_journal_metrics}`
  for `evolution.snippet_journal_write.request.v1`.
- `apply_extracted_payloads(snippet_journal_write_mode="request")` still sends
  one combined broker request and still refuses direct-helper fallback after
  broker or validator failure.

For the second event-surface slice, focused tests should prove:

- `evolution.snippet_write.request.v1` and
  `evolution.journal_write.request.v1` are present in the runtime event
  registry as request events.
- EvolutionDB manifest and datastore contract metadata list both new request
  handlers while retaining the combined request handler.
- Each new handler registers under datastore id `evolutiondb`.
- The snippet handler rejects a missing/wrong source, non-object `snippets`, and
  any non-empty `journal` payload.
- The journal handler rejects a missing/wrong source, non-object `journal`, and
  any non-empty `snippets` payload.
- The snippet handler returns `{status, snippet_journal_metrics}` with snippet
  target files/counters populated and journal counters at zero.
- The journal handler returns `{status, snippet_journal_metrics}` with journal
  target files/counters populated and snippet counters at zero.
- Existing `apply_extracted_payloads(snippet_journal_write_mode="request")`
  tests still prove extraction sends only the combined event in this slice.

## W4 Smoke

W4 should smoke runtime code only after W3/W6/W8 review:

- installed direct extraction with default direct mode still writes visible
  `*.snippets.md` and `journal/*.journal.md` content
- installed request-mode extraction with the existing combined event still writes
  visible snippet and journal content
- identity/context or journal recall sees the same persisted content after the
  helper split
- failHard write failure stops loudly with no fallback
- M9.2 DocsDB, M9.3 session ingest, M9.4 MemoryDB request, M9.5 combined
  EvolutionDB request, M10 compatibility aliases, and M11 CLI hidden flags remain
  healthy

For the second event-surface slice, W4 should additionally smoke:

- both separate request events are registered under EvolutionDB in the installed
  environment
- direct broker calls to the snippet-only and journal-only events return the
  existing `snippet_journal_metrics` envelope
- daemon/extraction request mode still uses the combined event, not the separate
  events

## Deferred Decisions

- whether extraction should ever issue two ordered broker requests for snippet
  and journal writes
- whether direct request mode should ever become the default
- whether hidden CLI request-mode flags should ever become public
- lifecycle persistence and SessionDB first-party manifest registration
- source-window metadata enrichment
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
