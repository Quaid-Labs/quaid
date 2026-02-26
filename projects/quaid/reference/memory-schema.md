# Database Schema
<!-- PURPOSE: Database schema: nodes, edges, FTS5, indexes, doc_registry -->
<!-- SOURCES: schema.sql -->

> Auto-generated from `modules/quaid/schema.sql` — 2026-02-07
> Schema version: 3 | Embedding model: qwen3-embedding:8b (4096-dim)

## Nodes — All Memory Entities

```sql
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,                    -- UUID
    type TEXT NOT NULL,                     -- Person, Place, Project, Event, Fact, Preference, Concept
    name TEXT NOT NULL,                     -- Display name / main content
    attributes TEXT DEFAULT '{}',           -- JSON blob for type-specific data
    embedding BLOB,                         -- 4096-dim float32 array (qwen3-embedding:8b)

    -- Verification and quality
    verified INTEGER DEFAULT 0,             -- 0=auto-extracted, 1=user confirmed (slower decay)
    pinned INTEGER DEFAULT 0,              -- 1=core facts that never decay
    confidence REAL DEFAULT 0.5,            -- 0-1 confidence score
    source TEXT,                            -- Where this came from (file, message, etc.)
    source_id TEXT,                         -- Message ID or file path

    -- Multi-user support
    owner_id TEXT,                          -- Who owns this memory (null = shared/legacy)

    -- Privacy tiers
    privacy TEXT DEFAULT 'shared' CHECK(privacy IN ('private', 'shared', 'public')),

    -- Session tracking
    session_id TEXT,                        -- Session where this memory was created (for dedup)

    -- Classification
    fact_type TEXT DEFAULT 'unknown',       -- Subcategory (e.g. financial, health, family)
    knowledge_type TEXT DEFAULT 'fact' CHECK(knowledge_type IN ('fact', 'belief', 'preference', 'experience')),
    extraction_confidence REAL DEFAULT 0.5, -- 0-1: how confident the classifier was
    speaker TEXT,                           -- Who stated this fact (e.g. "Quaid", "Hauser")

    -- Temporal validity
    valid_from TEXT,                        -- ISO8601 datetime
    valid_until TEXT,                       -- ISO8601 datetime (null = still valid)

    -- Content integrity
    content_hash TEXT,                      -- SHA256 of name text (fast exact-dedup pre-filter)
    superseded_by TEXT,                     -- ID of node that replaced this one (fact versioning)
    keywords TEXT,                          -- Space-separated derived search terms (generated at extraction)

    -- Lifecycle
    status TEXT DEFAULT 'approved',         -- pending → approved → active
    deleted_at TEXT DEFAULT NULL,           -- Legacy (unused, kept for migration compat)
    deletion_reason TEXT DEFAULT NULL,      -- Legacy (unused, kept for migration compat)

    -- Metadata
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    accessed_at TEXT DEFAULT (datetime('now')),
    access_count INTEGER DEFAULT 0,
    storage_strength REAL DEFAULT 0.0,       -- Bjork: cumulative encoding strength (never decreases, caps at 10.0)
    confirmation_count INTEGER DEFAULT 0,    -- How many times re-confirmed by extraction
    last_confirmed_at TEXT                   -- When last confirmed
);
```

**Key columns:**
- `pinned=1` — core facts immune to decay
- `status` — lifecycle: `pending` → `approved` (via janitor review) → `active` (via graduation)
- `privacy` — defaults to `'shared'`, NOT `'private'`
- `owner_id` — all facts should have an owner; null is legacy data
- `session_id` — used for dedup (skip facts from same session)
- `knowledge_type` — epistemological classification: `fact`, `belief`, `preference`, or `experience`; defaults to `'fact'`
- `content_hash` — SHA-256 of normalized content; enables O(1) exact-match dedup before embedding comparison
- `superseded_by` — fact versioning; points to the node that replaced this one
- `confirmation_count` — incremented each time extraction re-confirms this fact; used to boost confidence
- `last_confirmed_at` — timestamp of most recent re-confirmation; useful for staleness checks
- `keywords` — space-separated derived search terms generated at extraction time; indexed by FTS5 for vocabulary bridging (e.g., "health stomach gastric" for a fact about digestive symptoms)

**Attributes JSON blob** (`attributes` column) may contain:
- `source_type` — `"user"` / `"assistant"` / `"tool"` / `"import"` — indicates how the fact was obtained. Assistant-inferred facts get a 0.9x confidence multiplier at store time. Used by janitor review for differential trust policies.

**Special node types:**
- Standard: `Person`, `Place`, `Project`, `Event`, `Fact`, `Preference`, `Concept`, `Organization`
- `resolution_summary` — Created by the janitor MERGE operation. Contains consolidated text from two or more merged facts. Linked to the originals via `resolved_from` edges for provenance.

**Special edge relations:**
- `resolved_from` — Links a `resolution_summary` node back to the original facts that were merged to create it. Direction: `resolution_summary --resolved_from--> original_fact`.

## Edges — Relationships Between Nodes

```sql
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,                    -- UUID
    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,                 -- lives_at, works_on, knows, owns, prefers, etc.
    attributes TEXT DEFAULT '{}',           -- JSON for relationship metadata
    weight REAL DEFAULT 1.0,               -- Relationship strength
    source_fact_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,  -- Fact that created this edge

    -- Temporal
    valid_from TEXT,
    valid_until TEXT,

    -- Metadata
    created_at TEXT DEFAULT (datetime('now')),

    UNIQUE(source_id, target_id, relation)
);
```

**Key constraints:**
- `ON DELETE CASCADE` on source_id/target_id — deleting a node auto-deletes its edges
- `ON DELETE SET NULL` on source_fact_id — deleting source fact preserves edge but clears link
- `UNIQUE(source_id, target_id, relation)` — one edge per direction per relation type; use INSERT OR REPLACE

**Edge normalization** (enforced by janitor):
- Inverse map: `child_of` → `parent_of`, `led_to`/`caused`/`resulted_in`/`triggered` → `caused_by` (FLIP direction)
- Synonym map: `mother_of` → `parent_of`, `because_of`/`due_to` → `caused_by` (SAME direction)
- Symmetric relations: alphabetical ordering (e.g. `sibling_of(A,B)` where A < B)

## FTS5 — Full-Text Search

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name,
    keywords,
    content='nodes',
    content_rowid='rowid',
    tokenize='porter unicode61'
);
```

**Sync triggers** keep FTS in sync with nodes (both `name` and `keywords` columns):
```sql
CREATE TRIGGER nodes_ai AFTER INSERT ON nodes ...   -- INSERT INTO nodes_fts(rowid, name, keywords)
CREATE TRIGGER nodes_ad AFTER DELETE ON nodes ...    -- DELETE via rowid
CREATE TRIGGER nodes_au AFTER UPDATE ON nodes ...    -- DELETE old + INSERT new (name, keywords)
```

The `keywords` column enables vocabulary bridging — FTS queries can match derived terms not present in the original fact text (e.g., searching "health problems" can match a fact with keywords "health stomach gastric medical").

Used by `search_fts()` for keyword matching. **Does NOT filter by `owner_id`** — caller must post-filter.

## Embedding Cache — Cached Embedding Vectors

```sql
CREATE TABLE IF NOT EXISTS embedding_cache (
    text_hash TEXT PRIMARY KEY,             -- SHA-256 of the input text
    embedding BLOB NOT NULL,                -- Cached embedding vector (float32 array)
    model TEXT NOT NULL,                    -- Model used to generate embedding
    created_at TEXT DEFAULT (datetime('now'))
);
```

Avoids redundant embedding API calls for identical text. Keyed on `text_hash` (SHA-256 of raw input text) + `model` for cache invalidation on model changes.

## Entity Aliases — Fuzzy Name Matching

```sql
CREATE TABLE IF NOT EXISTS entity_aliases (
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,           -- The alternate name (e.g., "Sol", "Mom")
    canonical_name TEXT NOT NULL,  -- The canonical name (e.g., "Douglas Quaid")
    canonical_node_id TEXT,        -- Optional: link to the Person/entity node
    owner_id TEXT,                 -- Owner who defined this alias
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(alias, owner_id)
);
```

Maps alternate names and nicknames to canonical entity names. `canonical_node_id` optionally links to the corresponding Person/entity node. Scoped per `owner_id` — the same alias can map to different canonical names for different users.

## Contradictions — Detected Conflicting Facts

```sql
CREATE TABLE IF NOT EXISTS contradictions (
    id TEXT PRIMARY KEY,
    node_a_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    node_b_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    explanation TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'resolved', 'false_positive')),
    resolution TEXT,           -- 'keep_a', 'keep_b', 'keep_both', 'merge'
    resolution_reason TEXT,
    detected_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    UNIQUE(node_a_id, node_b_id)
);
```

## Dedup Log — Tracks Dedup Decisions for Review

```sql
CREATE TABLE IF NOT EXISTS dedup_log (
    id TEXT PRIMARY KEY,
    new_text TEXT NOT NULL,
    existing_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
    existing_text TEXT NOT NULL,
    similarity REAL NOT NULL,
    decision TEXT NOT NULL,  -- auto_reject, haiku_reject, haiku_accept, fallback_reject
    haiku_reasoning TEXT,
    review_status TEXT DEFAULT 'unreviewed'
        CHECK(review_status IN ('unreviewed', 'confirmed', 'reversed')),
    review_resolution TEXT,
    reviewed_at TEXT,
    owner_id TEXT,
    source TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

**Note:** Column is `existing_node_id`, NOT `kept_id` or `removed_id`.

## Decay Review Queue — Low-Confidence Facts Queued for Review

```sql
CREATE TABLE IF NOT EXISTS decay_review_queue (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    node_text TEXT NOT NULL,
    node_type TEXT,
    confidence_at_queue REAL NOT NULL,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    verified INTEGER DEFAULT 0,
    created_at_node TEXT,
    decision TEXT,  -- null (pending), delete, extend, pin
    decision_reason TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'reviewed')),
    queued_at TEXT DEFAULT (datetime('now')),
    reviewed_at TEXT
);
```

## Recall Log — Recall Observability

```sql
CREATE TABLE IF NOT EXISTS recall_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    owner_id TEXT,
    intent TEXT,                          -- WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY
    results_count INTEGER,
    avg_similarity REAL,
    top_similarity REAL,
    multi_pass_triggered INTEGER DEFAULT 0,
    fts_fallback_used INTEGER DEFAULT 0,
    reranker_used INTEGER DEFAULT 0,
    reranker_changes INTEGER DEFAULT 0,   -- How many results changed position
    reranker_top1_changed INTEGER DEFAULT 0,  -- Whether #1 result changed after reranking
    reranker_avg_displacement REAL,           -- Average absolute rank shift
    graph_discoveries INTEGER DEFAULT 0,
    latency_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
```

Tracks every `recall()` call for observability. Used by the dashboard and janitor cleanup (Task 9 prunes entries older than 90 days).

## Health Snapshots — Periodic DB Health Metrics

```sql
CREATE TABLE IF NOT EXISTS health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_nodes INTEGER,
    total_edges INTEGER,
    avg_confidence REAL,
    nodes_by_status TEXT,          -- JSON
    confidence_distribution TEXT,  -- JSON (buckets: 0.0-0.3, 0.3-0.5, 0.5-0.7, 0.7-0.9, 0.9-1.0)
    staleness_distribution TEXT,   -- JSON (0-7d, 7-30d, 30-90d, 90d+)
    orphan_count INTEGER,
    embedding_coverage REAL,      -- percentage
    created_at TEXT DEFAULT (datetime('now'))
);
```

Stores periodic snapshots of knowledge base health indicators. Written by the janitor after each run. Tracks node counts, edge counts, average confidence, status distribution, staleness distribution, orphan counts, and embedding coverage. Pruned by Task 9 (entries older than 180 days).

## Metadata — System State

```sql
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Seeded values:
-- schema_version = '3'
-- embedding_model = 'qwen3-embedding:8b'
-- embedding_dim = '4096'
```

## Janitor Metadata — Janitor System State

Created by `janitor.py` `init_janitor_metadata()` — NOT in schema.sql.

```sql
CREATE TABLE IF NOT EXISTS janitor_metadata (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Used by the janitor to persist key-value state across runs (e.g. last workspace check hash).

## Janitor Runs — Janitor Execution History

Created by `janitor.py` `init_janitor_metadata()` — NOT in schema.sql.

```sql
CREATE TABLE IF NOT EXISTS janitor_runs (
    id INTEGER PRIMARY KEY,
    task_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    memories_processed INTEGER DEFAULT 0,
    actions_taken INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'        -- running, completed, failed
);
```

Tracks each janitor task execution. Used for incremental mode — `get_last_task_run()` queries `MAX(completed_at)` for a given task to determine what's changed since the last run.

## Edge Keywords — Graph Expansion Triggers

```sql
CREATE TABLE IF NOT EXISTS edge_keywords (
    relation TEXT PRIMARY KEY,
    keywords TEXT NOT NULL,  -- JSON array of trigger keywords
    description TEXT,        -- Human-readable description of this relation
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

LLM-generated keywords map edge types to natural language triggers. Used by `should_expand_graph()` to decide when recall needs graph traversal.

## Doc Registry — Project & Document Tracking

Managed by `docs_registry.py` `ensure_table()` — NOT in schema.sql.

```sql
CREATE TABLE IF NOT EXISTS doc_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL DEFAULT 'default',
    asset_type TEXT NOT NULL DEFAULT 'doc',
    title TEXT,
    description TEXT,
    tags TEXT DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'active',
    auto_update INTEGER DEFAULT 0,
    source_files TEXT,                    -- JSON array: files this doc tracks
    last_indexed_at TEXT,
    last_modified_at TEXT,
    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
    registered_by TEXT DEFAULT 'system'
);
```

In-directory files auto-belong to their project via `homeDir` matching. Registry entries are for: external files (fast path→project lookup), docs with `source_files` tracking (mtime staleness), and files needing explicit metadata.

## Project Definitions — Project Configuration (DB)

Managed by `docs_registry.py` `ensure_table()` — NOT in schema.sql. Source of truth for project definitions (migrated from JSON config).

```sql
CREATE TABLE IF NOT EXISTS project_definitions (
    name TEXT PRIMARY KEY,                    -- Kebab-case identifier (e.g., "quaid")
    label TEXT NOT NULL,                      -- Human-readable name
    home_dir TEXT NOT NULL,                   -- Workspace-relative path
    source_roots TEXT DEFAULT '[]',           -- JSON array of paths
    auto_index INTEGER DEFAULT 1,
    patterns TEXT DEFAULT '["*.md"]',         -- JSON array of glob patterns
    exclude TEXT DEFAULT '["*.db","*.log","*.pyc","__pycache__/"]',  -- JSON array
    description TEXT DEFAULT '',
    state TEXT DEFAULT 'active' CHECK(state IN ('active', 'archived', 'deleted')),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

Seeded from `config/memory.json → projects.definitions` on first run (if table is empty). After seeding, the DB is the source of truth — JSON is ignored. `config.py` loads definitions from this table with a JSON fallback for fresh installs.

## Doc Update Log — Documentation Update Audit Trail

```sql
CREATE TABLE IF NOT EXISTS doc_update_log (
    id INTEGER PRIMARY KEY,
    doc_path TEXT NOT NULL,
    source_files TEXT,              -- JSON array of source paths
    staleness_score REAL,
    change_summary TEXT,
    commit_hash TEXT,               -- git commit that fixed it
    agent_id TEXT DEFAULT 'unknown',
    timestamp TEXT DEFAULT (datetime('now'))
);
```

Replaces the old `data/docs-update-log.json` file for concurrent safety and queryability. Records each doc auto-update with the triggering source files, staleness score, and a summary of what changed. Used by `detect_drift_from_git()` in Task 1b to track drift between source files and their corresponding documentation.

## Indexes

```sql
-- Nodes
CREATE INDEX idx_nodes_type ON nodes(type);
CREATE INDEX idx_nodes_privacy ON nodes(privacy);
CREATE INDEX idx_nodes_verified ON nodes(verified);
CREATE INDEX idx_nodes_pinned ON nodes(pinned);
CREATE INDEX idx_nodes_source ON nodes(source);
CREATE INDEX idx_nodes_owner ON nodes(owner_id);
CREATE INDEX idx_nodes_owner_status ON nodes(owner_id, status);
CREATE INDEX idx_nodes_session ON nodes(session_id);
CREATE INDEX idx_nodes_status ON nodes(status);
CREATE INDEX idx_nodes_accessed ON nodes(accessed_at);
CREATE INDEX idx_nodes_confidence ON nodes(confidence);
CREATE INDEX idx_nodes_content_hash ON nodes(content_hash);
CREATE INDEX idx_nodes_created ON nodes(created_at);

-- Edges
CREATE INDEX idx_edges_source ON edges(source_id);
CREATE INDEX idx_edges_target ON edges(target_id);
CREATE INDEX idx_edges_relation ON edges(relation);
CREATE INDEX idx_edges_source_fact ON edges(source_fact_id);

-- Embedding cache
CREATE INDEX idx_embedding_cache_model ON embedding_cache(model);

-- Entity aliases
CREATE INDEX idx_aliases_alias ON entity_aliases(alias);
CREATE INDEX idx_aliases_canonical ON entity_aliases(canonical_name);

-- Support tables
CREATE INDEX idx_contradictions_status ON contradictions(status);
CREATE INDEX idx_dedup_log_review ON dedup_log(review_status);
CREATE INDEX idx_decay_queue_status ON decay_review_queue(status);
CREATE INDEX idx_recall_log_created ON recall_log(created_at);

-- Doc registry (managed by ensure_table())
CREATE INDEX idx_doc_registry_project ON doc_registry(project);
CREATE INDEX idx_doc_registry_state ON doc_registry(state);
```

## PRAGMA Settings

Set in `lib/database.get_connection()`:
- `PRAGMA foreign_keys = ON` — enables cascade deletes
- `PRAGMA busy_timeout = 30000` — 30s wait for concurrent access
- `PRAGMA journal_mode = WAL` — write-ahead logging for concurrent reads
- `PRAGMA synchronous = NORMAL` — safe with WAL, faster than FULL
- `PRAGMA cache_size = -64000` — 64MB page cache
- `PRAGMA temp_store = MEMORY` — temp tables in memory
- Row factory: `sqlite3.Row` for named column access