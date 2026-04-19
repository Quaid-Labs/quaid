#!/usr/bin/env python3
"""Quaid supervisor entrypoint for project documentation workers.

This is the first supervisor-owned runtime component. The long-term direction is
for instance daemons and lifecycle workers to register under this same parent.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import project_docs
from core.project_registry import list_projects

_STOP = False


def _handle_stop(_signum, _frame) -> None:
    global _STOP
    _STOP = True


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
    while not _STOP:
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
                project_docs.worker_heartbeat_path(project).unlink(missing_ok=True)
                project_docs.worker_pid_path(project).unlink(missing_ok=True)
            except Exception:
                pass
            known_workers.pop(project, None)
        if once:
            return 0
        time.sleep(interval)
    for project in list(known_workers.keys()):
        try:
            project_docs.stop_worker(project)
        except Exception:
            pass
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
