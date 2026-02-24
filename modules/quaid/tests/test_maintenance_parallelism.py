import time

from datastore.memorydb.maintenance_ops import (
    _llm_parallel_workers,
    _run_llm_batches_parallel,
)


def test_llm_parallel_workers_defaults(monkeypatch):
    monkeypatch.delenv("QUAID_BENCHMARK_MODE", raising=False)
    monkeypatch.delenv("QUAID_JANITOR_LLM_PARALLELISM", raising=False)
    monkeypatch.delenv("QUAID_JANITOR_LLM_PARALLELISM_REVIEW_PENDING", raising=False)
    assert _llm_parallel_workers("review_pending") == 1


def test_llm_parallel_workers_benchmark_default(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    monkeypatch.delenv("QUAID_JANITOR_LLM_PARALLELISM", raising=False)
    monkeypatch.delenv("QUAID_JANITOR_LLM_PARALLELISM_REVIEW_PENDING", raising=False)
    assert _llm_parallel_workers("review_pending") == 2


def test_llm_parallel_workers_env_overrides(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    monkeypatch.setenv("QUAID_JANITOR_LLM_PARALLELISM", "3")
    monkeypatch.setenv("QUAID_JANITOR_LLM_PARALLELISM_REVIEW_PENDING", "5")
    assert _llm_parallel_workers("review_pending") == 5
    assert _llm_parallel_workers("dedup_review") == 3


def test_run_llm_batches_parallel_preserves_order(monkeypatch):
    monkeypatch.setenv("QUAID_JANITOR_LLM_PARALLELISM", "4")
    batches = ["a", "b", "c", "d"]

    def runner(batch_num, batch):
        # Deliberately invert completion order.
        time.sleep(0.02 * (len(batches) - batch_num))
        return {"batch_num": batch_num, "value": batch}

    out = _run_llm_batches_parallel(batches, "review_pending", runner)
    assert [o["batch_num"] for o in out] == [1, 2, 3, 4]
    assert [o["value"] for o in out] == ["a", "b", "c", "d"]
