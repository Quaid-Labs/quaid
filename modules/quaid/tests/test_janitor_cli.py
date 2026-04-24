from __future__ import annotations

from pathlib import Path


def test_janitor_worker_run_all_once_bypasses_schedule_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    from core import janitor_worker
    from core.lifecycle import janitor

    calls = []

    monkeypatch.setattr(
        janitor,
        "run_task_optimized",
        lambda *, task, dry_run: calls.append((task, dry_run)) or {"success": True},
    )

    assert janitor_worker.run_all_once() == 0
    assert calls == [("all", False)]


def test_janitor_main_routes_all_apply_through_supervisor_request(monkeypatch, tmp_path):
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
        lambda request_id, *, timeout_seconds: calls.append(
            ("wait", {"request_id": request_id, "timeout_seconds": timeout_seconds})
        )
        or {"request_id": request_id, "status": "completed", "errors": []},
    )
    monkeypatch.setattr(janitor, "run_task_optimized", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError()))

    assert janitor.main(["--task", "all", "--apply"]) == 0
    assert calls[0] == ("request", {"instance": None, "reason": "janitor-cli-apply", "requested_by": "janitor-cli"})
    assert calls[1][0] == "wait"
    assert calls[1][1]["request_id"] == "req-1"


def test_janitor_main_routes_all_apply_without_instance_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.delenv("QUAID_ADAPTER_TYPE", raising=False)

    from core import project_docs
    from core.lifecycle import janitor

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
        lambda request_id, *, timeout_seconds: calls.append(
            ("wait", {"request_id": request_id, "timeout_seconds": timeout_seconds})
        )
        or {"request_id": request_id, "status": "completed", "errors": []},
    )

    assert janitor.main(["--task", "all", "--apply"]) == 0
    assert calls[0] == ("request", {"instance": None, "reason": "janitor-cli-apply", "requested_by": "janitor-cli"})
    assert calls[1][0] == "wait"
    assert calls[1][1]["request_id"] == "req-2"


def test_janitor_main_rejects_instance_with_direct_only_flags(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")

    from core.lifecycle import janitor

    rc = janitor.main(["--task", "all", "--apply", "--instance", "alpha", "--time-budget", "1"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "--instance requires the supervisor-owned path" in captured.err
