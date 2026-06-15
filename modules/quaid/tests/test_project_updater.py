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
_INDEXED_DOC_PATHS = []


class _FakeDocsRAG:
    def __init__(self, *args, **kwargs):
        pass

    def index_document(self, file_path):
        _INDEXED_DOC_PATHS.append(str(file_path))
        return 2


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    """Set up isolated test environment."""
    global _tmp_db
    _tmp_db = tmp_path / "test_registry.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(_tmp_db))
    monkeypatch.setenv("DOCS_DB_PATH", str(_tmp_db))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    from lib.adapter import set_adapter, reset_adapter, TestAdapter
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    iroot = adapter.instance_root()
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(iroot))  # kept for backward compat

    # Create directories
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
    (iroot / "config.json").write_text(json.dumps(config_data))

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
    monkeypatch.setattr(config_mod, "_config_paths", lambda: [iroot / "config.json"])
    config_mod.reload_config()
    _INDEXED_DOC_PATHS.clear()

    import datastore.docsdb.rag as rag_mod
    monkeypatch.setattr(rag_mod, "DocsRAG", _FakeDocsRAG)

    yield iroot

    reset_adapter()


def _get_registry():
    from datastore.docsdb.registry import DocsRegistry
    return DocsRegistry(db_path=_tmp_db)


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

    def test_refresh_tolerates_missing_managed_section_keys(self, setup_env, monkeypatch):
        from datastore.docsdb import project_updater

        project_md_path = setup_env / "projects" / "test-project" / "PROJECT.md"
        monkeypatch.setattr(
            project_updater.docs_registry,
            "_managed_project_sections",
            lambda *_args, **_kwargs: {
                "project_home": "home only",
            },
        )

        assert project_updater.refresh_project_md("test-project") is True
        assert "home only" in project_md_path.read_text(encoding="utf-8")

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
        assert "- [2026-03-03T23:59:59] Updated README links" in history
        assert history.count("Updated README links") == 2
        assert "Added API docs" in history

    def test_indexes_project_log_after_append(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_log = tmp_path / "projects" / "test-project" / "PROJECT.log"

        metrics = append_project_logs(
            {"test-project": ["Session 5: Added dated recall support"]},
            trigger="Compaction",
            date_str="2026-03-05",
            dry_run=False,
        )

        assert str(project_log.resolve()) in _INDEXED_DOC_PATHS
        assert metrics["history_entries_written"] == 1
        assert metrics["history_logs_indexed"] == 1
        assert metrics["history_chunks_indexed"] == 2

    def test_can_skip_project_log_indexing_for_worker_owned_commit(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_log = tmp_path / "projects" / "test-project" / "PROJECT.log"

        metrics = append_project_logs(
            {"test-project": ["Session 5: Queued by project-docs worker"]},
            trigger="Reset",
            date_str="2026-03-06",
            dry_run=False,
            index_history=False,
        )

        assert project_log.exists()
        assert "Queued by project-docs worker" in project_log.read_text(encoding="utf-8")
        assert metrics["history_entries_written"] == 1
        assert metrics["history_logs_indexed"] == 0
        assert _INDEXED_DOC_PATHS == []

    def test_project_log_replay_does_not_duplicate_existing_history_line(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        project_log = setup_env / "projects" / "test-project" / "PROJECT.log"
        payload = {"test-project": ["Session 5: Replay-safe queue entry"]}

        first = append_project_logs(
            payload,
            trigger="Reset",
            date_str="2026-03-06",
            dry_run=False,
            index_history=False,
            update_project_md=False,
        )
        second = append_project_logs(
            payload,
            trigger="Reset",
            date_str="2026-03-06",
            dry_run=False,
            index_history=False,
            update_project_md=False,
        )

        history = project_log.read_text(encoding="utf-8")
        assert first["history_entries_written"] == 1
        assert second["history_entries_written"] == 0
        assert history.count("Replay-safe queue entry") == 1

    def test_can_append_project_log_without_updating_project_md(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_log = tmp_path / "projects" / "test-project" / "PROJECT.log"
        before = project_md.read_text(encoding="utf-8")

        metrics = append_project_logs(
            {"test-project": ["Session 5: Worker queue append only"]},
            trigger="Reset",
            date_str="2026-03-06",
            dry_run=False,
            index_history=False,
            update_project_md=False,
        )

        assert "Worker queue append only" in project_log.read_text(encoding="utf-8")
        assert project_md.read_text(encoding="utf-8") == before
        assert metrics["history_entries_written"] == 1
        assert metrics["entries_written"] == 0

    def test_project_log_history_uses_quaid_now_when_date_not_passed(self, setup_env, monkeypatch):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_log = tmp_path / "projects" / "test-project" / "PROJECT.log"
        monkeypatch.setenv("QUAID_NOW", "2026-03-07T15:30:00")

        append_project_logs(
            {"test-project": ["Session 5: Added migration notes"]},
            trigger="Reset",
            dry_run=False,
        )

        assert "- 2026-03-07 [Reset] Added migration notes" in project_md.read_text()
        assert "- [2026-03-07T15:30:00] Added migration notes" in project_log.read_text()

    def test_project_log_now_fallback_is_utc_aware(self, setup_env, monkeypatch):
        from datetime import timezone

        from datastore.docsdb.project_updater import _project_log_now

        monkeypatch.delenv("QUAID_NOW", raising=False)

        assert _project_log_now().tzinfo is timezone.utc

    def test_project_log_history_rejects_malformed_quaid_now(self, setup_env, monkeypatch):
        from datastore.docsdb.project_updater import append_project_logs

        monkeypatch.setenv("QUAID_NOW", "not-a-date")

        with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
            append_project_logs(
                {"test-project": ["Session 5: Added migration notes"]},
                trigger="Reset",
                dry_run=False,
            )

    def test_structured_project_logs_preserve_per_entry_dates(self, setup_env):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_log = tmp_path / "projects" / "test-project" / "PROJECT.log"

        metrics = append_project_logs(
            {
                "test-project": [
                    {"text": "Shipped retry middleware", "created_at": "2026-03-01T09:15:00"},
                    {"text": "Added error banner", "created_at": "2026-03-05"},
                ]
            },
            trigger="Compaction",
            date_str="2026-03-07",
            dry_run=False,
        )

        assert metrics["entries_seen"] == 2
        assert metrics["entries_written"] == 2
        content = project_md.read_text(encoding="utf-8")
        assert "- 2026-03-01 [Compaction] Shipped retry middleware" in content
        assert "- 2026-03-05 [Compaction] Added error banner" in content

        history = project_log.read_text(encoding="utf-8")
        assert "- [2026-03-01T09:15:00] Shipped retry middleware" in history
        assert "- [2026-03-05T23:59:59] Added error banner" in history
        assert "2026-03-07T23:59:59" not in history

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

    def test_unknown_project_reroutes_to_quaid_project_log_when_available(self, setup_env, capsys):
        from config import ProjectDefinition
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        registry = _get_registry()
        quaid_dir = tmp_path / "projects" / "quaid"
        quaid_dir.mkdir(parents=True, exist_ok=True)
        quaid_md = quaid_dir / "PROJECT.md"
        quaid_log = quaid_dir / "PROJECT.log"
        quaid_md.write_text(
            render_project_md_template(
                label="Quaid",
                description="Quaid meta project.",
                project_home=str(quaid_dir),
                source_roots=[str(tmp_path / "src")],
                exclude_patterns=["*.log", "*.db"],
            ),
            encoding="utf-8",
        )
        registry.save_project_definition(
            "quaid",
            ProjectDefinition(
                label="Quaid",
                home_dir="projects/quaid/",
                source_roots=[str(tmp_path / "src")],
                auto_index=True,
                patterns=["*.md"],
                exclude=["*.log", "*.db", "__pycache__/"],
                description="Quaid meta project.",
                state="active",
            ),
        )

        metrics = append_project_logs(
            {"does-not-exist": ["Session 1: should reroute"]},
            trigger="Compaction",
            date_str="2026-03-03",
            dry_run=False,
        )

        assert metrics["projects_seen"] == 1
        assert metrics["projects_unknown"] == 1
        assert metrics["projects_updated"] == 1
        assert metrics["entries_written"] == 1
        content = quaid_md.read_text(encoding="utf-8")
        assert "[from deleted/unknown project does-not-exist] should reroute" in content
        history = quaid_log.read_text(encoding="utf-8")
        assert "[from deleted/unknown project does-not-exist] should reroute" in history
        out = capsys.readouterr().out
        assert "rerouting 1 entries to quaid" in out

    def test_missing_project_md_writes_history_and_warns(self, setup_env, capsys):
        from datastore.docsdb.project_updater import append_project_logs

        tmp_path = setup_env
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_log = tmp_path / "projects" / "test-project" / "PROJECT.log"
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
        assert metrics["projects_history_only"] == 1
        assert metrics["history_entries_written"] == 1
        assert metrics["history_logs_indexed"] == 1
        assert metrics["history_chunks_indexed"] == 2
        assert project_log.exists()
        assert "missing file" in project_log.read_text(encoding="utf-8")
        out = capsys.readouterr().out
        assert "[project-log][warn] missing PROJECT.md:" in out
        assert "history_only_entries=1" in out

    def test_project_log_index_failure_warns_and_continues_when_fail_open(
        self,
        setup_env,
        monkeypatch,
        capsys,
    ):
        from config import ProjectDefinition
        import datastore.docsdb.project_updater as project_updater

        tmp_path = setup_env
        registry = _get_registry()
        second_dir = tmp_path / "projects" / "second-project"
        second_dir.mkdir(parents=True, exist_ok=True)
        (second_dir / "PROJECT.md").write_text(
            render_project_md_template(
                label="Second Project",
                description="Second test project.",
                project_home=str(second_dir),
                source_roots=[str(tmp_path / "src")],
                exclude_patterns=["*.log", "*.db"],
            ),
            encoding="utf-8",
        )
        registry.save_project_definition(
            "second-project",
            ProjectDefinition(
                label="Second Project",
                home_dir="projects/second-project/",
                source_roots=[str(tmp_path / "src")],
                auto_index=True,
                patterns=["*.md"],
                exclude=["*.log", "*.db", "__pycache__/"],
                description="Second test project.",
                state="active",
            ),
        )

        def _index(log_path, *, project_name, trigger):
            if project_name == "test-project":
                raise RuntimeError("index unavailable")
            return 2

        monkeypatch.setattr(project_updater, "_index_project_history_log", _index)
        monkeypatch.setattr(project_updater, "is_fail_hard_enabled", lambda: False)

        metrics = project_updater.append_project_logs(
            {
                "test-project": ["Session 1: first project"],
                "second-project": ["Session 1: second project"],
            },
            trigger="Compaction",
            date_str="2026-03-03",
            dry_run=False,
        )

        assert metrics["entries_written"] == 2
        assert metrics["history_entries_written"] == 2
        assert metrics["history_index_failures"] == 1
        assert metrics["history_logs_indexed"] == 1
        assert metrics["history_chunks_indexed"] == 2
        assert "second project" in (second_dir / "PROJECT.log").read_text(encoding="utf-8")
        out = capsys.readouterr().out
        assert "failed PROJECT.log index" in out

    def test_project_log_index_failure_raises_when_fail_hard(self, setup_env, monkeypatch):
        import datastore.docsdb.project_updater as project_updater

        def _index(log_path, *, project_name, trigger):
            raise RuntimeError("index unavailable")

        monkeypatch.setattr(project_updater, "_index_project_history_log", _index)
        monkeypatch.setattr(project_updater, "is_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="index unavailable"):
            project_updater.append_project_logs(
                {"test-project": ["Session 1: fail hard"]},
                trigger="Compaction",
                date_str="2026-03-03",
                dry_run=False,
            )

    def test_project_log_zero_chunks_warns_without_fail_hard_abort(self, setup_env, monkeypatch):
        import datastore.docsdb.project_updater as project_updater

        monkeypatch.setattr(project_updater, "_index_project_history_log", lambda *a, **k: 0)
        monkeypatch.setattr(project_updater, "is_fail_hard_enabled", lambda: True)

        metrics = project_updater.append_project_logs(
            {"test-project": ["Session 1: tiny"]},
            trigger="Compaction",
            date_str="2026-03-03",
            dry_run=False,
        )

        assert metrics["entries_written"] == 1
        assert metrics["history_logs_unindexed"] == 1
        assert metrics["history_logs_indexed"] == 0

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
