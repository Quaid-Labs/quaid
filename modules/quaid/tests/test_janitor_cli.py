from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace


def _fresh_import_janitor():
    for name in (
        "core.lifecycle.janitor",
        "core.lifecycle.datastore_runtime",
        "datastore.memorydb.maintenance_ops",
    ):
        sys.modules.pop(name, None)
    return importlib.import_module("core.lifecycle.janitor")


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
        lambda request_id, *, timeout_seconds: calls.append(
            ("wait", {"request_id": request_id, "timeout_seconds": timeout_seconds})
        )
        or {"request_id": request_id, "status": "completed", "errors": []},
    )

    assert janitor.main(["--task", "all", "--apply"]) == 0
    assert calls[0] == ("request", {"instance": None, "reason": "janitor-cli-apply", "requested_by": "janitor-cli"})
    assert calls[1][0] == "wait"
    assert calls[1][1]["request_id"] == "req-2"


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
