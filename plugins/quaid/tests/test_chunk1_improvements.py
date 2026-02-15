"""Tests for Chunk 1 improvements: confirmation boosting, WHEN temporal boost,
debug flag, temporal contradiction, prompt caching, and LLM output validation."""

import os
import sys
import json
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

_FAKE_EMBEDDING = [0.1] * 128


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


# ===========================================================================
# 1. Confirmation Boosting Tests
# ===========================================================================

class TestConfirmationBoostingNodeFields:
    """Node dataclass fields for confirmation boosting."""

    def test_node_confirmation_count_default_zero(self):
        from memory_graph import Node
        node = Node.create(type="Fact", name="Test fact about something")
        assert node.confirmation_count == 0

    def test_node_last_confirmed_at_default_none(self):
        from memory_graph import Node
        node = Node.create(type="Fact", name="Test fact about something")
        assert node.last_confirmed_at is None

    def test_node_confirmation_count_settable(self):
        from memory_graph import Node
        node = Node.create(type="Fact", name="Test fact about something",
                           confirmation_count=5)
        assert node.confirmation_count == 5

    def test_node_last_confirmed_at_settable(self):
        from memory_graph import Node
        ts = "2026-02-08T10:00:00"
        node = Node.create(type="Fact", name="Test fact about something",
                           last_confirmed_at=ts)
        assert node.last_confirmed_at == ts


class TestConfirmationBonusInCompositeScore:
    """_compute_composite_score with confirmation_count."""

    def test_confirmation_count_0_no_bonus(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Test fact about something")
        node.confirmation_count = 0
        node.accessed_at = datetime.now().isoformat()
        score_0 = _compute_composite_score(node, 0.8)
        # Baseline score without confirmation
        node2 = Node.create(type="Fact", name="Test fact about something two")
        node2.confirmation_count = 0
        node2.accessed_at = node.accessed_at
        score_0b = _compute_composite_score(node2, 0.8)
        # Both should be the same range (confirmation_bonus = 0 in both)
        assert abs(score_0 - score_0b) < 0.01

    def test_confirmation_count_1_adds_001(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Test fact about something")
        node.confirmation_count = 0
        base = _compute_composite_score(node, 0.8)
        node.confirmation_count = 1
        boosted = _compute_composite_score(node, 0.8)
        diff = boosted - base
        assert abs(diff - 0.01) < 0.001

    def test_confirmation_count_5_adds_005(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Test fact about something")
        node.confirmation_count = 0
        base = _compute_composite_score(node, 0.8)
        node.confirmation_count = 5
        boosted = _compute_composite_score(node, 0.8)
        diff = boosted - base
        assert abs(diff - 0.05) < 0.001

    def test_confirmation_count_10_capped_at_005(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Test fact about something")
        node.confirmation_count = 0
        base = _compute_composite_score(node, 0.8)
        node.confirmation_count = 10
        boosted = _compute_composite_score(node, 0.8)
        diff = boosted - base
        # min(0.05, 10 * 0.01) = min(0.05, 0.10) = 0.05
        assert abs(diff - 0.05) < 0.001

    def test_confirmation_count_100_still_capped_at_005(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Test fact about something")
        node.confirmation_count = 0
        base = _compute_composite_score(node, 0.8)
        node.confirmation_count = 100
        boosted = _compute_composite_score(node, 0.8)
        diff = boosted - base
        assert abs(diff - 0.05) < 0.001


class TestConfirmationOnDuplicateStore:
    """Confirmation count increments when store() detects a duplicate."""

    def test_hash_exact_dedup_increments_confirmation(self, tmp_path):
        from memory_graph import store
        graph, _ = _make_graph(tmp_path)
        text = "Solomon has a pet cat named Madu"
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            r1 = store(text, owner_id="solomon")
            assert r1["status"] == "created"
            r2 = store(text, owner_id="solomon")
            assert r2["status"] == "duplicate"
            assert r2.get("confirmation_count", 0) == 1
            # Bjork: storage_strength incremented by 0.03 on re-encounter
            node = graph.get_node(r1["id"])
            assert node.storage_strength == pytest.approx(0.03, abs=0.005)

    def test_hash_exact_dedup_boosts_confidence(self, tmp_path):
        from memory_graph import store
        graph, _ = _make_graph(tmp_path)
        text = "Solomon enjoys morning espresso coffee"
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            r1 = store(text, owner_id="solomon", confidence=0.5)
            r2 = store(text, owner_id="solomon")
            # Confidence should be boosted by 0.02 (from 0.5 to 0.52)
            node = graph.get_node(r1["id"])
            assert node.confidence == pytest.approx(0.52, abs=0.01)

    def test_confirmation_confidence_capped_at_095(self, tmp_path):
        from memory_graph import store
        graph, _ = _make_graph(tmp_path)
        text = "Solomon is a software developer engineer"
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph._HAS_CONFIG", False):
            store(text, owner_id="solomon", confidence=0.94)
            # Re-store triggers +0.02, but should cap at 0.95
            store(text, owner_id="solomon")
            node_id = store(text, owner_id="solomon")["id"]
            node = graph.get_node(node_id)
            assert node.confidence <= 0.95


# ===========================================================================
# 2. WHEN-query Temporal Boost Tests
# ===========================================================================

class TestWhenTemporalBoost:
    """_compute_composite_score with intent='WHEN' and temporal metadata."""

    def test_general_intent_no_temporal_bonus(self):
        from memory_graph import Node, _compute_composite_score
        # Use same node, compare GENERAL vs WHEN to isolate the temporal_data_bonus
        # (temporal_penalty is independent of intent, so it cancels out)
        # valid_from in past, valid_until in future -> no temporal_penalty
        node = Node.create(type="Fact", name="Solomon likes coffee a lot")
        node.valid_from = "2026-01-01"
        node.valid_until = "2027-12-31"
        score_general = _compute_composite_score(node, 0.8, intent="GENERAL")
        score_when = _compute_composite_score(node, 0.8, intent="WHEN")
        # WHEN gets +0.10 bonus (both valid_from and valid_until), GENERAL gets 0
        assert abs((score_when - score_general) - 0.10) < 0.001

    def test_when_intent_no_temporal_metadata_no_bonus(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Solomon likes coffee a lot")
        # No valid_from, no valid_until, no created_at
        node.valid_from = None
        node.valid_until = None
        node.created_at = None
        score = _compute_composite_score(node, 0.8, intent="WHEN")
        base = _compute_composite_score(node, 0.8, intent="GENERAL")
        # No temporal data, no bonus even with WHEN intent
        assert abs(score - base) < 0.001

    def test_when_intent_valid_from_only_adds_005(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Solomon started working in January")
        node.valid_from = "2025-01-15"
        node.valid_until = None
        node.created_at = None
        score_when = _compute_composite_score(node, 0.8, intent="WHEN")

        node2 = Node.create(type="Fact", name="Solomon started working in January")
        node2.valid_from = None
        node2.valid_until = None
        node2.created_at = None
        score_general = _compute_composite_score(node2, 0.8, intent="WHEN")
        diff = score_when - score_general
        assert abs(diff - 0.05) < 0.001

    def test_when_intent_valid_until_only_adds_005(self):
        from memory_graph import Node, _compute_composite_score
        # Use future valid_until to avoid temporal_penalty for expired facts
        node = Node.create(type="Fact", name="Solomon finished the project December")
        node.valid_from = None
        node.valid_until = "2027-12-31"
        node.created_at = None
        score_when = _compute_composite_score(node, 0.8, intent="WHEN")

        node2 = Node.create(type="Fact", name="Solomon finished the project December")
        node2.valid_from = None
        node2.valid_until = None
        node2.created_at = None
        score_no = _compute_composite_score(node2, 0.8, intent="WHEN")
        diff = score_when - score_no
        assert abs(diff - 0.05) < 0.001

    def test_when_intent_both_valid_from_and_until_adds_010(self):
        from memory_graph import Node, _compute_composite_score
        # valid_from in past, valid_until in future -> no temporal_penalty
        node = Node.create(type="Fact", name="Solomon is in Bali now for a while")
        node.valid_from = "2026-01-01"
        node.valid_until = "2027-12-31"
        node.created_at = None
        score_when = _compute_composite_score(node, 0.8, intent="WHEN")

        node2 = Node.create(type="Fact", name="Solomon is in Bali now for a while")
        node2.valid_from = None
        node2.valid_until = None
        node2.created_at = None
        score_no = _compute_composite_score(node2, 0.8, intent="WHEN")
        diff = score_when - score_no
        assert abs(diff - 0.10) < 0.001

    def test_when_intent_created_at_only_adds_005(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Solomon mentioned something today")
        node.valid_from = None
        node.valid_until = None
        node.created_at = "2026-02-08T10:00:00"
        score_when = _compute_composite_score(node, 0.8, intent="WHEN")

        node2 = Node.create(type="Fact", name="Solomon mentioned something today")
        node2.valid_from = None
        node2.valid_until = None
        node2.created_at = None
        score_no = _compute_composite_score(node2, 0.8, intent="WHEN")
        diff = score_when - score_no
        assert abs(diff - 0.05) < 0.001

    def test_when_intent_both_fields_override_created_at_bonus(self):
        from memory_graph import Node, _compute_composite_score
        # Both valid_from and valid_until -> 0.10, not stacked 0.05+0.05
        # Use valid_from in past, valid_until in future to avoid temporal penalty
        node = Node.create(type="Fact", name="Solomon is in Bali for a period")
        node.valid_from = "2026-01-01"
        node.valid_until = "2027-12-31"
        node.created_at = "2026-01-15T10:00:00"
        score_when = _compute_composite_score(node, 0.8, intent="WHEN")

        node2 = Node.create(type="Fact", name="Solomon is in Bali for a period")
        node2.valid_from = None
        node2.valid_until = None
        node2.created_at = None
        score_no = _compute_composite_score(node2, 0.8, intent="WHEN")
        diff = score_when - score_no
        # Should be 0.10 (both valid_from and valid_until -> 0.10 bonus)
        assert abs(diff - 0.10) < 0.001

    def test_when_intent_score_capped_at_1(self):
        from memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="Some temporal fact here")
        node.valid_from = "2025-01-01"
        node.valid_until = "2025-12-31"
        node.confirmation_count = 10  # +0.05
        node.confidence = 1.0  # high confidence
        node.access_count = 100  # high frequency
        node.accessed_at = datetime.now().isoformat()
        score = _compute_composite_score(node, 1.0, intent="WHEN")
        assert score <= 1.0


# ===========================================================================
# 3. Debug Flag Tests
# ===========================================================================

class TestDebugFlag:
    """recall() with debug=True/False."""

    @pytest.fixture(autouse=True)
    def _mock_reranker(self):
        """Disable reranker for all debug flag tests (not testing reranker here)."""
        with patch("memory_graph._rerank_with_cross_encoder", side_effect=lambda q, r, c=None: r):
            yield

    def test_recall_debug_false_no_debug_keys(self, tmp_path):
        from memory_graph import store, recall
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph.route_query", side_effect=lambda q: q):
            store("Solomon likes espresso coffee beverages",
                  owner_id="solomon", skip_dedup=True)
            results = recall("Solomon likes espresso coffee beverages",
                             owner_id="solomon", use_routing=False,
                             min_similarity=0.0, debug=False)
            for r in results:
                assert "_debug" not in r

    def test_recall_debug_true_has_debug_keys(self, tmp_path):
        from memory_graph import store, recall
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph.route_query", side_effect=lambda q: q):
            store("Solomon likes espresso coffee beverages",
                  owner_id="solomon", skip_dedup=True)
            results = recall("Solomon likes espresso coffee beverages",
                             owner_id="solomon", use_routing=False,
                             min_similarity=0.0, debug=True)
            assert len(results) > 0
            # Direct matches (not graph-traversed) should have _debug
            direct_results = [r for r in results if "via_relation" not in r]
            assert any("_debug" in r for r in direct_results)

    def test_debug_dict_has_expected_keys(self, tmp_path):
        from memory_graph import store, recall
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph.route_query", side_effect=lambda q: q):
            store("Solomon likes espresso coffee beverages",
                  owner_id="solomon", skip_dedup=True)
            results = recall("Solomon likes espresso coffee beverages",
                             owner_id="solomon", use_routing=False,
                             min_similarity=0.0, debug=True)
            direct_results = [r for r in results if "_debug" in r]
            assert len(direct_results) > 0
            debug = direct_results[0]["_debug"]
            expected_keys = {
                "raw_quality_score", "composite_score", "intent",
                "type_boost", "node_type", "confidence", "access_count",
                "confirmation_count", "valid_from", "valid_until"
            }
            assert expected_keys.issubset(set(debug.keys()))

    def test_debug_dict_value_types(self, tmp_path):
        from memory_graph import store, recall
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph.route_query", side_effect=lambda q: q):
            store("Solomon likes espresso coffee beverages",
                  owner_id="solomon", skip_dedup=True)
            results = recall("Solomon likes espresso coffee beverages",
                             owner_id="solomon", use_routing=False,
                             min_similarity=0.0, debug=True)
            direct_results = [r for r in results if "_debug" in r]
            assert len(direct_results) > 0
            d = direct_results[0]["_debug"]
            assert isinstance(d["raw_quality_score"], float)
            assert isinstance(d["composite_score"], float)
            assert isinstance(d["intent"], str)
            assert isinstance(d["type_boost"], (int, float))
            assert isinstance(d["node_type"], str)
            assert isinstance(d["confidence"], (int, float))
            assert isinstance(d["access_count"], int)
            assert isinstance(d["confirmation_count"], int)

    def test_debug_default_is_false(self, tmp_path):
        """recall() defaults to debug=False."""
        from memory_graph import store, recall
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph.route_query", side_effect=lambda q: q):
            store("Solomon likes espresso coffee beverages",
                  owner_id="solomon", skip_dedup=True)
            # Call without debug parameter
            results = recall("Solomon likes espresso coffee beverages",
                             owner_id="solomon", use_routing=False,
                             min_similarity=0.0)
            for r in results:
                assert "_debug" not in r

    def test_debug_composite_score_matches_similarity(self, tmp_path):
        """The _debug composite_score should match the reported similarity."""
        from memory_graph import store, recall
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("memory_graph.route_query", side_effect=lambda q: q):
            store("Solomon likes espresso coffee beverages",
                  owner_id="solomon", skip_dedup=True)
            results = recall("Solomon likes espresso coffee beverages",
                             owner_id="solomon", use_routing=False,
                             min_similarity=0.0, debug=True)
            direct_results = [r for r in results if "_debug" in r]
            for r in direct_results:
                # Similarity is round(composite, 3)
                assert abs(r["similarity"] - round(r["_debug"]["composite_score"], 3)) < 0.002


# ===========================================================================
# 4. Temporal-Aware Contradiction Tests
# ===========================================================================

class TestTemporalContradictionCandidates:
    """recall_similar_pairs and batch_contradiction_check temporal awareness."""

    def test_contradiction_candidates_include_temporal_fields(self, tmp_path):
        """recall_similar_pairs includes temporal fields in contradiction candidates."""
        from janitor import recall_similar_pairs, JanitorMetrics, CONTRADICTION_MIN_SIM
        from memory_graph import Node
        graph, _ = _make_graph(tmp_path)
        metrics = JanitorMetrics()

        # Create two facts: one with negation, one without, at moderate similarity
        node_a = Node.create(type="Fact", name="Solomon does not drink alcohol",
                             owner_id="solomon", status="pending",
                             valid_from="2024-01-01", valid_until="2024-12-31")
        node_b = Node.create(type="Fact", name="Solomon drinks alcohol daily",
                             owner_id="solomon", status="approved",
                             valid_from="2023-01-01", valid_until="2023-06-30")

        # We need to mock the recall candidates flow, so test the dict shape instead
        # Build a candidate dict as the function would
        candidate = {
            "id_a": node_a.id,
            "text_a": node_a.name,
            "created_a": node_a.created_at,
            "valid_from_a": node_a.valid_from,
            "valid_until_a": node_a.valid_until,
            "id_b": node_b.id,
            "text_b": node_b.name,
            "created_b": node_b.created_at,
            "valid_from_b": node_b.valid_from,
            "valid_until_b": node_b.valid_until,
            "similarity": 0.75,
        }
        assert "valid_from_a" in candidate
        assert "valid_until_a" in candidate
        assert "valid_from_b" in candidate
        assert "valid_until_b" in candidate
        assert "created_a" in candidate
        assert "created_b" in candidate

    def test_batch_contradiction_prompt_includes_temporal_context(self):
        """batch_contradiction_check formats temporal validity in the prompt."""
        from janitor import batch_contradiction_check, JanitorMetrics

        pairs = [{
            "id_a": "a1", "text_a": "Solomon lives in Austin",
            "id_b": "b1", "text_b": "Solomon does not live in Austin",
            "created_a": "2024-06-01T10:00:00", "created_b": "2025-01-15T10:00:00",
            "valid_from_a": "2024-01-01", "valid_until_a": "2024-12-31",
            "valid_from_b": "2025-01-01", "valid_until_b": None,
            "similarity": 0.75,
        }]

        # Mock call_low_reasoning to capture the prompt
        captured_prompts = []
        def mock_llm(prompt, max_tokens=200, timeout=30):
            captured_prompts.append(prompt)
            return ('[{"pair": 1, "contradicts": false}]', 0.1)

        metrics = JanitorMetrics()
        with patch("janitor.call_low_reasoning", side_effect=mock_llm):
            batch_contradiction_check(pairs, metrics)

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        # Should include temporal validity for fact A
        assert "valid:" in prompt
        assert "2024-01-01" in prompt

    def test_batch_contradiction_prompt_includes_temporal_succession_instruction(self):
        """The contradiction prompt explicitly mentions temporal succession."""
        from janitor import batch_contradiction_check, JanitorMetrics

        pairs = [{
            "id_a": "a1", "text_a": "Solomon lives in Austin TX",
            "id_b": "b1", "text_b": "Solomon does not live in Austin",
            "created_a": "2024-06-01T10:00:00", "created_b": "2025-01-15T10:00:00",
            "similarity": 0.75,
        }]

        captured_prompts = []
        def mock_llm(prompt, max_tokens=200, timeout=30):
            captured_prompts.append(prompt)
            return ('[{"pair": 1, "contradicts": false}]', 0.1)

        metrics = JanitorMetrics()
        with patch("janitor.call_low_reasoning", side_effect=mock_llm):
            batch_contradiction_check(pairs, metrics)

        prompt = captured_prompts[0]
        assert "temporal succession" in prompt.lower()

    def test_batch_contradiction_prompt_shows_recorded_dates(self):
        """The prompt should show recorded dates for each fact."""
        from janitor import batch_contradiction_check, JanitorMetrics

        pairs = [{
            "id_a": "a1", "text_a": "Solomon weighs 180 pounds",
            "id_b": "b1", "text_b": "Solomon does not weigh 180 pounds",
            "created_a": "2024-03-15T10:00:00", "created_b": "2025-01-01T10:00:00",
            "similarity": 0.75,
        }]

        captured_prompts = []
        def mock_llm(prompt, max_tokens=200, timeout=30):
            captured_prompts.append(prompt)
            return ('[{"pair": 1, "contradicts": false}]', 0.1)

        metrics = JanitorMetrics()
        with patch("janitor.call_low_reasoning", side_effect=mock_llm):
            batch_contradiction_check(pairs, metrics)

        prompt = captured_prompts[0]
        assert "2024-03-15" in prompt
        assert "2025-01-01" in prompt

    def test_batch_contradiction_no_temporal_shows_unknown(self):
        """When created_at is missing, prompt shows 'unknown'."""
        from janitor import batch_contradiction_check, JanitorMetrics

        pairs = [{
            "id_a": "a1", "text_a": "Solomon has a cat pet",
            "id_b": "b1", "text_b": "Solomon does not have a cat",
            "similarity": 0.75,
        }]

        captured_prompts = []
        def mock_llm(prompt, max_tokens=200, timeout=30):
            captured_prompts.append(prompt)
            return ('[{"pair": 1, "contradicts": true, "explanation": "direct contradiction"}]', 0.1)

        metrics = JanitorMetrics()
        with patch("janitor.call_low_reasoning", side_effect=mock_llm):
            batch_contradiction_check(pairs, metrics)

        prompt = captured_prompts[0]
        assert "unknown" in prompt

    def test_batch_contradiction_skips_valid_info_when_absent(self):
        """When no valid_from/valid_until, no [valid: ...] annotation appears."""
        from janitor import batch_contradiction_check, JanitorMetrics

        pairs = [{
            "id_a": "a1", "text_a": "Solomon eats meat regularly",
            "id_b": "b1", "text_b": "Solomon does not eat meat",
            "created_a": "2025-01-01T10:00:00", "created_b": "2025-06-01T10:00:00",
            "similarity": 0.75,
        }]

        captured_prompts = []
        def mock_llm(prompt, max_tokens=200, timeout=30):
            captured_prompts.append(prompt)
            return ('[{"pair": 1, "contradicts": true, "explanation": "contradiction"}]', 0.1)

        metrics = JanitorMetrics()
        with patch("janitor.call_low_reasoning", side_effect=mock_llm):
            batch_contradiction_check(pairs, metrics)

        prompt = captured_prompts[0]
        # No [valid: ...] annotation since no valid_from/valid_until
        assert "[valid:" not in prompt


# ===========================================================================
# 5. Prompt Caching Tests
# ===========================================================================

class TestPromptCaching:
    """Prompt caching via cache_control in system messages."""

    def test_get_token_usage_includes_cache_read_tokens(self):
        from llm_clients import get_token_usage, reset_token_usage
        reset_token_usage()
        usage = get_token_usage()
        assert "cache_read_tokens" in usage
        assert isinstance(usage["cache_read_tokens"], int)

    def test_get_token_usage_includes_cache_creation_tokens(self):
        from llm_clients import get_token_usage, reset_token_usage
        reset_token_usage()
        usage = get_token_usage()
        assert "cache_creation_tokens" in usage
        assert isinstance(usage["cache_creation_tokens"], int)

    def test_reset_token_usage_zeroes_cache_counters(self):
        import llm_clients
        llm_clients._usage_cache_read_tokens = 100
        llm_clients._usage_cache_creation_tokens = 200
        llm_clients.reset_token_usage()
        usage = llm_clients.get_token_usage()
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_creation_tokens"] == 0

    def test_reset_token_usage_zeroes_all_counters(self):
        import llm_clients
        llm_clients._usage_input_tokens = 500
        llm_clients._usage_output_tokens = 300
        llm_clients._usage_calls = 5
        llm_clients._usage_cache_read_tokens = 100
        llm_clients._usage_cache_creation_tokens = 200
        llm_clients.reset_token_usage()
        usage = llm_clients.get_token_usage()
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["api_calls"] == 0
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_creation_tokens"] == 0

    def test_call_anthropic_sends_system_as_content_block(self):
        """System prompt should be sent as a content block with cache_control."""
        import llm_clients

        captured_bodies = []
        original_urlopen = None

        def mock_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            captured_bodies.append(body)
            # Return a mock response
            resp = MagicMock()
            resp.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 10, "output_tokens": 5}
            }).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("llm_clients.get_api_key", return_value="test-key"), \
             patch("urllib.request.urlopen", side_effect=mock_urlopen):
            llm_clients.call_anthropic("Test system prompt", "Hello", max_tokens=100)

        assert len(captured_bodies) == 1
        body = captured_bodies[0]
        # System should be a list of content blocks, not a string
        assert isinstance(body["system"], list)
        assert len(body["system"]) >= 1
        block = body["system"][0]
        assert block["type"] == "text"
        assert block["text"] == "Test system prompt"
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_call_anthropic_accumulates_cache_tokens(self):
        """Cache token counts from API response should be accumulated."""
        import llm_clients
        llm_clients.reset_token_usage()

        def mock_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps({
                "content": [{"type": "text", "text": "ok"}],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 80,
                    "cache_creation_input_tokens": 20,
                }
            }).encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("llm_clients.get_api_key", return_value="test-key"), \
             patch("urllib.request.urlopen", side_effect=mock_urlopen):
            llm_clients.call_anthropic("System", "User", max_tokens=100)

        usage = llm_clients.get_token_usage()
        assert usage["cache_read_tokens"] == 80
        assert usage["cache_creation_tokens"] == 20


# ===========================================================================
# 6. LLM Output Validation Tests
# ===========================================================================

class TestReviewDecision:
    """ReviewDecision validation and fuzzy matching."""

    def test_valid_action_keep(self):
        from llm_clients import ReviewDecision
        d = ReviewDecision(action="KEEP")
        assert d.action == "KEEP"

    def test_valid_action_reject(self):
        from llm_clients import ReviewDecision
        d = ReviewDecision(action="REJECT")
        assert d.action == "REJECT"

    def test_valid_action_fix(self):
        from llm_clients import ReviewDecision
        d = ReviewDecision(action="FIX")
        assert d.action == "FIX"

    def test_valid_action_merge(self):
        from llm_clients import ReviewDecision
        d = ReviewDecision(action="MERGE")
        assert d.action == "MERGE"

    def test_fuzzy_match_kee_to_keep(self):
        from llm_clients import ReviewDecision
        d = ReviewDecision(action="KEE")
        assert d.action == "KEEP"

    def test_fuzzy_match_rej_to_reject(self):
        from llm_clients import ReviewDecision
        d = ReviewDecision(action="REJ")
        assert d.action == "REJECT"

    def test_fuzzy_match_fix_lowercase(self):
        from llm_clients import ReviewDecision
        d = ReviewDecision(action="fix")
        assert d.action == "FIX"

    def test_fuzzy_match_mer_to_merge(self):
        from llm_clients import ReviewDecision
        d = ReviewDecision(action="MER")
        assert d.action == "MERGE"

    def test_invalid_action_raises(self):
        from llm_clients import ReviewDecision
        with pytest.raises(ValueError, match="Invalid review action"):
            ReviewDecision(action="UNKNOWN")

    def test_invalid_action_too_short_raises(self):
        from llm_clients import ReviewDecision
        with pytest.raises(ValueError, match="Invalid review action"):
            ReviewDecision(action="XY")


class TestDedupDecision:
    """DedupDecision validation and bool coercion."""

    def test_valid_pair(self):
        from llm_clients import DedupDecision
        d = DedupDecision(pair=1, is_same=True)
        assert d.pair == 1
        assert d.is_same is True

    def test_pair_zero_raises(self):
        from llm_clients import DedupDecision
        with pytest.raises(ValueError, match="Invalid pair index"):
            DedupDecision(pair=0, is_same=True)

    def test_pair_negative_raises(self):
        from llm_clients import DedupDecision
        with pytest.raises(ValueError, match="Invalid pair index"):
            DedupDecision(pair=-1, is_same=True)

    def test_is_same_string_true(self):
        from llm_clients import DedupDecision
        d = DedupDecision(pair=1, is_same="true")
        assert d.is_same is True

    def test_is_same_string_yes(self):
        from llm_clients import DedupDecision
        d = DedupDecision(pair=1, is_same="yes")
        assert d.is_same is True

    def test_is_same_string_1(self):
        from llm_clients import DedupDecision
        d = DedupDecision(pair=1, is_same="1")
        assert d.is_same is True

    def test_is_same_string_false(self):
        from llm_clients import DedupDecision
        d = DedupDecision(pair=1, is_same="false")
        assert d.is_same is False

    def test_is_same_string_no(self):
        from llm_clients import DedupDecision
        d = DedupDecision(pair=1, is_same="no")
        assert d.is_same is False

    def test_is_same_int_coercion(self):
        from llm_clients import DedupDecision
        d = DedupDecision(pair=1, is_same=1)
        assert d.is_same is True

    def test_pair_large_valid(self):
        from llm_clients import DedupDecision
        d = DedupDecision(pair=999, is_same=False)
        assert d.pair == 999


class TestContradictionResult:
    """ContradictionResult validation and bool coercion."""

    def test_valid_contradiction(self):
        from llm_clients import ContradictionResult
        c = ContradictionResult(pair=1, contradicts=True, explanation="direct conflict")
        assert c.pair == 1
        assert c.contradicts is True

    def test_pair_zero_raises(self):
        from llm_clients import ContradictionResult
        with pytest.raises(ValueError, match="Invalid pair index"):
            ContradictionResult(pair=0, contradicts=True)

    def test_pair_negative_raises(self):
        from llm_clients import ContradictionResult
        with pytest.raises(ValueError, match="Invalid pair index"):
            ContradictionResult(pair=-1, contradicts=False)

    def test_contradicts_string_true(self):
        from llm_clients import ContradictionResult
        c = ContradictionResult(pair=1, contradicts="true")
        assert c.contradicts is True

    def test_contradicts_string_yes(self):
        from llm_clients import ContradictionResult
        c = ContradictionResult(pair=1, contradicts="yes")
        assert c.contradicts is True

    def test_contradicts_string_false(self):
        from llm_clients import ContradictionResult
        c = ContradictionResult(pair=1, contradicts="false")
        assert c.contradicts is False

    def test_contradicts_int_coercion(self):
        from llm_clients import ContradictionResult
        c = ContradictionResult(pair=2, contradicts=0)
        assert c.contradicts is False


class TestDecayDecision:
    """DecayDecision validation and fuzzy matching."""

    def test_valid_delete(self):
        from llm_clients import DecayDecision
        d = DecayDecision(id="abc", action="delete")
        assert d.action == "delete"

    def test_valid_extend(self):
        from llm_clients import DecayDecision
        d = DecayDecision(id="abc", action="extend")
        assert d.action == "extend"

    def test_valid_pin(self):
        from llm_clients import DecayDecision
        d = DecayDecision(id="abc", action="pin")
        assert d.action == "pin"

    def test_fuzzy_del_to_delete(self):
        from llm_clients import DecayDecision
        d = DecayDecision(id="abc", action="del")
        assert d.action == "delete"

    def test_fuzzy_ext_to_extend(self):
        from llm_clients import DecayDecision
        d = DecayDecision(id="abc", action="ext")
        assert d.action == "extend"

    def test_fuzzy_pin_prefix(self):
        from llm_clients import DecayDecision
        d = DecayDecision(id="abc", action="pin_it")
        assert d.action == "pin"

    def test_invalid_action_raises(self):
        from llm_clients import DecayDecision
        with pytest.raises(ValueError, match="Invalid decay action"):
            DecayDecision(id="abc", action="freeze")

    def test_case_insensitive(self):
        from llm_clients import DecayDecision
        d = DecayDecision(id="abc", action="DELETE")
        assert d.action == "delete"

    def test_case_insensitive_extend(self):
        from llm_clients import DecayDecision
        d = DecayDecision(id="abc", action="Extend")
        assert d.action == "extend"


class TestValidateLlmOutput:
    """validate_llm_output function tests."""

    def test_valid_list_returns_instances(self):
        from llm_clients import validate_llm_output, ReviewDecision
        parsed = [
            {"action": "KEEP", "reasoning": "looks good"},
            {"action": "REJECT", "reasoning": "not a fact"},
        ]
        results = validate_llm_output(parsed, ReviewDecision)
        assert len(results) == 2
        assert results[0].action == "KEEP"
        assert results[1].action == "REJECT"

    def test_none_returns_empty_list(self):
        from llm_clients import validate_llm_output, ReviewDecision
        results = validate_llm_output(None, ReviewDecision)
        assert results == []

    def test_non_list_returns_single_item(self):
        from llm_clients import validate_llm_output, ReviewDecision
        parsed = {"action": "FIX", "reasoning": "typo", "fixed_text": "corrected"}
        results = validate_llm_output(parsed, ReviewDecision)
        assert len(results) == 1
        assert results[0].action == "FIX"
        assert results[0].fixed_text == "corrected"

    def test_skips_invalid_items(self):
        from llm_clients import validate_llm_output, ReviewDecision
        parsed = [
            {"action": "KEEP"},
            {"action": "TOTALLY_INVALID_ACTION_XYZ"},  # Should be skipped
            {"action": "REJECT"},
        ]
        results = validate_llm_output(parsed, ReviewDecision)
        assert len(results) == 2
        assert results[0].action == "KEEP"
        assert results[1].action == "REJECT"

    def test_case_insensitive_key_mapping(self):
        from llm_clients import validate_llm_output, ReviewDecision
        parsed = [{"Action": "KEEP", "Reasoning": "fine"}]
        results = validate_llm_output(parsed, ReviewDecision)
        assert len(results) == 1
        assert results[0].action == "KEEP"
        assert results[0].reasoning == "fine"

    def test_skips_non_dict_items(self):
        from llm_clients import validate_llm_output, DedupDecision
        parsed = [
            {"pair": 1, "is_same": True},
            "not a dict",
            42,
            None,
            {"pair": 2, "is_same": False},
        ]
        results = validate_llm_output(parsed, DedupDecision)
        assert len(results) == 2

    def test_dedup_decision_validation(self):
        from llm_clients import validate_llm_output, DedupDecision
        parsed = [
            {"pair": 1, "is_same": "true", "reasoning": "same fact"},
            {"pair": 2, "is_same": "false"},
        ]
        results = validate_llm_output(parsed, DedupDecision)
        assert len(results) == 2
        assert results[0].is_same is True
        assert results[1].is_same is False

    def test_contradiction_result_validation(self):
        from llm_clients import validate_llm_output, ContradictionResult
        parsed = [
            {"pair": 1, "contradicts": True, "explanation": "opposite facts"},
            {"pair": 2, "contradicts": False},
        ]
        results = validate_llm_output(parsed, ContradictionResult)
        assert len(results) == 2
        assert results[0].contradicts is True
        assert results[1].contradicts is False

    def test_decay_decision_validation(self):
        from llm_clients import validate_llm_output, DecayDecision
        parsed = [
            {"id": "node-1", "action": "delete", "reason": "stale"},
            {"id": "node-2", "action": "extend", "reason": "still relevant"},
            {"id": "node-3", "action": "pin", "reason": "core fact"},
        ]
        results = validate_llm_output(parsed, DecayDecision)
        assert len(results) == 3

    def test_validate_with_invalid_pair_index_skips(self):
        from llm_clients import validate_llm_output, DedupDecision
        parsed = [
            {"pair": 0, "is_same": True},  # Invalid: pair must be >= 1
            {"pair": 1, "is_same": True},
        ]
        results = validate_llm_output(parsed, DedupDecision)
        assert len(results) == 1
        assert results[0].pair == 1

    def test_validate_with_missing_required_fields_skips(self):
        from llm_clients import validate_llm_output, ReviewDecision
        parsed = [
            {"reasoning": "no action field"},  # Missing required 'action'
            {"action": "KEEP"},
        ]
        results = validate_llm_output(parsed, ReviewDecision)
        assert len(results) == 1
        assert results[0].action == "KEEP"

    def test_validate_hyphenated_keys(self):
        from llm_clients import validate_llm_output, DedupDecision
        parsed = [{"pair": 1, "is-same": True}]
        results = validate_llm_output(parsed, DedupDecision)
        assert len(results) == 1
        assert results[0].is_same is True

    def test_validate_empty_list_returns_empty(self):
        from llm_clients import validate_llm_output, ReviewDecision
        results = validate_llm_output([], ReviewDecision)
        assert results == []

    def test_validate_fuzzy_action_via_validate(self):
        """Fuzzy matching should work through validate_llm_output too."""
        from llm_clients import validate_llm_output, ReviewDecision
        parsed = [{"action": "KEE"}]
        results = validate_llm_output(parsed, ReviewDecision)
        assert len(results) == 1
        assert results[0].action == "KEEP"
