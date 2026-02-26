#!/usr/bin/env python3
"""Quaid MCP Server — memory tools via Model Context Protocol.

Exposes Quaid's memory graph as MCP tools over stdio transport.
Works with Claude Desktop, Claude Code, Cursor, Windsurf, etc.

Usage:
    python3 mcp_server.py                     # stdio transport (default)
    QUAID_OWNER=alice python3 mcp_server.py  # set owner identity

Environment variables:
    QUAID_OWNER         Owner identity for stored memories (default: "default")
    MEMORY_DB_PATH      Override database path
    QUAID_HOME          Override Quaid home directory (default: ~/quaid/)
    OLLAMA_URL          Override Ollama endpoint
"""

import os
import sys
import json
import logging

# MCP uses stdout for JSON-RPC — redirect stdout to stderr before any imports
# to catch stray prints from memory_graph.py, lib/embeddings.py, etc.
_real_stdout = sys.stdout
sys.stdout = sys.stderr

# Suppress Quaid's own banner/status prints
os.environ["QUAID_QUIET"] = "1"

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mcp.server.fastmcp import FastMCP

from core.interface.api import (
    store,
    recall,
    search,
    create_edge,
    forget,
    get_memory,
    stats,
    projects_search_docs,
)
from ingest.extract import extract_from_transcript
from lib.runtime_context import (
    get_adapter_instance,
    get_llm_provider,
    get_sessions_dir,
    get_workspace_dir,
)
from lib.fail_policy import is_fail_hard_enabled

OWNER_ID = os.environ.get("QUAID_OWNER", "default")
logger = logging.getLogger(__name__)

mcp = FastMCP("quaid", instructions=(
    "Quaid is a persistent knowledge layer. Use memory_extract to extract memories "
    "from conversation transcripts, memory_write to perform canonical datastore writes, "
    "memory_store to save individual facts, "
    "memory_recall to retrieve relevant memories, memory_search for fast lookups, "
    "memory_create_edge to link entities, memory_forget to delete memories, "
    "memory_get to fetch by ID, memory_stats for database info, memory_capabilities "
    "for runtime capability discovery, projects_search "
    "to search project documentation, and memory_event_* tools for event bus actions. "
    "Memories persist across sessions."
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
def memory_recall(
    query: str,
    limit: int = 5,
    technical_scope: str = "any",
    min_similarity: float = 0.0,
    debug: bool = False,
    use_routing: bool = True,
    use_aliases: bool = True,
    use_intent: bool = True,
    use_multi_pass: bool = True,
    use_reranker: bool = True,
    date_from: str = "",
    date_to: str = "",
    actor_id: str = "",
    subject_entity_id: str = "",
    source_channel: str = "",
    source_conversation_id: str = "",
    source_author_id: str = "",
    viewer_entity_id: str = "",
    participant_entity_ids_json: str = "",
    include_unscoped: bool = True,
) -> list:
    """Recall memories matching a natural language query.

    Uses hybrid retrieval: vector similarity + full-text search + graph traversal,
    with optional HyDE expansion and LLM reranking.

    Args:
        query: Natural language query (e.g. "What are the user's hobbies?").
        limit: Maximum number of results (1-20).
        technical_scope: "personal", "technical", or "any".
        min_similarity: Optional floor (0.0 uses config/default behavior).
        debug: Include scoring breakdown payloads.
        use_routing: Enable intent/routing search heuristics.
        use_aliases: Resolve entity aliases before search.
        use_intent: Enable intent classifier branch.
        use_multi_pass: Allow second-pass retrieval.
        use_reranker: Enable LLM reranker.
        date_from/date_to: Optional YYYY-MM-DD range filter on created_at.

    Returns:
        List of memory dicts with text, category, similarity score, and related graph paths.
    """
    limit = max(1, min(limit, 20))
    technical_scope = (technical_scope or "any").strip().lower()
    if technical_scope not in {"personal", "technical", "any"}:
        technical_scope = "any"
    try:
        parsed_min_similarity = float(min_similarity) if min_similarity is not None else 0.0
    except (TypeError, ValueError):
        raise ValueError(f"invalid min_similarity: {min_similarity!r}")
    has_advanced = (
        bool(debug)
        or (min_similarity is not None and parsed_min_similarity > 0)
        or not bool(use_routing)
        or not bool(use_aliases)
        or not bool(use_intent)
        or not bool(use_multi_pass)
        or not bool(use_reranker)
        or bool(date_from.strip() if date_from else "")
        or bool(date_to.strip() if date_to else "")
        or bool(actor_id.strip() if actor_id else "")
        or bool(subject_entity_id.strip() if subject_entity_id else "")
        or bool(source_channel.strip() if source_channel else "")
        or bool(source_conversation_id.strip() if source_conversation_id else "")
        or bool(source_author_id.strip() if source_author_id else "")
        or bool(viewer_entity_id.strip() if viewer_entity_id else "")
        or bool(participant_entity_ids_json.strip() if participant_entity_ids_json else "")
        or not bool(include_unscoped)
    )

    # Fast path for common/default usage: stay on the stable API wrapper.
    if not has_advanced:
        return recall(query=query, owner_id=OWNER_ID, limit=limit, technical_scope=technical_scope)

    participant_entity_ids = None
    if participant_entity_ids_json and participant_entity_ids_json.strip():
        try:
            parsed = json.loads(participant_entity_ids_json)
            if not isinstance(parsed, list):
                raise ValueError("participant_entity_ids_json must decode to a JSON array")
            participant_entity_ids = [str(p).strip() for p in parsed if str(p).strip()]
        except Exception as e:
            raise ValueError(f"invalid participant_entity_ids_json: {e}")

    # Advanced path: still route through API boundary.
    return recall(
        query=query,
        owner_id=OWNER_ID,
        limit=limit,
        technical_scope=technical_scope,
        min_similarity=(parsed_min_similarity if parsed_min_similarity > 0 else None),
        debug=bool(debug),
        use_routing=bool(use_routing),
        use_aliases=bool(use_aliases),
        use_intent=bool(use_intent),
        use_multi_pass=bool(use_multi_pass),
        use_reranker=bool(use_reranker),
        date_from=(date_from.strip() if date_from else None),
        date_to=(date_to.strip() if date_to else None),
        actor_id=(actor_id.strip() if actor_id else None),
        subject_entity_id=(subject_entity_id.strip() if subject_entity_id else None),
        source_channel=(source_channel.strip().lower() if source_channel else None),
        source_conversation_id=(source_conversation_id.strip() if source_conversation_id else None),
        source_author_id=(source_author_id.strip() if source_author_id else None),
        viewer_entity_id=(viewer_entity_id.strip() if viewer_entity_id else None),
        participant_entity_ids=participant_entity_ids,
        include_unscoped=bool(include_unscoped),
    )


@mcp.tool()
def memory_write(
    datastore: str,
    action: str,
    payload_json: str,
) -> dict:
    """Canonical datastore write interface (adapter-friendly).

    Supported now:
      - datastore="vector", action="store_fact"
      - datastore="graph", action="create_edge"
    """
    import json as _json_w

    ds = str(datastore or "").strip().lower()
    act = str(action or "").strip().lower()
    try:
        payload = _json_w.loads(payload_json or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload_json must decode to a JSON object")
    except Exception as e:
        return {"error": f"invalid payload_json: {e}"}

    if ds == "vector" and act == "store_fact":
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"error": "payload.text is required for vector/store_fact"}
        raw_confidence = payload.get("confidence")
        if raw_confidence is None:
            raw_confidence = payload.get("extraction_confidence")
        try:
            confidence = float(raw_confidence if raw_confidence is not None else 0.5)
        except (TypeError, ValueError):
            return {"error": f"invalid confidence: {raw_confidence!r}"}
        return store(
            text=text,
            owner_id=OWNER_ID,
            category=str(payload.get("category") or "fact"),
            confidence=confidence,
            knowledge_type=str(payload.get("knowledge_type") or "fact"),
            source=str(payload.get("source") or "mcp"),
            source_type=str(payload.get("source_type") or "import"),
            pinned=bool(payload.get("pinned") or False),
            is_technical=bool(payload.get("is_technical") or False),
        )

    if ds == "graph" and act == "create_edge":
        subject_name = str(payload.get("subject_name") or "").strip()
        relation = str(payload.get("relation") or "").strip()
        object_name = str(payload.get("object_name") or "").strip()
        if not subject_name or not relation or not object_name:
            return {"error": "payload.subject_name, payload.relation, payload.object_name are required"}
        return create_edge(
            subject_name=subject_name,
            relation=relation,
            object_name=object_name,
            owner_id=OWNER_ID,
            source_fact_id=str(payload.get("source_fact_id") or "").strip() or None,
        )

    return {"error": f"unsupported datastore/action: {ds}/{act}"}


@mcp.tool()
def memory_search(
    query: str,
    limit: int = 10,
    viewer_entity_id: str = "",
    source_channel: str = "",
    source_conversation_id: str = "",
    source_author_id: str = "",
    subject_entity_id: str = "",
    participant_entity_ids_json: str = "",
) -> list:
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
    participant_entity_ids = None
    if participant_entity_ids_json and participant_entity_ids_json.strip():
        try:
            parsed = json.loads(participant_entity_ids_json)
            if not isinstance(parsed, list):
                raise ValueError("participant_entity_ids_json must decode to a JSON array")
            participant_entity_ids = [str(p).strip() for p in parsed if str(p).strip()]
        except Exception as e:
            raise ValueError(f"invalid participant_entity_ids_json: {e}")

    return search(
        query=query,
        owner_id=OWNER_ID,
        limit=limit,
        viewer_entity_id=(viewer_entity_id.strip() if viewer_entity_id else None),
        source_channel=(source_channel.strip().lower() if source_channel else None),
        source_conversation_id=(source_conversation_id.strip() if source_conversation_id else None),
        source_author_id=(source_author_id.strip() if source_author_id else None),
        subject_entity_id=(subject_entity_id.strip() if subject_entity_id else None),
        participant_entity_ids=participant_entity_ids,
    )


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
    return stats()


@mcp.tool()
def projects_search(query: str, limit: int = 5, project: str = "") -> dict:
    """Search project documentation using semantic RAG search.

    When a project is specified, the full PROJECT.md is included alongside
    RAG search results for complete project context.

    Args:
        query: Search query for documentation.
        limit: Maximum results (1-20).
        project: Optional project name to scope results (e.g. "quaid").

    Returns:
        Dict with 'chunks' (list of search results) and optionally 'project_md' (full PROJECT.md content).
    """
    limit = max(1, min(limit, 20))
    return projects_search_docs(query=query, limit=limit, project=(project or None))


@mcp.tool()
def session_recall(action: str = "list", session_id: str = "", limit: int = 5) -> dict:
    """List recent sessions or load a specific session's content.

    Args:
        action: "list" to show recent sessions, "load" to get a specific session.
        session_id: Session ID to load (required when action="load").
        limit: How many sessions to list (default 5, max 20).

    Returns:
        Dict with sessions list or session transcript content.
    """
    import json as _json
    from pathlib import Path as _Path

    _ws = get_workspace_dir()
    log_path = _ws / "data" / "extraction-log.json"

    extraction_log: dict = {}
    try:
        extraction_log = _json.loads(log_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("session_recall failed to parse extraction log %s: %s", log_path, exc)

    if action == "list":
        entries = [
            (sid, info) for sid, info in extraction_log.items()
            if isinstance(info, dict) and info.get("last_extracted_at")
        ]
        entries.sort(key=lambda x: x[1].get("last_extracted_at", ""), reverse=True)
        entries = entries[:max(1, min(limit, 20))]
        return {
            "sessions": [
                {
                    "session_id": sid,
                    "last_extracted_at": info.get("last_extracted_at"),
                    "message_count": info.get("message_count"),
                    "label": info.get("label"),
                    "topic_hint": info.get("topic_hint"),
                }
                for sid, info in entries
            ]
        }

    if action == "load" and session_id:
        import re as _re
        if not _re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', session_id):
            return {"error": "Invalid session_id format"}
        sessions_dir = get_sessions_dir()
        if not sessions_dir:
            if is_fail_hard_enabled():
                raise RuntimeError("Sessions directory not available.")
            return {"session_id": session_id, "fallback": True, "message": "Sessions directory not available."}
        sessions_dir = _Path(sessions_dir)
        session_path = sessions_dir / f"{session_id}.jsonl"
        if session_path.exists():
            transcript = get_adapter_instance().parse_session_jsonl(session_path)
            # Return last 10k chars
            truncated = transcript[-10000:] if len(transcript) > 10000 else transcript
            return {"session_id": session_id, "transcript": truncated, "truncated": len(transcript) > 10000}

        if is_fail_hard_enabled():
            raise RuntimeError("Session file not available.")
        return {"session_id": session_id, "fallback": True, "message": "Session file not available."}

    return {"error": "Provide action='list' or action='load' with session_id."}


@mcp.tool()
def memory_provider() -> str:
    """Show current LLM and embeddings provider status.

    Returns:
        JSON string with adapter type, LLM provider, embeddings provider,
        model profiles, and embedding dimensions.
    """
    import json as _json
    from lib.embeddings import get_embeddings_provider as _gep
    adapter = get_adapter_instance()
    try:
        llm = get_llm_provider()
        llm_name = type(llm).__name__
        profiles = llm.get_profiles()
    except Exception as e:
        llm_name = f"error: {e}"
        profiles = {}
    embed = _gep()
    return _json.dumps({
        "adapter": type(adapter).__name__,
        "llm_provider": llm_name,
        "llm_profiles": profiles,
        "embeddings_provider": type(embed).__name__,
        "embeddings_model": embed.model_name,
        "embeddings_dim": embed.dimension(),
    }, indent=2)


@mcp.tool()
def memory_capabilities() -> dict:
    """Return read/write/event capabilities for runtime orchestration."""
    from core.runtime.events import get_event_registry
    return {
        "owner_id": OWNER_ID,
        "recall": {
            "technical_scope": ["personal", "technical", "any"],
            "supports": [
                "min_similarity", "debug", "use_routing", "use_aliases",
                "use_intent", "use_multi_pass", "use_reranker", "date_from", "date_to",
            ],
        },
        "writes": [
            {
                "datastore": "vector",
                "action": "store_fact",
                "payload_keys": [
                    "text", "category", "confidence", "knowledge_type",
                    "source", "source_type", "pinned", "is_technical",
                ],
            },
            {
                "datastore": "graph",
                "action": "create_edge",
                "payload_keys": ["subject_name", "relation", "object_name", "source_fact_id"],
            },
        ],
        "events": get_event_registry(),
    }


@mcp.tool()
def memory_event_emit(
    name: str,
    payload_json: str = "{}",
    source: str = "mcp",
    session_id: str = "",
    priority: str = "normal",
    dispatch: str = "auto",
) -> dict:
    """Emit a runtime event into Quaid's queue.

    Args:
        name: Event name (e.g. "session.reset", "notification.delayed").
        payload_json: JSON object string payload.
        source: Event source label.
        session_id: Optional session id.
        priority: low|normal|high.
        dispatch: auto|immediate|queued. In auto mode, active events are processed
            immediately while passive events stay queued for async handling.
    """
    from core.runtime.events import emit_event, process_events, get_event_capability
    import json as _json2

    try:
        payload = _json2.loads(payload_json) if payload_json else {}
        if not isinstance(payload, dict):
            raise ValueError("payload_json must decode to an object")
    except Exception as e:
        return {"error": f"invalid payload_json: {e}"}

    mode = str(dispatch or "auto").strip().lower()
    if mode not in {"auto", "immediate", "queued"}:
        mode = "auto"

    capability = get_event_capability(name) or {}
    delivery_mode = str(capability.get("delivery_mode") or "active").strip().lower()

    event = emit_event(
        name=name,
        payload=payload,
        source=source,
        session_id=session_id or None,
        owner_id=OWNER_ID,
        priority=priority,
    )

    should_process = (
        mode == "immediate" or
        (mode == "auto" and delivery_mode == "active")
    )
    processed = None
    if should_process:
        processed = process_events(limit=1, names=[str(name)])

    return {
        "event": event,
        "delivery_mode": delivery_mode,
        "dispatch": mode,
        "processed": processed,
    }


@mcp.tool()
def memory_event_list(status: str = "pending", limit: int = 20) -> dict:
    """List queued runtime events.

    Args:
        status: pending|processed|failed|all.
        limit: Max events to return.
    """
    from core.runtime.events import list_events
    return {"events": list_events(status=status, limit=max(1, min(limit, 200)))}


@mcp.tool()
def memory_event_process(limit: int = 20, names_csv: str = "") -> dict:
    """Process pending events with registered handlers.

    Args:
        limit: Max events to process.
        names_csv: Optional comma-separated event names to process.
    """
    from core.runtime.events import process_events
    names = [x.strip() for x in (names_csv or "").split(",") if x.strip()]
    return process_events(limit=max(1, min(limit, 200)), names=names)


@mcp.tool()
def memory_event_capabilities() -> dict:
    """List event capabilities this runtime can emit/process/listen to.

    Includes per-event delivery_mode (active|passive) for orchestration.
    """
    from core.runtime.events import get_event_registry
    return {"events": get_event_registry()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.stdout = _real_stdout  # Restore for MCP JSON-RPC protocol
    mcp.run(transport="stdio")
