"""Tests for Batch 2: Data Quality Foundation features.

Covers:
- Content hash pre-filter (fast exact dedup)
- Embedding cache (avoid recomputation)
- Fact versioning (supersedes tracking)
- KB health metrics
"""

import os
import sys
import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env BEFORE imports
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
    """Create a fresh MemoryGraph."""
    from memory_graph import MemoryGraph
    db_file = tmp_path / "batch2_test.db"
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph


# ---------------------------------------------------------------------------
# _content_hash
# ---------------------------------------------------------------------------

class TestContentHash:
    """Tests for _content_hash() utility function."""

    def test_deterministic(self):
        from memory_graph import _content_hash
        assert _content_hash("hello world") == _content_hash("hello world")

    def test_case_insensitive(self):
        from memory_graph import _content_hash
        assert _content_hash("Hello World") == _content_hash("hello world")

    def test_whitespace_normalized(self):
        from memory_graph import _content_hash
        assert _content_hash("hello  world") == _content_hash("hello world")
        assert _content_hash("  hello world  ") == _content_hash("hello world")

    def test_different_texts_different_hashes(self):
        from memory_graph import _content_hash
        assert _content_hash("solomon likes coffee") != _content_hash("solomon likes tea")

    def test_returns_sha256_hex(self):
        from memory_graph import _content_hash
        h = _content_hash("test")
        assert len(h) == 64  # SHA256 hex is 64 chars
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Content hash on add_node
# ---------------------------------------------------------------------------

class TestContentHashOnInsert:
    """Tests for content_hash being set on node insert."""

    def test_add_node_sets_content_hash(self, tmp_path):
        from memory_graph import Node, _content_hash
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Solomon likes coffee", owner_id="solomon")
            graph.add_node(node)
            retrieved = graph.get_node(node.id)
            assert retrieved.content_hash is not None
            assert retrieved.content_hash == _content_hash("Solomon likes coffee")

    def test_update_node_sets_content_hash(self, tmp_path):
        from memory_graph import Node, _content_hash
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Solomon likes coffee", owner_id="solomon")
            graph.add_node(node)
            # Update with new name
            node.name = "Solomon loves espresso"
            node.content_hash = None  # Force recompute
            graph.update_node(node)
            retrieved = graph.get_node(node.id)
            assert retrieved.content_hash == _content_hash("Solomon loves espresso")


# ---------------------------------------------------------------------------
# Content hash dedup in store()
# ---------------------------------------------------------------------------

class TestContentHashDedup:
    """Tests for content hash pre-filter in store()."""

    def test_exact_duplicate_caught_by_hash(self, tmp_path):
        """Identical text (after normalization) is caught by hash before cosine."""
        from memory_graph import store, get_graph, _content_hash, MemoryGraph
        db_file = tmp_path / "dedup_test.db"
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            with patch("memory_graph.get_graph") as mock_get:
                graph = MemoryGraph(db_path=db_file)
                mock_get.return_value = graph
                # Store first
                r1 = store("Solomon enjoys espresso coffee", owner_id="solomon", status="approved")
                assert r1["status"] == "created"
                # Store exact same text — should be caught by hash
                r2 = store("Solomon enjoys espresso coffee", owner_id="solomon", status="approved")
                assert r2["status"] == "duplicate"
                assert r2["similarity"] == 1.0

    def test_case_variation_caught_by_hash(self, tmp_path):
        """Case-different text with same words is caught by hash."""
        from memory_graph import store, get_graph, MemoryGraph
        db_file = tmp_path / "dedup_case.db"
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            with patch("memory_graph.get_graph") as mock_get:
                graph = MemoryGraph(db_path=db_file)
                mock_get.return_value = graph
                r1 = store("Solomon enjoys espresso coffee", owner_id="solomon", status="approved")
                assert r1["status"] == "created"
                r2 = store("solomon enjoys espresso coffee", owner_id="solomon", status="approved")
                assert r2["status"] == "duplicate"


# ---------------------------------------------------------------------------
# Embedding cache
# ---------------------------------------------------------------------------

class TestEmbeddingCache:
    """Tests for embedding cache in MemoryGraph.get_embedding()."""

    def test_cache_stores_and_retrieves(self, tmp_path):
        """Second call with same text returns cached embedding without Ollama call."""
        call_count = [0]

        def _counting_embedding(text):
            call_count[0] += 1
            return _fake_get_embedding(text)

        with patch("memory_graph._lib_get_embedding", side_effect=_counting_embedding):
            graph = _make_graph(tmp_path)
            # First call — should hit Ollama
            e1 = graph.get_embedding("test text for caching")
            assert call_count[0] == 1
            assert e1 is not None
            # Second call — should hit cache
            e2 = graph.get_embedding("test text for caching")
            assert call_count[0] == 1  # No additional Ollama call
            # Compare with tolerance (pack/unpack cycle: float64→float32→float64)
            assert len(e2) == len(e1)
            for a, b in zip(e1, e2):
                assert abs(a - b) < 1e-5

    def test_different_text_not_cached(self, tmp_path):
        """Different text should not return cached embedding."""
        call_count = [0]

        def _counting_embedding(text):
            call_count[0] += 1
            return _fake_get_embedding(text)

        with patch("memory_graph._lib_get_embedding", side_effect=_counting_embedding):
            graph = _make_graph(tmp_path)
            graph.get_embedding("text one for cache test")
            graph.get_embedding("text two for cache test")
            assert call_count[0] == 2  # Both hit Ollama

    def test_cache_survives_new_graph_instance(self, tmp_path):
        """Cache is in DB, so new MemoryGraph instance should find cached embeddings."""
        call_count = [0]

        def _counting_embedding(text):
            call_count[0] += 1
            return _fake_get_embedding(text)

        db_file = tmp_path / "cache_persist.db"
        with patch("memory_graph._lib_get_embedding", side_effect=_counting_embedding):
            from memory_graph import MemoryGraph
            graph1 = MemoryGraph(db_path=db_file)
            graph1.get_embedding("persistent cache test text")
            assert call_count[0] == 1

            graph2 = MemoryGraph(db_path=db_file)
            e = graph2.get_embedding("persistent cache test text")
            assert call_count[0] == 1  # Still 1 — cache hit
            assert e is not None


# ---------------------------------------------------------------------------
# Fact versioning (supersede)
# ---------------------------------------------------------------------------

class TestFactVersioning:
    """Tests for supersede_node() and get_fact_history()."""

    def test_supersede_marks_old_node(self, tmp_path):
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            old = Node.create(type="Fact", name="Solomon lives in Texas", owner_id="solomon")
            new = Node.create(type="Fact", name="Solomon lives in Bali", owner_id="solomon")
            graph.add_node(old)
            graph.add_node(new)

            result = graph.supersede_node(old.id, new.id)
            assert result is True

            old_retrieved = graph.get_node(old.id)
            assert old_retrieved.superseded_by == new.id
            assert old_retrieved.confidence == 0.1
            assert old_retrieved.valid_until is not None

    def test_superseded_node_excluded_from_search(self, tmp_path):
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            old = Node.create(type="Fact", name="Solomon lives in Texas state", owner_id="solomon", status="approved")
            new = Node.create(type="Fact", name="Solomon lives in Bali Indonesia", owner_id="solomon", status="approved")
            graph.add_node(old)
            graph.add_node(new)
            graph.supersede_node(old.id, new.id)

            # FTS search should not return superseded node
            results = graph.search_fts("Solomon lives", limit=10)
            result_ids = [n.id for n, _ in results]
            assert old.id not in result_ids
            assert new.id in result_ids

    def test_get_fact_history_single(self, tmp_path):
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Solomon likes coffee", owner_id="solomon")
            graph.add_node(node)
            history = graph.get_fact_history(node.id)
            assert len(history) == 1
            assert history[0].id == node.id

    def test_get_fact_history_chain(self, tmp_path):
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            v1 = Node.create(type="Fact", name="Solomon lives in Austin", owner_id="solomon")
            v2 = Node.create(type="Fact", name="Solomon lives in Bali", owner_id="solomon")
            v3 = Node.create(type="Fact", name="Solomon lives in Ubud Bali", owner_id="solomon")
            graph.add_node(v1)
            graph.add_node(v2)
            graph.add_node(v3)
            graph.supersede_node(v1.id, v2.id)
            graph.supersede_node(v2.id, v3.id)

            history = graph.get_fact_history(v3.id)
            assert len(history) == 3
            assert history[0].id == v1.id  # Oldest
            assert history[1].id == v2.id  # Middle
            assert history[2].id == v3.id  # Current

    def test_double_supersede_noop(self, tmp_path):
        """Superseding an already-superseded node should be a no-op."""
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            old = Node.create(type="Fact", name="Solomon lives in Texas", owner_id="solomon")
            new1 = Node.create(type="Fact", name="Solomon lives in Bali", owner_id="solomon")
            new2 = Node.create(type="Fact", name="Solomon lives in Japan", owner_id="solomon")
            graph.add_node(old)
            graph.add_node(new1)
            graph.add_node(new2)
            graph.supersede_node(old.id, new1.id)
            result = graph.supersede_node(old.id, new2.id)
            assert result is False  # Already superseded


# ---------------------------------------------------------------------------
# KB health metrics
# ---------------------------------------------------------------------------

class TestHealthMetrics:
    """Tests for MemoryGraph.get_health_metrics()."""

    def test_returns_expected_keys(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            metrics = graph.get_health_metrics()
            expected_keys = {
                "total_nodes", "total_edges", "confidence_by_status",
                "embedding_coverage", "content_hash_coverage",
                "superseded_facts", "orphan_nodes",
                "staleness_distribution", "top_edge_types",
                "dedup_log", "embedding_cache_size",
            }
            assert expected_keys == set(metrics.keys())

    def test_empty_db_returns_zeroes(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            metrics = graph.get_health_metrics()
            assert metrics["total_nodes"] == 0
            assert metrics["total_edges"] == 0
            assert metrics["superseded_facts"] == 0
            assert metrics["orphan_nodes"] == 0

    def test_counts_with_data(self, tmp_path):
        from memory_graph import Node, Edge
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            n1 = Node.create(type="Fact", name="Solomon likes coffee drinks", owner_id="solomon", status="approved")
            n2 = Node.create(type="Fact", name="Solomon lives in Bali Indonesia", owner_id="solomon", status="approved")
            n3 = Node.create(type="Fact", name="Yuni is Solomon wife partner", owner_id="solomon", status="pending")
            graph.add_node(n1)
            graph.add_node(n2)
            graph.add_node(n3)

            edge = Edge.create(source_id=n1.id, target_id=n2.id, relation="related_to")
            graph.add_edge(edge)

            metrics = graph.get_health_metrics()
            assert metrics["total_nodes"] == 3
            assert metrics["total_edges"] == 1
            assert metrics["orphan_nodes"] == 1  # n3 has no edges
            assert metrics["content_hash_coverage"] == "3/3"
            assert "approved" in metrics["confidence_by_status"]
            assert "pending" in metrics["confidence_by_status"]
            assert metrics["confidence_by_status"]["approved"]["count"] == 2

    def test_superseded_counted(self, tmp_path):
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            old = Node.create(type="Fact", name="Solomon lives in Texas state", owner_id="solomon")
            new = Node.create(type="Fact", name="Solomon lives in Bali Indonesia", owner_id="solomon")
            graph.add_node(old)
            graph.add_node(new)
            graph.supersede_node(old.id, new.id)
            metrics = graph.get_health_metrics()
            assert metrics["superseded_facts"] == 1

    def test_staleness_distribution(self, tmp_path):
        from memory_graph import Node
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            node = Node.create(type="Fact", name="Solomon likes coffee espresso", owner_id="solomon")
            graph.add_node(node)
            metrics = graph.get_health_metrics()
            # Node was just created, so it should be in 0-7d bucket
            assert metrics["staleness_distribution"]["0-7d"] == 1

    def test_embedding_cache_counted(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph(tmp_path)
            graph.get_embedding("cache test text for metrics")
            metrics = graph.get_health_metrics()
            assert metrics["embedding_cache_size"] >= 1
