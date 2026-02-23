"""Datastore maintenance facade for lifecycle/janitor orchestration.

Provides a single import surface over memory_graph maintenance primitives so
lifecycle orchestration code does not bind to memory_graph internals directly.
"""

from memory_graph import (
    get_graph,
    MemoryGraph,
    Node,
    Edge,
    store as store_memory,
    store_contradiction,
    get_pending_contradictions,
    resolve_contradiction,
    mark_contradiction_false_positive,
    soft_delete,
    get_recent_dedup_rejections,
    resolve_dedup_review,
    queue_for_decay_review,
    get_pending_decay_reviews,
    resolve_decay_review,
    ensure_keywords_for_relation,
    get_edge_keywords,
    delete_edges_by_source_fact,
    create_edge,
    content_hash,
    hard_delete_node,
    store_edge_keywords,
)

__all__ = [
    "get_graph",
    "MemoryGraph",
    "Node",
    "Edge",
    "store_memory",
    "store_contradiction",
    "get_pending_contradictions",
    "resolve_contradiction",
    "mark_contradiction_false_positive",
    "soft_delete",
    "get_recent_dedup_rejections",
    "resolve_dedup_review",
    "queue_for_decay_review",
    "get_pending_decay_reviews",
    "resolve_decay_review",
    "ensure_keywords_for_relation",
    "get_edge_keywords",
    "delete_edges_by_source_fact",
    "create_edge",
    "content_hash",
    "hard_delete_node",
    "store_edge_keywords",
]
