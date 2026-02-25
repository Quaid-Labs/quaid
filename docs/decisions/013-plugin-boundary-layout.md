# 013: Plugin Boundary Layout and Root Shim Policy

## Status
Accepted

Update (2026-02-25): Core docs/snippets lifecycle ownership was moved into
`datastore/docsdb/*` so datastore owns those maintenance internals. Janitor
and core remain orchestration/composition only.

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
  - `modules/quaid/core/lifecycle/datastore_runtime.py` (composition bridge)

- Datastore docs domain (first-class datastore ownership):
  - `modules/quaid/datastore/docsdb/rag.py`
  - `modules/quaid/datastore/docsdb/registry.py`
  - `modules/quaid/datastore/docsdb/updater.py`
  - `modules/quaid/datastore/docsdb/project_updater.py`
  - `modules/quaid/datastore/docsdb/soul_snippets.py`

Plugin-root compatibility shims were removed. Canonical ownership now lives only inside
the boundary directories above.

## Import policy
- Internal modules import canonical paths (`core.*`, `ingest.*`, `datastore.*`, `adaptors.*`, `orchestrator.*`).
- Subsystems do not import sibling subsystems directly; cross-boundary calls go through core contracts/services.
- Boundary rules are enforced by `modules/quaid/scripts/check-boundaries.py`.

## Consequences
- Boundaries are now represented in filesystem layout.
- Boundary ownership now matches filesystem layout with no legacy root shims.
