# PROJECT.log Single-Writer Plan

Status: extraction queue path implemented; rotation follow-up pending
Owner: W1 runtime/project-system
Last updated: 2026-05-19

Related plan: `projects/quaid/operations/project-docs-supervisor-plan.md`.

## Implementation Record

The extraction/project-docs-worker single-writer path is implemented on this
branch:

- `ed59ffc9b` added `datastore.docsdb.project_log_queue`, moved extraction
  project-log side effects to durable queue writes, added queue-aware project
  status, and drained queued items from the project-docs worker path.
- `71be469ae` kept queue drain append-only by passing
  `update_project_md=False` and `index_history=False` to the visible commit
  primitive; the worker then reads `PROJECT.log` from the cursor and lets the
  project-docs update path own `PROJECT.md` and indexing.
- `986c299fd` routed extraction and worker access through the
  `core.docs.updater` wrapper so ingest/core callers do not import the
  datastore queue module directly.
- `f845f1b98` materialized valid queued transcript-driven projects through the
  project-docs monitor path and skipped deleted/reserved queued projects.
- `90d0f03d1` kept project-log indexing behind the core project-docs boundary.

Current source proof:

- `ingest.extract.apply_extracted_payloads()` calls `enqueue_project_logs()`
  after MemoryDB publish and snippet/journal writes, records queue metrics, and
  does not claim visible `PROJECT.log` writes.
- `core.project_docs.execute_update_once()` enters `project_update_lock()`,
  calls `_commit_queued_project_logs()`, then reads `PROJECT.log` from the
  stored cursor and routes the resulting project-log entries through the
  DocsDB project-doc update request path.
- `datastore.docsdb.project_log_queue` owns durable file-per-item queue
  persistence and failHard queue-write behavior.

Remaining explicit gap: `core.log_rotation.rotate_project_logs()` can still
rewrite `PROJECT.log` outside the project-docs worker lock. That rotation path
must be routed through the project-docs worker or guarded by the same
`project_update_lock(project)` before this plan can be marked fully closed.
This docs update does not approve or implement the rotation follow-up.

## Problem

At plan-open time, `PROJECT.log` and the `PROJECT.md` recent-log block could be
written by multiple instance extraction daemons. Extraction publish routed
through `ingest.extract.apply_extracted_payloads()`, which called
`core.docs.updater.append_project_logs()`, immediately appended `PROJECT.log`,
rewrote `PROJECT.md`, and indexed the log.

That makes the write path unsafe for shared projects:

- multiple instance extractors can append to the same project chronology;
- `PROJECT.md` is a read-modify-write surface and can lose concurrent updates;
- immediate indexing can race with another append or with the project-docs cursor;
- the project-docs worker already has the correct per-project lock, but extraction
  bypassed it.

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

Implemented datastore-owned module:

```text
modules/quaid/datastore/docsdb/project_log_queue.py
```

Implemented APIs:

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
Its flow now drains the queue inside `project_update_lock(project)`.

Worker update sequence:

1. Acquire `project_update_lock(project)`.
2. Drain queued project-log items for the project in deterministic order.
3. Commit drained entries to `PROJECT.log` only.
4. Mark queue items committed after the append-only `PROJECT.log` write succeeds.
5. Read `PROJECT.log` from the stored cursor, including the just-appended bytes.
6. Run the existing project-docs update from that cursor slice.
7. Let the project-docs update path be the sole `PROJECT.md` writer for those
   entries; the queue drain must not also rewrite `PROJECT.md`.
8. Reindex registered docs and `PROJECT.log`.
9. Advance `project_log_offset` only after successful apply/index.

If the append-only `PROJECT.log` write succeeds but the docs update or index step
fails, the queue item should remain committed and the stored project-log cursor
should remain old. On the next worker tick, the existing cursor mechanism will
re-read the appended log bytes without duplicating the append.

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

`core/log_rotation.py` still mutates `PROJECT.log`. It must not remain a second
visible writer before this plan is fully closed.

Launch-safe options:

- run rotation through the project-docs worker; or
- require rotation to acquire the same `project_update_lock(project)`.

The long-term preference is that rotation becomes another project-docs worker
operation.

### Selected Rotation Follow-Up Slice

The first runtime follow-up should use the smallest lock-safe shape:

1. Keep `core.log_rotation.rotate_log_file()` generic and unchanged.
2. Update `core.log_rotation.rotate_project_logs()` so each visible
   `PROJECT.log` rotation acquires `core.project_docs.project_update_lock()`
   for that project before calling `rotate_log_file()`.
3. Use `blocking=False` for the project lock. If a project-docs worker is
   already updating the project, skip rotation for that project and log an
   informational message; do not wait inside janitor and do not rewrite the log
   concurrently.
4. If resolving or acquiring the lock raises unexpectedly, preserve failHard
   behavior: raise under `retrieval.fail_hard=true`; log and skip that project
   under fail-soft mode.
5. Leave `rotate_journal_logs()` unchanged. Journal rotation is EvolutionDB
   lifecycle work and is not part of the project-log single-writer gap.

Non-targets for this slice:

- no queue format changes
- no project-docs request-event or worker-loop changes
- no `PROJECT.md`, docs index, source-window, ranking, or recall policy changes
- no default janitor scheduling change
- no move of rotation into a new DocsDB request event

Required tests before W4:

- `rotate_project_logs()` acquires the same per-project lock used by
  `project_docs.execute_update_once()` before rotating a managed `PROJECT.log`.
- When that lock is already held, `rotate_project_logs()` skips the project and
  leaves the live `PROJECT.log` unchanged.
- Existing log-rotation behavior for unlocked projects, hidden directories,
  missing project dirs, projects without `PROJECT.log`, and journal logs remains
  unchanged.

W4 smoke for the runtime follow-up should prove an installed janitor/log
rotation run does not disturb queued project-log worker behavior and that a
locked project is skipped rather than rewritten concurrently.

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
- worker drain does not directly update `PROJECT.md`;
- failed worker append leaves that queue item pending while later valid items can
  still drain under fail-soft mode;
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
6. Confirm `PROJECT.md` is updated by the project-docs update path, not by the
   queue drain itself.
7. Confirm `quaid project status <project>` becomes fresh after the worker run.
8. Confirm docs recall can retrieve the newly indexed `PROJECT.log` entries.

## Implementation Checklist

1. [x] Add `datastore.docsdb.project_log_queue`.
2. [x] Add queue-aware project status.
3. [x] Add queue drain inside `project_docs.execute_update_once()`.
4. [x] Move extraction publish to queue project logs instead of direct append.
5. [x] Make `append_project_logs()` monitor-owned by convention and tests for
   runtime producers.
6. [ ] Route or lock log rotation.
7. [x] Add focused unit/integration tests for the queue and worker path.
8. [ ] Run live multi-instance shared-project canary when the rotation
   follow-up is selected.
