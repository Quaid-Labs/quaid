import sqlite3
from pathlib import Path

from datastore.memorydb.memory_graph import Edge, MemoryGraph, Node


def _table_columns(conn, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1]) for r in rows}


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

        alias_cols = _table_columns(conn, "entity_aliases")
        assert "entity_id" in alias_cols
        assert "platform" in alias_cols
        assert "source_id" in alias_cols
        assert "handle" in alias_cols


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
