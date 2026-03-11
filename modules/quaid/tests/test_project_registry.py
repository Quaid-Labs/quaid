"""Tests for core/project_registry.py — project registry CRUD."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.project_registry import (
    create_project,
    delete_project,
    get_project,
    list_projects,
    update_project,
    projects_with_source_root,
    snapshot_all_projects,
    _load_registry,
    _save_registry,
    _registry_path,
)


@pytest.fixture
def mock_adapter(tmp_path):
    """Set up a mock adapter with tmp_path as quaid_home."""
    adapter = MagicMock()
    adapter.quaid_home.return_value = tmp_path
    adapter.adapter_id.return_value = "test-adapter"

    with patch("lib.adapter.get_adapter", return_value=adapter):
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
        reg.write_text("not valid json{{{")
        result = _load_registry()
        assert result == {"projects": {}}


class TestCreateProject:
    def test_creates_project(self, mock_adapter):
        adapter, tmp_path = mock_adapter
        entry = create_project("my-app", description="Test app")

        assert entry["description"] == "Test app"
        assert entry["source_root"] is None
        assert "test-adapter" in entry["instances"]

        # Canonical dir created
        canonical = tmp_path / "projects" / "my-app"
        assert canonical.is_dir()
        assert (canonical / "docs").is_dir()
        assert (canonical / "PROJECT.md").is_file()

        # In registry
        assert get_project("my-app") is not None

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

        entry = create_project("my-app", source_root=str(src))
        assert entry["source_root"] == str(src)

        # Shadow git should be initialized
        tracking = tmp_path / ".git-tracking" / "my-app"
        assert tracking.is_dir()


class TestUpdateProject:
    def test_updates_fields(self, mock_adapter):
        create_project("my-app", description="v1")
        updated = update_project("my-app", description="v2")
        assert updated["description"] == "v2"

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
