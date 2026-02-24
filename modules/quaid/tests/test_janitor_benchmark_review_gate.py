import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.lifecycle.janitor import _benchmark_review_gate_triggered
from datastore.memorydb.maintenance_ops import JanitorMetrics


def test_benchmark_review_gate_triggers_on_partial_coverage(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    metrics = JanitorMetrics()
    applied = {"review_coverage_ratio_pct": 99, "review_carryover": 0}

    assert _benchmark_review_gate_triggered(applied, metrics) is True
    assert metrics.has_errors


def test_benchmark_review_gate_triggers_on_carryover(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "true")
    metrics = JanitorMetrics()
    applied = {"review_coverage_ratio_pct": 100, "review_carryover": 2}

    assert _benchmark_review_gate_triggered(applied, metrics) is True
    assert metrics.has_errors


def test_benchmark_review_gate_noop_when_clean(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    metrics = JanitorMetrics()
    applied = {"review_coverage_ratio_pct": 100, "review_carryover": 0}

    assert _benchmark_review_gate_triggered(applied, metrics) is False
    assert not metrics.has_errors
