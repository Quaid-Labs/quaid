"""Core wrapper for docs updater datastore implementation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from datastore.docsdb import updater as _updater
from datastore.docsdb.project_updater import append_project_logs as _append_project_logs

logger = logging.getLogger(__name__)


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except Exception:
        return False


def check_staleness():
    return _updater.check_staleness()


def cmd_update_from_transcript(transcript_path: str, dry_run: bool = False, max_docs: int = 3):
    return _updater.cmd_update_from_transcript(transcript_path, dry_run=dry_run, max_docs=max_docs)


def append_project_logs(project_logs: dict[str, list[str]], trigger: str = "Compaction", dry_run: bool = False):
    return _append_project_logs(project_logs, trigger=trigger, dry_run=dry_run)


def update_registered_docs(
    project: str | None = None,
    dry_run: bool = False,
    protected_names: set[str] | None = None,
) -> int:
    """Update/reindex registered docs, optionally scoped to one project."""
    return _updater.cmd_update_stale(dry_run=dry_run, project=project, protected_names=protected_names)


def _resolve_registered_doc_path(registry: Any, file_path: str) -> Path:
    resolver = getattr(registry, "_resolve_path", None)
    if callable(resolver):
        return Path(resolver(file_path)).resolve()
    path = Path(file_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    try:
        from config import _workspace_root

        return (_workspace_root() / path).resolve()
    except Exception:
        return (Path.cwd() / path).resolve()


def index_one_stale_registered_doc(project: str | None = None) -> bool:
    """Index one stale registered doc from the supervisor-owned docs daemon path."""
    try:
        from datastore.docsdb.rag import DocsRAG
        from datastore.docsdb.registry import DocsRegistry
    except ImportError:
        if _fail_hard_enabled():
            raise
        return False

    registry = DocsRegistry()
    rag = DocsRAG()
    try:
        all_docs = registry.list_docs(project=project) if project else registry.list_docs()
    except TypeError:
        all_docs = registry.list_docs()

    candidate_paths: list[str] = []
    for entry in sorted(all_docs, key=lambda e: e.get("registered_at") or "", reverse=True):
        file_path = str(entry.get("file_path") or entry.get("path") or "").strip()
        if not file_path:
            continue
        try:
            resolved_path = _resolve_registered_doc_path(registry, file_path)
        except Exception as exc:
            logger.warning("Project docs stale-index skipped unresolved path %r: %s", file_path, exc)
            if _fail_hard_enabled():
                raise
            continue
        if resolved_path.exists():
            candidate_paths.append(str(resolved_path))

    if not candidate_paths:
        return False

    needs = rag.needs_reindex_many(candidate_paths)
    for file_path in candidate_paths:
        if not needs.get(file_path, True):
            continue
        try:
            chunks = rag.index_document(file_path)
            logger.info("[project-docs] indexed stale doc: %s (%d chunks)", file_path, chunks)
            return True
        except Exception as exc:
            logger.warning("[project-docs] failed to index stale doc %s: %s", file_path, exc)
            if _fail_hard_enabled():
                raise
            return False

    return False


def sync_project_visible_docs(project: str, canonical_path: str, *, root_docs: set[str], protected_names: set[str]) -> dict[str, int]:
    """Register new project docs and unregister visible docs removed by updater apply."""
    from datastore.docsdb.registry import DocsRegistry
    from datastore.docsdb.project_updater import refresh_project_md
    from lib.runtime_context import get_visible_quaid_home

    name = str(project or "").strip()
    canonical = Path(str(canonical_path or "")).resolve() if canonical_path else None
    visible_home = get_visible_quaid_home().resolve()
    registry = DocsRegistry()
    registered = 0
    unregistered = 0
    refreshed = 0

    if canonical is not None and canonical.is_dir():
        docs_to_register: list[Path] = []
        for root_name in sorted(root_docs):
            path = canonical / root_name
            if path.is_file():
                docs_to_register.append(path)
        docs_dir = canonical / "docs"
        if docs_dir.is_dir():
            docs_to_register.extend(p for p in sorted(docs_dir.rglob("*.md")) if p.is_file())
        for doc_path in docs_to_register:
            if doc_path.name in protected_names:
                continue
            try:
                rel_path = str(doc_path.resolve().relative_to(visible_home))
            except ValueError:
                rel_path = str(doc_path)
            if not registry.get(rel_path):
                registry.register(rel_path, project=name, asset_type="doc", registered_by="project-docs-worker")
                registered += 1

    for row in registry.list_docs(project=name):
        file_path = str(row.get("file_path") or "").strip()
        if not file_path:
            continue
        try:
            resolved = registry._resolve_path(file_path).resolve()  # registry-owned path semantics
        except Exception:
            continue
        if canonical is None:
            continue
        try:
            resolved.relative_to(canonical)
        except ValueError:
            continue
        if resolved.name in protected_names:
            continue
        if not resolved.exists() and registry.unregister(file_path):
            unregistered += 1

    refresh_project_md(name)
    refreshed = 1
    return {"registered": registered, "unregistered": unregistered, "project_md_refreshed": refreshed}


__all__ = [
    "check_staleness",
    "cmd_update_from_transcript",
    "append_project_logs",
    "update_registered_docs",
    "index_one_stale_registered_doc",
    "sync_project_visible_docs",
]
