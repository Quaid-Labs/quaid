"""Tests for entity summary node generation (generate_entity_summary, summarize_all_entities)."""

import os
import sys
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

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
    import hashlib
    h = hashlib.md5(text.encode()).digest()
    return [float(b) / 255.0 for b in h] * 8  # 128-dim


def _make_graph(tmp_path):
    """Create a MemoryGraph backed by a temp SQLite file."""
    from memory_graph import MemoryGraph
    db_file = tmp_path / "test.db"
    with patch("memory_graph._lib_get_embedding", side_effect=_fake_get_embedding):
        graph = MemoryGraph(db_path=db_file)
    return graph, db_file


def _add_edge(graph, source_id, target_id, relation):
    """Helper to add an edge using the proper Edge.create() API."""
    from memory_graph import Edge
    edge = Edge.create(source_id=source_id, target_id=target_id, relation=relation)
    graph.add_edge(edge)
    return edge


def _setup_entity_with_facts(graph, entity_name="Shannon", entity_type="Person", owner="solomon"):
    """Create a Person/Place/Concept entity with some connected facts."""
    from memory_graph import Node

    # Create entity node
    entity = Node.create(type=entity_type, name=entity_name, owner_id=owner)
    entity.embedding = _fake_get_embedding(entity_name)
    graph.add_node(entity)

    # Create some fact nodes
    facts = [
        f"{entity_name} works as a software engineer",
        f"{entity_name} loves hiking in the mountains",
        f"{entity_name} has a golden retriever named Max",
    ]
    fact_nodes = []
    for fact_text in facts:
        fact = Node.create(type="Fact", name=fact_text, owner_id=owner)
        fact.embedding = _fake_get_embedding(fact_text)
        graph.add_node(fact)
        fact_nodes.append(fact)

    # Create edges connecting entity to facts
    for fact_node in fact_nodes:
        _add_edge(graph, entity.id, fact_node.id, "has_fact")

    return entity, fact_nodes


# ---------------------------------------------------------------------------
# get_entity_summary()
# ---------------------------------------------------------------------------

class TestGetEntitySummary:
    """Tests for get_entity_summary() — retrieving stored summaries."""

    def test_returns_none_for_nonexistent_node(self, tmp_path):
        from memory_graph import get_entity_summary
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph):
            result = get_entity_summary("nonexistent-id")
            assert result is None

    def test_returns_none_when_no_summary(self, tmp_path):
        from memory_graph import get_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        node = Node.create(type="Person", name="TestPerson")
        node.embedding = _fake_get_embedding("TestPerson")
        graph.add_node(node)
        with patch("memory_graph.get_graph", return_value=graph):
            result = get_entity_summary(node.id)
            assert result is None

    def test_returns_stored_summary(self, tmp_path):
        from memory_graph import get_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        node = Node.create(type="Person", name="TestPerson", attributes={"summary": "A test person."})
        node.embedding = _fake_get_embedding("TestPerson")
        graph.add_node(node)
        with patch("memory_graph.get_graph", return_value=graph):
            result = get_entity_summary(node.id)
            assert result == "A test person."


# ---------------------------------------------------------------------------
# generate_entity_summary() — no-LLM (concatenation) mode
# ---------------------------------------------------------------------------

class TestGenerateEntitySummaryNoLLM:
    """Tests for generate_entity_summary() with use_llm=False (fallback concatenation)."""

    def test_returns_none_for_nonexistent_node(self, tmp_path):
        from memory_graph import generate_entity_summary
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary("nonexistent-id", use_llm=False)
            assert result is None

    def test_returns_none_for_fact_node(self, tmp_path):
        from memory_graph import generate_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        fact = Node.create(type="Fact", name="Some fact about something")
        fact.embedding = _fake_get_embedding("Some fact")
        graph.add_node(fact)
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(fact.id, use_llm=False)
            assert result is None

    def test_returns_none_for_entity_with_no_facts(self, tmp_path):
        from memory_graph import generate_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        person = Node.create(type="Person", name="Lonely Person")
        person.embedding = _fake_get_embedding("Lonely Person")
        graph.add_node(person)
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(person.id, use_llm=False)
            assert result is None

    def test_generates_summary_from_edge_connected_facts(self, tmp_path):
        from memory_graph import generate_entity_summary
        graph, _ = _make_graph(tmp_path)
        entity, fact_nodes = _setup_entity_with_facts(graph)
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(entity.id, use_llm=False)
            assert result is not None
            assert "Shannon" in result
            # Should contain content from the facts
            for fn in fact_nodes:
                assert fn.name in result

    def test_generates_summary_from_name_matching_facts(self, tmp_path):
        from memory_graph import generate_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        # Create entity without edges
        entity = Node.create(type="Person", name="Alice")
        entity.embedding = _fake_get_embedding("Alice")
        graph.add_node(entity)
        # Create facts mentioning Alice (no edges)
        fact = Node.create(type="Fact", name="Alice enjoys painting watercolors")
        fact.embedding = _fake_get_embedding("Alice enjoys painting")
        graph.add_node(fact)
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(entity.id, use_llm=False)
            assert result is not None
            assert "Alice" in result
            assert "painting watercolors" in result

    def test_stores_summary_in_node_attributes(self, tmp_path):
        from memory_graph import generate_entity_summary
        graph, _ = _make_graph(tmp_path)
        entity, _ = _setup_entity_with_facts(graph)
        with patch("memory_graph.get_graph", return_value=graph):
            generate_entity_summary(entity.id, use_llm=False)
            # Read node back from DB
            updated_node = graph.get_node(entity.id)
            assert "summary" in updated_node.attributes
            assert "summary_updated_at" in updated_node.attributes
            assert "summary_fact_count" in updated_node.attributes
            assert updated_node.attributes["summary_fact_count"] >= 3

    def test_works_with_place_type(self, tmp_path):
        from memory_graph import generate_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        place = Node.create(type="Place", name="Portland")
        place.embedding = _fake_get_embedding("Portland")
        graph.add_node(place)
        fact = Node.create(type="Fact", name="Portland is known for its coffee culture")
        fact.embedding = _fake_get_embedding("Portland coffee")
        graph.add_node(fact)
        _add_edge(graph, place.id, fact.id, "has_fact")
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(place.id, use_llm=False)
            assert result is not None
            assert "Portland" in result

    def test_works_with_concept_type(self, tmp_path):
        from memory_graph import generate_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        concept = Node.create(type="Concept", name="Machine Learning")
        concept.embedding = _fake_get_embedding("Machine Learning")
        graph.add_node(concept)
        fact = Node.create(type="Fact", name="Machine Learning is used in recommendation systems")
        fact.embedding = _fake_get_embedding("ML recs")
        graph.add_node(fact)
        _add_edge(graph, concept.id, fact.id, "has_fact")
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(concept.id, use_llm=False)
            assert result is not None
            assert "Machine Learning" in result

    def test_caps_at_10_facts_in_concatenation(self, tmp_path):
        from memory_graph import generate_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        entity = Node.create(type="Person", name="Prolific")
        entity.embedding = _fake_get_embedding("Prolific")
        graph.add_node(entity)
        # Create 15 facts
        for i in range(15):
            fact = Node.create(type="Fact", name=f"Prolific fact number {i} about something")
            fact.embedding = _fake_get_embedding(f"fact {i}")
            graph.add_node(fact)
            _add_edge(graph, entity.id, fact.id, "has_fact")
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(entity.id, use_llm=False)
            assert result is not None
            # Should mention remaining facts
            assert "more facts" in result

    def test_deduplicates_edge_and_name_matched_facts(self, tmp_path):
        from memory_graph import generate_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        entity = Node.create(type="Person", name="Bob")
        entity.embedding = _fake_get_embedding("Bob")
        graph.add_node(entity)
        # Create a fact that is both edge-connected AND mentions entity name
        fact = Node.create(type="Fact", name="Bob likes pizza")
        fact.embedding = _fake_get_embedding("Bob pizza")
        graph.add_node(fact)
        _add_edge(graph, entity.id, fact.id, "has_fact")
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(entity.id, use_llm=False)
            assert result is not None
            # Should only contain the fact once
            assert result.count("Bob likes pizza") == 1

    def test_includes_preference_and_event_types(self, tmp_path):
        from memory_graph import generate_entity_summary, Node
        graph, _ = _make_graph(tmp_path)
        entity = Node.create(type="Person", name="Carol")
        entity.embedding = _fake_get_embedding("Carol")
        graph.add_node(entity)
        pref = Node.create(type="Preference", name="Carol prefers dark chocolate")
        pref.embedding = _fake_get_embedding("Carol pref")
        graph.add_node(pref)
        event = Node.create(type="Event", name="Carol graduated in 2020")
        event.embedding = _fake_get_embedding("Carol event")
        graph.add_node(event)
        _add_edge(graph, entity.id, pref.id, "prefers")
        _add_edge(graph, entity.id, event.id, "participated_in")
        with patch("memory_graph.get_graph", return_value=graph):
            result = generate_entity_summary(entity.id, use_llm=False)
            assert result is not None
            assert "dark chocolate" in result
            assert "graduated" in result


# ---------------------------------------------------------------------------
# generate_entity_summary() — LLM mode
# ---------------------------------------------------------------------------

class TestGenerateEntitySummaryWithLLM:
    """Tests for generate_entity_summary() with use_llm=True (mocked LLM)."""

    def test_uses_llm_when_available(self, tmp_path):
        from memory_graph import generate_entity_summary
        graph, _ = _make_graph(tmp_path)
        entity, _ = _setup_entity_with_facts(graph)
        mock_response = "Shannon is a software engineer who loves hiking and has a golden retriever named Max."
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._HAS_LLM_CLIENTS", True), \
             patch("memory_graph.call_low_reasoning", return_value=(mock_response, {"usage": {}})):
            result = generate_entity_summary(entity.id, use_llm=True)
            assert result == mock_response

    def test_falls_back_on_llm_failure(self, tmp_path):
        from memory_graph import generate_entity_summary
        graph, _ = _make_graph(tmp_path)
        entity, _ = _setup_entity_with_facts(graph)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._HAS_LLM_CLIENTS", True), \
             patch("memory_graph.call_low_reasoning", side_effect=Exception("API error")):
            result = generate_entity_summary(entity.id, use_llm=True)
            # Should still get a concatenation-based summary
            assert result is not None
            assert "Shannon" in result

    def test_falls_back_when_llm_not_available(self, tmp_path):
        from memory_graph import generate_entity_summary
        graph, _ = _make_graph(tmp_path)
        entity, _ = _setup_entity_with_facts(graph)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._HAS_LLM_CLIENTS", False):
            result = generate_entity_summary(entity.id, use_llm=True)
            # Should get concatenation fallback
            assert result is not None
            assert "Shannon" in result

    def test_llm_summary_stored_in_attributes(self, tmp_path):
        from memory_graph import generate_entity_summary
        graph, _ = _make_graph(tmp_path)
        entity, _ = _setup_entity_with_facts(graph)
        mock_response = "Shannon is a software engineer."
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._HAS_LLM_CLIENTS", True), \
             patch("memory_graph.call_low_reasoning", return_value=(mock_response, {"usage": {}})):
            generate_entity_summary(entity.id, use_llm=True)
            updated_node = graph.get_node(entity.id)
            assert updated_node.attributes["summary"] == mock_response
            assert "summary_updated_at" in updated_node.attributes
            assert updated_node.attributes["summary_fact_count"] >= 3

    def test_llm_empty_response_falls_back(self, tmp_path):
        from memory_graph import generate_entity_summary
        graph, _ = _make_graph(tmp_path)
        entity, _ = _setup_entity_with_facts(graph)
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._HAS_LLM_CLIENTS", True), \
             patch("memory_graph.call_low_reasoning", return_value=("", {"usage": {}})):
            result = generate_entity_summary(entity.id, use_llm=True)
            # Empty response should fall through to concatenation
            assert result is not None
            assert "Shannon" in result


# ---------------------------------------------------------------------------
# summarize_all_entities()
# ---------------------------------------------------------------------------

class TestSummarizeAllEntities:
    """Tests for summarize_all_entities() — batch summarization."""

    def test_summarizes_all_entity_types(self, tmp_path):
        from memory_graph import summarize_all_entities, Node
        graph, _ = _make_graph(tmp_path)
        # Create one of each type
        for name, typ in [("Alice", "Person"), ("Portland", "Place"), ("AI", "Concept")]:
            entity = Node.create(type=typ, name=name, owner_id="solomon")
            entity.embedding = _fake_get_embedding(name)
            graph.add_node(entity)
            fact = Node.create(type="Fact", name=f"{name} is interesting", owner_id="solomon")
            fact.embedding = _fake_get_embedding(f"{name} fact")
            graph.add_node(fact)
            _add_edge(graph, entity.id, fact.id, "has_fact")
        with patch("memory_graph.get_graph", return_value=graph):
            stats = summarize_all_entities(owner_id="solomon", use_llm=False)
            assert stats["total"] == 3
            assert stats["generated"] == 3
            assert stats["skipped"] == 0

    def test_filters_by_owner(self, tmp_path):
        from memory_graph import summarize_all_entities, Node
        graph, _ = _make_graph(tmp_path)
        # Create entities for different owners
        for owner in ["solomon", "other"]:
            entity = Node.create(type="Person", name=f"Person_{owner}", owner_id=owner)
            entity.embedding = _fake_get_embedding(f"Person_{owner}")
            graph.add_node(entity)
            fact = Node.create(type="Fact", name=f"Person_{owner} is a person", owner_id=owner)
            fact.embedding = _fake_get_embedding(f"Person_{owner} fact")
            graph.add_node(fact)
            _add_edge(graph, entity.id, fact.id, "has_fact")
        with patch("memory_graph.get_graph", return_value=graph):
            stats = summarize_all_entities(owner_id="solomon", use_llm=False)
            assert stats["total"] == 1
            assert stats["generated"] == 1

    def test_filters_by_entity_type(self, tmp_path):
        from memory_graph import summarize_all_entities, Node
        graph, _ = _make_graph(tmp_path)
        for name, typ in [("Alice", "Person"), ("Portland", "Place"), ("AI", "Concept")]:
            entity = Node.create(type=typ, name=name)
            entity.embedding = _fake_get_embedding(name)
            graph.add_node(entity)
            fact = Node.create(type="Fact", name=f"{name} exists")
            fact.embedding = _fake_get_embedding(f"{name} fact")
            graph.add_node(fact)
            _add_edge(graph, entity.id, fact.id, "has_fact")
        with patch("memory_graph.get_graph", return_value=graph):
            stats = summarize_all_entities(entity_types=["Person"], use_llm=False)
            assert stats["total"] == 1
            assert stats["generated"] == 1

    def test_skips_already_summarized_in_no_llm_mode(self, tmp_path):
        from memory_graph import summarize_all_entities, Node
        graph, _ = _make_graph(tmp_path)
        # Create entity that already has a summary
        entity = Node.create(type="Person", name="Bob", attributes={"summary": "Already summarized."})
        entity.embedding = _fake_get_embedding("Bob")
        graph.add_node(entity)
        fact = Node.create(type="Fact", name="Bob is great")
        fact.embedding = _fake_get_embedding("Bob fact")
        graph.add_node(fact)
        _add_edge(graph, entity.id, fact.id, "has_fact")
        with patch("memory_graph.get_graph", return_value=graph):
            stats = summarize_all_entities(use_llm=False)
            assert stats["total"] == 1
            assert stats["skipped"] == 1
            assert stats["generated"] == 0

    def test_regenerates_with_llm_even_if_summarized(self, tmp_path):
        from memory_graph import summarize_all_entities, Node
        graph, _ = _make_graph(tmp_path)
        entity = Node.create(type="Person", name="Bob", attributes={"summary": "Old summary."})
        entity.embedding = _fake_get_embedding("Bob")
        graph.add_node(entity)
        fact = Node.create(type="Fact", name="Bob likes coding")
        fact.embedding = _fake_get_embedding("Bob coding")
        graph.add_node(fact)
        _add_edge(graph, entity.id, fact.id, "has_fact")
        mock_response = "Bob is a coder."
        with patch("memory_graph.get_graph", return_value=graph), \
             patch("memory_graph._HAS_LLM_CLIENTS", True), \
             patch("memory_graph.call_low_reasoning", return_value=(mock_response, {"usage": {}})):
            stats = summarize_all_entities(use_llm=True)
            assert stats["generated"] == 1
            assert stats["skipped"] == 0

    def test_handles_empty_database(self, tmp_path):
        from memory_graph import summarize_all_entities
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph):
            stats = summarize_all_entities(use_llm=False)
            assert stats["total"] == 0
            assert stats["generated"] == 0
            assert stats["skipped"] == 0

    def test_returns_correct_stats_structure(self, tmp_path):
        from memory_graph import summarize_all_entities
        graph, _ = _make_graph(tmp_path)
        with patch("memory_graph.get_graph", return_value=graph):
            stats = summarize_all_entities(use_llm=False)
            assert "generated" in stats
            assert "skipped" in stats
            assert "failed" in stats
            assert "total" in stats
