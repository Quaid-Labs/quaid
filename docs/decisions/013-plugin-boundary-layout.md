# 013: Plugin Boundary Layout and Root Shim Policy

## Status
Accepted

## Context
The plugin architecture uses explicit directory ownership and strict import boundaries.

Directory ownership:
- `ingest/` for transcript/document ingestion orchestration
- `core/` for orchestration and composition logic
- `adaptors/` for host/runtime integration
- `orchestrator/` for runtime orchestration
- `datastore/` for datastore ownership and maintenance internals

## Decision
Canonical module ownership:

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
  - `modules/quaid/datastore/notedb/soul_snippets.py`

Plugin-root compatibility shims are not used.

## Import policy
- Internal modules import canonical paths (`core.*`, `ingest.*`, `datastore.*`, `adaptors.*`, `orchestrator.*`).
- Subsystems do not import sibling subsystems directly; cross-boundary calls go through core contracts/services.
- Boundary rules are enforced by `modules/quaid/scripts/check-boundaries.py`.

## Consequences
- Boundaries are represented directly in filesystem layout.
- Datastore domains own their internal maintenance logic.
- Janitor remains orchestration-only via lifecycle routine registration.
