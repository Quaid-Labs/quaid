"""Global LLM concurrency gate.

All LLM calls should pass through this allocator so system-wide concurrency
is centrally managed from config.
"""

from __future__ import annotations

import threading
import sys
import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


_POOL_LOCK = threading.Lock()
_POOL: Optional[threading.BoundedSemaphore] = None
_DEEP_POOL: Optional[threading.BoundedSemaphore] = None
_POOL_SIZE = 0
_DEEP_POOL_SIZE = 0
_POOL_RESIZE_WARNED = False
_DEFAULT_PROCESS_LOCK_SLOTS = 4
_DEFAULT_FAST_RESERVED_SLOTS = 1


def _configured_slots() -> int:
    from config import get_config

    cfg = get_config()
    core = getattr(cfg, "core", None)
    parallel = getattr(core, "parallel", None) if core else None
    if parallel is None:
        raise RuntimeError("Missing required config: core.parallel")
    slots = int(getattr(parallel, "llm_workers", 4) or 4)
    return max(1, slots)


def _configured_fast_reserved_slots(total_slots: Optional[int] = None) -> int:
    raw = str(os.environ.get("QUAID_LLM_FAST_RESERVED_SLOTS", "") or "").strip()
    if raw:
        try:
            reserve = max(0, int(raw))
        except Exception:
            reserve = _DEFAULT_FAST_RESERVED_SLOTS
    else:
        reserve = _DEFAULT_FAST_RESERVED_SLOTS
    total = _configured_slots() if total_slots is None else max(1, int(total_slots))
    if total <= 1:
        return 0
    return max(0, min(total - 1, reserve))


def _ensure_pool() -> threading.BoundedSemaphore:
    global _POOL, _DEEP_POOL, _POOL_SIZE, _DEEP_POOL_SIZE, _POOL_RESIZE_WARNED
    desired = _configured_slots()
    desired_deep = max(1, desired - _configured_fast_reserved_slots(desired))
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = threading.BoundedSemaphore(desired)
            _DEEP_POOL = threading.BoundedSemaphore(desired_deep)
            _POOL_SIZE = desired
            _DEEP_POOL_SIZE = desired_deep
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


def _ensure_deep_pool() -> threading.BoundedSemaphore:
    _ensure_pool()
    assert _DEEP_POOL is not None
    return _DEEP_POOL


def _process_lock_enabled() -> bool:
    override = str(os.environ.get("QUAID_LLM_PROCESS_LOCK", "")).strip().lower()
    if override in {"0", "false", "no", "off"}:
        return False
    if override in {"1", "true", "yes", "on"}:
        return True
    return True


def _process_lock_slot_count() -> int:
    raw = str(os.environ.get("QUAID_LLM_PROCESS_LOCK_SLOTS", "") or "").strip()
    if not raw:
        try:
            return max(1, _configured_slots())
        except Exception:
            return _DEFAULT_PROCESS_LOCK_SLOTS
    try:
        return max(1, min(64, int(raw)))
    except Exception:
        return _DEFAULT_PROCESS_LOCK_SLOTS


def _process_lock_paths(pool_kind: str = "fast") -> list[Path]:
    """Return cross-process LLM slot lock paths for the requested pool kind."""
    if not _process_lock_enabled():
        return []

    home_raw = str(os.environ.get("QUAID_HOME", "") or "").strip()
    home = Path(home_raw).expanduser() if home_raw else Path.home() / ".quaid"
    lock_dir = home / "shared" / "run" / "llm"
    slot_count = _process_lock_slot_count()
    reserved_fast = _configured_fast_reserved_slots(slot_count)
    start_idx = reserved_fast if str(pool_kind or "fast").strip().lower() == "deep" else 0
    return [lock_dir / f"slot-{idx}.lock" for idx in range(start_idx, slot_count)]


def _process_lock_path() -> Optional[Path]:
    """Return the first cross-process LLM slot path, kept for diagnostics/tests."""
    paths = _process_lock_paths("fast")
    if not paths:
        return None
    return paths[0]


@contextmanager
def _acquire_process_lock(timeout_seconds: Optional[float], pool_kind: str = "fast") -> Iterator[None]:
    lock_paths = _process_lock_paths(pool_kind)
    if not lock_paths:
        yield
        return

    lock_paths[0].parent.mkdir(parents=True, exist_ok=True)
    deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, float(timeout_seconds))
    acquired_fd = None
    try:
        while acquired_fd is None:
            last_blocked_error = None
            for lock_path in lock_paths:
                fd = open(lock_path, "a")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired_fd = fd
                    break
                except OSError as exc:
                    fd.close()
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    last_blocked_error = exc
            if acquired_fd is not None:
                break
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Timed out waiting for cross-process LLM worker slot") from last_blocked_error
                time.sleep(min(0.05, remaining))
            else:
                time.sleep(0.05)
        yield
    finally:
        if acquired_fd is not None:
            try:
                fcntl.flock(acquired_fd, fcntl.LOCK_UN)
            finally:
                acquired_fd.close()


@contextmanager
def acquire_llm_slot(timeout_seconds: Optional[float] = None, pool_kind: str = "fast") -> Iterator[None]:
    """Acquire a shared LLM slot before making provider calls."""
    sem = _ensure_pool()
    deep_sem = _ensure_deep_pool() if str(pool_kind or "fast").strip().lower() == "deep" else None
    deep_acquired = False
    if deep_sem is not None:
        if timeout_seconds is None:
            deep_acquired = deep_sem.acquire()
        else:
            deep_acquired = deep_sem.acquire(timeout=max(0.0, float(timeout_seconds)))
        if not deep_acquired:
            raise TimeoutError("Timed out waiting for deep LLM worker slot")
    try:
        if timeout_seconds is None:
            acquired = sem.acquire()
        else:
            acquired = sem.acquire(timeout=max(0.0, float(timeout_seconds)))
    except BaseException:
        if deep_acquired and deep_sem is not None:
            deep_sem.release()
        raise
    if not acquired:
        if deep_acquired and deep_sem is not None:
            deep_sem.release()
        raise TimeoutError("Timed out waiting for LLM worker slot")
    try:
        with _acquire_process_lock(timeout_seconds, pool_kind):
            yield
    finally:
        sem.release()
        if deep_acquired and deep_sem is not None:
            deep_sem.release()
