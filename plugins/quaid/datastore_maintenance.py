"""Narrow datastore facade for non-datastore modules.

This surface is intentionally small. Janitor and datastore-owned maintenance
routines should import datastore internals directly from
`datastore.memorydb.*`, not through this facade.
"""

from datastore.memorydb.memory_graph import (
    get_graph,
    MemoryGraph,
    Node,
    store as store_memory,
    create_edge,
)

__all__ = [
    "get_graph",
    "MemoryGraph",
    "Node",
    "store_memory",
    "create_edge",
]
