"""Smoke tests for benchmark-mode handling of transient LLM batch issues."""

from datastore.memorydb.maintenance_ops import JanitorMetrics, _record_llm_batch_issue


def test_record_llm_batch_issue_non_benchmark_records_error(monkeypatch):
    monkeypatch.delenv("QUAID_BENCHMARK_MODE", raising=False)
    metrics = JanitorMetrics()

    _record_llm_batch_issue(metrics, "batch failed")

    assert len(metrics.errors) == 1
    assert metrics.errors[0]["error"] == "batch failed"


def test_record_llm_batch_issue_benchmark_mode_is_non_fatal(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    metrics = JanitorMetrics()

    _record_llm_batch_issue(metrics, "batch invalid JSON")

    assert metrics.errors == []
