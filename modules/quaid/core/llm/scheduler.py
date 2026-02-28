#!/usr/bin/env python3
"""Global adaptive LLM scheduler.

This centralizes timeout-driven throttling/backoff and slow-release recovery for
all LLM-parallel call sites that opt in.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class GlobalLlmScheduler:
    """Global bounded executor with per-workload adaptive concurrency caps."""

    def __init__(self, max_workers: int = 32) -> None:
        self._max_workers = max(1, int(max_workers))
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._caps: Dict[str, int] = {}
        self._caps_lock = threading.RLock()

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _resolve_start_workers(
        self,
        *,
        workload_key: str,
        configured_workers: int,
        requested_workers: Optional[int],
        item_count: int,
    ) -> int:
        configured = max(1, int(configured_workers))
        requested = configured if requested_workers is None else max(1, int(requested_workers))
        ceiling = max(1, min(configured, requested, item_count))
        with self._caps_lock:
            current = int(self._caps.get(workload_key, configured))
            current = max(1, min(current, configured))
            self._caps[workload_key] = current
            return min(current, ceiling)

    def _set_backoff_cap(self, workload_key: str, configured_workers: int, active_workers: int) -> int:
        configured = max(1, int(configured_workers))
        next_workers = max(1, int(active_workers) // 2)
        with self._caps_lock:
            self._caps[workload_key] = max(1, min(next_workers, configured))
            return self._caps[workload_key]

    def _record_success(self, workload_key: str, configured_workers: int, active_workers: int) -> None:
        configured = max(1, int(configured_workers))
        with self._caps_lock:
            current = int(self._caps.get(workload_key, configured))
            current = max(1, min(current, configured))
            if current < configured:
                current = min(configured, max(active_workers, current) + 1)
            self._caps[workload_key] = current

    def run_map(
        self,
        *,
        workload_key: str,
        items: List[Any],
        fn: Callable[[Any], Any],
        configured_workers: int,
        requested_workers: Optional[int] = None,
        timeout_seconds: float = 300.0,
        timeout_retries: int = 1,
    ) -> List[Any]:
        seq = list(items or [])
        if not seq:
            return []

        configured = max(1, min(int(configured_workers), self._max_workers))
        worker_count = self._resolve_start_workers(
            workload_key=workload_key,
            configured_workers=configured,
            requested_workers=requested_workers,
            item_count=len(seq),
        )
        if worker_count <= 1:
            out = [fn(item) for item in seq]
            self._record_success(workload_key, configured, 1)
            return out

        timeout = max(0.001, float(timeout_seconds))
        retries_left = max(0, int(timeout_retries))
        results: List[Any] = [None] * len(seq)
        remaining_indices = list(range(len(seq)))

        while True:
            if not remaining_indices:
                self._record_success(workload_key, configured, worker_count)
                return results

            if worker_count <= 1:
                for idx in remaining_indices:
                    results[idx] = fn(seq[idx])
                self._record_success(workload_key, configured, 1)
                return results

            deadline = time.monotonic() + timeout
            in_flight: Dict[Any, int] = {}
            attempt_pending = list(remaining_indices)
            cursor = 0

            def _submit_available() -> None:
                nonlocal cursor
                while cursor < len(attempt_pending) and len(in_flight) < worker_count:
                    idx = attempt_pending[cursor]
                    cursor += 1
                    fut = self._executor.submit(fn, seq[idx])
                    in_flight[fut] = idx

            _submit_available()
            timed_out = False
            completed_this_attempt: List[int] = []

            while in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    done = next(as_completed(list(in_flight.keys()), timeout=remaining))
                except TimeoutError:
                    timed_out = True
                    break
                idx = in_flight.pop(done)
                try:
                    results[idx] = done.result()
                except Exception:
                    for fut in list(in_flight.keys()):
                        fut.cancel()
                    raise
                completed_this_attempt.append(idx)
                _submit_available()

            if not timed_out:
                # Completed all in-flight work for this attempt; if there are no remaining
                # indices we are done, else loop will execute next attempt for leftovers.
                if completed_this_attempt:
                    remaining_set = set(remaining_indices)
                    remaining_set.difference_update(completed_this_attempt)
                    remaining_indices = [idx for idx in remaining_indices if idx in remaining_set]
                continue

            # Timeout path: cancel queued futures, keep only incomplete indices for retry.
            for fut in list(in_flight.keys()):
                fut.cancel()

            completed_set = set(completed_this_attempt)
            remaining_indices = [idx for idx in remaining_indices if idx not in completed_set]

            next_workers = self._set_backoff_cap(workload_key, configured, worker_count)
            if retries_left <= 0:
                raise TimeoutError(
                    f"Parallel map timed out after {timeout:.2f}s "
                    f"(items={len(seq)}, workers={worker_count}, workload={workload_key})"
                )

            logger.warning(
                "LLM scheduler timeout workload=%s workers=%s timeout=%.2fs retry_workers=%s retries_left=%s remaining=%s",
                workload_key,
                worker_count,
                timeout,
                next_workers,
                retries_left - 1,
                len(remaining_indices),
            )
            worker_count = max(1, min(next_workers, len(remaining_indices)))
            retries_left -= 1


_SCHEDULER: Optional[GlobalLlmScheduler] = None
_SCHEDULER_LOCK = threading.RLock()


def _scheduler_max_workers(default_max_workers: int = 32) -> int:
    raw = str(os.environ.get("QUAID_GLOBAL_LLM_MAX_WORKERS", "") or "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            logger.warning("Invalid QUAID_GLOBAL_LLM_MAX_WORKERS=%r; using default", raw)
    return int(default_max_workers)


def get_global_llm_scheduler() -> GlobalLlmScheduler:
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        if _SCHEDULER is None:
            _SCHEDULER = GlobalLlmScheduler(max_workers=_scheduler_max_workers())
        return _SCHEDULER


def reset_global_llm_scheduler(wait: bool = False) -> None:
    global _SCHEDULER
    with _SCHEDULER_LOCK:
        sched = _SCHEDULER
        _SCHEDULER = None
        if sched is not None:
            sched.shutdown(wait=wait)


atexit.register(reset_global_llm_scheduler, wait=False)
