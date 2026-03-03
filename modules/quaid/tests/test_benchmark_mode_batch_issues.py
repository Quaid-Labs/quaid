"""Smoke tests for transient LLM batch issue handling."""

from datastore.memorydb.maintenance_ops import JanitorMetrics, _record_llm_batch_issue


def test_record_llm_batch_issue_records_error(monkeypatch):
    monkeypatch.delenv("QUAID_BENCHMARK_MODE", raising=False)
    metrics = JanitorMetrics()

    _record_llm_batch_issue(metrics, "batch failed")

    assert len(metrics.errors) == 1
    assert metrics.errors[0]["error"] == "batch failed"


def test_record_llm_batch_issue_is_fatal_even_with_benchmark_flag(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    metrics = JanitorMetrics()

    _record_llm_batch_issue(metrics, "batch invalid JSON")

    assert len(metrics.errors) == 1
    assert metrics.errors[0]["error"] == "batch invalid JSON"
