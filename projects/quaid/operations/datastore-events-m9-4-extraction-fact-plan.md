# Datastore Events M9.4 Extraction Fact Publish Plan

Status: draft plan; no runtime implementation
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

The first M9.4 runtime patch must either:

- factor the MemoryDB-owned publish portion into a helper/contract while
  leaving NoteDB/DocsDB side effects in their current owners, or
- explicitly select a narrower no-runtime/test-only guard slice if factoring is
  too risky for the next step.

## Candidate First Runtime Slice

Candidate event/request:

- `memory.extraction_publish.request.v1`

Candidate owner:

- MemoryDB, via `core.plugins.memorydb_contract` or a MemoryDB-owned publish
  module.

Candidate producer:

- daemon final rolling flush publish path in `core/extraction_daemon.py`

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
  - `target_datastore`
- publish controls:
  - `dry_run`
  - `allowed_domains` or a clearly defined owner-side domain snapshot
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
- source chunk ids are attached to facts before storage.
- recall can retrieve newly stored facts and linked source evidence with the
  same provenance fields as before.

## W4 Smoke

W4 should smoke the runtime patch only after W3/W6/W8 review:

- normal extraction final flush stores at least one fact and edge
- `quaid recall` or equivalent memory lookup can retrieve the newly stored fact
- source evidence/provenance remains available for the stored fact
- failHard handler failure stops the daemon path without direct fallback
- M9.3 session ingest routes still work

## Open Decisions

- Whether the first M9.4 patch should migrate only daemon final flush or also
  direct `extract_from_transcript()` publish calls.
- Whether domain allow-list resolution belongs in the producer payload or the
  MemoryDB handler.
- Whether source chunk persistence should remain via the session-memory bridge
  port for this slice or move behind the same MemoryDB request handler.
- Whether a request event is required for the first patch or a MemoryDB-owned
  shared helper is a safer intermediate step before broker routing.

These decisions require W3/W6 review before runtime code lands.
