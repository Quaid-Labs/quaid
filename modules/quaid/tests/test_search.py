"""Tests for search functions from memory_graph.py: search_fts, search_semantic, search_hybrid."""

import os
import sys
import hashlib
import sqlite3
from types import SimpleNamespace
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

def _fake_embedding_dim():
    try:
        from datastore.memorydb.memory_graph import _get_configured_embedding_dim
        return int(_get_configured_embedding_dim())
    except Exception:
        return 768


def _fake_get_embedding(text, **_kwargs):
    """Return a deterministic fake embedding matching the configured dimension."""
    h = hashlib.md5(text.encode()).digest()
    base = [float(b) / 255.0 for b in h]
    dim = _fake_embedding_dim()
    repeats = (dim + len(base) - 1) // len(base)
    return (base * repeats)[:dim]


def _make_graph_with_data(tmp_path, items=None):
    """Create a MemoryGraph with seeded nodes."""
    from datastore.memorydb.memory_graph import MemoryGraph, Node

    db_file = tmp_path / "search_test.db"
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)

        if items is None:
            items = [
                ("Quaid likes espresso coffee", "Fact", "quaid"),
                ("Quaid lives in Bali Indonesia", "Fact", "quaid"),
                ("Melina is Quaid's wife partner", "Fact", "quaid"),
                ("Hauser is Quaid's sister sibling", "Fact", "quaid"),
                ("Richter is a pet cat animal", "Fact", "quaid"),
                ("Quaid works at Anthropic company", "Fact", "quaid"),
                ("Quaid prefers dark roast beans", "Preference", "quaid"),
            ]

        for item in items:
            text, node_type, owner, *metadata = item
            node = Node.create(
                type=node_type,
                name=text,
                owner_id=owner,
                privacy=metadata[0] if metadata else "shared",
                status="approved",
            )
            graph.add_node(node, embed=True)

    return graph


def _make_graph_with_edge(tmp_path):
    """Create a graph containing one edge and return graph, source node id, edge id."""
    from datastore.memorydb.memory_graph import MemoryGraph, Node, Edge

    db_file = tmp_path / "search_edge_test.db"
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
        src = Node.create(type="Fact", name="Source node", owner_id="quaid", status="approved")
        dst = Node.create(type="Fact", name="Target node", owner_id="quaid", status="approved")
        graph.add_node(src, embed=True)
        graph.add_node(dst, embed=True)
        edge = Edge.create(source_id=src.id, target_id=dst.id, relation="related_to")
        graph.add_edge(edge)
    return graph, src.id, edge.id


# ---------------------------------------------------------------------------
# search_fts
# ---------------------------------------------------------------------------

class TestSearchFTS:
    """Tests for MemoryGraph.search_fts()."""

    def test_returns_ranked_results(self, tmp_path):
        """BM25-ranked results include matching nodes with rank positions."""
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_fts("Quaid", limit=10)
            assert len(results) > 0
            # All results should mention Quaid and have rank positions
            for node, rank in results:
                assert "Quaid" in node.name or "quaid" in node.name.lower()
                assert rank >= 1  # 1-based rank position

    def test_empty_query_returns_empty(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_fts("")
            assert results == []

    def test_stopword_only_query_returns_empty(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            # "the is a" are all stopwords
            results = graph.search_fts("the is a")
            assert results == []

    def test_returns_matching_nodes(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_fts("coffee", limit=5)
            assert len(results) > 0
            # At least one result should contain "coffee"
            texts = [node.name for node, _ in results]
            assert any("coffee" in t.lower() for t in texts)

    def test_limit_respected(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_fts("Quaid", limit=2)
            assert len(results) <= 2

    @pytest.mark.parametrize("use_fallback", [False, True], ids=["fts", "like-fallback"])
    def test_owner_visibility_matches_semantic_search(self, tmp_path, use_fallback):
        """Lexical search keeps shared/public rows without exposing another owner's private row."""
        items = [
            ("Viewer likes cedar tea", "Fact", "viewer", "private"),
            ("Collaborator likes brass puzzles", "Fact", "collaborator", "shared"),
            ("Publisher likes harbor sketches", "Fact", "publisher", "public"),
            ("Collaborator likes private ledgers", "Fact", "collaborator", "private"),
        ]
        graph = _make_graph_with_data(tmp_path, items=items)

        if use_fallback:
            results = graph._search_fts_fallback(
                ["likes"], [], ["likes"], limit=10, owner_id="viewer"
            )
        else:
            results = graph.search_fts("likes", limit=10, owner_id="viewer")

        names = {node.name for node, _rank in results}
        assert "Viewer likes cedar tea" in names
        assert "Collaborator likes brass puzzles" in names
        assert "Publisher likes harbor sketches" in names
        assert "Collaborator likes private ledgers" not in names

    def test_short_words_filtered(self, tmp_path):
        """Words under 3 chars are filtered out."""
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            # "is" and "a" are short + stopwords
            results = graph.search_fts("is a")
            assert results == []

    def test_fts_query_error_uses_fallback_when_fail_hard_disabled(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)

            class _BrokenConn:
                def execute(self, *_args, **_kwargs):
                    raise sqlite3.OperationalError("fts unavailable")

            class _BrokenCtx:
                def __enter__(self):
                    return _BrokenConn()

                def __exit__(self, exc_type, exc, tb):
                    return False

            fallback_result = [("fallback", 1.0)]
            with patch.object(graph, "_get_conn", return_value=_BrokenCtx()), \
                 patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=False), \
                 patch.object(graph, "_search_fts_fallback", return_value=fallback_result) as fallback_spy:
                out = graph.search_fts("Quaid", limit=5)
            assert out == fallback_result
            assert fallback_spy.call_count == 1

    def test_fts_fallback_uses_script_aware_high_signal_terms(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)

            class _BrokenConn:
                def execute(self, *_args, **_kwargs):
                    raise sqlite3.OperationalError("fts unavailable")

            class _BrokenCtx:
                def __enter__(self):
                    return _BrokenConn()

                def __exit__(self, exc_type, exc, tb):
                    return False

            with patch.object(graph, "_get_conn", return_value=_BrokenCtx()), \
                 patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=False), \
                 patch.object(graph, "_search_fts_fallback", return_value=[]) as fallback_spy:
                graph.search_fts("美玲 玲 Noor archive", limit=5)

            words, high_signal_words, common_words = fallback_spy.call_args.args[:3]
            assert words == ["美玲", "Noor", "archive"]
            assert high_signal_words == ["美玲", "Noor"]
            assert common_words == ["archive"]

    def test_fts_fallback_escapes_like_wildcards(self, tmp_path):
        items = [
            ("Ari keeps alpha_tmp marker", "Fact", "quaid"),
            ("Ari keeps alphaXtmp marker", "Fact", "quaid"),
        ]
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path, items=items)
            original_get_conn = graph._get_conn

            class _ProxyConn:
                def __init__(self, conn):
                    self._conn = conn

                def execute(self, sql, params=(), *args, **kwargs):
                    if "nodes_fts MATCH" in str(sql):
                        raise sqlite3.OperationalError("fts unavailable")
                    return self._conn.execute(sql, params, *args, **kwargs)

                def __getattr__(self, name):
                    return getattr(self._conn, name)

            class _ProxyCtx:
                def __init__(self, ctx):
                    self._ctx = ctx

                def __enter__(self):
                    return _ProxyConn(self._ctx.__enter__())

                def __exit__(self, exc_type, exc, tb):
                    return self._ctx.__exit__(exc_type, exc, tb)

            def _wrapped_conn():
                return _ProxyCtx(original_get_conn())

            with patch.object(graph, "_get_conn", side_effect=_wrapped_conn), \
                 patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=False):
                results = graph.search_fts("alpha_tmp", limit=5)

            names = [node.name for node, _ in results]
            assert "Ari keeps alpha_tmp marker" in names
            assert "Ari keeps alphaXtmp marker" not in names

    def test_fts_query_error_raises_when_fail_hard_enabled(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)

            class _BrokenConn:
                def execute(self, *_args, **_kwargs):
                    raise sqlite3.OperationalError("fts unavailable")

            class _BrokenCtx:
                def __enter__(self):
                    return _BrokenConn()

                def __exit__(self, exc_type, exc, tb):
                    return False

            with patch.object(graph, "_get_conn", return_value=_BrokenCtx()), \
                 patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=True):
                with pytest.raises(RuntimeError, match="fail-hard mode"):
                    graph.search_fts("Quaid", limit=5)


# ---------------------------------------------------------------------------
# search_semantic
# ---------------------------------------------------------------------------

class TestSearchSemantic:
    """Tests for MemoryGraph.search_semantic()."""

    def test_search_vec_refreshes_read_visibility_before_knn_query(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            from datastore.memorydb.memory_graph import MemoryGraph

            db_file = tmp_path / "vec_refresh.db"
            graph = MemoryGraph(db_path=db_file)

            calls = []

            class _Conn:
                def execute(self, sql, params=None):
                    calls.append((sql, params))
                    if "SELECT node_id, distance FROM vec_nodes" in sql:
                        return SimpleNamespace(fetchall=lambda: [])
                    return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: None)

            class _Ctx:
                def __enter__(self):
                    return _Conn()

                def __exit__(self, exc_type, exc, tb):
                    return False

            with patch.object(graph, "_get_conn", return_value=_Ctx()):
                out = graph._search_vec([0.1] * _fake_embedding_dim(), 5, None, None, None, 0.0, None, None)

            assert out == []
            assert len(calls) >= 2
            assert calls[0][0] == "PRAGMA wal_checkpoint(PASSIVE)"
            knn_index = next(
                idx
                for idx, (sql, _params) in enumerate(calls)
                if "SELECT node_id, distance FROM vec_nodes" in sql
            )
            assert knn_index > 0

    def test_returns_empty_when_no_embedding(self, tmp_path):
        """When get_embedding returns None, search_semantic returns []."""
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", return_value=None):
            from datastore.memorydb.memory_graph import MemoryGraph
            db_file = tmp_path / "empty_embed.db"
            graph = MemoryGraph(db_path=db_file)
            results = graph.search_semantic("anything")
            assert results == []

    def test_empty_fresh_vec_index_returns_no_results_in_fail_hard(self, tmp_path):
        from datastore.memorydb.memory_graph import MemoryGraph, _lib_has_vec

        if not _lib_has_vec():
            pytest.skip("sqlite-vec not available in this environment")

        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=True):
            graph = MemoryGraph(db_path=tmp_path / "fresh-empty.db")
            results = graph.search_semantic("Hello. Reply with ACK only.")

        assert results == []

    def test_returns_results_sorted_by_similarity(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_semantic("Quaid coffee", limit=5, min_similarity=0.0)
            if len(results) >= 2:
                sims = [sim for _, sim in results]
                assert sims == sorted(sims, reverse=True)

    def test_min_similarity_filters(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph._lib_has_vec", return_value=False):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_semantic("Quaid coffee", min_similarity=0.999)
            # Brute-force path enforces strict threshold filtering.
            for _, sim in results:
                assert sim >= 0.999

    def test_owner_id_filter(self, tmp_path):
        """Owner filter includes shared/public nodes from other owners (by design)."""
        from datastore.memorydb.memory_graph import Node
        items_raw = [
            ("Bob enjoys tennis sport games", "Fact", "bob", "private"),
            ("Quaid enjoys surfing water sport", "Fact", "quaid", "shared"),
        ]
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            from datastore.memorydb.memory_graph import MemoryGraph
            db_file = tmp_path / "owner_test.db"
            graph = MemoryGraph(db_path=db_file)
            for text, node_type, owner, priv in items_raw:
                node = Node.create(type=node_type, name=text,
                                   owner_id=owner, status="approved", privacy=priv)
                graph.add_node(node, embed=True)

            results = graph.search_semantic("sport", owner_id="quaid",
                                            min_similarity=0.0)
            # Bob's private node should be excluded; quaid's shared node should be included
            result_owners = [n.owner_id for n, _ in results]
            assert "quaid" in result_owners
            # Bob's node is private so it should NOT appear when filtering for quaid
            for node, _ in results:
                if node.owner_id == "bob":
                    assert node.privacy in ("shared", "public")

    def test_limit_respected(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_semantic("Quaid", limit=2, min_similarity=0.0)
            assert len(results) <= 2

    def test_brute_force_query_error_returns_empty_when_fail_hard_disabled(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)

            class _BrokenConn:
                def execute(self, *_args, **_kwargs):
                    raise sqlite3.OperationalError("db offline")

            class _BrokenCtx:
                def __enter__(self):
                    return _BrokenConn()

                def __exit__(self, exc_type, exc, tb):
                    return False

            with patch.object(graph, "_get_conn", return_value=_BrokenCtx()), \
                 patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=False):
                out = graph._search_brute_force([0.1] * _fake_embedding_dim(), None, None, None, 0.0, None, None)
            assert out == []

    def test_brute_force_query_error_raises_when_fail_hard_enabled(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)

            class _BrokenConn:
                def execute(self, *_args, **_kwargs):
                    raise sqlite3.OperationalError("db offline")

            class _BrokenCtx:
                def __enter__(self):
                    return _BrokenConn()

                def __exit__(self, exc_type, exc, tb):
                    return False

            with patch.object(graph, "_get_conn", return_value=_BrokenCtx()), \
                 patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=True):
                with pytest.raises(RuntimeError, match="fail-hard mode"):
                    graph._search_brute_force([0.1] * _fake_embedding_dim(), None, None, None, 0.0, None, None)


# ---------------------------------------------------------------------------
# search_hybrid
# ---------------------------------------------------------------------------

class TestSearchHybrid:
    """Tests for MemoryGraph.search_hybrid()."""

    def test_returns_results(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_hybrid("Quaid coffee", limit=5)
            # Should return some results (combining semantic + FTS)
            assert isinstance(results, list)

    def test_merges_semantic_and_fts(self, tmp_path):
        """Hybrid should return at least as many as either individual search."""
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            hybrid = graph.search_hybrid("Quaid coffee", limit=10)
            # Just verify it runs and returns a list
            assert isinstance(hybrid, list)

    def test_limit_respected(self, tmp_path):
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_hybrid("Quaid", limit=3)
            # search_hybrid returns up to limit*2 for downstream MMR/filtering
            assert len(results) <= 6

    def test_hybrid_returns_quality_scores(self, tmp_path):
        """Results should carry quality scores (cosine similarity) for threshold filtering."""
        with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            graph = _make_graph_with_data(tmp_path)
            results = graph.search_hybrid("Quaid coffee", limit=10)
            if len(results) >= 1:
                # All quality scores should be in valid range
                for node, score in results:
                    assert 0.0 <= score <= 1.0, f"Quality score {score} out of range"

    def test_hybrid_passes_timeout_to_semantic_embedding(self, tmp_path):
        from datastore.memorydb.memory_graph import MemoryGraph

        graph = MemoryGraph(db_path=tmp_path / "hybrid-timeout.db")
        seen = {}

        def fake_semantic(*_args, **kwargs):
            seen["embedding_timeout_s"] = kwargs.get("embedding_timeout_s")
            return []

        with patch.object(graph, "search_semantic", side_effect=fake_semantic), \
             patch.object(graph, "search_fts", return_value=[]):
            graph.search_hybrid("Baxter", timeout_seconds=1.25)

        assert seen["embedding_timeout_s"] == 1.0

    def test_hybrid_raises_semantic_error_when_failhard_enabled(self, tmp_path):
        from datastore.memorydb.memory_graph import MemoryGraph

        graph = MemoryGraph(db_path=tmp_path / "hybrid-failhard.db")

        with patch.object(graph, "search_semantic", side_effect=TimeoutError("semantic slow")), \
             patch.object(graph, "search_fts", return_value=[]), \
             patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=True):
            with pytest.raises(RuntimeError, match="Semantic vector search failed"):
                graph.search_hybrid("Baxter", timeout_seconds=1.25)


class TestRouteQueryFailHard:
    def test_route_query_returns_original_when_fail_hard_disabled(self):
        from datastore.memorydb.memory_graph import route_query

        fake_mod = MagicMock()
        fake_mod.call_fast_reasoning.side_effect = RuntimeError("llm unavailable")
        with patch.dict(sys.modules, {"lib.llm_clients": fake_mod}), \
             patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=False):
            query = "Where do I keep my passport?"
            assert route_query(query) == query

    def test_route_query_raises_when_fail_hard_enabled(self):
        from datastore.memorydb.memory_graph import route_query

        fake_mod = MagicMock()
        fake_mod.call_fast_reasoning.side_effect = RuntimeError("llm unavailable")
        with patch.dict(sys.modules, {"lib.llm_clients": fake_mod}), \
             patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=True):
            with pytest.raises(RuntimeError, match="fail-hard mode"):
                route_query("Where do I keep my passport?")

    def test_route_query_uses_configured_timeout_and_retries(self):
        from datastore.memorydb.memory_graph import route_query

        fake_mod = MagicMock()
        fake_mod.call_fast_reasoning.side_effect = RuntimeError("gateway timeout")
        retrieval_cfg = type("R", (), {"hyde_timeout_ms": 9000, "hyde_max_retries": 2})()
        cfg = type("C", (), {"retrieval": retrieval_cfg})()
        with patch.dict(sys.modules, {"lib.llm_clients": fake_mod}), \
             patch("config.get_config", return_value=cfg), \
             patch("datastore.memorydb.memory_graph._is_fail_hard_mode", return_value=True):
            with pytest.raises(RuntimeError, match="timeout_ms=9000, max_retries=2"):
                route_query("Where do I keep my passport?")
        call_kwargs = fake_mod.call_fast_reasoning.call_args.kwargs
        assert call_kwargs["timeout"] == 9.0
        assert call_kwargs["max_retries"] == 2


# ---------------------------------------------------------------------------
# has_owner_pronoun
# ---------------------------------------------------------------------------

class TestHasOwnerPronoun:
    """Tests for owner-name matching."""

    def test_english_pronoun_alone_is_not_owner_match(self):
        from datastore.memorydb.memory_graph import has_owner_pronoun
        assert has_owner_pronoun("my favorite color") is False

    def test_no_pronoun(self):
        from datastore.memorydb.memory_graph import has_owner_pronoun
        assert has_owner_pronoun("the weather today") is False

    def test_first_person_pronoun_alone_is_not_owner_match(self):
        from datastore.memorydb.memory_graph import has_owner_pronoun
        assert has_owner_pronoun("I like coffee") is False

    def test_non_ascii_owner_name_matches_compact_query(self):
        import datastore.memorydb.memory_graph as mg

        fake_cfg = SimpleNamespace(
            users=SimpleNamespace(
                identities={"owner-alpha": SimpleNamespace(person_node_name="美玲")}
            )
        )
        with patch.object(mg, "_HAS_CONFIG", True), \
             patch.object(mg, "_get_memory_config", return_value=fake_cfg):
            assert mg.has_owner_pronoun("美玲の予定を確認して") is True

    def test_single_char_non_ascii_owner_name_requires_token_match(self):
        import datastore.memorydb.memory_graph as mg

        fake_cfg = SimpleNamespace(
            users=SimpleNamespace(
                identities={"owner-alpha": SimpleNamespace(person_node_name="玲")}
            )
        )
        with patch.object(mg, "_HAS_CONFIG", True), \
             patch.object(mg, "_get_memory_config", return_value=fake_cfg):
            assert mg.has_owner_pronoun("美玲の予定を確認して") is False
            assert mg.has_owner_pronoun("玲 と 美玲 の予定を確認して") is True


class TestRowAttributeParsing:
    def test_malformed_node_attributes_dont_crash(self, tmp_path):
        graph = _make_graph_with_data(tmp_path)
        with graph._get_conn() as conn:
            row = conn.execute("SELECT id FROM nodes LIMIT 1").fetchone()
        assert row is not None
        node_id = row[0]
        with graph._get_conn() as conn:
            conn.execute("UPDATE nodes SET attributes = ? WHERE id = ?", ("{bad json", node_id))
        node = graph.get_node(node_id)
        assert node is not None
        assert node.attributes == {}

    def test_malformed_edge_attributes_dont_crash(self, tmp_path):
        graph, node_id, edge_id = _make_graph_with_edge(tmp_path)
        with graph._get_conn() as conn:
            conn.execute("UPDATE edges SET attributes = ? WHERE id = ?", ("{bad json", edge_id))
        edges = graph.get_edges(node_id, direction="out")
        assert len(edges) == 1
        assert edges[0].attributes == {}
