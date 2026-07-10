"""Tests for core/project_registry.py — project registry CRUD."""

import json
import logging
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.project_registry import (
    create_project,
    delete_project,
    get_project,
    is_misc_project_deleted,
    link_project,
    list_projects,
    unlink_project,
    update_project,
    projects_with_source_root,
    snapshot_all_projects,
    _load_registry,
    _save_registry,
    _registry_lock_path,
    _registry_path,
)


@pytest.fixture
def mock_adapter(tmp_path):
    """Set up a mock adapter with tmp_path as quaid_home."""
    adapter = MagicMock()
    adapter.quaid_home.return_value = tmp_path
    adapter.instance_root.return_value = tmp_path / "test-instance"
    adapter.adapter_id.return_value = "test-adapter"

    with patch.dict(
        os.environ,
        {
            "QUAID_HOME": str(tmp_path),
            "QUAID_VISIBLE_HOME": str(tmp_path.with_name(tmp_path.name.lstrip("."))),
        },
        clear=False,
    ), patch("lib.adapter.get_adapter", return_value=adapter):
        yield adapter, tmp_path


class TestRegistryIO:
    def test_load_empty(self, mock_adapter):
        _, tmp_path = mock_adapter
        result = _load_registry()
        assert result == {"projects": {}, "deleted_projects": {}}

    def test_save_and_load(self, mock_adapter):
        _, tmp_path = mock_adapter
        data = {"projects": {"test": {"description": "hello"}}}
        _save_registry(data)

        loaded = _load_registry()
        assert loaded["projects"]["test"]["description"] == "hello"

    def test_load_corrupt_file(self, mock_adapter):
        _, tmp_path = mock_adapter
        reg = tmp_path / "project-registry.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("not valid json{{{")
        with patch("core.project_registry._fail_hard_enabled", return_value=False):
            result = _load_registry()
        assert result == {"projects": {}, "deleted_projects": {}}

    def test_load_corrupt_file_raises_under_failhard(self, mock_adapter):
        _, tmp_path = mock_adapter
        reg = tmp_path / "project-registry.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("not valid json{{{")

        with patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="Failed to read project registry"):
            _load_registry()

    def test_load_registry_skips_invalid_project_names_when_failsoft(self, mock_adapter):
        _adapter, _tmp_path = mock_adapter
        _save_registry(
            {
                "projects": {"demo": {"description": "ok"}, "../../escape": {"description": "bad"}},
                "deleted_projects": {"old-demo": "2026-06-13", "*.json": "2026-06-13"},
            }
        )

        with patch("core.project_registry._fail_hard_enabled", return_value=False):
            loaded = _load_registry()

        assert set(loaded["projects"]) == {"demo"}
        assert set(loaded["deleted_projects"]) == {"old-demo"}

    def test_load_registry_rejects_invalid_project_names_under_failhard(self, mock_adapter):
        _adapter, _tmp_path = mock_adapter
        _save_registry({"projects": {"../../escape": {"description": "bad"}}})

        with patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="Invalid project name in registry"):
            _load_registry()

    def test_registry_lock_path_uses_stable_sidecar(self, mock_adapter):
        _, tmp_path = mock_adapter
        assert _registry_lock_path() == tmp_path / "project-registry.json.lock"

    def test_temp_canonical_path_resolution_failure_logs(self, caplog):
        from core import project_registry as registry_mod

        class _BadPath:
            def expanduser(self):
                raise RuntimeError("path broken")

            def __str__(self):
                return "<bad-path>"

        with caplog.at_level(logging.DEBUG, logger="core.project_registry"):
            assert registry_mod._is_temp_canonical_path(_BadPath()) is False

        assert "Failed to classify temp project path" in caplog.text
        assert "path broken" in caplog.text

    def test_registry_lock_releases_thread_mutex_while_waiting_for_flock(self, mock_adapter, monkeypatch):
        from lib import project_registry_lock

        events = []

        class _FakeLock:
            def acquire(self, **_kwargs):
                events.append("thread-acquire")
                return True

            def release(self):
                events.append("thread-release")

        attempts = 0

        def _flock(_fd, flags):
            nonlocal attempts
            if flags & project_registry_lock.fcntl.LOCK_UN:
                events.append("flock-unlock")
                return
            attempts += 1
            events.append(f"flock-{attempts}")
            if attempts == 1:
                raise BlockingIOError(project_registry_lock.errno.EAGAIN, "busy")

        monkeypatch.setattr(project_registry_lock, "_registry_thread_lock", _FakeLock())
        monkeypatch.setattr(project_registry_lock.fcntl, "flock", _flock)
        monkeypatch.setattr(project_registry_lock.time, "sleep", lambda seconds: events.append(f"sleep-{seconds}"))

        with project_registry_lock.registry_lock():
            events.append("yield")

        assert events == [
            "thread-acquire",
            "flock-1",
            "thread-release",
            "sleep-0.05",
            "thread-acquire",
            "flock-2",
            "yield",
            "flock-unlock",
            "thread-release",
        ]

    def test_registry_lock_times_out_waiting_for_flock(self, mock_adapter, monkeypatch):
        from lib import project_registry_lock

        events = []

        class _FakeLock:
            def acquire(self, **_kwargs):
                events.append("thread-acquire")
                return True

            def release(self):
                events.append("thread-release")

        def _flock(_fd, flags):
            if flags & project_registry_lock.fcntl.LOCK_UN:
                events.append("flock-unlock")
                return
            events.append("flock-busy")
            raise BlockingIOError(project_registry_lock.errno.EAGAIN, "busy")

        monotonic_values = iter([0.0, 0.0, 31.0])
        monkeypatch.setattr(project_registry_lock, "_registry_thread_lock", _FakeLock())
        monkeypatch.setattr(project_registry_lock.fcntl, "flock", _flock)
        monkeypatch.setattr(project_registry_lock.time, "monotonic", lambda: next(monotonic_values))
        monkeypatch.setattr(project_registry_lock.time, "sleep", lambda seconds: events.append(f"sleep-{seconds}"))

        with pytest.raises(TimeoutError, match="Timed out waiting for project registry lock"):
            with project_registry_lock.registry_lock():
                pytest.fail("registry lock should time out")

        assert events == [
            "thread-acquire",
            "flock-busy",
            "thread-release",
            "sleep-0.05",
        ]


class TestCreateProject:
    def test_creates_project(self, mock_adapter, monkeypatch):
        adapter, tmp_path = mock_adapter
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:00:00Z")
        with patch("core.project_registry._sync_docs_registry_project") as sync_docs:
            entry = create_project("my-app", description="Test app")

        assert entry["description"] == "Test app"
        assert entry["source_root"] is None
        assert entry["created_at"] == "2026-03-11T05:00:00+00:00"
        # instances contains instance_id() (from QUAID_INSTANCE env), not adapter_id()
        assert len(entry["instances"]) >= 1

        # Canonical dir created
        canonical = tmp_path / "projects" / "my-app"
        assert canonical.is_dir()
        assert (canonical / "docs").is_dir()
        assert (canonical / "PROJECT.md").is_file()
        project_md = (canonical / "PROJECT.md").read_text()
        assert "## What This Is" in project_md
        assert "## Primary Artifacts" in project_md
        assert str(canonical) in project_md

        # In registry
        assert get_project("my-app")["created_at"] == "2026-03-11T05:00:00+00:00"
        sync_docs.assert_called_once()

    def test_create_rejects_malformed_quaid_now(self, mock_adapter, monkeypatch):
        monkeypatch.setenv("QUAID_NOW", "not-a-date")

        with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            create_project("my-app")

    def test_normalizes_project_name_to_lowercase(self, mock_adapter):
        _, tmp_path = mock_adapter
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("My-App")

        assert (tmp_path / "projects" / "my-app").is_dir()
        assert get_project("MY-APP") is not None

    def test_accepts_unicode_project_names(self, mock_adapter):
        _, tmp_path = mock_adapter
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("Man\u0303ana-App")
            create_project("研究-資料")

        assert (tmp_path / "projects" / "mañana-app").is_dir()
        assert get_project("MAÑANA-APP") is not None
        assert (tmp_path / "projects" / "研究-資料").is_dir()
        assert get_project("研究-資料") is not None

    def test_rejects_duplicate(self, mock_adapter):
        create_project("my-app")
        with pytest.raises(ValueError, match="already exists"):
            create_project("my-app")

    def test_rejects_duplicate_after_lowercase_normalization(self, mock_adapter):
        create_project("My-App")
        with pytest.raises(ValueError, match="already exists"):
            create_project("my-app")

    def test_allows_unscoped_create_when_instance_env_missing(self, mock_adapter, monkeypatch):
        _, _tmp_path = mock_adapter
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)

        with patch("core.project_registry._sync_docs_registry_project"):
            entry = create_project("ambient-app", description="Ambient project")

        assert entry["instances"] == []

    def test_with_source_root(self, mock_adapter):
        _, tmp_path = mock_adapter
        src = tmp_path / "user-code"
        src.mkdir()
        (src / "main.py").write_text("print('hi')")

        with patch("core.project_registry._sync_docs_registry_project"):
            entry = create_project("my-app", source_root=str(src))
        assert entry["source_root"] == str(src)

        # Shadow git should be initialized
        tracking = tmp_path / ".git-tracking" / "my-app"
        assert tracking.is_dir()

    def test_create_shadow_git_failure_raises_when_failhard(self, mock_adapter):
        _, tmp_path = mock_adapter
        src = tmp_path / "user-code"
        src.mkdir()

        with patch("core.shadow_git.ShadowGit.init", side_effect=RuntimeError("shadow broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="shadow broken"):
            create_project("my-app", source_root=str(src))

    def test_create_docs_sync_failure_raises_when_failhard(self, mock_adapter):
        with patch("core.project_registry._sync_docs_registry_project", side_effect=RuntimeError("sync broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="sync broken"):
            create_project("my-app")


class TestUpdateProject:
    def test_updates_fields(self, mock_adapter):
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("my-app", description="v1")
        with patch("core.project_registry._sync_docs_registry_project") as sync_docs:
            updated = update_project("my-app", description="v2")
        assert updated["description"] == "v2"
        sync_docs.assert_called_once()

    def test_update_docs_sync_failure_raises_when_failhard(self, mock_adapter):
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("my-app", description="v1")

        with patch("core.project_registry._sync_docs_registry_project", side_effect=RuntimeError("sync broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="sync broken"):
            update_project("my-app", description="v2")

    def test_rejects_unknown_project(self, mock_adapter):
        with pytest.raises(KeyError):
            update_project("nonexistent", description="nope")

    def test_ignores_disallowed_fields(self, mock_adapter):
        create_project("my-app")
        updated = update_project("my-app", canonical_path="/evil", description="ok")
        # canonical_path should not be changed
        assert "evil" not in updated.get("canonical_path", "")
        assert updated["description"] == "ok"


class TestDeleteProject:
    def test_deletes_project(self, mock_adapter, monkeypatch):
        _, tmp_path = mock_adapter
        create_project("my-app")
        assert get_project("my-app") is not None

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:03:00Z")
        delete_project("my-app")
        assert get_project("my-app") is None
        assert not (tmp_path / "projects" / "my-app").exists()
        data = _load_registry()
        assert data["deleted_projects"]["my-app"] == "2026-03-11T05:03:00+00:00"

    def test_delete_raises_under_failhard_when_project_directory_remains(self, mock_adapter):
        _, tmp_path = mock_adapter
        create_project("my-app")

        with patch("core.project_registry._safe_remove_project_dir", return_value=False), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             patch("core.project_registry.time.sleep"), \
             pytest.raises(RuntimeError, match="project_dirs_present=True"):
            delete_project("my-app")

        assert (tmp_path / "projects" / "my-app").exists()

    def test_delete_shadow_git_failure_raises_when_failhard(self, mock_adapter):
        _, tmp_path = mock_adapter
        src = tmp_path / "user-code"
        src.mkdir()
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("my-app", source_root=str(src))

        with patch("core.shadow_git.ShadowGit.destroy", side_effect=RuntimeError("destroy broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="destroy broken"):
            delete_project("my-app")

    def test_delete_project_dir_failure_raises_when_failhard(self, mock_adapter):
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("my-app")

        with patch("core.project_registry._safe_remove_project_dir", side_effect=RuntimeError("remove broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="remove broken"):
            delete_project("my-app")

    def test_delete_project_docs_cleanup_failure_raises_when_failhard(self, mock_adapter):
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("my-app")

        with patch("core.project_docs.cleanup_project_state", side_effect=RuntimeError("worker broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="worker broken"):
            delete_project("my-app")

    def test_delete_docs_db_cleanup_failure_raises_when_failhard(self, mock_adapter):
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("my-app")

        with patch("core.project_registry._delete_docs_db_project_rows", side_effect=RuntimeError("db broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="db broken"):
            delete_project("my-app")

    def test_rejects_unknown(self, mock_adapter):
        with pytest.raises(KeyError):
            delete_project("nonexistent")

    def test_cleans_up_shadow_git(self, mock_adapter):
        _, tmp_path = mock_adapter
        src = tmp_path / "user-code"
        src.mkdir()
        (src / "a.py").write_text("code")

        create_project("my-app", source_root=str(src))
        tracking = tmp_path / ".git-tracking" / "my-app"
        assert tracking.is_dir()

        delete_project("my-app")
        assert not tracking.exists()

        # User's files untouched
        assert (src / "a.py").is_file()

    def test_cleans_pending_project_review_entries(self, mock_adapter):
        _, tmp_path = mock_adapter
        create_project("my-app")

        queue_path = tmp_path / "instances" / "test-instance" / "logs" / "janitor" / "pending-project-review.json"
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            json.dumps(
                [
                    {
                        "section": "App Notes",
                        "project_hint": "my-app",
                        "source_file": str(tmp_path / "projects" / "my-app" / "README.md"),
                    },
                    {
                        "section": "Other Notes",
                        "project_hint": "other-app",
                        "source_file": str(tmp_path / "projects" / "other-app" / "README.md"),
                    },
                ]
            )
        )

        delete_project("my-app")

        kept = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(kept) == 1
        assert kept[0]["project_hint"] == "other-app"

    def test_cleans_staged_project_events(self, mock_adapter):
        _, tmp_path = mock_adapter
        create_project("my-app")

        visible_staging = tmp_path.with_name(tmp_path.name.lstrip(".")) / "projects" / "staging"
        failed_staging = visible_staging / "failed"
        visible_staging.mkdir(parents=True, exist_ok=True)
        failed_staging.mkdir(parents=True, exist_ok=True)

        target_event = {
            "project_hint": "my-app",
            "files_touched": [str(tmp_path.with_name(tmp_path.name.lstrip(".")) / "projects" / "my-app" / "PROJECT.md")],
            "summary": "target",
        }
        keep_event = {
            "project_hint": "other-app",
            "files_touched": [str(tmp_path.with_name(tmp_path.name.lstrip(".")) / "projects" / "other-app" / "PROJECT.md")],
            "summary": "keep",
        }
        (visible_staging / "1-compact.json").write_text(json.dumps(target_event), encoding="utf-8")
        (failed_staging / "2-compact.json").write_text(json.dumps(target_event), encoding="utf-8")
        (visible_staging / "3-compact.json").write_text(json.dumps(keep_event), encoding="utf-8")

        delete_project("my-app")

        assert not (visible_staging / "1-compact.json").exists()
        assert not (failed_staging / "2-compact.json").exists()
        assert (visible_staging / "3-compact.json").exists()

    def test_misc_deleted_state_reflects_registry_marker(self, mock_adapter):
        _, tmp_path = mock_adapter
        instance_id = "claude-code-private-tmp-m13"
        _save_registry(
            {
                "projects": {},
                "deleted_projects": {f"misc--{instance_id}": "2026-04-24T00:00:00+00:00"},
            },
            quaid_home=tmp_path,
        )

        assert is_misc_project_deleted(instance_id, quaid_home=tmp_path) is True

    def test_recreate_misc_project_clears_deleted_registry_state(self, mock_adapter):
        _, tmp_path = mock_adapter
        instance_id = "claude-code-private-tmp-m13"
        _save_registry(
            {
                "projects": {},
                "deleted_projects": {f"misc--{instance_id}": "2026-04-24T00:00:00+00:00"},
            },
            quaid_home=tmp_path,
        )
        assert is_misc_project_deleted(instance_id, quaid_home=tmp_path) is True

        with patch("lib.instance.instance_id", return_value=instance_id), \
             patch("core.project_registry._sync_docs_registry_project"):
            create_project(f"misc--{instance_id}", description="scratch", initial_instance=instance_id)

        assert is_misc_project_deleted(instance_id, quaid_home=tmp_path) is False
        assert get_project(f"misc--{instance_id}") is not None

    def test_is_misc_project_deleted_reads_target_quaid_home(self, mock_adapter):
        _, tmp_path = mock_adapter
        instance_id = "claude-code-private-tmp-m13"
        other_home = tmp_path.with_name(f"{tmp_path.name}-other")
        _save_registry(
            {
                "projects": {},
                "deleted_projects": {f"misc--{instance_id}": "2026-04-24T00:00:00+00:00"},
            },
            quaid_home=other_home,
        )

        assert is_misc_project_deleted(instance_id, quaid_home=other_home) is True
        assert is_misc_project_deleted(instance_id, quaid_home=tmp_path) is False


class TestListAndQuery:
    def test_list_projects(self, mock_adapter):
        create_project("app-a")
        create_project("app-b")
        projects = list_projects()
        assert "app-a" in projects
        assert "app-b" in projects

    def test_projects_with_source_root(self, mock_adapter):
        _, tmp_path = mock_adapter
        src = tmp_path / "code"
        src.mkdir()

        create_project("tracked", source_root=str(src))
        create_project("untracked")

        with_root = projects_with_source_root()
        assert len(with_root) == 1
        assert with_root[0]["name"] == "tracked"


class TestSnapshotAllProjects:
    def test_snapshots_tracked_projects(self, mock_adapter):
        _, tmp_path = mock_adapter
        src = tmp_path / "code"
        src.mkdir()
        (src / "main.py").write_text("v1")

        create_project("my-app", source_root=str(src))

        # Modify a file
        (src / "main.py").write_text("v2")

        results = snapshot_all_projects()
        assert len(results) == 1
        assert results[0]["project"] == "my-app"
        assert any(c["path"] == "main.py" for c in results[0]["changes"])

    def test_skips_missing_source_root(self, mock_adapter):
        _, tmp_path = mock_adapter
        create_project("orphan", source_root="/nonexistent/path")
        results = snapshot_all_projects()
        assert results == []

    def test_no_changes_returns_empty(self, mock_adapter):
        _, tmp_path = mock_adapter
        src = tmp_path / "code"
        src.mkdir()
        (src / "main.py").write_text("static")

        create_project("my-app", source_root=str(src))
        # Initial snapshot already taken by create_project

        results = snapshot_all_projects()
        assert results == []


class TestCreateProjectUsesInstanceId:
    def test_instances_list_records_instance_id(self, mock_adapter):
        """create_project() uses lib.instance.instance_id(), not adapter.adapter_id().

        The instances list should contain the value returned by instance_id(),
        not whatever adapter_id() returns.
        """
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="my-instance-abc"):
            entry = create_project("my-app", description="Test")

        assert "my-instance-abc" in entry["instances"]
        # adapter_id should NOT appear — it is not the source of the instance token
        assert "test-adapter" not in entry["instances"]

    def test_instances_list_not_empty(self, mock_adapter):
        """instances list must have at least one entry after project creation."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="env-instance-xyz"):
            entry = create_project("my-app")

        assert len(entry["instances"]) >= 1


class TestLinkProject:
    def test_link_adds_current_instance(self, mock_adapter, monkeypatch):
        """link_project() adds the current instance ID to the instances list."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:01:00Z")
        with patch("lib.instance.instance_id", return_value="second-instance"):
            entry = link_project("my-app")

        assert "second-instance" in entry["instances"]
        assert entry["updated_at"] == "2026-03-11T05:01:00+00:00"

    def test_link_is_idempotent(self, mock_adapter):
        """Calling link_project() twice for the same instance does not duplicate it."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        with patch("lib.instance.instance_id", return_value="second-instance"):
            link_project("my-app")
            entry = link_project("my-app")

        assert entry["instances"].count("second-instance") == 1

    def test_link_rejects_unknown_project(self, mock_adapter):
        with patch("lib.instance.instance_id", return_value="some-instance"):
            with pytest.raises(KeyError):
                link_project("nonexistent")

    def test_link_persists_to_registry(self, mock_adapter):
        """Linked instance survives a registry reload."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        with patch("lib.instance.instance_id", return_value="linker-instance"):
            link_project("my-app")

        loaded = get_project("my-app")
        assert "linker-instance" in loaded["instances"]

    def test_link_accepts_explicit_instance_id(self, mock_adapter):
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        entry = link_project("my-app", instance_id="bootstrap-instance")

        assert "bootstrap-instance" in entry["instances"]


class TestUnlinkProject:
    def test_unlink_removes_current_instance(self, mock_adapter, monkeypatch):
        """unlink_project() removes the current instance from the instances list."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        with patch("lib.instance.instance_id", return_value="second-instance"):
            link_project("my-app")

        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:02:00Z")
        with patch("lib.instance.instance_id", return_value="second-instance"):
            entry = unlink_project("my-app")

        assert "second-instance" not in entry["instances"]
        assert entry["updated_at"] == "2026-03-11T05:02:00+00:00"
        # creator should still be present
        assert "creator-instance" in entry["instances"]

    def test_unlink_preserves_sibling_instance(self, mock_adapter):
        """Unlinking the caller must not remove other linked instances."""
        _, tmp_path = mock_adapter
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        project_rules = rules_dir / "quaid-my-app-project-catalog.md"
        project_rules.write_text("shared project catalog", encoding="utf-8")

        with patch("lib.instance.instance_id", return_value="codex-main"):
            create_project("my-app")
        with patch("lib.instance.instance_id", return_value="codex-silo2"):
            link_project("my-app")

        with patch("lib.instance.instance_id", return_value="codex-main"):
            entry = unlink_project("my-app")

        assert entry["instances"] == ["codex-silo2"]
        assert get_project("my-app")["instances"] == ["codex-silo2"]
        assert project_rules.is_file()

    def test_unlink_is_idempotent(self, mock_adapter):
        """Calling unlink_project() when already unlinked does not raise."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        # "other-instance" was never linked — second call should not raise
        with patch("lib.instance.instance_id", return_value="other-instance"):
            entry = unlink_project("my-app")
            entry2 = unlink_project("my-app")

        assert "other-instance" not in entry["instances"]
        assert "other-instance" not in entry2["instances"]

    def test_unlink_rejects_unknown_project(self, mock_adapter):
        with patch("lib.instance.instance_id", return_value="some-instance"):
            with pytest.raises(KeyError):
                unlink_project("nonexistent")

    def test_unlink_persists_to_registry(self, mock_adapter):
        """Unlinked state survives a registry reload."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        with patch("lib.instance.instance_id", return_value="drop-instance"):
            link_project("my-app")
            unlink_project("my-app")

        loaded = get_project("my-app")
        assert "drop-instance" not in loaded["instances"]

    def test_unlink_prune_failure_does_not_mutate_registry(self, mock_adapter, monkeypatch):
        """failHard side-effect failures must not leave a partial unlink."""
        _, tmp_path = mock_adapter
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        project_rules = rules_dir / "quaid-my-app-project-catalog.md"
        project_rules.write_text("stale my-app catalog", encoding="utf-8")

        with patch("lib.instance.instance_id", return_value="drop-instance"):
            create_project("my-app")

        monkeypatch.chdir(tmp_path)
        with patch("lib.instance.instance_id", return_value="drop-instance"), \
             patch("pathlib.Path.unlink", side_effect=OSError("rules prune failed")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(OSError, match="rules prune failed"):
            unlink_project("my-app")

        loaded = get_project("my-app")
        assert loaded["instances"] == ["drop-instance"]

    def test_unlink_prunes_project_cached_rules_files(self, mock_adapter, monkeypatch):
        _, tmp_path = mock_adapter
        rules_dir = tmp_path / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        stale_project_rules = rules_dir / "quaid-my-app-project-catalog.md"
        stale_project_rules.write_text("stale my-app catalog", encoding="utf-8")
        other_project_rules = rules_dir / "quaid-other-project-catalog.md"
        other_project_rules.write_text("other catalog", encoding="utf-8")
        legacy_combined = rules_dir / "quaid-projects.md.bak"
        legacy_combined.write_text("legacy backup", encoding="utf-8")

        with patch("lib.instance.instance_id", return_value="drop-instance"):
            create_project("my-app")

        monkeypatch.chdir(tmp_path)
        with patch("lib.instance.instance_id", return_value="drop-instance"):
            unlink_project("my-app")

        assert not stale_project_rules.exists()
        assert other_project_rules.is_file()
        assert legacy_combined.is_file()

    def test_cached_rules_adapter_failure_warns_when_fail_open(self, mock_adapter, monkeypatch, caplog):
        from core import project_registry as registry_mod

        _, tmp_path = mock_adapter
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("QUAID_RULES_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)

        with patch("lib.adapter.get_adapter", side_effect=RuntimeError("adapter broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=False), \
             caplog.at_level(logging.WARNING, logger="core.project_registry"):
            dirs = registry_mod._current_cached_rules_dirs()

        assert dirs == [tmp_path / ".claude" / "rules"]
        assert "Failed to resolve cached rules directory from adapter" in caplog.text
        assert "adapter broken" in caplog.text

    def test_cached_rules_adapter_failure_raises_when_failhard(self, mock_adapter, monkeypatch):
        from core import project_registry as registry_mod

        _, tmp_path = mock_adapter
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("QUAID_RULES_DIR", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)

        with patch("lib.adapter.get_adapter", side_effect=RuntimeError("adapter broken")), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="Failed to resolve cached rules directory"):
            registry_mod._current_cached_rules_dirs()

    @pytest.mark.parametrize(
        ("project_name", "instance_name"),
        [
            ("quaid", "creator-instance"),
            ("misc--claude-code-private-tmp-m13", "claude-code-private-tmp-m13"),
        ],
    )
    def test_unlink_rejects_reserved_projects(self, mock_adapter, project_name, instance_name):
        with patch("lib.instance.instance_id", return_value=instance_name), \
             patch("core.project_registry._sync_docs_registry_project"):
            create_project(project_name, initial_instance=instance_name)

        with patch("lib.instance.instance_id", return_value=instance_name):
            with pytest.raises(ValueError, match="Cannot unlink reserved project"):
                unlink_project(project_name)


class TestDeleteProjectPurgesDb:
    def test_create_project_clears_delete_marker(self, mock_adapter):
        from lib.project_registry import is_deleted

        create_project("my-app")
        delete_project("my-app")
        assert is_deleted("my-app") is True

        create_project("my-app")

        assert is_deleted("my-app") is False

    def test_delete_marker_wins_over_resurrected_registry_row(self, mock_adapter):
        import json
        from core import project_registry as registry_mod
        from core.project_registry import project_exists_raw

        _, tmp_path = mock_adapter
        create_project("my-app")
        delete_project("my-app")

        path = registry_mod._registry_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("projects", {})["my-app"] = {
            "canonical_path": str(tmp_path / "projects" / "my-app"),
            "instances": ["pytest-runner"],
            "description": "stale resurrection",
        }
        path.write_text(json.dumps(data), encoding="utf-8")

        assert project_exists_raw("my-app") is False
        assert get_project("my-app") is None
        assert "my-app" not in list_projects()
        cleaned = json.loads(path.read_text(encoding="utf-8"))
        assert "my-app" not in cleaned.get("projects", {})

    @pytest.mark.parametrize(
        ("project_name", "instance_name"),
        [
            ("quaid", "creator-instance"),
            ("misc--claude-code-private-tmp-m13", "claude-code-private-tmp-m13"),
        ],
    )
    def test_delete_rejects_reserved_projects(self, mock_adapter, project_name, instance_name):
        with patch("lib.instance.instance_id", return_value=instance_name), \
             patch("core.project_registry._sync_docs_registry_project"):
            create_project(project_name, initial_instance=instance_name)

        with pytest.raises(ValueError, match="Cannot delete reserved project"):
            delete_project(project_name)

        assert get_project(project_name) is not None

    def test_delete_hides_project_before_worker_cleanup(self, mock_adapter):
        """delete_project() should hide the project before slower monitor cleanup."""
        from core import project_docs
        from core.project_registry import project_exists_raw

        create_project("my-app")
        checked = False

        def _stop_worker_after_hidden(project):
            nonlocal checked
            if not checked:
                checked = True
                assert project == "my-app"
                assert project_exists_raw("my-app") is False
                assert get_project("my-app") is None
                assert "my-app" not in list_projects()

        with patch.object(project_docs, "stop_worker", side_effect=_stop_worker_after_hidden), \
             patch.object(project_docs, "cleanup_project_state", return_value={"removed": 0}):
            delete_project("my-app")

        assert checked is True

    def test_delete_purges_project_definitions_and_doc_registry(self, mock_adapter):
        """delete_project() removes project_definitions and doc_registry rows from SQLite."""
        import sqlite3
        from contextlib import contextmanager

        _, tmp_path = mock_adapter

        # Build an in-memory SQLite DB that already has the rows we expect to be purged
        mem_conn = sqlite3.connect(":memory:")
        mem_conn.execute(
            "CREATE TABLE project_definitions (name TEXT PRIMARY KEY, data TEXT)"
        )
        mem_conn.execute(
            "CREATE TABLE doc_registry (id INTEGER PRIMARY KEY, project TEXT, file_path TEXT)"
        )
        mem_conn.execute(
            "INSERT INTO project_definitions VALUES ('my-app', '{}')"
        )
        mem_conn.execute(
            "INSERT INTO doc_registry (project, file_path) VALUES ('my-app', '/some/file.md')"
        )
        mem_conn.execute(
            "INSERT INTO doc_registry (project, file_path) VALUES ('other-project', '/other/file.md')"
        )
        mem_conn.commit()

        @contextmanager
        def _fake_get_connection(_db_path):
            yield mem_conn
            mem_conn.commit()

        create_project("my-app")

        with patch("lib.database.get_connection", _fake_get_connection), \
             patch("lib.config.get_db_path", return_value=tmp_path / "memory.db"):
            delete_project("my-app")

        # project_definitions row must be gone
        row = mem_conn.execute(
            "SELECT name FROM project_definitions WHERE name = 'my-app'"
        ).fetchone()
        assert row is None

        # doc_registry rows for this project must be gone
        rows = mem_conn.execute(
            "SELECT id FROM doc_registry WHERE project = 'my-app'"
        ).fetchall()
        assert rows == []

        # unrelated project rows must be untouched
        other = mem_conn.execute(
            "SELECT id FROM doc_registry WHERE project = 'other-project'"
        ).fetchall()
        assert len(other) == 1

    def test_delete_final_cleanup_removes_registry_resurrection(self, mock_adapter):
        """delete_project() wins races where reconciliation re-adds JSON before DB cleanup finishes."""
        import json
        import sqlite3
        from contextlib import contextmanager
        from core import project_registry as registry_mod

        _, tmp_path = mock_adapter
        mem_conn = sqlite3.connect(":memory:")
        mem_conn.execute("CREATE TABLE project_definitions (name TEXT PRIMARY KEY, data TEXT)")
        mem_conn.execute("CREATE TABLE doc_registry (id INTEGER PRIMARY KEY, project TEXT, file_path TEXT)")
        mem_conn.commit()

        create_project("my-app")
        resurrected = False

        @contextmanager
        def _fake_get_connection(_db_path):
            nonlocal resurrected
            yield mem_conn
            mem_conn.commit()
            # Simulate a concurrent list/show reconciliation that observed stale
            # DB rows before this delete transaction finished its DB cleanup.
            if not resurrected:
                resurrected = True
                path = registry_mod._registry_path()
                data = json.loads(path.read_text(encoding="utf-8"))
                data.setdefault("projects", {})["my-app"] = {
                    "canonical_path": str(tmp_path / "projects" / "my-app"),
                    "instances": ["pytest-runner"],
                    "description": "resurrected",
                }
                path.write_text(json.dumps(data), encoding="utf-8")

        with patch("lib.database.get_connection", _fake_get_connection), \
             patch("lib.config.get_db_path", return_value=tmp_path / "memory.db"):
            delete_project("my-app")

        assert get_project("my-app") is None

    def test_delete_settles_late_project_docs_state_recreation(self, mock_adapter):
        """delete_project() cleans worker state recreated by a stale supervisor tick."""
        _, tmp_path = mock_adapter
        from core import project_docs

        create_project("my-app")
        calls = 0
        real_cleanup = project_docs.cleanup_project_state

        def _cleanup_then_recreate(project):
            nonlocal calls
            result = real_cleanup(project)
            calls += 1
            if calls == 2:
                project_docs.state_path(project).parent.mkdir(parents=True, exist_ok=True)
                project_docs.state_path(project).write_text("{}", encoding="utf-8")
                project_docs._spawn_lock_path("worker", project).parent.mkdir(parents=True, exist_ok=True)
                project_docs._spawn_lock_path("worker", project).write_text("lock", encoding="utf-8")
            return result

        with patch("core.project_docs.cleanup_project_state", side_effect=_cleanup_then_recreate):
            delete_project("my-app")

        assert get_project("my-app") is None
        assert not (tmp_path / "data" / "project-docs" / "state" / "my-app.json").exists()
        assert not (tmp_path / "data" / "project-docs" / "locks" / "my-app.worker.spawn.lock").exists()

    def test_delete_handles_missing_db_gracefully(self, mock_adapter):
        """delete_project() does not raise when the DB connection fails."""
        _, tmp_path = mock_adapter
        create_project("my-app")

        with patch("lib.database.get_connection", side_effect=Exception("db unavailable")), \
             patch("core.project_registry._fail_hard_enabled", return_value=False):
            # Should complete without raising (error is logged as a warning)
            delete_project("my-app")

        # Project is removed from registry regardless
        assert get_project("my-app") is None

    def test_cleanup_staged_project_event_parse_failure_warns_when_fail_open(self, mock_adapter, caplog):
        from core import project_registry as registry_mod

        _adapter, tmp_path = mock_adapter
        visible_home = tmp_path.with_name(tmp_path.name.lstrip("."))
        staging_dir = visible_home / "projects" / "staging"
        staging_dir.mkdir(parents=True)
        event_file = staging_dir / "bad.json"
        event_file.write_text("{not json", encoding="utf-8")

        with patch("core.project_registry._fail_hard_enabled", return_value=False), \
             caplog.at_level(logging.WARNING, logger="core.project_registry"):
            removed = registry_mod._cleanup_staged_project_events(
                quaid_home=tmp_path,
                visible_home=visible_home,
                project_name="my-app",
                canonical=tmp_path / "projects" / "my-app",
            )

        assert removed == 0
        assert event_file.exists()
        assert "Failed to read staged project event" in caplog.text

    def test_cleanup_staged_project_event_parse_failure_raises_when_failhard(self, mock_adapter):
        from core import project_registry as registry_mod

        _adapter, tmp_path = mock_adapter
        visible_home = tmp_path.with_name(tmp_path.name.lstrip("."))
        staging_dir = visible_home / "projects" / "staging"
        staging_dir.mkdir(parents=True)
        (staging_dir / "bad.json").write_text("{not json", encoding="utf-8")

        with patch("core.project_registry._fail_hard_enabled", return_value=True), \
             pytest.raises(RuntimeError, match="Failed to read staged project event"):
            registry_mod._cleanup_staged_project_events(
                quaid_home=tmp_path,
                visible_home=visible_home,
                project_name="my-app",
                canonical=tmp_path / "projects" / "my-app",
            )

    def test_reconcile_docs_registry_failure_uses_failhard_helper(self, mock_adapter, monkeypatch, caplog):
        from core import project_registry as registry_mod
        from datastore.docsdb import registry as docs_registry_mod

        class _FailingDocsRegistry:
            def __init__(self, **_kwargs):
                pass

            def reconcile_global_project_registry(self):
                raise RuntimeError("reconcile broken")

        monkeypatch.setattr(docs_registry_mod, "DocsRegistry", _FailingDocsRegistry)

        with patch("core.project_registry._fail_hard_enabled", return_value=True), \
             caplog.at_level(logging.WARNING, logger="core.project_registry"), \
             pytest.raises(RuntimeError, match="reconcile broken"):
            registry_mod._reconcile_docs_registry_projects()

        assert "Docs/project registry reconciliation skipped" in caplog.text

    def test_delete_warns_when_cleanup_does_not_converge(self, mock_adapter, caplog):
        create_project("my-app")

        with patch("core.project_registry._delete_docs_db_project_rows", return_value=[]), \
             patch("core.project_registry._docs_db_project_rows_exist", return_value=True), \
             patch("core.project_registry._fail_hard_enabled", return_value=False), \
             patch("core.project_registry.time.sleep"), \
             caplog.at_level(logging.WARNING):
            delete_project("my-app")

        assert "did not fully converge" in caplog.text
        assert get_project("my-app") is None

    def test_delete_raises_under_failhard_when_cleanup_does_not_converge(self, mock_adapter):
        create_project("my-app")

        with patch("core.project_registry._delete_docs_db_project_rows", return_value=[]), \
             patch("core.project_registry._docs_db_project_rows_exist", return_value=True), \
             patch("core.project_registry._fail_hard_enabled", return_value=True), \
             patch("core.project_registry.time.sleep"), \
             pytest.raises(RuntimeError, match="did not fully converge"):
            delete_project("my-app")
