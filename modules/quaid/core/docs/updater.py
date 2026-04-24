"""Core wrapper for docs updater datastore implementation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from datastore.docsdb import updater as _updater
from datastore.docsdb import project_log_queue as _project_log_queue
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


def append_project_logs(
    project_logs: dict[str, list[object]],
    trigger: str = "Compaction",
    date_str: str | None = None,
    dry_run: bool = False,
    *,
    index_history: bool = True,
    update_project_md: bool = True,
):
    return _append_project_logs(
        project_logs,
        trigger=trigger,
        date_str=date_str,
        dry_run=dry_run,
        index_history=index_history,
        update_project_md=update_project_md,
    )


def enqueue_project_logs(
    project_logs: dict[str, list[Any]],
    *,
    trigger: str = "CLI",
    date_str: str | None = None,
    session_id: str | None = None,
    owner_id: str | None = None,
    source_instance: str | None = None,
    source_adapter: str | None = None,
    dry_run: bool = False,
):
    return _project_log_queue.enqueue_project_logs(
        project_logs,
        trigger=trigger,
        date_str=date_str,
        session_id=session_id,
        owner_id=owner_id,
        source_instance=source_instance,
        source_adapter=source_adapter,
        dry_run=dry_run,
    )


def pending_project_log_count(project: str) -> int:
    return int(_project_log_queue.pending_project_log_count(project) or 0)


def drain_project_log_queue(project: str):
    return _project_log_queue.drain_project_log_queue(project)


def mark_project_log_queue_committed(project: str, item_ids: list[str]) -> dict[str, int]:
    return _project_log_queue.mark_project_log_queue_committed(project, item_ids)


def cleanup_project_log_queue(project: str) -> dict[str, int]:
    return _project_log_queue.cleanup_project_log_queue(project)


def update_registered_docs(
    project: str | None = None,
    dry_run: bool = False,
    protected_names: set[str] | None = None,
    *,
    index_project_logs_after: bool = True,
) -> int:
    """Update/reindex registered docs, optionally scoped to one project."""
    return _updater.cmd_update_stale(
        dry_run=dry_run,
        project=project,
        protected_names=protected_names,
        project_log_indexer=index_project_logs if index_project_logs_after else None,
    )


def _project_log_paths(project: str | None = None) -> list[str]:
    try:
        from core.project_registry import get_project, list_projects
    except ImportError:
        if _fail_hard_enabled():
            raise
        return []

    linked_projects, scope_resolved = _linked_projects_for_current_instance()
    if not scope_resolved:
        message = "cannot resolve instance linkage for PROJECT.log indexing"
        if _fail_hard_enabled():
            raise RuntimeError(message)
        logger.warning(
            "[project-docs] %s; skipping PROJECT.log indexing to avoid cross-instance contamination",
            message,
        )
        return []
    if project and str(project) not in linked_projects:
        return []

    try:
        if project:
            entry = get_project(str(project))
            projects = {str(project): entry} if entry else {}
        else:
            projects = list_projects()
            projects = {
                name: entry
                for name, entry in projects.items()
                if str(name) in linked_projects
            }
    except Exception as exc:
        logger.warning("[project-docs] skipped PROJECT.log discovery: %s", exc)
        if _fail_hard_enabled():
            raise
        return []

    paths: list[str] = []
    for name, entry in sorted(projects.items()):
        if not isinstance(entry, dict):
            continue
        log_path = _project_log_path_for_entry(str(name), entry)
        if log_path is None:
            continue
        if log_path.is_file():
            paths.append(str(log_path.resolve()))
    return paths


def _project_log_path_for_entry(name: str, entry: dict[str, Any]) -> Path | None:
    candidates: list[Path] = []
    canonical = str(entry.get("canonical_path") or "").strip()
    if canonical:
        candidates.append(Path(canonical).expanduser() / "PROJECT.log")
    managed = _managed_project_log_path(name)
    if managed is not None and managed not in candidates:
        candidates.append(managed)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if candidates else None


def _managed_project_log_path(name: str) -> Path | None:
    try:
        from core.project_docs import validate_project_name
        from lib.runtime_context import get_projects_dir

        return get_projects_dir() / validate_project_name(name) / "PROJECT.log"
    except Exception as exc:
        logger.warning("[project-docs] cannot resolve managed PROJECT.log path for %s: %s", name, exc)
        if _fail_hard_enabled():
            raise
        return None


def _linked_projects_for_current_instance() -> tuple[set[str], bool]:
    """Return linked projects for the active instance, if the instance can be resolved."""
    try:
        from datastore.docsdb.rag import _linked_projects_for_current_instance as _linked

        linked, resolved = _linked()
        return {str(name) for name in linked}, bool(resolved)
    except Exception:
        return set(), False


def index_project_logs(project: str | None = None) -> int:
    """Index append-only PROJECT.log files without making them editable docs."""
    try:
        from datastore.docsdb.rag import DocsRAG
    except ImportError:
        if _fail_hard_enabled():
            raise
        return 0

    paths = _project_log_paths(project)
    if not paths:
        return 0

    rag = DocsRAG()
    needs = rag.needs_reindex_many(paths)
    indexed = 0
    for file_path in paths:
        if not needs.get(file_path, True):
            continue
        try:
            chunks = int(rag.index_document(file_path) or 0)
            logger.info("[project-docs] indexed PROJECT.log: %s (%d chunks)", file_path, chunks)
            if chunks > 0:
                indexed += 1
        except Exception as exc:
            logger.warning("[project-docs] failed to index PROJECT.log %s: %s", file_path, exc)
            if _fail_hard_enabled():
                raise
    return indexed


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
    if index_project_logs(project=project):
        return True

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
    "enqueue_project_logs",
    "pending_project_log_count",
    "drain_project_log_queue",
    "mark_project_log_queue_committed",
    "cleanup_project_log_queue",
    "update_registered_docs",
    "index_project_logs",
    "index_one_stale_registered_doc",
    "sync_project_visible_docs",
]
