"""Tests for the root Quaid supervisor loop."""

from __future__ import annotations

import subprocess


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
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(supervisor, "_start_instance_monitor", lambda name: started_instances.append(name) or 100)
    monkeypatch.setattr(supervisor, "_start_janitor_worker", lambda name: started_janitors.append(name) or _DoneProc())
    monkeypatch.setattr(supervisor, "_janitor_check_interval_seconds", lambda: 0.5)
    monkeypatch.setattr(supervisor, "_interval_from_env", lambda _name, default: default)

    assert supervisor.run_supervisor(once=True, interval_seconds=0.5) == 0
    assert started_instances == ["alpha", "beta"]
    assert started_janitors == ["alpha", "beta"]


def test_supervisor_stops_removed_instance_monitor(monkeypatch):
    from core import project_docs_supervisor as supervisor

    stopped = []
    known = {"old": 123}

    monkeypatch.setattr(supervisor, "list_instances", lambda: [])
    monkeypatch.setattr(supervisor, "_stop_instance_monitor", lambda name: stopped.append(name) or True)

    supervisor._maintain_instance_monitors(known)

    assert stopped == ["old"]
    assert known == {}


def test_start_instance_monitor_strips_inherited_memory_db_overrides(monkeypatch, tmp_path):
    from core import project_docs_supervisor as supervisor

    captured = {}

    class _FakePopen:
        pid = 12345

        def __init__(self, *_args, **kwargs):
            captured["env"] = dict(kwargs.get("env") or {})

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "instances" / "openclaw-main" / "data" / "memory.db"))
    monkeypatch.setenv(
        "MEMORY_ARCHIVE_DB_PATH",
        str(tmp_path / "instances" / "openclaw-main" / "data" / "memory_archive.db"),
    )
    monkeypatch.setattr(supervisor, "quaid_home", lambda: tmp_path)
    monkeypatch.setattr(supervisor, "_read_instance_daemon_pid", lambda _name: None)
    monkeypatch.setattr(supervisor, "_wait_for_instance_pid", lambda _name, pid, **_kwargs: pid)
    monkeypatch.setattr(supervisor.subprocess, "Popen", _FakePopen)

    assert supervisor._start_instance_monitor("codex-private-tmp-cdx-livetest") == 12345

    env = captured["env"]
    assert "MEMORY_DB_PATH" not in env
    assert "MEMORY_ARCHIVE_DB_PATH" not in env
    assert env["QUAID_HOME"] == str(tmp_path)
    assert env["QUAID_INSTANCE"] == "codex-private-tmp-cdx-livetest"
    assert env["QUAID_DAEMON"] == "1"


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


def test_janitor_worker_throttles_per_instance(monkeypatch):
    from core import project_docs_supervisor as supervisor

    starts = []

    class _RunningProc:
        def poll(self):
            return None

    monkeypatch.setattr(supervisor, "list_instances", lambda: ["alpha"])
    monkeypatch.setattr(supervisor, "_start_janitor_worker", lambda name: starts.append(name) or _RunningProc())

    workers: dict[str, subprocess.Popen] = {}
    checks: dict[str, float] = {}
    supervisor._maintain_janitor_workers(workers, checks, now=100.0, check_interval=10.0)
    supervisor._maintain_janitor_workers(workers, checks, now=105.0, check_interval=10.0)

    assert starts == ["alpha"]
    assert "alpha" in workers
