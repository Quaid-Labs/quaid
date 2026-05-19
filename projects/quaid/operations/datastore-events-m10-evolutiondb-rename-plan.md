# Datastore Events M10 EvolutionDB Runtime Rename Plan

Status: draft plan; no runtime implementation
Owner: W1 runtime/datastore, W3 recall and identity-context review
Plan source: `projects/quaid/operations/datastore-events-m9-monitor-migration-plan.md`

## Precondition

Do not implement runtime code for M10 until:

1. M9 selected monitor-write migrations are closed through W4/W3/W6/W8.
2. W3 reviews the selected rename slice because journal recall and identity
   context read visible NoteDB/EvolutionDB files.
3. W6 reviews the compatibility boundary because installed alpha homes and
   existing imports may still reference `datastore.notedb`.
4. W8 confirms static lanes include import-boundary checks, event/contract
   registry tests, journal/snippet tests, janitor lifecycle tests, and
   extraction orchestration tests.

This document is planning only. It does not approve runtime code.

## Goal

M10 should align the runtime module name with the canonical datastore id:
`evolutiondb`.

Current state after M9:

- `evolutiondb` is the canonical datastore id in manifests and contracts.
- The runtime implementation still lives under `datastore.notedb`.
- `notedb` remains a runtime alias in the manifest.
- `core.plugins.notedb_contract` is still the contract module because M9 avoided
  a half-rename before the runtime package moved.

The rename should reduce naming drift without changing visible snippet/journal
behavior, journal recall behavior, maintenance scheduling, file paths, or event
contracts.

## Current Boundary

Current runtime surfaces that mention NoteDB include:

- `datastore.notedb.soul_snippets`
- `core.lifecycle.soul_snippets`, a wrapper around `datastore.notedb`
- `core.plugins.notedb_contract`
- tests and maintenance paths that patch or import `datastore.notedb`
- manifest metadata with canonical id `evolutiondb` and `runtime_aliases:
  ["notedb"]`

Current M9 request/event surfaces already use EvolutionDB ownership names:

- `recall.journal.request.v1` belongs to `evolutiondb`
- `evolution.snippet_journal_write.request.v1` belongs to `evolutiondb`
- `run_snippet_journal_write_payload()` writes through the lifecycle wrapper,
  not directly through `datastore.notedb`

## Proposed Slice Sequence

### Slice 1: Compatibility Package And Import Seam

Create `datastore.evolutiondb` as the runtime package while keeping
`datastore.notedb` as a loud, compatibility-only alias for installed alpha
state and existing imports.

Preferred shape:

- add `datastore/evolutiondb/soul_snippets.py` as the canonical implementation
  module, or move the implementation there with a compatibility wrapper left in
  `datastore/notedb/soul_snippets.py`
- keep `datastore.notedb.soul_snippets` importable as a small shim that reexports
  from `datastore.evolutiondb.soul_snippets`
- keep markdown file paths and filenames unchanged
- keep `core.lifecycle.soul_snippets` as the core-owned facade used by higher
  layers
- update first-party manifest module metadata to the new canonical runtime path
  while retaining `runtime_aliases: ["notedb"]`
- do not change event names in this slice

The compatibility shim must be small, loud in comments, and tied to alpha-user
upgrade compatibility. It should not grow new behavior.

### Slice 2: Contract Module Naming Decision

After the runtime package exists, decide whether to rename
`core.plugins.notedb_contract` to `core.plugins.evolutiondb_contract`.

Do not combine this decision with Slice 1 unless W6 confirms the import surface
is small enough to review safely.

If renamed:

- keep a compatibility wrapper for `core.plugins.notedb_contract` until all
  internal imports and tests are migrated
- update contract handler specs to the canonical module path
- preserve request handler registration behavior and result envelopes
- keep event names unchanged

If not renamed in M10:

- document why the contract module intentionally stays aligned with the legacy
  runtime package name for another milestone
- add tests pinning that the canonical datastore id remains `evolutiondb`
  despite the contract module name

### Slice 3: Alias Retirement Planning Only

Do not remove `datastore.notedb` compatibility in M10 unless explicit operator
approval says installed alpha state no longer needs it.

If removal is ever selected, it needs a separate plan covering:

- installed alpha upgrade path
- external import audit
- release notes and operator impact
- W4 installed-instance smoke from an older alpha state

## Non-Targets

- no snippet or journal markdown file path changes
- no `.ego` import/export changes
- no journal recall ranking, scoring, planner, or source-window changes
- no event-name changes for `recall.journal.request.v1` or
  `evolution.snippet_journal_write.request.v1`
- no MemoryDB, DocsDB, SessionDB, project-doc, or extraction fact-write changes
- no lifecycle persistence or SessionDB manifest registration
- no prompt/model changes for snippet review or journal distillation
- no removal of `datastore.notedb` compatibility without explicit operator
  approval

## FailHard Policy

- `failHard=true`: import, registration, maintenance, recall, or write failures
  introduced by the rename must raise through the existing caller path.
- `failHard=false`: compatibility fallback may keep installed alpha imports
  working, but it must not hide failed canonical imports or report successful
  writes when the underlying write failed.
- Do not catch `ImportError` and silently switch to the legacy package in product
  code. The legacy package should be the shim, not a runtime fallback decision.

## Parity Invariants

Runtime implementation must preserve:

- visible `*.snippets.md` file paths and content format
- `journal/*.journal.md` paths, duplicate behavior, sequence numbers, and
  archive behavior
- `core.lifecycle.soul_snippets` public functions and return values
- `run_snippet_journal_write_payload()` counters, `target_files`, and error
  semantics
- `evolution.snippet_journal_write.request.v1` request handler envelope
- `recall.journal.request.v1` behavior
- janitor routine registration for snippet review and journal distillation
- failHard warn-before-raise behavior from the M9 write paths
- manifest id `evolutiondb` and alias `notedb`
- importability of legacy `datastore.notedb.soul_snippets` during the
  compatibility window

## Required Tests Before W4

Add or preserve tests proving:

- canonical `datastore.evolutiondb.soul_snippets` imports and exposes the same
  writer, reader, review, distillation, and maintenance entrypoints
- legacy `datastore.notedb.soul_snippets` imports as a compatibility shim and
  returns the same functions/results
- `core.lifecycle.soul_snippets` still delegates to the canonical implementation
- manifest module path and runtime alias metadata are correct
- `core.plugins` contract handler specs point to the chosen canonical contract
  module path or intentionally documented compatibility module path
- snippet/journal helper direct path and request event still write identical
  visible files
- journal recall still reads the same persisted journal content
- janitor lifecycle tests still register and run snippet review/distillation
- boundary check has no new core-to-datastore violations beyond allowlisted
  composition points

## W4 Smoke

W4 should smoke runtime code only after W3/W6/W8 review:

- daemon starts cleanly after deploying the rename slice and pruning stale `.pyc`
  caches
- installed extraction writes a snippet and journal entry through the selected
  request path
- journal recall or identity/context read sees the written content
- janitor snippet review/journal distillation routine registration still loads
- legacy `datastore.notedb` import works during the compatibility window
- M9.2 DocsDB, M9.3 session ingest, M9.4 MemoryDB extraction publish, and M9.5
  EvolutionDB snippet/journal request routes remain healthy

## Deferred Decisions

- exact timing for renaming `core.plugins.notedb_contract`
- removal date for the `datastore.notedb` compatibility shim
- whether release notes are needed for alpha users
- whether `.ego` import/export should reference `evolutiondb` names in a later
  product milestone
