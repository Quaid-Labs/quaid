import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb.memory_graph import Node
from datastore.memorydb import semantic_clustering


def test_call_clustering_llm_warns_and_returns_empty_when_fail_hard_disabled(caplog):
    caplog.set_level("WARNING")
    with patch.object(semantic_clustering, "call_fast_reasoning", side_effect=RuntimeError("llm down")), \
         patch.object(semantic_clustering, "is_fail_hard_enabled", return_value=False):
        out = semantic_clustering.call_clustering_llm("classify this")
    assert out == ""
    assert "semantic clustering LLM call failed" in caplog.text


def test_call_clustering_llm_raises_when_fail_hard_enabled():
    with patch.object(semantic_clustering, "call_fast_reasoning", side_effect=RuntimeError("llm down")), \
         patch.object(semantic_clustering, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Semantic clustering LLM call failed while fail-hard mode is enabled"):
            semantic_clustering.call_clustering_llm("classify this")


def test_classify_node_semantic_cluster_logs_fallback_for_unknown_response(caplog):
    caplog.set_level("WARNING")
    node = Node.create(type="Fact", name="zxqvplrtn")
    with patch.object(semantic_clustering, "call_clustering_llm", return_value="unknown-label"):
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    assert cluster == "uncategorized"
    assert "semantic clustering fallback used" in caplog.text


# ---------------------------------------------------------------------------
# classify_node_semantic_cluster — heuristic paths (no LLM call needed)
# ---------------------------------------------------------------------------


def test_classify_person_type_returns_people():
    node = Node.create(type="Person", name="Alice Smith")
    # Person type matches "people" cluster types — LLM should NOT be called
    with patch.object(semantic_clustering, "call_clustering_llm") as mock_llm:
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    mock_llm.assert_not_called()
    assert cluster == "people"


def test_classify_place_type_returns_places():
    node = Node.create(type="Place", name="Quaid HQ")
    with patch.object(semantic_clustering, "call_clustering_llm") as mock_llm:
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    mock_llm.assert_not_called()
    assert cluster == "places"


def test_classify_preference_type_returns_preferences():
    node = Node.create(type="Preference", name="likes coffee")
    with patch.object(semantic_clustering, "call_clustering_llm") as mock_llm:
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    mock_llm.assert_not_called()
    assert cluster == "preferences"


def test_classify_event_type_returns_events():
    node = Node.create(type="Event", name="team meeting on Monday")
    with patch.object(semantic_clustering, "call_clustering_llm") as mock_llm:
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    mock_llm.assert_not_called()
    assert cluster == "events"


def test_classify_concept_with_tech_keyword_returns_technology():
    node = Node.create(type="Concept", name="uses Python database for storage")
    with patch.object(semantic_clustering, "call_clustering_llm") as mock_llm:
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    mock_llm.assert_not_called()
    assert cluster == "technology"


def test_classify_fact_with_keyword_match_skips_llm():
    """Keyword match on Fact type without LLM (heuristic wins)."""
    node = Node.create(type="Fact", name="lives in Portland")
    with patch.object(semantic_clustering, "call_clustering_llm") as mock_llm:
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    mock_llm.assert_not_called()
    assert cluster == "places"


def test_classify_llm_response_parsed_to_cluster():
    """LLM response of 'people' is correctly mapped to 'people' cluster."""
    node = Node.create(type="Fact", name="zxqvplrtn_unique_gibberish")
    with patch.object(semantic_clustering, "call_clustering_llm", return_value="people"):
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    assert cluster == "people"


def test_call_clustering_llm_returns_stripped_result():
    """Successful LLM response is stripped of whitespace."""
    with patch.object(semantic_clustering, "call_fast_reasoning", return_value=("  technology  ", {})):
        out = semantic_clustering.call_clustering_llm("test prompt")
    assert out == "technology"
