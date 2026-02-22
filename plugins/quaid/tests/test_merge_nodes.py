"""Tests for _merge_nodes_into() helper in janitor.py.

Validates the fix for the destructive merge pattern:
1. Confidence inheritance (max from originals, not hardcoded 0.9)
2. Confirmation count accumulation (sum from originals, not reset to 0)
3. Storage strength inheritance (max from originals)
4. Status="active" (not "approved")
5. Edge migration (repoint, not delete)
6. Owner ID from originals (not hardcoded "quaid")
7. Earliest created_at preserved
"""

import os
import sys
import hashlib
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env BEFORE imports so lib.config picks it up
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_get_embedding(text):
    """Return a deterministic fake embedding based on text hash."""
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim


def _make_graph(tmp_path):
    """Create a MemoryGraph backed by a temp SQLite file."""
    from memory_graph import MemoryGraph
    db_file = tmp_path / "test.db"
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph, db_file


def _make_node(graph, name, owner_id="quaid", confidence=0.8, status="active",
               confirmation_count=0, storage_strength=0.0, created_at=None):
    """Create and add a node directly."""
    from memory_graph import Node
    node = Node.create(
        type="Fact",
        name=name,
        verified=True,
        confidence=confidence,
        owner_id=owner_id,
        status=status,
    )
    if created_at:
        node.created_at = created_at
    node.confirmation_count = confirmation_count
    node.storage_strength = storage_strength
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph.add_node(node)
    return node


def _store_and_get(graph, text, owner_id="quaid", confidence=0.8, status="active",
                   confirmation_count=0, storage_strength=0.0, created_at=None):
    """Store via store() and return the node with overrides applied."""
    from memory_graph import store
    with patch("memory_graph.get_graph", return_value=graph), \
         patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
         patch("memory_graph._HAS_CONFIG", False):
        result = store(text, owner_id=owner_id, confidence=confidence,
                       skip_dedup=True, status=status, created_at=created_at)
    node = graph.get_node(result["id"])
    if confirmation_count:
        node.confirmation_count = confirmation_count
    if storage_strength:
        node.storage_strength = storage_strength
    if confirmation_count or storage_strength:
        graph.update_node(node)
    return node


def _create_edge_direct(graph, source_id, target_id, relation, source_fact_id=None):
    """Create an edge directly in the database."""
    with graph._get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO edges (source_id, target_id, relation, source_fact_id) VALUES (?, ?, ?, ?)",
            (source_id, target_id, relation, source_fact_id)
        )


def _get_edges(graph, node_id):
    """Get all edges referencing a node (as source, target, or source_fact)."""
    with graph._get_conn() as conn:
        rows = conn.execute(
            "SELECT source_id, target_id, relation, source_fact_id FROM edges "
            "WHERE source_id = ? OR target_id = ? OR source_fact_id = ?",
            (node_id, node_id, node_id)
        ).fetchall()
    return [dict(r) for r in rows]


def _count_nodes(graph):
    """Count total nodes in the database."""
    with graph._get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]


# ===========================================================================
# 1. Confidence Inheritance
# ===========================================================================

class TestMergeConfidenceInheritance:
    """Merged node inherits max confidence from originals."""

    def test_inherits_max_confidence(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid likes morning coffee routines", confidence=0.7)
        node_b = _store_and_get(graph, "Quaid enjoys morning coffee every day", confidence=0.95)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid likes morning coffee every day",
                [node_a.id, node_b.id], source="dedup_merge"
            )

        merged = graph.get_node(result["id"])
        assert merged.confidence == 0.95

    def test_does_not_hardcode_09(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid has a pet cat named Richter", confidence=0.3)
        node_b = _store_and_get(graph, "Quaid owns a cat called Richter", confidence=0.4)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid has a pet cat named Richter",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        # Should be 0.4 (max of originals), NOT 0.9
        assert merged.confidence == pytest.approx(0.4, abs=0.01)

    def test_three_way_merge_inherits_max(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid works from his home office", confidence=0.6)
        node_b = _store_and_get(graph, "Quaid works remotely from home", confidence=0.85)
        node_c = _store_and_get(graph, "Quaid is a remote worker at home", confidence=0.7)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid works remotely from his home office",
                [node_a.id, node_b.id, node_c.id]
            )

        merged = graph.get_node(result["id"])
        assert merged.confidence == pytest.approx(0.85, abs=0.01)


# ===========================================================================
# 2. Confirmation Count Accumulation
# ===========================================================================

class TestMergeConfirmationCount:
    """Merged node sums confirmation_count from originals."""

    def test_sums_confirmation_counts(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid drinks espresso in morning", confirmation_count=3)
        node_b = _store_and_get(graph, "Quaid has espresso every morning", confirmation_count=5)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid drinks espresso every morning",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        assert merged.confirmation_count == 8  # 3 + 5

    def test_does_not_reset_to_zero(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid lives in Portland Oregon", confirmation_count=7)
        node_b = _store_and_get(graph, "Quaid resides in Portland Oregon", confirmation_count=0)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid lives in Portland Oregon area",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        assert merged.confirmation_count == 7  # 7 + 0, NOT 0


# ===========================================================================
# 3. Storage Strength Inheritance
# ===========================================================================

class TestMergeStorageStrength:
    """Merged node inherits max storage_strength from originals."""

    def test_inherits_max_storage_strength(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid uses FastAPI for backend work", storage_strength=0.15)
        node_b = _store_and_get(graph, "Quaid builds backends with FastAPI", storage_strength=0.42)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid uses FastAPI for backend development",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        assert merged.storage_strength == pytest.approx(0.42, abs=0.01)


# ===========================================================================
# 4. Status = "active"
# ===========================================================================

class TestMergeStatus:
    """Merged node gets status='active', not 'approved'."""

    def test_merged_status_is_active(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid prefers dark mode editors", status="active")
        node_b = _store_and_get(graph, "Quaid likes dark mode in editors", status="active")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid prefers dark mode in code editors",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        assert merged.status == "active"

    def test_merged_status_not_approved(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid has a Mac mini M4", status="approved")
        node_b = _store_and_get(graph, "Quaid uses Mac mini M4 computer", status="approved")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid has a Mac mini M4 computer",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        # Must be "active", not "approved" — merges skip the approval flow
        assert merged.status == "active"


# ===========================================================================
# 5. Edge Migration
# ===========================================================================

class TestMergeEdgeMigration:
    """Edges are migrated to merged node, not deleted."""

    def test_source_edges_migrated(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid is a software developer engineer")
        target_node = _make_node(graph, "Python")
        _create_edge_direct(graph, node_a.id, target_node.id, "knows")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid is a professional software developer",
                [node_a.id]
            )

        merged_id = result["id"]
        edges = _get_edges(graph, merged_id)
        assert len(edges) >= 1
        assert any(e["source_id"] == merged_id and e["target_id"] == target_node.id for e in edges)

    def test_target_edges_migrated(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Richter is Quaid pet cat friend")
        source_node = _make_node(graph, "Douglas Quaid")
        _create_edge_direct(graph, source_node.id, node_a.id, "owns")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Richter is Quaid beloved pet cat",
                [node_a.id]
            )

        merged_id = result["id"]
        edges = _get_edges(graph, merged_id)
        assert len(edges) >= 1
        assert any(e["source_id"] == source_node.id and e["target_id"] == merged_id for e in edges)

    def test_source_fact_edges_migrated(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid likes hiking in the mountains")
        entity1 = _make_node(graph, "Douglas Quaid")
        entity2 = _make_node(graph, "Hiking")
        _create_edge_direct(graph, entity1.id, entity2.id, "enjoys", source_fact_id=node_a.id)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid enjoys hiking in mountain trails",
                [node_a.id]
            )

        merged_id = result["id"]
        with graph._get_conn() as conn:
            rows = conn.execute(
                "SELECT source_fact_id FROM edges WHERE source_id = ? AND target_id = ?",
                (entity1.id, entity2.id)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["source_fact_id"] == merged_id

    def test_original_edges_cleaned_up(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid speaks English and Spanish")
        node_b = _store_and_get(graph, "Quaid is bilingual English Spanish")
        entity = _make_node(graph, "English Language")
        _create_edge_direct(graph, node_a.id, entity.id, "speaks")
        _create_edge_direct(graph, node_b.id, entity.id, "speaks")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid speaks English and Spanish fluently",
                [node_a.id, node_b.id]
            )

        merged_id = result["id"]
        # No edges should reference the original nodes
        assert _get_edges(graph, node_a.id) == []
        assert _get_edges(graph, node_b.id) == []
        # Merged node should have the edge (one, since UNIQUE constraint dedupes)
        edges = _get_edges(graph, merged_id)
        assert any(e["source_id"] == merged_id and e["target_id"] == entity.id for e in edges)


# ===========================================================================
# 5b. Edge Migration — Advanced Cases
# ===========================================================================

class TestMergeEdgeMigrationAdvanced:
    """Advanced edge migration: self-loops, bidirectional, multi-relation."""

    def test_bidirectional_edges_between_originals_no_self_loop(self, tmp_path):
        """Merging nodes with edges to each other must not create self-loops."""
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid mentors junior developers")
        node_b = _store_and_get(graph, "Junior developers learn from Quaid")
        _create_edge_direct(graph, node_a.id, node_b.id, "mentors")
        _create_edge_direct(graph, node_b.id, node_a.id, "learns_from")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid mentors junior developers who learn from him",
                [node_a.id, node_b.id]
            )

        merged_id = result["id"]
        edges = _get_edges(graph, merged_id)
        # No self-referencing edges should exist
        self_loops = [e for e in edges
                      if e["source_id"] == merged_id and e["target_id"] == merged_id]
        assert len(self_loops) == 0, f"Found {len(self_loops)} self-loops after merge"

    def test_multiple_relations_to_same_target_preserved(self, tmp_path):
        """Different relations from merged nodes to same target are all preserved."""
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid knows Python programming language")
        node_b = _store_and_get(graph, "Quaid uses Python for daily work")
        python_node = _make_node(graph, "Python")
        _create_edge_direct(graph, node_a.id, python_node.id, "knows")
        _create_edge_direct(graph, node_b.id, python_node.id, "uses")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid knows and uses Python daily",
                [node_a.id, node_b.id]
            )

        merged_id = result["id"]
        edges = _get_edges(graph, merged_id)
        relations = {e["relation"] for e in edges
                     if e["source_id"] == merged_id and e["target_id"] == python_node.id}
        assert "knows" in relations, "knows relation lost during merge"
        assert "uses" in relations, "uses relation lost during merge"

    def test_duplicate_same_relation_deduped(self, tmp_path):
        """Same relation from two originals to same target yields exactly one edge."""
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid knows how to use FastAPI")
        node_b = _store_and_get(graph, "Quaid is skilled with FastAPI")
        fastapi_node = _make_node(graph, "FastAPI")
        _create_edge_direct(graph, node_a.id, fastapi_node.id, "knows")
        _create_edge_direct(graph, node_b.id, fastapi_node.id, "knows")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid is skilled with FastAPI framework",
                [node_a.id, node_b.id]
            )

        merged_id = result["id"]
        with graph._get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE source_id = ? AND target_id = ? AND relation = ?",
                (merged_id, fastapi_node.id, "knows")
            ).fetchone()[0]
        assert count == 1, f"Expected exactly 1 edge, found {count}"

    def test_contradictions_and_decay_queue_cleaned(self, tmp_path):
        """Merge cleans up contradictions and decay_review_queue for originals."""
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid prefers tea over coffee")
        node_b = _store_and_get(graph, "Quaid likes tea more than coffee")

        with graph._get_conn() as conn:
            conn.execute(
                "INSERT INTO contradictions (id, node_a_id, node_b_id, status) VALUES (?, ?, ?, ?)",
                ("contra-test-1", node_a.id, node_b.id, "pending")
            )
            conn.execute(
                "INSERT INTO decay_review_queue (id, node_id, node_text, confidence_at_queue, status) VALUES (?, ?, ?, ?, ?)",
                ("decay-test-1", node_a.id, "Quaid prefers tea", 0.3, "pending")
            )

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            _merge_nodes_into(
                graph, "Quaid prefers tea over coffee always",
                [node_a.id, node_b.id]
            )

        with graph._get_conn() as conn:
            contra_count = conn.execute(
                "SELECT COUNT(*) FROM contradictions WHERE node_a_id = ? OR node_b_id = ?",
                (node_a.id, node_b.id)
            ).fetchone()[0]
            decay_count = conn.execute(
                "SELECT COUNT(*) FROM decay_review_queue WHERE node_id = ?",
                (node_a.id,)
            ).fetchone()[0]
        assert contra_count == 0, "Contradictions not cleaned after merge"
        assert decay_count == 0, "Decay queue not cleaned after merge"

    def test_missing_originals_still_creates_merge(self, tmp_path):
        """Merge with non-existent original IDs still creates the merged node."""
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Some fact that should still exist",
                ["nonexistent-id-aaa", "nonexistent-id-bbb"]
            )

        # Should still create the merged node (with defaults)
        assert result is not None
        assert result.get("id") is not None
        merged = graph.get_node(result["id"])
        assert merged is not None


# ===========================================================================
# 6. Owner ID from Originals
# ===========================================================================

class TestMergeOwnerInheritance:
    """Merged node inherits owner from originals, not hardcoded."""

    def test_inherits_owner_from_originals(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "TestUser prefers Python for scripting", owner_id="testuser")
        node_b = _store_and_get(graph, "TestUser likes Python for scripts", owner_id="testuser")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "TestUser prefers Python for scripting tasks",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        assert merged.owner_id == "testuser"

    def test_does_not_hardcode_solomon(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Alice prefers tea over coffee morning", owner_id="alice")
        node_b = _store_and_get(graph, "Alice likes tea instead of coffee", owner_id="alice")

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Alice prefers tea over coffee in mornings",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        assert merged.owner_id != "quaid"
        assert merged.owner_id == "alice"


# ===========================================================================
# 7. Created At Preservation
# ===========================================================================

class TestMergeCreatedAt:
    """Merged node preserves the earliest created_at."""

    def test_preserves_earliest_created_at(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        early = "2025-01-15T10:00:00"
        late = "2026-02-10T14:00:00"
        node_a = _store_and_get(graph, "Quaid started running in January", created_at=early)
        node_b = _store_and_get(graph, "Quaid took up running recently", created_at=late)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid started running in January 2025",
                [node_a.id, node_b.id]
            )

        merged = graph.get_node(result["id"])
        assert merged.created_at == early


# ===========================================================================
# 8. Original Nodes Deleted
# ===========================================================================

class TestMergeOriginalsDeleted:
    """Original nodes are deleted after merge."""

    def test_originals_deleted(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid has two cats at home")
        node_b = _store_and_get(graph, "Quaid owns two pet cats at home")
        count_before = _count_nodes(graph)

        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            result = _merge_nodes_into(
                graph, "Quaid has two pet cats at his home",
                [node_a.id, node_b.id]
            )

        # 2 deleted, 1 created = net -1
        assert _count_nodes(graph) == count_before - 1
        assert graph.get_node(node_a.id) is None
        assert graph.get_node(node_b.id) is None
        assert graph.get_node(result["id"]) is not None


# ===========================================================================
# 9. Dry Run
# ===========================================================================

class TestMergeDryRun:
    """Dry run returns None and doesn't modify anything."""

    def test_dry_run_no_changes(self, tmp_path):
        from janitor import _merge_nodes_into
        graph, _ = _make_graph(tmp_path)
        node_a = _store_and_get(graph, "Quaid reads science fiction books")
        node_b = _store_and_get(graph, "Quaid enjoys reading sci-fi novels")
        count_before = _count_nodes(graph)

        result = _merge_nodes_into(
            graph, "Quaid reads science fiction novels",
            [node_a.id, node_b.id], dry_run=True
        )

        assert result is None
        assert _count_nodes(graph) == count_before
        assert graph.get_node(node_a.id) is not None
        assert graph.get_node(node_b.id) is not None


# ===========================================================================
# 10. Default Owner ID Helper
# ===========================================================================

class TestDefaultOwnerId:
    """_default_owner_id() reads from config with fallback."""

    def test_returns_config_value(self):
        from janitor import _default_owner_id
        # Should return the config value (which is "quaid" in test env)
        result = _default_owner_id()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_fallback_on_error(self):
        from janitor import _default_owner_id
        mock_cfg = MagicMock()
        mock_cfg.users.default_owner = None  # Triggers except branch
        # Force attribute access to raise
        mock_cfg.users = MagicMock(spec=[])  # spec=[] means no attributes
        with patch("janitor._cfg", mock_cfg):
            result = _default_owner_id()
            assert result == "default"
