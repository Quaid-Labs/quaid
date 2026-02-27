# Quaid AI Agent Reference

This document is designed for AI agents (Claude, GPT, etc.) working on or with the Quaid knowledge layer. It provides a complete index of files, functions, patterns, and known issues. Treat this as your primary orientation guide before making any changes.

---

## System Overview

Quaid is a graph-based persistent knowledge layer for AI agents. It works with any system that supports [MCP](https://modelcontextprotocol.io) (Claude Desktop, Claude Code, Cursor, Windsurf, etc.) and ships with a deep integration for [OpenClaw](https://github.com/openclaw/openclaw). Backed by SQLite with sqlite-vec for vector search, FTS5 for full-text search, and an LLM-powered nightly maintenance pipeline ("janitor").

**Architecture stack:**
- **Interfaces:** MCP server (stdio, any MCP client), CLI (`quaid` commands), OpenClaw plugin (TypeScript hooks)
- **Backend:** Python modules for graph operations, extraction, retrieval, maintenance, docs, and project tracking
- **Storage:** SQLite database with WAL mode, sqlite-vec ANN index, FTS5 full-text index
- **Embeddings:** Ollama local server (qwen3-embedding:8b, 4096 dimensions)
- **LLM calls:** Anthropic API (Opus for deep-reasoning, Haiku for fast-reasoning / reranking)
- **Config:** JSON config file at `config/memory.json`

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
| `adaptors/openclaw/maintenance.py` | OpenClaw-specific lifecycle registrations | `register_lifecycle_routines()` (workspace audit registration) |
| `core/lifecycle/workspace_audit.py` | Workspace markdown audit implementation | `run_workspace_check()`, `check_bloat()` |
| `datastore/notedb/soul_snippets.py` | Dual snippet + journal learning system | `run_soul_snippets_review()`, `run_journal_distillation()` |
| `datastore/docsdb/rag.py` | RAG indexing/search and lifecycle registration | `search_docs()`, `index_document()`, `register_lifecycle_routines()` |
| `datastore/docsdb/updater.py` | Doc staleness/cleanup maintenance routines | `check_staleness()`, `update_doc_from_diffs()`, `register_lifecycle_routines()` |
| `datastore/docsdb/registry.py` | Project/doc registry and path resolution | `create_project()`, `auto_discover()`, `register()`, `find_project_for_path()` |
| `datastore/docsdb/project_updater.py` | Background project event processor | `process_event()`, `refresh_project_md()` |
| `core/runtime/events.py` | Queue-backed runtime event bus | `emit_event()`, `list_events()`, `process_events()`, `get_event_registry()` |
| `core/runtime/notify.py` | User notifications via adapter/runtime context | `notify_user()`, retrieval/extraction/janitor/doc notifications |
| `core/runtime/logger.py` | Structured JSONL logger with rotation | `Logger`, `rotate_logs()`, `memory_logger`, `janitor_logger` |
| `core/interface/mcp_server.py` | MCP server surface | `memory_extract`, `memory_store`, `memory_recall`, `memory_write`, `memory_search`, `memory_get`, `memory_forget`, `memory_create_edge`, `memory_stats`, `projects_search`, `session_recall`, `memory_provider`, `memory_capabilities`, `memory_event_*` |
| `core/interface/api.py` | Public API facade | `store()`, `recall()`, `search()`, `create_edge()`, `forget()`, `get_memory()`, `stats()`, `extract_transcript()`, `projects_search_docs()` |
| `lib/llm_clients.py` | Canonical LLM client wrapper with prompt parsing/usage tracking | `call_deep_reasoning()`, `call_fast_reasoning()`, `call_llm()`, `parse_json_response()` |
| `core/llm/clients.py` | Compatibility alias to canonical LLM client module | module alias to `lib.llm_clients` |
| `ingest/extract.py` | Extraction module — transcript to memories | `extract_from_transcript()`, `parse_session_jsonl()`, `build_transcript()` |
| `ingest/docs_ingest.py` | Ingest pipeline for docs from transcript/source mapping | ingest orchestration helpers |
| `datastore/memorydb/semantic_clustering.py` | Semantic clustering for contradiction candidate reduction | `classify_node_semantic_cluster()`, `get_memory_clusters()` |
| `datastore/memorydb/schema.sql` | Database DDL (nodes, edges, FTS5, indexes, operational tables) | Full schema definition |
| `datastore/memorydb/enable_wal.py` | WAL mode enablement helper | `enable_wal_mode()` |

### Shared Library (`lib/`)

| File | Purpose | Key Exports |
|------|---------|-------------|
| `adapter.py` | Platform adapter layer base/standalone adapters | `QuaidAdapter`, `StandaloneAdapter`, `get_adapter()`, `set_adapter()`, `reset_adapter()`, `ChannelInfo` |
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
| `adaptors/openclaw/adapter.ts` | OpenClaw runtime integration (SOURCE OF TRUTH) | Hook registration, tool schemas (`memory_recall`, `memory_store`, `projects_search`), extraction triggers, notifications |
| `orchestrator/default-orchestrator.ts` | Knowledge routing/orchestration | `total_recall`, datastore normalization/routing, recall aggregation/fusion |
| `core/data-writers.ts` | Canonical write routing/dispatch | `createDataWriteEngine()`, `writeData()`, DataWriter registry/specs |
| `adaptors/openclaw/index.js` / `adapter.js` / `orchestrator/default-orchestrator.js` | Runtime JS loaded by gateway | Keep TS/JS runtime pairs synchronized; gateway executes `.js` |

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

## Configuration

Config is loaded from `config/memory.json`. Parsed by `config.py` into typed dataclasses.

### Config Tree

```
config/memory.json
  models                   -- Model IDs, context windows, max output tokens
    fastReasoning           -- "claude-haiku-4-5" (Haiku)
    deepReasoning          -- "claude-opus-4-6" (Opus)
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
  ollama                   -- URL, embedding model (qwen3-embedding:8b), embedding dim (4096)
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
| 2 | review | Opus reviews pending facts | ~$0.01-0.05 per batch |
| 2a | temporal | Resolve relative dates (no LLM) | Free |
| 2b | dedup_review | Review dedup rejections (Opus) | ~$0.01-0.05 |
| 3+4 | duplicates/contradictions | Shared recall pass, then dedup + contradiction detection | ~$0.01-0.05 |
| 4b | contradictions (resolve) | Resolve contradictions (Opus) | ~$0.01-0.05 |
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
`clawdbot gateway restart` does not reliably kill the old process. The reliable sequence is:
```bash
clawdbot gateway stop
pkill -f "openclaw-gateway"
# Wait for port 18789 to be free
clawdbot gateway start
clawdbot gateway install
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

#### LongMemEval Judge Prompts
Must end with "Answer yes or no only." Uses the paper's exact format: "Correct Answer:" (capital A), "Model Response:" (capital R), "Rubric:" (not "Desired response rubric:"). Reference implementation at `xiaowu0162/LongMemEval/src/evaluation/evaluate_qa.py`.

#### Parallel Eval Output Collision
When running evaluations in parallel, MUST use separate `--results-dir` values. Both write `evaluation_results.json` per conversation and `locomo_results.json`, silently overwriting each other.

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
quaid search <query>          # Search memories (full recall pipeline)
quaid recall <query>          # Recall pipeline helper
quaid get-node <id>           # Get a memory by ID
quaid get-edges <id>          # Get edges for a memory node
quaid docs search <query>     # Search project documentation

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
quaid mcp-server              # Start MCP server (stdio transport)
```

### Python Module CLIs (Advanced)

All commands run from the `modules/quaid/` directory.

```bash
# Memory operations
python3 datastore/memorydb/memory_graph.py store "fact text" --owner default --category preference
python3 datastore/memorydb/memory_graph.py search "query" --owner default --limit 10
python3 datastore/memorydb/memory_graph.py search-all "query"     # Unified memory search (datastore only)
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
| `QUAID_OWNER` | Owner identity for MCP server and CLI | `"default"` |
| `QUAID_HOME` | Root directory for standalone mode | `~/quaid/` |
| `adapter.type` (in `config/memory.json`) | Select adapter: `standalone` or `openclaw` | Required |
| `CLAWDBOT_WORKSPACE` | Workspace root hint (for OpenClaw paths) | Optional |
| `MEMORY_DB_PATH` | Override database file path | `<quaid_home>/data/memory.db` |
| `OLLAMA_URL` | Ollama server URL | `http://localhost:11434` |
| `ANTHROPIC_API_KEY` | Anthropic API key for LLM calls | Loaded from `.env` file or macOS Keychain |
| `OPENAI_API_KEY` | OpenAI API key (for benchmark judging) | Must be set explicitly |
| `QUAID_DEV` | Enable dev mode (unit tests in janitor, verbose output) | Not set |
| `QUAID_QUIET` | Suppress informational config messages | Not set |
| `MOCK_EMBEDDINGS` | Use deterministic fake embeddings (for testing) | Not set |

API key fallback chain: `ANTHROPIC_API_KEY` env var -> `.env` file in `QUAID_HOME` -> macOS Keychain (OpenClaw adapter only).

---

## Test Suite

### Overview

- Current baseline: 1224 selected pytest tests (+333 deselected) and 222 vitest tests
- All tests passing as of Feb 2026

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
| `test_mcp_server.py` | MCP server tool definitions and responses |
| `test_mcp_integration.py` | MCP store-recall round-trip integration tests |
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

## Benchmark Results (LoCoMo, Feb 2026)

| Configuration | Accuracy | Answer Model | Notes |
|---------------|----------|--------------|-------|
| Quaid + Haiku | 70.28% | Haiku | Fair comparison tier |
| Mem0 (graphRAG) | 68.9% | GPT-4o-mini | Apr 2025 numbers |
| Mem0 | 66.9% | GPT-4o-mini | Apr 2025 numbers |
| Zep | 66.0% | GPT-4o-mini | Apr 2025 numbers |
| LangMem | 58.1% | GPT-4o-mini | Apr 2025 numbers |
| OpenAI | 52.9% | GPT-4o-mini | Apr 2025 numbers |
| Quaid + Journal + Haiku | 74.48% +/- 0.05 | Haiku | Best Haiku result, nearly matches Opus at ~46% cost |
| **Quaid + Opus** | **75.00%** | **Opus** | **Production config** |
| v2 Standard (full janitor) | 69.11% +/- 0.17 | Haiku | Regression from dedup merge bug (since fixed) |
| Full-context Haiku | 79.59% +/- 0.17 | Haiku | Upper bound (no knowledge layer) |
| Full-context Opus | 86.62% +/- 0.09 | Opus | Upper bound (no knowledge layer) |

**Key insight:** Journal + Haiku (74.5%) nearly matches v1 Opus (75.0%) at roughly 46% of the cost. Journal helps most on temporal questions (+7.6pp) and single-hop questions (+6.8pp).

**Benchmark code:** See `benchmark/agentlife/` and [docs/BENCHMARKS.md](BENCHMARKS.md) for full methodology.

**LongMemEval** (ICLR 2025): 500 QA pairs, 7 types, 19,195 unique sessions. Pending full evaluation run.

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
- Key architectural milestones: crash-safe merges, dual learning system, RRF fusion, BEAM graph search, prompt caching, output-aware batching, MCP server, standalone CLI
- ~39K lines of code: 18K Python, 13K tests, 5K TS/JS, 3.1K vitest

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
| Memory architecture | `projects/quaid/reference/memory-system-design.md` |
| Plugin implementation | `projects/quaid/reference/memory-local-implementation.md` |
| Janitor pipeline | `projects/quaid/reference/janitor-reference.md` |
| Database schema | `projects/quaid/reference/memory-schema.md` |
| Deduplication | `projects/quaid/reference/memory-deduplication-system.md` |
| Operations guide | `projects/quaid/reference/memory-operations-guide.md` |
| Projects CLI | `projects/quaid/reference/projects-cli-reference.md` |
| Project onboarding | `projects/quaid/operations/project_onboarding.md` |

> **Note:** These documentation paths reference internal workspace files and are not included in the public release repo. They are available in development environments where the full workspace is present.

**RAG search for docs:** `python3 datastore/docsdb/rag.py search "query"`
