"""Shared lock primitives for project-registry.json mutation."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import threading
import time
from pathlib import Path

_registry_thread_lock = threading.Lock()
REGISTRY_LOCK_TIMEOUT_SECONDS = 30.0
REGISTRY_LOCK_RETRY_SECONDS = 0.05


def registry_path() -> Path:
    home = os.environ.get("QUAID_HOME", "").strip()
    if home:
        return Path(home).resolve() / "project-registry.json"
    try:
        from lib.adapter import get_adapter

        return get_adapter().quaid_home() / "project-registry.json"
    except Exception:
        return (Path.home() / ".quaid") / "project-registry.json"


def registry_lock_path() -> Path:
    return registry_path().with_suffix(".json.lock")


@contextlib.contextmanager
def registry_lock():
    path = registry_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    started_at = time.monotonic()
    while True:
        remaining = REGISTRY_LOCK_TIMEOUT_SECONDS - (time.monotonic() - started_at)
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for project registry lock: {path}")
        if not _registry_thread_lock.acquire(timeout=min(remaining, REGISTRY_LOCK_RETRY_SECONDS)):
            continue
        try:
            lock_fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                os.close(lock_fd)
                lock_fd = None
        except Exception:
            if lock_fd is not None:
                os.close(lock_fd)
            _registry_thread_lock.release()
            raise
        _registry_thread_lock.release()
        time.sleep(REGISTRY_LOCK_RETRY_SECONDS)

    try:
        yield
    finally:
        try:
            if lock_fd is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            _registry_thread_lock.release()
