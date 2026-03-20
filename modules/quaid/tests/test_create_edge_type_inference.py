import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MEMORY_DB_PATH"] = ":memory:"


def test_edge_type_inference_for_work_and_place_relations():
    from datastore.memorydb.memory_graph import _infer_edge_entity_type

    assert _infer_edge_entity_type("Jen", "works_at", is_subject=True) == "Person"
    assert _infer_edge_entity_type("TechFlow", "works_at", is_subject=False) == "Organization"
    assert _infer_edge_entity_type("Maya", "lives_in", is_subject=True) == "Person"
    assert _infer_edge_entity_type("Austin", "lives_in", is_subject=False) == "Place"


def test_edge_type_inference_for_social_relations():
    from datastore.memorydb.memory_graph import _infer_edge_entity_type

    assert _infer_edge_entity_type("Maya", "colleague_of", is_subject=True) == "Person"
    assert _infer_edge_entity_type("Priya", "colleague_of", is_subject=False) == "Person"
    assert _infer_edge_entity_type("Linda", "family_of", is_subject=True) == "Person"
    assert _infer_edge_entity_type("Sam", "knows", is_subject=False) == "Person"
