"""Durable project-log write queue for project-docs workers.

Runtime extractors enqueue project-log intent here. The project-docs worker is
the only component that drains the queue and writes visible project files.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from lib.fail_policy import is_fail_hard_enabled

logger = logging.getLogger(__name__)

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ENTRY_ID_RE = re.compile(r"^[0-9]+-[0-9]+-[a-f0-9-]+$")


def _quaid_home() -> Path:
    try:
        from lib.runtime_context import get_quaid_home

        return get_quaid_home()
    except Exception:
        if is_fail_hard_enabled():
            raise
        raw = os.environ.get("QUAID_HOME", "").strip()
        return Path(raw).expanduser().resolve() if raw else Path.home() / ".quaid"


def queue_root(quaid_home: Optional[Path] = None) -> Path:
    home = Path(quaid_home).expanduser().resolve() if quaid_home is not None else _quaid_home()
    return home / "data" / "project-docs" / "project-log-queue"


def project_queue_dir(project: str, *, quaid_home: Optional[Path] = None) -> Path:
    return queue_root(quaid_home) / _validate_project(project)


def _validate_project(project: str) -> str:
    name = str(project or "").strip()
    if not _PROJECT_RE.fullmatch(name):
        raise ValueError(f"Invalid project name for project-log queue: {project!r}")
    return name


def _normalize_entries(entries: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        text = str(entry or "").strip()
        if not text:
            continue
        text = re.sub(r"\s+", " ", text)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _metrics() -> Dict[str, int]:
    return {
        "projects_seen": 0,
        "projects_queued": 0,
        "entries_seen": 0,
        "entries_queued": 0,
        "entries_written": 0,
        "queue_failures": 0,
    }


def enqueue_project_logs(
    project_logs: Dict[str, List[str]],
    *,
    trigger: str,
    date_str: Optional[str] = None,
    session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    source_instance: Optional[str] = None,
    source_adapter: Optional[str] = None,
    dry_run: bool = False,
    quaid_home: Optional[Path] = None,
) -> Dict[str, int]:
    """Persist project-log write intent for the project-docs worker to commit."""
    metrics = _metrics()
    if not isinstance(project_logs, dict) or not project_logs:
        return metrics

    for raw_project, raw_entries in project_logs.items():
        metrics["projects_seen"] += 1
        try:
            project = _validate_project(str(raw_project or ""))
            entries = _normalize_entries(raw_entries or [])
            metrics["entries_seen"] += len(entries)
            if not entries:
                continue
            if dry_run:
                continue

            created_ns = time.time_ns()
            item_id = f"{created_ns}-{os.getpid()}-{uuid.uuid4().hex}"
            payload: Dict[str, Any] = {
                "id": item_id,
                "project": project,
                "entries": entries,
                "trigger": str(trigger or "CLI").strip() or "CLI",
                "date_str": str(date_str).strip() if date_str else None,
                "session_id": str(session_id).strip() if session_id else None,
                "owner_id": str(owner_id).strip() if owner_id else None,
                "source_instance": str(source_instance).strip() if source_instance else None,
                "source_adapter": str(source_adapter).strip() if source_adapter else None,
                "created_at": _utc_now(),
            }
            target = project_queue_dir(project, quaid_home=quaid_home) / f"{item_id}.json"
            _atomic_write_json(target, payload)
            metrics["projects_queued"] += 1
            metrics["entries_queued"] += len(entries)
        except Exception as exc:
            metrics["queue_failures"] += 1
            logger.warning("Failed to enqueue PROJECT.log entries for %r: %s", raw_project, exc)
            if is_fail_hard_enabled():
                raise
    return metrics


def pending_project_log_count(project: str, *, quaid_home: Optional[Path] = None) -> int:
    """Return pending queue item count for a project."""
    directory = project_queue_dir(project, quaid_home=quaid_home)
    try:
        return sum(1 for path in directory.glob("*.json") if path.is_file())
    except FileNotFoundError:
        return 0
    except Exception:
        if is_fail_hard_enabled():
            raise
        logger.warning("Failed counting project-log queue for %s", project, exc_info=True)
        return 0


def drain_project_log_queue(
    project: str,
    *,
    limit: Optional[int] = None,
    quaid_home: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Read pending queue items for a project in deterministic order.

    This does not remove queue files. Call mark_project_log_queue_committed()
    after the visible PROJECT.log/PROJECT.md commit succeeds.
    """
    directory = project_queue_dir(project, quaid_home=quaid_home)
    try:
        paths = sorted(path for path in directory.glob("*.json") if path.is_file())
    except FileNotFoundError:
        return []
    if limit is not None:
        paths = paths[: max(0, int(limit))]

    items: List[Dict[str, Any]] = []
    expected_project = _validate_project(project)
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("queue item must be a JSON object")
            item_id = str(data.get("id") or path.stem).strip()
            if not _ENTRY_ID_RE.fullmatch(item_id):
                raise ValueError(f"invalid queue item id: {item_id!r}")
            data["id"] = item_id
            item_project = _validate_project(str(data.get("project") or project))
            if item_project != expected_project:
                raise ValueError(f"queue item project mismatch: {item_project!r} != {expected_project!r}")
            data["project"] = item_project
            data["entries"] = _normalize_entries(data.get("entries") or [])
            data["_queue_path"] = str(path)
            items.append(data)
        except Exception as exc:
            logger.warning("Failed reading project-log queue item %s: %s", path, exc)
            if is_fail_hard_enabled():
                raise
    return items


def mark_project_log_queue_committed(
    project: str,
    item_ids: List[str],
    *,
    quaid_home: Optional[Path] = None,
) -> None:
    """Remove queue items after the project-docs worker commits them."""
    ids = {str(item_id or "").strip() for item_id in item_ids}
    ids = {item_id for item_id in ids if _ENTRY_ID_RE.fullmatch(item_id)}
    if not ids:
        return
    directory = project_queue_dir(project, quaid_home=quaid_home)
    for item_id in sorted(ids):
        try:
            (directory / f"{item_id}.json").unlink(missing_ok=True)
        except Exception:
            logger.warning("Failed removing committed project-log queue item %s/%s", project, item_id, exc_info=True)
            if is_fail_hard_enabled():
                raise


def cleanup_project_log_queue(project: str, *, quaid_home: Optional[Path] = None) -> Dict[str, int]:
    """Remove all queue files for a project."""
    removed = 0
    directory = project_queue_dir(project, quaid_home=quaid_home)
    try:
        for path in directory.glob("*"):
            if not path.is_file():
                continue
            path.unlink(missing_ok=True)
            removed += 1
        try:
            directory.rmdir()
        except OSError:
            pass
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("Failed cleaning project-log queue for %s", project, exc_info=True)
        if is_fail_hard_enabled():
            raise
    return {"removed": removed}
