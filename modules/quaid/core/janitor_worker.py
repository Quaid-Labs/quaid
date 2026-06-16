#!/usr/bin/env python3
"""Supervisor-owned janitor worker.

The supervisor owns process lifecycle; this worker owns the existing janitor
schedule check and exits after one scheduler tick.
"""

from __future__ import annotations

import argparse
import json
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
                print(
                    "[janitor-worker] supervisor exited before worker completed",
                    file=sys.stderr,
                    flush=True,
                )
                os._exit(1)
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
    from core.lifecycle import janitor

    try:
        result = janitor.run_task_optimized(task="all", dry_run=False)
    except Exception as exc:
        _write_run_all_failure_markers(exc, janitor)
        raise
    return 0 if bool(result.get("success")) else 1


def _write_run_all_failure_markers(exc: Exception, janitor_module) -> None:
    """Write standard terminal janitor markers before preserving failHard exit."""
    try:
        completed_at = janitor_module._now_iso()
        logs_dir = janitor_module._logs_dir()
        error_message = f"{type(exc).__name__}: {exc}"
        metrics = {
            "total_duration_seconds": 0.0,
            "task_durations": {},
            "task_metrics": {},
            "llm_calls": 0,
            "llm_time_seconds": 0.0,
            "errors": 1,
            "error_details": [{"time": completed_at, "error": error_message}],
            "warnings": 0,
            "warning_details": [],
            "stage_budget_report": {},
            "carryover_report": {},
        }
        result = {
            "success": False,
            "memory_pipeline_ok": False,
            "metrics": metrics,
            "applied_changes": {},
        }
        janitor_module.janitor_logger.info(
            "janitor_complete",
            task="all",
            success=False,
            duration_seconds=0.0,
            llm_calls=0,
            errors=1,
            error=error_message,
        )
        checkpoint_path = logs_dir / "janitor" / "checkpoint-all.json"
        checkpoint: dict[str, object] = {}
        try:
            loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                checkpoint.update(loaded)
        except FileNotFoundError:
            pass
        except Exception as checkpoint_exc:
            checkpoint["checkpoint_read_error"] = str(checkpoint_exc)
        checkpoint.update(
            {
                "task": "all",
                "dry_run": False,
                "status": "failed",
                "started_at": checkpoint.get("started_at") or completed_at,
                "heartbeat_at": completed_at,
                "finished_at": completed_at,
                "last_failed_at": completed_at,
                "terminal_status": "failed",
                "terminal_stage": checkpoint.get("current_stage", "worker-fatal"),
                "worker_exit_error": error_message,
            }
        )
        checkpoint.setdefault("current_stage", "worker-fatal")
        checkpoint.setdefault("completed_stages", [])
        janitor_module._atomic_write_json(checkpoint_path, checkpoint)
        try:
            usage = janitor_module.get_token_usage()
        except Exception:
            usage = {"api_calls": 0, "input_tokens": 0, "output_tokens": 0}
        try:
            estimated_cost = janitor_module.estimate_cost()
        except Exception:
            estimated_cost = 0.0
        janitor_module._write_janitor_stats(
            task="all",
            dry_run=False,
            result=result,
            usage=usage,
            estimated_cost_usd=estimated_cost,
            completed_at=completed_at,
            logs_dir=logs_dir,
        )
    except Exception as marker_exc:
        print(
            f"[janitor-worker] failed to write terminal failure markers: {marker_exc}",
            file=sys.stderr,
            flush=True,
        )


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
