import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb import maintenance_ops


def test_batch_extract_edges_falls_back_to_family_heuristic_on_empty_llm_result():
    facts = [{"id": "fact-1", "text": "Diana has a daughter named Alice"}]
    metrics = maintenance_ops.JanitorMetrics()

    with patch.object(
        maintenance_ops,
        "call_deep_reasoning",
        return_value=('[{"fact": 1, "edges": []}]', 0.05),
    ), patch.object(maintenance_ops, "_owner_full_name", return_value="Solomon Steadman"):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert len(results) == 1
    assert len(results[0]) == 1
    edge = results[0][0]
    assert edge["fact_id"] == "fact-1"
    assert edge["subject"] == "Diana"
    assert edge["relation"] == "parent_of"
    assert edge["object"] == "Alice"
    assert edge["subject_type"] == "Person"
    assert edge["object_type"] == "Person"


def test_family_heuristic_extracts_possessive_child_pattern():
    edges = maintenance_ops._heuristic_family_edges_for_fact(
        fact_id="fact-2",
        fact_text="Diana's daughter Alice just opened a ceramics studio.",
        owner_full="Solomon Steadman",
    )

    assert len(edges) == 1
    edge = edges[0]
    assert edge["subject"] == "Diana"
    assert edge["relation"] == "parent_of"
    assert edge["object"] == "Alice"


def test_family_heuristic_keeps_parent_stable_for_multi_child_sentence():
    edges = maintenance_ops._heuristic_family_edges_for_fact(
        fact_id="fact-3",
        fact_text="Amy has a daughter named Diana and also has a son named Bob.",
        owner_full="Solomon Steadman",
    )

    triples = {(edge["subject"], edge["relation"], edge["object"]) for edge in edges}
    assert ("Amy", "parent_of", "Diana") in triples
    assert ("Amy", "parent_of", "Bob") in triples
    assert ("Diana", "parent_of", "Bob") not in triples
