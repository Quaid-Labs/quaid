#!/usr/bin/env python3
"""Supervisor-owned project documentation worker."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

# Allow direct script execution from the Quaid wrapper/subprocesses.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import project_docs

_STOP = False


def _handle_stop(_signum, _frame) -> None:
    global _STOP
    _STOP = True


def _supervisor_alive() -> bool:
    raw = os.environ.get("QUAID_SUPERVISOR_PID", "").strip()
    if not raw:
        return True
    try:
        pid = int(raw)
    except Exception:
        return False
    return project_docs.read_supervisor_pid() == pid


def _update_heartbeat_interval(interval_seconds: float) -> float:
    stale_after = project_docs.worker_stale_after_seconds(interval_seconds)
    return max(0.5, min(5.0, float(interval_seconds), stale_after / 3.0))


def _start_update_heartbeat(project: str, interval_seconds: float) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    heartbeat_interval = _update_heartbeat_interval(interval_seconds)

    def _loop() -> None:
        while not stop_event.wait(heartbeat_interval):
            project_docs.write_worker_heartbeat(project, {"status": "updating"})

    thread = threading.Thread(
        target=_loop,
        name=f"project-docs-update-heartbeat-{project}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


def _refresh_runtime_config_for_update(project: str) -> None:
    """Reload per-process model caches before long-lived worker LLM calls."""
    try:
        from config import reload_config
        from lib.embeddings import reset_embeddings_provider
        from lib.llm_clients import reset_model_config_cache

        reload_config()
        reset_model_config_cache()
        reset_embeddings_provider()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Project docs worker config refresh failed for %s: %s",
            project,
            exc,
        )
        try:
            from lib.fail_policy import is_fail_hard_enabled

            fail_hard = is_fail_hard_enabled()
        except Exception:
            fail_hard = True
        if fail_hard:
            raise


def run_worker(project: str, *, once: bool = False, interval_seconds: Optional[float] = None) -> int:
    name = project_docs.validate_project_name(project)
    interval = interval_seconds
    if interval is None:
        try:
            interval = float(os.environ.get("QUAID_PROJECT_DOCS_WORKER_INTERVAL_SECONDS", "5"))
        except Exception:
            interval = 5.0
    interval = max(0.5, float(interval))

    project_docs.write_worker_heartbeat(name, {"status": "starting"})
    while not _STOP:
        if not _supervisor_alive():
            project_docs.merge_state(name, {"status": "stopped", "last_error": "supervisor exited"})
            project_docs.clear_worker_pid_for_current_process(name)
            return 0
        try:
            request = project_docs.read_update_request(name)
            status = project_docs.project_status(name)
            stale = status.get("status") == "stale"
            if request or stale:
                project_docs.write_worker_heartbeat(name, {"status": "updating"})
                heartbeat_stop, heartbeat_thread = _start_update_heartbeat(name, interval)
                try:
                    _refresh_runtime_config_for_update(name)
                    result = project_docs.execute_update_once(name, request=request)
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=1.0)
                project_docs.write_worker_heartbeat(name, {"status": "idle", "last_result": result})
            else:
                project_docs.write_worker_heartbeat(name, {"status": "idle"})
        except KeyError:
            # Project was deleted; supervisor will stop/remove this worker.
            project_docs.clear_worker_pid_for_current_process(name)
            try:
                project_docs.worker_heartbeat_path(name).unlink(missing_ok=True)
            except OSError:
                pass
            return 0
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("Project docs worker tick failed for %s", name)
            project_docs.merge_state(name, {"status": "error", "last_error": str(exc), "last_failed_at": project_docs.utc_now()})
            project_docs.write_worker_heartbeat(name, {"status": "error", "last_error": str(exc)})
        if once:
            project_docs.clear_worker_pid_for_current_process(name)
            return 0
        time.sleep(interval)
    project_docs.write_worker_heartbeat(name, {"status": "stopped"})
    project_docs.clear_worker_pid_for_current_process(name)
    return 0


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    parser = argparse.ArgumentParser(description="Quaid project docs worker")
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="Run a project docs worker")
    run_p.add_argument("project")
    run_p.add_argument("--once", action="store_true", help="Run one tick and exit")
    args = parser.parse_args()
    if args.command == "run":
        raise SystemExit(run_worker(args.project, once=bool(args.once)))
    parser.print_help()
    raise SystemExit(1)


if __name__ == "__main__":
    main()
