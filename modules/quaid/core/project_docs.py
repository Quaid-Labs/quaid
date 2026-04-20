"""Project documentation supervisor state and force-update primitives.

This module owns hidden operational state for project-doc updates. Visible
project files remain user-facing artifacts; worker cursors, force requests,
heartbeats, and locks live under QUAID_HOME/data/project-docs/.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PROJECT_LOG = "PROJECT.log"
UPDATABLE_ROOT_DOCS = {"PROJECT.md", "TOOLS.md", "AGENTS.md"}
SUPERVISOR_ROLE = "project-docs-supervisor"
WORKER_ROLE = "project-docs-worker"

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def validate_project_name(project: str) -> str:
    name = str(project or "").strip()
    if not _PROJECT_RE.match(name):
        raise ValueError(f"Invalid project name: {project!r}")
    return name


def get_quaid_home() -> Path:
    """Return QUAID_HOME without adapter bootstrap side effects."""
    raw = os.environ.get("QUAID_HOME", "").strip()
    return Path(raw).expanduser().resolve() if raw else Path.home() / ".quaid"


def quaid_tracking_dir(quaid_home: Path) -> Path:
    """Shadow git tracking base directory."""
    return quaid_home / ".git-tracking"


def project_docs_root(quaid_home: Optional[Path] = None) -> Path:
    home = quaid_home.resolve() if quaid_home is not None else get_quaid_home().resolve()
    return home / "data" / "project-docs"


def _state_dir() -> Path:
    return project_docs_root() / "state"


def _request_dir() -> Path:
    return project_docs_root() / "requests"


def _lock_dir() -> Path:
    return project_docs_root() / "locks"


def _worker_dir() -> Path:
    return project_docs_root() / "workers"


def _spawn_lock_path(kind: str, project: Optional[str] = None) -> Path:
    if project:
        return _safe_path(_lock_dir(), project, f".{kind}.spawn.lock")
    return _lock_dir() / f"{kind}.spawn.lock"


def _safe_path(base: Path, project: str, suffix: str) -> Path:
    name = validate_project_name(project)
    return base / f"{name}{suffix}"


def state_path(project: str) -> Path:
    return _safe_path(_state_dir(), project, ".json")


def request_path(project: str) -> Path:
    return _safe_path(_request_dir(), project, ".json")


def lock_path(project: str) -> Path:
    return _safe_path(_lock_dir(), project, ".lock")


def worker_pid_path(project: str) -> Path:
    return _safe_path(_worker_dir(), project, ".pid")


def worker_heartbeat_path(project: str) -> Path:
    return _safe_path(_worker_dir(), project, ".heartbeat.json")


def worker_log_path(project: str) -> Path:
    return _safe_path(_worker_dir(), project, ".log")


def supervisor_dir() -> Path:
    return project_docs_root() / "supervisor"


def supervisor_pid_path() -> Path:
    return supervisor_dir() / "supervisor.pid"


def supervisor_log_path() -> Path:
    return supervisor_dir() / "supervisor.log"


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except Exception:
        return False


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("Corrupt project-docs JSON: %s: %s", path, exc)
        if _fail_hard_enabled():
            raise
        return default
    except OSError as exc:
        logger.error("Failed reading project-docs JSON: %s: %s", path, exc)
        if _fail_hard_enabled():
            raise
        return default


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


@contextlib.contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def read_state(project: str) -> Dict[str, Any]:
    data = _read_json(state_path(project), {})
    return data if isinstance(data, dict) else {}


def read_worker_log_tail(project: str, *, max_lines: int = 40, max_bytes: int = 65536) -> List[str]:
    path = worker_log_path(project)
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(max(0, size - max_bytes))
            data = fh.read(max_bytes)
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-max(1, int(max_lines)):]
    except OSError as exc:
        logger.warning("Failed reading project-docs worker log tail for %s: %s", project, exc)
        if _fail_hard_enabled():
            raise
        return []


def cleanup_project_state(project: str) -> Dict[str, int]:
    """Remove all per-project project-docs operational state.

    Project deletion is authoritative: force requests, cursors, lock files,
    worker pid/heartbeat/logs, and temporary atomic-write files must not survive
    and later resurrect or confuse a deleted project.
    """
    name = validate_project_name(project)
    removed = 0
    candidates = [
        request_path(name),
        state_path(name),
        lock_path(name),
        _spawn_lock_path("worker", name),
        worker_pid_path(name),
        worker_heartbeat_path(name),
        worker_log_path(name),
    ]
    temp_patterns = [
        _request_dir() / f".{name}.json.*.tmp",
        _state_dir() / f".{name}.json.*.tmp",
        _worker_dir() / f".{name}.pid.*.tmp",
        _worker_dir() / f".{name}.heartbeat.json.*.tmp",
    ]
    for pattern in temp_patterns:
        candidates.extend(pattern.parent.glob(pattern.name))
    seen: set[str] = set()
    for path in candidates:
        try:
            key = str(path.resolve(strict=False))
        except Exception:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            logger.warning("Failed removing project-docs state file for %s: %s", name, path)
    return {"removed": removed}


def has_project_state(project: str) -> bool:
    """Return True when per-project docs operational state still exists."""
    name = validate_project_name(project)
    candidates = [
        request_path(name),
        state_path(name),
        lock_path(name),
        _spawn_lock_path("worker", name),
        worker_pid_path(name),
        worker_heartbeat_path(name),
        worker_log_path(name),
    ]
    temp_patterns = [
        _request_dir() / f".{name}.json.*.tmp",
        _state_dir() / f".{name}.json.*.tmp",
        _worker_dir() / f".{name}.pid.*.tmp",
        _worker_dir() / f".{name}.heartbeat.json.*.tmp",
    ]
    for path in candidates:
        if path.exists():
            return True
    for pattern in temp_patterns:
        if any(pattern.parent.glob(pattern.name)):
            return True
    return False


def project_is_registered_for_worker(project: str) -> bool:
    """Check deletion authority without triggering docs-registry reconciliation."""
    name = validate_project_name(project)
    try:
        from core.project_registry import project_exists_raw

        return bool(project_exists_raw(name))
    except Exception:
        logger.exception("Failed checking raw project registry for worker lifecycle: %s", name)
        if _fail_hard_enabled():
            raise
        return True


def write_state(project: str, state: Dict[str, Any]) -> None:
    payload = dict(state or {})
    payload["project"] = validate_project_name(project)
    payload["updated_at"] = utc_now()
    _atomic_write_json(state_path(project), payload)


def merge_state(project: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    state = read_state(project)
    state.update(updates)
    write_state(project, state)
    return state


def merge_progress(project: str, phase: str, message: str = "", **details: Any) -> Dict[str, Any]:
    progress = {
        "phase": str(phase or "").strip() or "unknown",
        "message": str(message or "").strip(),
        "updated_at": utc_now(),
    }
    for key, value in details.items():
        progress[str(key)] = value
    return merge_state(project, {"phase": progress["phase"], "progress": progress})


@contextlib.contextmanager
def project_update_lock(project: str, *, blocking: bool = True) -> Iterator[bool]:
    path = lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except OSError:
            if blocking:
                raise
            yield False
            return
        yield True
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _read_pid_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8").strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("pid file must contain a JSON object")
        pid = int(data.get("pid") or 0)
        if pid <= 0:
            raise ValueError("pid file has invalid pid")
        data["pid"] = pid
        return data
    except json.JSONDecodeError as exc:
        logger.warning("Corrupt project-docs pid file ignored: %s: %s", path, exc)
        return None
    except Exception as exc:
        logger.warning("Invalid project-docs pid file ignored: %s: %s", path, exc)
        return None


def _process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()
    except Exception:
        return ""


def _pid_record_matches(record: Dict[str, Any], *, role: str, project: Optional[str] = None) -> bool:
    pid = int(record.get("pid") or 0)
    if not _pid_alive(pid):
        return False
    if record.get("role") != role:
        return False
    if project is not None and record.get("project") != validate_project_name(project):
        return False
    command = _process_command(pid)
    if role == SUPERVISOR_ROLE:
        if "project_docs_supervisor.py" in command and " run" in f" {command}":
            return True
        return "project_docs_cli.py" in command and " supervisor " in f" {command} " and " run" in f" {command}"
    if role == WORKER_ROLE:
        name = validate_project_name(project or str(record.get("project") or ""))
        return "project_docs_worker.py" in command and f" {name}" in f" {command}"
    return False


def _read_valid_pid(path: Path, *, role: str, project: Optional[str] = None) -> Optional[int]:
    record = _read_pid_record(path)
    if not record:
        return None
    if _pid_record_matches(record, role=role, project=project):
        return int(record["pid"])
    return None


def _write_pid_record(path: Path, *, role: str, pid: int, token: str, project: Optional[str] = None) -> None:
    payload: Dict[str, Any] = {
        "pid": int(pid),
        "role": role,
        "token": token,
        "started_at": utc_now(),
    }
    if project is not None:
        payload["project"] = validate_project_name(project)
    _atomic_write_json(path, payload)


def read_pid(path: Path) -> Optional[int]:
    """Return a live PID from a pid file without trusting it for kill/spawn decisions.

    Role-specific callers should use read_supervisor_pid/read_worker_pid so PID
    recycling cannot make Quaid trust or kill an unrelated process.
    """
    record = _read_pid_record(path)
    if not record:
        return None
    pid = int(record.get("pid") or 0)
    return pid if _pid_alive(pid) else None


def read_supervisor_pid() -> Optional[int]:
    return _read_valid_pid(supervisor_pid_path(), role=SUPERVISOR_ROLE)


def read_worker_pid(project: str) -> Optional[int]:
    name = validate_project_name(project)
    return _read_valid_pid(worker_pid_path(name), role=WORKER_ROLE, project=name)


def write_supervisor_pid(token: str) -> None:
    _write_pid_record(supervisor_pid_path(), role=SUPERVISOR_ROLE, pid=os.getpid(), token=token)


def clear_supervisor_pid_for_current_process() -> None:
    token = os.environ.get("QUAID_SUPERVISOR_TOKEN", "").strip() or None
    _unlink_pid_record_if_matches(supervisor_pid_path(), pid=os.getpid(), token=token)


def clear_worker_pid_for_current_process(project: str) -> None:
    token = os.environ.get("QUAID_PROJECT_DOCS_WORKER_TOKEN", "").strip() or None
    _unlink_pid_record_if_matches(worker_pid_path(project), pid=os.getpid(), token=token)


def get_project_entry(project: str) -> Dict[str, Any]:
    from core.project_registry import get_project

    name = validate_project_name(project)
    entry = get_project(name)
    if not entry:
        raise KeyError(f"Project not found: {name}")
    return dict(entry)


def request_update(project: str, *, reason: str = "manual", requested_by: str = "cli") -> Dict[str, Any]:
    """Persist an async force-update request for a project docs worker."""
    name = validate_project_name(project)
    get_project_entry(name)
    req = {
        "project": name,
        "request_id": f"{int(time.time() * 1000)}-{os.getpid()}",
        "requested_at": utc_now(),
        "requested_by": requested_by,
        "reason": reason,
        "status": "pending",
    }
    _atomic_write_json(request_path(name), req)
    merge_state(
        name,
        {
            "status": "queued",
            "pending_request_id": req["request_id"],
            "force_requested_at": req["requested_at"],
            "last_error": None,
        },
    )
    return req


def read_update_request(project: str) -> Optional[Dict[str, Any]]:
    req = _read_json(request_path(project), None)
    return req if isinstance(req, dict) else None


def clear_update_request(project: str, request_id: Optional[str] = None) -> None:
    req = read_update_request(project)
    if request_id and req and req.get("request_id") != request_id:
        return
    try:
        request_path(project).unlink(missing_ok=True)
    except OSError:
        pass


def wait_for_request(project: str, request_id: str, *, timeout_seconds: float = 300.0) -> Dict[str, Any]:
    name = validate_project_name(project)
    deadline = time.time() + max(0.1, float(timeout_seconds))
    while time.time() < deadline:
        state = read_state(name)
        if state.get("last_request_id") == request_id:
            return state
        req = read_update_request(name)
        if not req or req.get("request_id") != request_id:
            return state
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for project docs update request {request_id}")


def _shadow_git(project: str, entry: Dict[str, Any]):
    from core.shadow_git import ShadowGit

    source_root = str(entry.get("source_root") or "").strip()
    if not source_root:
        return None
    return ShadowGit(
        project,
        Path(source_root),
        tracking_base=quaid_tracking_dir(get_quaid_home()),
    )


def _project_log_path(entry: Dict[str, Any]) -> Optional[Path]:
    raw = str(entry.get("canonical_path") or "").strip()
    if not raw:
        return None
    return Path(raw) / PROJECT_LOG


def _read_project_log_since(entry: Dict[str, Any], offset: int) -> Tuple[List[str], int, int]:
    log_path = _project_log_path(entry)
    if not log_path or not log_path.is_file():
        return [], 0, 0
    try:
        size = log_path.stat().st_size
        safe_offset = max(0, min(int(offset or 0), size))
        with log_path.open("rb") as fh:
            fh.seek(safe_offset)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines, safe_offset, size
    except Exception as exc:
        logger.exception("Failed reading PROJECT.log for project docs update: %s", log_path)
        raise RuntimeError(f"failed to read PROJECT.log at {log_path}: {exc}") from exc


def _current_project_log_size(entry: Dict[str, Any]) -> int:
    log_path = _project_log_path(entry)
    if not log_path or not log_path.is_file():
        return 0
    try:
        return int(log_path.stat().st_size)
    except Exception as exc:
        logger.exception("Failed stat for PROJECT.log: %s", log_path)
        raise RuntimeError(f"failed to stat PROJECT.log at {log_path}: {exc}") from exc


def pending_source_changes(project: str, entry: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    name = validate_project_name(project)
    entry = entry or get_project_entry(name)
    sg = _shadow_git(name, entry)
    if sg is None:
        raise RuntimeError(f"Project {name} has no source_root for shadow-git tracking")
    try:
        return [
            {"status": c.status, "path": c.path, "old_path": c.old_path}
            for c in sg.pending_changes()
        ]
    except Exception as exc:
        logger.exception("Failed reading pending source changes for project %s", name)
        raise RuntimeError(f"failed to read pending source changes for {name}: {exc}") from exc


def project_status(project: str) -> Dict[str, Any]:
    name = validate_project_name(project)
    entry = get_project_entry(name)
    state = read_state(name)
    req = read_update_request(name)
    worker_pid = read_worker_pid(name)
    worker_heartbeat = read_worker_heartbeat(name)
    log_path = worker_log_path(name)
    supervisor_pid = read_supervisor_pid()
    sg = _shadow_git(name, entry)
    current_shadow_head = sg.current_head() if sg is not None else None
    docs_cursor_head = state.get("last_shadow_commit")
    shadow_cursor_pending = bool(current_shadow_head and current_shadow_head != docs_cursor_head)
    source_error = None
    try:
        changes = pending_source_changes(name, entry)
    except RuntimeError as exc:
        logger.warning("Project docs status source check failed for %s: %s", name, exc)
        changes = []
        source_error = str(exc)
    log_offset = int(state.get("project_log_offset") or 0)
    log_size = _current_project_log_size(entry)
    log_pending = max(0, log_size - min(log_offset, log_size))
    stale = bool(req) or bool(changes) or shadow_cursor_pending or log_pending > 0
    status_value = "stale" if stale else "fresh"
    if source_error and not stale:
        status_value = "error"
    fresh = status_value == "fresh"
    return {
        "project": name,
        "registered": True,
        "status": status_value,
        "fresh": fresh,
        "source_root": entry.get("source_root"),
        "canonical_path": entry.get("canonical_path"),
        "source_error": source_error,
        "pending_request": req,
        "pending_source_changes": changes,
        "pending_source_change_count": len(changes),
        "current_shadow_head": current_shadow_head,
        "docs_cursor_head": docs_cursor_head,
        "shadow_cursor_pending": shadow_cursor_pending,
        "project_log_offset": log_offset,
        "project_log_cursor": log_offset,
        "project_log_size": log_size,
        "project_log_bytes_pending": log_pending,
        "worker_pid": worker_pid,
        "worker_heartbeat": worker_heartbeat,
        "worker_log_path": str(log_path),
        "worker_log_tail": read_worker_log_tail(name, max_lines=40),
        "supervisor_pid": supervisor_pid,
        "last_update_status": state.get("status"),
        "last_update_started_at": state.get("last_started_at"),
        "last_update_completed_at": state.get("last_completed_at"),
        "last_update_error": state.get("last_error"),
        "phase": state.get("phase"),
        "progress": state.get("progress") or {},
        "state": state,
    }


def project_diff(project: str, *, full: bool = False) -> Dict[str, Any]:
    name = validate_project_name(project)
    entry = get_project_entry(name)
    sg = _shadow_git(name, entry)
    changes = pending_source_changes(name, entry)
    diff_text = ""
    state = read_state(name)
    docs_cursor_head = state.get("last_shadow_commit")
    if sg is not None:
        committed_snapshot = sg.committed_snapshot_since(docs_cursor_head)
        if committed_snapshot is not None:
            changes = [
                {"status": c.status, "path": c.path, "old_path": c.old_path}
                for c in committed_snapshot.changes
            ] + changes
            committed_diff = sg.committed_diff_since(docs_cursor_head, full=full) or ""
            if committed_diff:
                diff_text = committed_diff
        try:
            pending_diff = sg.pending_diff(full=full) or ""
            if pending_diff:
                diff_text = f"{diff_text}\n{pending_diff}".strip() if diff_text else pending_diff
        except Exception as exc:
            logger.exception("Failed reading pending source diff for project %s", name)
            raise RuntimeError(f"failed to read pending source diff for {name}: {exc}") from exc
    log_offset = int(state.get("project_log_offset") or 0)
    log_lines, _, log_size = _read_project_log_since(entry, log_offset)
    return {
        "project": name,
        "full": bool(full),
        "changes": changes,
        "change_count": len(changes),
        "diff": diff_text,
        "project_log_entries": log_lines,
        "project_log_entry_count": len(log_lines),
        "project_log_size": log_size,
    }


def snapshot_project(project: str, entry: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    name = validate_project_name(project)
    entry = entry or get_project_entry(name)
    sg = _shadow_git(name, entry)
    if sg is None:
        return None
    snapshot = sg.snapshot()
    if not snapshot:
        return None
    return {
        "project": name,
        "is_initial": snapshot.is_initial,
        "commit_hash": snapshot.commit_hash,
        "diff": sg.get_diff() or "",
        "changes": [
            {"status": c.status, "path": c.path, "old_path": c.old_path}
            for c in snapshot.changes
        ],
    }


def committed_shadow_snapshot_since_cursor(
    project: str,
    entry: Optional[Dict[str, Any]] = None,
    cursor: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    name = validate_project_name(project)
    entry = entry or get_project_entry(name)
    sg = _shadow_git(name, entry)
    if sg is None:
        return None
    snapshot = sg.committed_snapshot_since(cursor)
    if not snapshot:
        return None
    return {
        "project": name,
        "is_initial": snapshot.is_initial,
        "commit_hash": snapshot.commit_hash,
        "diff": sg.committed_diff_since(cursor, full=True) or "",
        "changes": [
            {"status": c.status, "path": c.path, "old_path": c.old_path}
            for c in snapshot.changes
        ],
    }


def sync_project_docs_registry(project: str, entry: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """Register new visible docs and unregister docs deleted by updater apply.

    This is intentionally owned by the project-docs apply transaction. Source
    deletion alone must not unregister docs; only visible docs missing from the
    project docs tree after an updater apply are removed.
    """
    name = validate_project_name(project)
    entry = entry or get_project_entry(name)
    from core.docs import updater as docs_updater

    canonical_raw = str(entry.get("canonical_path") or "").strip()
    try:
        return docs_updater.sync_project_visible_docs(
            name,
            canonical_raw,
            root_docs=UPDATABLE_ROOT_DOCS,
            protected_names={PROJECT_LOG},
        )
    except Exception as exc:
        logger.warning("Project docs registry sync failed for %s: %s", name, exc)
        raise


def auto_register_project_docs(project: Optional[str] = None) -> int:
    """Register visible project docs from the supervisor-owned docs daemon path."""
    from core.project_registry import get_project as get_project_entry
    from core.project_registry import list_projects

    if project:
        name = validate_project_name(project)
        projects = {name: get_project_entry(name)}
    else:
        projects = list_projects()

    registered = 0
    for name, entry in sorted(projects.items()):
        if not entry:
            continue
        try:
            result = sync_project_docs_registry(name, entry)
            registered += int(result.get("registered") or 0)
        except Exception as exc:
            logger.warning("Project docs auto-register failed for %s: %s", name, exc)
            if _fail_hard_enabled():
                raise
    return registered


def index_one_stale_registered_doc(project: Optional[str] = None) -> bool:
    """Index one stale registered doc from the supervisor-owned docs daemon path."""
    from core.docs import updater as docs_updater

    return docs_updater.index_one_stale_registered_doc(project=project)


def refresh_docs_rag_once(project: Optional[str] = None) -> Dict[str, Any]:
    """Run one docs RAG maintenance pass under project-docs ownership."""
    registered = auto_register_project_docs(project)
    indexed = index_one_stale_registered_doc(project)
    return {"registered": registered, "indexed_one": bool(indexed)}


def _notify_project_docs_update(project: str, result: Dict[str, Any]) -> None:
    try:
        from lib.runtime_context import queue_deferred_notice

        metrics = result.get("metrics") or {}
        registry_sync = result.get("registry_sync") or {}
        message = (
            f"Project docs update completed for {project}: "
            f"docs_updated={int(metrics.get('docs_updated') or 0)}, "
            f"docs_registered={int(registry_sync.get('registered') or 0)}, "
            f"docs_unregistered={int(registry_sync.get('unregistered') or 0)}, "
            f"indexed_docs={int(result.get('indexed_docs') or 0)}"
        )
        queue_deferred_notice(
            message,
            kind="project_doc_update",
            priority="info",
            source="project-docs-worker",
        )
    except Exception:
        logger.warning("Failed to queue project docs update notice for %s", project)


def execute_update_once(project: str, *, request: Optional[Dict[str, Any]] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Run one project-doc update under the project lock.

    This is the primitive the project docs worker owns. It snapshots source
    changes, reads PROJECT.log from the last cursor, updates docs, reindexes the
    project's registered docs, and advances hidden cursors only after the apply
    phase returns.
    """
    name = validate_project_name(project)
    entry = get_project_entry(name)
    request = request or read_update_request(name)
    request_id = str((request or {}).get("request_id") or "") or None
    with project_update_lock(name, blocking=False) as acquired:
        if not acquired:
            if request_id:
                merge_state(
                    name,
                    {
                        "status": "queued",
                        "pending_request_id": request_id,
                        "last_error": "update lock busy; request retained",
                    },
                )
            return {"project": name, "status": "locked", "request_id": request_id, "request_retained": bool(request_id)}
        started = utc_now()
        merge_state(name, {"status": "updating", "last_started_at": started, "last_error": None})
        merge_progress(name, "starting", "project-docs update started")
        try:
            state = read_state(name)
            log_offset = int(state.get("project_log_offset") or 0)
            docs_cursor_head = state.get("last_shadow_commit")
            merge_progress(name, "read_project_log", "reading PROJECT.log cursor", project_log_offset=log_offset)
            log_entries, _old_log_offset, log_size = _read_project_log_since(entry, log_offset)
            merge_progress(name, "snapshot", "snapshotting source changes")
            snapshot_project(name, entry)
            snapshot = committed_shadow_snapshot_since_cursor(name, entry, docs_cursor_head)
            snapshots = [snapshot] if snapshot else []
            metrics: Dict[str, Any] = {
                "projects_checked": 0,
                "docs_updated": 0,
                "docs_skipped": 0,
                "trivial_skipped": 0,
                "errors": 0,
            }
            registry_sync: Dict[str, int] = {"registered": 0, "unregistered": 0, "project_md_refreshed": 0}
            index_count = 0
            from core.docs_updater_hook import update_project_docs
            from core.docs import updater as docs_updater

            if snapshots or log_entries or request:
                merge_progress(
                    name,
                    "update_docs",
                    "running project docs update",
                    snapshot_changes=len((snapshot or {}).get("changes") or []),
                    project_log_entries=len(log_entries),
                    request_id=request_id,
                )
                metrics = update_project_docs(
                    snapshots,
                    extraction_result={"project_logs": {name: log_entries}},
                    dry_run=dry_run,
                    force_project=name,
                )
            if not dry_run:
                merge_progress(name, "sync_registry", "syncing visible project docs registry")
                registry_sync = sync_project_docs_registry(name, entry)
                try:
                    merge_progress(name, "index_docs", "indexing registered project docs")
                    index_count = int(
                        docs_updater.update_registered_docs(
                            project=name,
                            dry_run=False,
                            protected_names={PROJECT_LOG},
                        ) or 0
                    )
                except Exception as exc:
                    if _fail_hard_enabled():
                        raise
                    metrics["errors"] = int(metrics.get("errors", 0) or 0) + 1
                    metrics["index_error"] = str(exc)
            completed = utc_now()
            next_state = {
                "status": "fresh" if not metrics.get("errors") else "error",
                "last_completed_at": completed,
                "last_request_id": request_id,
                "last_metrics": metrics,
                "last_registry_sync": registry_sync,
                "last_indexed_docs": index_count,
                "project_log_offset": log_size,
                "phase": "idle",
                "progress": {
                    "phase": "idle",
                    "message": "project-docs update complete",
                    "updated_at": completed,
                    "docs_updated": int(metrics.get("docs_updated") or 0),
                    "docs_registered": int(registry_sync.get("registered") or 0),
                    "indexed_docs": index_count,
                },
            }
            if snapshot and snapshot.get("commit_hash"):
                next_state["last_shadow_commit"] = snapshot.get("commit_hash")
            if metrics.get("errors"):
                next_state["last_error"] = str(metrics)
            else:
                next_state["last_error"] = None
            merge_state(name, next_state)
            if request_id and not dry_run:
                clear_update_request(name, request_id=request_id)
            result = {
                "project": name,
                "status": next_state["status"],
                "request_id": request_id,
                "snapshot": snapshot,
                "project_log_entries": len(log_entries),
                "metrics": metrics,
                "registry_sync": registry_sync,
                "indexed_docs": index_count,
            }
            if not dry_run and next_state["status"] == "fresh":
                _notify_project_docs_update(name, result)
            return result
        except Exception as exc:
            merge_state(
                name,
                {
                    "status": "error",
                    "last_error": str(exc),
                    "last_failed_at": utc_now(),
                    "last_request_id": request_id,
                },
            )
            raise


def write_worker_heartbeat(project: str, payload: Optional[Dict[str, Any]] = None) -> None:
    data = {"project": validate_project_name(project), "pid": os.getpid(), "heartbeat_at": utc_now()}
    if payload:
        data.update(payload)
    _atomic_write_json(worker_heartbeat_path(project), data)
    token = os.environ.get("QUAID_PROJECT_DOCS_WORKER_TOKEN", "").strip()
    _write_pid_record(worker_pid_path(project), role=WORKER_ROLE, pid=os.getpid(), token=token, project=project)


def read_worker_heartbeat(project: str) -> Dict[str, Any]:
    data = _read_json(worker_heartbeat_path(project), {})
    return data if isinstance(data, dict) else {}


def _parse_iso_ts(value: Any) -> Optional[float]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return None


def worker_stale_after_seconds(interval_seconds: Optional[float] = None) -> float:
    raw = os.environ.get("QUAID_PROJECT_DOCS_WORKER_STALE_SECONDS", "").strip()
    if raw:
        try:
            return max(5.0, float(raw))
        except ValueError:
            logger.warning("Invalid QUAID_PROJECT_DOCS_WORKER_STALE_SECONDS=%r; using default", raw)
    base = float(interval_seconds or os.environ.get("QUAID_PROJECT_DOCS_WORKER_INTERVAL_SECONDS", "5") or 5)
    return max(900.0, base * 12.0)


def pid_startup_wait_seconds() -> float:
    raw = os.environ.get("QUAID_PROJECT_DOCS_PID_WAIT_SECONDS", "").strip()
    if raw:
        try:
            return max(5.0, min(120.0, float(raw)))
        except ValueError:
            logger.warning("Invalid QUAID_PROJECT_DOCS_PID_WAIT_SECONDS=%r; using default", raw)
    return 30.0


def _worker_heartbeat_stale(project: str, *, stale_after_seconds: float) -> bool:
    heartbeat = read_worker_heartbeat(project)
    ts = _parse_iso_ts(heartbeat.get("heartbeat_at"))
    if ts is None:
        return True
    return (time.time() - ts) > stale_after_seconds


def reap_stale_worker(project: str, *, stale_after_seconds: float) -> bool:
    name = validate_project_name(project)
    state = read_state(name)
    pid = read_worker_pid(name)
    stale = _worker_heartbeat_stale(name, stale_after_seconds=stale_after_seconds)
    if pid is not None and not stale:
        return False
    if pid is not None and stale:
        logger.warning("Project docs worker heartbeat stale for %s; restarting pid=%s", name, pid)
        stop_worker(name)
    # stop_worker can race with a worker that completes normally while handling
    # SIGTERM. Re-read state before queuing a retry so a successful fresh cursor
    # update is not overwritten by the stale pre-stop snapshot.
    current_state = read_state(name)
    if current_state.get("status") == "updating":
        merge_state(
            name,
            {
                "status": "queued",
                "last_error": "worker stopped or heartbeat stale during update; retry queued",
                "last_failed_at": utc_now(),
            },
        )
    return stale or pid is None


def _wait_for_pid(
    path: Path,
    *,
    expected_pid: int,
    role: str,
    project: Optional[str] = None,
    proc: Optional[subprocess.Popen] = None,
    timeout_seconds: Optional[float] = None,
) -> int:
    timeout = pid_startup_wait_seconds() if timeout_seconds is None else float(timeout_seconds)
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _read_valid_pid(path, role=role, project=project)
        if pid == expected_pid:
            return pid
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"{role} exited before writing pid file rc={proc.returncode}")
        time.sleep(0.05)
    raise TimeoutError(f"{role} did not write a valid pid file for pid {expected_pid}")


def _terminate_process(proc: subprocess.Popen, *, grace_seconds: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass
    except Exception:
        pass


def reap_child_processes() -> int:
    """Reap finished child processes when running as their supervisor parent."""
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        except OSError:
            return reaped
        if pid <= 0:
            return reaped
        reaped += 1


def _unlink_pid_record_if_matches(path: Path, *, pid: int, token: Optional[str] = None) -> None:
    record = _read_pid_record(path)
    if not record or int(record.get("pid") or 0) != int(pid):
        return
    if token is not None and record.get("token") != token:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_supervisor_alive() -> int:
    pid = read_supervisor_pid()
    if pid is not None:
        return pid
    return start_supervisor()


def start_supervisor() -> int:
    with _exclusive_file_lock(_spawn_lock_path("supervisor")):
        existing = read_supervisor_pid()
        if existing is not None:
            return existing
        supervisor_dir().mkdir(parents=True, exist_ok=True)
        log_path = supervisor_log_path()
        script = Path(__file__).parent / "project_docs_supervisor.py"
        env = dict(os.environ)
        env.setdefault("QUAID_SUPERVISOR_INTERVAL_SECONDS", "5")
        env["QUAID_SUPERVISOR_TOKEN"] = uuid.uuid4().hex
        with log_path.open("ab") as log_fh:
            proc = subprocess.Popen(
                [sys.executable, str(script), "run"],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
                env=env,
            )
        try:
            return _wait_for_pid(
                supervisor_pid_path(),
                expected_pid=int(proc.pid),
                role=SUPERVISOR_ROLE,
                proc=proc,
            )
        except Exception:
            _terminate_process(proc)
            raise


def stop_supervisor() -> bool:
    with _exclusive_file_lock(_spawn_lock_path("supervisor")):
        record = _read_pid_record(supervisor_pid_path())
        pid = int((record or {}).get("pid") or 0)
        if not record:
            return False
        if not _pid_record_matches(record, role=SUPERVISOR_ROLE):
            _unlink_pid_record_if_matches(supervisor_pid_path(), pid=pid, token=record.get("token"))
            return False
        try:
            try:
                os.killpg(pid, signal.SIGTERM)
            except Exception:
                os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 5.0
            while time.time() < deadline:
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except Exception:
                    os.kill(pid, signal.SIGKILL)
            if _worker_dir().is_dir():
                for pid_file in sorted(_worker_dir().glob("*.pid")):
                    project = pid_file.stem
                    try:
                        stop_worker(project)
                    except Exception:
                        logger.exception("Failed stopping project docs worker for %s", project)
            try:
                from core.project_docs_supervisor import stop_all_instance_monitors
                stop_all_instance_monitors()
            except Exception:
                logger.exception("Failed stopping supervisor-owned instance monitors")
            return True
        finally:
            if not _pid_alive(pid):
                _unlink_pid_record_if_matches(supervisor_pid_path(), pid=pid, token=record.get("token"))


def start_worker(project: str) -> int:
    name = validate_project_name(project)
    if not project_is_registered_for_worker(name):
        raise KeyError(f"Project not found: {name}")
    with _exclusive_file_lock(_spawn_lock_path("worker", name)):
        if not project_is_registered_for_worker(name):
            raise KeyError(f"Project not found: {name}")
        existing = read_worker_pid(name)
        if existing is not None:
            return existing
        _worker_dir().mkdir(parents=True, exist_ok=True)
        log_path = worker_log_path(name)
        script = Path(__file__).parent / "project_docs_worker.py"
        env = dict(os.environ)
        env.setdefault("QUAID_PROJECT_DOCS_WORKER_INTERVAL_SECONDS", "5")
        env["QUAID_PROJECT_DOCS_WORKER_TOKEN"] = uuid.uuid4().hex
        supervisor_pid = read_supervisor_pid()
        if supervisor_pid:
            env["QUAID_SUPERVISOR_PID"] = str(supervisor_pid)
        with log_path.open("ab") as log_fh:
            proc = subprocess.Popen(
                [sys.executable, str(script), "run", name],
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=False,
                env=env,
            )
        try:
            return _wait_for_pid(
                worker_pid_path(name),
                expected_pid=int(proc.pid),
                role=WORKER_ROLE,
                project=name,
                proc=proc,
            )
        except Exception:
            _terminate_process(proc)
            raise


def stop_worker(project: str) -> bool:
    name = validate_project_name(project)
    with _exclusive_file_lock(_spawn_lock_path("worker", name)):
        record = _read_pid_record(worker_pid_path(name))
        pid = int((record or {}).get("pid") or 0)
        if not record:
            return False
        if not _pid_record_matches(record, role=WORKER_ROLE, project=name):
            _unlink_pid_record_if_matches(worker_pid_path(name), pid=pid, token=record.get("token"))
            return False
        if pid != os.getpid():
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
            reap_child_processes()
        if not _pid_alive(pid):
            _unlink_pid_record_if_matches(worker_pid_path(name), pid=pid, token=record.get("token"))
        return True


def format_status(status: Dict[str, Any]) -> str:
    lines = [f"Project: {status['project']}", f"Status: {status['status']}"]
    if status.get("source_root"):
        lines.append(f"Source root: {status['source_root']}")
    lines.append(f"Pending source changes: {status.get('pending_source_change_count', 0)}")
    if status.get("source_error"):
        lines.append(f"Source tracking error: {status.get('source_error')}")
    lines.append(f"Pending PROJECT.log bytes: {status.get('project_log_bytes_pending', 0)}")
    if status.get("pending_request"):
        req = status["pending_request"]
        lines.append(f"Pending force request: {req.get('request_id')} at {req.get('requested_at')}")
    lines.append(f"Supervisor PID: {status.get('supervisor_pid') or '(not running)'}")
    lines.append(f"Worker PID: {status.get('worker_pid') or '(not running)'}")
    if status.get("phase"):
        lines.append(f"Phase: {status.get('phase')}")
    progress = status.get("progress") or {}
    if progress.get("message"):
        lines.append(f"Progress: {progress.get('message')}")
    if status.get("worker_log_path"):
        lines.append(f"Worker log: {status.get('worker_log_path')}")
    state = status.get("state") or {}
    if state.get("last_completed_at"):
        lines.append(f"Last completed: {state.get('last_completed_at')}")
    if state.get("last_error"):
        lines.append(f"Last error: {state.get('last_error')}")
    tail = status.get("worker_log_tail") or []
    if tail:
        lines.append("Recent worker log:")
        for line in tail[-5:]:
            lines.append(f"  {line}")
    return "\n".join(lines)


def format_diff(diff: Dict[str, Any]) -> str:
    lines = [f"Project: {diff['project']}", f"Source changes: {diff.get('change_count', 0)}"]
    for change in diff.get("changes", []):
        old = f" (was {change.get('old_path')})" if change.get("old_path") else ""
        lines.append(f"  {change.get('status', '?')}\t{change.get('path', '')}{old}")
    if diff.get("project_log_entry_count"):
        lines.append(f"PROJECT.log entries since cursor: {diff['project_log_entry_count']}")
        for line in diff.get("project_log_entries", [])[:20]:
            lines.append(f"  {line}")
        if len(diff.get("project_log_entries", [])) > 20:
            lines.append("  ...")
    if diff.get("diff"):
        lines.append("\nDiff:")
        lines.append(str(diff["diff"]).rstrip())
    return "\n".join(lines)
