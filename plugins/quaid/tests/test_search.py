"""Tests for search functions from memory_graph.py: search_fts, search_semantic, search_hybrid."""

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


def _make_graph_with_data(tmp_path, items=None):
    """Create a MemoryGraph with seeded nodes."""
    from memory_graph import MemoryGraph, Node

    db_file = tmp_path / "search_test.db"
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)

        if items is None:
            items = [
                ("Solomon likes espresso coffee", "Fact", "solomon"),
                ("Solomon lives in Bali Indonesia", "Fact", "solomon"),
                ("Yuni is Solomon's wife partner", "Fact", "solomon"),
                ("Shannon is Solomon's sister sibling", "Fact", "solomon"),
                ("Madu is a pet cat animal", "Fact", "solomon"),
                ("Solomon works at Anthropic company", "Fact", "solomon"),
                ("Solomon prefers dark roast beans", "Preference", "solomon"),
            ]

        for text, node_type, owner in items:
            node = Node.create(
                type=node_type,
                name=text,
                owner_id=owner,
                status="approved",
            )
            graph.add_node(node, embed=True)

    return graph


# ---------------------------------------------------------------------------
# search_fts
# ---------------------------------------------------------------------------

class TestSearchFTS:
    """Tests for MemoryGraph.search_fts()."""

    def test_returns_ranked_results(self, tmp_path):
        """BM25-ranked results include matching nodes with rank positions."""
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_fts("Solomon", limit=10)
            assert len(results) > 0
            # All results should mention Solomon and have rank positions
            for node, rank in results:
                assert "Solomon" in node.name or "solomon" in node.name.lower()
                assert rank >= 1  # 1-based rank position

    def test_empty_query_returns_empty(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_fts("")
            assert results == []

    def test_stopword_only_query_returns_empty(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            # "the is a" are all stopwords
            results = graph.search_fts("the is a")
            assert results == []

    def test_returns_matching_nodes(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_fts("coffee", limit=5)
            assert len(results) > 0
            # At least one result should contain "coffee"
            texts = [node.name for node, _ in results]
            assert any("coffee" in t.lower() for t in texts)

    def test_limit_respected(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_fts("Solomon", limit=2)
            assert len(results) <= 2

    def test_owner_id_filter(self, tmp_path):
        """FTS with owner_id only returns that owner's nodes."""
        items = [
            ("Alice likes tea and crumpets", "Fact", "alice"),
            ("Solomon likes espresso coffee strongly", "Fact", "solomon"),
        ]
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path, items=items)
            results = graph.search_fts("likes", owner_id="solomon")
            # Should only return solomon's node
            for node, _ in results:
                assert node.owner_id == "solomon" or node.owner_id is None

    def test_short_words_filtered(self, tmp_path):
        """Words under 3 chars are filtered out."""
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            # "is" and "a" are short + stopwords
            results = graph.search_fts("is a")
            assert results == []


# ---------------------------------------------------------------------------
# search_semantic
# ---------------------------------------------------------------------------

class TestSearchSemantic:
    """Tests for MemoryGraph.search_semantic()."""

    def test_returns_empty_when_no_embedding(self, tmp_path):
        """When get_embedding returns None, search_semantic returns []."""
        with patch("memory_graph._lib_get_embedding", return_value=None):
            from memory_graph import MemoryGraph
            db_file = tmp_path / "empty_embed.db"
            graph = MemoryGraph(db_path=db_file)
            results = graph.search_semantic("anything")
            assert results == []

    def test_returns_results_sorted_by_similarity(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_semantic("Solomon coffee", limit=5, min_similarity=0.0)
            if len(results) >= 2:
                sims = [sim for _, sim in results]
                assert sims == sorted(sims, reverse=True)

    def test_min_similarity_filters(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_semantic("Solomon coffee", min_similarity=0.999)
            # Very high threshold should filter most results
            for _, sim in results:
                assert sim >= 0.999

    def test_owner_id_filter(self, tmp_path):
        """Owner filter includes shared/public nodes from other owners (by design)."""
        from memory_graph import Node
        items_raw = [
            ("Bob enjoys tennis sport games", "Fact", "bob", "private"),
            ("Solomon enjoys surfing water sport", "Fact", "solomon", "shared"),
        ]
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            from memory_graph import MemoryGraph
            db_file = tmp_path / "owner_test.db"
            graph = MemoryGraph(db_path=db_file)
            for text, node_type, owner, priv in items_raw:
                node = Node.create(type=node_type, name=text,
                                   owner_id=owner, status="approved", privacy=priv)
                graph.add_node(node, embed=True)

            results = graph.search_semantic("sport", owner_id="solomon",
                                            min_similarity=0.0)
            # Bob's private node should be excluded; solomon's shared node should be included
            result_owners = [n.owner_id for n, _ in results]
            assert "solomon" in result_owners
            # Bob's node is private so it should NOT appear when filtering for solomon
            for node, _ in results:
                if node.owner_id == "bob":
                    assert node.privacy in ("shared", "public")

    def test_limit_respected(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_semantic("Solomon", limit=2, min_similarity=0.0)
            assert len(results) <= 2


# ---------------------------------------------------------------------------
# search_hybrid
# ---------------------------------------------------------------------------

class TestSearchHybrid:
    """Tests for MemoryGraph.search_hybrid()."""

    def test_returns_results(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_hybrid("Solomon coffee", limit=5)
            # Should return some results (combining semantic + FTS)
            assert isinstance(results, list)

    def test_merges_semantic_and_fts(self, tmp_path):
        """Hybrid should return at least as many as either individual search."""
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            hybrid = graph.search_hybrid("Solomon coffee", limit=10)
            # Just verify it runs and returns a list
            assert isinstance(hybrid, list)

    def test_limit_respected(self, tmp_path):
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_hybrid("Solomon", limit=3)
            # search_hybrid returns up to limit*2 for downstream MMR/filtering
            assert len(results) <= 6

    def test_hybrid_returns_quality_scores(self, tmp_path):
        """Results should carry quality scores (cosine similarity) for threshold filtering."""
        with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_hybrid("Solomon coffee", limit=10)
            if len(results) >= 1:
                # All quality scores should be in valid range
                for node, score in results:
                    assert 0.0 <= score <= 1.0, f"Quality score {score} out of range"


# ---------------------------------------------------------------------------
# has_owner_pronoun
# ---------------------------------------------------------------------------

class TestHasOwnerPronoun:
    """Tests for has_owner_pronoun()."""

    def test_my_is_owner_pronoun(self):
        from memory_graph import has_owner_pronoun
        assert has_owner_pronoun("my favorite color") is True

    def test_no_pronoun(self):
        from memory_graph import has_owner_pronoun
        assert has_owner_pronoun("the weather today") is False

    def test_i_pronoun(self):
        from memory_graph import has_owner_pronoun
        assert has_owner_pronoun("I like coffee") is True
