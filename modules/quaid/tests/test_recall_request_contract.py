from __future__ import annotations

import pytest

from core.contracts.recall import (
    RECALL_DOCS_REQUEST,
    RECALL_GRAPH_REQUEST,
    RECALL_JOURNAL_REQUEST,
    RECALL_MEMORY_REQUEST,
    build_recall_request_payload,
    list_recall_request_routes,
    resolve_recall_request_routes,
    validate_recall_request_routes_against_manifests,
)
from core.runtime.events import emit_broker_event, get_event_capability, list_events, process_events
from lib.adapter import TestAdapter, reset_adapter, set_adapter


@pytest.fixture(autouse=True)
def _adapter_state():
    reset_adapter()
    yield
    reset_adapter()


def test_recall_request_routes_are_manifested_and_registered_events() -> None:
    assert validate_recall_request_routes_against_manifests() == []

    event_types = {route.event_type for route in list_recall_request_routes()}
    assert event_types == {
        RECALL_MEMORY_REQUEST,
        RECALL_GRAPH_REQUEST,
        RECALL_DOCS_REQUEST,
        "recall.project_context.request.v1",
        RECALL_JOURNAL_REQUEST,
    }
    for event_type in event_types:
        capability = get_event_capability(event_type)
        assert capability is not None
        assert capability["delivery_mode"] == "request"


def test_recall_request_selector_aliases_route_to_datastores() -> None:
    routes = resolve_recall_request_routes(
        ["project", "source_chunks", "vector_basic", "journal", "graph", "project"]
    )

    assert [(route.selector, route.event_type, route.datastore_id, route.handler_store) for route in routes] == [
        ("project", RECALL_DOCS_REQUEST, "docsdb", "docs"),
        ("session_chunks", RECALL_MEMORY_REQUEST, "memorydb", "session_chunks"),
        ("vector_basic", RECALL_MEMORY_REQUEST, "memorydb", "vector"),
        ("journal", RECALL_JOURNAL_REQUEST, "insightdb", "journal"),
        ("graph", RECALL_GRAPH_REQUEST, "memorydb", "graph"),
    ]

    assert resolve_recall_request_routes(["insight"])[0].selector == "journal"


def test_recall_request_payload_keeps_contract_shape() -> None:
    route = resolve_recall_request_routes(["vector_technical"])[0]

    payload = build_recall_request_payload(
        query="  what did I fix?  ",
        route=route,
        limit=7,
        options={"domain": {"technical": True}},
    )

    assert payload == {
        "query": "what did I fix?",
        "limit": 7,
        "selector": "vector_technical",
        "store": "vector",
        "datastore_id": "memorydb",
        "options": {"domain": {"technical": True}},
    }


def test_project_selector_round_trips_as_requested_selector() -> None:
    route = resolve_recall_request_routes(["project"])[0]

    payload = build_recall_request_payload(query="project docs", route=route, limit=3)

    assert route.selector == "project"
    assert route.handler_store == "docs"
    assert payload["selector"] == "project"
    assert payload["store"] == "docs"


def test_memory_selectors_share_vector_handler_store_but_remain_distinct() -> None:
    routes = resolve_recall_request_routes(["vector_basic", "vector_technical"])

    assert [(route.selector, route.handler_store) for route in routes] == [
        ("vector_basic", "vector"),
        ("vector_technical", "vector"),
    ]


def test_recall_request_contract_rejects_unknown_or_empty_selectors() -> None:
    with pytest.raises(ValueError, match="unknown recall selector"):
        resolve_recall_request_routes(["vector_basic", "missing-store"])

    with pytest.raises(ValueError, match="at least one recall selector"):
        resolve_recall_request_routes([""])


def test_recall_request_payload_requires_query_and_positive_limit() -> None:
    route = resolve_recall_request_routes(["docs"])[0]

    with pytest.raises(ValueError, match="query is required"):
        build_recall_request_payload(query="", route=route, limit=1)

    with pytest.raises(ValueError, match="limit must be positive"):
        build_recall_request_payload(query="docs", route=route, limit=0)


def test_unactivated_recall_request_fails_closed_when_dispatched(monkeypatch, tmp_path) -> None:
    import core.runtime.events as events

    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: False)
    set_adapter(TestAdapter(tmp_path))

    emit_broker_event(RECALL_MEMORY_REQUEST, {"query": "baratza"}, source="pytest")
    result = process_events(limit=1, names=[RECALL_MEMORY_REQUEST])

    assert result["processed"] == 0
    assert result["failed"] == 1
    failed = list_events(status="failed", limit=10)
    assert len(failed) == 1
    assert failed[0]["result"]["error"] == "recall.memory.request.v1 request handler not activated in M4"


def test_unactivated_recall_request_raises_under_fail_hard(monkeypatch, tmp_path) -> None:
    import core.runtime.events as events

    set_adapter(TestAdapter(tmp_path))
    emit_broker_event(RECALL_DOCS_REQUEST, {"query": "docs"}, source="pytest")
    monkeypatch.setattr(events, "_is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Event handler failed while fail-hard mode is enabled"):
        process_events(limit=1, names=[RECALL_DOCS_REQUEST])
