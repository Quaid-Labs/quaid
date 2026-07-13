import builtins
import threading
import time

import pytest

from core.llm.scheduler import GlobalLlmScheduler


@pytest.fixture(autouse=True)
def _disable_platform_scheduler(monkeypatch):
    import core.llm.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "get_platform_scheduler_client_for_current_instance", lambda: None)


def test_scheduler_timeout_backoff_and_slow_release():
    scheduler = GlobalLlmScheduler(max_workers=8)
    workload = "test.lifecycle"

    try:
        with pytest.raises(TimeoutError):
            scheduler.run_map(
                workload_key=workload,
                items=[1, 2, 3, 4],
                fn=lambda _x: time.sleep(0.05),
                configured_workers=8,
                requested_workers=8,
                timeout_seconds=0.01,
                timeout_retries=0,
            )

        # Backoff halves workers from 4 active to 2 for this workload.
        assert scheduler._caps[workload] == 2

        out = scheduler.run_map(
            workload_key=workload,
            items=[1, 2, 3],
            fn=lambda x: x,
            configured_workers=8,
            requested_workers=8,
            timeout_seconds=1.0,
            timeout_retries=0,
        )
        assert out == [1, 2, 3]

        # Slow release increases cap by +1 after a clean run (2 -> 3).
        assert scheduler._caps[workload] == 3
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_respects_requested_worker_cap():
    scheduler = GlobalLlmScheduler(max_workers=16)
    workload = "test.requested_cap"
    try:
        out = scheduler.run_map(
            workload_key=workload,
            items=[1, 2, 3, 4, 5],
            fn=lambda x: x,
            configured_workers=16,
            requested_workers=2,
            timeout_seconds=1.0,
            timeout_retries=0,
        )
        assert out == [1, 2, 3, 4, 5]
        assert scheduler._caps[workload] <= 16
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_retries_only_incomplete_items_after_timeout():
    scheduler = GlobalLlmScheduler(max_workers=8)
    workload = "test.retry_remaining_only"
    calls = {}
    calls_lock = threading.Lock()
    first_item_started = threading.Event()
    release_first_item = threading.Event()

    def _fn(item: int) -> int:
        with calls_lock:
            calls[item] = calls.get(item, 0) + 1
            call_number = calls[item]
        if item == 0 and call_number == 1:
            first_item_started.set()
            release_first_item.wait(timeout=1.0)
        else:
            assert first_item_started.wait(timeout=1.0)
        return item

    try:
        out = scheduler.run_map(
            workload_key=workload,
            items=[0, 1, 2],
            fn=_fn,
            configured_workers=8,
            requested_workers=2,
            timeout_seconds=0.01,
            timeout_retries=1,
        )
        assert out == [0, 1, 2]
        # Fast items should not re-run on retry; timed-out item can run again.
        assert calls[1] == 1
        assert calls[2] == 1
        assert calls[0] == 2
    finally:
        release_first_item.set()
        scheduler.shutdown(wait=False)


def test_scheduler_worker_slot_contention_does_not_backoff_workload_cap():
    scheduler = GlobalLlmScheduler(max_workers=2)
    workload = "test.queue_wait"
    acquired = [scheduler._worker_slots.acquire(timeout=0.01) for _ in range(2)]
    assert acquired == [True, True]
    try:
        with pytest.raises(TimeoutError, match="worker slot unavailable"):
            scheduler.run_map(
                workload_key=workload,
                items=[1, 2],
                fn=lambda x: x,
                configured_workers=2,
                requested_workers=2,
                timeout_seconds=0.01,
                timeout_retries=0,
            )

        # Queue wait is platform contention, not slow work for this workload.
        assert scheduler._caps[workload] == 2
    finally:
        for _ in acquired:
            scheduler._worker_slots.release()
        scheduler.shutdown(wait=False)


def test_scheduler_cancels_pending_on_worker_error():
    scheduler = GlobalLlmScheduler(max_workers=8)
    workload = "test.cancel_on_error"
    started = []

    def _fn(item: int) -> int:
        started.append(item)
        if item == 0:
            raise RuntimeError("boom")
        time.sleep(0.05)
        return item

    try:
        with pytest.raises(RuntimeError, match="boom"):
            scheduler.run_map(
                workload_key=workload,
                items=[0, 1, 2],
                fn=_fn,
                configured_workers=8,
                requested_workers=1,
                timeout_seconds=1.0,
                timeout_retries=0,
            )
        assert started == [0]
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_serial_path_times_out_instead_of_blocking():
    scheduler = GlobalLlmScheduler(max_workers=1)
    started = []
    try:
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="Serial map timed out"):
            scheduler.run_map(
                workload_key="test.serial_timeout",
                items=[1],
                fn=lambda item: started.append(item) or time.sleep(0.2),
                configured_workers=1,
                requested_workers=1,
                timeout_seconds=0.01,
                timeout_retries=0,
            )
        assert time.monotonic() - start < 0.15
        assert started == [1]
        assert scheduler._caps["test.serial_timeout"] == 1
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_backoff_is_scoped_per_workload():
    scheduler = GlobalLlmScheduler(max_workers=8)
    try:
        with pytest.raises(TimeoutError):
            scheduler.run_map(
                workload_key="test.slow_janitor",
                items=[1, 2, 3, 4],
                fn=lambda _x: time.sleep(0.05),
                configured_workers=8,
                requested_workers=8,
                timeout_seconds=0.01,
                timeout_retries=0,
            )

        out = scheduler.run_map(
            workload_key="test.fast_recall",
            items=[1, 2, 3, 4],
            fn=lambda x: x,
            configured_workers=8,
            requested_workers=8,
            timeout_seconds=1.0,
            timeout_retries=0,
        )

        assert out == [1, 2, 3, 4]
        assert scheduler._caps["test.slow_janitor"] == 2
        assert scheduler._caps["test.fast_recall"] == 8
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_platform_acquire_failure_raises_when_fail_hard(monkeypatch):
    import core.llm.scheduler as scheduler_mod

    class _FailAcquireClient:
        def acquire(self, _n):
            raise RuntimeError("acquire failed")

    scheduler = GlobalLlmScheduler(max_workers=1)
    monkeypatch.setattr(scheduler_mod, "get_platform_scheduler_client_for_current_instance", lambda: _FailAcquireClient())
    monkeypatch.setattr(scheduler_mod, "_fail_hard_enabled", lambda: True)
    try:
        with pytest.raises(RuntimeError, match="acquire failed"):
            scheduler.run_map(
                workload_key="test.acquire_fail",
                items=[1],
                fn=lambda x: x,
                configured_workers=1,
                timeout_seconds=1.0,
                timeout_retries=0,
            )
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_platform_release_failure_raises_when_fail_hard(monkeypatch):
    import core.llm.scheduler as scheduler_mod

    class _FailReleaseClient:
        def acquire(self, _n):
            return None

        def release(self, _n):
            raise RuntimeError("release failed")

    scheduler = GlobalLlmScheduler(max_workers=1)
    monkeypatch.setattr(scheduler_mod, "get_platform_scheduler_client_for_current_instance", lambda: _FailReleaseClient())
    monkeypatch.setattr(scheduler_mod, "_fail_hard_enabled", lambda: True)
    try:
        with pytest.raises(RuntimeError, match="release failed"):
            scheduler.run_map(
                workload_key="test.release_fail",
                items=[1],
                fn=lambda x: x,
                configured_workers=1,
                timeout_seconds=1.0,
                timeout_retries=0,
            )
    finally:
        scheduler.shutdown(wait=False)


def test_scheduler_invalid_worker_env_raises_when_fail_hard(monkeypatch):
    import core.llm.scheduler as scheduler_mod

    monkeypatch.setenv("QUAID_GLOBAL_LLM_MAX_WORKERS", "not-an-int")
    monkeypatch.setattr(scheduler_mod, "_fail_hard_enabled", lambda: True)

    with pytest.raises(ValueError):
        scheduler_mod._scheduler_max_workers()


def test_scheduler_fail_hard_wrapper_fails_closed_on_import_error(monkeypatch):
    import core.llm.scheduler as scheduler_mod

    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "lib.fail_policy":
            raise ImportError("missing fail policy")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert scheduler_mod._fail_hard_enabled() is True


def test_scheduler_non_positive_platform_slots_warns_when_not_fail_hard(monkeypatch, caplog):
    import core.llm.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_fail_hard_enabled", lambda: False)

    assert scheduler_mod._platform_scheduler_slots(0) == 8
    assert "Invalid platform_scheduler_slots=0" in caplog.text


def test_scheduler_non_positive_platform_slots_raises_when_fail_hard(monkeypatch):
    import core.llm.scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "_fail_hard_enabled", lambda: True)

    with pytest.raises(ValueError, match="platform_scheduler_slots must be positive"):
        scheduler_mod._platform_scheduler_slots(0)


def test_reset_global_scheduler_shutdown_does_not_hold_global_lock(monkeypatch):
    import core.llm.scheduler as scheduler_mod

    shutdown_entered = threading.Event()
    release_shutdown = threading.Event()

    class _BlockingScheduler:
        def shutdown(self, wait=False):
            assert wait is True
            shutdown_entered.set()
            assert release_shutdown.wait(timeout=2.0)

    class _FreshScheduler:
        def shutdown(self, wait=False):
            return None

    monkeypatch.setattr(scheduler_mod, "_SCHEDULER", _BlockingScheduler())
    monkeypatch.setattr(scheduler_mod, "_scheduler_max_workers", lambda: 1)
    monkeypatch.setattr(scheduler_mod, "GlobalLlmScheduler", lambda max_workers: _FreshScheduler())

    reset_thread = threading.Thread(
        target=scheduler_mod.reset_global_llm_scheduler,
        kwargs={"wait": True},
    )
    reset_thread.start()
    try:
        assert shutdown_entered.wait(timeout=1.0)

        resolved = []
        getter_thread = threading.Thread(
            target=lambda: resolved.append(scheduler_mod.get_global_llm_scheduler())
        )
        getter_thread.start()
        getter_thread.join(timeout=0.5)

        assert resolved
        assert isinstance(resolved[0], _FreshScheduler)
    finally:
        release_shutdown.set()
        reset_thread.join(timeout=1.0)

    assert not reset_thread.is_alive()
