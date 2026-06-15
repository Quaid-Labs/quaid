"""DocsDB-owned system-context metadata builders."""

from __future__ import annotations

import logging
import os
from typing import Any

from core.project_registry import list_projects

logger = logging.getLogger(__name__)


def current_instance_id() -> str:
    try:
        from lib.adapter import get_adapter

        instance = str(get_adapter().instance_id() or "").strip()
        if instance:
            return instance
    except Exception as exc:
        logger.debug("Failed resolving docsdb system-context adapter instance id: %s", exc)
    return str(os.environ.get("QUAID_INSTANCE", "") or "").strip()


def linked_projects(*, instance_id: str | None = None) -> list[dict[str, str]]:
    current_instance = str(instance_id or current_instance_id()).strip()
    rows: list[dict[str, str]] = []
    for name, entry in (list_projects() or {}).items():
        path_str = str(entry.get("canonical_path", "") or "").strip()
        instances = [
            str(item).strip()
            for item in list(entry.get("instances", []) or [])
            if str(item).strip()
        ]
        if not path_str:
            continue
        if current_instance:
            if not instances or current_instance not in instances:
                continue
        rows.append({"name": str(name).strip(), "path": path_str})
    rows.sort(key=lambda row: (0 if row["name"] == "quaid" else 1, row["name"]))
    return rows


def build_system_context_metadata(*, instance_id: str | None = None) -> dict[str, Any]:
    projects = linked_projects(instance_id=instance_id)
    if not projects:
        return {}
    rendered = "; ".join(f"{item['name']} ({item['path']})" for item in projects)
    return {
        "entries": [
            {
                "key": "linked_projects",
                "label": "linked projects",
                "value": rendered,
                "note": (
                    "Preinject does not cover project or docs detail. "
                    "MANDATORY ORDER: For project document questions, run docs recall before filesystem grep/cat "
                    "(for example: quaid recall \"<query>\" '{\"stores\":[\"docs\"],\"project\":\"<project-name>\"}'). "
                    "Use filesystem reads only when docs recall returns no relevant hits, weak hits, or only index/catalog rows; "
                    "for read-only one-fact lookups, read the catalog-listed file directly without linking."
                ),
                "order": 30,
            }
        ]
    }
