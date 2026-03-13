# Knowledge Layer — Tool Usage Guide

Project Description: Quaid is an active knowledge layer for agentic systems. Use these tools to recall, inspect, and maintain evolving knowledge.

## Purpose

`TOOLS.md` explains how to use tools effectively.
It is not a full API schema or implementation spec.

## CLI Interface

All agents interact with Quaid through the `quaid` CLI. No tool registration
needed — agents call CLI commands via their shell/Bash tool.

**Required environment:** Set `QUAID_HOME` to your instance directory before
running CLI commands. Each adapter instance (Claude Code, OpenClaw, etc.) has
its own `QUAID_HOME` silo with its own config, database, and identity files.
Do NOT set `QUAID_HOME` globally in shell profile — let each adapter's
hooks/runtime set it per-invocation to avoid cross-instance collisions.

### Memory Commands
- `quaid recall "query"` — Search memories (semantic + graph traversal + reranking)
- `quaid store "text"` — Store a new memory
- `quaid search "query"` — Fast search (no reranking)
- `quaid stats` — Database statistics and health
- `quaid delete-node <id>` — Delete a specific memory
- `quaid create-edge <from> <to> <label>` — Create a relationship edge

### Documentation Commands
- `quaid docs search "query"` — Search project documentation (RAG)
- `quaid docs list` — List registered docs
- `quaid docs check` — Check for stale docs
- `quaid docs update` — Update stale docs from source diffs

### Project Commands
- `quaid project list` — List all registered projects
- `quaid project create <name> [--description "..."] [--source-root /path]` — Create a new project
- `quaid project show <name>` — Show project details
- `quaid project link <name>` — Add current instance to an existing project's instances list (idempotent)
- `quaid project unlink <name>` — Remove current instance from project's instances list (idempotent, does not delete)
- `quaid project delete <name>` — Delete a project: unlinks all instances, removes canonical project dir, and purges all SQLite rows (`project_definitions` + `doc_registry`). Does NOT touch the source directory.
- `quaid project snapshot [<name>]` — Take shadow git snapshot(s) for change tracking
- `quaid project sync` — Sync project context files to adapter workspaces

### Doc Registry Commands
- `quaid registry list` — List all registered docs
- `quaid registry register <file_path> --project <name>` — Register a doc in the doc registry
- `quaid global-registry list` — Cross-instance project registry
- `quaid updater doc-health <project> [--dry-run]` — Evaluate doc lifecycle

### Project File Placement
When working with a project, decide whether files belong **in the project directory** or should be **linked externally**:

- **In the project dir**: files that are owned by the project and don't need to live elsewhere. Examples: essays, notes, standalone docs, project-specific configs. The canonical path is `QUAID_HOME/<instance>/projects/<name>/`.
- **In `docs/` subdirectory**: project documentation goes here (architecture docs, reference guides, operational runbooks). Created automatically with each project at `QUAID_HOME/<instance>/projects/<name>/docs/`.
- **Linked externally**: code files that must live in specific repo paths. Register them with `quaid registry register <path> --project <name>` (doc registry) so the project tracks them without moving them. Use the doc registry, not the project registry, for externally-linked files.

Note: Files shared across multiple instances should be placed in a shared project directory or registered via the doc registry so all instances can discover them.

Rule of thumb: if the file only makes sense in the context of this project, put it in the project directory. If it has to live in a specific location for code/build reasons, link it via `quaid registry register`.

### Combined Search
- `quaid hook-search "query"` — Search memories + docs together

### Session Commands
- `quaid session list` — List recent extracted sessions
- `quaid session load <id>` — Load a session transcript

### Domain Management
- `quaid domain list` — List registered knowledge domains
- `quaid domain register <name> [--description "..."]` — Register a new domain

### Maintenance
- `quaid janitor --task all --dry-run` — Preview janitor maintenance
- `quaid janitor --task all --apply` — Run janitor maintenance (`--apply --approve` required when `janitor.applyMode=ask`)
- `quaid doctor` — Health check

Note: `--task contradictions` is retained for CLI backward compatibility but is NOT active in the `--task all` pipeline. It prints a "DECOMMISSIONED" notice and exits. Stale/conflicting fact handling is now supersession/recency-based via the `dedup` and `decay` tasks.

Note: If `janitor.applyMode=ask` is set in `config/memory.json`, running with `--apply` alone prints a dry-run result and prompts you to re-run with `--approve`. Pass both `--apply --approve` to actually execute changes. When `applyMode=auto` (the standard cron default), `--approve` is a no-op.

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
- Plugin contracts are declaration-validated at startup; strict mode raises on drift, non-strict logs warnings.

## Core Commands and Parameters

### `quaid recall "query"`
Use this for user facts, relationships, timelines, and project-state recall.
Full recall pipeline with semantic search, graph traversal, and reranking.

### `quaid search "query"`
Fast search (semantic + FTS, no reranking). Useful for quick lookups.

Flags (shared by `recall` and `search`):
- `--limit N` — max result count (default: 5).
- `--project <name>` — filter by project.
- `--date-from YYYY-MM-DD` / `--date-to YYYY-MM-DD` — date range filter.
- `--domain-filter '{"all": true}'` — domain filter JSON.
  - default: `{"all": true}` (all tagged + untagged memories)
  - strict example: `{"technical": true}` (only technical-tagged memories)
  - rule: if any domain key is `true`, `all` is ignored and only true domains are included.
- `--domain-boost '["technical","project"]'` — preferential scoring boost by domain.
  - list form: `["technical","project"]` (defaults to `1.3x` each)
  - map form: `{"technical": 1.5, "research": 1.2}`
  - boost applies as multiplier to scored matches before final ranking.
  - guidance: prefer `--domain-boost` for broad recall; use strict `--domain-filter` only when you are certain non-target domains should be excluded.
- `--json` — JSON output.
- `--debug` — show scoring breakdown.

<!-- Manually maintained — update when quaid domain register adds new domains -->
Available domains (from datastore `domain_registry` active rows):
- `finance`: budgeting, purchases, salary, bills
- `health`: training, injuries, routines, wellness
- `household`: home, chores, food planning, shared logistics
- `legal`: contracts, policy, and regulatory constraints
- `personal`: identity, preferences, relationships, life events
- `project`: project status, tasks, files, milestones
- `research`: options considered, comparisons, tradeoff analysis
- `schedule`: dates, appointments, deadlines
- `technical`: code, infra, APIs, architecture
- `travel`: trips, moves, places, logistics
- `work`: job/team/process decisions not deeply technical
<!-- end domain list -->

Domain list maintenance:
- Source of truth is datastore `domain_registry` (`active=1`).
- `quaid domain register` updates the registry. Update the domain list above manually when new domains are added.

Use cases:
- relationship questions (`family`, `who is X`, `how are X and Y connected`)
- current state questions (`what changed`, `what is true now`)
- technical/project questions (`tests`, `version`, `architecture`, `bug/fix`)

Recommended usage patterns:
- Known personal query: target `vector_basic` + `graph`
- Known technical query: target `vector_technical` + `project`
- Ambiguous query: use routed recall (`total_recall` path)
- Agent-action query: use routed recall with `agent_actions` intent
- Known-scope query (work/technical/health/project/etc.): add `--domain-boost` first (preferred over strict `--domain-filter`).
- Multi-domain query: include all relevant domains in `--domain-boost` (for example `'{"work": 1.3, "technical": 1.3}'`).
- Use strict `--domain-filter` only when non-target domains must be excluded.

### `quaid session list` / `quaid session load <id>`
Use this for recent-session discovery and transcript retrieval.

- `quaid session list [--limit 5]` — list recent extracted sessions
- `quaid session load --session-id <id>` — load a session transcript

“Load last session” workflow:
1. `quaid session list --limit 1`
2. take returned session id
3. `quaid session load --session-id <id>`

### `quaid docs search "query"`
Use this for project docs and implementation references.

- `quaid docs search "query"` — semantic search across all project docs
- `quaid docs search "query" --project quaid` — restrict to one project

Use cases:
- finding where a feature is documented
- project-scoped architecture questions
- searching a specific project before opening docs

Notes:
- Docs search is focused on project documentation and is project-aware.
- `quaid recall` can include the `project` store, but docs search is better for broad doc lookup.
- Project history is append-only in `projects/<project>/PROJECT.log`; Docs/RAG indexes `PROJECT.log` alongside Markdown docs.

### `quaid store "text"`
Use this only for explicit/manual memory insertion when needed.
Default behavior should favor automatic extraction.

- `quaid store "the fact to store"` — store a memory
- `quaid store "the fact" --category fact` — store with category hint

Categories: `preference`, `fact`, `decision`, `entity`, `other`

### `quaid domain list` / `quaid domain register`
Manage knowledge domains for memory categorization.

- `quaid domain list` — list active domains
- `quaid domain register <name> "description"` — register a new domain

### `quaid delete-node <id>`
Use this for explicit deletion requests.

- `quaid delete-node <node_id>` — delete a specific memory by ID

### Docs/Project Admin Commands
- `quaid docs list [--project <name>]` — list registered docs
- `quaid docs search "query"` — search project documentation
- `quaid docs check` — check for stale docs
- `quaid docs update --apply` — update stale docs from source diffs
- `quaid project create <name> [--label "Display Name"]` — create a new project
- `quaid project link <name>` — add current instance to an existing project
- `quaid project unlink <name>` — remove current instance from a project
- `quaid registry list` — list all registered docs in the doc registry
- `quaid registry register <file_path> --project <name>` — register a doc (links external files into a project)
- `quaid updater doc-health <project> [--dry-run]` — evaluate doc lifecycle

Project placement policy:
- Before creating any non-temporary file, place it inside an existing tracked project whenever possible.
- If no existing project fits, create one with `quaid project create <name>` and place the file there.
- Temporary or scratch files go in `temp/` or `scratch/` — explicitly tell the user these are untracked.
- If a temp/scratch file becomes durable, move it into a tracked project.

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
3. For scoped relationship/life areas, add `domainBoost` (for example `{"personal": 1.3, "household": 1.3}`) before broadening.

### Technical status question
1. Recall from `vector_technical` + `project`.
2. If still ambiguous, run `quaid docs search` scoped to the project.
3. For project/work-specific prompts, add `domainBoost` first (for example `{"technical": 1.3, "project": 1.3, "work": 1.3}`).

### Previous-session reference looks missing
1. Run `quaid recall` first with specific entities.
2. If still missing and user clearly means the latest session: `quaid session list --limit 1` then `quaid session load --session-id <id>`.

### Conflicting or stale facts
1. Recall with recency-sensitive ranking.
2. Prefer newest consistent facts.
3. If conflict persists, surface uncertainty and suggest janitor review.

## Operational Commands (Operator Reference)

```bash
# Recall and inspection
quaid search "query"
quaid recall "query"
quaid stats
quaid health

# Manual memory operations
quaid store "text"
quaid get-edges <node_id>
quaid get-node <node_id>
quaid delete-node <node_id>

# Project docs
quaid docs search "query"
quaid docs check
quaid docs update --apply
quaid registry list --project quaid

# Janitor maintenance
quaid janitor --task all --dry-run
quaid janitor --task all --apply
quaid janitor --task all --apply --approve  # required when applyMode=ask
quaid janitor --task all --apply --time-budget 1800 --token-budget 12000

# Project system
quaid project list
quaid project create <name> --label "Display Name"
quaid project show <name>
quaid project link <name>
quaid project unlink <name>
quaid project delete <name>
quaid project snapshot [<name>]
quaid project sync

# Instances
quaid instances list
quaid instances list --json

# Config
quaid config show
quaid config edit --shared
quaid config edit --instance <id>
quaid config set <dotted.key> <value> --shared

# Doc lifecycle
quaid updater doc-health <project> --dry-run
quaid global-registry list

# Sessions and domains
quaid session list
quaid session load <id>
quaid domain list
quaid domain register <name> "description"

# Health check
quaid doctor
```

## Related Docs

- `projects/quaid/AGENTS.md` — project behavior and operating rules
- `projects/quaid/PROJECT.md` — full project overview and architecture map
- `projects/quaid/reference/memory-local-implementation.md` — implementation details
- `projects/quaid/reference/janitor-reference.md` — janitor internals and tasks
