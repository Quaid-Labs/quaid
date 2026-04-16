"""Shared lock primitives for project-registry.json mutation."""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
from pathlib import Path

_registry_thread_lock = threading.Lock()


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
    with _registry_thread_lock:
        lock_fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
