import os
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.lifecycle import janitor
from datastore.memorydb.maintenance_ops import JanitorMetrics


def test_default_owner_fallback_when_fail_hard_disabled(monkeypatch):
    cfg = SimpleNamespace()
    monkeypatch.setattr(janitor, "_cfg", cfg)
    monkeypatch.setattr(janitor, "get_config", lambda: cfg)
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

    def _fake_collect(
        *,
        registry,
        slots,
        surface,
        config,
        plugin_config,
        workspace_root,
        strict,
        payload=None,
        skip_plugin_ids=None,
    ):
        assert registry is not None
        assert strict is True
        assert skip_plugin_ids in (None, [])
        if surface != "maintenance":
            return [], [], []
        calls["surface"] = surface
        calls["slots"] = slots
        calls["payload"] = dict(payload or {})
        assert workspace_root == str(tmp_path)
        return [], [], [("memorydb.core", {"handled": True, "metrics": {"memories_reviewed": 1}})]

    monkeypatch.setattr("core.runtime.plugins.get_runtime_registry", lambda: object())
    monkeypatch.setattr("core.runtime.plugins.run_plugin_contract_surface_collect", _fake_collect)

    result = janitor.run_task_optimized("review", dry_run=False, incremental=False, resume_checkpoint=False)

    assert result["success"] is True
    assert calls["surface"] == "maintenance"
    assert calls["payload"]["stage"] == "review"
    assert calls["payload"]["subtask"] == "review"
    assert calls["payload"]["dry_run"] is False
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


def test_checkpoint_heartbeat_updates_during_long_stage(monkeypatch):
    writes = []

    def _save_fn(*, stage="", status=None, completed=False):
        writes.append({"stage": stage, "status": status, "completed": completed})

    monkeypatch.setenv("QUAID_JANITOR_CHECKPOINT_HEARTBEAT_S", "0.1")
    stop_event, thread = janitor._start_checkpoint_heartbeat(
        _save_fn,
        lambda: "review",
        enabled=True,
    )

    try:
        time.sleep(0.25)
    finally:
        assert stop_event is not None
        stop_event.set()
        assert thread is not None
        thread.join(timeout=1.0)

    assert writes, "Expected at least one heartbeat write"
    assert all(row["stage"] == "review" for row in writes)
    assert all(row["status"] == "running" for row in writes)


def test_append_decision_log_archives_via_rotation(tmp_path, monkeypatch):
    """_append_decision_log archives old entries via rotate_log_file, not truncation.

    The decision log is append-only; rotation is token-budget-based (archiving to
    a sibling directory), not line-count-based.  QUAID_DECISION_LOG_MAX_LINES is no
    longer the controlling parameter — it is ignored since the switch to archiving.

    This test verifies:
    - All written entries appear in the live file after normal-sized writes (rotation
      does not fire for small entries that stay under the token budget).
    - All written payloads are valid JSON and contain the expected fields.
    - No lines are silently discarded.
    """
    decision_path = tmp_path / "janitor" / "decision-log.jsonl"
    monkeypatch.setattr(janitor, "_decision_log_path", lambda: decision_path)

    for idx in range(5):
        janitor._append_decision_log("test", {"idx": idx})

    lines = decision_path.read_text(encoding="utf-8").splitlines()
    # All 5 entries must be present — rotation does not discard entries
    assert len(lines) == 5
    payloads = [janitor.json.loads(line) for line in lines]
    assert [p["idx"] for p in payloads] == [0, 1, 2, 3, 4]
    for p in payloads:
        assert "ts" in p
        assert p["kind"] == "test"


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


def test_cli_task_choices_include_temporal():
    assert "temporal" in janitor.JANITOR_TASK_CHOICES
