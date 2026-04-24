#!/usr/bin/env python3
"""Supervisor-owned janitor worker.

The supervisor owns process lifecycle; this worker owns the existing janitor
schedule check and exits after one scheduler tick.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_STOP = False


def _handle_stop(_signum, _frame) -> None:
    global _STOP
    _STOP = True
    raise SystemExit(0)


def _supervisor_alive() -> bool:
    raw = os.environ.get("QUAID_SUPERVISOR_PID", "").strip()
    if not raw:
        return True
    try:
        pid = int(raw)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _start_supervisor_watchdog() -> threading.Thread | None:
    if not os.environ.get("QUAID_SUPERVISOR_PID", "").strip():
        return None

    def _watch() -> None:
        while not _STOP:
            if not _supervisor_alive():
                os._exit(0)
            time.sleep(5.0)

    thread = threading.Thread(target=_watch, name="janitor-supervisor-watchdog", daemon=True)
    thread.start()
    return thread


def run_scheduler_once() -> int:
    from core.compatibility import JanitorScheduler
    from lib.instance import instance_root

    root = instance_root()
    scheduler = JanitorScheduler(data_dir=root / "data", quaid_home=root)
    scheduler.tick()
    return 0


def run_all_once() -> int:
    from core.lifecycle.janitor import run_task_optimized

    result = run_task_optimized(task="all", dry_run=False)
    return 0 if bool(result.get("success")) else 1


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    parser = argparse.ArgumentParser(description="Quaid supervisor-owned janitor worker")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scheduler-once", help="Run one scheduled janitor eligibility tick")
    sub.add_parser("run-all-once", help="Run one immediate janitor maintenance pass")
    args = parser.parse_args()
    _start_supervisor_watchdog()
    if args.command == "scheduler-once":
        raise SystemExit(run_scheduler_once())
    if args.command == "run-all-once":
        raise SystemExit(run_all_once())
    parser.print_help()
    raise SystemExit(1)


if __name__ == "__main__":
    main()
