# Datastore Events M13 Split Extraction Routing Plan

Status: runtime slice complete; default request routing still deferred
Owner: W1 runtime/datastore, W3 recall and identity-context review
Plan source: `projects/quaid/operations/datastore-events-m12-evolution-snippet-journal-split-plan.md`

## Precondition

Do not implement runtime code for M13 until:

1. M12 helper-split runtime is closed through W4/W3/W6/W8.
2. M12 split request-event surfaces are closed through W4/W3/W6/W8.
3. M11 direct Python and hidden CLI request-mode opt-in work remains closed, so
   callers already explicitly choose request mode before broker routing is used.
4. W3 reviews the selected slice because snippets and journal files are visible
   identity and recall surfaces.
5. W6 reviews the ownership boundary because this slice touches ingest
   extraction orchestration, EvolutionDB request envelopes, and failHard
   behavior.
6. W8 confirms static coverage includes extraction request-mode tests,
   EvolutionDB event/request tests, docs consistency, and boundary checks.

This document records the runtime slice that routes explicit extraction request
mode through split EvolutionDB events. It does not approve default request
routing, public CLI promotion, daemon scheduling changes, new event names,
request-envelope changes, alias retirement, plugin-id rename, or public
push/release actions.

## Goal

M13 addresses the deferred M12 question of whether extraction producers should
route through the separate snippet-only and journal-only EvolutionDB request
events.

The selected goal is narrow: when `apply_extracted_payloads()` is already in
explicit `snippet_journal_write_mode="request"`, route snippet and journal
writes through the split request events instead of the combined request event,
while returning the same additive `snippet_journal_metrics` object to callers.

Default direct extraction stays direct. The hidden CLI and daemon only see this
change when they already explicitly choose `snippet_journal_write_mode="request"`.

## Current Boundary

Current post-M12 path:

1. `apply_extracted_payloads()` builds one snippet/journal payload from
   `raw_snippets`, `raw_journal`, trigger/date/time metadata, write flags, and
   dry-run.
2. Direct mode calls `run_snippet_journal_write_payload()`.
3. Request mode sends one combined
   `evolution.snippet_journal_write.request.v1` broker request.
4. M12 added additive split request surfaces:
   `evolution.snippet_write.request.v1` and
   `evolution.journal_write.request.v1`.
5. The split handlers delegate through the existing public combined helper with
   the opposite family forced to `{}` and return the existing
   `{status, snippet_journal_metrics}` envelope shape.
6. Extraction producers still do not use the split surfaces.

## Implementation Record

Runtime slice implemented by:

- `516732b88` `refactor(datastore): route extraction request mode through split events`
- `9437788d` `test(datastore): cover split request failure ordering`

Implemented behavior:

- `apply_extracted_payloads(snippet_journal_write_mode="request")` now sends
  ordered split broker requests: `evolution.snippet_write.request.v1` first,
  then `evolution.journal_write.request.v1` when both families have payloads and
  enabled write flags.
- Direct mode remains unchanged and still calls
  `run_snippet_journal_write_payload()` synchronously.
- The combined `evolution.snippet_journal_write.request.v1` event and handler
  remain registered and callable for compatibility, but extraction request mode
  no longer emits the combined event.
- Split response validation preserves the existing `snippet_journal_metrics`
  object shape and rejects failed split responses as request-mode failures.
- Skipped or empty families use inline zero-metrics synthesis; no no-op broker
  requests are sent.
- Family metrics are merged with snippet counters and
  `target_files["snippets"]` coming only from the snippet response, journal
  counters and `target_files["journal"]` coming only from the journal response,
  and errors concatenated in snippet-then-journal order.
- Failure behavior is explicit: snippet failure stops before journal dispatch;
  journal failure after snippet success raises and does not report partial
  success; neither path falls back to the direct helper.
- Hidden CLI flags, daemon signal shape, event names, request envelopes, visible
  markdown paths, project-log queueing, `publish_complete` ordering, and
  recall/source-window/ranking behavior are unchanged.
- Tests pin ordered split dispatch, combined-event non-emission by extraction
  request mode, snippet-only and journal-only routing, write-flag and
  empty-family suppression, inline zero metrics, aggregation shape,
  fail-soft-envelope rejection, no direct fallback, snippet-fail-skips-journal,
  and journal-fail-after-snippet behavior.

Validation:

- W4 live/source-proof PASS on R201 for `516732b88`; `9437788d` was a
  cleanup/test follow-up with no fresh live gate needed.
- W3 runtime/recall APPROVED/no findings for `516732b88`; `9437788d` had no
  recall/visible/default behavior delta.
- W6 APPROVED after the follow-up removed the unreachable merge-status branch
  and added the snippet-fail-skips-journal trip-wire test.
- W8 static PASS and runtime HOLD closed for the pair.

## Selected First Slice

Selected scope: route explicit extraction request mode through split request
events.

Implementation shape:

- Keep `snippet_journal_write_mode` values unchanged: `direct` and `request`
  only. Do not add a third mode or a routing-options object in this slice.
- Keep `direct` mode unchanged: it still calls
  `run_snippet_journal_write_payload()` once.
- Change `request` mode inside `apply_extracted_payloads()` so it sends ordered
  split broker requests instead of the combined broker request.
- Use `evolution.snippet_write.request.v1` for snippet payloads and
  `evolution.journal_write.request.v1` for journal payloads.
- Preserve snippet-before-journal ordering. If both write families are requested,
  send the snippet request first and the journal request second.
- Preserve write flags. If `write_snippets=false`, do not send a snippet request;
  if `write_journal=false`, do not send a journal request. The returned combined
  metrics must still represent skipped/zeroed families the same way current
  request-mode callers expect.
- Preserve empty-family behavior. Do not invent writes for empty snippet or
  journal payloads; aggregate zero counters and empty target files for families
  that have no payload to send.
- Skipped or empty-family aggregation shape is inline synthesis, not a no-op
  broker request: when a write flag is false or a family payload is empty,
  synthesize a zero-counter metrics entry for that family using the same
  key/value shape the split handler returns for a fully empty successful payload
  (`status="ok"`, counters at `0`, target files as `[]`, `errors=[]`). Do not
  send a no-op broker request.
- Preserve the existing additive `snippet_journal_metrics` shape by merging the
  per-family metrics returned by the split handlers into one combined metrics
  object with the same keys, target-file structure, status, and errors list.
- Keep `handle_snippet_journal_write_request()` and the combined
  `evolution.snippet_journal_write.request.v1` event available for installed
  alpha compatibility, direct broker callers, and regression tests. Do not remove
  or deprecate the combined event in this slice.
- Do not change `extract_from_transcript()` signatures, hidden CLI flags, daemon
  signal parsing, event names, request envelopes, visible markdown paths, or
  recall/source-window/ranking policy.

## Response Aggregation Rules

The split request validator should preserve the observable contract of the
current combined request validator:

- Each broker response must come from datastore id `evolutiondb`.
- Each handler result must be an object with `snippet_journal_metrics` as an
  object.
- Each metrics object must include `target_files.snippets`,
  `target_files.journal`, and `errors` lists.
- The aggregate metrics object must keep the current keys:
  `status`, `snippet_files_seen`, `snippet_items_seen`,
  `snippet_files_written`, `snippet_items_written`, `snippet_files_skipped`,
  `journal_files_seen`, `journal_files_written`, `journal_files_skipped`,
  `target_files`, and `errors`.
- Snippet counters and `target_files.snippets` come only from the snippet
  response. Journal counters and `target_files.journal` come only from the
  journal response.
- Aggregate `errors` is the ordered concatenation of split response errors.
- Aggregate `status` remains `ok` only when every sent request succeeds and all
  returned family metrics are `ok`; otherwise request-mode validation raises
  through the existing warn-before-raise path and does not return partial success.

## Failure Semantics

- Request-mode broker, registration, handler, validation, or writer failure must
  not fall back to the direct helper after request mode is selected.
- The snippet request and journal request must not share a broad `try`/`except`
  scope that can misclassify one write family's failure as the other's failure.
- If the snippet request fails, do not send the journal request.
- If the snippet request succeeds and the journal request fails, raise after the
  journal failure with the existing request-mode warning discipline. Do not retry
  the journal request, do not roll back snippet files, and do not report success.
  This preserves the current write-order reality: snippet writes happen before
  journal writes, and failHard failures are surfaced rather than hidden.
- A fail-soft handler envelope (`status="failed"`) from either split event is
  still a request-mode failure to `apply_extracted_payloads()` and must raise via
  response validation. Handler fail-soft behavior is for broker surface
  consistency, not a fallback path for extraction.

## Non-Targets

- no new event names
- no combined event removal
- no request-envelope shape changes
- no new extraction mode kwarg values
- no default direct-behavior change
- no public CLI help change
- no daemon scheduling or signal-shape change
- no MemoryDB publish routing change
- no project-log queueing or `publish_complete` ordering change
- no snippet or journal markdown path, format, trigger, date, duplicate,
  archive, or USER projection change
- no journal recall ranking, scoring, planner, source-window, or source metadata
  policy change
- no `.ego` import/export behavior change
- no alias retirement or `notedb.core` plugin-id rename

## Required Tests Before W4

Focused tests should prove:

- `apply_extracted_payloads(snippet_journal_write_mode="request")` sends
  `evolution.snippet_write.request.v1` before
  `evolution.journal_write.request.v1` when both families have payloads and both
  write flags are true.
- The old combined `evolution.snippet_journal_write.request.v1` event is not
  emitted by extraction request mode after this slice.
- Snippet-only request mode sends only `evolution.snippet_write.request.v1` and
  returns the same combined metrics shape with journal counters and target files
  at zero.
- Journal-only request mode sends only `evolution.journal_write.request.v1` and
  returns the same combined metrics shape with snippet counters and target files
  at zero.
- `write_snippets=false` suppresses the snippet request; `write_journal=false`
  suppresses the journal request; aggregate metrics preserve the current skip
  and target-file behavior.
- Mixed success aggregates snippet and journal metrics into the existing
  `snippet_journal_metrics` shape without changing visible target file names.
- Snippet request failure logs before raising, sends no journal request, and does
  not call the direct helper.
- Journal request failure after snippet success logs before raising, does not
  call the direct helper, and does not report partial success.
- Handler fail-soft envelopes from the split events are treated as extraction
  request-mode failures by response validation.
- Existing split handler tests still prove source guards, cross-family rejection,
  failHard warn-before-raise, and family-zero metrics.
- Existing direct mode and hidden CLI default behavior tests still pass.

## W4 Smoke

W4 should smoke runtime code only after W3/W6/W8 review:

- installed direct extraction with default direct mode still writes visible
  `*.snippets.md` and `journal/*.journal.md` content
- installed hidden CLI request mode or direct Python request mode writes visible
  snippet and journal content through the split request events
- live/source proof shows snippet request is sent before journal request for a
  mixed payload
- live/source proof shows the combined event remains registered but extraction
  request mode no longer emits it
- identity/context or journal recall sees the same persisted snippet and journal
  content after request-mode extraction
- failHard split-request failure stops loudly with no direct-helper fallback
- M9.2 DocsDB, M9.3 session ingest, M9.4 MemoryDB request, M9.5 combined
  EvolutionDB request, M10 compatibility aliases, M11 hidden CLI flags, and M12
  split event surfaces remain healthy

## Deferred Decisions

- whether direct request mode should ever become the default
- whether hidden CLI request-mode flags should ever become public
- SessionDB first-party manifest metadata, tracked as M14 in
  `projects/quaid/operations/datastore-events-m14-sessiondb-manifest-plan.md`
- lifecycle persistence and source-window metadata enrichment
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
