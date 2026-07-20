#!/usr/bin/env python3
"""Supervisor-owned janitor worker.

The supervisor owns process lifecycle; this worker owns the existing janitor
schedule check and exits after one scheduler tick.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import signal
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_STOP = False
_RUN_ALL_TIMEOUT_ENV = "QUAID_JANITOR_RUN_ALL_TIMEOUT_SECONDS"
_FALLBACK_RUN_ALL_TIMEOUT_SECONDS = 240.0 * 60.0
_RUN_ALL_TIMEOUT_EXIT_CODE = 124
_RUN_ALL_TIMEOUT_MARKER_GRACE_SECONDS = 5.0


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


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except ImportError:
        return True


def _run_all_timeout_seconds() -> float:
    raw = str(os.environ.get(_RUN_ALL_TIMEOUT_ENV, "") or "").strip()
    if not raw:
        return _configured_run_all_timeout_seconds()
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError("value must be positive")
    except Exception as exc:
        fallback = _configured_run_all_timeout_seconds()
        _print_marker_warning(
            f"invalid {_RUN_ALL_TIMEOUT_ENV}={raw!r}; using default "
            f"{fallback:.1f}s: {exc}"
        )
        if _fail_hard_enabled():
            raise RuntimeError(f"{_RUN_ALL_TIMEOUT_ENV} config invalid") from exc
        return fallback
    return max(1.0, value)


def _configured_run_all_timeout_seconds() -> float:
    try:
        from config import get_config

        cfg = get_config()
        raw_minutes = getattr(getattr(cfg, "janitor", None), "task_timeout_minutes", None)
        minutes = int(raw_minutes if raw_minutes is not None else 240)
    except Exception as exc:
        _print_marker_warning(
            "failed to read janitor.task_timeout_minutes; using fallback "
            f"{_FALLBACK_RUN_ALL_TIMEOUT_SECONDS:.1f}s: {exc}"
        )
        if _fail_hard_enabled():
            raise RuntimeError("janitor run-all timeout config unavailable") from exc
        return _FALLBACK_RUN_ALL_TIMEOUT_SECONDS
    if minutes <= 0:
        return float("inf")
    return max(1.0, minutes * 60.0)


def _start_run_all_deadline_watchdog(
    janitor_module,
    *,
    timeout_seconds: float | None = None,
) -> threading.Event:
    timeout = _run_all_timeout_seconds() if timeout_seconds is None else float(timeout_seconds)
    if timeout == float("inf"):
        return threading.Event()
    if timeout <= 0:
        raise ValueError("janitor run-all timeout must be positive")
    stop_event = threading.Event()

    def _watch() -> None:
        if stop_event.wait(timeout):
            return
        exc = TimeoutError(f"janitor run-all-once exceeded hard timeout of {timeout:.1f}s")
        _print_marker_warning(
            f"{exc}; exiting with code {_RUN_ALL_TIMEOUT_EXIT_CODE} to release held locks"
        )

        marker_thread = threading.Thread(
            target=_write_run_all_failure_markers,
            args=(exc, janitor_module),
            name="janitor-run-all-timeout-markers",
            daemon=True,
        )
        marker_thread.start()
        marker_thread.join(timeout=_RUN_ALL_TIMEOUT_MARKER_GRACE_SECONDS)
        if marker_thread.is_alive():
            _print_marker_warning(
                "timed out writing terminal failure markers; forcing worker exit"
            )
        _release_run_all_lock(janitor_module)
        os._exit(_RUN_ALL_TIMEOUT_EXIT_CODE)

    thread = threading.Thread(target=_watch, name="janitor-run-all-deadline", daemon=True)
    thread.start()
    return stop_event


def _release_run_all_lock(janitor_module) -> None:
    release_fn = getattr(janitor_module, "_release_lock", None)
    if not callable(release_fn):
        return
    try:
        release_fn()
    except Exception as exc:
        _print_marker_warning(f"failed to release janitor lock before forced exit: {exc}")


def run_scheduler_once() -> int:
    from core.compatibility import JanitorScheduler
    from lib.instance import instance_root

    root = instance_root()
    scheduler = JanitorScheduler(data_dir=root / "data", quaid_home=root)
    scheduler.tick()
    return 0


def run_all_once() -> int:
    from core.lifecycle import janitor

    deadline_stop = _start_run_all_deadline_watchdog(janitor)
    try:
        result = janitor.run_task_optimized(task="all", dry_run=False)
    except Exception as exc:
        _write_run_all_failure_markers(exc, janitor)
        raise
    finally:
        deadline_stop.set()
    return 0 if bool(result.get("success")) else 1


def _write_run_all_failure_markers(exc: Exception, janitor_module) -> None:
    """Write standard terminal janitor markers before preserving failHard exit."""
    try:
        completed_at = _worker_now_iso()
        logs_dir = _worker_logs_dir(janitor_module)
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
        _append_janitor_log_event(
            logs_dir,
            task="all",
            event="janitor_complete",
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
            _print_marker_warning(f"failed to read existing checkpoint {checkpoint_path}: {checkpoint_exc}")
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
        _atomic_write_json(checkpoint_path, checkpoint)
        try:
            usage = janitor_module.get_token_usage()
        except Exception as usage_exc:
            _print_marker_warning(f"failed to collect token usage for terminal failure marker: {usage_exc}")
            usage = {"api_calls": 0, "input_tokens": 0, "output_tokens": 0}
        try:
            estimated_cost = janitor_module.estimate_cost()
        except Exception as cost_exc:
            _print_marker_warning(f"failed to estimate cost for terminal failure marker: {cost_exc}")
            estimated_cost = 0.0
        _write_failure_stats(logs_dir, result=result, usage=usage, estimated_cost_usd=estimated_cost, completed_at=completed_at)
    except Exception as marker_exc:
        print(
            f"[janitor-worker] failed to write terminal failure markers: {marker_exc}",
            file=sys.stderr,
            flush=True,
        )


def _print_marker_warning(message: str) -> None:
    print(f"[janitor-worker] {message}", file=sys.stderr, flush=True)


def _worker_now_iso() -> str:
    raw = str(os.environ.get("QUAID_NOW", "") or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Invalid QUAID_NOW={raw!r}") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _worker_logs_dir(janitor_module) -> Path:
    logs_dir_fn = getattr(janitor_module, "_logs_dir", None)
    if callable(logs_dir_fn):
        return Path(logs_dir_fn())
    from lib.runtime_context import get_logs_dir

    return get_logs_dir()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _append_janitor_log_event(logs_dir: Path, event: str, **data: object) -> None:
    log_path = logs_dir / "janitor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": _worker_now_iso().replace("+00:00", "Z"),
        "level": "info",
        "component": "janitor",
        "event": event,
        **data,
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _write_failure_stats(
    logs_dir: Path,
    *,
    result: dict[str, object],
    usage: dict[str, object],
    estimated_cost_usd: float,
    completed_at: str,
) -> None:
    stats_file = logs_dir / "janitor-stats.json"
    existing: dict[str, object] = {}
    try:
        loaded = json.loads(stats_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            existing = loaded
    except FileNotFoundError:
        existing = {}
    except Exception as stats_exc:
        _print_marker_warning(f"failed to read existing janitor stats {stats_file}: {stats_exc}")
        existing = {}
    previous_completed_at = str(existing.get("last_janitor_completed_at") or "").strip()
    stats_data: dict[str, object] = {
        "last_run": completed_at,
        "task": "all",
        "dry_run": False,
        "success": False,
        "applied_changes": result.get("applied_changes", {}),
        "metrics": result.get("metrics", {}),
        "api_usage": {
            "calls": usage.get("api_calls", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "estimated_cost_usd": estimated_cost_usd,
        },
        "last_janitor_failed_at": completed_at,
    }
    if previous_completed_at:
        stats_data["last_janitor_completed_at"] = previous_completed_at
    _atomic_write_json(stats_file, stats_data)


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
