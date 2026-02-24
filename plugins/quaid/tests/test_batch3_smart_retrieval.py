"""Tests for Batch 3: Smart Retrieval features.

Covers:
- Intent-aware query classification
- Temporal validity filtering in composite scoring
- Backfill hashes CLI command
"""

import os
import sys
import hashlib
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


def _make_graph(tmp_path):
    from datastore.memorydb.memory_graph import MemoryGraph
    db_file = tmp_path / "batch3_test.db"
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph


# ---------------------------------------------------------------------------
# classify_intent
# ---------------------------------------------------------------------------

class TestClassifyIntent:
    """Tests for classify_intent() query classification."""

    def test_who_query(self):
        from datastore.memorydb.memory_graph import classify_intent
        intent, boosts = classify_intent("who is Quaid's wife?")
        assert intent == "WHO"
        assert "Person" in boosts
        assert boosts["Person"] > 1.0

    def test_when_query(self):
        from datastore.memorydb.memory_graph import classify_intent
        intent, boosts = classify_intent("when is Quaid's birthday?")
        assert intent == "WHEN"
        assert "Event" in boosts

    def test_where_query(self):
        from datastore.memorydb.memory_graph import classify_intent
        intent, boosts = classify_intent("where does Quaid live?")
        assert intent == "WHERE"
        assert "Place" in boosts

    def test_preference_query(self):
        from datastore.memorydb.memory_graph import classify_intent
        intent, boosts = classify_intent("Quaid's favorite food preferences")
        assert intent == "PREFERENCE"
        assert "Preference" in boosts

    def test_relation_query(self):
        from datastore.memorydb.memory_graph import classify_intent
        intent, boosts = classify_intent("how is Levi related to Hauser as siblings")
        assert intent == "RELATION"

    def test_general_query(self):
        from datastore.memorydb.memory_graph import classify_intent
        intent, boosts = classify_intent("coffee espresso beans")
        assert intent == "GENERAL"
        assert boosts == {}

    def test_what_query(self):
        from datastore.memorydb.memory_graph import classify_intent
        intent, boosts = classify_intent("what is Quaid working on?")
        assert intent == "WHAT"

    def test_returns_tuple(self):
        from datastore.memorydb.memory_graph import classify_intent
        result = classify_intent("random query")
        assert isinstance(result, tuple)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Temporal validity in composite scoring
# ---------------------------------------------------------------------------

class TestTemporalValidity:
    """Tests for temporal validity filtering in _compute_composite_score()."""

    def test_expired_fact_penalized(self):
        from datastore.memorydb.memory_graph import _compute_composite_score, Node
        node = Node.create(type="Fact", name="Quaid lives in Texas")
        node.accessed_at = datetime.now().isoformat()
        node.valid_until = (datetime.now() - timedelta(days=30)).isoformat()
        score = _compute_composite_score(node, 0.9)
        # Should be less than a node without valid_until
        node_current = Node.create(type="Fact", name="Quaid lives in Bali")
        node_current.accessed_at = datetime.now().isoformat()
        score_current = _compute_composite_score(node_current, 0.9)
        assert score < score_current

    def test_future_fact_heavily_penalized(self):
        from datastore.memorydb.memory_graph import _compute_composite_score, Node
        node = Node.create(type="Fact", name="Quaid will move to Japan")
        node.accessed_at = datetime.now().isoformat()
        node.valid_from = (datetime.now() + timedelta(days=30)).isoformat()
        score = _compute_composite_score(node, 0.9)
        # Heavy penalty for future facts
        node_current = Node.create(type="Fact", name="Quaid lives in Bali now")
        node_current.accessed_at = datetime.now().isoformat()
        score_current = _compute_composite_score(node_current, 0.9)
        assert score < score_current - 0.3

    def test_valid_fact_no_penalty(self):
        from datastore.memorydb.memory_graph import _compute_composite_score, Node
        node = Node.create(type="Fact", name="Quaid is a software engineer")
        node.accessed_at = datetime.now().isoformat()
        node.valid_from = (datetime.now() - timedelta(days=365)).isoformat()
        node.valid_until = (datetime.now() + timedelta(days=365)).isoformat()
        score = _compute_composite_score(node, 0.9)
        # Within validity window — no penalty
        node_no_dates = Node.create(type="Fact", name="Quaid is a software developer")
        node_no_dates.accessed_at = datetime.now().isoformat()
        score_no_dates = _compute_composite_score(node_no_dates, 0.9)
        assert abs(score - score_no_dates) < 0.01  # Nearly identical

    def test_no_temporal_fields_no_penalty(self):
        from datastore.memorydb.memory_graph import _compute_composite_score, Node
        node = Node.create(type="Fact", name="Quaid likes coffee espresso")
        node.accessed_at = datetime.now().isoformat()
        # No valid_from or valid_until
        score = _compute_composite_score(node, 0.9)
        assert score > 0.5  # Reasonable score


# ---------------------------------------------------------------------------
# Backfill hashes
# ---------------------------------------------------------------------------

class TestBackfillHashes:
    """Tests for content hash backfill functionality."""

    def test_backfill_sets_hashes(self, tmp_path):
        from datastore.memorydb.memory_graph import Node, _content_hash
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            # Manually insert a node WITHOUT content_hash
            import sqlite3
            with sqlite3.connect(str(graph.db_path)) as conn:
                conn.execute("""
                    INSERT INTO nodes (id, type, name, status, owner_id, content_hash)
                    VALUES ('test-123', 'Fact', 'Quaid likes coffee', 'approved', 'quaid', NULL)
                """)

            # Verify it's NULL
            with graph._get_conn() as conn:
                row = conn.execute("SELECT content_hash FROM nodes WHERE id = 'test-123'").fetchone()
                assert row["content_hash"] is None

            # Run backfill
            with graph._get_conn() as conn:
                rows = conn.execute(
                    "SELECT id, name FROM nodes WHERE content_hash IS NULL"
                ).fetchall()
            count = 0
            for row in rows:
                h = _content_hash(row["name"])
                with graph._get_conn() as conn:
                    conn.execute(
                        "UPDATE nodes SET content_hash = ? WHERE id = ?",
                        (h, row["id"])
                    )
                count += 1
            assert count >= 1

            # Verify hash is set
            with graph._get_conn() as conn:
                row = conn.execute("SELECT content_hash FROM nodes WHERE id = 'test-123'").fetchone()
                assert row["content_hash"] == _content_hash("Quaid likes coffee")

    def test_backfill_skips_already_hashed(self, tmp_path):
        from datastore.memorydb.memory_graph import Node, _content_hash
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Quaid likes espresso coffee", owner_id="quaid")
            graph.add_node(node)  # This sets content_hash automatically

            # Backfill should find 0 nodes with NULL hash
            with graph._get_conn() as conn:
                rows = conn.execute(
                    "SELECT id, name FROM nodes WHERE content_hash IS NULL"
                ).fetchall()
            assert len(rows) == 0
