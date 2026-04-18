"""Project documentation supervisor state and force-update primitives.

This module owns hidden operational state for project-doc updates. Visible
project files remain user-facing artifacts; worker cursors, force requests,
heartbeats, and locks live under QUAID_HOME/data/project-docs/.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from lib.adapter import quaid_tracking_dir
from lib.runtime_context import get_quaid_home

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PROJECT_LOG = "PROJECT.log"


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def validate_project_name(project: str) -> str:
    name = str(project or "").strip()
    if not _PROJECT_RE.match(name):
        raise ValueError(f"Invalid project name: {project!r}")
    return name


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


def supervisor_dir() -> Path:
    return project_docs_root() / "supervisor"


def supervisor_pid_path() -> Path:
    return supervisor_dir() / "supervisor.pid"


def supervisor_log_path() -> Path:
    return supervisor_dir() / "supervisor.log"


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.is_file():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
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


def read_state(project: str) -> Dict[str, Any]:
    data = _read_json(state_path(project), {})
    return data if isinstance(data, dict) else {}


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


def read_pid(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
    except Exception:
        return None
    return pid if _pid_alive(pid) else None


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
    except Exception:
        return [], 0, 0


def _current_project_log_size(entry: Dict[str, Any]) -> int:
    log_path = _project_log_path(entry)
    if not log_path or not log_path.is_file():
        return 0
    try:
        return int(log_path.stat().st_size)
    except Exception:
        return 0


def pending_source_changes(project: str, entry: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    name = validate_project_name(project)
    entry = entry or get_project_entry(name)
    sg = _shadow_git(name, entry)
    if sg is None:
        return []
    try:
        return [
            {"status": c.status, "path": c.path, "old_path": c.old_path}
            for c in sg.pending_changes()
        ]
    except Exception as exc:
        return [{"status": "?", "path": f"shadow-git-error: {exc}", "old_path": None}]


def project_status(project: str) -> Dict[str, Any]:
    name = validate_project_name(project)
    entry = get_project_entry(name)
    state = read_state(name)
    req = read_update_request(name)
    worker_pid = read_pid(worker_pid_path(name))
    supervisor_pid = read_pid(supervisor_pid_path())
    changes = pending_source_changes(name, entry)
    log_offset = int(state.get("project_log_offset") or 0)
    log_size = _current_project_log_size(entry)
    log_pending = max(0, log_size - min(log_offset, log_size))
    stale = bool(req) or bool(changes) or log_pending > 0
    return {
        "project": name,
        "registered": True,
        "status": "stale" if stale else "fresh",
        "source_root": entry.get("source_root"),
        "canonical_path": entry.get("canonical_path"),
        "pending_request": req,
        "pending_source_changes": changes,
        "pending_source_change_count": len(changes),
        "project_log_offset": log_offset,
        "project_log_size": log_size,
        "project_log_bytes_pending": log_pending,
        "worker_pid": worker_pid,
        "supervisor_pid": supervisor_pid,
        "state": state,
    }


def project_diff(project: str, *, full: bool = False) -> Dict[str, Any]:
    name = validate_project_name(project)
    entry = get_project_entry(name)
    sg = _shadow_git(name, entry)
    changes = pending_source_changes(name, entry)
    diff_text = ""
    if sg is not None:
        try:
            diff_text = sg.pending_diff(full=full) or ""
        except Exception as exc:
            diff_text = f"shadow-git diff failed: {exc}"
    state = read_state(name)
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
            return {"project": name, "status": "locked", "updated_docs": 0, "errors": 0}
        started = utc_now()
        merge_state(name, {"status": "updating", "last_started_at": started, "last_error": None})
        state = read_state(name)
        log_offset = int(state.get("project_log_offset") or 0)
        log_entries, _old_log_offset, log_size = _read_project_log_since(entry, log_offset)
        snapshot = snapshot_project(name, entry)
        snapshots = [snapshot] if snapshot else []
        metrics: Dict[str, Any] = {
            "projects_checked": 0,
            "docs_updated": 0,
            "docs_skipped": 0,
            "trivial_skipped": 0,
            "errors": 0,
        }
        index_count = 0
        try:
            from core.docs_updater_hook import update_project_docs
            from core.docs import updater as docs_updater

            if snapshots or log_entries or request:
                metrics = update_project_docs(
                    snapshots,
                    extraction_result={"project_logs": {name: log_entries}},
                    dry_run=dry_run,
                    force_project=name,
                )
            if not dry_run:
                try:
                    index_count = int(docs_updater.update_registered_docs(project=name, dry_run=False) or 0)
                except Exception as exc:
                    metrics["errors"] = int(metrics.get("errors", 0) or 0) + 1
                    metrics["index_error"] = str(exc)
            completed = utc_now()
            next_state = {
                "status": "fresh" if not metrics.get("errors") else "error",
                "last_completed_at": completed,
                "last_request_id": request_id,
                "last_metrics": metrics,
                "last_indexed_docs": index_count,
                "project_log_offset": log_size,
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
            return {
                "project": name,
                "status": next_state["status"],
                "request_id": request_id,
                "snapshot": snapshot,
                "project_log_entries": len(log_entries),
                "metrics": metrics,
                "indexed_docs": index_count,
            }
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
    worker_pid_path(project).parent.mkdir(parents=True, exist_ok=True)
    worker_pid_path(project).write_text(f"{os.getpid()}\n", encoding="utf-8")


def ensure_supervisor_alive() -> int:
    pid = read_pid(supervisor_pid_path())
    if pid is not None:
        return pid
    return start_supervisor()


def start_supervisor() -> int:
    supervisor_dir().mkdir(parents=True, exist_ok=True)
    log_path = supervisor_log_path()
    script = Path(__file__).parent / "project_docs_supervisor.py"
    env = dict(os.environ)
    env.setdefault("QUAID_SUPERVISOR_INTERVAL_SECONDS", "5")
    with log_path.open("ab") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(script), "run"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            env=env,
        )
    supervisor_pid_path().write_text(f"{proc.pid}\n", encoding="utf-8")
    return int(proc.pid)


def stop_supervisor() -> bool:
    pid = read_pid(supervisor_pid_path())
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
        if _worker_dir().is_dir():
            for pid_file in sorted(_worker_dir().glob("*.pid")):
                project = pid_file.stem
                try:
                    stop_worker(project)
                except Exception:
                    pass
        return True
    finally:
        try:
            supervisor_pid_path().unlink(missing_ok=True)
        except OSError:
            pass


def start_worker(project: str) -> int:
    name = validate_project_name(project)
    existing = read_pid(worker_pid_path(name))
    if existing is not None:
        return existing
    _worker_dir().mkdir(parents=True, exist_ok=True)
    log_path = _worker_dir() / f"{name}.log"
    script = Path(__file__).parent / "project_docs_worker.py"
    env = dict(os.environ)
    env.setdefault("QUAID_PROJECT_DOCS_WORKER_INTERVAL_SECONDS", "5")
    supervisor_pid = read_pid(supervisor_pid_path())
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
    worker_pid_path(name).write_text(f"{proc.pid}\n", encoding="utf-8")
    return int(proc.pid)


def stop_worker(project: str) -> bool:
    name = validate_project_name(project)
    pid = read_pid(worker_pid_path(name))
    if pid is None:
        return False
    try:
        if pid != os.getpid():
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        return True
    finally:
        try:
            worker_pid_path(name).unlink(missing_ok=True)
        except OSError:
            pass


def format_status(status: Dict[str, Any]) -> str:
    lines = [f"Project: {status['project']}", f"Status: {status['status']}"]
    if status.get("source_root"):
        lines.append(f"Source root: {status['source_root']}")
    lines.append(f"Pending source changes: {status.get('pending_source_change_count', 0)}")
    lines.append(f"Pending PROJECT.log bytes: {status.get('project_log_bytes_pending', 0)}")
    if status.get("pending_request"):
        req = status["pending_request"]
        lines.append(f"Pending force request: {req.get('request_id')} at {req.get('requested_at')}")
    lines.append(f"Supervisor PID: {status.get('supervisor_pid') or '(not running)'}")
    lines.append(f"Worker PID: {status.get('worker_pid') or '(not running)'}")
    state = status.get("state") or {}
    if state.get("last_completed_at"):
        lines.append(f"Last completed: {state.get('last_completed_at')}")
    if state.get("last_error"):
        lines.append(f"Last error: {state.get('last_error')}")
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
