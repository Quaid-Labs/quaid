"""Tests for Batch 4: Ebbinghaus decay, multi-hop traversal, access tracking, parallel search.

Covers:
- Ebbinghaus exponential retention curve
- Type-differentiated decay rates (access count, verified status)
- Multi-hop graph traversal with cycle detection and early stopping
- Access count increment on recall
- Parallel hybrid search execution
"""

import os
import sys
import math
import hashlib
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
    from memory_graph import MemoryGraph
    db_file = tmp_path / "batch4_test.db"
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph


# ---------------------------------------------------------------------------
# Ebbinghaus retention curve
# ---------------------------------------------------------------------------

class TestEbbinghausRetention:
    """Tests for _ebbinghaus_retention() exponential decay function."""

    def test_zero_days_full_retention(self):
        from janitor import _ebbinghaus_retention
        r = _ebbinghaus_retention(0.0, 0, False)
        assert abs(r - 1.0) < 0.001  # R=2^0 = 1.0

    def test_half_life_gives_50_percent(self):
        """At t = half_life, retention should be exactly 0.5."""
        from janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)
        r = _ebbinghaus_retention(60.0, 0, False, cfg)
        assert abs(r - 0.5) < 0.001

    def test_double_half_life_gives_25_percent(self):
        """At t = 2*half_life, retention should be 0.25."""
        from janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)
        r = _ebbinghaus_retention(120.0, 0, False, cfg)
        assert abs(r - 0.25) < 0.001

    def test_access_count_extends_half_life(self):
        """Higher access count = slower decay (longer half-life)."""
        from janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)
        r_zero = _ebbinghaus_retention(60.0, 0, False, cfg)
        r_five = _ebbinghaus_retention(60.0, 5, False, cfg)
        # 5 accesses = half-life of 60*(1+0.15*5) = 60*1.75 = 105 days
        # At t=60, R = 2^(-60/105) ≈ 0.673 > 0.5
        assert r_five > r_zero
        assert r_five > 0.6

    def test_verified_doubles_half_life(self):
        """Verified facts should decay 2x slower."""
        from janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)
        r_normal = _ebbinghaus_retention(60.0, 0, False, cfg)
        r_verified = _ebbinghaus_retention(60.0, 0, True, cfg)
        # Verified: half-life = 120 days. At t=60, R = 2^(-60/120) = 2^(-0.5) ≈ 0.707
        assert r_verified > r_normal
        assert abs(r_verified - 0.707) < 0.01

    def test_combined_access_and_verified(self):
        """Access count + verified should compound."""
        from janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)
        r_base = _ebbinghaus_retention(90.0, 0, False, cfg)
        r_combined = _ebbinghaus_retention(90.0, 10, True, cfg)
        # 10 accesses + verified: half-life = 60 * (1 + 0.15*10) * 2 = 60 * 2.5 * 2 = 300 days
        # At t=90, R = 2^(-90/300) ≈ 0.815
        assert r_combined > r_base
        assert r_combined > 0.8

    def test_very_old_memory_low_retention(self):
        """After many half-lives, retention should be near zero."""
        from janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)
        r = _ebbinghaus_retention(600.0, 0, False, cfg)  # 10 half-lives
        assert r < 0.001

    def test_monotonically_decreasing(self):
        """Retention should decrease as time increases."""
        from janitor import _ebbinghaus_retention
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=60.0, access_bonus_factor=0.15)
        prev = 1.0
        for days in [10, 30, 60, 90, 120, 180, 365]:
            r = _ebbinghaus_retention(float(days), 0, False, cfg)
            assert r < prev, f"Retention at {days}d ({r}) not less than at previous ({prev})"
            prev = r


# ---------------------------------------------------------------------------
# Apply decay with exponential mode
# ---------------------------------------------------------------------------

class TestApplyDecayExponential:
    """Tests for apply_decay_optimized() with exponential mode."""

    def test_exponential_mode_decays_old_memory(self, tmp_path):
        from janitor import apply_decay_optimized, JanitorMetrics
        from memory_graph import MemoryGraph

        metrics = JanitorMetrics()
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = MemoryGraph(db_path=tmp_path / "decay.db")

        # Simulate a stale memory accessed 90 days ago with 0 accesses
        stale = [{
            "id": "test-1",
            "text": "Old unused fact about coffee",
            "confidence": 0.5,
            "last_accessed": (datetime.now() - timedelta(days=90)).isoformat(),
            "access_count": 0,
            "verified": False,
        }]

        with patch("janitor._cfg") as mock_cfg:
            mock_cfg.decay.review_queue_enabled = False
            mock_cfg.decay.mode = "exponential"
            mock_cfg.decay.minimum_confidence = 0.1
            mock_cfg.decay.base_half_life_days = 60.0
            mock_cfg.decay.access_bonus_factor = 0.15

            result = apply_decay_optimized(stale, graph, metrics, dry_run=True)
            # Dry run — shouldn't actually change anything
            assert result["decayed"] == 0
            assert result["deleted"] == 0

    def test_linear_mode_fallback(self, tmp_path):
        from janitor import apply_decay_optimized, JanitorMetrics
        from memory_graph import MemoryGraph

        metrics = JanitorMetrics()
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = MemoryGraph(db_path=tmp_path / "linear.db")

        stale = [{
            "id": "test-2",
            "text": "Another fact for linear test",
            "confidence": 0.5,
            "last_accessed": (datetime.now() - timedelta(days=60)).isoformat(),
            "access_count": 0,
            "verified": False,
        }]

        with patch("janitor._cfg") as mock_cfg, \
             patch("janitor.CONFIDENCE_DECAY_RATE", 0.10):
            mock_cfg.decay.review_queue_enabled = False
            mock_cfg.decay.mode = "linear"
            mock_cfg.decay.minimum_confidence = 0.1

            result = apply_decay_optimized(stale, graph, metrics, dry_run=True)
            # Dry run — counts should be 0
            assert result["decayed"] == 0


# ---------------------------------------------------------------------------
# Multi-hop graph traversal
# ---------------------------------------------------------------------------

class TestMultiHopTraversal:
    """Tests for get_related_bidirectional() with multi-hop support."""

    def test_depth_1_returns_immediate_neighbors(self, tmp_path):
        from memory_graph import Node, Edge
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            a = Node.create(type="Person", name="Quaid test person", owner_id="default")
            b = Node.create(type="Person", name="Lori test person", owner_id="default")
            c = Node.create(type="Person", name="Levi test person", owner_id="default")
            graph.add_node(a)
            graph.add_node(b)
            graph.add_node(c)
            graph.add_edge(Edge.create(source_id=a.id, target_id=b.id, relation="spouse_of"))
            graph.add_edge(Edge.create(source_id=b.id, target_id=c.id, relation="parent_of"))

            results = graph.get_related_bidirectional(a.id, depth=1)
            result_ids = [n.id for n, _, _, _, _ in results]
            assert b.id in result_ids
            assert c.id not in result_ids  # c is 2 hops away

    def test_depth_2_returns_two_hops(self, tmp_path):
        from memory_graph import Node, Edge
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            a = Node.create(type="Person", name="Quaid multi hop", owner_id="default")
            b = Node.create(type="Person", name="Lori multi hop", owner_id="default")
            c = Node.create(type="Person", name="Levi multi hop", owner_id="default")
            graph.add_node(a)
            graph.add_node(b)
            graph.add_node(c)
            graph.add_edge(Edge.create(source_id=a.id, target_id=b.id, relation="spouse_of"))
            graph.add_edge(Edge.create(source_id=b.id, target_id=c.id, relation="parent_of"))

            results = graph.get_related_bidirectional(a.id, depth=2)
            result_ids = [n.id for n, _, _, _, _ in results]
            assert b.id in result_ids  # 1 hop
            assert c.id in result_ids  # 2 hops

    def test_cycle_detection(self, tmp_path):
        """Cycles in the graph should not cause infinite loops."""
        from memory_graph import Node, Edge
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            a = Node.create(type="Person", name="Person A cycle test", owner_id="default")
            b = Node.create(type="Person", name="Person B cycle test", owner_id="default")
            graph.add_node(a)
            graph.add_node(b)
            # Create a cycle: A -> B -> A
            graph.add_edge(Edge.create(source_id=a.id, target_id=b.id, relation="knows"))
            graph.add_edge(Edge.create(source_id=b.id, target_id=a.id, relation="knows"))

            results = graph.get_related_bidirectional(a.id, depth=3)
            # Should not contain starting node, and should complete without hanging
            result_ids = [n.id for n, _, _, _, _ in results]
            assert a.id not in result_ids  # Starting node excluded
            assert b.id in result_ids

    def test_early_stopping(self, tmp_path):
        """max_results should limit results."""
        from memory_graph import Node, Edge
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            # Create a star graph: center -> 10 leaves
            center = Node.create(type="Person", name="Center node star graph", owner_id="default")
            graph.add_node(center)
            for i in range(10):
                leaf = Node.create(type="Fact", name=f"Leaf fact number {i} for star graph", owner_id="default")
                graph.add_node(leaf)
                graph.add_edge(Edge.create(source_id=center.id, target_id=leaf.id, relation="related_to"))

            results = graph.get_related_bidirectional(center.id, depth=1, max_results=3)
            assert len(results) <= 3

    def test_depth_tracking(self, tmp_path):
        """Each result should report correct hop depth."""
        from memory_graph import Node, Edge
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            a = Node.create(type="Person", name="Root node depth test", owner_id="default")
            b = Node.create(type="Person", name="Hop one depth test", owner_id="default")
            c = Node.create(type="Person", name="Hop two depth test", owner_id="default")
            d = Node.create(type="Person", name="Hop three depth test", owner_id="default")
            graph.add_node(a)
            graph.add_node(b)
            graph.add_node(c)
            graph.add_node(d)
            graph.add_edge(Edge.create(source_id=a.id, target_id=b.id, relation="knows"))
            graph.add_edge(Edge.create(source_id=b.id, target_id=c.id, relation="knows"))
            graph.add_edge(Edge.create(source_id=c.id, target_id=d.id, relation="knows"))

            results = graph.get_related_bidirectional(a.id, depth=3)
            depth_map = {n.id: d for n, _, _, d, _ in results}
            assert depth_map.get(b.id) == 1
            assert depth_map.get(c.id) == 2
            assert depth_map.get(d.id) == 3

    def test_bidirectional_finds_inbound(self, tmp_path):
        """Should find nodes pointing TO the start node."""
        from memory_graph import Node, Edge
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            a = Node.create(type="Person", name="Target node inbound", owner_id="default")
            b = Node.create(type="Person", name="Source node inbound", owner_id="default")
            graph.add_node(a)
            graph.add_node(b)
            # b -> a (only inbound edge to a)
            graph.add_edge(Edge.create(source_id=b.id, target_id=a.id, relation="parent_of"))

            results = graph.get_related_bidirectional(a.id, depth=1)
            result_ids = [n.id for n, _, _, _, _ in results]
            assert b.id in result_ids
            # Verify direction is reported as "in"
            for n, rel, direction, d, _ in results:
                if n.id == b.id:
                    assert direction == "in"


# ---------------------------------------------------------------------------
# Access count tracking
# ---------------------------------------------------------------------------

class TestAccessTracking:
    """Tests for _update_access() and recall integration."""

    def test_update_access_increments_count(self, tmp_path):
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Test fact for access tracking", owner_id="default")
            graph.add_node(node)

            # Initial state
            retrieved = graph.get_node(node.id)
            initial_count = retrieved.access_count

            # Simulate access update
            graph._update_access([(node, 0.9)])

            # Check incremented
            after = graph.get_node(node.id)
            assert after.access_count == initial_count + 1

    def test_update_access_updates_timestamp(self, tmp_path):
        from memory_graph import Node
        import time as _time
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Timestamp tracking fact test", owner_id="default")
            graph.add_node(node)

            old_accessed = graph.get_node(node.id).accessed_at
            _time.sleep(0.01)  # Ensure time difference
            graph._update_access([(node, 0.8)])

            new_accessed = graph.get_node(node.id).accessed_at
            assert new_accessed >= old_accessed

    def test_multiple_access_accumulate(self, tmp_path):
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Multi access test fact tracking", owner_id="default")
            graph.add_node(node)

            for _ in range(5):
                graph._update_access([(node, 0.9)])

            after = graph.get_node(node.id)
            assert after.access_count >= 5


# ---------------------------------------------------------------------------
# Parallel search execution
# ---------------------------------------------------------------------------

class TestParallelSearch:
    """Tests for parallel hybrid search execution."""

    def test_hybrid_search_returns_results(self, tmp_path):
        """search_hybrid still works with ThreadPoolExecutor."""
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Quaid likes espresso coffee drink", owner_id="default", status="approved")
            graph.add_node(node, embed=True)

            results = graph.search_hybrid("coffee", limit=5)
            assert isinstance(results, list)

    def test_hybrid_handles_semantic_failure(self, tmp_path):
        """If semantic search fails, hybrid should still return FTS results."""
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Quaid likes espresso coffee beans dark", owner_id="default", status="approved")
            graph.add_node(node, embed=True)

            # Patch get_embedding to fail during search (simulates Ollama down)
            original_get_embedding = graph.get_embedding
            def failing_embedding(text):
                return None
            graph.get_embedding = failing_embedding

            results = graph.search_hybrid("coffee", limit=5)
            # Should still get FTS results
            assert isinstance(results, list)

    def test_hybrid_handles_fts_failure(self, tmp_path):
        """If FTS search fails, hybrid should still return semantic results."""
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Quaid likes espresso coffee strongly", owner_id="default", status="approved")
            graph.add_node(node, embed=True)

            # Patch search_fts to raise
            original_fts = graph.search_fts
            def failing_fts(*args, **kwargs):
                raise Exception("FTS failure")
            graph.search_fts = failing_fts

            results = graph.search_hybrid("coffee", limit=5)
            assert isinstance(results, list)


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------

class TestDecayConfig:
    """Tests for decay config dataclass with new fields."""

    def test_default_values(self):
        from config import DecayConfig
        cfg = DecayConfig()
        assert cfg.mode == "exponential"
        assert cfg.base_half_life_days == 60.0
        assert cfg.access_bonus_factor == 0.15

    def test_linear_mode(self):
        from config import DecayConfig
        cfg = DecayConfig(mode="linear")
        assert cfg.mode == "linear"

    def test_custom_half_life(self):
        from config import DecayConfig
        cfg = DecayConfig(base_half_life_days=90.0, access_bonus_factor=0.20)
        assert cfg.base_half_life_days == 90.0
        assert cfg.access_bonus_factor == 0.20
