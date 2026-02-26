"""Tests for project_updater.py — event processing, PROJECT.md refresh, cascading."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_tmp_db = None


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    """Set up isolated test environment."""
    global _tmp_db
    _tmp_db = tmp_path / "test_registry.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(_tmp_db))
    from lib.adapter import set_adapter, reset_adapter, StandaloneAdapter
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))  # kept for backward compat

    # Create directories
    (tmp_path / "config").mkdir()
    (tmp_path / "projects" / "staging").mkdir(parents=True)
    (tmp_path / "projects" / "test-project").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()

    # Create config
    config_data = {
        "projects": {
            "enabled": True,
            "projectsDir": "projects/",
            "stagingDir": "projects/staging/",
            "definitions": {
                "test-project": {
                    "label": "Test Project",
                    "homeDir": "projects/test-project/",
                    "sourceRoots": ["src/"],
                    "autoIndex": True,
                    "patterns": ["*.md"],
                    "exclude": ["*.log", "*.db", "__pycache__/"],
                    "description": "A test project"
                }
            },
            "defaultProject": "default"
        },
        "docs": {
            "stalenessCheckEnabled": True,
            "sourceMapping": {},
            "docPurposes": {},
            "coreMarkdown": {"enabled": False}
        },
        "rag": {"docsDir": "docs"},
    }
    (tmp_path / "config" / "memory.json").write_text(json.dumps(config_data))

    # Create PROJECT.md
    project_md = """# Project: Test Project

## Overview
A test project.

## Files & Assets

### In This Directory
<!-- Auto-discovered -->

### External Files
| File | Purpose | Auto-Update |
|------|---------|-------------|

## Documents
| Document | Tracks | Auto-Update |
|----------|--------|-------------|

## Related Projects

## Update Rules

## Exclude
- *.log
- *.db
"""
    (tmp_path / "projects" / "test-project" / "PROJECT.md").write_text(project_md)

    sys.path.insert(0, str(Path(__file__).parent.parent))
    import config as config_mod
    monkeypatch.setattr(config_mod, "_config_paths", lambda: [tmp_path / "config" / "memory.json"])
    config_mod.reload_config()

    yield tmp_path

    reset_adapter()


def _get_registry():
    from core.docs.registry import DocsRegistry
    return DocsRegistry(db_path=_tmp_db)


class TestProcessEvent:
    def test_basic_event(self, setup_env):
        """Process a basic event file with project hint."""
        from datastore.docsdb.project_updater import process_event

        tmp_path = setup_env
        event = {
            "project_hint": "test-project",
            "files_touched": ["src/main.py"],
            "summary": "Updated main module",
            "trigger": "compact",
            "session_id": "test-123",
        }
        event_path = tmp_path / "projects" / "staging" / "test-event.json"
        event_path.write_text(json.dumps(event))

        result = process_event(str(event_path))
        assert result["success"] is True
        assert result["project"] == "test-project"
        # Event file should be cleaned up
        assert not event_path.exists()

    def test_missing_event_file(self, setup_env):
        from datastore.docsdb.project_updater import process_event
        result = process_event("/nonexistent/event.json")
        assert result["success"] is False

    def test_unresolvable_project(self, setup_env):
        """Event with no resolvable project is skipped."""
        from datastore.docsdb.project_updater import process_event

        tmp_path = setup_env
        event = {
            "project_hint": None,
            "files_touched": ["/some/random/path.py"],
            "summary": "Unknown project",
            "trigger": "compact",
        }
        event_path = tmp_path / "projects" / "staging" / "test-event.json"
        event_path.write_text(json.dumps(event))

        result = process_event(str(event_path))
        assert result["success"] is False
        assert result["error"] == "project_not_resolved"


class TestProcessAllEvents:
    def test_multiple_events(self, setup_env):
        """Process multiple events chronologically."""
        from datastore.docsdb.project_updater import process_all_events

        tmp_path = setup_env
        staging = tmp_path / "projects" / "staging"

        for i in range(3):
            event = {
                "project_hint": "test-project",
                "files_touched": [],
                "summary": f"Event {i}",
                "trigger": "compact",
            }
            (staging / f"{1000+i}-compact.json").write_text(json.dumps(event))

        result = process_all_events()
        assert result["processed"] == 3
        # All event files should be cleaned up
        assert len(list(staging.glob("*.json"))) == 0

    def test_no_events(self, setup_env):
        from datastore.docsdb.project_updater import process_all_events
        result = process_all_events()
        assert result["processed"] == 0


class TestProcessEventWatchdog:
    def test_main_process_event_moves_file_to_failed_on_watchdog_timeout(self, setup_env, capsys):
        import datastore.docsdb.project_updater as project_updater

        tmp_path = setup_env
        event_path = tmp_path / "projects" / "staging" / "watchdog-event.json"
        event_path.write_text(json.dumps({
            "project_hint": "test-project",
            "files_touched": [],
            "summary": "will timeout",
            "trigger": "compact",
        }))

        argv = list(sys.argv)
        try:
            sys.argv = ["project_updater.py", "process-event", str(event_path)]
            with patch.object(project_updater, "_watchdog_seconds", return_value=1), \
                 patch.object(project_updater, "_run_with_watchdog", side_effect=TimeoutError("timed out")):
                project_updater.main()
        finally:
            sys.argv = argv

        failed_path = event_path.parent / "failed" / event_path.name
        assert failed_path.exists()
        out = capsys.readouterr().out
        assert "watchdog_timeout" in out


class TestRefreshProjectMd:
    def test_updates_file_list(self, setup_env):
        """Refresh regenerates the file list in PROJECT.md."""
        from datastore.docsdb.project_updater import refresh_project_md

        tmp_path = setup_env
        registry = _get_registry()

        # Register some docs
        registry.register("projects/test-project/notes.md", project="test-project")
        registry.register("docs/external.md", project="test-project",
                          description="External doc", auto_update=True,
                          source_files=["src/main.py"])

        # Create the notes file
        (tmp_path / "projects" / "test-project" / "notes.md").write_text("# Notes")

        ok = refresh_project_md("test-project")
        assert ok is True

        content = (tmp_path / "projects" / "test-project" / "PROJECT.md").read_text()
        assert "notes.md" in content

    def test_unknown_project(self, setup_env):
        from datastore.docsdb.project_updater import refresh_project_md
        ok = refresh_project_md("nonexistent")
        assert ok is False

    def test_refresh_recovers_missing_external_heading(self, setup_env):
        """Refresh should still rebuild Files & Assets if headings are malformed."""
        from datastore.docsdb.project_updater import refresh_project_md

        tmp_path = setup_env
        registry = _get_registry()
        project_md_path = tmp_path / "projects" / "test-project" / "PROJECT.md"

        # Simulate legacy/broken PROJECT.md lacking "### External Files".
        project_md_path.write_text(
            """# Project: Test Project

## Overview
A test project.

## Files & Assets

### In This Directory
(auto-populated by janitor)

## Documents
| Document | Tracks | Auto-Update |
|----------|--------|-------------|
"""
        )

        # Ensure there is at least one discoverable doc under the project.
        notes = tmp_path / "projects" / "test-project" / "notes.md"
        notes.write_text("# Notes")
        registry.register("projects/test-project/notes.md", project="test-project")

        ok = refresh_project_md("test-project")
        assert ok is True
        content = project_md_path.read_text()
        assert "### In This Directory" in content
        assert "### External Files" in content
        assert "- projects/test-project/notes.md" in content


class TestExclusionPatterns:
    def test_excluded_files_not_discovered(self, setup_env):
        """Excluded files don't appear in auto-discover."""
        tmp_path = setup_env
        registry = _get_registry()

        proj_dir = tmp_path / "projects" / "test-project"
        (proj_dir / "readme.md").write_text("# Readme")
        (proj_dir / "debug.log").write_text("log data")  # Should be excluded
        pycache = proj_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.md").write_text("# Cached")  # Should be excluded

        found = registry.auto_discover("test-project")
        file_names = [Path(f).name for f in found]
        assert "readme.md" in file_names
        assert "debug.log" not in file_names
        assert "cached.md" not in file_names


class TestCascade:
    """Cascade was removed as dead code — tests verify removal."""
    def test_cascade_function_removed(self, setup_env):
        """_check_cascade was dead code and has been removed."""
        import datastore.docsdb.project_updater as project_updater
        assert not hasattr(project_updater, '_check_cascade')
