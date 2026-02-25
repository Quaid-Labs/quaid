from pathlib import Path

from datastore.memorydb.memory_graph import MemoryGraph


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

        node_cols = _table_columns(conn, "nodes")
        assert "speaker_entity_id" in node_cols
        assert "conversation_id" in node_cols
        assert "visibility_scope" in node_cols
        assert "sensitivity" in node_cols
        assert "provenance_confidence" in node_cols

        alias_cols = _table_columns(conn, "entity_aliases")
        assert "entity_id" in alias_cols
        assert "platform" in alias_cols
        assert "source_id" in alias_cols
        assert "handle" in alias_cols

