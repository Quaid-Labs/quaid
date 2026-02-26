"""Narrow datastore facade for non-datastore modules.

This surface is intentionally small. Janitor and datastore-owned maintenance
routines should import datastore internals directly from
`datastore.memorydb.*`, not through this facade.
"""

from datastore.memorydb.memory_graph import (
    store as store_memory,
    recall as recall_memories,
    search as search_memories,
    stats as datastore_stats,
    forget as forget_memory,
    get_memory as get_memory_by_id,
    create_edge,
)

__all__ = [
    "store_memory",
    "recall_memories",
    "search_memories",
    "datastore_stats",
    "forget_memory",
    "get_memory_by_id",
    "create_edge",
]
