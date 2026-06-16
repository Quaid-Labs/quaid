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


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except ImportError as exc:
        logger.critical("fail-hard policy unavailable in LLM scheduler; failing closed: %s", exc)
        return True


class GlobalLlmScheduler:
    """Global bounded executor with per-workload adaptive concurrency caps."""

    def __init__(self, max_workers: int = 32) -> None:
        self._max_workers = max(1, int(max_workers))
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._worker_slots = threading.BoundedSemaphore(self._max_workers)
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

    def _submit_to_executor(
        self,
        *,
        workload_key: str,
        item: Any,
        fn: Callable[[Any], Any],
        slot_timeout: float,
    ):
        timeout = max(0.0, float(slot_timeout))
        if not self._worker_slots.acquire(timeout=timeout):
            raise TimeoutError(
                f"Global LLM scheduler worker slot unavailable after {timeout:.2f}s "
                f"(workload={workload_key})"
            )

        released = False
        release_lock = threading.Lock()

        def _release_once() -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
                self._worker_slots.release()

        def _run():
            try:
                return fn(item)
            finally:
                _release_once()

        try:
            fut = self._executor.submit(_run)
        except Exception:
            _release_once()
            raise

        def _release_if_cancelled(done_fut) -> None:
            if done_fut.cancelled():
                _release_once()

        fut.add_done_callback(_release_if_cancelled)
        return fut

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

        # Acquire one platform-level slot for this run_map invocation. The local
        # scheduler still owns in-process worker fanout; this is not a per-worker
        # lease. Non-fatal: if no platform client, proceed normally.
        _platform_client = get_platform_scheduler_client_for_current_instance()
        if _platform_client is not None:
            try:
                _platform_client.acquire(1)
            except Exception as exc:
                logger.warning(
                    "platform slot acquire failed (%s): proceeding without slot gating", exc
                )
                if _fail_hard_enabled():
                    raise
                _platform_client = None

        try:
            return self._run_map_inner(
                workload_key=workload_key,
                seq=seq,
                fn=fn,
                configured_workers=configured_workers,
                requested_workers=requested_workers,
                timeout_seconds=timeout_seconds,
                timeout_retries=timeout_retries,
            )
        finally:
            if _platform_client is not None:
                try:
                    _platform_client.release(1)
                except Exception as exc:
                    logger.warning("platform slot release failed: %s", exc)
                    if _fail_hard_enabled():
                        raise

    def _run_map_inner(
        self,
        *,
        workload_key: str,
        seq: List[Any],
        fn: Callable[[Any], Any],
        configured_workers: int,
        requested_workers: Optional[int] = None,
        timeout_seconds: float = 300.0,
        timeout_retries: int = 1,
    ) -> List[Any]:
        configured = max(1, min(int(configured_workers), self._max_workers))
        timeout = max(0.001, float(timeout_seconds))
        worker_count = self._resolve_start_workers(
            workload_key=workload_key,
            configured_workers=configured,
            requested_workers=requested_workers,
            item_count=len(seq),
        )
        if worker_count <= 1:
            return self._run_serial_with_timeout(
                workload_key=workload_key,
                seq=seq,
                fn=fn,
                configured_workers=configured,
                timeout_seconds=timeout,
            )

        retries_left = max(0, int(timeout_retries))
        results: List[Any] = [None] * len(seq)
        remaining_indices = list(range(len(seq)))

        while True:
            if not remaining_indices:
                self._record_success(workload_key, configured, worker_count)
                return results

            if worker_count <= 1:
                serial_results = self._run_serial_with_timeout(
                    workload_key=workload_key,
                    seq=[seq[idx] for idx in remaining_indices],
                    fn=fn,
                    configured_workers=configured,
                    timeout_seconds=timeout,
                )
                for idx, value in zip(remaining_indices, serial_results):
                    results[idx] = value
                return results

            in_flight: Dict[Any, int] = {}
            attempt_pending = list(remaining_indices)
            cursor = 0

            def _submit_available(*, slot_timeout: float) -> None:
                nonlocal cursor
                while cursor < len(attempt_pending) and len(in_flight) < worker_count:
                    idx = attempt_pending[cursor]
                    try:
                        acquire_timeout = slot_timeout if not in_flight else 0.0
                        fut = self._submit_to_executor(
                            workload_key=workload_key,
                            item=seq[idx],
                            fn=fn,
                            slot_timeout=acquire_timeout,
                        )
                    except TimeoutError:
                        if in_flight:
                            return
                        raise
                    cursor += 1
                    in_flight[fut] = idx

            _submit_available(slot_timeout=timeout)
            deadline = time.monotonic() + timeout
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
                _submit_available(slot_timeout=0.0)

            if not timed_out:
                # Completed all in-flight work for this attempt; if there are no remaining
                # indices we are done, else loop will execute next attempt for leftovers.
                if completed_this_attempt:
                    remaining_set = set(remaining_indices)
                    remaining_set.difference_update(completed_this_attempt)
                    remaining_indices = [idx for idx in remaining_indices if idx in remaining_set]
                continue

            # Timeout path: cancel queued futures, keep only incomplete indices for retry.
            for fut, idx in list(in_flight.items()):
                if not fut.done():
                    continue
                in_flight.pop(fut, None)
                try:
                    results[idx] = fut.result()
                except Exception:
                    for pending in list(in_flight.keys()):
                        pending.cancel()
                    raise
                completed_this_attempt.append(idx)

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

    def _run_serial_with_timeout(
        self,
        *,
        workload_key: str,
        seq: List[Any],
        fn: Callable[[Any], Any],
        configured_workers: int,
        timeout_seconds: float,
    ) -> List[Any]:
        timeout = max(0.001, float(timeout_seconds))
        deadline = time.monotonic() + timeout
        results: List[Any] = []
        serial_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="quaid-llm-serial")
        try:
            for idx, item in enumerate(seq):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._set_backoff_cap(workload_key, configured_workers, 1)
                    raise TimeoutError(
                        f"Serial map timed out after {timeout:.2f}s "
                        f"(items={len(seq)}, item={idx + 1}, workload={workload_key})"
                    )
                fut = serial_executor.submit(fn, item)
                try:
                    results.append(fut.result(timeout=remaining))
                except TimeoutError:
                    fut.cancel()
                    self._set_backoff_cap(workload_key, configured_workers, 1)
                    raise TimeoutError(
                        f"Serial map timed out after {timeout:.2f}s "
                        f"(items={len(seq)}, item={idx + 1}, workload={workload_key})"
                    )
                except Exception:
                    fut.cancel()
                    raise
            self._record_success(workload_key, configured_workers, 1)
            return results
        finally:
            serial_executor.shutdown(wait=False, cancel_futures=True)


_SCHEDULER: Optional[GlobalLlmScheduler] = None
_SCHEDULER_LOCK = threading.RLock()

_PLATFORM_CLIENT = None
_PLATFORM_CLIENT_LOCK = threading.RLock()
_PLATFORM_CLIENT_INITIALIZED = False


def _scheduler_max_workers(default_max_workers: int = 32) -> int:
    raw = str(os.environ.get("QUAID_GLOBAL_LLM_MAX_WORKERS", "") or "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            logger.warning("Invalid QUAID_GLOBAL_LLM_MAX_WORKERS=%r; using default", raw)
            if _fail_hard_enabled():
                raise
    return int(default_max_workers)


def _platform_scheduler_slots(raw: Any, default_slots: int = 8) -> int:
    if raw is None:
        return int(default_slots)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid platform_scheduler_slots=%r; using default", raw)
        if _fail_hard_enabled():
            raise
        return int(default_slots)
    if parsed <= 0:
        logger.warning("Invalid platform_scheduler_slots=%r; using default", raw)
        if _fail_hard_enabled():
            raise ValueError("platform_scheduler_slots must be positive")
        return int(default_slots)
    return parsed


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


def get_platform_scheduler_client_for_current_instance():
    """Get the platform-level LLM slot client for the current adapter instance.

    Reads quaid_home and platform from the active adapter, then reads
    total_slots from config (core.parallel.platform_scheduler_slots, default 8).
    Result is cached module-level after first successful initialization.

    Returns None if unavailable (non-fatal: callers proceed without slot gating).
    """
    global _PLATFORM_CLIENT, _PLATFORM_CLIENT_INITIALIZED
    with _PLATFORM_CLIENT_LOCK:
        if _PLATFORM_CLIENT_INITIALIZED:
            return _PLATFORM_CLIENT
        try:
            from lib.adapter import get_adapter
            from core.platform_scheduler import get_platform_scheduler_client
            adapter = get_adapter()
            quaid_home = adapter.quaid_home()
            platform = adapter.agent_id_prefix()
            total_slots = 8
            try:
                from config import get_config
                from core.runtime.parallel_runtime import get_parallel_config
                cfg = get_config()
                parallel_cfg = get_parallel_config(cfg)
                total_slots = _platform_scheduler_slots(
                    getattr(parallel_cfg, "platform_scheduler_slots", None),
                    default_slots=total_slots,
                )
            except Exception as exc:
                logger.warning("platform_scheduler_slots config read failed; using default: %s", exc)
                if _fail_hard_enabled():
                    raise
            _PLATFORM_CLIENT = get_platform_scheduler_client(quaid_home, platform, total_slots)
            _PLATFORM_CLIENT_INITIALIZED = True
        except Exception as exc:
            logger.warning(
                "platform scheduler client init failed (%s): proceeding without slot gating", exc
            )
            if _fail_hard_enabled():
                raise
            _PLATFORM_CLIENT = None
        return _PLATFORM_CLIENT


def reset_platform_scheduler_client() -> None:
    """Reset the cached platform scheduler client (for testing / re-init)."""
    global _PLATFORM_CLIENT, _PLATFORM_CLIENT_INITIALIZED
    with _PLATFORM_CLIENT_LOCK:
        client = _PLATFORM_CLIENT
        _PLATFORM_CLIENT = None
        _PLATFORM_CLIENT_INITIALIZED = False
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                logger.warning("platform scheduler client close failed: %s", exc)
                if _fail_hard_enabled():
                    raise
