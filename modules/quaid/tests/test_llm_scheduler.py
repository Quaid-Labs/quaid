import builtins
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

    def _fn(item: int) -> int:
        calls[item] = calls.get(item, 0) + 1
        if item == 0 and calls[item] == 1:
            time.sleep(0.05)
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
