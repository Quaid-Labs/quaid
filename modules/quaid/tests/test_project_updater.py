"""Tests for project_updater.py — event processing, PROJECT.md refresh, cascading."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from lib.project_templates import render_project_md_template

_tmp_db = None


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    """Set up isolated test environment."""
    global _tmp_db
    _tmp_db = tmp_path / "test_registry.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(_tmp_db))
    from lib.adapter import set_adapter, reset_adapter, TestAdapter
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    iroot = adapter.instance_root()
    monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(iroot))  # kept for backward compat

    # Create directories
    (iroot / "config").mkdir(exist_ok=True)
    shared_projects_dir = tmp_path / "projects"
    shared_projects_dir.mkdir(parents=True, exist_ok=True)
    instance_projects = iroot / "projects"
    if not instance_projects.exists():
        instance_projects.symlink_to(shared_projects_dir, target_is_directory=True)
    (shared_projects_dir / "staging").mkdir(parents=True, exist_ok=True)
    (shared_projects_dir / "test-project").mkdir(parents=True)
    (iroot / "src").mkdir()
    (iroot / "docs").mkdir()

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
    (iroot / "config" / "memory.json").write_text(json.dumps(config_data))

    # Create PROJECT.md
    project_md = render_project_md_template(
        label="Test Project",
        description="A test project.",
        project_home=str(shared_projects_dir / "test-project"),
        source_roots=[str(iroot / "src")],
        exclude_patterns=["*.log", "*.db"],
    )
    (shared_projects_dir / "test-project" / "PROJECT.md").write_text(project_md)

    sys.path.insert(0, str(Path(__file__).parent.parent))
    import config as config_mod
    monkeypatch.setattr(config_mod, "_config_paths", lambda: [iroot / "config" / "memory.json"])
    config_mod.reload_config()

    yield iroot

    reset_adapter()


def _get_registry():
    from datastore.docsdb.registry import DocsRegistry
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

    def test_warns_when_failed_event_move_errors(self, setup_env, capsys):
        import datastore.docsdb.project_updater as project_updater

        tmp_path = setup_env
        staging = tmp_path / "projects" / "staging"
        event_path = staging / "bad-event.json"
        event_path.write_text(json.dumps({
            "project_hint": "test-project",
            "files_touched": [],
            "summary": "bad event",
            "trigger": "compact",
        }))

        with patch.object(project_updater, "process_event", side_effect=RuntimeError("boom")), \
             patch.object(Path, "rename", side_effect=OSError("rename denied")):
            result = project_updater.process_all_events()

        assert result["errors"] == 1
        err = capsys.readouterr().err
        assert "failed to move event bad-event.json into failed/" in err


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
        assert "## Primary Artifacts" in content
        assert "### External Files" in content
        assert "- `notes.md`" in content

    def test_preserves_markerized_custom_sections(self, setup_env):
        """Refresh should preserve custom scaffold content when markers already exist."""
        from datastore.docsdb.project_updater import refresh_project_md

        tmp_path = setup_env
        registry = _get_registry()
        project_md_path = tmp_path / "projects" / "test-project" / "PROJECT.md"
        custom = project_md_path.read_text().replace(
            "## Primary Artifacts",
            "## Start Here By Task\n- Read `docs/overview.md` first.\n\n## Primary Artifacts",
            1,
        )
        project_md_path.write_text(custom)
        notes = tmp_path / "projects" / "test-project" / "notes.md"
        notes.write_text("# Notes")
        registry.register("projects/test-project/notes.md", project="test-project")

        ok = refresh_project_md("test-project")
        assert ok is True

        content = project_md_path.read_text()
        assert "## Start Here By Task" in content
        assert "- Read `docs/overview.md` first." in content
        assert "- `notes.md`" in content


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


class TestAppendProjectLogs:
    def test_normalizes_session_prefix_and_dedupes_entries(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_log = tmp_path / "projects" / "test-project" / "PROJECT.log"

        metrics = append_project_logs(
            {
                "test-project": [
                    "Session 3 (compact): Updated README links",
                    "- Session 8: Updated README links",
                    "  * Session 9 (reset): Added API docs  ",
                ]
            },
            trigger="Compaction",
            date_str="2026-03-03",
            dry_run=False,
        )

        assert metrics["projects_seen"] == 1
        assert metrics["projects_updated"] == 1
        assert metrics["entries_seen"] == 2
        assert metrics["entries_written"] == 2

        content = project_md.read_text()
        assert "- 2026-03-03 [Compaction] Updated README links" in content
        assert "- 2026-03-03 [Compaction] Added API docs" in content
        # PROJECT.log keeps full append-only history (including duplicates).
        history = project_log.read_text()
        assert history.count("Updated README links") == 2
        assert "Added API docs" in history

    def test_appends_into_existing_project_log_block(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_md.write_text(
            project_md.read_text()
            + "\n## Project Log\n"
            + "<!-- BEGIN:PROJECT_LOG -->\n"
            + "- 2026-03-01 [Compaction] Existing entry\n"
            + "<!-- END:PROJECT_LOG -->\n"
        )

        metrics = append_project_logs(
            {"test-project": ["Session 2: Added retry logic"]},
            trigger="Reset",
            date_str="2026-03-03",
            dry_run=False,
        )

        assert metrics["entries_seen"] == 1
        assert metrics["entries_written"] == 1
        content = project_md.read_text()
        assert "- 2026-03-01 [Compaction] Existing entry" in content
        assert "- 2026-03-03 [Reset] Added retry logic" in content

    def test_dry_run_reports_metrics_without_writing(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        before = project_md.read_text()

        metrics = append_project_logs(
            {"test-project": ["Session 4: Dry run only"]},
            trigger="Compaction",
            date_str="2026-03-03",
            dry_run=True,
        )

        assert metrics["projects_updated"] == 1
        assert metrics["entries_written"] == 1
        assert project_md.read_text() == before

    def test_visible_project_md_log_is_capped_but_history_is_append_only(self, setup_env, monkeypatch):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_log = tmp_path / "projects" / "test-project" / "PROJECT.log"
        monkeypatch.setenv("QUAID_PROJECT_MD_RECENT_LIMIT", "2")

        append_project_logs(
            {"test-project": ["Session 1: one", "Session 2: two", "Session 3: three"]},
            trigger="Compaction",
            date_str="2026-03-03",
            dry_run=False,
        )

        content = project_md.read_text()
        assert "Session 1" not in content
        assert "- 2026-03-03 [Compaction] two" in content
        assert "- 2026-03-03 [Compaction] three" in content

        history = project_log.read_text()
        assert "one" in history
        assert "two" in history
        assert "three" in history

    def test_unknown_project_is_reported_and_skipped(self, setup_env, capsys):
        from datastore.docsdb.project_updater import append_project_logs

        metrics = append_project_logs(
            {"does-not-exist": ["Session 1: ignore"]},
            trigger="Compaction",
            date_str="2026-03-03",
            dry_run=False,
        )

        assert metrics["projects_seen"] == 1
        assert metrics["projects_unknown"] == 1
        assert metrics["projects_updated"] == 0
        out = capsys.readouterr().out
        assert "[project-log] unknown project: does-not-exist" in out

    def test_missing_project_md_is_reported_and_skipped(self, setup_env, capsys):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_md.unlink()

        metrics = append_project_logs(
            {"test-project": ["Session 1: missing file"]},
            trigger="Compaction",
            date_str="2026-03-03",
            dry_run=False,
        )

        assert metrics["projects_seen"] == 1
        assert metrics["projects_missing_file"] == 1
        assert metrics["projects_updated"] == 0
        out = capsys.readouterr().out
        assert "[project-log] missing PROJECT.md:" in out

    def test_empty_or_invalid_payload_is_noop(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        assert append_project_logs({}, dry_run=False)["projects_seen"] == 0
        assert append_project_logs(None, dry_run=False)["projects_seen"] == 0


class TestCascade:
    """Cascade was removed as dead code — tests verify removal."""
    def test_cascade_function_removed(self, setup_env):
        """_check_cascade was dead code and has been removed."""
        import datastore.docsdb.project_updater as project_updater
        assert not hasattr(project_updater, '_check_cascade')
