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


def test_janitor_metrics_summary_includes_task_metrics():
    metrics = JanitorMetrics()
    metrics.start_task("review")
    metrics.add_llm_call(0.25)
    metrics.add_warning("w1")
    metrics.add_error("e1")
    metrics.end_task("review")

    summary = metrics.summary()
    task_metrics = summary.get("task_metrics", {})
    assert "review" in task_metrics
    assert task_metrics["review"]["llm_calls"] == 1
    assert task_metrics["review"]["errors"] == 1
    assert task_metrics["review"]["warnings"] == 1
