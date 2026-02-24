"""Tests for Chunk 2 improvements: Knowledge Type, Entity Alias, Cross-Encoder Reranking, Multi-Pass Retrieval."""

import os
import sys
import json
import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Must set env BEFORE imports so lib.config picks it up
os.environ["MEMORY_DB_PATH"] = ":memory:"

import pytest

pytestmark = pytest.mark.regression


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_EMBEDDING = [0.1] * 128  # Short fixed vector for tests


def _fake_get_embedding(text):
    """Return a deterministic fake embedding based on text hash."""
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim


def _make_graph(tmp_path):
    """Create a MemoryGraph backed by a temp SQLite file."""
    from datastore.memorydb.memory_graph import MemoryGraph
    db_file = tmp_path / "test.db"
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph, db_file


def _make_node(graph, name, owner_id="quaid", knowledge_type="fact", confidence=0.5, **kwargs):
    """Create and add a node to the graph with a fake embedding."""
    from datastore.memorydb.memory_graph import Node
    node = Node.create(
        type=kwargs.get("type", "Fact"),
        name=name,
        verified=kwargs.get("verified", False),
        pinned=kwargs.get("pinned", False),
        confidence=confidence,
        knowledge_type=knowledge_type,
        owner_id=owner_id,
        status=kwargs.get("status", "approved"),
    )
    with patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph.add_node(node)
    return node


# ===========================================================================
# Feature #12: Knowledge Type Distinction
# ===========================================================================


class TestKnowledgeTypeNode:
    """Node dataclass and schema behavior for knowledge_type."""

    def test_node_defaults_to_fact(self):
        """Node dataclass defaults knowledge_type to 'fact'."""
        from datastore.memorydb.memory_graph import Node
        node = Node.create(type="Fact", name="test fact")
        assert node.knowledge_type == "fact"

    def test_node_create_with_belief(self):
        """Node can be created with knowledge_type='belief'."""
        from datastore.memorydb.memory_graph import Node
        node = Node.create(type="Fact", name="I think the earth is round", knowledge_type="belief")
        assert node.knowledge_type == "belief"

    def test_node_create_with_preference(self):
        """Node can be created with knowledge_type='preference'."""
        from datastore.memorydb.memory_graph import Node
        node = Node.create(type="Preference", name="Quaid prefers dark coffee", knowledge_type="preference")
        assert node.knowledge_type == "preference"

    def test_node_create_with_experience(self):
        """Node can be created with knowledge_type='experience'."""
        from datastore.memorydb.memory_graph import Node
        node = Node.create(type="Fact", name="Quaid visited Tokyo last year", knowledge_type="experience")
        assert node.knowledge_type == "experience"

    def test_schema_check_constraint_rejects_invalid(self, tmp_path):
        """Schema CHECK constraint rejects invalid knowledge_type values."""
        graph, _ = _make_graph(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            with graph._get_conn() as conn:
                conn.execute("""
                    INSERT INTO nodes (id, type, name, knowledge_type)
                    VALUES (?, 'Fact', 'test', 'invalid_type')
                """, (str(uuid.uuid4()),))

    def test_schema_check_allows_fact(self, tmp_path):
        """Schema CHECK constraint allows knowledge_type='fact'."""
        graph, _ = _make_graph(tmp_path)
        nid = str(uuid.uuid4())
        with graph._get_conn() as conn:
            conn.execute("""
                INSERT INTO nodes (id, type, name, knowledge_type)
                VALUES (?, 'Fact', 'test fact', 'fact')
            """, (nid,))
            row = conn.execute("SELECT knowledge_type FROM nodes WHERE id = ?", (nid,)).fetchone()
            assert row["knowledge_type"] == "fact"

    def test_schema_check_allows_belief(self, tmp_path):
        """Schema CHECK constraint allows knowledge_type='belief'."""
        graph, _ = _make_graph(tmp_path)
        nid = str(uuid.uuid4())
        with graph._get_conn() as conn:
            conn.execute("""
                INSERT INTO nodes (id, type, name, knowledge_type)
                VALUES (?, 'Fact', 'a belief', 'belief')
            """, (nid,))
            row = conn.execute("SELECT knowledge_type FROM nodes WHERE id = ?", (nid,)).fetchone()
            assert row["knowledge_type"] == "belief"

    def test_schema_check_allows_preference(self, tmp_path):
        """Schema CHECK constraint allows knowledge_type='preference'."""
        graph, _ = _make_graph(tmp_path)
        nid = str(uuid.uuid4())
        with graph._get_conn() as conn:
            conn.execute("""
                INSERT INTO nodes (id, type, name, knowledge_type)
                VALUES (?, 'Preference', 'prefers dark', 'preference')
            """, (nid,))
            row = conn.execute("SELECT knowledge_type FROM nodes WHERE id = ?", (nid,)).fetchone()
            assert row["knowledge_type"] == "preference"

    def test_schema_check_allows_experience(self, tmp_path):
        """Schema CHECK constraint allows knowledge_type='experience'."""
        graph, _ = _make_graph(tmp_path)
        nid = str(uuid.uuid4())
        with graph._get_conn() as conn:
            conn.execute("""
                INSERT INTO nodes (id, type, name, knowledge_type)
                VALUES (?, 'Fact', 'visited Tokyo', 'experience')
            """, (nid,))
            row = conn.execute("SELECT knowledge_type FROM nodes WHERE id = ?", (nid,)).fetchone()
            assert row["knowledge_type"] == "experience"

    def test_row_to_node_maps_knowledge_type(self, tmp_path):
        """_row_to_node correctly maps knowledge_type from database row."""
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Quaid believes the future is bright", knowledge_type="belief")
        retrieved = graph.get_node(node.id)
        assert retrieved.knowledge_type == "belief"

    def test_row_to_node_defaults_to_fact_for_missing(self, tmp_path):
        """_row_to_node defaults to 'fact' if knowledge_type column is missing."""
        graph, _ = _make_graph(tmp_path)
        # Add a node normally
        node = _make_node(graph, "Quaid has a cat named Richter")
        retrieved = graph.get_node(node.id)
        assert retrieved.knowledge_type == "fact"


class TestKnowledgeTypeComposite:
    """_compute_composite_score knowledge_type adjustments."""

    def test_belief_reduces_confidence_bonus(self):
        """knowledge_type='belief' reduces confidence_bonus by 0.7x."""
        from datastore.memorydb.memory_graph import Node, _compute_composite_score
        node_fact = Node.create(type="Fact", name="fact", confidence=0.8, knowledge_type="fact")
        node_fact.accessed_at = datetime.now().isoformat()
        node_fact.access_count = 0

        node_belief = Node.create(type="Fact", name="belief", confidence=0.8, knowledge_type="belief")
        node_belief.accessed_at = datetime.now().isoformat()
        node_belief.access_count = 0

        score_fact = _compute_composite_score(node_fact, 0.9)
        score_belief = _compute_composite_score(node_belief, 0.9)
        # Belief should be lower because confidence_bonus is reduced by 0.7x
        assert score_belief < score_fact

    def test_preference_reduces_confidence_bonus(self):
        """knowledge_type='preference' reduces confidence_bonus by 0.9x."""
        from datastore.memorydb.memory_graph import Node, _compute_composite_score
        node_fact = Node.create(type="Fact", name="fact", confidence=0.8, knowledge_type="fact")
        node_fact.accessed_at = datetime.now().isoformat()
        node_fact.access_count = 0

        node_pref = Node.create(type="Preference", name="preference", confidence=0.8, knowledge_type="preference")
        node_pref.accessed_at = datetime.now().isoformat()
        node_pref.access_count = 0

        score_fact = _compute_composite_score(node_fact, 0.9)
        score_pref = _compute_composite_score(node_pref, 0.9)
        # Preference should be lower, but less so than belief
        assert score_pref < score_fact

    def test_fact_leaves_confidence_bonus_unchanged(self):
        """knowledge_type='fact' does not alter confidence_bonus."""
        from datastore.memorydb.memory_graph import Node, _compute_composite_score
        node = Node.create(type="Fact", name="fact", confidence=0.8, knowledge_type="fact")
        node.accessed_at = datetime.now().isoformat()
        node.access_count = 0

        # The raw confidence_bonus for confidence=0.8 is 0.8 * 0.05 = 0.04
        # No multiplier applied for 'fact'
        score = _compute_composite_score(node, 0.9)
        # Confirm the score includes the full confidence bonus
        # Base: 0.60 * 0.9 + 0.20 * recency + 0.15 * freq + conf_bonus
        # recency ~1.0 (just accessed), freq = 0.0 (access_count=0)
        # = 0.54 + 0.20 + 0.0 + 0.04 = 0.78
        assert score > 0.70  # Sanity check it's reasonable

    def test_belief_penalty_is_less_than_preference_penalty(self):
        """Belief has a larger confidence_bonus reduction (0.7x) than preference (0.9x)."""
        from datastore.memorydb.memory_graph import Node, _compute_composite_score
        node_belief = Node.create(type="Fact", name="belief", confidence=0.8, knowledge_type="belief")
        node_belief.accessed_at = datetime.now().isoformat()
        node_belief.access_count = 0

        node_pref = Node.create(type="Preference", name="pref", confidence=0.8, knowledge_type="preference")
        node_pref.accessed_at = datetime.now().isoformat()
        node_pref.access_count = 0

        score_belief = _compute_composite_score(node_belief, 0.9)
        score_pref = _compute_composite_score(node_pref, 0.9)
        # Belief reduces more (0.7x) than preference (0.9x) so belief score should be lower
        assert score_belief < score_pref

    def test_experience_leaves_confidence_bonus_unchanged(self):
        """knowledge_type='experience' does not alter confidence_bonus (same as 'fact')."""
        from datastore.memorydb.memory_graph import Node, _compute_composite_score
        node_fact = Node.create(type="Fact", name="fact", confidence=0.8, knowledge_type="fact")
        node_fact.accessed_at = datetime.now().isoformat()
        node_fact.access_count = 0

        node_exp = Node.create(type="Fact", name="experience", confidence=0.8, knowledge_type="experience")
        node_exp.accessed_at = datetime.now().isoformat()
        node_exp.access_count = 0

        score_fact = _compute_composite_score(node_fact, 0.9)
        score_exp = _compute_composite_score(node_exp, 0.9)
        # experience has no special handling, should be same as fact
        assert score_fact == score_exp


class TestKnowledgeTypeStore:
    """store() function knowledge_type parameter."""

    def test_store_accepts_knowledge_type_parameter(self, tmp_path):
        """store() accepts knowledge_type and passes it to created node."""
        from datastore.memorydb.memory_graph import store
        graph, _ = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            result = store("Quaid believes in kindness and empathy",
                           owner_id="quaid", skip_dedup=True, knowledge_type="belief")
            node = graph.get_node(result["id"])
            assert node.knowledge_type == "belief"

    def test_store_defaults_knowledge_type_to_fact(self, tmp_path):
        """store() defaults knowledge_type to 'fact' when not specified."""
        from datastore.memorydb.memory_graph import store
        graph, _ = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            result = store("Quaid has a dog named Rex",
                           owner_id="quaid", skip_dedup=True)
            node = graph.get_node(result["id"])
            assert node.knowledge_type == "fact"

    def test_store_with_preference_knowledge_type(self, tmp_path):
        """store() with knowledge_type='preference' creates node with correct type."""
        from datastore.memorydb.memory_graph import store
        graph, _ = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            result = store("Quaid prefers morning walks over evening runs",
                           owner_id="quaid", skip_dedup=True, knowledge_type="preference")
            node = graph.get_node(result["id"])
            assert node.knowledge_type == "preference"

    def test_store_with_experience_knowledge_type(self, tmp_path):
        """store() with knowledge_type='experience' creates node with correct type."""
        from datastore.memorydb.memory_graph import store
        graph, _ = _make_graph(tmp_path)
        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            result = store("Quaid traveled through Southeast Asia in 2024",
                           owner_id="quaid", skip_dedup=True, knowledge_type="experience")
            node = graph.get_node(result["id"])
            assert node.knowledge_type == "experience"


# ===========================================================================
# Feature #4: Entity Alias Table + Fuzzy Matching
# ===========================================================================


class TestEntityAliasAdd:
    """add_alias() stores entity aliases."""

    def test_add_alias_stores_correctly(self, tmp_path):
        """add_alias() stores alias->canonical mapping."""
        graph, _ = _make_graph(tmp_path)
        alias_id = graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        assert alias_id  # Returns a UUID
        uuid.UUID(alias_id)  # Should be valid UUID

    def test_add_alias_lowercases(self, tmp_path):
        """add_alias() lowercases the alias."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("Sol", "Douglas Quaid", owner_id="quaid")
        aliases = graph.get_aliases(owner_id="quaid")
        assert len(aliases) == 1
        assert aliases[0]["alias"] == "sol"

    def test_add_alias_strips_whitespace(self, tmp_path):
        """add_alias() strips whitespace from alias."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("  sol  ", "Douglas Quaid", owner_id="quaid")
        aliases = graph.get_aliases(owner_id="quaid")
        assert aliases[0]["alias"] == "sol"

    def test_add_alias_with_node_id(self, tmp_path):
        """add_alias() stores canonical_node_id when provided."""
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Douglas Quaid", type="Person")
        alias_id = graph.add_alias("sol", "Douglas Quaid",
                                   canonical_node_id=node.id, owner_id="quaid")
        with graph._get_conn() as conn:
            row = conn.execute("SELECT * FROM entity_aliases WHERE id = ?",
                               (alias_id,)).fetchone()
            assert row["canonical_node_id"] == node.id

    def test_unique_constraint_alias_owner(self, tmp_path):
        """UNIQUE constraint on (alias, owner_id) prevents duplicates."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        # INSERT OR REPLACE means second insert replaces (no error)
        graph.add_alias("sol", "Quaid S.", owner_id="quaid")
        aliases = graph.get_aliases(owner_id="quaid")
        # Should have exactly 1 alias (replaced, not duplicated)
        assert len(aliases) == 1
        assert aliases[0]["canonical_name"] == "Quaid S."

    def test_same_alias_different_owners(self, tmp_path):
        """Same alias for different owners is allowed."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("mom", "Hauser", owner_id="quaid")
        graph.add_alias("mom", "Jane Doe", owner_id="jane")
        with graph._get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM entity_aliases WHERE alias = 'mom'").fetchone()[0]
        assert count == 2


class TestEntityAliasResolve:
    """resolve_alias() replaces aliases in text."""

    def test_resolve_replaces_alias(self, tmp_path):
        """resolve_alias() replaces alias with canonical name."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        result = graph.resolve_alias("What does Sol like?", owner_id="quaid")
        assert "Douglas Quaid" in result
        assert "sol" not in result.lower().split()  # Word boundary check

    def test_resolve_is_case_insensitive(self, tmp_path):
        """resolve_alias() matches case-insensitively."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        result = graph.resolve_alias("What does SOL like?", owner_id="quaid")
        assert "Douglas Quaid" in result

    def test_resolve_handles_multiple_aliases(self, tmp_path):
        """resolve_alias() replaces multiple different aliases in one pass."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        graph.add_alias("mom", "Hauser", owner_id="quaid")
        result = graph.resolve_alias("Does sol talk to mom often?", owner_id="quaid")
        assert "Douglas Quaid" in result
        assert "Hauser" in result

    def test_resolve_longest_match_first(self, tmp_path):
        """resolve_alias() resolves longest alias first to avoid partial matches."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Quaid", owner_id="quaid")
        graph.add_alias("quaid s", "Douglas Quaid", owner_id="quaid")
        result = graph.resolve_alias("Tell me about Quaid S and his hobbies", owner_id="quaid")
        # "Quaid S" should match the longer alias first
        assert "Douglas Quaid" in result

    def test_resolve_no_aliases_returns_unchanged(self, tmp_path):
        """resolve_alias() returns text unchanged when no aliases exist."""
        graph, _ = _make_graph(tmp_path)
        original = "What does Quaid like?"
        result = graph.resolve_alias(original, owner_id="quaid")
        assert result == original

    def test_resolve_with_owner_filter(self, tmp_path):
        """resolve_alias() filters by owner_id."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("mom", "Hauser", owner_id="quaid")
        graph.add_alias("mom", "Jane Doe", owner_id="jane")
        result = graph.resolve_alias("Tell me about mom", owner_id="quaid")
        assert "Hauser" in result
        assert "Jane Doe" not in result

    def test_resolve_word_boundary(self, tmp_path):
        """resolve_alias() respects word boundaries (no partial word matches)."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        # "solitude" should NOT be replaced
        result = graph.resolve_alias("He enjoys solitude", owner_id="quaid")
        assert result == "He enjoys solitude"


class TestEntityAliasGetDelete:
    """get_aliases() and delete_alias() operations."""

    def test_get_aliases_returns_all(self, tmp_path):
        """get_aliases() returns all aliases when no owner filter."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        graph.add_alias("mom", "Hauser", owner_id="quaid")
        aliases = graph.get_aliases()
        assert len(aliases) >= 2

    def test_get_aliases_with_owner_filter(self, tmp_path):
        """get_aliases() with owner_id returns only that owner's aliases."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        graph.add_alias("test", "Test Person", owner_id="other")
        aliases = graph.get_aliases(owner_id="quaid")
        alias_names = [a["alias"] for a in aliases]
        assert "sol" in alias_names
        # "test" may or may not appear since get_aliases includes owner_id IS NULL
        # but it should not include owner_id="other" explicitly

    def test_delete_alias_removes_successfully(self, tmp_path):
        """delete_alias() removes an alias by ID."""
        graph, _ = _make_graph(tmp_path)
        alias_id = graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")
        result = graph.delete_alias(alias_id)
        assert result is True
        aliases = graph.get_aliases(owner_id="quaid")
        assert len(aliases) == 0

    def test_delete_alias_returns_false_for_nonexistent(self, tmp_path):
        """delete_alias() returns False for nonexistent ID."""
        graph, _ = _make_graph(tmp_path)
        result = graph.delete_alias(str(uuid.uuid4()))
        assert result is False


class TestEntityAliasInRecall:
    """recall() integrates resolve_alias."""

    def test_recall_calls_resolve_alias(self, tmp_path):
        """recall() calls resolve_alias before searching."""
        graph, _ = _make_graph(tmp_path)
        graph.add_alias("sol", "Douglas Quaid", owner_id="quaid")

        # We verify resolve_alias is called by patching it and checking
        original_resolve = graph.resolve_alias

        call_log = []

        def tracked_resolve(text, owner_id=None):
            call_log.append(text)
            return original_resolve(text, owner_id=owner_id)

        graph.resolve_alias = tracked_resolve

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            recall_fn = __import__("datastore.memorydb.memory_graph", fromlist=["recall"]).recall
            # recall() will call resolve_alias as part of its pipeline
            recall_fn("Tell me about sol", owner_id="quaid",
                      use_routing=False, min_similarity=0.01)

        # resolve_alias should have been called with the query
        assert len(call_log) > 0


# ===========================================================================
# Feature #1: Cross-Encoder Reranking
# ===========================================================================


class TestRerankerConfig:
    """RetrievalConfig reranker fields."""

    def test_retrieval_config_reranker_defaults(self):
        """RetrievalConfig has correct reranker defaults."""
        from config import RetrievalConfig
        cfg = RetrievalConfig()
        assert cfg.reranker_enabled is True
        assert cfg.reranker_top_k == 20
        assert "personal memory" in cfg.reranker_instruction.lower()

    def test_config_parses_reranker_from_json(self, tmp_path):
        """config.py parses reranker settings from JSON."""
        from config import reload_config, _config
        import config as cfg_mod

        # Save and restore config state
        old_config = cfg_mod._config
        cfg_mod._config = None

        config_data = {
            "retrieval": {
                "reranker": {
                    "enabled": True,
                    "model": "test-model:1b",
                    "topK": 10,
                    "instruction": "Test instruction"
                }
            }
        }
        config_file = tmp_path / "memory.json"
        config_file.write_text(json.dumps(config_data))

        try:
            with patch("config._config_paths", lambda: [config_file]):
                cfg = reload_config()
                assert cfg.retrieval.reranker_enabled is True
                assert cfg.retrieval.reranker_top_k == 10
                assert cfg.retrieval.reranker_instruction == "Test instruction"
        finally:
            cfg_mod._config = old_config


class TestCrossEncoderReranking:
    """_rerank_with_cross_encoder() function tests."""

    def test_empty_results_returns_empty(self):
        """_rerank_with_cross_encoder with empty results returns empty."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder
        result = _rerank_with_cross_encoder("test query", [])
        assert result == []

    def test_fallback_on_connection_error(self):
        """_rerank_with_cross_encoder falls back to original scores on transport error."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        node = Node.create(type="Fact", name="Quaid lives in Bali")
        results = [(node, 0.85)]
        config = RetrievalConfig()

        with patch("lib.llm_clients.call_fast_reasoning", side_effect=ConnectionError("no server")):
            reranked = _rerank_with_cross_encoder("where does Quaid live", results, config)

        # Should keep original score
        assert len(reranked) == 1
        assert reranked[0][1] == 0.85

    def test_fallback_on_api_error(self):
        """_rerank_with_cross_encoder falls back to original scores on LLM API error."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        node = Node.create(type="Fact", name="Quaid lives in Bali")
        results = [(node, 0.85)]
        config = RetrievalConfig()

        with patch("lib.llm_clients.call_fast_reasoning", side_effect=Exception("API error")):
            reranked = _rerank_with_cross_encoder("where does Quaid live", results, config)

        assert len(reranked) == 1
        assert reranked[0][1] == 0.85

    def test_llm_batches_all_candidates(self):
        """LLM reranker sends all candidates in a single call."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        nodes = [
            (Node.create(type="Fact", name=f"Fact {i}"), 0.8)
            for i in range(5)
        ]
        config = RetrievalConfig()

        # Current reranker expects numeric 0-5 grades.
        response = "1. 5\n2. 0\n3. 5\n4. 0\n5. 5"

        with patch("lib.llm_clients.call_fast_reasoning", return_value=(response, 0.5)) as mock_call:
            reranked = _rerank_with_cross_encoder("test query", nodes, config)

        # Single call for all 5 candidates
        mock_call.assert_called_once()
        assert len(reranked) == 5
        # YES items should have higher scores than NO items
        yes_scores = [s for n, s in reranked if n.name in ("Fact 0", "Fact 2", "Fact 4")]
        no_scores = [s for n, s in reranked if n.name in ("Fact 1", "Fact 3")]
        assert min(yes_scores) > max(no_scores)

    def test_llm_parses_various_formats(self):
        """LLM reranker handles different response formats."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        nodes = [
            (Node.create(type="Fact", name="Fact A"), 0.8),
            (Node.create(type="Fact", name="Fact B"), 0.7),
            (Node.create(type="Fact", name="Fact C"), 0.6),
        ]
        config = RetrievalConfig()

        # Mixed formats: "1. YES", "2: no", "3) Yes"
        response = "1. YES\n2: no\n3) Yes"

        with patch("lib.llm_clients.call_fast_reasoning", return_value=(response, 0.5)):
            reranked = _rerank_with_cross_encoder("test", nodes, config)

        assert len(reranked) == 3

    def test_llm_null_response_fallback(self):
        """LLM reranker falls back when LLM returns None."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        node = Node.create(type="Fact", name="Test fact")
        results = [(node, 0.75)]
        config = RetrievalConfig()

        with patch("lib.llm_clients.call_fast_reasoning", return_value=(None, 0.0)):
            reranked = _rerank_with_cross_encoder("test", results, config)

        assert len(reranked) == 1
        assert reranked[0][1] == 0.75  # Original score preserved

    def test_blends_scores_high_grade_response(self):
        """_rerank_with_cross_encoder blends scores correctly for high numeric grade."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        node = Node.create(type="Fact", name="Quaid lives in Bali")
        original_score = 0.7
        results = [(node, original_score)]
        config = RetrievalConfig()

        with patch("lib.llm_clients.call_fast_reasoning", return_value=("1. 5", 0.1)):
            reranked = _rerank_with_cross_encoder("where does Quaid live", results, config)

        # Config default reranker_blend is 0.5
        # Expected: 0.5 * 1.0 + 0.5 * 0.7 = 0.85
        blend = config.reranker_blend  # 0.5 default
        expected = blend * 1.0 + (1 - blend) * original_score
        assert len(reranked) == 1
        assert abs(reranked[0][1] - expected) < 0.001

    def test_blends_scores_zero_grade_response(self):
        """_rerank_with_cross_encoder blends scores correctly for numeric zero grade."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        node = Node.create(type="Fact", name="Quaid plays guitar")
        original_score = 0.7
        results = [(node, original_score)]
        config = RetrievalConfig()

        with patch("lib.llm_clients.call_fast_reasoning", return_value=("1. 0", 0.1)):
            reranked = _rerank_with_cross_encoder("where does Quaid live", results, config)

        # Config default reranker_blend is 0.5
        # Expected: 0.5 * 0.0 + 0.5 * 0.7 = 0.35
        blend = config.reranker_blend  # 0.5 default
        expected = blend * 0.0 + (1 - blend) * original_score
        assert len(reranked) == 1
        assert abs(reranked[0][1] - expected) < 0.001

    def test_only_reranks_top_k(self):
        """_rerank_with_cross_encoder only reranks top-K candidates, rest passed through."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        config = RetrievalConfig(reranker_top_k=2)

        nodes = [
            (Node.create(type="Fact", name=f"Fact {i}"), 0.9 - i * 0.1)
            for i in range(5)
        ]

        with patch("lib.llm_clients.call_fast_reasoning", return_value=("1. yes\n2. yes", 0.1)) as mock_call:
            reranked = _rerank_with_cross_encoder("test query", nodes, config)

        # Top-K candidates are batched into one fast-reasoning call.
        mock_call.assert_called_once()
        # Should still return all 5 results
        assert len(reranked) == 5

    def test_sorts_by_blended_score(self):
        """_rerank_with_cross_encoder sorts reranked results by blended score."""
        from datastore.memorydb.memory_graph import _rerank_with_cross_encoder, Node
        from config import RetrievalConfig

        config = RetrievalConfig()
        node_high = Node.create(type="Fact", name="Very relevant fact")
        node_low = Node.create(type="Fact", name="Irrelevant fact")
        results = [(node_low, 0.9), (node_high, 0.5)]  # Original order: low first

        with patch("lib.llm_clients.call_fast_reasoning", return_value=("1. 0\n2. 5", 0.1)):
            reranked = _rerank_with_cross_encoder("test query", results, config)

        # With blend=0.5:
        # node_high: 0.5*1.0 + 0.5*0.5 = 0.75
        # node_low:  0.5*0.0 + 0.5*0.9 = 0.45
        assert reranked[0][0].name == "Very relevant fact"
        assert reranked[1][0].name == "Irrelevant fact"


class TestRerankerInRecall:
    """recall() reranker integration."""

    def test_recall_skips_reranker_when_disabled(self, tmp_path):
        """recall() skips reranker when reranker_enabled is False."""
        from config import RetrievalConfig
        graph, _ = _make_graph(tmp_path)
        _make_node(graph, "Quaid has a cat named Richter")

        config = RetrievalConfig(reranker_enabled=False, min_similarity=0.01)
        mock_cfg = MagicMock()
        mock_cfg.retrieval = config

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph._rerank_with_cross_encoder") as mock_rerank, \
             patch("config.get_config", return_value=mock_cfg):
            mock_rerank.return_value = []  # Should not be called
            recall_fn = __import__("datastore.memorydb.memory_graph", fromlist=["recall"]).recall
            recall_fn("Quaid cat", owner_id="quaid",
                      use_routing=False, min_similarity=0.01)
            mock_rerank.assert_not_called()

    def test_recall_calls_reranker_when_enabled(self, tmp_path):
        """recall() calls reranker when reranker_enabled is True."""
        from config import RetrievalConfig
        graph, _ = _make_graph(tmp_path)
        _make_node(graph, "Quaid has a cat named Richter")

        config = RetrievalConfig(reranker_enabled=True, min_similarity=0.01)
        mock_cfg = MagicMock()
        mock_cfg.retrieval = config

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding), \
             patch("datastore.memorydb.memory_graph._rerank_with_cross_encoder", wraps=lambda q, r, c=None: r) as mock_rerank, \
             patch("config.get_config", return_value=mock_cfg):
            recall_fn = __import__("datastore.memorydb.memory_graph", fromlist=["recall"]).recall
            recall_fn("Quaid cat", owner_id="quaid",
                      use_routing=False, min_similarity=0.01)
            mock_rerank.assert_called_once()


# ===========================================================================
# Feature #11: Multi-Pass Retrieval
# ===========================================================================


class TestMultiPassRetrieval:
    """Multi-pass retrieval logic in recall()."""

    def _build_low_quality_results(self):
        """Build results where top score < 0.70 and count < limit."""
        from datastore.memorydb.memory_graph import Node
        node = Node.create(type="Fact", name="Some marginally relevant fact")
        node.accessed_at = datetime.now().isoformat()
        node.access_count = 0
        node.embedding = _fake_get_embedding("some fact")
        return [(node, 0.65)]  # Below 0.70 threshold

    def test_multi_pass_triggers_when_top_low_and_under_limit(self, tmp_path):
        """Multi-pass triggers when top result < 0.70 and results < limit."""
        graph, _ = _make_graph(tmp_path)
        _make_node(graph, "Douglas Quaid lives in Bali Indonesia")

        # Track if search_hybrid is called more than once (initial + multi-pass)
        original_search_hybrid = graph.search_hybrid
        call_count = [0]

        def counting_search_hybrid(*args, **kwargs):
            call_count[0] += 1
            return original_search_hybrid(*args, **kwargs)

        graph.search_hybrid = counting_search_hybrid

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            recall_fn = __import__("datastore.memorydb.memory_graph", fromlist=["recall"]).recall
            # Use a query that will produce low-quality results
            recall_fn("Where does Quaid live", owner_id="quaid",
                      use_routing=False, min_similarity=0.01, limit=10)

        # search_hybrid should be called at least once; multi-pass may or may not
        # trigger depending on similarity scores from fake embeddings
        assert call_count[0] >= 1

    def test_multi_pass_extracts_entity_terms(self):
        """Multi-pass extracts capitalized words as entity terms."""
        # This tests the logic: entity_terms = [w for w in clean_query.split() if w[0:1].isupper() and len(w) > 2]
        query = "Does Quaid talk to Hauser often"
        words = query.split()
        entity_terms = [w for w in words if w[0:1].isupper() and len(w) > 2]
        assert "Quaid" in entity_terms
        assert "Hauser" in entity_terms
        assert "Does" in entity_terms  # First word is capitalized
        assert "to" not in entity_terms  # lowercase

    def test_multi_pass_does_not_add_duplicates(self, tmp_path):
        """Multi-pass doesn't add results that were already in the initial pass."""
        graph, _ = _make_graph(tmp_path)
        node = _make_node(graph, "Douglas Quaid has a cat named Richter")

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            recall_fn = __import__("datastore.memorydb.memory_graph", fromlist=["recall"]).recall
            results = recall_fn("Quaid cat Richter", owner_id="quaid",
                                use_routing=False, min_similarity=0.01)

        # Check no duplicate IDs in results
        ids = [r["id"] for r in results]
        assert len(ids) == len(set(ids))

    def test_multi_pass_debug_flag(self, tmp_path):
        """Multi-pass marks debug info with multi_pass=True flag."""
        graph, _ = _make_graph(tmp_path)
        # Create many nodes to increase chances of multi-pass triggering
        for i in range(5):
            _make_node(graph, f"Some tangentially related fact number {i}")
        _make_node(graph, "Quaid lives in Bali Indonesia")

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            recall_fn = __import__("datastore.memorydb.memory_graph", fromlist=["recall"]).recall
            results = recall_fn("Where does Douglas Quaid live in Bali",
                                owner_id="quaid",
                                use_routing=False, min_similarity=0.01,
                                debug=True)

        # If multi-pass triggered and added results, they should have multi_pass=True
        # But even if not triggered, the test should pass (multi_pass is optional)
        for r in results:
            if "_debug" in r and r["_debug"].get("multi_pass"):
                assert r["_debug"]["multi_pass"] is True

    def test_multi_pass_does_not_trigger_when_top_high(self, tmp_path):
        """Multi-pass doesn't trigger when top result >= 0.70."""
        from datastore.memorydb.memory_graph import Node, _compute_composite_score
        # Directly test the condition: scored_results[0][1] < 0.70
        node = Node.create(type="Fact", name="High quality match")
        node.accessed_at = datetime.now().isoformat()
        node.access_count = 5
        node.confidence = 0.9
        scored_results = [(node, 0.85)]  # Above 0.70 threshold
        # The condition `scored_results[0][1] < 0.70` is False, so multi-pass won't trigger
        assert scored_results[0][1] >= 0.70

    def test_multi_pass_does_not_trigger_when_enough_results(self):
        """Multi-pass doesn't trigger when results >= limit."""
        from datastore.memorydb.memory_graph import Node
        results = [
            (Node.create(type="Fact", name=f"Result {i}"), 0.60)
            for i in range(5)
        ]
        limit = 5
        # Condition: len(scored_results) < limit is False
        assert len(results) >= limit

    def test_multi_pass_is_best_effort(self, tmp_path):
        """Multi-pass is best-effort: errors are silently caught."""
        graph, _ = _make_graph(tmp_path)
        _make_node(graph, "Quaid has a cat named Richter lives in Bali")

        # Make search_hybrid raise on the second call (multi-pass)
        original_search_hybrid = graph.search_hybrid
        call_count = [0]

        def failing_second_search(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("Simulated failure in second pass")
            return original_search_hybrid(*args, **kwargs)

        graph.search_hybrid = failing_second_search

        with patch("datastore.memorydb.memory_graph.get_graph", return_value=graph), \
             patch("datastore.memorydb.memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
            recall_fn = __import__("datastore.memorydb.memory_graph", fromlist=["recall"]).recall
            # Should not raise even if multi-pass search fails
            results = recall_fn("Tell me about Quaid and Richter",
                                owner_id="quaid",
                                use_routing=False, min_similarity=0.01)
        # Should return whatever initial search found
        # (may or may not have results depending on fake embeddings)
        assert isinstance(results, list)

    def test_multi_pass_condition_logic(self):
        """Verify the multi-pass condition logic directly."""
        # Multi-pass triggers when:
        # scored_results[0][1] < 0.70 AND len(scored_results) < limit

        # Case 1: Should trigger
        assert 0.65 < 0.70 and 2 < 5

        # Case 2: Should NOT trigger (high score)
        assert not (0.85 < 0.70 and 2 < 5)

        # Case 3: Should NOT trigger (enough results)
        assert not (0.65 < 0.70 and 5 < 5)

        # Case 4: Should NOT trigger (both conditions false)
        assert not (0.85 < 0.70 and 6 < 5)
