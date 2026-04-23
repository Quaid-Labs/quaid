# PROJECT.log Single-Writer Plan

Status: launch-blocking design plan
Owner: W1 runtime/project-system
Last updated: 2026-04-23

Related plan: `projects/quaid/operations/project-docs-supervisor-plan.md`.

## Problem

`PROJECT.log` and the `PROJECT.md` recent-log block can currently be written by
multiple instance extraction daemons. Extraction publish routes through
`ingest.extract.apply_extracted_payloads()`, which calls
`core.docs.updater.append_project_logs()`, which immediately appends
`PROJECT.log`, rewrites `PROJECT.md`, and indexes the log.

That makes the write path unsafe for shared projects:

- multiple instance extractors can append to the same project chronology;
- `PROJECT.md` is a read-modify-write surface and can lose concurrent updates;
- immediate indexing can race with another append or with the project-docs cursor;
- the project-docs worker already has the correct per-project lock, but extraction
  currently bypasses it.

The launch invariant is simple: visible project artifacts must have one writer
per project.

## Decision

Use the docs datastore as the request router. Extraction and other producers
submit durable project-log write intent into a datastore-owned queue. The existing
project-docs worker drains that queue inside the normal project-docs update loop
and remains the only writer of visible project files.

This keeps the direction aligned with the intended architecture:

- ingest/extraction emits facts, snippets, journal entries, and project-log intent;
- datastore owns durable persistence semantics for docs/project-log work units;
- core/project-docs owns lifecycle, worker locking, status, and update
  orchestration;
- visible project files are committed only by the project-docs monitor.

This is also the shape needed later when datastores submit prompt chunks to the
extraction engine and digest the returned results.

## Queue Ownership

Add a datastore-owned module:

```text
modules/quaid/datastore/docsdb/project_log_queue.py
```

Near-term APIs:

```python
enqueue_project_logs(
    project_logs: dict[str, list[str]],
    *,
    trigger: str,
    date_str: str | None = None,
    session_id: str | None = None,
    owner_id: str | None = None,
    source_instance: str | None = None,
    source_adapter: str | None = None,
) -> dict[str, int]

pending_project_log_count(project: str) -> int

drain_project_log_queue(project: str, *, limit: int | None = None) -> list[dict]

mark_project_log_queue_committed(project: str, item_ids: list[str]) -> None
```

The queue module should validate project names and normalize entries enough to
avoid corrupt queue files, but the final append/reroute behavior remains in the
monitor-owned commit path.

## Durable Queue Format

Use file-per-item queue records, not a shared append file. This avoids recreating
the same concurrency problem in the queue.

Suggested location:

```text
${QUAID_HOME}/data/project-docs/project-log-queue/<project>/<created_ns>-<pid>-<uuid>.json
```

Write protocol:

1. Create parent directory.
2. Write JSON to a temp file in the same directory.
3. `fsync` the temp file.
4. Atomic rename into place.

Queue item fields:

```json
{
  "id": "<created_ns>-<pid>-<uuid>",
  "project": "quaid",
  "entries": ["..."],
  "trigger": "Reset",
  "date_str": "2026-04-23T12:00:00",
  "session_id": "...",
  "owner_id": "...",
  "source_instance": "...",
  "source_adapter": "codex",
  "created_at": "2026-04-23T12:00:00Z"
}
```

## Runtime Publish Flow

Change extraction publish behavior:

1. `apply_extracted_payloads()` still builds and synthesizes `result["project_logs"]`.
2. Instead of calling `append_project_logs()` directly, it calls
   `project_log_queue.enqueue_project_logs()`.
3. It records queue metrics:
   - `entries_seen`
   - `entries_queued`
   - `projects_queued`
   - `queue_failures`
4. Runtime publish must not claim `entries_written`; only the project-docs worker
   can report visible writes.

Under `retrieval.fail_hard=true`, queue write failure raises. Under
`fail_hard=false`, queue write failure logs loudly and returns failure metrics.

## Project-Docs Worker Drain Flow

The existing project-docs worker is already the right single-writer boundary.
Update its flow so queue drain happens inside `project_update_lock(project)`.

Worker update sequence:

1. Acquire `project_update_lock(project)`.
2. Drain queued project-log items for the project in deterministic order.
3. Commit drained entries to `PROJECT.log`.
4. Update the `PROJECT.md` recent-log block using the same entries.
5. Read `PROJECT.log` from the stored cursor.
6. Run the existing project-docs update.
7. Reindex registered docs and `PROJECT.log`.
8. Advance `project_log_offset` only after successful apply/index.
9. Mark queue items committed only after the visible-file commit and update
   transaction succeeds.

If any visible-file write, docs update, or index step fails, leave queue items
pending. This makes recovery deterministic on the next worker tick.

## Staleness And Wakeup

`project_status(project)` should treat pending queue items as stale. The worker
loop already updates when status is stale, so queue presence becomes a natural
due condition.

Enqueue may also update hidden project-docs state to `status=queued`, but it must
not overwrite a real force-update request. Force requests and queued project-log
items are independent due conditions for the same worker.

## Unknown Or Deleted Projects

Current project-log append behavior reroutes some unknown/deleted project entries
to the `quaid` project with a source prefix. Keep that behavior centralized in
the monitor-owned commit path so all producers share the same policy.

The queue layer should not silently invent projects. It should either:

- enqueue under the named project when the project is registered;
- route to the known fallback project if the commit policy says to do so;
- or fail loudly under `fail_hard=true`.

## Rotation And Other Writers

`core/log_rotation.py` also mutates `PROJECT.log`. It must not remain a second
visible writer.

Launch-safe options:

- run rotation through the project-docs worker; or
- require rotation to acquire the same `project_update_lock(project)`.

The long-term preference is that rotation becomes another project-docs worker
operation.

## Migration Notes

`datastore.docsdb.project_updater.append_project_logs()` should become a
monitor-owned commit primitive, not a general runtime API. It can remain in place
for the first implementation if all non-monitor callers are moved to the queue.

Code comments and tests should make this boundary explicit:

- runtime producers enqueue;
- project-docs monitor commits;
- no adapter or instance daemon writes visible project files directly.

## Validation

Required tests:

- queue writes from multiple simulated producers create all queue items;
- queue write uses temp file plus atomic rename;
- project status reports stale when pending queue items exist;
- worker drain appends all queued entries exactly once;
- worker drain updates `PROJECT.md` recent-log block without losing entries;
- failed worker commit leaves queue items pending;
- successful worker commit removes or marks queue items committed;
- `project_log_offset` advances only after successful update/index;
- `fail_hard=true` raises on queue/commit failure;
- `fail_hard=false` logs loudly and preserves recoverable state;
- rotation cannot run concurrently with queue drain.

Live validation:

1. Create one shared project linked to multiple adapter instances.
2. Trigger project-log entries from CC, CDX, and OC close together.
3. Confirm all entries land as queue items.
4. Confirm one project-docs worker drains and commits them.
5. Confirm `PROJECT.log` contains every entry once.
6. Confirm `PROJECT.md` recent-log block has no lost update.
7. Confirm `quaid project status <project>` becomes fresh after the worker run.
8. Confirm docs recall can retrieve the newly indexed `PROJECT.log` entries.

## Implementation Checklist

1. Add `datastore.docsdb.project_log_queue`.
2. Add queue-aware project status.
3. Add queue drain inside `project_docs.execute_update_once()`.
4. Move extraction publish to queue project logs instead of direct append.
5. Make `append_project_logs()` monitor-owned by convention and tests.
6. Route or lock log rotation.
7. Add focused unit/integration tests.
8. Run live multi-instance shared-project canary.
