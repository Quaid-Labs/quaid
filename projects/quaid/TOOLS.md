# Knowledge Layer — Tool Usage Guide

Project Description: Quaid is an active knowledge layer for agentic systems. Use these tools to recall, inspect, and maintain evolving knowledge.

## Purpose

`TOOLS.md` explains how to use tools effectively.
It is not a full API schema or implementation spec.

## Current Tool Surface

OpenClaw adapter tool surface:
- `memory_recall`
- `memory_store`
- `memory_forget`
- `projects_search`
- `docs_read`
- `docs_list`
- `docs_register`
- `project_create`
- `project_list`
- `session_recall`

MCP server tool surface (`core/interface/mcp_server.py`):
- `memory_extract`
- `memory_store`
- `memory_recall`
- `memory_write`
- `memory_search`
- `memory_get`
- `memory_forget`
- `memory_create_edge`
- `memory_stats`
- `projects_search`
- `session_recall`
- `memory_provider`
- `memory_capabilities`
- `memory_event_emit`
- `memory_event_list`
- `memory_event_process`
- `memory_event_capabilities`

## Plugin Contract Surfaces

Runtime-enforced executable surfaces:
- `init`
- `config`
- `status`
- `dashboard`
- `maintenance`
- `tool_runtime`
- `health`

Manifest-declared non-executable surfaces:
- `tools`
- `api`
- `events`
- `ingest_triggers`
- `auth_requirements`
- `migrations`
- `notifications`

Strict-mode behavior:
- `plugins.strict=true` enforces fail-fast on undeclared tool/event registrations.
- `plugins.strict=false` downgrades declaration mismatches to warnings.
- `plugins.strict` is independent from `retrieval.fail_hard`: plugin contract validation vs memory/LLM fail policy.

## Core Tools and Param Maps

### `memory_recall`
Use this for user facts, relationships, timelines, and project-state recall.

Parameter map:
- `query` (string): natural-language recall query.
- `options.limit` (number): max result count (clamped by config).
- `options.datastores` (array): choose stores (`vector`, `vector_basic`, `vector_technical`, `graph`, `journal`, `project`).
- `options.graph.expand` (bool): graph traversal toggle.
- `options.graph.depth` (number): graph traversal depth (1-3 practical range).
- `options.routing.enabled` (bool): enable routed/plan-first recall (`total_recall`).
- `options.routing.reasoning` (`fast|deep`): router reasoning tier.
- `options.routing.intent` (`general|agent_actions|relationship|technical`): intent bias.
- `options.routing.failOpen` (bool): router failure behavior (`true` = continue with non-routed fallback on router errors, `false` = raise).
- `options.filters.project` (string): optional project filter.
- `options.filters.docs` (string[]): optional docs filter when project store is involved.
- `options.filters.dateFrom` / `options.filters.dateTo` (YYYY-MM-DD).
- `options.filters.domain` (object map): domain filter map.
  - default: `{"all": true}` (all tagged + untagged memories)
  - strict example: `{"technical": true}` (only technical-tagged memories)
  - rule: if any domain key is `true`, `all` is ignored and only true domains are included.
- MCP `memory_recall` path (`core/interface/mcp_server.py`) also supports `domain_json` (stringified JSON object, for example `{"all": true}` or `{"technical": true}`).
- `options.ranking.sourceTypeBoosts` (object): optional source-type weighting.
- `options.datastoreOptions.<store>`: per-store options.

<!-- AUTO-GENERATED:DOMAIN-LIST:START -->
Available domains (from `config/memory.json -> retrieval.domains`):
- `personal`: identity, preferences, relationships, life events
- `technical`: code, infra, APIs, architecture
- `project`: project status, tasks, files, milestones
- `work`: job/team/process decisions not deeply technical
- `health`: training, injuries, routines, wellness
- `finance`: budgeting, purchases, salary, bills
- `travel`: trips, moves, places, logistics
- `schedule`: dates, appointments, deadlines
- `research`: options considered, comparisons, tradeoff analysis
- `household`: home, chores, food planning, shared logistics
- `legal`: contracts, policy, and regulatory constraints
<!-- AUTO-GENERATED:DOMAIN-LIST:END -->

Domain list maintenance:
- Source of truth is `config/memory.json -> retrieval.domains`.
- Rebuild this block whenever domains change so tool docs stay aligned with runtime filtering behavior.

Use cases:
- relationship questions (`family`, `who is X`, `how are X and Y connected`)
- current state questions (`what changed`, `what is true now`)
- technical/project questions (`tests`, `version`, `architecture`, `bug/fix`)

Recommended usage patterns:
- Known personal query: target `vector_basic` + `graph`
- Known technical query: target `vector_technical` + `project`
- Ambiguous query: use routed recall (`total_recall` path)
- Agent-action query: use routed recall with `agent_actions` intent

### `session_recall`
Use this for recent-session discovery and transcript retrieval.

Parameter map:
- `action` (`list|load`):
  - `list`: returns recent extracted sessions.
  - `load`: loads one session transcript by `session_id`. If transcript is unavailable, returns an error payload (no automatic extracted-facts fallback).
- `session_id` (string): required for `load`. Pattern: `[a-zA-Z0-9_-]{1,128}`.
- `limit` (number): list size for `action=list` (default 5, max 20).

“Load last session” workflow:
1. call `session_recall` with `action="list", limit=1`
2. take returned session id
3. call `session_recall` with `action="load", session_id="<id>"`

### `projects_search`
Use this for project docs and implementation references.

Parameter map:
- `query` (string, required): semantic search query.
- `limit` (number, optional): max hits (default `5`).
- `project` (string, optional): restrict search to one project.
- `docs` (string array, optional): optional doc path/name filters to narrow RAG scope.

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

Parameter map:
- `text` (string): exact memory note text to queue for extraction.
- `category` (`preference|fact|decision|entity|other`, optional): lightweight hint.
- OpenClaw runtime path: accepts only `text` and optional `category`.
- MCP path (`core/interface/mcp_server.py`) also accepts `domains_json` (stringified JSON array, for example `["technical","project"]`).

### `memory_forget`
Use this for explicit deletion requests.

Parameter map:
- OpenClaw runtime path: `memoryId` (string) to delete one specific memory by id.
- MCP path (`core/interface/mcp_server.py`): `node_id` (string) to delete one specific memory by id.
- `query` (string): delete matching memories by query.

### Docs/Project Admin Tools
- `docs_read`
  - `identifier` (string): doc path or title.
- `docs_list`
  - `project` (optional string), `type` (optional string).
- `docs_register`
  - `file_path` (required workspace-relative path), plus optional `project`, `title`, `description`, `auto_update`, `source_files`.
  - `auto_update` defaults to `false`; set it to `true` only for docs that should be drift-tracked from mapped source files.
- `project_create`
  - `name` (required; validated by `registry.py` regex `^[a-zA-Z0-9][a-zA-Z0-9_-]*$`; kebab-case is recommended for consistency), plus optional `label`, `description`, `source_roots`.
- `project_list`
  - no parameters.

## Knowledge Stores

- `vector_basic`: personal/life facts, preferences, relationships
- `vector_technical`: technical/project-state facts
- `vector`: combined vector retrieval across personal + technical memories
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

### Previous-session reference looks missing
1. Run `memory_recall` first with specific entities.
2. If still missing and user clearly means the latest session: `session_recall(action=list, limit=1)` then `session_recall(action=load, session_id=...)`.

### Conflicting or stale facts
1. Recall with recency-sensitive ranking.
2. Prefer newest consistent facts.
3. If conflict persists, surface uncertainty and suggest janitor review.

## Operational Commands (Operator Reference)

```bash
cd modules/quaid

# Recall and inspection
python3 datastore/memorydb/memory_graph.py search "query" --owner quaid --limit 50 --min-similarity 0.6
python3 datastore/memorydb/memory_graph.py search-graph "query" --owner quaid
python3 datastore/memorydb/memory_graph.py search-graph-aware "query" --owner quaid
python3 datastore/memorydb/memory_graph.py stats
python3 datastore/memorydb/memory_graph.py health
python3 core/runtime/plugin_health.py

# Manual memory operations
python3 datastore/memorydb/memory_graph.py store "text" --owner quaid --category fact
python3 datastore/memorydb/memory_graph.py fact-history <node_id>
python3 datastore/memorydb/memory_graph.py get-edges <node_id>

# Project docs search
python3 datastore/docsdb/rag.py search "query"
python3 datastore/docsdb/registry.py list --project quaid

# Janitor maintenance
python3 core/lifecycle/janitor.py --task all --dry-run
python3 core/lifecycle/janitor.py --task all --apply
python3 core/lifecycle/janitor.py --task review --apply --approve
python3 core/lifecycle/janitor.py --task journal --apply --force-distill
python3 core/lifecycle/janitor.py --task all --apply --time-budget 1800 --token-budget 12000
```

## Related Docs

- `projects/quaid/AGENTS.md` — project behavior and operating rules
- `projects/quaid/PROJECT.md` — full project overview and architecture map
- `projects/quaid/reference/memory-local-implementation.md` — implementation details
- `projects/quaid/reference/janitor-reference.md` — janitor internals and tasks
