"""Tests for supervisor-owned project docs update state."""

from __future__ import annotations

import json
import os
from pathlib import Path
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


def test_status_and_diff_report_pending_source_change(project_env):
    _tmp_path, src, _entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('v2')\n", encoding="utf-8")

    status = project_docs.project_status("demo")
    diff = project_docs.project_diff("demo", full=False)

    assert status["status"] == "stale"
    assert status["pending_source_change_count"] >= 1
    assert any(change["path"] == "tool.py" for change in diff["changes"])


def test_execute_update_once_snapshots_applies_indexes_and_advances_cursors(project_env):
    tmp_path, src, entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('v2')\n", encoding="utf-8")
    project_log = Path(entry["canonical_path"]) / "PROJECT.log"
    project_log.write_text("- [2026-04-19T00:00:00] Tool behavior changed\n", encoding="utf-8")
    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    with patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}) as update_docs, \
         patch("core.docs.updater.update_registered_docs", return_value=2) as update_registered:
        result = project_docs.execute_update_once("demo", request=request)

    assert result["status"] == "fresh"
    assert result["indexed_docs"] == 2
    assert result["snapshot"]["commit_hash"]
    update_docs.assert_called_once()
    assert update_docs.call_args.kwargs["force_project"] == "demo"
    assert update_docs.call_args.kwargs["extraction_result"]["project_logs"]["demo"]
    update_registered.assert_called_once_with(project="demo", dry_run=False)
    assert not project_docs.request_path("demo").exists()
    state = project_docs.read_state("demo")
    assert state["status"] == "fresh"
    assert state["project_log_offset"] == project_log.stat().st_size
    assert state["last_indexed_docs"] == 2


def test_delete_project_removes_project_docs_worker_state(project_env):
    tmp_path, _src, _entry = project_env
    from core import project_docs
    from core.project_registry import delete_project

    project_docs.request_update("demo", reason="manual-test", requested_by="pytest")
    project_docs.write_worker_heartbeat("demo", {"status": "idle"})

    with patch("core.project_registry._sync_docs_registry_project"):
        delete_project("demo")

    assert not (tmp_path / "data" / "project-docs" / "requests" / "demo.json").exists()
    assert not (tmp_path / "data" / "project-docs" / "state" / "demo.json").exists()
    assert not (tmp_path / "data" / "project-docs" / "workers" / "demo.heartbeat.json").exists()
