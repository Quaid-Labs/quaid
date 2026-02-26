import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb.memory_graph import Node
from datastore.memorydb import semantic_clustering


def test_call_ollama_clustering_warns_and_returns_empty_when_fail_hard_disabled(caplog):
    caplog.set_level("WARNING")
    with patch.object(semantic_clustering, "call_fast_reasoning", side_effect=RuntimeError("llm down")), \
         patch.object(semantic_clustering, "is_fail_hard_enabled", return_value=False):
        out = semantic_clustering.call_ollama_clustering("classify this")
    assert out == ""
    assert "semantic clustering LLM call failed" in caplog.text


def test_call_ollama_clustering_raises_when_fail_hard_enabled():
    with patch.object(semantic_clustering, "call_fast_reasoning", side_effect=RuntimeError("llm down")), \
         patch.object(semantic_clustering, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="Semantic clustering LLM call failed while fail-hard mode is enabled"):
            semantic_clustering.call_ollama_clustering("classify this")


def test_classify_node_semantic_cluster_logs_fallback_for_unknown_response(caplog):
    caplog.set_level("WARNING")
    node = Node.create(type="Fact", name="zxqvplrtn")
    with patch.object(semantic_clustering, "call_ollama_clustering", return_value="unknown-label"):
        cluster = semantic_clustering.classify_node_semantic_cluster(node)
    assert cluster == "technology"
    assert "semantic clustering fallback used" in caplog.text
