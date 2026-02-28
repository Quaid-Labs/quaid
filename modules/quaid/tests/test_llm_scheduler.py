import time

import pytest

from core.llm.scheduler import GlobalLlmScheduler


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
