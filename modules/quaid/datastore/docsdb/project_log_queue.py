"""Durable project-log write queue for project-docs workers.

Runtime extractors enqueue project-log intent here. The project-docs worker is
the only component that drains the queue and writes visible project files.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from lib.fail_policy import is_fail_hard_enabled

logger = logging.getLogger(__name__)
_LOCK_STATE = threading.local()

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


def project_queue_lock_path(project: str, *, quaid_home: Optional[Path] = None) -> Path:
    return project_queue_dir(project, quaid_home=quaid_home) / ".drain.lock"


def project_queue_dead_letter_dir(project: str, *, quaid_home: Optional[Path] = None) -> Path:
    return project_queue_dir(project, quaid_home=quaid_home) / "dead-letter"


@contextlib.contextmanager
def project_queue_lock(project: str, *, quaid_home: Optional[Path] = None):
    """Serialize drain->visible-write->mark for one project's queue."""
    name = _validate_project(project)
    lock_path = project_queue_lock_path(project, quaid_home=quaid_home)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    locked = False
    try:
        try:
            import fcntl  # type: ignore

            fcntl.flock(handle, fcntl.LOCK_EX)
            locked = True
        except Exception:
            if is_fail_hard_enabled():
                raise
            logger.warning("Failed acquiring project-log queue lock for %s", project, exc_info=True)
        if not locked:
            yield
        else:
            held = getattr(_LOCK_STATE, "held_projects", set())
            _LOCK_STATE.held_projects = {*held, name}
            try:
                yield
            finally:
                _LOCK_STATE.held_projects = set(held)
    finally:
        release_exc: Optional[BaseException] = None
        if locked:
            try:
                import fcntl  # type: ignore

                fcntl.flock(handle, fcntl.LOCK_UN)
            except Exception as exc:
                if is_fail_hard_enabled():
                    release_exc = exc
                else:
                    logger.warning("Failed releasing project-log queue lock for %s", project, exc_info=True)
        try:
            handle.close()
        finally:
            if release_exc is not None:
                raise release_exc


def _assert_project_queue_lock_held(project: str) -> str:
    name = _validate_project(project)
    held = getattr(_LOCK_STATE, "held_projects", set())
    if name not in held:
        raise RuntimeError(f"project-log queue lock is required for {name}")
    return name


def _validate_project(project: str) -> str:
    name = unicodedata.normalize("NFKC", str(project or "")).casefold().strip()
    if not name:
        raise ValueError(f"Invalid project name for project-log queue: {project!r}")
    first, rest = name[0], name[1:]
    if not first.isalnum() or any(
        not (ch.isalnum() or unicodedata.category(ch).startswith("M") or ch in {"-", "_"})
        for ch in rest
    ):
        raise ValueError(f"Invalid project name for project-log queue: {project!r}")
    return name


def _normalize_entry_timestamp(raw: Any) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text = f"{text}T23:59:59"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
        return None


def _normalize_entries_with_dropped_count(entries: Iterable[Any]) -> Tuple[List[Dict[str, Any]], int]:
    out: List[Dict[str, Any]] = []
    dropped = 0
    seen: set[Tuple[str, str]] = set()
    for entry in entries or []:
        if isinstance(entry, dict):
            text = entry.get("text", entry.get("entry", entry.get("note", "")))
            created_at = (
                _normalize_entry_timestamp(entry.get("created_at"))
                or _normalize_entry_timestamp(entry.get("timestamp"))
                or _normalize_entry_timestamp(entry.get("date"))
            )
        else:
            text = entry
            created_at = None
        text = str(text or "").strip()
        if not text:
            dropped += 1
            continue
        text = re.sub(r"\s+", " ", text)
        key = (text, str(created_at or ""))
        if key in seen:
            continue
        seen.add(key)
        item: Dict[str, Any] = {"text": text}
        if created_at:
            item["created_at"] = created_at
        out.append(item)
    return out, dropped


def _normalize_entries(entries: Iterable[Any]) -> List[Dict[str, Any]]:
    return _normalize_entries_with_dropped_count(entries)[0]


def _is_invalid_quaid_now_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and str(exc).startswith("Invalid QUAID_NOW=")


def _utc_now() -> str:
    raw = os.environ.get("QUAID_NOW", "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.isoformat()
        except ValueError as exc:
            if is_fail_hard_enabled():
                raise RuntimeError(f"Invalid QUAID_NOW={raw!r}") from exc
            logger.warning("Invalid QUAID_NOW=%r; using wall clock", raw)
    return datetime.now(tz=timezone.utc).isoformat()


def _project_log_queue_id() -> str:
    return f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}"


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        with handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Failed cleaning up temp file %s: %s", tmp, exc)
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
    project_logs: Dict[str, List[Any]],
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

            created_at = _utc_now()
            item_id = _project_log_queue_id()
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
                "created_at": created_at,
            }
            target = project_queue_dir(project, quaid_home=quaid_home) / f"{item_id}.json"
            _atomic_write_json(target, payload)
            metrics["projects_queued"] += 1
            metrics["entries_queued"] += len(entries)
            metrics["entries_written"] += len(entries)
        except Exception as exc:
            if _is_invalid_quaid_now_error(exc):
                raise
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


def _dead_letter_queue_file(
    project: str,
    path: Path,
    reason: str,
    *,
    quaid_home: Optional[Path] = None,
) -> bool:
    if not path.exists():
        return False
    dead_dir = project_queue_dead_letter_dir(project, quaid_home=quaid_home)
    dead_dir.mkdir(parents=True, exist_ok=True)
    target = dead_dir / f"{_project_log_queue_id()}-{path.name}"
    path.replace(target)
    try:
        _atomic_write_json(
            target.with_name(f"{target.name}.metadata.json"),
            {
                "project": project,
                "original_file": path.name,
                "dead_lettered_at": _utc_now(),
                "reason": str(reason or "").strip() or "invalid project-log queue item",
            },
        )
    except Exception as exc:
        if _is_invalid_quaid_now_error(exc):
            raise
        logger.warning("Failed writing project-log dead-letter metadata for %s", target, exc_info=True)
        if is_fail_hard_enabled():
            raise
    logger.critical("Dead-lettered project-log queue item %s -> %s: %s", path, target, reason)
    return True


def dead_letter_project_log_queue_item(
    project: str,
    item_id: str,
    reason: str,
    *,
    quaid_home: Optional[Path] = None,
) -> bool:
    """Move a poison queue item out of the pending drain path for inspection."""
    project = _assert_project_queue_lock_held(project)
    item_id = str(item_id or "").strip()
    if not _ENTRY_ID_RE.fullmatch(item_id):
        raise ValueError(f"invalid queue item id: {item_id!r}")
    path = project_queue_dir(project, quaid_home=quaid_home) / f"{item_id}.json"
    return _dead_letter_queue_file(project, path, reason, quaid_home=quaid_home)


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
    project = _assert_project_queue_lock_held(project)
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
            raw_entries = data.get("entries") or []
            entries, dropped_entries = _normalize_entries_with_dropped_count(raw_entries)
            data["entries"] = entries
            data["_dropped_entries_count"] = dropped_entries
            data["_queue_path"] = str(path)
            items.append(data)
        except (ValueError, json.JSONDecodeError, UnicodeError) as exc:
            logger.warning("Failed reading project-log queue item %s: %s", path, exc)
            if is_fail_hard_enabled():
                raise
            _dead_letter_queue_file(expected_project, path, str(exc), quaid_home=quaid_home)
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
    project = _assert_project_queue_lock_held(project)
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
    project = _assert_project_queue_lock_held(project)
    removed = 0
    directory = project_queue_dir(project, quaid_home=quaid_home)
    try:
        for path in sorted(directory.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
                removed += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
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
