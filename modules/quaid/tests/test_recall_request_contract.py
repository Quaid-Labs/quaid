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
from core.runtime.events import get_event_capability


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
        ("docs", RECALL_DOCS_REQUEST, "docsdb", "docs"),
        ("session_chunks", RECALL_MEMORY_REQUEST, "memorydb", "session_chunks"),
        ("vector_basic", RECALL_MEMORY_REQUEST, "memorydb", "vector_basic"),
        ("journal", RECALL_JOURNAL_REQUEST, "evolutiondb", "journal"),
        ("graph", RECALL_GRAPH_REQUEST, "memorydb", "graph"),
    ]


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
        "store": "vector_technical",
        "datastore_id": "memorydb",
        "options": {"domain": {"technical": True}},
    }


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
