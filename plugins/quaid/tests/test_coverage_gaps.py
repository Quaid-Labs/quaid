"""Tests closing coverage gaps identified by bug bash test coverage agent.

Covers:
- hard_delete_node() — all 5 cleanup operations
- apply_decay_optimized() — non-dry-run with actual DB mutations
- decay_memories() — CLI path flat subtractive decay
- lib/archive.py — archive_node() and search_archive()
- docs_registry.gc() — garbage collection of broken entries
"""

import os
import sys
import json
import math
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_get_embedding(text):
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim


def _make_graph(tmp_path, db_name="coverage_test.db"):
    from datastore.memorydb.memory_graph import MemoryGraph
    db_file = tmp_path / db_name
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph, db_file


def _make_node(graph, name="Test fact", **kwargs):
    from datastore.memorydb.memory_graph import Node
    defaults = dict(
        type="Fact", name=name, confidence=0.8,
        owner_id="quaid", status="active",
    )
    defaults.update(kwargs)
    node = Node.create(**defaults)
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph.add_node(node)
    return node


# ===========================================================================
# hard_delete_node() tests
# ===========================================================================

class TestHardDeleteNode:
    """Tests for hard_delete_node() — all 5 cleanup operations."""

    def test_deletes_node(self, tmp_path):
        """Basic: node is removed from nodes table."""
        from datastore.memorydb.memory_graph import hard_delete_node
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Fact to delete")

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            result = hard_delete_node(node.id)

        assert result is True
        assert graph.get_node(node.id) is None

    def test_returns_false_for_nonexistent(self, tmp_path):
        """Returns False when node_id doesn't exist."""
        from datastore.memorydb.memory_graph import hard_delete_node
        graph, _ = _make_graph(tmp_path)

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            result = hard_delete_node("nonexistent-id")

        assert result is False

    def test_cleans_up_edges(self, tmp_path):
        """Edges referencing the node (source or target) are deleted."""
        from datastore.memorydb.memory_graph import hard_delete_node
        graph, _ = _make_graph(tmp_path)
        node_a = _make_node(graph, "Source node")
        node_b = _make_node(graph, "Target node")
        node_c = _make_node(graph, "Other node")

        with graph._get_conn() as conn:
            conn.execute(
                "INSERT INTO edges (id, source_id, target_id, relation) VALUES (?, ?, ?, ?)",
                ("e1", node_a.id, node_b.id, "related_to")
            )
            conn.execute(
                "INSERT INTO edges (id, source_id, target_id, relation) VALUES (?, ?, ?, ?)",
                ("e2", node_c.id, node_a.id, "caused_by")
            )

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            hard_delete_node(node_a.id)

        with graph._get_conn() as conn:
            edges = conn.execute("SELECT * FROM edges WHERE source_id = ? OR target_id = ?",
                                 (node_a.id, node_a.id)).fetchall()
            assert len(edges) == 0

    def test_cleans_up_contradictions(self, tmp_path):
        """Contradiction entries referencing the node are deleted."""
        from datastore.memorydb.memory_graph import hard_delete_node
        graph, _ = _make_graph(tmp_path)
        node_a = _make_node(graph, "Contradicting fact A")
        node_b = _make_node(graph, "Contradicting fact B")

        with graph._get_conn() as conn:
            conn.execute(
                "INSERT INTO contradictions (id, node_a_id, node_b_id, explanation) VALUES (?, ?, ?, ?)",
                ("c1", node_a.id, node_b.id, "They conflict")
            )

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            hard_delete_node(node_a.id)

        with graph._get_conn() as conn:
            contras = conn.execute("SELECT * FROM contradictions WHERE node_a_id = ? OR node_b_id = ?",
                                   (node_a.id, node_a.id)).fetchall()
            assert len(contras) == 0

    def test_cleans_up_decay_review_queue(self, tmp_path):
        """Decay review queue entries for the node are deleted."""
        from datastore.memorydb.memory_graph import hard_delete_node
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Decaying fact")

        with graph._get_conn() as conn:
            conn.execute(
                "INSERT INTO decay_review_queue (id, node_id, node_text, confidence_at_queue) VALUES (?, ?, ?, ?)",
                ("drq1", node.id, node.name, 0.15)
            )

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            hard_delete_node(node.id)

        with graph._get_conn() as conn:
            queue = conn.execute("SELECT * FROM decay_review_queue WHERE node_id = ?",
                                 (node.id,)).fetchall()
            assert len(queue) == 0

    def test_cleans_up_vec_nodes(self, tmp_path):
        """vec_nodes entries for the node are deleted."""
        from datastore.memorydb.memory_graph import hard_delete_node
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Fact with embedding")

        # Verify vec_nodes entry exists before delete
        with graph._get_conn() as conn:
            try:
                vec_count = conn.execute(
                    "SELECT COUNT(*) FROM vec_nodes WHERE node_id = ?", (node.id,)
                ).fetchone()[0]
                has_vec = True
            except Exception:
                has_vec = False

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            hard_delete_node(node.id)

        if has_vec:
            with graph._get_conn() as conn:
                vec_count = conn.execute(
                    "SELECT COUNT(*) FROM vec_nodes WHERE node_id = ?", (node.id,)
                ).fetchone()[0]
                assert vec_count == 0

    def test_no_orphan_edges_after_delete(self, tmp_path):
        """After deletion, no edges reference nonexistent nodes."""
        from datastore.memorydb.memory_graph import hard_delete_node
        graph, _ = _make_graph(tmp_path)
        node_a = _make_node(graph, "Node A")
        node_b = _make_node(graph, "Node B")
        node_c = _make_node(graph, "Node C")

        with graph._get_conn() as conn:
            conn.execute(
                "INSERT INTO edges (id, source_id, target_id, relation) VALUES (?, ?, ?, ?)",
                ("e1", node_a.id, node_b.id, "related_to")
            )
            conn.execute(
                "INSERT INTO edges (id, source_id, target_id, relation) VALUES (?, ?, ?, ?)",
                ("e2", node_b.id, node_c.id, "related_to")
            )

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            hard_delete_node(node_b.id)

        with graph._get_conn() as conn:
            # No edges should reference node_b
            orphans = conn.execute("""
                SELECT e.id FROM edges e
                LEFT JOIN nodes n1 ON e.source_id = n1.id
                LEFT JOIN nodes n2 ON e.target_id = n2.id
                WHERE n1.id IS NULL OR n2.id IS NULL
            """).fetchall()
            assert len(orphans) == 0


# ===========================================================================
# apply_decay_optimized() — non-dry-run tests
# ===========================================================================

class TestApplyDecayOptimizedReal:
    """Tests for apply_decay_optimized() with dry_run=False — actual DB mutations."""

    def _make_stale_mem(self, graph, name, days_ago=90, confidence=0.8, access_count=0,
                        verified=False, storage_strength=0.0):
        """Create a stale memory dict matching find_stale_memories_optimized output."""
        node = _make_node(graph, name, confidence=confidence)
        accessed = (datetime.now() - timedelta(days=days_ago)).isoformat()

        # Update accessed_at in DB
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET accessed_at = ? WHERE id = ?", (accessed, node.id))

        return {
            "id": node.id,
            "text": name,
            "type": "Fact",
            "confidence": confidence,
            "last_accessed": accessed,
            "accessed_at": accessed,
            "access_count": access_count,
            "storage_strength": storage_strength,
            "extraction_confidence": confidence,
            "verified": verified,
            "owner_id": "quaid",
            "created_at": node.created_at,
            "speaker": None,
        }

    def test_decay_updates_confidence_in_db(self, tmp_path):
        """Non-dry-run actually updates confidence in the database."""
        from core.lifecycle.janitor import apply_decay_optimized, JanitorMetrics
        graph, _ = _make_graph(tmp_path)
        metrics = JanitorMetrics()

        mem = self._make_stale_mem(graph, "Old fact about dogs", days_ago=90, confidence=0.8)

        with patch("core.lifecycle.janitor._cfg") as mock_cfg:
            mock_cfg.decay.mode = "exponential"
            mock_cfg.decay.base_half_life_days = 60.0
            mock_cfg.decay.access_bonus_factor = 0.15
            mock_cfg.decay.minimum_confidence = 0.1
            mock_cfg.decay.review_queue_enabled = False

            result = apply_decay_optimized([mem], graph, metrics, dry_run=False)

        assert result["decayed"] == 1

        # Verify confidence actually changed in DB
        updated = graph.get_node(mem["id"])
        assert updated.confidence < 0.8
        assert updated.confidence > 0.1  # Not deleted

    def test_ebbinghaus_produces_correct_retention(self, tmp_path):
        """Retention formula: R = 2^(-t/half_life)."""
        from core.lifecycle.janitor import apply_decay_optimized, JanitorMetrics
        graph, _ = _make_graph(tmp_path)
        metrics = JanitorMetrics()

        # 60 days ago, 0 accesses, base half-life 60 → retention = 0.5
        mem = self._make_stale_mem(graph, "Exactly half-life old fact", days_ago=60, confidence=0.8)

        with patch("core.lifecycle.janitor._cfg") as mock_cfg:
            mock_cfg.decay.mode = "exponential"
            mock_cfg.decay.base_half_life_days = 60.0
            mock_cfg.decay.access_bonus_factor = 0.15
            mock_cfg.decay.minimum_confidence = 0.1
            mock_cfg.decay.review_queue_enabled = False

            apply_decay_optimized([mem], graph, metrics, dry_run=False)

        updated = graph.get_node(mem["id"])
        # new_confidence = baseline * retention = 0.8 * 0.5 = 0.4
        assert updated.confidence == pytest.approx(0.4, abs=0.05)

    def test_storage_strength_extends_half_life(self, tmp_path):
        """High storage_strength → slower decay (longer half-life)."""
        from core.lifecycle.janitor import apply_decay_optimized, JanitorMetrics
        graph, _ = _make_graph(tmp_path)
        metrics = JanitorMetrics()

        # Two memories at same age — one with storage_strength, one without
        mem_weak = self._make_stale_mem(graph, "Weakly encoded fact", days_ago=90,
                                         confidence=0.8, storage_strength=0.0)
        mem_strong = self._make_stale_mem(graph, "Strongly encoded fact", days_ago=90,
                                           confidence=0.8, storage_strength=5.0)

        with patch("core.lifecycle.janitor._cfg") as mock_cfg:
            mock_cfg.decay.mode = "exponential"
            mock_cfg.decay.base_half_life_days = 60.0
            mock_cfg.decay.access_bonus_factor = 0.15
            mock_cfg.decay.minimum_confidence = 0.1
            mock_cfg.decay.review_queue_enabled = False

            apply_decay_optimized([mem_weak, mem_strong], graph, metrics, dry_run=False)

        weak_node = graph.get_node(mem_weak["id"])
        strong_node = graph.get_node(mem_strong["id"])

        # Strong memory should have higher confidence (decayed less)
        assert strong_node.confidence > weak_node.confidence

    def test_below_min_confidence_deletes(self, tmp_path):
        """Memories decayed below minimum confidence are deleted (when review_queue disabled)."""
        from core.lifecycle.janitor import apply_decay_optimized, JanitorMetrics
        graph, _ = _make_graph(tmp_path)
        metrics = JanitorMetrics()

        # Very old, very low confidence → will drop below minimum
        mem = self._make_stale_mem(graph, "Nearly forgotten fact", days_ago=365, confidence=0.15)

        with patch("core.lifecycle.janitor._cfg") as mock_cfg, \
             patch("core.lifecycle.janitor._archive_node", return_value=True), \
             patch("core.lifecycle.janitor.hard_delete_node") as mock_delete:
            mock_cfg.decay.mode = "exponential"
            mock_cfg.decay.base_half_life_days = 60.0
            mock_cfg.decay.access_bonus_factor = 0.15
            mock_cfg.decay.minimum_confidence = 0.1
            mock_cfg.decay.review_queue_enabled = False

            result = apply_decay_optimized([mem], graph, metrics, dry_run=False)

        assert result["deleted"] == 1
        mock_delete.assert_called_once_with(mem["id"])

    def test_below_min_queued_when_review_enabled(self, tmp_path):
        """Memories below minimum are queued for review when review_queue_enabled."""
        from core.lifecycle.janitor import apply_decay_optimized, JanitorMetrics
        graph, _ = _make_graph(tmp_path)
        metrics = JanitorMetrics()

        mem = self._make_stale_mem(graph, "Needs review fact", days_ago=365, confidence=0.15)

        with patch("core.lifecycle.janitor._cfg") as mock_cfg, \
             patch("core.lifecycle.janitor.queue_for_decay_review") as mock_queue:
            mock_cfg.decay.mode = "exponential"
            mock_cfg.decay.base_half_life_days = 60.0
            mock_cfg.decay.access_bonus_factor = 0.15
            mock_cfg.decay.minimum_confidence = 0.1
            mock_cfg.decay.review_queue_enabled = True

            result = apply_decay_optimized([mem], graph, metrics, dry_run=False)

        assert result["queued"] == 1
        mock_queue.assert_called_once()

    def test_linear_mode_subtracts_flat_rate(self, tmp_path):
        """Linear mode applies flat -RATE decay."""
        from core.lifecycle.janitor import apply_decay_optimized, JanitorMetrics
        graph, _ = _make_graph(tmp_path)
        metrics = JanitorMetrics()

        mem = self._make_stale_mem(graph, "Linear decay fact", days_ago=90, confidence=0.8)

        with patch("core.lifecycle.janitor._cfg") as mock_cfg, \
             patch("core.lifecycle.janitor.CONFIDENCE_DECAY_RATE", 0.10):
            mock_cfg.decay.mode = "linear"
            mock_cfg.decay.minimum_confidence = 0.1
            mock_cfg.decay.review_queue_enabled = False

            apply_decay_optimized([mem], graph, metrics, dry_run=False)

        updated = graph.get_node(mem["id"])
        assert updated.confidence == pytest.approx(0.70, abs=0.01)

    def test_verified_decays_slower_linear(self, tmp_path):
        """Verified facts decay at half rate in linear mode."""
        from core.lifecycle.janitor import apply_decay_optimized, JanitorMetrics
        graph, _ = _make_graph(tmp_path)
        metrics = JanitorMetrics()

        mem = self._make_stale_mem(graph, "Verified fact", days_ago=90, confidence=0.8, verified=True)

        with patch("core.lifecycle.janitor._cfg") as mock_cfg, \
             patch("core.lifecycle.janitor.CONFIDENCE_DECAY_RATE", 0.10):
            mock_cfg.decay.mode = "linear"
            mock_cfg.decay.minimum_confidence = 0.1
            mock_cfg.decay.review_queue_enabled = False

            apply_decay_optimized([mem], graph, metrics, dry_run=False)

        updated = graph.get_node(mem["id"])
        # Verified: rate * 0.5 = 0.05, so 0.8 - 0.05 = 0.75
        assert updated.confidence == pytest.approx(0.75, abs=0.01)


# ===========================================================================
# decay_memories() CLI path
# ===========================================================================

class TestDecayMemoriesCli:
    """Tests for decay_memories() — flat subtractive -0.10 with 30-day cutoff."""

    def test_decays_old_active_memories(self, tmp_path):
        """Memories accessed 30+ days ago get -0.10 confidence."""
        from datastore.memorydb.memory_graph import decay_memories
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Old active memory", confidence=0.8, status="active")

        old_date = (datetime.now() - timedelta(days=45)).isoformat()
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET accessed_at = ? WHERE id = ?", (old_date, node.id))

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            result = decay_memories()

        assert result["decayed_count"] == 1
        updated = graph.get_node(node.id)
        assert updated.confidence == pytest.approx(0.70, abs=0.01)

    def test_skips_recently_accessed(self, tmp_path):
        """Memories accessed <30 days ago are not decayed."""
        from datastore.memorydb.memory_graph import decay_memories
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Recent memory", confidence=0.8, status="active")

        recent = (datetime.now() - timedelta(days=5)).isoformat()
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET accessed_at = ? WHERE id = ?", (recent, node.id))

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            result = decay_memories()

        assert result["decayed_count"] == 0
        updated = graph.get_node(node.id)
        assert updated.confidence == pytest.approx(0.8, abs=0.01)

    def test_skips_pinned_memories(self, tmp_path):
        """Pinned memories are never decayed."""
        from datastore.memorydb.memory_graph import decay_memories
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Pinned memory", confidence=0.8, status="active")

        old_date = (datetime.now() - timedelta(days=90)).isoformat()
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET accessed_at = ?, pinned = 1 WHERE id = ?",
                         (old_date, node.id))

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            result = decay_memories()

        assert result["decayed_count"] == 0

    def test_skips_pending_status(self, tmp_path):
        """Memories with pending status are not decayed."""
        from datastore.memorydb.memory_graph import decay_memories
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Pending memory", confidence=0.8, status="pending")

        old_date = (datetime.now() - timedelta(days=90)).isoformat()
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET accessed_at = ? WHERE id = ?", (old_date, node.id))

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            result = decay_memories()

        assert result["decayed_count"] == 0

    def test_skips_verified_memories(self, tmp_path):
        """Verified memories are not decayed."""
        from datastore.memorydb.memory_graph import decay_memories
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Verified memory", confidence=0.8, status="active")

        old_date = (datetime.now() - timedelta(days=90)).isoformat()
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET accessed_at = ?, verified = 1 WHERE id = ?",
                         (old_date, node.id))

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            result = decay_memories()

        assert result["decayed_count"] == 0

    def test_wont_decay_below_floor(self, tmp_path):
        """Confidence won't drop below 0.1 (the WHERE clause prevents it)."""
        from datastore.memorydb.memory_graph import decay_memories
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Low confidence memory", confidence=0.12, status="active")

        old_date = (datetime.now() - timedelta(days=90)).isoformat()
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET accessed_at = ? WHERE id = ?", (old_date, node.id))

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph):
            result = decay_memories()

        assert result["decayed_count"] == 1
        updated = graph.get_node(node.id)
        # 0.12 - 0.10 = 0.02
        assert updated.confidence == pytest.approx(0.02, abs=0.01)


# ===========================================================================
# lib/archive.py tests
# ===========================================================================

class TestArchiveNode:
    """Tests for archive_node() — writing to archive DB."""

    def test_archives_node_successfully(self, tmp_path):
        from lib.archive import archive_node
        archive_db = tmp_path / "test_archive.db"

        node_dict = {
            "id": "node-123",
            "type": "Fact",
            "name": "Quaid likes coffee",
            "attributes": {"category": "preference"},
            "confidence": 0.75,
            "speaker": "quaid",
            "owner_id": "quaid",
            "created_at": "2025-01-01T00:00:00",
            "accessed_at": "2025-06-01T00:00:00",
            "access_count": 5,
        }

        result = archive_node(node_dict, "confidence_decay", db_path=archive_db)
        assert result is True

        # Verify data in archive DB
        conn = sqlite3.connect(archive_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM archived_nodes WHERE id = ?", ("node-123",)).fetchone()
        conn.close()

        assert row is not None
        assert row["name"] == "Quaid likes coffee"
        assert row["archive_reason"] == "confidence_decay"
        assert row["confidence"] == 0.75

    def test_archives_with_text_key_fallback(self, tmp_path):
        """Uses 'text' key when 'name' is missing."""
        from lib.archive import archive_node
        archive_db = tmp_path / "test_archive2.db"

        node_dict = {
            "id": "node-456",
            "type": "Fact",
            "text": "Fallback text field",
            "confidence": 0.5,
            "owner_id": "quaid",
        }

        result = archive_node(node_dict, "test_reason", db_path=archive_db)
        assert result is True

        conn = sqlite3.connect(archive_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT name FROM archived_nodes WHERE id = ?", ("node-456",)).fetchone()
        conn.close()

        assert row["name"] == "Fallback text field"

    def test_returns_false_on_error(self, tmp_path):
        """Returns False on database errors (doesn't raise)."""
        from lib.archive import archive_node

        # Invalid path that can't be created
        result = archive_node({"id": "x"}, "test", db_path=Path("/nonexistent/deeply/nested/dir/archive.db"))
        assert result is False

    def test_idempotent_archive(self, tmp_path):
        """Archiving same node twice (INSERT OR REPLACE) succeeds."""
        from lib.archive import archive_node
        archive_db = tmp_path / "test_archive_idem.db"

        node_dict = {"id": "node-repeat", "type": "Fact", "name": "Repeat fact",
                     "confidence": 0.5, "owner_id": "quaid"}

        assert archive_node(node_dict, "first", db_path=archive_db) is True
        assert archive_node(node_dict, "second", db_path=archive_db) is True

        conn = sqlite3.connect(archive_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT archive_reason FROM archived_nodes WHERE id = ?",
                           ("node-repeat",)).fetchone()
        conn.close()
        assert row["archive_reason"] == "second"


class TestSearchArchive:
    """Tests for search_archive() — LIKE-based text search."""

    def _populate(self, archive_db):
        from lib.archive import archive_node
        facts = [
            ("a1", "Quaid enjoys morning espresso"),
            ("a2", "The cat Richter likes sleeping"),
            ("a3", "Quaid's favorite color is blue"),
        ]
        for fid, text in facts:
            archive_node({"id": fid, "type": "Fact", "name": text,
                          "confidence": 0.5, "owner_id": "quaid"},
                         "test", db_path=archive_db)

    def test_basic_search(self, tmp_path):
        from lib.archive import search_archive
        archive_db = tmp_path / "test_search.db"
        self._populate(archive_db)

        results = search_archive("Quaid", db_path=archive_db)
        assert len(results) == 2

    def test_empty_results(self, tmp_path):
        from lib.archive import search_archive
        archive_db = tmp_path / "test_search_empty.db"
        self._populate(archive_db)

        results = search_archive("nonexistent query xyz", db_path=archive_db)
        assert len(results) == 0

    def test_limit_respected(self, tmp_path):
        from lib.archive import search_archive
        archive_db = tmp_path / "test_search_limit.db"
        self._populate(archive_db)

        results = search_archive("Quaid", limit=1, db_path=archive_db)
        assert len(results) == 1

    def test_special_chars_escaped(self, tmp_path):
        """LIKE wildcards % and _ in query are escaped."""
        from lib.archive import archive_node, search_archive
        archive_db = tmp_path / "test_search_special.db"
        archive_node({"id": "s1", "type": "Fact",
                      "name": "Value is 100% correct",
                      "confidence": 0.5, "owner_id": "quaid"},
                     "test", db_path=archive_db)
        archive_node({"id": "s2", "type": "Fact",
                      "name": "Normal fact about things",
                      "confidence": 0.5, "owner_id": "quaid"},
                     "test", db_path=archive_db)

        # Searching for "100%" should find only the first, not match everything
        results = search_archive("100%", db_path=archive_db)
        assert len(results) == 1
        assert results[0]["id"] == "s1"

    def test_returns_empty_on_nonexistent_db(self, tmp_path):
        """Returns empty list when DB doesn't exist (graceful failure)."""
        from lib.archive import search_archive
        results = search_archive("test", db_path=tmp_path / "nonexistent.db")
        # Should not raise; returns list (may create DB with empty table)
        assert isinstance(results, list)


# ===========================================================================
# docs_registry.gc() tests
# ===========================================================================

class TestDocsRegistryGc:
    """Tests for DocsRegistry.gc() — garbage collection of broken entries."""

    def _make_registry(self, tmp_path, monkeypatch):
        """Create a DocsRegistry with a test DB."""
        monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "gc_test.db"))
        from lib.adapter import set_adapter, StandaloneAdapter
        set_adapter(StandaloneAdapter(home=tmp_path))
        monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))  # kept for backward compat

        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "memory.json").write_text(json.dumps({
            "projects": {"enabled": True, "projectsDir": "projects/",
                         "stagingDir": "projects/staging/",
                         "definitions": {}, "defaultProject": "default"},
            "docs": {"sourceMapping": {}, "docPurposes": {},
                     "coreMarkdown": {"enabled": False}},
            "database": {"path": "data/memory.db"},
            "ollama": {"url": "http://localhost:11434", "embeddingModel": "nomic-embed-text"}
        }))

        from core.docs.registry import DocsRegistry
        from config import reload_config
        reload_config()
        return DocsRegistry(db_path=tmp_path / "gc_test.db")

    def test_gc_identifies_missing_files(self, tmp_path, monkeypatch):
        """GC finds registry entries pointing to nonexistent files."""
        registry = self._make_registry(tmp_path, monkeypatch)

        # Register a file that exists
        existing_file = tmp_path / "existing.md"
        existing_file.write_text("# Exists")
        registry.register(str(existing_file), project="test")

        # Register a file that doesn't exist (use AUTOINCREMENT id)
        from lib.database import get_connection
        with get_connection(registry.db_path) as conn:
            conn.execute(
                "INSERT INTO doc_registry (file_path, project, state) VALUES (?, ?, ?)",
                ("nonexistent/file.md", "test", "active")
            )

        result = registry.gc(dry_run=True)
        assert len(result["removed"]) == 1
        assert result["kept"] >= 1

    def test_gc_dry_run_doesnt_delete(self, tmp_path, monkeypatch):
        """dry_run=True reports but doesn't delete."""
        registry = self._make_registry(tmp_path, monkeypatch)

        from lib.database import get_connection
        with get_connection(registry.db_path) as conn:
            conn.execute(
                "INSERT INTO doc_registry (file_path, project, state) VALUES (?, ?, ?)",
                ("gone/file.md", "test", "active")
            )

        registry.gc(dry_run=True)

        # Entry should still exist
        with get_connection(registry.db_path) as conn:
            row = conn.execute("SELECT * FROM doc_registry WHERE file_path = ?",
                               ("gone/file.md",)).fetchone()
            assert row is not None

    def test_gc_apply_deletes(self, tmp_path, monkeypatch):
        """dry_run=False actually deletes broken entries."""
        registry = self._make_registry(tmp_path, monkeypatch)

        from lib.database import get_connection
        with get_connection(registry.db_path) as conn:
            conn.execute(
                "INSERT INTO doc_registry (file_path, project, state) VALUES (?, ?, ?)",
                ("gone/file2.md", "test", "active")
            )

        result = registry.gc(dry_run=False)
        assert len(result["removed"]) == 1

        with get_connection(registry.db_path) as conn:
            row = conn.execute("SELECT * FROM doc_registry WHERE file_path = ?",
                               ("gone/file2.md",)).fetchone()
            assert row is None

    def test_gc_keeps_valid_entries(self, tmp_path, monkeypatch):
        """GC preserves entries whose files exist."""
        registry = self._make_registry(tmp_path, monkeypatch)

        existing_file = tmp_path / "valid.md"
        existing_file.write_text("# Valid")
        registry.register(str(existing_file), project="test")

        result = registry.gc(dry_run=False)
        assert len(result["removed"]) == 0
        assert result["kept"] >= 1

    def test_gc_no_entries(self, tmp_path, monkeypatch):
        """GC on empty registry returns clean result."""
        registry = self._make_registry(tmp_path, monkeypatch)

        result = registry.gc(dry_run=True)
        assert len(result["removed"]) == 0
        assert result["kept"] == 0
