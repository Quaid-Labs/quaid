import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datastore.memorydb import maintenance_ops


def test_batch_extract_edges_returns_empty_when_llm_returns_no_edges():
    facts = [{"id": "fact-1", "text": "Diana has a daughter named Alice"}]
    metrics = maintenance_ops.JanitorMetrics()

    with patch.object(
        maintenance_ops,
        "call_deep_reasoning",
        return_value=('[{"fact": 1, "edges": []}]', 0.05),
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert len(results) == 1
    assert len(results[0]) == 0


def test_batch_extract_edges_uses_llm_edges_for_kinship_pattern():
    facts = [{"id": "fact-2", "text": "Diana has a daughter named Alice", "owner_id": "default"}]
    metrics = maintenance_ops.JanitorMetrics()
    response = (
        '[{"fact": 1, "edges": ['
        '{"subject":"Diana","subject_type":"Person","relation":"parent_of","object":"Alice","object_type":"Person"}'
        ']}]',
        0.05,
    )

    with patch.object(maintenance_ops, "call_deep_reasoning", return_value=response), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert len(results) == 1
    assert len(results[0]) == 1
    edge = results[0][0]
    assert edge["subject"] == "Diana"
    assert edge["relation"] == "parent_of"
    assert edge["object"] == "Alice"


def test_batch_extract_edges_maps_user_alias_via_fact_owner_resolution():
    facts = [{"id": "fact-4", "text": "David is the user's brother.", "owner_id": "default"}]
    metrics = maintenance_ops.JanitorMetrics()

    response = (
        '[{"fact": 1, "edges": ['
        '{"subject":"David","subject_type":"Person","relation":"sibling_of","object":"the user","object_type":"Person"}'
        ']}]',
        0.05,
    )

    with patch.object(maintenance_ops, "call_deep_reasoning", return_value=response), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ):
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    assert len(results) == 1
    assert len(results[0]) == 1
    edge = results[0][0]
    assert edge["subject"] == "David"
    assert edge["relation"] == "sibling_of"
    assert edge["object"] == "Solomon Steadman"


def test_batch_extract_edges_resolves_owner_once_per_fact_not_per_edge():
    facts = [{"id": "fact-5", "text": "David is the user's brother and Lisa is the user's spouse.", "owner_id": "default"}]
    metrics = maintenance_ops.JanitorMetrics()

    response = (
        '[{"fact": 1, "edges": ['
        '{"subject":"David","subject_type":"Person","relation":"sibling_of","object":"the user","object_type":"Person"},'
        '{"subject":"Lisa","subject_type":"Person","relation":"spouse_of","object":"the user","object_type":"Person"}'
        ']}]',
        0.05,
    )

    with patch.object(maintenance_ops, "call_deep_reasoning", return_value=response), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=SimpleNamespace(name="Solomon Steadman")
    ) as resolve_owner:
        results = maintenance_ops.batch_extract_edges(
            facts=facts,
            graph=object(),
            metrics=metrics,
            relations_list="parent_of, sibling_of, spouse_of",
        )

    # One owner lookup for single-owner prompt guidance, one for the fact itself.
    assert resolve_owner.call_count == 2
    assert len(results) == 1
    assert len(results[0]) == 2


def test_owner_full_name_logs_when_owner_resolution_fails_and_not_fail_hard():
    fake_cfg = SimpleNamespace(
        users=SimpleNamespace(
            default_owner="default",
            identities={"default": SimpleNamespace(person_node_name="Config Owner")},
        )
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "resolve_owner_person", side_effect=RuntimeError("db unavailable")
    ), patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=False), patch.object(
        maintenance_ops.logger, "warning"
    ) as warn:
        out = maintenance_ops._owner_full_name("default")

    assert out == "Config Owner"
    assert warn.call_count == 1


def test_owner_full_name_raises_when_owner_resolution_fails_and_fail_hard():
    fake_cfg = SimpleNamespace(
        users=SimpleNamespace(
            default_owner="default",
            identities={"default": SimpleNamespace(person_node_name="Config Owner")},
        )
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "resolve_owner_person", side_effect=RuntimeError("db unavailable")
    ), patch.object(maintenance_ops, "is_fail_hard_enabled", return_value=True):
        try:
            maintenance_ops._owner_full_name("default")
            assert False, "expected RuntimeError in fail-hard mode"
        except RuntimeError as exc:
            assert "Unable to resolve owner person" in str(exc)


def test_owner_full_name_falls_back_to_top_level_owner_name_for_unresolved_slug():
    fake_cfg = SimpleNamespace(
        owner_name="Solomon Steadman",
        users=SimpleNamespace(
            default_owner="solomon-steadman",
            identities={},
        ),
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=None
    ):
        out = maintenance_ops._owner_full_name("solomon-steadman")

    assert out == "Solomon Steadman"


def test_owner_full_name_falls_back_to_configured_slug_display_name_when_unresolved():
    fake_cfg = SimpleNamespace(
        users=SimpleNamespace(
            default_owner="solomon-steadman",
            identities={},
        ),
    )
    with patch.object(maintenance_ops, "_cfg", fake_cfg), patch.object(
        maintenance_ops, "resolve_owner_person", return_value=None
    ):
        out = maintenance_ops._owner_full_name("solomon-steadman")

    assert out == "Solomon Steadman"
