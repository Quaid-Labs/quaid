import sqlite3
from pathlib import Path

from datastore.memorydb.memory_graph import Edge, MemoryGraph, Node


def _table_columns(conn, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows}


def _index_names(conn, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {str(r[1]) for r in rows}


def _primary_key_columns(conn, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(r[1]) for r in sorted((r for r in rows if int(r[5] or 0)), key=lambda r: int(r[5]))]


def test_memory_graph_initializes_multi_user_foundation_schema(tmp_path):
    db_path = Path(tmp_path) / "memory.db"
    graph = MemoryGraph(db_path=db_path)
    with graph._get_conn() as conn:
        tables = {
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "entities" in tables
        assert "sources" in tables
        assert "source_participants" in tables
        assert "identity_credentials" in tables
        assert "identity_sessions" in tables
        assert "delegation_grants" in tables
        assert "trust_assertions" in tables
        assert "policy_audit_log" in tables

        node_cols = _table_columns(conn, "nodes")
        assert "speaker_entity_id" in node_cols
        assert "conversation_id" in node_cols
        assert "visibility_scope" in node_cols
        assert "sensitivity" in node_cols
        assert "provenance_confidence" in node_cols
        assert "origin_package_id" in node_cols
        assert "origin_version_id" in node_cols

        edge_cols = _table_columns(conn, "edges")
        assert "origin_package_id" in edge_cols
        assert "origin_version_id" in edge_cols

        node_indexes = _index_names(conn, "nodes")
        assert "idx_nodes_superseded_by" in node_indexes
        assert _primary_key_columns(conn, "embedding_cache") == ["text_hash", "model"]

        alias_cols = _table_columns(conn, "entity_aliases")
        assert "entity_id" in alias_cols
        assert "platform" in alias_cols
        assert "source_id" in alias_cols
        assert "handle" in alias_cols


def test_memory_graph_initializes_empty_vec_index_when_sqlite_vec_available(tmp_path):
    from datastore.memorydb.memory_graph import _lib_has_vec

    if not _lib_has_vec():
        return

    db_path = Path(tmp_path) / "memory.db"
    graph = MemoryGraph(db_path=db_path)
    with graph._get_conn() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'vec_nodes'"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM vec_nodes").fetchone()[0]

    assert row is not None
    assert "vec0" in str(row[0])
    assert count == 0


def test_memory_graph_migrates_origin_columns_on_existing_db(tmp_path):
    db_path = Path(tmp_path) / "legacy-memory.db"
    schema_path = (
        Path(__file__).resolve().parent.parent / "datastore" / "memorydb" / "schema.sql"
    )
    legacy_schema = (
        schema_path.read_text()
        .replace("    origin_package_id TEXT,                 -- Imported package or lineage identifier\n", "")
        .replace("    origin_version_id TEXT,                 -- Imported package version identifier\n", "")
        .replace(
            "CREATE INDEX IF NOT EXISTS idx_nodes_origin_package_id ON nodes(origin_package_id);\n",
            "",
        )
        .replace(
            "CREATE INDEX IF NOT EXISTS idx_edges_origin_package_id ON edges(origin_package_id);\n",
            "",
        )
    )

    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_schema)

    graph = MemoryGraph(db_path=db_path)
    with graph._get_conn() as conn:
        node_cols = _table_columns(conn, "nodes")
        edge_cols = _table_columns(conn, "edges")

    assert "origin_package_id" in node_cols
    assert "origin_version_id" in node_cols
    assert "origin_package_id" in edge_cols
    assert "origin_version_id" in edge_cols


def test_memory_graph_migrates_superseded_by_index_on_existing_db(tmp_path):
    db_path = Path(tmp_path) / "legacy-memory.db"
    schema_path = (
        Path(__file__).resolve().parent.parent / "datastore" / "memorydb" / "schema.sql"
    )
    legacy_schema = schema_path.read_text().replace(
        "CREATE INDEX IF NOT EXISTS idx_nodes_superseded_by ON nodes(superseded_by);\n",
        "",
    )

    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_schema)
        assert "idx_nodes_superseded_by" not in _index_names(conn, "nodes")

    graph = MemoryGraph(db_path=db_path)
    with graph._get_conn() as conn:
        node_indexes = _index_names(conn, "nodes")

    assert "idx_nodes_superseded_by" in node_indexes


def test_memory_graph_migrates_embedding_cache_to_model_scoped_primary_key(tmp_path):
    db_path = Path(tmp_path) / "legacy-cache.db"
    schema_path = (
        Path(__file__).resolve().parent.parent / "datastore" / "memorydb" / "schema.sql"
    )
    legacy_schema = schema_path.read_text().replace(
        """CREATE TABLE IF NOT EXISTS embedding_cache (
    text_hash TEXT NOT NULL,                -- SHA256 of input text
    embedding BLOB NOT NULL,                -- Packed float32 array
    model TEXT NOT NULL,                    -- Embedding model used (from config: ollama.embeddingModel)
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (text_hash, model)
);
""",
        """CREATE TABLE IF NOT EXISTS embedding_cache (
    text_hash TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
""",
    )

    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_schema)
        conn.execute(
            "INSERT INTO embedding_cache (text_hash, embedding, model, created_at) VALUES (?, ?, ?, ?)",
            ("hash-one", b"packed", "old-model", "2026-01-02T03:04:05"),
        )
        assert _primary_key_columns(conn, "embedding_cache") == ["text_hash"]

    graph = MemoryGraph(db_path=db_path)
    with graph._get_conn() as conn:
        pk_columns = _primary_key_columns(conn, "embedding_cache")
        rows = conn.execute(
            "SELECT text_hash, embedding, model, created_at FROM embedding_cache"
        ).fetchall()

    assert pk_columns == ["text_hash", "model"]
    assert len(rows) == 1
    assert rows[0]["text_hash"] == "hash-one"
    assert rows[0]["embedding"] == b"packed"
    assert rows[0]["model"] == "old-model"
    assert rows[0]["created_at"] == "2026-01-02T03:04:05"


def test_memory_graph_repairs_partial_baseline_schema_when_nodes_exist(tmp_path):
    db_path = Path(tmp_path) / "partial-memory.db"
    schema_path = (
        Path(__file__).resolve().parent.parent / "datastore" / "memorydb" / "schema.sql"
    )

    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        for table in (
            "entity_aliases",
            "embedding_cache",
            "metadata",
            "edge_keywords",
            "identity_handles",
            "identity_credentials",
            "identity_sessions",
            "delegation_grants",
            "trust_assertions",
            "policy_audit_log",
            "recall_log",
            "health_snapshots",
            "doc_update_log",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

    graph = MemoryGraph(db_path=db_path)
    with graph._get_conn() as conn:
        tables = {
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert "nodes" in tables
    assert "entity_aliases" in tables
    assert "embedding_cache" in tables
    assert "metadata" in tables
    assert "identity_handles" in tables
    assert "recall_log" in tables
    assert "doc_update_log" in tables


def test_memory_graph_round_trips_import_provenance(tmp_path):
    db_path = Path(tmp_path) / "memory.db"
    graph = MemoryGraph(db_path=db_path)

    imported = Node.create(
        type="Fact",
        name="PTO carryover policy allows 5 days",
        origin_package_id="hr-agent",
        origin_version_id="2026.04",
    )
    target = Node.create(type="Concept", name="PTO policy")
    graph.add_node(imported, embed=False)
    graph.add_node(target, embed=False)

    edge = Edge.create(
        source_id=target.id,
        target_id=imported.id,
        relation="has_fact",
        origin_package_id="hr-agent",
        origin_version_id="2026.04",
    )
    graph.add_edge(edge)

    stored_node = graph.get_node(imported.id)
    stored_edge = next(e for e in graph.get_edges(target.id, direction="out") if e.id == edge.id)

    assert stored_node is not None
    assert stored_node.origin_package_id == "hr-agent"
    assert stored_node.origin_version_id == "2026.04"
    assert stored_edge.origin_package_id == "hr-agent"
    assert stored_edge.origin_version_id == "2026.04"
