# Datastore Events M19 SessionDB Source-Window Metadata Plan

Status: draft plan; no runtime implementation yet
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

This document selects a narrow source-window metadata ownership slice only. It
does not approve changing the `session_chunks` recall selector owner, changing
ranking/planner behavior, changing source-window item selection or token budget,
adding SessionDB recall selectors, removing MemoryDB compatibility wrappers,
changing active/request session ingest payloads or envelopes, lifecycle
persistence, data migration, CLI behavior, default request routing, public push,
or release actions.

## Goal

M14 through M18 moved SessionDB from metadata-only registration to owning the
session ingest helper, request route, active helper import, and active failHard
exception propagation. MemoryDB still correctly owns the user-facing
`session_chunks` recall selector and final recall expansion/output policy.

The remaining source-window ownership boundary is mixed: SessionDB stores the
transcript rows and can expand a microchunk to nearby SessionDB rows, but
MemoryDB recall code reconstructs source-date header metadata for the expanded
window. M19 selects a first metadata-only runtime slice: let SessionDB include
explicit source-window header metadata in `expand_microchunk()` results, then let
MemoryDB consume that metadata while producing the same recall output as before.

This is not a selector move. MemoryDB remains the recall owner; SessionDB only
supplies transcript/provenance metadata for MemoryDB to format under the existing
policy.

## Current Boundary

Current post-M18 path:

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

Implement one runtime metadata slice only:

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

- request/active compatibility-wrapper removal from `core.plugins.memorydb_contract`
- whether SessionDB should expose dedicated request handlers beyond
  `session.ingest_log.request.v1` and generic metadata/maintenance surfaces
- lifecycle persistence for ack-only lifecycle events
- source-window selector ownership or SessionDB recall capability
- source-window ranking/planner policy changes
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
