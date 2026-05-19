# Datastore Events M9.4 Extraction Fact Publish Plan

Status: synchronous helper and daemon request-event slices complete
Owner: W1 runtime/datastore, W3 recall quality
Plan source: `projects/quaid/operations/datastore-events-m9-monitor-migration-plan.md`

## Precondition

Do not implement runtime code for M9.4 until:

1. M9.3 session ingest slices are closed through W4/W6/W8.
2. W3 reviews the selected extraction publish slice because facts, edges,
   source chunks, provenance, and dedup behavior are recall-visible.
3. W6 reviews ownership boundaries so this slice does not move NoteDB,
   DocsDB, or lifecycle side effects into MemoryDB by accident.
4. W8 confirms the proposed static lanes cover extraction, memory writes,
   event routing, and boundary checks.

## Goal

M9.4 should move the selected extraction fact publish write family behind
MemoryDB ownership while preserving normal product behavior.

The selected write family is:

- MemoryDB facts
- MemoryDB graph edges
- MemoryDB extraction source chunks / source evidence metadata
- publish counters, dedup telemetry, and provenance fields tied to those writes

The goal is not to rewrite extraction, ranking, recall, or source-window policy.
Extraction remains the producer of structured payloads; MemoryDB owns durable
fact/edge/source evidence persistence.

## Current Boundary

Current path for daemon rolling flush:

1. `core/extraction_daemon.py` builds a final `flush_payload` from staged
   extraction payloads and optional tail extraction.
2. `core/extraction_daemon.py` calls
   `ingest.extract.apply_extracted_payloads(flush_payload, ...)` directly.
3. `apply_extracted_payloads()` stores source chunks, facts, and edges through
   the memory/session bridge ports.
4. The same function also handles non-MemoryDB side effects:
   - USER/project snippets through NoteDB-style snippet writers
   - journal entries
   - project-log queueing through DocsDB/project-docs machinery
5. The daemon reads the result counters (`facts_stored`, `facts_skipped`,
   `edges_created`, source chunk counters, project-log metrics) and writes
   runtime metrics, notifications, session-log ingest, cursor advancement, and
   extraction buffer logs.

Current path for direct extraction/CLI:

1. `ingest.extract.extract_from_transcript()` extracts payloads.
2. It calls `apply_extracted_payloads()` before returning unless running in a
   staged/dry-run flow.

## Design Constraint

Do not wrap all of `apply_extracted_payloads()` as a MemoryDB request handler.
That would incorrectly make MemoryDB own unrelated snippet, journal, and
project-log side effects just because they are currently co-located in one
function.

W3/W6 review selected the first behavior shape:

- Keep `apply_extracted_payloads()` as the orchestration entrypoint for the
  current daemon/direct extraction publish flow.
- Factor only the MemoryDB-owned publish portion into a MemoryDB-owned helper
  or contract helper.
- Call that helper from inside `apply_extracted_payloads()` for the selected
  facts/edges/source-chunks publish family.
- Leave snippet, journal, and project-log side effects in their current owners
  and call order; do not move those writes into the MemoryDB helper.
- Do not split the daemon into separate `memorydb_publish()` and
  `apply_extracted_payloads_non_memorydb_side_effects()` calls in the first
  behavior patch. That would duplicate orchestration and increase the risk of
  changing current side-effect ordering or result shape.

The first runtime patch may use only a synchronous MemoryDB-owned helper if that
is the safest step. If a request event is introduced in the same patch, the
request handler must delegate to the same helper and preserve failHard/no-fallback
semantics.

## Candidate First Runtime Slice

Candidate event/request:

- `memory.extraction_publish.request.v1`

Candidate owner:

- MemoryDB, via `core.plugins.memorydb_contract` or a MemoryDB-owned publish
  module.

Candidate producer:

- daemon final rolling flush publish path in `core/extraction_daemon.py`

The direct `extract_from_transcript()` publish path is not selected for request
routing in the first behavior slice. A transparent internal helper extraction
inside `apply_extracted_payloads()` is allowed only if parity tests prove direct
extraction output is unchanged. Any direct-path request/broker migration is a
future slice.

Candidate payload:

- raw facts selected for publish
- raw extraction source chunks selected for publish
- owner/session/source identity fields:
  - `owner_id`
  - `session_id`
  - `label`
  - `actor_id`
  - `speaker_entity_id`
  - `subject_entity_id`
  - `participant_entity_ids`
  - `source_channel`
  - `source_conversation_id`
  - `source_author_id`
  - `target_datastore` only as the existing MemoryDB store pass-through value;
    do not introduce new accepted values or future-proofed routing semantics in
    this slice
- publish controls:
  - `dry_run`
  - owner-side domain policy snapshot resolved inside the MemoryDB boundary;
    producers must not be trusted as the authoritative allow-list source
  - publish batch size behavior, if configurable

Candidate response:

- the same MemoryDB publish counters the daemon currently consumes:
  - `facts_stored`
  - `facts_skipped`
  - `edges_created`
  - `source_chunks_stored`
  - `source_chunks_existing`
  - `source_chunks_failed`
  - `publish_batches`
  - dedup telemetry counters
  - `facts` result rows used for notifications/debugging

The response must preserve enough result shape for daemon metrics, notifications,
publish traces, and extraction buffer logs to remain unchanged.

The daemon-side response validator should validate only the status and counters
the daemon actually consumes. Internal implementation details such as transaction
mode, rowid-window rechecks, or dedup SQL mechanics belong in MemoryDB helper
tests, not as speculative response-envelope validation.

## Non-Targets

- no snippet writes
- no journal writes
- no project-log queueing
- no DocsDB project update/index behavior
- no lifecycle ack events
- no session ingest request changes from M9.3
- no recall planner/ranking/scoring changes
- no source-window behavior changes
- no broad rewrite of `extract_from_transcript()`
- no broad split of `ingest.extract` beyond the minimum required for the
  selected MemoryDB publish boundary

## FailHard Policy

- `failHard=true`: request dispatch, handler validation, source chunk storage,
  fact storage, edge creation, domain validation, or MemoryDB transaction failure
  must raise through the daemon. Do not fall back to direct
  `apply_extracted_payloads()` after listener/broker failure.
- `failHard=false`: degraded behavior may remain only where current behavior
  already degrades, and it must log loudly without claiming facts were stored.
- MemoryDB write failures must preserve existing exception identity or
  contextual `RuntimeError(... ) from exc` behavior where current code relies on
  it.

## Parity Invariants

Implementation must preserve:

- source chunk materialization and fact `source_chunk_id` attachment
- fact temporal normalization (`created_at`, `mentioned_at`, project-log date
  hints)
- domain normalization and unsupported-domain skip behavior
- payload duplicate collapse counters
- embedding prewarm telemetry
- publish batching and `BEGIN IMMEDIATE` transaction behavior
- rowid window / delta dedup recheck behavior
- exact `facts_stored`, `facts_skipped`, `edges_created`, and
  `publish_batches` semantics
- dedup telemetry accumulation
- edge creation only for stored/updated facts with a `source_fact_id`
- circuit-breaker behavior
- publish trace event names and data needed for debugging
- daemon metrics and notifications that consume publish results

## Required Tests Before W4

Add or preserve tests proving:

- MemoryDB publish request/helper produces the same fact rows, edge rows,
  source chunk rows, counters, and dedup telemetry as the current direct path.
- daemon final flush routes through the selected MemoryDB publish boundary and
  no longer falls back to the direct MemoryDB publish path after request failure.
- failHard request/handler/write failure raises through daemon.
- fail-soft failure logs loudly and does not increment stored counters.
- snippet, journal, and project-log side effects are not pulled into the
  MemoryDB request handler.
- publish batching still uses bounded batches and shared write connections for
  fact and edge writes.
- source chunk ids are attached to facts before storage, with source chunk
  materialization and fact `source_chunk_id` attachment kept in the same
  MemoryDB-owned helper/request path.
- recall can retrieve newly stored facts and linked source evidence with the
  same provenance fields as before.

## W4 Smoke

W4 should smoke the runtime patch only after W3/W6/W8 review:

- normal extraction final flush stores at least one fact and edge
- `quaid recall` or equivalent memory lookup can retrieve the newly stored fact
- source evidence/provenance remains available for the stored fact
- failHard handler failure stops the daemon path without direct fallback
- M9.3 session ingest routes still work

## Resolved First-Slice Decisions

- First behavior slice: daemon final rolling flush only.
- Direct `extract_from_transcript()` request routing remains unchanged unless a
  later slice explicitly selects and parity-tests it.
- Split shape: `apply_extracted_payloads()` internally delegates the selected
  MemoryDB fact/edge/source-chunk publish work to a MemoryDB-owned helper while
  preserving the current non-MemoryDB side effects in their existing owners.
- Domain allow-list resolution and validation belong inside the MemoryDB-owned
  boundary, not in producer-supplied policy.
- Source chunk materialization and fact `source_chunk_id` attachment stay in the
  same MemoryDB-owned helper/request path.
- A synchronous helper is the safest first implementation. A request event is
  allowed only if it delegates to that same helper and preserves request,
  failHard, and no-fallback semantics.

## First Runtime Slice Closure

The synchronous helper slice closed at:

- `65dbab41d` `refactor(datastore): route extraction publish through MemoryDB`
- `045883370` `fix(datastore): initialize extraction publish dry-run counter`

Implemented shape:

- `apply_extracted_payloads()` remains the orchestration entrypoint.
- MemoryDB-owned fact, edge, source-chunk materialization, domain policy,
  dedup telemetry, batching, and provenance publish logic moved behind
  `core.plugins.memorydb_contract.run_extraction_publish_payload()`, which
  delegates to `datastore.memorydb.extraction_publish`.
- Snippet, journal, and project-log side effects remain in `ingest.extract`
  after the MemoryDB helper returns.
- The helper resolves domain policy inside the MemoryDB boundary and ignores
  producer-supplied `allowed_domains` as an authoritative policy source.
- The follow-up initializes `facts_planned` for direct dry-run helper callers
  and documents the helper return-value versus `result["facts"]` mutation
  contract. It also documents that snippet, journal, and project-log counts are
  trace-only telemetry and not MemoryDB-owned writes.

Closure evidence:

- W4 live PASS for `65dbab41d`; W4 source-proof PASS for `045883370`.
- W3 recall review PASS/no findings for `65dbab41d`; no W3 re-review needed
  for the dry-run counter/docstring follow-up.
- W6 approved `65dbab41d` with low follow-up findings; `045883370` closed
  those findings with W6 APPROVED/no findings.
- W8 static PASS and runtime hold close recorded for the pair.

## Request Event Runtime Slice Closure

The daemon final rolling flush request-event slice closed at:

- `9acb2da60` `refactor(datastore): route extraction publish through request event`
- `41f4aacf8` `fix(datastore): log extraction publish request failures`

Implemented shape:

- `memory.extraction_publish.request.v1` is registered in `core.runtime.events`
  with `delivery_mode=request`, `fireable=True`, and `processable=False`.
- MemoryDB declares the request in both the first-party datastore manifest and
  `MemoryDbDatastoreContract` handler specs.
- `core.plugins.memorydb_contract.handle_extraction_publish_request()` unwraps
  the request payload, validates `source`, `result`, `owner_id`, and `label`,
  then delegates to the same `run_extraction_publish_payload()` helper used by
  the synchronous direct path.
- `apply_extracted_payloads()` gained an explicit `memory_publish_mode` kwarg.
  The default remains `"direct"` for direct `extract_from_transcript()` and CLI
  callers; the daemon final rolling flush passes `"request"`.
- The daemon still calls `apply_extracted_payloads()` once. It does not bypass
  the ingest orchestrator or call the broker directly.
- Snippet, journal, and project-log side effects remain in `ingest.extract` and
  run after MemoryDB publish returns in both direct and request modes.
- Request-mode broker, handler, validation, or MemoryDB write failures do not
  fall back to the synchronous helper.

Follow-up hardening:

- `41f4aacf8` routes every extraction publish broker-response validation failure
  through a single warning-before-raise helper, restoring the failHard diagnostic
  discipline for all malformed or failed response shapes.
- The MemoryDB request handler now rejects missing or empty `owner_id` and
  `label` rather than silently coercing them to `default`/`unknown`.

Closure evidence:

- W4 live PASS for `9acb2da60`, including R201 deploy, clean daemon restart,
  request event registration, manifest/contract binding, daemon request mode,
  no-fallback behavior, invalid-mode guard, side-effect ordering, and M9.2/M9.3
  route stability.
- W3 recall/provenance review PASS/no findings for `9acb2da60`; W3 was notified
  that `41f4aacf8` was diagnostics/payload-guard hardening only with no recall
  or source/provenance behavior change.
- W6 approved `9acb2da60` with two follow-up findings; `41f4aacf8` closed both
  with W6 APPROVED/no findings.
- W8 static PASS and runtime hold close recorded for the pair.

M9.4 request routing is now implemented for the selected daemon final rolling
flush producer. Direct `extract_from_transcript()` / CLI request routing remains
explicitly deferred.

## Deferred Decisions

- Whether a later slice should migrate direct `extract_from_transcript()` request
  routing.
- Whether future producer payloads need additional source metadata fields after
  the selected daemon final flush path is stable.
