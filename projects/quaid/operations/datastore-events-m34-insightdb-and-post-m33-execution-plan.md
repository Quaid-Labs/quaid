# Datastore Events M34 InsightDB And Post-M33 Execution Plan

Status: selected plan; runtime implementation begins with Slice 1 only
Owner: W1 runtime/datastore, W3 for recall/source-window behavior
Plan source: Solomon instruction after M33: continue remaining datastore refactor work, do not do `.ego`, final clear gated on W4/W8

## Operator Direction

Solomon selected the remaining post-M33 datastore refactor work, with one explicit hold:

- Do continue non-`.ego` post-M33 datastore refactor work.
- Do not do `.ego` import/export work until Solomon says so.
- Use W4 live/installed validation along the way for runtime slices.
- Gate final clear on W4 and W8 full clear.
- Correct the naming target: the former EvolutionDB / NoteDB surfaces should become InsightDB.

## Current Naming Reality

The repo currently has:

- legacy datastore id: `evolutiondb`
- legacy runtime package: `datastore.evolutiondb`
- legacy compatibility package: `datastore.notedb`
- legacy contract module: `core.plugins.evolutiondb_contract`
- legacy compatibility contract module: `core.plugins.notedb_contract`
- plugin id still: `notedb.core`

There are no current `insightdb` / `InsightDB` runtime surfaces before this slice. M10 only completed NoteDB -> EvolutionDB naming; Solomon clarified that the intended final name is InsightDB.

## Selected Work Buckets

M34 selects the remaining post-M33 buckets except `.ego`.

1. InsightDB canonical rename and compatibility cleanup.
2. Lifecycle hook migration and adapter direct-signal retirement.
3. Daemon restart/stop automation from lifecycle events, if still needed after hook migration.
4. Request routing and CLI exposure decisions.
5. Recall/source-window ownership work, with W3 owning the behavior contract.
6. Compatibility naming cleanup after the clean InsightDB rename, excluding `.ego`.

## Execution Rules

- Use narrow slices. Do not bundle multiple buckets into one runtime patch.
- Every runtime slice gets W4 live/installed validation, W6 review, and W8 static validation.
- W3 reviews any recall/source-window or recall-visible persistence/timing change.
- W8 integration/push remains W8-owned.
- No `.ego` import/export work in M34.
- Preserve failHard behavior. Do not add fallbacks that bypass failHard.
- Do not add migration shims unless Solomon explicitly asks; alpha users are technical and limited for this rename.

## Slice 1: Clean InsightDB Canonical Rename

Goal:

- Make InsightDB the canonical datastore name in runtime code without carrying EvolutionDB/NoteDB migration shims.

Selected runtime shape:

- Rename `datastore.evolutiondb` to `datastore.insightdb`.
- Rename `core.plugins.evolutiondb_contract` to `core.plugins.insightdb_contract`.
- Remove `datastore.notedb` and `core.plugins.notedb_contract` compatibility shims.
- Change first-party datastore manifest canonical id from `evolutiondb` to `insightdb`.
- Change plugin id from `notedb.core` to `insightdb.core`.
- Set `runtime_aliases` to `[]`; do not keep `evolutiondb` or `notedb` as aliases.
- Update request handler registration to register under datastore id `insightdb`.
- Update request-response validators to expect `insightdb` only.
- Keep snippet/journal event names unchanged in this slice: `recall.journal.request.v1`, `evolution.snippet_journal_write.request.v1`, `evolution.snippet_write.request.v1`, `evolution.journal_write.request.v1`. Event-name cleanup can be selected later if desired, but is not bundled with the datastore/package rename.

Non-targets for Slice 1:

- No snippet/journal file path changes.
- No request-mode default changes.
- No public CLI changes.
- No `.ego` work.
- No recall/source-window behavior changes.
- No lifecycle hook migration.
- No event-name rename from `evolution.*` to `insight.*` in this slice.

Required local tests before dispatch:

- Datastore registry/manifest tests for canonical `insightdb` and aliases.
- Event/request tests for snippet/journal handlers under `insightdb`.
- Extraction request-mode tests for canonical response validation.
- Janitor/lifecycle snippet tests for canonical module import.
- Static grep/source assertions prove `datastore.evolutiondb`, `datastore.notedb`, `core.plugins.evolutiondb_contract`, and `core.plugins.notedb_contract` are absent from runtime code; targeted tests may mention those strings only to assert the legacy imports are removed.
- Boundary and docs consistency checks.

W4 smoke for Slice 1:

- Installed runtime starts cleanly after deploy.
- Snippet and journal write through direct and request paths.
- Journal recall or identity/context read sees the written content.
- Runtime uses `datastore.insightdb` and `core.plugins.insightdb_contract`; legacy `datastore.evolutiondb`, `datastore.notedb`, `core.plugins.evolutiondb_contract`, and `core.plugins.notedb_contract` imports are not required.
- Existing M29-M32 lifecycle facade emitters remain healthy.

## Later Slice Sketches

### Slice 2: Post-Rename Event And Docs Naming Cleanup

- Decide whether `evolution.*` event names should stay because they describe product concepts, or be renamed to `insight.*`.
- Do not do `.ego` work.
- If event names change, migrate in one narrow runtime slice with W4/W8 gates.

### Slice 3: Lifecycle Hook Migration

- Migrate one OpenClaw lifecycle hook family at a time to facade lifecycle emitters.
- Preserve adapter-side host ownership and duplicate suppression.
- Remove replaced direct signal writes in the same patch only when proven safe.
- W4 live validation required for each migrated hook family.

### Slice 4: Adapter Direct-Signal Retirement

- Retire adapter direct-signal paths only after hook migration proves facade emission is authoritative.
- Keep rollback-free failHard behavior: no hidden fallback to direct signal writes after event/facade failure.

### Slice 5: Daemon Restart/Stop Automation

- Select only if lifecycle events should control daemon process lifecycle beyond the M28 wake path.
- Do not change daemon polling/signal processing cadence as a latency workaround.

### Slice 6: Request Routing And CLI

- Decide whether request mode becomes default.
- Decide whether hidden CLI flags become public.
- Preserve failHard: request-mode failures must not fall back to direct writes.

### Slice 7: Recall And Source-Window Ownership

- W3 owns behavior contract.
- Do not move `session_chunks`, add SessionDB recall, or change source-window ranking/planner/output policy without W3-approved tests and benchmark review.

## Final Clear

Final clear for the selected post-M33 work requires:

- all selected runtime slices closed through W4 live validation,
- W8 static/runtime full clear,
- W3 clear for recall/source-window slices,
- W6 review closure,
- docs closure recording what remains deferred,
- no `.ego` work unless Solomon separately approves it.
