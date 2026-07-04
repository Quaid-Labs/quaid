"""Tests for supervisor-owned project docs update state."""

from __future__ import annotations

import builtins
import contextlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lib.adapter import TestAdapter, reset_adapter, set_adapter


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "tool.py").write_text("print('v1')\n", encoding="utf-8")
    from core.project_registry import create_project

    with patch("core.project_registry._sync_docs_registry_project"):
        entry = create_project("demo", description="Demo", source_root=str(src))
    yield tmp_path, src, entry
    reset_adapter()


def test_fail_hard_enabled_fails_closed_on_import_error(monkeypatch, caplog):
    from core import project_docs

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "lib.fail_policy":
            raise ImportError("missing fail policy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    caplog.set_level(logging.CRITICAL)

    assert project_docs._fail_hard_enabled() is True
    assert "fail-hard policy unavailable in project docs" in caplog.text


def test_fail_hard_enabled_propagates_policy_runtime_errors(monkeypatch):
    from core import project_docs

    monkeypatch.setitem(
        sys.modules,
        "lib.fail_policy",
        SimpleNamespace(is_fail_hard_enabled=lambda: (_ for _ in ()).throw(RuntimeError("policy bug"))),
    )

    with pytest.raises(RuntimeError, match="policy bug"):
        project_docs._fail_hard_enabled()


def test_project_docs_utc_now_honors_quaid_now(monkeypatch):
    from core import project_docs

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07")

    assert project_docs.utc_now() == "2026-03-11T05:06:07+00:00"
    assert project_docs._now_epoch() == pytest.approx(datetime(2026, 3, 11, 5, 6, 7, tzinfo=timezone.utc).timestamp())


def test_project_docs_utc_now_rejects_malformed_quaid_now(monkeypatch):
    from core import project_docs

    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        project_docs.utc_now()


def test_request_update_writes_hidden_state(project_env):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    assert request["project"] == "demo"
    request_file = tmp_path / "data" / "project-docs" / "requests" / "demo.json"
    state_file = tmp_path / "data" / "project-docs" / "state" / "demo.json"
    assert json.loads(request_file.read_text())["request_id"] == request["request_id"]
    state = json.loads(state_file.read_text())
    assert state["status"] == "queued"
    assert state["pending_request_id"] == request["request_id"]


def test_request_update_records_runtime_context(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "codex")

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    assert request["requested_instance"] == "codex-private-tmp-cdx-livetest"
    assert request["requested_adapter_type"] == "codex"


def test_validate_project_name_rejects_path_and_glob_names(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    assert project_docs.validate_project_name("Demo_Project") == "demo_project"
    assert project_docs.validate_project_name("Man\u0303ana-App") == "mañana-app"
    assert project_docs.validate_project_name("研究-資料") == "研究-資料"
    for raw in ("../../escape", "*.json", "demo/name", "demo\\name", "demo.name", "demo[abc]"):
        with pytest.raises(ValueError, match="Invalid project name"):
            project_docs.validate_project_name(raw)


def test_request_update_does_not_demote_active_update_state(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    project_docs.write_state(
        "demo",
        {
            "status": "updating",
            "last_started_at": project_docs.utc_now(),
            "phase": "update_docs",
        },
    )

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")
    state = project_docs.read_state("demo")

    assert state["status"] == "updating"
    assert state["phase"] == "update_docs"
    assert state["pending_request_id"] == request["request_id"]
    assert state["force_requested_at"] == request["requested_at"]


def test_clear_update_request_rechecks_under_state_lock(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")
    replacement = dict(request)
    replacement["request_id"] = "replacement-request"
    replacement["requested_at"] = project_docs.utc_now()
    lock_paths: list[Path] = []
    real_lock = project_docs._exclusive_file_lock

    @contextlib.contextmanager
    def swapping_state_lock(path):
        lock_paths.append(path)
        with real_lock(path):
            project_docs._atomic_write_json(project_docs.request_path("demo"), replacement)
            yield

    monkeypatch.setattr(project_docs, "_exclusive_file_lock", swapping_state_lock)

    project_docs.clear_update_request("demo", request_id=request["request_id"])

    assert lock_paths == [project_docs.state_lock_path("demo")]
    assert project_docs.read_update_request("demo")["request_id"] == "replacement-request"


def test_clear_update_request_honors_failhard_on_unlink_failure(project_env, monkeypatch, caplog):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")
    target = project_docs.request_path("demo")
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == target:
            raise OSError("unlink denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    monkeypatch.setattr(project_docs, "_fail_hard_enabled", lambda: True)
    caplog.set_level(logging.WARNING)

    with pytest.raises(OSError, match="unlink denied"):
        project_docs.clear_update_request("demo", request_id=request["request_id"])

    assert "Failed clearing project-docs update request for demo" in caplog.text


def test_start_worker_env_uses_pending_request_runtime_context(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "codex")
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
    project_docs.request_update("demo", reason="manual-test", requested_by="pytest")
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "openclaw")

    captured = {}

    class _FakePopen:
        pid = 4242

        def __init__(self, args, **kwargs):
            captured["args"] = list(args)
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setattr(project_docs.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(project_docs, "_wait_for_pid", lambda *_args, **_kwargs: 4242)

    assert project_docs.start_worker("demo") == 4242

    assert captured["args"][-2:] == ["run", "demo"]
    env = captured["env"]
    assert env["QUAID_HOME"] == str(tmp_path)
    assert env["QUAID_INSTANCE"] == "codex-private-tmp-cdx-livetest"
    assert env["QUAID_ADAPTER_TYPE"] == "codex"
    assert env["QUAID_NOW"] == "2026-03-11T05:06:07Z"
    assert "MEMORY_DB_PATH" not in env


def test_start_worker_env_uses_single_linked_instance_without_request(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
    captured = {}

    class _FakePopen:
        pid = 4243

        def __init__(self, _args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setattr(project_docs.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(project_docs, "_wait_for_pid", lambda *_args, **_kwargs: 4243)

    assert project_docs.start_worker("demo") == 4243

    env = captured["env"]
    assert env["QUAID_HOME"] == str(tmp_path)
    assert env["QUAID_INSTANCE"] == "pytest-runner"
    assert "QUAID_ADAPTER_TYPE" not in env


def test_start_worker_refuses_multi_instance_without_request(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from core.project_registry import update_project

    with patch("core.project_registry._sync_docs_registry_project"):
        update_project(
            "demo",
            instances=["codex-private-tmp-cdx-livetest", "openclaw-main"],
        )
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "openclaw")
    monkeypatch.setattr(
        project_docs.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("worker should not spawn")),
    )

    with pytest.raises(RuntimeError, match="cannot resolve QUAID_INSTANCE"):
        project_docs.start_worker("demo")


def test_start_worker_refuses_unlinked_project_without_request(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from core.project_registry import update_project

    with patch("core.project_registry._sync_docs_registry_project"):
        update_project("demo", instances=[])
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
    monkeypatch.setattr(
        project_docs.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("worker should not spawn")),
    )

    with pytest.raises(RuntimeError, match="valid_linked_instances=0"):
        project_docs.start_worker("demo")


def test_get_project_entry_uses_raw_registry_without_instance_env(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)

    with patch("core.project_registry.get_project", side_effect=AssertionError("reconciled get_project should not be used")):
        entry = project_docs.get_project_entry("demo")

    assert entry["canonical_path"].endswith("/projects/demo")


def test_project_runtime_context_uses_request_instance(monkeypatch):
    from core import project_docs

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)

    with project_docs._project_runtime_context(
        {"instances": []},
        request={"requested_instance": "claude-code-private-tmp-cc-livetest"},
    ):
        assert os.environ.get("QUAID_INSTANCE") == "claude-code-private-tmp-cc-livetest"
        assert os.environ.get("QUAID_ADAPTER_TYPE") == "claude-code"

    assert "QUAID_INSTANCE" not in os.environ
    assert "QUAID_ADAPTER_TYPE" not in os.environ


def test_matching_supervisor_pids_merges_partial_ps_outputs(monkeypatch, tmp_path):
    from core import project_docs

    calls = []
    home = tmp_path.resolve()

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout = stdout
            self.returncode = returncode

    def fake_run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            # Darwin can accept the Linux/procps form while returning only the
            # caller tty session. Continue to BSD forms instead of accepting it.
            return Result(stdout=f"11 /bin/zsh QUAID_HOME={home}\n")
        if len(calls) == 2:
            return Result(
                stdout=(
                    "202 /usr/bin/python3 /tmp/core/project_docs_supervisor.py run "
                    f"QUAID_HOME={home}\n"
                )
            )
        return Result(stdout="", returncode=0)

    monkeypatch.setattr(project_docs.subprocess, "run", fake_run)
    monkeypatch.setattr(project_docs, "_pid_alive", lambda _pid: True)

    assert project_docs._matching_supervisor_pids(quaid_home=home) == [202]
    assert calls[0] == ["ps", "eww", "-eo", "pid=,command="]
    assert calls[1] == ["ps", "eww", "-ax", "-o", "pid=", "-o", "command="]


def test_project_runtime_context_uses_single_linked_instance(monkeypatch):
    from core import project_docs

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)

    with project_docs._project_runtime_context({"instances": ["codex-private-tmp-cdx-livetest"]}):
        assert os.environ.get("QUAID_INSTANCE") == "codex-private-tmp-cdx-livetest"
        assert os.environ.get("QUAID_ADAPTER_TYPE") == "codex"

    assert "QUAID_INSTANCE" not in os.environ
    assert "QUAID_ADAPTER_TYPE" not in os.environ


def test_project_runtime_context_overrides_ambient_instance(monkeypatch):
    from core import project_docs

    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "openclaw")

    with project_docs._project_runtime_context({"instances": ["codex-private-tmp-cdx-livetest"]}):
        assert os.environ.get("QUAID_INSTANCE") == "codex-private-tmp-cdx-livetest"
        assert os.environ.get("QUAID_ADAPTER_TYPE") == "codex"

    assert os.environ.get("QUAID_INSTANCE") == "openclaw-main"
    assert os.environ.get("QUAID_ADAPTER_TYPE") == "openclaw"


def test_project_runtime_context_clears_cross_instance_db_overrides(monkeypatch, tmp_path):
    from core import project_docs

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "openclaw")
    foreign_db = tmp_path / "instances" / "openclaw-main" / "data" / "memory.db"
    foreign_archive = tmp_path / "instances" / "openclaw-main" / "data" / "memory_archive.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(foreign_db))
    monkeypatch.setenv("MEMORY_ARCHIVE_DB_PATH", str(foreign_archive))

    with project_docs._project_runtime_context({"instances": ["codex-private-tmp-cdx-livetest"]}):
        assert os.environ.get("QUAID_INSTANCE") == "codex-private-tmp-cdx-livetest"
        assert "MEMORY_DB_PATH" not in os.environ
        assert "MEMORY_ARCHIVE_DB_PATH" not in os.environ

    assert os.environ.get("MEMORY_DB_PATH") == str(foreign_db)
    assert os.environ.get("MEMORY_ARCHIVE_DB_PATH") == str(foreign_archive)


def test_project_docs_update_notice_uses_request_runtime_context(monkeypatch):
    from core import project_docs

    reset_adapter()
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)
    captured = {}

    def fake_queue(message, **kwargs):
        captured["message"] = message
        captured["kwargs"] = kwargs
        captured["instance"] = os.environ.get("QUAID_INSTANCE")
        captured["adapter"] = os.environ.get("QUAID_ADAPTER_TYPE")
        return True

    with patch("lib.runtime_context.queue_deferred_notice", fake_queue):
        project_docs._notify_project_docs_update(
            "demo",
            {
                "metrics": {"docs_updated": 1},
                "registry_sync": {"registered": 2, "unregistered": 0},
                "indexed_docs": 3,
            },
            entry={"instances": ["claude-code-private-tmp-cc-livetest"]},
            request={
                "requested_instance": "codex-private-tmp-cdx-livetest",
                "requested_adapter_type": "codex",
            },
        )

    assert captured["instance"] == "codex-private-tmp-cdx-livetest"
    assert captured["adapter"] == "codex"
    assert captured["kwargs"]["kind"] == "project_doc_update"
    assert "docs_updated=1" in captured["message"]
    assert "QUAID_INSTANCE" not in os.environ
    assert "QUAID_ADAPTER_TYPE" not in os.environ


def test_project_docs_worker_refreshes_runtime_config_before_update(monkeypatch):
    from core import project_docs_worker

    calls = []
    monkeypatch.setattr(project_docs_worker.project_docs, "validate_project_name", lambda project: project)
    monkeypatch.setattr(project_docs_worker, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(project_docs_worker.project_docs, "read_update_request", lambda _project: {"request_id": "req-1"})
    monkeypatch.setattr(project_docs_worker.project_docs, "update_request_ready_for_worker", lambda request: True)
    monkeypatch.setattr(
        project_docs_worker.project_docs,
        "project_has_pending_update",
        lambda _project: (_ for _ in ()).throw(AssertionError("ready request should not need stale probe")),
    )
    monkeypatch.setattr(
        project_docs_worker.project_docs,
        "write_worker_heartbeat",
        lambda project, payload=None: calls.append(("heartbeat", project, (payload or {}).get("status"))),
    )
    monkeypatch.setattr(
        project_docs_worker,
        "_start_update_heartbeat",
        lambda _project, _interval: (
            type("Stop", (), {"set": lambda self: calls.append(("heartbeat_stop",))})(),
            type("Thread", (), {"join": lambda self, timeout=None: calls.append(("heartbeat_join", timeout))})(),
        ),
    )
    monkeypatch.setattr(
        project_docs_worker,
        "_refresh_runtime_config_for_update",
        lambda project: calls.append(("refresh", project)),
    )

    def fake_execute(project, *, request=None):
        calls.append(("execute", project, request))
        return {"project": project, "status": "fresh"}

    monkeypatch.setattr(project_docs_worker.project_docs, "execute_update_once", fake_execute)
    monkeypatch.setattr(project_docs_worker.project_docs, "clear_worker_pid_for_current_process", lambda project: calls.append(("clear_pid", project)))

    assert project_docs_worker.run_worker("demo", once=True, interval_seconds=0.5) == 0
    assert ("refresh", "demo") in calls
    assert ("execute", "demo", {"request_id": "req-1"}) in calls
    assert calls.index(("refresh", "demo")) < calls.index(("execute", "demo", {"request_id": "req-1"}))


def test_project_docs_worker_refresh_resets_model_caches(monkeypatch):
    from core import project_docs_worker

    calls = []
    monkeypatch.setattr("config.reload_config", lambda: calls.append("reload_config"))
    monkeypatch.setattr("lib.llm_clients.reset_model_config_cache", lambda: calls.append("reset_llm"))
    monkeypatch.setattr("lib.embeddings.reset_embeddings_provider", lambda: calls.append("reset_embeddings"))

    project_docs_worker._refresh_runtime_config_for_update("demo")

    assert calls == ["reload_config", "reset_llm", "reset_embeddings"]


def test_project_docs_worker_writes_fatal_log_before_failhard_raise(monkeypatch, tmp_path):
    from core import project_docs_worker

    log_path = tmp_path / "workers" / "demo.log"
    monkeypatch.setattr(project_docs_worker.project_docs, "validate_project_name", lambda project: project)
    monkeypatch.setattr(project_docs_worker, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(project_docs_worker.project_docs, "read_update_request", lambda _project: {"request_id": "req-1"})
    monkeypatch.setattr(project_docs_worker.project_docs, "update_request_ready_for_worker", lambda request: True)
    monkeypatch.setattr(project_docs_worker.project_docs, "write_worker_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(project_docs_worker.project_docs, "worker_log_path", lambda _project: log_path)
    monkeypatch.setattr(project_docs_worker.project_docs, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        project_docs_worker,
        "_start_update_heartbeat",
        lambda _project, _interval: (
            type("Stop", (), {"set": lambda self: None})(),
            type("Thread", (), {"join": lambda self, timeout=None: None})(),
        ),
    )
    monkeypatch.setattr(project_docs_worker, "_refresh_runtime_config_for_update", lambda _project: None)
    monkeypatch.setattr(
        project_docs_worker.project_docs,
        "execute_update_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("edit block mismatch")),
    )

    with pytest.raises(RuntimeError, match="edit block mismatch"):
        project_docs_worker.run_worker("demo", once=True, interval_seconds=0.5)

    assert "Project docs worker fatal error for demo: edit block mismatch" in log_path.read_text(encoding="utf-8")


def test_project_docs_worker_supervisor_pid_parse_failure_logs(monkeypatch, caplog):
    from core import project_docs_worker

    monkeypatch.setenv("QUAID_SUPERVISOR_PID", "not-a-pid")

    with caplog.at_level(logging.WARNING, logger="core.project_docs_worker"):
        assert project_docs_worker._supervisor_alive() is False

    assert "QUAID_SUPERVISOR_PID='not-a-pid' is invalid" in caplog.text


def test_project_docs_worker_config_refresh_logs_fail_policy_failure(monkeypatch, caplog):
    from core import project_docs_worker

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "lib.fail_policy":
            raise ImportError("fail policy missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("config.reload_config", lambda: (_ for _ in ()).throw(RuntimeError("reload broken")))
    monkeypatch.setattr("builtins.__import__", fake_import)

    with caplog.at_level(logging.WARNING, logger="core.project_docs_worker"):
        with pytest.raises(RuntimeError, match="reload broken"):
            project_docs_worker._refresh_runtime_config_for_update("demo")

    assert "Project docs worker fail-hard policy check failed during config refresh for demo" in caplog.text


def test_request_janitor_run_writes_hidden_state_and_blocks_parallel_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    from core import project_docs

    request = project_docs.request_janitor_run(instance="alpha", reason="manual-test", requested_by="pytest")

    assert request["scope"] == "instance"
    assert request["instance"] == "alpha"
    request_file = tmp_path / "data" / "project-docs" / "supervisor" / "janitor-request.json"
    payload = json.loads(request_file.read_text(encoding="utf-8"))
    assert payload["request_id"] == request["request_id"]
    assert payload["status"] == "pending"

    with pytest.raises(RuntimeError, match="already in progress"):
        project_docs.request_janitor_run(reason="second-request", requested_by="pytest")


def test_wait_for_janitor_request_returns_final_record(tmp_path, monkeypatch):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    from core import project_docs

    request = project_docs.request_janitor_run(reason="manual-test", requested_by="pytest")
    project_docs.write_janitor_request(
        {
            **request,
            "status": "completed",
            "started_at": project_docs.utc_now(),
            "completed_at": project_docs.utc_now(),
            "exit_codes": {"alpha": 0},
            "errors": [],
        }
    )

    result = project_docs.wait_for_janitor_request(request["request_id"], timeout_seconds=0.5)

    assert result["status"] == "completed"
    assert result["exit_codes"] == {"alpha": 0}


def test_status_and_diff_report_pending_source_change(project_env):
    _tmp_path, src, _entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('v2')\n", encoding="utf-8")

    status = project_docs.project_status("demo")
    diff = project_docs.project_diff("demo", full=False)

    assert status["status"] == "stale"
    assert status["fresh"] is False
    assert status["pending_source_change_count"] >= 1
    assert status["project_log_cursor"] == status["project_log_offset"]
    assert "current_shadow_head" in status
    assert "docs_cursor_head" in status
    assert "worker_heartbeat" in status
    assert "worker_log_path" in status
    assert "worker_log_tail" in status
    assert any(change["path"] == "tool.py" for change in diff["changes"])


def test_status_includes_worker_log_tail(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    log_path = project_docs.worker_log_path("demo")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(f"line-{i}" for i in range(50)), encoding="utf-8")

    status = project_docs.project_status("demo")
    rendered = project_docs.format_status(status)

    assert status["worker_log_path"] == str(log_path)
    assert status["worker_log_tail"][-1] == "line-49"
    assert "Recent worker log:" in rendered
    assert "line-49" in rendered


def test_format_status_hides_stale_last_error_when_project_is_fresh():
    from core import project_docs

    rendered = project_docs.format_status(
        {
            "project": "demo",
            "status": "fresh",
            "pending_source_change_count": 0,
            "project_log_bytes_pending": 0,
            "project_log_queue_pending": 0,
            "supervisor_pid": None,
            "worker_pid": None,
            "progress": {},
            "state": {
                "last_completed_at": "2026-04-25T00:00:00Z",
                "last_error": "QUAID_INSTANCE environment variable is not set",
            },
        }
    )
    assert "Last error:" not in rendered


def test_format_status_hides_benign_quaid_instance_worker_log_noise_when_project_is_fresh():
    from core import project_docs

    rendered = project_docs.format_status(
        {
            "project": "demo",
            "status": "fresh",
            "pending_source_change_count": 0,
            "project_log_bytes_pending": 0,
            "project_log_queue_pending": 0,
            "supervisor_pid": None,
            "worker_pid": None,
            "progress": {},
            "worker_log_tail": [
                "Project docs worker tick failed for demo",
                "QUAID_INSTANCE environment variable is not set",
            ],
            "state": {
                "last_completed_at": "2026-04-25T00:00:00Z",
                "last_metrics": {"docs_updated": 1},
            },
        }
    )

    assert "QUAID_INSTANCE environment variable is not set" not in rendered
    assert "Recent worker log:" in rendered
    assert "Project docs worker tick failed for demo" in rendered


def test_format_status_keeps_quaid_instance_warning_before_successful_docs_update():
    from core import project_docs

    rendered = project_docs.format_status(
        {
            "project": "demo",
            "status": "fresh",
            "pending_source_change_count": 0,
            "project_log_bytes_pending": 0,
            "project_log_queue_pending": 0,
            "supervisor_pid": None,
            "worker_pid": None,
            "progress": {},
            "worker_log_tail": [
                "QUAID_INSTANCE environment variable is not set",
            ],
            "state": {"last_completed_at": "2026-04-25T00:00:00Z"},
        }
    )

    assert "QUAID_INSTANCE environment variable is not set" in rendered


def test_execute_update_once_snapshots_applies_indexes_and_advances_cursors(project_env):
    tmp_path, src, entry = project_env
    from core import project_docs

    calls = []
    (src / "tool.py").write_text("print('v2')\n", encoding="utf-8")
    project_log = Path(entry["canonical_path"]) / "PROJECT.log"
    project_log.write_text("- [2026-04-19T00:00:00] Tool behavior changed\n", encoding="utf-8")
    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    def _update_project_docs(*args, **kwargs):
        calls.append("update_docs")
        return {"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}

    def _sync_registry(project, canonical_path, *, root_docs, protected_names):
        calls.append("sync_registry")
        assert project == "demo"
        assert canonical_path == str(Path(entry["canonical_path"]))
        assert root_docs == {"PROJECT.md", "TOOLS.md", "AGENTS.md"}
        assert protected_names == {"PROJECT.log"}
        if calls == ["sync_registry"]:
            return {"registered": 3, "unregistered": 1, "project_md_refreshed": 1}
        assert calls == ["sync_registry", "update_docs", "sync_registry"]
        return {"registered": 0, "unregistered": 0, "project_md_refreshed": 1}

    def _update_registered_docs(*args, **kwargs):
        calls.append("update_registered_docs")
        return 2

    def _index_project_logs(*args, **kwargs):
        calls.append("index_project_logs")
        return 1

    with patch("core.docs_updater_hook.update_project_docs", side_effect=_update_project_docs) as update_docs, \
         patch("core.project_docs.sync_project_docs_registry", side_effect=AssertionError("worker direct registry sync should use DocsDB broker")), \
         patch("core.docs.updater.sync_project_visible_docs", side_effect=_sync_registry) as sync_registry, \
         patch("core.docs.updater.update_registered_docs", side_effect=_update_registered_docs) as update_registered, \
         patch("core.docs.updater.index_project_logs", side_effect=_index_project_logs) as index_project_logs:
        result = project_docs.execute_update_once("demo", request=request)

    assert result["status"] == "fresh"
    assert result["indexed_docs"] == 2
    assert result["indexed_project_logs"] == 1
    assert result["snapshot"]["commit_hash"]
    assert calls == ["sync_registry", "update_docs", "sync_registry", "update_registered_docs", "index_project_logs"]
    update_docs.assert_called_once()
    assert update_docs.call_args.kwargs["force_project"] == "demo"
    assert update_docs.call_args.kwargs["extraction_result"]["project_logs"]["demo"]
    assert sync_registry.call_count == 2
    for call in sync_registry.call_args_list:
        assert call.args == ("demo", str(Path(entry["canonical_path"])))
        assert call.kwargs == {
            "root_docs": {"PROJECT.md", "TOOLS.md", "AGENTS.md"},
            "protected_names": {"PROJECT.log"},
        }
    update_registered.assert_called_once_with(
        project="demo",
        dry_run=False,
        protected_names={"PROJECT.log"},
        index_project_logs_after=False,
    )
    index_project_logs.assert_called_once_with(project="demo")
    assert not project_docs.request_path("demo").exists()
    state = project_docs.read_state("demo")
    assert state["status"] == "fresh"
    assert state["phase"] == "idle"
    assert state["progress"]["message"] == "project-docs update complete"
    assert state["last_progress_update"] == state["progress"]["updated_at"]
    assert state["project_log_offset"] == project_log.stat().st_size
    assert state["last_indexed_docs"] == 2
    assert state["last_indexed_project_logs"] == 1
    assert state["last_registry_sync"] == {"registered": 3, "unregistered": 1, "project_md_refreshed": 2}
    assert state["last_metrics"]["docs_updated"] == 1


def test_execute_update_once_index_failure_respects_fail_policy(project_env, caplog):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    calls: list[str] = []

    def _fail_index(*_args, **_kwargs):
        calls.append("update_registered_docs")
        raise RuntimeError("index boom")

    caplog.set_level(logging.WARNING)
    with patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}), \
         patch("core.project_docs.sync_project_docs_registry", side_effect=AssertionError("worker direct registry sync should use DocsDB broker")), \
         patch("core.docs.updater.sync_project_visible_docs", return_value={"registered": 1, "unregistered": 0, "project_md_refreshed": 1}), \
         patch("core.docs.updater.update_registered_docs", side_effect=_fail_index), \
         patch("core.docs.updater.index_project_logs", side_effect=AssertionError("project-log indexing should not run after registered-doc failure")), \
         patch("core.project_docs._fail_hard_enabled", return_value=False), \
         patch("core.plugins.docsdb_contract._fail_hard_enabled", return_value=False), \
         patch("core.runtime.events._is_fail_hard_enabled", return_value=False):
        result = project_docs.execute_update_once("demo")

    assert result["status"] == "error"
    assert result["metrics"]["errors"] == 1
    assert result["metrics"]["index_error"] == "index boom"
    assert result["indexed_docs"] == 0
    assert result["indexed_project_logs"] == 0
    assert calls == ["update_registered_docs"]
    assert "project-docs update index failed for demo (fail-soft): index boom" in caplog.text
    state = project_docs.read_state("demo")
    assert state["status"] == "error"
    assert "index boom" in state["last_error"]

    caplog.clear()
    with patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}), \
         patch("core.project_docs.sync_project_docs_registry", side_effect=AssertionError("worker direct registry sync should use DocsDB broker")), \
         patch("core.docs.updater.sync_project_visible_docs", return_value={"registered": 1, "unregistered": 0, "project_md_refreshed": 1}), \
         patch("core.docs.updater.update_registered_docs", side_effect=RuntimeError("failhard index boom")), \
         patch("core.docs.updater.index_project_logs", side_effect=AssertionError("project-log indexing should not run after failHard registered-doc failure")), \
         patch("core.project_docs._fail_hard_enabled", return_value=True), \
         patch("core.plugins.docsdb_contract._fail_hard_enabled", return_value=True), \
         patch("core.runtime.events._is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="failhard index boom"):
            project_docs.execute_update_once("demo")

    state = project_docs.read_state("demo")
    assert state["status"] == "error"
    assert state["last_error"] == "failhard index boom"
    assert "project-docs update index failed for demo: failhard index boom" in caplog.text
    assert "project-docs update index failed for demo (fail-soft)" not in caplog.text


def test_execute_update_once_dry_run_skips_registry_and_index(project_env):
    _tmp_path, src, _entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('dry run')\n", encoding="utf-8")
    request = project_docs.request_update("demo", reason="dry-run-test", requested_by="pytest")
    calls: list[object] = []

    def _update_project_docs(snapshots, *, extraction_result, dry_run, force_project):
        calls.append(("update_docs", dry_run, force_project, bool(snapshots), bool(extraction_result["project_logs"]["demo"])))
        return {"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}

    with patch("core.docs_updater_hook.update_project_docs", side_effect=_update_project_docs), \
         patch("core.docs.updater.sync_project_visible_docs", side_effect=AssertionError("dry-run must not sync docs registry")), \
         patch("core.docs.updater.update_registered_docs", side_effect=AssertionError("dry-run must not index registered docs")), \
         patch("core.docs.updater.index_project_logs", side_effect=AssertionError("dry-run must not index project logs")):
        result = project_docs.execute_update_once("demo", request=request, dry_run=True)

    assert result["status"] == "fresh"
    assert result["registry_sync"] == {"registered": 0, "unregistered": 0, "project_md_refreshed": 0}
    assert result["indexed_docs"] == 0
    assert result["indexed_project_logs"] == 0
    assert calls == [("update_docs", True, "demo", True, False)]
    assert project_docs.request_path("demo").exists()


def test_execute_update_once_broker_failure_has_no_direct_fallback(project_env):
    _tmp_path, src, _entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('broker failure')\n", encoding="utf-8")

    with patch("core.plugins.docsdb_contract.register_project_docs_update_request_handler"), \
         patch("core.runtime.events.request_broker_event", return_value={"status": "failed", "error": "synthetic broker failure"}), \
         patch("core.docs_updater_hook.update_project_docs", side_effect=AssertionError("worker must not fall back to direct docs update")), \
         patch("core.project_docs.sync_project_docs_registry", side_effect=AssertionError("worker must not fall back to direct registry sync")), \
         patch("core.docs.updater.update_registered_docs", side_effect=AssertionError("worker must not fall back to direct registered-doc indexing")), \
         patch("core.docs.updater.index_project_logs", side_effect=AssertionError("worker must not fall back to direct project-log indexing")):
        with pytest.raises(RuntimeError, match="synthetic broker failure"):
            project_docs.execute_update_once("demo")

    state = project_docs.read_state("demo")
    assert state["status"] == "error"
    assert state["last_error"] == "synthetic broker failure"


def test_execute_update_once_records_project_md_edit_mismatch_when_fail_open(project_env, caplog):
    _tmp_path, src, _entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('edit mismatch')\n", encoding="utf-8")
    mismatch = RuntimeError("1 edit block(s) did not match PROJECT.md content")

    caplog.set_level(logging.ERROR, logger="core.project_docs")
    with patch("core.project_docs._request_project_docs_update_via_broker", side_effect=mismatch), \
         patch("core.project_docs._fail_hard_enabled", return_value=False):
        result = project_docs.execute_update_once("demo")

    assert result["status"] == "error"
    assert result["metrics"]["errors"] == 1
    assert result["metrics"]["update_error"] == str(mismatch)
    assert result["indexed_docs"] == 0
    assert result["indexed_project_logs"] == 0
    state = project_docs.read_state("demo")
    assert state["status"] == "error"
    assert state["last_metrics"]["update_error"] == str(mismatch)
    assert "Project docs update edit-block mismatch for demo" in caplog.text


def test_execute_update_once_raises_project_md_edit_mismatch_when_fail_hard(project_env):
    _tmp_path, src, _entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('edit mismatch')\n", encoding="utf-8")
    mismatch = RuntimeError("1 edit block(s) did not match PROJECT.md content")

    with patch("core.project_docs._request_project_docs_update_via_broker", side_effect=mismatch), \
         patch("core.project_docs._fail_hard_enabled", return_value=True), \
         pytest.raises(RuntimeError, match="1 edit block"):
        project_docs.execute_update_once("demo")

    state = project_docs.read_state("demo")
    assert state["status"] == "error"
    assert state["last_error"] == str(mismatch)


def test_project_docs_poison_request_exhausts_retries_without_tight_loop(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")
    monkeypatch.setenv("QUAID_PROJECT_DOCS_REQUEST_MAX_RETRIES", "2")
    monkeypatch.setenv("QUAID_PROJECT_DOCS_REQUEST_RETRY_BASE_SECONDS", "0")

    with patch("core.plugins.docsdb_contract.register_project_docs_update_request_handler"), \
         patch("core.runtime.events.request_broker_event", return_value={"status": "failed", "error": "poison"}), \
         patch("core.project_docs._fail_hard_enabled", return_value=False):
        with pytest.raises(RuntimeError, match="poison"):
            project_docs.execute_update_once("demo", request=request)

        retrying = project_docs.read_update_request("demo")
        assert retrying["status"] == "retrying"
        assert retrying["attempt_count"] == 1
        assert project_docs.update_request_ready_for_worker(retrying) is True

        with pytest.raises(RuntimeError, match="poison"):
            project_docs.execute_update_once("demo", request=retrying)

    failed = project_docs.read_update_request("demo")
    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 2
    assert project_docs.update_request_ready_for_worker(failed) is False
    state = project_docs.read_state("demo")
    assert state["status"] == "error"
    assert "failed after 2 attempts" in state["last_error"]
    status = project_docs.project_status("demo")
    assert status["pending_request"]["status"] == "failed"


def test_project_docs_permanent_request_failure_does_not_retry(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    project_docs._record_update_request_failure(
        "demo",
        request,
        ValueError("invalid project docs payload"),
    )

    failed = project_docs.read_update_request("demo")
    assert failed["status"] == "failed"
    assert failed["attempt_count"] == 1
    assert "next_retry_at" not in failed
    assert project_docs.update_request_ready_for_worker(failed) is False
    state = project_docs.read_state("demo")
    assert state["status"] == "error"
    assert "failed permanently" in state["last_error"]


def test_project_docs_failhard_broker_failure_records_retry_metadata(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    with patch("core.plugins.docsdb_contract.register_project_docs_update_request_handler"), \
         patch("core.runtime.events.request_broker_event", return_value={"status": "failed", "error": "poison"}), \
         patch("core.project_docs._fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="poison"):
            project_docs.execute_update_once("demo", request=request)

    retained = project_docs.read_update_request("demo")
    assert retained["status"] == "retrying"
    assert retained["attempt_count"] == 1
    assert retained["last_error"] == "poison"
    assert retained.get("next_retry_at")
    assert project_docs.update_request_ready_for_worker(retained) is False


def test_project_docs_request_retry_backoff_honors_quaid_now(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
    monkeypatch.setenv("QUAID_PROJECT_DOCS_REQUEST_RETRY_BASE_SECONDS", "60")
    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    project_docs._record_update_request_failure("demo", request, RuntimeError("transient broker failure"))

    retrying = project_docs.read_update_request("demo")
    assert retrying["next_retry_at"] == "2026-03-11T05:07:07+00:00"
    assert project_docs.update_request_ready_for_worker(retrying) is False

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:07:08Z")
    assert project_docs.update_request_ready_for_worker(retrying) is True


def test_project_docs_worker_respects_request_retry_backoff(monkeypatch):
    from core import project_docs_worker

    calls = []
    future = (datetime.now(tz=timezone.utc) + timedelta(seconds=60)).isoformat()

    monkeypatch.setattr(project_docs_worker.project_docs, "validate_project_name", lambda project: project)
    monkeypatch.setattr(project_docs_worker, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(
        project_docs_worker.project_docs,
        "read_update_request",
        lambda _project: {"request_id": "req-1", "status": "retrying", "next_retry_at": future},
    )
    monkeypatch.setattr(project_docs_worker.project_docs, "update_request_ready_for_worker", lambda request: False)
    monkeypatch.setattr(
        project_docs_worker.project_docs,
        "project_has_pending_update",
        lambda _project: (_ for _ in ()).throw(AssertionError("backing-off request should skip stale probe")),
    )
    monkeypatch.setattr(project_docs_worker.project_docs, "write_worker_heartbeat", lambda *args, **kwargs: calls.append(("heartbeat", args, kwargs)))
    monkeypatch.setattr(project_docs_worker.project_docs, "execute_update_once", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("backoff request should not run")))
    monkeypatch.setattr(project_docs_worker.project_docs, "clear_worker_pid_for_current_process", lambda project: calls.append(("clear", project)))

    assert project_docs_worker.run_worker("demo", once=True, interval_seconds=0.5) == 0

    assert ("clear", "demo") in calls


def test_project_docs_worker_uses_lightweight_pending_predicate(monkeypatch):
    from core import project_docs_worker

    calls = []
    monkeypatch.setattr(project_docs_worker.project_docs, "validate_project_name", lambda project: project)
    monkeypatch.setattr(project_docs_worker, "_supervisor_alive", lambda: True)
    monkeypatch.setattr(project_docs_worker.project_docs, "read_update_request", lambda _project: None)
    monkeypatch.setattr(project_docs_worker.project_docs, "update_request_ready_for_worker", lambda request: False)
    monkeypatch.setattr(project_docs_worker.project_docs, "project_has_pending_update", lambda _project: False)
    monkeypatch.setattr(
        project_docs_worker.project_docs,
        "project_status",
        lambda _project: (_ for _ in ()).throw(AssertionError("worker tick should not call display status")),
    )
    monkeypatch.setattr(project_docs_worker.project_docs, "write_worker_heartbeat", lambda *args, **kwargs: calls.append(("heartbeat", args, kwargs)))
    monkeypatch.setattr(project_docs_worker.project_docs, "clear_worker_pid_for_current_process", lambda project: calls.append(("clear", project)))

    assert project_docs_worker.run_worker("demo", once=True, interval_seconds=0.5) == 0

    assert ("clear", "demo") in calls


def test_project_status_reports_pending_project_log_queue(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from datastore.docsdb import project_log_queue

    metrics = project_log_queue.enqueue_project_logs(
        {"demo": ["Queued project log milestone"]},
        trigger="Reset",
    )

    assert metrics["entries_queued"] == 1
    assert metrics["entries_written"] == 1
    status = project_docs.project_status("demo")
    assert status["status"] == "stale"
    assert status["fresh"] is False
    assert status["project_log_queue_pending"] == 1


def test_project_log_queue_honors_quaid_now_for_created_at(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from datastore.docsdb import project_log_queue

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

    metrics = project_log_queue.enqueue_project_logs(
        {"demo": ["Queued project log milestone"]},
        trigger="Reset",
    )

    assert metrics["entries_queued"] == 1
    with project_log_queue.project_queue_lock("demo"):
        items = project_log_queue.drain_project_log_queue("demo")

    assert len(items) == 1
    assert items[0]["created_at"] == "2026-03-11T05:06:07+00:00"


def test_project_log_queue_malformed_quaid_now_honors_failhard(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from datastore.docsdb import project_log_queue

    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with patch("datastore.docsdb.project_log_queue.is_fail_hard_enabled", return_value=True), \
         pytest.raises(RuntimeError, match="Invalid QUAID_NOW"):
        project_log_queue.enqueue_project_logs(
            {"demo": ["Queued project log milestone"]},
            trigger="Reset",
        )

    assert project_log_queue.pending_project_log_count("demo") == 0

    with patch("datastore.docsdb.project_log_queue.is_fail_hard_enabled", return_value=False):
        metrics = project_log_queue.enqueue_project_logs(
            {"demo": ["Queued project log milestone"]},
            trigger="Reset",
        )

    assert metrics["entries_written"] == 1
    with project_log_queue.project_queue_lock("demo"):
        items = project_log_queue.drain_project_log_queue("demo")

    assert len(items) == 1
    assert items[0]["created_at"] != "not-a-date"


def test_project_log_queue_requires_lock_for_drain_mark_cleanup(project_env):
    _tmp_path, _src, _entry = project_env
    from datastore.docsdb import project_log_queue

    metrics = project_log_queue.enqueue_project_logs(
        {"demo": ["Queued project log milestone"]},
        trigger="Reset",
    )
    assert metrics["entries_written"] == 1

    with pytest.raises(RuntimeError, match="project-log queue lock is required"):
        project_log_queue.drain_project_log_queue("demo")
    with pytest.raises(RuntimeError, match="project-log queue lock is required"):
        project_log_queue.mark_project_log_queue_committed("demo", ["bad-id"])
    with pytest.raises(RuntimeError, match="project-log queue lock is required"):
        project_log_queue.cleanup_project_log_queue("demo")

    with project_log_queue.project_queue_lock("demo"):
        items = project_log_queue.drain_project_log_queue("demo")
        assert len(items) == 1
        project_log_queue.mark_project_log_queue_committed("demo", [items[0]["id"]])

    assert project_log_queue.pending_project_log_count("demo") == 0


def test_project_log_queue_accepts_unicode_project_names(project_env):
    _tmp_path, _src, _entry = project_env
    from datastore.docsdb import project_log_queue

    project = "mañana-app"
    decomposed_project = "man\u0303ana-app"
    underscore_project = "demo_project"

    metrics = project_log_queue.enqueue_project_logs(
        {
            decomposed_project: ["Queued unicode project log milestone"],
            "Demo_Project": ["Queued underscore project log milestone"],
        },
        trigger="Reset",
    )

    assert metrics["entries_written"] == 2
    assert project_log_queue.pending_project_log_count(project) == 1
    assert project_log_queue.pending_project_log_count(underscore_project) == 1
    with project_log_queue.project_queue_lock(project):
        items = project_log_queue.drain_project_log_queue(project)
        project_log_queue.mark_project_log_queue_committed(project, [items[0]["id"]])
    with project_log_queue.project_queue_lock(underscore_project):
        underscore_items = project_log_queue.drain_project_log_queue(underscore_project)
        project_log_queue.mark_project_log_queue_committed(underscore_project, [underscore_items[0]["id"]])

    assert items[0]["project"] == project
    assert items[0]["entries"] == [{"text": "Queued unicode project log milestone"}]
    assert underscore_items[0]["project"] == underscore_project
    assert underscore_items[0]["entries"] == [{"text": "Queued underscore project log milestone"}]
    assert project_log_queue.pending_project_log_count(project) == 0
    assert project_log_queue.pending_project_log_count(underscore_project) == 0


def test_project_log_queue_failed_flock_does_not_authorize_drain(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from datastore.docsdb import project_log_queue

    class BrokenFcntl:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(_handle, _flags):
            raise OSError("flock unavailable")

    monkeypatch.setitem(sys.modules, "fcntl", BrokenFcntl)
    monkeypatch.setattr(project_log_queue, "is_fail_hard_enabled", lambda: False)

    with project_log_queue.project_queue_lock("demo"):
        with pytest.raises(RuntimeError, match="project-log queue lock is required"):
            project_log_queue.drain_project_log_queue("demo")


def test_execute_update_once_drains_project_log_queue_under_worker_lock(project_env):
    _tmp_path, _src, entry = project_env
    from core import project_docs
    from datastore.docsdb import project_log_queue

    project_log_queue.enqueue_project_logs(
        {"demo": ["Queued project log milestone"]},
        trigger="Reset",
        date_str="2026-04-23T08:00:00",
        session_id="session-queue",
    )
    project_log = Path(entry["canonical_path"]) / "PROJECT.log"
    project_md = Path(entry["canonical_path"]) / "PROJECT.md"

    with patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}) as update_docs, \
         patch("core.docs.updater.update_registered_docs", return_value=2) as update_registered, \
         patch("core.docs.updater.index_project_logs", return_value=1) as index_project_logs:
        result = project_docs.execute_update_once("demo")

    assert result["status"] == "fresh"
    assert result["project_log_queue"]["history_entries_written"] == 1
    assert project_log_queue.pending_project_log_count("demo") == 0
    assert "Queued project log milestone" in project_log.read_text(encoding="utf-8")
    assert "Queued project log milestone" not in project_md.read_text(encoding="utf-8")
    state = project_docs.read_state("demo")
    assert state["project_log_offset"] == project_log.stat().st_size
    update_docs.assert_called_once()
    assert "Queued project log milestone" in update_docs.call_args.kwargs["extraction_result"]["project_logs"]["demo"][0]
    update_registered.assert_called_once_with(
        project="demo",
        dry_run=False,
        protected_names={"PROJECT.log"},
        index_project_logs_after=False,
    )


def test_commit_queued_project_logs_holds_queue_lock_around_drain_and_mark(monkeypatch):
    from core import project_docs

    events: list[str] = []

    class _Lock:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")

    def _drain(project):
        assert project == "demo"
        assert events == ["enter"]
        return [
            {
                "id": "1-2-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "entries": [{"text": "queued item"}],
                "trigger": "Reset",
            }
        ]

    def _append(*_args, **_kwargs):
        assert events == ["enter"]
        return {"history_entries_written": 1}

    def _mark(project, item_ids):
        assert project == "demo"
        assert item_ids == ["1-2-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
        assert events == ["enter"]

    monkeypatch.setattr("core.docs.updater.project_log_queue_lock", lambda project: _Lock())
    monkeypatch.setattr("core.docs.updater.drain_project_log_queue", _drain)
    monkeypatch.setattr("core.docs.updater.append_project_logs", _append)
    monkeypatch.setattr("core.docs.updater.mark_project_log_queue_committed", _mark)

    metrics = project_docs._commit_queued_project_logs("demo")

    assert metrics["items_committed"] == 1
    assert metrics["history_entries_written"] == 1
    assert events == ["enter", "exit"]


def test_commit_queued_project_logs_logs_missing_id_before_failhard(monkeypatch, caplog):
    from core import project_docs

    class _Lock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("core.docs.updater.project_log_queue_lock", lambda _project: _Lock())
    monkeypatch.setattr("core.docs.updater.drain_project_log_queue", lambda _project: [{"entries": [{"text": "queued"}]}])
    monkeypatch.setattr("core.project_docs._fail_hard_enabled", lambda: True)

    with caplog.at_level(logging.WARNING, logger="core.project_docs"):
        with pytest.raises(RuntimeError, match="project-log queue item missing id for demo"):
            project_docs._commit_queued_project_logs("demo")

    assert "Skipping project-log queue item without id for demo" in caplog.text


def test_commit_queued_project_logs_logs_dropped_entries_before_failhard(monkeypatch, caplog):
    from core import project_docs

    class _Lock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return None

    item_id = "1-2-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    monkeypatch.setattr("core.docs.updater.project_log_queue_lock", lambda _project: _Lock())
    monkeypatch.setattr(
        "core.docs.updater.drain_project_log_queue",
        lambda _project: [{"id": item_id, "_dropped_entries_count": 1, "entries": [{"text": "queued"}]}],
    )
    monkeypatch.setattr("core.project_docs._fail_hard_enabled", lambda: True)

    with caplog.at_level(logging.CRITICAL, logger="core.project_docs"):
        with pytest.raises(RuntimeError, match=f"project-log queue item {item_id} for demo dropped 1 malformed entries"):
            project_docs._commit_queued_project_logs("demo")

    assert f"project-log queue item {item_id} for demo dropped 1 malformed entries" in caplog.text


def test_commit_queued_project_logs_dead_letters_malformed_item(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from datastore.docsdb import project_log_queue

    item_id = "1-2-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    queue_dir = project_log_queue.project_queue_dir("demo")
    queue_dir.mkdir(parents=True)
    (queue_dir / f"{item_id}.json").write_text(
        json.dumps(
            {
                "id": item_id,
                "project": "demo",
                "entries": [{"text": "valid queued item"}, {"text": ""}],
                "trigger": "Reset",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with patch("core.docs.updater.append_project_logs", side_effect=AssertionError("malformed item should not commit")), \
         patch("core.project_docs._fail_hard_enabled", return_value=False):
        metrics = project_docs._commit_queued_project_logs("demo")

    assert metrics["errors"] == 1
    assert metrics["entries_seen"] == 2
    assert metrics["items_committed"] == 0
    assert project_log_queue.pending_project_log_count("demo") == 0
    dead_letters = list(project_log_queue.project_queue_dead_letter_dir("demo").glob("*.json"))
    assert any(path.name.endswith(f"{item_id}.json") for path in dead_letters)


def test_project_docs_process_command_timeout_logs_debug(monkeypatch, caplog):
    from core import project_docs

    def timeout_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["ps"], timeout=2)

    monkeypatch.setattr(project_docs.subprocess, "run", timeout_run)

    with caplog.at_level(logging.DEBUG, logger="core.project_docs"):
        assert project_docs._process_command(123) == ""

    assert "Failed inspecting process command for pid=123" in caplog.text


def test_adapter_type_from_instance_name_logs_resolution_failure(monkeypatch, caplog):
    from core import project_docs

    monkeypatch.setattr(
        "lib.adapter._adapter_type_from_instance_id",
        lambda _instance: (_ for _ in ()).throw(RuntimeError("adapter lookup failed")),
    )

    with caplog.at_level(logging.DEBUG, logger="core.project_docs"):
        assert project_docs._adapter_type_from_instance_name("codex-demo") == ""

    assert "Failed resolving adapter type for instance 'codex-demo'" in caplog.text


def test_parse_iso_ts_logs_unparseable_timestamp(caplog):
    from core import project_docs

    with caplog.at_level(logging.DEBUG, logger="core.project_docs"):
        assert project_docs._parse_iso_ts("not-a-date") is None

    assert "Unparseable ISO timestamp 'not-a-date'" in caplog.text


def test_project_log_queue_dead_letters_invalid_json_during_drain(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from datastore.docsdb import project_log_queue

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
    queue_dir = project_log_queue.project_queue_dir("demo")
    queue_dir.mkdir(parents=True)
    poison = queue_dir / "1-2-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.json"
    poison.write_text("{not json", encoding="utf-8")
    project_log_queue.enqueue_project_logs({"demo": ["valid queued item"]}, trigger="Reset")

    with project_log_queue.project_queue_lock("demo"), \
         patch("datastore.docsdb.project_log_queue.is_fail_hard_enabled", return_value=False):
        items = project_log_queue.drain_project_log_queue("demo")

    assert [item["entries"][0]["text"] for item in items] == ["valid queued item"]
    assert project_log_queue.pending_project_log_count("demo") == 1
    assert not poison.exists()
    dead_letters = list(project_log_queue.project_queue_dead_letter_dir("demo").glob("*.json"))
    assert any(path.name.endswith(poison.name) for path in dead_letters)
    metadata_files = list(project_log_queue.project_queue_dead_letter_dir("demo").glob("*.metadata.json"))
    assert len(metadata_files) == 1
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["dead_lettered_at"] == "2026-03-11T05:06:07+00:00"


def test_project_log_queue_dead_letter_malformed_quaid_now_falls_back_when_fail_open(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from datastore.docsdb import project_log_queue

    queue_dir = project_log_queue.project_queue_dir("demo")
    queue_dir.mkdir(parents=True)
    poison = queue_dir / "1-2-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.json"
    poison.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with project_log_queue.project_queue_lock("demo"), \
         patch("datastore.docsdb.project_log_queue.is_fail_hard_enabled", return_value=False):
        project_log_queue.drain_project_log_queue("demo")

    assert not poison.exists()
    metadata_files = list(project_log_queue.project_queue_dead_letter_dir("demo").glob("*.metadata.json"))
    assert len(metadata_files) == 1
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["dead_lettered_at"] != "not-a-date"


def test_execute_update_once_does_not_run_supervisor_docs_maintenance_tick(project_env):
    _tmp_path, src, _entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('worker path')\n", encoding="utf-8")

    with patch("core.project_docs.auto_register_project_docs", side_effect=AssertionError("supervisor auto-register tick only")), \
         patch("core.project_docs.index_one_stale_registered_doc", side_effect=AssertionError("supervisor stale-index tick only")), \
         patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}), \
         patch("core.docs.updater.sync_project_visible_docs", return_value={"registered": 1, "unregistered": 0, "project_md_refreshed": 1}), \
         patch("core.docs.updater.update_registered_docs", return_value=1), \
         patch("core.docs.updater.index_project_logs", return_value=0):
        result = project_docs.execute_update_once("demo")

    assert result["status"] == "fresh"


def test_execute_update_once_preserves_structured_project_log_entry_dates(project_env):
    _tmp_path, _src, entry = project_env
    from core import project_docs
    from datastore.docsdb import project_log_queue

    project_log_queue.enqueue_project_logs(
        {
            "demo": [
                {"text": "Started retry middleware rollout", "created_at": "2026-03-01T09:15:00"},
                {"text": "Added error banner", "created_at": "2026-03-05"},
            ]
        },
        trigger="Compaction",
        date_str="2026-04-23T08:00:00",
        session_id="session-queue",
    )
    project_log = Path(entry["canonical_path"]) / "PROJECT.log"

    with patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}), \
         patch("core.docs.updater.update_registered_docs", return_value=2), \
         patch("core.docs.updater.index_project_logs", return_value=1):
        result = project_docs.execute_update_once("demo")

    assert result["status"] == "fresh"
    assert result["project_log_queue"]["history_entries_written"] == 2
    assert project_log_queue.pending_project_log_count("demo") == 0
    project_log_text = project_log.read_text(encoding="utf-8")
    assert "- [2026-03-01T09:15:00+00:00] Started retry middleware rollout" in project_log_text
    assert "- [2026-03-05T23:59:59+00:00] Added error banner" in project_log_text


def test_auto_register_project_docs_materializes_queued_transcript_projects(tmp_path, monkeypatch):
    from core import project_docs
    from core.project_registry import project_exists_raw
    from datastore.docsdb import project_log_queue

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        project_log_queue.enqueue_project_logs(
            {"recipe-app": ["Added image_url field to recipe cards"]},
            trigger="Compaction",
        )
        synced: list[str] = []
        monkeypatch.setattr("core.project_registry._sync_docs_registry_project", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            project_docs,
            "sync_project_docs_registry",
            lambda name, entry=None: synced.append(name) or {"registered": 1},
        )

        assert project_docs.auto_register_project_docs() == 1
        assert project_exists_raw("recipe-app") is True
        assert synced == ["recipe-app"]
        assert (tmp_path / "projects" / "recipe-app" / "PROJECT.md").is_file()
    finally:
        reset_adapter()


def test_auto_register_project_docs_skips_deleted_queued_projects(tmp_path, monkeypatch):
    from core import project_docs
    from core.project_registry import create_project, delete_project, project_exists_raw
    from datastore.docsdb import project_log_queue

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        monkeypatch.setattr("core.project_registry._sync_docs_registry_project", lambda *args, **kwargs: None)
        create_project("recipe-app", description="Recipe app")
        delete_project("recipe-app")
        project_log_queue.enqueue_project_logs(
            {"recipe-app": ["Queued transcript note after delete"]},
            trigger="Compaction",
        )
        synced: list[str] = []
        monkeypatch.setattr(
            project_docs,
            "sync_project_docs_registry",
            lambda name, entry=None: synced.append(name) or {"registered": 1},
        )

        assert project_docs.auto_register_project_docs() == 0
        assert project_exists_raw("recipe-app") is False
        assert synced == []
    finally:
        reset_adapter()


def test_auto_register_project_docs_explicit_missing_project_raises(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    with pytest.raises(KeyError, match="Project not found"):
        project_docs.auto_register_project_docs("missing-project")


def test_cleanup_project_state_removes_project_log_queue(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from datastore.docsdb import project_log_queue

    project_log_queue.enqueue_project_logs(
        {"demo": ["Queued project log milestone"]},
        trigger="Reset",
    )

    assert project_docs.has_project_state("demo") is True
    removed = project_docs.cleanup_project_state("demo")

    assert removed["removed"] >= 1
    assert project_log_queue.pending_project_log_count("demo") == 0


def test_execute_update_once_continues_after_failsoft_queue_item_error(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from datastore.docsdb import project_log_queue

    project_log_queue.enqueue_project_logs({"demo": ["first queued item"]}, trigger="Reset")
    project_log_queue.enqueue_project_logs({"demo": ["second queued item"]}, trigger="Reset")

    with patch("core.docs.updater.append_project_logs", side_effect=[RuntimeError("synthetic append failure"), {"history_entries_written": 1}]) as append_logs, \
         patch("core.docs.updater.update_registered_docs", return_value=0), \
         patch("core.docs.updater.index_project_logs", return_value=0), \
         patch("core.project_docs._fail_hard_enabled", return_value=False):
        result = project_docs.execute_update_once("demo")

    assert result["status"] == "error"
    assert result["project_log_queue"]["errors"] == 1
    assert result["project_log_queue"]["items_seen"] == 2
    assert result["project_log_queue"]["items_committed"] == 1
    assert append_logs.call_count == 2
    assert project_log_queue.pending_project_log_count("demo") == 1


def test_execute_update_once_replays_committed_shadow_cursor_gap(project_env):
    _tmp_path, src, _entry = project_env
    from core import project_docs

    with patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}), \
         patch("core.docs.updater.update_registered_docs", return_value=0), \
         patch("core.docs.updater.index_project_logs", return_value=0):
        first = project_docs.execute_update_once("demo")
    first_head = first["snapshot"]["commit_hash"]

    (src / "tool.py").write_text("print('v2')\n", encoding="utf-8")
    crash_snapshot = project_docs.snapshot_project("demo")
    assert crash_snapshot["commit_hash"] != first_head
    assert project_docs.pending_source_changes("demo") == []

    status = project_docs.project_status("demo")
    diff = project_docs.project_diff("demo", full=False)

    assert status["status"] == "stale"
    assert status["fresh"] is False
    assert status["shadow_cursor_pending"] is True
    assert status["pending_source_change_count"] == 0
    assert diff["change_count"] >= 1
    assert any(change["path"] == "tool.py" for change in diff["changes"])

    with patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}) as update_docs, \
         patch("core.docs.updater.update_registered_docs", return_value=1), \
         patch("core.docs.updater.index_project_logs", return_value=0):
        result = project_docs.execute_update_once("demo")

    assert result["status"] == "fresh"
    assert result["snapshot"]["commit_hash"] == crash_snapshot["commit_hash"]
    assert result["snapshot"]["diff"]
    update_docs.assert_called_once()
    state = project_docs.read_state("demo")
    assert state["last_shadow_commit"] == crash_snapshot["commit_hash"]
    assert project_docs.project_status("demo")["fresh"] is True


def test_index_project_logs_indexes_append_only_project_log(project_env, monkeypatch):
    _tmp_path, _src, entry = project_env
    from core.docs import updater

    project_log = Path(entry["canonical_path"]) / "PROJECT.log"
    project_log.write_text("- [2026-04-20T00:00:00] Milestone shipped\n", encoding="utf-8")

    indexed = []

    class FakeRag:
        def needs_reindex_many(self, paths):
            return {path: True for path in paths}

        def index_document(self, file_path):
            indexed.append(file_path)
            return 2

    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", FakeRag)

    assert updater.index_project_logs(project="demo") == 1
    assert indexed == [str(project_log.resolve())]


def test_index_project_logs_filters_unlinked_global_projects(project_env, tmp_path, monkeypatch):
    _tmp_path, _src, entry = project_env
    from core.docs import updater

    demo_log = Path(entry["canonical_path"]) / "PROJECT.log"
    demo_log.write_text("- [2026-04-20T00:00:00] Demo milestone\n", encoding="utf-8")
    other_dir = tmp_path / "other-project"
    other_dir.mkdir()
    other_log = other_dir / "PROJECT.log"
    other_log.write_text("- [2026-04-20T00:00:00] Foreign milestone\n", encoding="utf-8")

    indexed = []

    class FakeRag:
        def needs_reindex_many(self, paths):
            return {path: True for path in paths}

        def index_document(self, file_path):
            indexed.append(file_path)
            return 1

    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", FakeRag)
    monkeypatch.setattr(
        updater,
        "_linked_projects_for_current_instance",
        lambda: ({"demo"}, True),
    )
    monkeypatch.setattr(
        "core.project_registry.list_projects",
        lambda: {
            "demo": {"canonical_path": entry["canonical_path"]},
            "foreign": {"canonical_path": str(other_dir)},
        },
    )
    monkeypatch.setattr(
        "core.project_registry.get_project",
        lambda name: {
            "demo": {"canonical_path": entry["canonical_path"]},
            "foreign": {"canonical_path": str(other_dir)},
        }.get(name),
    )

    assert updater.index_project_logs() == 1
    assert indexed == [str(demo_log.resolve())]

    indexed.clear()
    assert updater.index_project_logs(project="foreign") == 0
    assert indexed == []


def test_index_project_logs_uses_managed_dir_when_canonical_path_missing(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core.docs import updater

    managed_log = tmp_path / "projects" / "demo" / "PROJECT.log"
    managed_log.write_text("- [2026-04-20T00:00:00] Managed milestone\n", encoding="utf-8")

    indexed = []

    class FakeRag:
        def needs_reindex_many(self, paths):
            return {path: True for path in paths}

        def index_document(self, file_path):
            indexed.append(file_path)
            return 1

    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", FakeRag)
    monkeypatch.setattr(updater, "_linked_projects_for_current_instance", lambda: ({"demo"}, True))
    monkeypatch.setattr("core.project_registry.get_project", lambda name: {"source_root": None})

    assert updater.index_project_logs(project="demo") == 1
    assert indexed == [str(managed_log.resolve())]


def test_index_project_logs_uses_managed_dir_without_adapter(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core.docs import updater

    managed_log = tmp_path / "projects" / "demo" / "PROJECT.log"
    managed_log.parent.mkdir(parents=True, exist_ok=True)
    managed_log.write_text("- [2026-04-20T00:00:00] Managed milestone\n", encoding="utf-8")

    indexed = []

    class FakeRag:
        def needs_reindex_many(self, paths):
            return {path: True for path in paths}

        def index_document(self, file_path):
            indexed.append(file_path)
            return 1

    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", FakeRag)
    monkeypatch.setattr(updater, "_linked_projects_for_current_instance", lambda: ({"demo"}, True))
    monkeypatch.setattr("core.project_registry.get_project", lambda name: {"source_root": None})

    reset_adapter()
    monkeypatch.setattr("lib.runtime_context.get_adapter", lambda: (_ for _ in ()).throw(RuntimeError("adapter should not be used")))

    assert updater.index_project_logs(project="demo") == 1
    assert indexed == [str(managed_log.resolve())]


def test_index_project_logs_uses_managed_dir_when_canonical_log_missing(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core.docs import updater

    canonical_without_log = tmp_path / "canonical-without-log"
    canonical_without_log.mkdir()
    managed_log = tmp_path / "projects" / "demo" / "PROJECT.log"
    managed_log.write_text("- [2026-04-20T00:00:00] Managed milestone\n", encoding="utf-8")

    indexed = []

    class FakeRag:
        def needs_reindex_many(self, paths):
            return {path: True for path in paths}

        def index_document(self, file_path):
            indexed.append(file_path)
            return 1

    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", FakeRag)
    monkeypatch.setattr(updater, "_linked_projects_for_current_instance", lambda: ({"demo"}, True))
    monkeypatch.setattr(
        "core.project_registry.get_project",
        lambda name: {"canonical_path": str(canonical_without_log), "source_root": None},
    )

    assert updater.index_project_logs(project="demo") == 1
    assert indexed == [str(managed_log.resolve())]


def test_index_project_logs_skips_when_instance_scope_unresolved(project_env, monkeypatch, caplog):
    _tmp_path, _src, entry = project_env
    from core.docs import updater

    project_log = Path(entry["canonical_path"]) / "PROJECT.log"
    project_log.write_text("- [2026-04-20T00:00:00] Demo milestone\n", encoding="utf-8")

    indexed = []

    class FakeRag:
        def needs_reindex_many(self, paths):
            return {path: True for path in paths}

        def index_document(self, file_path):
            indexed.append(file_path)
            return 1

    def should_not_discover_projects():
        raise AssertionError("PROJECT.log discovery should fail closed when scope is unresolved")

    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", FakeRag)
    monkeypatch.setattr(updater, "_linked_projects_for_current_instance", lambda: (set(), False))
    monkeypatch.setattr(updater, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr("core.project_registry.list_projects", should_not_discover_projects)
    monkeypatch.setattr("core.project_registry.get_project", lambda name: entry if name == "demo" else None)
    caplog.set_level(logging.WARNING, logger="core.docs.updater")

    assert updater.index_project_logs() == 0
    assert updater.index_project_logs(project="demo") == 1
    assert indexed == [str(project_log.resolve())]
    assert "cross-instance contamination" in caplog.text


def test_linked_projects_resolution_failure_logs(monkeypatch, caplog):
    from core.docs import updater

    monkeypatch.setattr(
        "datastore.docsdb.rag._linked_projects_for_current_instance",
        lambda: (_ for _ in ()).throw(RuntimeError("linkage broken")),
    )

    with caplog.at_level(logging.WARNING, logger="core.docs.updater"):
        assert updater._linked_projects_for_current_instance() == (set(), False)

    assert "failed to resolve linked projects for current instance" in caplog.text


def test_resolve_registered_doc_path_workspace_failure_logs(monkeypatch, tmp_path, caplog):
    from core.docs import updater

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("config._workspace_root", lambda: (_ for _ in ()).throw(RuntimeError("workspace broken")))

    with caplog.at_level(logging.DEBUG, logger="core.docs.updater"):
        resolved = updater._resolve_registered_doc_path(object(), "docs/example.md")

    assert resolved == (tmp_path / "docs" / "example.md").resolve()
    assert "workspace root resolution failed" in caplog.text


def test_sync_project_visible_docs_logs_unresolvable_registered_path(project_env, monkeypatch, caplog):
    _tmp_path, _src, entry = project_env
    from core.docs import updater

    class _BadRegistry:
        def list_docs(self, project=None):
            return [{"file_path": "broken.md"}]

        def get(self, _path):
            return {}

        def register(self, *_args, **_kwargs):
            return {}

        def unregister(self, _path):
            raise AssertionError("unregister should not run for unresolvable path")

        def _resolve_path(self, _path):
            raise RuntimeError("path broken")

    monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", lambda: _BadRegistry())

    with caplog.at_level(logging.WARNING, logger="core.docs.updater"):
        result = updater.sync_project_visible_docs(
            "demo",
            entry["canonical_path"],
            root_docs=set(),
            protected_names=set(),
        )

    assert result["unregistered"] == 0
    assert "failed to resolve path 'broken.md' for unregistration check" in caplog.text


def test_index_project_logs_raises_when_instance_scope_unresolved_fail_hard(project_env, monkeypatch):
    _tmp_path, _src, entry = project_env
    from core.docs import updater

    project_log = Path(entry["canonical_path"]) / "PROJECT.log"
    project_log.write_text("- [2026-04-20T00:00:00] Demo milestone\n", encoding="utf-8")

    monkeypatch.setattr(updater, "_linked_projects_for_current_instance", lambda: (set(), False))
    monkeypatch.setattr(updater, "_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="cannot resolve instance linkage"):
        updater.index_project_logs()


def test_project_status_counts_project_log_without_canonical_path(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    managed_log = tmp_path / "projects" / "demo" / "PROJECT.log"
    managed_log.write_text("- [2026-04-20T00:00:00] Managed milestone\n", encoding="utf-8")

    monkeypatch.setattr(
        "core.project_registry.get_project_raw",
        lambda name: {"source_root": None, "description": "Demo"},
    )

    status = project_docs.project_status("demo")
    diff = project_docs.project_diff("demo")

    assert status["status"] == "stale"
    assert status["source_error"] is None
    assert status["project_log_size"] == managed_log.stat().st_size
    assert status["project_log_bytes_pending"] == managed_log.stat().st_size
    assert diff["source_error"] is None
    assert diff["project_log_entry_count"] == 1
    assert "Managed milestone" in diff["project_log_entries"][0]


def test_project_status_counts_project_log_without_adapter(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    managed_log = tmp_path / "projects" / "demo" / "PROJECT.log"
    managed_log.parent.mkdir(parents=True, exist_ok=True)
    managed_log.write_text("- [2026-04-20T00:00:00] Managed milestone\n", encoding="utf-8")

    monkeypatch.setattr(
        "core.project_registry.get_project",
        lambda name: {"source_root": None, "description": "Demo"},
    )

    reset_adapter()
    monkeypatch.setattr("lib.runtime_context.get_adapter", lambda: (_ for _ in ()).throw(RuntimeError("adapter should not be used")))

    status = project_docs.project_status("demo")
    diff = project_docs.project_diff("demo")

    assert status["status"] == "stale"
    assert status["source_error"] is None
    assert status["project_log_size"] == managed_log.stat().st_size
    assert status["project_log_bytes_pending"] == managed_log.stat().st_size
    assert diff["project_log_entry_count"] == 1
    assert "Managed milestone" in diff["project_log_entries"][0]


def test_project_status_counts_managed_log_when_canonical_log_missing(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    canonical_without_log = tmp_path / "canonical-without-log"
    canonical_without_log.mkdir()
    managed_log = tmp_path / "projects" / "demo" / "PROJECT.log"
    managed_log.write_text("- [2026-04-20T00:00:00] Managed milestone\n", encoding="utf-8")

    monkeypatch.setattr(
        "core.project_registry.get_project",
        lambda name: {
            "canonical_path": str(canonical_without_log),
            "source_root": None,
            "description": "Demo",
        },
    )

    status = project_docs.project_status("demo")
    diff = project_docs.project_diff("demo")

    assert status["status"] == "stale"
    assert status["source_error"] is None
    assert status["project_log_size"] == managed_log.stat().st_size
    assert status["project_log_bytes_pending"] == managed_log.stat().st_size
    assert diff["source_error"] is None
    assert diff["project_log_entry_count"] == 1
    assert "Managed milestone" in diff["project_log_entries"][0]


def test_project_status_no_source_root_is_fresh_when_managed_log_cursor_current(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    managed_log = tmp_path / "projects" / "demo" / "PROJECT.log"
    managed_log.write_text("- [2026-04-20T00:00:00] Managed milestone\n", encoding="utf-8")
    log_size = managed_log.stat().st_size

    monkeypatch.setattr(
        "core.project_registry.get_project_raw",
        lambda name: {"source_root": None, "description": "Demo"},
    )
    project_docs.merge_state("demo", {"project_log_offset": log_size})

    status = project_docs.project_status("demo")
    diff = project_docs.project_diff("demo")

    assert status["status"] == "fresh"
    assert status["fresh"] is True
    assert status["source_error"] is None
    assert status["project_log_bytes_pending"] == 0
    assert diff["source_error"] is None
    assert diff["project_log_entry_count"] == 0


def test_execute_update_once_preserves_force_request_when_locked(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    with project_docs.project_update_lock("demo", blocking=True) as acquired:
        assert acquired is True
        result = project_docs.execute_update_once("demo", request=request)

    assert result["status"] == "locked"
    assert result["request_retained"] is True
    assert project_docs.read_update_request("demo")["request_id"] == request["request_id"]
    assert project_docs.read_state("demo")["status"] == "queued"


def test_project_log_read_failure_raises_without_advancing_cursor(project_env):
    _tmp_path, _src, entry = project_env
    from core import project_docs

    project_log = Path(entry["canonical_path"]) / "PROJECT.log"
    project_log.write_text("- important entry\n", encoding="utf-8")
    project_docs.write_state("demo", {"project_log_offset": 0})
    real_open = Path.open

    def flaky_open(self, *args, **kwargs):
        if self == project_log:
            raise OSError("synthetic read failure")
        return real_open(self, *args, **kwargs)

    with patch.object(Path, "open", flaky_open):
        with pytest.raises(RuntimeError, match="failed to read PROJECT.log"):
            project_docs.execute_update_once("demo")

    assert project_docs.read_state("demo")["project_log_offset"] == 0


def test_sync_project_docs_registry_registers_new_docs_and_removes_deleted_docs(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from datastore.docsdb.registry import DocsRegistry

    registry = DocsRegistry()
    project_dir = Path(_entry["canonical_path"])
    docs_dir = project_dir / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "new.md").write_text("# New Doc\n", encoding="utf-8")
    registry.register("projects/demo/docs/old.md", project="demo", registered_by="pytest")

    result = project_docs.sync_project_docs_registry("demo", _entry)

    assert result["registered"] >= 1
    assert result["unregistered"] == 1
    assert registry.get("projects/demo/docs/new.md") is not None
    assert registry.get("projects/demo/docs/old.md") is None


def test_pid_identity_rejects_unrelated_process(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    project_docs._write_pid_record(
        project_docs.supervisor_pid_path(),
        role=project_docs.SUPERVISOR_ROLE,
        pid=os.getpid(),
        token="pytest",
    )

    assert project_docs.read_supervisor_pid() is None


def test_worker_pid_reader_tolerates_fresh_command_probe_miss(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    project_docs._write_pid_record(
        project_docs.worker_pid_path("demo"),
        role=project_docs.WORKER_ROLE,
        pid=os.getpid(),
        token="pytest",
        project="demo",
    )
    monkeypatch.setattr(project_docs, "_process_command", lambda _pid: "")

    assert project_docs.read_worker_pid("demo") == os.getpid()

    record = json.loads(project_docs.worker_pid_path("demo").read_text(encoding="utf-8"))
    record["started_at"] = "2000-01-01T00:00:00Z"
    project_docs.worker_pid_path("demo").write_text(json.dumps(record), encoding="utf-8")

    assert project_docs.read_worker_pid("demo") is None


def test_start_supervisor_reaps_matching_orphans_before_spawn(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    terminated = []
    captured = {}

    class _FakePopen:
        pid = 33333

        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

        def poll(self):
            return None

    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest")
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")
    monkeypatch.setenv("INSTANCE", "claude-code-private-tmp-cc-livetest")
    monkeypatch.setenv("SILO", str(_tmp_path / "instances" / "claude-code-private-tmp-cc-livetest"))
    monkeypatch.setenv("LANE", "cc")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "claude-code")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/cc-livetest")
    monkeypatch.setenv("MEMORY_DB_PATH", str(_tmp_path / "instances" / "openclaw-main" / "data" / "memory.db"))
    monkeypatch.setenv(
        "MEMORY_ARCHIVE_DB_PATH",
        str(_tmp_path / "instances" / "openclaw-main" / "data" / "memory_archive.db"),
    )
    monkeypatch.setattr(project_docs, "read_supervisor_pid", lambda: None)
    monkeypatch.setattr(project_docs, "_matching_supervisor_pids", lambda **_kwargs: [11111, 22222])
    monkeypatch.setattr(project_docs, "_terminate_supervisor_pid", lambda pid, **_kwargs: terminated.append(pid))
    monkeypatch.setattr(project_docs.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(project_docs, "_wait_for_pid", lambda *args, **kwargs: 33333)

    assert project_docs.start_supervisor() == 33333
    assert terminated == [11111, 22222]
    assert captured["env"]["QUAID_HOME"] == str(project_docs.get_quaid_home())
    assert "QUAID_INSTANCE" not in captured["env"]
    assert "INSTANCE" not in captured["env"]
    assert "SILO" not in captured["env"]
    assert "LANE" not in captured["env"]
    assert "QUAID_ADAPTER_TYPE" not in captured["env"]
    assert captured["env"]["QUAID_NOW"] == "2026-03-11T05:06:07Z"
    assert "CLAUDE_PROJECT_DIR" not in captured["env"]
    assert "MEMORY_DB_PATH" not in captured["env"]
    assert "MEMORY_ARCHIVE_DB_PATH" not in captured["env"]
    assert captured["env"]["QUAID_SUPERVISOR_BOOT"] == "1"


def test_ensure_supervisor_alive_raises_previous_failure_when_failhard(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    project_docs.write_supervisor_failure(RuntimeError("supervisor boom"))
    monkeypatch.setattr(project_docs, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        project_docs.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("supervisor should not restart")),
    )

    with pytest.raises(RuntimeError, match="project-docs supervisor previously failed.*supervisor boom"):
        project_docs.ensure_supervisor_alive()

    assert project_docs.read_supervisor_failure() is not None


def test_ensure_supervisor_alive_clears_previous_failure_when_failopen(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    class _FakePopen:
        pid = 33335

        def __init__(self, *_args, **_kwargs):
            pass

        def poll(self):
            return None

    project_docs.write_supervisor_failure(RuntimeError("transient supervisor boom"))
    monkeypatch.setattr(project_docs, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(project_docs, "read_supervisor_pid", lambda: None)
    monkeypatch.setattr(project_docs, "_matching_supervisor_pids", lambda **_kwargs: [])
    monkeypatch.setattr(project_docs.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(project_docs, "_wait_for_pid", lambda *args, **kwargs: 33335)

    assert project_docs.ensure_supervisor_alive() == 33335
    assert project_docs.read_supervisor_failure() is None


def test_start_supervisor_hydrates_anthropic_key_from_shared_auth(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    auth_path = tmp_path / "shared" / "auth" / "credentials.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps({"credentials": {"anthropic_oauth": {"token": "sk-ant-oat01-supervisor"}}}),
        encoding="utf-8",
    )
    captured = {}

    class _FakePopen:
        pid = 33334

        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

        def poll(self):
            return None

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(project_docs, "read_supervisor_pid", lambda: None)
    monkeypatch.setattr(project_docs, "_matching_supervisor_pids", lambda **_kwargs: [])
    monkeypatch.setattr(project_docs.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(project_docs, "_wait_for_pid", lambda *args, **kwargs: 33334)

    assert project_docs.start_supervisor() == 33334
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-ant-oat01-supervisor"


def test_start_worker_strips_inherited_memory_db_overrides(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    captured = {}

    class _FakePopen:
        pid = 44444

        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

        def poll(self):
            return None

    monkeypatch.setenv("MEMORY_DB_PATH", str(_tmp_path / "instances" / "openclaw-main" / "data" / "memory.db"))
    monkeypatch.setenv("INSTANCE", "openclaw-main")
    monkeypatch.setenv("SILO", str(_tmp_path / "instances" / "openclaw-main"))
    monkeypatch.setenv("LANE_UPPER", "OC")
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "openclaw")
    monkeypatch.setenv(
        "MEMORY_ARCHIVE_DB_PATH",
        str(_tmp_path / "instances" / "openclaw-main" / "data" / "memory_archive.db"),
    )
    monkeypatch.setattr(project_docs, "project_is_registered_for_worker", lambda _name: True)
    monkeypatch.setattr(project_docs, "read_worker_pid", lambda _name: None)
    monkeypatch.setattr(project_docs, "read_supervisor_pid", lambda: 12345)
    monkeypatch.setattr(project_docs.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(project_docs, "_wait_for_pid", lambda *args, **kwargs: 44444)

    assert project_docs.start_worker("demo") == 44444
    assert "MEMORY_DB_PATH" not in captured["env"]
    assert "MEMORY_ARCHIVE_DB_PATH" not in captured["env"]
    assert "INSTANCE" not in captured["env"]
    assert "SILO" not in captured["env"]
    assert "LANE_UPPER" not in captured["env"]
    assert "QUAID_ADAPTER_TYPE" not in captured["env"]
    assert captured["env"]["QUAID_SUPERVISOR_PID"] == "12345"
    assert captured["env"]["QUAID_PROJECT_DOCS_WORKER_TOKEN"]


def test_start_worker_preserves_explicit_anthropic_key(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    captured = {}

    class _FakePopen:
        pid = 44445

        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

        def poll(self):
            return None

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-explicit-worker")
    monkeypatch.setattr(project_docs, "project_is_registered_for_worker", lambda _name: True)
    monkeypatch.setattr(project_docs, "read_worker_pid", lambda _name: None)
    monkeypatch.setattr(project_docs, "read_supervisor_pid", lambda: 12345)
    monkeypatch.setattr(project_docs.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(project_docs, "_wait_for_pid", lambda *args, **kwargs: 44445)

    assert project_docs.start_worker("demo") == 44445
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-ant-explicit-worker"


def test_start_worker_hydrates_anthropic_key_from_shared_auth(project_env, monkeypatch):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    auth_path = tmp_path / "shared" / "auth" / "credentials.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(
        json.dumps({"credentials": {"anthropic_api": {"token": "sk-ant-api-worker"}}}),
        encoding="utf-8",
    )
    captured = {}

    class _FakePopen:
        pid = 44446

        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

        def poll(self):
            return None

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(project_docs, "project_is_registered_for_worker", lambda _name: True)
    monkeypatch.setattr(project_docs, "read_worker_pid", lambda _name: None)
    monkeypatch.setattr(project_docs, "read_supervisor_pid", lambda: 12345)
    monkeypatch.setattr(project_docs.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(project_docs, "_wait_for_pid", lambda *args, **kwargs: 44446)

    assert project_docs.start_worker("demo") == 44446
    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-ant-api-worker"


def test_stop_supervisor_kills_pidfile_target_and_matching_orphans(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    project_docs._write_pid_record(
        project_docs.supervisor_pid_path(),
        role=project_docs.SUPERVISOR_ROLE,
        pid=11111,
        token="pytest",
    )

    terminated = []
    monitor_stops = []

    monkeypatch.setattr(project_docs, "_pid_record_matches", lambda record, **_kwargs: True)
    monkeypatch.setattr(project_docs, "_matching_supervisor_pids", lambda **_kwargs: [11111, 22222, 33333])
    monkeypatch.setattr(project_docs, "_terminate_supervisor_pid", lambda pid, **_kwargs: terminated.append(pid))
    monkeypatch.setattr(project_docs, "_worker_dir", lambda: project_docs.supervisor_dir() / "no-workers")

    def _fake_stop_all_instance_monitors():
        monitor_stops.append(True)

    with patch("core.project_docs_supervisor.stop_all_instance_monitors", _fake_stop_all_instance_monitors):
        assert project_docs.stop_supervisor() is True

    assert terminated == [11111, 22222, 33333]
    assert monitor_stops == [True]
    assert not project_docs.supervisor_pid_path().exists()


def test_project_docs_home_resolution_avoids_adapter_bootstrap(project_env):
    tmp_path, _src, _entry = project_env
    from core import project_docs

    assert project_docs.get_quaid_home.__module__ == "core.project_docs"
    assert project_docs.project_docs_root() == tmp_path / "data" / "project-docs"


def test_worker_heartbeat_writes_atomic_json_pid_record(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    project_docs.write_worker_heartbeat("demo", {"status": "idle"})

    pid_data = json.loads(project_docs.worker_pid_path("demo").read_text(encoding="utf-8"))
    assert pid_data["pid"] == os.getpid()
    assert pid_data["role"] == project_docs.WORKER_ROLE
    assert pid_data["project"] == "demo"
    assert pid_data["token"] is None


def test_pid_startup_wait_allows_first_bootstrap_headroom(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    monkeypatch.delenv("QUAID_PROJECT_DOCS_PID_WAIT_SECONDS", raising=False)
    assert project_docs.pid_startup_wait_seconds() == 30.0

    monkeypatch.setenv("QUAID_PROJECT_DOCS_PID_WAIT_SECONDS", "3")
    assert project_docs.pid_startup_wait_seconds() == 5.0

    monkeypatch.setenv("QUAID_PROJECT_DOCS_PID_WAIT_SECONDS", "240")
    assert project_docs.pid_startup_wait_seconds() == 120.0


def test_worker_update_heartbeat_interval_stays_inside_stale_window(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs_worker

    monkeypatch.setenv("QUAID_PROJECT_DOCS_WORKER_STALE_SECONDS", "5")

    assert project_docs_worker._update_heartbeat_interval(30.0) < 5.0
    assert project_docs_worker._update_heartbeat_interval(0.5) == 0.5


def test_stop_worker_tolerates_process_exit_race(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    project_docs._write_pid_record(
        project_docs.worker_pid_path("demo"),
        role=project_docs.WORKER_ROLE,
        pid=12345,
        token="pytest",
        project="demo",
    )
    monkeypatch.setattr(project_docs, "_pid_record_matches", lambda record, **_kwargs: True)
    monkeypatch.setattr(project_docs, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(project_docs, "reap_child_processes", lambda: 0)

    def exited_before_signal(pid, sig):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(project_docs.os, "kill", exited_before_signal)

    assert project_docs.stop_worker("demo") is True
    assert not project_docs.worker_pid_path("demo").exists()


def test_reap_stale_worker_does_not_overwrite_racing_success(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    project_docs.write_state("demo", {"status": "updating", "last_started_at": project_docs.utc_now()})

    monkeypatch.setattr(project_docs, "read_worker_pid", lambda _name: 12345)
    monkeypatch.setattr(project_docs, "_worker_heartbeat_stale", lambda _name, *, stale_after_seconds: True)

    def _stop_worker(_name):
        project_docs.merge_state("demo", {"status": "fresh", "last_error": None, "last_completed_at": project_docs.utc_now()})

    monkeypatch.setattr(project_docs, "stop_worker", _stop_worker)

    assert project_docs.reap_stale_worker("demo", stale_after_seconds=5.0) is True
    state = project_docs.read_state("demo")
    assert state["status"] == "fresh"
    assert state["last_error"] is None


def test_reap_stale_worker_uses_progress_age_even_with_fresh_heartbeat(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    old = (datetime.now(tz=timezone.utc) - timedelta(seconds=120)).isoformat()
    project_docs.write_state(
        "demo",
        {
            "status": "updating",
            "last_started_at": old,
            "last_progress_update": old,
            "progress": {"phase": "update_docs", "updated_at": old},
        },
    )
    project_docs.write_worker_heartbeat("demo", {"status": "updating"})
    stopped = []
    monkeypatch.setattr(project_docs, "read_worker_pid", lambda _name: 12345)
    monkeypatch.setattr(project_docs, "stop_worker", lambda name: stopped.append(name) or True)

    assert project_docs.reap_stale_worker("demo", stale_after_seconds=5.0) is True

    assert stopped == ["demo"]
    state = project_docs.read_state("demo")
    assert state["status"] == "queued"
    assert "stale during update" in state["last_error"]


def test_cleanup_project_state_removes_all_project_artifacts(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs

    paths = [
        project_docs.request_path("demo"),
        project_docs.state_path("demo"),
        project_docs.lock_path("demo"),
        project_docs._spawn_lock_path("worker", "demo"),
        project_docs.worker_pid_path("demo"),
        project_docs.worker_heartbeat_path("demo"),
        project_docs._worker_dir() / "demo.log",
        project_docs._state_dir() / ".demo.json.123.tmp",
        project_docs._worker_dir() / ".demo.heartbeat.json.123.tmp",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    result = project_docs.cleanup_project_state("demo")

    assert result["removed"] >= len(paths)
    assert all(not path.exists() for path in paths)
    assert project_docs.has_project_state("demo") is False


def test_delete_project_removes_project_docs_worker_state(project_env):
    tmp_path, _src, _entry = project_env
    from core import project_docs
    from core.project_registry import delete_project

    project_docs.request_update("demo", reason="manual-test", requested_by="pytest")
    project_docs.write_worker_heartbeat("demo", {"status": "idle"})
    project_docs.lock_path("demo").parent.mkdir(parents=True, exist_ok=True)
    project_docs.lock_path("demo").write_text("lock", encoding="utf-8")
    project_docs._spawn_lock_path("worker", "demo").write_text("spawn-lock", encoding="utf-8")
    (project_docs._worker_dir() / "demo.log").write_text("log", encoding="utf-8")

    with patch("core.project_registry._sync_docs_registry_project"):
        delete_project("demo")

    assert not (tmp_path / "data" / "project-docs" / "requests" / "demo.json").exists()
    assert not (tmp_path / "data" / "project-docs" / "state" / "demo.json").exists()
    assert not (tmp_path / "data" / "project-docs" / "workers" / "demo.heartbeat.json").exists()
    assert not (tmp_path / "data" / "project-docs" / "locks" / "demo.lock").exists()
    assert not (tmp_path / "data" / "project-docs" / "locks" / "demo.worker.spawn.lock").exists()
    assert not (tmp_path / "data" / "project-docs" / "workers" / "demo.log").exists()
    assert project_docs.has_project_state("demo") is False


def test_delete_project_stops_live_project_docs_worker(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from core.project_registry import delete_project

    monkeypatch.setenv("QUAID_PROJECT_DOCS_WORKER_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("QUAID_PROJECT_DOCS_PID_WAIT_SECONDS", "45")
    pid = project_docs.start_worker("demo")
    try:
        assert project_docs.read_worker_pid("demo") == pid

        with patch("core.project_registry._sync_docs_registry_project"):
            delete_project("demo")

        assert project_docs.read_worker_pid("demo") is None
        assert project_docs.has_project_state("demo") is False
    finally:
        try:
            project_docs.stop_worker("demo")
        except Exception:
            pass
        project_docs.cleanup_project_state("demo")


def test_start_worker_deleted_project_does_not_create_spawn_lock(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from core.project_registry import delete_project

    with patch("core.project_registry._sync_docs_registry_project"):
        delete_project("demo")
    spawn_lock = project_docs._spawn_lock_path("worker", "demo")
    spawn_lock.unlink(missing_ok=True)

    with pytest.raises(KeyError):
        project_docs.start_worker("demo")

    assert not spawn_lock.exists()


def test_supervisor_runs_docs_rag_refresh_ticks(project_env, monkeypatch):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from core import project_docs_supervisor

    calls: list[object] = []
    monkeypatch.setattr(project_docs, "write_supervisor_pid", lambda _token: None)
    monkeypatch.setattr(project_docs, "reap_child_processes", lambda: 0)
    monkeypatch.setattr(project_docs, "worker_stale_after_seconds", lambda _interval: 30.0)
    monkeypatch.setattr(project_docs, "reap_stale_worker", lambda _project, *, stale_after_seconds: False)
    monkeypatch.setattr(project_docs, "start_worker", lambda _project: 123)
    monkeypatch.setattr(
        project_docs,
        "auto_register_project_docs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old auto-register helper called")),
    )
    monkeypatch.setattr(
        project_docs,
        "index_one_stale_registered_doc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("old stale-index helper called")),
    )
    monkeypatch.setattr("core.docs.updater.queued_project_log_projects", lambda project=None: [])
    monkeypatch.setattr("core.docs.updater.sync_project_visible_docs", lambda *args, **kwargs: calls.append("sync") or {"registered": 1})
    monkeypatch.setattr("core.docs.updater.index_one_stale_registered_doc", lambda *, project=None: calls.append(("index", project)) or True)
    monkeypatch.setattr(project_docs_supervisor, "_maintain_instance_monitors", lambda _known: None)
    monkeypatch.setattr(project_docs_supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)

    project_docs_supervisor.run_supervisor(once=True, interval_seconds=0.5)

    assert calls == ["sync", ("index", None)]


def test_supervisor_skips_project_deleted_after_project_snapshot(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from core import project_docs_supervisor

    with patch("core.project_docs_supervisor.list_projects", return_value={"demo": {}}), \
         patch("core.project_docs_supervisor._maintain_instance_monitors", lambda _known: None), \
         patch("core.project_docs_supervisor._maintain_janitor_workers", lambda *_args, **_kwargs: None), \
         patch("core.project_docs.auto_register_project_docs", return_value=0), \
         patch("core.project_docs.index_one_stale_registered_doc", return_value=False), \
         patch("core.project_docs.project_is_registered_for_worker", return_value=False), \
         patch("core.project_docs.start_worker") as start_worker:
        assert project_docs_supervisor.run_supervisor(once=True) == 0

    start_worker.assert_not_called()
    assert project_docs.has_project_state("demo") is False


def test_supervisor_removal_path_cleans_full_project_state(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from core import project_docs_supervisor

    project_docs_supervisor._STOP = False
    sleep_calls = 0

    def fake_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            project_docs_supervisor._STOP = True

    def fake_stop_worker(project):
        # stop_worker takes the spawn lock and can create this file even when
        # the project was already deleted. The supervisor removal path must
        # clean the full monitor state, not just heartbeat/pid files.
        path = project_docs._spawn_lock_path("worker", project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("lock", encoding="utf-8")
        return False

    with patch("core.project_docs_supervisor.list_projects", side_effect=[{"demo": {"instances": ["pytest-runner"]}}, {}]), \
         patch("core.project_docs_supervisor._maintain_instance_monitors", lambda _known: None), \
         patch("core.project_docs_supervisor._maintain_janitor_workers", lambda *_args, **_kwargs: None), \
         patch("core.project_docs.project_is_registered_for_worker", return_value=True), \
         patch("core.project_docs.start_worker", return_value=123), \
         patch("core.project_docs.stop_worker", side_effect=fake_stop_worker), \
         patch("core.project_docs.reap_child_processes", return_value=0), \
         patch("core.project_docs.auto_register_project_docs", return_value=0), \
         patch("core.project_docs.index_one_stale_registered_doc", return_value=False), \
         patch("core.project_docs.write_supervisor_pid", lambda _token: None), \
         patch("core.project_docs.clear_supervisor_pid_for_current_process", lambda: None), \
         patch.object(project_docs_supervisor.time, "sleep", fake_sleep):
        assert project_docs_supervisor.run_supervisor(interval_seconds=0.5) == 0

    project_docs_supervisor._STOP = False
    assert project_docs.has_project_state("demo") is False
