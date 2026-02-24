"""Core-owned bridge to datastore lifecycle/runtime operations.

Janitor and other core orchestrators should import datastore interactions from
this module instead of importing datastore modules directly.
"""

from datastore.memorydb.memory_graph import get_graph
from datastore.memorydb.maintenance_ops import (
    JanitorMetrics,
    backfill_embeddings,
    checkpoint_wal,
    count_nodes_by_status,
    get_update_check_cache,
    get_last_run_time,
    graduate_approved_to_active,
    init_janitor_metadata,
    list_recent_fact_texts,
    record_health_snapshot,
    record_janitor_run,
    write_update_check_cache,
    _is_benchmark_mode,
)

__all__ = [
    "JanitorMetrics",
    "backfill_embeddings",
    "checkpoint_wal",
    "count_nodes_by_status",
    "get_graph",
    "get_last_run_time",
    "get_update_check_cache",
    "graduate_approved_to_active",
    "init_janitor_metadata",
    "list_recent_fact_texts",
    "record_health_snapshot",
    "record_janitor_run",
    "write_update_check_cache",
    "_is_benchmark_mode",
]
