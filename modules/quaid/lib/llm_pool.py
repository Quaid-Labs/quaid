"""Global LLM concurrency gate.

All LLM calls should pass through this allocator so system-wide concurrency
is centrally managed from config.
"""

from __future__ import annotations

import threading
import sys
from contextlib import contextmanager
from typing import Iterator, Optional


_POOL_LOCK = threading.Lock()
_POOL: Optional[threading.BoundedSemaphore] = None
_POOL_SIZE = 0
_POOL_RESIZE_WARNED = False


def _configured_slots() -> int:
    from config import get_config

    cfg = get_config()
    core = getattr(cfg, "core", None)
    parallel = getattr(core, "parallel", None) if core else None
    if parallel is None:
        raise RuntimeError("Missing required config: core.parallel")
    slots = int(getattr(parallel, "llm_workers", 4) or 4)
    return max(1, slots)


def _ensure_pool() -> threading.BoundedSemaphore:
    global _POOL, _POOL_SIZE, _POOL_RESIZE_WARNED
    desired = _configured_slots()
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = threading.BoundedSemaphore(desired)
            _POOL_SIZE = desired
            _POOL_RESIZE_WARNED = False
        elif _POOL_SIZE != desired and not _POOL_RESIZE_WARNED:
            # Resizing a live semaphore can strand waiters on the old instance.
            # Keep the existing pool for process lifetime; changes apply on restart.
            _POOL_RESIZE_WARNED = True
            print(
                f"[llm_pool] Requested pool resize {_POOL_SIZE} -> {desired} ignored for safety; "
                "restart process to apply.",
                file=sys.stderr,
            )
        return _POOL


@contextmanager
def acquire_llm_slot(timeout_seconds: Optional[float] = None) -> Iterator[None]:
    """Acquire a shared LLM slot before making provider calls."""
    sem = _ensure_pool()
    if timeout_seconds is None:
        acquired = sem.acquire()
    else:
        acquired = sem.acquire(timeout=max(0.0, float(timeout_seconds)))
    if not acquired:
        raise TimeoutError("Timed out waiting for LLM worker slot")
    try:
        yield
    finally:
        sem.release()
