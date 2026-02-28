import os
import sys
import fcntl
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.runtime.parallel_runtime import MAX_THREAD_LOCK_CACHE, ResourceLockRegistry


def test_resource_lock_registry_prunes_thread_lock_cache(tmp_path: Path):
    reg = ResourceLockRegistry(tmp_path / "locks")

    for i in range(MAX_THREAD_LOCK_CACHE + 80):
        reg._thread_lock(f"resource-{i}")

    assert len(reg._thread_locks) <= MAX_THREAD_LOCK_CACHE


def test_acquire_many_logs_unlock_failures(tmp_path: Path):
    reg = ResourceLockRegistry(tmp_path / "locks")
    real_flock = fcntl.flock
    unlock_attempts = {"count": 0}

    def _flock_with_unlock_error(fd, op):
        if op == fcntl.LOCK_UN and unlock_attempts["count"] == 0:
            unlock_attempts["count"] += 1
            raise OSError("unlock boom")
        return real_flock(fd, op)

    with patch("core.runtime.parallel_runtime.fcntl.flock", side_effect=_flock_with_unlock_error), \
         patch("core.runtime.parallel_runtime.logger.warning") as log_warning:
        with reg.acquire_many(["resource-a"]):
            pass

    assert any("Failed to release file lock" in str(call.args[0]) for call in log_warning.call_args_list)


def test_acquire_many_logs_close_failures(tmp_path: Path):
    reg = ResourceLockRegistry(tmp_path / "locks")
    real_close = os.close
    close_attempts = {"count": 0}

    def _close_with_error(fd):
        if close_attempts["count"] == 0:
            close_attempts["count"] += 1
            raise OSError("close boom")
        return real_close(fd)

    with patch("core.runtime.parallel_runtime.os.close", side_effect=_close_with_error), \
         patch("core.runtime.parallel_runtime.logger.warning") as log_warning:
        with reg.acquire_many(["resource-b"]):
            pass

    assert any("Failed to close lock fd=" in str(call.args[0]) for call in log_warning.call_args_list)


def test_acquire_many_uses_short_lock_poll_interval(tmp_path: Path):
    reg = ResourceLockRegistry(tmp_path / "locks")
    real_flock = fcntl.flock
    sleep_calls = []
    attempts = {"count": 0}

    def _flock_once_blocking(fd, op):
        if op == (fcntl.LOCK_EX | fcntl.LOCK_NB) and attempts["count"] == 0:
            attempts["count"] += 1
            raise BlockingIOError()
        return real_flock(fd, op)

    def _record_sleep(seconds):
        sleep_calls.append(float(seconds))

    with patch("core.runtime.parallel_runtime.fcntl.flock", side_effect=_flock_once_blocking), \
         patch("core.runtime.parallel_runtime.time.sleep", side_effect=_record_sleep):
        with reg.acquire_many(["resource-c"], timeout_seconds=1):
            pass

    assert sleep_calls
    assert max(sleep_calls) <= 0.01


def test_acquire_many_closes_fd_on_nonblocking_flock_error(tmp_path: Path):
    reg = ResourceLockRegistry(tmp_path / "locks")
    close_calls = {"count": 0}
    real_close = os.close

    def _flock_raises_oserror(_fd, op):
        if op == (fcntl.LOCK_EX | fcntl.LOCK_NB):
            raise OSError("flock boom")
        return None

    def _count_close(fd):
        close_calls["count"] += 1
        return real_close(fd)

    with patch("core.runtime.parallel_runtime.fcntl.flock", side_effect=_flock_raises_oserror), \
         patch("core.runtime.parallel_runtime.os.close", side_effect=_count_close):
        try:
            with reg.acquire_many(["resource-d"], timeout_seconds=1):
                pass
            assert False, "Expected OSError from flock"
        except OSError as exc:
            assert "flock boom" in str(exc)

    assert close_calls["count"] >= 1
