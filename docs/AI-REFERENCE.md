# Quaid AI Agent Reference

This document is designed for AI agents (Claude, GPT, etc.) working on or with the Quaid memory system. It provides a complete index of files, functions, patterns, and known issues. Treat this as your primary orientation guide before making any changes.

---

## System Overview

Quaid is a graph-based personal memory system for AI assistants. It runs as an OpenClaw plugin (TypeScript entry point, Python core) backed by SQLite with sqlite-vec for vector search, FTS5 for full-text search, and an LLM-powered nightly maintenance pipeline ("janitor").

**Architecture stack:**
- **Frontend:** TypeScript OpenClaw plugin (index.ts / index.js) registers hooks and tools with the gateway
- **Backend:** Python modules for graph operations, retrieval, maintenance, docs, and project tracking
- **Storage:** SQLite database with WAL mode, sqlite-vec ANN index, FTS5 full-text index
- **Embeddings:** Ollama local server (qwen3-embedding:8b, 4096 dimensions)
- **LLM calls:** Anthropic API (Opus for high-reasoning, Haiku for low-reasoning / reranking)
- **Config:** JSON config file at `config/memory.json`

**Retrieval pipeline:**
```
Query
  -> Intent classification (WHO/WHEN/WHERE/WHAT/WHY/PREFERENCE/RELATION)
  -> HyDE query expansion (hypothetical answer embedding)
  -> Parallel: sqlite-vec ANN + FTS5 BM25 + Graph traversal
  -> Reciprocal Rank Fusion (dynamic weights per intent)
  -> Optional: Haiku LLM reranker (0-5 graded scale)
  -> Optional: Multi-pass second retrieval (confidence-gated)
  -> Read-time injection filtering (_sanitize_for_context)
```

---

## File Index

### Core Python Files

| File | Purpose | Key Exports / Functions |
|------|---------|------------------------|
| `memory_graph.py` | Graph operations, hybrid retrieval, CLI | `store()`, `recall()`, `search()`, `create_edge()`, `get_graph()`, `get_related_bidirectional()`, `_get_fusion_weights()`, `_normalize_edge()`, `_sanitize_for_context()`, `_forget()`, `_get_memory()`, `MemoryGraph`, `Node`, `Edge`, CLI (argparse at bottom) |
| `janitor.py` | 17-task nightly maintenance pipeline | `run_task_optimized()`, `_merge_nodes_into()`, `_normalize_edge()`, `run_tests()`, task functions for backup/embeddings/review/temporal/dedup/contradictions/decay/workspace/docs/snippets/journal/rag/tests/cleanup |
| `soul_snippets.py` | Dual snippet + journal learning system | `run_soul_snippets_review()`, `run_journal_distillation()`, `archive_entries()`, `insert_into_file()`, `apply_distillation()`, snippet/journal I/O helpers |
| `config.py` | Typed config from memory.json | `get_config()`, `reload_config()`, `load_config()`, `MemoryConfig`, `CoreMarkdownConfig`, `NotificationsConfig`, `ModelsConfig`, `DecayConfig`, `JanitorConfig`, `DocsConfig`, `RetrievalConfig`, `CaptureConfig` |
| `llm_clients.py` | Anthropic API wrapper with prompt caching | `call_high_reasoning()`, `call_low_reasoning()`, `get_api_key()`, `parse_json_response()`, prompt caching (~90% savings on repeated prompts) |
| `docs_rag.py` | RAG indexing and search over project docs | `search()`, `index_docs()`, chunk management |
| `docs_updater.py` | Auto-update docs from git diffs | `check_staleness()`, `update_doc_from_diffs()`, changelog, Haiku pre-filter gate (skips trivial diffs) |
| `docs_registry.py` | Project/doc registry, CRUD, path resolution | `create_project()`, `auto_discover()`, `register()`, `find_project_for_path()`, `gc()`, `DocsRegistry`, `ensure_table()` |
| `workspace_audit.py` | Core markdown monitoring and bloat detection | `run_workspace_check()`, `check_bloat()`, Opus review of changed files |
| `notify.py` | User notifications via gateway | `notify_user()`, `notify_memory_extraction()`, `notify_memory_recall()`, `_check_janitor_health()` |
| `project_updater.py` | Background project event processor | `process_event()`, `refresh_project_md()` |
| `api.py` | Public API with simplified signatures | `store()`, `recall()`, `search()`, `create_edge()`, `forget()`, `get_memory()`, `get_graph()`, `Node`, `Edge` |
| `logger.py` | Structured JSONL logger with rotation | `log()`, `Logger` class, `rotate_logs()`, `clean_old_archives()`, `get_log_path()`, module-level `memory_logger`, `janitor_logger` |
| `semantic_clustering.py` | Groups memories by domain for O(n) contradiction checking | `classify_node_semantic_cluster()`, `get_memory_clusters()`, `get_contradiction_pairs_by_cluster()` |
| `schema.sql` | Database DDL (nodes, edges, FTS5, indexes, operational tables) | Full schema definition |
| `enable_wal.py` | One-time WAL mode enablement script | `enable_wal_mode()` |
| `seed.py` | Seed database from MEMORY.md files | Parsing and population script |
| `seed_optimized.py` | Seed with atomic facts via CLI | Uses `memory_graph.py store` subprocess |
| `test_recall.py` | Manual recall test harness | Simulates injection and memory_recall paths |

### Shared Library (`lib/`)

| File | Purpose | Key Exports |
|------|---------|-------------|
| `config.py` | Path resolution, env overrides for tests | `get_db_path()`, `get_ollama_url()`, `get_embedding_model()`, `get_archive_db_path()` |
| `database.py` | SQLite connection factory | `get_connection()` -- @contextmanager, enables WAL mode, FK ON, busy_timeout=30000 |
| `embeddings.py` | Ollama embedding calls | `get_embedding()`, `pack_embedding()`, `unpack_embedding()` |
| `similarity.py` | Cosine similarity calculations | `cosine_similarity()` |
| `tokens.py` | Token counting and text comparison | `TokenBatchBuilder` (with `output_tokens_per_item` param), `count_tokens()` |
| `archive.py` | Archive database for decayed memories | `archive_node()`, `search_archive()`, `_get_archive_conn()` |
| `__init__.py` | Package init | (empty or minimal) |

### TypeScript / JavaScript

| File | Purpose | Notes |
|------|---------|-------|
| `index.ts` | Plugin entry point (SOURCE OF TRUTH) | Hook registration (before_agent_start, before_compaction, before_reset), tool definitions (memory_recall, memory_store), extraction trigger |
| `index.js` | Compiled JS -- gateway loads this | Must be kept in sync with .ts manually. Gateway loads `.js`, not `.ts`. Full restart required after plugin changes. |

### Prompt Templates (`prompts/`)

| File | Purpose |
|------|---------|
| `project_onboarding.md` | Agent instructions for project discovery and registration workflow |

### Database Migrations (`migrations/`)

| File | Purpose |
|------|---------|
| `001_add_owner_id.sql` | Add owner_id column to nodes |
| `002_add_session_fields.sql` | Add session tracking fields |
| `002_privacy_columns.sql` | Add privacy tier columns |
| `003_add_doc_chunks.sql` | Add doc chunk support |

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
- **node_embeddings** -- sqlite-vec virtual table for ANN search
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
- **doc_registry** -- Project/doc tracking (schema in docs_registry.py `ensure_table()`)

### FTS Sync Triggers

Triggers `nodes_ai`, `nodes_ad`, `nodes_au` keep `nodes_fts` in sync with `nodes` on INSERT/DELETE/UPDATE.

---

## Configuration

Config is loaded from `config/memory.json`. Parsed by `config.py` into typed dataclasses.

### Config Tree

```
config/memory.json
  models                   -- Model IDs, context windows, max output tokens
    lowReasoning           -- "claude-haiku-4-5" (Haiku)
    highReasoning          -- "claude-opus-4-6" (Opus)
    lowReasoningContext    -- 200000
    highReasoningContext   -- 200000
    lowReasoningMaxOutput  -- 8192
    highReasoningMaxOutput -- 16384
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
                              autoRejectThreshold (0.98), grayZoneLow (0.88), haikuVerifyEnabled
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

### Config Loading

- `get_config()` caches globally -- use `reload_config()` to force refresh
- Re-entrancy guard: `load_config()` -> `get_db_path()` -> `get_config()` cycle broken by `_config_loading` boolean
- Env overrides: `MEMORY_DB_PATH`, `OLLAMA_URL`, `CLAWDBOT_WORKSPACE` (critical for tests and stress test isolation)
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

All three merge paths (dedup/contradiction/review) use the shared `_merge_nodes_into()` helper in `janitor.py`. This helper handles:
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
| 0 | backup | Keychain + core file backups | Free |
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
| 6 | (deprecated) | Edge extraction (now at capture time) | N/A |
| 7 | rag | Reindex docs for RAG + project discovery | Free (local) |
| 8 | tests | Run pytest suite | Free |
| 9 | cleanup | Prune old logs, orphaned embeddings | Free |

**Fail-fast:** If any memory task (2-5) fails, remaining memory tasks are SKIPPED and graduation is BLOCKED.

---

## Known Gotchas

These are hard-won lessons from 81+ bug fixes across 9 production rounds + 3 stress test rounds + 1 journal round + 6 benchmark analysis rounds. Read ALL of them before making changes.

### Model IDs
Short aliases only (`claude-opus-4-6`, `claude-haiku-4-5`). Dated IDs (e.g. `claude-opus-4-6-20260101`) return 404 errors.

### TS/JS Sync
`index.ts` is source of truth, `index.js` must match manually. Gateway loads `.js`, not `.ts`. Full restart required after plugin changes (SIGUSR1 does not reload TS).

### Config Gotcha
The `coreMarkdown.files` section has filename keys like `"SOUL.md"`. The snake_case conversion in `config.py` would corrupt these to `"s_o_u_l.md"`. Fix: use `raw_config` directly for that section.

### DB: update_node() vs add_node()
Use `update_node()` for existing nodes, `add_node()` only for new nodes (avoids CASCADE trigger issues).

### Edge Normalization
`_INVERSE_MAP` flips direction, `_SYNONYM_MAP` keeps same direction. `mother_of` is a SYNONYM, `child_of` is an INVERSE. Getting these confused breaks the graph.

### FTS Owner Gap
`search_fts()` does not filter by `owner_id`. This causes leakage at low similarity thresholds. Always apply owner filtering after FTS results.

### node.attributes
Always assign as a dict, never `json.dumps()`. Serialization happens inside `add_node()` / `update_node()`. Passing a JSON string will result in double-serialization.

### get_connection()
Is a `@contextmanager`. MUST use `with get_connection() as conn:`. Calling it without `with` will not properly manage the connection lifecycle.

### Resolution Summaries
After MERGE operations, use `status="archived"` on resolution summary nodes to prevent FTS recall leakage.

### _ollama_healthy() Cache
Has a 30-second cache. To force a re-check, clear `_ollama_healthy._cache`.

### Stress Test Worktree
Imports come from `CLAWDBOT_WORKSPACE` (worktree), not the prod repo. You must commit fixes before testing in the worktree, or they will not be picked up.

### Review Batch Truncation (FIXED)
`TokenBatchBuilder` now has an `output_tokens_per_item` param. The review task uses `models.max_output('high')` (16384) instead of `opusReview.maxTokens` (4000) to avoid output truncation.

### create-edge --create-missing
Without the `--create-missing` flag, the recovery wrapper silently fails on edges for entities not yet in the graph. Always include this flag when creating edges programmatically.

### Change Awareness Queries
Must split into `old_keywords` / `new_keywords`. A single new fact can satisfy `found_count >= 2` alone, causing false positives.

### Week-gated Queries
Evolution chain queries need a `required_week` field. Without it, queries for un-injected facts silently report as failures instead of being properly gated.

### recovery_wrapper.js storeFact
Returns a node ID (parsed from stdout), not a boolean. This is needed for `source_fact_id` edge linkage. Treating the return value as boolean will break edge association.

### Journal apply_distillation
Edits must flush to disk BEFORE additions. `insert_into_file()` re-reads from disk, and would overwrite in-memory edits that have not been flushed.

### Journal archive_entries()
Takes a list of dicts (with `date`, `trigger`, `content` keys), NOT a list of date strings.

### Journal TS Array Fallback
The LLM may return arrays instead of strings for `journal_entries` values. The code handles both, but new callers must be aware of this.

### Gateway Stale Process
`clawdbot gateway restart` does not reliably kill the old process. The reliable sequence is:
```bash
clawdbot gateway stop
pkill -f "openclaw-gateway"
# Wait for port 18789 to be free
clawdbot gateway start
clawdbot gateway install
```

### Dual System is Intentional
Snippets = fast path (continuous core markdown refinement). Journal = slow path (long-term reflective log). They are NOT replacing each other. Do not merge or remove either system.

### Merge Destructive Pattern (FIXED)
All 3 merge paths (dedup/contradiction/review) previously had 5 bugs: confidence reset to default, `confirmation_count` reset, wrong status, edges deleted instead of migrated, hardcoded owner. Now fixed with the shared `_merge_nodes_into()` helper. 19 tests cover: confidence inheritance, confirmation_count sum, edge migration, status="active", owner from originals.

### KNN Index Perturbation
Deleting/adding nodes from the sqlite-vec index reshuffles neighbor sets for ALL queries. 73% of dedup regressions were from this cascade effect, not direct merge replacement. Be cautious when deleting nodes from the vector index.

### update_node() Doesn't Update created_at
The UPDATE SQL in `update_node()` deliberately excludes `created_at`. If you need to set `created_at`, use `store(..., created_at=...)` instead.

### _switch_to_db()
MUST explicitly pass `db_path` to the `MemoryGraph()` constructor. The `DB_PATH` default parameter is captured at import time, so changing the environment variable alone does not work.

### Dedup Merge owner_id
`janitor.py` hardcodes the default `owner_id`. Benchmark reprocessing needs a post-janitor SQL fixup to correct the owner.

### Contradiction Detection Scope
Only checks `pending` / `approved` facts. The ingest pipeline stores facts as `active`, making contradiction detection a no-op for benchmarks unless status is adjusted.

### LongMemEval Judge Prompts
Must end with "Answer yes or no only." Uses the paper's exact format: "Correct Answer:" (capital A), "Model Response:" (capital R), "Rubric:" (not "Desired response rubric:"). Reference implementation at `xiaowu0162/LongMemEval/src/evaluation/evaluate_qa.py`.

### Journal Archive After Distillation
After a full janitor run, journal entries get distilled into core markdown then archived to `journal/archive/*.md`. The main `*.journal.md` file is left empty (just a header). For A/B tests, you must load `journal/archive/*.md` too -- otherwise "with journal" is identical to "standard" (both empty).

### Parallel Eval Output Collision
When running A/B evaluations in parallel, MUST use separate `--results-dir` values. Both write `evaluation_results.json` per conversation and `locomo_results.json`, silently overwriting each other.

### Python stdout Buffering
When running eval scripts in background, use `PYTHONUNBUFFERED=1` and `python3 -u`. Otherwise output is fully buffered and monitoring shows nothing until the process exits.

---

## CLI Reference

All commands run from the `plugins/quaid/` directory.

### Memory Operations

```bash
# Store a fact
python3 memory_graph.py store "Quaid prefers dark mode" --owner default --category preference

# Search memories (hybrid retrieval)
python3 memory_graph.py search "dark mode preferences" --owner default --limit 10

# Search memories + docs (combined)
python3 memory_graph.py search-all "dark mode"

# Full recall pipeline (with HyDE, reranking, multi-pass)
# (Used by the memory_recall tool, not typically called directly)

# Get database statistics
python3 memory_graph.py stats

# Get edges for a node
python3 memory_graph.py get-edges <node_id>
```

### Janitor Pipeline

```bash
# Full pipeline preview (no changes)
python3 janitor.py --task all --dry-run

# Run specific tasks
python3 janitor.py --task review --apply          # Opus review of pending facts
python3 janitor.py --task workspace               # Core markdown audit
python3 janitor.py --task snippets --apply         # FOLD/REWRITE/DISCARD snippet review
python3 janitor.py --task snippets --dry-run       # Preview snippet review
python3 janitor.py --task journal --apply          # Journal distillation
python3 janitor.py --task journal --apply --force-distill  # Force distillation
python3 janitor.py --task embeddings               # Backfill missing embeddings
python3 janitor.py --task decay --apply            # Run Ebbinghaus decay
python3 janitor.py --task duplicates --apply       # Dedup pass
python3 janitor.py --task tests                    # Run pytest suite
python3 janitor.py --task cleanup                  # Prune old logs
```

### Documentation System

```bash
# Check doc staleness (free, no LLM calls)
python3 docs_updater.py check

# View update history
python3 docs_updater.py changelog

# Fix stale docs (Opus calls)
python3 docs_updater.py update-stale --apply

# RAG search (free, local embeddings only)
python3 docs_rag.py search "query text"
```

### Projects System

```bash
# List registered docs for a project
python3 docs_registry.py list --project quaid

# Find which project a file belongs to
python3 docs_registry.py find-project <file_path>

# Create a new project
python3 docs_registry.py create-project <name> --label "Human Name"

# Auto-discover docs for a project
python3 docs_registry.py discover --project <name>

# Garbage collect orphaned entries
python3 docs_registry.py gc
```

### Workspace Audit

```bash
# Check line counts vs limits
python3 workspace_audit.py --bloat

# Check only changed files
python3 workspace_audit.py --check-only
```

### Testing

```bash
# Run all pytest tests
python3 -m pytest tests/ -v

# Run specific test file
python3 -m pytest tests/test_memory_graph.py -v

# Run specific test
python3 -m pytest tests/test_invariants.py::test_name -v

# Manual recall test harness
python3 test_recall.py
python3 test_recall.py --query "custom query" --verbose
```

---

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLAWDBOT_WORKSPACE` | Workspace root directory | Auto-detected from gateway |
| `MEMORY_DB_PATH` | Override database file path | `<workspace>/data/memory.db` |
| `OLLAMA_URL` | Ollama server URL | `http://localhost:11434` |
| `ANTHROPIC_API_KEY` | Anthropic API key for LLM calls | Loaded from `.env` file or macOS Keychain |
| `OPENAI_API_KEY` | OpenAI API key (for benchmark judging) | Must be set explicitly |
| `OPENROUTER_API_KEY` | OpenRouter API key (alternative LLM routing) | Must be set explicitly |
| `QUAID_DEV` | Enable dev mode (unit tests in janitor, verbose output) | Not set |
| `QUAID_QUIET` | Suppress informational config messages | Not set |
| `MOCK_EMBEDDINGS` | Use deterministic fake embeddings (for testing) | Not set |

API key fallback chain: `ANTHROPIC_API_KEY` env var -> `.env` file -> macOS Keychain.

---

## Test Suite

### Overview

- ~1000 pytest tests + 163 vitest tests = ~1160+ total tests
- All tests passing as of Feb 2026

### Test Files (pytest)

| File | Coverage Area |
|------|---------------|
| `test_store_recall.py` | Store and recall pipeline |
| `test_search.py` | Search functionality |
| `test_memory_graph.py` | (if exists) Core graph operations |
| `test_janitor.py` | (if exists) Janitor pipeline |
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
| `test_coverage_gaps.py` | Coverage gap identification |
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

| Configuration | Accuracy | Notes |
|---------------|----------|-------|
| Quaid + Opus | 75.00% | Production config |
| Quaid + Journal + Haiku | 74.48% +/- 0.05 | Best Haiku result, nearly matches Opus at ~46% cost |
| Quaid + Haiku | 70.28% | Standard Haiku |
| v2 Standard (full janitor) | 69.11% +/- 0.17 | Regression from dedup merge bug (since fixed) |
| Mem0 (graphRAG) | 68.9% | Apr 2025 numbers |
| Mem0 | 66.9% | Apr 2025 numbers |
| Zep | 66.0% | Apr 2025 numbers |
| LangMem | 58.1% | Apr 2025 numbers |
| OpenAI | 52.9% | Apr 2025 numbers |
| Full-context Haiku | 79.59% +/- 0.17 | Upper bound (no memory system) |
| Full-context Opus | 86.62% +/- 0.09 | Upper bound (no memory system) |

**Key insight:** Journal + Haiku (74.5%) nearly matches v1 Opus (75.0%) at roughly 46% of the cost. Journal helps most on temporal questions (+7.6pp) and single-hop questions (+6.8pp).

**Benchmark code:** `memory-stress-test/runner/locomo/`
**Full results:** `memory-stress-test/runner/locomo/RESULTS.md`

**LongMemEval** (ICLR 2025): Implemented at `memory-stress-test/runner/longmemeval/`. 500 QA pairs, 7 types, 19,195 unique sessions. Smoke test: 80% on 5 entries. Full run: ~$60 (Haiku), not yet completed.

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

Edit `memory_graph.py`, find the argparse section at the bottom:
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

1. Add a function in `janitor.py` following the pattern of existing tasks
2. Register it in `run_task_optimized()` task dispatch
3. Decide its position in the pipeline ordering
4. Add `--task <name>` handling to the CLI argument parser
5. Write tests

### Running the Full Pipeline

```bash
# Preview (no changes, no LLM calls for most tasks)
python3 janitor.py --task all --dry-run

# Apply all tasks (costs ~$0.10-0.50 depending on pending work)
python3 janitor.py --task all --apply
```

---

## Project History

- Started as inline memory extraction in an OpenClaw plugin
- Evolved: sqlite-vec vectors, FTS5, graph traversal, janitor pipeline
- 81+ bugs fixed across 9 production rounds + 3 stress test rounds + 1 journal round + 6 benchmark analysis rounds
- Key architectural milestones: crash-safe merges, dual learning system, RRF fusion, BEAM graph search, prompt caching, output-aware batching
- ~37K lines of code: 17K Python, 12K tests, 5K TS/JS, 3.1K vitest

---

## Safety Rules

- Do not exfiltrate private data
- Do not run destructive commands without asking
- `trash` is preferred over `rm` -- always recoverable
- Wrap plugins in error handlers -- do not crash the gateway
- Test changes before deploying
- Backup before risky changes: `scripts/backup-core.sh`

---

## Further Documentation

| Topic | Primary Doc |
|-------|-------------|
| Memory architecture | `projects/quaid/memory-system-design.md` |
| Plugin implementation | `projects/quaid/memory-local-implementation.md` |
| Janitor pipeline | `projects/quaid/janitor-reference.md` |
| Database schema | `projects/quaid/memory-schema.md` |
| Deduplication | `projects/quaid/memory-deduplication-system.md` |
| Model routing | `projects/infrastructure/model-strategy.md` |
| Claude Code hooks | `projects/infrastructure/claude-code-integration.md` |
| Project onboarding | `plugins/quaid/prompts/project_onboarding.md` |

**RAG search for docs:** `python3 docs_rag.py search "query"`
