"""Core-owned bridge to datastore lifecycle/runtime operations.

Janitor and other core orchestrators should import datastore interactions from
this module instead of importing datastore modules directly.
"""

from core.lifecycle.soul_snippets import write_journal_entry, write_snippet_entry


def _memory_graph_module():
    from datastore.memorydb import memory_graph

    return memory_graph


def _maintenance_ops_module():
    from datastore.memorydb import maintenance_ops

    return maintenance_ops


def _docs_rag_class():
    from datastore.docsdb.rag import DocsRAG as _DocsRAG

    return _DocsRAG


class JanitorMetrics:
    def __new__(cls, *args, **kwargs):
        return _maintenance_ops_module().JanitorMetrics(*args, **kwargs)


class DocsRAG:
    def __new__(cls, *args, **kwargs):
        return _docs_rag_class()(*args, **kwargs)


def get_graph(*args, **kwargs):
    return _memory_graph_module().get_graph(*args, **kwargs)


def backfill_edges(*args, **kwargs):
    return _maintenance_ops_module().backfill_edges(*args, **kwargs)


def backfill_embeddings(*args, **kwargs):
    return _maintenance_ops_module().backfill_embeddings(*args, **kwargs)


def checkpoint_wal(*args, **kwargs):
    return _maintenance_ops_module().checkpoint_wal(*args, **kwargs)


def count_nodes_by_status(*args, **kwargs):
    return _maintenance_ops_module().count_nodes_by_status(*args, **kwargs)


def get_update_check_cache(*args, **kwargs):
    return _maintenance_ops_module().get_update_check_cache(*args, **kwargs)


def get_last_run_time(*args, **kwargs):
    return _maintenance_ops_module().get_last_run_time(*args, **kwargs)


def get_last_successful_janitor_completed_at(*args, **kwargs):
    return _maintenance_ops_module().get_last_successful_janitor_completed_at(*args, **kwargs)


def graduate_approved_to_active(*args, **kwargs):
    return _maintenance_ops_module().graduate_approved_to_active(*args, **kwargs)


def init_janitor_metadata(*args, **kwargs):
    return _maintenance_ops_module().init_janitor_metadata(*args, **kwargs)


def list_recent_fact_texts(*args, **kwargs):
    return _maintenance_ops_module().list_recent_fact_texts(*args, **kwargs)


def record_health_snapshot(*args, **kwargs):
    return _maintenance_ops_module().record_health_snapshot(*args, **kwargs)


def record_janitor_run(*args, **kwargs):
    return _maintenance_ops_module().record_janitor_run(*args, **kwargs)


def write_update_check_cache(*args, **kwargs):
    return _maintenance_ops_module().write_update_check_cache(*args, **kwargs)


def is_benchmark_mode(*args, **kwargs):
    return _maintenance_ops_module().is_benchmark_mode(*args, **kwargs)

__all__ = [
    "JanitorMetrics",
    "backfill_edges",
    "backfill_embeddings",
    "checkpoint_wal",
    "count_nodes_by_status",
    "get_graph",
    "get_last_run_time",
    "get_last_successful_janitor_completed_at",
    "get_update_check_cache",
    "graduate_approved_to_active",
    "init_janitor_metadata",
    "list_recent_fact_texts",
    "record_health_snapshot",
    "record_janitor_run",
    "write_update_check_cache",
    "is_benchmark_mode",
    "DocsRAG",
    "write_journal_entry",
    "write_snippet_entry",
]
