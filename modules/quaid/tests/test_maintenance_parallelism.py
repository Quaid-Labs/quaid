import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import datastore.memorydb.maintenance_ops as ops

from datastore.memorydb.maintenance_ops import (
    JanitorMetrics,
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


def test_run_llm_batches_parallel_preserves_exception_type(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_cfg",
        SimpleNamespace(
            core=SimpleNamespace(
                parallel=SimpleNamespace(enabled=True, llm_workers=4, task_workers={})
            )
        ),
    )
    batches = ["ok", "boom"]

    class RetryableBatchError(RuntimeError):
        pass

    def runner(batch_num, batch):
        if batch == "boom":
            raise RetryableBatchError("temporary failure")
        return {"batch_num": batch_num, "value": batch}

    out = _run_llm_batches_parallel(batches, "review_pending", runner)
    assert out[0]["value"] == "ok"
    assert out[1]["batch_num"] == 2
    assert out[1]["error_type"] == "RetryableBatchError"
    assert "temporary failure" in out[1]["error"]


def test_run_llm_batches_parallel_honors_overall_timeout(monkeypatch):
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

    def runner(_batch_num, _batch):
        time.sleep(0.2)
        return {"ok": True}

    out = _run_llm_batches_parallel(
        batches,
        "review_pending",
        runner,
        overall_timeout_seconds=0.05,
    )
    timed_out = [row for row in out if row.get("error_type") == "TimeoutError"]
    assert timed_out


def test_janitor_metrics_thread_safe_counters():
    metrics = JanitorMetrics()
    metrics.start_task("parallel")

    def _tick(_):
        metrics.add_llm_call(0.01)
        metrics.add_warning("w")
        return True

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_tick, range(200)))

    metrics.end_task("parallel")
    summary = metrics.summary()
    assert summary["llm_calls"] == 200
    assert summary["warnings"] == 200
    assert summary["task_metrics"]["parallel"]["llm_calls"] == 200


def test_find_contradictions_applies_remaining_budget_to_parallel_timeout(monkeypatch):
    captured = {}

    def _fake_parallel(batches, task_name, runner, overall_timeout_seconds=None):
        captured["batches"] = batches
        captured["task_name"] = task_name
        captured["timeout"] = overall_timeout_seconds
        return []

    monkeypatch.setattr(ops, "_run_llm_batches_parallel", _fake_parallel)
    monkeypatch.setattr(ops, "MAX_EXECUTION_TIME", 30)

    metrics = JanitorMetrics()
    ops.find_contradictions_from_pairs(
        [{"text_a": "A", "text_b": "B"}],
        metrics,
        dry_run=True,
    )

    assert captured["task_name"] == "contradictions"
    assert captured["timeout"] is not None
    assert 0 < captured["timeout"] <= 30
