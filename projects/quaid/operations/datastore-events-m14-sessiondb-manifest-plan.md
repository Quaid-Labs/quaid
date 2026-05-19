# Datastore Events M14 SessionDB Manifest Plan

Status: metadata runtime slice complete; lifecycle/source-window ownership deferred
Owner: W1 runtime/datastore, W3 recall and source-window review
Plan source: `projects/quaid/operations/datastore-events-m9-3-lifecycle-session-plan.md`

## Precondition

Do not implement runtime code for M14 until:

1. M9.3 session-log ingest request routing and active handler convergence remain
   closed through W4/W3/W6/W8.
2. M13 split extraction routing remains closed, so extraction request routing is
   not entangled with this SessionDB metadata decision.
3. W3 reviews the selected slice because SessionDB owns transcript provenance
   that feeds MemoryDB `session_chunks` recall evidence and source-window
   expansion.
4. W6 reviews the ownership boundary because this slice changes first-party
   datastore metadata and contract declarations.
5. W8 confirms static coverage includes datastore registry, datastore contract,
   session ingest, session memory bridge, and recall source-window guard lanes.

This document records the completed metadata-only first slice. It does not
approve lifecycle persistence, SessionDB write-route rewiring, source-window
metadata enrichment, new event names, activated SessionDB handlers,
SessionDB-specific request handlers, recall selector changes, data migration,
CLI changes, default request routing, public push, or release actions.

## Goal

M14 revisits the deferred M2/M9.3 decision of whether `sessiondb` should become
a first-party datastore manifest entry.

The selected goal is narrow: declare SessionDB as a first-party datastore in the
core manifest/contract metadata so the registry can describe the durable
transcript/provenance store that already exists, while leaving all runtime write,
read, recall, and source-window behavior unchanged.

This is not a lifecycle persistence slice. It is not a recall planner slice. It
is not a SessionDB ownership rewrite for `session.ingest_log`.

## Current Boundary

Current post-M13 boundary:

1. `datastore.sessiondb.session_store` owns durable sessions, transcript
   chunks, message pairs, microchunks, and MemoryDB attachment identifiers.
2. `core.services.session_memory_bridge` composes SessionDB storage with
   MemoryDB projection and still registers internal bridge routes named
   `sessiondb` and `memorydb`.
3. `session.ingest_log.request.v1` remains registered under MemoryDB because
   the selected M9.3 write path projects SessionDB transcript evidence into
   MemoryDB `session_chunks` recall evidence.
4. The active `session.ingest_log` handler delegates through the MemoryDB-owned
   shared helper and preserves active event envelope semantics.
5. SessionDB has a user-facing inspection CLI namespace through
   `core.session_cli`, but it is not listed in the first-party datastore
   manifest registry.
6. MemoryDB remains the owner of user-facing recall selectors such as
   `session_chunks`; SessionDB supplies transcript provenance and expansion
   data behind that selector.

## Implementation Record

Runtime slice implemented by:

- `f0574902b` `refactor(datastore): register SessionDB manifest metadata`
- `522f16e28` `test(datastore): align SessionDB manifest metadata checks`

Implemented behavior:

- `core.datastore_registry` now includes first-party `sessiondb` manifest
  metadata with `plugin_id="sessiondb.core"`, `module="datastore.sessiondb.session_store"`,
  `runtime_aliases=[]`, `capabilities.recall=[]`, and `capabilities.writes=[]`.
- SessionDB `capabilities.stores` declares transcript/provenance table metadata
  only: `sessions`, `transcript_chunks`, `message_pairs`, `microchunks`, and
  `message_pair_attachments`.
- SessionDB request metadata is limited to existing generic datastore request
  events: `datastore.validate.request.v1`, `datastore.explain.request.v1`, and
  `maintenance.run.request.v1`.
- `SessionDbDatastoreContract` is present but inactive/metadata-only, matching
  the M3 contract style.
- The follow-up removed the unbacked `sessiondb.maintenance` maintenance-task
  placeholder, so `maintenance_tasks=[]` until a future reviewed maintenance
  slice registers an actual routine.
- MemoryDB continues to own `session.ingest_log.request.v1`, the active
  `session.ingest_log` path, and the user-facing `session_chunks` recall
  selector/projection.
- No new events, activated handlers, SessionDB-specific request handlers,
  lifecycle persistence, source-window enrichment, routing changes, daemon
  changes, CLI syntax changes, recall selector changes, or source-window
  behavior changes were introduced.

Validation recorded:

- W4 live/source-proof PASS on R201 for `f0574902b`; `522f16e28` was a
  metadata cleanup/source-proof follow-up and required no fresh live gate.
- W3 runtime/recall APPROVED with no findings; W3 treated `522f16e28` as a
  no-recall/source-window/routing-delta follow-up.
- W6 APPROVED after `522f16e28` removed the unbacked maintenance-task metadata
  and made the corresponding test gap moot.
- W8 static PASS after the repaired CLI wrapper/unit lane included `sessiondb`
  in manifest-list output.

## Selected First Slice: Metadata-Only Manifest Registration

Implemented one runtime metadata slice only:

1. Add a first-party `sessiondb` manifest to
   `core.datastore_registry.FIRST_PARTY_DATASTORE_MANIFESTS`.
2. Add a matching `SessionDbDatastoreContract` class to
   `core.contracts.datastore` and include it in
   `build_first_party_datastore_contracts()`.
3. Keep SessionDB request metadata limited to the existing generic datastore
   request events `datastore.validate.request.v1`,
   `datastore.explain.request.v1`, and `maintenance.run.request.v1`. Do not
   introduce a SessionDB-specific request event in this slice.
4. Do not move `session.ingest_log.request.v1` out of the MemoryDB manifest in
   this slice. MemoryDB remains the selected owner of the existing ingest
   request route and `session_chunks` recall projection.
5. Keep `capabilities.recall` empty for SessionDB in this slice unless W3
   explicitly approves a selector ownership change. Store metadata may describe
   SessionDB-owned transcript/provenance tables, but it must not claim the
   user-facing `session_chunks` recall selector.
6. Manifest capabilities shape: `capabilities.recall` is `[]`.
   `capabilities.stores` may include declarative-only metadata for
   SessionDB-owned tables such as `sessions`, `transcript_chunks`,
   `message_pairs`, `microchunks`, and message-pair attachments. Do not declare
   active SessionDB read, write, or recall surfaces in this slice.
7. Manifest identity shape: `plugin_id` is `sessiondb.core`, matching the
   first-party `<datastore>.core` pattern. Do not alias the plugin id to any
   other datastore.
8. Manifest alias shape: `runtime_aliases` is an empty list `[]`; SessionDB has
   no prior runtime datastore name to alias.
9. Keep all SessionDB contract methods metadata-only/inactive, matching the
   existing M3 contract style. Contract construction, validation, and inactive
   `nack` behavior may be tested, but no production SessionDB operation should
   be routed through the contract.
10. Preserve failHard behavior in registry/contract validation. Invalid SessionDB
   manifest metadata must raise under `failHard=true` and log/skip under
   `failHard=false`, matching existing manifest validation policy.

## Non-Targets

- no lifecycle datastore table or lifecycle acknowledgement persistence
- no changes to `session.new`, `session.reset`, `session.compaction`,
  `session.timeout`, `session.agent_start`, or `session.agent_end`
- no change to `session.ingest_log` active delivery mode
- no change to `session.ingest_log.request.v1` ownership or handler routing
- no SessionDB-specific request event names or activated handlers
- no MemoryDB session projection changes
- no `session_chunks` recall planner, ranking, scoring, result, or selector
  changes
- no source-window expansion or metadata enrichment changes
- no transcript chunking, message-pair parsing, microchunk splitting, or row
  schema migration
- no SessionDB CLI syntax or output changes
- no daemon, adapter, hook, or extraction routing changes
- no `.ego` import/export behavior change
- no compatibility-alias retirement or `notedb.core` plugin-id rename

## FailHard Policy

- `failHard=true`: manifest or contract validation failures raise loudly. Do
  not silently omit the SessionDB manifest to make registry tests pass.
- `failHard=false`: invalid SessionDB metadata may be logged and skipped through
  the existing registry fail-soft path, but the log must identify the manifest
  validation failure.
- Runtime SessionDB storage, MemoryDB projection, transcript resolution, and
  source-window expansion behavior are not changed by this slice. Do not add a
  fallback path around those systems while adding metadata.

## Required Tests Before W4

Add or preserve focused tests proving:

- `list_datastore_manifests()` includes `sessiondb` with the expected static
  metadata and sorted registry behavior
- `get_datastore_manifest("sessiondb")` returns a copy and validates through
  `validate_datastore_manifest()`
- `build_first_party_datastore_contracts()` includes `sessiondb` and
  `validate_datastore_contract()` returns no errors for it
- SessionDB contract handler specs match only the request handlers declared in
  the SessionDB manifest
- every SessionDB manifest request handler refers to an already registered
  request event; no new event names are introduced
- MemoryDB still owns `session.ingest_log.request.v1` and the `session_chunks`
  recall selector after SessionDB metadata is added
- `session.ingest_log` active and request-path tests still pass without routing
  through a SessionDB contract
- session-memory bridge projection tests still prove SessionDB transcript rows
  and MemoryDB session chunks remain recallable through the existing path
- source-window expansion tests still pass with no changed expansion metadata
  policy

## W4 Smoke

For the first metadata-only runtime slice, W4 should source-proof the installed
registry/contract state and run a narrow session ingest smoke only if runtime
code lands after this plan:

- SessionDB appears in first-party datastore registry output.
- `session.ingest_log.request.v1` still routes through the existing MemoryDB
  handler.
- A real transcript ingest still writes SessionDB rows and projects MemoryDB
  `session_chunks` evidence.
- Recall/source-window behavior for newly indexed session evidence is unchanged.
- Lifecycle acknowledgement events still return acknowledgement envelopes and do
  not persist new datastore rows.

## Deferred Decisions

- SessionDB session-ingest helper ownership, tracked as M15 in
  `projects/quaid/operations/datastore-events-m15-sessiondb-ingest-helper-plan.md`
- whether SessionDB should ever own `session.ingest_log.request.v1`
- whether SessionDB should expose dedicated request handlers beyond generic
  metadata/maintenance surfaces
- lifecycle persistence for ack-only lifecycle events
- source-window metadata enrichment and selector ownership
- whether direct request mode should ever become the extraction default
- whether hidden CLI request-mode flags should ever become public
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
