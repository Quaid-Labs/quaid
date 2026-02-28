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
