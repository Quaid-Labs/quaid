"""Cross-process resource lock primitives."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Tuple

MAX_THREAD_LOCK_CACHE = 1024


def _lock_poll_interval_seconds(default_seconds: float = 0.005) -> float:
    raw = os.environ.get("QUAID_LOCK_POLL_INTERVAL_MS", "")
    if str(raw).strip():
        try:
            parsed_ms = float(raw)
            if parsed_ms > 0:
                return max(0.001, parsed_ms / 1000.0)
        except (TypeError, ValueError):
            logging.getLogger(__name__).warning(
                "Invalid QUAID_LOCK_POLL_INTERVAL_MS=%r; using default",
                raw,
            )
    return float(default_seconds)


LOCK_POLL_INTERVAL_SECONDS = _lock_poll_interval_seconds()
logger = logging.getLogger(__name__)


class ResourceLockRegistry:
    """Global lock registry for file/db resource coordination."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._thread_locks: Dict[str, threading.RLock] = {}
        self._thread_lock_touched: Dict[str, float] = {}
        self._thread_lock_refs: Dict[str, int] = {}
        self._thread_guard = threading.Lock()

    def _prune_thread_locks(self) -> None:
        if len(self._thread_locks) < MAX_THREAD_LOCK_CACHE:
            return
        # Drop unlocked, oldest locks first; never block on an in-use lock.
        by_age = sorted(self._thread_lock_touched.items(), key=lambda item: item[1])
        for resource, _ in by_age:
            if len(self._thread_locks) <= MAX_THREAD_LOCK_CACHE:
                break
            lock = self._thread_locks.get(resource)
            if lock is None:
                self._thread_lock_touched.pop(resource, None)
                self._thread_lock_refs.pop(resource, None)
                continue
            if self._thread_lock_refs.get(resource, 0) > 0:
                continue
            # Never evict a lock currently owned by this thread.
            # RLock.acquire(blocking=False) is re-entrant and would otherwise
            # succeed, causing active locks to be dropped from the registry.
            if getattr(lock, "_is_owned", lambda: False)():
                continue
            if not lock.acquire(blocking=False):
                continue
            try:
                self._thread_locks.pop(resource, None)
                self._thread_lock_touched.pop(resource, None)
                self._thread_lock_refs.pop(resource, None)
            finally:
                lock.release()

    def _thread_lock_unlocked(self, resource: str) -> threading.RLock:
        lock = self._thread_locks.get(resource)
        if lock is None:
            lock = threading.RLock()
            self._thread_locks[resource] = lock
            self._thread_lock_touched[resource] = time.monotonic()
            return lock
        self._thread_lock_touched[resource] = time.monotonic()
        return lock

    def _thread_lock(self, resource: str) -> threading.RLock:
        with self._thread_guard:
            lock = self._thread_lock_unlocked(resource)
            self._prune_thread_locks()
            return lock

    def _reserve_thread_lock(self, resource: str) -> threading.RLock:
        with self._thread_guard:
            lock = self._thread_lock_unlocked(resource)
            self._thread_lock_refs[resource] = self._thread_lock_refs.get(resource, 0) + 1
            self._prune_thread_locks()
            return lock

    def _release_thread_lock_reservation(self, resource: str) -> None:
        with self._thread_guard:
            refs = self._thread_lock_refs.get(resource, 0)
            if refs <= 1:
                self._thread_lock_refs.pop(resource, None)
            else:
                self._thread_lock_refs[resource] = refs - 1

    def _lockfile_for(self, resource: str) -> Path:
        digest = hashlib.sha1(resource.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.lock"

    @contextmanager
    def acquire_many(self, resources: List[str], timeout_seconds: int = 120):
        ordered = sorted(set(str(r).strip() for r in (resources or []) if str(r).strip()))
        if not ordered:
            yield
            return

        deadline = time.monotonic() + max(1, int(timeout_seconds))
        acquired_thread: List[Tuple[str, threading.RLock]] = []
        acquired_fds: List[int] = []
        try:
            for resource in ordered:
                thread_lock = self._reserve_thread_lock(resource)
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    if not thread_lock.acquire(timeout=remaining):
                        raise TimeoutError(f"thread lock timeout for {resource}")
                except Exception:
                    self._release_thread_lock_reservation(resource)
                    raise
                acquired_thread.append((resource, thread_lock))

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
                        time.sleep(min(LOCK_POLL_INTERVAL_SECONDS, remaining))
                    except Exception:
                        try:
                            os.close(fd)
                        except Exception as close_exc:
                            logger.warning(
                                "Failed to close lock fd=%s during lock error cleanup: %s",
                                fd,
                                close_exc,
                            )
                        raise
                acquired_fds.append(fd)

            yield
        finally:
            for fd in reversed(acquired_fds):
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception as exc:
                    logger.warning("Failed to release file lock fd=%s: %s", fd, exc)
                try:
                    os.close(fd)
                except Exception as exc:
                    logger.warning("Failed to close lock fd=%s: %s", fd, exc)
            for resource, lock in reversed(acquired_thread):
                try:
                    lock.release()
                except Exception as exc:
                    logger.warning("Failed to release thread lock: %s", exc)
                finally:
                    self._release_thread_lock_reservation(resource)
