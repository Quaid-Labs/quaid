"""Tests for the root Quaid supervisor loop."""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import types

import pytest


def _mark_deleted_misc_project(tmp_path, instance_id: str) -> None:
    from core.project_registry import _load_registry, _save_registry

    registry = _load_registry(quaid_home=tmp_path)
    registry.setdefault("deleted_projects", {})[f"misc--{instance_id}"] = "2026-04-24T00:00:00+00:00"
    _save_registry(registry, quaid_home=tmp_path)


def _write_janitor_checkpoint(
    tmp_path,
    instance_id: str,
    status: str = "completed",
    *,
    started_at: str = "2099-01-01T00:00:00+00:00",
    finished_at: str = "2099-01-01T00:00:01+00:00",
) -> None:
    checkpoint = tmp_path / "instances" / instance_id / "logs" / "janitor" / "checkpoint-all.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task": "all",
        "status": status,
        "started_at": started_at,
        "heartbeat_at": finished_at,
    }
    if status == "completed":
        payload["finished_at"] = finished_at
        payload["last_completed_at"] = finished_at
        payload["terminal_status"] = status
    elif status == "failed":
        payload["finished_at"] = finished_at
        payload["last_failed_at"] = finished_at
        payload["terminal_status"] = status
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    if status in {"completed", "failed"}:
        stats = tmp_path / "instances" / instance_id / "logs" / "janitor-stats.json"
        stats.parent.mkdir(parents=True, exist_ok=True)
        stats.write_text(
            json.dumps(
                {
                    "last_run": finished_at,
                    "task": "all",
                    "dry_run": False,
                    "success": status == "completed",
                    "applied_changes": {},
                    "metrics": {},
                    "api_usage": {},
                }
            ),
            encoding="utf-8",
        )


def _write_janitor_stats(
    tmp_path,
    instance_id: str,
    *,
    last_run: str = "2099-01-01T00:00:01+00:00",
    dry_run: bool = False,
    success: bool = True,
) -> None:
    stats = tmp_path / "instances" / instance_id / "logs" / "janitor-stats.json"
    stats.parent.mkdir(parents=True, exist_ok=True)
    stats.write_text(
        json.dumps(
            {
                "last_run": last_run,
                "task": "all",
                "dry_run": dry_run,
                "success": success,
                "applied_changes": {},
                "metrics": {},
                "api_usage": {},
            }
        ),
        encoding="utf-8",
    )


def _internal_instance_for(tmp_path, adapter_id: str, *parts: str) -> str:
    from lib.instance import instance_slug_from_project_dir

    return f"{adapter_id}-{instance_slug_from_project_dir(str(tmp_path.joinpath(*parts)))}"


def _install_authoritative_docs_tick_supervisor_proxy(monkeypatch, supervisor) -> None:
    proxy = types.SimpleNamespace(
        write_supervisor_pid=lambda _token: None,
        clear_supervisor_pid_for_current_process=lambda: None,
        reap_child_processes=lambda: 0,
        worker_stale_after_seconds=lambda _interval: 30.0,
        auto_register_project_docs=lambda: (_ for _ in ()).throw(
            AssertionError("supervisor must not call auto-register directly")
        ),
        index_one_stale_registered_doc=lambda: (_ for _ in ()).throw(
            AssertionError("supervisor must not call stale-index directly")
        ),
    )
    monkeypatch.setattr(supervisor, "project_docs", proxy)


def test_supervisor_tick_starts_instance_monitors_and_janitor_workers(monkeypatch):
    from core import project_docs_supervisor as supervisor

    started_instances = []
    started_janitors = []

    class _DoneProc:
        def poll(self):
            return 0

    monkeypatch.setattr(supervisor.project_docs, "write_supervisor_pid", lambda _token: None)
    monkeypatch.setattr(supervisor.project_docs, "clear_supervisor_pid_for_current_process", lambda: None)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    monkeypatch.setattr(supervisor.project_docs, "auto_register_project_docs", lambda: None)
    monkeypatch.setattr(supervisor.project_docs, "index_one_stale_registered_doc", lambda: None)
    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha", "beta"])
    monkeypatch.setattr(supervisor, "list_projects", lambda: {})
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(supervisor, "_start_instance_monitor", lambda name: started_instances.append(name) or 100)
    monkeypatch.setattr(supervisor, "_start_janitor_worker", lambda name: started_janitors.append(name) or _DoneProc())
    monkeypatch.setattr(supervisor, "_janitor_check_interval_seconds", lambda: 0.5)
    monkeypatch.setattr(supervisor, "_interval_from_env", lambda _name, default: default)

    assert supervisor.run_supervisor(once=True, interval_seconds=0.5) == 0
    assert started_instances == ["alpha", "beta"]
    assert started_janitors == ["alpha", "beta"]


def test_supervisor_main_records_failure_marker(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(
        supervisor,
        "run_supervisor",
        lambda *, once=False: (_ for _ in ()).throw(RuntimeError("supervisor boom")),
    )
    monkeypatch.setattr(sys, "argv", ["project_docs_supervisor.py", "run", "--once"])

    with pytest.raises(RuntimeError, match="supervisor boom"):
        supervisor.main()

    failure = supervisor.project_docs.read_supervisor_failure()
    assert failure is not None
    assert failure["error_type"] == "RuntimeError"
    assert failure["error"] == "supervisor boom"


def test_supervisor_boot_mode_skips_docs_ticks_and_uses_raw_project_listing(monkeypatch):
    from core import project_docs_supervisor as supervisor

    calls: list[object] = []

    monkeypatch.setenv("QUAID_SUPERVISOR_BOOT", "1")
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setattr(supervisor.project_docs, "write_supervisor_pid", lambda _token: None)
    monkeypatch.setattr(supervisor.project_docs, "clear_supervisor_pid_for_current_process", lambda: None)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    monkeypatch.setattr(supervisor.project_docs, "worker_stale_after_seconds", lambda _interval: 30.0)
    monkeypatch.setattr(supervisor.project_docs, "project_is_registered_for_worker", lambda _project: True)
    monkeypatch.setattr(supervisor.project_docs, "reap_stale_worker", lambda _project, *, stale_after_seconds: False)
    monkeypatch.setattr(supervisor.project_docs, "start_worker", lambda _project: 123)
    monkeypatch.setattr(
        supervisor.project_docs,
        "auto_register_project_docs",
        lambda: (_ for _ in ()).throw(AssertionError("docs auto-register should be skipped")),
    )
    monkeypatch.setattr(
        supervisor.project_docs,
        "index_one_stale_registered_doc",
        lambda: (_ for _ in ()).throw(AssertionError("stale-doc index should be skipped")),
    )
    monkeypatch.setattr(supervisor, "_maintain_instance_monitors", lambda _known: None)
    monkeypatch.setattr(supervisor, "_maintain_on_demand_janitor_request", lambda *args: None)
    monkeypatch.setattr(supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        supervisor,
        "list_projects",
        lambda: (_ for _ in ()).throw(AssertionError("dispatcher boot should not reconcile projects")),
    )
    monkeypatch.setattr(supervisor, "list_projects_raw", lambda: calls.append("raw") or {"demo": {"instances": ["pytest-runner"]}})

    assert supervisor.run_supervisor(once=True, interval_seconds=0.5) == 0
    assert calls == ["raw"]


def test_supervisor_without_instance_skips_docs_ticks_and_uses_raw_project_listing(monkeypatch):
    from core import project_docs_supervisor as supervisor

    calls: list[str] = []

    monkeypatch.delenv("QUAID_SUPERVISOR_BOOT", raising=False)
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setattr(supervisor.project_docs, "write_supervisor_pid", lambda _token: None)
    monkeypatch.setattr(supervisor.project_docs, "clear_supervisor_pid_for_current_process", lambda: None)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    monkeypatch.setattr(supervisor.project_docs, "worker_stale_after_seconds", lambda _interval: 30.0)
    monkeypatch.setattr(supervisor.project_docs, "project_is_registered_for_worker", lambda _project: True)
    monkeypatch.setattr(supervisor.project_docs, "reap_stale_worker", lambda _project, *, stale_after_seconds: False)
    monkeypatch.setattr(supervisor.project_docs, "start_worker", lambda _project: 123)
    monkeypatch.setattr(
        supervisor.project_docs,
        "auto_register_project_docs",
        lambda: (_ for _ in ()).throw(AssertionError("docs auto-register should be skipped")),
    )
    monkeypatch.setattr(
        supervisor.project_docs,
        "index_one_stale_registered_doc",
        lambda: (_ for _ in ()).throw(AssertionError("stale-doc index should be skipped")),
    )
    monkeypatch.setattr(supervisor, "_maintain_instance_monitors", lambda _known: None)
    monkeypatch.setattr(supervisor, "_maintain_on_demand_janitor_request", lambda *args: None)
    monkeypatch.setattr(supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        supervisor,
        "list_projects",
        lambda: (_ for _ in ()).throw(AssertionError("dispatcher-only mode should not reconcile projects")),
    )
    monkeypatch.setattr(supervisor, "list_projects_raw", lambda: calls.append("raw") or {"demo": {"instances": ["pytest-runner"]}})

    assert supervisor.run_supervisor(once=True, interval_seconds=0.5) == 0
    assert calls == ["raw"]


def test_supervisor_docs_tick_routes_authoritative_listener_without_direct_calls(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor
    from core.runtime.events import DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT
    from core.runtime.paths import get_runtime_root
    from lib.adapter import TestAdapter, reset_adapter, set_adapter

    calls: list[str] = []

    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.delenv("QUAID_SUPERVISOR_BOOT", raising=False)
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        monkeypatch.setattr(
            "core.project_docs.auto_register_project_docs",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
        )
        monkeypatch.setattr(
            "core.project_docs.index_one_stale_registered_doc",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
        )
        monkeypatch.setattr("core.docs.updater.queued_project_log_projects", lambda project=None: [])
        monkeypatch.setattr("core.project_registry.list_projects", lambda: {"demo": {"canonical_path": str(tmp_path / "demo")}})
        monkeypatch.setattr("core.docs.updater.sync_project_visible_docs", lambda *args, **kwargs: calls.append("sync") or {"registered": 2})
        monkeypatch.setattr("core.docs.updater.index_one_stale_registered_doc", lambda *, project=None: calls.append(("index", project)) or True)
        _install_authoritative_docs_tick_supervisor_proxy(monkeypatch, supervisor)
        monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)
        monkeypatch.setattr(supervisor, "_maintain_instance_monitors", lambda _known: None)
        monkeypatch.setattr(supervisor, "_maintain_on_demand_janitor_request", lambda *args: None)
        monkeypatch.setattr(supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(supervisor, "_supervisor_projects", lambda: {})
        monkeypatch.setattr(supervisor, "_interval_from_env", lambda _name, _default: 0.5)

        assert supervisor.run_supervisor(once=True, interval_seconds=0.5) == 0

        assert calls == ["sync", ("index", None)]
        queue_path = get_runtime_root(adapter.instance_root()) / "events" / "queue.json"
        queued = json.loads(queue_path.read_text(encoding="utf-8")).get("events") or []
        event = next(item for item in queued if item.get("name") == DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT)
        assert event["status"] == "processed"
        assert event["payload"]["requested_operations"] == {"auto_register": True, "stale_index": True}
        assert event["result"]["listener_result"]["mode"] == "authoritative"
        assert event["result"]["listener_result"]["direct_result"]["registered"] == 2
        assert event["result"]["listener_result"]["direct_result"]["indexed_one"] is True
    finally:
        reset_adapter()


def test_supervisor_docs_listener_failure_logs_when_not_failhard(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor
    from core.runtime import events

    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.delenv("QUAID_SUPERVISOR_BOOT", raising=False)
    _install_authoritative_docs_tick_supervisor_proxy(monkeypatch, supervisor)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(supervisor, "_maintain_instance_monitors", lambda _known: None)
    monkeypatch.setattr(supervisor, "_maintain_on_demand_janitor_request", lambda *args: None)
    monkeypatch.setattr(supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(supervisor, "_supervisor_projects", lambda: {})
    monkeypatch.setattr(supervisor, "_interval_from_env", lambda _name, _default: 0.5)
    monkeypatch.setattr(events, "emit_broker_event", lambda *args, **kwargs: {})
    monkeypatch.setattr(events, "dispatch_broker_events", lambda *args, **kwargs: {"failed": 1})

    with caplog.at_level("WARNING"):
        assert supervisor.run_supervisor(once=True, interval_seconds=0.5) == 0

    assert "project docs maintenance listener failed" in caplog.text


def test_supervisor_docs_listener_failure_raises_when_failhard(monkeypatch):
    from core import project_docs_supervisor as supervisor
    from core.runtime import events

    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.delenv("QUAID_SUPERVISOR_BOOT", raising=False)
    _install_authoritative_docs_tick_supervisor_proxy(monkeypatch, supervisor)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(supervisor, "_maintain_instance_monitors", lambda _known: None)
    monkeypatch.setattr(supervisor, "_maintain_on_demand_janitor_request", lambda *args: None)
    monkeypatch.setattr(supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(supervisor, "_supervisor_projects", lambda: {})
    monkeypatch.setattr(supervisor, "_interval_from_env", lambda _name, _default: 0.5)
    monkeypatch.setattr(events, "emit_broker_event", lambda *args, **kwargs: {})
    monkeypatch.setattr(events, "dispatch_broker_events", lambda *args, **kwargs: {"failed": 1})

    with pytest.raises(RuntimeError, match="project docs maintenance listener failed"):
        supervisor.run_supervisor(once=True, interval_seconds=0.5)


def test_supervisor_fail_hard_helper_does_not_suppress_runtime_import_failure(monkeypatch):
    from core import project_docs_supervisor as supervisor

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "lib.fail_policy":
            raise RuntimeError("broken fail policy")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    with pytest.raises(RuntimeError, match="broken fail policy"):
        supervisor._fail_hard_enabled()


def test_supervisor_fail_hard_helper_raises_missing_policy_import(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "lib.fail_policy":
            raise ImportError("missing fail policy")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    caplog.set_level("CRITICAL", logger=supervisor.__name__)

    with pytest.raises(RuntimeError, match="fail-hard policy unavailable"):
        supervisor._fail_hard_enabled()
    assert "fail-hard policy unavailable in project docs supervisor" in caplog.text


def test_instance_misc_deleted_failure_logs_before_failhard(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(
        supervisor,
        "is_misc_project_deleted",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("registry broken")),
    )
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        with pytest.raises(RuntimeError, match="registry broken"):
            supervisor._instance_misc_project_deleted("alpha")

    assert "failed to check misc project deletion state for alpha" in caplog.text


def test_process_command_failure_logs_debug(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("ps down")))

    with caplog.at_level("DEBUG", logger=supervisor.__name__):
        assert supervisor._process_command(123) is None

    assert "ps command inspection failed for pid=123" in caplog.text
    assert "ps down" in caplog.text


def test_read_instance_daemon_pid_parse_failure_logs(monkeypatch, tmp_path, caplog):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    pid_path = tmp_path / "instances" / "alpha" / "data" / "extraction-daemon.pid"
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("not-a-pid", encoding="utf-8")

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        assert supervisor._read_instance_daemon_pid("alpha") is None

    assert "failed to read daemon pid for alpha" in caplog.text


def test_daemon_pid_supervisor_token_inspection_failure_logs(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("ps down")))

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        assert supervisor._daemon_pid_has_supervisor_token(123) is False

    assert "ps supervisor token check failed for pid=123" in caplog.text


def test_invalid_linked_project_instance_logs_debug(caplog):
    from core import project_docs_supervisor as supervisor

    with caplog.at_level("DEBUG", logger=supervisor.__name__):
        assert supervisor._valid_linked_project_instances({"instances": ["bad/instance", "alpha"]}) == ["alpha"]

    assert "skipping invalid linked project instance" in caplog.text


def test_janitor_request_malformed_instances_log(caplog):
    from core import project_docs_supervisor as supervisor

    request = {
        "started_instances": ["alpha", "bad/instance"],
        "worker_pids": {"beta": 123, "also/bad": 456, "gamma": "not-a-pid"},
    }

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        assert supervisor._janitor_request_started_instances(request) == ["alpha", "beta", "gamma"]
        assert supervisor._janitor_request_worker_pids(request) == {"beta": 123}

    assert "skipping invalid instance name" in caplog.text
    assert "skipping invalid worker_pids entry" in caplog.text


def test_maintain_instance_monitors_logs_stop_failure_when_fail_open(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor, "_live_instances_for_supervisor", lambda: (set(), {"alpha"}))
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda _name: (_ for _ in ()).throw(RuntimeError("stop broken")))
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)
    known = {"alpha": 123}

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        supervisor._maintain_instance_monitors(known)

    assert known == {}
    assert "failed to stop inactive instance monitor for alpha" in caplog.text


def test_stop_all_instance_monitors_logs_failure_when_fail_open(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha"])
    monkeypatch.setattr(supervisor, "_internal_path_derived_instances_on_disk", lambda: set())
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda _name: (_ for _ in ()).throw(RuntimeError("stop broken")))
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        supervisor.stop_all_instance_monitors()

    assert "failed to stop instance monitor for alpha" in caplog.text


def test_supervisor_worker_start_failure_raises_when_failhard(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.delenv("QUAID_SUPERVISOR_BOOT", raising=False)
    monkeypatch.setattr(supervisor.project_docs, "write_supervisor_pid", lambda _token: None)
    monkeypatch.setattr(supervisor.project_docs, "clear_supervisor_pid_for_current_process", lambda: None)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    monkeypatch.setattr(supervisor.project_docs, "worker_stale_after_seconds", lambda _interval: 30.0)
    monkeypatch.setattr(supervisor.project_docs, "project_is_registered_for_worker", lambda _project: True)
    monkeypatch.setattr(supervisor.project_docs, "reap_stale_worker", lambda _project, *, stale_after_seconds: False)
    monkeypatch.setattr(supervisor.project_docs, "start_worker", lambda _project: (_ for _ in ()).throw(RuntimeError("worker start boom")))
    monkeypatch.setattr(supervisor.project_docs, "merge_state", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(supervisor, "_maintain_instance_monitors", lambda _known: None)
    monkeypatch.setattr(supervisor, "_maintain_on_demand_janitor_request", lambda *args: None)
    monkeypatch.setattr(supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(supervisor, "_supervisor_projects", lambda: {"demo": {"instances": ["pytest-runner"]}})

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        with pytest.raises(RuntimeError, match="worker start boom"):
            supervisor.run_supervisor(once=True, interval_seconds=0.5)

    assert "project docs worker start failed for demo" in caplog.text


def test_known_project_worker_exit_raises_when_failhard(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    merged = []
    known_workers = {"demo": 1234}

    monkeypatch.setattr(supervisor.project_docs, "read_worker_pid", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_update_request", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_state", lambda _project: {})
    monkeypatch.setattr(supervisor.project_docs, "utc_now", lambda: "2026-06-18T00:00:00+00:00")
    monkeypatch.setattr(supervisor.project_docs, "merge_state", lambda project, updates: merged.append((project, updates)) or {})
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        with pytest.raises(RuntimeError, match="project docs worker for demo exited unexpectedly pid=1234"):
            supervisor._handle_known_project_worker_exit("demo", known_workers)

    assert "project docs worker for demo exited unexpectedly pid=1234" in caplog.text
    assert known_workers == {}
    assert merged == [
        (
            "demo",
            {
                "status": "error",
                "last_error": "project docs worker for demo exited unexpectedly pid=1234",
                "last_failed_at": "2026-06-18T00:00:00+00:00",
            },
        )
    ]


def test_known_project_worker_exit_allows_retry_when_failopen(monkeypatch):
    from core import project_docs_supervisor as supervisor

    merged = []
    known_workers = {"demo": 1234}

    monkeypatch.setattr(supervisor.project_docs, "read_worker_pid", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_update_request", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_state", lambda _project: {})
    monkeypatch.setattr(supervisor.project_docs, "utc_now", lambda: "2026-06-18T00:00:00+00:00")
    monkeypatch.setattr(supervisor.project_docs, "merge_state", lambda project, updates: merged.append((project, updates)) or {})
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)

    assert supervisor._handle_known_project_worker_exit("demo", known_workers) is True
    assert known_workers == {}
    assert merged[0][1]["status"] == "error"


def test_known_project_worker_exit_marks_active_request_failed_when_failopen(monkeypatch):
    from core import project_docs_supervisor as supervisor

    recorded = []
    known_workers = {"demo": 1234}
    request = {"request_id": "req-1", "status": "pending"}

    monkeypatch.setattr(supervisor.project_docs, "read_worker_pid", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_update_request", lambda _project: request)
    monkeypatch.setattr(
        supervisor.project_docs,
        "record_update_request_worker_exit",
        lambda project, req, message: recorded.append((project, req, message)),
    )
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)

    assert supervisor._handle_known_project_worker_exit("demo", known_workers) is True

    assert known_workers == {}
    assert recorded == [
        (
            "demo",
            request,
            "project docs worker for demo exited unexpectedly pid=1234",
        )
    ]


def test_known_project_worker_exit_records_active_request_without_failhard_supervisor_crash(monkeypatch):
    from core import project_docs_supervisor as supervisor

    recorded = []
    known_workers = {"demo": 1234}
    request = {"request_id": "req-1", "status": "pending"}

    monkeypatch.setattr(supervisor.project_docs, "read_worker_pid", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_update_request", lambda _project: request)
    monkeypatch.setattr(
        supervisor.project_docs,
        "record_update_request_worker_exit",
        lambda project, req, message: recorded.append((project, req, message)),
    )
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)

    assert supervisor._handle_known_project_worker_exit("demo", known_workers) is True

    assert known_workers == {}
    assert recorded == [
        (
            "demo",
            request,
            "project docs worker for demo exited unexpectedly pid=1234",
        )
    ]


def test_known_project_worker_exit_existing_failed_request_does_not_raise_failhard(monkeypatch):
    from core import project_docs_supervisor as supervisor

    known_workers = {"demo": 1234}
    request = {"request_id": "req-1", "status": "failed", "last_error": "worker failed"}

    monkeypatch.setattr(supervisor.project_docs, "read_worker_pid", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_update_request", lambda _project: request)
    monkeypatch.setattr(
        supervisor.project_docs,
        "record_update_request_worker_exit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("already terminal")),
    )
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)

    assert supervisor._handle_known_project_worker_exit("demo", known_workers) is True
    assert known_workers == {}


def test_known_project_worker_exit_existing_error_state_does_not_raise_failhard(monkeypatch):
    from core import project_docs_supervisor as supervisor

    known_workers = {"demo": 1234}
    state = {"status": "error", "last_error": "update failed"}

    monkeypatch.setattr(supervisor.project_docs, "read_worker_pid", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_update_request", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_state", lambda _project: state)
    monkeypatch.setattr(
        supervisor.project_docs,
        "merge_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("already recorded")),
    )
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)

    assert supervisor._handle_known_project_worker_exit("demo", known_workers) is True
    assert known_workers == {}


@pytest.mark.parametrize("status", ["fresh", "stopped"])
def test_known_project_worker_exit_recorded_terminal_state_does_not_raise_failhard(monkeypatch, status):
    from core import project_docs_supervisor as supervisor

    known_workers = {"demo": 1234}
    state = {"status": status}

    monkeypatch.setattr(supervisor.project_docs, "read_worker_pid", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_update_request", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_state", lambda _project: state)
    monkeypatch.setattr(
        supervisor.project_docs,
        "merge_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("terminal state already recorded")),
    )
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)

    assert supervisor._handle_known_project_worker_exit("demo", known_workers) is True
    assert known_workers == {}


def test_record_update_request_worker_exit_marks_request_failed(tmp_path, monkeypatch):
    from core import project_docs

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "alpha")
    request = {"request_id": "req-1", "status": "pending"}
    project_docs._atomic_write_json(project_docs.request_path("demo"), request)

    project_docs.record_update_request_worker_exit("demo", request, "worker exited")

    stored = project_docs.read_update_request("demo")
    state = project_docs.read_state("demo")
    assert stored["status"] == "failed"
    assert stored["last_error"] == "worker exited"
    assert "next_retry_at" not in stored
    assert state["status"] == "error"
    assert state["pending_request_id"] is None
    assert state["last_request_id"] == request["request_id"]


def test_supervisor_skips_ambiguous_multi_instance_project_without_crashing_failhard(monkeypatch, caplog):
    from core import project_docs_supervisor as supervisor

    merged: list[tuple[str, dict]] = []
    monkeypatch.setenv("QUAID_SUPERVISOR_BOOT", "1")
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setattr(supervisor.project_docs, "write_supervisor_pid", lambda _token: None)
    monkeypatch.setattr(supervisor.project_docs, "clear_supervisor_pid_for_current_process", lambda: None)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    monkeypatch.setattr(supervisor.project_docs, "worker_stale_after_seconds", lambda _interval: 30.0)
    monkeypatch.setattr(supervisor.project_docs, "project_is_registered_for_worker", lambda _project: True)
    monkeypatch.setattr(supervisor.project_docs, "reap_stale_worker", lambda _project, *, stale_after_seconds: False)
    monkeypatch.setattr(supervisor.project_docs, "read_worker_pid", lambda _project: None)
    monkeypatch.setattr(supervisor.project_docs, "read_update_request", lambda _project: None)
    monkeypatch.setattr(
        supervisor.project_docs,
        "start_worker",
        lambda _project: (_ for _ in ()).throw(AssertionError("ambiguous worker should not spawn")),
    )
    monkeypatch.setattr(supervisor.project_docs, "merge_state", lambda project, updates: merged.append((project, updates)) or {})
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(supervisor, "_maintain_instance_monitors", lambda _known: None)
    monkeypatch.setattr(supervisor, "_maintain_on_demand_janitor_request", lambda *args: None)
    monkeypatch.setattr(supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        supervisor,
        "_supervisor_projects",
        lambda: {
            "demo": {
                "instances": [
                    "claude-main",
                    "codex-private-tmp-cdx-livetest",
                ],
            }
        },
    )

    with caplog.at_level("WARNING"):
        assert supervisor.run_supervisor(once=True, interval_seconds=0.5) == 0

    assert "project docs worker start skipped for demo" in caplog.text
    assert merged
    assert merged[0][0] == "demo"
    assert merged[0][1]["status"] == "error"
    assert "valid_linked_instances=2" in merged[0][1]["last_error"]


def test_supervisor_stops_removed_instance_monitor(monkeypatch):
    from core import project_docs_supervisor as supervisor

    stopped = []
    known = {"old": 123}

    monkeypatch.setattr(supervisor, "list_instances", lambda: [])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda name: stopped.append(name) or True)

    supervisor._maintain_instance_monitors(known)

    assert stopped == ["old"]
    assert known == {}


def test_supervisor_stops_deleted_misc_instance_monitor(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    started = []
    stopped = []
    known = {"claude-code-dead": 222}

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(
        supervisor,
        "list_instances",
        lambda: ["claude-code-live", "claude-code-dead"],
    )
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(supervisor, "_start_instance_monitor", lambda name: started.append(name) or 111)
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda name: stopped.append(name) or True)
    _mark_deleted_misc_project(tmp_path, "claude-code-dead")

    supervisor._maintain_instance_monitors(known)

    assert stopped == ["claude-code-dead"]
    assert started == ["claude-code-live"]
    assert known == {"claude-code-live": 111}


def test_supervisor_preserves_configured_internal_path_derived_instance_monitor(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    started = []
    stopped = []
    internal_instance = _internal_instance_for(tmp_path, "claude-code", "plugins", "quaid")
    (tmp_path / "instances" / internal_instance).mkdir(parents=True)
    (tmp_path / "instances" / internal_instance / "config.json").write_text("{}")
    known = {}

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: ["claude-code-live"])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(supervisor, "_start_instance_monitor", lambda name: started.append(name) or 111)
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda name: stopped.append(name) or True)

    supervisor._maintain_instance_monitors(known)

    assert stopped == []
    assert started == ["claude-code-live", internal_instance]
    assert known == {"claude-code-live": 111, internal_instance: 111}


def test_supervisor_stops_stale_internal_path_derived_instance_monitor(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    stopped = []
    internal_instance = _internal_instance_for(tmp_path, "claude-code", "plugins", "quaid")
    known = {internal_instance: 222}

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: [])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda name: stopped.append(name) or True)

    supervisor._maintain_instance_monitors(known)

    assert stopped == [internal_instance]
    assert known == {}


def test_supervisor_stops_disabled_configured_internal_path_derived_instance_monitor(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    stopped = []
    internal_instance = _internal_instance_for(tmp_path, "claude-code", "plugins", "quaid")
    (tmp_path / "instances" / internal_instance).mkdir(parents=True)
    (tmp_path / "instances" / internal_instance / "config.json").write_text("{}")
    known = {internal_instance: 222}

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: [])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: True)
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda name: stopped.append(name) or True)

    supervisor._maintain_instance_monitors(known)

    assert stopped == [internal_instance]
    assert known == {}


def test_supervisor_stops_disabled_instance_monitor(monkeypatch):
    from core import project_docs_supervisor as supervisor

    started = []
    stopped = []
    known = {"claude-code-paused": 222}

    monkeypatch.setattr(
        supervisor,
        "list_instances",
        lambda: ["claude-code-live", "claude-code-paused"],
    )
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(
        supervisor.project_docs,
        "is_instance_monitor_disabled",
        lambda name: name == "claude-code-paused",
    )
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(supervisor, "_start_instance_monitor", lambda name: started.append(name) or 111)
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda name: stopped.append(name) or True)

    supervisor._maintain_instance_monitors(known)

    assert stopped == ["claude-code-paused"]
    assert started == ["claude-code-live"]
    assert known == {"claude-code-live": 111}


def test_start_instance_monitor_refuses_deleted_misc_instance(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    _mark_deleted_misc_project(tmp_path, "claude-code-dead")

    with pytest.raises(RuntimeError, match="deleted misc instance"):
        supervisor._start_instance_monitor("claude-code-dead")

    assert not (tmp_path / "instances" / "claude-code-dead").exists()


def test_start_instance_monitor_refuses_disabled_instance(monkeypatch):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: True)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)

    with pytest.raises(RuntimeError, match="disabled instance"):
        supervisor._start_instance_monitor("claude-code-paused")


def test_start_instance_monitor_strips_inherited_memory_db_overrides(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    captured = {}

    class _FakePopen:
        pid = 12345

        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("INSTANCE", "claude-code-private-tmp-cc-livetest")
    monkeypatch.setenv("SILO", str(tmp_path / "instances" / "claude-code-private-tmp-cc-livetest"))
    monkeypatch.setenv("LANE", "cc")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "claude-code")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/cc-livetest")
    monkeypatch.setenv("QUAID_SUPERVISOR_PID", "11111")
    monkeypatch.setenv("QUAID_SUPERVISOR_TOKEN", "inherited-token")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "instances" / "openclaw-main" / "data" / "memory.db"))
    monkeypatch.setenv(
        "MEMORY_ARCHIVE_DB_PATH",
        str(tmp_path / "instances" / "openclaw-main" / "data" / "memory_archive.db"),
    )
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(supervisor, "_wait_for_instance_pid", lambda _name, _pid, **_kwargs: 12345)
    monkeypatch.setattr(supervisor.subprocess, "Popen", _FakePopen)

    assert supervisor._start_instance_monitor("codex-private-tmp-cdx-livetest") == 12345

    assert captured["args"][-1] == "start"
    env = captured["env"]
    assert "MEMORY_DB_PATH" not in env
    assert "MEMORY_ARCHIVE_DB_PATH" not in env
    assert env["QUAID_HOME"] == str(tmp_path)
    assert env["QUAID_INSTANCE"] == "codex-private-tmp-cdx-livetest"
    assert env["QUAID_DAEMON"] == "1"
    assert env["QUAID_SUPERVISOR_DISABLE"] == "1"
    assert "QUAID_SUPERVISOR_PID" not in env
    assert "QUAID_SUPERVISOR_TOKEN" not in env
    assert "INSTANCE" not in env
    assert "SILO" not in env
    assert "LANE" not in env
    assert "QUAID_ADAPTER_TYPE" not in env
    assert "CLAUDE_PROJECT_DIR" not in env


def test_start_instance_monitor_adopts_matching_live_daemon_without_pidfile(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor
    from core import extraction_daemon

    adopted = []

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [8424])
    monkeypatch.setattr(supervisor, "_daemon_pid_has_supervisor_token", lambda _pid: False)
    monkeypatch.setattr(extraction_daemon, "write_pid", lambda pid: adopted.append(pid))
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not spawn")),
    )

    assert supervisor._start_instance_monitor("codex-private-tmp-cdx-livetest") == 8424
    assert adopted == [8424]


def test_start_instance_monitor_reaps_supervisor_attached_daemon(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor
    from core import extraction_daemon

    terminated = []
    captured = {}

    class _FakePopen:
        pid = 31337

        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [8424])
    monkeypatch.setattr(supervisor, "_daemon_pid_has_supervisor_token", lambda _pid: True)
    monkeypatch.setattr(extraction_daemon, "_terminate_daemon_pid", lambda pid: terminated.append(pid) or True)
    monkeypatch.setattr(supervisor, "_wait_for_instance_pid", lambda _name, _pid, **_kwargs: 9001)
    monkeypatch.setattr(supervisor.subprocess, "Popen", _FakePopen)

    assert supervisor._start_instance_monitor("codex-private-tmp-cdx-livetest") == 9001
    assert terminated == [8424]
    assert captured["args"][-1] == "start"
    assert captured["env"]["QUAID_SUPERVISOR_DISABLE"] == "1"
    assert "QUAID_SUPERVISOR_PID" not in captured["env"]


def test_stop_instance_monitor_kills_pidfile_target_and_matching_orphans(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor
    from core import extraction_daemon

    terminated = []

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: 111)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [111, 222])
    monkeypatch.setattr(
        extraction_daemon,
        "_terminate_daemon_pid",
        lambda pid, **_kwargs: terminated.append(pid) or True,
    )

    assert supervisor._stop_instance_monitor("codex-private-tmp-cdx-livetest") is True
    assert terminated == [111, 222]


def test_maintain_instance_monitors_reconciles_duplicate_live_daemons(monkeypatch):
    from core import project_docs_supervisor as supervisor
    from core import extraction_daemon

    started = []
    stopped = []
    known = {"codex-private-tmp-cdx-livetest": 8452}

    monkeypatch.setattr(supervisor, "_live_instances_for_supervisor", lambda: ({"codex-private-tmp-cdx-livetest"}, set()))
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: 8452)
    monkeypatch.setattr(supervisor, "_start_instance_monitor", lambda name: started.append(name) or 9001)
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda name: stopped.append(name) or True)
    monkeypatch.setattr(extraction_daemon, "_matching_daemon_pids", lambda **_kwargs: [8424, 8452])

    supervisor._maintain_instance_monitors(known)

    assert stopped == ["codex-private-tmp-cdx-livetest"]
    assert started == ["codex-private-tmp-cdx-livetest"]
    assert known == {"codex-private-tmp-cdx-livetest": 9001}


def test_wait_for_instance_pid_accepts_concurrent_live_monitor(monkeypatch):
    from core import project_docs_supervisor as supervisor

    terminated = []

    class _RunningProc:
        returncode = None

        def poll(self):
            return None

    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: 22222)
    monkeypatch.setattr(supervisor.project_docs, "_terminate_process", lambda proc: terminated.append(proc))

    proc = _RunningProc()
    assert supervisor._wait_for_instance_pid("alpha", 11111, timeout_seconds=5, proc=proc) == 22222
    assert terminated == [proc]


def test_wait_for_instance_pid_reports_child_exit(monkeypatch):
    from core import project_docs_supervisor as supervisor

    class _ExitedProc:
        returncode = 7

        def poll(self):
            return 7

    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)

    try:
        supervisor._wait_for_instance_pid("alpha", 11111, timeout_seconds=5, proc=_ExitedProc())
    except RuntimeError as exc:
        assert "exited before writing pid file rc=7" in str(exc)
    else:
        raise AssertionError("expected child-exit RuntimeError")


def test_start_janitor_worker_strips_inherited_memory_db_overrides(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    captured = {}

    class _FakePopen:
        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("INSTANCE", "openclaw-main")
    monkeypatch.setenv("SILO", str(tmp_path / "instances" / "openclaw-main"))
    monkeypatch.setenv("LANE_UPPER", "OC")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "openclaw")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "instances" / "openclaw-main" / "data" / "memory.db"))
    monkeypatch.setenv(
        "MEMORY_ARCHIVE_DB_PATH",
        str(tmp_path / "instances" / "openclaw-main" / "data" / "memory_archive.db"),
    )
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor.subprocess, "Popen", _FakePopen)

    supervisor._start_janitor_worker("claude-code-private-tmp-cc-livetest")

    env = captured["env"]
    assert "MEMORY_DB_PATH" not in env
    assert "MEMORY_ARCHIVE_DB_PATH" not in env
    assert env["QUAID_HOME"] == str(tmp_path)
    assert env["QUAID_INSTANCE"] == "claude-code-private-tmp-cc-livetest"
    assert "INSTANCE" not in env
    assert "SILO" not in env
    assert "LANE_UPPER" not in env
    assert "QUAID_ADAPTER_TYPE" not in env


def test_start_requested_janitor_run_starts_all_live_instances(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    starts = []

    class _RunningProc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha", "beta"])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(
        supervisor,
        "_spawn_janitor_worker",
        lambda name, *, command: starts.append((name, command)) or _RunningProc(100 + len(starts)),
    )

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    active = supervisor._maintain_on_demand_janitor_request(None, {}, {})

    assert active is not None
    assert starts == [("alpha", "run-all-once"), ("beta", "run-all-once")]
    payload = project_docs.read_janitor_request()
    assert payload["request_id"] == request["request_id"]
    assert payload["status"] == "running"
    assert payload["started_instances"] == ["alpha", "beta"]


def test_start_requested_janitor_run_includes_monitor_disabled_instances(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    starts = []

    class _RunningProc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha", "beta"])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(
        supervisor.project_docs,
        "is_instance_monitor_disabled",
        lambda name: name == "beta",
    )
    monkeypatch.setattr(
        supervisor,
        "_spawn_janitor_worker",
        lambda name, *, command: starts.append((name, command)) or _RunningProc(100 + len(starts)),
    )

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    active = supervisor._maintain_on_demand_janitor_request(None, {}, {})

    assert active is not None
    assert starts == [("alpha", "run-all-once"), ("beta", "run-all-once")]
    payload = project_docs.read_janitor_request()
    assert payload["request_id"] == request["request_id"]
    assert payload["instances"] == ["alpha", "beta"]
    assert payload["started_instances"] == ["alpha", "beta"]


def test_start_requested_janitor_run_all_includes_configured_internal_path_derived_instance(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    starts = []
    internal_instance = "codex-users-admin-quaid-plugins-quaid"
    (tmp_path / "instances" / internal_instance).mkdir(parents=True)
    (tmp_path / "instances" / internal_instance / "config.json").write_text("{}")

    class _RunningProc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha"])
    monkeypatch.setattr(supervisor, "internal_path_derived_instance_ids", lambda _home: {internal_instance})
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(
        supervisor,
        "_spawn_janitor_worker",
        lambda name, *, command: starts.append((name, command)) or _RunningProc(100 + len(starts)),
    )

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    active = supervisor._maintain_on_demand_janitor_request(None, {}, {})

    assert active is not None
    assert starts == [("alpha", "run-all-once"), (internal_instance, "run-all-once")]
    payload = project_docs.read_janitor_request()
    assert payload["request_id"] == request["request_id"]
    assert payload["instances"] == ["alpha", internal_instance]
    assert payload["started_instances"] == ["alpha", internal_instance]


def test_on_demand_janitor_request_writes_under_shared_lock(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    starts = []
    lock_depth = 0
    writes: list[str] = []

    class _DoneProc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return 0

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha"])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    _write_janitor_checkpoint(tmp_path, "alpha")
    monkeypatch.setattr(
        supervisor,
        "_spawn_janitor_worker",
        lambda name, *, command: starts.append((name, command)) or _DoneProc(321),
    )

    request = project_docs.request_janitor_run(instance="alpha", reason="pytest", requested_by="pytest")
    expected_lock_path = project_docs._spawn_lock_path("janitor-request")
    original_write = project_docs.write_janitor_request

    @contextlib.contextmanager
    def checked_lock(path):
        nonlocal lock_depth
        assert path == expected_lock_path
        lock_depth += 1
        try:
            yield
        finally:
            lock_depth -= 1

    def checked_write(payload):
        assert lock_depth > 0
        writes.append(str(payload.get("status") or ""))
        return original_write(payload)

    monkeypatch.setattr(project_docs, "_exclusive_file_lock", checked_lock)
    monkeypatch.setattr(project_docs, "write_janitor_request", checked_write)

    workers: dict[str, subprocess.Popen] = {}
    active = supervisor._maintain_on_demand_janitor_request(None, {}, workers)
    active = supervisor._maintain_on_demand_janitor_request(active, {}, workers)

    assert active is None
    assert starts == [("alpha", "run-all-once")]
    assert writes == ["running", "completed"]
    payload = project_docs.read_janitor_request()
    assert payload["request_id"] == request["request_id"]
    assert payload["status"] == "completed"


def test_requested_janitor_run_completes_single_instance(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    starts = []

    class _DoneProc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return 0

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha", "beta"])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    _write_janitor_checkpoint(tmp_path, "beta")
    monkeypatch.setattr(
        supervisor,
        "_spawn_janitor_worker",
        lambda name, *, command: starts.append((name, command)) or _DoneProc(321),
    )

    request = project_docs.request_janitor_run(instance="beta", reason="pytest", requested_by="pytest")
    workers: dict[str, subprocess.Popen] = {}
    active = supervisor._maintain_on_demand_janitor_request(None, {}, workers)
    active = supervisor._maintain_on_demand_janitor_request(active, {}, workers)

    assert active is None
    assert starts == [("beta", "run-all-once")]
    payload = project_docs.read_janitor_request()
    assert payload["request_id"] == request["request_id"]
    assert payload["status"] == "completed"
    assert payload["exit_codes"] == {"beta": 0}


def test_requested_janitor_run_accepts_configured_internal_path_derived_instance(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    starts = []
    internal_instance = "codex-users-admin-quaid-plugins-quaid"
    (tmp_path / "instances" / internal_instance).mkdir(parents=True)
    (tmp_path / "instances" / internal_instance / "config.json").write_text("{}")

    class _DoneProc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return 0

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: [])
    monkeypatch.setattr(supervisor, "internal_path_derived_instance_ids", lambda _home: {internal_instance})
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    _write_janitor_checkpoint(tmp_path, internal_instance)
    monkeypatch.setattr(
        supervisor,
        "_spawn_janitor_worker",
        lambda name, *, command: starts.append((name, command)) or _DoneProc(321),
    )

    request = project_docs.request_janitor_run(instance=internal_instance, reason="pytest", requested_by="pytest")
    workers: dict[str, subprocess.Popen] = {}
    active = supervisor._maintain_on_demand_janitor_request(None, {}, workers)
    active = supervisor._maintain_on_demand_janitor_request(active, {}, workers)

    assert active is None
    assert starts == [(internal_instance, "run-all-once")]
    payload = project_docs.read_janitor_request()
    assert payload["request_id"] == request["request_id"]
    assert payload["status"] == "completed"
    assert payload["instances"] == [internal_instance]
    assert payload["exit_codes"] == {internal_instance: 0}


def test_requested_janitor_run_rejects_stale_internal_path_derived_instance(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    starts = []
    internal_instance = "codex-users-admin-quaid-plugins-quaid"
    (tmp_path / "instances" / internal_instance).mkdir(parents=True)

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: [])
    monkeypatch.setattr(supervisor, "internal_path_derived_instance_ids", lambda _home: {internal_instance})
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(
        supervisor,
        "_spawn_janitor_worker",
        lambda name, *, command: starts.append((name, command)),
    )

    request = project_docs.request_janitor_run(instance=internal_instance, reason="pytest", requested_by="pytest")
    active = supervisor._maintain_on_demand_janitor_request(None, {}, {})

    assert active is None
    assert starts == []
    payload = project_docs.read_janitor_request()
    assert payload["request_id"] == request["request_id"]
    assert payload["status"] == "failed"
    assert payload["instances"] == []
    assert payload["errors"] == [f"instance {internal_instance} was not found"]


def test_requested_janitor_run_accumulates_exit_codes_across_polls(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    class _StepProc:
        def __init__(self, pid, codes):
            self.pid = pid
            self._codes = list(codes)

        def poll(self):
            if self._codes:
                return self._codes.pop(0)
            return 0

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)
    _write_janitor_checkpoint(tmp_path, "alpha")
    _write_janitor_checkpoint(tmp_path, "beta")

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    workers: dict[str, subprocess.Popen] = {
        "alpha": _StepProc(101, [0]),
        "beta": _StepProc(202, [None, 0]),
    }
    active = {"request_id": request["request_id"], "errors": [], "exit_codes": {}}

    active = supervisor._maintain_on_demand_janitor_request(active, {}, workers)
    assert active is not None
    assert active["exit_codes"] == {"alpha": 0}
    payload = project_docs.read_janitor_request()
    assert payload["exit_codes"] == {"alpha": 0}

    active = supervisor._maintain_on_demand_janitor_request(active, {}, workers)
    assert active is None
    payload = project_docs.read_janitor_request()
    assert payload["status"] == "completed"
    assert payload["exit_codes"] == {"alpha": 0, "beta": 0}


def test_requested_janitor_run_accepts_zero_exit_when_child_checkpoint_failed(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    class _DoneProc:
        pid = 101

        def poll(self):
            return 0

    _write_janitor_checkpoint(tmp_path, "alpha", "failed")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    workers: dict[str, subprocess.Popen] = {"alpha": _DoneProc()}
    active = {"request_id": request["request_id"], "errors": [], "exit_codes": {}}

    active = supervisor._maintain_on_demand_janitor_request(active, {}, workers)

    assert active is None
    payload = project_docs.read_janitor_request()
    assert payload["status"] == "completed"
    assert payload["exit_codes"] == {"alpha": 0}
    assert payload["errors"] == []


def test_requested_janitor_run_raises_on_worker_failure_when_failhard(monkeypatch, tmp_path, caplog):
    from core import project_docs, project_docs_supervisor as supervisor

    class _FailedProc:
        pid = 101

        def poll(self):
            return 1

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    workers: dict[str, subprocess.Popen] = {"alpha": _FailedProc()}
    active = {"request_id": request["request_id"], "errors": [], "exit_codes": {}}

    with caplog.at_level("ERROR", logger=supervisor.__name__):
        with pytest.raises(RuntimeError, match="instance alpha janitor exited rc=1"):
            supervisor._maintain_on_demand_janitor_request(active, {}, workers)

    assert "janitor request failed under failHard: instance alpha janitor exited rc=1" in caplog.text
    payload = project_docs.read_janitor_request()
    assert payload["status"] == "failed"
    assert payload["exit_codes"] == {"alpha": 1}
    assert payload["errors"] == ["instance alpha janitor exited rc=1"]


def test_requested_janitor_run_accepts_zero_exit_when_child_checkpoint_still_running(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    class _DoneProc:
        pid = 101

        def poll(self):
            return 0

    _write_janitor_checkpoint(tmp_path, "alpha", "running")

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    workers: dict[str, subprocess.Popen] = {"alpha": _DoneProc()}
    active = {"request_id": request["request_id"], "errors": [], "exit_codes": {}}

    active = supervisor._maintain_on_demand_janitor_request(active, {}, workers)

    assert active is None
    payload = project_docs.read_janitor_request()
    assert payload["status"] == "completed"
    assert payload["exit_codes"] == {"alpha": 0}
    assert payload["errors"] == []


def test_requested_janitor_run_accepts_zero_exit_when_child_checkpoint_missing(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    class _DoneProc:
        pid = 101

        def poll(self):
            return 0

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    workers: dict[str, subprocess.Popen] = {"alpha": _DoneProc()}
    active = {"request_id": request["request_id"], "errors": [], "exit_codes": {}}

    active = supervisor._maintain_on_demand_janitor_request(active, {}, workers)

    assert active is None
    payload = project_docs.read_janitor_request()
    assert payload["status"] == "completed"
    assert payload["exit_codes"] == {"alpha": 0}
    assert payload["errors"] == []


def test_requested_janitor_run_accepts_zero_exit_with_stale_dry_run_stats(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    class _DoneProc:
        pid = 101

        def poll(self):
            return 0

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    active = {
        "request_id": request["request_id"],
        "started_at": "2026-06-16T08:15:40+00:00",
        "started_instances": ["alpha"],
        "errors": [],
        "exit_codes": {},
    }
    _write_janitor_checkpoint(
        tmp_path,
        "alpha",
        "completed",
        started_at="2026-06-16T08:15:41+00:00",
        finished_at="2026-06-16T08:15:42+00:00",
    )
    _write_janitor_stats(
        tmp_path,
        "alpha",
        last_run="2026-06-16T08:15:27+00:00",
        dry_run=True,
        success=True,
    )

    active = supervisor._maintain_on_demand_janitor_request(active, {}, {"alpha": _DoneProc()})

    assert active is None
    payload = project_docs.read_janitor_request()
    assert payload["status"] == "completed"
    assert payload["exit_codes"] == {"alpha": 0}
    assert payload["errors"] == []


def test_running_janitor_request_recovers_from_persisted_dead_worker(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_pid_matches_janitor_worker", lambda _pid: False)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: False)

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    project_docs.write_janitor_request(
        {
            **request,
            "status": "running",
            "started_at": "2026-06-16T08:15:40+00:00",
            "started_instances": ["alpha"],
            "worker_pids": {"alpha": 1234},
        }
    )
    _write_janitor_checkpoint(tmp_path, "alpha", "failed")

    active = supervisor._maintain_on_demand_janitor_request(None, {}, {})

    assert active is None
    payload = project_docs.read_janitor_request()
    assert payload["status"] == "failed"
    assert payload["worker_pids"] == {}
    assert payload["errors"] == [
        "instance alpha janitor worker pid=1234 is no longer active",
        "instance alpha janitor checkpoint status=failed",
    ]


def test_running_janitor_request_tracks_live_worker_pid_after_supervisor_restart(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_pid_matches_janitor_worker", lambda pid: pid == 1234)

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    project_docs.write_janitor_request(
        {
            **request,
            "status": "running",
            "started_instances": ["alpha"],
            "worker_pids": {"alpha": 1234},
        }
    )

    active = supervisor._maintain_on_demand_janitor_request(None, {}, {})

    assert active is not None
    assert active["request_id"] == request["request_id"]
    assert active["worker_pids"] == {"alpha": 1234}
    payload = project_docs.read_janitor_request()
    assert payload["status"] == "running"


def test_janitor_checkpoint_status_raises_missing_checkpoint_when_failhard(monkeypatch, tmp_path, caplog):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)

    with caplog.at_level("WARNING", logger=supervisor.__name__):
        with pytest.raises(RuntimeError, match="instance alpha janitor checkpoint missing"):
            supervisor._janitor_checkpoint_status("alpha")

    assert "instance alpha janitor checkpoint missing" in caplog.text


def test_requested_janitor_run_writes_failed_status_before_failhard_reraise(monkeypatch, tmp_path):
    from core import project_docs, project_docs_supervisor as supervisor

    class _FailedProc:
        pid = 101

        def poll(self):
            return 1

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: 0)

    request = project_docs.request_janitor_run(reason="pytest", requested_by="pytest")
    workers: dict[str, subprocess.Popen] = {"alpha": _FailedProc()}
    active = {"request_id": request["request_id"], "errors": [], "exit_codes": {}}

    with pytest.raises(RuntimeError, match="instance alpha janitor exited rc=1"):
        supervisor._maintain_on_demand_janitor_request(active, {}, workers)

    payload = project_docs.read_janitor_request()
    assert payload["status"] == "failed"
    assert payload["exit_codes"] == {"alpha": 1}
    assert payload["errors"] == ["instance alpha janitor exited rc=1"]


def test_start_janitor_worker_refuses_disabled_instance(monkeypatch):
    from core import project_docs_supervisor as supervisor

    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: True)

    with pytest.raises(RuntimeError, match="disabled instance"):
        supervisor._start_janitor_worker("claude-code-paused")


def test_on_demand_janitor_worker_allows_disabled_instance_monitor(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    captured = {}

    class _FakePopen:
        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: True)
    monkeypatch.setattr(supervisor.subprocess, "Popen", _FakePopen)

    supervisor._spawn_janitor_worker("claude-code-paused", command="run-all-once")

    assert captured["env"]["QUAID_INSTANCE"] == "claude-code-paused"


def test_janitor_worker_throttles_per_instance(monkeypatch):
    from core import project_docs_supervisor as supervisor

    starts = []

    class _RunningProc:
        def poll(self):
            return None

    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha"])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor, "_start_janitor_worker", lambda name: starts.append(name) or _RunningProc())

    workers: dict[str, subprocess.Popen] = {}
    checks: dict[str, float] = {}
    supervisor._maintain_janitor_workers(workers, checks, now=100.0, check_interval=10.0)
    supervisor._maintain_janitor_workers(workers, checks, now=105.0, check_interval=10.0)

    assert starts == ["alpha"]
    assert "alpha" in workers


def test_janitor_worker_includes_configured_internal_path_derived_instance(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    starts = []
    internal_instance = _internal_instance_for(tmp_path, "codex", "plugins", "quaid")
    (tmp_path / "instances" / internal_instance).mkdir(parents=True)
    (tmp_path / "instances" / internal_instance / "config.json").write_text("{}")

    class _RunningProc:
        def poll(self):
            return None

    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "list_instances", lambda: [])
    monkeypatch.setattr(supervisor, "_instance_misc_project_deleted", lambda _name: False)
    monkeypatch.setattr(supervisor.project_docs, "is_instance_monitor_disabled", lambda _name: False)
    monkeypatch.setattr(supervisor, "_start_janitor_worker", lambda name: starts.append(name) or _RunningProc())

    workers: dict[str, subprocess.Popen] = {}
    checks: dict[str, float] = {}
    supervisor._maintain_janitor_workers(workers, checks, now=100.0, check_interval=10.0)

    assert starts == [internal_instance]
    assert set(workers) == {internal_instance}


def test_supervisor_stops_deleted_misc_janitor_worker(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    starts = []
    terminated = []
    reaped = []

    class _RunningProc:
        def poll(self):
            return None

    dead_proc = _RunningProc()
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(
        supervisor,
        "list_instances",
        lambda: ["claude-code-live", "claude-code-dead"],
    )
    monkeypatch.setattr(supervisor, "_start_janitor_worker", lambda name: starts.append(name) or _RunningProc())
    monkeypatch.setattr(supervisor.project_docs, "_terminate_process", lambda proc: terminated.append(proc))
    monkeypatch.setattr(supervisor.project_docs, "reap_child_processes", lambda: reaped.append(True) or 0)
    _mark_deleted_misc_project(tmp_path, "claude-code-dead")

    workers: dict[str, subprocess.Popen] = {"claude-code-dead": dead_proc}
    checks: dict[str, float] = {"claude-code-dead": 10.0}
    supervisor._maintain_janitor_workers(workers, checks, now=100.0, check_interval=0.5)

    assert terminated == [dead_proc]
    assert starts == ["claude-code-live"]
    assert "claude-code-dead" not in workers
    assert "claude-code-dead" not in checks
    assert "claude-code-live" in workers
    assert reaped


def test_supervisor_global_reaper_skips_tracked_janitor_processes():
    from core import project_docs_supervisor as supervisor

    class _Proc:
        def poll(self):
            return None

    assert supervisor._has_tracked_janitor_processes({}, {}) is False
    assert supervisor._has_tracked_janitor_processes({"alpha": _Proc()}, {}) is True
    assert supervisor._has_tracked_janitor_processes({}, {"beta": _Proc()}) is True
