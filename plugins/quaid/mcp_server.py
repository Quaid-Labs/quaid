#!/usr/bin/env python3
"""Quaid MCP Server — memory tools via Model Context Protocol.

Exposes Quaid's memory graph as MCP tools over stdio transport.
Works with Claude Desktop, Claude Code, Cursor, Windsurf, etc.

Usage:
    python3 mcp_server.py                     # stdio transport (default)
    QUAID_OWNER=solomon python3 mcp_server.py  # set owner identity

Environment variables:
    QUAID_OWNER         Owner identity for stored memories (default: "default")
    MEMORY_DB_PATH      Override database path
    CLAWDBOT_WORKSPACE  Override workspace root
    OLLAMA_URL          Override Ollama endpoint
"""

import os
import sys

# MCP uses stdout for JSON-RPC — redirect stdout to stderr before any imports
# to catch stray prints from memory_graph.py, lib/embeddings.py, etc.
_real_stdout = sys.stdout
sys.stdout = sys.stderr

# Suppress Quaid's own banner/status prints
os.environ["QUAID_QUIET"] = "1"

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from api import store, recall, search, create_edge, forget, get_memory, get_graph
from docs_rag import DocsRAG

OWNER_ID = os.environ.get("QUAID_OWNER", "default")

mcp = FastMCP("quaid", instructions=(
    "Quaid is a persistent memory system. Use memory_extract to extract memories "
    "from conversation transcripts, memory_store to save individual facts, "
    "memory_recall to retrieve relevant memories, memory_search for fast lookups, "
    "memory_create_edge to link entities, memory_forget to delete memories, "
    "memory_get to fetch by ID, memory_stats for database info, and docs_search "
    "to search project documentation. Memories persist across sessions."
))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def memory_extract(
    transcript: str,
    label: str = "mcp",
    dry_run: bool = False,
) -> dict:
    """Extract memories from a conversation transcript using Opus.

    Sends the transcript through the full extraction pipeline: Opus analyzes
    the text and extracts facts, relationship edges, soul snippets, and
    journal entries. Extracted facts are stored in the memory graph.

    Args:
        transcript: The conversation transcript (plain text, not JSONL).
        label: Source label for logging (default "mcp").
        dry_run: If true, parse and return results without storing.

    Returns:
        Dict with facts_stored, facts_skipped, edges_created, and extracted details.
    """
    from extract import extract_from_transcript
    return extract_from_transcript(
        transcript=transcript,
        owner_id=OWNER_ID,
        label=label,
        dry_run=dry_run,
    )


@mcp.tool()
def memory_store(
    text: str,
    category: str = "fact",
    confidence: float = 0.5,
    knowledge_type: str = "fact",
    source: str = "mcp",
    pinned: bool = False,
) -> dict:
    """Store a new memory in the knowledge graph.

    Args:
        text: The fact or memory to store (at least 3 words).
        category: Memory category — "fact", "preference", "decision", or "entity".
        confidence: Initial confidence level, 0.0 to 1.0.
        knowledge_type: "fact", "belief", "preference", or "experience".
        source: Where this fact came from (e.g. "user", "conversation").
        pinned: If true, this memory never decays.

    Returns:
        Dict with id, status ("created"/"duplicate"/"updated"), and similarity if duplicate.
    """
    return store(
        text=text,
        owner_id=OWNER_ID,
        category=category,
        confidence=confidence,
        knowledge_type=knowledge_type,
        source=source,
        source_type="import",
        pinned=pinned,
    )


@mcp.tool()
def memory_recall(query: str, limit: int = 5) -> list:
    """Recall memories matching a natural language query.

    Uses hybrid retrieval: vector similarity + full-text search + graph traversal,
    with optional HyDE expansion and LLM reranking.

    Args:
        query: Natural language query (e.g. "What are the user's hobbies?").
        limit: Maximum number of results (1-20).

    Returns:
        List of memory dicts with text, category, similarity score, and related graph paths.
    """
    limit = max(1, min(limit, 20))
    return recall(query=query, owner_id=OWNER_ID, limit=limit)


@mcp.tool()
def memory_search(query: str, limit: int = 10) -> list:
    """Search memories using fast hybrid retrieval (simpler than recall).

    Skips HyDE expansion, intent classification, multi-pass retrieval, and reranking.
    Use this for quick, direct lookups.

    Args:
        query: Search query string.
        limit: Maximum results (1-50).

    Returns:
        List of memory dicts with text, category, similarity, confidence.
    """
    limit = max(1, min(limit, 50))
    return search(query=query, owner_id=OWNER_ID, limit=limit)


@mcp.tool()
def memory_get(node_id: str) -> dict:
    """Get a single memory by its ID.

    Args:
        node_id: UUID of the memory node.

    Returns:
        Full memory dict with id, type, name, content, confidence, attributes, etc.
        Returns error dict if not found.
    """
    result = get_memory(node_id)
    if result is None:
        return {"error": f"Memory not found: {node_id}"}
    return result


@mcp.tool()
def memory_forget(node_id: str = "", query: str = "") -> dict:
    """Delete a memory by ID or by query match.

    Provide either node_id (exact) or query (finds best match and deletes it).

    Args:
        node_id: UUID of the memory to delete.
        query: Natural language query to find and delete the best match.

    Returns:
        Dict with deleted status.
    """
    if not node_id and not query:
        return {"error": "Provide either node_id or query"}
    deleted = forget(
        node_id=node_id if node_id else None,
        query=query if query else None,
    )
    return {"deleted": deleted}


@mcp.tool()
def memory_create_edge(subject_name: str, relation: str, object_name: str) -> dict:
    """Create a relationship edge between two entities.

    Entities are looked up by name (case-insensitive, fuzzy matching).
    If an entity doesn't exist, a Person node is created automatically.

    Args:
        subject_name: Source entity name (e.g. "Alice").
        relation: Relationship type (e.g. "spouse_of", "parent_of", "works_at").
        object_name: Target entity name (e.g. "Bob").

    Returns:
        Dict with edge_id, status, and any created entity IDs.
    """
    return create_edge(
        subject_name=subject_name,
        relation=relation,
        object_name=object_name,
        owner_id=OWNER_ID,
    )


@mcp.tool()
def memory_stats() -> dict:
    """Get database statistics.

    Returns:
        Dict with total_nodes, edges, counts by type and status, verified/unverified.
    """
    return get_graph().get_stats()


@mcp.tool()
def docs_search(query: str, limit: int = 5, project: str = "") -> list:
    """Search project documentation using semantic RAG search.

    Args:
        query: Search query for documentation.
        limit: Maximum results (1-20).
        project: Optional project name to scope results (e.g. "quaid").

    Returns:
        List of document chunk dicts with content, source_file, similarity.
    """
    limit = max(1, min(limit, 20))
    rag = DocsRAG()
    return rag.search_docs(
        query=query,
        limit=limit,
        project=project if project else None,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.stdout = _real_stdout  # Restore for MCP JSON-RPC protocol
    mcp.run(transport="stdio")
