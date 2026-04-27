"""Tests for the root Quaid supervisor loop."""

from __future__ import annotations

import subprocess

import pytest


def _mark_deleted_misc_project(tmp_path, instance_id: str) -> None:
    from core.project_registry import _load_registry, _save_registry

    registry = _load_registry(quaid_home=tmp_path)
    registry.setdefault("deleted_projects", {})[f"misc--{instance_id}"] = "2026-04-24T00:00:00+00:00"
    _save_registry(registry, quaid_home=tmp_path)


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


def test_supervisor_boot_mode_skips_docs_ticks_and_uses_raw_project_listing(monkeypatch):
    from core import project_docs_supervisor as supervisor

    calls: list[str] = []

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
    monkeypatch.setattr(supervisor, "list_projects_raw", lambda: calls.append("raw") or {"demo": {}})

    assert supervisor.run_supervisor(once=True, interval_seconds=0.5) == 0
    assert calls == ["raw"]


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
