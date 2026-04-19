# Project Docs Supervisor Plan

Status: active design plan
Owner: W1 runtime/project-system
Last updated: 2026-04-19

## Why This Exists

Project CRUD and registry behavior exists, but automatic project documentation updates are not currently functional enough for launch. The previous project-doc updater path was deprecated and never rebuilt into the current project architecture. This is a core Quaid pillar, so the fix is launch-critical rather than post-launch polish.

The new project-docs updater should be built in the same direction as the broader Quaid supervisor architecture instead of patching the old event-queue updater.

## Architecture Direction

The Quaid supervisor is the root runtime owner.

Near-term process tree:

```text
quaid-supervisor
  +- instance-daemon <instance-a>
  +- instance-daemon <instance-b>
  +- project-docs-monitor <project-a>
  +- project-docs-monitor <project-b>
  +- project-docs-monitor <project-c>
  +- janitor worker / scheduler
```

Long-term process tree:

```text
quaid-supervisor
  +- instance-daemon <instance-a>
  +- instance-daemon <instance-b>
  +- project-docs-monitor <project-a>
  +- project-docs-monitor <project-b>
  +- janitor worker / scheduler
  +- registered datastore/ingest runtime monitors
  +- other runtime daemons
```

If the supervisor is stopped, Quaid runtime should stop. Workers should also watchdog supervisor liveness so they exit if the supervisor disappears unexpectedly.

Terminology direction: the long-lived per-project docs process is better described
as a docs monitor than a docs daemon thread. The supervisor owns lifecycle and
process grouping; the docs monitor owns the project-docs domain operations.
Use the same language for future instance monitors when instance daemons move
under supervisor ownership.

## Tick Ownership Decision

Use a hybrid model:

- Supervisor owns lifecycle, process ownership, registration, liveness checks, and wake orchestration.
- Each daemon/worker owns its own domain tick and job logic.

Supervisor should not run project-doc freshness logic directly. The project-docs worker owns:

- shadow-git snapshot cadence
- PROJECT.log cursor checks
- quiet-window decisions
- force-update request handling
- docs update execution
- status/cursor advancement

Supervisor owns:

- starting missing project-doc workers
- restarting crashed workers
- stopping workers for deleted/disabled projects
- recording child process state
- optionally nudging/waking a worker after CLI requests
- ensuring workers are grouped under supervisor ownership

This avoids putting business logic in the supervisor and avoids a single scheduler becoming the bottleneck or failure domain for all project work.

## `ensure_alive` Direction

Current `ensure_alive` calls mostly mean "make sure this instance daemon is up." During the transition they should mean:

1. Ensure the main supervisor is up.
2. Ensure the relevant runtime component or instance is registered or alive.
3. If the component is supervisor-owned, ask the supervisor to instantiate/ensure it.
4. If the component has not been migrated yet, keep the existing legacy instance-daemon ensure path as a transitional implementation detail.

Future meaning:

- Ensure the supervisor is up and this instance/component is registered inside it.
- Supervisor owns instantiation and process lifecycle.

For project docs workers, there should be no legacy direct ownership path: the supervisor owns them from the first implementation.

## Source Of Truth

Project docs update decisions derive from durable state, not event queues.

- Project definitions identify projects, source roots, visible project home, and linked instances.
- Shadow git records source/project input changes.
- PROJECT.log is append-only chronology written by extraction/logging paths.
- Project-docs cursor/status metadata records what docs were last updated against.

Do not put updater cursor state inside the visible project tree. Cursor/status/request metadata belongs in shared hidden Quaid state, preferably shared SQLite if a clean migration path exists, otherwise atomic JSON under `QUAID_HOME/data/project-docs/`.

## CLI Semantics

Canonical commands:

```bash
quaid docs update <project>          # async force request; worker owns the update
quaid project status <project>       # freshness/status/worker/cursor summary
quaid project diff <project>         # compact source divergence since docs cursor
quaid project diff <project> --stat  # stat-only view
quaid project diff <project> --full  # explicit full diff escape hatch
```

`quaid docs update <project>` must not run a separate updater path in the CLI. It sets a force-update request. The supervisor-owned project-docs worker observes that request on its next tick or wake and owns the update.

The CLI may print request/status information, for example:

```text
Docs update requested for recipe-app.
Worker: running
Request id: <id>
Check: quaid project status recipe-app
```

## Removed/Rejected Concepts

Because Quaid is prelaunch, no compatibility layer is required for the deprecated project-doc updater model.

Remove or do not preserve:

- legacy staged project events as docs-update drivers
- `doc-health` naming
- `request-docs` semantics
- dirty docs queues/state
- benchmark-only quiet/update environment knobs
- event processor direct LLM docs writes

Legacy project events should be torn out, not kept as compatibility inputs.

## Editable And Protected Surfaces

Docs updater may edit:

- `PROJECT.md`
- `TOOLS.md`
- `AGENTS.md`
- `docs/**`

Docs updater must not edit:

- `PROJECT.log`

`PROJECT.log` is append-only. The updater reads it through a cursor and advances that cursor only after successful docs apply.

## Update Flow

Project-docs worker update job:

1. Acquire project update lock.
2. Resolve project definition/source roots/project home.
3. Snapshot source roots into shadow git if needed.
4. Select immutable shadow commit for this update.
5. Read source diff from docs cursor commit to selected commit.
6. Read PROJECT.log slice from stored cursor to selected offset.
7. Read `PROJECT.md`, `TOOLS.md`, `AGENTS.md`, and docs inventory.
8. Planner LLM emits scoped doc tasks.
9. Draft pass creates scoped edits per doc or doc group.
10. Final pass reviews all proposed edits together.
11. Apply accepted changes atomically.
12. Update registry/RAG for changed docs.
13. Advance cursor/status only after successful apply.

V1 can run low/no parallelism as long as the structure is planner -> scoped draft -> final review -> apply.

Benchmark harnesses should register benchmark projects explicitly through product
surfaces, the same way a live project would be created/linked/registered. The
docs registry reconciliation path is invariant repair and defense-in-depth for
pre-existing orphan state; it is not the normal mechanism for benchmark project
discovery.

## Due Conditions

Worker should update when one of these is true:

- force-update request exists
- shadow HEAD differs from cursor and selected HEAD has been stable for quiet window
- PROJECT.log advanced past cursor and has been stable for quiet window
- registered project repo reaches a meaningful commit boundary and is otherwise safe to update

Docs output must not reset source quiet windows or self-trigger circular updates.

## Registry And Delete Invariants

- Source deletion must not silently unregister docs.
- Missing registered docs outside docs-updater apply are stale/anomaly state.
- Only docs-updater apply transaction or project delete transaction may unregister/archive docs.
- Project delete must stop/remove project-docs monitor state.
- Deleted/disabled projects must not be resurrected by legacy staged events; legacy staged events should be removed.
- The docs monitor should own project-docs cleanup semantics: force requests,
  status/cursor state, project-doc locks, heartbeat/pid/log/temp files, and
  docs-specific shadow tracking. The supervisor may coordinate process stopping,
  but cleanup knowledge should not sprawl across registry and supervisor code.

## Implementation Milestones

### Milestone 1: State, Requests, Status, Diff, Legacy Removal

- Add hidden project-docs cursor/status/request metadata.
- Add project update lock.
- Add `quaid project status <project>`.
- Add `quaid project diff <project> [--stat|--full]`.
- Add `quaid docs update <project>` as async force-request writer.
- Remove legacy project-event docs updater driver.

### Milestone 2: Supervisor-Owned Docs Workers

- Add minimal supervisor process.
- Supervisor owns project-docs worker subprocesses.
- Worker ticks independently and handles force requests/freshness.
- Supervisor starts, restarts, and stops docs workers based on registered projects.

### Milestone 3: Docs Update Execution

- Worker executes planner -> draft -> final -> apply.
- Protect `PROJECT.log`.
- Advance cursor only on successful apply.
- Update docs registry/RAG after accepted changes.

### Milestone 4: Supervisor Integration With Existing `ensure_alive`

- Existing ensure_alive paths also ensure supervisor is up.
- Instance-daemon ownership can remain transitional until migrated.
- Future work migrates all daemons under supervisor ownership.

### Milestone 5: Registry/Delete Hardening

- Audit unregister/delete paths.
- Enforce source-deletion and missing-doc invariants.
- Ensure project delete stops docs monitor and clears monitor-owned state.
- Move tactical inline cleanup toward a docs-monitor-owned cleanup primitive so
  the project registry requests deletion but does not need to know every docs
  monitor artifact path.

### Milestone 6: Large Project And Large File Safety

- Add a bounded project-inspection planner before any content-heavy LLM prompt.
- Scan projects in fidelity layers: coarse tree summary, directory rollups, changed-path rollups, then only bounded file-content drill-down.
- Compute input size before each expansion step; stop expanding when the next layer would exceed configured byte/token/file-count budgets.
- Never expand binary files into prompt context. Catalog them by path, size, type, and change metadata only.
- Collapse huge trees into hierarchical summaries: top-level directory counts, extension/type counts, total bytes, changed bytes, top-N largest files, top-N changed dirs/files, and long-tail counts.
- Collapse huge diffs into diffstat/name-status and top-N bounded hunks; do not pass unbounded diffs to the LLM.
- Treat cap hits as catalog-only mode, not whole-project failure: docs should state that detailed content inspection was skipped due caps and rely on `PROJECT.log` or later scoped updates for semantics.
- Refuse only when even metadata scanning cannot complete within a stall/IO safety budget.
- Because docs updates are async, long wall-clock runtime is allowed. Stall protection should key off lack of heartbeat/progress movement, not elapsed duration alone.
- Expose progress through `quaid project status` and a log/tail surface: phase, current directory/file, files/bytes scanned, LLM calls queued/completed, caps hit, and recent worker log lines.

## Validation Plan

For implementation commits:

1. Local focused tests.
2. W6 code review.
3. Revise on real findings.
4. W8 static validation after W6 approval.
5. W4 live VM validation after W6 approval.

Live VM canary:

1. Create/link/register project with source root through product CLI/API surfaces.
2. Start supervisor/docs worker.
3. Change source with durable project fact/API/command.
4. `quaid project status <project>` reports stale.
5. `quaid project diff <project>` shows changed source.
6. `quaid docs update <project>` records force request.
7. Worker processes request and status becomes updating/current.
8. Docs update naturally, without benchmark-authored hints.
9. `PROJECT.log` is unchanged by updater.
10. Docs recall answers from updated project docs.

Benchmark canary:

1. Harness creates/registers benchmark projects explicitly; it must not rely on
   write-on-read reconciliation to discover project labels from docs rows.
2. Harness starts supervisor/docs monitors or foreground supervisor mode.
3. Harness waits on `quaid project status <project> --json` freshness/cursor
   agreement before scoring docs recall.
4. Harness archives status/diff/docs-list/project docs artifacts for review.

## Implementation Log

### 2026-04-19 Slice A: Supervisor-Owned Force Update Primitive

Implemented direction:
- Hidden project-docs operational state lives under `QUAID_HOME/data/project-docs/`.
- `quaid docs update <project>` queues an async force-update request and ensures the project-docs supervisor is alive.
- `quaid project status <project>` reports freshness from hidden state, pending source changes, pending `PROJECT.log` bytes, and worker/supervisor PIDs.
- `quaid project diff <project> [--stat|--full]` reports pending shadow-git changes plus pending `PROJECT.log` entries since the cursor.
- `quaid-supervisor` now owns project-docs workers; workers own their domain tick and run project updates under a per-project lock.
- Project deletion stops/removes docs worker state and pending force requests.
- Legacy staged project event processing was removed from extraction, janitor/RAG maintenance, the e2e pressure probe, and the project updater CLI.
- `PROJECT.log` append-only extraction logging remains intact and is now consumed by the docs worker cursor rather than staged events.

Validation notes:
- Focused project/docs, registry, extraction, hook, and daemon tests pass locally.
- Boundary check passes.
- Test harness sets `QUAID_SUPERVISOR_DISABLE=1` so tests do not spawn supervisor workers against the dev registry.

Open next slices:
- Improve the worker apply planner so edits are more selective across `PROJECT.md`, `TOOLS.md`, `AGENTS.md`, and `docs/**`.
- Add live VM acceptance: source change -> status stale -> diff shows source/log delta -> `docs update` queue -> worker apply -> docs recall surfaces updated fact.
- Move existing instance daemon lifecycle under the supervisor after project-doc workers are validated.

### 2026-04-19 Slice B: W6 Blocker Hardening

W6 bug-bash on `e3efe312a` found launch-blocking concurrency and data-loss risks. Revision direction implemented locally before W4/W8 handoff:

- Supervisor and worker spawn paths now run under file-lock guarded critical sections.
- PID files are JSON identity records with role/project/token metadata; bare `os.kill(pid, 0)` is no longer trusted for supervisor/worker identity.
- Stop paths validate identity before signaling or unlinking pid files, reducing PID-recycle risk.
- Worker pid writes are atomic and tied to heartbeat writes.
- Supervisor checks heartbeat staleness and resets stuck `updating` state to queued/error for retry.
- Lock contention preserves force-update requests instead of allowing a caller to observe a false successful/fresh state.

### 2026-04-19 Slice C: Docs/Project Registry Invariant

Benchmark evidence showed `recipe-app` docs rows in the docs registry while the
canonical project registry could not list or show `recipe-app`. That is an
invalid state: docs recall and docs list must not invent a project label that
the project system cannot link, status, update, or delete.

Implemented direction:
- Docs registry project definitions and project-scoped docs registrations now
  sync the canonical project registry as part of registration.
- Canonical `quaid project list/show` reconciles active docs-registry project
  metadata before reading the registry, repairing pre-existing rows created by
  older paths.
- Docs RAG linked-project scope performs the same reconciliation before
  deciding which projects are linked to the active instance.
- Direct docs rows are only reconciled when the visible project home exists;
  arbitrary stale rows are not enough to resurrect a deleted project.

Validation notes:
- Added regression coverage for direct docs registration and existing
  docs-registry rows promoting `recipe-app` into canonical project registry.
- Focused docs/project/RAG suite passes locally.
- `PROJECT.log` read/stat failures now raise and do not advance the hidden cursor.
- Project docs registry sync moved behind the `core.docs.updater` wrapper to preserve layer boundaries.
- After docs apply, the worker registers newly visible project docs, unregisters project-doc files deleted by updater apply, refreshes PROJECT.md registry sections, reindexes registered docs, and queues a deferred project-doc update notice.
- `quaid docs update <project>` remains async by default per operator direction, but now supports `--wait` and surfaces the previous error state.
- Stale `doc-health` / staged project-event instructions were removed or reframed in tracked project docs.

Validation after this revision:

- `python3 -m py_compile` on changed Python runtime files passed.
- Targeted `ruff` on changed runtime/test files passed.
- `python3 scripts/check-boundaries.py` passed.
- `tests/test_project_docs.py` passed: 9 tests.
- Impacted Python suite passed: 452 tests across project docs/updater/registry, docs hook, extraction, daemon, and CC/CDX hook coverage.
- CLI smoke covered `project create`, async `docs update --wait`, `project status`, and `supervisor stop`; no supervisor/worker process leak after stop.

### 2026-04-19 Slice D: Runtime Supervisor Migration

Implemented direction:
- The project-docs supervisor is now the root Quaid runtime supervisor.
- Registered instance daemons run as supervisor-owned instance monitors.
- Instance daemons watchdog supervisor liveness and exit when their parent
  supervisor disappears.
- The legacy extraction daemon no longer owns the janitor scheduler tick.
- Janitor scheduling now runs in a bounded supervisor-owned worker process.
- DocsDB's `project_docs_monitor` maintenance routine remains datastore-owned:
  janitor queues async project-docs monitor requests through the datastore
  lifecycle callback instead of doing heavy docs writes inline.
- Supervisor-owned janitor workers do not recursively bootstrap another
  supervisor; they trust the live supervisor parent passed in
  `QUAID_SUPERVISOR_PID`.
- `quaid daemon start` routes through supervisor ensure by default, while
  `QUAID_SUPERVISOR_DISABLE=1` preserves the legacy direct daemon path for
  controlled test/runtime escape hatches.
- `quaid supervisor --type runtime|all|project-docs` accepts the broader
  runtime supervisor terminology while keeping `project-docs` as a compatible
  selector.

Validation notes:
- Focused local coverage added for supervisor instance monitor start/stop,
  janitor worker throttling, extraction daemon supervisor-watchdog exit, and
  supervisor-owned janitor monitor requests.
- Existing project-docs removal-path tests were isolated from the new runtime
  monitor ticks so they continue testing only project-docs worker cleanup.

Open follow-up:
- W4 should add a quick live milestone covering supervisor-owned instance
  monitor lifecycle, project-docs auto-update, janitor monitor request kickoff,
  and full supervisor stop cleanup.
- W8 should fold the new focused tests into the static suite ownership path.
