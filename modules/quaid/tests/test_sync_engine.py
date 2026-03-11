"""Tests for core/sync_engine.py — project context file sync."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.sync_engine import sync_project, sync_all_projects, SYNCABLE_NAMES, _cleanup_stale_targets


class TestSyncProject:
    def test_copies_new_file(self, tmp_path):
        canonical = tmp_path / "projects" / "myapp"
        canonical.mkdir(parents=True)
        (canonical / "TOOLS.md").write_text("# Tools")

        target = tmp_path / "workspace" / "projects"
        target.mkdir(parents=True)

        result = sync_project(canonical, target, "myapp")
        assert "TOOLS.md" in result.copied
        assert (target / "myapp" / "TOOLS.md").read_text() == "# Tools"

    def test_skips_unchanged_file(self, tmp_path):
        canonical = tmp_path / "projects" / "myapp"
        canonical.mkdir(parents=True)
        (canonical / "TOOLS.md").write_text("# Tools")

        target = tmp_path / "workspace" / "projects"
        (target / "myapp").mkdir(parents=True)
        # Copy with same or newer mtime
        import shutil
        shutil.copy2(canonical / "TOOLS.md", target / "myapp" / "TOOLS.md")

        result = sync_project(canonical, target, "myapp")
        assert "TOOLS.md" in result.skipped
        assert not result.copied

    def test_updates_newer_file(self, tmp_path):
        canonical = tmp_path / "projects" / "myapp"
        canonical.mkdir(parents=True)

        target = tmp_path / "workspace" / "projects"
        (target / "myapp").mkdir(parents=True)
        (target / "myapp" / "TOOLS.md").write_text("old content")

        import time
        time.sleep(0.05)
        (canonical / "TOOLS.md").write_text("new content")

        result = sync_project(canonical, target, "myapp")
        assert "TOOLS.md" in result.copied
        assert (target / "myapp" / "TOOLS.md").read_text() == "new content"

    def test_removes_deleted_canonical(self, tmp_path):
        canonical = tmp_path / "projects" / "myapp"
        canonical.mkdir(parents=True)

        target = tmp_path / "workspace" / "projects"
        (target / "myapp").mkdir(parents=True)
        (target / "myapp" / "TOOLS.md").write_text("stale")

        result = sync_project(canonical, target, "myapp")
        assert "TOOLS.md" in result.removed
        assert not (target / "myapp" / "TOOLS.md").exists()

    def test_only_syncs_syncable_names(self, tmp_path):
        canonical = tmp_path / "projects" / "myapp"
        canonical.mkdir(parents=True)
        (canonical / "TOOLS.md").write_text("# Tools")
        (canonical / "random.txt").write_text("not synced")
        (canonical / "PROJECT.md").write_text("not synced either")

        target = tmp_path / "workspace" / "projects"
        result = sync_project(canonical, target, "myapp")
        assert "TOOLS.md" in result.copied
        assert not (target / "myapp" / "random.txt").exists()
        # PROJECT.md is not in SYNCABLE_NAMES
        assert not (target / "myapp" / "PROJECT.md").exists()

    def test_writes_readme(self, tmp_path):
        canonical = tmp_path / "projects" / "myapp"
        canonical.mkdir(parents=True)
        (canonical / "TOOLS.md").write_text("# Tools")

        target = tmp_path / "workspace" / "projects"
        sync_project(canonical, target, "myapp")

        readme = target / "myapp" / "README.md"
        assert readme.exists()
        assert "DO NOT EDIT" in readme.read_text()
        assert str(canonical) in readme.read_text()

    def test_multiple_files(self, tmp_path):
        canonical = tmp_path / "projects" / "myapp"
        canonical.mkdir(parents=True)
        (canonical / "TOOLS.md").write_text("# Tools")
        (canonical / "AGENTS.md").write_text("# Agents")

        target = tmp_path / "workspace" / "projects"
        result = sync_project(canonical, target, "myapp")
        assert "TOOLS.md" in result.copied
        assert "AGENTS.md" in result.copied


class TestCleanupStaleTargets:
    def test_removes_stale_target(self, tmp_path):
        canonical = tmp_path / "canonical"
        canonical.mkdir()
        (canonical / "active").mkdir()

        target = tmp_path / "target"
        (target / "active").mkdir(parents=True)
        (target / "stale").mkdir()
        (target / "stale" / "TOOLS.md").write_text("old")

        _cleanup_stale_targets(canonical, target)
        assert (target / "active").exists()
        assert not (target / "stale").exists()


class TestSyncAllProjects:
    def test_no_sync_when_adapter_returns_none(self, tmp_path, monkeypatch):
        mock_adapter = MagicMock()
        mock_adapter.get_context_sync_target.return_value = None
        mock_adapter.projects_dir.return_value = tmp_path / "projects"

        monkeypatch.setattr("lib.adapter.get_adapter", lambda: mock_adapter)
        results = sync_all_projects()
        assert results == []

    def test_syncs_when_adapter_has_target(self, tmp_path, monkeypatch):
        projects = tmp_path / "projects"
        (projects / "myapp").mkdir(parents=True)
        (projects / "myapp" / "TOOLS.md").write_text("# Tools")

        target = tmp_path / "workspace"
        target.mkdir()

        mock_adapter = MagicMock()
        mock_adapter.get_context_sync_target.return_value = target
        mock_adapter.projects_dir.return_value = projects

        monkeypatch.setattr("lib.adapter.get_adapter", lambda: mock_adapter)
        results = sync_all_projects()
        assert len(results) == 1
        assert results[0].project == "myapp"
        assert "TOOLS.md" in results[0].copied
