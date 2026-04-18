"""Tests for core/project_registry.py — project registry CRUD."""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.project_registry import (
    clear_misc_auto_create_disabled,
    create_project,
    delete_project,
    get_project,
    is_misc_auto_create_disabled,
    link_project,
    list_projects,
    mark_misc_auto_create_disabled,
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
        assert result == {"projects": {}}

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
        result = _load_registry()
        assert result == {"projects": {}}

    def test_registry_lock_path_uses_stable_sidecar(self, mock_adapter):
        _, tmp_path = mock_adapter
        assert _registry_lock_path() == tmp_path / "project-registry.json.lock"


class TestCreateProject:
    def test_creates_project(self, mock_adapter):
        adapter, tmp_path = mock_adapter
        with patch("core.project_registry._sync_docs_registry_project") as sync_docs:
            entry = create_project("my-app", description="Test app")

        assert entry["description"] == "Test app"
        assert entry["source_root"] is None
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
        assert get_project("my-app") is not None
        sync_docs.assert_called_once()

    def test_rejects_invalid_name(self, mock_adapter):
        with pytest.raises(ValueError, match="Invalid project name"):
            create_project("My App")

        with pytest.raises(ValueError, match="Invalid project name"):
            create_project("has spaces")

        with pytest.raises(ValueError, match="Invalid project name"):
            create_project("-starts-with-dash")

    def test_rejects_duplicate(self, mock_adapter):
        create_project("my-app")
        with pytest.raises(ValueError, match="already exists"):
            create_project("my-app")

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


class TestUpdateProject:
    def test_updates_fields(self, mock_adapter):
        with patch("core.project_registry._sync_docs_registry_project"):
            create_project("my-app", description="v1")
        with patch("core.project_registry._sync_docs_registry_project") as sync_docs:
            updated = update_project("my-app", description="v2")
        assert updated["description"] == "v2"
        sync_docs.assert_called_once()

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
    def test_deletes_project(self, mock_adapter):
        _, tmp_path = mock_adapter
        create_project("my-app")
        assert get_project("my-app") is not None

        delete_project("my-app")
        assert get_project("my-app") is None
        assert not (tmp_path / "projects" / "my-app").exists()

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

    def test_delete_misc_project_sets_auto_create_tombstone(self, mock_adapter):
        _, tmp_path = mock_adapter
        instance_id = "claude-code-private-tmp-m13"
        with patch("lib.instance.instance_id", return_value=instance_id), \
             patch("core.project_registry._sync_docs_registry_project"):
            create_project(f"misc--{instance_id}", description="scratch")
        assert is_misc_auto_create_disabled(instance_id, quaid_home=tmp_path) is False

        delete_project(f"misc--{instance_id}")

        assert is_misc_auto_create_disabled(instance_id, quaid_home=tmp_path) is True

    def test_recreate_misc_project_clears_auto_create_tombstone(self, mock_adapter):
        _, tmp_path = mock_adapter
        instance_id = "claude-code-private-tmp-m13"
        marker = mark_misc_auto_create_disabled(instance_id, quaid_home=tmp_path)
        assert marker.is_file()

        with patch("lib.instance.instance_id", return_value=instance_id), \
             patch("core.project_registry._sync_docs_registry_project"):
            create_project(f"misc--{instance_id}", description="scratch", initial_instance=instance_id)

        assert is_misc_auto_create_disabled(instance_id, quaid_home=tmp_path) is False
        clear_misc_auto_create_disabled(instance_id, quaid_home=tmp_path)


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
    def test_link_adds_current_instance(self, mock_adapter):
        """link_project() adds the current instance ID to the instances list."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        with patch("lib.instance.instance_id", return_value="second-instance"):
            entry = link_project("my-app")

        assert "second-instance" in entry["instances"]

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
    def test_unlink_removes_current_instance(self, mock_adapter):
        """unlink_project() removes the current instance from the instances list."""
        _, tmp_path = mock_adapter
        with patch("lib.instance.instance_id", return_value="creator-instance"):
            create_project("my-app")

        with patch("lib.instance.instance_id", return_value="second-instance"):
            link_project("my-app")

        with patch("lib.instance.instance_id", return_value="second-instance"):
            entry = unlink_project("my-app")

        assert "second-instance" not in entry["instances"]
        # creator should still be present
        assert "creator-instance" in entry["instances"]

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


class TestDeleteProjectPurgesDb:
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

    def test_delete_handles_missing_db_gracefully(self, mock_adapter):
        """delete_project() does not raise when the DB connection fails."""
        _, tmp_path = mock_adapter
        create_project("my-app")

        with patch("lib.database.get_connection", side_effect=Exception("db unavailable")):
            # Should complete without raising (error is logged as a warning)
            delete_project("my-app")

        # Project is removed from registry regardless
        assert get_project("my-app") is None
