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
from core.project_registry import list_projects
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


def _wait_for_instance_pid(instance: str, expected_pid: int, timeout_seconds: float | None = None) -> int:
    timeout = project_docs.pid_startup_wait_seconds() if timeout_seconds is None else float(timeout_seconds)
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        pid = _read_instance_daemon_pid(instance)
        if pid == int(expected_pid):
            return pid
        time.sleep(0.1)
    raise TimeoutError(f"instance monitor {instance} did not write pid file for pid {expected_pid}")


def _start_instance_monitor(instance: str) -> int:
    name = validate_instance_id(instance)
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
        return _wait_for_instance_pid(name, int(proc.pid))
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
    name = validate_instance_id(instance)
    script = Path(__file__).parent / "janitor_worker.py"
    env = _instance_child_env(name)
    with _janitor_worker_log_path(name).open("ab") as log_fh:
        return subprocess.Popen(
            [sys.executable, str(script), "scheduler-once"],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=False,
            env=env,
        )


def _maintain_instance_monitors(known_instances: Dict[str, int]) -> None:
    live = set(list_instances())
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
) -> None:
    live = set(list_instances())
    for instance, proc in list(janitor_workers.items()):
        if proc.poll() is not None:
            janitor_workers.pop(instance, None)
            project_docs.reap_child_processes()
    for instance in sorted(live):
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
            _maintain_janitor_workers(
                janitor_workers,
                last_janitor_checks,
                now=now,
                check_interval=janitor_check_interval,
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
