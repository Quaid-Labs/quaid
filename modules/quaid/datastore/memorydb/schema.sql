-- Local Graph Memory System Schema
-- SQLite with JSON support for flexible attributes

-- Nodes table - stores all memory entities
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,                    -- UUID
    type TEXT NOT NULL,                     -- Person, Place, Project, Event, Fact, Preference, Concept
    name TEXT NOT NULL,                     -- Display name / main content
    attributes TEXT DEFAULT '{}',           -- JSON blob for type-specific data
    embedding BLOB,                         -- float32 array, dim from config (ollama.embeddingDim)
    
    -- Verification and quality
    verified INTEGER DEFAULT 0,             -- 0=auto-extracted, 1=user confirmed (slower decay)
    pinned INTEGER DEFAULT 0,               -- 1=core facts that never decay
    confidence REAL DEFAULT 0.5,            -- 0-1 confidence score
    source TEXT,                            -- Where this came from (file, message, etc.)
    source_id TEXT,                         -- Message ID or file path
    
    -- Multi-user support
    owner_id TEXT,                          -- Who owns this memory (null = shared)
    actor_id TEXT,                          -- Canonical entity that asserted/performed this memory event
    subject_entity_id TEXT,                 -- Canonical entity this memory is primarily about

    -- Privacy tiers
    privacy TEXT DEFAULT 'shared' CHECK(privacy IN ('private', 'shared', 'public')),

    -- Session tracking
    session_id TEXT,                        -- Session where this memory was created (for dedup)
    source_channel TEXT,                    -- Channel/source type (telegram/discord/slack/dm/etc.)
    source_conversation_id TEXT,            -- Stable conversation/thread/group identifier
    source_author_id TEXT,                  -- External speaker/author handle or ID

    -- Classification
    fact_type TEXT DEFAULT 'unknown',       -- Subcategory (e.g. financial, health, family)
    knowledge_type TEXT DEFAULT 'fact' CHECK(knowledge_type IN ('fact', 'belief', 'preference', 'experience')),
    extraction_confidence REAL DEFAULT 0.5, -- 0-1: how confident the classifier was
    speaker TEXT,                           -- Who stated this fact (e.g. "Alice", "Bob")

    -- Temporal validity
    valid_from TEXT,                        -- ISO8601 datetime
    valid_until TEXT,                       -- ISO8601 datetime (null = still valid)

    -- Content integrity
    content_hash TEXT,                      -- SHA256 of name text (fast exact-dedup pre-filter)
    superseded_by TEXT,                     -- ID of node that replaced this one (fact versioning)
    keywords TEXT,                          -- Space-separated derived search terms (generated at extraction)

    -- Lifecycle
    status TEXT DEFAULT 'approved',         -- pending/active/approved
    deleted_at TEXT DEFAULT NULL,           -- Unused placeholder column
    deletion_reason TEXT DEFAULT NULL,      -- Unused placeholder column

    -- Metadata
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    accessed_at TEXT DEFAULT (datetime('now')),
    access_count INTEGER DEFAULT 0,
    storage_strength REAL DEFAULT 0.0,       -- Bjork: cumulative encoding strength (never decreases)
    confirmation_count INTEGER DEFAULT 0,    -- How many times re-confirmed by extraction
    last_confirmed_at TEXT                   -- When last confirmed
);

-- Edges table - relationships between nodes
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,                    -- UUID
    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,                 -- lives_at, works_on, knows, owns, prefers, etc.
    attributes TEXT DEFAULT '{}',           -- JSON for relationship metadata
    weight REAL DEFAULT 1.0,                -- Relationship strength
    source_fact_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,  -- Fact that created this edge

    -- Temporal
    valid_from TEXT,
    valid_until TEXT,

    -- Metadata
    created_at TEXT DEFAULT (datetime('now')),

    UNIQUE(source_id, target_id, relation)
);

-- Full-text search index on node names/content + derived keywords
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name,
    keywords,
    content='nodes',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, name, keywords) VALUES (new.rowid, new.name, new.keywords);
END;

CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, keywords) VALUES('delete', old.rowid, old.name, old.keywords);
END;

CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, keywords) VALUES('delete', old.rowid, old.name, old.keywords);
    INSERT INTO nodes_fts(rowid, name, keywords) VALUES (new.rowid, new.name, new.keywords);
END;

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_privacy ON nodes(privacy);
CREATE INDEX IF NOT EXISTS idx_nodes_verified ON nodes(verified);
CREATE INDEX IF NOT EXISTS idx_nodes_pinned ON nodes(pinned);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source);
CREATE INDEX IF NOT EXISTS idx_nodes_owner ON nodes(owner_id);
CREATE INDEX IF NOT EXISTS idx_nodes_owner_status ON nodes(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_nodes_actor ON nodes(actor_id);
CREATE INDEX IF NOT EXISTS idx_nodes_subject_entity ON nodes(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_nodes_session ON nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_nodes_source_conversation ON nodes(source_conversation_id);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_accessed ON nodes(accessed_at);
CREATE INDEX IF NOT EXISTS idx_nodes_confidence ON nodes(confidence);
CREATE INDEX IF NOT EXISTS idx_nodes_content_hash ON nodes(content_hash);
CREATE INDEX IF NOT EXISTS idx_nodes_created ON nodes(created_at);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation);
CREATE INDEX IF NOT EXISTS idx_edges_source_fact ON edges(source_fact_id);

-- Contradictions table - detected conflicting facts
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

CREATE INDEX IF NOT EXISTS idx_contradictions_status ON contradictions(status);

-- Dedup log - tracks all dedup rejections for review
CREATE TABLE IF NOT EXISTS dedup_log (
    id TEXT PRIMARY KEY,
    new_text TEXT NOT NULL,
    existing_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
    existing_text TEXT NOT NULL,
    similarity REAL NOT NULL,
    decision TEXT NOT NULL,  -- auto_reject, llm_reject, llm_accept, fallback_reject
    llm_reasoning TEXT,
    review_status TEXT DEFAULT 'unreviewed'
        CHECK(review_status IN ('unreviewed', 'confirmed', 'reversed')),
    review_resolution TEXT,
    reviewed_at TEXT,
    owner_id TEXT,
    source TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dedup_log_review ON dedup_log(review_status);

-- Decay review queue - memories queued for review instead of silent deletion
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
CREATE INDEX IF NOT EXISTS idx_decay_queue_status ON decay_review_queue(status);

-- Metadata table for system state
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Edge keywords table - maps edge types to trigger keywords for graph expansion
-- Keywords are generated by LLM when new edge types are discovered
CREATE TABLE IF NOT EXISTS edge_keywords (
    relation TEXT PRIMARY KEY,
    keywords TEXT NOT NULL,  -- JSON array of trigger keywords
    description TEXT,        -- Human-readable description of this relation
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Embedding cache - avoids re-computing embeddings for identical text
CREATE TABLE IF NOT EXISTS embedding_cache (
    text_hash TEXT PRIMARY KEY,             -- SHA256 of input text
    embedding BLOB NOT NULL,                -- Packed float32 array
    model TEXT NOT NULL,                    -- Embedding model used (from config: ollama.embeddingModel)
    created_at TEXT DEFAULT (datetime('now'))
);

-- Entity aliases for fuzzy name matching
CREATE TABLE IF NOT EXISTS entity_aliases (
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,           -- The alternate name (e.g., "Sol", "Mom")
    canonical_name TEXT NOT NULL,  -- The canonical name (e.g., "Alice Smith")
    canonical_node_id TEXT,        -- Optional: link to the Person/entity node
    owner_id TEXT,                 -- Owner who defined this alias
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(alias, owner_id)
);
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON entity_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON entity_aliases(canonical_name);

-- Identity handles map (forward-looking multi-user/group-chat support)
-- Maps source-specific handles/usernames to canonical entity IDs.
CREATE TABLE IF NOT EXISTS identity_handles (
    id TEXT PRIMARY KEY,
    owner_id TEXT,                     -- Tenant/user namespace
    source_channel TEXT NOT NULL,      -- telegram/discord/slack/dm/etc.
    conversation_id TEXT,              -- Optional group/thread scope
    handle TEXT NOT NULL,              -- Raw handle seen in source context
    canonical_entity_id TEXT NOT NULL, -- Canonical entity/person ID
    confidence REAL DEFAULT 1.0,       -- Mapping confidence score
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(owner_id, source_channel, conversation_id, handle)
);
CREATE INDEX IF NOT EXISTS idx_identity_handles_lookup
    ON identity_handles(owner_id, source_channel, conversation_id, handle);
CREATE INDEX IF NOT EXISTS idx_identity_handles_entity
    ON identity_handles(canonical_entity_id);

-- Doc registry - managed by docs_registry.py ensure_table()
-- Table definition lives in docs_registry.py to avoid schema drift.
-- See docs_registry.py DocsRegistry.ensure_table() for the canonical DDL.

-- Recall log - tracks every recall() call for observability
CREATE TABLE IF NOT EXISTS recall_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    owner_id TEXT,
    intent TEXT,                          -- WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION
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
CREATE INDEX IF NOT EXISTS idx_recall_log_created ON recall_log(created_at);

-- Health snapshots - periodic DB health metrics (written by janitor)
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

-- Documentation update audit log (replaces JSON changelogs for concurrent safety)
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

-- Initialize metadata
INSERT OR IGNORE INTO metadata (key, value) VALUES
    ('schema_version', '4'),
    ('embedding_model', 'qwen3-embedding:8b'),
    ('embedding_dim', '4096'),
    ('last_seed', NULL);
