"""Narrow datastore facade for non-datastore modules.

This surface is intentionally small. Janitor and datastore-owned maintenance
routines should import datastore internals directly from
`datastore.memorydb.*`, not through this facade.
"""

from datastore.memorydb.memory_graph import (
    batch_write as batch_memory_write,
    store as store_memory,
    recall as recall_memories,
    recall_fast as recall_memories_fast,
    search as search_memories,
    warm_embedding_cache as warm_memory_embeddings,
    stats as datastore_stats,
    list_domains as list_memory_domains,
    register_domain as register_memory_domain,
    forget as forget_memory,
    get_memory as get_memory_by_id,
    create_edge,
    store_source_chunk as store_memory_source_chunk,
    store_source_chunks as store_memory_source_chunks,
    list_source_chunks as list_memory_source_chunks,
    get_source_chunk as get_memory_source_chunk,
    store_session_chunk as store_memory_session_chunk,
    store_session_chunks as store_memory_session_chunks,
    list_session_chunks as list_memory_session_chunks,
    get_session_chunk as get_memory_session_chunk,
)

__all__ = [
    "batch_memory_write",
    "store_memory",
    "recall_memories",
    "recall_memories_fast",
    "search_memories",
    "warm_memory_embeddings",
    "datastore_stats",
    "list_memory_domains",
    "register_memory_domain",
    "forget_memory",
    "get_memory_by_id",
    "create_edge",
    "store_memory_source_chunk",
    "store_memory_source_chunks",
    "list_memory_source_chunks",
    "get_memory_source_chunk",
    "store_memory_session_chunk",
    "store_memory_session_chunks",
    "list_memory_session_chunks",
    "get_memory_session_chunk",
]
