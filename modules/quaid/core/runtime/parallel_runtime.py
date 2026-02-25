"""Core-owned parallel runtime primitives.

Provides:
- Global resource lock registry for lifecycle routines.
- Strict core parallel config resolution.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List


def get_parallel_config(cfg: Any) -> Any:
    """Resolve core parallel config strictly from cfg.core.parallel."""
    core = getattr(cfg, "core", None)
    core_parallel = getattr(core, "parallel", None) if core else None
    if core_parallel is None:
        raise RuntimeError("Missing required config: core.parallel")
    return core_parallel


class ResourceLockRegistry:
    """Global lock registry for file/db resource coordination."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._thread_locks: Dict[str, threading.RLock] = {}
        self._thread_guard = threading.Lock()

    def _thread_lock(self, resource: str) -> threading.RLock:
        with self._thread_guard:
            lock = self._thread_locks.get(resource)
            if lock is None:
                lock = threading.RLock()
                self._thread_locks[resource] = lock
            return lock

    def _lockfile_for(self, resource: str) -> Path:
        digest = hashlib.sha1(resource.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.lock"

    @contextmanager
    def acquire_many(self, resources: List[str], timeout_seconds: int = 120):
        import fcntl

        ordered = sorted(set(str(r).strip() for r in (resources or []) if str(r).strip()))
        if not ordered:
            yield
            return

        deadline = time.monotonic() + max(1, int(timeout_seconds or 120))
        acquired_thread: List[threading.RLock] = []
        acquired_fds: List[int] = []
        try:
            for resource in ordered:
                thread_lock = self._thread_lock(resource)
                remaining = max(0.0, deadline - time.monotonic())
                if not thread_lock.acquire(timeout=remaining):
                    raise TimeoutError(f"thread lock timeout for {resource}")
                acquired_thread.append(thread_lock)

                lockfile = self._lockfile_for(resource)
                fd = os.open(str(lockfile), os.O_RDWR | os.O_CREAT, 0o600)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            os.close(fd)
                            raise TimeoutError(f"file lock timeout for {resource}")
                        time.sleep(min(0.05, remaining))
                acquired_fds.append(fd)

            yield
        finally:
            for fd in reversed(acquired_fds):
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    os.close(fd)
                except Exception:
                    pass
            for lock in reversed(acquired_thread):
                try:
                    lock.release()
                except Exception:
                    pass
