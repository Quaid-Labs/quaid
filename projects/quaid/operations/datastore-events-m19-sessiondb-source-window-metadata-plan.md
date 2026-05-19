# Datastore Events M19 SessionDB Source-Window Metadata Plan

Status: runtime metadata slice complete; selector/policy decisions deferred
Owner: W1 runtime/datastore, W3 recall and source-window review
Plan source: `projects/quaid/operations/datastore-events-m18-sessiondb-active-ingest-failhard-plan.md`

## Precondition

Do not implement runtime code for M19 until:

1. M18 active `session.ingest_log` failHard cleanup is closed through
   W4/W3/W6/W8.
2. W3 reviews the selected slice because source-window metadata is recall-visible
   and affects how session evidence is expanded around selected chunks.
3. W6 reviews the ownership boundary because this slice moves a small piece of
   source-window metadata construction from MemoryDB recall code toward
   SessionDB transcript provenance ownership.
4. W8 confirms static coverage includes session source-window expansion,
   session-memory bridge, store recall, active/request session ingest,
   datastore manifests/contracts, and boundary checks.

This document records the completed narrow source-window metadata ownership
slice only. It does not approve changing the `session_chunks` recall selector
owner, changing ranking/planner behavior, changing source-window item selection
or token budget, adding SessionDB recall selectors, removing MemoryDB
compatibility wrappers, changing active/request session ingest payloads or
envelopes, lifecycle persistence, data migration, CLI behavior, default request
routing, public push, or release actions.

## Goal

M14 through M18 moved SessionDB from metadata-only registration to owning the
session ingest helper, request route, active helper import, and active failHard
exception propagation. MemoryDB still correctly owns the user-facing
`session_chunks` recall selector and final recall expansion/output policy.

The remaining source-window ownership boundary is mixed: SessionDB stores the
transcript rows and can expand a microchunk to nearby SessionDB rows, but
MemoryDB recall code reconstructed source-date header metadata for the expanded
window. M19 selected a first metadata-only runtime slice: let SessionDB include
explicit source-window header metadata in `expand_microchunk()` results, then let
MemoryDB consume that metadata while producing the same recall output as before.

This is not a selector move. MemoryDB remains the recall owner; SessionDB only
supplies transcript/provenance metadata for MemoryDB to format under the existing
policy.

## Current Boundary

Pre-M19 path:

1. `datastore.sessiondb.session_store.expand_microchunk()` returns the selected
   microchunk, pair, pair-window rows, compact microchunk-window rows, source
   chunk row, and `source_date`.
2. `core.services.session_memory_bridge.expand_microchunk()` forwards that
   SessionDB response to MemoryDB recall callers.
3. `datastore.memorydb.memory_graph._sessiondb_bridge_expand_microchunk()` calls
   the bridge during source-window expansion.
4. MemoryDB recall builds source-date header rows through
   `_ensure_session_window_source_date_header()` and
   `_sessiondb_bridge_expansion_window()` using SessionDB/memory projection
   fields.
5. MemoryDB owns `session_chunks` recall selector, final source-window item
   selection, token budgets, ranking, planner behavior, and user-facing recall
   output.

## Selected First Slice: Source-Window Header Metadata Only

Implemented one runtime metadata slice only:

1. Add a SessionDB-owned `source_window_header` metadata object to
   `datastore.sessiondb.session_store.expand_microchunk()` results when the
   expanded microchunk has a source date. The object should contain only
   provenance fields needed to construct the existing header row: `source_date`,
   `session_id`, `pair_id`, `microchunk_id`, and a stable header identifier.
   Stable header identifier shape: composite of `session_id` and `source_date`
   (for example, `f"{session_id}:{source_date}"`) so multiple microchunks from
   the same session on the same date dedupe to the same header. Do not use a
   non-deterministic UUID-style identifier.
2. Do not insert the header object into SessionDB's `window` or
   `microchunk_window` arrays. Those arrays must keep their current row shapes
   and counts.
3. Update MemoryDB source-window expansion to prefer the SessionDB-provided
   `source_window_header` metadata when constructing the existing source-date
   header row. If the metadata is absent for older responses, preserve the
   current MemoryDB construction path.
4. Preserve the rendered recall text and metadata shape for existing cases:
   `source_date: <date>` header text, `session_source_header=True`,
   `source_type='session_chunk'`, `session_window_expanded`,
   `session_window_expansion_source`, center chunk/microchunk metadata, and
   `session_window_chunk_ids` remain unchanged.
5. Preserve source-window selection and budget behavior exactly. Do not change
   before/after radius, support-window behavior, graph-cluster source chunk
   expansion, query-overlap ranking, required center row handling, or token
   limits.
6. Preserve MemoryDB ownership of `session_chunks` recall/write capability and
   SessionDB `capabilities.recall=[]`.
7. Preserve active/request session ingest behavior and M16/M17/M18 ownership
   boundaries.
8. Preserve MemoryDB compatibility wrappers; do not remove, warn from, or
   deprecate them in this slice.

## Non-Targets

- no new event names
- no new recall selector and no SessionDB recall capability
- no change to MemoryDB `session_chunks` recall/write ownership
- no change to source-window item selection, ranking, scoring, planner behavior,
  token budgets, or output ordering
- no change to active/request session ingest payloads or result envelopes
- no request ownership, manifest ownership, or broker response shape change
- no removal of MemoryDB compatibility wrappers
- no lifecycle persistence for ack-only lifecycle events
- no transcript row schema migration
- no daemon scheduling, signal-shape, CLI, default request routing, `.ego`,
  alias retirement, or plugin-id rename changes

## FailHard Policy

- `failHard=true`: malformed `source_window_header` metadata or source-window
  expansion failures must raise through the existing source-window expansion
  failHard paths. Do not silently fall back to incomplete or misdated headers in
  failHard mode.
  Malformed metadata means at least one of: not a dict, missing required key
  (`session_id`, `source_date`, `pair_id`, `microchunk_id`, or stable header
  identifier), or wrong type for a required key, such as non-string
  `source_date`. Empty strings or null values for required keys count as
  malformed because they cannot produce a valid header row.
- `failHard=false`: existing fail-soft recall behavior may continue, but any
  ignored malformed metadata must log loudly and preserve the previous output
  shape rather than reporting enriched provenance that was not actually used.
- Do not add fallback to MemoryDB-only header construction for states that are
  reachable only because the new SessionDB metadata is malformed under failHard.
  Backward-compatible handling for absent metadata is allowed because installed
  alpha/runtime responses may not include the new field until all call paths are
  updated.
- Do not wrap SessionDB metadata extraction and MemoryDB header construction in
  a shared `try`/`except` scope that could mask malformed-metadata failures as
  successful fallback. Each path raises out of its own block.

## Required Tests Before W4

Add or preserve focused tests proving:

- `session_store.expand_microchunk()` returns `source_window_header` metadata
  when the expanded microchunk has a `source_date`, and does not mutate the
  `window` or `microchunk_window` row counts.
- `session_memory_bridge.expand_microchunk()` forwards the new metadata without
  changing its public call shape.
- MemoryDB source-window expansion consumes the SessionDB-provided metadata and
  renders the same `source_date: <date>` header text and same session window
  metadata fields as before.
- When `source_window_header` metadata is absent from the SessionDB response
  (simulated by monkeypatching `session_store.expand_microchunk()` to omit the
  new field), MemoryDB's backward-compatible fallback construction produces
  output identical to the metadata-enriched path for the same input: same
  `source_date` header text, same metadata fields, and same row identifiers.
- Existing source-window tests for direct `sessiondb_bridge` expansion,
  graph-cluster source chunk expansion, source-date header insertion, no-created
  `created_at` fallback, support-window filtering, and budget behavior still
  pass unchanged.
- MemoryDB remains the only owner of the `session_chunks` recall selector;
  SessionDB `capabilities.recall` remains `[]`.
- Active/request session ingest parity still writes SessionDB rows and MemoryDB
  `session_chunks` with the same counts, metadata, source kind, and microchunk
  linkage.
- failHard source-window expansion failures still raise; fail-soft malformed or
  absent metadata handling remains loud and does not claim false provenance.

## W4 Smoke

After W3/W6/W8 review, W4 should source-proof the installed runtime and run a
narrow session source-window smoke:

- `session_store.expand_microchunk()` returns `source_window_header` metadata for
  dated session evidence without changing window row counts.
- A recall/source-window expansion for dated session evidence still renders the
  same `source_date: <date>` context header and selected session rows.
- `session.ingest_log` active and `session.ingest_log.request.v1` request paths
  still ingest transcripts and project MemoryDB `session_chunks` evidence.
- MemoryDB still owns `session_chunks`; SessionDB recall remains empty.

## Deferred Decisions

- SessionDB ingest wrapper retirement closed in M23 at `bfe5836b` +
  `4a3824d88`
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- lifecycle observation metadata for ack-only lifecycle events closed in M20 at
  `bc58b8a06` + `195fc7678`; lifecycle-triggered transcript ingest and daemon
  automation remain deferred
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration

## Implementation Record

Runtime closed in `cf9eddd26` (`refactor(datastore): add SessionDB
source-window metadata`) with test follow-up `e4c4ec0d5` (`test(datastore):
tighten M19 source-window guards`).

Implemented behavior:

- `datastore.sessiondb.session_store.expand_microchunk()` now emits a top-level
  `source_window_header` metadata object when the expanded microchunk has a
  source date. The object contains only `session_id`, `source_date`, `pair_id`,
  `microchunk_id`, and deterministic `header_id=f"{session_id}:{source_date}"`.
- SessionDB `window` and `microchunk_window` arrays remain unchanged; the
  source header is metadata only, not an inserted SessionDB row.
- MemoryDB source-window expansion validates and consumes
  `source_window_header` when present, then constructs the same source-date
  header row shape used before M19.
- Absent `source_window_header` metadata remains a backward-compatible installed
  alpha path: MemoryDB uses the pre-M19 header construction path and produces
  identical output for the same input.
- Malformed `source_window_header` metadata raises through the existing
  source-window failHard path under `failHard=true`; under `failHard=false`,
  MemoryDB logs loudly and preserves the previous output shape without claiming
  enriched provenance.
- The legacy fallback header row also carries the deterministic header id, and
  source-header dedup uses that id. This is intentional M19 scope so enriched
  and absent-metadata paths produce matching row identifiers and dedupe headers
  consistently.
- MemoryDB `session_chunks` recall/write ownership, final source-window
  selection, ranking, planner behavior, token budgets, output ordering,
  active/request session ingest envelopes, M16 request ownership, M17 active
  import routing, M18 failHard behavior, MemoryDB compatibility wrappers, and
  SessionDB `capabilities.recall=[]` are unchanged.

Test coverage:

- SessionDB bridge integration asserts exact `source_window_header` metadata and
  proves `window`/`microchunk_window` rows are not mutated with header rows.
- MemoryDB recall tests assert enriched and absent-metadata paths produce the
  same rendered text, source date, session-window metadata, row identifiers, and
  header-row fields.
- Malformed-header tests cover non-dict metadata, missing/wrong/empty required
  keys, invalid dates, and `header_id` mismatch.
- FailHard/fail-soft tests prove malformed metadata raises under failHard and
  logs/falls back under fail-soft.
- Existing sessiondb bridge, source-window, and session-memory bridge lanes
  continue to pass.

Validation record:

- W4 R201 live/source-proof PASS on `cf9eddd26`; `e4c4ec0d5` was test-only and
  required no fresh live smoke.
- W3 runtime/recall APPROVED with no findings: provenance metadata source only,
  MemoryDB remains `session_chunks` recall/write selector and final expansion
  policy owner, SessionDB recall remains `[]`, and no source-window selection,
  ranking, planner, token-budget, output-ordering, active/request envelope, or
  lifecycle behavior drift was found.
- W6 APPROVED after `e4c4ec0d5` closed the malformed-shape coverage gap,
  tightened absent-metadata parity assertions, and confirmed header-id fallback
  and dedup-key additions as intentional M19 concomitant scope.
- W8 static PASS/runtime HOLD closed for the `cf9eddd26` + `e4c4ec0d5` pair:
  focused source-window tests, sessiondb bridge/source-window selector, session
  memory bridge, py_compile, ruff, diff/docs checks, boundary check, and unit
  wrapper all passed.
