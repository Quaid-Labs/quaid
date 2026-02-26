import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.runtime.parallel_runtime import MAX_THREAD_LOCK_CACHE, ResourceLockRegistry


def test_resource_lock_registry_prunes_thread_lock_cache(tmp_path: Path):
    reg = ResourceLockRegistry(tmp_path / "locks")

    for i in range(MAX_THREAD_LOCK_CACHE + 80):
        reg._thread_lock(f"resource-{i}")

    assert len(reg._thread_locks) <= MAX_THREAD_LOCK_CACHE
