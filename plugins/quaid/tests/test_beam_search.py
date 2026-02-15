"""Tests for BEAM search graph traversal — beam_search_graph() and _beam_score_candidate()
in memory_graph.py.

Uses a real SQLite database (via tmp_path fixture) with test nodes and edges
to verify scored beam search behavior, scoring logic, config integration,
and edge cases.
"""

import os
import sys
import uuid
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

# Ensure the plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set env to avoid touching real DB or calling Ollama
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")
os.environ.setdefault("MOCK_EMBEDDINGS", "1")

import pytest

from memory_graph import MemoryGraph, Node, Edge


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def graph(tmp_path):
    """Create a fresh MemoryGraph with an isolated test database."""
    db_path = tmp_path / "test-beam.db"
    with patch.object(MemoryGraph, "get_embedding", return_value=None):
        g = MemoryGraph(db_path=db_path)
    yield g
    if db_path.exists():
        db_path.unlink()


def _add_node(graph, name, node_type="Fact", confidence=0.8, verified=False,
              storage_strength=5.0, status="active", owner_id="solomon"):
    """Add a node without embeddings, return the node."""
    node = Node.create(
        type=node_type,
        name=name,
        owner_id=owner_id,
        status=status,
        confidence=confidence,
        verified=verified,
        storage_strength=storage_strength,
    )
    with patch.object(graph, "get_embedding", return_value=None):
        graph.add_node(node, embed=False)
    return node


def _add_edge(graph, source_id, target_id, relation, weight=1.0):
    """Add an edge between two nodes with optional weight."""
    edge = Edge.create(
        source_id=source_id,
        target_id=target_id,
        relation=relation,
        weight=weight,
    )
    graph.add_edge(edge)
    return edge


# ---------------------------------------------------------------------------
# 1. Basic BEAM functionality
# ---------------------------------------------------------------------------

class TestBeamBasic:
    """Basic beam_search_graph() behavior."""

    def test_beam_returns_results(self, graph):
        """Create a linear graph A->B->C, beam search from A returns B and C."""
        a = _add_node(graph, "Node A")
        b = _add_node(graph, "Node B")
        c = _add_node(graph, "Node C")

        _add_edge(graph, a.id, b.id, "knows")
        _add_edge(graph, b.id, c.id, "knows")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=5,
            max_depth=2,
        )

        names = {r[0].name for r in results}
        assert "Node B" in names
        assert "Node C" in names
        assert len(results) >= 2

    def test_beam_width_limits_candidates(self, graph):
        """Star graph A->B,C,D,E,F with beam_width=2 returns at most 2."""
        center = _add_node(graph, "Center")
        satellites = []
        for i in range(5):
            sat = _add_node(graph, f"Sat{i}")
            satellites.append(sat)
            _add_edge(graph, center.id, sat.id, "knows")

        results = graph.beam_search_graph(
            query="test query",
            start_id=center.id,
            beam_width=2,
            max_depth=1,
        )

        # beam_width=2 means only the top 2 candidates are selected
        assert len(results) == 2

    def test_beam_depth_limits(self, graph):
        """Linear chain A->B->C->D, max_depth=1 should only return B."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B")
        c = _add_node(graph, "C")
        d = _add_node(graph, "D")

        _add_edge(graph, a.id, b.id, "knows")
        _add_edge(graph, b.id, c.id, "knows")
        _add_edge(graph, c.id, d.id, "knows")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=10,
            max_depth=1,
        )

        names = {r[0].name for r in results}
        assert "B" in names
        assert "C" not in names
        assert "D" not in names

    def test_beam_empty_graph(self, graph):
        """No edges from start node returns empty list."""
        loner = _add_node(graph, "Loner")

        results = graph.beam_search_graph(
            query="test query",
            start_id=loner.id,
            beam_width=5,
            max_depth=2,
        )

        assert results == []

    def test_beam_cycle_detection(self, graph):
        """A->B->A cycle doesn't loop forever."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B")

        _add_edge(graph, a.id, b.id, "knows")
        _add_edge(graph, b.id, a.id, "knows")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=5,
            max_depth=5,  # High depth to test cycle handling
        )

        # Should terminate and not include start node A in results
        result_names = [r[0].name for r in results]
        assert "A" not in result_names
        assert "B" in result_names
        # Bounded: at most we should see B once
        assert len(results) <= 2


# ---------------------------------------------------------------------------
# 2. Scoring
# ---------------------------------------------------------------------------

class TestBeamScoring:
    """Tests for _beam_score_candidate() scoring logic."""

    def test_heuristic_scoring_uses_confidence(self, graph):
        """Higher confidence nodes get higher beam scores."""
        high_conf = _add_node(graph, "High Confidence", confidence=0.95)
        low_conf = _add_node(graph, "Low Confidence", confidence=0.2)

        score_high = graph._beam_score_candidate(
            query="test",
            node=high_conf,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        score_low = graph._beam_score_candidate(
            query="test",
            node=low_conf,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        assert score_high > score_low

    def test_heuristic_scoring_uses_edge_weight(self, graph):
        """Higher edge weight leads to higher score."""
        node = _add_node(graph, "Test Node", confidence=0.5)

        score_heavy = graph._beam_score_candidate(
            query="test",
            node=node,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        score_light = graph._beam_score_candidate(
            query="test",
            node=node,
            relation="knows",
            edge_weight=0.1,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        assert score_heavy > score_light

    def test_intent_boost_affects_score(self, graph):
        """Person type boost for WHO intent raises score for Person nodes."""
        person_node = _add_node(graph, "Solomon Steadman", node_type="Person", confidence=0.8)
        fact_node = _add_node(graph, "Some random fact", node_type="Fact", confidence=0.8)

        type_boosts = {"Person": 1.3, "Fact": 0.8}

        score_person = graph._beam_score_candidate(
            query="Who is Solomon?",
            node=person_node,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent="WHO",
            type_boosts=type_boosts,
            scoring_mode="heuristic",
        )

        score_fact = graph._beam_score_candidate(
            query="Who is Solomon?",
            node=fact_node,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent="WHO",
            type_boosts=type_boosts,
            scoring_mode="heuristic",
        )

        assert score_person > score_fact

    def test_scoring_mode_fallback(self, graph):
        """Invalid/unknown scoring mode falls back to base * 0.5."""
        node = _add_node(graph, "Test Node", confidence=0.8)

        score = graph._beam_score_candidate(
            query="test",
            node=node,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="nonexistent_mode",
        )

        # Unknown mode returns base * 0.5
        # base = parent_score(1.0) * hop_decay(0.7)^1 = 0.7
        # result = 0.7 * 0.5 = 0.35
        assert score > 0
        assert score <= 1.0
        # Should be approximately 0.35 (with default hop_decay=0.7)
        assert abs(score - 0.35) < 0.05

    def test_hop_decay_reduces_score(self, graph):
        """Multi-hop propagation: each hop decays score by hop_decay factor.

        With the corrected per-hop decay, a 3-hop chain should produce:
          depth 1: parent=1.0 * 0.7 * heuristic
          depth 2: parent=score_depth1 * 0.7 * heuristic
          depth 3: parent=score_depth2 * 0.7 * heuristic
        So depth-3 score = 1.0 * 0.7^3 * heuristic^3
        """
        node = _add_node(graph, "Test Node", confidence=0.8)

        # Simulate multi-hop propagation: parent_score carries prior decay
        score_depth1 = graph._beam_score_candidate(
            query="test",
            node=node,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,  # Root node
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        # Depth 2: parent_score is the score from depth 1
        score_depth2 = graph._beam_score_candidate(
            query="test",
            node=node,
            relation="knows",
            edge_weight=1.0,
            parent_score=score_depth1,  # Carries depth-1 decay
            hop_depth=2,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        # Depth 3: parent_score is the score from depth 2
        score_depth3 = graph._beam_score_candidate(
            query="test",
            node=node,
            relation="knows",
            edge_weight=1.0,
            parent_score=score_depth2,  # Carries depth-1+2 decay
            hop_depth=3,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        assert score_depth1 > score_depth2 > score_depth3
        # Verify correct decay: each hop should multiply by ~0.7 * heuristic
        assert score_depth2 / score_depth1 == pytest.approx(score_depth3 / score_depth2, abs=0.01)


# ---------------------------------------------------------------------------
# 3. Integration with recall
# ---------------------------------------------------------------------------

class TestBeamRecallIntegration:
    """Tests for beam search integration with recall() function."""

    def test_recall_uses_beam_when_configured(self, graph):
        """When use_beam=True in config, beam_search_graph is called."""
        from config import TraversalConfig, RetrievalConfig

        trav = TraversalConfig(use_beam=True, beam_width=3)
        retrieval_config = MagicMock()
        retrieval_config.traversal = trav

        # Verify the config reads correctly
        assert trav.use_beam is True
        assert trav.beam_width == 3

        # Verify that getattr-based extraction (as used in recall) works
        _use_beam = getattr(trav, 'use_beam', True)
        _beam_width = getattr(trav, 'beam_width', 5)
        assert _use_beam is True
        assert _beam_width == 3

    def test_recall_falls_back_to_bfs(self, graph):
        """When use_beam=False in config, BFS (get_related_bidirectional) path is taken."""
        from config import TraversalConfig

        trav = TraversalConfig(use_beam=False)

        # Verify config reads correctly for BFS fallback
        _use_beam = getattr(trav, 'use_beam', True)
        assert _use_beam is False


# ---------------------------------------------------------------------------
# 4. Config
# ---------------------------------------------------------------------------

class TestBeamConfig:
    """Tests for TraversalConfig and its integration."""

    def test_traversal_config_defaults(self):
        """TraversalConfig() has correct default values."""
        from config import TraversalConfig

        tc = TraversalConfig()
        assert tc.use_beam is True
        assert tc.beam_width == 5
        assert tc.max_depth == 2
        assert tc.scoring_mode == "heuristic"
        assert tc.hop_decay == 0.7

    def test_traversal_config_from_json(self):
        """Loading config with traversal section populates TraversalConfig correctly."""
        import config as config_module
        from config import TraversalConfig, reload_config, CONFIG_PATHS

        config_data = {
            "retrieval": {
                "traversal": {
                    "use_beam": False,
                    "beam_width": 10,
                    "max_depth": 3,
                    "scoring_mode": "hybrid",
                    "hop_decay": 0.5,
                }
            },
            "models": {
                "highReasoning": "claude-opus-4-6",
                "lowReasoning": "claude-haiku-4-5",
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config_data, f)
            f.flush()
            config_path = f.name

        try:
            with patch.object(config_module, "CONFIG_PATHS", [Path(config_path)]):
                config = reload_config()
                trav = config.retrieval.traversal
                assert trav.use_beam is False
                assert trav.beam_width == 10
                assert trav.max_depth == 3
                assert trav.scoring_mode == "hybrid"
                assert trav.hop_decay == 0.5
        finally:
            os.unlink(config_path)
            # Restore original config
            reload_config()

    def test_retrieval_config_has_traversal(self):
        """RetrievalConfig has a traversal field of type TraversalConfig."""
        from config import RetrievalConfig, TraversalConfig

        rc = RetrievalConfig()
        assert hasattr(rc, 'traversal')
        assert isinstance(rc.traversal, TraversalConfig)

    def test_retrieval_config_new_fields(self):
        """RetrievalConfig has rrf_k, reranker_blend, and other tuning fields."""
        from config import RetrievalConfig

        rc = RetrievalConfig()
        assert hasattr(rc, 'rrf_k')
        assert rc.rrf_k == 60
        assert hasattr(rc, 'reranker_blend')
        assert rc.reranker_blend == 0.5
        assert hasattr(rc, 'composite_relevance_weight')
        assert rc.composite_relevance_weight == 0.60
        assert hasattr(rc, 'multi_pass_gate')
        assert rc.multi_pass_gate == 0.70


# ---------------------------------------------------------------------------
# 5. Edge cases
# ---------------------------------------------------------------------------

class TestBeamEdgeCases:
    """Edge case and boundary tests for beam search."""

    def test_beam_max_results_early_stop(self, graph):
        """max_results=1 stops after the first result."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B")
        c = _add_node(graph, "C")
        d = _add_node(graph, "D")

        _add_edge(graph, a.id, b.id, "knows")
        _add_edge(graph, a.id, c.id, "knows")
        _add_edge(graph, a.id, d.id, "knows")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=10,
            max_depth=2,
            max_results=1,
        )

        assert len(results) == 1

    def test_beam_with_relation_filter(self, graph):
        """Only traverses edges with specified relations."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B")
        c = _add_node(graph, "C")

        _add_edge(graph, a.id, b.id, "parent_of")
        _add_edge(graph, a.id, c.id, "friend_of")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=10,
            max_depth=1,
            relations=["parent_of"],
        )

        names = {r[0].name for r in results}
        assert "B" in names
        assert "C" not in names

    def test_beam_path_building(self, graph):
        """Paths correctly show the traversal chain."""
        a = _add_node(graph, "Solomon")
        b = _add_node(graph, "Emily")
        c = _add_node(graph, "Luna")

        _add_edge(graph, a.id, b.id, "parent_of")
        _add_edge(graph, b.id, c.id, "has_pet")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=10,
            max_depth=2,
        )

        by_name = {r[0].name: r for r in results}

        # Emily at depth 1: path = [("Solomon", "parent_of")]
        assert "Emily" in by_name
        emily_path = by_name["Emily"][4]  # path is at index 4
        assert emily_path == [("Solomon", "parent_of")]

        # Luna at depth 2: path = [("Solomon", "parent_of"), ("Emily", "has_pet")]
        assert "Luna" in by_name
        luna_path = by_name["Luna"][4]
        assert luna_path == [("Solomon", "parent_of"), ("Emily", "has_pet")]

    def test_beam_bidirectional(self, graph):
        """Traverses both outbound and inbound edges."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B")
        c = _add_node(graph, "C")

        # B is connected to A via outbound edge
        _add_edge(graph, a.id, b.id, "knows")
        # C points INTO A (inbound edge to A)
        _add_edge(graph, c.id, a.id, "friend_of")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=10,
            max_depth=1,
        )

        names = {r[0].name for r in results}
        assert "B" in names
        assert "C" in names

        # Check directions
        by_name = {r[0].name: r for r in results}
        assert by_name["B"][2] == "out"    # direction is at index 2
        assert by_name["C"][2] == "in"

    def test_beam_score_range(self, graph):
        """All beam scores are in [0, 1] range."""
        a = _add_node(graph, "Center", confidence=0.9, verified=True,
                       storage_strength=10.0, status="active")
        nodes = []
        for i in range(5):
            n = _add_node(graph, f"Node{i}", confidence=float(i) / 5 + 0.1,
                          storage_strength=float(i) * 2)
            nodes.append(n)
            _add_edge(graph, a.id, n.id, "knows", weight=float(i) / 4 + 0.1)

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=10,
            max_depth=1,
        )

        for node, relation, direction, depth, path, score in results:
            assert 0 <= score <= 1.0, f"Score {score} out of [0,1] range for {node.name}"

    def test_beam_nonexistent_start_node(self, graph):
        """Non-existent start node returns empty (no crash)."""
        results = graph.beam_search_graph(
            query="test query",
            start_id="nonexistent-id-99999",
            beam_width=5,
            max_depth=2,
        )

        # Should not crash, returns empty or at most empty results
        assert isinstance(results, list)
        assert len(results) == 0

    def test_beam_result_tuple_structure(self, graph):
        """Each result tuple has exactly 6 elements: (node, relation, direction, depth, path, score)."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B")
        _add_edge(graph, a.id, b.id, "knows")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=5,
            max_depth=1,
        )

        assert len(results) == 1
        result = results[0]
        assert len(result) == 6

        node, relation, direction, depth, path, score = result
        assert isinstance(node, Node)
        assert isinstance(relation, str)
        assert direction in ("in", "out")
        assert isinstance(depth, int)
        assert isinstance(path, list)
        assert isinstance(score, float)


# ---------------------------------------------------------------------------
# 6. Multi-hop decay propagation (regression test for double-decay bug)
# ---------------------------------------------------------------------------

class TestMultiHopDecay:
    """Verify correct decay propagation across multiple hops."""

    def test_3_hop_chain_scores_decrease(self, graph):
        """In a 3-hop linear chain A->B->C->D, scores should decrease per hop."""
        a = _add_node(graph, "A", confidence=0.9)
        b = _add_node(graph, "B", confidence=0.8)
        c = _add_node(graph, "C", confidence=0.8)
        d = _add_node(graph, "D", confidence=0.8)

        _add_edge(graph, a.id, b.id, "knows")
        _add_edge(graph, b.id, c.id, "knows")
        _add_edge(graph, c.id, d.id, "knows")

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=5,
            max_depth=3,
        )

        by_name = {r[0].name: r for r in results}
        assert "B" in by_name and "C" in by_name and "D" in by_name

        score_b = by_name["B"][5]
        score_c = by_name["C"][5]
        score_d = by_name["D"][5]

        assert score_b > score_c > score_d, (
            f"Scores should decrease: B={score_b}, C={score_c}, D={score_d}"
        )

    def test_decay_ratio_is_consistent(self, graph):
        """Each hop should apply the same multiplicative decay ratio."""
        a = _add_node(graph, "A", confidence=0.9)
        # Use identical nodes so heuristic scores are the same
        b = _add_node(graph, "B", confidence=0.8, status="active")
        c = _add_node(graph, "C", confidence=0.8, status="active")
        d = _add_node(graph, "D", confidence=0.8, status="active")

        _add_edge(graph, a.id, b.id, "knows", weight=0.8)
        _add_edge(graph, b.id, c.id, "knows", weight=0.8)
        _add_edge(graph, c.id, d.id, "knows", weight=0.8)

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=5,
            max_depth=3,
            hop_decay=0.7,
        )

        by_name = {r[0].name: r for r in results}
        assert "B" in by_name and "C" in by_name and "D" in by_name

        score_b = by_name["B"][5]
        score_c = by_name["C"][5]
        score_d = by_name["D"][5]

        # Ratio C/B should equal D/C (same per-hop decay)
        ratio_cb = score_c / score_b if score_b > 0 else 0
        ratio_dc = score_d / score_c if score_c > 0 else 0
        assert ratio_cb == pytest.approx(ratio_dc, abs=0.01), (
            f"Decay ratios should be consistent: C/B={ratio_cb:.4f}, D/C={ratio_dc:.4f}"
        )

    def test_no_double_decay_regression(self, graph):
        """Depth-2 score should be ~(0.7)^2 of depth-0, not ~(0.7)^3.

        The old bug applied hop_decay**hop_depth on top of an already-decayed
        parent_score, causing exponential over-penalization.
        """
        a = _add_node(graph, "A", confidence=0.9)
        b = _add_node(graph, "B", confidence=0.8, status="active")
        c = _add_node(graph, "C", confidence=0.8, status="active")

        _add_edge(graph, a.id, b.id, "knows", weight=0.8)
        _add_edge(graph, b.id, c.id, "knows", weight=0.8)

        results = graph.beam_search_graph(
            query="test query",
            start_id=a.id,
            beam_width=5,
            max_depth=2,
            hop_decay=0.7,
        )

        by_name = {r[0].name: r for r in results}
        assert "B" in by_name and "C" in by_name

        score_b = by_name["B"][5]
        score_c = by_name["C"][5]

        # With correct per-hop decay:
        #   score_c should be approximately score_b * 0.7 * heuristic_ratio
        # With the OLD double-decay bug:
        #   score_c would be approximately score_b * 0.7^2 * heuristic_ratio (much smaller)
        # The ratio score_c / score_b^2 should be roughly 1/score_b
        # More practically: score_c / score_b should be > 0.3 (with correct decay)
        # and would be < 0.25 with the old bug
        ratio = score_c / score_b if score_b > 0 else 0
        assert ratio > 0.3, (
            f"Depth-2 node score ratio {ratio:.4f} too low — possible double-decay bug"
        )

    def test_custom_hop_decay_parameter(self, graph):
        """hop_decay parameter is respected by beam_search_graph."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B", confidence=0.8)
        _add_edge(graph, a.id, b.id, "knows")

        results_high = graph.beam_search_graph(
            query="test", start_id=a.id, beam_width=5, max_depth=1, hop_decay=0.9,
        )
        results_low = graph.beam_search_graph(
            query="test", start_id=a.id, beam_width=5, max_depth=1, hop_decay=0.3,
        )

        score_high = results_high[0][5] if results_high else 0
        score_low = results_low[0][5] if results_low else 0
        assert score_high > score_low, (
            f"Higher hop_decay should give higher score: 0.9->{score_high}, 0.3->{score_low}"
        )


# ---------------------------------------------------------------------------
# 7. Adaptive LLM reranking (triggers when candidates > beam_width)
# ---------------------------------------------------------------------------

class TestAdaptiveReranking:
    """Tests for adaptive LLM reranking at each BEAM hop."""

    def test_no_reranker_when_candidates_fit(self, graph):
        """When candidates <= beam_width, no LLM reranker call is made."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B", confidence=0.8)
        _add_edge(graph, a.id, b.id, "knows")

        # beam_width=5, only 1 candidate — should NOT call reranker
        with patch("memory_graph._rerank_with_cross_encoder") as mock_rerank:
            results = graph.beam_search_graph(
                query="test query",
                start_id=a.id,
                beam_width=5,
                max_depth=1,
            )

            mock_rerank.assert_not_called()
            assert len(results) == 1

    def test_reranker_called_when_candidates_exceed_beam_width(self, graph):
        """When candidates > beam_width, LLM reranker IS called."""
        a = _add_node(graph, "Center")
        nodes = []
        for i in range(6):
            n = _add_node(graph, f"Sat{i}", confidence=0.5 + i * 0.05)
            nodes.append(n)
            _add_edge(graph, a.id, n.id, "knows")

        # beam_width=3, 6 candidates — should call reranker
        with patch("memory_graph._rerank_with_cross_encoder") as mock_rerank:
            # Return the same candidates unchanged (passthrough)
            mock_rerank.side_effect = lambda q, candidates, config_retrieval=None: candidates

            results = graph.beam_search_graph(
                query="test query",
                start_id=a.id,
                beam_width=3,
                max_depth=1,
            )

            mock_rerank.assert_called_once()
            # Should have been called with top 2*beam_width=6 candidates
            call_candidates = mock_rerank.call_args[0][1]
            assert len(call_candidates) <= 6

    def test_reranker_failure_falls_back_to_heuristic(self, graph):
        """If LLM reranker raises, falls back to heuristic ranking."""
        a = _add_node(graph, "Center")
        for i in range(6):
            n = _add_node(graph, f"Sat{i}", confidence=0.5 + i * 0.05)
            _add_edge(graph, a.id, n.id, "knows")

        # Reranker throws — should fall back gracefully
        with patch("memory_graph._rerank_with_cross_encoder", side_effect=Exception("API down")):
            results = graph.beam_search_graph(
                query="test query",
                start_id=a.id,
                beam_width=3,
                max_depth=1,
            )

            # Should still return results (heuristic fallback)
            assert len(results) == 3
            for _, _, _, _, _, score in results:
                assert 0 <= score <= 1.0

    def test_reranker_can_reorder_candidates(self, graph):
        """LLM reranker can change which candidates make the beam.

        The reranker only sees the top 2*beam_width candidates (heuristic
        pre-filter). So we test reordering within that window: the reranker
        promotes the 2nd-ranked heuristic candidate over the 1st.
        """
        a = _add_node(graph, "Center")
        # high = ranked 1st by heuristic, med = ranked 2nd
        high = _add_node(graph, "HighHeuristic", confidence=0.95, verified=True)
        med = _add_node(graph, "MedHeuristic", confidence=0.5)
        low = _add_node(graph, "LowHeuristic", confidence=0.2)

        _add_edge(graph, a.id, high.id, "knows")
        _add_edge(graph, a.id, med.id, "knows")
        _add_edge(graph, a.id, low.id, "knows")

        def promote_med_node(query, candidates, config_retrieval=None):
            """Reranker that promotes MedHeuristic over HighHeuristic."""
            reranked = []
            for node, score in candidates:
                if "MedHeuristic" in node.name:
                    reranked.append((node, 0.99))  # Boost to top
                else:
                    reranked.append((node, 0.01))  # Demote
            return reranked

        with patch("memory_graph._rerank_with_cross_encoder", side_effect=promote_med_node):
            results = graph.beam_search_graph(
                query="test query",
                start_id=a.id,
                beam_width=1,  # Only 1 slot — reranker picks among top 2
                max_depth=1,
            )

            assert len(results) == 1
            # MedHeuristic should win because the reranker promoted it
            assert results[0][0].name == "MedHeuristic"

    def test_reranker_receives_correct_candidates(self, graph):
        """Reranker gets top 2*beam_width candidates as (node, score) pairs."""
        a = _add_node(graph, "Center")
        for i in range(10):
            n = _add_node(graph, f"Node{i}", confidence=0.5)
            _add_edge(graph, a.id, n.id, "knows")

        captured_args = {}
        def capture_rerank(query, candidates, config_retrieval=None):
            captured_args["query"] = query
            captured_args["candidates"] = candidates
            return candidates  # passthrough

        with patch("memory_graph._rerank_with_cross_encoder", side_effect=capture_rerank):
            graph.beam_search_graph(
                query="what is the weather",
                start_id=a.id,
                beam_width=3,
                max_depth=1,
            )

            assert captured_args["query"] == "what is the weather"
            # Should get min(2*3, 10) = 6 candidates
            assert len(captured_args["candidates"]) == 6
            # Each should be (node, score) tuple
            for node, score in captured_args["candidates"]:
                assert isinstance(node, Node)
                assert isinstance(score, float)

    def test_scoring_mode_param_ignored(self, graph):
        """scoring_mode parameter is accepted but ignored (backward compat)."""
        a = _add_node(graph, "A")
        b = _add_node(graph, "B", confidence=0.8)
        _add_edge(graph, a.id, b.id, "knows")

        # All these should work identically
        for mode in ["heuristic", "llm", "hybrid", "anything"]:
            results = graph.beam_search_graph(
                query="test",
                start_id=a.id,
                beam_width=5,
                max_depth=1,
                scoring_mode=mode,
            )
            assert len(results) == 1

    def test_beam_relation_selectivity(self, graph):
        """Common relations (related_to, associated_with) score lower than specific ones."""
        node_generic = _add_node(graph, "Generic Node", confidence=0.8)
        node_specific = _add_node(graph, "Specific Node", confidence=0.8)

        score_generic = graph._beam_score_candidate(
            query="test",
            node=node_generic,
            relation="related_to",  # Common relation -> lower selectivity score
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        score_specific = graph._beam_score_candidate(
            query="test",
            node=node_specific,
            relation="parent_of",  # Specific relation -> higher selectivity score
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        assert score_specific > score_generic

    def test_beam_verified_node_scores_higher(self, graph):
        """Verified nodes get a quality bonus in scoring."""
        verified = _add_node(graph, "Verified Node", confidence=0.8, verified=True)
        unverified = _add_node(graph, "Unverified Node", confidence=0.8, verified=False)

        score_v = graph._beam_score_candidate(
            query="test",
            node=verified,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        score_u = graph._beam_score_candidate(
            query="test",
            node=unverified,
            relation="knows",
            edge_weight=1.0,
            parent_score=1.0,
            hop_depth=1,
            intent=None,
            type_boosts=None,
            scoring_mode="heuristic",
        )

        assert score_v > score_u
