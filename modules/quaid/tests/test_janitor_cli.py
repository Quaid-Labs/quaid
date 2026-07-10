from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest


def _fresh_import_janitor():
    for name in (
        "core.lifecycle.janitor",
        "core.lifecycle.datastore_runtime",
        "datastore.memorydb.maintenance_ops",
    ):
        sys.modules.pop(name, None)
    return importlib.import_module("core.lifecycle.janitor")


def test_janitor_import_config_failure_honors_fail_hard(monkeypatch):
    import config
    import lib.fail_policy

    monkeypatch.setattr(config, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("bad config")))
    monkeypatch.setattr(lib.fail_policy, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to load janitor config"):
        _fresh_import_janitor()


def test_janitor_import_config_failure_warns_when_fail_open(monkeypatch, tmp_path, caplog):
    import config
    import lib.fail_policy

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(config, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("bad config")))
    monkeypatch.setattr(lib.fail_policy, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger="core.lifecycle.janitor"):
        janitor = _fresh_import_janitor()

    assert janitor._cfg is None
    assert "Failed to load janitor config: bad config" in caplog.text
    sys.modules.pop("core.lifecycle.janitor", None)
    import core.lifecycle as lifecycle_pkg

    if getattr(lifecycle_pkg, "janitor", None) is janitor:
        delattr(lifecycle_pkg, "janitor")


def test_janitor_worker_run_all_once_bypasses_schedule_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    from core import janitor_worker
    janitor = importlib.import_module("core.lifecycle.janitor")

    calls = []

    monkeypatch.setattr(
        janitor,
        "run_task_optimized",
        lambda *, task, dry_run: calls.append((task, dry_run)) or {"success": True},
    )

    assert janitor_worker.run_all_once() == 0
    assert calls == [("all", False)]


def test_janitor_worker_run_all_once_writes_terminal_markers_on_uncaught_error(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_NOW", "2026-06-16T08:15:42Z")

    from core import janitor_worker
    janitor = importlib.import_module("core.lifecycle.janitor")

    def fail_run(*, task, dry_run):
        assert (task, dry_run) == ("all", False)
        raise RuntimeError("Anthropic API HTTPError code=429")

    monkeypatch.setattr(janitor, "run_task_optimized", fail_run)
    monkeypatch.delattr(janitor, "_now_iso", raising=False)
    monkeypatch.setattr(janitor, "get_token_usage", lambda: {"api_calls": 0, "input_tokens": 0, "output_tokens": 0})
    monkeypatch.setattr(janitor, "estimate_cost", lambda: 0.0)

    with pytest.raises(RuntimeError, match="HTTPError code=429"):
        janitor_worker.run_all_once()

    logs_dir = tmp_path / "instances" / "pytest-runner" / "logs"
    log_lines = (logs_dir / "janitor.log").read_text(encoding="utf-8").splitlines()
    complete_events = [
        json.loads(line)
        for line in log_lines
        if json.loads(line).get("event") == "janitor_complete"
    ]
    assert complete_events
    assert complete_events[-1]["success"] is False
    assert complete_events[-1]["errors"] == 1
    assert "HTTPError code=429" in complete_events[-1]["error"]

    checkpoint = json.loads((logs_dir / "janitor" / "checkpoint-all.json").read_text(encoding="utf-8"))
    assert checkpoint["status"] == "failed"
    assert checkpoint["terminal_status"] == "failed"
    assert checkpoint["last_failed_at"] == "2026-06-16T08:15:42+00:00"
    assert "HTTPError code=429" in checkpoint["worker_exit_error"]

    stats = json.loads((logs_dir / "janitor-stats.json").read_text(encoding="utf-8"))
    assert stats["task"] == "all"
    assert stats["dry_run"] is False
    assert stats["success"] is False
    assert stats["last_janitor_failed_at"] == "2026-06-16T08:15:42+00:00"
    assert stats["metrics"]["errors"] == 1


def test_janitor_worker_failure_marker_reports_diagnostic_fallbacks(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("QUAID_NOW", "2026-06-16T08:15:42Z")

    from core import janitor_worker
    janitor = importlib.import_module("core.lifecycle.janitor")

    logs_dir = tmp_path / "instances" / "pytest-runner" / "logs"
    stats_path = logs_dir / "janitor-stats.json"
    stats_path.parent.mkdir(parents=True)
    stats_path.write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(
        janitor,
        "run_task_optimized",
        lambda *, task, dry_run: (_ for _ in ()).throw(RuntimeError("primary maintenance failure")),
    )
    monkeypatch.setattr(janitor, "get_token_usage", lambda: (_ for _ in ()).throw(RuntimeError("usage unavailable")))
    monkeypatch.setattr(janitor, "estimate_cost", lambda: (_ for _ in ()).throw(RuntimeError("cost unavailable")))

    with pytest.raises(RuntimeError, match="primary maintenance failure"):
        janitor_worker.run_all_once()

    err = capsys.readouterr().err
    assert "failed to collect token usage" in err
    assert "failed to estimate cost" in err
    assert "failed to read existing janitor stats" in err
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert stats["api_usage"]["calls"] == 0
    assert stats["api_usage"]["estimated_cost_usd"] == 0.0
    assert stats["last_janitor_failed_at"] == "2026-06-16T08:15:42+00:00"


def test_janitor_worker_clock_rejects_malformed_quaid_now(monkeypatch):
    from core import janitor_worker

    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        janitor_worker._worker_now_iso()


def test_write_janitor_stats_records_apply_completion_and_preserves_it(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    from core.lifecycle import janitor

    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(janitor, "_logs_dir", lambda: logs_dir)
    monkeypatch.setattr(janitor, "get_token_usage", lambda: {"api_calls": 0, "input_tokens": 0, "output_tokens": 0})
    monkeypatch.setattr(janitor, "estimate_cost", lambda: 0.0)

    apply_result = {"success": True, "applied_changes": {"memories_reviewed": 1}, "metrics": {"errors": 0}}
    stats_path = janitor._write_janitor_stats(
        task="all",
        dry_run=False,
        result=apply_result,
        completed_at="2026-05-01T01:02:03",
    )

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert stats["last_janitor_completed_at"] == "2026-05-01T01:02:03"
    assert stats["dry_run"] is False

    dry_run_result = {"success": True, "applied_changes": {}, "metrics": {"errors": 0}}
    janitor._write_janitor_stats(
        task="all",
        dry_run=True,
        result=dry_run_result,
        completed_at="2026-05-01T02:03:04",
    )

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert stats["last_run"] == "2026-05-01T02:03:04"
    assert stats["dry_run"] is True
    assert stats["last_janitor_completed_at"] == "2026-05-01T01:02:03"


def test_write_janitor_stats_fallback_honors_quaid_now(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")

    from core.lifecycle import janitor

    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(janitor, "_logs_dir", lambda: logs_dir)
    monkeypatch.setattr(janitor, "get_token_usage", lambda: {"api_calls": 0, "input_tokens": 0, "output_tokens": 0})
    monkeypatch.setattr(janitor, "estimate_cost", lambda: 0.0)

    stats_path = janitor._write_janitor_stats(
        task="cleanup",
        dry_run=True,
        result={"success": True, "applied_changes": {}, "metrics": {}},
    )

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    assert stats["last_run"] == "2026-02-03T04:05:06+00:00"


def test_atomic_write_text_cleans_temp_file_on_replace_failure(monkeypatch, tmp_path):
    from core.lifecycle import janitor

    target = tmp_path / "janitor-stats.json"
    original_replace = os.replace

    def fail_replace(src, dst):
        if Path(src).parent == tmp_path and Path(dst) == target:
            raise OSError("replace failed")
        return original_replace(src, dst)

    monkeypatch.setattr(janitor.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        janitor._atomic_write_text(target, "payload")

    assert not target.exists()
    assert not list(tmp_path.glob("tmp*"))


def test_janitor_main_routes_all_apply_through_supervisor_request(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    from core import project_docs
    from core.lifecycle import janitor

    calls = []
    monkeypatch.setattr(project_docs, "ensure_supervisor_alive", lambda: 4321)
    monkeypatch.setattr(
        project_docs,
        "request_janitor_run",
        lambda **kwargs: calls.append(("request", kwargs)) or {"request_id": "req-1"},
    )
    monkeypatch.setattr(
        project_docs,
        "wait_for_janitor_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("apply must return after queueing")),
    )
    monkeypatch.setattr(janitor, "run_task_optimized", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError()))

    assert janitor.main(["--task", "all", "--apply"]) == 0
    assert calls[0] == ("request", {"instance": None, "reason": "janitor-cli-apply", "requested_by": "janitor-cli"})
    captured = capsys.readouterr()
    assert "Queued supervisor-owned janitor request" in captured.out
    assert "Request ID: req-1" in captured.out
    assert "Request queued; poll with `quaid janitor --status`." in captured.out
    log_path = tmp_path / "logs" / "janitor.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "janitor_supervisor_request_queued" in log_text
    assert "janitor_supervisor_request_complete" not in log_text


def test_janitor_audit_log_honors_fail_hard(monkeypatch, tmp_path):
    from core.lifecycle import janitor

    log_dir = tmp_path / "not-a-dir"
    log_dir.write_text("blocks mkdir", encoding="utf-8")
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to write janitor audit log"):
        janitor._write_janitor_log_entry(log_dir, "janitor_complete")


def test_janitor_audit_log_honors_quaid_now(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")

    from core.lifecycle import janitor

    janitor._write_janitor_log_entry(tmp_path, "janitor_complete")

    line = (tmp_path / "janitor.log").read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(line)
    assert payload["ts"] == "2026-02-03T04:05:06+00:00"


def test_janitor_wal_checkpoint_raises_when_fail_hard(monkeypatch):
    from core.lifecycle import janitor

    monkeypatch.setattr(janitor, "checkpoint_wal", lambda _graph: (_ for _ in ()).throw(OSError("wal locked")))
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="WAL checkpoint failed"):
        janitor._checkpoint_wal_after_run(object(), task="all", dry_run=False)


def test_janitor_wal_checkpoint_warns_when_not_fail_hard(monkeypatch, capsys):
    from core.lifecycle import janitor

    monkeypatch.setattr(janitor, "checkpoint_wal", lambda _graph: (_ for _ in ()).throw(OSError("wal locked")))
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)

    janitor._checkpoint_wal_after_run(object(), task="all", dry_run=False)

    captured = capsys.readouterr()
    assert "WAL checkpoint failed" in captured.err


def test_janitor_scheduler_reset_raises_when_fail_hard(monkeypatch):
    from core.lifecycle import janitor
    from core.llm import scheduler

    monkeypatch.setattr(
        scheduler,
        "reset_global_llm_scheduler",
        lambda *, wait: (_ for _ in ()).throw(RuntimeError("pool down")),
    )
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to reset global LLM scheduler"):
        janitor._reset_global_llm_scheduler_after_main()


def test_janitor_scheduler_reset_warns_when_not_fail_hard(monkeypatch, capsys):
    from core.lifecycle import janitor
    from core.llm import scheduler

    monkeypatch.setattr(
        scheduler,
        "reset_global_llm_scheduler",
        lambda *, wait: (_ for _ in ()).throw(RuntimeError("pool down")),
    )
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)

    janitor._reset_global_llm_scheduler_after_main()

    captured = capsys.readouterr()
    assert "Failed to reset global LLM scheduler" in captured.err


def test_janitor_update_check_network_failure_is_informational_under_fail_hard(monkeypatch, tmp_path):
    import urllib.error
    import urllib.request

    from core.lifecycle import janitor

    version_file = tmp_path / "VERSION"
    version_file.write_text("1.0.0", encoding="utf-8")
    monkeypatch.setattr(janitor, "_version_file", lambda: version_file)
    monkeypatch.setattr(janitor, "get_repo_slug", lambda: "owner/repo")
    monkeypatch.setattr(janitor, "get_graph", lambda: object())
    monkeypatch.setattr(janitor, "get_update_check_cache", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )

    assert janitor._check_for_updates() is None


def test_janitor_update_check_task_failure_honors_fail_hard(monkeypatch, tmp_path):
    from core.lifecycle import janitor

    class _Metrics:
        def __init__(self) -> None:
            self.errors = []
            self.started = []

        def start_task(self, name: str) -> None:
            self.started.append(name)

        def add_error(self, error: str) -> None:
            self.errors.append(error)

    metrics = _Metrics()
    monkeypatch.setattr(janitor, "rotate_logs", lambda: None)
    monkeypatch.setattr(janitor, "reset_token_usage", lambda: None)
    monkeypatch.setattr(janitor, "get_graph", lambda: object())
    monkeypatch.setattr(janitor, "JanitorMetrics", lambda: metrics)
    monkeypatch.setattr(janitor, "_ambient_instance_graph_summary", lambda: None)
    monkeypatch.setattr(janitor, "init_janitor_metadata", lambda _graph: None)
    monkeypatch.setattr(janitor, "get_last_run_time", lambda _graph, _task: None)
    monkeypatch.setattr(janitor, "_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(janitor, "is_benchmark_mode", lambda: False)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        janitor,
        "_check_for_updates",
        lambda: (_ for _ in ()).throw(RuntimeError("release API failed")),
    )

    with pytest.raises(RuntimeError, match="Critical error in task update_check"):
        janitor._run_task_optimized_inner("update_check", dry_run=True)

    assert metrics.started == ["update_check"]
    assert any("Update check task failed" in error for error in metrics.errors)


def test_janitor_lock_attempt_does_not_truncate_held_lock(monkeypatch, tmp_path):
    import fcntl

    from core.lifecycle import janitor

    data_dir = tmp_path / "data"
    lock_path = data_dir / ".janitor.lock"
    data_dir.mkdir(parents=True)
    lock_text = "12345\n2026-06-13T00:00:00"
    lock_path.write_text(lock_text, encoding="utf-8")
    monkeypatch.setattr(janitor, "_data_dir", lambda: data_dir)
    janitor._lock_fd = None

    holder = open(lock_path, "a+")
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        assert janitor._acquire_lock() is False
        assert lock_path.read_text(encoding="utf-8") == lock_text
        assert janitor._lock_fd is None
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
        janitor._lock_fd = None


def test_janitor_lock_payload_honors_quaid_now(monkeypatch, tmp_path):
    from core.lifecycle import janitor

    monkeypatch.setenv("QUAID_NOW", "2026-02-03T04:05:06Z")
    data_dir = tmp_path / "data"
    monkeypatch.setattr(janitor, "_data_dir", lambda: data_dir)
    janitor._lock_fd = None

    try:
        assert janitor._acquire_lock() is True
        lines = (data_dir / ".janitor.lock").read_text(encoding="utf-8").splitlines()
        assert lines[1] == "2026-02-03T04:05:06+00:00"
    finally:
        janitor._release_lock()
        janitor._lock_fd = None


def test_janitor_lock_filesystem_failure_honors_fail_hard(monkeypatch, tmp_path):
    from core.lifecycle import janitor

    blocked_data_dir = tmp_path / "data-file"
    blocked_data_dir.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(janitor, "_data_dir", lambda: blocked_data_dir)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: True)
    janitor._lock_fd = None

    with pytest.raises(OSError):
        janitor._acquire_lock()


def test_janitor_main_routes_all_apply_without_instance_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)

    from core import project_docs
    janitor = _fresh_import_janitor()

    calls = []

    monkeypatch.setattr(janitor, "_refresh_runtime_state", lambda: (_ for _ in ()).throw(AssertionError("should not bootstrap full config")))
    monkeypatch.setattr(project_docs, "ensure_supervisor_alive", lambda: 4321)
    monkeypatch.setattr(
        project_docs,
        "request_janitor_run",
        lambda **kwargs: calls.append(("request", kwargs)) or {"request_id": "req-2"},
    )
    monkeypatch.setattr(
        project_docs,
        "wait_for_janitor_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("apply must return after queueing")),
    )

    assert janitor.main(["--task", "all", "--apply"]) == 0
    assert calls[0] == ("request", {"instance": None, "reason": "janitor-cli-apply", "requested_by": "janitor-cli"})
    assert len(calls) == 1


def test_janitor_main_all_apply_attaches_to_existing_request(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    from core import project_docs
    from core.lifecycle import janitor

    active_request = {
        "request_id": "req-active",
        "status": "running",
        "errors": [],
    }
    monkeypatch.setattr(project_docs, "ensure_supervisor_alive", lambda: 4321)
    monkeypatch.setattr(
        project_docs,
        "request_janitor_run",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Janitor request already in progress (req-active)")),
    )
    monkeypatch.setattr(project_docs, "read_janitor_request", lambda: dict(active_request))
    monkeypatch.setattr(
        project_docs,
        "wait_for_janitor_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("apply must return after attaching")),
    )
    monkeypatch.setattr(janitor, "run_task_optimized", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError()))

    assert janitor.main(["--task", "all", "--apply"]) == 0
    captured = capsys.readouterr()
    assert "already in progress" in captured.out
    assert "Attached to existing supervisor request" in captured.out
    assert "Request queued; poll with `quaid janitor --status`." in captured.out


def test_janitor_main_all_apply_reports_unusable_existing_request(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    from core import project_docs
    from core.lifecycle import janitor

    monkeypatch.setattr(project_docs, "ensure_supervisor_alive", lambda: 4321)
    monkeypatch.setattr(
        project_docs,
        "request_janitor_run",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Janitor request already in progress (req-stale)")),
    )
    monkeypatch.setattr(
        project_docs,
        "read_janitor_request",
        lambda: {"request_id": "req-stale", "status": "completed", "errors": []},
    )

    with pytest.raises(RuntimeError, match="unexpected state .*status='completed'"):
        janitor.main(["--task", "all", "--apply"])


def test_janitor_status_reports_no_supervisor_request(monkeypatch, capsys):
    from core import project_docs
    from core.lifecycle import janitor

    monkeypatch.setattr(project_docs, "read_janitor_request", lambda: None)

    assert janitor.main(["--status"]) == 0
    captured = capsys.readouterr()
    assert "No supervisor-owned janitor request found" in captured.out


def test_janitor_status_reports_running_supervisor_request(monkeypatch, capsys):
    from core import project_docs
    from core.lifecycle import janitor

    monkeypatch.setattr(
        project_docs,
        "read_janitor_request",
        lambda: {
            "request_id": "req-status",
            "status": "running",
            "scope": "all",
            "instances": ["alpha", "beta"],
            "started_instances": ["alpha"],
            "exit_codes": {"alpha": 0},
            "errors": [],
        },
    )

    assert janitor.main(["--status"]) == 0
    captured = capsys.readouterr()
    assert "Request status: running" in captured.out
    assert "request_id: req-status" in captured.out
    assert "instances: alpha, beta" in captured.out
    assert "started_instances: alpha" in captured.out
    assert "exit_codes: alpha=0" in captured.out


@pytest.mark.parametrize("status", ["pending", "completed"])
def test_janitor_status_returns_zero_for_non_failed_supervisor_request(monkeypatch, capsys, status):
    from core import project_docs
    from core.lifecycle import janitor

    monkeypatch.setattr(
        project_docs,
        "read_janitor_request",
        lambda: {
            "request_id": f"req-{status}",
            "status": status,
            "errors": [],
        },
    )

    assert janitor.main(["--status"]) == 0
    captured = capsys.readouterr()
    assert f"Request status: {status}" in captured.out
    assert f"request_id: req-{status}" in captured.out


def test_janitor_status_returns_nonzero_for_failed_supervisor_request(monkeypatch, capsys):
    from core import project_docs
    from core.lifecycle import janitor

    monkeypatch.setattr(
        project_docs,
        "read_janitor_request",
        lambda: {
            "request_id": "req-failed",
            "status": "failed",
            "errors": ["instance alpha janitor exited rc=1"],
        },
    )

    assert janitor.main(["--status"]) == 1
    captured = capsys.readouterr()
    assert "Request status: failed" in captured.out
    assert "request_id: req-failed" in captured.out
    assert "instance alpha janitor exited rc=1" in captured.err


def test_janitor_main_all_dry_run_without_instance_uses_ambient_boot_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
    monkeypatch.delenv("QUAID_SUPERVISOR_BOOT", raising=False)

    janitor = _fresh_import_janitor()
    calls = []

    def _fake_refresh():
        calls.append(("refresh", os.environ.get("QUAID_SUPERVISOR_BOOT")))
        janitor._cfg = SimpleNamespace(
            janitor=SimpleNamespace(token_budget=0, apply_mode="auto"),
            decay=SimpleNamespace(threshold_days=90, rate_percent=10),
        )
        janitor._LIFECYCLE_REGISTRY = object()

    monkeypatch.setattr(janitor, "_refresh_runtime_state", _fake_refresh)
    monkeypatch.setattr(janitor, "_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(janitor, "run_task_optimized", lambda *args, **kwargs: calls.append(("run", os.environ.get("QUAID_SUPERVISOR_BOOT"))) or {
        "success": True,
        "applied_changes": 0,
        "metrics": {},
    })
    monkeypatch.setattr(janitor, "get_token_usage", lambda: {"api_calls": 0, "input_tokens": 0, "output_tokens": 0})
    monkeypatch.setattr(janitor, "estimate_cost", lambda: 0.0)

    assert janitor.main(["--task", "all", "--dry-run"]) == 0
    refresh_calls = [value for tag, value in calls if tag == "refresh"]
    run_calls = [value for tag, value in calls if tag == "run"]
    assert refresh_calls
    assert all(value == "1" for value in refresh_calls)
    assert run_calls == ["1"]
    assert "QUAID_SUPERVISOR_BOOT" not in os.environ


def test_ambient_instance_graph_summary_aggregates_registered_instances(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
    monkeypatch.delenv("MEMORY_DB_PATH", raising=False)
    monkeypatch.delenv("MEMORY_ARCHIVE_DB_PATH", raising=False)

    def _seed_instance(name: str, *, statuses: list[str], confidences: list[float], edge_count: int) -> None:
        instance_root = tmp_path / "instances" / name
        instance_root.mkdir(parents=True, exist_ok=True)
        (instance_root / "config.json").write_text(json.dumps({}), encoding="utf-8")
        db_path = instance_root / "data" / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE nodes (id TEXT, status TEXT, confidence REAL)")
            conn.execute("CREATE TABLE edges (id TEXT)")
            for idx, (status, confidence) in enumerate(zip(statuses, confidences), start=1):
                conn.execute(
                    "INSERT INTO nodes (id, status, confidence) VALUES (?, ?, ?)",
                    (f"{name}-n{idx}", status, confidence),
                )
            for idx in range(edge_count):
                conn.execute("INSERT INTO edges (id) VALUES (?)", (f"{name}-e{idx}",))

    _seed_instance(
        "claude-code-private-tmp-cc-livetest",
        statuses=["pending", "active"],
        confidences=[1.0, 1.0],
        edge_count=2,
    )
    _seed_instance(
        "codex-private-tmp-cdx-livetest",
        statuses=["pending", "pending", "approved"],
        confidences=[0.0, 0.0, 0.0],
        edge_count=1,
    )

    janitor = _fresh_import_janitor()
    summary = janitor._ambient_instance_graph_summary()

    assert summary is not None
    assert summary["instance_count"] == 2
    assert summary["pending_nodes"] == 3
    assert summary["total_nodes"] == 5
    assert summary["total_edges"] == 3
    assert summary["pending_nodes_by_instance"] == {
        "claude-code-private-tmp-cc-livetest": 1,
        "codex-private-tmp-cdx-livetest": 2,
    }
    assert summary["errors"] == []
    assert summary["avg_confidence"] == 0.4


def test_janitor_main_rejects_instance_with_direct_only_flags(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    from core.lifecycle import janitor

    rc = janitor.main(["--task", "all", "--apply", "--instance", "alpha", "--time-budget", "1"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "--instance requires the supervisor-owned path" in captured.err
