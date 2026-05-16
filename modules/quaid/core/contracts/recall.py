"""Brokered recall request contract metadata.

M4 defines how current user-facing recall selectors map to datastore request
events. This module does not dispatch production recall or call legacy recall
functions; it gives later milestones a single contract to activate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from core.datastore_registry import build_datastore_registry


@dataclass(frozen=True)
class RecallRequestRoute:
    selector: str
    event_type: str
    datastore_id: str
    handler_store: str
    aliases: Tuple[str, ...]


RECALL_MEMORY_REQUEST = "recall.memory.request.v1"
RECALL_GRAPH_REQUEST = "recall.graph.request.v1"
RECALL_DOCS_REQUEST = "recall.docs.request.v1"
RECALL_PROJECT_CONTEXT_REQUEST = "recall.project_context.request.v1"
RECALL_JOURNAL_REQUEST = "recall.journal.request.v1"


_RECALL_REQUEST_ROUTES: Tuple[RecallRequestRoute, ...] = (
    RecallRequestRoute(
        selector="vector",
        event_type=RECALL_MEMORY_REQUEST,
        datastore_id="memorydb",
        handler_store="vector",
        aliases=("memory", "facts"),
    ),
    RecallRequestRoute(
        selector="vector_basic",
        event_type=RECALL_MEMORY_REQUEST,
        datastore_id="memorydb",
        handler_store="vector_basic",
        aliases=("basic", "personal"),
    ),
    RecallRequestRoute(
        selector="vector_technical",
        event_type=RECALL_MEMORY_REQUEST,
        datastore_id="memorydb",
        handler_store="vector_technical",
        aliases=("technical", "project_memory"),
    ),
    RecallRequestRoute(
        selector="graph",
        event_type=RECALL_GRAPH_REQUEST,
        datastore_id="memorydb",
        handler_store="graph",
        aliases=("relations", "relationship", "relationships"),
    ),
    RecallRequestRoute(
        selector="session_chunks",
        event_type=RECALL_MEMORY_REQUEST,
        datastore_id="memorydb",
        handler_store="session_chunks",
        aliases=("source_chunks", "session", "transcript_chunks"),
    ),
    RecallRequestRoute(
        selector="docs",
        event_type=RECALL_DOCS_REQUEST,
        datastore_id="docsdb",
        handler_store="docs",
        aliases=("documents",),
    ),
    RecallRequestRoute(
        selector="project",
        event_type=RECALL_DOCS_REQUEST,
        datastore_id="docsdb",
        handler_store="docs",
        aliases=("projects", "project_docs"),
    ),
    RecallRequestRoute(
        selector="project_context",
        event_type=RECALL_PROJECT_CONTEXT_REQUEST,
        datastore_id="docsdb",
        handler_store="project_context",
        aliases=("system_context", "workspace_context"),
    ),
    RecallRequestRoute(
        selector="journal",
        event_type=RECALL_JOURNAL_REQUEST,
        datastore_id="evolutiondb",
        handler_store="journal",
        aliases=("evolution", "notedb"),
    ),
)


def _normalize_selector(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def list_recall_request_routes() -> List[RecallRequestRoute]:
    return list(_RECALL_REQUEST_ROUTES)


def _route_lookup() -> Dict[str, RecallRequestRoute]:
    lookup: Dict[str, RecallRequestRoute] = {}
    for route in _RECALL_REQUEST_ROUTES:
        lookup[route.selector] = route
        for alias in route.aliases:
            lookup[_normalize_selector(alias)] = route
    return lookup


def resolve_recall_request_routes(selectors: Iterable[Any]) -> List[RecallRequestRoute]:
    lookup = _route_lookup()
    routes: List[RecallRequestRoute] = []
    seen: set[Tuple[str, str]] = set()
    for raw in selectors:
        token = _normalize_selector(raw)
        if not token:
            continue
        route = lookup.get(token)
        if route is None:
            raise ValueError(f"unknown recall selector: {raw}")
        key = (route.event_type, route.handler_store)
        if key in seen:
            continue
        seen.add(key)
        routes.append(route)
    if not routes:
        raise ValueError("at least one recall selector is required")
    return routes


def build_recall_request_payload(
    *,
    query: str,
    route: RecallRequestRoute,
    limit: int,
    options: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    text = str(query or "").strip()
    if not text:
        raise ValueError("query is required")
    if int(limit) <= 0:
        raise ValueError("limit must be positive")
    return {
        "query": text,
        "limit": int(limit),
        "selector": route.selector,
        "store": route.handler_store,
        "datastore_id": route.datastore_id,
        "options": dict(options or {}),
    }


def validate_recall_request_routes_against_manifests(
    registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> List[str]:
    manifests = dict(registry or build_datastore_registry())
    errors: List[str] = []
    for route in _RECALL_REQUEST_ROUTES:
        manifest = manifests.get(route.datastore_id)
        if manifest is None:
            errors.append(f"{route.selector}: datastore not registered: {route.datastore_id}")
            continue
        request_handlers = set(manifest.get("request_handlers") or [])
        if route.event_type not in request_handlers:
            errors.append(
                f"{route.selector}: {route.datastore_id} does not declare request handler {route.event_type}"
            )
        recall_capabilities = set((manifest.get("capabilities") or {}).get("recall") or [])
        if route.handler_store not in recall_capabilities:
            errors.append(
                f"{route.selector}: {route.datastore_id} does not declare recall capability {route.handler_store}"
            )
    return errors
