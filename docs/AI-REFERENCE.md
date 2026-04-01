# Quaid AI Agent Reference

This document is designed for AI agents (Claude, GPT, etc.) working on or with the Quaid knowledge layer. It provides a complete index of files, functions, patterns, and known issues. Treat this as your primary orientation guide before making any changes.

---

## AI Installer Path

For agent-driven installs, use:

```bash
node setup-quaid.mjs --agent --workspace "/absolute/path/to/workspace"
```

Avoid `curl | bash` when the session needs explicit workspace control.
Full installer guidance: [`docs/AI-INSTALL.md`](AI-INSTALL.md).

---

## System Overview

Quaid is a graph-based persistent knowledge layer for AI agents. It ships with deep host integrations for [OpenClaw](https://github.com/openclaw/openclaw), Claude Code, and Codex, plus a standalone CLI. Backed by SQLite with sqlite-vec for vector search, FTS5 for full-text search, and an LLM-powered nightly maintenance pipeline ("janitor").

**Architecture stack:**
- **Interfaces:** CLI (`quaid` commands), OpenClaw plugin (TypeScript hooks), Claude Code adapter/hooks, Codex adapter/hooks
- **Backend:** Python modules for graph operations, extraction, retrieval, maintenance, docs, and project tracking
- **Storage:** SQLite database with WAL mode, sqlite-vec ANN index, FTS5 full-text index
- **Embeddings:** Ollama local server (nomic-embed-text, 768 dimensions)
- **LLM calls:** provider-routed deep-reasoning and fast-reasoning LLM tiers
- **Config:** JSON config file at `<QUAID_HOME>/<INSTANCE_ID>/config/memory.json`

**Retrieval pipeline (current):**
```
Query
  -> (Optional) total_recall planning pass (query cleanup + datastore routing)
  -> Intent classification (WHO/WHEN/WHERE/WHAT/WHY/PREFERENCE/RELATION)
  -> Parallel datastore search (vector_basic/vector_technical/graph/journal/projects)
  -> Reciprocal Rank Fusion (dynamic weights per intent)
  -> Optional: Haiku LLM reranker (0-5 graded scale)
  -> Read-time injection filtering (_sanitize_for_context)
```

**Write pipeline (current):**
```
Write request
  -> writeData(datastore, action, payload)
  -> DataWriter dispatch (vector / graph / journal / project)
  -> datastore command execution
  -> normalized write result (created/updated/duplicate/skipped/failed)
```

---

## File Index

### Core Python Files

| File | Purpose | Key Exports / Functions |
|------|---------|------------------------|
| `datastore/memorydb/memory_graph.py` | Graph operations, hybrid retrieval, CLI, datastore cleanup registration | `store()`, `recall()`, `search()`, `create_edge()`, `get_graph()`, `_sanitize_for_context()`, `MemoryGraph`, `Node`, `Edge`, `register_lifecycle_routines()` |
| `datastore/memorydb/maintenance_ops.py` | Datastore-owned memory maintenance intelligence | dedup/contradiction/decay/review routines, `TokenBatchBuilder`, `JanitorMetrics` |
| `datastore/memorydb/maintenance.py` | Datastore lifecycle registration facade | `register_lifecycle_routines()` for `memory_graph_maintenance` |
| `core/lifecycle/janitor.py` | Janitor orchestration, scheduling, reporting, lifecycle dispatch | `run_task_optimized()`, `run_tests()`, task orchestration and policy gating |
| `core/lifecycle/janitor_lifecycle.py` | Lifecycle routine registry and dispatch | `LifecycleRegistry`, `RoutineContext`, `RoutineResult`, `build_default_registry()` |
| `core/lifecycle/workspace_audit.py` | Workspace markdown audit implementation | `run_workspace_check()`, `check_bloat()` |
| `datastore/notedb/soul_snippets.py` | Dual snippet + journal learning system | `run_soul_snippets_review()`, `run_journal_distillation()` |
| `datastore/docsdb/rag.py` | RAG indexing/search and lifecycle registration | `search_docs()`, `index_document()`, `register_lifecycle_routines()` |
| `datastore/docsdb/updater.py` | Doc staleness/cleanup maintenance routines | `check_staleness()`, `update_doc_from_diffs()`, `register_lifecycle_routines()` |
| `datastore/docsdb/registry.py` | Project/doc registry and path resolution | `create_project()`, `auto_discover()`, `register()`, `find_project_for_path()` |
| `datastore/docsdb/project_updater.py` | Background project event processor | `process_event()`, `refresh_project_md()` |
| `core/runtime/events.py` | Queue-backed runtime event bus | `emit_event()`, `list_events()`, `process_events()`, `get_event_registry()` |
| `core/runtime/notify.py` | User notifications via adapter/runtime context | `notify_user()`, retrieval/extraction/janitor/doc notifications |
| `core/runtime/logger.py` | Structured JSONL logger with rotation | `Logger`, `rotate_logs()`, `memory_logger`, `janitor_logger` |
| `core/interface/api.py` | Public API facade | `store()`, `recall()`, `search()`, `create_edge()`, `forget()`, `get_memory()`, `stats()`, `extract_transcript()`, `projects_search_docs()` |
| `lib/llm_clients.py` | Canonical LLM client wrapper with prompt parsing/usage tracking | `call_deep_reasoning()`, `call_fast_reasoning()`, `call_llm()`, `parse_json_response()` |
| `core/llm/clients.py` | Compatibility alias to canonical LLM client module | module alias to `lib.llm_clients` |
| `ingest/extract.py` | Extraction module — transcript to memories | `extract_from_transcript()`, `parse_session_jsonl()`, `build_transcript()` |
| `ingest/docs_ingest.py` | Ingest pipeline for docs from transcript/source mapping | ingest orchestration helpers |
| `datastore/memorydb/semantic_clustering.py` | Semantic clustering for contradiction candidate reduction | `classify_node_semantic_cluster()`, `get_memory_clusters()` |
| `datastore/memorydb/schema.sql` | Database DDL (nodes, edges, FTS5, indexes, operational tables) | Full schema definition |
| `datastore/memorydb/enable_wal.py` | WAL mode enablement helper | `enable_wal_mode()` |

### Adapter Layer (`adaptors/`)

| File | Purpose | Key Exports / Notes |
|------|---------|---------------------|
| `adaptors/openclaw/adapter.py` | OpenClaw-specific adapter | `OpenClawAdapter`, sessions from `~/.openclaw/sessions/`, notifications via `openclaw message send` CLI |
| `adaptors/openclaw/maintenance.py` | OpenClaw lifecycle registrations | `register_lifecycle_routines()` (workspace audit) |
| `adaptors/claude_code/adapter.py` | Claude Code adapter | `ClaudeCodeAdapter`, sessions from `~/.claude/projects/`, deferred notifications via pending file, OAuth token auth |
| `adaptors/codex/adapter.py` | Codex adapter | `CodexAdapter`, sessions from `~/.codex/sessions/`, deferred notifications via pending file, host-managed app-server provider path |
| `adaptors/claude_code/hooks.py` | Backwards-compatible shim for CC hooks | Auto-provisions a new instance silo on first invocation if needed, then delegates to `core.interface.hooks.main` |
| `core/interface/hooks.py` | Adapter-agnostic hook entry points (SOURCE OF TRUTH for hooks) | `hook_inject`, `hook_inject_compact`, `hook_extract`, `hook_session_init`, `hook_subagent_start`, `hook_subagent_stop` |

### Shared Library (`lib/`)

| File | Purpose | Key Exports |
|------|---------|-------------|
| `adapter.py` | Platform adapter layer base/standalone adapters | `QuaidAdapter`, `StandaloneAdapter`, `get_adapter()`, `set_adapter()`, `reset_adapter()`, `ChannelInfo` |
| `instance.py` | Instance identity resolution | `instance_id()`, `quaid_home()`, `instance_root()`, `shared_dir()`, `list_instances()`, `validate_instance_id()` |
| `config.py` | Path resolution, env overrides for tests | `get_db_path()`, `get_ollama_url()`, `get_embedding_model()`, `get_archive_db_path()` |
| `database.py` | SQLite connection factory | `get_connection()` -- @contextmanager, enables WAL mode, FK ON, busy_timeout=30000 |
| `embeddings.py` | Ollama embedding calls | `get_embedding()`, `pack_embedding()`, `unpack_embedding()` |
| `markdown.py` | Protected region helpers for core markdown | `strip_protected_regions()`, `check_overlap()`, `find_protected_positions()` |
| `similarity.py` | Cosine similarity calculations | `cosine_similarity()` |
| `tokens.py` | Token counting and text comparison | `TokenBatchBuilder` (with `output_tokens_per_item` param), `count_tokens()` |
| `archive.py` | Archive database for decayed memories | `archive_node()`, `search_archive()`, `_get_archive_conn()` |
| `__init__.py` | Package init | (empty or minimal) |

### TypeScript / JavaScript

| File | Purpose | Notes |
|------|---------|-------|
| `adaptors/openclaw/index.ts` | Plugin entry shim | Minimal export indirection to runtime adapter module |
| `adaptors/openclaw/adapter.ts` | OpenClaw runtime integration (SOURCE OF TRUTH) | Hook registration, extraction trigger wiring, session-timeout manager, daemon signal dispatch, notifications. **No tool registration** — tools were removed; agents use the `quaid` CLI instead (see `projects/quaid/TOOLS.md`). |
| `core/facade.ts` | Adapter-facing entry point | `createQuaidFacade(deps)` — single constructor adapters call. Wires datastore bridge, knowledge engine, project catalog, extraction queue, lifecycle signal history, and model-tier resolution. |
| `core/knowledge-engine.ts` | TypeScript recall router | `createKnowledgeEngine(deps)` — routed recall via fast-reasoning LLM prepass (`routeRecallPlan`) or direct explicit-stores mode. Handles dedup, source-type boosting, fail-open fallback, and repair retry on malformed router JSON. Project catalog is capped at 40 entries in router prompt. |
| `core/session-timeout.ts` | Idle-session extraction | `SessionTimeoutManager` — debounced timer per session; fires extraction (via daemon signal) when no `onAgentStart` clears it within `timeoutMinutes`. Stale-sweep recovers sessions missed during process downtime. |
| `core/knowledge-stores.ts` | Datastore registry | `STORE_REGISTRY` — canonical list of datastores with defaults for flat vs. graph-expand recall. `getRoutableDatastoreKeys()` excludes `vector` (aggregate) from router choices. |
| `core/project-catalog.ts` | Project catalog reader | `createProjectCatalogReader` — reads project names and descriptions from `config/memory.json -> projects.definitions`. Falls back to `TOOLS.md`/`PROJECT.md` first-useful-line extraction. |
| `core/data-writers.ts` | Canonical write routing/dispatch | `createDataWriteEngine()`, `writeData()`, DataWriter registry/specs |
| `core/spawn-with-timeout.ts` | Subprocess spawning | `spawnWithTimeout` — spawns a child process with SIGTERM + SIGKILL escalation on timeout. Used by all Python bridge calls from the TypeScript layer. |
| `orchestrator/default-orchestrator.ts` | Orchestrator entry shim | Re-exports `createKnowledgeEngine` and related types from `core/knowledge-engine.ts` — the stable public surface adapters use for routed recall. |
| `adaptors/openclaw/index.js` / `adapter.js` / `core/facade.js` etc. | Runtime JS loaded by gateway | Keep TS/JS runtime pairs synchronized; gateway executes `.js`. Check with `npm run check:runtime-pairs`. |

### CI / Release Guard Scripts

| File | Purpose |
|------|---------|
| `modules/quaid/scripts/check-runtime-pairs.mjs` | Enforces TS/JS runtime pair sync (`--strict` checks HEAD commit too) |
| `modules/quaid/scripts/run-all-tests.sh` | Orchestrated quick/full test launcher with syntax, integration, and Python isolated suites |
| `modules/quaid/scripts/run-quaid-e2e-matrix.sh` | Bootstrap/runtime auth-path matrix runner with failure classification + JSON summary |
| `scripts/check-docs-consistency.mjs` | GitHub-facing docs drift gate (README/ARCHITECTURE/AI-REFERENCE invariants) |
| `scripts/release-verify.mjs` | Release/version consistency gate (package/setup/README/release-note alignment) |
| `scripts/release-owner-check.mjs` | Ownership/attribution gate (author/committer identity + blocked bot/co-author tags) |
| `scripts/release-check.sh` | Combined pre-push release checklist (docs + release + ownership + runtime pair checks) |

### Prompt Templates (`prompts/`)

| File | Purpose |
|------|---------|
| `extraction.txt` | Extraction system prompt for Opus (~160 lines, used by `ingest/extract.py`) |

Project onboarding instructions live at `projects/quaid/operations/project_onboarding.md`.

### Database Migrations

Quaid currently performs schema evolution in-place from `datastore/memorydb/memory_graph.py` during startup/DB init.
There is no standalone `migrations/` directory in the current repository layout.

---

## Claude Code Integration

Quaid integrates with Claude Code via hooks registered in `~/.claude/settings.json`. Hooks call the `quaid` CLI which routes to `core/interface/hooks.py`.

### Hook Wiring

| Hook Event | `quaid` Command | Purpose |
|-----------|----------------|---------|
| `SessionStart` | `quaid hook-session-init` | Write project docs to `.claude/rules/`, sweep orphaned sessions, seed extraction cursor |
| `UserPromptSubmit` | `quaid hook-inject` | Recall memories and inject as `additionalContext` for each user message |
| `PreCompact` | `quaid hook-extract --precompact` | Signal extraction daemon before context compaction |
| `SessionEnd` | `quaid hook-extract` | Signal extraction daemon at session end |
| `SubagentStart` | `quaid hook-subagent-start` | Register subagent in registry (skips standalone extraction for child) |
| `SubagentStop` | `quaid hook-subagent-stop` | Mark subagent complete, transcript harvested by parent extraction |

Each hook command reads JSON from stdin and must be invoked with `QUAID_HOME` and `QUAID_INSTANCE` set:

```bash
QUAID_HOME=/path/to/quaid QUAID_INSTANCE=claude-code quaid hook-inject
```

There is no `PostCompact` hook wired. `hook-inject-compact` exists as a callable command but is not auto-registered in `settings.json`.

### Session-Init (Project Context)

`quaid hook-session-init` runs at `SessionStart` (wired to the `SessionStart` hook in `~/.claude/settings.json`). It:

1. Calls `ensure_alive()` to start the extraction daemon if needed.
2. Sweeps orphaned sessions from previous runs (queues extraction for any transcript with un-extracted content past the cursor).
3. Seeds an extraction cursor for the current session so the daemon can discover it for timeout-based extraction.
4. Scans `$QUAID_HOME/projects/*/` for `TOOLS.md` and `AGENTS.md`, collects identity files (`USER.md`, `SOUL.md`, `ENVIRONMENT.md`) from `$QUAID_HOME/<INSTANCE_ID>/identity/`, checks janitor health and compatibility state, then writes the combined content to `{cwd}/.claude/rules/quaid-projects.md` (or `$QUAID_RULES_DIR/quaid-projects.md` if set).

The write is idempotent — if content is unchanged the file is not touched, preventing unnecessary prompt cache invalidation. Claude Code auto-loads `rules/*.md` and preserves them through compaction (unlike `additionalContext` which is lost on compaction).

### Auth Token

The Claude Code adapter requires a long-lived OAuth token for daemon-mode API calls (nightly janitor, extraction daemon). For interactive sessions, `hook-session-init` **automatically refreshes** `.auth-token` from `CLAUDE_CODE_OAUTH_TOKEN` on every CC session start — the manual step below is only needed for daemon processes that run outside an active CC session.

```bash
claude setup-token    # Generate token
python3 config_cli.py set-auth <token>  # Store it for the active adapter instance
```
> **Note:** `quaid config set-auth` is not yet routed through the `quaid` wrapper script (it prints a usage error). Use the direct Python CLI above until this is fixed.

---

## Database Schema

### Core Tables

**nodes** -- All memory entities (facts, persons, concepts, places, events, preferences, orgs)
```
id TEXT PRIMARY KEY                    -- UUID
type TEXT NOT NULL                     -- Person, Place, Project, Event, Fact, Preference, Concept
name TEXT NOT NULL                     -- Display name / main content
attributes TEXT DEFAULT '{}'           -- JSON blob for type-specific data
embedding BLOB                        -- float32 array, dim from config (4096)
verified INTEGER DEFAULT 0            -- 0=auto-extracted, 1=user confirmed
pinned INTEGER DEFAULT 0              -- 1=core facts that never decay
confidence REAL DEFAULT 0.5           -- 0-1 confidence score
source TEXT                           -- Where this came from
source_id TEXT                        -- Message ID or file path
owner_id TEXT                         -- Who owns this memory
privacy TEXT DEFAULT 'shared'         -- private/shared/public
session_id TEXT                       -- Session where created (for dedup)
fact_type TEXT DEFAULT 'unknown'      -- Subcategory (financial, health, family)
knowledge_type TEXT DEFAULT 'fact'    -- fact/belief/preference/experience
extraction_confidence REAL DEFAULT 0.5
speaker TEXT                          -- Who stated this fact
valid_from TEXT                       -- ISO8601 datetime
valid_until TEXT                      -- ISO8601 datetime (null = still valid)
content_hash TEXT                     -- SHA256 of name text (fast exact-dedup)
superseded_by TEXT                    -- ID of replacement node
keywords TEXT                         -- Space-separated search terms
status TEXT DEFAULT 'approved'        -- pending/active/approved
created_at TEXT                       -- datetime('now')
updated_at TEXT                       -- datetime('now')
accessed_at TEXT                      -- datetime('now')
access_count INTEGER DEFAULT 0
storage_strength REAL DEFAULT 0.0    -- Bjork cumulative encoding strength (never decreases)
confirmation_count INTEGER DEFAULT 0 -- How many times re-confirmed
last_confirmed_at TEXT
```

**edges** -- Relationships between nodes
```
id TEXT PRIMARY KEY                    -- UUID
source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE
target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE
relation TEXT NOT NULL                 -- lives_at, works_on, knows, owns, prefers, etc.
attributes TEXT DEFAULT '{}'           -- JSON for relationship metadata
weight REAL DEFAULT 1.0               -- Relationship strength
source_fact_id TEXT REFERENCES nodes(id) ON DELETE SET NULL  -- Fact that created this edge
valid_from TEXT
valid_until TEXT
created_at TEXT
UNIQUE(source_id, target_id, relation)
```

### Index Tables

- **nodes_fts** -- FTS5 virtual table: `name` (weight 2x), `keywords` (weight 1x), content='nodes', tokenize='porter unicode61'
- **vec_nodes** -- sqlite-vec virtual table for ANN search
- **edge_keywords** -- FTS5 for edge relation search, maps edge types to trigger keywords

### Operational Tables

- **contradictions** -- Detected conflicting facts: node_a_id, node_b_id, status (pending/resolved/false_positive), resolution
- **dedup_log** -- All dedup rejections for review: new_text, `existing_node_id` (NOT kept_id/removed_id), similarity, decision, review_status
- **decay_review_queue** -- Memories queued for review: node_id, confidence_at_queue, decision (null/delete/extend/pin)
- **metadata** -- Key-value system state: schema_version, embedding_model, embedding_dim
- **embedding_cache** -- Avoids re-computing embeddings: text_hash -> packed embedding blob
- **entity_aliases** -- Fuzzy name matching: alias -> canonical_name, with canonical_node_id
- **recall_log** -- Every recall() call with latency, result counts, reranker stats
- **health_snapshots** -- Periodic DB health metrics (written by janitor)
- **doc_update_log** -- Documentation update audit trail
- **doc_registry** -- Project/doc tracking (schema in `datastore/docsdb/registry.py` `ensure_table()`)

### FTS Sync Triggers

Triggers `nodes_ai`, `nodes_ad`, `nodes_au` keep `nodes_fts` in sync with `nodes` on INSERT/DELETE/UPDATE.

---

## QUAID_HOME Directory Layout

Each Quaid instance (adapter silo) lives in its own subdirectory under `QUAID_HOME`. Two environment variables control which silo is active:

- `QUAID_HOME` — root directory (default `~/quaid/`)
- `QUAID_INSTANCE` — instance identifier, e.g. `openclaw`, `claude-code`, or `codex` (no default; required)

```
$QUAID_HOME/
├── shared/                          # Cross-instance shared resources
│   ├── config/
│   │   └── memory.json              # Machine-wide embeddings config (Ollama URL, model, dim)
│   │                                # First-install-wins: written once, inherited by all instances
│   ├── projects/                    # Shared project docs (TOOLS.md, AGENTS.md, etc.)
│   │   └── <project-name>/
│   └── project-registry.json        # Global registry (projects -> instances)
└── <INSTANCE_ID>/                   # Per-instance silo (e.g. "openclaw", "claude-code", "codex")
    ├── config/
    │   └── memory.json              # Instance config (adapter.type, models, janitor, etc.)
    ├── data/
    │   ├── memory.db                # SQLite database
    │   ├── extraction-signals/      # Signals from hooks -> daemon
    │   ├── session-cursors/         # Extraction progress per session
    │   └── extraction-daemon.pid    # Daemon PID file
    ├── identity/                    # Quaid-managed identity files
    │   ├── USER.md
    │   ├── SOUL.md
    │   └── ENVIRONMENT.md
    ├── journal/                     # Journal files (journal/*.journal.md, archive/)
    ├── logs/                        # Structured JSONL logs, janitor checkpoints
    └── *.snippets.md                # Soul snippet staging files
```

Reserved instance names (cannot be used as instance identifiers): `shared`, `projects`, `config`, `data`, `logs`, `temp`, `tmp`, `quaid`, `plugins`, `lib`, `core`, `docs`, `assets`, `release`, `scripts`, `test`, `tests`, `benchmark`, `node_modules`.

---

## Configuration

Config is loaded from `<QUAID_HOME>/<INSTANCE_ID>/config/memory.json` (the per-instance config). Parsed by `config.py` into typed dataclasses. A shared config at `<QUAID_HOME>/shared/config/memory.json` provides machine-wide defaults (embeddings model, Ollama URL) that instance configs can override.

### Config Tree

```
config/memory.json
  models                   -- Model IDs, context windows, max output tokens
    fastReasoning           -- "default" (resolved at runtime; CC adapter default: "claude-haiku-4-5")
    deepReasoning          -- "default" (resolved at runtime; CC adapter default: "claude-sonnet-4-6")
    fastReasoningContext    -- 200000
    deepReasoningContext   -- 200000
    fastReasoningMaxOutput  -- 8192
    deepReasoningMaxOutput -- 16384
    batchBudgetPercent     -- 0.5
  capture                  -- Extraction settings
    enabled, strictness, skipPatterns
  decay                    -- Memory decay settings
    enabled, thresholdDays (30), ratePercent, minimumConfidence (0.1)
    protectVerified, protectPinned, reviewQueueEnabled
    mode ("exponential"), baseHalfLifeDays (60), accessBonusFactor (0.15)
  janitor                  -- Nightly pipeline settings
    enabled, dryRun, taskTimeoutMinutes
    opusReview             -- batchSize (50), maxTokens (4000), model
    dedup                  -- similarityThreshold (0.85), highSimilarityThreshold (0.95),
                              autoRejectThreshold (0.98), grayZoneLow (0.88), llmVerifyEnabled
    contradiction          -- Conflict detection settings
  retrieval                -- Recall settings (limits, similarity, notify)
  notifications            -- fullText (no truncation), showProcessingStart
  logging                  -- Log level, retention
  docs                     -- Doc auto-update settings
    sourceMapping          -- 8 source->doc mappings
    docPurposes            -- 6 doc descriptions
    coreMarkdown           -- Bloat monitoring (8 files with maxLines)
    journal                -- Journal/snippet settings (mode, targets, distillation)
  projects                 -- Projects system (definitions, staging dir)
  users                    -- Owner identity mapping
    identities.<owner>.personNodeName -- Maps owner to person node name
  ollama                   -- URL, embedding model (nomic-embed-text), embedding dim (768)
```

Additional keys worth knowing:
- `prompt_set` -- active prompt family selector (default `default`)
- `capture.inactivityTimeoutMinutes` / `capture.chunkSize`
- `janitor.applyMode` / `janitor.approvalPolicies`
- `retrieval.mmrLambda` / `retrieval.coSessionDecay` / `retrieval.routerFailOpen` / `retrieval.autoInject` / `retrieval.domains`

### Config Loading

- `get_config()` caches globally -- use `reload_config()` to force refresh
- Re-entrancy guard: `load_config()` -> `get_db_path()` -> `get_config()` cycle broken by `_config_loading` boolean
- Env overrides: `MEMORY_DB_PATH`, `OLLAMA_URL`, `QUAID_HOME`, `CLAWDBOT_WORKSPACE` (path resolution via adapter layer)
- `QUAID_DEV=1` enables dev features (unit tests in janitor)
- `QUAID_QUIET=1` suppresses config info messages

---

## Key Patterns

### Edge Normalization

The system normalizes edge relations to canonical forms. Three mechanisms:

- **`_INVERSE_MAP`**: Flips source/target direction. `child_of` -> `parent_of`, `led_to`/`caused`/`resulted_in`/`triggered` -> `caused_by`
- **`_SYNONYM_MAP`**: Same direction, different name. `mother_of` -> `parent_of`, `because_of`/`due_to` -> `caused_by`
- **`_SYMMETRIC_RELATIONS`**: Alphabetical ordering. `spouse_of`, `sibling_of`, `friend_of` -- always sorted so (A,B) where A < B

**Critical distinction:** `mother_of` is a SYNONYM (same direction), `child_of` is an INVERSE (flips direction). Causal canonical form is `caused_by` (effect -> cause direction).

### Memory Lifecycle

```
Conversation -> /compact or /reset -> Opus extracts facts+edges -> status=pending
    |
Nightly janitor -> review -> status=approved -> dedup -> decay -> status=active
    |
Active -> Ebbinghaus decay -> decayed -> re-reviewed -> kept or archived
Active -> Duplicate detected -> merged (crash-safe, single transaction)
Active -> Contradiction detected -> resolved -> winner kept
```

- Edges extracted AT CAPTURE TIME (not by janitor)
- `source_fact_id` links edges to their source fact
- Once `active`, a fact is never reprocessed -- only decay touches it

### Dual Snippet + Journal System

Two complementary learning systems, triggered at compaction/reset:

**Snippets** (fast path -- continuous core markdown refinement):
```
Conversation -> Opus extracts soul_snippets (bullet points)
  -> Written to *.snippets.md (per core markdown file)
  -> Nightly janitor (task 1d-snippets) -> FOLD/REWRITE/DISCARD review
  -> Approved snippets merged into SOUL.md, USER.md, etc.
```

**Journal** (slow path -- long-term reflective log):
```
Conversation -> Opus extracts journal_entries (diary paragraphs)
  -> Written to journal/*.journal.md (newest at top)
  -> Nightly janitor (task 1d-journal) -> Opus distills themes -> updates core markdown
  -> Distilled entries -> journal/archive/{FILE}-{YYYY-MM}.md
```

- Snippet format: `*.snippets.md` (bullets, `## Trigger -- date time`)
- Journal format: `journal/*.journal.md` (paragraphs, `## date -- Trigger`)
- Both dedup by date+trigger per file (one Compaction and one Reset per day)
- `journal/.distillation-state.json` tracks last distillation per file
- The dual system is intentional. Snippets = fast path (continuous core markdown refinement), Journal = slow path (long-term reflective log). They are NOT replacing each other.

### Merge Operations

All three merge paths (dedup/contradiction/review) use the shared `_merge_nodes_into()` helper in `datastore/memorydb/maintenance_ops.py`. This helper handles:
- Confidence inheritance (max of the two nodes)
- `confirmation_count` sum
- Edge migration (all edges transferred to survivor)
- `status="active"` on survivor
- `owner_id` from originals

Merges are crash-safe, executed in single database transactions.

### Retrieval Features

- **sqlite-vec**: Indexed ANN (Approximate Nearest Neighbor) vector search
- **Haiku API graded reranker**: 0-5 scale scoring
- **RRF fusion**: Reciprocal Rank Fusion with `_get_fusion_weights(intent)` for dynamic per-query-type weight adjustment
- **Intent classification**: WHO, WHEN, WHERE, WHAT, WHY, PREFERENCE, RELATION
- **HyDE**: Hypothetical Document Embedding (generates hypothetical answer for better vector match)
- **Multi-pass retrieval**: Confidence-gated second pass if first pass is insufficient
- **FTS5 BM25**: Porter stemming, column weights (name 2x, keywords 1x)
- **Temporal contiguity**: Co-session facts surface together
- **Competitive inhibition**: Reranker losers lose storage_strength (Bjork model)
- **Entity alias resolution**: Maps short names/nicknames to canonical entity names
- **Graph path explanation**: Traversal chains included in results
- **Read-time injection filtering**: `_sanitize_for_context()` + `[MEMORY]` prefix tags

---

## Janitor Pipeline (Execution Order)

| Task | Name | Purpose | LLM Cost |
|------|------|---------|----------|
| 0b | embeddings | Backfill missing embeddings | Free (local Ollama) |
| 0c | edges | Backfill missing relationship edges from stored facts | Free |
| 2 | review | Opus reviews pending facts | ~$0.01-0.05 per batch |
| 2a | temporal | Resolve relative dates (no LLM) | Free |
| 2b | dedup_review | Review dedup rejections (Opus) | ~$0.01-0.05 |
| 3+4 | duplicates/contradictions | Shared recall pass, then dedup (contradiction detection decommissioned) | ~$0.01-0.05 |
| 4b | contradictions (resolve) | **Disabled by default** (`janitor.contradiction.enabled=false`) | — |
| 5 | decay | Ebbinghaus confidence decay | Free |
| 5b | decay_review | Review decayed memories (Opus) | ~$0.01-0.05 |
| 1 | workspace | Core markdown review (Opus) | ~$0.05 |
| 1b | docs_staleness | Update stale docs from git diffs | ~$0.01-0.10 |
| 1c | docs_cleanup | Clean bloated docs (churn-based) | ~$0.01-0.05 |
| 1d | snippets | Soul snippets review (FOLD/REWRITE/DISCARD into core markdown) | ~$0.01-0.05 |
| 1d | journal | Distill journal entries into core markdown | ~$0.05-0.10 |
| 7 | rag | Reindex docs for RAG + project discovery | Free (local) |
| 8 | tests | Run vitest suite (`npm test`) | Free |
| 9 | cleanup | Prune old logs, orphaned embeddings | Free |
| 10 | update_check | Check for Quaid updates (version comparison + cache) | Free |
| 11 | graduate | Promote approved memories to active after a healthy memory pipeline | Free |

**Fail-fast:** If any memory task (2-5) fails, remaining memory tasks are SKIPPED and graduation is BLOCKED.

---

## Known Gotchas

These are hard-won lessons from 81+ bug fixes across 9 production rounds + 3 stress test rounds + 1 journal round + 6 benchmark analysis rounds. Read ALL of them before making changes.

### Database

#### DB: update_node() vs add_node()
Use `update_node()` for existing nodes, `add_node()` only for new nodes (avoids CASCADE trigger issues).

#### node.attributes
Always assign as a dict, never `json.dumps()`. Serialization happens inside `add_node()` / `update_node()`. Passing a JSON string will result in double-serialization.

#### get_connection()
Is a `@contextmanager`. MUST use `with get_connection() as conn:`. Calling it without `with` will not properly manage the connection lifecycle.

#### Resolution Summaries
After MERGE operations, use `status="archived"` on resolution summary nodes to prevent FTS recall leakage.

#### KNN Index Perturbation
Deleting/adding nodes from the sqlite-vec index reshuffles neighbor sets for ALL queries. 73% of dedup regressions were from this cascade effect, not direct merge replacement. Be cautious when deleting nodes from the vector index.

#### update_node() Doesn't Update created_at
The UPDATE SQL in `update_node()` deliberately excludes `created_at`. If you need to set `created_at`, use `store(..., created_at=...)` instead.

#### _switch_to_db()
MUST explicitly pass `db_path` to the `MemoryGraph()` constructor. The `DB_PATH` default parameter is captured at import time, so changing the environment variable alone does not work.

### Configuration

#### Model IDs
Short aliases only (`claude-opus-4-6`, `claude-haiku-4-5`). Dated IDs (e.g. `claude-opus-4-6-20260101`) return 404 errors.

#### Config Gotcha
The `coreMarkdown.files` section has filename keys like `"SOUL.md"`. The snake_case conversion in `config.py` would corrupt these to `"s_o_u_l.md"`. Fix: use `raw_config` directly for that section.

### TypeScript / Gateway

#### TS/JS Sync
`adaptors/openclaw/adapter.ts` is source of truth for runtime behavior; `adaptors/openclaw/adapter.js` must match manually. `adaptors/openclaw/index.ts` remains a minimal entry shim. Gateway loads `.js`, not `.ts`. Full restart required after plugin changes (SIGUSR1 does not reload TS).

#### Gateway Stale Process
`openclaw gateway restart` does not reliably kill the old process. The reliable sequence is:
```bash
openclaw gateway stop
pkill -f "openclaw-gateway"
# Wait for port 18789 to be free
openclaw gateway start
openclaw gateway install
```

### Janitor Pipeline

#### Edge Normalization
`_INVERSE_MAP` flips direction, `_SYNONYM_MAP` keeps same direction. `mother_of` is a SYNONYM, `child_of` is an INVERSE. Getting these confused breaks the graph.

#### FTS Owner Gap
`search_fts()` does not filter by `owner_id`. This causes leakage at low similarity thresholds. Always apply owner filtering after FTS results.

#### _ollama_healthy() Cache
Has a 30-second cache. To force a re-check, clear `_ollama_healthy._cache`.

#### Review Batch Truncation (FIXED)
`TokenBatchBuilder` now has an `output_tokens_per_item` param. The review task uses `models.max_output('deep')` (16384) instead of `opusReview.maxTokens` (4000) to avoid output truncation.

#### create-edge --create-missing
Use `create-edge --create-missing` when you want missing entities auto-created before edge insertion.

#### Merge Destructive Pattern (FIXED)
All 3 merge paths (dedup/contradiction/review) previously had 5 bugs: confidence reset to default, `confirmation_count` reset, wrong status, edges deleted instead of migrated, hardcoded owner. Now fixed with the shared `_merge_nodes_into()` helper. 19 tests cover: confidence inheritance, confirmation_count sum, edge migration, status="active", owner from originals.

#### Dedup Merge owner_id
`datastore/memorydb/maintenance_ops.py` currently defaults `owner_id` when merging without source nodes. Benchmark reprocessing may need a post-janitor SQL fixup to normalize owner IDs.

#### Contradiction Detection Scope
Only checks `pending` / `approved` facts. The ingest pipeline stores facts as `active`, making contradiction detection a no-op for benchmarks unless status is adjusted.

#### Dual System is Intentional
Snippets = fast path (continuous core markdown refinement). Journal = slow path (long-term reflective log). They are NOT replacing each other. Do not merge or remove either system.

### Journal System

#### Journal apply_distillation
Edits must flush to disk BEFORE additions. `insert_into_file()` re-reads from disk, and would overwrite in-memory edits that have not been flushed.

#### Journal archive_entries()
Takes a list of dicts (with `date`, `trigger`, `content` keys), NOT a list of date strings.

#### Journal TS Array Fallback
The LLM may return arrays instead of strings for `journal_entries` values. The code handles both, but new callers must be aware of this.

#### Journal Archive After Distillation
After a full janitor run, journal entries get distilled into core markdown then archived to `journal/archive/*.md`. The main `*.journal.md` file is left empty (just a header). When evaluating the journal system, you must load `journal/archive/*.md` too -- otherwise "with journal" is identical to "standard" (both empty).

### Benchmarks & Evaluation

#### Change Awareness Queries
Must split into `old_keywords` / `new_keywords`. A single new fact can satisfy `found_count >= 2` alone, causing false positives.

#### Week-gated Queries
Evolution chain queries need a `required_week` field. Without it, queries for un-injected facts silently report as failures instead of being properly gated.

#### Legacy Yes/No Judge Prompts
Must end with "Answer yes or no only." Uses the exact format: "Correct Answer:" (capital A), "Model Response:" (capital R), "Rubric:" (not "Desired response rubric:").

#### Parallel Eval Output Collision
When running evaluations in parallel, MUST use separate `--results-dir` values. Shared output filenames can overwrite each other across concurrent runs.

#### Python stdout Buffering
When running eval scripts in background, use `PYTHONUNBUFFERED=1` and `python3 -u`. Otherwise output is fully buffered and monitoring shows nothing until the process exits.

---

## Critical Invariants

Architectural constraints that must not be broken. Violating these causes data corruption, retrieval failures, or subtle bugs.

**Edge normalization maps are authoritative.** `_INVERSE_MAP` (flip direction), `_SYNONYM_MAP` (same direction), and `_SYMMETRIC_RELATIONS` (alphabetical sort) define canonical edge forms. Adding an entry to the wrong map silently corrupts the graph. `mother_of` is a SYNONYM, `child_of` is an INVERSE.

**Janitor task ordering is load-bearing.** Memory tasks (Phase 2) must run in order: embeddings before review, review before dedup, dedup before contradiction resolution, contradictions before decay. Reordering causes cascading failures (e.g., dedup without embeddings finds nothing).

**`_merge_nodes_into()` is the only safe merge path.** All three merge operations (dedup, contradiction, review) must use this shared helper. It executes in a single transaction, preserves confidence (max), sums confirmation_count, migrates edges, and sets status=active. Direct node deletion or manual merging bypasses these guarantees.

**FTS triggers must stay in sync.** `nodes_ai`, `nodes_ad`, `nodes_au` triggers keep `nodes_fts` synchronized with `nodes`. Any schema change to the `nodes` table must preserve or update these triggers, or FTS search silently returns stale results.

**The dual snippet/journal system is intentionally separate.** Snippets (fast path, continuous core markdown updates) and journal (slow path, reflective synthesis) serve different cognitive functions. Merging them, removing either, or routing one's output through the other breaks the design. Both are independently triggered at compaction/reset and independently processed by the janitor.

**Pending facts are visible to recall.** Facts with `status=pending` are returned by the retrieval pipeline. The janitor improves quality (review, dedup, contradiction resolution) but does not gate visibility. Any change that filters out pending facts from recall will cause newly extracted memories to be invisible until the next janitor run.

**Content hash dedup happens before embedding.** `store()` checks `content_hash` (SHA256) first for exact dedup, then generates the embedding, then checks semantic similarity. Reordering these steps wastes embedding compute on exact duplicates.

**Datastore owns metadata preservation on dedup paths.** `store()` applies metadata flags (`source_type`, `domains`) on duplicate/update paths too. Ingest/API layers should not mutate graph internals after write.

---

## CLI Reference

### `quaid` CLI (Primary Interface)

The `quaid` CLI works standalone -- no gateway needed.

```bash
# Store
quaid store <text>            # Store a single memory (--category, --owner, --domains, ...)

# Search & Retrieve
quaid recall <query>          # Recall memories (semantic + graph + rerank)
quaid recall <query>          # Recall pipeline helper
quaid get-node <id>           # Get a memory by ID
quaid get-edges <id>          # Get edges for a memory node
quaid recall <query> '{"stores":["docs"]}'  # Search project documentation

# Manage
quaid forget [query]          # Delete a memory (--id <id>)
quaid create-edge <s> <r> <o> # Create a relationship edge
quaid registry <subcmd>       # Project/doc registry (list/read/register/create-project/...)
quaid updater <subcmd>        # Project event processor

# Admin
quaid doctor                  # Health check (DB, embeddings, API key, gateway)
quaid config                  # Show current configuration
quaid stats                   # Database statistics
quaid health                  # Detailed KB health metrics
quaid janitor [opts]          # Run janitor pipeline (--dry-run, --task <name>)
quaid event [subcmd]          # Event bus (emit/list/process/capabilities)
quaid compat [subcmd]         # Compatibility check (status/check/matrix)
quaid docs changelog          # View doc update history
```

### Python Module CLIs (Advanced)

All commands run from the `modules/quaid/` directory.

```bash
# Memory operations
python3 datastore/memorydb/memory_graph.py store "fact text" --owner default --category preference
python3 datastore/memorydb/memory_graph.py recall "query"   # Recall memories
python3 datastore/memorydb/memory_graph.py stats
python3 datastore/memorydb/memory_graph.py get-edges <node_id>

# Extraction
python3 ingest/extract.py transcript.txt --owner default
python3 ingest/extract.py session.jsonl --dry-run --json
echo "User: hi" | python3 ingest/extract.py - --owner default

# Janitor pipeline
python3 core/lifecycle/janitor.py --task all --dry-run           # Preview (no changes)
python3 core/lifecycle/janitor.py --task review --apply           # Opus review of pending facts
python3 core/lifecycle/janitor.py --task snippets --apply         # FOLD/REWRITE/DISCARD snippet review
python3 core/lifecycle/janitor.py --task journal --apply          # Journal distillation
python3 core/lifecycle/janitor.py --task embeddings               # Backfill missing embeddings
python3 core/lifecycle/janitor.py --task decay --apply            # Run Ebbinghaus decay
python3 core/lifecycle/janitor.py --task duplicates --apply       # Dedup pass
python3 core/lifecycle/janitor.py --task cleanup                  # Prune old logs

# Documentation
python3 datastore/docsdb/updater.py check                      # Check doc staleness (free)
python3 datastore/docsdb/updater.py update-stale --apply       # Fix stale docs (Opus calls)
python3 datastore/docsdb/rag.py search "query text"     # RAG search (free, local)

# Projects
python3 datastore/docsdb/registry.py list --project quaid
python3 datastore/docsdb/registry.py find-project <file_path>
python3 datastore/docsdb/registry.py create-project <name> --label "Human Name"
python3 datastore/docsdb/registry.py discover --project <name>
python3 datastore/docsdb/registry.py gc                        # Garbage collect

# Workspace audit
python3 core/lifecycle/workspace_audit.py --bloat                 # Line counts vs limits
python3 core/lifecycle/workspace_audit.py --check-only            # Changed files
```

### Testing

```bash
# Run all pytest tests
python3 -m pytest tests/ -v

# Run specific test file
python3 -m pytest tests/test_extract.py -v

# Run specific test
python3 -m pytest tests/test_invariants.py::test_name -v
```

---

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `QUAID_OWNER` | Owner identity for CLI and adapter-driven runtime operations | `"default"` |
| `QUAID_HOME` | Root directory containing all Quaid instances | `~/quaid/` |
| `QUAID_INSTANCE` | Instance identifier — selects which silo under `QUAID_HOME` is active (e.g. `openclaw`, `claude-code`) | Required — no implicit default |
| `adapter.type` (in `config/memory.json`) | Select adapter: `standalone`, `openclaw`, or `claude-code` | Required |
| `CLAWDBOT_WORKSPACE` | OC adapter workspace root (Python adapter). Also accepted by the TS adapter as lowest-priority fallback after `QUAID_HOME` and `QUAID_WORKSPACE`. | Optional |
| `QUAID_WORKSPACE` | OC TS adapter workspace root (second-priority, after `QUAID_HOME`). | Optional |
| `QUAID_MESSAGE_CLI` | Override the `openclaw` binary path used for OC notifications. Skips auto-detection. | Not set |
| `OPENCLAW_GATEWAY_URL` | Override OC gateway base URL for LLM routing. | `http://127.0.0.1:<port from openclaw.json>` |
| `OPENCLAW_GATEWAY_TOKEN` | Override OC gateway auth token. | Read from `~/.openclaw/openclaw.json` |
| `MEMORY_DB_PATH` | Override database file path | `<quaid_home>/<instance_id>/data/memory.db` |
| `OLLAMA_URL` | Ollama server URL | `http://localhost:11434` |
| `ANTHROPIC_API_KEY` | Anthropic API key for LLM calls | Loaded from env; `.env` file at `QUAID_HOME/.env` is a fallback only when `retrieval.fail_hard=false` |
| `OPENAI_API_KEY` | OpenAI API key (for benchmark judging) | Must be set explicitly |
| `QUAID_DEV` | Enable dev mode (unit tests in janitor, verbose output) | Not set |
| `QUAID_QUIET` | Suppress informational config messages | Not set |
| `QUAID_DISABLE_NOTIFICATIONS` | Suppress all runtime notifications (bypassed when `force=True`) | Not set |
| `QUAID_RULES_DIR` | Override `.claude/rules/` directory for CC `hook-session-init` output | Not set |
| `MOCK_EMBEDDINGS` | Use deterministic fake embeddings (for testing) | Not set |

API key fallback chain for Python adapters: `ANTHROPIC_API_KEY` env var -> `.env` file at `QUAID_HOME/.env` (only attempted when `retrieval.fail_hard=false`; skipped in the default strict mode). In OC, the Python adapter also resolves credentials from `~/.openclaw/agents/main/agent/auth-profiles.json` (gateway credential store) for internal LLM calls. There is no macOS Keychain fallback in any adapter.

OC LLM calls route through the gateway's `/v1/responses` endpoint (not the Anthropic API directly). CC uses OAuth direct API via `ClaudeCodeOAuthLLMProvider`. The active LLM provider for each adapter is determined by `adapter.type` in `config/memory.json` and resolved through the facade tier system (`deep`/`fast`).

---

## Test Suite

### Overview

- Current baseline: 2,236 selected pytest tests (+312 deselected) and 333 vitest tests
- All tests passing as of Mar 2026

### Test Files (pytest)

| File | Coverage Area |
|------|---------------|
| `test_store_recall.py` | Store and recall pipeline |
| `test_search.py` | Search functionality |
| `test_graph_traversal.py` | Core graph traversal and vec write failure handling |
| `test_janitor_lifecycle.py` | Janitor lifecycle orchestration and task plumbing |
| `test_edge_normalization.py` | Edge normalization (inverse, synonym, symmetric) |
| `test_soul_snippets.py` | Snippet and journal systems |
| `test_docs_registry.py` | Project/doc registry CRUD |
| `test_docs_updater.py` | Doc staleness and updates |
| `test_docs_rag.py` | RAG indexing and search |
| `test_config.py` | Config loading and parsing |
| `test_llm_clients.py` | LLM client wrapper |
| `test_notify.py` | Notification system |
| `test_workspace_audit.py` | Workspace audit and bloat detection |
| `test_project_updater.py` | Project event processing |
| `test_lib.py` | Shared library functions |
| `test_text_utils.py` | Text utilities and token counting |
| `test_temporal_resolution.py` | Temporal date resolution |
| `test_token_batching.py` | TokenBatchBuilder behavior |
| `test_invariants.py` | 8 structural lifecycle invariants |
| `test_merge_nodes.py` | 19 tests for merge operations (confidence, count, edges, status, owner) |
| `test_golden_recall.py` | Golden dataset regression (30 facts, 20 queries, Recall@5) |
| `test_graph_traversal.py` | Graph traversal and path finding |
| `test_entity_summary.py` | Entity summary generation |
| `test_storage_strength.py` | Bjork storage strength model |
| `test_beam_search.py` | BEAM graph search algorithm |
| `test_adapter.py` | Platform adapter layer (55 tests: selection, paths, credentials, notifications) |
| `test_coverage_gaps.py` | Coverage gap identification |
| `test_extract.py` | Extraction module (transcript parsing, pipeline, dry-run) |
| `test_integration.py` | Cross-module integration tests |
| `test_protected_regions.py` | Protected region handling |
| `test_batch2_data_quality.py` | Data quality checks |
| `test_batch3_smart_retrieval.py` | Smart retrieval features |
| `test_batch4_decay_traversal.py` | Decay and traversal interaction |
| `test_chunk1_improvements.py` | Improvement batch 1 |
| `test_chunk2_improvements.py` | Improvement batch 2 |

### Test Files (vitest / TypeScript)

| File | Coverage Area |
|------|---------------|
| `store.test.ts` | Memory storage via TS |
| `query.test.ts` | Query operations |
| `recall-pipeline.test.ts` | Full recall pipeline |
| `dedup.test.ts` | Deduplication |
| `decay.test.ts` | Decay operations |
| `decay-review.test.ts` | Decay review queue |
| `delete.test.ts` | Memory deletion |
| `embedding.test.ts` | Embedding operations |
| `session.test.ts` | Session tracking |
| `pinning.test.ts` | Memory pinning |
| `isolation.test.ts` | Multi-user isolation |
| `edge-cases.test.ts` | Edge case handling |
| `rag.test.ts` | RAG functionality |
| `setup.ts` | Test setup/teardown utilities |

### Key Test Concepts

- **Golden dataset**: 30 carefully curated facts + 20 queries with known-good answers. Tests Recall@5 regression.
- **Lifecycle invariants**: 8 structural rules that must always hold (in `test_invariants.py`).
- **Adversarial queries**: 10 adversarial queries in the golden dataset testing resistance to misleading queries.
- **Merge tests**: 19 tests covering crash-safe merge behavior across all 3 paths.

---

## Benchmark Results (AgentLife)

Canonical benchmark docs:
- https://github.com/quaid-labs/agentlife
- https://github.com/quaid-labs/agentlife/blob/main/published/runbooks/AGENTLIFE_TECHNICAL_REPORT.md

---

## Architecture Decisions

### Why SQLite (not Postgres/Neo4j)?
Local-first, zero-dependency, single-file database. WAL mode provides concurrent read access. sqlite-vec gives vector search without a separate service.

### Why RRF Fusion (not learned weights)?
Reciprocal Rank Fusion is parameter-free and robust. Dynamic weights per intent type (`_get_fusion_weights()`) give query-adaptive behavior without training data.

### Why Dual Snippet + Journal?
Snippets are fast, surgical updates to core markdown files. Journal provides deeper reflective context that develops over time. They serve different cognitive functions and are complementary.

### Why Crash-Safe Merges?
Early bugs lost data during node merges (edges deleted, confidence reset, counts zeroed). The shared `_merge_nodes_into()` helper executes everything in a single SQLite transaction.

### Why Prompt Versioning?
SHA256 hash prefix on all janitor LLM decisions provides traceability. If the prompt changes, the hash changes, making it easy to attribute behavior changes.

---

## Common Development Tasks

### Adding a CLI Command

Edit `datastore/memorydb/memory_graph.py`, find the argparse section at the bottom:
```python
subparsers.add_parser("my-command", help="...")
# Then add handler:
if args.command == "my-command":
    ...
```

### Adding a Config Section

1. Add a dataclass in `config.py`
2. Add a field to the parent config (e.g., `DocsConfig`)
3. Parse in `load_config()`
4. Access via `get_config().section.field`

### Adding a Janitor Task

1. Add a function in `datastore/memorydb/maintenance_ops.py` for datastore intelligence and orchestrate it from `core/lifecycle/janitor.py`
2. Register it in `run_task_optimized()` task dispatch
3. Decide its position in the pipeline ordering
4. Add `--task <name>` handling to the CLI argument parser
5. Write tests

### Running the Full Pipeline

```bash
# Preview (no changes, no LLM calls for most tasks)
python3 core/lifecycle/janitor.py --task all --dry-run

# Apply all tasks (costs ~$0.10-0.50 depending on pending work)
python3 core/lifecycle/janitor.py --task all --apply
```

---

## Project History

- Started as inline memory extraction in an OpenClaw plugin
- Evolved: sqlite-vec vectors, FTS5, graph traversal, janitor pipeline
- 81+ bugs fixed across 9 production rounds + 3 stress test rounds + 1 journal round + 6 benchmark analysis rounds
- Key architectural milestones: crash-safe merges, dual learning system, RRF fusion, BEAM graph search, prompt caching, output-aware batching, extraction daemon, standalone CLI
- Large codebase: production Python, TypeScript, and extensive test suites across all layers

---

## Safety Rules

- Do not exfiltrate private data
- Do not run destructive commands without asking
- `trash` is preferred over `rm` -- always recoverable
- Wrap plugins in error handlers -- do not crash the gateway
- Test changes before deploying
- Backup before risky changes: run the workspace backup scripts (`scripts/backup-*.sh`) to save your data

---

## Further Documentation

| Topic | Primary Doc |
|-------|-------------|
| Memory architecture | `projects/quaid/reference/memory-reference.md` |
| Plugin implementation | `projects/quaid/reference/memory-reference.md` |
| Janitor pipeline | `projects/quaid/reference/janitor-reference.md` |
| Database schema | `projects/quaid/reference/memory-reference.md` |
| Deduplication | `projects/quaid/reference/memory-deduplication-system.md` |
| Operations guide | `projects/quaid/reference/memory-operations-guide.md` |
| Projects CLI | `projects/quaid/reference/projects-reference.md` |
| Project onboarding | `projects/quaid/operations/project_onboarding.md` |

> **Note:** These documentation paths reference internal workspace files and are not included in the public release repo. They are available in development environments where the full workspace is present.

**RAG search for docs:** `python3 datastore/docsdb/rag.py search "query"`
