"""Tests for supervisor-owned project docs update state."""

from __future__ import annotations

import json
import logging
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


def test_execute_update_once_snapshots_applies_indexes_and_advances_cursors(project_env):
    tmp_path, src, entry = project_env
    from core import project_docs

    (src / "tool.py").write_text("print('v2')\n", encoding="utf-8")
    project_log = Path(entry["canonical_path"]) / "PROJECT.log"
    project_log.write_text("- [2026-04-19T00:00:00] Tool behavior changed\n", encoding="utf-8")
    request = project_docs.request_update("demo", reason="manual-test", requested_by="pytest")

    with patch("core.docs_updater_hook.update_project_docs", return_value={"projects_checked": 1, "docs_updated": 1, "docs_skipped": 0, "trivial_skipped": 0, "errors": 0}) as update_docs, \
         patch("core.docs.updater.update_registered_docs", return_value=2) as update_registered, \
         patch("core.docs.updater.index_project_logs", return_value=1) as index_project_logs:
        result = project_docs.execute_update_once("demo", request=request)

    assert result["status"] == "fresh"
    assert result["indexed_docs"] == 2
    assert result["indexed_project_logs"] == 1
    assert result["snapshot"]["commit_hash"]
    update_docs.assert_called_once()
    assert update_docs.call_args.kwargs["force_project"] == "demo"
    assert update_docs.call_args.kwargs["extraction_result"]["project_logs"]["demo"]
    update_registered.assert_called_once_with(project="demo", dry_run=False, protected_names={"PROJECT.log"})
    index_project_logs.assert_called_once_with(project="demo")
    assert not project_docs.request_path("demo").exists()
    state = project_docs.read_state("demo")
    assert state["status"] == "fresh"
    assert state["phase"] == "idle"
    assert state["progress"]["message"] == "project-docs update complete"
    assert state["project_log_offset"] == project_log.stat().st_size
    assert state["last_indexed_docs"] == 2
    assert state["last_indexed_project_logs"] == 1
    assert state["last_registry_sync"]["project_md_refreshed"] in (0, 1)


def test_project_status_reports_pending_project_log_queue(project_env):
    _tmp_path, _src, _entry = project_env
    from core import project_docs
    from datastore.docsdb import project_log_queue

    metrics = project_log_queue.enqueue_project_logs(
        {"demo": ["Queued project log milestone"]},
        trigger="Reset",
    )

    assert metrics["entries_queued"] == 1
    status = project_docs.project_status("demo")
    assert status["status"] == "stale"
    assert status["fresh"] is False
    assert status["project_log_queue_pending"] == 1


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
    update_registered.assert_called_once_with(project="demo", dry_run=False, protected_names={"PROJECT.log"})
    index_project_logs.assert_called_once_with(project="demo")


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

    def should_not_discover_projects():
        raise AssertionError("PROJECT.log discovery should fail closed when scope is unresolved")

    monkeypatch.setattr(updater, "_linked_projects_for_current_instance", lambda: (set(), False))
    monkeypatch.setattr(updater, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr("core.project_registry.list_projects", should_not_discover_projects)
    caplog.set_level(logging.WARNING, logger="core.docs.updater")

    assert updater.index_project_logs() == 0
    assert updater.index_project_logs(project="demo") == 0
    assert "cross-instance contamination" in caplog.text


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
        "core.project_registry.get_project",
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
        "core.project_registry.get_project",
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

    calls: list[str] = []
    monkeypatch.setattr(project_docs, "write_supervisor_pid", lambda _token: None)
    monkeypatch.setattr(project_docs, "reap_child_processes", lambda: 0)
    monkeypatch.setattr(project_docs, "worker_stale_after_seconds", lambda _interval: 30.0)
    monkeypatch.setattr(project_docs, "reap_stale_worker", lambda _project, *, stale_after_seconds: False)
    monkeypatch.setattr(project_docs, "start_worker", lambda _project: 123)
    monkeypatch.setattr(project_docs, "auto_register_project_docs", lambda: calls.append("register") or 1)
    monkeypatch.setattr(project_docs, "index_one_stale_registered_doc", lambda: calls.append("index") or True)
    monkeypatch.setattr(project_docs_supervisor, "_maintain_instance_monitors", lambda _known: None)
    monkeypatch.setattr(project_docs_supervisor, "_maintain_janitor_workers", lambda *_args, **_kwargs: None)

    project_docs_supervisor.run_supervisor(once=True, interval_seconds=0.5)

    assert calls == ["register", "index"]


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

    with patch("core.project_docs_supervisor.list_projects", side_effect=[{"demo": {}}, {}]), \
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
