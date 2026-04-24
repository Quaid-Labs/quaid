#!/usr/bin/env python3
"""Quaid runtime supervisor entrypoint.

The supervisor owns long-lived monitor process lifecycle. Domain workers still
own their own work: instance daemons process extraction signals, project-docs
workers apply docs updates, and janitor workers run bounded maintenance ticks.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import project_docs
from core.project_registry import is_misc_project_deleted, list_projects
from lib.instance import list_instances, quaid_home, validate_instance_id

_STOP = False
_INSTANCE_DB_OVERRIDE_ENV_KEYS = ("MEMORY_DB_PATH", "MEMORY_ARCHIVE_DB_PATH")


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
    except Exception:
        return False
    return bool(is_fail_hard_enabled())


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _instance_root(instance: str) -> Path:
    return quaid_home() / "instances" / validate_instance_id(instance)


def _instance_child_env(instance: str, *, daemon: bool = False) -> dict[str, str]:
    """Build an env for a supervisor-owned per-instance child process."""
    name = validate_instance_id(instance)
    env = dict(os.environ)
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
    except Exception:
        return None


def _read_instance_daemon_pid(instance: str) -> int | None:
    path = _instance_daemon_pid_path(instance)
    if not path.is_file():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except Exception:
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
    except Exception:
        if _fail_hard_enabled():
            raise
        return False


def _live_instances_for_supervisor() -> tuple[set[str], set[str]]:
    all_instances = set(list_instances())
    inactive = {
        instance
        for instance in all_instances
        if _instance_misc_project_deleted(instance) or project_docs.is_instance_monitor_disabled(instance)
    }
    return all_instances - inactive, inactive


def _wait_for_instance_pid(
    instance: str,
    expected_pid: int,
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
        if pid == int(expected_pid):
            return pid
        if pid is not None:
            if proc is not None and proc.poll() is None:
                project_docs._terminate_process(proc)
            return pid
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"instance monitor {instance} exited before writing pid file rc={proc.returncode}")
        time.sleep(0.1)
    detail = f"last_seen_pid={last_seen_pid}" if last_seen_pid is not None else "no live pid observed"
    raise TimeoutError(f"instance monitor {instance} did not write pid file for pid {expected_pid} ({detail})")


def _start_instance_monitor(instance: str) -> int:
    name = validate_instance_id(instance)
    if _instance_misc_project_deleted(name):
        raise RuntimeError(f"refusing to start monitor for deleted misc instance {name}")
    if project_docs.is_instance_monitor_disabled(name):
        raise RuntimeError(f"refusing to start monitor for disabled instance {name}")
    existing = _read_instance_daemon_pid(name)
    if existing is not None:
        return existing
    script = Path(__file__).parent / "extraction_daemon.py"
    env = _instance_child_env(name, daemon=True)
    with _instance_daemon_log_path(name).open("ab") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(script), "_worker"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=False,
            env=env,
        )
    try:
        return _wait_for_instance_pid(name, int(proc.pid), proc=proc)
    except Exception:
        project_docs._terminate_process(proc)
        raise


def _stop_instance_monitor(instance: str) -> bool:
    name = validate_instance_id(instance)
    pid = _read_instance_daemon_pid(name)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        if _pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    finally:
        if not _pid_alive(pid):
            try:
                _instance_daemon_pid_path(name).unlink(missing_ok=True)
            except OSError:
                pass
    return True


def _janitor_check_interval_seconds() -> float:
    return _interval_from_env("QUAID_SUPERVISOR_JANITOR_CHECK_INTERVAL_SECONDS", 900.0)


def _start_janitor_worker(instance: str) -> subprocess.Popen:
    return _spawn_janitor_worker(instance, command="scheduler-once")


def _spawn_janitor_worker(instance: str, *, command: str) -> subprocess.Popen:
    name = validate_instance_id(instance)
    if _instance_misc_project_deleted(name):
        raise RuntimeError(f"refusing to start janitor worker for deleted misc instance {name}")
    if project_docs.is_instance_monitor_disabled(name):
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
    live, inactive = _live_instances_for_supervisor()
    all_instances = set(list_instances())
    scope = str(request.get("scope") or "all").strip().lower()
    if scope == "instance":
        raw = validate_instance_id(str(request.get("instance") or ""))
        if raw in live:
            return [raw], []
        if raw in inactive:
            return [], [f"instance {raw} is disabled or deleted"]
        if raw in all_instances:
            return [], [f"instance {raw} is not eligible for janitor"]
        return [], [f"instance {raw} was not found"]
    return sorted(live), []


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
        project_docs.write_janitor_request(payload)
        return {
            "request_id": payload.get("request_id"),
            "errors": list(request_errors),
            "targets": list(targets),
            "started_instances": list(started_instances),
        }
    payload["status"] = "failed" if request_errors else "completed"
    payload["completed_at"] = project_docs.utc_now()
    project_docs.write_janitor_request(payload)
    return None


def _maintain_on_demand_janitor_request(
    active_request: Dict[str, object] | None,
    scheduled_workers: Dict[str, subprocess.Popen],
    on_demand_workers: Dict[str, subprocess.Popen],
) -> Dict[str, object] | None:
    if active_request is None:
        request = project_docs.read_janitor_request()
        if not request:
            return None
        status = str(request.get("status") or "").strip().lower()
        if status == "running":
            payload = dict(request)
            errors = list(payload.get("errors") or [])
            errors.append("supervisor restarted before janitor request completed")
            payload["errors"] = errors
            payload["status"] = "failed"
            payload["completed_at"] = project_docs.utc_now()
            project_docs.write_janitor_request(payload)
            return None
        if status != "pending":
            return None
        return _start_requested_janitor_run(request, scheduled_workers, on_demand_workers)

    exit_codes: Dict[str, int] = {}
    all_done = True
    for instance, proc in list(on_demand_workers.items()):
        code = proc.poll()
        if code is None:
            all_done = False
            continue
        exit_codes[instance] = int(code)
        on_demand_workers.pop(instance, None)
        project_docs.reap_child_processes()
    if not all_done:
        return active_request

    request_id = str(active_request.get("request_id") or "").strip()
    payload = project_docs.read_janitor_request() or {}
    if str(payload.get("request_id") or "").strip() != request_id:
        payload = {"request_id": request_id}
    errors = list(active_request.get("errors") or [])
    for instance, code in exit_codes.items():
        if code != 0:
            errors.append(f"instance {instance} janitor exited rc={code}")
    payload["status"] = "failed" if errors else "completed"
    payload["completed_at"] = project_docs.utc_now()
    payload["errors"] = errors
    payload["exit_codes"] = {str(k): int(v) for k, v in sorted(exit_codes.items())}
    project_docs.write_janitor_request(payload)
    return None


def _maintain_instance_monitors(known_instances: Dict[str, int]) -> None:
    live, inactive_instances = _live_instances_for_supervisor()
    for instance in sorted(inactive_instances):
        try:
            _stop_instance_monitor(instance)
        except Exception:
            pass
        known_instances.pop(instance, None)
    for instance in sorted(live):
        pid = _read_instance_daemon_pid(instance)
        if pid is None:
            pid = _start_instance_monitor(instance)
        known_instances[instance] = pid
    for instance in list(known_instances.keys()):
        if instance in live:
            continue
        try:
            _stop_instance_monitor(instance)
        except Exception:
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


def stop_all_instance_monitors() -> None:
    for instance in list_instances():
        try:
            _stop_instance_monitor(instance)
        except Exception:
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
        projects = list_projects()
        live = set(projects.keys())
        stale_after = project_docs.worker_stale_after_seconds(interval)
        for project in sorted(live):
            if not project_docs.project_is_registered_for_worker(project):
                project_docs.cleanup_project_state(project)
                known_workers.pop(project, None)
                continue
            try:
                project_docs.reap_stale_worker(project, stale_after_seconds=stale_after)
                pid = project_docs.start_worker(project)
                known_workers[project] = pid
            except KeyError:
                project_docs.cleanup_project_state(project)
                known_workers.pop(project, None)
            except Exception as exc:
                if project_docs.project_is_registered_for_worker(project):
                    project_docs.merge_state(project, {"status": "error", "last_error": f"worker start failed: {exc}"})
                else:
                    project_docs.cleanup_project_state(project)
                    known_workers.pop(project, None)
        for project in list(known_workers.keys()):
            if project in live:
                continue
            try:
                project_docs.stop_worker(project)
                project_docs.reap_child_processes()
                project_docs.cleanup_project_state(project)
            except Exception:
                pass
            known_workers.pop(project, None)
        if now - last_auto_register_check > auto_register_interval:
            try:
                project_docs.auto_register_project_docs()
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning("project docs auto-register tick failed: %s", exc)
                if _fail_hard_enabled():
                    raise
            last_auto_register_check = now
        if now - last_stale_doc_check > stale_doc_interval:
            try:
                project_docs.index_one_stale_registered_doc()
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning("project docs stale-index tick failed: %s", exc)
                if _fail_hard_enabled():
                    raise
            last_stale_doc_check = now
        if once:
            return 0
        time.sleep(interval)
    for project in list(known_workers.keys()):
        try:
            project_docs.stop_worker(project)
            project_docs.reap_child_processes()
        except Exception:
            pass
    for instance in list(known_instances.keys()):
        try:
            _stop_instance_monitor(instance)
            project_docs.reap_child_processes()
        except Exception:
            pass
    for proc in list(janitor_workers.values()):
        project_docs._terminate_process(proc)
        project_docs.reap_child_processes()
    for proc in list(on_demand_janitor_workers.values()):
        project_docs._terminate_process(proc)
        project_docs.reap_child_processes()
    if active_janitor_request is not None:
        payload = project_docs.read_janitor_request() or {}
        if str(payload.get("request_id") or "").strip() == str(active_janitor_request.get("request_id") or "").strip():
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
        raise SystemExit(run_supervisor(once=bool(args.once)))
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
