"""Global LLM concurrency gate.

All LLM calls should pass through this allocator so system-wide concurrency
is centrally managed from config.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional


_POOL_LOCK = threading.Lock()
_POOL: Optional[threading.BoundedSemaphore] = None
_POOL_SIZE = 0


def _configured_slots() -> int:
    try:
        from config import get_config

        cfg = get_config()
        slots = int(getattr(cfg.janitor.parallel, "llm_workers", 4) or 4)
        return max(1, slots)
    except Exception:
        return 4


def _ensure_pool() -> threading.BoundedSemaphore:
    global _POOL, _POOL_SIZE
    desired = _configured_slots()
    with _POOL_LOCK:
        if _POOL is None or _POOL_SIZE != desired:
            _POOL = threading.BoundedSemaphore(desired)
            _POOL_SIZE = desired
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

