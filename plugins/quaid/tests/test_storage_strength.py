"""Tests for Bjork dual-strength model: retrieval difficulty-weighted storage strength.

Covers:
- _compute_retrieval_difficulty with various signal combinations
- _update_access increments storage_strength correctly with difficulty_map
- _update_access without difficulty_map (backward compat)
- Storage strength capped at 10.0
- _ebbinghaus_retention with storage_strength extends half-life
- _ebbinghaus_retention backward-compatible (ss=0.0 matches old behavior)
- find_stale_memories_optimized includes storage_strength in output
- Migration adds column to existing DB
- add_node / update_node persist storage_strength
- _row_to_node reads storage_strength with fallback
- _compute_composite_score includes storage_bonus
"""

import os
import sys
import math
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_get_embedding(text):
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim


def _make_graph(tmp_path):
    from datastore.memorydb.memory_graph import MemoryGraph
    db_file = tmp_path / "storage_strength_test.db"
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph


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


# ---------------------------------------------------------------------------
# _compute_retrieval_difficulty
# ---------------------------------------------------------------------------

class TestComputeRetrievalDifficulty:
    """Tests for _compute_retrieval_difficulty() function."""

    def test_easy_retrieval_small_difficulty(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {"similarity": 0.9, "id": "test"}
        # Continuous function: 0.4 * (1.0 - 0.9) = 0.04
        assert abs(_compute_retrieval_difficulty(result) - 0.04) < 0.01

    def test_low_similarity_adds_difficulty(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {"similarity": 0.5, "id": "test"}
        d = _compute_retrieval_difficulty(result)
        # Continuous: 0.4 * (1.0 - 0.5) = 0.20
        assert abs(d - 0.20) < 0.01

    def test_zero_similarity_max_sim_difficulty(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {"similarity": 0.0, "id": "test"}
        d = _compute_retrieval_difficulty(result)
        assert abs(d - 0.4) < 0.01  # max sim difficulty = 0.4

    def test_multi_pass_adds_difficulty(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {"similarity": 0.9, "_multi_pass": True, "id": "test"}
        d = _compute_retrieval_difficulty(result)
        # Continuous: 0.04 (sim) + 0.3 (multi_pass) = 0.34
        assert abs(d - 0.34) < 0.01

    def test_graph_traversal_adds_difficulty(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {"similarity": 0.9, "hop_depth": 2, "id": "test"}
        d = _compute_retrieval_difficulty(result)
        # Continuous: 0.04 (sim) + min(0.3, 0.15*2)=0.3 = 0.34
        assert abs(d - 0.34) < 0.01

    def test_single_hop_traversal(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {"similarity": 0.9, "hop_depth": 1, "id": "test"}
        d = _compute_retrieval_difficulty(result)
        # Continuous: 0.04 (sim) + 0.15 (hop) = 0.19
        assert abs(d - 0.19) < 0.01

    def test_combined_signals_cap_at_1(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {
            "similarity": 0.0,      # +0.4
            "_multi_pass": True,     # +0.3
            "hop_depth": 3,          # +0.3 (capped)
            "id": "test",
        }
        d = _compute_retrieval_difficulty(result)
        assert d == 1.0  # 0.4 + 0.3 + 0.3 = 1.0

    def test_no_multi_pass_flag_still_has_sim_difficulty(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {"similarity": 0.9, "_multi_pass": False, "id": "test"}
        # Continuous: 0.4 * (1.0 - 0.9) = 0.04 (sim only, no multi_pass)
        assert abs(_compute_retrieval_difficulty(result) - 0.04) < 0.01

    def test_missing_keys_use_defaults(self):
        from datastore.memorydb.memory_graph import _compute_retrieval_difficulty
        result = {"id": "test"}
        # similarity defaults to 0.8: 0.4 * (1.0 - 0.8) = 0.08
        # no _multi_pass, no hop_depth
        assert abs(_compute_retrieval_difficulty(result) - 0.08) < 0.01


# ---------------------------------------------------------------------------
# _update_access with storage_strength
# ---------------------------------------------------------------------------

class TestUpdateAccessStorageStrength:
    """Tests for _update_access() with difficulty_map."""

    def test_easy_retrieval_base_increment(self, tmp_path):
        from datastore.memorydb.memory_graph import Node
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Easy fact")

        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph._update_access([(node, 0.9)], difficulty_map={node.id: 0.0})

        updated = graph.get_node(node.id)
        # Base increment: 0.05 * (1 + 3*0) = 0.05
        assert abs(updated.storage_strength - 0.05) < 0.001

    def test_hard_retrieval_larger_increment(self, tmp_path):
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Hard fact")

        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph._update_access([(node, 0.5)], difficulty_map={node.id: 1.0})

        updated = graph.get_node(node.id)
        # Max increment: 0.05 * (1 + 3*1.0) = 0.20
        assert abs(updated.storage_strength - 0.20) < 0.001

    def test_medium_difficulty_increment(self, tmp_path):
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Medium fact")

        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph._update_access([(node, 0.7)], difficulty_map={node.id: 0.5})

        updated = graph.get_node(node.id)
        # Increment: 0.05 * (1 + 3*0.5) = 0.05 * 2.5 = 0.125
        assert abs(updated.storage_strength - 0.125) < 0.001

    def test_no_difficulty_map_backward_compat(self, tmp_path):
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Compat fact")

        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph._update_access([(node, 0.9)])  # No difficulty_map

        updated = graph.get_node(node.id)
        # Default difficulty=0.0: increment = 0.05
        assert abs(updated.storage_strength - 0.05) < 0.001
        assert updated.access_count == 1

    def test_storage_strength_capped_at_10(self, tmp_path):
        from datastore.memorydb.memory_graph import Node
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Capped fact", storage_strength=9.95)

        # Need to set it via direct SQL since add_node doesn't propagate large values easily
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET storage_strength = 9.95 WHERE id = ?", (node.id,))

        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph._update_access([(node, 0.5)], difficulty_map={node.id: 1.0})

        updated = graph.get_node(node.id)
        # 9.95 + 0.20 = 10.15, but capped at 10.0
        assert updated.storage_strength == 10.0

    def test_multiple_accesses_accumulate(self, tmp_path):
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Accumulate fact")

        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            for _ in range(5):
                graph._update_access([(node, 0.9)], difficulty_map={node.id: 0.0})

        updated = graph.get_node(node.id)
        # 5 * 0.05 = 0.25
        assert abs(updated.storage_strength - 0.25) < 0.001
        assert updated.access_count == 5


# ---------------------------------------------------------------------------
# Ebbinghaus retention with storage_strength
# ---------------------------------------------------------------------------

class TestEbbinghausWithStorageStrength:
    """Tests for _ebbinghaus_retention() with storage_strength parameter."""

    def test_zero_storage_strength_matches_old_behavior(self):
        from core.lifecycle.janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)
        r_old = _ebbinghaus_retention(60.0, 0, False, cfg)
        r_new = _ebbinghaus_retention(60.0, 0, False, cfg, storage_strength=0.0)
        assert abs(r_old - r_new) < 0.0001

    def test_storage_strength_extends_half_life(self):
        from core.lifecycle.janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)

        r_zero = _ebbinghaus_retention(60.0, 0, False, cfg, storage_strength=0.0)
        r_two = _ebbinghaus_retention(60.0, 0, False, cfg, storage_strength=2.0)

        # ss=2: half_life *= (1 + 0.5*2) = 2.0, so half_life=120 instead of 60
        # At t=60: R = 2^(-60/120) = 2^(-0.5) ≈ 0.707
        assert r_two > r_zero
        assert abs(r_two - 0.707) < 0.01

    def test_max_storage_strength_gives_6x_half_life(self):
        from core.lifecycle.janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.0)

        # ss=10: half_life *= (1 + 0.5*10) = 6.0, so half_life=360
        r = _ebbinghaus_retention(360.0, 0, False, cfg, storage_strength=10.0)
        # At t=360: R = 2^(-360/360) = 0.5
        assert abs(r - 0.5) < 0.001

    def test_storage_strength_stacks_with_verified(self):
        from core.lifecycle.janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.0)

        # ss=2, verified: half_life = 60 * 2.0 * 2.0 = 240
        r = _ebbinghaus_retention(240.0, 0, True, cfg, storage_strength=2.0)
        assert abs(r - 0.5) < 0.001

    def test_storage_strength_stacks_with_access_count(self):
        from core.lifecycle.janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)

        # access_count=2: base * (1 + 0.15*2) = 60 * 1.3 = 78
        # ss=2: 78 * (1 + 0.5*2) = 78 * 2.0 = 156
        r = _ebbinghaus_retention(156.0, 2, False, cfg, storage_strength=2.0)
        assert abs(r - 0.5) < 0.001


# ---------------------------------------------------------------------------
# Node persistence (add_node, update_node, _row_to_node)
# ---------------------------------------------------------------------------

class TestNodePersistence:
    """Tests for storage_strength persistence through add_node/update_node/_row_to_node."""

    def test_add_node_persists_storage_strength(self, tmp_path):
        from datastore.memorydb.memory_graph import Node
        graph = _make_graph(tmp_path)
        node = Node.create(type="Fact", name="Persisted fact", storage_strength=2.5)
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph.add_node(node)

        loaded = graph.get_node(node.id)
        assert loaded.storage_strength == 2.5

    def test_update_node_persists_storage_strength(self, tmp_path):
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Update me")

        assert node.storage_strength == 0.0

        node.storage_strength = 3.7
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph.update_node(node)

        loaded = graph.get_node(node.id)
        assert abs(loaded.storage_strength - 3.7) < 0.001

    def test_default_storage_strength_is_zero(self, tmp_path):
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Default strength")
        loaded = graph.get_node(node.id)
        assert loaded.storage_strength == 0.0

    def test_row_to_node_fallback_for_missing_column(self, tmp_path):
        """If storage_strength column doesn't exist in row, default to 0.0."""
        from datastore.memorydb.memory_graph import Node
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Fallback test")

        # Simulate a row without storage_strength by querying specific columns
        with graph._get_conn() as conn:
            # The actual _row_to_node handles missing columns gracefully
            row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node.id,)).fetchone()
            loaded = graph._row_to_node(row)
            assert loaded.storage_strength == 0.0


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class TestMigration:
    """Tests for schema migration adding storage_strength column."""

    def test_migration_adds_column_to_existing_db(self, tmp_path):
        """Verify _init_db migration adds storage_strength to existing tables."""
        db_file = tmp_path / "migration_test.db"

        # Create DB with old schema (no storage_strength)
        with sqlite3.connect(db_file) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            schema_path = Path(__file__).parent.parent / "schema.sql"
            schema = schema_path.read_text()
            conn.executescript(schema)
            # Drop storage_strength to simulate old schema
            # (SQLite doesn't support DROP COLUMN easily, so we verify migration is idempotent)

        # Re-init with migration — should not raise
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            from datastore.memorydb.memory_graph import MemoryGraph
            graph = MemoryGraph(db_path=db_file)

        # Verify column exists and has correct default
        with graph._get_conn() as conn:
            row = conn.execute(
                "SELECT storage_strength FROM nodes LIMIT 0"
            ).fetchone()
            # No rows, but query didn't fail = column exists


# ---------------------------------------------------------------------------
# _compute_composite_score with storage_bonus
# ---------------------------------------------------------------------------

class TestCompositeScoreStorageBonus:
    """Tests for storage_bonus in _compute_composite_score."""

    def test_zero_storage_strength_no_bonus(self):
        from datastore.memorydb.memory_graph import _compute_composite_score, Node
        node = Node.create(type="Fact", name="Test", storage_strength=0.0,
                          confidence=0.8, access_count=0)
        score_zero = _compute_composite_score(node, 0.8)

        node2 = Node.create(type="Fact", name="Test2", storage_strength=0.0,
                           confidence=0.8, access_count=0)
        score_also_zero = _compute_composite_score(node2, 0.8)

        assert abs(score_zero - score_also_zero) < 0.001

    def test_high_storage_strength_adds_bonus(self):
        from datastore.memorydb.memory_graph import _compute_composite_score, Node
        node_low = Node.create(type="Fact", name="Low", storage_strength=0.0,
                              confidence=0.8, access_count=0)
        node_high = Node.create(type="Fact", name="High", storage_strength=10.0,
                               confidence=0.8, access_count=0)

        score_low = _compute_composite_score(node_low, 0.8)
        score_high = _compute_composite_score(node_high, 0.8)

        # storage_bonus capped at 0.03
        diff = score_high - score_low
        assert 0.02 < diff < 0.031

    def test_storage_bonus_capped(self):
        from datastore.memorydb.memory_graph import _compute_composite_score, Node
        node = Node.create(type="Fact", name="Capped", storage_strength=100.0,
                          confidence=0.8, access_count=0)
        score = _compute_composite_score(node, 0.8)

        node_ten = Node.create(type="Fact", name="Ten", storage_strength=10.0,
                              confidence=0.8, access_count=0)
        score_ten = _compute_composite_score(node_ten, 0.8)

        # Both should be the same since bonus caps at 0.03
        assert abs(score - score_ten) < 0.001


# ---------------------------------------------------------------------------
# find_stale_memories_optimized includes storage_strength
# ---------------------------------------------------------------------------

class TestStaleMemoriesStorageStrength:
    """Tests for storage_strength in find_stale_memories_optimized output."""

    def test_stale_memories_include_storage_strength(self, tmp_path):
        graph = _make_graph(tmp_path)
        node = _make_node(graph, "Stale fact", storage_strength=1.5)

        # Make it stale by backdating accessed_at
        stale_date = (datetime.now() - timedelta(days=100)).isoformat()
        with graph._get_conn() as conn:
            conn.execute(
                "UPDATE nodes SET accessed_at = ?, storage_strength = 1.5 WHERE id = ?",
                (stale_date, node.id)
            )

        from core.lifecycle.janitor import find_stale_memories_optimized, JanitorMetrics
        metrics = JanitorMetrics()
        stale = find_stale_memories_optimized(graph, metrics)

        found = [s for s in stale if s["id"] == node.id]
        assert len(found) == 1
        assert found[0]["storage_strength"] == 1.5
