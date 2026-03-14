"""Unit tests for lib/llm_pool.py.

Covers acquire_llm_slot() context manager, pool initialization,
timeout handling, concurrency gate, and resize-warning behavior.
"""

import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_pool():
    """Reset global pool state between tests."""
    import lib.llm_pool as m
    with m._POOL_LOCK:
        m._POOL = None
        m._POOL_SIZE = 0
        m._POOL_RESIZE_WARNED = False


def _cfg(workers: int = 2):
    """Build a minimal config with core.parallel.llm_workers."""
    parallel = SimpleNamespace(llm_workers=workers)
    core = SimpleNamespace(parallel=parallel)
    return SimpleNamespace(core=core)


@pytest.fixture(autouse=True)
def isolated_pool():
    """Each test starts with a clean pool."""
    _reset_pool()
    yield
    _reset_pool()


# ---------------------------------------------------------------------------
# Basic acquire / release
# ---------------------------------------------------------------------------


class TestAcquireLlmSlot:
    def test_context_manager_yields(self):
        from lib.llm_pool import acquire_llm_slot
        with patch("config.get_config", return_value=_cfg(workers=2)):
            with acquire_llm_slot():
                pass  # No exception = slot acquired and released

    def test_multiple_sequential_acquires(self):
        from lib.llm_pool import acquire_llm_slot
        with patch("config.get_config", return_value=_cfg(workers=1)):
            for _ in range(3):
                with acquire_llm_slot():
                    pass  # Slot released after each context — no deadlock

    def test_exception_inside_context_releases_slot(self):
        from lib.llm_pool import acquire_llm_slot
        with patch("config.get_config", return_value=_cfg(workers=1)):
            with pytest.raises(ValueError):
                with acquire_llm_slot():
                    raise ValueError("boom")
            # Slot should be released — next acquire should not block
            with acquire_llm_slot():
                pass

    def test_slot_is_released_on_normal_exit(self):
        """Pool internal counter returns to initial value after release."""
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot
        with patch("config.get_config", return_value=_cfg(workers=3)):
            with acquire_llm_slot():
                sem = m._POOL
                assert sem is not None
            # BoundedSemaphore internal counter is not public, but we can verify
            # a second acquire still works (counter was restored)
            with acquire_llm_slot():
                pass


# ---------------------------------------------------------------------------
# Concurrency gate
# ---------------------------------------------------------------------------


class TestConcurrencyGate:
    def test_pool_limits_concurrent_slots(self):
        """With workers=2, at most 2 threads hold slots simultaneously."""
        from lib.llm_pool import acquire_llm_slot
        workers = 2
        concurrent_peak = [0]
        concurrent_now = [0]
        lock = threading.Lock()
        errors = []

        def task():
            try:
                with acquire_llm_slot():
                    with lock:
                        concurrent_now[0] += 1
                        concurrent_peak[0] = max(concurrent_peak[0], concurrent_now[0])
                    time.sleep(0.02)
                    with lock:
                        concurrent_now[0] -= 1
            except Exception as e:
                errors.append(e)

        with patch("config.get_config", return_value=_cfg(workers=workers)):
            threads = [threading.Thread(target=task) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert not errors
        assert concurrent_peak[0] <= workers

    def test_all_threads_complete(self):
        """Every thread gets a slot eventually."""
        from lib.llm_pool import acquire_llm_slot
        results = []
        lock = threading.Lock()

        def task(i):
            with acquire_llm_slot():
                with lock:
                    results.append(i)

        with patch("config.get_config", return_value=_cfg(workers=2)):
            threads = [threading.Thread(target=task, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        assert len(results) == 8


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


class TestAcquireTimeout:
    def test_timeout_raises_when_all_slots_held(self):
        """If all slots are occupied, acquire with timeout raises TimeoutError."""
        from lib.llm_pool import acquire_llm_slot
        ready = threading.Event()
        release = threading.Event()

        def holder():
            with acquire_llm_slot():
                ready.set()
                release.wait(timeout=3)

        with patch("config.get_config", return_value=_cfg(workers=1)):
            t = threading.Thread(target=holder)
            t.start()
            ready.wait(timeout=2)
            try:
                with pytest.raises(TimeoutError, match="Timed out waiting for LLM worker slot"):
                    with acquire_llm_slot(timeout_seconds=0.05):
                        pass
            finally:
                release.set()
                t.join(timeout=2)

    def test_no_timeout_blocks_until_available(self):
        """acquire_llm_slot() with no timeout blocks until slot is free."""
        from lib.llm_pool import acquire_llm_slot
        acquired_order = []
        lock = threading.Lock()
        start = threading.Event()
        release_first = threading.Event()

        def first():
            with acquire_llm_slot():
                start.set()
                with lock:
                    acquired_order.append("first-in")
                release_first.wait(timeout=3)
                with lock:
                    acquired_order.append("first-out")

        def second():
            start.wait(timeout=2)
            with acquire_llm_slot():  # blocks until first releases
                with lock:
                    acquired_order.append("second-in")

        with patch("config.get_config", return_value=_cfg(workers=1)):
            t1 = threading.Thread(target=first)
            t2 = threading.Thread(target=second)
            t1.start()
            t2.start()
            # Let second thread block, then release first
            time.sleep(0.05)
            release_first.set()
            t1.join(timeout=3)
            t2.join(timeout=3)

        assert acquired_order[0] == "first-in"
        assert acquired_order[1] == "first-out"
        assert acquired_order[2] == "second-in"


# ---------------------------------------------------------------------------
# Pool initialization
# ---------------------------------------------------------------------------


class TestPoolInit:
    def test_pool_created_on_first_acquire(self):
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot
        assert m._POOL is None
        with patch("config.get_config", return_value=_cfg(workers=3)):
            with acquire_llm_slot():
                assert m._POOL is not None
                assert m._POOL_SIZE == 3

    def test_pool_size_matches_config(self):
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot
        with patch("config.get_config", return_value=_cfg(workers=5)):
            with acquire_llm_slot():
                assert m._POOL_SIZE == 5

    def test_zero_workers_falls_back_to_default_four(self):
        """llm_workers=0 is falsy — `int(0) or 4` evaluates to 4."""
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot
        cfg = SimpleNamespace(
            core=SimpleNamespace(parallel=SimpleNamespace(llm_workers=0))
        )
        with patch("config.get_config", return_value=cfg):
            with acquire_llm_slot():
                assert m._POOL_SIZE == 4


# ---------------------------------------------------------------------------
# Resize warning
# ---------------------------------------------------------------------------


class TestResizeWarning:
    def test_resize_warning_printed_to_stderr(self, capsys):
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot

        # Initialize pool with workers=2
        with patch("config.get_config", return_value=_cfg(workers=2)):
            with acquire_llm_slot():
                pass

        # Now config says workers=4 — resize should warn
        with patch("config.get_config", return_value=_cfg(workers=4)):
            with acquire_llm_slot():
                pass

        captured = capsys.readouterr()
        assert "resize" in captured.err.lower() or "ignored" in captured.err.lower()

    def test_resize_warning_only_once(self, capsys):
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot

        with patch("config.get_config", return_value=_cfg(workers=2)):
            with acquire_llm_slot():
                pass

        with patch("config.get_config", return_value=_cfg(workers=4)):
            for _ in range(3):
                with acquire_llm_slot():
                    pass

        captured = capsys.readouterr()
        # Warning message should appear only once
        assert captured.err.count("[llm_pool]") == 1

    def test_pool_size_unchanged_after_resize_attempt(self):
        """Pool size stays at original value after resize is ignored."""
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot

        with patch("config.get_config", return_value=_cfg(workers=2)):
            with acquire_llm_slot():
                pass
        assert m._POOL_SIZE == 2

        with patch("config.get_config", return_value=_cfg(workers=4)):
            with acquire_llm_slot():
                pass
        # Still 2 — resize was ignored
        assert m._POOL_SIZE == 2
