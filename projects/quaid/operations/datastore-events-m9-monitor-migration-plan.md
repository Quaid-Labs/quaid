# Datastore Events M9 Monitor Migration Plan

Status: draft coordination plan; no runtime implementation
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Precondition

Do not implement any M9 sub-milestone until:

1. M8 completes the selected project-docs supervisor maintenance migration.
2. W4 smoke confirms the M8 listener-owned path is authoritative and stable.
3. W6 confirms no unrelated monitor/write paths were touched by M8.
4. The specific M9 sub-milestone has a focused implementation plan reviewed by
   the relevant domain owner before code lands.

This document is planning only. It records the ordered M9 sequence and stop
rules; it does not approve any runtime implementation.

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
- Remaining lifecycle/session sub-slices require their own focused plan before
  runtime implementation.

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
- W4 live/source-proof PASS, W3 recall PASS/no re-review needed, W6 concerns
  closed, and W8 static/runtime closure recorded for that pair.
- The completed slice selected an internal `apply_extracted_payloads()` ->
  MemoryDB-owned helper split before any broader request routing.
- Request event routing plan is drafted in the M9.4 tracking doc, but runtime
  implementation is not approved until W3/W6/W8 review that next slice.
- A pre-slice cleanup should move publish-only defaults into a shared
  MemoryDB/core-owned seam before request-event wiring.

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

Scope:

- evolution/snippet/journal writes behind `evolutiondb` listeners
- janitor distillation and identity-file update behavior for the selected path

Out of scope:

- runtime `datastore.notedb` module rename; that waits for M10
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
- `evolutiondb` remains only a canonical datastore id/planning name until the
  dedicated M10 runtime rename starts
