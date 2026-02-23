"""
Quaid Public API — Clean entry points for external use.

This module wraps the internal memory_graph functions with simplified
signatures suitable for plugin integrators and external callers.

Usage:
    from api import store, recall, search, create_edge, forget, get_memory

    result = store("User prefers dark mode", owner_id="quaid")
    memories = recall("UI preferences?", owner_id="quaid")
    results = search("dark mode", owner_id="quaid")
"""

from typing import Optional, List, Dict, Any

from memory_graph import (
    store as _store,
    recall as _recall,
    create_edge as _create_edge,
    get_graph,
    _forget as _internal_forget,
    _get_memory as _internal_get_memory,
    Node,
    Edge,
)


def store(
    text: str,
    owner_id: str,
    category: str = "fact",
    confidence: float = 0.5,
    verified: bool = False,
    pinned: bool = False,
    source: Optional[str] = None,
    knowledge_type: str = "fact",
    source_type: Optional[str] = None,
    is_technical: bool = False,
) -> Dict[str, Any]:
    """Store a new memory with automatic deduplication.

    Args:
        text: The fact or memory to store. Must be at least 3 words.
        owner_id: Owner identifier (e.g. "quaid"). Required.
        category: Memory category — "fact", "preference", "decision", "entity".
        confidence: Initial confidence level, 0.0 to 1.0. Default 0.5.
        verified: Mark as verified (higher trust, skips some review).
        pinned: Pinned memories never decay.
        source: Where this fact came from (e.g. "telegram", "manual").
        knowledge_type: "fact", "belief", "preference", or "experience".
        source_type: Who stated it — "user", "assistant", "tool", or "import".
        is_technical: True for technical/project-state memories.

    Returns:
        Dict with keys:
            id: The node ID (UUID string)
            status: "created", "duplicate", or "updated"
            similarity: Similarity score if duplicate was found
            existing_text: Text of the matched duplicate (if any)

    Raises:
        ValueError: If text is empty, too short, or owner_id is missing.

    Example:
        >>> result = store("Quaid lives on Mars", owner_id="quaid")
        >>> print(result["status"])  # "created"
        >>> print(result["id"])      # "a1b2c3..."
    """
    return _store(
        text=text,
        category=category,
        owner_id=owner_id,
        confidence=confidence,
        verified=verified,
        pinned=pinned,
        source=source,
        knowledge_type=knowledge_type,
        source_type=source_type,
        is_technical=is_technical,
    )


def recall(
    query: str,
    owner_id: str,
    limit: int = 5,
    min_similarity: Optional[float] = None,
    debug: bool = False,
    technical_scope: str = "any",
    use_routing: bool = True,
    use_aliases: bool = True,
    use_intent: bool = True,
    use_multi_pass: bool = True,
    use_reranker: bool = True,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Recall memories matching a natural language query.

    Uses hybrid retrieval: vector similarity (sqlite-vec) + full-text search
    (FTS5) + graph traversal, fused with Reciprocal Rank Fusion. Optionally
    applies HyDE query expansion and LLM reranking.

    Args:
        query: Natural language query (e.g. "What are Quaid's hobbies?").
        owner_id: Owner identifier to scope results.
        limit: Maximum number of results. Default 5.
        min_similarity: Minimum similarity threshold. None uses config default.
        debug: If True, include scoring breakdown in results.
        technical_scope: "personal", "technical", or "any".

    Returns:
        List of dicts, each with:
            id: Node ID
            text: The memory text
            category: Memory category
            similarity: Combined relevance score (0.0 to 1.0)
            confidence: Current confidence level
            source: Where the memory came from
            graph_context: Related facts from graph traversal (if any)

    Example:
        >>> memories = recall("hobbies", owner_id="quaid")
        >>> for m in memories:
        ...     print(f"{m['similarity']:.2f} {m['text']}")
    """
    return _recall(
        query=query,
        owner_id=owner_id,
        limit=limit,
        min_similarity=min_similarity,
        debug=debug,
        technical_scope=technical_scope,
        use_routing=use_routing,
        use_aliases=use_aliases,
        use_intent=use_intent,
        use_multi_pass=use_multi_pass,
        use_reranker=use_reranker,
        date_from=date_from,
        date_to=date_to,
    )


def search(
    query: str,
    owner_id: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Search memories using hybrid retrieval (simpler than recall).

    Unlike recall(), this skips HyDE expansion, intent classification,
    multi-pass retrieval, and reranking. Use this for fast, direct lookups.

    Args:
        query: Search query string.
        owner_id: Owner identifier to scope results.
        limit: Maximum results. Default 10.

    Returns:
        List of dicts with id, text, category, similarity, confidence.

    Example:
        >>> results = search("Mars colony", owner_id="quaid")
    """
    graph = get_graph()
    raw = graph.search_hybrid(query, limit=limit, owner_id=owner_id)
    return [
        {
            "id": node.id,
            "text": node.name,
            "category": node.type,
            "similarity": round(score, 4),
            "confidence": node.confidence,
            "owner_id": node.owner_id,
            "created_at": node.created_at,
        }
        for node, score in raw
    ]


def create_edge(
    subject_name: str,
    relation: str,
    object_name: str,
    owner_id: str,
    source_fact_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a relationship edge between two entities.

    Entities are looked up by name (case-insensitive, with fuzzy matching).
    If an entity doesn't exist, a Person node is created automatically.

    Args:
        subject_name: Source entity name (e.g. "Melina").
        relation: Relationship type (e.g. "spouse_of", "parent_of", "works_at").
            Normalized automatically (e.g. "child_of" → inverted to "parent_of").
        object_name: Target entity name (e.g. "Quaid").
        owner_id: Owner identifier for any newly created entities.
        source_fact_id: Optional ID of the fact that established this relationship.

    Returns:
        Dict with edge_id, status, and any created entity IDs.

    Example:
        >>> create_edge("Melina", "spouse_of", "Quaid", owner_id="quaid")
    """
    return _create_edge(
        subject_name=subject_name,
        relation=relation,
        object_name=object_name,
        source_fact_id=source_fact_id,
        owner_id=owner_id,
    )


def forget(
    node_id: Optional[str] = None,
    query: Optional[str] = None,
) -> bool:
    """Delete a memory by ID or by query match.

    If node_id is provided, deletes that specific memory.
    If query is provided, finds the best match and deletes it.

    Args:
        node_id: UUID of the memory to delete.
        query: Natural language query to find and delete the best match.

    Returns:
        True if a memory was deleted, False otherwise.

    Example:
        >>> forget(node_id="a1b2c3-...")
        True
        >>> forget(query="Mars colony preferences")
        True
    """
    return _internal_forget(query=query, node_id=node_id)


def get_memory(node_id: str) -> Optional[Dict[str, Any]]:
    """Get a single memory by its ID.

    Args:
        node_id: UUID of the memory.

    Returns:
        Dict with id, type, name, content, verified, pinned, confidence,
        owner_id, created_at, updated_at, attributes. None if not found.

    Example:
        >>> mem = get_memory("a1b2c3-...")
        >>> print(mem["name"])
    """
    return _internal_get_memory(node_id)


def stats() -> Dict[str, Any]:
    """Return graph-level statistics."""
    return get_graph().get_stats()


# Re-export types and graph accessor for advanced use
__all__ = [
    "store",
    "recall",
    "search",
    "create_edge",
    "forget",
    "get_memory",
    "stats",
    "get_graph",
    "Node",
    "Edge",
]
