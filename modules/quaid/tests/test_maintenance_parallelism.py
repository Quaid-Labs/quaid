import time
from types import SimpleNamespace

import datastore.memorydb.maintenance_ops as ops

from datastore.memorydb.maintenance_ops import (
    _llm_parallel_workers,
    _run_llm_batches_parallel,
)


def test_llm_parallel_workers_defaults(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_cfg",
        SimpleNamespace(
            core=SimpleNamespace(
                parallel=SimpleNamespace(enabled=True, llm_workers=4, task_workers={})
            )
        ),
    )
    assert _llm_parallel_workers("review_pending") == 4


def test_llm_parallel_workers_disabled(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_cfg",
        SimpleNamespace(
            core=SimpleNamespace(
                parallel=SimpleNamespace(enabled=False, llm_workers=4, task_workers={})
            )
        ),
    )
    assert _llm_parallel_workers("review_pending") == 1


def test_llm_parallel_workers_config_overrides(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_cfg",
        SimpleNamespace(
            core=SimpleNamespace(
                parallel=SimpleNamespace(
                    enabled=True,
                    llm_workers=3,
                    task_workers={"REVIEW_PENDING": 5},
                )
            )
        ),
    )
    assert _llm_parallel_workers("review_pending") == 5
    assert _llm_parallel_workers("dedup_review") == 3


def test_run_llm_batches_parallel_preserves_order(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_cfg",
        SimpleNamespace(
            core=SimpleNamespace(
                parallel=SimpleNamespace(enabled=True, llm_workers=4, task_workers={})
            )
        ),
    )
    batches = ["a", "b", "c", "d"]

    def runner(batch_num, batch):
        # Deliberately invert completion order.
        time.sleep(0.02 * (len(batches) - batch_num))
        return {"batch_num": batch_num, "value": batch}

    out = _run_llm_batches_parallel(batches, "review_pending", runner)
    assert [o["batch_num"] for o in out] == [1, 2, 3, 4]
    assert [o["value"] for o in out] == ["a", "b", "c", "d"]
