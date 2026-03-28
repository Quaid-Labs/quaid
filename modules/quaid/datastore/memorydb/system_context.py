"""MemoryDB-owned system-context metadata builders."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from datastore.memorydb.domain_registry import load_active_domains
from lib.config import get_db_path


def _resolve_db_path(db_path: Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    return get_db_path()


def active_domains(*, db_path: Path | None = None) -> list[str]:
    domains = load_active_domains(_resolve_db_path(db_path), bootstrap_if_empty=False)
    return sorted(str(key).strip() for key in domains.keys() if str(key).strip())


def list_relation_types() -> list[str]:
    """Small indirection to keep contract tests monkeypatchable."""
    from datastore.memorydb.memory_graph import list_relation_types as _list_relation_types

    return _list_relation_types()


def build_system_context_metadata(*, db_path: Path | None = None) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    domains = active_domains(db_path=db_path)
    relation_types: list[str] = []
    try:
        relation_types = list_relation_types()
    except Exception:
        relation_types = []
    if domains:
        entries.append(
            {
                "key": "domains",
                "label": "active domains",
                "value": ", ".join(domains),
                "order": 10,
            }
        )
    if relation_types:
        entries.append(
            {
                "key": "graph_relation_types",
                "label": "active graph relation types",
                "value": ", ".join(relation_types),
                "note": (
                    "Preinject does not cover graph structure or edge traversal. "
                    "If a query depends on these relations, use graph recall explicitly."
                ),
                "order": 20,
            }
        )
    return {"entries": entries}
