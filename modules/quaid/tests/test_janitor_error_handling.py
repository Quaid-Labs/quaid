import os
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.lifecycle import janitor
from datastore.memorydb.maintenance_ops import JanitorMetrics


def test_default_owner_fallback_when_fail_hard_disabled(monkeypatch):
    monkeypatch.setattr(janitor, "_cfg", SimpleNamespace())
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)
    assert janitor._default_owner_id() == "default"


def test_plugin_maintenance_slots_includes_all_plugin_surfaces(monkeypatch):
    monkeypatch.setattr(
        janitor,
        "_cfg",
        SimpleNamespace(
            plugins=SimpleNamespace(
                slots=SimpleNamespace(
                    adapter="openclaw.adapter",
                    ingest=["core.extract"],
                    datastores=["memorydb.core"],
                )
            )
        ),
    )
    slots = janitor._plugin_maintenance_slots()
    assert slots == {
        "adapter": "openclaw.adapter",
        "ingest": ["core.extract"],
        "datastores": ["memorydb.core"],
    }


def test_default_owner_raises_when_fail_hard_enabled(monkeypatch):
    monkeypatch.setattr(janitor, "_cfg", SimpleNamespace())
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    with pytest.raises(RuntimeError, match="default owner"):
        janitor._default_owner_id()


def test_queue_approval_request_invalid_json_raises_when_fail_hard_enabled(tmp_path, monkeypatch):
    bad = tmp_path / "pending-approval-requests.json"
    bad.write_text("{bad json", encoding="utf-8")
    monkeypatch.setattr(janitor, "_pending_approvals_json_path", lambda: bad)
    monkeypatch.setattr(janitor, "_pending_approvals_md_path", lambda: tmp_path / "pending-approval-requests.md")
    monkeypatch.setattr(janitor, "_append_decision_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(janitor, "_queue_delayed_notification", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="pending approval requests JSON"):
        janitor._queue_approval_request("memory", "review", "bad parse case")


def test_benchmark_gate_invalid_inputs_raise_when_fail_hard_enabled(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    metrics = JanitorMetrics()

    with pytest.raises(RuntimeError, match="review_coverage_ratio_pct"):
        janitor._benchmark_review_gate_triggered(
            {"review_coverage_ratio_pct": "not-a-number", "review_carryover": 0},
            metrics,
        )


def test_benchmark_gate_invalid_inputs_degrade_when_fail_hard_disabled(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)
    metrics = JanitorMetrics()

    out = janitor._benchmark_review_gate_triggered(
        {"review_coverage_ratio_pct": "not-a-number", "review_carryover": 0},
        metrics,
    )
    assert out is True
    assert metrics.has_errors


def test_run_tests_uses_configurable_timeout(monkeypatch):
    captured = {}

    def _fake_run(*_args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(
            returncode=0,
            stdout="Total: 1\nPassed: 1\nFailed: 0\n",
            stderr="",
        )

    monkeypatch.setenv("QUAID_JANITOR_TEST_TIMEOUT_S", "42")
    monkeypatch.setattr(janitor.subprocess, "run", _fake_run)

    metrics = JanitorMetrics()
    out = janitor.run_tests(metrics)
    assert captured["timeout"] == 42
    assert out["success"] is True


def test_append_decision_log_trims_to_configured_tail(tmp_path, monkeypatch):
    decision_path = tmp_path / "janitor" / "decision-log.jsonl"
    monkeypatch.setenv("QUAID_DECISION_LOG_MAX_LINES", "3")
    monkeypatch.setattr(janitor, "_decision_log_path", lambda: decision_path)

    for idx in range(5):
        janitor._append_decision_log("test", {"idx": idx})

    lines = decision_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    payloads = [janitor.json.loads(line) for line in lines]
    assert [p["idx"] for p in payloads] == [2, 3, 4]


def test_check_for_updates_ignores_non_object_github_payload(tmp_path, monkeypatch):
    version_file = Path(janitor.__file__).parent / "VERSION"
    original = version_file.read_text(encoding="utf-8") if version_file.exists() else None
    version_file.write_text("0.1.0", encoding="utf-8")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'["bad-payload"]'

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: _Resp())
    monkeypatch.setattr(janitor, "get_graph", lambda: object())
    monkeypatch.setattr(janitor, "get_update_check_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(janitor, "write_update_check_cache", lambda *_args, **_kwargs: None)

    try:
        out = janitor._check_for_updates()
        assert out is None
    finally:
        if original is None:
            version_file.unlink(missing_ok=True)
        else:
            version_file.write_text(original, encoding="utf-8")
