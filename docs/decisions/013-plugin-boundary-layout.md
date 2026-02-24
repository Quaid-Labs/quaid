# 013: Plugin Boundary Layout and Root Shim Policy

## Status
Accepted

## Context
The plugin accumulated boundary-owned modules at `modules/quaid/*.py`, which blurred ownership and encouraged cross-layer imports.

The target architecture is explicit directory ownership:
- `ingest/` for transcript/document ingestion orchestration
- `core/` for lifecycle/docs/core orchestration logic
- `adapters/` for host/runtime integration
- `orchestrator/` for runtime orchestration
- `datastore/` for datastore ownership and maintenance internals

## Decision
Canonical module ownership moved out of plugin root:

- Ingest:
  - `modules/quaid/ingest/extract.py`
  - `modules/quaid/ingest/docs_ingest.py`

- Core lifecycle:
  - `modules/quaid/core/lifecycle/janitor.py`
  - `modules/quaid/core/lifecycle/janitor_lifecycle.py`
  - `modules/quaid/core/lifecycle/workspace_audit.py`
  - `modules/quaid/core/lifecycle/soul_snippets.py`

- Core docs:
  - `modules/quaid/core/docs/rag.py`
  - `modules/quaid/core/docs/registry.py`
  - `modules/quaid/core/docs/updater.py`
  - `modules/quaid/core/docs/project_updater.py`

Plugin-root modules with these names are now compatibility shims only.

## Compatibility policy
- Keep root shims to avoid breaking existing imports/CLI/tests.
- Internal modules should import canonical paths (`core.*`, `ingest.*`, `datastore.*`) and not root shims.
- Runtime script paths (adapter bridge, CLI wrapper, e2e harness) should call canonical module paths.

## Consequences
- Boundaries are now represented in filesystem layout.
- Root files remain temporarily for compatibility, but no longer own logic.
- Future cleanup can remove shims once external callers/tests fully migrate.
