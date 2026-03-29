# Project: Quaid

## What This Is
Quaid is an active knowledge layer for agentic systems. It extracts durable facts from conversations and runtime activity, recalls them with hybrid search, keeps project documentation current, and maintains long-lived identity and project state through background janitor maintenance.

This file is the starter brief for working on Quaid itself. It should help an agent or human orient quickly, find the right code or docs, and understand the current launch posture without reading the full operational history first.

## Current State
- Status: prerelease, ship-first.
- Current priority is installability, live reliability, recall correctness, and distillation quality, not broad cleanup or backcompat work.
- Architectural boundaries exist, but some drift remains; prefer small fixes that move behavior toward the intended layer model without widening that drift.
- Identity files (`SOUL.md`, `USER.md`, `ENVIRONMENT.md`) are live outputs written per instance under `identity/`. The copies in this project are base templates and should stay stable.
- `PROJECT.md` should stay distilled. Use `PROJECT.log` for full chronology and raw operational history.

## Start Here By Task
- Install, bootstrap, or instance layout:
  `setup-quaid.mjs`, `setup-quaid.sh`, `reference/config-instances.md`, `reference/config-reference.md`
- Extraction, rolling ingest, or daemon behavior:
  `reference/extraction-pipeline.md`, `modules/quaid/ingest/extract.py`, `modules/quaid/core/extraction_daemon.py`
- Recall, storage, dedup, or retrieval behavior:
  `reference/memory-reference.md`, `reference/memory-deduplication-system.md`, `modules/quaid/datastore/memorydb/memory_graph.py`
- Identity distillation or janitor maintenance:
  `reference/janitor-reference.md`, `modules/quaid/core/lifecycle/janitor.py`, `modules/quaid/datastore/notedb/soul_snippets.py`
- Projects, docs, and RAG behavior:
  `reference/projects-reference.md`, `reference/rag-docs-system.md`, `modules/quaid/datastore/docsdb/registry.py`, `modules/quaid/datastore/docsdb/project_updater.py`, `modules/quaid/datastore/docsdb/rag.py`, `modules/quaid/core/project_registry.py`
- Adapter, hook, or platform-specific behavior:
  `reference/hooks-session-lifecycle.md`, `docs/INTERFACES.md`, `modules/quaid/adaptors/`

## Primary Artifacts

### Project Home
<!-- BEGIN:PROJECT_HOME -->
- `QUAID_HOME/projects/quaid`
<!-- END:PROJECT_HOME -->

### Source Roots
<!-- BEGIN:SOURCE_ROOTS -->
- `(installer should register the active Quaid runtime tree here)`
<!-- END:SOURCE_ROOTS -->

### In This Project Directory
<!-- BEGIN:IN_DIR_FILES -->
<!-- Auto-discovered — project-owned files inside the canonical project directory -->
- `PROJECT.md`
- `AGENTS.md`
- `TOOLS.md`
- `USER.md`
- `SOUL.md`
- `ENVIRONMENT.md`
- `reference/`
- `operations/`
<!-- END:IN_DIR_FILES -->

### External Files
<!-- BEGIN:EXTERNAL_FILES -->
| File | Purpose | Auto-Update |
|------|---------|-------------|
<!-- END:EXTERNAL_FILES -->

## How To Work On It
- Load this file first when Quaid is the active project.
- Use `Start Here By Task` to jump to the subsystem you need before you start grepping.
- Read the registered docs below for deeper reference, then open the named source entrypoints when you need implementation detail.
- Keep this file focused on the current shape of the project. Do not turn it into a day-by-day work log.

## How To Validate
- For targeted runtime changes, start with focused tests under `modules/quaid/tests/`.
- For install/bootstrap changes, validate with a fresh instance through `setup-quaid.mjs` or `setup-quaid.sh`.
- For adapter or lifecycle behavior, use the live/e2e scripts and reference docs under `modules/quaid/scripts/` and `operations/`.
- For heavy transcript or benchmark pressure, use the external benchmark harness rather than ad-hoc driver scripts.

## Key Constraints and Decisions
- Quaid is shipping before major cleanup. Prefer the smallest safe fix that improves live behavior.
- Benchmark and live validation lanes are fail-hard; do not hide provider/runtime failures behind silent fallbacks.
- Intended layer model is adaptor -> facade -> orchestration -> core -> ingest -> datastore -> janitor, with the daemon treated as its own operational component.
- Shared project docs and per-instance identity are different concerns. Project templates live here; generated identity lives under each instance's `identity/` directory.
- `PROJECT.md` is for distilled overview and current frontier. `PROJECT.log` is the append-only history.

## Where To Learn More

### Registered Docs
<!-- BEGIN:REGISTERED_DOCS -->
| Document | Why Read It | Auto-Update |
|----------|-------------|-------------|
<!-- END:REGISTERED_DOCS -->

## Related Projects
- Register closely related repos, deliverables, or external working files here when they materially change how Quaid work is done.

## Recent Major Changes
<!-- BEGIN:PROJECT_LOG -->
<!-- END:PROJECT_LOG -->

## Update Rules
- Keep `What This Is`, `Current State`, `Start Here By Task`, `How To Work On It`, and `Key Constraints and Decisions` distilled and current.
- Rewrite or merge recurring observations instead of appending more chronology.
- Use `Recent Major Changes` for a short recent frontier only. Full operational history lives in `PROJECT.log`.
- Registry-backed sections (`Project Home`, `Source Roots`, `In This Project Directory`, `External Files`, `Registered Docs`) should be refreshed from the current registry/config state.

## Exclude
- `*.db`
- `*.log`
- `*.pyc`
- `__pycache__/`
