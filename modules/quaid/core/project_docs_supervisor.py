#!/usr/bin/env python3
"""Quaid runtime supervisor entrypoint.

The supervisor owns long-lived monitor process lifecycle. Domain workers still
own their own work: instance daemons process extraction signals, project-docs
workers apply docs updates, and janitor workers run bounded maintenance ticks.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import project_docs
from core.project_registry import is_misc_project_deleted, list_projects, list_projects_raw
from lib.instance import internal_path_derived_instance_ids, list_instances, quaid_home, validate_instance_id

_STOP = False
_INSTANCE_DB_OVERRIDE_ENV_KEYS = ("MEMORY_DB_PATH", "MEMORY_ARCHIVE_DB_PATH")
_LOGGER = logging.getLogger(__name__)


def _handle_stop(_signum, _frame) -> None:
    global _STOP
    _STOP = True


def _interval_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.5, float(raw))
    except ValueError:
        return default


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled
    except ImportError as exc:
        logging.getLogger(__name__).critical("fail-hard policy unavailable in project docs supervisor: %s", exc)
        raise RuntimeError("fail-hard policy unavailable in project docs supervisor") from exc
    return bool(is_fail_hard_enabled())


def _janitor_request_lock():
    return project_docs._exclusive_file_lock(project_docs._spawn_lock_path("janitor-request"))


def _emit_project_docs_maintenance_event(
    *,
    observed_at: float,
    auto_register_interval: float,
    stale_doc_interval: float,
    auto_register_requested: bool,
    stale_index_requested: bool,
) -> None:
    if not auto_register_requested and not stale_index_requested:
        return
    payload = {
        "project": None,
        "observed_at": datetime.fromtimestamp(observed_at, tz=timezone.utc).isoformat(),
        "source": "project-docs-supervisor",
        "tick_kind": "auto_register_and_stale_index",
        "auto_register_interval_seconds": float(auto_register_interval),
        "stale_index_interval_seconds": float(stale_doc_interval),
        "requested_operations": {
            "auto_register": bool(auto_register_requested),
            "stale_index": bool(stale_index_requested),
        },
        "dry_run": False,
    }
    try:
        from core.runtime.events import (
            DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT,
            dispatch_broker_events,
            emit_broker_event,
        )

        emit_broker_event(
            DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT,
            payload=payload,
            source="project-docs-supervisor",
        )
        dispatched = dispatch_broker_events(limit=20, names=[DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT])
    except Exception as exc:
        logging.getLogger(__name__).warning("project docs maintenance event failed: %s", exc)
        if _fail_hard_enabled():
            raise
        return
    if int(dispatched.get("failed") or 0) > 0:
        logging.getLogger(__name__).warning(
            "project docs maintenance listener failed: %s",
            dispatched,
        )
        if _fail_hard_enabled():
            raise RuntimeError(f"project docs maintenance listener failed: {dispatched}")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_matches_janitor_worker(pid: int) -> bool:
    if not _pid_alive(pid):
        return False
    command = _process_command(pid)
    if command is None:
        return True
    return "janitor_worker" in command


def _instance_root(instance: str) -> Path:
    return quaid_home() / "instances" / validate_instance_id(instance)


def _instance_child_env(instance: str, *, daemon: bool = False) -> dict[str, str]:
    """Build an env for a supervisor-owned per-instance child process."""
    name = validate_instance_id(instance)
    env = project_docs.scrub_background_process_env(dict(os.environ))
    for key in _INSTANCE_DB_OVERRIDE_ENV_KEYS:
        env.pop(key, None)
    env["QUAID_HOME"] = str(quaid_home())
    env["QUAID_INSTANCE"] = name
    env["QUAID_SUPERVISOR_PID"] = str(os.getpid())
    if daemon:
        env["QUAID_DAEMON"] = "1"
    return env


def _instance_daemon_pid_path(instance: str) -> Path:
    return _instance_root(instance) / "data" / "extraction-daemon.pid"


def _instance_daemon_log_path(instance: str) -> Path:
    log_dir = _instance_root(instance) / "logs" / "daemon"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "extraction-daemon.log"


def _janitor_worker_log_path(instance: str) -> Path:
    log_dir = _instance_root(instance) / "logs" / "janitor"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "supervisor-worker.log"


def _process_command(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout or ""
    except Exception as exc:
        _LOGGER.debug("ps command inspection failed for pid=%s: %s", pid, exc)
        return None


def _read_instance_daemon_pid(instance: str) -> int | None:
    path = _instance_daemon_pid_path(instance)
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except Exception as exc:
        _LOGGER.warning("failed to read daemon pid for %s, treating as dead: %s", instance, exc)
        pid = 0
    if pid > 0 and _pid_alive(pid):
        command = _process_command(pid)
        # Fail open on command inspection errors so transient ps failures do
        # not cause duplicate monitor spawns for a live instance daemon.
        if command is None or "extraction_daemon" in command:
            return pid
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def _instance_misc_project_deleted(instance: str) -> bool:
    try:
        return bool(is_misc_project_deleted(instance, quaid_home=quaid_home()))
    except Exception as exc:
        _LOGGER.warning("failed to check misc project deletion state for %s: %s", instance, exc)
        if _fail_hard_enabled():
            raise
        return False


def _live_instances_for_supervisor() -> tuple[set[str], set[str]]:
    configured_path_derived = _internal_path_derived_instances_on_disk()
    all_instances = set(list_instances()) | configured_path_derived
    inactive = {
        instance
        for instance in all_instances
        if _instance_misc_project_deleted(instance) or project_docs.is_instance_monitor_disabled(instance)
    }
    return all_instances - inactive, inactive


def _internal_path_derived_instances_on_disk() -> set[str]:
    root = quaid_home() / "instances"
    if not root.is_dir():
        return set()
    return {
        instance
        for instance in internal_path_derived_instance_ids(quaid_home())
        if (root / instance / "config.json").is_file()
    }


def _wait_for_instance_pid(
    instance: str,
    expected_pid: Optional[int],
    timeout_seconds: float | None = None,
    proc: subprocess.Popen | None = None,
) -> int:
    timeout = project_docs.pid_startup_wait_seconds() if timeout_seconds is None else float(timeout_seconds)
    deadline = time.time() + max(0.5, timeout)
    last_seen_pid: int | None = None
    while time.time() < deadline:
        pid = _read_instance_daemon_pid(instance)
        if pid is not None:
            last_seen_pid = pid
        if expected_pid is None and pid is not None:
            return pid
        if expected_pid is not None and pid == int(expected_pid):
            return pid
        if pid is not None:
            if proc is not None and proc.poll() is None:
                project_docs._terminate_process(proc)
            return pid
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"instance monitor {instance} exited before writing pid file rc={proc.returncode}")
        time.sleep(0.1)
    detail = f"last_seen_pid={last_seen_pid}" if last_seen_pid is not None else "no live pid observed"
    expected = f"pid {expected_pid}" if expected_pid is not None else "a live daemon pid"
    raise TimeoutError(f"instance monitor {instance} did not write pid file for {expected} ({detail})")


def _daemon_pid_has_supervisor_token(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "eww", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception as exc:
        _LOGGER.warning("ps supervisor token check failed for pid=%s; treating as no supervisor token: %s", pid, exc)
        return False
    command = result.stdout or ""
    return " QUAID_SUPERVISOR_PID=" in f" {command}"


def _start_instance_monitor(instance: str) -> int:
    name = validate_instance_id(instance)
    if _instance_misc_project_deleted(name):
        raise RuntimeError(f"refusing to start monitor for deleted misc instance {name}")
    if project_docs.is_instance_monitor_disabled(name):
        raise RuntimeError(f"refusing to start monitor for disabled instance {name}")
    existing = _read_instance_daemon_pid(name)
    if existing is not None:
        return existing
    from core import extraction_daemon as _extraction_daemon

    matching_workers = _extraction_daemon._matching_daemon_pids(quaid_home=quaid_home(), instance=name)
    matching_all = _extraction_daemon._matching_daemon_pids(
        quaid_home=quaid_home(),
        instance=name,
        include_foreground=True,
    )
    detached_workers = [pid for pid in matching_workers if not _daemon_pid_has_supervisor_token(pid)]
    if len(detached_workers) == 1 and matching_all == detached_workers:
        _extraction_daemon.write_pid(detached_workers[0])
        return detached_workers[0]
    if matching_all:
        logging.getLogger(__name__).warning(
            "reaping %d matching extraction daemon(s) before supervisor spawn for %s: %s",
            len(matching_all),
            name,
            ",".join(str(pid) for pid in matching_all),
        )
        for pid in matching_all:
            _extraction_daemon._terminate_daemon_pid(pid)
        try:
            _instance_daemon_pid_path(name).unlink(missing_ok=True)
        except OSError:
            pass
    script = Path(__file__).parent / "extraction_daemon.py"
    env = _instance_child_env(name, daemon=True)
    for key in list(env):
        if key.startswith("QUAID_SUPERVISOR_"):
            env.pop(key, None)
    env["QUAID_SUPERVISOR_DISABLE"] = "1"
    with _instance_daemon_log_path(name).open("ab") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(script), "start"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            env=env,
        )
    try:
        return _wait_for_instance_pid(name, None, proc=proc)
    except Exception:
        project_docs._terminate_process(proc)
        raise


def _stop_instance_monitor(instance: str) -> bool:
    name = validate_instance_id(instance)
    from core import extraction_daemon as _extraction_daemon

    targets: list[int] = []
    pid = _read_instance_daemon_pid(name)
    if pid is not None:
        targets.append(pid)
    for match_pid in _extraction_daemon._matching_daemon_pids(
        quaid_home=quaid_home(),
        instance=name,
        include_foreground=True,
    ):
        if match_pid not in targets:
            targets.append(match_pid)
    if not targets:
        try:
            _instance_daemon_pid_path(name).unlink(missing_ok=True)
        except OSError:
            pass
        return False
    for target_pid in targets:
        _extraction_daemon._terminate_daemon_pid(target_pid)
    try:
        _instance_daemon_pid_path(name).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _janitor_check_interval_seconds() -> float:
    return _interval_from_env("QUAID_SUPERVISOR_JANITOR_CHECK_INTERVAL_SECONDS", 900.0)


def _dispatcher_only_mode() -> bool:
    return bool(os.environ.get("QUAID_SUPERVISOR_BOOT", "").strip()) or not bool(
        os.environ.get("QUAID_INSTANCE", "").strip()
    )


def _supervisor_projects() -> Dict[str, Dict[str, object]]:
    return list_projects_raw() if _dispatcher_only_mode() else list_projects()


def _valid_linked_project_instances(entry: Dict[str, object]) -> list[str]:
    linked_instances: list[str] = []
    for raw in list(entry.get("instances") or []):
        try:
            linked_instances.append(validate_instance_id(str(raw or "").strip()))
        except Exception as exc:
            _LOGGER.debug("skipping invalid linked project instance %r: %s", raw, exc)
            continue
    return sorted(set(linked_instances))


def _project_worker_start_skip_reason(project: str, entry: Dict[str, object]) -> Optional[str]:
    if project_docs.read_worker_pid(project) is not None:
        return None
    request = project_docs.read_update_request(project) or {}
    if str(request.get("requested_instance") or "").strip():
        return None
    valid_linked = _valid_linked_project_instances(entry)
    if len(valid_linked) == 1:
        return None
    return (
        f"cannot resolve QUAID_INSTANCE for project {project}; queued request must include "
        "requested_instance or project must be linked to exactly one valid instance "
        f"(valid_linked_instances={len(valid_linked)}: {', '.join(valid_linked) or 'none'})"
    )


def _handle_known_project_worker_exit(project: str, known_workers: Dict[str, int]) -> bool:
    known_pid = int(known_workers.get(project, 0) or 0)
    if known_pid <= 0:
        return False
    if project_docs.read_worker_pid(project) is not None:
        return False
    message = f"project docs worker for {project} exited unexpectedly pid={known_pid}"
    _LOGGER.warning(message)
    request = project_docs.read_update_request(project)
    if request:
        status = str(request.get("status") or "").strip().lower()
        if status in {"failed", "completed", "cancelled"}:
            _LOGGER.info(
                "project docs worker exit for %s already recorded in terminal request status=%s; "
                "containing supervisor-level raise",
                project,
                status,
            )
            known_workers.pop(project, None)
            return True
        try:
            project_docs.record_update_request_worker_exit(project, request, message)
        except Exception as exc:
            _LOGGER.warning("failed to record project docs worker exit for %s: %s", project, exc)
            if _fail_hard_enabled():
                raise
        # The worker failure is now durable in the request/state files. Raising
        # here under failHard would only cascade into a global supervisor marker.
        _LOGGER.warning(
            "project docs worker exit for %s recorded in active request; containing supervisor-level raise",
            project,
        )
        known_workers.pop(project, None)
        return True
    else:
        state = project_docs.read_state(project)
        state_status = str(state.get("status") or "").strip().lower()
        if state_status in {"fresh", "error", "stopped"}:
            _LOGGER.info(
                "project docs worker exit for %s already reflected in project state status=%s; "
                "containing supervisor-level raise",
                project,
                state_status,
            )
            known_workers.pop(project, None)
            return True
        project_docs.merge_state(
            project,
            {
                "status": "error",
                "last_error": message,
                "last_failed_at": project_docs.utc_now(),
            },
        )
    known_workers.pop(project, None)
    if _fail_hard_enabled():
        raise RuntimeError(message)
    return True


def _start_janitor_worker(instance: str) -> subprocess.Popen:
    return _spawn_janitor_worker(instance, command="scheduler-once")


def _spawn_janitor_worker(instance: str, *, command: str) -> subprocess.Popen:
    name = validate_instance_id(instance)
    if _instance_misc_project_deleted(name):
        raise RuntimeError(f"refusing to start janitor worker for deleted misc instance {name}")
    if command != "run-all-once" and project_docs.is_instance_monitor_disabled(name):
        raise RuntimeError(f"refusing to start janitor worker for disabled instance {name}")
    script = Path(__file__).parent / "janitor_worker.py"
    env = _instance_child_env(name)
    with _janitor_worker_log_path(name).open("ab") as log_fh:
        return subprocess.Popen(
            [sys.executable, str(script), str(command)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=False,
            env=env,
        )


def _requested_janitor_instances(request: Dict[str, object]) -> tuple[list[str], list[str]]:
    all_instances = set(list_instances()) | _internal_path_derived_instances_on_disk()
    deleted = {
        instance
        for instance in all_instances
        if _instance_misc_project_deleted(instance)
    }
    scope = str(request.get("scope") or "all").strip().lower()
    if scope == "instance":
        raw = validate_instance_id(str(request.get("instance") or ""))
        if raw in all_instances and raw not in deleted:
            return [raw], []
        if raw in deleted:
            return [], [f"instance {raw} is deleted"]
        return [], [f"instance {raw} was not found"]
    return sorted(all_instances - deleted), []


def _parse_janitor_timestamp(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _janitor_terminal_timestamp(payload: Dict[str, object], status: str) -> datetime | None:
    candidates = ["finished_at"]
    if status == "completed":
        candidates.append("last_completed_at")
    elif status == "failed":
        candidates.append("last_failed_at")
    for key in candidates:
        parsed = _parse_janitor_timestamp(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _janitor_stats_completion_error(instance: str, status: str, request_started_at: datetime) -> str | None:
    stats_path = quaid_home() / "instances" / instance / "logs" / "janitor-stats.json"
    try:
        raw = stats_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except FileNotFoundError:
        return f"instance {instance} janitor stats missing"
    except Exception as exc:
        return f"instance {instance} janitor stats unreadable: {exc}"
    if not isinstance(payload, dict):
        return f"instance {instance} janitor stats invalid"
    if str(payload.get("task") or "").strip() != "all":
        return f"instance {instance} janitor stats task={payload.get('task')!r}"
    if bool(payload.get("dry_run")):
        return f"instance {instance} janitor stats came from dry-run"
    try:
        last_run = _parse_janitor_timestamp(payload.get("last_run"))
    except Exception as exc:
        return f"instance {instance} janitor stats timestamp invalid: {exc}"
    if last_run is None:
        return f"instance {instance} janitor stats missing last_run"
    if last_run < request_started_at:
        return f"instance {instance} janitor stats stale for request"
    success = bool(payload.get("success"))
    if status == "completed" and not success:
        return f"instance {instance} janitor stats success=false"
    if status == "failed" and success:
        return f"instance {instance} janitor stats success=true with failed checkpoint"
    return None


def _janitor_checkpoint_status(
    instance: str,
    *,
    request_started_at: object = "",
) -> tuple[str, Optional[str]]:
    name = validate_instance_id(instance)
    checkpoint_path = quaid_home() / "instances" / name / "logs" / "janitor" / "checkpoint-all.json"

    def _checkpoint_error(message: str, exc: Exception | None = None) -> tuple[str, str]:
        _LOGGER.warning(message)
        if _fail_hard_enabled():
            raise RuntimeError(message) from exc
        return "", message

    try:
        raw = checkpoint_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except FileNotFoundError as exc:
        return _checkpoint_error(f"instance {name} janitor checkpoint missing", exc)
    except Exception as exc:
        return _checkpoint_error(f"instance {name} janitor checkpoint unreadable: {exc}", exc)
    if not isinstance(payload, dict):
        return _checkpoint_error(f"instance {name} janitor checkpoint invalid")
    status = str(payload.get("status") or "").strip().lower()
    if not status:
        return _checkpoint_error(f"instance {name} janitor checkpoint missing status")
    if request_started_at and status in {"completed", "failed"}:
        try:
            request_started = _parse_janitor_timestamp(request_started_at)
            checkpoint_started = _parse_janitor_timestamp(payload.get("started_at"))
            checkpoint_finished = _janitor_terminal_timestamp(payload, status)
        except Exception as exc:
            return _checkpoint_error(f"instance {name} janitor checkpoint timestamp invalid: {exc}", exc)
        if request_started is None:
            return _checkpoint_error(f"instance {name} janitor request timestamp invalid")
        if checkpoint_started is None:
            return _checkpoint_error(f"instance {name} janitor checkpoint missing started_at")
        if checkpoint_started < request_started:
            return _checkpoint_error(f"instance {name} janitor checkpoint stale for request")
        if checkpoint_finished is None:
            return _checkpoint_error(f"instance {name} janitor checkpoint missing terminal timestamp")
        if checkpoint_finished < request_started:
            return _checkpoint_error(f"instance {name} janitor checkpoint terminal timestamp stale for request")
        stats_error = _janitor_stats_completion_error(name, status, request_started)
        if stats_error:
            return _checkpoint_error(stats_error)
    return status, None


def _janitor_request_started_instances(request: Dict[str, object]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    candidates: list[object] = []
    for key in ("started_instances", "instances"):
        raw = request.get(key)
        if isinstance(raw, list):
            candidates.extend(raw)
    worker_pids = request.get("worker_pids")
    if isinstance(worker_pids, dict):
        candidates.extend(worker_pids.keys())
    exit_codes = request.get("exit_codes")
    if isinstance(exit_codes, dict):
        candidates.extend(exit_codes.keys())
    for raw in candidates:
        try:
            name = validate_instance_id(str(raw or "").strip())
        except Exception as exc:
            _LOGGER.warning("skipping invalid instance name %r in janitor request: %s", raw, exc)
            continue
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _janitor_request_worker_pids(request: Dict[str, object]) -> dict[str, int]:
    payload = request.get("worker_pids")
    if not isinstance(payload, dict):
        return {}
    worker_pids: dict[str, int] = {}
    for raw_name, raw_pid in payload.items():
        try:
            name = validate_instance_id(str(raw_name or "").strip())
            pid = int(raw_pid)
        except Exception as exc:
            _LOGGER.warning("skipping invalid worker_pids entry name=%r pid=%r: %s", raw_name, raw_pid, exc)
            continue
        if pid > 0:
            worker_pids[name] = pid
    return worker_pids


def _active_janitor_request_payload(request: Dict[str, object]) -> Dict[str, object]:
    started_instances = _janitor_request_started_instances(request)
    return {
        "request_id": str(request.get("request_id") or "").strip(),
        "started_at": str(request.get("started_at") or "").strip(),
        "errors": list(request.get("errors") or []),
        "targets": list(started_instances),
        "started_instances": list(started_instances),
        "worker_pids": _janitor_request_worker_pids(request),
        "exit_codes": {str(k): int(v) for k, v in dict(request.get("exit_codes") or {}).items()},
    }


def _finalize_janitor_request_payload(
    payload: Dict[str, object],
    *,
    started_instances: list[str],
    errors: list[str],
    exit_codes: Dict[str, int],
    worker_pids: Dict[str, int] | None = None,
    request_started_at: object = "",
) -> None:
    final_errors = list(errors)
    fail_hard_exc: Exception | None = None
    worker_pids = worker_pids or {}
    if not started_instances and not exit_codes:
        final_errors.append("janitor request running with no tracked workers")
    for instance in started_instances:
        code = exit_codes.get(instance)
        if code is not None and code != 0:
            final_errors.append(f"instance {instance} janitor exited rc={code}")
            continue
        if code == 0:
            continue
        if code is None and int(worker_pids.get(instance, 0) or 0) > 0:
            final_errors.append(
                f"instance {instance} janitor worker pid={int(worker_pids[instance])} is no longer active"
            )
        try:
            checkpoint_status, checkpoint_error = _janitor_checkpoint_status(
                instance,
                request_started_at=request_started_at,
            )
        except Exception as exc:
            final_errors.append(str(exc) or f"instance {instance} janitor checkpoint inspection failed")
            if fail_hard_exc is None:
                fail_hard_exc = exc
            continue
        if checkpoint_error:
            final_errors.append(checkpoint_error)
        elif checkpoint_status != "completed":
            final_errors.append(f"instance {instance} janitor checkpoint status={checkpoint_status}")
    payload["status"] = "failed" if final_errors else "completed"
    payload["completed_at"] = project_docs.utc_now()
    payload["errors"] = final_errors
    payload["exit_codes"] = {str(k): int(v) for k, v in sorted(exit_codes.items())}
    payload["worker_pids"] = {}
    project_docs.write_janitor_request(payload)
    if final_errors and _fail_hard_enabled():
        message = "janitor request failed under failHard: " + "; ".join(final_errors)
        _LOGGER.error(message)
        if fail_hard_exc is not None:
            raise RuntimeError(message) from fail_hard_exc
        raise RuntimeError(message)


def _start_requested_janitor_run(
    request: Dict[str, object],
    scheduled_workers: Dict[str, subprocess.Popen],
    on_demand_workers: Dict[str, subprocess.Popen],
) -> Dict[str, object] | None:
    targets, errors = _requested_janitor_instances(request)
    worker_pids: Dict[str, int] = {}
    started_instances: list[str] = []
    request_errors = list(errors)
    for instance in targets:
        scheduled = scheduled_workers.get(instance)
        if scheduled is not None and scheduled.poll() is None:
            request_errors.append(f"instance {instance} already has a scheduled janitor worker")
            continue
        try:
            proc = _spawn_janitor_worker(instance, command="run-all-once")
            on_demand_workers[instance] = proc
            started_instances.append(instance)
            worker_pids[instance] = int(getattr(proc, "pid", 0) or 0)
        except Exception as exc:
            request_errors.append(f"failed to start janitor worker for {instance}: {exc}")
            if _fail_hard_enabled():
                raise RuntimeError(f"failed to start janitor worker for {instance}") from exc
    payload = dict(request)
    payload["instances"] = targets
    payload["started_instances"] = started_instances
    payload["worker_pids"] = worker_pids
    payload["errors"] = request_errors
    payload["started_at"] = project_docs.utc_now()
    payload["completed_at"] = None
    payload["exit_codes"] = {}
    if on_demand_workers:
        payload["status"] = "running"
        with _janitor_request_lock():
            project_docs.write_janitor_request(payload)
        return {
            "request_id": payload.get("request_id"),
            "started_at": str(payload.get("started_at") or "").strip(),
            "errors": list(request_errors),
            "targets": list(targets),
            "started_instances": list(started_instances),
            "worker_pids": dict(worker_pids),
            "exit_codes": {},
        }
    payload["status"] = "failed" if request_errors else "completed"
    payload["completed_at"] = project_docs.utc_now()
    with _janitor_request_lock():
        project_docs.write_janitor_request(payload)
    return None


def _maintain_on_demand_janitor_request(
    active_request: Dict[str, object] | None,
    scheduled_workers: Dict[str, subprocess.Popen],
    on_demand_workers: Dict[str, subprocess.Popen],
) -> Dict[str, object] | None:
    pending_running_request: Dict[str, object] | None = None
    if active_request is None:
        with _janitor_request_lock():
            request = project_docs.read_janitor_request()
            if not request:
                return None
            status = str(request.get("status") or "").strip().lower()
            if status == "running":
                pending_running_request = _active_janitor_request_payload(request)
            elif status != "pending":
                return None
        if pending_running_request is not None:
            return _maintain_on_demand_janitor_request(
                pending_running_request,
                scheduled_workers,
                on_demand_workers,
            )
        return _start_requested_janitor_run(request, scheduled_workers, on_demand_workers)

    accumulated_exit_codes = {
        str(k): int(v) for k, v in dict(active_request.get("exit_codes") or {}).items()
    }
    request_id = str(active_request.get("request_id") or "").strip()
    started_instances = _janitor_request_started_instances(active_request)
    if not started_instances:
        started_instances = sorted(
            {
                *[str(name) for name in on_demand_workers.keys()],
                *[str(name) for name in accumulated_exit_codes.keys()],
                *[str(name) for name in _janitor_request_worker_pids(active_request).keys()],
            }
        )
    worker_pids = _janitor_request_worker_pids(active_request)
    exit_codes: Dict[str, int] = {}
    live_worker_pids: dict[str, int] = {}
    all_done = True
    for instance in started_instances:
        proc = on_demand_workers.get(instance)
        if proc is not None:
            code = proc.poll()
            if code is None:
                all_done = False
                pid = int(getattr(proc, "pid", 0) or worker_pids.get(instance) or 0)
                if pid > 0:
                    live_worker_pids[instance] = pid
                continue
            exit_codes[instance] = int(code)
            on_demand_workers.pop(instance, None)
            project_docs.reap_child_processes()
            continue
        pid = int(worker_pids.get(instance, 0) or 0)
        if pid > 0 and _pid_matches_janitor_worker(pid):
            all_done = False
            live_worker_pids[instance] = pid
    if exit_codes:
        accumulated_exit_codes.update({str(k): int(v) for k, v in exit_codes.items()})
    if not all_done:
        if exit_codes or live_worker_pids != worker_pids:
            with _janitor_request_lock():
                payload = project_docs.read_janitor_request() or {}
                if str(payload.get("request_id") or "").strip() == request_id:
                    payload["exit_codes"] = {str(k): int(v) for k, v in sorted(accumulated_exit_codes.items())}
                    payload["worker_pids"] = {str(k): int(v) for k, v in sorted(live_worker_pids.items())}
                    project_docs.write_janitor_request(payload)
        active_request = dict(active_request)
        active_request["exit_codes"] = dict(accumulated_exit_codes)
        active_request["started_instances"] = list(started_instances)
        active_request["worker_pids"] = dict(live_worker_pids)
        return active_request

    with _janitor_request_lock():
        payload = project_docs.read_janitor_request() or {}
        if str(payload.get("request_id") or "").strip() != request_id:
            payload = {"request_id": request_id}
        _finalize_janitor_request_payload(
            payload,
            started_instances=started_instances,
            errors=list(active_request.get("errors") or []),
            exit_codes=accumulated_exit_codes,
            worker_pids=worker_pids,
            request_started_at=str(active_request.get("started_at") or payload.get("started_at") or "").strip(),
        )
    return None


def _maintain_instance_monitors(known_instances: Dict[str, int]) -> None:
    live, inactive_instances = _live_instances_for_supervisor()
    from core import extraction_daemon as _extraction_daemon

    for instance in sorted(inactive_instances):
        try:
            _stop_instance_monitor(instance)
        except Exception as exc:
            _LOGGER.warning("failed to stop inactive instance monitor for %s: %s", instance, exc)
            if _fail_hard_enabled():
                raise
            pass
        known_instances.pop(instance, None)
    for instance in sorted(live):
        pid = _read_instance_daemon_pid(instance)
        matching = _extraction_daemon._matching_daemon_pids(
            quaid_home=quaid_home(),
            instance=instance,
            include_foreground=True,
        )
        if pid is not None and len(matching) > 1:
            logging.getLogger(__name__).warning(
                "reconciling duplicate extraction daemons for %s: pidfile=%s matches=%s",
                instance,
                pid,
                ",".join(str(match) for match in matching),
            )
            try:
                _stop_instance_monitor(instance)
            except Exception as exc:
                _LOGGER.warning("failed to stop duplicate instance monitor for %s: %s", instance, exc)
                if _fail_hard_enabled():
                    raise
                pass
            known_instances.pop(instance, None)
            pid = None
        if pid is None:
            pid = _start_instance_monitor(instance)
        known_instances[instance] = pid
    for instance in list(known_instances.keys()):
        if instance in live:
            continue
        try:
            _stop_instance_monitor(instance)
        except Exception as exc:
            _LOGGER.warning("failed to stop stale instance monitor for %s: %s", instance, exc)
            if _fail_hard_enabled():
                raise
            pass
        known_instances.pop(instance, None)


def _maintain_janitor_workers(
    janitor_workers: Dict[str, subprocess.Popen],
    last_janitor_checks: Dict[str, float],
    *,
    now: float,
    check_interval: float,
    busy_instances: set[str] | None = None,
) -> None:
    busy = set(busy_instances or ())
    live, inactive_instances = _live_instances_for_supervisor()
    for instance, proc in list(janitor_workers.items()):
        if instance in inactive_instances:
            janitor_workers.pop(instance, None)
            project_docs._terminate_process(proc)
            last_janitor_checks.pop(instance, None)
            project_docs.reap_child_processes()
        elif proc.poll() is not None:
            janitor_workers.pop(instance, None)
            project_docs.reap_child_processes()
    for instance in sorted(live):
        if instance in busy:
            continue
        proc = janitor_workers.get(instance)
        if proc is not None and proc.poll() is None:
            continue
        last_check = float(last_janitor_checks.get(instance, 0.0) or 0.0)
        if now - last_check < check_interval:
            continue
        last_janitor_checks[instance] = now
        janitor_workers[instance] = _start_janitor_worker(instance)
    for instance in list(janitor_workers.keys()):
        if instance in live:
            continue
        proc = janitor_workers.pop(instance)
        project_docs._terminate_process(proc)
        last_janitor_checks.pop(instance, None)


def _has_tracked_janitor_processes(
    janitor_workers: Dict[str, subprocess.Popen],
    on_demand_janitor_workers: Dict[str, subprocess.Popen],
) -> bool:
    return bool(janitor_workers or on_demand_janitor_workers)


def stop_all_instance_monitors() -> None:
    for instance in sorted(set(list_instances()) | _internal_path_derived_instances_on_disk()):
        try:
            _stop_instance_monitor(instance)
        except Exception as exc:
            _LOGGER.warning("failed to stop instance monitor for %s: %s", instance, exc)
            if _fail_hard_enabled():
                raise
            pass


def run_supervisor(*, once: bool = False, interval_seconds: float | None = None) -> int:
    interval = interval_seconds
    if interval is None:
        try:
            interval = float(os.environ.get("QUAID_SUPERVISOR_INTERVAL_SECONDS", "5"))
        except Exception:
            interval = 5.0
    interval = max(0.5, float(interval))
    token = os.environ.get("QUAID_SUPERVISOR_TOKEN", "").strip()
    project_docs.write_supervisor_pid(token)
    known_workers: Dict[str, int] = {}
    known_instances: Dict[str, int] = {}
    janitor_workers: Dict[str, subprocess.Popen] = {}
    on_demand_janitor_workers: Dict[str, subprocess.Popen] = {}
    active_janitor_request: Dict[str, object] | None = None
    last_janitor_checks: Dict[str, float] = {}
    last_stale_doc_check = 0.0
    last_auto_register_check = 0.0
    stale_doc_interval = _interval_from_env("QUAID_PROJECT_DOCS_STALE_INDEX_INTERVAL_SECONDS", 60.0)
    auto_register_interval = _interval_from_env("QUAID_PROJECT_DOCS_AUTO_REGISTER_INTERVAL_SECONDS", 300.0)
    janitor_check_interval = _janitor_check_interval_seconds()
    while not _STOP:
        if not _has_tracked_janitor_processes(janitor_workers, on_demand_janitor_workers):
            project_docs.reap_child_processes()
        now = time.time()
        try:
            _maintain_instance_monitors(known_instances)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("instance monitor tick failed: %s", exc)
            if _fail_hard_enabled():
                raise
        try:
            active_janitor_request = _maintain_on_demand_janitor_request(
                active_janitor_request,
                janitor_workers,
                on_demand_janitor_workers,
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("janitor request tick failed: %s", exc)
            if _fail_hard_enabled():
                raise
        try:
            _maintain_janitor_workers(
                janitor_workers,
                last_janitor_checks,
                now=now,
                check_interval=janitor_check_interval,
                busy_instances=set(on_demand_janitor_workers.keys()),
            )
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("janitor worker tick failed: %s", exc)
            if _fail_hard_enabled():
                raise
        projects = _supervisor_projects()
        live = set(projects.keys())
        stale_after = project_docs.worker_stale_after_seconds(interval)
        for project in sorted(live):
            if not project_docs.project_is_registered_for_worker(project):
                project_docs.cleanup_project_state(project)
                known_workers.pop(project, None)
                continue
            try:
                if _handle_known_project_worker_exit(project, known_workers):
                    continue
                project_docs.reap_stale_worker(project, stale_after_seconds=stale_after)
                skip_reason = _project_worker_start_skip_reason(project, projects.get(project) or {})
                if skip_reason:
                    _LOGGER.warning(
                        "project docs worker start skipped for %s: %s",
                        project,
                        skip_reason,
                    )
                    project_docs.merge_state(
                        project,
                        {
                            "status": "error",
                            "last_error": f"worker start skipped: {skip_reason}",
                            "last_failed_at": project_docs.utc_now(),
                        },
                    )
                    known_workers.pop(project, None)
                    continue
                pid = project_docs.start_worker(project)
                known_workers[project] = pid
            except KeyError:
                project_docs.cleanup_project_state(project)
                known_workers.pop(project, None)
            except Exception as exc:
                _LOGGER.warning("project docs worker start failed for %s: %s", project, exc)
                if project_docs.project_is_registered_for_worker(project):
                    project_docs.merge_state(project, {"status": "error", "last_error": f"worker start failed: {exc}"})
                else:
                    project_docs.cleanup_project_state(project)
                    known_workers.pop(project, None)
                if _fail_hard_enabled():
                    raise
        for project in list(known_workers.keys()):
            if project in live:
                continue
            try:
                project_docs.stop_worker(project)
                project_docs.reap_child_processes()
                project_docs.cleanup_project_state(project)
            except Exception as exc:
                _LOGGER.warning("failed to stop project docs worker for %s: %s", project, exc)
                if _fail_hard_enabled():
                    raise
                pass
            known_workers.pop(project, None)
        auto_register_requested = False
        stale_index_requested = False
        if not _dispatcher_only_mode() and now - last_auto_register_check > auto_register_interval:
            auto_register_requested = True
            last_auto_register_check = now
        if not _dispatcher_only_mode() and now - last_stale_doc_check > stale_doc_interval:
            stale_index_requested = True
            last_stale_doc_check = now
        _emit_project_docs_maintenance_event(
            observed_at=now,
            auto_register_interval=auto_register_interval,
            stale_doc_interval=stale_doc_interval,
            auto_register_requested=auto_register_requested,
            stale_index_requested=stale_index_requested,
        )
        if once:
            return 0
        time.sleep(interval)
    for project in list(known_workers.keys()):
        try:
            project_docs.stop_worker(project)
            project_docs.reap_child_processes()
        except Exception as exc:
            _LOGGER.warning("failed to stop project docs worker during supervisor shutdown for %s: %s", project, exc)
            if _fail_hard_enabled():
                raise
            pass
    for instance in list(known_instances.keys()):
        try:
            _stop_instance_monitor(instance)
            project_docs.reap_child_processes()
        except Exception as exc:
            _LOGGER.warning("failed to stop instance monitor during supervisor shutdown for %s: %s", instance, exc)
            if _fail_hard_enabled():
                raise
            pass
    for proc in list(janitor_workers.values()):
        project_docs._terminate_process(proc)
        project_docs.reap_child_processes()
    for proc in list(on_demand_janitor_workers.values()):
        project_docs._terminate_process(proc)
        project_docs.reap_child_processes()
    if active_janitor_request is not None:
        with _janitor_request_lock():
            payload = project_docs.read_janitor_request() or {}
            if str(payload.get("request_id") or "").strip() == str(
                active_janitor_request.get("request_id") or ""
            ).strip():
                errors = list(payload.get("errors") or [])
                errors.append("supervisor stopped before janitor request completed")
                payload["errors"] = errors
                payload["status"] = "failed"
                payload["completed_at"] = project_docs.utc_now()
                project_docs.write_janitor_request(payload)
    project_docs.clear_supervisor_pid_for_current_process()
    return 0


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    parser = argparse.ArgumentParser(description="Quaid supervisor")
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="Run supervisor loop")
    run_p.add_argument("--once", action="store_true")
    sub.add_parser("ensure", help="Ensure supervisor is running")
    sub.add_parser("stop", help="Stop supervisor")
    args = parser.parse_args()
    if args.command == "run":
        try:
            code = run_supervisor(once=bool(args.once))
        except Exception as exc:
            try:
                project_docs.write_supervisor_failure(exc)
            except Exception:
                _LOGGER.exception("failed writing project-docs supervisor failure marker")
            raise
        project_docs.clear_supervisor_failure()
        raise SystemExit(code)
    if args.command == "ensure":
        print(project_docs.ensure_supervisor_alive())
        return
    if args.command == "stop":
        stopped = project_docs.stop_supervisor()
        print("stopped" if stopped else "not running")
        return
    parser.print_help()
    raise SystemExit(1)


if __name__ == "__main__":
    main()
