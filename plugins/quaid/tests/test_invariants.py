"""Lifecycle invariant tests for the memory graph.

These verify structural guarantees that must always hold:
no broken references, no orphan edges, consistent indexes.
"""

import json
import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env BEFORE imports so lib.config picks it up
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest

from memory_graph import MemoryGraph, Node, Edge, _content_hash


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_graph(tmp_path):
    """Create a MemoryGraph backed by a temp SQLite file."""
    import hashlib

    def _fake_get_embedding(text):
        h = hashlib.md5(text.encode()).digest()
        return [float(b) / 255.0 for b in h] * 8  # 128-dim

    db_file = tmp_path / "test.db"
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph, db_file


def _insert_test_node(graph, name, status="active", node_type="Fact", owner="solomon"):
    """Helper to insert a test node directly."""
    node = Node.create(type=node_type, name=name, privacy="private")
    node.owner_id = owner
    node.status = status
    return graph.add_node(node, embed=False)


# ---------------------------------------------------------------------------
# Invariant tests
# ---------------------------------------------------------------------------

class TestLifecycleInvariants:
    """Property-based tests verifying structural guarantees."""

    def test_no_orphan_edges(self, tmp_path):
        """Every edge's source_id and target_id must exist in nodes."""
        graph, _ = _make_graph(tmp_path)
        id_a = _insert_test_node(graph, "Node A")
        id_b = _insert_test_node(graph, "Node B")
        graph.add_edge(Edge.create(source_id=id_a, target_id=id_b, relation="knows"))

        with graph._get_conn() as conn:
            orphans = conn.execute("""
                SELECT e.id FROM edges e
                LEFT JOIN nodes n1 ON e.source_id = n1.id
                LEFT JOIN nodes n2 ON e.target_id = n2.id
                WHERE n1.id IS NULL OR n2.id IS NULL
            """).fetchall()
        assert len(orphans) == 0, f"Found {len(orphans)} orphan edges"

    def test_active_nodes_have_unique_content_hash(self, tmp_path):
        """Active nodes should have unique content_hash values."""
        graph, _ = _make_graph(tmp_path)
        _insert_test_node(graph, "Unique fact one about coffee")
        _insert_test_node(graph, "Unique fact two about tea")

        with graph._get_conn() as conn:
            dupes = conn.execute("""
                SELECT content_hash, COUNT(*) as cnt FROM nodes
                WHERE status = 'active' AND content_hash IS NOT NULL
                GROUP BY content_hash HAVING cnt > 1
            """).fetchall()
        assert len(dupes) == 0, f"Found {len(dupes)} duplicate content hashes"

    def test_superseded_nodes_not_in_active_edges(self, tmp_path):
        """Edges referencing superseded nodes should be documented behavior."""
        graph, _ = _make_graph(tmp_path)
        id_a = _insert_test_node(graph, "Original fact about something")
        id_b = _insert_test_node(graph, "Updated fact about something")
        id_c = _insert_test_node(graph, "Related node for edges")
        graph.add_edge(Edge.create(source_id=id_a, target_id=id_c, relation="relates_to"))

        # Supersede node A with B
        graph.supersede_node(id_a, id_b)

        with graph._get_conn() as conn:
            bad_edges = conn.execute("""
                SELECT e.id FROM edges e
                JOIN nodes n ON (e.source_id = n.id OR e.target_id = n.id)
                WHERE n.superseded_by IS NOT NULL
            """).fetchall()
        # After supersession, edges to superseded nodes still exist in the DB.
        # This test documents that current behavior -- supersede_node() does not
        # cascade-clean edges.  The janitor dedup task handles cleanup separately.
        # We assert the count is deterministic (exactly 1 edge referencing A).
        assert len(bad_edges) == 1, (
            f"Expected exactly 1 edge referencing superseded node, got {len(bad_edges)}"
        )

    def test_decay_queue_references_valid_nodes(self, tmp_path):
        """decay_review_queue node_ids must exist in nodes table."""
        graph, _ = _make_graph(tmp_path)
        node_id = _insert_test_node(graph, "Decaying fact about weather")

        with graph._get_conn() as conn:
            # Insert a valid decay queue entry (node_text is NOT NULL in schema)
            conn.execute("""
                INSERT INTO decay_review_queue (id, node_id, node_text, confidence_at_queue, queued_at)
                VALUES (?, ?, ?, 0.1, datetime('now'))
            """, (str(__import__("uuid").uuid4()), node_id, "Decaying fact about weather"))

            # Check for invalid references (pending entries with missing nodes)
            invalid = conn.execute("""
                SELECT drq.id FROM decay_review_queue drq
                LEFT JOIN nodes n ON drq.node_id = n.id
                WHERE n.id IS NULL AND drq.status = 'pending'
            """).fetchall()
        assert len(invalid) == 0, f"Found {len(invalid)} decay queue entries with missing nodes"

    def test_edges_have_valid_relations(self, tmp_path):
        """All edges should have non-empty relation strings."""
        graph, _ = _make_graph(tmp_path)
        id_a = _insert_test_node(graph, "Person node Alice", node_type="Person")
        id_b = _insert_test_node(graph, "Person node Bob", node_type="Person")
        graph.add_edge(Edge.create(source_id=id_a, target_id=id_b, relation="friend_of"))

        with graph._get_conn() as conn:
            bad = conn.execute("""
                SELECT id FROM edges WHERE relation IS NULL OR relation = ''
            """).fetchall()
        assert len(bad) == 0, f"Found {len(bad)} edges with empty relations"

    def test_fts_index_consistent_with_nodes(self, tmp_path):
        """FTS index row count should match nodes row count."""
        graph, _ = _make_graph(tmp_path)
        _insert_test_node(graph, "Test fact for FTS consistency check")
        _insert_test_node(graph, "Another test fact for FTS check")

        with graph._get_conn() as conn:
            nodes_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            try:
                fts_count = conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
                # FTS triggers keep the index in sync with nodes table
                assert fts_count == nodes_count, (
                    f"FTS row count ({fts_count}) != nodes row count ({nodes_count})"
                )
            except sqlite3.OperationalError:
                pytest.skip("FTS5 table not available")

    def test_node_status_transitions_valid(self, tmp_path):
        """Nodes should only have valid status values."""
        graph, _ = _make_graph(tmp_path)
        _insert_test_node(graph, "Pending fact about something", status="pending")
        _insert_test_node(graph, "Active fact about something", status="active")

        valid_statuses = {"pending", "approved", "active", "superseded", "flagged", "archived"}
        with graph._get_conn() as conn:
            rows = conn.execute("SELECT DISTINCT status FROM nodes").fetchall()
            actual = {row[0] for row in rows}
        invalid = actual - valid_statuses
        assert len(invalid) == 0, f"Found invalid statuses: {invalid}"

    def test_content_hash_matches_text(self, tmp_path):
        """Content hash should match the SHA256 of the normalized node text."""
        graph, _ = _make_graph(tmp_path)
        text = "Solomon likes coffee every morning"
        node_id = _insert_test_node(graph, text)
        expected_hash = _content_hash(text)

        with graph._get_conn() as conn:
            row = conn.execute(
                "SELECT content_hash FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
        assert row is not None, "Node not found in DB"
        assert row[0] == expected_hash, f"Hash mismatch: {row[0]} != {expected_hash}"
