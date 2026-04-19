# Runtime Supervisor Reference

Quaid uses one root runtime supervisor per `QUAID_HOME` to own long-lived
background process lifecycle. Domain workers still own their domain logic; the
supervisor owns start, stop, restart, process grouping, and cleanup.

## Process Tree

Current runtime shape:

```text
quaid-supervisor
  +- instance monitor / extraction daemon <instance-a>
  +- instance monitor / extraction daemon <instance-b>
  +- project-docs worker <project-a>
  +- project-docs worker <project-b>
  +- janitor worker / scheduler-once <instance-a>
```

The supervisor is expected to be the process-group leader. Children inherit the
supervisor process group, which lets `quaid supervisor stop` terminate the full
runtime tree rather than chasing individual worker PIDs.

## Ownership Boundaries

| Component | Lifecycle owner | Domain owner | Responsibility |
|---|---|---|---|
| Runtime supervisor | `core/project_docs_supervisor.py` | core runtime | Owns process tree, starts/stops/reaps monitors and workers |
| Instance monitor | supervisor | `core/extraction_daemon.py` | Processes extraction signals, idle checks, embedding retries, version watcher |
| Project-docs worker | supervisor | `core/project_docs_worker.py` + docs updater | Applies project docs changes from shadow git and `PROJECT.log` cursors |
| Janitor worker | supervisor | `core/lifecycle/janitor.py` + lifecycle registry | Runs one scheduler eligibility tick and exits |
| DocsDB project-docs monitor routine | janitor lifecycle callout | `core/plugins/docsdb_contract.py` | Queues async project-docs requests; does not write docs inline |

The supervisor must not accumulate domain policy. It should know what should be
running and whether it is alive; workers decide what to do when they tick.

## Commands

```bash
quaid supervisor status
quaid supervisor ensure
quaid supervisor stop

# Accepted selectors. They currently target the same runtime supervisor.
quaid supervisor status --type runtime
quaid supervisor status --type all
quaid supervisor status --type project-docs
```

`quaid daemon start` now routes through supervisor ensure by default. Set
`QUAID_SUPERVISOR_DISABLE=1` only for controlled test or emergency legacy
operation; then the extraction daemon may start directly.

## Instance Monitors

An instance monitor is the existing extraction daemon under supervisor
ownership. For every registered instance under
`QUAID_HOME/instances/<instance>/config.json`, the supervisor ensures one
monitor process is alive.

Instance monitor behavior:

- writes its PID to `QUAID_HOME/instances/<instance>/data/extraction-daemon.pid`
- writes logs under `QUAID_HOME/instances/<instance>/logs/daemon/`
- keeps extraction-domain behavior: signal processing, idle extraction,
  embedding retry, and version checks
- no longer runs janitor scheduling inline
- exits when `QUAID_SUPERVISOR_PID` is set and the parent supervisor disappears

`ensure_alive()` semantics changed with supervisor ownership. When supervisor
mode is enabled, it ensures the supervisor is alive and waits briefly for the
supervisor to create the instance monitor. It does not self-spawn the legacy
daemon unless `QUAID_SUPERVISOR_DISABLE=1` or supervisor ensure fails in
non-fail-hard mode.

## Project-Docs Workers

Project docs updates are async and supervisor-owned.

```bash
quaid docs update <project>          # queue a force request
quaid docs update <project> --wait   # queue and wait for fresh state
quaid project status <project>       # freshness, phase, progress, cursors, log tail
quaid project diff <project> [--full]
```

The worker reads project source changes through shadow git and reads
`PROJECT.log` through a hidden cursor. It may edit `PROJECT.md`, `TOOLS.md`,
`AGENTS.md`, and `docs/**/*.md`; it must not edit `PROJECT.log`.

Hidden project-docs state lives under:

```text
QUAID_HOME/data/project-docs/
  requests/
  state/
  locks/
  workers/
  supervisor/
```

Project delete must stop the worker and remove request/state/lock/pid/heartbeat
logs, atomic-write temp files, visible scaffold, and the project shadow-git dir.

## Janitor Workers

The extraction daemon no longer runs `JanitorScheduler.tick()` inline.

The supervisor periodically starts a one-shot janitor worker per registered
instance:

```text
core/janitor_worker.py scheduler-once
```

That worker runs the existing scheduler eligibility check and exits. If work is
due, janitor still dispatches through `core/lifecycle/janitor_lifecycle.py`.
Datastore-owned maintenance routines publish callouts into that registry.

For project docs, janitor calls the DocsDB-owned `project_docs_monitor` routine.
That routine only queues async project-docs monitor requests and reports the
supervisor PID. It must not perform heavy docs writes or RAG indexing inline.
When the routine is already running inside a supervisor-owned janitor worker, it
uses the live `QUAID_SUPERVISOR_PID` instead of recursively starting another
supervisor.

## Teardown Pattern

Preferred cleanup:

```bash
quaid supervisor stop
```

This stops the supervisor process group and then runs cleanup sweeps for known
project-docs workers and instance monitors.

Benchmark or live-test emergency cleanup should target the supervisor process
group, not individual child workers:

```bash
SUP_PID="$(quaid supervisor status)"
PGID="$(ps -o pgid= -p "$SUP_PID" | tr -d ' ')"
kill -TERM "-$PGID"
sleep 2
pgrep -g "$PGID" || true
```

Use `KILL` only after a graceful stop fails. Avoid killing one project-docs
worker or one extraction daemon by itself unless you are intentionally testing
worker recovery; otherwise child cleanup can leave stale state or hide a
supervisor lifecycle bug.

## Relevant Environment Variables

| Variable | Purpose |
|---|---|
| `QUAID_SUPERVISOR_DISABLE=1` | Disable supervisor ownership and use legacy direct daemon path |
| `QUAID_SUPERVISOR_INTERVAL_SECONDS` | Supervisor tick interval |
| `QUAID_SUPERVISOR_JANITOR_CHECK_INTERVAL_SECONDS` | Minimum interval between janitor scheduler worker starts per instance |
| `QUAID_INSTANCE_MONITOR_WAIT_SECONDS` | How long `ensure_alive()` waits for a supervisor-owned instance monitor PID |
| `QUAID_PROJECT_DOCS_WORKER_INTERVAL_SECONDS` | Project-docs worker tick interval |
| `QUAID_PROJECT_DOCS_WORKER_STALE_AFTER_SECONDS` | Heartbeat stale threshold for project-docs worker restart |
| `QUAID_PROJECT_DOCS_PID_WAIT_SECONDS` | Startup PID handshake timeout for supervisor/worker children |

Long project-docs updates can legitimately take minutes or hours on large
projects. Treat heartbeat/progress movement as liveness. Wall-clock timeout
should protect against true stalls, not normal async runtime.

## Validation Coverage

The M19 supervisor runtime canary validated:

- single supervisor PID
- supervisor is process-group leader
- children inherit supervisor PGID
- project-docs auto-refresh still reaches fresh without manual docs update
- `PROJECT.log` remains append-only
- janitor `project_docs_monitor` request path is async, not inline docs work
- active project delete removes worker/state/locks/shadow git/scaffold
- `quaid supervisor stop` leaves zero processes in the supervisor PGID

