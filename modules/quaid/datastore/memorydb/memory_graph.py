#!/usr/bin/env python3
"""
Local Graph Memory System
SQLite + Ollama embeddings for fully local memory storage.
"""

# Public API — everything else is internal (tests may import _ prefixed functions)
__all__ = [
    # Data types
    "MemoryGraph", "Node", "Edge",
    # Core operations (CLI / plugin entry points)
    "store", "recall", "create_edge", "get_graph", "initialize_db",
    # Graph management
    "hard_delete_node", "soft_delete", "forget", "get_memory",
    # Contradiction pipeline
    "store_contradiction", "get_pending_contradictions",
    "resolve_contradiction", "mark_contradiction_false_positive",
    # Dedup pipeline
    "get_recent_dedup_rejections", "resolve_dedup_review",
    "log_dedup_decision", "content_hash",
    # Decay pipeline
    "queue_for_decay_review", "get_pending_decay_reviews",
    "resolve_decay_review", "decay_memories",
    # Edge keywords
    "ensure_keywords_for_relation", "get_edge_keywords", "store_edge_keywords",
    "delete_edges_by_source_fact", "seed_edge_keywords_from_db",
    "generate_keywords_for_relation", "get_all_edge_keywords_flat",
    "invalidate_edge_keywords_cache",
    # Entity summaries
    "get_entity_summary", "generate_entity_summary", "summarize_all_entities",
    # Query utilities
    "classify_intent", "has_owner_pronoun", "resolve_owner_person",
    "graph_aware_recall", "route_query", "extract_entities_from_text",
    "should_expand_graph",
]

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import urllib.request
import urllib.error
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from lib.config import get_db_path, get_ollama_url
from lib.database import get_connection as _lib_get_connection, has_vec as _lib_has_vec
from lib.embeddings import (
    get_embedding as _lib_get_embedding,
    pack_embedding as _lib_pack_embedding,
    unpack_embedding as _lib_unpack_embedding,
)
from lib.worker_pool import run_callables
from lib.similarity import cosine_similarity as _lib_cosine_similarity
from lib.tokens import (
    extract_key_tokens as _lib_extract_key_tokens,
    texts_are_near_identical,
    STOPWORDS as _LIB_STOPWORDS,
)
from lib.runtime_context import get_workspace_dir, get_adapter_instance

logger = logging.getLogger(__name__)

# Prompt injection blocklist — defense-in-depth for stored facts
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'ignore\s+(previous|earlier|all|these|my)\s+\w*\s*(instructions|prompts?)',
        r'system\s+prompt',
        r'\bAPI\s+key\b',
        r'\bpassword\b(?!\s+manager)',
        r'forget\s+(everything|all|previous)',
        r'\byou\s+(are|must|should|will)\s+(now|always)\b',
        r'\b(override|bypass|jailbreak)\b',
        r'\b(secret\s+key|access\s+token|bearer\s+token)\b',
    ]
]

_LOW_INFO_ENTITY_CATEGORIES = {"person", "place", "entity", "concept", "event", "organization", "pet"}
_LOW_INFO_ENTITY_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z0-9'_-]*(?:\s+[A-Za-z][A-Za-z0-9'_-]*)?")

# Optional imports for LLM-verified dedup (graceful degradation if unavailable)
try:
    from lib.llm_clients import call_fast_reasoning, parse_json_response
    _HAS_LLM_CLIENTS = True
except ImportError:
    _HAS_LLM_CLIENTS = False

try:
    from config import get_config as _get_memory_config
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False

# Configuration — resolved from config system
DB_PATH = get_db_path()


@dataclass
class Node:
    """A memory node (entity, fact, preference, etc.)"""
    id: str
    type: str  # Person, Place, Project, Event, Fact, Preference, Concept
    name: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None
    verified: bool = False
    pinned: bool = False  # Core facts that never decay
    confidence: float = 0.5
    source: Optional[str] = None
    source_id: Optional[str] = None
    privacy: str = "shared"  # private, shared, public
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    accessed_at: Optional[str] = None
    access_count: int = 0
    storage_strength: float = 0.0  # Bjork: cumulative encoding strength (hard retrievals add more)
    confirmation_count: int = 0  # How many times this fact has been re-confirmed
    last_confirmed_at: Optional[str] = None  # When last confirmed by re-extraction
    owner_id: Optional[str] = None  # Multi-user: who owns this memory
    session_id: Optional[str] = None  # Session where this memory was created
    fact_type: str = "unknown"  # mutable, immutable, contextual
    knowledge_type: str = "fact"  # fact, belief, preference, experience
    extraction_confidence: float = 0.5  # How confident the classifier was
    status: str = "pending"  # pending, approved, active
    speaker: Optional[str] = None  # Who stated this fact (e.g., "Alice", "Bob")
    speaker_entity_id: Optional[str] = None  # Canonical entity who produced the source utterance
    conversation_id: Optional[str] = None  # Canonical conversation/thread identifier
    visibility_scope: str = "source_shared"  # private_subject/source_shared/global_shared/system
    sensitivity: str = "normal"  # normal/restricted/secret
    provenance_confidence: float = 0.5  # Confidence in attribution chain
    content_hash: Optional[str] = None  # SHA256 of name text (fast dedup pre-filter)
    superseded_by: Optional[str] = None  # ID of node that replaced this one (fact versioning)
    keywords: Optional[str] = None  # Space-separated derived search terms

    @classmethod
    def create(cls, type: str, name: str, **kwargs) -> "Node":
        """Create a new node with auto-generated ID."""
        return cls(
            id=str(uuid.uuid4()),
            type=type,
            name=name,
            **kwargs
        )


@dataclass
class Edge:
    """A relationship between two nodes."""
    id: str
    source_id: str
    target_id: str
    relation: str  # lives_at, works_on, knows, owns, prefers, etc.
    attributes: Dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    created_at: Optional[str] = None
    source_fact_id: Optional[str] = None  # The fact node that created this edge

    @classmethod
    def create(cls, source_id: str, target_id: str, relation: str, **kwargs) -> "Edge":
        """Create a new edge with auto-generated ID."""
        return cls(
            id=str(uuid.uuid4()),
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            **kwargs
        )


def content_hash(text: str) -> str:
    """Compute SHA256 hash of normalized text for fast exact-dedup detection."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class MemoryGraph:
    """Local graph-based memory system."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize database with schema."""
        schema_path = Path(__file__).resolve().parent / "schema.sql"
        with open(schema_path) as f:
            schema = f.read()

        with self._get_conn() as conn:
            # Apply schema with sqlite's parser so inline comments do not
            # accidentally suppress statements (e.g., FTS/triggers).
            conn.executescript(schema)

            # Migrate: add new columns to existing DBs (safe, idempotent)
            for col, typedef in [
                ("content_hash", "TEXT"),
                ("superseded_by", "TEXT"),
                ("knowledge_type", "TEXT DEFAULT 'fact'"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

            for col, typedef in [
                ("confirmation_count", "INTEGER DEFAULT 0"),
                ("last_confirmed_at", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # Forward-compatible multi-user attribution columns.
            for col, typedef in [
                ("speaker_entity_id", "TEXT"),
                ("conversation_id", "TEXT"),
                ("visibility_scope", "TEXT DEFAULT 'source_shared'"),
                ("sensitivity", "TEXT DEFAULT 'normal'"),
                ("provenance_confidence", "REAL DEFAULT 0.5"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE nodes ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # Migrate: add keywords column to nodes
            try:
                conn.execute("ALTER TABLE nodes ADD COLUMN keywords TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Migrate: add storage_strength column (Bjork dual-strength model)
            try:
                conn.execute("ALTER TABLE nodes ADD COLUMN storage_strength REAL DEFAULT 0.0")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Rebuild FTS to include keywords column if schema predates it
            try:
                conn.execute("SELECT keywords FROM nodes_fts LIMIT 0")
            except sqlite3.OperationalError:
                # FTS schema predates keywords — drop triggers + table, re-run schema
                conn.execute("DROP TRIGGER IF EXISTS nodes_ai")
                conn.execute("DROP TRIGGER IF EXISTS nodes_ad")
                conn.execute("DROP TRIGGER IF EXISTS nodes_au")
                conn.execute("DROP TABLE IF EXISTS nodes_fts")
                conn.executescript(schema)
                conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")

            # Migrate FTS to porter stemming tokenizer if not already using it
            try:
                fts_sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
                ).fetchone()
                if fts_sql and 'porter' not in (fts_sql[0] or '').lower():
                    conn.execute("DROP TRIGGER IF EXISTS nodes_ai")
                    conn.execute("DROP TRIGGER IF EXISTS nodes_ad")
                    conn.execute("DROP TRIGGER IF EXISTS nodes_au")
                    conn.execute("DROP TABLE IF EXISTS nodes_fts")
                    # Recreate with porter tokenizer (from schema.sql).
                    conn.executescript(schema)
                    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
            except Exception:
                pass  # Fresh DB or FTS not yet created

            # Migrate recall_log: add reranker delta tracking columns
            for col, typedef in [
                ("reranker_top1_changed", "INTEGER DEFAULT 0"),
                ("reranker_avg_displacement", "REAL"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE recall_log ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # Migrate dedup_log: rename haiku_reasoning → llm_reasoning
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(dedup_log)").fetchall()]
                if "haiku_reasoning" in cols and "llm_reasoning" not in cols:
                    conn.execute("ALTER TABLE dedup_log RENAME COLUMN haiku_reasoning TO llm_reasoning")
            except sqlite3.OperationalError:
                pass  # Column already renamed or table doesn't exist yet

            # Multi-user canonical identity/source tables for forward compatibility.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    entity_id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(entity_type IN ('human', 'agent', 'org', 'system', 'unknown')),
                    canonical_name TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL DEFAULT 'dm'
                        CHECK(source_type IN ('dm', 'group', 'thread', 'workspace')),
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    parent_source_id TEXT,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_participants (
                    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
                    role TEXT NOT NULL DEFAULT 'member'
                        CHECK(role IN ('member', 'owner', 'agent', 'observer')),
                    active_from TEXT DEFAULT (datetime('now')),
                    active_to TEXT,
                    PRIMARY KEY (source_id, entity_id, active_from)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities(entity_type, canonical_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_platform_external ON sources(platform, external_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source_participants_entity ON source_participants(entity_id, active_to)")

            # Extend legacy entity_aliases table with canonical identity linkage columns.
            for col, typedef in [
                ("entity_id", "TEXT"),
                ("platform", "TEXT"),
                ("source_id", "TEXT"),
                ("handle", "TEXT"),
                ("display_name", "TEXT"),
                ("confidence", "REAL DEFAULT 1.0"),
                ("updated_at", "TEXT DEFAULT (datetime('now'))"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE entity_aliases ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity ON entity_aliases(entity_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_aliases_lookup ON entity_aliases(platform, source_id, handle)")

            # Default/backfill attribution values for legacy rows.
            conn.execute(
                """
                UPDATE nodes
                SET conversation_id = COALESCE(conversation_id, source_conversation_id)
                WHERE conversation_id IS NULL OR conversation_id = ''
                """
            )
            conn.execute(
                """
                UPDATE nodes
                SET speaker_entity_id = COALESCE(speaker_entity_id, actor_id)
                WHERE speaker_entity_id IS NULL OR speaker_entity_id = ''
                """
            )
            conn.execute(
                """
                UPDATE nodes
                SET visibility_scope = CASE
                    WHEN owner_id IS NOT NULL AND TRIM(owner_id) != '' THEN 'private_subject'
                    ELSE 'source_shared'
                END
                WHERE visibility_scope IS NULL OR visibility_scope = ''
                """
            )
            conn.execute(
                """
                UPDATE nodes
                SET sensitivity = 'normal'
                WHERE sensitivity IS NULL OR sensitivity = ''
                """
            )
            conn.execute(
                """
                UPDATE nodes
                SET provenance_confidence = COALESCE(extraction_confidence, confidence, 0.5)
                WHERE provenance_confidence IS NULL
                """
            )

            # FTS5 rebuild if out of sync (count mismatch or rowid drift)
            try:
                fts_count = conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
                node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                needs_rebuild = fts_count != node_count and node_count > 0
                if not needs_rebuild and node_count > 0:
                    # Spot-check: verify a sample node's rowid matches FTS
                    sample = conn.execute("SELECT rowid, name FROM nodes ORDER BY rowid DESC LIMIT 1").fetchone()
                    if sample:
                        fts_hit = conn.execute(
                            "SELECT rowid FROM nodes_fts WHERE rowid = ?", (sample[0],)
                        ).fetchone()
                        needs_rebuild = fts_hit is None
                if needs_rebuild:
                    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"FTS5 rebuild check failed: {e}")

        # sqlite-vec: create and populate vector index (separate connection with extension loaded)
        if _lib_has_vec():
            self._init_vec_index()

    def _init_vec_index(self):
        """Create vec_nodes virtual table and backfill from existing embeddings.

        The table dimension is detected from actual stored embeddings rather than
        config, so tests with smaller embeddings work correctly.  If no embeddings
        exist yet, table creation is deferred to the first add_node/update_node call.
        """
        with self._get_conn() as conn:
            # Check if vec_nodes already exists
            try:
                conn.execute("SELECT COUNT(*) FROM vec_nodes")
                table_exists = True
            except sqlite3.OperationalError:
                table_exists = False

            if not table_exists:
                # Detect dimension from first existing embedding in the DB
                sample = conn.execute(
                    "SELECT embedding FROM nodes WHERE embedding IS NOT NULL LIMIT 1"
                ).fetchone()
                if not sample:
                    return  # No embeddings yet — defer table creation
                dim = len(sample["embedding"]) // 4  # 4 bytes per float32
                conn.execute(
                    f"CREATE VIRTUAL TABLE vec_nodes USING vec0(node_id TEXT PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)"
                )

            # Backfill: insert any nodes with embeddings not yet in vec_nodes
            missing = conn.execute("""
                SELECT n.id, n.embedding FROM nodes n
                WHERE n.embedding IS NOT NULL
                  AND n.id NOT IN (SELECT node_id FROM vec_nodes)
            """).fetchall()
            if missing:
                for row in missing:
                    try:
                        conn.execute(
                            "INSERT INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                            (row["id"], row["embedding"])
                        )
                    except Exception as exc:
                        logger.warning(
                            "vec backfill skipped node %s due to vec_nodes insert failure: %s",
                            row["id"],
                            exc,
                        )
                        if _is_fail_hard_mode():
                            raise RuntimeError(
                                "Vector index backfill failed while fail-hard mode is enabled"
                            ) from exc
                print(f"[vec] Backfilled {len(missing)} embeddings into vec_nodes", file=sys.stderr)

    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection. Delegates to lib.database."""
        return _lib_get_connection(self.db_path)

    def _ensure_vec_table(self, conn, embedding: List[float]) -> None:
        """Lazily create vec_nodes table if it doesn't exist yet.

        Uses the dimension of the provided embedding so tests with smaller
        vectors work without config overrides.
        """
        try:
            conn.execute("SELECT 1 FROM vec_nodes LIMIT 0")
        except sqlite3.OperationalError:
            dim = len(embedding)
            conn.execute(
                f"CREATE VIRTUAL TABLE vec_nodes USING vec0(node_id TEXT PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)"
            )

    # ==========================================================================
    # Embeddings
    # ==========================================================================

    def get_embedding(self, text: str) -> Optional[List[float]]:
        """Get embedding, checking cache first to avoid redundant Ollama calls."""
        text_hash = content_hash(text)
        model = "unknown"
        try:
            from lib.embeddings import get_embeddings_provider
            model = get_embeddings_provider().model_name
        except Exception:
            pass

        # Check cache (must match current model to avoid stale embeddings)
        # Skip cache entirely when model is unknown — avoids cross-model cache pollution
        if model != "unknown":
            try:
                with self._get_conn() as conn:
                    row = conn.execute(
                        "SELECT embedding FROM embedding_cache WHERE text_hash = ? AND model = ?",
                        (text_hash, model)
                    ).fetchone()
                    if row and row["embedding"]:
                        return self._unpack_embedding(row["embedding"])
            except Exception:
                pass  # Cache miss or table doesn't exist

        # Cache miss — compute fresh
        embedding = _lib_get_embedding(text)
        if embedding:
            try:
                with self._get_conn() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model) VALUES (?, ?, ?)",
                        (text_hash, self._pack_embedding(embedding), model)
                    )
            except Exception:
                pass  # Cache write failure is non-fatal
        return embedding

    def _pack_embedding(self, embedding: List[float]) -> bytes:
        """Pack embedding as binary blob. Delegates to lib.embeddings."""
        return _lib_pack_embedding(embedding)

    def _unpack_embedding(self, blob: bytes) -> List[float]:
        """Unpack embedding from binary blob. Delegates to lib.embeddings."""
        return _lib_unpack_embedding(blob)

    def cosine_similarity(self, a: List[float], b: List[float]) -> float:
        """Calculate cosine similarity. Delegates to lib.similarity."""
        return _lib_cosine_similarity(a, b)

    # ==========================================================================
    # Node Operations
    # ==========================================================================

    def add_node(self, node: Node, embed: bool = True) -> str:
        """Add a NEW node to the graph. For updating existing nodes, use update_node().

        WARNING: This uses INSERT OR REPLACE which will DELETE then re-INSERT if the
        node ID already exists, triggering ON DELETE CASCADE on edges. Only use for
        genuinely new nodes. Use update_node() for modifying existing nodes.
        """
        if embed and not node.embedding:
            # Combine name and key attributes for embedding
            embed_text = node.name
            if node.attributes:
                embed_text += " " + " ".join(str(v) for v in node.attributes.values() if v)
            node.embedding = self.get_embedding(embed_text)

        # Compute content hash for fast dedup
        if not node.content_hash:
            node.content_hash = content_hash(node.name)

        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO nodes
                (id, type, name, attributes, embedding, verified, pinned, confidence,
                 source, source_id, privacy, valid_from, valid_until,
                 created_at, updated_at, accessed_at, access_count, storage_strength, owner_id, session_id,
                 fact_type, knowledge_type, extraction_confidence, status, speaker, speaker_entity_id,
                 conversation_id, visibility_scope, sensitivity, provenance_confidence,
                 content_hash, superseded_by, confirmation_count, last_confirmed_at, keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                node.id, node.type, node.name,
                json.dumps(node.attributes),
                self._pack_embedding(node.embedding) if node.embedding else None,
                1 if node.verified else 0,
                1 if node.pinned else 0,
                node.confidence,
                node.source, node.source_id,
                node.privacy,
                node.valid_from, node.valid_until,
                node.created_at or datetime.now().isoformat(),
                datetime.now().isoformat(),
                node.accessed_at or datetime.now().isoformat(),
                node.access_count,
                node.storage_strength,
                node.owner_id,
                node.session_id,
                node.fact_type,
                node.knowledge_type,
                node.extraction_confidence,
                node.status,
                node.speaker,
                node.speaker_entity_id,
                node.conversation_id,
                node.visibility_scope,
                node.sensitivity,
                node.provenance_confidence,
                node.content_hash,
                node.superseded_by,
                node.confirmation_count,
                node.last_confirmed_at,
                node.keywords,
            ))
            # Maintain vec index
            if node.embedding and _lib_has_vec():
                packed = self._pack_embedding(node.embedding)
                try:
                    self._ensure_vec_table(conn, node.embedding)
                    conn.execute("INSERT OR REPLACE INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                                 (node.id, packed))
                except Exception as exc:
                    logger.warning(
                        "add_node inserted node %s but failed vec_nodes upsert: %s",
                        node.id,
                        exc,
                    )
                    if _is_fail_hard_mode():
                        raise RuntimeError(
                            "Vector index upsert failed during add_node while fail-hard mode is enabled"
                        ) from exc
        return node.id

    def update_node(self, node: Node, embed: bool = False) -> bool:
        """Update an existing node without triggering ON DELETE CASCADE on edges.

        Uses UPDATE instead of INSERT OR REPLACE, preserving all edges.
        Returns True if the node was found and updated, False if not found.
        """
        if embed and not node.embedding:
            embed_text = node.name
            if node.attributes:
                embed_text += " " + " ".join(str(v) for v in node.attributes.values() if v)
            node.embedding = self.get_embedding(embed_text)

        # Recompute content hash if name changed
        if not node.content_hash:
            node.content_hash = content_hash(node.name)

        with self._get_conn() as conn:
            result = conn.execute("""
                UPDATE nodes SET
                    type = ?, name = ?, attributes = ?, embedding = ?,
                    verified = ?, pinned = ?, confidence = ?,
                    source = ?, source_id = ?, privacy = ?,
                    valid_from = ?, valid_until = ?,
                    updated_at = ?, accessed_at = ?, access_count = ?,
                    storage_strength = ?,
                    owner_id = ?, session_id = ?,
                    fact_type = ?, knowledge_type = ?, extraction_confidence = ?,
                    status = ?, speaker = ?, speaker_entity_id = ?,
                    conversation_id = ?, visibility_scope = ?, sensitivity = ?,
                    provenance_confidence = ?,
                    content_hash = ?, superseded_by = ?,
                    confirmation_count = ?, last_confirmed_at = ?,
                    keywords = ?
                WHERE id = ?
            """, (
                node.type, node.name,
                json.dumps(node.attributes),
                self._pack_embedding(node.embedding) if node.embedding else None,
                1 if node.verified else 0,
                1 if node.pinned else 0,
                node.confidence,
                node.source, node.source_id,
                node.privacy,
                node.valid_from, node.valid_until,
                datetime.now().isoformat(),
                node.accessed_at or datetime.now().isoformat(),
                node.access_count,
                node.storage_strength,
                node.owner_id,
                node.session_id,
                node.fact_type,
                node.knowledge_type,
                node.extraction_confidence,
                node.status,
                node.speaker,
                node.speaker_entity_id,
                node.conversation_id,
                node.visibility_scope,
                node.sensitivity,
                node.provenance_confidence,
                node.content_hash,
                node.superseded_by,
                node.confirmation_count,
                node.last_confirmed_at,
                node.keywords,
                node.id
            ))
            # Maintain vec index
            if node.embedding and _lib_has_vec() and result.rowcount > 0:
                packed = self._pack_embedding(node.embedding)
                try:
                    self._ensure_vec_table(conn, node.embedding)
                    conn.execute("INSERT OR REPLACE INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                                 (node.id, packed))
                except Exception as exc:
                    logger.warning(
                        "update_node updated node %s but failed vec_nodes upsert: %s",
                        node.id,
                        exc,
                    )
                    if _is_fail_hard_mode():
                        raise RuntimeError(
                            "Vector index upsert failed during update_node while fail-hard mode is enabled"
                        ) from exc
            return result.rowcount > 0

    def get_node(self, node_id: str) -> Optional[Node]:
        """Get a node by ID."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if row:
                return self._row_to_node(row)
        return None

    def find_node_by_name(self, name: str, type: Optional[str] = None) -> Optional[Node]:
        """Find a node by exact name match."""
        with self._get_conn() as conn:
            if type:
                row = conn.execute(
                    "SELECT * FROM nodes WHERE name = ? AND type = ?",
                    (name, type)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM nodes WHERE name = ?", (name,)
                ).fetchone()
            if row:
                return self._row_to_node(row)
        return None

    def delete_node(self, node_id: str) -> bool:
        """Delete a node and all its edges."""
        with self._get_conn() as conn:
            result = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
            if result.rowcount > 0 and _lib_has_vec():
                try:
                    conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (node_id,))
                except Exception as exc:
                    logger.warning(
                        "delete_node removed node %s but failed vec_nodes cleanup: %s",
                        node_id,
                        exc,
                    )
            return result.rowcount > 0

    def supersede_node(self, old_id: str, new_id: str) -> bool:
        """Mark old_id as superseded by new_id (fact versioning).

        The old node is kept for history but excluded from search results.
        Its confidence is reduced to 0.1 and valid_until is set to now.
        """
        with self._get_conn() as conn:
            result = conn.execute("""
                UPDATE nodes SET superseded_by = ?, confidence = 0.1,
                    valid_until = ?, updated_at = ?
                WHERE id = ? AND superseded_by IS NULL
            """, (new_id, datetime.now().isoformat(), datetime.now().isoformat(), old_id))
            return result.rowcount > 0

    def get_fact_history(self, node_id: str) -> List[Node]:
        """Follow the supersedes chain to get fact evolution history.

        Returns list from oldest to newest, ending with the current version.
        """
        history = []
        visited = set()
        with self._get_conn() as conn:
            # Walk backwards: find nodes that were superseded to arrive at this one.
            def _find_predecessors(nid: str) -> None:
                if nid in visited:
                    return
                visited.add(nid)
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE superseded_by = ?", (nid,)
                ).fetchall()
                for row in rows:
                    pred = self._row_to_node(row)
                    _find_predecessors(pred.id)
                    history.append(pred)

            _find_predecessors(node_id)

            # Add the current node at the end.
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if row:
                history.append(self._row_to_node(row))

        return history

    def _row_to_node(self, row: sqlite3.Row) -> Node:
        """Convert database row to Node object."""
        attrs_raw = row['attributes']
        try:
            attributes = json.loads(attrs_raw) if attrs_raw else {}
        except (TypeError, ValueError):
            import logging
            logging.getLogger(__name__).warning(
                "[memory_graph] malformed node attributes JSON for node_id=%s; using empty attributes",
                row['id']
            )
            attributes = {}
        return Node(
            id=row['id'],
            type=row['type'],
            name=row['name'],
            attributes=attributes,
            embedding=self._unpack_embedding(row['embedding']) if row['embedding'] else None,
            verified=bool(row['verified']),
            pinned=bool(row['pinned']) if 'pinned' in row.keys() else False,
            confidence=row['confidence'],
            source=row['source'],
            source_id=row['source_id'],
            privacy=row['privacy'],
            valid_from=row['valid_from'],
            valid_until=row['valid_until'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            accessed_at=row['accessed_at'],
            access_count=row['access_count'],
            storage_strength=row['storage_strength'] if 'storage_strength' in row.keys() else 0.0,
            owner_id=row['owner_id'] if 'owner_id' in row.keys() else None,
            session_id=row['session_id'] if 'session_id' in row.keys() else None,
            fact_type=row['fact_type'] if 'fact_type' in row.keys() else 'unknown',
            knowledge_type=row['knowledge_type'] if 'knowledge_type' in row.keys() else 'fact',
            extraction_confidence=row['extraction_confidence'] if 'extraction_confidence' in row.keys() else 0.5,
            status=row['status'] if 'status' in row.keys() else 'pending',
            speaker=row['speaker'] if 'speaker' in row.keys() else None,
            speaker_entity_id=row['speaker_entity_id'] if 'speaker_entity_id' in row.keys() else None,
            conversation_id=row['conversation_id'] if 'conversation_id' in row.keys() else None,
            visibility_scope=row['visibility_scope'] if 'visibility_scope' in row.keys() else 'source_shared',
            sensitivity=row['sensitivity'] if 'sensitivity' in row.keys() else 'normal',
            provenance_confidence=row['provenance_confidence'] if 'provenance_confidence' in row.keys() else 0.5,
            content_hash=row['content_hash'] if 'content_hash' in row.keys() else None,
            superseded_by=row['superseded_by'] if 'superseded_by' in row.keys() else None,
            confirmation_count=row['confirmation_count'] if 'confirmation_count' in row.keys() else 0,
            last_confirmed_at=row['last_confirmed_at'] if 'last_confirmed_at' in row.keys() else None,
            keywords=row['keywords'] if 'keywords' in row.keys() else None,
        )

    # ==========================================================================
    # Edge Operations
    # ==========================================================================

    def add_edge(self, edge: Edge) -> str:
        """Add an edge to the graph."""
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO edges
                (id, source_id, target_id, relation, attributes, weight,
                 valid_from, valid_until, created_at, source_fact_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                edge.id, edge.source_id, edge.target_id, edge.relation,
                json.dumps(edge.attributes),
                edge.weight,
                edge.valid_from, edge.valid_until,
                edge.created_at or datetime.now().isoformat(),
                edge.source_fact_id
            ))
        return edge.id

    def get_known_relations(self) -> List[str]:
        """Get all distinct relation types currently in the edges table."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT relation FROM edges ORDER BY relation"
            ).fetchall()
            return [r[0] for r in rows]

    def get_edges(self, node_id: str, direction: str = "both") -> List[Edge]:
        """Get all edges connected to a node."""
        edges = []
        with self._get_conn() as conn:
            if direction in ("out", "both"):
                rows = conn.execute(
                    "SELECT * FROM edges WHERE source_id = ?", (node_id,)
                ).fetchall()
                edges.extend(self._row_to_edge(r) for r in rows)
            if direction in ("in", "both"):
                rows = conn.execute(
                    "SELECT * FROM edges WHERE target_id = ?", (node_id,)
                ).fetchall()
                edges.extend(self._row_to_edge(r) for r in rows)
        return edges

    def _row_to_edge(self, row: sqlite3.Row) -> Edge:
        """Convert database row to Edge object."""
        attrs_raw = row['attributes']
        try:
            attributes = json.loads(attrs_raw) if attrs_raw else {}
        except (TypeError, ValueError):
            import logging
            logging.getLogger(__name__).warning(
                "[memory_graph] malformed edge attributes JSON for edge_id=%s; using empty attributes",
                row['id']
            )
            attributes = {}
        return Edge(
            id=row['id'],
            source_id=row['source_id'],
            target_id=row['target_id'],
            relation=row['relation'],
            attributes=attributes,
            weight=row['weight'],
            valid_from=row['valid_from'],
            valid_until=row['valid_until'],
            created_at=row['created_at'],
            source_fact_id=row['source_fact_id'] if 'source_fact_id' in row.keys() else None
        )

    # ==========================================================================
    # Search Operations
    # ==========================================================================

    def search_semantic(
        self,
        query: str,
        limit: int = 10,
        types: Optional[List[str]] = None,
        privacy: Optional[List[str]] = None,
        owner_id: Optional[str] = None,
        min_similarity: float = 0.3,
        current_session_id: Optional[str] = None,
        compaction_time: Optional[str] = None
    ) -> List[tuple[Node, float]]:
        """Search nodes by semantic similarity.

        Uses sqlite-vec indexed KNN when available, falls back to
        FTS5 pre-filter + brute-force cosine when not installed.
        """
        query_embedding = self.get_embedding(query)
        if not query_embedding:
            if _is_fail_hard_mode():
                raise RuntimeError(
                    "Embedding provider returned no vector for semantic search. "
                    "Fail-hard mode is ON (retrieval.fail_hard=true), "
                    "so degraded FTS-only fallback is blocked. "
                    "Set retrieval.fail_hard=false to allow fallback, "
                    "but this is not recommended because it masks infrastructure faults."
                )
            return []

        results = []

        if _lib_has_vec():
            results = self._search_vec(query_embedding, limit * 4, types, privacy,
                                       owner_id, min_similarity, current_session_id, compaction_time)
        else:
            results = self._search_brute_force(query_embedding, types, privacy,
                                                owner_id, min_similarity, current_session_id, compaction_time)

        # Sort by similarity descending
        results.sort(key=lambda x: x[1], reverse=True)

        return results[:limit]

    def _search_vec(self, query_embedding, candidate_limit, types, privacy,
                    owner_id, min_similarity, current_session_id, compaction_time):
        """KNN search via sqlite-vec. Returns [(Node, similarity), ...]."""
        packed_query = self._pack_embedding(query_embedding)
        # cosine distance: 0 = identical, 1 = orthogonal, 2 = opposite
        max_distance = 1.0 - min_similarity  # convert similarity threshold to distance

        results = []
        with self._get_conn() as conn:
            # vec0 KNN query — retrieve more candidates than needed for post-filtering
            vec_rows = conn.execute(
                "SELECT node_id, distance FROM vec_nodes WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (packed_query, candidate_limit)
            ).fetchall()

            if not vec_rows:
                return []

            # Batch-fetch the actual nodes for filtering.
            # Guarantee at least min_results candidates even if below similarity
            # threshold — they'll be reranked downstream anyway.
            min_results = max(5, candidate_limit // 4)
            above_threshold = [r["node_id"] for r in vec_rows if r["distance"] <= max_distance]
            if len(above_threshold) >= min_results:
                node_ids = above_threshold
            else:
                # Not enough above threshold — take best available up to min_results
                node_ids = [r["node_id"] for r in sorted(vec_rows, key=lambda r: r["distance"])[:max(min_results, len(above_threshold))]]
            if not node_ids:
                return []

            distances = {r["node_id"]: r["distance"] for r in vec_rows}

            placeholders = ",".join("?" * len(node_ids))
            sql = f"""SELECT * FROM nodes WHERE id IN ({placeholders})
                      AND (status IS NULL OR status IN ('approved', 'pending', 'active'))
                      AND superseded_by IS NULL"""
            params = list(node_ids)

            if types:
                sql += f" AND type IN ({','.join('?' * len(types))})"
                params.extend(types)
            if privacy:
                sql += f" AND privacy IN ({','.join('?' * len(privacy))})"
                params.extend(privacy)
            if owner_id:
                sql += " AND (owner_id = ? OR owner_id IS NULL OR privacy IN ('shared', 'public'))"
                params.append(owner_id)
            if current_session_id:
                if compaction_time:
                    sql += " AND (session_id IS NULL OR session_id != ? OR (session_id = ? AND created_at <= ?))"
                    params.extend([current_session_id, current_session_id, compaction_time])
                else:
                    sql += " AND (session_id IS NULL OR session_id != ?)"
                    params.append(current_session_id)

            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                node = self._row_to_node(row)
                similarity = 1.0 - distances.get(node.id, 1.0)
                results.append((node, similarity))

        return results

    def _search_brute_force(self, query_embedding, types, privacy,
                            owner_id, min_similarity, current_session_id, compaction_time):
        """Brute-force cosine search (fallback when sqlite-vec not installed)."""
        results = []

        def _build_full_scan_sql():
            sql = """SELECT * FROM nodes WHERE embedding IS NOT NULL
                     AND (status IS NULL OR status IN ('approved', 'pending', 'active'))
                     AND superseded_by IS NULL"""
            params = []
            if types:
                sql += f" AND type IN ({','.join('?' * len(types))})"
                params.extend(types)
            if privacy:
                sql += f" AND privacy IN ({','.join('?' * len(privacy))})"
                params.extend(privacy)
            if owner_id:
                sql += " AND (owner_id = ? OR owner_id IS NULL OR privacy IN ('shared', 'public'))"
                params.append(owner_id)
            if current_session_id:
                if compaction_time:
                    sql += " AND (session_id IS NULL OR session_id != ? OR (session_id = ? AND created_at <= ?))"
                    params.extend([current_session_id, current_session_id, compaction_time])
                else:
                    sql += " AND (session_id IS NULL OR session_id != ?)"
                    params.append(current_session_id)
            sql += " ORDER BY accessed_at DESC LIMIT 200"
            return sql, params

        with self._get_conn() as conn:
            sql, params = _build_full_scan_sql()
            try:
                rows = conn.execute(sql, params).fetchall()
            except Exception as e:
                logger.warning("Brute-force semantic search query failed: %s", e)
                if _is_fail_hard_mode():
                    raise RuntimeError(
                        "Brute-force semantic search failed while fail-hard mode is enabled"
                    ) from e
                rows = []

            for row in rows:
                node = self._row_to_node(row)
                if node.embedding:
                    sim = self.cosine_similarity(query_embedding, node.embedding)
                    if sim >= min_similarity:
                        results.append((node, sim))

        return results

    def search_fts(self, query: str, limit: int = 10, owner_id: Optional[str] = None) -> List[tuple[Node, float]]:
        """Search nodes using FTS5 MATCH with BM25 ranking.

        Returns (Node, rank) tuples ordered by FTS5 bm25() relevance.
        Rank values are the raw position (1-based) in BM25-sorted results,
        used by RRF fusion in search_hybrid().
        """
        tokens = _lib_extract_key_tokens(query)
        if not tokens:
            return []

        fts_query = " OR ".join(f'"{t}"' for t in tokens)

        with self._get_conn() as conn:
            try:
                owner_clause = "AND (n.owner_id = ? OR n.owner_id IS NULL)" if owner_id else ""
                params = [fts_query] + ([owner_id] if owner_id else []) + [limit * 3]
                rows = conn.execute(f"""
                    SELECT n.*, bm25(nodes_fts, 2.0, 1.0) AS bm25_score
                    FROM nodes_fts
                    JOIN nodes n ON n.rowid = nodes_fts.rowid
                    WHERE nodes_fts MATCH ?
                      AND (n.status IS NULL OR n.status IN ('approved', 'pending', 'active'))
                      AND n.deleted_at IS NULL
                      AND n.superseded_by IS NULL
                      {owner_clause}
                    ORDER BY bm25(nodes_fts, 2.0, 1.0)
                    LIMIT ?
                """, params).fetchall()
            except Exception as e:
                if _is_fail_hard_mode():
                    raise RuntimeError(
                        "FTS search failed while fail-hard mode is enabled"
                    ) from e
                # FTS5 index may not be rebuilt yet; fall back to LIKE
                raw_words = re.sub(r'[^\w\s]', '', query).split()
                words = [w for w in raw_words if len(w) >= 3 and w.lower() not in _LIB_STOPWORDS]
                proper_nouns = [w for w in words if w[0].isupper() and not w.isupper()]
                common_words = [w for w in words if w not in proper_nouns]
                return self._search_fts_fallback(words, proper_nouns, common_words, limit, owner_id=owner_id)

            # Deduplicate and preserve BM25 ordering (rank = position)
            seen = {}
            for rank, r in enumerate(rows, 1):
                node = self._row_to_node(r)
                if node.id not in seen:
                    seen[node.id] = (node, float(rank))

            return list(seen.values())[:limit]

    def _search_fts_fallback(self, words, proper_nouns, common_words, limit, owner_id: Optional[str] = None):
        """LIKE-based fallback when FTS5 index is unavailable."""
        owner_clause = "AND (owner_id = ? OR owner_id IS NULL)" if owner_id else ""
        with self._get_conn() as conn:
            node_hits: dict = {}

            for word in proper_nouns:
                pattern = f"%{word}%"
                params = [pattern] + ([owner_id] if owner_id else []) + [limit * 3]
                rows = conn.execute(f"""
                    SELECT * FROM nodes
                    WHERE name LIKE ? AND (status IS NULL OR status IN ('approved', 'pending', 'active'))
                    AND deleted_at IS NULL AND superseded_by IS NULL
                    {owner_clause}
                    LIMIT ?
                """, params).fetchall()
                for r in rows:
                    node = self._row_to_node(r)
                    if node.id in node_hits:
                        node_hits[node.id] = (node, node_hits[node.id][1] + 3)
                    else:
                        node_hits[node.id] = (node, 3)

            for word in common_words:
                pattern = f"%{word}%"
                params = [pattern] + ([owner_id] if owner_id else []) + [limit * 2]
                rows = conn.execute(f"""
                    SELECT * FROM nodes
                    WHERE name LIKE ? AND (status IS NULL OR status IN ('approved', 'pending', 'active'))
                    AND deleted_at IS NULL AND superseded_by IS NULL
                    {owner_clause}
                    LIMIT ?
                """, params).fetchall()
                for r in rows:
                    node = self._row_to_node(r)
                    if node.id in node_hits:
                        node_hits[node.id] = (node, node_hits[node.id][1] + 1)
                    else:
                        node_hits[node.id] = (node, 1)

            sorted_nodes = sorted(node_hits.values(), key=lambda x: x[1], reverse=True)
            # Convert hit scores to 1-based rank positions for RRF
            ranked = [(node, rank) for rank, (node, _score) in enumerate(sorted_nodes[:limit], 1)]
            return ranked

    def search_hybrid(
        self,
        query: str,
        limit: int = 10,
        types: Optional[List[str]] = None,
        privacy: Optional[List[str]] = None,
        owner_id: Optional[str] = None,
        current_session_id: Optional[str] = None,
        compaction_time: Optional[str] = None,
        intent: Optional[str] = None
    ) -> List[tuple[Node, float]]:
        """Hybrid search combining semantic + FTS via Reciprocal Rank Fusion.

        Uses RRF (k=60) to combine ranked lists from semantic and FTS search.
        Both searches run concurrently via the core worker pool.
        Each result also carries a quality_score (cosine similarity from semantic
        search) used for threshold filtering in recall().
        """
        # RRF constant from config (default 60, min 1 to prevent div-by-zero)
        RRF_K = 60
        try:
            from config import get_config
            RRF_K = max(1, get_config().retrieval.rrf_k)
        except Exception:
            pass
        VECTOR_WEIGHT, FTS_WEIGHT = _get_fusion_weights(intent)

        # Run semantic and FTS search concurrently.
        results = run_callables(
            [
                lambda: self.search_semantic(
                    query,
                    limit=limit * 2,
                    types=types,
                    privacy=privacy,
                    owner_id=owner_id,
                    current_session_id=current_session_id,
                    compaction_time=compaction_time,
                ),
                lambda: self.search_fts(query, limit=limit * 2, owner_id=owner_id),
            ],
            max_workers=2,
            pool_name="search-hybrid",
            timeout_seconds=10.0,
            return_exceptions=True,
        )
        semantic_results = [] if isinstance(results[0], Exception) else results[0]
        fts_results = [] if isinstance(results[1], Exception) else results[1]

        # Build RRF scores
        # Track both RRF rank score (for ordering) and quality score (for thresholding)
        rrf_scores: Dict[str, float] = {}       # node_id -> RRF score
        quality_scores: Dict[str, float] = {}    # node_id -> cosine similarity
        nodes: Dict[str, Node] = {}              # node_id -> Node

        for rank, (node, sim) in enumerate(semantic_results, 1):
            nodes[node.id] = node
            rrf_scores[node.id] = VECTOR_WEIGHT / (RRF_K + rank)
            quality_scores[node.id] = sim  # cosine similarity as quality

        for node, fts_rank in fts_results:
            nodes.setdefault(node.id, node)
            rrf_scores.setdefault(node.id, 0.0)
            rrf_scores[node.id] += FTS_WEIGHT / (RRF_K + fts_rank)
            # FTS-only results lack semantic validation — cap quality lower
            if node.id not in quality_scores:
                quality_scores[node.id] = max(0.4, 0.7 - fts_rank * 0.02)

        # Sort by RRF score, attach quality_score for downstream filtering
        results = []
        for node_id in sorted(rrf_scores, key=rrf_scores.get, reverse=True):
            node = nodes[node_id]
            # Store quality_score on the node temporarily for recall() filtering
            results.append((node, quality_scores[node_id]))

        return results[:limit * 2]  # Return extra for downstream filtering

    def _update_access(self, results: List[tuple[Node, float]], difficulty_map: Optional[Dict[str, float]] = None):
        """Update access stats and storage strength for search results.

        Args:
            results: List of (Node, score) tuples.
            difficulty_map: Optional mapping of node ID → retrieval difficulty [0.0, 1.0].
                Hard retrievals increment storage_strength more (Bjork model).
        """
        now = datetime.now().isoformat()
        with self._get_conn() as conn:
            for node, _ in results:
                difficulty = (difficulty_map or {}).get(node.id, 0.0)
                # Bjork: base increment + difficulty bonus (hard retrievals strengthen more)
                ss_increment = 0.05 * (1.0 + 3.0 * difficulty)
                conn.execute("""
                    UPDATE nodes SET
                        accessed_at = ?,
                        access_count = access_count + 1,
                        storage_strength = MIN(COALESCE(storage_strength, 0.0) + ?, 10.0)
                    WHERE id = ?
                """, (now, ss_increment, node.id))

    # ==========================================================================
    # Graph Traversal
    # ==========================================================================

    def get_related(
        self,
        node_id: str,
        relation: Optional[str] = None,
        depth: int = 1
    ) -> List[tuple[Node, str, int]]:
        """Get related nodes up to a certain depth. Uses single connection + JOIN."""
        visited = set()
        results = []
        queue = [(node_id, 0)]

        with self._get_conn() as conn:
            while queue:
                current_id, current_depth = queue.pop(0)
                if current_id in visited or current_depth > depth:
                    continue
                visited.add(current_id)

                sql = """SELECT n.*, e.relation, e.target_id
                         FROM edges e JOIN nodes n ON n.id = e.target_id
                         WHERE e.source_id = ?"""
                params = [current_id]
                if relation:
                    sql += " AND e.relation = ?"
                    params.append(relation)

                rows = conn.execute(sql, params).fetchall()
                for row in rows:
                    target_id = row['target_id']
                    edge_relation = row['relation']
                    if target_id not in visited:
                        target = self._row_to_node(row)
                        results.append((target, edge_relation, current_depth + 1))
                        if current_depth + 1 < depth:
                            queue.append((target_id, current_depth + 1))

        return results

    def get_related_bidirectional(
        self,
        node_id: str,
        relations: Optional[List[str]] = None,
        depth: int = 2,
        max_results: int = 20
    ) -> List[tuple]:
        """Get related nodes traversing BOTH inbound and outbound edges.

        Multi-hop BFS with cycle detection and early stopping.
        Uses single connection + JOIN queries to avoid N+1.

        Args:
            node_id: Starting node ID
            relations: Optional list of relations to filter by
            depth: Maximum traversal depth (default 2 for multi-hop)
            max_results: Early stop after this many results

        Returns:
            List of (node, relation, direction, depth, path) tuples.
            direction is "out" or "in" indicating edge direction from start node.
            path is a list of (node_name, relation) tuples showing the traversal
            chain from the start node to this result. For example, a depth-2
            result might have path=[("Alice", "parent_of"), ("Bob", "has_pet")]
            meaning Alice --parent_of--> Bob --has_pet--> [result_node].
        """
        visited = set()
        results = []
        # paths tracks the traversal chain to each node: {node_id: [(from_name, relation), ...]}
        paths: Dict[str, list] = {}

        with self._get_conn() as conn:
            # Look up start node name for path building
            start_row = conn.execute("SELECT name FROM nodes WHERE id = ?", (node_id,)).fetchone()
            start_name = start_row["name"] if start_row else "?"

            # Queue: (current_id, current_depth, current_name)
            queue = [(node_id, 0, start_name)]

            while queue:
                if len(results) >= max_results:
                    break

                current_id, current_depth, current_name = queue.pop(0)
                if current_id in visited or current_depth > depth:
                    continue
                visited.add(current_id)

                # Path to the current node (empty for start node)
                current_path = paths.get(current_id, [])

                # Outbound edges: JOIN to get target node in one query
                out_sql = """SELECT n.*, e.relation AS edge_relation
                             FROM edges e JOIN nodes n ON n.id = e.target_id
                             WHERE e.source_id = ?"""
                out_params = [current_id]
                if relations:
                    out_sql += f" AND e.relation IN ({','.join('?' * len(relations))})"
                    out_params.extend(relations)

                for row in conn.execute(out_sql, out_params).fetchall():
                    target = self._row_to_node(row)
                    edge_relation = row['edge_relation']
                    if target.id not in visited:
                        # Build path: current path + this hop
                        target_path = current_path + [(current_name, edge_relation)]
                        if target.id not in paths:
                            paths[target.id] = target_path
                        results.append((target, edge_relation, "out", current_depth + 1, target_path))
                        if len(results) >= max_results:
                            break
                        if current_depth + 1 < depth:
                            queue.append((target.id, current_depth + 1, target.name))

                if len(results) >= max_results:
                    break

                # Inbound edges: JOIN to get source node in one query
                in_sql = """SELECT n.*, e.relation AS edge_relation
                            FROM edges e JOIN nodes n ON n.id = e.source_id
                            WHERE e.target_id = ?"""
                in_params = [current_id]
                if relations:
                    in_sql += f" AND e.relation IN ({','.join('?' * len(relations))})"
                    in_params.extend(relations)

                for row in conn.execute(in_sql, in_params).fetchall():
                    source = self._row_to_node(row)
                    edge_relation = row['edge_relation']
                    if source.id not in visited:
                        # Build path: current path + this hop (inbound: source points to current)
                        source_path = current_path + [(current_name, edge_relation)]
                        if source.id not in paths:
                            paths[source.id] = source_path
                        results.append((source, edge_relation, "in", current_depth + 1, source_path))
                        if len(results) >= max_results:
                            break
                        if current_depth + 1 < depth:
                            queue.append((source.id, current_depth + 1, source.name))

        return results

    def beam_search_graph(
        self,
        query: str,
        start_id: str,
        beam_width: int = 5,
        max_depth: int = 2,
        max_results: int = 20,
        intent: Optional[str] = None,
        type_boosts: Optional[Dict[str, float]] = None,
        scoring_mode: str = "heuristic",
        relations: Optional[List[str]] = None,
        hop_decay: float = 0.7,
        config_retrieval=None,
    ) -> List[tuple]:
        """Scored BEAM search on the memory graph.

        Unlike BFS which expands ALL neighbors at each hop, BEAM keeps only
        the top-B candidates per level, dramatically reducing node visits
        while focusing on the most promising paths.

        Scoring is adaptive:
          1. All candidates scored with fast heuristic (edge weight, node
             quality, intent alignment, relation selectivity).
          2. If more candidates than beam_width (truncation needed), the top
             2*beam_width candidates are re-scored via the fast-reasoning LLM
             reranker in a single batched call.
          3. If candidates fit within beam_width, no LLM call needed.

        This ensures LLM cost is only incurred when there's actual
        competition for beam slots.

        Args:
            query: Search query (for scoring relevance)
            start_id: Starting node ID
            beam_width: Keep top-B candidates per hop level
            max_depth: Maximum traversal depth
            max_results: Early stop after this many results
            intent: Query intent (WHO, WHAT, etc.) for type boosting
            type_boosts: Node type -> multiplier map
            scoring_mode: Currently unused. Scoring is adaptive:
                heuristic + conditional LLM reranker.
            relations: Optional list of relations to filter by

        Returns:
            List of (node, relation, direction, depth, path, beam_score) tuples.
        """
        visited = set()
        results = []
        paths: Dict[str, list] = {}

        with self._get_conn() as conn:
            # Look up start node
            start_row = conn.execute(
                "SELECT name FROM nodes WHERE id = ?", (start_id,)
            ).fetchone()
            start_name = start_row["name"] if start_row else "?"

            # Current beam: [(node_id, depth, name, beam_score)]
            beam = [(start_id, 0, start_name, 1.0)]

            for depth_level in range(max_depth + 1):
                if not beam or len(results) >= max_results:
                    break

                # Expand all candidates in current beam
                next_candidates = []

                for current_id, current_depth, current_name, parent_score in beam:
                    if current_id in visited:
                        continue
                    visited.add(current_id)

                    current_path = paths.get(current_id, [])

                    # Skip start node from results
                    if current_depth > 0:
                        # Already added when selected for beam
                        pass

                    if current_depth >= max_depth:
                        continue

                    # Gather all neighbors (outbound + inbound)
                    neighbors = []

                    # Outbound edges
                    out_sql = """SELECT n.*, e.relation AS edge_relation, e.weight AS edge_weight
                                 FROM edges e JOIN nodes n ON n.id = e.target_id
                                 WHERE e.source_id = ?"""
                    out_params = [current_id]
                    if relations:
                        out_sql += f" AND e.relation IN ({','.join('?' * len(relations))})"
                        out_params.extend(relations)

                    for row in conn.execute(out_sql, out_params).fetchall():
                        target = self._row_to_node(row)
                        if target.id not in visited:
                            edge_weight = row["edge_weight"] if row["edge_weight"] else 0.5
                            target_path = current_path + [(current_name, row["edge_relation"])]
                            neighbors.append((
                                target, row["edge_relation"], "out",
                                current_depth + 1, target_path, edge_weight, parent_score
                            ))

                    # Inbound edges
                    in_sql = """SELECT n.*, e.relation AS edge_relation, e.weight AS edge_weight
                                FROM edges e JOIN nodes n ON n.id = e.source_id
                                WHERE e.target_id = ?"""
                    in_params = [current_id]
                    if relations:
                        in_sql += f" AND e.relation IN ({','.join('?' * len(relations))})"
                        in_params.extend(relations)

                    for row in conn.execute(in_sql, in_params).fetchall():
                        source = self._row_to_node(row)
                        if source.id not in visited:
                            edge_weight = row["edge_weight"] if row["edge_weight"] else 0.5
                            source_path = current_path + [(current_name, row["edge_relation"])]
                            neighbors.append((
                                source, row["edge_relation"], "in",
                                current_depth + 1, source_path, edge_weight, parent_score
                            ))

                    # Score all neighbors with heuristic (fast, free)
                    for node, relation, direction, hop_depth, path, edge_weight, p_score in neighbors:
                        score = self._beam_score_candidate(
                            query, node, relation, edge_weight, p_score,
                            hop_depth, intent, type_boosts, "heuristic",
                            hop_decay=hop_decay,
                        )
                        next_candidates.append((
                            node, relation, direction, hop_depth, path, score
                        ))

                # Sort by heuristic score
                next_candidates.sort(key=lambda x: x[5], reverse=True)

                # Adaptive LLM reranking: only when candidates exceed beam slots
                # (i.e., truncation would happen — the heuristic alone picks winners)
                if len(next_candidates) > beam_width:
                    rerank_count = min(2 * beam_width, len(next_candidates))
                    to_rerank = next_candidates[:rerank_count]
                    rest = next_candidates[rerank_count:]

                    # Rerank via the fast-reasoning LLM reranker (single batched call)
                    rerank_input = [(cand[0], cand[5]) for cand in to_rerank]
                    try:
                        reranked = _rerank_with_cross_encoder(query, rerank_input, config_retrieval)
                        # Map reranked scores back to candidate tuples
                        reranked_map = {node.id: score for node, score in reranked}
                        rescored = []
                        for cand in to_rerank:
                            node_c = cand[0]
                            new_score = reranked_map.get(node_c.id, cand[5])
                            rescored.append((*cand[:5], new_score))
                        rescored.sort(key=lambda x: x[5], reverse=True)
                        next_candidates = rescored + rest
                    except Exception:
                        logger.debug("BEAM reranker failed; using heuristic-only ranking", exc_info=True)

                beam = []

                for node, relation, direction, hop_depth, path, score in next_candidates[:beam_width]:
                    if node.id not in visited and len(results) < max_results:
                        if node.id not in paths:
                            paths[node.id] = path
                        results.append((node, relation, direction, hop_depth, path, score))
                        beam.append((node.id, hop_depth, node.name, score))

        return results

    def _beam_score_candidate(
        self,
        query: str,
        node: 'Node',
        relation: str,
        edge_weight: float,
        parent_score: float,
        hop_depth: int,
        intent: Optional[str],
        type_boosts: Optional[Dict[str, float]],
        scoring_mode: str,
        hop_decay: float = 0.7,
    ) -> float:
        """Score a graph traversal candidate for BEAM selection.

        Heuristic scoring (fast, free):
          - Edge weight (30%): Higher-weight edges are more important
          - Node quality (40%): Confidence, verification status, storage strength
          - Intent alignment (20%): Type boost match
          - Relation selectivity (10%): Rarer relations are more informative

        Args:
            query: Search query
            node: Candidate node
            relation: Edge relation connecting to candidate
            edge_weight: Weight of the connecting edge
            parent_score: Score of the parent node in the beam
            hop_depth: Current depth in traversal
            intent: Query intent classification
            type_boosts: Type -> multiplier map
            scoring_mode: "heuristic" | "llm" | "hybrid"
            hop_decay: Score multiplier per hop (from config, default 0.7)

        Returns:
            Score in [0, 1] range
        """
        # Base: parent score decayed by one hop (parent already contains prior decay)
        base = parent_score * hop_decay

        if scoring_mode == "heuristic" or scoring_mode == "hybrid":
            # Edge weight component (0-1, default 0.5 for unweighted edges)
            edge_score = min(1.0, edge_weight)

            # Node quality component
            quality = 0.0
            quality += node.confidence * 0.5
            quality += 0.2 if node.verified else 0.0
            quality += min(0.2, node.storage_strength * 0.02)
            quality += 0.1 if node.status == "active" else 0.0

            # Intent alignment
            intent_score = 0.5  # neutral default
            if type_boosts and node.type in type_boosts:
                intent_score = min(1.0, type_boosts[node.type] / 1.3)  # Normalize

            # Relation selectivity (common relations score lower)
            _COMMON_RELATIONS = {"related_to", "associated_with", "mentioned_in"}
            relation_score = 0.3 if relation in _COMMON_RELATIONS else 0.7

            # Weighted combination
            heuristic = (
                0.30 * edge_score +
                0.40 * quality +
                0.20 * intent_score +
                0.10 * relation_score
            )

            score = base * heuristic

            return min(1.0, score)

        elif scoring_mode == "llm":
            # LLM-scored: use fast-reasoning model to evaluate relevance
            try:
                from lib.llm_clients import call_fast_reasoning
                prompt = (
                    f"Given the query: \"{query}\"\n"
                    f"Rate how relevant this memory graph node is as a traversal candidate (0-5):\n"
                    f"Node: \"{node.name}\" (type: {node.type})\n"
                    f"Connected via: {relation}\n"
                    f"Respond with just a number 0-5."
                )
                response, _ = call_fast_reasoning(prompt, max_tokens=10, timeout=5.0)
                if response:
                    import re
                    m = re.search(r'(\d)', response.strip())
                    if m:
                        llm_score = min(int(m.group(1)), 5) / 5.0
                        return min(1.0, base * llm_score)
            except Exception:
                logger.debug("BEAM llm scoring failed; falling back to heuristic", exc_info=True)
            # Fallback to heuristic on LLM failure
            return self._beam_score_candidate(
                query, node, relation, edge_weight, parent_score,
                hop_depth, intent, type_boosts, "heuristic",
                hop_decay=hop_decay,
            )

        return base * 0.5  # Unknown mode fallback

    # ==========================================================================
    # Entity Alias Operations
    # ==========================================================================

    def add_alias(self, alias: str, canonical_name: str, canonical_node_id: str = None, owner_id: str = None) -> str:
        """Add an entity alias mapping."""
        alias_id = str(uuid.uuid4())
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO entity_aliases
                (id, alias, canonical_name, canonical_node_id, owner_id)
                VALUES (?, ?, ?, ?, ?)
            """, (alias_id, alias.lower().strip(), canonical_name, canonical_node_id, owner_id))
        return alias_id

    def resolve_alias(self, text: str, owner_id: str = None) -> str:
        """Resolve entity aliases in text. Returns text with aliases replaced by canonical names."""
        with self._get_conn() as conn:
            query = "SELECT alias, canonical_name FROM entity_aliases"
            params = []
            if owner_id:
                query += " WHERE owner_id = ? OR owner_id IS NULL"
                params.append(owner_id)
            rows = conn.execute(query, params).fetchall()

        if not rows:
            return text

        # Sort by alias length descending (longest match first)
        aliases = sorted(rows, key=lambda r: len(r['alias']), reverse=True)
        result = text
        for row in aliases:
            # Word-boundary replacement (case-insensitive)
            pattern = r'\b' + re.escape(row['alias']) + r'\b'
            result = re.sub(pattern, row['canonical_name'], result, flags=re.IGNORECASE)
        return result

    def get_aliases(self, owner_id: str = None) -> list:
        """List all aliases."""
        with self._get_conn() as conn:
            query = "SELECT * FROM entity_aliases"
            params = []
            if owner_id:
                query += " WHERE owner_id = ? OR owner_id IS NULL"
                params.append(owner_id)
            query += " ORDER BY alias"
            return conn.execute(query, params).fetchall()

    def delete_alias(self, alias_id: str) -> bool:
        """Delete an alias by ID."""
        with self._get_conn() as conn:
            result = conn.execute("DELETE FROM entity_aliases WHERE id = ?", (alias_id,))
            return result.rowcount > 0

    # ==========================================================================
    # Stats
    # ==========================================================================

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        last_janitor_completed_at = ""
        with self._get_conn() as conn:
            total_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            type_counts = dict(conn.execute(
                "SELECT type, COUNT(*) FROM nodes GROUP BY type"
            ).fetchall())
            verified_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE verified = 1"
            ).fetchone()[0]
            status_counts = dict(conn.execute(
                "SELECT status, COUNT(*) FROM nodes GROUP BY status"
            ).fetchall())
            try:
                row = conn.execute(
                    "SELECT MAX(completed_at) AS completed_at FROM janitor_runs WHERE status = 'completed'"
                ).fetchone()
                if row and row["completed_at"]:
                    last_janitor_completed_at = str(row["completed_at"])
            except Exception:
                pass

        return {
            "total_nodes": total_count,
            "edges": edge_count,
            "by_type": type_counts,
            "by_status": status_counts,
            "verified": verified_count,
            "unverified": total_count - verified_count,
            "last_janitor_completed_at": last_janitor_completed_at,
        }

    def get_health_metrics(self) -> Dict[str, Any]:
        """Get detailed knowledge base health metrics."""
        with self._get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

            # Confidence stats per status
            confidence_stats = {}
            for row in conn.execute("""
                SELECT status, COUNT(*) as cnt,
                       ROUND(AVG(confidence), 3) as avg_conf,
                       ROUND(MIN(confidence), 3) as min_conf,
                       ROUND(MAX(confidence), 3) as max_conf
                FROM nodes GROUP BY status
            """).fetchall():
                confidence_stats[row["status"] or "unknown"] = {
                    "count": row["cnt"],
                    "avg_confidence": row["avg_conf"],
                    "min_confidence": row["min_conf"],
                    "max_confidence": row["max_conf"],
                }

            # Embedding coverage
            with_embedding = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL"
            ).fetchone()[0]

            # Content hash coverage
            with_hash = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE content_hash IS NOT NULL"
            ).fetchone()[0]

            # Superseded facts
            superseded = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]

            # Orphan nodes (no edges at all)
            orphans = conn.execute("""
                SELECT COUNT(*) FROM nodes n
                WHERE NOT EXISTS (SELECT 1 FROM edges e WHERE e.source_id = n.id OR e.target_id = n.id)
            """).fetchone()[0]

            # Staleness distribution (days since accessed)
            staleness = {"0-7d": 0, "7-30d": 0, "30-90d": 0, "90d+": 0, "never": 0}
            for row in conn.execute("SELECT accessed_at FROM nodes").fetchall():
                if not row["accessed_at"]:
                    staleness["never"] += 1
                    continue
                try:
                    accessed = datetime.fromisoformat(row["accessed_at"].replace('Z', '+00:00'))
                    days = (datetime.now(accessed.tzinfo) - accessed).days if accessed.tzinfo else (datetime.now() - accessed).days
                    if days <= 7: staleness["0-7d"] += 1
                    elif days <= 30: staleness["7-30d"] += 1
                    elif days <= 90: staleness["30-90d"] += 1
                    else: staleness["90d+"] += 1
                except (ValueError, TypeError):
                    staleness["never"] += 1

            # Top edge types
            top_relations = dict(conn.execute("""
                SELECT relation, COUNT(*) as cnt FROM edges
                GROUP BY relation ORDER BY cnt DESC LIMIT 10
            """).fetchall())

            # Dedup log stats
            dedup_stats = {}
            try:
                for row in conn.execute("""
                    SELECT decision, COUNT(*) as cnt FROM dedup_log
                    GROUP BY decision
                """).fetchall():
                    dedup_stats[row["decision"]] = row["cnt"]
            except Exception:
                pass

            # Embedding cache size
            cache_size = 0
            try:
                cache_size = conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0]
            except Exception:
                pass

        return {
            "total_nodes": total,
            "total_edges": edge_count,
            "confidence_by_status": confidence_stats,
            "embedding_coverage": f"{with_embedding}/{total}" if total else "0/0",
            "content_hash_coverage": f"{with_hash}/{total}" if total else "0/0",
            "superseded_facts": superseded,
            "orphan_nodes": orphans,
            "staleness_distribution": staleness,
            "top_edge_types": top_relations,
            "dedup_log": dedup_stats,
            "embedding_cache_size": cache_size,
        }


# ==========================================================================
# Graph-Aware Search Helpers
# ==========================================================================

# Pronouns that indicate the query is about the owner
_OWNER_PRONOUNS = {"my", "mine", "our", "ours", "me", "i", "we", "i'm", "i've", "i'd"}

# Family-related edge types for priority expansion
_FAMILY_RELATIONS = {
    "parent_of", "sibling_of", "spouse_of", "family_of", "has_pet",
    "friend_of", "child_of", "knows", "related_to"
}

# Cached edge keywords (loaded once, refreshed on demand)
_edge_keywords_cache: Optional[Dict[str, set]] = None
_edge_keywords_all: Optional[set] = None  # Flattened set of all keywords


def get_edge_keywords() -> Dict[str, List[str]]:
    """Get all edge keywords from database.

    Returns:
        Dict mapping relation type to list of trigger keywords.
    """
    graph = get_graph()
    with graph._get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT relation, keywords FROM edge_keywords"
            ).fetchall()
            result = {}
            for row in rows:
                try:
                    parsed = json.loads(row["keywords"])
                    if not isinstance(parsed, list):
                        raise ValueError("keywords payload must be a JSON array")
                    keywords = [str(k).strip() for k in parsed if str(k).strip()]
                    result[row["relation"]] = keywords
                except Exception as exc:
                    logger.warning(
                        "[memory_graph] invalid edge_keywords payload relation=%s error=%s",
                        row["relation"],
                        exc,
                    )
                    result[row["relation"]] = []
            return result
        except Exception:
            return {}


def get_all_edge_keywords_flat() -> set:
    """Get flattened set of all edge keywords for fast lookup.

    Uses cache to avoid repeated DB queries.
    """
    global _edge_keywords_all
    if _edge_keywords_all is not None:
        return _edge_keywords_all

    keywords = get_edge_keywords()
    # Build in local var to avoid exposing partially-populated set to other threads
    result = set()
    for kw_list in keywords.values():
        result.update(kw.lower() for kw in kw_list)
    _edge_keywords_all = result  # Single atomic assignment
    return _edge_keywords_all


def invalidate_edge_keywords_cache():
    """Clear the edge keywords cache (call after adding new keywords)."""
    global _edge_keywords_cache, _edge_keywords_all
    _edge_keywords_cache = None
    _edge_keywords_all = None


def store_edge_keywords(relation: str, keywords: List[str], description: str = "") -> bool:
    """Store keywords for an edge relation type.

    Args:
        relation: The edge relation type (e.g., "sibling_of")
        keywords: List of trigger keywords (e.g., ["sister", "brother", "sibling"])
        description: Optional human-readable description

    Returns:
        True if stored successfully.
    """
    graph = get_graph()
    with graph._get_conn() as conn:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO edge_keywords (relation, keywords, description, updated_at)
                VALUES (?, ?, ?, datetime('now'))
            """, (relation, json.dumps(keywords), description))
            invalidate_edge_keywords_cache()
            return True
        except Exception as e:
            print(f"[edge_keywords] Failed to store keywords for {relation}: {e}", file=sys.stderr)
            return False


def generate_keywords_for_relation(relation: str) -> Optional[List[str]]:
    """Use LLM to generate trigger keywords for a relation type.

    Args:
        relation: The edge relation type (e.g., "sibling_of")

    Returns:
        List of keywords, or None if generation failed.
    """
    if not _HAS_LLM_CLIENTS:
        return None

    prompt = f"""Given the knowledge graph edge type "{relation}", generate a list of keywords that would appear in a user's natural language query when they want information related to this relationship.

Examples:
- "sibling_of" → ["sister", "brother", "sibling", "siblings", "sis", "bro"]
- "parent_of" → ["parent", "mom", "dad", "mother", "father", "parents", "mama", "papa"]
- "has_pet" → ["pet", "dog", "cat", "puppy", "kitten", "animal", "pets"]
- "lives_in" → ["live", "lives", "living", "home", "house", "apartment", "address", "residence"]
- "works_at" → ["work", "works", "job", "employer", "company", "office"]

Now generate keywords for "{relation}".
Return ONLY a JSON array of lowercase keywords, nothing else.
Example output: ["keyword1", "keyword2", "keyword3"]"""

    try:
        from lib.llm_clients import call_fast_reasoning, parse_json_response
        response, _ = call_fast_reasoning(prompt, max_tokens=200, timeout=30)
        if not response:
            return None

        parsed = parse_json_response(response)
        if isinstance(parsed, list):
            # Ensure all items are strings and lowercase
            return [str(kw).lower().strip() for kw in parsed if kw]
        return None
    except Exception as e:
        print(f"[edge_keywords] LLM generation failed for {relation}: {e}", file=sys.stderr)
        return None


def ensure_keywords_for_relation(relation: str) -> bool:
    """Ensure keywords exist for a relation, generating if needed.

    Args:
        relation: The edge relation type

    Returns:
        True if keywords exist (or were generated), False otherwise.
    """
    # Check if keywords already exist
    existing = get_edge_keywords()
    if relation in existing and existing[relation]:
        return True

    # Generate keywords
    keywords = generate_keywords_for_relation(relation)
    if keywords:
        store_edge_keywords(relation, keywords, f"Auto-generated for {relation}")
        print(f"[edge_keywords] Generated keywords for '{relation}': {keywords}", file=sys.stderr)
        return True

    return False


def should_expand_graph(query: str) -> bool:
    """Fast check: does this query benefit from graph expansion?

    Checks if query contains any keywords associated with edge relations.
    Also checks for pronouns + known person names.

    Args:
        query: The user's query

    Returns:
        True if graph expansion would likely be beneficial.
    """
    query_lower = query.lower()
    words = set(re.sub(r'[^\w\s]', '', query_lower).split())

    # 1. Check against edge keywords (fast set intersection)
    all_keywords = get_all_edge_keywords_flat()
    if words & all_keywords:
        return True

    # 2. Check for pronouns + known person names
    # "I'm meeting Jane" - is Jane a Person node?
    if has_owner_pronoun(query):
        # Extract capitalized words (potential names)
        names = re.findall(r'\b[A-Z][a-z]+\b', query)
        if names:
            graph = get_graph()
            for name in names[:5]:  # Limit lookups
                node = graph.find_node_by_name(name, type="Person")
                if node:
                    return True

    return False


def seed_edge_keywords_from_db() -> int:
    """Seed keywords for all existing edge relations in the database.

    Returns:
        Number of relations that got keywords generated.
    """
    graph = get_graph()

    # Get all unique relations from edges table
    relations = graph.get_known_relations()

    # Get existing keywords
    existing = get_edge_keywords()

    seeded = 0
    for relation in relations:
        if relation in existing and existing[relation]:
            continue  # Already has keywords

        # Skip internal relations
        if relation in ("has_fact",):
            continue

        if ensure_keywords_for_relation(relation):
            seeded += 1

    return seeded


def resolve_owner_person(owner_id: str) -> Optional[Node]:
    """Map owner_id to their Person node using config mapping.

    Args:
        owner_id: The owner identifier (e.g., "alice")

    Returns:
        The Person node for that owner, or None if not found.
    """
    if not _HAS_CONFIG:
        return None

    graph = get_graph()
    cfg = _get_memory_config()
    identity = cfg.users.identities.get(owner_id)

    # Primary: explicit configured person node name.
    if identity and identity.person_node_name:
        node = graph.find_node_by_name(identity.person_node_name, type="Person")
        if node:
            return node
        # Fallback: allow shortened variant (e.g., "Douglas Quaid" -> "Quaid").
        parts = [p.strip() for p in identity.person_node_name.split() if p.strip()]
        for candidate in reversed(parts):
            node = graph.find_node_by_name(candidate, type="Person")
            if node:
                return node

    # Fallback: owner id itself may map to person name in graph.
    for candidate in (owner_id, owner_id.replace("_", " ").title()):
        node = graph.find_node_by_name(candidate, type="Person")
        if node:
            return node

    return None


def _get_owner_names() -> set:
    """Get the owner's name variants for pronoun resolution."""
    if not _HAS_CONFIG:
        return set()
    try:
        cfg = _get_memory_config()
        names = set()
        for identity in cfg.users.identities.values():
            if identity.person_node_name:
                # Add full name and each part: "Alice Smith" -> {"alice", "smith", "alice smith"}
                full = identity.person_node_name.lower()
                names.add(full)
                names.update(full.split())
        return names
    except Exception:
        return set()


def has_owner_pronoun(query: str) -> bool:
    """Check if query contains pronouns or the owner's name."""
    # Strip punctuation and possessives ('s)
    cleaned = re.sub(r"'s\b", "", query.lower())
    words = set(re.sub(r'[^\w\s]', '', cleaned).split())
    if words & _OWNER_PRONOUNS:
        return True
    # Also check if the owner's name appears in the query
    owner_names = _get_owner_names()
    return bool(words & owner_names)


def extract_entities_from_text(text: str) -> List[Node]:
    """Extract entity names from text and look them up in the graph.

    Uses simple heuristics: capitalized words (proper nouns) that exist
    as Person/Place/Entity nodes in the database.

    Args:
        text: Text to extract entities from

    Returns:
        List of matched Node objects
    """
    graph = get_graph()

    # Extract capitalized words (potential proper nouns)
    # Skip common stopwords and short words
    words = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text)

    # Also get single capitalized words
    single_caps = re.findall(r'\b[A-Z][a-z]{2,}\b', text)
    words.extend(single_caps)

    # Dedupe while preserving order
    seen = set()
    unique_words = []
    for w in words:
        if w.lower() not in seen and w.lower() not in _LIB_STOPWORDS:
            seen.add(w.lower())
            unique_words.append(w)

    # Look up each candidate — exact match first, then prefix match on entity types
    entities = []
    for word in unique_words[:10]:  # Limit to prevent excessive lookups
        node = graph.find_node_by_name(word)
        if not node:
            # Case-insensitive exact, then prefix match on entity types
            with graph._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM nodes WHERE LOWER(name) = LOWER(?) AND type IN ('Person', 'Place', 'Pet', 'Entity', 'Concept') LIMIT 1",
                    (word,)
                ).fetchone()
                if not row:
                    row = conn.execute(
                        "SELECT * FROM nodes WHERE name LIKE ? AND type IN ('Person', 'Place', 'Pet', 'Organization') ORDER BY LENGTH(name) LIMIT 1",
                        (word + "%",)
                    ).fetchone()
                if row:
                    node = graph._row_to_node(row)
        if node and node.type in ("Person", "Place", "Entity", "Pet", "Concept"):
            entities.append(node)

    return entities


def graph_aware_recall(
    query: str,
    owner_id: str = None,
    limit: int = 5,
    min_similarity: float = 0.60,
    graph_depth: int = 1,
    technical_scope: str = "any",
    project: Optional[str] = None,
) -> Dict[str, Any]:
    """Combined search: vector search + pronoun resolution + bidirectional graph expansion.

    Args:
        query: Search query
        owner_id: Owner identifier for pronoun resolution
        limit: Maximum results to return
        min_similarity: Minimum similarity threshold for vector search

    Returns:
        Dict with direct_results, graph_results, entities_found, source_breakdown
    """
    if owner_id is None:
        from config import get_config
        owner_id = get_config().users.default_owner
    graph = get_graph()

    results = {
        "direct_results": [],      # Vector search hits
        "graph_results": [],       # From graph expansion
        "entities_found": [],      # Discovered entity nodes
        "source_breakdown": {
            "vector_count": 0,
            "graph_count": 0,
            "pronoun_resolved": False,
            "owner_person": None
        }
    }

    expand_from: List[str] = []  # Node IDs to expand from
    seen_ids: set = set()

    # Determine which relations to expand based on query
    query_lower = query.lower()
    if any(kw in query_lower for kw in ["family", "parent", "mom", "dad", "sister", "brother", "sibling", "child", "kids", "relative", "nephew", "niece", "uncle", "aunt", "cousin"]):
        # Family-focused query: only expand family relations
        expand_relations = ["parent_of", "sibling_of", "spouse_of", "family_of", "child_of", "related_to"]
    elif any(kw in query_lower for kw in ["pet", "dog", "cat", "animal"]):
        # Pet-focused query
        expand_relations = ["has_pet"]
    elif any(kw in query_lower for kw in ["friend", "contact", "know"]):
        # Social-focused query
        expand_relations = ["friend_of", "knows", "colleague_of"]
    elif any(kw in query_lower for kw in ["why", "reason", "cause", "because", "how come", "led to", "due to"]):
        # Causal query: traverse causal edges (deeper for multi-hop explanations)
        expand_relations = ["caused_by", "led_to", "resulted_in"]
        graph_depth = max(graph_depth, 2)  # Causal chains are inherently multi-hop
    else:
        # General query: all relations but limit results
        expand_relations = None  # No filter, but we'll limit below

    # 1. Pronoun resolution
    if has_owner_pronoun(query):
        owner_person = resolve_owner_person(owner_id)
        if owner_person:
            expand_from.append(owner_person.id)
            results["source_breakdown"]["pronoun_resolved"] = True
            results["source_breakdown"]["owner_person"] = owner_person.name

    # 2. Vector search (fact-only): keep direct hits strictly factual, then
    # combine with graph traversal discoveries in this graph-aware pathway.
    direct_all = recall(
        query,
        limit=limit * 3,
        owner_id=owner_id,
        min_similarity=min_similarity,
        technical_scope=technical_scope,
        project=project,
    )
    direct = [r for r in direct_all if str(r.get("category", "")).lower() == "fact"]
    results["direct_results"] = direct[:limit]  # Ensure limit is respected
    results["source_breakdown"]["vector_count"] = len(results["direct_results"])

    # 3. For each Fact, find associated Person via reverse edge traversal
    # Only add Fact IDs to seen_ids, not Person/Entity nodes that might appear in results
    for fact in direct:
        fact_id = fact.get("id")
        fact_category = fact.get("category", "").lower()
        if fact_id and fact_category == "fact":
            seen_ids.add(fact_id)
            # Check for inbound has_fact edges
            in_edges = graph.get_edges(fact_id, direction="in")
            for edge in in_edges:
                if edge.relation == "has_fact":
                    person = graph.get_node(edge.source_id)
                    if person and person.type == "Person":
                        expand_from.append(person.id)

    # 4. Extract entities from query
    query_entities = extract_entities_from_text(query)
    for entity in query_entities:
        expand_from.append(entity.id)
        results["entities_found"].append({
            "id": entity.id,
            "name": entity.name,
            "type": entity.type
        })

    # If we still don't have an anchor node, infer one from direct fact subjects
    # (e.g., "Quaid has sisters..." -> anchor "Quaid").
    if not expand_from and direct:
        inferred_names = []
        for fact in direct:
            text = str(fact.get("text", "")).strip()
            m = re.match(r"^([A-Z][A-Za-z0-9'_-]*(?:\s+[A-Z][A-Za-z0-9'_-]*){0,2})\b", text)
            if m:
                inferred_names.append(m.group(1))
        for name in inferred_names:
            node = graph.find_node_by_name(name, type="Person")
            if node:
                expand_from.append(node.id)
                break

    # 5. Bidirectional graph expansion with relation filtering
    max_graph_results = limit * 2  # Cap graph results
    for node_id in set(expand_from):
        if node_id in seen_ids:
            continue

        # Get related nodes bidirectionally
        related = graph.get_related_bidirectional(node_id, relations=expand_relations, depth=graph_depth)

        for node, relation, direction, depth, path in related:
            if node.id not in seen_ids:
                seen_ids.add(node.id)

                # Get source node name for display
                source_node = graph.get_node(node_id)
                source_name = source_node.name if source_node else "?"

                # Build graph_path string from traversal chain
                if path:
                    path_parts = []
                    for from_name, rel in path:
                        path_parts.append(f"{from_name} --{rel}-->")
                    graph_path = " ".join(path_parts) + " " + node.name
                else:
                    graph_path = f"{source_name} --{relation}--> {node.name}"

                results["graph_results"].append({
                    "id": node.id,
                    "name": node.name,
                    "type": node.type,
                    "relation": relation,
                    "direction": direction,
                    "depth": depth,
                    "source_name": source_name,
                    "via": "graph",
                    "graph_path": graph_path,
                })

                # Limit graph results
                if len(results["graph_results"]) >= max_graph_results:
                    break

        if len(results["graph_results"]) >= max_graph_results:
            break

    results["source_breakdown"]["graph_count"] = len(results["graph_results"])

    return results


# ==========================================================================
# High-level API for Clawdbot integration
# ==========================================================================

_graph: Optional[MemoryGraph] = None
_graph_lock = threading.Lock()

def get_graph() -> MemoryGraph:
    """Get singleton graph instance."""
    global _graph
    with _graph_lock:
        if _graph is None:
            _graph = MemoryGraph()
        return _graph


def stats() -> Dict[str, Any]:
    """Datastore stats interface for API/core callers."""
    return get_graph().get_stats()


def search(
    query: str,
    limit: int = 10,
    owner_id: Optional[str] = None,
    source_channel: Optional[str] = None,
    source_conversation_id: Optional[str] = None,
    source_author_id: Optional[str] = None,
    subject_entity_id: Optional[str] = None,
    participant_entity_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Datastore search interface for API/core callers."""
    if not query or not query.strip():
        return []

    def _participants(raw: Any) -> set[str]:
        if isinstance(raw, list):
            return {str(v).strip() for v in raw if str(v).strip()}
        if isinstance(raw, str):
            txt = raw.strip()
            if not txt:
                return set()
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, list):
                    return {str(v).strip() for v in parsed if str(v).strip()}
            except Exception:
                pass
            return {p.strip() for p in txt.split(",") if p.strip()}
        return set()

    graph = get_graph()
    raw = graph.search_hybrid(query, limit=limit, owner_id=owner_id)
    out: List[Dict[str, Any]] = []
    source_channel_norm = str(source_channel or "").strip().lower() or None
    source_conversation_norm = str(source_conversation_id or "").strip() or None
    source_author_norm = str(source_author_id or "").strip() or None
    subject_entity_norm = str(subject_entity_id or "").strip() or None
    requested_participants = _participants(participant_entity_ids)
    for node, score in raw:
        attrs = node.attributes if isinstance(node.attributes, dict) else {}
        row_source_channel = str(attrs.get("source_channel") or "").strip().lower() or None
        row_source_conversation = (
            str(attrs.get("source_conversation_id") or attrs.get("conversation_id") or node.conversation_id or "").strip()
            or None
        )
        row_source_author = str(attrs.get("source_author_id") or "").strip() or None
        row_subject_entity = str(attrs.get("subject_entity_id") or "").strip() or None
        row_participants = _participants(attrs.get("participant_entity_ids"))

        if source_channel_norm and row_source_channel != source_channel_norm:
            continue
        if source_conversation_norm and row_source_conversation != source_conversation_norm:
            continue
        if source_author_norm and row_source_author != source_author_norm:
            continue
        if subject_entity_norm and row_subject_entity != subject_entity_norm:
            continue
        if requested_participants and requested_participants.isdisjoint(row_participants):
            continue

        out.append(
            {
                "id": node.id,
                "text": node.name,
                "category": node.type,
                "similarity": round(score, 4),
                "confidence": node.confidence,
                "owner_id": node.owner_id,
                "created_at": node.created_at,
                "source_channel": row_source_channel,
                "source_conversation_id": row_source_conversation,
                "source_author_id": row_source_author,
                "speaker_entity_id": str(attrs.get("speaker_entity_id") or node.speaker_entity_id or "").strip() or None,
                "subject_entity_id": row_subject_entity,
                "conversation_id": str(attrs.get("conversation_id") or node.conversation_id or "").strip() or None,
                "visibility_scope": str(attrs.get("visibility_scope") or node.visibility_scope or "source_shared").strip(),
                "sensitivity": str(attrs.get("sensitivity") or node.sensitivity or "normal").strip(),
                "provenance_confidence": attrs.get("provenance_confidence", node.provenance_confidence),
                "participant_entity_ids": sorted(row_participants) if row_participants else [],
            }
        )
    return out


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register memory datastore lifecycle maintenance routines."""

    def _run_datastore_cleanup(ctx):
        result = result_factory()
        graph = ctx.graph or get_graph()
        cleanup_stats = {
            "recall_log": 0,
            "dedup_log": 0,
            "embedding_cache": 0,
            "health_snapshots": 0,
            "janitor_metadata": 0,
            "janitor_runs": 0,
        }
        cleanup_queries = {
            "recall_log": "DELETE FROM recall_log WHERE created_at < datetime('now', '-90 days')",
            "dedup_log": "DELETE FROM dedup_log WHERE review_status != 'unreviewed' AND created_at < datetime('now', '-90 days')",
            "health_snapshots": "DELETE FROM health_snapshots WHERE created_at < datetime('now', '-180 days')",
            "embedding_cache": "DELETE FROM embedding_cache WHERE created_at < datetime('now', '-30 days')",
            "janitor_metadata": "DELETE FROM janitor_metadata WHERE updated_at < datetime('now', '-180 days')",
            "janitor_runs": "DELETE FROM janitor_runs WHERE completed_at < datetime('now', '-180 days')",
        }
        try:
            with graph._get_conn() as conn:
                for table, sql in cleanup_queries.items():
                    if ctx.dry_run:
                        count_sql = sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)
                        row = conn.execute(count_sql).fetchone()
                        cleanup_stats[table] = row[0] if row else 0
                    else:
                        cur = conn.execute(sql)
                        cleanup_stats[table] = cur.rowcount

            total = sum(cleanup_stats.values())
            result.logs.append(f"{'Would remove' if ctx.dry_run else 'Removed'}: {total} rows total")
            for table, count in cleanup_stats.items():
                if count > 0:
                    result.logs.append(f"  {table}: {count}")
            result.data["cleanup"] = cleanup_stats
        except Exception as exc:
            result.errors.append(f"Cleanup error: {exc}")
        return result

    registry.register("datastore_cleanup", _run_datastore_cleanup)


def _ollama_healthy(timeout: float = 0.2) -> bool:
    """Fast health check — can Ollama respond within timeout?

    Hits /api/tags (lightest endpoint). Caches result for 30s to avoid
    per-recall overhead. Returns False if Ollama is unreachable.
    """
    # In test environments with mock embeddings, always report healthy
    # (search_hybrid handles mock embeddings internally)
    import os as _os
    if _os.environ.get("MOCK_EMBEDDINGS"):
        return True
    import time as _time
    now = _time.monotonic()
    # Cache: (timestamp, result)
    if hasattr(_ollama_healthy, "_cache"):
        cache = _ollama_healthy._cache
        if isinstance(cache, tuple) and len(cache) == 2:
            ts, result = cache
            if now - ts < 30:
                return bool(result)
    try:
        url = get_ollama_url()
        req = urllib.request.Request(f"{url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            healthy = resp.status == 200
    except Exception:
        healthy = False
    _ollama_healthy._cache = (now, healthy)
    return healthy


def _is_fail_hard_mode() -> bool:
    """Return whether embedding failures should raise instead of degrading."""
    # Test and mock environments intentionally run without embeddings.
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("MOCK_EMBEDDINGS"):
        return False

    try:
        from lib.fail_policy import is_fail_hard_enabled
        return bool(is_fail_hard_enabled())
    except Exception:
        return True


def route_query(query: str, timeout_ms: int = 3000) -> str:
    """HyDE (Hypothetical Document Embedding) — generate a hypothetical answer
    to the query, then use that for semantic search. The embedding of an answer
    is closer to stored facts in vector space than keywords or the raw question.

    Falls back to original query if LLM unavailable or fails.
    """
    # Skip routing for very short queries (proper nouns, single words)
    if len(query) < 15:
        return query

    prompt = f'Rephrase this question as a declarative statement about someone\'s personal life. Do NOT invent specific names, dates, or places. Keep it general.\n\nQuestion: "{query[:300]}"\n\nStatement:'

    try:
        from lib.llm_clients import call_fast_reasoning
        result, _ = call_fast_reasoning(
            prompt=prompt,
            max_tokens=50,
            timeout=timeout_ms / 1000,
            system_prompt="You rephrase questions as declarative statements. Respond with only the statement, no explanation.",
            max_retries=0,  # Best-effort HyDE — fail fast, don't retry
        )
        if result:
            result = result.strip().strip('"\'')
            if len(result) > 5 and len(result) < 500:
                return result
    except Exception as e:
        if _is_fail_hard_mode():
            raise RuntimeError(
                "HyDE routing failed while fail-hard mode is enabled"
            ) from e

    return query


def _compute_retrieval_difficulty(result: dict) -> float:
    """Compute how hard this memory was to retrieve (Bjork: desirable difficulty).

    Signals:
    - raw similarity score (lower = harder to find via embedding)
    - multi_pass (found only on second pass = harder)
    - via_relation / hop_depth (found via graph traversal = harder)

    Returns difficulty in [0.0, 1.0].
    """
    difficulty = 0.0

    # Similarity-based: continuous function — all retrievals contribute some difficulty,
    # but low-similarity retrievals contribute proportionally more
    sim = result.get("similarity", 0.8)
    difficulty += 0.4 * (1.0 - sim)  # sim=1.0→0.0, sim=0.8→0.08, sim=0.5→0.20

    # Multi-pass: found only on confidence-gated second pass
    if result.get("_multi_pass"):
        difficulty += 0.3

    # Graph traversal: found via edges, not direct search
    hop_depth = result.get("hop_depth", 0)
    if hop_depth > 0:
        difficulty += min(0.3, 0.15 * hop_depth)  # +0.15 per hop, max +0.3

    return min(difficulty, 1.0)


def _compute_composite_score(
    node: Node,
    search_score: float,
    config_retrieval=None,
    intent: str = "GENERAL",
    prefer_fresh: bool = False,
) -> float:
    """Compute composite score mixing search relevance, recency, and frequency.

    Weights: search_relevance=0.60, recency=0.20, frequency=0.15
    Recency: 1.0 for accessed today, decays to 0.0 over 90 days.
    Frequency: log-scaled access_count, capped at 1.0.
    """
    import math

    boost_recent = True
    boost_frequent = True
    w_relevance = 0.60
    w_recency = 0.20
    w_frequency = 0.15
    recency_days = 90
    if config_retrieval:
        boost_recent = config_retrieval.boost_recent
        boost_frequent = config_retrieval.boost_frequent
        w_relevance = getattr(config_retrieval, 'composite_relevance_weight', 0.60)
        w_recency = getattr(config_retrieval, 'composite_recency_weight', 0.20)
        w_frequency = getattr(config_retrieval, 'composite_frequency_weight', 0.15)
        recency_days = getattr(config_retrieval, 'recency_decay_days', 90)

    # Temporal-current queries should favor freshness over legacy popularity.
    if prefer_fresh:
        w_relevance = min(w_relevance, 0.50)
        w_recency = max(w_recency, 0.35)
        w_frequency = min(w_frequency, 0.10)

    # Base search relevance (already 0-1 from quality_score)
    relevance = min(search_score, 1.0)

    # Recency signal
    recency = 0.0
    if boost_recent and node.accessed_at:
        try:
            last_access = datetime.fromisoformat(node.accessed_at.replace('Z', '+00:00'))
            days_ago = (datetime.now(last_access.tzinfo) - last_access).days if last_access.tzinfo else (datetime.now() - last_access).days
            recency = max(0.0, 1.0 - days_ago / max(1, recency_days))
        except (ValueError, TypeError):
            pass

    # Frequency signal (log-scaled, access_count 1->0.0, 10->0.59, 50->1.0)
    frequency = 0.0
    if boost_frequent and node.access_count > 0:
        frequency = min(1.0, math.log1p(node.access_count) / math.log1p(50))

    # Confirmation bonus (independently confirmed facts are more reliable)
    confirmation_bonus = 0.0
    if node.confirmation_count > 0:
        confirmation_bonus = min(0.05, node.confirmation_count * 0.01)

    # Temporal validity penalty
    temporal_penalty = 0.0
    now = datetime.now()
    if node.valid_until:
        try:
            until = datetime.fromisoformat(node.valid_until.replace('Z', '+00:00'))
            until_naive = until.replace(tzinfo=None) if until.tzinfo else until
            if until_naive < now:
                # Expired fact — penalize based on how long ago it expired
                days_expired = (now - until_naive).days
                temporal_penalty = min(0.3, days_expired * 0.01)  # Up to -0.3
        except (ValueError, TypeError):
            pass
    if node.valid_from:
        try:
            vfrom = datetime.fromisoformat(node.valid_from.replace('Z', '+00:00'))
            vfrom_naive = vfrom.replace(tzinfo=None) if vfrom.tzinfo else vfrom
            if vfrom_naive > now:
                temporal_penalty = 0.5  # Future fact — heavy penalty
        except (ValueError, TypeError):
            pass

    # WHEN intent temporal boost: facts with temporal data are more relevant
    temporal_data_bonus = 0.0
    if intent == "WHEN":
        if node.valid_from or node.valid_until or node.created_at:
            temporal_data_bonus = 0.05  # Small bonus for having temporal metadata
        if node.valid_from and node.valid_until:
            temporal_data_bonus = 0.10  # Larger bonus for fully-bounded temporal range

    # Confidence boost (small signal: 0.9 confidence -> +0.045, 0.5 -> +0.025)
    confidence_bonus = node.confidence * 0.05

    # Knowledge type adjustments
    if node.knowledge_type == "belief":
        confidence_bonus *= 0.7  # Beliefs are less authoritative than facts
    elif node.knowledge_type == "preference":
        # Preferences don't decay as fast but are worth less in general recall
        confidence_bonus *= 0.9

    # Storage strength bonus (deeply encoded facts are slightly more reliable)
    storage_bonus = min(0.03, node.storage_strength * 0.003)

    # Weighted composite (weights from config)
    if boost_recent and boost_frequent:
        composite = w_relevance * relevance + w_recency * recency + w_frequency * frequency + confidence_bonus + confirmation_bonus + temporal_data_bonus + storage_bonus
    elif boost_recent:
        composite = (w_relevance + w_frequency) * relevance + (w_recency + 0.05) * recency + confidence_bonus + confirmation_bonus + temporal_data_bonus + storage_bonus
    elif boost_frequent:
        composite = (w_relevance + w_recency) * relevance + w_frequency * frequency + confidence_bonus + confirmation_bonus + temporal_data_bonus + storage_bonus
    else:
        composite = relevance + confidence_bonus + confirmation_bonus + temporal_data_bonus + storage_bonus

    # Apply temporal validity penalty
    if prefer_fresh and temporal_penalty > 0.0:
        temporal_penalty = min(0.6, temporal_penalty * 1.5)
    composite = max(0.0, composite - temporal_penalty)

    return min(composite, 1.0)


def _apply_mmr(results: List[tuple], graph, limit: int, mmr_lambda: float = 0.7) -> List[tuple]:
    """Apply Maximal Marginal Relevance to diversify results.

    MMR(d) = lambda * sim(q, d) - (1-lambda) * max(sim(d, d_i) for d_i in selected)
    Uses embedding cosine similarity between result nodes.
    """
    if len(results) <= 1:
        return results

    selected = [results[0]]  # Always keep the top result
    candidates = list(results[1:])

    while candidates and len(selected) < limit:
        best_mmr = -1.0
        best_idx = 0

        for i, (node, score) in enumerate(candidates):
            # Relevance component
            relevance = score

            # Diversity component: max similarity to any already-selected result
            max_sim_to_selected = 0.0
            if node.embedding:
                for sel_node, _ in selected:
                    if sel_node.embedding:
                        sim = graph.cosine_similarity(node.embedding, sel_node.embedding)
                        max_sim_to_selected = max(max_sim_to_selected, sim)

            mmr = mmr_lambda * relevance - (1 - mmr_lambda) * max_sim_to_selected
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i

        selected.append(candidates.pop(best_idx))

    return selected


# ==========================================================================
# Intent-Aware Query Classification
# ==========================================================================

# Intent categories with regex patterns and type boosts
_INTENT_PATTERNS = {
    "WHO": {
        "patterns": [r"\bwho\b", r"\bwhose\b", r"\bwhom\b", r"person\b", r"people\b", r"family\b", r"friend\b"],
        "type_boosts": {"Person": 1.3, "Fact": 1.0},
    },
    "WHEN": {
        "patterns": [r"\bwhen\b", r"\bdate\b", r"\btime\b", r"\byear\b", r"\bmonth\b", r"\bday\b", r"\bschedule\b", r"\bbirthday\b", r"\banniversary\b"],
        "type_boosts": {"Event": 1.3, "Fact": 1.0},
    },
    "WHERE": {
        "patterns": [r"\bwhere\b", r"\blocation\b", r"\baddress\b", r"\blive[sd]?\b", r"\bcity\b", r"\bcountry\b"],
        "type_boosts": {"Place": 1.3, "Fact": 1.0},
    },
    "WHAT": {
        "patterns": [r"\bwhat\b", r"\bwhich\b", r"\btell me about\b", r"\bdescribe\b"],
        "type_boosts": {},  # No specific boost
    },
    "PREFERENCE": {
        "patterns": [r"\blike[sd]?\b", r"\bprefer[s]?\b", r"\bfavorite\b", r"\bhate[sd]?\b", r"\bavoid\b", r"\benjoy\b"],
        "type_boosts": {"Preference": 1.3},
    },
    "RELATION": {
        "patterns": [r"\brelated to\b", r"\bconnect", r"\brelationship\b", r"\bmarried\b", r"\bspouse\b", r"\bchild\b", r"\bparent\b", r"\bsibling\b", r"\bpet\b"],
        "type_boosts": {"Person": 1.2},
    },
    "WHY": {
        "patterns": [r"\bwhy\b", r"\breason\b", r"\bcause[sd]?\b", r"\bbecause\b", r"\bled to\b", r"\bresult(?:ed)? in\b", r"\bdue to\b", r"\bhow come\b"],
        "type_boosts": {"Event": 1.2, "Fact": 1.1},
    },
    "PROJECT": {
        "patterns": [
            r"\btech\s*stack\b", r"\bdatabase\b", r"\bschema\b", r"\bapi\b", r"\bendpoint\b",
            r"\bmiddleware\b", r"\bframework\b", r"\barchitecture\b", r"\bdeployment\b",
            r"\bimplementation\b", r"\bcode\b", r"\bfunction\b", r"\bfile\b", r"\bmodule\b",
            r"\bpackage\b", r"\blibrary\b", r"\bdependenc", r"\broute\b", r"\bserver\b",
            r"\bfrontend\b", r"\bbackend\b", r"\btesting\b", r"\btest suite\b",
            r"\bbug\b", r"\bfeature\b", r"\brefactor\b", r"\bcommit\b", r"\bbranch\b",
            r"\bproject\b", r"\bapp\b", r"\brecipe app\b", r"\bportfolio\b",
        ],
        "type_boosts": {"Fact": 1.0, "Decision": 1.2},
    },
}


def classify_intent(query: str) -> Tuple[str, Dict[str, float]]:
    """Classify query intent using regex patterns.

    Returns:
        (intent_name, type_boosts) where type_boosts maps node type to multiplier.
    """
    query_lower = query.lower()
    best_intent = "GENERAL"
    best_score = 0
    best_boosts: Dict[str, float] = {}

    for intent_name, config in _INTENT_PATTERNS.items():
        score = sum(1 for p in config["patterns"] if re.search(p, query_lower, re.IGNORECASE))
        if score > best_score:
            best_score = score
            best_intent = intent_name
            best_boosts = config["type_boosts"]

    return best_intent, best_boosts


def _expand_low_signal_query(query: str, intent: str) -> str:
    """Expand under-specified recall queries when first pass yields low signal."""
    q = (query or "").strip()
    if not q:
        return q
    intent = (intent or "GENERAL").upper()
    if intent == "WHEN":
        return f"{q} timeline latest current date before after changed"
    if intent == "PROJECT":
        return f"{q} project implementation code schema api tests migration"
    if intent in {"WHO", "RELATION"}:
        return f"{q} who relationship said suggested source"
    return f"{q} key facts names dates details"


def _get_fusion_weights(intent: Optional[str] = None) -> Tuple[float, float]:
    """Return (vector_weight, fts_weight) based on query intent.

    Entity-heavy queries boost FTS for exact name matching.
    Preference/semantic queries boost vector for meaning matching.
    """
    if intent in ("WHO", "WHERE", "RELATION"):
        return (0.5, 0.5)  # Entity queries — boost FTS (exact name match)
    elif intent == "WHEN":
        return (0.4, 0.6)  # Temporal — strong FTS boost (date strings)
    elif intent == "PREFERENCE":
        return (0.8, 0.2)  # Preferences — semantic meaning matters more
    elif intent == "WHY":
        return (0.8, 0.2)  # Causal — heavily semantic (causal relations are meaning-based)
    elif intent == "PROJECT":
        return (0.6, 0.4)  # Project queries — moderate FTS boost for tech terms
    else:
        return (0.7, 0.3)  # Default unchanged


def _rerank_with_cross_encoder(query: str, results: List[tuple], config_retrieval=None) -> List[tuple]:
    """Rerank results using an LLM for relevance judgments.

    Always uses a single fast-reasoning LLM call path (provider selected by adapter).

    Falls back to original ranking if the model is unavailable.
    """
    if not results:
        return results

    instruction = "Given a personal memory query, determine if this memory is relevant to the query"
    top_k = 20

    if config_retrieval:
        instruction = getattr(config_retrieval, 'reranker_instruction', instruction)
        top_k = getattr(config_retrieval, 'reranker_top_k', top_k)

    # Only rerank top-K candidates
    to_rerank = results[:top_k]
    rest = results[top_k:]

    reranked = _rerank_via_llm(query, to_rerank, instruction, config_retrieval)

    # Sort reranked by blended score
    reranked.sort(key=lambda x: x[1], reverse=True)
    return reranked + rest


def _rerank_via_llm(query: str, candidates: List[tuple], instruction: str, config_retrieval=None) -> List[tuple]:
    """Batch rerank via fast-reasoning LLM call — single call for all candidates."""
    from lib.llm_clients import call_fast_reasoning

    # Build numbered candidate list
    lines = []
    for i, (node, _score) in enumerate(candidates):
        lines.append(f"{i+1}. {node.name}")
    candidate_text = "\n".join(lines)

    prompt = (
        f"{instruction}\n\n"
        f"Query: {query}\n\n"
        f"Documents:\n{candidate_text}\n\n"
        f"Rate each document's relevance to the query on a scale of 0-5:\n"
        f"0=irrelevant, 1=tangential, 2=somewhat relevant, 3=relevant, 4=highly relevant, 5=perfect match\n"
        f"Format: one per line, e.g.:\n1. 4\n2. 0\n3. 5\n"
        f"Respond ONLY with the numbered list."
    )

    try:
        response, _duration = call_fast_reasoning(
            prompt, max_tokens=200, timeout=10,
            system_prompt="You are a relevance judge. Respond ONLY with a numbered list of 0-5 scores."
        )

        if not response:
            return [(node, score) for node, score in candidates]

        # Parse responses: "1. 4\n2. 0\n..."
        verdicts = {}
        for line in response.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            # Try graded score first (0-5)
            m = re.match(r'(\d+)[.\s:)]+\s*(\d+)', line)
            if m:
                verdicts[int(m.group(1))] = min(int(m.group(2)), 5)
                continue
        reranked = []
        inhibited_ids = []  # Track nodes that scored poorly for storage_strength decay
        for i, (node, score) in enumerate(candidates):
            grade = verdicts.get(i + 1)
            if grade is None:
                reranked.append((node, score))
            else:
                rerank_score = min(grade, 5) / 5.0  # Normalize to [0, 1]
                _blend = 0.5
                if config_retrieval:
                    _blend = getattr(config_retrieval, 'reranker_blend', 0.5)
                blended = _blend * rerank_score + (1 - _blend) * score
                reranked.append((node, blended))
                # Competitive inhibition: only inhibit when the item looked relevant
                # (high original score) but the reranker judged it irrelevant.
                # This avoids eroding valid facts that just weren't relevant to THIS query.
                if grade <= 1 and score >= 0.65:
                    inhibited_ids.append(node.id)

        # Apply competitive inhibition — small decrement for losing candidates
        if inhibited_ids:
            try:
                graph = get_graph()
                with graph._get_conn() as conn:
                    for nid in inhibited_ids:
                        conn.execute(
                            "UPDATE nodes SET storage_strength = MAX(storage_strength - 0.02, 0.0) WHERE id = ?",
                            (nid,)
                        )
            except Exception:
                pass  # Inhibition is best-effort

        return reranked
    except Exception:
        return [(node, score) for node, score in candidates]


def _log_recall(graph, query: str, owner_id: Optional[str], intent: str,
                results_count: int, avg_similarity: float, top_similarity: float,
                multi_pass_triggered: bool, fts_fallback_used: bool,
                reranker_used: bool, reranker_changes: int,
                reranker_top1_changed: int, reranker_avg_displacement: float,
                graph_discoveries: int, latency_ms: int):
    """Log recall metrics to recall_log table (best-effort, never raises)."""
    try:
        with graph._get_conn() as conn:
            conn.execute("""
                INSERT INTO recall_log
                    (query, owner_id, intent, results_count, avg_similarity, top_similarity,
                     multi_pass_triggered, fts_fallback_used, reranker_used, reranker_changes,
                     reranker_top1_changed, reranker_avg_displacement,
                     graph_discoveries, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (query, owner_id, intent, results_count,
                  round(avg_similarity, 4), round(top_similarity, 4),
                  1 if multi_pass_triggered else 0,
                  1 if fts_fallback_used else 0,
                  1 if reranker_used else 0,
                  reranker_changes, reranker_top1_changed,
                  round(reranker_avg_displacement, 3) if reranker_avg_displacement else None,
                  graph_discoveries, latency_ms))
    except Exception:
        pass  # Observability logging is strictly best-effort


def _normalize_project_tag(value: Optional[str]) -> Optional[str]:
    """Normalize project/domain labels used for technical memory filtering."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    norm = re.sub(r"[^a-z0-9._/-]+", "-", raw)
    norm = re.sub(r"-{2,}", "-", norm).strip("-")
    return norm[:64] if norm else None


def recall(
    query: str,
    limit: int = 5,
    privacy: Optional[List[str]] = None,
    owner_id: Optional[str] = None,
    min_similarity: Optional[float] = None,
    use_routing: bool = True,
    use_aliases: bool = True,
    use_intent: bool = True,
    use_multi_pass: bool = True,
    use_reranker: Optional[bool] = None,
    current_session_id: Optional[str] = None,
    compaction_time: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    source_channel: Optional[str] = None,
    source_conversation_id: Optional[str] = None,
    source_author_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    subject_entity_id: Optional[str] = None,
    viewer_entity_id: Optional[str] = None,
    participant_entity_ids: Optional[List[str]] = None,
    include_unscoped: bool = True,
    debug: bool = False,
    technical_scope: str = "any",
    project: Optional[str] = None,
    low_signal_retry: bool = True,
) -> List[Dict[str, Any]]:
    """
    Recall memories matching a query.
    Returns list of dicts with text, category, similarity.

    Uses RRF-fused hybrid search, composite scoring (relevance + recency +
    frequency), and MMR diversity to return high-quality, diverse results.

    Args:
        query: Search query
        limit: Max results to return
        privacy: Privacy levels to search
        owner_id: Filter by owner (includes shared/public if set)
        min_similarity: Minimum similarity threshold (None = read from config retrieval.minSimilarity)
        use_routing: Whether to apply HyDE query expansion via LLM before search
        use_aliases: Whether to resolve entity aliases (e.g., Mom → Linda)
        use_intent: Whether to classify query intent for fusion weight tuning
        use_multi_pass: Whether to attempt a second-pass broader search on low-quality results
        use_reranker: Override config reranker_enabled (None = use config)
        date_from: Only return memories created on or after this date (YYYY-MM-DD)
        date_to: Only return memories created on or before this date (YYYY-MM-DD)
        technical_scope: "personal", "technical", or "any" (default)
        project: Optional project/domain label filter (applies to technical results)
    """
    if not query or not query.strip():
        return []

    import time as _time
    _recall_start = _time.monotonic()
    _fts_fallback_used = False
    _multi_pass_triggered = False
    _multi_pass_ids = set()  # Track which node IDs came from second pass (for Bjork difficulty)
    _reranker_changes = 0
    _reranker_top1_changed = 0
    _reranker_avg_displacement = 0.0
    _graph_discoveries = 0

    config_retrieval = None
    try:
        from config import get_config
        config_retrieval = get_config().retrieval
        if min_similarity is None:
            min_similarity = config_retrieval.min_similarity
    except Exception:
        if min_similarity is None:
            min_similarity = 0.60
    if privacy is None:
        privacy = ["private", "shared", "public"]
    graph = get_graph()
    technical_scope = (technical_scope or "any").strip().lower()
    if technical_scope not in {"personal", "technical", "any"}:
        technical_scope = "any"
    requested_project = _normalize_project_tag(project)

    # Strip gateway metadata from query (e.g. "[Telegram User id:...] actual message")
    clean_query = query
    meta_match = re.search(r'\]\s*([^\[].+)$', query, re.DOTALL)
    if meta_match and len(meta_match.group(1).strip()) >= 3:
        clean_query = meta_match.group(1).strip()

    # Resolve entity aliases in query
    if use_aliases:
        clean_query = graph.resolve_alias(clean_query, owner_id=owner_id)

    # Classify query intent BEFORE search — used for fusion weights and type boosting
    if use_intent:
        intent, type_boosts = classify_intent(clean_query)
    else:
        intent = "GENERAL"
        type_boosts = {}
    _temporal_fresh_cues = (
        r"\b(latest|currently|current|now|today|most recent|recently|as of)\b"
    )
    prefer_fresh = bool(re.search(_temporal_fresh_cues, clean_query, re.IGNORECASE))

    # Fast Ollama health check — skip semantic search entirely if Ollama is down
    # Saves ~30s of embedding timeout waits when Ollama is unreachable
    _ollama_up = _ollama_healthy()

    # Search with buffer for composite scoring + MMR selection
    search_limit = limit * 3
    if _ollama_up:
        # Route query through LLM (HyDE) — only when embeddings will be used
        _use_hyde = use_routing
        if not _HAS_LLM_CLIENTS:
            _use_hyde = False
        search_query = route_query(clean_query) if _use_hyde else clean_query
        results = graph.search_hybrid(search_query, limit=search_limit, privacy=privacy, owner_id=owner_id, current_session_id=current_session_id, compaction_time=compaction_time, intent=intent)
    else:
        search_query = clean_query  # No HyDE when embeddings unavailable
        import logging
        msg = "Ollama unreachable — recall falling back to FTS-only"
        logging.getLogger(__name__).warning(msg)
        if _is_fail_hard_mode():
            raise RuntimeError(
                "Embedding provider unavailable during recall. "
                "Fail-hard mode is ON (retrieval.fail_hard=true), "
                "so degraded FTS-only fallback is blocked. "
                "Set retrieval.fail_hard=false to allow fallback, "
                "but this is not recommended because it masks infrastructure faults."
            )
        results = []  # Skip semantic search, go straight to FTS fallback
        _fts_fallback_used = True

    # FTS fallback: if hybrid search returned nothing (Ollama may be down),
    # fall back to keyword-only search so recall isn't completely broken
    if not results:
        fts_fallback = graph.search_fts(clean_query, limit=search_limit, owner_id=owner_id)
        for node, fts_rank in fts_fallback:
            # Estimate quality score from rank position
            quality = max(0.5, 1.0 - fts_rank * 0.02)
            results.append((node, quality))
        if fts_fallback:
            _fts_fallback_used = True

    # Also run FTS on the raw query (before routing) to catch proper nouns
    # that route_query may have dropped or transformed
    elif use_routing and search_query != clean_query:
        raw_fts = graph.search_fts(clean_query, limit=limit, owner_id=owner_id)
        result_ids = {node.id for node, _ in results}
        for node, fts_rank in raw_fts:
            if node.id not in result_ids:
                quality = max(0.5, 1.0 - fts_rank * 0.02)
                results.append((node, quality))
                result_ids.add(node.id)

    # Apply composite scoring (search relevance + recency + frequency + intent boost)
    scored_results = []
    debug_info = {} if debug else None
    for node, quality_score in results:
        composite = _compute_composite_score(
            node,
            quality_score,
            config_retrieval,
            intent=intent,
            prefer_fresh=prefer_fresh,
        )
        # Apply intent-based type boost
        type_boost_applied = 1.0
        if type_boosts and node.type in type_boosts:
            type_boost_applied = type_boosts[node.type]
            composite = min(composite * type_boost_applied, 1.0)
        scored_results.append((node, composite))

        if debug:
            debug_info[node.id] = {
                "raw_quality_score": round(quality_score, 4),
                "composite_score": round(composite, 4),
                "intent": intent,
                "type_boost": type_boost_applied,
                "node_type": node.type,
                "confidence": node.confidence,
                "access_count": node.access_count,
                "confirmation_count": node.confirmation_count,
                "valid_from": node.valid_from,
                "valid_until": node.valid_until,
            }

    # Cross-encoder reranking (if enabled)
    reranker_enabled = False
    if config_retrieval:
        reranker_enabled = getattr(config_retrieval, 'reranker_enabled', False)
    if use_reranker is not None:
        reranker_enabled = use_reranker
    if reranker_enabled:
        try:
            _pre_rerank_order = [node.id for node, _ in scored_results[:20]]
            scored_results = _rerank_with_cross_encoder(clean_query, scored_results, config_retrieval)
            _post_rerank_order = [node.id for node, _ in sorted(scored_results[:20], key=lambda x: x[1], reverse=True)]
            _reranker_changes = sum(1 for i, nid in enumerate(_pre_rerank_order)
                                    if i < len(_post_rerank_order) and _post_rerank_order[i] != nid)
            # Enhanced reranker delta tracking
            if _pre_rerank_order and _post_rerank_order:
                _reranker_top1_changed = 1 if _pre_rerank_order[0] != _post_rerank_order[0] else 0
                # Compute average absolute rank displacement
                post_index_map = {nid: i for i, nid in enumerate(_post_rerank_order)}
                displacements = []
                for pre_i, nid in enumerate(_pre_rerank_order):
                    if nid in post_index_map:
                        displacements.append(abs(pre_i - post_index_map[nid]))
                if displacements:
                    _reranker_avg_displacement = sum(displacements) / len(displacements)
        except Exception:
            pass  # Reranking is best-effort; fall back to original scoring

    # Filter by composite score threshold
    scored_results = [
        (node, score) for node, score in scored_results
        if score >= min_similarity
    ]

    # Sort by composite score
    scored_results.sort(key=lambda x: x[1], reverse=True)

    # Multi-pass retrieval: if top results are low quality, try broader search
    _multi_pass_gate = 0.70
    if config_retrieval:
        _multi_pass_gate = getattr(config_retrieval, 'multi_pass_gate', 0.70)
    if use_multi_pass and scored_results and scored_results[0][1] < _multi_pass_gate and len(scored_results) < limit:
        _multi_pass_triggered = True
        # Extract entity names from query for targeted second pass
        entity_terms = [w for w in clean_query.split() if w[0:1].isupper() and len(w) > 2]
        # Also try individual key terms
        key_terms = _lib_extract_key_tokens(clean_query)

        second_pass_queries = []
        if entity_terms:
            second_pass_queries.append(" ".join(entity_terms))
        if key_terms and key_terms != [clean_query]:
            second_pass_queries.append(" ".join(key_terms[:5]))

        existing_ids = {node.id for node, _ in scored_results}
        for sq in second_pass_queries:
            if sq.strip() and sq.strip() != search_query.strip():
                try:
                    extra = graph.search_hybrid(sq, limit=limit * 2, privacy=privacy, owner_id=owner_id, current_session_id=current_session_id, compaction_time=compaction_time, intent=intent)
                    for node, quality_score in extra:
                        if node.id not in existing_ids:
                            composite = _compute_composite_score(
                                node,
                                quality_score,
                                config_retrieval,
                                intent=intent,
                                prefer_fresh=prefer_fresh,
                            )
                            if type_boosts and node.type in type_boosts:
                                composite = min(composite * type_boosts[node.type], 1.0)
                            if composite >= min_similarity:
                                scored_results.append((node, composite))
                                existing_ids.add(node.id)
                                _multi_pass_ids.add(node.id)
                                if debug and debug_info is not None:
                                    debug_info[node.id] = {
                                        "raw_quality_score": round(quality_score, 4),
                                        "composite_score": round(composite, 4),
                                        "intent": intent,
                                        "type_boost": 1.0,
                                        "node_type": node.type,
                                        "confidence": node.confidence,
                                        "access_count": node.access_count,
                                        "confirmation_count": node.confirmation_count,
                                        "valid_from": node.valid_from,
                                        "valid_until": node.valid_until,
                                        "multi_pass": True,
                                    }
                except Exception:
                    pass  # Second pass is best-effort

        # Re-sort after adding second-pass results
        scored_results.sort(key=lambda x: x[1], reverse=True)

    # Filter by minimum similarity before MMR (first-pass results were unfiltered)
    scored_results = [(node, score) for node, score in scored_results if score >= min_similarity]

    # Apply MMR diversity (select diverse top-N from candidates)
    _mmr_lambda = 0.7
    if config_retrieval:
        _mmr_lambda = getattr(config_retrieval, 'mmr_lambda', 0.7)
    diverse_results = _apply_mmr(scored_results, graph, limit, mmr_lambda=_mmr_lambda)

    output = []
    seen_ids = set()

    for node, score in diverse_results:
        seen_ids.add(node.id)
        _attrs = node.attributes if isinstance(node.attributes, dict) else {}
        result_dict = {
            "text": _sanitize_for_context(node.name),
            "category": node.type.lower(),
            "similarity": round(score, 3),
            "verified": node.verified,
            "pinned": node.pinned,
            "id": node.id,
            "extraction_confidence": node.extraction_confidence,
            "created_at": node.created_at,
            "valid_from": node.valid_from,
            "valid_until": node.valid_until,
            "privacy": node.privacy,
            "owner_id": node.owner_id,
            "_multi_pass": node.id in _multi_pass_ids,
            "is_technical": _attrs.get("is_technical", False),
            "source_type": _attrs.get("source_type"),
            "project": _attrs.get("project"),
            "source_channel": _attrs.get("source_channel"),
            "source_conversation_id": _attrs.get("source_conversation_id"),
            "source_author_id": _attrs.get("source_author_id"),
            "actor_id": _attrs.get("actor_id"),
            "speaker_entity_id": _attrs.get("speaker_entity_id", node.speaker_entity_id),
            "subject_entity_id": _attrs.get("subject_entity_id"),
            "conversation_id": _attrs.get("conversation_id", node.conversation_id),
            "visibility_scope": _attrs.get("visibility_scope", node.visibility_scope),
            "sensitivity": _attrs.get("sensitivity", node.sensitivity),
            "provenance_confidence": _attrs.get("provenance_confidence", node.provenance_confidence),
            "participant_entity_ids": _attrs.get("participant_entity_ids"),
        }
        if debug and debug_info and node.id in debug_info:
            result_dict["_debug"] = debug_info[node.id]
        output.append(result_dict)

    # Temporal contiguity: facts from the same session were co-encoded
    # and likely share context — surface them as bonus candidates
    if diverse_results:
        session_ids = {}
        for node, score in diverse_results[:5]:
            if node.session_id and node.session_id not in session_ids:
                session_ids[node.session_id] = score
        for sid, anchor_score in session_ids.items():
            try:
                with graph._get_conn() as conn:
                    if seen_ids:
                        exclude_clause = "AND id NOT IN ({})".format(",".join("?" for _ in seen_ids))
                        params = [sid] + list(seen_ids)
                    else:
                        exclude_clause = ""
                        params = [sid]
                    rows = conn.execute(
                        "SELECT * FROM nodes WHERE session_id = ? AND status IN ('active', 'approved') {} LIMIT 5".format(
                            exclude_clause
                        ),
                        params
                    ).fetchall()
                    for row in rows:
                        co_node = graph._row_to_node(row)
                        if co_node.id not in seen_ids and (not owner_id or co_node.owner_id == owner_id):
                            seen_ids.add(co_node.id)
                            # Co-encoded facts get a fraction of the anchor's score
                            _co_decay = 0.6
                            if config_retrieval:
                                _co_decay = getattr(config_retrieval, 'co_session_decay', 0.6)
                            co_score = anchor_score * _co_decay
                            # Use a lower threshold for co-session facts (75% of min_similarity)
                            co_threshold = min_similarity * 0.75
                            if co_score >= co_threshold:
                                _co_attrs = co_node.attributes if isinstance(co_node.attributes, dict) else {}
                                output.append({
                                    "text": _sanitize_for_context(co_node.name),
                                    "category": co_node.type.lower(),
                                    "similarity": round(co_score, 3),
                                    "verified": co_node.verified,
                                    "pinned": co_node.pinned,
                                    "id": co_node.id,
                                    "via_relation": "co_session",
                                    "hop_depth": 0,
                                    "is_technical": _co_attrs.get("is_technical", False),
                                    "source_type": _co_attrs.get("source_type"),
                                    "project": _co_attrs.get("project"),
                                })
            except Exception:
                pass  # Temporal contiguity is best-effort

    # Add related nodes via multi-hop graph traversal
    # Uses BEAM search (scored, pruned) or BFS (exhaustive) based on config
    if diverse_results:
        _use_beam = True  # Default matches TraversalConfig.use_beam
        _beam_width = 5
        _traversal_depth = 2
        _hop_decay_factor = 0.7
        if config_retrieval:
            trav = getattr(config_retrieval, 'traversal', None)
            if trav:
                _use_beam = getattr(trav, 'use_beam', True)
                _beam_width = getattr(trav, 'beam_width', 5)
                _traversal_depth = getattr(trav, 'max_depth', 2)
                _hop_decay_factor = getattr(trav, 'hop_decay', 0.7)
            else:
                _use_beam = True  # Default to BEAM

        for node, score in diverse_results[:3]:  # Only traverse top 3
            if _use_beam:
                try:
                    related = graph.beam_search_graph(
                        query=clean_query,
                        start_id=node.id,
                        beam_width=_beam_width,
                        max_depth=_traversal_depth,
                        max_results=10,
                        intent=intent,
                        type_boosts=type_boosts,
                        hop_decay=_hop_decay_factor,
                        config_retrieval=config_retrieval,
                    )
                except Exception:
                    # BEAM failed — fall back to BFS for this node
                    related = []
                    try:
                        bfs_related = graph.get_related_bidirectional(
                            node.id, depth=_traversal_depth, max_results=10
                        )
                        for rel_node, relation, direction, hop_depth, path in bfs_related:
                            hop_decay = _hop_decay_factor ** hop_depth
                            related.append((rel_node, relation, direction, hop_depth, path, hop_decay))
                    except Exception:
                        pass  # Best-effort graph traversal
                for rel_node, relation, direction, hop_depth, path, beam_score in related:
                    if rel_node.id not in seen_ids:
                        seen_ids.add(rel_node.id)
                        _graph_discoveries += 1
                        rel_score = score * beam_score
                        if rel_score < min_similarity:
                            continue

                        if path:
                            path_parts = [f"{fn} --{rel}-->" for fn, rel in path]
                            graph_path = " ".join(path_parts) + " " + rel_node.name
                        else:
                            graph_path = f"{node.name} --{relation}--> {rel_node.name}"

                        _rel_attrs = rel_node.attributes if isinstance(rel_node.attributes, dict) else {}
                        output.append({
                            "text": _sanitize_for_context(f"{node.name} → {relation} → {rel_node.name}"),
                            "category": rel_node.type.lower(),
                            "similarity": round(rel_score, 3),
                            "verified": rel_node.verified,
                            "pinned": rel_node.pinned,
                            "id": rel_node.id,
                            "via_relation": relation,
                            "hop_depth": hop_depth,
                            "graph_path": graph_path,
                            "_multi_pass": rel_node.id in _multi_pass_ids,
                            "is_technical": _rel_attrs.get("is_technical", False),
                            "project": _rel_attrs.get("project"),
                        })
            else:
                # BFS fallback
                related = graph.get_related_bidirectional(
                    node.id, depth=_traversal_depth, max_results=10
                )
                for rel_node, relation, direction, hop_depth, path in related:
                    if rel_node.id not in seen_ids:
                        seen_ids.add(rel_node.id)
                        _graph_discoveries += 1
                        hop_decay = _hop_decay_factor ** hop_depth
                        rel_score = score * hop_decay
                        if rel_score < min_similarity:
                            continue

                        if path:
                            path_parts = [f"{fn} --{rel}-->" for fn, rel in path]
                            graph_path = " ".join(path_parts) + " " + rel_node.name
                        else:
                            graph_path = f"{node.name} --{relation}--> {rel_node.name}"

                        _rel_attrs = rel_node.attributes if isinstance(rel_node.attributes, dict) else {}
                        output.append({
                            "text": _sanitize_for_context(f"{node.name} → {relation} → {rel_node.name}"),
                            "category": rel_node.type.lower(),
                            "similarity": round(rel_score, 3),
                            "verified": rel_node.verified,
                            "pinned": rel_node.pinned,
                            "id": rel_node.id,
                            "via_relation": relation,
                            "hop_depth": hop_depth,
                            "graph_path": graph_path,
                            "_multi_pass": rel_node.id in _multi_pass_ids,
                            "is_technical": _rel_attrs.get("is_technical", False),
                            "project": _rel_attrs.get("project"),
                        })

    # Explicit scope filter for technical vs personal retrieval.
    if technical_scope == "technical":
        output = [r for r in output if bool(r.get("is_technical"))]
    elif technical_scope == "personal":
        output = [r for r in output if not bool(r.get("is_technical"))]
    if requested_project:
        output = [
            r for r in output
            if _normalize_project_tag(r.get("project")) == requested_project
        ]

    # Apply date-range filter BEFORE limit (so we get `limit` results in range)
    if date_from or date_to:
        def _in_date_range(r: Dict[str, Any]) -> bool:
            created = r.get("created_at", "")
            if not created:
                return True  # Include results without dates
            date_part = created.split("T")[0] if "T" in created else created
            if date_from and date_part < date_from:
                return False
            if date_to and date_part > date_to:
                return False
            return True
        output = [r for r in output if _in_date_range(r)]

    # Hide low-information standalone entity nodes (e.g., single names) when
    # we have more informative factual/relational results. Keep as fallback.
    low_info = [r for r in output if _is_low_information_entity_result(r)]
    informative = [r for r in output if not _is_low_information_entity_result(r)]
    if informative:
        output = informative
    else:
        output = low_info

    # Sort by similarity and limit
    scope_filters = {
        "source_channel": source_channel,
        "source_conversation_id": source_conversation_id,
        "source_author_id": source_author_id,
        "actor_id": actor_id,
        "subject_entity_id": subject_entity_id,
    }
    active_scope_filters = {k: v for k, v in scope_filters.items() if v not in (None, "")}
    if active_scope_filters:
        filtered_output = []
        for row in output:
            is_unscoped = all(row.get(k) in (None, "") for k in active_scope_filters.keys())
            if is_unscoped and include_unscoped:
                filtered_output.append(row)
                continue

            matched = True
            for key, expected in active_scope_filters.items():
                actual = row.get(key)
                if actual in (None, ""):
                    matched = False
                    break
                if str(actual) != str(expected):
                    matched = False
                    break
            if matched:
                filtered_output.append(row)
        output = filtered_output

    if participant_entity_ids:
        requested = {str(p).strip() for p in participant_entity_ids if str(p).strip()}
        if requested:
            participant_filtered = []
            for row in output:
                row_participants = row.get("participant_entity_ids")
                if not isinstance(row_participants, list):
                    if include_unscoped:
                        participant_filtered.append(row)
                    continue
                row_set = {str(p).strip() for p in row_participants if str(p).strip()}
                if row_set & requested:
                    participant_filtered.append(row)
            output = participant_filtered

    output.sort(key=lambda x: x["similarity"], reverse=True)
    final_output = output[:limit]

    # Update access stats for returned results (feeds into Ebbinghaus decay)
    if final_output:
        try:
            result_ids = {r["id"] for r in final_output}
            # Track access for both direct search results and graph-traversal results
            result_nodes = [(node, score) for node, score in diverse_results
                            if node.id in result_ids]
            # Also track graph-traversal nodes by looking them up
            tracked_ids = {node.id for node, _ in result_nodes}
            for r in final_output:
                if r["id"] not in tracked_ids and r.get("via_relation"):
                    node = graph.get_node(r["id"])
                    if node:
                        result_nodes.append((node, r["similarity"]))
            # Build retrieval difficulty map for storage strength updates (Bjork model)
            difficulty_map = {r["id"]: _compute_retrieval_difficulty(r) for r in final_output}
            graph._update_access(result_nodes, difficulty_map=difficulty_map)
        except Exception:
            pass  # Access tracking is best-effort

    # Log recall metrics for observability
    _recall_elapsed_ms = int((_time.monotonic() - _recall_start) * 1000)
    _similarities = [r["similarity"] for r in final_output]
    _log_recall(
        graph, query, owner_id, intent,
        results_count=len(final_output),
        avg_similarity=sum(_similarities) / len(_similarities) if _similarities else 0.0,
        top_similarity=max(_similarities) if _similarities else 0.0,
        multi_pass_triggered=_multi_pass_triggered,
        fts_fallback_used=_fts_fallback_used,
        reranker_used=reranker_enabled,
        reranker_changes=_reranker_changes,
        reranker_top1_changed=_reranker_top1_changed,
        reranker_avg_displacement=_reranker_avg_displacement,
        graph_discoveries=_graph_discoveries,
        latency_ms=_recall_elapsed_ms,
    )

    # Low-signal retry: if first pass produced no useful results, retry once with
    # an intent-shaped expanded query. This logic lives in datastore recall so it
    # applies consistently across interfaces, not in harnesses.
    if low_signal_retry:
        low_info_count = sum(1 for r in final_output if _is_low_information_entity_result(r))
        low_signal = (len(final_output) == 0) or (
            len(final_output) <= max(1, limit // 3) and low_info_count == len(final_output)
        )
        if low_signal:
            retry_query = _expand_low_signal_query(clean_query, intent)
            if retry_query and retry_query != clean_query:
                try:
                    retry_output = recall(
                        retry_query,
                        limit=limit,
                        privacy=privacy,
                        owner_id=owner_id,
                        min_similarity=min_similarity,
                        use_routing=use_routing,
                        use_aliases=use_aliases,
                        use_intent=use_intent,
                        use_multi_pass=use_multi_pass,
                        use_reranker=use_reranker,
                        current_session_id=current_session_id,
                        compaction_time=compaction_time,
                        date_from=date_from,
                        date_to=date_to,
                        debug=False,
                        technical_scope=technical_scope,
                        low_signal_retry=False,
                    )
                    if len(retry_output) > len(final_output):
                        return retry_output
                except Exception:
                    pass

    return final_output


def _check_injection_blocklist(text: str) -> Optional[str]:
    """Check text for prompt injection patterns. Returns matched text if flagged, None otherwise."""
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _is_low_information_entity_result(result: Dict[str, Any]) -> bool:
    """True if a result is just an entity stub (e.g. bare person name)."""
    if result.get("via_relation") or result.get("graph_path"):
        return False
    category = str(result.get("category", "")).strip().lower()
    if category not in _LOW_INFO_ENTITY_CATEGORIES:
        return False
    text = " ".join(str(result.get("text", "")).split())
    if not text or "→" in text or "--" in text:
        return False
    words = text.split()
    if len(words) > 2:
        return False
    return _LOW_INFO_ENTITY_TEXT_RE.fullmatch(text) is not None


def _sanitize_for_context(text: str) -> str:
    """Strip potential injection patterns from recalled text (defense-in-depth)."""
    result = text
    for pattern in _INJECTION_PATTERNS:
        result = pattern.sub("[FILTERED]", result)
    return result


def _validate_confidence_unit_interval(value: Any, field_name: str) -> float:
    """Normalize confidence-like values and enforce [0.0, 1.0]."""
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number between 0.0 and 1.0") from exc
    if parsed < 0.0 or parsed > 1.0:
        raise ValueError(f"{field_name} must be between 0.0 and 1.0 (got {parsed})")
    return parsed


def store(
    text: str,
    category: str = "fact",
    verified: bool = False,
    pinned: bool = False,
    confidence: float = 0.5,
    privacy: str = "shared",
    source: Optional[str] = None,
    source_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    session_id: Optional[str] = None,
    fact_type: str = "mutable",
    extraction_confidence: float = 0.5,
    dedup_threshold: float = 0.95,  # Catch near-identical duplicates (was 0.92, raised to avoid false positives on similar-but-different facts)
    update_if_dup: bool = True,
    skip_dedup: bool = False,  # If True, skip deduplication entirely
    speaker: Optional[str] = None,  # Who stated this fact
    status: Optional[str] = None,  # Override default status (pending for auto, approved for manual)
    knowledge_type: str = "fact",  # fact, belief, preference, experience
    keywords: Optional[str] = None,  # Space-separated derived search terms
    source_type: Optional[str] = None,  # user, assistant, tool, import
    target_datastore: Optional[str] = None,  # reserved routing seam (no-op in memorydb)
    is_technical: bool = False,  # technical/project-state memory flag
    project: Optional[str] = None,  # project/domain label for technical facts
    source_channel: Optional[str] = None,  # conversation channel/source (telegram/discord/etc.)
    source_conversation_id: Optional[str] = None,  # stable thread/group identifier
    source_author_id: Optional[str] = None,  # external speaker/author identifier
    speaker_entity_id: Optional[str] = None,  # canonical speaker entity id
    conversation_id: Optional[str] = None,  # canonical conversation/thread identifier
    participant_entity_ids: Optional[List[str]] = None,  # canonical participants for this context
    visibility_scope: Optional[str] = None,  # private_subject/source_shared/global_shared/system
    sensitivity: Optional[str] = None,  # normal/restricted/secret
    provenance_confidence: Optional[float] = None,  # attribution confidence
    actor_id: Optional[str] = None,  # canonical actor entity id
    subject_entity_id: Optional[str] = None,  # canonical subject entity id
    created_at: Optional[str] = None,  # Override created_at timestamp (ISO format)
    accessed_at: Optional[str] = None,  # Override accessed_at timestamp (ISO format)
) -> Dict[str, Any]:
    """
    Store a new memory with deduplication.
    
    Args:
        text: The memory text to store
        category: preference, fact, decision, entity, other
        verified: Whether this is a verified fact
        pinned: Whether this memory is pinned (never decays)
        confidence: Confidence level 0.0-1.0
        privacy: private, shared, public
        source: Source of the memory (e.g., "telegram")
        source_id: Source message ID
        owner_id: Owner identifier for multi-user support
        dedup_threshold: Similarity threshold for duplicate detection (0.85 = very similar)
        update_if_dup: If duplicate found, update it instead of skipping
    
    Returns:
        Dict with: id, status ("created", "duplicate", "updated"), similarity (if dup)
    """
    # Input validation
    if not text or not text.strip():
        raise ValueError("Content cannot be empty")

    text = text.strip()
    word_count = len(text.split())
    if word_count < 3 and category not in _LOW_INFO_ENTITY_CATEGORIES:
        raise ValueError(f"Facts must be at least 3 words (got {word_count}: '{text}'). A fact needs a subject, verb, and object (e.g., 'X is Y').")

    if not owner_id:
        raise ValueError("Owner is required")

    confidence = _validate_confidence_unit_interval(confidence, "confidence")
    extraction_confidence = _validate_confidence_unit_interval(
        extraction_confidence,
        "extraction_confidence",
    )
    if provenance_confidence is not None:
        provenance_confidence = _validate_confidence_unit_interval(
            provenance_confidence,
            "provenance_confidence",
        )
    # Normalize source_type aliases at storage boundary.
    if source_type is not None:
        source_type = str(source_type).strip().lower()
        if source_type == "agent":
            source_type = "assistant"
        if source_type not in {"user", "assistant", "both", "tool", "import"}:
            source_type = None
    project = _normalize_project_tag(project)

    # Default speaker attribution for assistant-originated facts when omitted.
    if (not speaker) and source_type == "assistant":
        speaker = "Assistant"
    if speaker_entity_id and not actor_id:
        actor_id = speaker_entity_id
    if conversation_id and not source_conversation_id:
        source_conversation_id = conversation_id
    
    # Map category to type
    type_map = {
        "preference": "Preference",
        "fact": "Fact",
        "decision": "Event",
        "entity": "Concept",
        "other": "Fact"
    }
    node_type = type_map.get(category.lower(), "Fact")

    graph = get_graph()

    def _apply_metadata_flags(existing: Node) -> None:
        """Persist source/technical metadata on dedup-update paths."""
        if not (
            source_type
            or target_datastore
            or is_technical
            or project
            or source_channel
            or source_conversation_id
            or source_author_id
            or speaker_entity_id
            or conversation_id
            or participant_entity_ids
            or visibility_scope
            or sensitivity
            or provenance_confidence is not None
            or actor_id
            or subject_entity_id
        ):
            return
        attrs = existing.attributes if isinstance(existing.attributes, dict) else (existing.attributes or {})
        if source_type and not attrs.get("source_type"):
            attrs["source_type"] = source_type
        if target_datastore and not attrs.get("target_datastore"):
            attrs["target_datastore"] = target_datastore
        if speaker and not existing.speaker:
            existing.speaker = speaker
        if is_technical:
            attrs["is_technical"] = True
        if project and not attrs.get("project"):
            attrs["project"] = project
        if source_channel and not attrs.get("source_channel"):
            attrs["source_channel"] = source_channel
        if source_conversation_id and not attrs.get("source_conversation_id"):
            attrs["source_conversation_id"] = source_conversation_id
        if source_author_id and not attrs.get("source_author_id"):
            attrs["source_author_id"] = source_author_id
        if speaker_entity_id and not attrs.get("speaker_entity_id"):
            attrs["speaker_entity_id"] = speaker_entity_id
        if conversation_id and not attrs.get("conversation_id"):
            attrs["conversation_id"] = conversation_id
        if participant_entity_ids and not attrs.get("participant_entity_ids"):
            attrs["participant_entity_ids"] = list(participant_entity_ids)
        if visibility_scope and not attrs.get("visibility_scope"):
            attrs["visibility_scope"] = visibility_scope
        if sensitivity and not attrs.get("sensitivity"):
            attrs["sensitivity"] = sensitivity
        if provenance_confidence is not None and attrs.get("provenance_confidence") is None:
            attrs["provenance_confidence"] = float(provenance_confidence)
        if actor_id and not attrs.get("actor_id"):
            attrs["actor_id"] = actor_id
        if subject_entity_id and not attrs.get("subject_entity_id"):
            attrs["subject_entity_id"] = subject_entity_id
        if speaker_entity_id and not existing.speaker_entity_id:
            existing.speaker_entity_id = speaker_entity_id
        if conversation_id and not existing.conversation_id:
            existing.conversation_id = conversation_id
        if visibility_scope and not existing.visibility_scope:
            existing.visibility_scope = visibility_scope
        if sensitivity and not existing.sensitivity:
            existing.sensitivity = sensitivity
        if provenance_confidence is not None and existing.provenance_confidence is None:
            existing.provenance_confidence = float(provenance_confidence)
        existing.attributes = attrs

    # Fast exact-dedup: content hash check (before embedding, saves API calls)
    text_hash = content_hash(text)
    if not skip_dedup:
        with graph._get_conn() as conn:
            owner_clause = "AND (owner_id = ? OR owner_id IS NULL)" if owner_id else ""
            params = [text_hash] + ([owner_id] if owner_id else [])
            existing_row = conn.execute(f"""
                SELECT * FROM nodes WHERE content_hash = ?
                  AND (status IS NULL OR status IN ('approved', 'pending', 'active', 'flagged'))
                  AND deleted_at IS NULL
                  {owner_clause}
                LIMIT 1
            """, params).fetchone()
            if existing_row:
                existing = graph._row_to_node(existing_row)
                log_dedup_decision(graph, text, existing.id, existing.name,
                                   1.0, "hash_exact", owner_id=owner_id, source=source)
                # Confirmation boosting: re-extraction confirms this fact
                existing.confirmation_count += 1
                existing.last_confirmed_at = datetime.now().isoformat()
                existing.confidence = min(existing.confidence + 0.02, 0.95)
                # Bjork: re-encoding strengthens storage (smaller than retrieval increment)
                existing.storage_strength = min(10.0, existing.storage_strength + 0.03)
                _apply_metadata_flags(existing)
                if update_if_dup and verified and not existing.verified:
                    existing.verified = True
                    existing.confidence = 0.9
                graph.update_node(existing)
                if update_if_dup and verified:
                    return {
                        "id": existing.id,
                        "status": "updated",
                        "similarity": 1.0,
                        "existing_text": existing.name,
                        "confirmation_count": existing.confirmation_count,
                    }
                return {
                    "id": existing.id,
                    "status": "duplicate",
                    "similarity": 1.0,
                    "existing_text": existing.name,
                    "confirmation_count": existing.confirmation_count,
                }

    # Dedup check: three-zone logic with optional LLM verification
    embedding = graph.get_embedding(text)
    if not skip_dedup and embedding:
        # Load dedup thresholds from config (with fallbacks)
        if _HAS_CONFIG:
            cfg = _get_memory_config()
            auto_reject_thresh = cfg.janitor.dedup.auto_reject_threshold
            gray_zone_low = cfg.janitor.dedup.gray_zone_low
            llm_verify_enabled = cfg.janitor.dedup.llm_verify_enabled
        else:
            auto_reject_thresh = 0.98
            gray_zone_low = 0.88
            llm_verify_enabled = False

        # Search for high-similarity matches using FTS5 token pre-filter
        tokens = _lib_extract_key_tokens(text)
        with graph._get_conn() as conn:
            if tokens:
                fts_query = " OR ".join(f'"{t}"' for t in tokens)
                try:
                    if owner_id:
                        rows = conn.execute("""
                            SELECT n.* FROM nodes_fts
                            JOIN nodes n ON n.rowid = nodes_fts.rowid
                            WHERE nodes_fts MATCH ?
                              AND n.embedding IS NOT NULL
                              AND n.superseded_by IS NULL
                              AND (n.status IS NULL OR n.status IN ('approved', 'pending', 'active'))
                              AND (n.owner_id = ? OR n.owner_id IS NULL)
                            LIMIT 500
                        """, (fts_query, owner_id)).fetchall()
                    else:
                        rows = conn.execute("""
                            SELECT n.* FROM nodes_fts
                            JOIN nodes n ON n.rowid = nodes_fts.rowid
                            WHERE nodes_fts MATCH ?
                              AND n.embedding IS NOT NULL
                              AND n.superseded_by IS NULL
                              AND (n.status IS NULL OR n.status IN ('approved', 'pending', 'active'))
                            LIMIT 500
                        """, (fts_query,)).fetchall()
                except Exception:
                    # Fallback to bounded scan if FTS5 unavailable (cap at 500 most recent)
                    if owner_id:
                        rows = conn.execute(
                            "SELECT * FROM nodes WHERE embedding IS NOT NULL AND (owner_id = ? OR owner_id IS NULL) ORDER BY accessed_at DESC LIMIT 500",
                            (owner_id,)
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            "SELECT * FROM nodes WHERE embedding IS NOT NULL ORDER BY accessed_at DESC LIMIT 500"
                        ).fetchall()
            else:
                rows = []  # No tokens = novel fact, no dedup possible

            for row in rows:
                existing = graph._row_to_node(row)
                if existing.embedding:
                    sim = graph.cosine_similarity(embedding, existing.embedding)

                    if sim >= auto_reject_thresh and texts_are_near_identical(text, existing.name):
                        # Zone 1: Auto-reject (high sim AND texts are near-identical strings)
                        log_dedup_decision(graph, text, existing.id, existing.name,
                                           sim, "auto_reject", owner_id=owner_id, source=source)
                        # Confirmation boosting: re-extraction confirms this fact
                        existing.confirmation_count += 1
                        existing.last_confirmed_at = datetime.now().isoformat()
                        existing.confidence = min(existing.confidence + 0.02, 0.95)
                        # Bjork: re-encoding strengthens storage (smaller than retrieval increment)
                        existing.storage_strength = min(10.0, existing.storage_strength + 0.03)
                        _apply_metadata_flags(existing)
                        if update_if_dup and verified and not existing.verified:
                            existing.verified = True
                            existing.confidence = 0.9
                        graph.update_node(existing)
                        if update_if_dup and verified:
                            return {
                                "id": existing.id,
                                "status": "updated",
                                "similarity": round(sim, 3),
                                "existing_text": existing.name,
                                "confirmation_count": existing.confirmation_count,
                            }
                        return {
                            "id": existing.id,
                            "status": "duplicate",
                            "similarity": round(sim, 3),
                            "existing_text": existing.name,
                            "confirmation_count": existing.confirmation_count,
                        }

                    elif sim >= gray_zone_low:
                        # Zone 2: Gray zone — LLM verification
                        if llm_verify_enabled:
                            llm_result = _llm_dedup_check(text, existing.name)
                            if llm_result is not None:
                                if llm_result["is_same"]:
                                    subsumes = llm_result.get("subsumes")
                                    decision = "llm_reject"
                                    if subsumes == "a_subsumes_b":
                                        decision = "llm_subsume_update"
                                    elif subsumes == "b_subsumes_a":
                                        decision = "llm_subsume_keep"
                                    # LLM confirms duplicate or subsumption
                                    log_dedup_decision(graph, text, existing.id, existing.name,
                                                       sim, decision,
                                                       llm_reasoning=llm_result.get("reasoning"),
                                                       owner_id=owner_id, source=source)
                                    # Confirmation boosting: re-extraction confirms this fact
                                    existing.confirmation_count += 1
                                    existing.last_confirmed_at = datetime.now().isoformat()
                                    existing.confidence = min(existing.confidence + 0.02, 0.95)
                                    # Bjork: re-encoding strengthens storage (smaller than retrieval increment)
                                    existing.storage_strength = min(10.0, existing.storage_strength + 0.03)
                                    _apply_metadata_flags(existing)
                                    if update_if_dup and verified and not existing.verified:
                                        existing.verified = True
                                        existing.confidence = 0.9
                                    # If new fact subsumes existing, upgrade the text
                                    if subsumes == "a_subsumes_b":
                                        existing.name = text
                                        existing.embedding = None  # force re-embed
                                        existing.content_hash = None
                                        # Remove stale ANN entry so vec_nodes doesn't return wrong results
                                        with graph._get_conn() as conn:
                                            conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (existing.id,))
                                    graph.update_node(existing)
                                    if (update_if_dup and verified) or subsumes == "a_subsumes_b":
                                        return {
                                            "id": existing.id,
                                            "status": "updated",
                                            "similarity": round(sim, 3),
                                            "existing_text": existing.name,
                                            "confirmation_count": existing.confirmation_count,
                                        }
                                    return {
                                        "id": existing.id,
                                        "status": "duplicate",
                                        "similarity": round(sim, 3),
                                        "existing_text": existing.name,
                                        "confirmation_count": existing.confirmation_count,
                                    }
                                else:
                                    # LLM says different — log and continue checking
                                    log_dedup_decision(graph, text, existing.id, existing.name,
                                                       sim, "llm_accept",
                                                       llm_reasoning=llm_result.get("reasoning"),
                                                       owner_id=owner_id, source=source)
                                    continue
                            else:
                                # LLM unavailable — use similarity threshold
                                # But only reject if texts are near-identical strings
                                # (embeddings can't distinguish proper noun swaps)
                                if sim >= dedup_threshold and texts_are_near_identical(text, existing.name):
                                    log_dedup_decision(graph, text, existing.id, existing.name,
                                                       sim, "fallback_reject",
                                                       owner_id=owner_id, source=source)
                                    # Confirmation boosting: re-extraction confirms this fact
                                    existing.confirmation_count += 1
                                    existing.last_confirmed_at = datetime.now().isoformat()
                                    existing.confidence = min(existing.confidence + 0.02, 0.95)
                                    # Bjork: re-encoding strengthens storage (smaller than retrieval increment)
                                    existing.storage_strength = min(10.0, existing.storage_strength + 0.03)
                                    _apply_metadata_flags(existing)
                                    if update_if_dup and verified and not existing.verified:
                                        existing.verified = True
                                        existing.confidence = 0.9
                                    graph.update_node(existing)
                                    if update_if_dup and verified:
                                        return {
                                            "id": existing.id,
                                            "status": "updated",
                                            "similarity": round(sim, 3),
                                            "existing_text": existing.name,
                                            "confirmation_count": existing.confirmation_count,
                                        }
                                    return {
                                        "id": existing.id,
                                        "status": "duplicate",
                                        "similarity": round(sim, 3),
                                        "existing_text": existing.name,
                                        "confirmation_count": existing.confirmation_count,
                                    }
                        else:
                            # LLM verification disabled — use similarity threshold
                            # But only reject if texts are near-identical strings
                            if sim >= dedup_threshold and texts_are_near_identical(text, existing.name):
                                log_dedup_decision(graph, text, existing.id, existing.name,
                                                   sim, "fallback_reject",
                                                   owner_id=owner_id, source=source)
                                # Confirmation boosting: re-extraction confirms this fact
                                existing.confirmation_count += 1
                                existing.last_confirmed_at = datetime.now().isoformat()
                                existing.confidence = min(existing.confidence + 0.02, 0.95)
                                # Bjork: re-encoding strengthens storage (smaller than retrieval increment)
                                existing.storage_strength = min(10.0, existing.storage_strength + 0.03)
                                _apply_metadata_flags(existing)
                                if update_if_dup and verified and not existing.verified:
                                    existing.verified = True
                                    existing.confidence = 0.9
                                graph.update_node(existing)
                                if update_if_dup and verified:
                                    return {
                                        "id": existing.id,
                                        "status": "updated",
                                        "similarity": round(sim, 3),
                                        "existing_text": existing.name,
                                        "confirmation_count": existing.confirmation_count,
                                    }
                                return {
                                    "id": existing.id,
                                    "status": "duplicate",
                                    "similarity": round(sim, 3),
                                    "existing_text": existing.name,
                                    "confirmation_count": existing.confirmation_count,
                                }

                    # Zone 3: sim < gray_zone_low — store normally (continue checking)
    
    # Confidence adjustment for assistant-inferred facts
    adjusted_confidence = confidence
    if source_type == "assistant":
        adjusted_confidence = round(confidence * 0.9, 3)

    # No duplicate found, create new node
    node = Node.create(
        type=node_type,
        name=text,
        verified=verified,
        pinned=pinned,
        privacy=privacy,
        source=source,
        source_id=source_id,
        confidence=adjusted_confidence,
        fact_type=fact_type,
        knowledge_type=knowledge_type,
        extraction_confidence=extraction_confidence,
        speaker=speaker
    )
    node.owner_id = owner_id
    node.session_id = session_id
    node.keywords = keywords
    if created_at:
        node.created_at = created_at
    if accessed_at:
        node.accessed_at = accessed_at

    node.speaker_entity_id = speaker_entity_id or actor_id
    node.conversation_id = conversation_id or source_conversation_id
    node.visibility_scope = visibility_scope or ("private_subject" if owner_id else "source_shared")
    node.sensitivity = sensitivity or "normal"
    node.provenance_confidence = float(
        provenance_confidence if provenance_confidence is not None else extraction_confidence
    )

    # Store metadata flags in attributes blob
    if (
        source_type
        or target_datastore
        or is_technical
        or project
        or source_channel
        or source_conversation_id
        or source_author_id
        or speaker_entity_id
        or conversation_id
        or participant_entity_ids
        or visibility_scope
        or sensitivity
        or provenance_confidence is not None
        or actor_id
        or subject_entity_id
    ):
        attrs = json.loads(node.attributes) if isinstance(node.attributes, str) else (node.attributes or {})
        if source_type:
            attrs["source_type"] = source_type
        if target_datastore:
            attrs["target_datastore"] = target_datastore
        if is_technical:
            attrs["is_technical"] = True
        if project:
            attrs["project"] = project
        if source_channel:
            attrs["source_channel"] = source_channel
        if source_conversation_id:
            attrs["source_conversation_id"] = source_conversation_id
        if source_author_id:
            attrs["source_author_id"] = source_author_id
        if speaker_entity_id:
            attrs["speaker_entity_id"] = speaker_entity_id
        if conversation_id:
            attrs["conversation_id"] = conversation_id
        if participant_entity_ids:
            attrs["participant_entity_ids"] = list(participant_entity_ids)
        if visibility_scope:
            attrs["visibility_scope"] = visibility_scope
        if sensitivity:
            attrs["sensitivity"] = sensitivity
        if provenance_confidence is not None:
            attrs["provenance_confidence"] = float(provenance_confidence)
        if actor_id:
            attrs["actor_id"] = actor_id
        if subject_entity_id:
            attrs["subject_entity_id"] = subject_entity_id
        node.attributes = attrs

    injection_match = None
    if not status:
        injection_match = _check_injection_blocklist(text)
        if injection_match:
            node.status = "flagged"
            attrs = json.loads(node.attributes) if isinstance(node.attributes, str) else (node.attributes or {})
            attrs["flagged_pattern"] = injection_match
            node.attributes = attrs
        else:
            node.status = "pending"
    else:
        node.status = status
    node.embedding = embedding  # Reuse the embedding we already computed
    node.content_hash = text_hash  # Reuse the hash we already computed
    node_id = graph.add_node(node, embed=False)  # Don't re-embed

    result = {"id": node_id, "status": "created"}
    if node.status == "flagged":
        result["flagged"] = True
        result["flagged_pattern"] = injection_match
    return result


def create_edge(
    subject_name: str,
    relation: str,
    object_name: str,
    source_fact_id: Optional[str] = None,
    create_missing_entities: bool = True,
    owner_id: Optional[str] = None,
    _conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """Create an edge between two named entities.

    Args:
        subject_name: Name of the source entity (e.g., "Carol Smith")
        relation: Relationship type (e.g., "parent_of")
        object_name: Name of the target entity (e.g., "Alice Smith")
        source_fact_id: Optional ID of the fact that created this edge
        create_missing_entities: If True, create Person nodes for missing entities
        owner_id: Owner ID for newly created entity nodes

    Returns:
        Dict with edge_id, status, and any created entity IDs
    """
    graph = get_graph()

    # Infer entity type from relation name
    _RELATION_OBJECT_TYPES = {
        "works_at": "Organization", "employed_by": "Organization",
        "member_of": "Organization", "founded": "Organization",
        "lives_in": "Place", "lives_at": "Place", "located_in": "Place",
        "born_in": "Place", "moved_to": "Place", "visited": "Place",
        "has_pet": "Pet", "owns_pet": "Pet",
        "has_feature": "Feature", "uses_tool": "Tool",
        "works_on": "Project", "contributes_to": "Project",
        "manages": "Project",
    }
    _RELATION_SUBJECT_TYPES = {
        "subsidiary_of": "Organization", "part_of": "Organization",
        "feature_of": "Project", "component_of": "Project",
    }

    # Relations that always indicate the other entity is a person
    _PERSON_RELATIONS = {
        "parent_of", "child_of", "sibling_of", "spouse_of", "partner_of",
        "friend_of", "aunt_of", "uncle_of", "cousin_of", "grandparent_of",
        "has_manager", "managed_by", "mentored_by", "mentors",
        "married_to", "dating", "roommate_of", "has_employee",
    }

    def _infer_entity_type(name: str, relation: str, is_subject: bool) -> str:
        """Infer entity type from relation and name heuristics."""
        rel_lower = relation.lower().replace("-", "_")
        if is_subject and rel_lower in _RELATION_SUBJECT_TYPES:
            return _RELATION_SUBJECT_TYPES[rel_lower]
        if not is_subject and rel_lower in _RELATION_OBJECT_TYPES:
            return _RELATION_OBJECT_TYPES[rel_lower]
        # Relationship edges → Person
        if rel_lower in _PERSON_RELATIONS:
            return "Person"
        # Fallback: unknown relation → Fact (not Person)
        return "Fact"

    def _find_entity(conn: sqlite3.Connection, name: str) -> Optional[Node]:
        """Find entity by exact name, then fuzzy match using SQL patterns.

        Resolution order:
        1. Exact name match (any type)
        2. Case-insensitive exact match on Person/Place/Pet/Organization
        3. Prefix match: "Alice" → "Alice Smith" (shortest match wins)
        4. Suffix match: "Smith" → "Alice Smith" (shortest match wins)

        Note: SQL pattern matching used instead of embedding similarity because
        entity nodes (Person/Place/Pet) have short names where pattern matching
        is more reliable than vector similarity for name resolution.
        """
        row = conn.execute(
            "SELECT * FROM nodes WHERE name = ? LIMIT 1",
            (name,)
        ).fetchone()
        if row:
            return graph._row_to_node(row)
        # Case-insensitive exact match
        row = conn.execute(
            "SELECT * FROM nodes WHERE LOWER(name) = LOWER(?) AND type IN ('Person', 'Place', 'Pet', 'Organization') LIMIT 1",
            (name,)
        ).fetchone()
        if row:
            return graph._row_to_node(row)
        # Escape SQL LIKE wildcards in entity names to prevent unintended matches
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # Prefix match: "Alice" → "Alice Smith"
        row = conn.execute(
            "SELECT * FROM nodes WHERE name LIKE ? ESCAPE '\\' AND type IN ('Person', 'Place', 'Pet', 'Organization') ORDER BY LENGTH(name) LIMIT 1",
            (escaped + "%",)
        ).fetchone()
        if row:
            return graph._row_to_node(row)
        # Suffix match: "Smith" → "Alice Smith"
        row = conn.execute(
            "SELECT * FROM nodes WHERE name LIKE ? ESCAPE '\\' AND type IN ('Person', 'Place', 'Pet', 'Organization') ORDER BY LENGTH(name) LIMIT 1",
            ("%" + escaped,)
        ).fetchone()
        if row:
            return graph._row_to_node(row)
        return None

    def _insert_entity(conn: sqlite3.Connection, node: Node) -> None:
        if not node.embedding:
            embed_text = node.name
            if node.attributes:
                embed_text += " " + " ".join(str(v) for v in node.attributes.values() if v)
            node.embedding = graph.get_embedding(embed_text)
        if not node.content_hash:
            node.content_hash = content_hash(node.name)

        conn.execute("""
            INSERT OR REPLACE INTO nodes
            (id, type, name, attributes, embedding, verified, pinned, confidence,
             source, source_id, privacy, valid_from, valid_until,
             created_at, updated_at, accessed_at, access_count, storage_strength, owner_id, session_id,
             fact_type, knowledge_type, extraction_confidence, status, speaker, speaker_entity_id,
             conversation_id, visibility_scope, sensitivity, provenance_confidence,
             content_hash, superseded_by, confirmation_count, last_confirmed_at, keywords)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            node.id, node.type, node.name,
            json.dumps(node.attributes),
            graph._pack_embedding(node.embedding) if node.embedding else None,
            1 if node.verified else 0,
            1 if node.pinned else 0,
            node.confidence,
            node.source, node.source_id,
            node.privacy,
            node.valid_from, node.valid_until,
            node.created_at or datetime.now().isoformat(),
            datetime.now().isoformat(),
            node.accessed_at or datetime.now().isoformat(),
            node.access_count,
            node.storage_strength,
            node.owner_id,
            node.session_id,
            node.fact_type,
            node.knowledge_type,
            node.extraction_confidence,
            node.status,
            node.speaker,
            node.speaker_entity_id,
            node.conversation_id,
            node.visibility_scope,
            node.sensitivity,
            node.provenance_confidence,
            node.content_hash,
            node.superseded_by,
            node.confirmation_count,
            node.last_confirmed_at,
            node.keywords,
        ))
        if node.embedding and _lib_has_vec():
            packed = graph._pack_embedding(node.embedding)
            try:
                graph._ensure_vec_table(conn, node.embedding)
                conn.execute(
                    "INSERT OR REPLACE INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                    (node.id, packed),
                )
            except Exception as exc:
                logger.warning(
                    "create_edge inserted entity %s but failed vec_nodes upsert: %s",
                    node.id,
                    exc,
                )
                if _is_fail_hard_mode():
                    raise RuntimeError(
                        "Vector index upsert failed during create_edge while fail-hard mode is enabled"
                    ) from exc

    conn_ctx = nullcontext(_conn) if _conn is not None else graph._get_conn()
    with conn_ctx as conn:
        # Find or create subject entity
        subject = _find_entity(conn, subject_name)
        subject_created = False
        if not subject and create_missing_entities:
            inferred_type = _infer_entity_type(subject_name, relation, is_subject=True)
            subject = Node.create(type=inferred_type, name=subject_name)
            subject.owner_id = owner_id
            subject.status = "active"  # Entity nodes are structural, not claims needing review
            _insert_entity(conn, subject)
            subject_created = True
        elif not subject:
            return {"status": "error", "message": f"Subject entity '{subject_name}' not found"}

        # Find or create object entity
        obj = _find_entity(conn, object_name)
        object_created = False
        if not obj and create_missing_entities:
            inferred_type = _infer_entity_type(object_name, relation, is_subject=False)
            obj = Node.create(type=inferred_type, name=object_name)
            obj.owner_id = owner_id
            obj.status = "active"  # Entity nodes are structural, not claims needing review
            _insert_entity(conn, obj)
            object_created = True
        elif not obj:
            return {"status": "error", "message": f"Object entity '{object_name}' not found"}

        # Create edge in same transaction as any new entities.
        edge = Edge.create(
            source_id=subject.id,
            target_id=obj.id,
            relation=relation,
            source_fact_id=source_fact_id
        )
        conn.execute("""
            INSERT OR REPLACE INTO edges
            (id, source_id, target_id, relation, attributes, weight,
             valid_from, valid_until, created_at, source_fact_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            edge.id, edge.source_id, edge.target_id, edge.relation,
            json.dumps(edge.attributes),
            edge.weight,
            edge.valid_from, edge.valid_until,
            edge.created_at or datetime.now().isoformat(),
            edge.source_fact_id,
        ))

        return {
            "edge_id": edge.id,
            "status": "created",
            "subject_id": subject.id,
            "object_id": obj.id,
            "subject_created": subject_created,
            "object_created": object_created
        }


def delete_edges_by_source_fact(source_fact_id: str) -> int:
    """Delete all edges that were created from a specific fact.

    Args:
        source_fact_id: The ID of the source fact

    Returns:
        Number of edges deleted
    """
    graph = get_graph()
    with graph._get_conn() as conn:
        result = conn.execute(
            "DELETE FROM edges WHERE source_fact_id = ?",
            (source_fact_id,)
        )
        return result.rowcount


def forget(query: Optional[str] = None, node_id: Optional[str] = None) -> bool:
    """Delete a memory by query or ID."""
    graph = get_graph()

    if node_id:
        return graph.delete_node(node_id)

    if query:
        results = graph.search_hybrid(query, limit=1)
        if results:
            node, _ = results[0]
            return graph.delete_node(node.id)

    return False


def get_memory(node_id: str) -> Optional[Dict[str, Any]]:
    """Get a single memory by ID."""
    graph = get_graph()
    node = graph.get_node(node_id)
    if node:
        return {
            "id": node.id,
            "type": node.type,
            "name": node.name,
            "content": node.name,
            "verified": node.verified,
            "pinned": node.pinned,
            "confidence": node.confidence,
            "owner_id": node.owner_id,
            "created_at": node.created_at,
            "updated_at": node.updated_at,
            "attributes": node.attributes
        }
    return None


def hard_delete_node(node_id: str, conn: Optional[sqlite3.Connection] = None) -> bool:
    """Hard delete a node and all related references from the database.

    Cleans up edges, contradictions, and decay_review_queue entries.
    dedup_log references are kept (audit trail).
    """
    graph = get_graph()

    def _delete_with_conn(active_conn: sqlite3.Connection) -> bool:
        active_conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (node_id, node_id))
        active_conn.execute("DELETE FROM contradictions WHERE node_a_id = ? OR node_b_id = ?", (node_id, node_id))
        active_conn.execute("DELETE FROM decay_review_queue WHERE node_id = ?", (node_id,))
        # Clean up vec_nodes index (virtual table, no CASCADE)
        try:
            active_conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (node_id,))
        except Exception:
            pass  # vec_nodes may not exist yet
        # dedup_log.existing_node_id uses ON DELETE SET NULL — audit trail preserved automatically
        result = active_conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        return result.rowcount > 0

    if conn is not None:
        return _delete_with_conn(conn)

    with graph._get_conn() as managed_conn:
        return _delete_with_conn(managed_conn)


def soft_delete(node_id: str, reason: str = "manual") -> bool:
    """Delete a memory and related references from the database."""
    return hard_delete_node(node_id)


def store_contradiction(node_a_id: str, node_b_id: str, explanation: str) -> Optional[str]:
    """Store a detected contradiction between two memory nodes."""
    graph = get_graph()
    contradiction_id = str(uuid.uuid4())
    # Normalize order so UNIQUE constraint works regardless of discovery direction
    a_id, b_id = sorted([node_a_id, node_b_id])
    try:
        with graph._get_conn() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO contradictions (id, node_a_id, node_b_id, explanation)
                VALUES (?, ?, ?, ?)
            """, (contradiction_id, a_id, b_id, explanation))
            if cursor.rowcount > 0:
                return contradiction_id
            existing = conn.execute(
                "SELECT id FROM contradictions WHERE node_a_id = ? AND node_b_id = ?",
                (a_id, b_id),
            ).fetchone()
            if existing:
                return str(existing["id"])
            return None
    except Exception as exc:
        try:
            from lib.fail_policy import is_fail_hard_enabled
            if is_fail_hard_enabled():
                raise RuntimeError(
                    f"Failed to store contradiction for {a_id} vs {b_id}"
                ) from exc
        except RuntimeError:
            raise
        except Exception:
            pass
        logger.warning(
            "store_contradiction failed for node_a=%s node_b=%s: %s",
            a_id,
            b_id,
            exc,
        )
        return None


def get_pending_contradictions(limit: int = 50) -> List[Dict[str, Any]]:
    """Get pending contradictions with full node context."""
    graph = get_graph()
    with graph._get_conn() as conn:
        rows = conn.execute("""
            SELECT c.id, c.node_a_id, c.node_b_id, c.explanation, c.detected_at,
                   a.name AS text_a, a.confidence AS conf_a, a.created_at AS created_a,
                   a.source AS source_a, a.speaker AS speaker_a, a.access_count AS access_a,
                   b.name AS text_b, b.confidence AS conf_b, b.created_at AS created_b,
                   b.source AS source_b, b.speaker AS speaker_b, b.access_count AS access_b
            FROM contradictions c
            JOIN nodes a ON c.node_a_id = a.id
            JOIN nodes b ON c.node_b_id = b.id
            WHERE c.status = 'pending'
            ORDER BY c.detected_at ASC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(row) for row in rows]


def resolve_contradiction(contradiction_id: str, resolution: str, reason: str) -> bool:
    """Mark a contradiction as resolved."""
    graph = get_graph()
    with graph._get_conn() as conn:
        result = conn.execute("""
            UPDATE contradictions
            SET status = 'resolved', resolution = ?, resolution_reason = ?,
                resolved_at = datetime('now')
            WHERE id = ?
        """, (resolution, reason, contradiction_id))
        return result.rowcount > 0


def mark_contradiction_false_positive(contradiction_id: str, reason: str) -> bool:
    """Mark a contradiction as a false positive."""
    graph = get_graph()
    with graph._get_conn() as conn:
        result = conn.execute("""
            UPDATE contradictions
            SET status = 'false_positive', resolution = 'keep_both',
                resolution_reason = ?, resolved_at = datetime('now')
            WHERE id = ?
        """, (reason, contradiction_id))
        return result.rowcount > 0


# ==========================================================================
# Dedup Logging & LLM Verification
# ==========================================================================

def log_dedup_decision(
    graph: MemoryGraph,
    new_text: str,
    existing_node_id: str,
    existing_text: str,
    similarity: float,
    decision: str,
    llm_reasoning: Optional[str] = None,
    owner_id: Optional[str] = None,
    source: Optional[str] = None,
) -> str:
    """Log a dedup decision to dedup_log table. Returns log entry ID."""
    log_id = str(uuid.uuid4())
    try:
        with graph._get_conn() as conn:
            conn.execute("""
                INSERT INTO dedup_log
                (id, new_text, existing_node_id, existing_text, similarity,
                 decision, llm_reasoning, owner_id, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (log_id, new_text, existing_node_id, existing_text,
                  similarity, decision, llm_reasoning, owner_id, source))
    except sqlite3.IntegrityError:
        # FK constraint can fail if the candidate node was hard-deleted between
        # the FTS search and this insert (WAL snapshot mismatch). Fall back to
        # NULL existing_node_id — the audit trail is preserved without the link.
        print(f"[dedup_log] WARNING: FK constraint for node {existing_node_id}, inserting with NULL reference", file=sys.stderr)
        with graph._get_conn() as conn:
            conn.execute("""
                INSERT INTO dedup_log
                (id, new_text, existing_node_id, existing_text, similarity,
                 decision, llm_reasoning, owner_id, source)
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
            """, (log_id, new_text, existing_text,
                  similarity, decision, llm_reasoning, owner_id, source))
    return log_id


def _llm_dedup_check(new_text: str, existing_text: str) -> Optional[Dict[str, Any]]:
    """Ask the fast-reasoning LLM whether two texts represent the same fact.

    Returns {"is_same": bool, "reasoning": str} or None on failure.
    """
    if not _HAS_LLM_CLIENTS:
        return None

    prompt = (
        "Are these two statements the same fact (just reworded), or does one "
        "SUBSUME the other (contains all the same info plus additional detail)?\n\n"
        "IMPORTANT RULES:\n"
        "- Negation flips meaning: 'likes coffee' vs 'doesn't like coffee' are DIFFERENT.\n"
        "- If Statement A includes ALL info from Statement B plus more detail, "
        "A subsumes B. Example: 'Maya has a dog named Biscuit' subsumes 'Maya has a dog'.\n"
        "- If they convey the same info in different words, they are the SAME.\n"
        "- Only mark as different if they contain genuinely distinct information.\n\n"
        f'Statement A (new): "{new_text}"\n'
        f'Statement B (existing): "{existing_text}"\n\n'
        'Respond with JSON only: {"is_same": true/false, "subsumes": "a_subsumes_b" | "b_subsumes_a" | null, "reasoning": "brief reason"}'
    )

    response, duration = call_fast_reasoning(prompt, max_tokens=100, timeout=30.0)
    if not response:
        return None

    parsed = parse_json_response(response)
    if isinstance(parsed, dict) and "is_same" in parsed:
        subsumes = parsed.get("subsumes")
        if subsumes not in ("a_subsumes_b", "b_subsumes_a"):
            subsumes = None
        return {
            "is_same": bool(parsed["is_same"]) or subsumes is not None,
            "subsumes": subsumes,
            "reasoning": str(parsed.get("reasoning", ""))
        }
    return None


def get_recent_dedup_rejections(hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent dedup rejections for nightly review."""
    graph = get_graph()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    with graph._get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM dedup_log
            WHERE review_status = 'unreviewed'
              AND created_at > ?
              AND decision != 'hash_exact'
            ORDER BY created_at DESC
            LIMIT ?
        """, (cutoff, limit)).fetchall()
    return [dict(row) for row in rows]


def resolve_dedup_review(log_id: str, status: str, resolution: Optional[str] = None) -> bool:
    """Mark a dedup log entry as reviewed. status: 'confirmed' or 'reversed'."""
    graph = get_graph()
    with graph._get_conn() as conn:
        result = conn.execute("""
            UPDATE dedup_log
            SET review_status = ?, review_resolution = ?,
                reviewed_at = datetime('now')
            WHERE id = ?
        """, (status, resolution, log_id))
        return result.rowcount > 0


# ==========================================================================
# Decay Review Queue
# ==========================================================================

def queue_for_decay_review(mem: Dict[str, Any]) -> str:
    """Queue a memory for decay review instead of silent deletion. Returns queue ID."""
    graph = get_graph()
    queue_id = str(uuid.uuid4())
    with graph._get_conn() as conn:
        # Skip if this node is already queued (prevents duplicate entries on re-runs)
        existing = conn.execute(
            "SELECT id FROM decay_review_queue WHERE node_id = ? AND status = 'pending'",
            (mem["id"],)
        ).fetchone()
        if existing:
            return existing["id"]
        conn.execute("""
            INSERT INTO decay_review_queue
            (id, node_id, node_text, node_type, confidence_at_queue,
             access_count, last_accessed, verified, created_at_node)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            queue_id,
            mem["id"],
            mem["text"],
            mem.get("type"),
            mem["confidence"],
            mem.get("access_count", 0),
            mem.get("last_accessed"),
            1 if mem.get("verified") else 0,
            mem.get("created_at"),
        ))
    return queue_id


def get_pending_decay_reviews(limit: int = 50) -> List[Dict[str, Any]]:
    """Get pending decay review queue items."""
    graph = get_graph()
    with graph._get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM decay_review_queue
            WHERE status = 'pending'
            ORDER BY queued_at ASC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(row) for row in rows]


def resolve_decay_review(
    queue_id: str, decision: str, reason: str, conn: Optional[sqlite3.Connection] = None
) -> bool:
    """Mark a decay review item as reviewed. decision: 'delete', 'extend', 'pin'."""
    graph = get_graph()

    def _resolve_with_conn(active_conn: sqlite3.Connection) -> bool:
        result = active_conn.execute("""
            UPDATE decay_review_queue
            SET decision = ?, decision_reason = ?, status = 'reviewed',
                reviewed_at = datetime('now')
            WHERE id = ?
        """, (decision, reason, queue_id))
        return result.rowcount > 0

    if conn is not None:
        return _resolve_with_conn(conn)

    with graph._get_conn() as managed_conn:
        return _resolve_with_conn(managed_conn)


def decay_memories() -> Dict[str, Any]:
    """Apply confidence decay to memories.

    Aligned with janitor semantics:
    - Subtractive decay (confidence - 0.10) instead of multiplicative
    - Only decays memories not accessed in 30+ days
    - Respects pinned flag
    """
    graph = get_graph()

    cutoff = (datetime.now() - timedelta(days=30)).isoformat()

    with graph._get_conn() as conn:
        result = conn.execute("""
            UPDATE nodes
            SET confidence = confidence - 0.10,
                updated_at = ?
            WHERE pinned = 0
              AND verified = 0
              AND confidence > 0.1
              AND superseded_by IS NULL
              AND (status IS NULL OR status IN ('approved', 'active'))
              AND accessed_at < ?
        """, (datetime.now().isoformat(), cutoff))

        return {"decayed_count": result.rowcount}


def get_entity_summary(node_id: str) -> Optional[str]:
    """Get the stored summary for an entity node, or None if not summarized."""
    graph = get_graph()
    node = graph.get_node(node_id)
    if not node:
        return None
    return node.attributes.get("summary")


def generate_entity_summary(node_id: str, use_llm: bool = True) -> Optional[str]:
    """Generate a summary paragraph for a Person/Place/Concept node from connected facts.

    Args:
        node_id: The entity node ID to summarize
        use_llm: If True, use LLM to generate natural summary. If False, concatenate facts.

    Returns:
        The generated summary text, or None if the node doesn't exist or has no facts.
    """
    graph = get_graph()
    node = graph.get_node(node_id)
    if not node:
        return None
    if node.type not in ("Person", "Place", "Concept"):
        return None

    # Gather all connected facts via edges
    related = graph.get_related_bidirectional(node_id, depth=1, max_results=50)
    facts = []
    for rel_node, relation, direction, depth, path in related:
        if rel_node.type in ("Fact", "Preference", "Event"):
            facts.append(rel_node.name)

    # Also search for facts that mention the entity name
    with graph._get_conn() as conn:
        name_pattern = f"%{node.name}%"
        rows = conn.execute(
            "SELECT name FROM nodes WHERE type IN ('Fact', 'Preference') AND name LIKE ? AND id != ? LIMIT 30",
            (name_pattern, node_id)
        ).fetchall()
        for row in rows:
            if row['name'] not in facts:
                facts.append(row['name'])

    if not facts:
        return None

    if use_llm and _HAS_LLM_CLIENTS:
        # Use LLM to generate natural summary
        facts_text = "\n".join(f"- {f}" for f in facts[:20])  # Cap at 20 to save tokens
        prompt = f"""Generate a brief summary paragraph (2-4 sentences) about "{node.name}" based on these known facts:

{facts_text}

Write a natural, flowing paragraph. Only include information from the facts above — do not add anything new. Be concise."""

        try:
            response, _ = call_fast_reasoning(prompt)
            if response:
                summary = response.strip()
                # Store in node attributes
                node.attributes["summary"] = summary
                node.attributes["summary_updated_at"] = datetime.now().isoformat()
                node.attributes["summary_fact_count"] = len(facts)
                graph.update_node(node)
                return summary
        except Exception as e:
            print(f"[memory_graph] LLM summary failed for {node.name}: {e}", file=sys.stderr)

    # Fallback: simple concatenation
    summary = f"{node.name}: " + ". ".join(facts[:10])
    if len(facts) > 10:
        summary += f" (and {len(facts) - 10} more facts)"

    node.attributes["summary"] = summary
    node.attributes["summary_updated_at"] = datetime.now().isoformat()
    node.attributes["summary_fact_count"] = len(facts)
    graph.update_node(node)
    return summary


def summarize_all_entities(owner_id: str = None, use_llm: bool = True, entity_types: List[str] = None) -> Dict[str, Any]:
    """Generate summaries for all Person/Place/Concept nodes.

    Args:
        owner_id: Optional owner ID to filter entities.
        use_llm: If True, use LLM to generate natural summaries.
        entity_types: List of node types to summarize (default: Person, Place, Concept).

    Returns:
        Dict with counts of summaries generated, skipped, and failed.
    """
    if entity_types is None:
        entity_types = ["Person", "Place", "Concept"]

    graph = get_graph()
    stats = {"generated": 0, "skipped": 0, "failed": 0, "total": 0}

    with graph._get_conn() as conn:
        query = f"SELECT id, name, type, attributes FROM nodes WHERE type IN ({','.join('?' for _ in entity_types)})"
        params: list = list(entity_types)
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)
        rows = conn.execute(query, params).fetchall()

    stats["total"] = len(rows)

    for row in rows:
        # Skip if already has a recent summary and not using LLM (quick mode)
        attrs_raw = row['attributes']
        try:
            attrs = json.loads(attrs_raw) if attrs_raw else {}
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[memory_graph] malformed node attributes JSON for summary node_id=%s; using empty attributes: %s",
                row['id'],
                exc,
            )
            attrs = {}
        if attrs.get("summary") and not use_llm:
            stats["skipped"] += 1
            continue

        summary = generate_entity_summary(row['id'], use_llm=use_llm)
        if summary:
            stats["generated"] += 1
        else:
            stats["skipped"] += 1

    return stats


def initialize_db() -> None:
    """Initialize the database - just ensure schema exists."""
    graph = get_graph()  # This calls _init_db() internally
    # The initialization is already done in the constructor


if __name__ == "__main__":
    import argparse
    import sys

    def main():
        parser = argparse.ArgumentParser(
            prog="memory_graph.py",
            description="Local Graph Memory System CLI",
        )
        subparsers = parser.add_subparsers(dest="command", help="Command")

        # --- init ---
        subparsers.add_parser("init", help="Initialize the database")

        # --- stats ---
        subparsers.add_parser("stats", help="Show database statistics")
        subparsers.add_parser("health", help="Show KB health metrics (detailed)")
        backfill_p = subparsers.add_parser("backfill-hashes", help="Backfill content_hash for nodes with NULL hash")
        backfill_p.add_argument("--dry-run", action="store_true", help="Preview what would be updated without making changes")

        # --- search ---
        search_p = subparsers.add_parser("search", help="Search memories via recall")
        search_p.add_argument("query", nargs="+", help="Search query")
        search_p.add_argument("--owner", default=None, help="Owner ID")
        search_p.add_argument("--limit", type=int, default=5, help="Max results (default: 5)")
        search_p.add_argument("--min-similarity", type=float, default=0.60, help="Min similarity threshold (default: 0.60)")
        search_p.add_argument("--current-session-id", default=None, help="Current session ID for filtering")
        search_p.add_argument("--compaction-time", default=None, help="Compaction timestamp for filtering")
        search_p.add_argument("--date-from", default=None, help="Only return memories from this date onward (YYYY-MM-DD)")
        search_p.add_argument("--date-to", default=None, help="Only return memories up to this date (YYYY-MM-DD)")
        search_p.add_argument("--technical-scope", default="any", choices=["personal", "technical", "any"], help="Filter by technical flag (default: any)")
        search_p.add_argument("--project", default=None, help="Filter by project/domain label")
        search_p.add_argument("--session-id", default=None, help="Filter results to a specific session ID")
        search_p.add_argument("--archive", action="store_true", help="Search archived memories instead")
        search_p.add_argument("--debug", action="store_true", help="Show scoring breakdown for each result")
        search_p.add_argument("--json", action="store_true", help="JSON output")

        # --- search-all ---
        search_all_p = subparsers.add_parser("search-all", help="Unified memory search (datastore only)")
        search_all_p.add_argument("query", nargs="+", help="Search query")
        search_all_p.add_argument("--owner", default=None, help="Owner ID")
        search_all_p.add_argument("--limit", type=int, default=5, help="Max results (default: 5)")
        search_all_p.add_argument("--min-similarity", type=float, default=0.60, help="Min similarity threshold (default: 0.60)")

        # --- search-graph ---
        search_graph_p = subparsers.add_parser("search-graph", help="Memory search with graph expansion")
        search_graph_p.add_argument("query", nargs="+", help="Search query")
        search_graph_p.add_argument("--owner", default=None, help="Owner ID")
        search_graph_p.add_argument("--limit", type=int, default=5, help="Max results (default: 5)")

        # --- search-graph-aware ---
        search_ga_p = subparsers.add_parser("search-graph-aware", help="Graph-aware search: vector + pronoun resolution + bidirectional expansion")
        search_ga_p.add_argument("query", nargs="+", help="Search query")
        search_ga_p.add_argument("--owner", default=None, help="Owner ID")
        search_ga_p.add_argument("--limit", type=int, default=10, help="Max results (default: 10)")
        search_ga_p.add_argument("--depth", type=int, default=1, help="Graph traversal depth (default: 1)")
        search_ga_p.add_argument("--technical-scope", default="any", choices=["personal", "technical", "any"], help="Filter by technical flag (default: any)")
        search_ga_p.add_argument("--project", default=None, help="Filter by project/domain label")
        search_ga_p.add_argument("--json", action="store_true", help="JSON output")

        # --- store ---
        store_p = subparsers.add_parser("store", help="Store a new memory")
        store_p.add_argument("text", help="Text of the memory to store")
        store_p.add_argument("--owner", default=None, help="Owner ID")
        store_p.add_argument("--category", default="fact", help="Category (default: fact)")
        store_p.add_argument("--source", default=None, help="Source of the memory")
        store_p.add_argument("--verified", action="store_true", help="Mark as verified")
        store_p.add_argument("--pinned", action="store_true", help="Mark as pinned")
        store_p.add_argument("--confidence", type=float, default=0.5, help="Confidence score (default: 0.5)")
        store_p.add_argument("--extraction-confidence", type=float, default=0.5, help="Extraction confidence (default: 0.5)")
        store_p.add_argument("--status", default=None, help="Initial status")
        store_p.add_argument("--privacy", default="shared", help="Privacy level (default: shared)")
        store_p.add_argument("--session-id", default=None, help="Session ID")
        store_p.add_argument("--speaker", default=None, help="Speaker name")
        store_p.add_argument("--skip-dedup", action="store_true", help="Skip deduplication check")
        store_p.add_argument("--knowledge-type", default="fact", choices=["fact", "belief", "preference", "experience"], help="Knowledge type (default: fact)")
        store_p.add_argument("--source-type", default=None, choices=["user", "assistant", "agent", "both", "tool", "import"], help="Source type (user, assistant|agent, both, tool, import)")
        store_p.add_argument("--keywords", default=None, help="Space-separated derived search keywords")
        store_p.add_argument("--created-at", default=None, help="Override created_at timestamp (ISO format)")
        store_p.add_argument("--accessed-at", default=None, help="Override accessed_at timestamp (ISO format)")
        store_p.add_argument("--project", default=None, help="Project name this fact belongs to (stored in attributes)")
        store_p.add_argument("--is-technical", action="store_true", help="Mark as technical/project implementation detail")
        store_p.add_argument("--sensitivity", default=None, help="Sensitivity tag (private_health, financial, relationship_conflict, family_trauma, emotional_vulnerability)")
        store_p.add_argument("--sensitivity-handling", default=None, help="Handling guidance for sensitive facts")

        # --- forget ---
        forget_p = subparsers.add_parser("forget", help="Permanently delete a memory by ID or query")
        forget_p.add_argument("query", nargs="*", help="Search query to find memory to forget")
        forget_p.add_argument("--id", dest="node_id", default=None, help="Node ID to forget directly")

        # --- delete ---
        delete_p = subparsers.add_parser("delete", help="Soft-delete a memory")
        delete_p.add_argument("id", help="Node ID to delete")
        delete_p.add_argument("--reason", default="manual", help="Deletion reason (default: manual)")

        # --- get-node ---
        get_node_p = subparsers.add_parser("get-node", help="Get a memory node by ID")
        get_node_p.add_argument("id", help="Node ID")

        # --- get-edges ---
        get_edges_p = subparsers.add_parser("get-edges", help="Get edges for a node")
        get_edges_p.add_argument("id", help="Node ID")

        # --- create-edge ---
        create_edge_p = subparsers.add_parser("create-edge", help="Create a graph edge")
        create_edge_p.add_argument("subject", help="Subject node name")
        create_edge_p.add_argument("relation", help="Relation type")
        create_edge_p.add_argument("object", help="Object node name")
        create_edge_p.add_argument("--owner", default=None, help="Owner ID")
        create_edge_p.add_argument("--source-fact-id", default=None, help="Source fact ID to link edge to")
        create_edge_p.add_argument("--create-missing", action="store_true",
                                   help="Create Person nodes for missing entities")
        create_edge_p.add_argument("--json", action="store_true", help="JSON output")

        # --- delete-edges-by-fact ---
        del_edges_p = subparsers.add_parser("delete-edges-by-fact", help="Delete edges linked to a source fact")
        del_edges_p.add_argument("fact_id", help="Source fact ID")

        # --- fact-history ---
        history_p = subparsers.add_parser("fact-history", help="Show fact evolution history (supersedes chain)")
        history_p.add_argument("id", help="Node ID")

        # --- recall ---
        recall_p = subparsers.add_parser("recall", help="Recall memories (same as search)")
        recall_p.add_argument("query", nargs="+", help="Search query")
        recall_p.add_argument("--owner", default=None, help="Owner ID")
        recall_p.add_argument("--limit", type=int, default=5, help="Max results (default: 5)")
        recall_p.add_argument("--min-similarity", type=float, default=0.60, help="Min similarity threshold (default: 0.60)")
        recall_p.add_argument("--technical-scope", default="any", choices=["personal", "technical", "any"], help="Filter by technical flag (default: any)")
        recall_p.add_argument("--project", default=None, help="Filter by project/domain label")
        recall_p.add_argument("--debug", action="store_true", help="Show scoring breakdown for each result")

        # --- decay ---
        subparsers.add_parser("decay", help="Run memory decay")

        # --- edge-keywords ---
        ek_p = subparsers.add_parser("edge-keywords", help="Edge keywords management")
        ek_sub = ek_p.add_subparsers(dest="subcmd", help="Subcommand")

        ek_sub.add_parser("list", help="List all edge keywords")
        ek_sub.add_parser("seed", help="Seed keywords from existing edge relations")

        ek_check_p = ek_sub.add_parser("check", help="Check if a query would trigger graph expansion")
        ek_check_p.add_argument("query", nargs="+", help="Query to check")

        ek_add_p = ek_sub.add_parser("add", help="Add keywords for a relation")
        ek_add_p.add_argument("relation", help="Relation name")
        ek_add_p.add_argument("keywords", help="Comma-separated keywords")

        ek_gen_p = ek_sub.add_parser("generate", help="Generate keywords for a relation using LLM")
        ek_gen_p.add_argument("relation", help="Relation name")

        # --- add-alias ---
        add_alias_p = subparsers.add_parser("add-alias", help="Add an entity alias")
        add_alias_p.add_argument("alias", help="The alias name (e.g., 'Sol')")
        add_alias_p.add_argument("canonical", help="The canonical name (e.g., 'Alice Smith')")
        add_alias_p.add_argument("--node-id", default=None, help="Optional canonical node ID")
        add_alias_p.add_argument("--owner", default=None, help="Owner ID")

        # --- list-aliases ---
        list_aliases_p = subparsers.add_parser("list-aliases", help="List entity aliases")
        list_aliases_p.add_argument("--owner", default=None, help="Filter by owner")

        # --- delete-alias ---
        delete_alias_p = subparsers.add_parser("delete-alias", help="Delete an entity alias by ID")
        delete_alias_p.add_argument("alias_id", help="Alias ID to delete")

        # --- summarize-entity ---
        sum_entity_p = subparsers.add_parser("summarize-entity", help="Generate summary for an entity node")
        sum_entity_p.add_argument("node_id", help="Entity node ID to summarize")
        sum_entity_p.add_argument("--no-llm", action="store_true", help="Skip LLM, use simple concatenation")

        # --- recall-stats ---
        recall_stats_p = subparsers.add_parser("recall-stats", help="Show recall observability stats")
        recall_stats_p.add_argument("--days", type=int, default=7, help="Number of days to analyze (default: 7)")

        # --- health-trend ---
        health_trend_p = subparsers.add_parser("health-trend", help="Show health snapshots over time")
        health_trend_p.add_argument("--limit", type=int, default=10, help="Max snapshots to show (default: 10)")

        # --- dedup-audit ---
        subparsers.add_parser("dedup-audit", help="Audit dedup_log for accuracy metrics")

        # --- flagged ---
        flagged_p = subparsers.add_parser("flagged", help="List/manage facts flagged by injection blocklist")
        flagged_p.add_argument("--approve", metavar="NODE_ID", help="Move flagged fact to pending status")
        flagged_p.add_argument("--reject", metavar="NODE_ID", help="Hard-delete a flagged fact")

        # --- summarize-all ---
        sum_all_p = subparsers.add_parser("summarize-all", help="Generate summaries for all entity nodes")
        sum_all_p.add_argument("--owner", default=None, help="Owner ID to filter entities")
        sum_all_p.add_argument("--no-llm", action="store_true", help="Skip LLM, use simple concatenation")
        sum_all_p.add_argument("--types", default=None, help="Comma-separated entity types (default: Person,Place,Concept)")

        # --- detect-provider ---
        subparsers.add_parser("detect-provider", help="Show current LLM and embeddings provider status")

        # Parse args
        args = parser.parse_args()

        if not args.command:
            graph = get_graph()
            print("Local Graph Memory System")
            print(json.dumps(graph.get_stats(), indent=2))
            return

        # --- Command handlers ---

        if args.command == "init":
            initialize_db()
            print("Database initialized")

        elif args.command == "stats":
            graph = get_graph()
            print(json.dumps(graph.get_stats(), indent=2))

        elif args.command == "health":
            graph = get_graph()
            print(json.dumps(graph.get_health_metrics(), indent=2))

        elif args.command == "backfill-hashes":
            graph = get_graph()
            with graph._get_conn() as conn:
                rows = conn.execute(
                    "SELECT id, name FROM nodes WHERE content_hash IS NULL"
                ).fetchall()
            if args.dry_run:
                print(f"Would backfill {len(rows)} content hashes:")
                for row in rows:
                    h = content_hash(row["name"])
                    print(f"  ID {row['id']}: {row['name'][:60]}... -> {h[:16]}...")
                print(f"\nTotal: {len(rows)} (dry run, no changes made)")
            else:
                count = 0
                with graph._get_conn() as conn:
                    for row in rows:
                        h = content_hash(row["name"])
                        conn.execute(
                            "UPDATE nodes SET content_hash = ? WHERE id = ?",
                            (h, row["id"])
                        )
                        count += 1
                print(f"Backfilled {count} content hashes")

        elif args.command == "search":
            query = " ".join(args.query)

            if args.archive:
                from lib.archive import search_archive as _search_archive
                archive_results = _search_archive(query, limit=args.limit)
                if args.json:
                    out = []
                    for r in archive_results:
                        out.append({
                            "text": r.get("name", ""),
                            "category": r.get("type", "?"),
                            "similarity": 1.0,
                            "id": r.get("id", ""),
                            "created_at": r.get("archived_at", ""),
                            "valid_from": "",
                            "valid_until": "",
                            "privacy": r.get("privacy", "shared"),
                            "owner_id": r.get("owner_id", ""),
                            "source_type": r.get("source_type", "archive"),
                            "via": "archive",
                            "archive_reason": r.get("archive_reason", ""),
                        })
                    print(json.dumps(out))
                else:
                    for r in archive_results:
                        print(f"[archive] [{r.get('type', '?')}] {r.get('name', '')} |ID:{r.get('id', '')}|archived:{r.get('archived_at', '')}|reason:{r.get('archive_reason', '')}")
                    if not archive_results:
                        print("No archived memories found")
            elif getattr(args, 'session_id', None):
                # Session-filtered search: return facts from a specific session
                mg = MemoryGraph()
                with mg._get_conn() as conn:
                    rows = conn.execute(
                        "SELECT id, type, name, content, extraction_confidence, created_at, privacy, owner_id, session_id "
                        "FROM nodes WHERE session_id = ? AND owner_id = ? AND status IN ('active', 'approved', 'pending') "
                        "ORDER BY created_at DESC LIMIT ?",
                        (args.session_id, args.owner or _get_memory_config().users.default_owner, int(args.limit))
                    ).fetchall()
                if args.json:
                    out = []
                    for r in rows:
                        out.append({
                            "text": r["content"] or r["name"],
                            "category": r["type"],
                            "similarity": 1.0,
                            "id": r["id"],
                            "created_at": r["created_at"] or "",
                            "valid_from": "",
                            "valid_until": "",
                            "privacy": r["privacy"] or "shared",
                            "owner_id": r["owner_id"] or "",
                            "source_type": "",
                        })
                    print(json.dumps(out))
                else:
                    for r in rows:
                        conf = r['extraction_confidence'] or 0.5
                        created = r['created_at'] or ''
                        date_str = f"({created.split('T')[0]})" if created else ""
                        print(f"[1.00] [{r['type']}]{date_str}[C:{conf:.1f}] {r['content'] or r['name']} |ID:{r['id']}|T:{created}|VF:|VU:|P:{r['privacy'] or 'shared'}|O:{r['owner_id'] or ''}")
                    if not rows:
                        print("No facts found for this session")
            else:
                results = recall(query, limit=args.limit, owner_id=args.owner, min_similarity=args.min_similarity, current_session_id=args.current_session_id, compaction_time=args.compaction_time, date_from=getattr(args, 'date_from', None), date_to=getattr(args, 'date_to', None), debug=getattr(args, 'debug', False), technical_scope=getattr(args, 'technical_scope', 'any'), project=getattr(args, 'project', None))
                if args.json:
                    print(json.dumps(results))
                else:
                    # Output format: [similarity] [category](date)[flags][C:confidence] text |ID:id|T:created_at|P:privacy|O:owner_id
                    for r in results:
                        flags = []
                        if r.get('verified'): flags.append('V')
                        if r.get('pinned'): flags.append('P')
                        if r.get('valid_until'): flags.append('superseded')
                        flag_str = f"[{''.join(flags)}]" if flags else ""
                        conf = r.get('extraction_confidence', 0.5)
                        created = r.get('created_at', '')
                        date_str = f"({created.split('T')[0]})" if created else ""
                        privacy = r.get('privacy', 'shared')
                        owner = r.get('owner_id', '')
                        valid_from = r.get('valid_from', '')
                        valid_until = r.get('valid_until', '')
                        source_type = r.get('source_type', '') or ''
                        print(f"[{r['similarity']:.2f}] [{r['category']}]{date_str}{flag_str}[C:{conf:.1f}] {r['text']} |ID:{r['id']}|T:{created}|VF:{valid_from}|VU:{valid_until}|P:{privacy}|O:{owner}|ST:{source_type}")
                        if r.get('_debug'):
                            d = r['_debug']
                            print(f"  [debug] raw_quality={d['raw_quality_score']} composite={d['composite_score']} intent={d['intent']} type_boost={d['type_boost']} conf={d['confidence']} access={d['access_count']} confirms={d['confirmation_count']}")

        elif args.command == "store":
            try:
                owner = args.owner or _get_memory_config().users.default_owner
                result = store(
                    args.text,
                    category=args.category,
                    verified=args.verified,
                    pinned=args.pinned,
                    confidence=args.confidence,
                    privacy=args.privacy,
                    extraction_confidence=args.extraction_confidence,
                    source=args.source,
                    owner_id=owner,
                    session_id=args.session_id,
                    skip_dedup=args.skip_dedup,
                    speaker=args.speaker,
                    status=args.status,
                    knowledge_type=args.knowledge_type,
                    keywords=args.keywords,
                    source_type=args.source_type,
                    is_technical=args.is_technical,
                    project=getattr(args, 'project', None),
                    created_at=getattr(args, 'created_at', None),
                    accessed_at=getattr(args, 'accessed_at', None),
                )
                # Add project tagging to attributes if specified.
                # Apply on ALL statuses (created, duplicate, updated) — dedup
                # returns existing node ID, and we still want to tag it.
                tag_node_id = result.get("id") or result.get("existing_id")
                if tag_node_id and (args.project or args.is_technical or args.sensitivity):
                    graph = get_graph()
                    node = graph.get_node(tag_node_id)
                    if node:
                        attrs = node.attributes if isinstance(node.attributes, dict) else {}
                        if args.project:
                            attrs["project"] = _normalize_project_tag(args.project)
                        if args.is_technical:
                            attrs["is_technical"] = True
                        if args.sensitivity:
                            attrs["sensitivity"] = args.sensitivity
                        if args.sensitivity_handling:
                            attrs["sensitivity_handling"] = args.sensitivity_handling
                        node.attributes = attrs
                        graph.update_node(node)
                if result["status"] == "created":
                    print(f"Stored: {result['id']}")
                elif result["status"] == "duplicate":
                    print(f"Duplicate (similarity: {result['similarity']}) [{result['id']}]: {result['existing_text'][:80]}")
                elif result["status"] == "updated":
                    print(f"Updated existing: {result['id']}")
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

        elif args.command == "get-node":
            node_id = args.id
            result = get_memory(node_id)
            if result:
                print(json.dumps(result, indent=2))
            else:
                print("Memory not found", file=sys.stderr)
                sys.exit(1)

        elif args.command == "delete":
            if soft_delete(args.id, args.reason):
                print(f"Deleted memory: {args.id}")
            else:
                print(f"Memory not found: {args.id}")  # Print to stdout, don't error

        elif args.command == "forget":
            if args.node_id:
                if forget(node_id=args.node_id):
                    print(f"Permanently deleted: {args.node_id}")
                else:
                    print("Memory not found", file=sys.stderr)
                    sys.exit(1)
            elif args.query:
                query = " ".join(args.query)
                if forget(query=query):
                    print(f"Deleted memory matching: {query}")
                else:
                    print("No matching memory found", file=sys.stderr)
                    sys.exit(1)
            else:
                print("Error: provide --id <id> or a search query", file=sys.stderr)
                sys.exit(1)

        elif args.command == "search-all":
            query = " ".join(args.query)
            memory_results = recall(query, limit=args.limit, owner_id=args.owner)
            for r in memory_results:
                flags = []
                if r.get('verified'): flags.append('V')
                if r.get('pinned'): flags.append('P')
                flag_str = f"[{''.join(flags)}]" if flags else ""
                print(f"[memory] [{r['similarity']:.2f}] [{r['category']}]{flag_str} {r['text']}")
            if not memory_results:
                print("No results found")

        elif args.command == "search-graph":
            query = " ".join(args.query)

            # Search memory first
            memory_results = recall(query, limit=args.limit, owner_id=args.owner)

            # Create MemoryGraph instance to get relationships
            graph = MemoryGraph()

            # Track already seen nodes to avoid duplicates
            seen_node_ids = {r.get('id') for r in memory_results if r.get('id')}

            # Print original search results
            for r in memory_results:
                flags = []
                if r.get('verified'): flags.append('V')
                if r.get('pinned'): flags.append('P')
                flag_str = f"[{''.join(flags)}]" if flags else ""
                print(f"[memory] [{r['similarity']:.2f}] [{r['category']}]{flag_str} {r['text']}")

            # For each result, find related nodes via graph traversal
            for result in memory_results:
                if result.get('id'):
                    try:
                        related_nodes = graph.get_related(result['id'], depth=1)
                        for node, relation, depth in related_nodes:
                            # Skip if we've already shown this node
                            if node.id not in seen_node_ids:
                                seen_node_ids.add(node.id)
                                print(f"[related:{relation}] {node.name}")
                    except Exception as e:
                        # Don't fail the whole search if graph traversal fails
                        print(f"[graph-error] Failed to get relations for {result['id']}: {e}", file=sys.stderr)

            if not memory_results:
                print("No results found")

        elif args.command == "search-graph-aware":
            query = " ".join(args.query)

            results = graph_aware_recall(
                query,
                owner_id=args.owner,
                limit=args.limit,
                graph_depth=args.depth,
                technical_scope=getattr(args, 'technical_scope', 'any'),
                project=getattr(args, 'project', None),
            )

            if args.json:
                print(json.dumps(results, indent=2))
            else:
                # Human-readable output format
                for r in results["direct_results"]:
                    flags = []
                    if r.get('verified'): flags.append('V')
                    if r.get('pinned'): flags.append('P')
                    flag_str = f"[{''.join(flags)}]" if flags else ""
                    print(f"[direct] [{r['similarity']:.2f}] [{r['category']}]{flag_str} {r['text']}")

                for r in results["graph_results"]:
                    direction = "-->" if r["direction"] == "out" else "<--"
                    rel = r["relation"]
                    source = r["source_name"]
                    target = r["name"]
                    if r["direction"] == "in":
                        # Reverse for display: source --rel--> target
                        print(f"[graph] {target} --{rel}--> {source}")
                    else:
                        print(f"[graph] {source} --{rel}--> {target}")

                # Metadata line
                meta = results["source_breakdown"]
                pronoun_str = "true" if meta["pronoun_resolved"] else "false"
                owner_person_str = f' owner_person="{meta["owner_person"]}"' if meta["owner_person"] else ""
                print(f"[meta] pronoun_resolved={pronoun_str}{owner_person_str}")
                print(f"[meta] direct_count={meta['vector_count']} graph_count={meta['graph_count']}")

                if not results["direct_results"] and not results["graph_results"]:
                    print("No results found")

        elif args.command == "edge-keywords":
            subcmd = args.subcmd or "list"

            if subcmd == "list":
                keywords = get_edge_keywords()
                if not keywords:
                    print("No edge keywords stored yet. Run 'edge-keywords seed' to generate.")
                else:
                    print(f"Edge keywords ({len(keywords)} relations):\n")
                    for relation, kws in sorted(keywords.items()):
                        print(f"  {relation}: {', '.join(kws)}")

            elif subcmd == "seed":
                print("Seeding keywords for existing edge relations...")
                count = seed_edge_keywords_from_db()
                print(f"Generated keywords for {count} relations")

            elif subcmd == "check":
                test_query = " ".join(args.query)
                result = should_expand_graph(test_query)
                print(f"Query: \"{test_query}\"")
                print(f"Should expand graph: {result}")

            elif subcmd == "add":
                keywords = [k.strip().lower() for k in args.keywords.split(",")]
                if store_edge_keywords(args.relation, keywords):
                    print(f"Stored keywords for '{args.relation}': {keywords}")
                else:
                    print(f"Failed to store keywords for '{args.relation}'")
                    sys.exit(1)

            elif subcmd == "generate":
                print(f"Generating keywords for '{args.relation}'...")
                keywords = generate_keywords_for_relation(args.relation)
                if keywords:
                    store_edge_keywords(args.relation, keywords)
                    print(f"Generated and stored: {keywords}")
                else:
                    print("Failed to generate keywords")
                    sys.exit(1)

        elif args.command == "decay":
            result = decay_memories()
            print(f"Decayed {result['decayed_count']} memories")

        elif args.command == "create-edge":
            create_missing = getattr(args, 'create_missing', False)
            result = create_edge(args.subject, args.relation, args.object, source_fact_id=args.source_fact_id, create_missing_entities=create_missing, owner_id=args.owner)

            if args.json:
                print(json.dumps(result))
            else:
                if result["status"] == "created":
                    print(f"Created edge: {args.subject} --{args.relation}--> {args.object}")
                    print(f"Edge ID: {result['edge_id']}")
                else:
                    print(f"Error: {result.get('message', 'Unknown error')}", file=sys.stderr)
                    sys.exit(1)

        elif args.command == "delete-edges-by-fact":
            count = delete_edges_by_source_fact(args.fact_id)
            print(f"Deleted {count} edges linked to fact {args.fact_id}")

        elif args.command == "get-edges":
            graph = get_graph()
            edges = graph.get_edges(args.id)
            if edges:
                print(json.dumps([asdict(e) for e in edges], indent=2))
            else:
                print("No edges found for node", file=sys.stderr)

        elif args.command == "fact-history":
            graph = get_graph()
            history = graph.get_fact_history(args.id)
            if history:
                for i, node in enumerate(history):
                    marker = " (current)" if node.superseded_by is None else f" → superseded by {node.superseded_by[:8]}"
                    print(f"  {i+1}. [{node.created_at}] {node.name} [C:{node.confidence:.1f}]{marker}")
            else:
                print("No history found for node", file=sys.stderr)

        elif args.command == "recall":
            query = " ".join(args.query)
            results = recall(query, limit=args.limit, owner_id=args.owner, min_similarity=args.min_similarity, debug=getattr(args, 'debug', False), technical_scope=getattr(args, 'technical_scope', 'any'), project=getattr(args, 'project', None))

            for r in results:
                flags = []
                if r.get('verified'): flags.append('V')
                if r.get('pinned'): flags.append('P')
                flag_str = f"[{''.join(flags)}]" if flags else ""
                conf = r.get('extraction_confidence', 0.5)
                created = r.get('created_at', '')
                privacy = r.get('privacy', 'shared')
                owner = r.get('owner_id', '')
                print(f"[{r['similarity']:.2f}] [{r['category']}]{flag_str}[C:{conf:.1f}] {r['text']} |ID:{r['id']}|T:{created}|P:{privacy}|O:{owner}")
                if r.get('_debug'):
                    d = r['_debug']
                    print(f"  [debug] raw_quality={d['raw_quality_score']} composite={d['composite_score']} intent={d['intent']} type_boost={d['type_boost']} conf={d['confidence']} access={d['access_count']} confirms={d['confirmation_count']}")

        elif args.command == "add-alias":
            graph = get_graph()
            alias_id = graph.add_alias(args.alias, args.canonical, canonical_node_id=args.node_id, owner_id=args.owner)
            print(f"Added alias: '{args.alias}' -> '{args.canonical}' (ID: {alias_id})")

        elif args.command == "list-aliases":
            graph = get_graph()
            aliases = graph.get_aliases(owner_id=args.owner)
            if aliases:
                for a in aliases:
                    node_str = f" [node:{a['canonical_node_id']}]" if a['canonical_node_id'] else ""
                    owner_str = f" (owner:{a['owner_id']})" if a['owner_id'] else ""
                    print(f"  {a['alias']} -> {a['canonical_name']}{node_str}{owner_str} |ID:{a['id']}")
            else:
                print("No aliases found")

        elif args.command == "delete-alias":
            graph = get_graph()
            if graph.delete_alias(args.alias_id):
                print(f"Deleted alias: {args.alias_id}")
            else:
                print(f"Alias not found: {args.alias_id}", file=sys.stderr)
                sys.exit(1)

        elif args.command == "recall-stats":
            graph = get_graph()
            days = args.days
            with graph._get_conn() as conn:
                cutoff = (datetime.now() - timedelta(days=days)).isoformat()
                rows = conn.execute("""
                    SELECT COUNT(*) as total,
                           ROUND(AVG(results_count), 1) as avg_results,
                           ROUND(AVG(avg_similarity), 3) as avg_sim,
                           ROUND(AVG(top_similarity), 3) as avg_top_sim,
                           ROUND(AVG(latency_ms), 0) as avg_latency_ms,
                           SUM(multi_pass_triggered) as multi_pass_count,
                           SUM(fts_fallback_used) as fts_fallback_count,
                           SUM(reranker_used) as reranker_count,
                           ROUND(AVG(CASE WHEN reranker_used THEN reranker_changes END), 1) as avg_reranker_changes,
                           SUM(CASE WHEN reranker_used THEN reranker_top1_changed ELSE 0 END) as top1_changed_count,
                           ROUND(AVG(CASE WHEN reranker_used THEN reranker_avg_displacement END), 2) as avg_displacement,
                           SUM(graph_discoveries) as total_graph_discoveries
                    FROM recall_log WHERE created_at >= ?
                """, (cutoff,)).fetchone()

                if not rows or rows["total"] == 0:
                    print(f"No recall logs in the last {days} days")
                else:
                    total = rows["total"]
                    reranker_count = rows['reranker_count'] or 0
                    print(f"Recall Stats (last {days} days)")
                    print(f"{'='*50}")
                    print(f"Total recalls:           {total}")
                    print(f"Avg results/recall:      {rows['avg_results']}")
                    print(f"Avg similarity:          {rows['avg_sim']}")
                    print(f"Avg top similarity:      {rows['avg_top_sim']}")
                    print(f"Avg latency:             {int(rows['avg_latency_ms'] or 0)}ms")
                    print(f"Multi-pass triggered:    {rows['multi_pass_count']}/{total} ({rows['multi_pass_count']*100//total}%)")
                    print(f"FTS fallback used:       {rows['fts_fallback_count']}/{total} ({rows['fts_fallback_count']*100//total}%)")
                    print(f"Reranker used:           {reranker_count}/{total} ({reranker_count*100//total}%)")
                    print(f"Avg reranker changes:    {rows['avg_reranker_changes'] or 0}")
                    if reranker_count > 0:
                        top1_count = rows['top1_changed_count'] or 0
                        print(f"Reranker top-1 changed:  {top1_count}/{reranker_count} ({top1_count*100//reranker_count}%)")
                        print(f"Avg rank displacement:   {rows['avg_displacement'] or 0}")
                    print(f"Graph discoveries:       {rows['total_graph_discoveries']}")

                    # Intent breakdown
                    intent_rows = conn.execute("""
                        SELECT intent, COUNT(*) as cnt FROM recall_log
                        WHERE created_at >= ? AND intent IS NOT NULL
                        GROUP BY intent ORDER BY cnt DESC
                    """, (cutoff,)).fetchall()
                    if intent_rows:
                        print(f"\nIntent distribution:")
                        for row in intent_rows:
                            print(f"  {row['intent']}: {row['cnt']}")

                    # Per-intent reranker breakdown
                    if reranker_count > 0:
                        reranker_by_intent = conn.execute("""
                            SELECT intent,
                                   COUNT(*) as cnt,
                                   SUM(reranker_top1_changed) as top1_changed,
                                   ROUND(AVG(reranker_avg_displacement), 1) as avg_disp
                            FROM recall_log
                            WHERE created_at >= ? AND reranker_used = 1 AND intent IS NOT NULL
                            GROUP BY intent ORDER BY cnt DESC
                        """, (cutoff,)).fetchall()
                        if reranker_by_intent:
                            print(f"\nReranker by intent:")
                            for row in reranker_by_intent:
                                t1 = row['top1_changed'] or 0
                                disp = row['avg_disp'] or 0
                                print(f"  {row['intent']:15s} {t1}/{row['cnt']} top-1 changed, avg displacement {disp}")

        elif args.command == "health-trend":
            graph = get_graph()
            with graph._get_conn() as conn:
                rows = conn.execute("""
                    SELECT * FROM health_snapshots
                    ORDER BY created_at DESC LIMIT ?
                """, (args.limit,)).fetchall()

                if not rows:
                    print("No health snapshots yet. Run janitor to generate.")
                else:
                    print(f"Health Trend (last {len(rows)} snapshots)")
                    print(f"{'='*70}")
                    for row in reversed(rows):  # Oldest first
                        print(f"\n[{row['created_at']}]")
                        print(f"  Nodes: {row['total_nodes']}  Edges: {row['total_edges']}  Avg conf: {row['avg_confidence']:.3f}")
                        print(f"  Orphans: {row['orphan_count']}  Embedding coverage: {row['embedding_coverage']:.1f}%")
                        if row['nodes_by_status']:
                            print(f"  Status: {row['nodes_by_status']}")
                        if row['confidence_distribution']:
                            print(f"  Confidence: {row['confidence_distribution']}")
                        if row['staleness_distribution']:
                            print(f"  Staleness: {row['staleness_distribution']}")

        elif args.command == "dedup-audit":
            graph = get_graph()
            with graph._get_conn() as conn:
                # Decision distribution
                decisions = conn.execute("""
                    SELECT decision, COUNT(*) as cnt FROM dedup_log
                    GROUP BY decision ORDER BY cnt DESC
                """).fetchall()

                total_dedup = sum(r["cnt"] for r in decisions)
                if not total_dedup:
                    print("No dedup log entries found")
                else:
                    print(f"Dedup Audit ({total_dedup} total entries)")
                    print(f"{'='*50}")
                    print(f"\nDecision distribution:")
                    for row in decisions:
                        pct = row["cnt"] * 100 // total_dedup
                        print(f"  {row['decision']}: {row['cnt']} ({pct}%)")

                    # Review status
                    reviews = conn.execute("""
                        SELECT review_status, COUNT(*) as cnt FROM dedup_log
                        GROUP BY review_status ORDER BY cnt DESC
                    """).fetchall()
                    print(f"\nReview status:")
                    for row in reviews:
                        print(f"  {row['review_status']}: {row['cnt']}")

                    reversed_count = sum(r["cnt"] for r in reviews if r["review_status"] == "reversed")
                    reviewed_count = sum(r["cnt"] for r in reviews if r["review_status"] != "unreviewed")
                    if reviewed_count > 0:
                        print(f"\nReversal rate: {reversed_count}/{reviewed_count} ({reversed_count*100//reviewed_count}%)")

                    # Gray zone analysis (0.88-0.95 similarity)
                    gray_zone = conn.execute("""
                        SELECT COUNT(*) as cnt,
                               ROUND(AVG(similarity), 3) as avg_sim,
                               decision,
                               COUNT(CASE WHEN review_status = 'reversed' THEN 1 END) as reversals
                        FROM dedup_log
                        WHERE similarity BETWEEN 0.88 AND 0.95
                        GROUP BY decision
                    """).fetchall()
                    if gray_zone:
                        total_gray = sum(r["cnt"] for r in gray_zone)
                        print(f"\nGray zone (0.88-0.95 similarity): {total_gray} entries")
                        for row in gray_zone:
                            print(f"  {row['decision']}: {row['cnt']} (avg sim: {row['avg_sim']}, reversals: {row['reversals']})")

                    # Sample LLM reasoning
                    samples = conn.execute("""
                        SELECT new_text, existing_text, similarity, decision, llm_reasoning
                        FROM dedup_log
                        WHERE llm_reasoning IS NOT NULL
                        ORDER BY created_at DESC LIMIT 5
                    """).fetchall()
                    if samples:
                        print(f"\nRecent LLM reasoning samples:")
                        for s in samples:
                            print(f"\n  [{s['decision']}] sim={s['similarity']:.3f}")
                            print(f"  New:      {s['new_text'][:80]}")
                            print(f"  Existing: {s['existing_text'][:80]}")
                            print(f"  Reason:   {s['llm_reasoning'][:120]}")

        elif args.command == "summarize-entity":
            summary = generate_entity_summary(args.node_id, use_llm=not args.no_llm)
            if summary:
                node = get_graph().get_node(args.node_id)
                node_name = node.name if node else args.node_id
                print(f"Summary for {node_name}:")
                print(summary)
            else:
                print(f"Could not generate summary for node {args.node_id}", file=sys.stderr)
                print("Node may not exist, may not be a Person/Place/Concept, or may have no connected facts.", file=sys.stderr)
                sys.exit(1)

        elif args.command == "summarize-all":
            entity_types = None
            if args.types:
                entity_types = [t.strip() for t in args.types.split(",")]
            stats = summarize_all_entities(
                owner_id=args.owner,
                use_llm=not args.no_llm,
                entity_types=entity_types
            )
            print(json.dumps(stats, indent=2))

        elif args.command == "flagged":
            graph = get_graph()
            if args.approve:
                node = graph.get_node(args.approve)
                if not node:
                    print(f"Node not found: {args.approve}", file=sys.stderr)
                    sys.exit(1)
                if node.status != "flagged":
                    print(f"Node {args.approve} is not flagged (status: {node.status})", file=sys.stderr)
                    sys.exit(1)
                node.status = "pending"
                graph.update_node(node)
                print(f"Approved: {args.approve} -> pending")
            elif args.reject:
                node = graph.get_node(args.reject)
                if not node:
                    print(f"Node not found: {args.reject}", file=sys.stderr)
                    sys.exit(1)
                if node.status != "flagged":
                    print(f"Node {args.reject} is not flagged (status: {node.status})", file=sys.stderr)
                    sys.exit(1)
                graph.hard_delete_node(args.reject)
                print(f"Rejected and deleted: {args.reject}")
            else:
                with graph._get_conn() as conn:
                    rows = conn.execute("""
                        SELECT id, name, attributes, created_at
                        FROM nodes WHERE status = 'flagged'
                        ORDER BY created_at DESC
                    """).fetchall()
                if not rows:
                    print("No flagged facts")
                else:
                    print(f"Flagged facts ({len(rows)}):")
                    for row in rows:
                        attrs = json.loads(row["attributes"]) if row["attributes"] else {}
                        pattern = attrs.get("flagged_pattern", "?")
                        print(f"  [{row['created_at']}] {row['name'][:80]}")
                        print(f"    ID: {row['id']}  Pattern: {pattern}")

        elif args.command == "detect-provider":
            from lib.embeddings import get_embeddings_provider
            adapter = get_adapter_instance()
            adapter_name = type(adapter).__name__
            try:
                llm = adapter.get_llm_provider()
                llm_name = type(llm).__name__
                profiles = llm.get_profiles()
            except Exception as e:
                llm_name = f"ERROR: {e}"
                profiles = {}
            embed = get_embeddings_provider()
            print("LLM:")
            print(f"  Adapter:    {adapter_name}")
            print(f"  Provider:   {llm_name}")
            deep_profile = profiles.get("deep") or profiles.get("high")
            fast_profile = profiles.get("fast") or profiles.get("low")
            if isinstance(deep_profile, dict):
                print(f"  Deep:       {deep_profile.get('model', '?')}")
            if isinstance(fast_profile, dict):
                print(f"  Fast:       {fast_profile.get('model', '?')}")
            print()
            print("Embeddings:")
            print(f"  Provider:   {type(embed).__name__}")
            print(f"  Model:      {embed.model_name} ({embed.dimension()}-dim)")

    try:
        main()
    except Exception as e:
        import traceback
        print(f"Error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
