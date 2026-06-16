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
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

PROJECT_LOG = "PROJECT.log"
UPDATABLE_ROOT_DOCS = {"PROJECT.md", "TOOLS.md", "AGENTS.md"}
SUPERVISOR_ROLE = "project-docs-supervisor"
WORKER_ROLE = "project-docs-worker"
_DB_OVERRIDE_ENV_KEYS = ("MEMORY_DB_PATH", "MEMORY_ARCHIVE_DB_PATH")
_BACKGROUND_PROCESS_SCRUB_ENV_KEYS = (
    "ART",
    "CLAUDE_PROJECT_DIR",
    "CODEX_PROJECT_DIR",
    "INSTANCE",
    "INSTANCE_X",
    "INSTANCE_Y",
    "LANE",
    "LANE_UPPER",
    "PROBE_ID",
    "QUAID_ADAPTER_TYPE",
    "QUAID_INSTANCE",
    "SILO",
    "SILO_X",
    "SILO_Y",
)

logger = logging.getLogger(__name__)


def _now_datetime() -> datetime:
    raw = str(os.environ.get("QUAID_NOW", "") or "").strip()
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Invalid QUAID_NOW=%r", raw)
            raise ValueError(f"Invalid QUAID_NOW={raw!r}") from None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(tz=timezone.utc)


def utc_now() -> str:
    return _now_datetime().isoformat()


def _now_epoch() -> float:
    return _now_datetime().timestamp()


def validate_project_name(project: str) -> str:
    from core.project_registry import _validate_project_name

    return _validate_project_name(project)


def get_quaid_home() -> Path:
    """Return QUAID_HOME without adapter bootstrap side effects."""
    raw = os.environ.get("QUAID_HOME", "").strip()
    return Path(raw).expanduser().resolve() if raw else Path.home() / ".quaid"


def scrub_background_process_env(env: Dict[str, str]) -> Dict[str, str]:
    """Remove caller-local lane/test variables from long-lived Quaid processes."""
    cleaned = dict(env)
    for key in _BACKGROUND_PROCESS_SCRUB_ENV_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _hydrate_anthropic_api_key_from_shared_auth(env: Dict[str, str]) -> Dict[str, str]:
    """Ensure project-doc subprocesses can use the shared Anthropic credential.

    Project-doc workers are supervisor-owned and may outlive the shell or host
    hook that had ANTHROPIC_API_KEY in its process environment. The canonical
    long-lived credential lives in QUAID_HOME/shared/auth/credentials.json.
    Hydrate only the subprocess env and never overwrite an explicit env var.
    """
    if str(env.get("ANTHROPIC_API_KEY") or "").strip():
        return env
    raw_home = str(env.get("QUAID_HOME") or "").strip()
    home = Path(raw_home).expanduser().resolve() if raw_home else get_quaid_home()
    path = home / "shared" / "auth" / "credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        creds = data.get("credentials", {}) if isinstance(data, dict) else {}
        if not isinstance(creds, dict):
            return env
        for kind in ("anthropic_oauth", "anthropic_api"):
            payload = creds.get(kind)
            token = ""
            if isinstance(payload, dict):
                token = str(payload.get("token") or "").strip()
            elif isinstance(payload, str):
                token = payload.strip()
            if token:
                env["ANTHROPIC_API_KEY"] = token
                return env
    except Exception as exc:
        logger.warning("Failed hydrating ANTHROPIC_API_KEY for project-docs subprocess: %s", exc)
        if _fail_hard_enabled():
            raise
    return env


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


def state_lock_path(project: str) -> Path:
    return _safe_path(_lock_dir(), project, ".state.lock")


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


def janitor_request_path() -> Path:
    return supervisor_dir() / "janitor-request.json"


def instance_monitor_disabled_dir() -> Path:
    return project_docs_root() / "instance-monitors-disabled"


def instance_monitor_disabled_path(instance: str) -> Path:
    from lib.instance import validate_instance_id

    return instance_monitor_disabled_dir() / f"{validate_instance_id(instance)}.json"


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except Exception as exc:
        logger.critical("fail-hard policy unavailable in project docs: %s", exc)
        return True


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
            os.close(fd)
        except OSError:
            pass
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
        state_lock_path(name),
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
    try:
        from core.docs import updater as docs_updater

        removed += int(docs_updater.cleanup_project_log_queue(name).get("removed", 0) or 0)
    except Exception as exc:
        logger.warning("Failed removing project-log queue state for %s: %s", name, exc)
        if _fail_hard_enabled():
            raise
    return {"removed": removed}


def has_project_state(project: str) -> bool:
    """Return True when per-project docs operational state still exists."""
    name = validate_project_name(project)
    candidates = [
        request_path(name),
        state_path(name),
        lock_path(name),
        state_lock_path(name),
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
    try:
        from core.docs import updater as docs_updater

        if docs_updater.pending_project_log_count(name) > 0:
            return True
    except Exception as exc:
        logger.warning("Failed checking project-log queue state for %s: %s", name, exc)
        if _fail_hard_enabled():
            raise
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


def _write_state_unlocked(project: str, state: Dict[str, Any]) -> None:
    payload = dict(state or {})
    payload["project"] = validate_project_name(project)
    payload["updated_at"] = utc_now()
    _atomic_write_json(state_path(project), payload)


def write_state(project: str, state: Dict[str, Any]) -> None:
    name = validate_project_name(project)
    with _exclusive_file_lock(state_lock_path(name)):
        _write_state_unlocked(name, state)


def _merge_state_unlocked(project: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    state = read_state(project)
    state.update(updates)
    _write_state_unlocked(project, state)
    return state


def merge_state(project: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    name = validate_project_name(project)
    with _exclusive_file_lock(state_lock_path(name)):
        return _merge_state_unlocked(name, updates)


def merge_progress(project: str, phase: str, message: str = "", **details: Any) -> Dict[str, Any]:
    updated_at = utc_now()
    progress = {
        "phase": str(phase or "").strip() or "unknown",
        "message": str(message or "").strip(),
        "updated_at": updated_at,
    }
    for key, value in details.items():
        progress[str(key)] = value
    return merge_state(project, {"phase": progress["phase"], "progress": progress, "last_progress_update": updated_at})


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


def _supervisor_command_matches(command: str) -> bool:
    text = f" {str(command or '').strip()} "
    if "project_docs_supervisor.py" in text and " run" in text:
        return True
    return "project_docs_cli.py" in text and " supervisor " in text and " run" in text


def _matching_supervisor_pids(*, quaid_home: Optional[Path] = None) -> List[int]:
    home = (quaid_home.resolve() if quaid_home is not None else get_quaid_home().resolve())
    home_marker = f"QUAID_HOME={home}"
    commands = [
        ["ps", "eww", "-eo", "pid=,command="],
        ["ps", "eww", "-ax", "-o", "pid=", "-o", "command="],
        ["ps", "eww", "-axo", "pid=,command="],
        ["ps", "axeww", "-o", "pid=,command="],
    ]
    matches: List[int] = []
    failures: List[str] = []
    saw_process_rows = False
    for ps_command in commands:
        try:
            result = subprocess.run(
                ps_command,
                capture_output=True,
                text=True,
                timeout=1,
            )
        except Exception as exc:
            failures.append(f"{' '.join(ps_command)}: {exc}")
            continue
        if result.returncode != 0 or not str(result.stdout or "").strip():
            failures.append(
                f"{' '.join(ps_command)}: rc={result.returncode} stdout_len={len(str(result.stdout or ''))}"
            )
            continue
        for raw in str(result.stdout or "").splitlines():
            line = str(raw or "").strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            saw_process_rows = True
            command = parts[1]
            if not _supervisor_command_matches(command):
                continue
            if home_marker not in command:
                continue
            if _pid_alive(pid):
                matches.append(pid)
    if not saw_process_rows:
        logger.warning(
            "project-docs supervisor ps scan returned no rows after %d variants for home=%s: %s",
            len(commands),
            home,
            "; ".join(failures[-4:]) if failures else "no output",
        )
    return sorted(set(matches))


def _pid_record_started_recently(record: Dict[str, Any], *, max_age_seconds: float = 300.0) -> bool:
    ts = _parse_iso_ts(record.get("started_at"))
    if ts is None:
        return False
    age = _now_epoch() - ts
    return -5.0 <= age <= max_age_seconds


def _pid_record_basic_matches(record: Dict[str, Any], *, role: str, project: Optional[str] = None) -> bool:
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if not _pid_alive(pid):
        return False
    if record.get("role") != role:
        return False
    if project is not None and record.get("project") != validate_project_name(project):
        return False
    return True


def _pid_record_matches(record: Dict[str, Any], *, role: str, project: Optional[str] = None) -> bool:
    if not _pid_record_basic_matches(record, role=role, project=project):
        return False
    pid = int(record.get("pid") or 0)
    command = _process_command(pid)
    if role == SUPERVISOR_ROLE:
        if "project_docs_supervisor.py" in command and " run" in f" {command}":
            return True
        return "project_docs_cli.py" in command and " supervisor " in f" {command} " and " run" in f" {command}"
    if role == WORKER_ROLE:
        name = validate_project_name(project or str(record.get("project") or ""))
        if "project_docs_worker.py" in command and f" {name}" in f" {command}":
            return True
        # Linux CI can briefly expose an empty or incomplete command line for a
        # just-spawned worker. Trust only fresh Quaid-written worker pid records;
        # old records still need command identity so stale PID reuse is rejected.
        return _pid_record_started_recently(record)
    return False


def _read_valid_pid(path: Path, *, role: str, project: Optional[str] = None) -> Optional[int]:
    record = _read_pid_record(path)
    if not record:
        return None
    if _pid_record_matches(record, role=role, project=project):
        return int(record["pid"])
    return None


def _write_pid_record(path: Path, *, role: str, pid: int, token: Optional[str], project: Optional[str] = None) -> None:
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


def disable_instance_monitor(instance: str, *, reason: str = "manual_stop") -> None:
    from lib.instance import validate_instance_id

    name = validate_instance_id(instance)
    _atomic_write_json(
        instance_monitor_disabled_path(name),
        {
            "instance": name,
            "reason": str(reason or "manual_stop"),
            "disabled_at": utc_now(),
        },
    )


def enable_instance_monitor(instance: str) -> None:
    try:
        instance_monitor_disabled_path(instance).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed clearing disabled instance monitor marker for %s: %s", instance, exc)
        if _fail_hard_enabled():
            raise


def is_instance_monitor_disabled(instance: str) -> bool:
    try:
        return instance_monitor_disabled_path(instance).is_file()
    except Exception as exc:
        logger.warning("Failed checking disabled instance monitor marker for %s: %s", instance, exc)
        if _fail_hard_enabled():
            raise
        return False


def clear_worker_pid_for_current_process(project: str) -> None:
    token = os.environ.get("QUAID_PROJECT_DOCS_WORKER_TOKEN", "").strip() or None
    _unlink_pid_record_if_matches(worker_pid_path(project), pid=os.getpid(), token=token)


def get_project_entry(project: str) -> Dict[str, Any]:
    from core.project_registry import get_project, get_project_raw

    name = validate_project_name(project)
    entry = get_project_raw(name)
    if not entry:
        entry = get_project(name)
    if not entry:
        raise KeyError(f"Project not found: {name}")
    return dict(entry)


def _adapter_type_from_instance_name(instance_name: str) -> str:
    name = str(instance_name or "").strip()
    if not name:
        return ""
    try:
        from lib.adapter import _adapter_type_from_instance_id

        return str(_adapter_type_from_instance_id(name) or "").strip().lower()
    except Exception:
        return ""


def _valid_linked_project_instances(entry: Dict[str, Any]) -> List[str]:
    from lib.instance import validate_instance_id

    linked_instances: List[str] = []
    for raw in list(entry.get("instances") or []):
        try:
            linked_instances.append(validate_instance_id(str(raw or "").strip()))
        except Exception:
            continue
    return sorted(set(linked_instances))


def _project_runtime_hints(
    entry: Dict[str, Any],
    request: Optional[Dict[str, Any]] = None,
    *,
    include_ambient_instance: bool = True,
    include_ambient_adapter: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    linked_instances = _valid_linked_project_instances(entry)
    requested_instance = str((request or {}).get("requested_instance") or "").strip()
    ambient_instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip() if include_ambient_instance else ""
    chosen_instance = requested_instance or (linked_instances[0] if len(linked_instances) == 1 else "") or ambient_instance

    chosen_adapter = str((request or {}).get("requested_adapter_type") or "").strip().lower()
    if not chosen_adapter and chosen_instance:
        chosen_adapter = _adapter_type_from_instance_name(chosen_instance)
    if (
        include_ambient_adapter
        and not chosen_adapter
        and (not chosen_instance or chosen_instance == ambient_instance)
    ):
        chosen_adapter = str(os.environ.get("QUAID_ADAPTER_TYPE", "") or "").strip().lower()
    if not chosen_adapter and chosen_instance:
        chosen_adapter = _adapter_type_from_instance_name(chosen_instance)

    return (chosen_instance or None, chosen_adapter or None)


def _apply_project_runtime_env(
    env: Dict[str, str],
    entry: Dict[str, Any],
    request: Optional[Dict[str, Any]] = None,
    *,
    project: Optional[str] = None,
    require_instance: bool = False,
) -> Dict[str, str]:
    # Worker subprocesses outlive the shell that queued them, so derive their
    # runtime identity only from durable project/request state.
    chosen_instance, chosen_adapter = _project_runtime_hints(
        entry,
        request=request,
        include_ambient_instance=False,
        include_ambient_adapter=False,
    )
    if chosen_instance:
        env["QUAID_INSTANCE"] = chosen_instance
    elif require_instance:
        valid_linked = _valid_linked_project_instances(entry)
        name = str(project or entry.get("name") or "").strip() or "unknown"
        raise RuntimeError(
            f"cannot resolve QUAID_INSTANCE for project {name}; "
            "queued request must include requested_instance or project must be linked to exactly one valid instance "
            f"(valid_linked_instances={len(valid_linked)}: {', '.join(valid_linked) or 'none'})"
        )
    if chosen_adapter:
        env["QUAID_ADAPTER_TYPE"] = chosen_adapter
    return env


def _cross_instance_db_override_owner(raw_path: Optional[str], *, target_instance: Optional[str]) -> Optional[str]:
    path_text = str(raw_path or "").strip()
    instance = str(target_instance or "").strip()
    if not path_text or not instance or path_text == ":memory:":
        return None
    home = get_quaid_home()
    try:
        rel = Path(path_text).expanduser().resolve(strict=False).relative_to(
            (home / "instances").resolve(strict=False)
        )
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if len(parts) < 3 or parts[1] != "data":
        return None
    owner = str(parts[0] or "").strip()
    if not owner or owner == instance:
        return None
    if parts[2] not in {
        "memory.db",
        "memory.sqlite",
        "memory.sqlite3",
        "memory_archive.db",
        "memory_archive.sqlite",
        "memory_archive.sqlite3",
    }:
        return None
    return owner


@contextlib.contextmanager
def _project_runtime_context(entry: Dict[str, Any], request: Optional[Dict[str, Any]] = None) -> Iterator[None]:
    chosen_instance, chosen_adapter = _project_runtime_hints(entry, request=request)
    original_instance = os.environ.get("QUAID_INSTANCE")
    original_adapter = os.environ.get("QUAID_ADAPTER_TYPE")
    cleared_overrides: Dict[str, str] = {}
    try:
        if chosen_instance:
            os.environ["QUAID_INSTANCE"] = chosen_instance
        if chosen_adapter:
            os.environ["QUAID_ADAPTER_TYPE"] = chosen_adapter
        effective_instance = chosen_instance or original_instance
        for key in _DB_OVERRIDE_ENV_KEYS:
            raw = os.environ.get(key)
            if _cross_instance_db_override_owner(raw, target_instance=effective_instance):
                cleared_overrides[key] = str(raw)
                os.environ.pop(key, None)
        yield
    finally:
        if original_instance is None:
            os.environ.pop("QUAID_INSTANCE", None)
        elif chosen_instance is not None:
            os.environ["QUAID_INSTANCE"] = original_instance
        if original_adapter is None:
            os.environ.pop("QUAID_ADAPTER_TYPE", None)
        elif chosen_adapter is not None:
            os.environ["QUAID_ADAPTER_TYPE"] = original_adapter
        for key, value in cleared_overrides.items():
            os.environ[key] = value


def request_update(project: str, *, reason: str = "manual", requested_by: str = "cli") -> Dict[str, Any]:
    """Persist an async force-update request for a project docs worker."""
    name = validate_project_name(project)
    entry = get_project_entry(name)
    requested_instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
    requested_adapter_type = str(os.environ.get("QUAID_ADAPTER_TYPE", "") or "").strip().lower()
    if not requested_instance:
        requested_instance, requested_adapter_type = _project_runtime_hints(entry)
    elif not requested_adapter_type:
        requested_adapter_type = _adapter_type_from_instance_name(requested_instance)
    req = {
        "project": name,
        "request_id": f"{int(time.time() * 1000)}-{os.getpid()}",
        "requested_at": utc_now(),
        "requested_by": requested_by,
        "reason": reason,
        "status": "pending",
        "requested_instance": requested_instance,
        "requested_adapter_type": requested_adapter_type,
    }
    with _exclusive_file_lock(state_lock_path(name)):
        _atomic_write_json(request_path(name), req)
        state = read_state(name)
        updates = {
            "pending_request_id": req["request_id"],
            "force_requested_at": req["requested_at"],
            "last_error": None,
        }
        if str(state.get("status") or "").strip().lower() != "updating":
            updates["status"] = "queued"
        state.update(updates)
        _write_state_unlocked(name, state)
    return req


def read_update_request(project: str) -> Optional[Dict[str, Any]]:
    req = _read_json(request_path(project), None)
    return req if isinstance(req, dict) else None


def _project_docs_request_max_retries() -> int:
    raw = os.environ.get("QUAID_PROJECT_DOCS_REQUEST_MAX_RETRIES", "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            logger.warning("Invalid QUAID_PROJECT_DOCS_REQUEST_MAX_RETRIES=%r; using default", raw)
            if _fail_hard_enabled():
                raise
    return 3


def _project_docs_request_retry_base_seconds() -> float:
    raw = os.environ.get("QUAID_PROJECT_DOCS_REQUEST_RETRY_BASE_SECONDS", "").strip()
    if raw:
        try:
            parsed = float(raw)
            if parsed >= 0:
                return parsed
        except (TypeError, ValueError):
            logger.warning("Invalid QUAID_PROJECT_DOCS_REQUEST_RETRY_BASE_SECONDS=%r; using default", raw)
            if _fail_hard_enabled():
                raise
    return 30.0


def _project_docs_request_retry_delay_seconds(attempt_count: int) -> float:
    base = _project_docs_request_retry_base_seconds()
    return min(900.0, base * (2 ** max(0, int(attempt_count) - 1)))


def update_request_ready_for_worker(request: Optional[Dict[str, Any]]) -> bool:
    if not request:
        return False
    status = str(request.get("status") or "pending").strip().lower()
    if status in {"failed", "completed", "cancelled"}:
        return False
    retry_at = _parse_iso_ts(request.get("next_retry_at"))
    if retry_at is not None and _now_epoch() < retry_at:
        return False
    return True


def _record_update_request_failure(project: str, request: Dict[str, Any], exc: Exception) -> None:
    name = validate_project_name(project)
    request_id = str((request or {}).get("request_id") or "").strip()
    if not request_id:
        return
    now = utc_now()
    try:
        attempt_count = int((request or {}).get("attempt_count") or 0) + 1
    except (TypeError, ValueError):
        attempt_count = 1
    max_retries = _project_docs_request_max_retries()
    permanent = _is_permanent_update_request_failure(exc)
    payload = dict(request or {})
    payload.update({
        "attempt_count": attempt_count,
        "last_attempt_at": now,
        "last_failed_at": now,
        "last_error": str(exc),
    })
    if permanent or attempt_count >= max_retries:
        payload["status"] = "failed"
        payload["completed_at"] = now
        payload.pop("next_retry_at", None)
        _atomic_write_json(request_path(name), payload)
        error_text = (
            f"project-docs update failed permanently: {exc}"
            if permanent
            else f"project-docs update failed after {attempt_count} attempts: {exc}"
        )
        merge_state(
            name,
            {
                "status": "error",
                "pending_request_id": None,
                "last_error": error_text,
                "last_failed_at": now,
                "last_request_id": request_id,
            },
        )
        return

    delay = _project_docs_request_retry_delay_seconds(attempt_count)
    next_retry_at = (_now_datetime() + timedelta(seconds=delay)).isoformat()
    payload["status"] = "retrying"
    payload["next_retry_at"] = next_retry_at
    _atomic_write_json(request_path(name), payload)
    merge_state(
        name,
        {
            "status": "queued",
            "pending_request_id": request_id,
            "next_retry_at": next_retry_at,
            "last_error": f"project-docs update failed; retry {attempt_count}/{max_retries} queued: {exc}",
            "last_failed_at": now,
            "last_request_id": request_id,
        },
    )


def _is_permanent_update_request_failure(exc: Exception) -> bool:
    if isinstance(exc, (KeyError, ValueError)):
        return True
    if isinstance(exc, RuntimeError):
        text = str(exc).lower()
        permanent_markers = (
            "cannot resolve quaid_instance",
            "cannot resolve instance linkage",
            "project not found",
            "invalid project",
            "integrityerror",
            "constraint failed",
        )
        return any(marker in text for marker in permanent_markers)
    return False


def clear_update_request(project: str, request_id: Optional[str] = None) -> None:
    name = validate_project_name(project)
    with _exclusive_file_lock(state_lock_path(name)):
        req = read_update_request(name)
        if request_id and req and req.get("request_id") != request_id:
            return
        try:
            request_path(name).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed clearing project-docs update request for %s request_id=%s: %s", name, request_id or "-", exc)
            if _fail_hard_enabled():
                raise


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


def read_janitor_request() -> Optional[Dict[str, Any]]:
    req = _read_json(janitor_request_path(), None)
    return req if isinstance(req, dict) else None


def write_janitor_request(request: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(request or {})
    payload["updated_at"] = utc_now()
    _atomic_write_json(janitor_request_path(), payload)
    return payload


def clear_janitor_request(request_id: Optional[str] = None) -> None:
    req = read_janitor_request()
    if request_id and req and req.get("request_id") != request_id:
        return
    try:
        janitor_request_path().unlink(missing_ok=True)
    except OSError:
        pass


def request_janitor_run(
    *,
    instance: Optional[str] = None,
    reason: str = "manual",
    requested_by: str = "cli",
) -> Dict[str, Any]:
    """Persist an async supervisor-owned janitor request."""
    from lib.instance import validate_instance_id

    with _exclusive_file_lock(_spawn_lock_path("janitor-request")):
        existing = read_janitor_request()
        if existing and str(existing.get("status") or "").strip() in {"pending", "running"}:
            raise RuntimeError(
                "Janitor request already in progress "
                f"({existing.get('request_id') or 'unknown request'})"
            )
        req = {
            "request_id": f"{int(time.time() * 1000)}-{os.getpid()}",
            "requested_at": utc_now(),
            "requested_by": str(requested_by or "cli").strip() or "cli",
            "reason": str(reason or "manual").strip() or "manual",
            "scope": "instance" if instance else "all",
            "instance": validate_instance_id(instance) if instance else None,
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "exit_codes": {},
            "errors": [],
        }
        return write_janitor_request(req)


def wait_for_janitor_request(request_id: str, *, timeout_seconds: float = 300.0) -> Dict[str, Any]:
    deadline = time.time() + max(0.1, float(timeout_seconds))
    while time.time() < deadline:
        req = read_janitor_request()
        if req and req.get("request_id") == request_id and str(req.get("status") or "").strip() in {
            "completed",
            "failed",
        }:
            return req
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for janitor request {request_id}")


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


def _project_log_path(entry: Dict[str, Any], project: Optional[str] = None) -> Optional[Path]:
    candidates: List[Path] = []
    raw = str(entry.get("canonical_path") or "").strip()
    if raw:
        candidates.append(Path(raw) / PROJECT_LOG)
    if project:
        managed = _managed_project_log_path(project)
        if managed is not None and managed not in candidates:
            candidates.append(managed)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0] if candidates else None


def _managed_project_log_path(project: str) -> Optional[Path]:
    try:
        from lib.runtime_context import get_projects_dir

        return get_projects_dir() / validate_project_name(project) / PROJECT_LOG
    except Exception as exc:
        logger.warning("Failed resolving managed PROJECT.log path for %s: %s", project, exc)
        if _fail_hard_enabled():
            raise
        return None


def _read_project_log_since(entry: Dict[str, Any], offset: int, project: Optional[str] = None) -> Tuple[List[str], int, int]:
    log_path = _project_log_path(entry, project=project)
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


def _current_project_log_size(entry: Dict[str, Any], project: Optional[str] = None) -> int:
    log_path = _project_log_path(entry, project=project)
    if not log_path or not log_path.is_file():
        return 0
    try:
        return int(log_path.stat().st_size)
    except Exception as exc:
        logger.exception("Failed stat for PROJECT.log: %s", log_path)
        raise RuntimeError(f"failed to stat PROJECT.log at {log_path}: {exc}") from exc


def _pending_project_log_queue_count(project: str) -> int:
    try:
        from core.docs import updater as docs_updater

        return int(docs_updater.pending_project_log_count(project) or 0)
    except Exception as exc:
        logger.warning("Failed checking project-log queue for %s: %s", project, exc)
        if _fail_hard_enabled():
            raise
        return 0


def _empty_project_log_queue_metrics() -> Dict[str, int]:
    return {
        "items_seen": 0,
        "items_committed": 0,
        "entries_seen": 0,
        "history_entries_written": 0,
        "errors": 0,
    }


def _queued_project_log_projects(project: Optional[str] = None) -> List[str]:
    from core.docs import updater as docs_updater

    if project:
        return docs_updater.queued_project_log_projects(validate_project_name(project))
    return docs_updater.queued_project_log_projects()


def materialize_queued_projects(project: Optional[str] = None) -> int:
    """Create durable projects for valid transcript-driven PROJECT.log queues."""
    from core.project_registry import create_project, project_deleted_raw, project_exists_raw

    created = 0
    for name in _queued_project_log_projects(project):
        if name == "quaid" or name.startswith("misc--"):
            logger.info("Skipping queued PROJECT.log auto-create for reserved project %s", name)
            continue
        if project_exists_raw(name):
            continue
        if project_deleted_raw(name):
            logger.info("Skipping queued PROJECT.log auto-create for deleted project %s", name)
            continue
        try:
            create_project(
                name,
                description="Project inferred from conversation continuity.",
            )
            created += 1
        except ValueError as exc:
            logger.info("Skipping queued PROJECT.log auto-create for %s: %s", name, exc)
        except Exception as exc:
            logger.warning("Failed auto-creating queued PROJECT.log project %s: %s", name, exc)
            if _fail_hard_enabled():
                raise
    return created


def _commit_queued_project_logs(project: str, *, dry_run: bool = False) -> Dict[str, int]:
    """Commit queued project-log entries under the project-docs worker lock."""
    name = validate_project_name(project)
    metrics = _empty_project_log_queue_metrics()
    try:
        from core.docs import updater as docs_updater

        queue_lock = docs_updater.project_log_queue_lock(name)
    except Exception as exc:
        metrics["errors"] += 1
        logger.warning("Failed initializing project-log queue commit for %s: %s", name, exc)
        if _fail_hard_enabled():
            raise
        return metrics

    with queue_lock:
        try:
            items = docs_updater.drain_project_log_queue(name)
        except Exception as exc:
            metrics["errors"] += 1
            logger.warning("Failed draining project-log queue for %s: %s", name, exc)
            if _fail_hard_enabled():
                raise
            return metrics

        for item in items:
            item_id = str(item.get("id") or "").strip()
            dropped_entries = int(item.get("_dropped_entries_count") or 0)
            entries = [
                entry
                for entry in (item.get("entries") or [])
                if (
                    str(entry.get("text", "")).strip()
                    if isinstance(entry, dict)
                    else str(entry).strip()
                )
            ]
            metrics["items_seen"] += 1
            metrics["entries_seen"] += len(entries) + max(0, dropped_entries)
            if not item_id:
                metrics["errors"] += 1
                if _fail_hard_enabled():
                    raise RuntimeError(f"project-log queue item missing id for {name}")
                logger.warning("Skipping project-log queue item without id for %s", name)
                continue
            if dropped_entries:
                metrics["errors"] += 1
                message = f"project-log queue item {item_id} for {name} dropped {dropped_entries} malformed entries"
                if _fail_hard_enabled():
                    raise RuntimeError(message)
                logger.critical("%s; dead-lettering item for inspection", message)
                docs_updater.dead_letter_project_log_queue_item(name, item_id, message)
                continue
            if not entries:
                if not dry_run:
                    docs_updater.mark_project_log_queue_committed(name, [item_id])
                metrics["items_committed"] += 1
                continue
            try:
                commit_metrics = docs_updater.append_project_logs(
                    {name: entries},
                    trigger=str(item.get("trigger") or "CLI"),
                    date_str=str(item.get("date_str") or "") or None,
                    dry_run=dry_run,
                    index_history=False,
                    update_project_md=False,
                )
                metrics["history_entries_written"] += int(commit_metrics.get("history_entries_written", 0) or 0)
                if not dry_run:
                    docs_updater.mark_project_log_queue_committed(name, [item_id])
                metrics["items_committed"] += 1
            except Exception as exc:
                metrics["errors"] += 1
                logger.warning("Failed committing project-log queue item %s for %s: %s", item_id, name, exc)
                if _fail_hard_enabled():
                    raise
                continue
    return metrics


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
    with _project_runtime_context(entry):
        state = read_state(name)
        req = read_update_request(name)
        req_status = str((req or {}).get("status") or "pending").strip().lower()
        request_active = bool(req) and req_status not in {"failed", "completed", "cancelled"}
        worker_pid = read_worker_pid(name)
        worker_heartbeat = read_worker_heartbeat(name)
        log_path = worker_log_path(name)
        supervisor_pid = read_supervisor_pid()
        sg = _shadow_git(name, entry)
        current_shadow_head = sg.current_head() if sg is not None else None
        docs_cursor_head = state.get("last_shadow_commit")
        shadow_cursor_pending = bool(current_shadow_head and current_shadow_head != docs_cursor_head)
        source_error = None
        if sg is None:
            changes = []
        else:
            try:
                changes = pending_source_changes(name, entry)
            except RuntimeError as exc:
                logger.warning("Project docs status source check failed for %s: %s", name, exc)
                changes = []
                source_error = str(exc)
        log_offset = int(state.get("project_log_offset") or 0)
        log_size = _current_project_log_size(entry, project=name)
        log_queue_pending = _pending_project_log_queue_count(name)
        log_pending = max(0, log_size - min(log_offset, log_size))
        stale = request_active or bool(changes) or shadow_cursor_pending or log_pending > 0 or log_queue_pending > 0
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
            "project_log_queue_pending": log_queue_pending,
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


def project_has_pending_update(project: str) -> bool:
    """Lightweight worker tick predicate for pending docs work.

    `project_status()` is display-oriented and reads log tails plus detailed
    change lists. The worker loop only needs to know whether there is work.
    """
    name = validate_project_name(project)
    entry = get_project_entry(name)
    with _project_runtime_context(entry):
        req = read_update_request(name)
        req_status = str((req or {}).get("status") or "pending").strip().lower()
        if bool(req) and req_status not in {"failed", "completed", "cancelled"}:
            return True
        state = read_state(name)
        sg = _shadow_git(name, entry)
        if sg is not None:
            current_shadow_head = sg.current_head()
            docs_cursor_head = state.get("last_shadow_commit")
            if current_shadow_head and current_shadow_head != docs_cursor_head:
                return True
            try:
                if pending_source_changes(name, entry):
                    return True
            except RuntimeError as exc:
                logger.warning("Project docs worker source check failed for %s: %s", name, exc)
                if _fail_hard_enabled():
                    raise
        log_offset = int(state.get("project_log_offset") or 0)
        log_size = _current_project_log_size(entry, project=name)
        if max(0, log_size - min(log_offset, log_size)) > 0:
            return True
        return _pending_project_log_queue_count(name) > 0


def project_diff(project: str, *, full: bool = False) -> Dict[str, Any]:
    name = validate_project_name(project)
    entry = get_project_entry(name)
    with _project_runtime_context(entry):
        sg = _shadow_git(name, entry)
        source_error = None
        if sg is None:
            changes = []
        else:
            try:
                changes = pending_source_changes(name, entry)
            except RuntimeError as exc:
                logger.warning("Project docs diff source check failed for %s: %s", name, exc)
                changes = []
                source_error = str(exc)
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
                    diff_text = f"{diff_text}\n{committed_diff}".strip() if diff_text else committed_diff
            try:
                pending_diff = sg.pending_diff(full=full) or ""
                if pending_diff:
                    diff_text = f"{diff_text}\n{pending_diff}".strip() if diff_text else pending_diff
            except Exception as exc:
                logger.exception("Failed reading pending source diff for project %s", name)
                raise RuntimeError(f"failed to read pending source diff for {name}: {exc}") from exc
        log_offset = int(state.get("project_log_offset") or 0)
        log_lines, _, log_size = _read_project_log_since(entry, log_offset, project=name)
        return {
            "project": name,
            "full": bool(full),
            "changes": changes,
            "change_count": len(changes),
            "diff": diff_text,
            "project_log_entries": log_lines,
            "project_log_entry_count": len(log_lines),
            "project_log_size": log_size,
            "source_error": source_error,
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
    from core.project_registry import list_projects

    materialize_queued_projects(project)

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


def _validate_project_docs_update_broker_response(response: Any) -> Dict[str, Any]:
    if not isinstance(response, dict):
        raise RuntimeError(f"project-docs update broker response must be an object, got {type(response).__name__}")
    if str(response.get("status") or "") != "ok":
        error = str(response.get("error") or "project-docs update broker request failed")
        responses = response.get("responses")
        if isinstance(responses, list) and len(responses) == 1 and isinstance(responses[0], dict):
            result_payload = responses[0].get("result")
            if isinstance(result_payload, dict) and result_payload.get("error"):
                error = str(result_payload.get("error"))
            elif responses[0].get("error"):
                error = str(responses[0].get("error"))
        raise RuntimeError(error)
    responses = response.get("responses")
    if not isinstance(responses, list) or len(responses) != 1:
        raise RuntimeError("project-docs update broker response must contain exactly one handler response")
    handler_response = responses[0]
    if not isinstance(handler_response, dict):
        raise RuntimeError("project-docs update broker handler response must be an object")
    if str(handler_response.get("datastore_id") or "") != "docsdb":
        raise RuntimeError("project-docs update broker handler must be docsdb")
    if str(handler_response.get("status") or "") not in {"ok", "acked"}:
        result_payload = handler_response.get("result")
        result_error = result_payload.get("error") if isinstance(result_payload, dict) else None
        raise RuntimeError(str(handler_response.get("error") or result_error or "project-docs update broker handler failed"))
    result = handler_response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("project-docs update broker handler result must be an object")
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        raise RuntimeError("project-docs update broker result.metrics must be an object")
    registry_sync = result.get("registry_sync")
    if not isinstance(registry_sync, dict):
        raise RuntimeError("project-docs update broker result.registry_sync must be an object")
    try:
        indexed_docs = int(result.get("indexed_docs") or 0)
        indexed_project_logs = int(result.get("indexed_project_logs") or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("project-docs update broker indexed counts must be integers") from exc
    return {
        "metrics": dict(metrics),
        "registry_sync": {
            "registered": int(registry_sync.get("registered") or 0),
            "unregistered": int(registry_sync.get("unregistered") or 0),
            "project_md_refreshed": int(registry_sync.get("project_md_refreshed") or 0),
        },
        "indexed_docs": indexed_docs,
        "indexed_project_logs": indexed_project_logs,
    }


def _request_project_docs_update_via_broker(
    project: str,
    *,
    snapshots: List[Dict[str, Any]],
    project_log_entries: List[str],
    project_log_offset: int,
    request: Optional[Dict[str, Any]],
    dry_run: bool,
) -> Dict[str, Any]:
    from core.plugins.docsdb_contract import register_project_docs_update_request_handler
    from core.runtime.events import DOCS_PROJECT_UPDATE_REQUEST_EVENT, request_broker_event

    register_project_docs_update_request_handler()
    try:
        response = request_broker_event(
            DOCS_PROJECT_UPDATE_REQUEST_EVENT,
            {
                "source": WORKER_ROLE,
                "project": project,
                "request_id": str((request or {}).get("request_id") or ""),
                "dry_run": bool(dry_run),
                "snapshots": list(snapshots or []),
                "project_log_entries": list(project_log_entries or []),
                "project_log_offset": int(project_log_offset),
                "request": dict(request or {}),
            },
            source="core.project_docs.execute_update_once",
            project_id=project,
            provenance={
                "replacement": "core.project_docs.execute_update_once direct project-doc apply/index operation",
            },
        )
    except RuntimeError as exc:
        if exc.__cause__ is not None:
            raise RuntimeError(str(exc.__cause__)) from exc.__cause__
        raise
    return _validate_project_docs_update_broker_response(response)


def _notify_project_docs_update(
    project: str,
    result: Dict[str, Any],
    *,
    entry: Optional[Dict[str, Any]] = None,
    request: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        with _project_runtime_context(entry or get_project_entry(project), request=request):
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
    except Exception as exc:
        logger.warning("Failed to queue project docs update notice for %s: %s", project, exc)
        if _fail_hard_enabled():
            raise


def execute_update_once(project: str, *, request: Optional[Dict[str, Any]] = None, dry_run: bool = False) -> Dict[str, Any]:
    """Run one project-doc update under the project lock.

    This is the primitive the project docs worker owns. It snapshots source
    changes, reads PROJECT.log from the last cursor, updates docs, reindexes the
    project's registered docs, and advances hidden cursors only after the apply
    phase returns.
    """
    name = validate_project_name(project)
    entry = get_project_entry(name)
    if request is None:
        current_request = read_update_request(name)
        request = current_request if update_request_ready_for_worker(current_request) else None
    request_id = str((request or {}).get("request_id") or "") or None
    with project_update_lock(name, blocking=False) as acquired:
        if not acquired:
            logger.info("Project docs update lock busy for %s request_id=%s", name, request_id or "-")
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
        logger.info("Project docs update starting for %s request_id=%s", name, request_id or "-")
        merge_state(name, {"status": "updating", "last_started_at": started, "last_error": None})
        merge_progress(name, "starting", "project-docs update started")
        try:
            with _project_runtime_context(entry, request=request):
                state = read_state(name)
                log_offset = int(state.get("project_log_offset") or 0)
                docs_cursor_head = state.get("last_shadow_commit")
                merge_progress(name, "commit_project_log_queue", "committing queued PROJECT.log entries")
                project_log_queue_metrics = _commit_queued_project_logs(name, dry_run=dry_run)
                merge_progress(name, "read_project_log", "reading PROJECT.log cursor", project_log_offset=log_offset)
                log_entries, _old_log_offset, log_size = _read_project_log_since(entry, log_offset, project=name)
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
                project_log_index_count = 0
                should_apply_docs = bool(snapshots or log_entries or request)
                if should_apply_docs:
                    merge_progress(
                        name,
                        "update_docs",
                        "running project docs update",
                        snapshot_changes=len((snapshot or {}).get("changes") or []),
                        project_log_entries=len(log_entries),
                        request_id=request_id,
                    )
                if not dry_run and not should_apply_docs:
                    merge_progress(name, "index_docs", "indexing registered project docs")
                broker_result = _request_project_docs_update_via_broker(
                    name,
                    snapshots=snapshots,
                    project_log_entries=log_entries,
                    project_log_offset=log_offset,
                    request=request,
                    dry_run=dry_run,
                )
                metrics = broker_result["metrics"]
                if project_log_queue_metrics.get("errors"):
                    metrics["errors"] = int(metrics.get("errors", 0) or 0) + int(project_log_queue_metrics.get("errors", 0) or 0)
                metrics["project_log_queue"] = project_log_queue_metrics
                registry_sync = broker_result["registry_sync"]
                index_count = int(broker_result["indexed_docs"] or 0)
                project_log_index_count = int(broker_result["indexed_project_logs"] or 0)
            completed = utc_now()
            next_state = {
                "status": "fresh" if not metrics.get("errors") else "error",
                "last_completed_at": completed,
                "last_progress_update": completed,
                "last_request_id": request_id,
                "last_metrics": metrics,
                "last_registry_sync": registry_sync,
                "last_indexed_docs": index_count,
                "last_indexed_project_logs": project_log_index_count,
                "project_log_offset": log_size,
                "phase": "idle",
                "progress": {
                    "phase": "idle",
                    "message": "project-docs update complete",
                    "updated_at": completed,
                    "docs_updated": int(metrics.get("docs_updated") or 0),
                    "docs_registered": int(registry_sync.get("registered") or 0),
                    "indexed_docs": index_count,
                    "indexed_project_logs": project_log_index_count,
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
                "project_log_queue": project_log_queue_metrics,
                "metrics": metrics,
                "registry_sync": registry_sync,
                "indexed_docs": index_count,
                "indexed_project_logs": project_log_index_count,
            }
            if not dry_run and next_state["status"] == "fresh":
                _notify_project_docs_update(name, result, entry=entry, request=request)
            logger.info(
                "Project docs update completed for %s request_id=%s status=%s docs_updated=%s errors=%s",
                name,
                request_id or "-",
                next_state["status"],
                int(metrics.get("docs_updated") or 0),
                int(metrics.get("errors") or 0),
            )
            return result
        except Exception as exc:
            logger.warning("Project docs update failed for %s request_id=%s: %s", name, request_id or "-", exc)
            if request_id and not dry_run:
                _record_update_request_failure(name, request or {}, exc)
                raise
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
    token = os.environ.get("QUAID_PROJECT_DOCS_WORKER_TOKEN", "").strip() or None
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
    return (_now_epoch() - ts) > stale_after_seconds


def _worker_progress_stale(project: str, *, stale_after_seconds: float) -> bool:
    state = read_state(project)
    if str(state.get("status") or "").strip().lower() != "updating":
        return False
    progress = state.get("progress") if isinstance(state.get("progress"), dict) else {}
    ts = (
        _parse_iso_ts(state.get("last_progress_update"))
        or _parse_iso_ts(progress.get("updated_at"))
        or _parse_iso_ts(state.get("last_started_at"))
    )
    if ts is None:
        return True
    return (_now_epoch() - ts) > stale_after_seconds


def reap_stale_worker(project: str, *, stale_after_seconds: float) -> bool:
    name = validate_project_name(project)
    pid = read_worker_pid(name)
    heartbeat_stale = _worker_heartbeat_stale(name, stale_after_seconds=stale_after_seconds)
    progress_stale = _worker_progress_stale(name, stale_after_seconds=stale_after_seconds)
    stale = heartbeat_stale or progress_stale
    if pid is not None and not stale:
        return False
    if pid is not None and stale:
        stale_reason = "progress" if progress_stale and not heartbeat_stale else "heartbeat"
        logger.warning("Project docs worker %s stale for %s; restarting pid=%s", stale_reason, name, pid)
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
                "last_error": "worker stopped or stale during update; retry queued",
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
        record = _read_pid_record(path)
        if (
            record
            and int(record.get("pid") or 0) == int(expected_pid)
            and record.get("role") == role
            and (project is None or record.get("project") == validate_project_name(project))
            and _pid_alive(expected_pid)
        ):
            return int(expected_pid)
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


def _terminate_supervisor_pid(pid: int, *, grace_seconds: float = 5.0) -> None:
    if pid <= 0 or not _pid_alive(pid):
        return
    try:
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            os.kill(pid, signal.SIGTERM)
        deadline = time.time() + max(0.5, grace_seconds)
        while time.time() < deadline:
            if not _pid_alive(pid):
                return
            time.sleep(0.1)
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            os.kill(pid, signal.SIGKILL)
    except OSError:
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
        matching = _matching_supervisor_pids()
        if existing is not None and (not matching or matching == [existing]):
            return existing
        if matching:
            for pid in matching:
                _terminate_supervisor_pid(pid)
            try:
                supervisor_pid_path().unlink(missing_ok=True)
            except OSError:
                pass
        supervisor_dir().mkdir(parents=True, exist_ok=True)
        log_path = supervisor_log_path()
        script = Path(__file__).parent / "project_docs_supervisor.py"
        env = scrub_background_process_env(dict(os.environ))
        for key in _DB_OVERRIDE_ENV_KEYS:
            env.pop(key, None)
        env["QUAID_HOME"] = str(get_quaid_home())
        env = _hydrate_anthropic_api_key_from_shared_auth(env)
        env["QUAID_SUPERVISOR_BOOT"] = "1"
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
        targets: List[int] = []
        if record and _pid_record_matches(record, role=SUPERVISOR_ROLE):
            targets.append(pid)
        for match_pid in _matching_supervisor_pids():
            if match_pid not in targets:
                targets.append(match_pid)
        if not targets:
            if record:
                try:
                    supervisor_pid_path().unlink(missing_ok=True)
                except OSError:
                    pass
            return False
        try:
            for target_pid in targets:
                _terminate_supervisor_pid(target_pid)
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
            try:
                supervisor_pid_path().unlink(missing_ok=True)
            except OSError:
                pass


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
        entry = get_project_entry(name)
        request = read_update_request(name)
        env = scrub_background_process_env(dict(os.environ))
        for key in _DB_OVERRIDE_ENV_KEYS:
            env.pop(key, None)
        env["QUAID_HOME"] = str(get_quaid_home())
        env = _apply_project_runtime_env(env, entry, request=request, project=name, require_instance=True)
        env.setdefault("QUAID_PROJECT_DOCS_WORKER_INTERVAL_SECONDS", "5")
        env = _hydrate_anthropic_api_key_from_shared_auth(env)
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
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
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
    lines.append(f"Pending PROJECT.log queue items: {status.get('project_log_queue_pending', 0)}")
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
    status_value = str(status.get("status") or "").strip().lower()
    show_last_error = status_value != "fresh"
    if show_last_error and state.get("last_error"):
        lines.append(f"Last error: {state.get('last_error')}")
    tail = status.get("worker_log_tail") or []
    metrics = state.get("last_metrics") if isinstance(state.get("last_metrics"), dict) else {}
    if status_value == "fresh" and int(metrics.get("docs_updated") or 0) > 0:
        tail = [
            line for line in tail
            if "QUAID_INSTANCE environment variable is not set" not in str(line)
        ]
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
