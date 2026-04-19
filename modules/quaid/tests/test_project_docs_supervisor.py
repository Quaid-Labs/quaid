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
