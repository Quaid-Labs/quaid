# Datastore Events M9.3 Lifecycle And Session Plan

Status: draft coordination plan; no runtime implementation
Owner: W1 runtime/datastore
Plan source: `projects/quaid/operations/datastore-events-m9-monitor-migration-plan.md`

## Precondition

Do not implement runtime code for M9.3 until:

1. M9.2 project-doc update migration passes W3/W4/W6/W8.
2. W4 confirms project-doc worker updates, docs recall/search, and worker
   status output remain stable after M9.2.
3. W6 confirms M9.2 did not leave fallback calls to the removed selected worker
   apply/index path or touch unrelated monitor/write families.
4. W3 reviews the M9.3 implementation plan because session log ingestion feeds
   `session_chunks` recall evidence and can affect recall-visible source-window
   behavior.

This document is planning only. It does not approve runtime implementation.

## M9.3 Goal

M9.3 migrates lifecycle/session observed writes so runtime monitors become
producers and the owning datastore boundary performs session persistence and
projection.

Current lifecycle events such as `session.new`, `session.reset`,
`session.compaction`, `session.timeout`, `session.agent_start`, and
`session.agent_end` are ack-only in `core/runtime/events.py`. The current
session-facing datastore write is `session.ingest_log`, plus direct daemon calls
to `core.ingest_runtime.run_session_logs_ingest()` after extraction flushes.

The first behavior slice should target that session-log ingest write path, not
the ack-only lifecycle events.

## Current Boundary

Current session-log ingest path:

1. `core/extraction_daemon.py` directly calls
   `core.ingest_runtime.run_session_logs_ingest()` on both no-new-content
   session-end handling and normal final flush handling.
2. `core/runtime/events.py` also handles `session.ingest_log` by calling
   `core.ingest_runtime.run_session_logs_ingest()`.
3. `core/ingest_runtime.py` imports `ingest.session_logs_ingest.run()`.
4. `ingest/session_logs_ingest.py` resolves the transcript source, adapter-parses
   host JSONL when needed, then calls
   `core.services.session_memory_bridge.get_session_memory_bridge().store_session_transcript()`.
5. `core/services/session_memory_bridge.py` stores the transcript through
   `sessiondb` callback routes and projects microchunk evidence into `memorydb`
   session chunks.

The active event bus already exists. M9.3 must evolve or reuse it; it must not
introduce a parallel lifecycle queue or duplicate registry.

## SessionDB Manifest Boundary

M2 intentionally did not register `sessiondb` as a first-party datastore
manifest. SessionDB currently participates through
`core.services.session_memory_bridge` as transcript/provenance plumbing and
projects recallable evidence into `memorydb` session chunks.

Before any behavior patch, implementation review must choose one explicit
ownership model:

- add a first-party `sessiondb` manifest and contract for session transcript
  writes, with `memorydb` projection called from the datastore-owned handler, or
- keep the first slice owned by `memorydb`/the session-memory bridge and record
  that `sessiondb` manifest registration remains deferred to a dedicated
  source-window slice.

Do not silently add `sessiondb` to manifests in the same patch as a behavior
migration unless W3/W6 have reviewed that ownership decision.

## Proposed First Slice

First slice target: replace the direct daemon-to-ingest session-log write calls
with a broker/event request that preserves the current synchronous outcome and
failHard behavior.

Selected producer paths:

- no-new-content `session_end` path in `core/extraction_daemon.py`
- normal final flush path in `core/extraction_daemon.py`

Selected handler path:

- the datastore-owned session-log ingest handler that resolves transcript
  source, stores SessionDB transcript records, and projects session chunks into
  MemoryDB evidence.

The implementation may reuse `session.ingest_log` if it can preserve current
synchronous error propagation and result logging, or it may introduce a request
event such as `session.ingest_log.request.v1` if request/fanin semantics are
needed. The patch must delete the replaced direct daemon calls in the same patch
and must not fall back to the old direct call after broker/listener failure.

## Deferred Sub-Slices

Defer these until separate review:

- changing the ack-only lifecycle handlers for `session.new`, `session.reset`,
  `session.compaction`, `session.timeout`, `session.agent_start`, or
  `session.agent_end`
- moving extraction fact writes or fact-batch persistence
- changing cursor advancement, signal processing locks, staged payload flushes,
  or extraction buffer behavior
- changing session transcript chunking, microchunk linking, or recall result
  formatting
- changing `session_chunks` routed/default recall behavior
- changing adapter lifecycle delivery surfaces

## Event Contract Requirements

The first slice must preserve the current session ingest inputs:

- `session_id`
- `owner_id`
- `label`
- `session_file`
- `transcript_path`
- `source_channel`
- `conversation_id`
- `participant_ids`
- `participant_aliases`
- `message_count`
- `topic_hint`

Payload must not include credentials, environment dumps, unrelated transcript
bodies, or unbounded session files beyond the transcript path/source already
used by the current ingest path.

Handler response must preserve the current result fields the daemon logs and
tests assert:

- `status`
- `reason` when skipped
- `session_id`
- `source_kind`
- chunk/index counts supplied by the session-memory bridge result

## Non-Targets

- docs registration/index paths from M9.1 and M9.2
- project-doc worker updates
- docs RAG recall/search behavior
- memory fact extraction writes
- graph/vector recall ranking or result formatting
- `session_chunks` recall planner behavior
- lifecycle ack event semantics
- public CLI syntax changes

## FailHard Policy

- `failHard=true`: broker/listener validation, dispatch, transcript resolution,
  SessionDB storage, or MemoryDB projection failure must raise through the
  daemon path. Do not fall back to the removed direct call.
- `failHard=false`: the daemon may preserve current warning/log behavior, but
  the failure must be loud and must not report the session log as indexed.
- Queue/read/write failures in the event path remain failHard-sensitive per the
  existing broker contract.

## Parity Invariants

Implementation must preserve:

- no-new-content session-end behavior, including stale-signal cleanup before a
  failHard raise
- normal final flush logging and status handling
- transcript source resolution order: explicit transcript path, session file,
  adapter session path, then missing
- adapter parsing for host JSONL transcripts before SessionDB storage
- participant id and alias normalization
- source channel, conversation id, message count, and topic hint forwarding
- SessionDB transcript row shape
- MemoryDB session chunk projection and microchunk linkage
- `source_kind` and skip reason semantics
- `session_chunks` recallability for newly indexed transcripts

## Required Tests Before W4

Add or preserve focused tests proving:

- daemon no-new-content path uses the broker/event request and no longer calls
  `run_session_logs_ingest()` directly
- daemon final flush path uses the broker/event request and no longer calls
  `run_session_logs_ingest()` directly
- `session.ingest_log` or the new request handler preserves payload forwarding
  and result shape
- transcript-path JSONL still adapter-parses before SessionDB storage
- fail-soft handler failure logs loudly and does not claim indexed status
- failHard handler failure raises through the daemon path without fallback
- SessionDB transcript rows and MemoryDB session chunk projection remain
  recallable through `session_chunks`

## W4 Smoke

After W3/W6/W8 review, W4 should smoke:

- session end with no new content still processes/cleans signals correctly
- normal final flush indexes a real session transcript
- `session_chunks` recall finds newly indexed session evidence
- failHard surfaces handler validation or execution failure
- CC/CDX/OC lifecycle milestones still complete without new deferred-notice or
  compaction regressions

## Handoff Criteria

M9.3 first slice is complete only when:

- selected daemon direct session-log ingest calls are removed
- W4 confirms lifecycle/session milestones and `session_chunks` recall remain
  stable
- W3 confirms no recall-visible session evidence behavior changed, or
  explicitly approves any observed change
- W6 confirms no unrelated extraction/fact write path migrated in the same patch
- W8 static validation passes focused event, session ingest, session bridge, and
  extraction daemon suites
