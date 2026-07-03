import os
import json
import sys
import time
import urllib.request
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.lifecycle import janitor
from datastore.memorydb.maintenance_ops import JanitorMetrics


def _minimal_janitor_cfg(*, plugins_enabled=False, plugins_strict=True, memory=True, journal=False, timeout_minutes=120):
    return SimpleNamespace(
        systems=SimpleNamespace(memory=memory, journal=journal, projects=False, workspace=False),
        plugins=SimpleNamespace(
            enabled=plugins_enabled,
            strict=plugins_strict,
            config={},
            slots=SimpleNamespace(adapter="", ingest=[], datastores=["memorydb.core"]),
        ),
        janitor=SimpleNamespace(
            apply_mode="auto",
            approval_policies={},
            test_timeout_seconds=60,
            run_tests=False,
            contradiction=SimpleNamespace(enabled=False, min_similarity=0.7, max_similarity=0.95),
            dedup=SimpleNamespace(similarity_threshold=0.85, high_similarity_threshold=0.95),
            task_timeout_minutes=timeout_minutes,
        ),
        decay=SimpleNamespace(threshold_days=90, rate_percent=10),
        notifications=SimpleNamespace(enabled=False, level="normal"),
        users=SimpleNamespace(default_owner="quaid"),
        core=SimpleNamespace(parallel=SimpleNamespace(enabled=False)),
    )


def _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg=None, lifecycle_result=None):
    cfg = cfg or _minimal_janitor_cfg()
    lifecycle_result = lifecycle_result or janitor.RoutineResult()
    monkeypatch.setattr(janitor, "_cfg", cfg)
    monkeypatch.setattr(janitor, "_refresh_runtime_state", lambda: None)
    monkeypatch.setattr(janitor, "rotate_logs", lambda: None)
    monkeypatch.setattr(janitor, "reset_token_usage", lambda: None)
    monkeypatch.setattr(janitor, "reset_token_budget", lambda: None)
    monkeypatch.setattr(janitor, "init_janitor_metadata", lambda _graph: None)
    monkeypatch.setattr(janitor, "get_last_run_time", lambda _graph, _task: None)
    monkeypatch.setattr(janitor, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(janitor, "_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(janitor, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(janitor, "is_benchmark_mode", lambda: False)
    monkeypatch.setattr(janitor, "_ambient_instance_graph_summary", lambda: None)
    monkeypatch.setattr(janitor, "_check_for_updates", lambda: None)
    monkeypatch.setattr(janitor, "_append_decision_log", lambda *_a, **_kw: None)
    monkeypatch.setattr(janitor, "save_run_time", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "_queue_delayed_notification", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "_send_notification", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "record_health_snapshot", lambda *_a, **_kw: {"total": 0, "total_edges": 0, "avg_confidence": 0.0}, raising=False)
    monkeypatch.setattr(janitor, "list_recent_fact_texts", lambda *_a, **_kw: [], raising=False)
    monkeypatch.setattr(janitor, "count_nodes_by_status", lambda *_a, **_kw: {}, raising=False)
    monkeypatch.setattr(janitor, "graduate_approved_to_active", lambda *_a, **_kw: 0, raising=False)
    monkeypatch.setattr(janitor, "checkpoint_wal", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(
        janitor,
        "get_llm_provider",
        lambda: SimpleNamespace(get_profiles=lambda: {"deep": {"available": True}}),
    )

    class _Graph:
        def get_health_metrics(self):
            return {}

    class _Registry:
        def run(self, *_args, **_kwargs):
            return lifecycle_result

    monkeypatch.setattr(janitor, "get_graph", lambda: _Graph())
    monkeypatch.setattr(janitor, "_lifecycle_registry", lambda: _Registry())
    return cfg


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
    monkeypatch.setattr(janitor, "record_janitor_run", lambda *_args, **_kwargs: None, raising=False)
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


def test_plugin_dispatch_failure_raises_when_fail_hard_enabled_even_if_plugins_not_strict(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg(plugins_enabled=True, plugins_strict=False, memory=True)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr("core.runtime.plugins.get_runtime_registry", lambda: object())
    monkeypatch.setattr(
        "core.runtime.plugins.run_plugin_contract_surface_collect",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("plugin dispatcher broke")),
    )

    with pytest.raises(RuntimeError, match="Critical error in task review"):
        janitor._run_task_optimized_inner("review", dry_run=False, incremental=False, resume_checkpoint=False)


def test_task_timeout_zero_is_unlimited(monkeypatch):
    cfg = _minimal_janitor_cfg(timeout_minutes=0)
    assert math.isinf(janitor._task_timeout_seconds(cfg))


def test_refresh_runtime_state_preserves_zero_task_timeout(monkeypatch):
    cfg = _minimal_janitor_cfg(timeout_minutes=0)
    monkeypatch.setattr(janitor, "get_config", lambda: cfg)
    monkeypatch.setattr(janitor, "build_default_registry", lambda: object())

    janitor._refresh_runtime_state()

    assert math.isinf(janitor.MAX_EXECUTION_TIME)


def test_version_file_points_to_module_root():
    assert janitor._version_file() == Path(janitor.__file__).resolve().parents[2] / "VERSION"
    assert janitor._version_file().is_file()


def test_record_janitor_run_failure_raises_when_fail_hard_enabled(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg(memory=False, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        janitor,
        "record_janitor_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("record broke")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="record broke"):
        janitor._run_task_optimized_inner("cleanup", dry_run=False, incremental=False, resume_checkpoint=False)


def test_record_janitor_run_timestamps_honor_quaid_now(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
    cfg = _minimal_janitor_cfg(memory=False, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    calls = []
    monkeypatch.setattr(janitor, "record_janitor_run", lambda **kwargs: calls.append(kwargs), raising=False)

    janitor._run_task_optimized_inner("cleanup", dry_run=False, incremental=False, resume_checkpoint=False)

    assert calls
    assert calls[0]["started_at_iso"] == "2026-02-03T04:05:06+00:00"
    assert calls[0]["completed_at_iso"] == "2026-02-03T04:05:06+00:00"


def test_completion_event_failure_raises_when_fail_hard_enabled(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg(memory=False, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(janitor, "record_janitor_run", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(
        "core.runtime.events.emit_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("event broke")),
    )
    monkeypatch.setattr("core.runtime.events.process_events", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="event broke"):
        janitor._run_task_optimized_inner("all", dry_run=False, incremental=False, resume_checkpoint=False)


def test_completion_event_today_window_honors_quaid_now(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
    cfg = _minimal_janitor_cfg(memory=False, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "record_janitor_run", lambda *_args, **_kwargs: None, raising=False)
    seen = {}

    def _list_recent_fact_texts(_graph, *, since_iso, limit):
        seen["since_iso"] = since_iso
        return []

    monkeypatch.setattr(
        janitor,
        "list_recent_fact_texts",
        _list_recent_fact_texts,
        raising=False,
    )
    monkeypatch.setattr("core.runtime.events.emit_event", lambda **_kwargs: None)
    monkeypatch.setattr("core.runtime.events.process_events", lambda **_kwargs: None)

    janitor._run_task_optimized_inner("all", dry_run=False, incremental=False, resume_checkpoint=False)

    assert seen["since_iso"] == "2026-02-03T00:00:00+00:00"


def test_health_snapshot_failure_raises_when_fail_hard_enabled(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg(memory=False, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        janitor,
        "record_health_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("health db down")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="Critical error in task all") as exc:
        janitor._run_task_optimized_inner("all", dry_run=False, incremental=False, resume_checkpoint=False)

    assert "Health snapshot recording failed" in str(exc.value.__cause__)


def test_pre_graduate_count_failure_raises_when_fail_hard_enabled(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg(memory=True, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        janitor,
        "count_nodes_by_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("status count failed")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="Critical error in task graduate") as exc:
        janitor._run_task_optimized_inner("graduate", dry_run=False, incremental=False, resume_checkpoint=False)

    assert "Pre-graduate status count failed" in str(exc.value.__cause__)


def test_benchmark_validation_count_failure_raises_when_fail_hard_enabled(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg(memory=True, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(janitor, "is_benchmark_mode", lambda: True)
    calls = 0

    def _count_nodes_by_status(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"pending": 0, "approved": 0}
        raise RuntimeError("benchmark count failed")

    monkeypatch.setattr(janitor, "count_nodes_by_status", _count_nodes_by_status, raising=False)

    with pytest.raises(RuntimeError, match="Benchmark mode DB validation failed"):
        janitor._run_task_optimized_inner("graduate", dry_run=False, incremental=False, resume_checkpoint=False)


def test_journal_log_rotation_failure_raises_when_fail_hard_enabled(monkeypatch, tmp_path):
    from core import log_rotation

    cfg = _minimal_janitor_cfg(memory=False, journal=True)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(janitor, "get_visible_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(
        log_rotation,
        "rotate_project_logs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rotation failed")),
    )
    monkeypatch.setattr(log_rotation, "rotate_journal_logs", lambda *_args, **_kwargs: 0)

    with pytest.raises(RuntimeError, match="Critical error in task journal") as exc:
        janitor._run_task_optimized_inner("journal", dry_run=False, incremental=False, resume_checkpoint=False)

    assert "Log rotation failed" in str(exc.value.__cause__)


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


def test_queue_approval_request_honors_quaid_now(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
    json_path = tmp_path / "pending-approval-requests.json"
    md_path = tmp_path / "pending-approval-requests.md"
    monkeypatch.setattr(janitor, "_pending_approvals_json_path", lambda: json_path)
    monkeypatch.setattr(janitor, "_pending_approvals_md_path", lambda: md_path)
    monkeypatch.setattr(janitor, "_append_decision_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(janitor, "_queue_delayed_notification", lambda *_args, **_kwargs: None)

    janitor._queue_approval_request("memory", "review", "check timestamp")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["requests"][0]["created_at"] == "2026-02-03T04:05:06+00:00"


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


def test_benchmark_gate_none_coverage_does_not_trigger(monkeypatch):
    monkeypatch.setenv("QUAID_BENCHMARK_MODE", "1")
    metrics = JanitorMetrics()

    out = janitor._benchmark_review_gate_triggered(
        {"review_coverage_ratio_pct": None, "review_carryover": 0},
        metrics,
    )

    assert out is False
    assert not metrics.has_errors


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


def test_run_tests_unexpected_error_raises_when_fail_hard(monkeypatch):
    monkeypatch.setattr(janitor.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")))
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    metrics = JanitorMetrics()
    with pytest.raises(RuntimeError, match="Unit test runner failed unexpectedly") as excinfo:
        janitor.run_tests(metrics)

    assert isinstance(excinfo.value.__cause__, OSError)
    assert metrics.has_errors


def test_pid_alive_logs_unknown_probe_failure(monkeypatch):
    warnings = []
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(janitor.os, "kill", lambda *_args: (_ for _ in ()).throw(OSError("probe failed")))
    monkeypatch.setattr(janitor.janitor_logger, "warn", lambda event, **fields: warnings.append((event, fields)))

    assert janitor._pid_alive(12345) is True
    assert warnings == [("janitor_pid_probe_failed", {"pid": 12345, "error": "probe failed"})]


def test_lock_owner_summary_logs_read_failure(monkeypatch, tmp_path):
    warnings = []
    missing = tmp_path / ".janitor.lock"
    monkeypatch.setattr(janitor, "_lock_file_path", lambda: missing)
    monkeypatch.setattr(janitor.janitor_logger, "warn", lambda event, **fields: warnings.append((event, fields)))

    assert janitor._lock_owner_summary() == ""
    assert warnings
    assert warnings[0][0] == "janitor_lock_owner_summary_failed"


def test_dry_run_pending_count_failure_raises_when_fail_hard(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg(memory=True, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Critical error in task temporal") as exc:
        janitor._run_task_optimized_inner("temporal", dry_run=True, incremental=False, resume_checkpoint=False)

    assert "Dry-run pending node count failed" in str(exc.value.__cause__)


def test_checkpoint_heartbeat_updates_during_long_stage(monkeypatch):
    writes = []

    def _save_fn(*, stage="", status=None, completed=False):
        writes.append({"stage": stage, "status": status, "completed": completed})

    monkeypatch.setenv("QUAID_JANITOR_CHECKPOINT_HEARTBEAT_S", "0.1")
    stop_event, thread, errors = janitor._start_checkpoint_heartbeat(
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
    assert errors == []
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


def test_append_decision_log_honors_quaid_now(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
    decision_path = tmp_path / "janitor" / "decision-log.jsonl"
    monkeypatch.setattr(janitor, "_decision_log_path", lambda: decision_path)

    janitor._append_decision_log("test", {"idx": 1})

    payload = json.loads(decision_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["ts"] == "2026-02-03T04:05:06+00:00"


def test_janitor_now_rejects_malformed_quaid_now(monkeypatch):
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        janitor._now_iso()


def test_run_task_rejects_malformed_quaid_now_from_checkpoint_save(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg()
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")

    def _set_bad_clock(*_args, **_kwargs):
        monkeypatch.setenv("QUAID_NOW", "not-a-date")
        return janitor.RoutineResult()

    monkeypatch.setattr(janitor, "_lifecycle_registry", lambda: SimpleNamespace(run=_set_bad_clock))

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        janitor._run_task_optimized_inner(
            "review",
            dry_run=False,
            incremental=False,
            resume_checkpoint=False,
        )


def test_llm_provider_check_raises_when_fail_hard(monkeypatch, tmp_path):
    _patch_minimal_janitor_run(monkeypatch, tmp_path)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        janitor,
        "get_llm_provider",
        lambda: (_ for _ in ()).throw(RuntimeError("provider down")),
    )

    with pytest.raises(RuntimeError, match="LLM provider check failed") as excinfo:
        janitor._run_task_optimized_inner(
            "review",
            dry_run=False,
            incremental=False,
            resume_checkpoint=False,
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "provider down"


def test_checkpoint_heartbeat_failure_records_error_when_fail_hard(monkeypatch):
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    class _FakeEvent:
        def __init__(self):
            self.calls = 0
            self.stopped = False

        def wait(self, _interval):
            if self.stopped:
                return True
            self.calls += 1
            return self.calls > 1

        def set(self):
            self.stopped = True

    class _FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            self.target()

    monkeypatch.setattr(janitor.threading, "Event", _FakeEvent)
    monkeypatch.setattr(janitor.threading, "Thread", _FakeThread)

    stop_event, thread, errors = janitor._start_checkpoint_heartbeat(
        lambda **_kwargs: (_ for _ in ()).throw(OSError("heartbeat write failed")),
        lambda: "review",
        enabled=True,
    )

    assert stop_event is not None
    assert thread is not None
    assert len(errors) == 1
    assert str(errors[0]) == "Janitor checkpoint heartbeat failed"
    assert str(errors[0].__cause__) == "heartbeat write failed"


def test_run_task_raises_recorded_heartbeat_failure_when_fail_hard(monkeypatch, tmp_path):
    cfg = _minimal_janitor_cfg(memory=False, journal=False)
    _patch_minimal_janitor_run(monkeypatch, tmp_path, cfg)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    class _FakeStop:
        def set(self):
            pass

    class _FakeThread:
        def join(self, timeout=None):
            pass

    err = RuntimeError("Janitor checkpoint heartbeat failed")
    err.__cause__ = OSError("checkpoint write failed")
    monkeypatch.setattr(
        janitor,
        "_start_checkpoint_heartbeat",
        lambda *_args, **_kwargs: (_FakeStop(), _FakeThread(), [err]),
    )

    with pytest.raises(RuntimeError, match="Critical error in task all") as excinfo:
        janitor._run_task_optimized_inner(
            "all",
            dry_run=False,
            incremental=False,
            resume_checkpoint=False,
        )

    assert excinfo.value.__cause__ is err


def test_pid_alive_probe_failure_raises_when_fail_hard(monkeypatch):
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        janitor.os,
        "kill",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )

    with pytest.raises(RuntimeError, match="Janitor PID probe failed"):
        janitor._pid_alive(12345)


def test_check_for_updates_generic_failure_raises_when_fail_hard(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.1.0", encoding="utf-8")
    monkeypatch.setattr(janitor, "_version_file", lambda: version_file)
    monkeypatch.setattr(janitor, "get_graph", lambda: object())
    monkeypatch.setattr(janitor, "get_update_check_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("payload failed")),
    )

    with pytest.raises(RuntimeError, match="Update check failed") as excinfo:
        janitor._check_for_updates()

    assert str(excinfo.value.__cause__) == "payload failed"


def test_check_for_updates_ignores_non_object_github_payload(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.1.0", encoding="utf-8")
    monkeypatch.setattr(janitor, "_version_file", lambda: version_file)

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
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)

    out = janitor._check_for_updates()
    assert out is None


def test_check_for_updates_returns_newer_release(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text("0.2.15-alpha", encoding="utf-8")
    monkeypatch.setattr(janitor, "_version_file", lambda: version_file)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                b'{"tag_name":"v0.2.16-alpha","name":"M6 recall rescue + update UX",'
                b'"body":"- Improves recall fallback for threshold-empty hybrid results\\n- Adds updater command",'
                b'"html_url":"https://github.com/quaid-labs/quaid/releases/tag/v0.2.16-alpha"}'
            )

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: _Resp())
    monkeypatch.setattr(janitor, "get_graph", lambda: object())
    monkeypatch.setattr(janitor, "get_update_check_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(janitor, "write_update_check_cache", lambda *_args, **_kwargs: None)

    out = janitor._check_for_updates()
    assert out is not None
    assert out["current"] == "0.2.15-alpha"
    assert out["latest"] == "0.2.16-alpha"
    assert out.get("message") == "M6 recall rescue + update UX"
    assert "releases/tag/v0.2.16-alpha" in out["url"]


def test_cli_task_choices_include_temporal():
    assert "temporal" in janitor.JANITOR_TASK_CHOICES
