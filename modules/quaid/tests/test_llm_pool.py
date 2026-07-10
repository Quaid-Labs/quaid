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
        m._DEEP_POOL = None
        m._POOL_SIZE = 0
        m._DEEP_POOL_SIZE = 0
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

    def test_interrupt_during_global_acquire_releases_deep_slot(self, monkeypatch):
        """KeyboardInterrupt after deep reservation must not leak deep capacity."""
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot

        class FakeDeepSemaphore:
            def __init__(self):
                self.acquired = 0
                self.released = 0

            def acquire(self, *args, **kwargs):
                self.acquired += 1
                return True

            def release(self):
                self.released += 1

        class InterruptingSemaphore:
            def acquire(self, *args, **kwargs):
                raise KeyboardInterrupt("interrupted")

        deep_sem = FakeDeepSemaphore()
        monkeypatch.setattr(m, "_ensure_pool", lambda: InterruptingSemaphore())
        monkeypatch.setattr(m, "_ensure_deep_pool", lambda: deep_sem)

        with pytest.raises(KeyboardInterrupt, match="interrupted"):
            with acquire_llm_slot(pool_kind="deep"):
                pass

        assert deep_sem.acquired == 1
        assert deep_sem.released == 1


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

    def test_zero_workers_preserved_then_bounded_to_one(self):
        """Explicit llm_workers=0 should not fall back to the default worker count."""
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot
        cfg = SimpleNamespace(
            core=SimpleNamespace(parallel=SimpleNamespace(llm_workers=0))
        )
        with patch("config.get_config", return_value=cfg):
            with acquire_llm_slot():
                assert m._POOL_SIZE == 1

    def test_fast_reserved_slots_invalid_env_warns_and_falls_back_when_fail_open(self, monkeypatch, caplog):
        import lib.llm_pool as m

        monkeypatch.setenv("QUAID_LLM_FAST_RESERVED_SLOTS", "bogus")
        monkeypatch.setattr(m, "_fail_hard_enabled", lambda: False)

        with caplog.at_level("WARNING", logger="lib.llm_pool"):
            assert m._configured_fast_reserved_slots(total_slots=4) == 1

        assert "Invalid QUAID_LLM_FAST_RESERVED_SLOTS" in caplog.text
        assert "bogus" in caplog.text

    def test_fast_reserved_slots_invalid_env_raises_when_failhard(self, monkeypatch):
        import lib.llm_pool as m

        monkeypatch.setenv("QUAID_LLM_FAST_RESERVED_SLOTS", "bogus")
        monkeypatch.setattr(m, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="LLM fast reserved slot config invalid"):
            m._configured_fast_reserved_slots(total_slots=4)


class TestProcessLock:
    def test_process_lock_enabled_globally(self, monkeypatch, tmp_path):
        """All adapters share the same global cross-process lock directory."""
        import lib.llm_pool as m

        monkeypatch.delenv("QUAID_LLM_PROCESS_LOCK", raising=False)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "codex-livetest")
        cfg = SimpleNamespace(
            core=SimpleNamespace(parallel=SimpleNamespace(llm_workers=1)),
            adapter=SimpleNamespace(type="codex"),
        )
        with patch("config.get_config", return_value=cfg):
            lock_path = m._process_lock_path()
            assert lock_path == tmp_path / "shared" / "run" / "llm" / "slot-0.lock"
            assert len(m._process_lock_paths()) == 1
            from lib.llm_pool import acquire_llm_slot
            with acquire_llm_slot(timeout_seconds=0.1):
                assert lock_path.exists()

    def test_process_lock_slot_count_env_override(self, monkeypatch, tmp_path):
        import lib.llm_pool as m

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "openclaw-livetest")
        monkeypatch.setenv("QUAID_LLM_PROCESS_LOCK_SLOTS", "2")
        cfg = SimpleNamespace(
            core=SimpleNamespace(parallel=SimpleNamespace(llm_workers=1)),
            adapter=SimpleNamespace(type="openclaw"),
        )
        with patch("config.get_config", return_value=cfg):
            assert m._process_lock_paths() == [
                tmp_path / "shared" / "run" / "llm" / "slot-0.lock",
                tmp_path / "shared" / "run" / "llm" / "slot-1.lock",
            ]

    def test_process_lock_slot_count_config_failure_warns_and_falls_back_when_fail_open(
        self, monkeypatch, caplog
    ):
        import lib.llm_pool as m

        monkeypatch.delenv("QUAID_LLM_PROCESS_LOCK_SLOTS", raising=False)
        monkeypatch.setattr(m, "_fail_hard_enabled", lambda: False)

        with patch("config.get_config", side_effect=RuntimeError("config down")), \
             caplog.at_level("WARNING", logger="lib.llm_pool"):
            assert m._process_lock_slot_count() == 4

        assert "Failed reading LLM process lock slot config" in caplog.text
        assert "config down" in caplog.text

    def test_process_lock_slot_count_invalid_env_raises_when_failhard(self, monkeypatch):
        import lib.llm_pool as m

        monkeypatch.setenv("QUAID_LLM_PROCESS_LOCK_SLOTS", "bogus")
        monkeypatch.setattr(m, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="LLM process lock slot config invalid"):
            m._process_lock_slot_count()

    def test_fast_lane_keeps_reserved_slot_when_deep_is_busy(self, monkeypatch, tmp_path):
        import lib.llm_pool as m
        from lib.llm_pool import acquire_llm_slot

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_LLM_PROCESS_LOCK", "0")
        monkeypatch.setenv("QUAID_LLM_FAST_RESERVED_SLOTS", "1")
        cfg = SimpleNamespace(
            core=SimpleNamespace(parallel=SimpleNamespace(llm_workers=2)),
            adapter=SimpleNamespace(type="codex"),
        )

        with patch("config.get_config", return_value=cfg):
            with acquire_llm_slot(pool_kind="deep", timeout_seconds=0.1):
                assert m._POOL_SIZE == 2
                assert m._DEEP_POOL_SIZE == 1
                with acquire_llm_slot(pool_kind="fast", timeout_seconds=0.1):
                    pass
                with pytest.raises(TimeoutError, match="deep LLM worker slot"):
                    with acquire_llm_slot(pool_kind="deep", timeout_seconds=0.05):
                        pass

    def test_process_lock_without_explicit_timeout_warns_and_uses_default_cap(
        self, monkeypatch, tmp_path, caplog
    ):
        import fcntl

        import lib.llm_pool as m

        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_LLM_PROCESS_LOCK_SLOTS", "1")
        monkeypatch.setattr(m, "_default_process_lock_timeout_seconds", lambda: 0.12)
        monkeypatch.setattr(m, "_process_lock_warn_seconds", lambda: 0.01)
        cfg = SimpleNamespace(core=SimpleNamespace(parallel=SimpleNamespace(llm_workers=1)))
        lock_path = tmp_path / "shared" / "run" / "llm" / "slot-0.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with open(lock_path, "a") as held_fd:
            fcntl.flock(held_fd, fcntl.LOCK_EX)
            try:
                started = time.monotonic()
                with patch("config.get_config", return_value=cfg), \
                     caplog.at_level("WARNING", logger="lib.llm_pool"), \
                     pytest.raises(TimeoutError, match="cross-process LLM worker slot"):
                    with m._acquire_process_lock(None):
                        pass
                elapsed = time.monotonic() - started
            finally:
                fcntl.flock(held_fd, fcntl.LOCK_UN)

        assert elapsed < 1.0
        assert "Timed out waiting for cross-process LLM fast worker slot" in caplog.text
        assert "timeout=0.1s" in caplog.text

    def test_process_lock_default_timeout_invalid_env_raises_when_failhard(self, monkeypatch):
        import lib.llm_pool as m

        monkeypatch.setenv("QUAID_LLM_PROCESS_LOCK_TIMEOUT_SECONDS", "bad")
        monkeypatch.setattr(m, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="QUAID_LLM_PROCESS_LOCK_TIMEOUT_SECONDS config invalid"):
            m._default_process_lock_timeout_seconds()


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
