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
    cfg = SimpleNamespace(
        plugins=SimpleNamespace(
            slots=SimpleNamespace(
                adapter="openclaw.adapter",
                ingest=["core.extract"],
                datastores=["memorydb.core"],
            )
        )
    )
    monkeypatch.setattr(
        janitor,
        "_cfg",
        cfg,
    )
    monkeypatch.setattr(janitor, "get_config", lambda: cfg)
    slots = janitor._plugin_maintenance_slots()
    assert slots == {
        "adapter": "openclaw.adapter",
        "ingest": ["core.extract"],
        "datastores": ["memorydb.core"],
    }


def test_review_stage_dispatches_plugin_maintenance_surface(monkeypatch, tmp_path):
    calls = {}

    monkeypatch.setattr(janitor, "_refresh_runtime_state", lambda: None)
    monkeypatch.setattr(janitor, "_acquire_lock", lambda: True)
    monkeypatch.setattr(janitor, "_release_lock", lambda: None)
    monkeypatch.setattr(janitor, "rotate_logs", lambda: None)
    monkeypatch.setattr(janitor, "reset_token_usage", lambda: None)
    monkeypatch.setattr(janitor, "reset_token_budget", lambda: None)
    monkeypatch.setattr(janitor, "get_graph", lambda: object())
    monkeypatch.setattr(janitor, "init_janitor_metadata", lambda _graph: None)
    monkeypatch.setattr(janitor, "get_last_run_time", lambda _graph, _task: None)
    monkeypatch.setattr(janitor, "is_benchmark_mode", lambda: False)
    monkeypatch.setattr(janitor, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(janitor, "_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(janitor, "_benchmark_review_gate_triggered", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        janitor,
        "get_llm_provider",
        lambda: SimpleNamespace(get_profiles=lambda: {"deep": {"available": True}}),
    )
    monkeypatch.setattr(janitor, "run_tests", lambda _metrics: {"success": True, "passed": 0, "failed": 0, "total": 0})
    monkeypatch.setattr(janitor, "_check_for_updates", lambda: None)
    monkeypatch.setattr(janitor, "_append_decision_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(janitor, "_checkpoint_save", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(janitor, "_send_notification", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(janitor, "_queue_delayed_notification", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(janitor, "save_run_time", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    monkeypatch.setattr(
        janitor,
        "_cfg",
        SimpleNamespace(
            systems=SimpleNamespace(memory=True, journal=False, projects=False, workspace=False),
            plugins=SimpleNamespace(
                enabled=True,
                strict=True,
                config={},
                slots=SimpleNamespace(
                    adapter="openclaw.adapter",
                    ingest=["core.extract"],
                    datastores=["memorydb.core"],
                ),
            ),
            janitor=SimpleNamespace(
                apply_mode="auto",
                approval_policies={},
                test_timeout_seconds=60,
            ),
            notifications=SimpleNamespace(enabled=False, level="normal"),
            users=SimpleNamespace(default_owner="quaid"),
        ),
    )

    def _fake_collect(*, registry, slots, surface, config, plugin_config, workspace_root, strict, payload):
        calls["surface"] = surface
        calls["slots"] = slots
        calls["payload"] = dict(payload or {})
        assert registry is not None
        assert strict is True
        assert workspace_root == str(tmp_path)
        return [], [], [("memorydb.core", {"handled": True, "metrics": {"memories_reviewed": 1}})]

    monkeypatch.setattr("core.runtime.plugins.get_runtime_registry", lambda: object())
    monkeypatch.setattr("core.runtime.plugins.run_plugin_contract_surface_collect", _fake_collect)

    result = janitor.run_task_optimized("review", dry_run=True, incremental=False, resume_checkpoint=False)

    assert result["success"] is True
    assert calls["surface"] == "maintenance"
    assert calls["payload"]["stage"] == "review"
    assert calls["payload"]["subtask"] == "review"
    assert "memorydb.core" in calls["slots"]["datastores"]


def test_default_owner_raises_when_fail_hard_enabled(monkeypatch):
    cfg = SimpleNamespace()
    monkeypatch.setattr(janitor, "_cfg", cfg)
    monkeypatch.setattr(janitor, "get_config", lambda: cfg)
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
