# Knowledge Layer — Tool Usage Guide

Project Description: Quaid is an active knowledge layer for agentic systems. Use these tools to recall, inspect, and maintain evolving knowledge.

## Purpose

`TOOLS.md` explains how to use tools effectively.
It is not a full API schema or implementation spec.

## Core Tools and When to Use Them

### `memory_recall`
Use this for user facts, relationships, timelines, and project-state recall.

Use cases:
- relationship questions (`family`, `who is X`, `how are X and Y connected`)
- current state questions (`what changed`, `what is true now`)
- technical/project questions (`tests`, `version`, `architecture`, `bug/fix`)

Recommended usage patterns:
- Known personal query: target `vector_basic` + `graph`
- Known technical query: target `vector_technical` + `project`
- Ambiguous query: use routed recall (`total_recall` path)
- Agent-action query: use routed recall with `agent_actions` intent

### `projects_search`
Use this for project docs and implementation references.

Use cases:
- finding where a feature is documented
- project-scoped architecture questions
- searching a specific project (`project=quaid`) before opening docs

Notes:
- `projects_search` is docs-focused and project-aware.
- `memory_recall` can include `project` store, but `projects_search` is still the better doc workflow for broad doc lookup.

### `memory_store`
Use this only for explicit/manual memory insertion when needed.
Default behavior should favor automatic extraction.

## Knowledge Stores

- `vector_basic`: personal/life facts, preferences, relationships
- `vector_technical`: technical/project-state facts
- `graph`: relationship traversal and connected entities
- `journal`: distilled reflective context
- `project`: project documentation index

## Retrieval Policy

- Treat auto-injected memory as a hint, not final truth.
- For concrete claims (names, dates, roles, version/status), run explicit recall before answering.
- Prefer project-scoped docs search for codebase/process questions.
- If injected context is weak or partial, do deeper tool recall.

## Fast Playbooks

### Personal relationship question
1. Recall from `vector_basic` + `graph`.
2. If low confidence, broaden to routed recall.

### Technical status question
1. Recall from `vector_technical` + `project`.
2. If still ambiguous, run `projects_search` scoped to the project.

### Conflicting or stale facts
1. Recall with recency-sensitive ranking.
2. Prefer newest consistent facts.
3. If conflict persists, surface uncertainty and suggest janitor review.

## Operational Commands (Operator Reference)

```bash
cd modules/quaid

# Recall and inspection
python3 memory_graph.py search "query" --owner quaid --limit 50 --min-similarity 0.6
python3 memory_graph.py search-graph "query" --owner quaid
python3 memory_graph.py stats
python3 memory_graph.py health

# Manual memory operations
python3 memory_graph.py store "text" --owner quaid --category fact
python3 memory_graph.py fact-history <node_id>
python3 memory_graph.py get-edges <node_id>

# Project docs search
python3 docs_rag.py search "query"
python3 docs_registry.py list --project quaid

# Janitor maintenance
python3 janitor.py --task all --dry-run
python3 janitor.py --task all --apply
```

## Related Docs

- `projects/quaid/AGENTS.md` — project behavior and operating rules
- `projects/quaid/PROJECT.md` — full project overview and architecture map
- `projects/quaid/reference/memory-local-implementation.md` — implementation details
- `projects/quaid/reference/janitor-reference.md` — janitor internals and tasks
