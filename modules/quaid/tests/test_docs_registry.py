"""Tests for docs_registry.py — CRUD, path resolution, project operations."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Point to a temp DB for isolation
_tmp_db = None


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    """Set up isolated test environment."""
    global _tmp_db
    _tmp_db = tmp_path / "test_registry.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(_tmp_db))
    # Use adapter for workspace isolation
    from lib.adapter import set_adapter, reset_adapter, StandaloneAdapter
    set_adapter(StandaloneAdapter(home=tmp_path))
    monkeypatch.setenv("CLAWDBOT_WORKSPACE", str(tmp_path))  # kept for backward compat

    # Create minimal config
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    projects_dir = tmp_path / "projects" / "staging"
    projects_dir.mkdir(parents=True)

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
                    "exclude": ["*.log", "__pycache__/"],
                    "description": "A test project"
                }
            },
            "defaultProject": "default"
        },
        "docs": {
            "sourceMapping": {},
            "docPurposes": {},
            "coreMarkdown": {"enabled": False}
        },
        "rag": {"docsDir": "docs"},
    }
    (config_dir / "memory.json").write_text(json.dumps(config_data))

    # Create project directory
    (tmp_path / "projects" / "test-project").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir(exist_ok=True)

    # Patch config paths to use test config and WORKSPACE in docs_registry
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import config as config_mod
    monkeypatch.setattr(config_mod, "_config_paths", lambda: [tmp_path / "config" / "memory.json"])
    config_mod.reload_config()

    yield tmp_path

    # Reset config cache and adapter so they don't pollute subsequent test files
    config_mod._config = None
    reset_adapter()


def _get_registry(tmp_path=None):
    from core.docs.registry import DocsRegistry
    return DocsRegistry(db_path=_tmp_db)


class TestEnsureTable:
    def test_idempotent(self, setup_env):
        """Table creation is idempotent."""
        r = _get_registry()
        r.ensure_table()
        r.ensure_table()  # Second call should not error


class TestRegisterAndGet:
    def test_register_basic(self, setup_env):
        r = _get_registry()
        row_id = r.register("docs/test.md", project="test-project", title="Test Doc")
        assert row_id > 0

        entry = r.get("docs/test.md")
        assert entry is not None
        assert entry["file_path"] == "docs/test.md"
        assert entry["project"] == "test-project"
        assert entry["title"] == "Test Doc"
        assert entry["state"] == "active"

    def test_register_upsert(self, setup_env):
        r = _get_registry()
        id1 = r.register("docs/test.md", project="proj-a", title="V1")
        id2 = r.register("docs/test.md", project="proj-b", title="V2")
        # Same row updated
        assert id1 == id2
        entry = r.get("docs/test.md")
        assert entry["project"] == "proj-b"
        assert entry["title"] == "V2"

    def test_register_with_source_files(self, setup_env):
        r = _get_registry()
        r.register("docs/api.md", project="test-project",
                    auto_update=True, source_files=["src/routes.js", "src/server.js"])
        entry = r.get("docs/api.md")
        assert entry["auto_update"] is True
        assert entry["source_files"] == ["src/routes.js", "src/server.js"]


class TestListDocs:
    def test_list_by_project(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", project="proj-a")
        r.register("docs/b.md", project="proj-b")
        r.register("docs/c.md", project="proj-a")

        proj_a = r.list_docs(project="proj-a")
        assert len(proj_a) == 2
        assert all(d["project"] == "proj-a" for d in proj_a)

    def test_list_by_type(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", asset_type="doc")
        r.register("notes/b.md", asset_type="note")

        docs = r.list_docs(asset_type="doc")
        assert len(docs) == 1
        assert docs[0]["asset_type"] == "doc"

    def test_list_excludes_deleted(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md")
        r.register("docs/b.md")
        r.unregister("docs/b.md")

        active = r.list_docs()
        assert len(active) == 1
        assert active[0]["file_path"] == "docs/a.md"


class TestRead:
    def test_read_by_path(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        # Create actual file
        doc_path = tmp_path / "docs" / "test.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Test\nContent here")

        r.register("docs/test.md", title="Test Doc")
        entry = r.read("docs/test.md")
        assert entry is not None
        assert entry["content"] == "# Test\nContent here"

    def test_read_by_title(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        doc_path = tmp_path / "docs" / "test.md"
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text("# Test\nContent here")

        r.register("docs/test.md", title="My Doc Title")
        entry = r.read("My Doc Title")
        assert entry is not None
        assert entry["file_path"] == "docs/test.md"


class TestUnregister:
    def test_soft_delete(self, setup_env):
        r = _get_registry()
        r.register("docs/test.md")
        ok = r.unregister("docs/test.md")
        assert ok is True

        # No longer returned by get
        assert r.get("docs/test.md") is None

        # Still in DB with state=deleted
        deleted = r.list_docs(state="deleted")
        assert len(deleted) == 1


class TestFindProjectForPath:
    def test_in_directory(self, setup_env):
        """Files in project homeDir auto-belong."""
        r = _get_registry()
        project = r.find_project_for_path("projects/test-project/readme.md")
        assert project == "test-project"

    def test_external_via_registry(self, setup_env):
        """External files found via registry lookup."""
        r = _get_registry()
        r.register("external/file.md", project="test-project")
        project = r.find_project_for_path("external/file.md")
        assert project == "test-project"

    def test_source_root(self, setup_env):
        """Files under sourceRoots belong to that project."""
        r = _get_registry()
        project = r.find_project_for_path("src/utils.py")
        assert project == "test-project"

    def test_source_file_reverse_lookup(self, setup_env):
        """Files tracked as source_files found via reverse lookup."""
        r = _get_registry()
        r.register("docs/api.md", project="test-project",
                    source_files=["lib/handler.py"])
        project = r.find_project_by_source_file("lib/handler.py")
        assert project == "test-project"

    def test_not_found(self, setup_env):
        r = _get_registry()
        project = r.find_project_for_path("random/file.txt")
        assert project is None


class TestGetSourceMappings:
    def test_returns_auto_update_docs(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", project="test-project",
                    auto_update=True, source_files=["src/a.py"])
        r.register("docs/b.md", project="test-project",
                    auto_update=False, source_files=["src/b.py"])

        mappings = r.get_source_mappings()
        assert "docs/a.md" in mappings
        assert "docs/b.md" not in mappings
        assert mappings["docs/a.md"] == ["src/a.py"]

    def test_filter_by_project(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", project="proj-a",
                    auto_update=True, source_files=["src/a.py"])
        r.register("docs/b.md", project="proj-b",
                    auto_update=True, source_files=["src/b.py"])

        mappings = r.get_source_mappings(project="proj-a")
        assert "docs/a.md" in mappings
        assert "docs/b.md" not in mappings


class TestAutoDiscover:
    def test_finds_new_files(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()

        # Create files in project dir
        proj_dir = tmp_path / "projects" / "test-project"
        (proj_dir / "readme.md").write_text("# Readme")
        (proj_dir / "notes.md").write_text("# Notes")

        found = r.auto_discover("test-project")
        assert len(found) == 2

        # Second call finds nothing new
        found2 = r.auto_discover("test-project")
        assert len(found2) == 0

    def test_respects_exclusions(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()

        proj_dir = tmp_path / "projects" / "test-project"
        (proj_dir / "readme.md").write_text("# Readme")
        (proj_dir / "debug.log").write_text("log content")

        found = r.auto_discover("test-project")
        # .log is excluded
        paths = [f for f in found]
        assert all(".log" not in p for p in paths)


class TestCreateProject:
    def test_scaffolds_directory(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        project_md = r.create_project("my-essay", label="My Essay", description="An essay project")

        assert project_md.exists()
        content = project_md.read_text()
        assert "# Project: My Essay" in content
        assert "An essay project" in content


class TestSyncFromChunks:
    def test_migrates_chunks(self, setup_env):
        from lib.database import get_connection
        r = _get_registry()

        # Create a mock doc_chunks table with entries
        with get_connection(_tmp_db) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS doc_chunks (
                    id TEXT PRIMARY KEY,
                    source_file TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    section_header TEXT,
                    embedding BLOB,
                    created_at TEXT DEFAULT (datetime('now')),
                    updated_at TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                INSERT INTO doc_chunks (id, source_file, chunk_index, content, embedding)
                VALUES ('test:0', ?, 0, 'chunk content', X'00')
            """, (str(setup_env / "docs" / "test.md"),))

        (setup_env / "docs").mkdir(exist_ok=True)
        (setup_env / "docs" / "test.md").write_text("# Test")

        count = r.sync_from_chunks()
        assert count == 1


class TestUpdateMetadata:
    def test_update_title(self, setup_env):
        r = _get_registry()
        r.register("docs/test.md", title="Old Title")
        r.update_metadata("docs/test.md", title="New Title")
        entry = r.get("docs/test.md")
        assert entry["title"] == "New Title"

    def test_update_source_files(self, setup_env):
        r = _get_registry()
        r.register("docs/test.md")
        r.update_metadata("docs/test.md", source_files=["src/a.py"])
        entry = r.get("docs/test.md")
        assert entry["source_files"] == ["src/a.py"]


class TestUpdateTimestamps:
    def test_update_indexed_at(self, setup_env):
        r = _get_registry()
        r.register("docs/test.md")
        r.update_timestamps("docs/test.md", indexed_at="2026-02-07T12:00:00")
        entry = r.get("docs/test.md")
        assert entry["last_indexed_at"] == "2026-02-07T12:00:00"


class TestRenameProject:
    def test_renames_registry_entries(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        r.register("docs/a.md", project="old-name")
        r.register("docs/b.md", project="old-name")
        r.register("docs/c.md", project="other")

        result = r.rename_project("old-name", "new-name")
        assert result["renamed"] == 2

        # Old project should be empty
        assert len(r.list_docs(project="old-name")) == 0
        # New project should have the docs
        assert len(r.list_docs(project="new-name")) == 2
        # Other project untouched
        assert len(r.list_docs(project="other")) == 1

    def test_moves_directory(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()

        # Create project dir with a file
        proj_dir = tmp_path / "projects" / "test-project"
        (proj_dir / "notes.md").write_text("# Notes")
        r.register("projects/test-project/notes.md", project="test-project")

        result = r.rename_project("test-project", "renamed-project")
        assert result["dir_moved"] is True
        assert (tmp_path / "projects" / "renamed-project" / "notes.md").exists()
        assert not (tmp_path / "projects" / "test-project").exists()


class TestArchiveProject:
    def test_archives_entries(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        r.register("docs/a.md", project="test-project")
        r.register("docs/b.md", project="test-project")

        result = r.archive_project("test-project")
        assert result["archived"] == 2

        # No active docs remain
        assert len(r.list_docs(project="test-project")) == 0
        # But they exist as archived
        assert len(r.list_docs(project="test-project", state="archived")) == 2

    def test_moves_dir_to_archive(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        (tmp_path / "projects" / "test-project" / "notes.md").write_text("# Notes")

        result = r.archive_project("test-project")
        assert result["dir_moved"] is True
        assert (tmp_path / "projects" / "archive" / "test-project" / "notes.md").exists()
        assert not (tmp_path / "projects" / "test-project").exists()


class TestDeleteProject:
    def test_deletes_entries(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", project="test-project")
        r.register("docs/b.md", project="test-project")

        result = r.delete_project("test-project")
        assert result["deleted"] == 2
        assert len(r.list_docs(project="test-project")) == 0

    def test_removes_directory(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        (tmp_path / "projects" / "test-project" / "notes.md").write_text("# Notes")

        def _trash_side_effect(cmd, **kwargs):
            target = Path(cmd[1])
            if target.exists():
                import shutil
                shutil.rmtree(target)
            return None

        with patch("core.docs.registry.subprocess.run") as mock_run:
            mock_run.side_effect = _trash_side_effect
            result = r.delete_project("test-project")

        assert result["dir_deleted"] is True
        assert not (tmp_path / "projects" / "test-project").exists()


class TestMoveFile:
    def test_reassigns_project_in_registry(self, setup_env):
        """move_file only updates registry — no physical file movement."""
        tmp_path = setup_env
        r = _get_registry()

        # Create source file
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "moving.md").write_text("# Moving")
        r.register("docs/moving.md", project="default", description="A doc")

        result = r.move_file("docs/moving.md", "test-project")
        assert result["moved"] is True

        # File should stay at original location (registry-only move)
        assert (tmp_path / "docs" / "moving.md").exists()

        # Registry should point to new project at same path
        entry = r.get("docs/moving.md")
        assert entry is not None
        assert entry["project"] == "test-project"
        assert entry["description"] == "A doc"  # metadata preserved

    def test_move_already_in_target(self, setup_env):
        """Moving a file already in the target project just updates registry."""
        tmp_path = setup_env
        r = _get_registry()

        proj_dir = tmp_path / "projects" / "test-project"
        (proj_dir / "existing.md").write_text("# Existing")
        r.register("projects/test-project/existing.md", project="other")

        result = r.move_file("projects/test-project/existing.md", "test-project")
        assert result["moved"] is True

        entry = r.get("projects/test-project/existing.md")
        assert entry["project"] == "test-project"

    def test_rejects_nonexistent_project(self, setup_env):
        """move_file should reject moves to non-existent projects."""
        r = _get_registry()
        (setup_env / "docs").mkdir(exist_ok=True)
        (setup_env / "docs" / "test.md").write_text("# Test")
        r.register("docs/test.md", project="default")
        with pytest.raises(ValueError, match="does not exist"):
            r.move_file("docs/test.md", "fake-project")

    def test_rejects_unregistered_file(self, setup_env):
        """move_file should reject files not in the registry."""
        r = _get_registry()
        with pytest.raises(ValueError, match="not registered"):
            r.move_file("docs/nonexistent.md", "test-project")


class TestCreateProjectConfig:
    """Tests for create_project writing to config/memory.json."""

    def test_creates_config_entry(self, setup_env):
        """create_project writes definition to DB (source of truth)."""
        tmp_path = setup_env
        r = _get_registry()
        r.create_project("new-proj", label="New Proj", description="Testing")

        defn = r.get_project_definition("new-proj")
        assert defn is not None
        assert defn.label == "New Proj"
        assert defn.home_dir == "projects/new-proj/"

    def test_registers_project_md(self, setup_env):
        r = _get_registry()
        r.create_project("new-proj")
        entry = r.get("projects/new-proj/PROJECT.md")
        assert entry is not None
        assert entry["project"] == "new-proj"

    def test_rejects_invalid_name(self, setup_env):
        r = _get_registry()
        with pytest.raises(ValueError, match="Invalid project name"):
            r.create_project("../../etc")

    def test_rejects_empty_name(self, setup_env):
        r = _get_registry()
        with pytest.raises(ValueError, match="Invalid project name"):
            r.create_project("")


class TestRegisterValidation:
    def test_rejects_empty_path(self, setup_env):
        r = _get_registry()
        with pytest.raises(ValueError, match="non-empty"):
            r.register("")

    def test_rejects_whitespace_path(self, setup_env):
        r = _get_registry()
        with pytest.raises(ValueError, match="non-empty"):
            r.register("   ")


class TestRenameProjectGuards:
    def test_rejects_rename_to_existing(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", project="proj-a")
        r.register("docs/b.md", project="proj-b")

        with pytest.raises(ValueError, match="already has"):
            r.rename_project("proj-a", "proj-b")

    def test_rejects_invalid_new_name(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", project="old-name")
        with pytest.raises(ValueError, match="Invalid project name"):
            r.rename_project("old-name", "../escape")

    def test_db_updated_after_rename(self, setup_env):
        """rename_project updates DB definition (source of truth)."""
        tmp_path = setup_env
        r = _get_registry()
        r.register("docs/a.md", project="test-project")
        result = r.rename_project("test-project", "renamed-proj")
        assert result["renamed"] >= 0

        # Old definition should be soft-deleted, new one active
        assert r.get_project_definition("test-project") is None
        new_defn = r.get_project_definition("renamed-proj")
        assert new_defn is not None
        assert new_defn.home_dir == "projects/renamed-proj/"


class TestPathPrefixBoundary:
    """Ensure path matching doesn't have false positives on similar prefixes."""

    def test_no_false_positive_on_similar_prefix(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        # test-project exists, test-project-extra should NOT match
        project = r.find_project_for_path("projects/test-project-extra/readme.md")
        assert project is None

    def test_source_root_boundary(self, setup_env):
        """src/ root shouldn't match src-old/ path."""
        r = _get_registry()
        project = r.find_project_for_path("src-old/file.py")
        assert project is None


class TestExclusionBoundary:
    """Ensure exclusion patterns don't have false positives."""

    def test_pycache_doesnt_match_partial(self, setup_env):
        r = _get_registry()
        # __pycache__/ pattern should not match a file with 'cache' in name
        assert r._is_excluded("/some/my_pycache_helper.py", ["__pycache__/"]) is False

    def test_pycache_matches_directory(self, setup_env):
        r = _get_registry()
        assert r._is_excluded("/some/__pycache__/module.pyc", ["__pycache__/"]) is True


class TestListProjects:
    def test_returns_project_info(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", project="test-project")
        projects = r.list_projects()
        assert len(projects) >= 1
        tp = [p for p in projects if p["name"] == "test-project"][0]
        assert tp["label"] == "Test Project"
        assert tp["doc_count"] == 1


class TestSyncExternalFiles:
    def test_parses_external_files_table(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()

        # Write a PROJECT.md with external files table
        proj_dir = tmp_path / "projects" / "test-project"
        project_md = proj_dir / "PROJECT.md"
        project_md.write_text("""# Project: Test

## Overview
Test project.

## Files & Assets

### In This Directory

### External Files
| File | Purpose | Auto-Update |
|------|---------|-------------|
| docs/api-reference.md | API docs | No |
| docs/design.md | Architecture | No |

## Documents
""")
        found = r.sync_external_files("test-project")
        assert len(found) == 2
        assert "docs/api-reference.md" in found

    def test_idempotent(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()

        proj_dir = tmp_path / "projects" / "test-project"
        project_md = proj_dir / "PROJECT.md"
        project_md.write_text("""# Project: Test

## Overview

## Files & Assets

### In This Directory

### External Files
| File | Purpose | Auto-Update |
|------|---------|-------------|
| docs/a.md | Docs | No |

## Documents
""")
        found1 = r.sync_external_files("test-project")
        found2 = r.sync_external_files("test-project")
        assert len(found1) == 1
        assert len(found2) == 0  # Already registered


class TestProcessEventMalformedJson:
    """Test that corrupt event files are handled gracefully."""

    def test_invalid_json(self, setup_env):
        tmp_path = setup_env
        import core.docs.project_updater as project_updater
        monkeypatch_module = None

        event_file = tmp_path / "projects" / "staging" / "bad-event.json"
        event_file.write_text("not valid json {{{")

        result = project_updater.process_event(str(event_file))
        assert result["success"] is False
        assert "invalid_event_json" in result["error"]
        # Event file should be cleaned up
        assert not event_file.exists()

    def test_empty_file(self, setup_env):
        tmp_path = setup_env
        import core.docs.project_updater as project_updater

        event_file = tmp_path / "projects" / "staging" / "empty-event.json"
        event_file.write_text("")

        result = project_updater.process_event(str(event_file))
        assert result["success"] is False


class TestVerifyProject:
    def test_finds_missing_files(self, setup_env):
        r = _get_registry()
        r.register("docs/exists.md", project="test-project")
        r.register("docs/missing.md", project="test-project")

        # Create only one file
        (setup_env / "docs").mkdir(exist_ok=True)
        (setup_env / "docs" / "exists.md").write_text("# Exists")

        result = r.verify_project("test-project")
        assert result["total"] == 2
        assert result["exists"] == 1
        assert "docs/missing.md" in result["missing"]

    def test_finds_orphans(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()

        proj_dir = tmp_path / "projects" / "test-project"
        (proj_dir / "registered.md").write_text("# Registered")
        (proj_dir / "orphan.md").write_text("# Orphan")

        r.register("projects/test-project/registered.md", project="test-project")

        result = r.verify_project("test-project")
        assert "projects/test-project/orphan.md" in result["orphans"]
        assert "projects/test-project/registered.md" not in result["orphans"]


class TestAtomicConfigWrite:
    """Verify config writes are atomic (temp file + rename)."""

    def test_config_survives_partial_write(self, setup_env):
        """Config file should not be corrupted on write failure."""
        tmp_path = setup_env
        r = _get_registry()
        config_path = tmp_path / "config" / "memory.json"

        original = config_path.read_text()
        # A successful update should work
        ok = r._update_config(lambda d: d.setdefault("test_key", "test_value"))
        assert ok is True
        # Config should have the new key
        updated = json.loads(config_path.read_text())
        assert updated.get("test_key") == "test_value"
        # No .tmp file left behind
        assert not config_path.with_suffix(".tmp").exists()

    def test_config_missing_returns_false(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        config_path = tmp_path / "config" / "memory.json"
        config_path.unlink()

        ok = r._update_config(lambda d: d.update({"x": 1}))
        assert ok is False


class TestReadEncodingSafety:
    """Verify read() handles non-UTF8 files gracefully."""

    def test_binary_file_returns_error(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()

        # Create a binary file
        bin_path = tmp_path / "binary.bin"
        bin_path.write_bytes(b"\x80\x81\x82\xff\xfe")
        r.register("binary.bin", project="test-project")

        result = r.read("binary.bin")
        assert result is not None
        assert result["content"] is None
        assert "content_error" in result

    def test_utf8_file_reads_fine(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()

        md_path = tmp_path / "test.md"
        md_path.write_text("# Hello World", encoding="utf-8")
        r.register("test.md", project="test-project")

        result = r.read("test.md")
        assert result["content"] == "# Hello World"
        assert "content_error" not in result


class TestRenameProjectPathFix:
    """Verify rename_project correctly updates file paths."""

    def test_path_prefix_updated_correctly(self, setup_env):
        """File paths should use old_name prefix (not new_name) for matching."""
        tmp_path = setup_env
        r = _get_registry()

        # Register files with test-project prefix (matches config)
        r.register("projects/test-project/a.md", project="test-project")
        r.register("projects/test-project/b.md", project="test-project")

        result = r.rename_project("test-project", "new-project")
        assert result["renamed"] == 2

        # Paths should have been updated
        docs = r.list_docs(project="new-project")
        paths = [d["file_path"] for d in docs]
        assert "projects/new-project/a.md" in paths
        assert "projects/new-project/b.md" in paths

    def test_rename_without_config_still_works(self, setup_env):
        """Projects not in config should still rename registry entries."""
        r = _get_registry()
        r.register("docs/x.md", project="untracked")

        result = r.rename_project("untracked", "renamed")
        assert result["renamed"] == 1
        assert len(r.list_docs(project="renamed")) == 1


class TestConsistentProcessEventReturn:
    """Verify process_event returns consistent structure."""

    def test_error_return_has_all_keys(self, setup_env):
        tmp_path = setup_env
        import core.docs.project_updater as project_updater

        result = project_updater.process_event("/nonexistent/event.json")
        assert "success" in result
        assert "project" in result
        assert "updates" in result
        assert "trigger" in result
        assert "error" in result
        assert result["success"] is False

    def test_invalid_json_has_all_keys(self, setup_env):
        tmp_path = setup_env
        import core.docs.project_updater as project_updater

        event_file = tmp_path / "projects" / "staging" / "bad.json"
        event_file.write_text("{not json")
        result = project_updater.process_event(str(event_file))

        assert "success" in result
        assert "project" in result
        assert "updates" in result
        assert "trigger" in result
        assert "error" in result


# ============================================================================
# Project definitions DB tests
# ============================================================================

class TestProjectDefinitionsTable:
    """Tests for project_definitions DB table and CRUD."""

    def test_table_created(self, setup_env):
        """ensure_table creates project_definitions table."""
        from lib.database import get_connection
        r = _get_registry()
        with get_connection(r.db_path) as conn:
            tables = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            assert "project_definitions" in tables

    def test_seed_from_json(self, setup_env):
        """Empty table seeds from JSON config."""
        r = _get_registry()
        defs = r.get_all_project_definitions()
        assert "test-project" in defs
        assert defs["test-project"].label == "Test Project"
        assert defs["test-project"].home_dir == "projects/test-project/"

    def test_seed_skips_when_populated(self, setup_env):
        """Seeding is idempotent — doesn't duplicate when table already has data."""
        r = _get_registry()
        # First seed happened in ensure_table
        defs1 = r.get_all_project_definitions()
        count1 = len(defs1)

        # Re-seed should not add duplicates
        r._seed_projects_from_json()
        defs2 = r.get_all_project_definitions()
        assert len(defs2) == count1

    def test_save_and_get_project_definition(self, setup_env):
        """Round-trip save/load of a project definition."""
        from config import ProjectDefinition
        r = _get_registry()
        defn = ProjectDefinition(
            label="My New Project",
            home_dir="projects/new-proj/",
            source_roots=["src/", "lib/"],
            auto_index=True,
            patterns=["*.md", "*.txt"],
            exclude=["*.log"],
            description="A test project definition",
        )
        r.save_project_definition("new-proj", defn)
        loaded = r.get_project_definition("new-proj")
        assert loaded is not None
        assert loaded.label == "My New Project"
        assert loaded.home_dir == "projects/new-proj/"
        assert loaded.source_roots == ["src/", "lib/"]
        assert loaded.patterns == ["*.md", "*.txt"]
        assert loaded.exclude == ["*.log"]
        assert loaded.description == "A test project definition"
        assert loaded.auto_index is True

    def test_delete_project_definition_sets_state(self, setup_env):
        """Soft delete sets state to 'deleted', not hard delete."""
        from lib.database import get_connection
        r = _get_registry()
        r.delete_project_definition("test-project")

        # Should not appear in active definitions
        defs = r.get_all_project_definitions()
        assert "test-project" not in defs

        # But should still exist in DB with state='deleted'
        with get_connection(r.db_path) as conn:
            row = conn.execute(
                "SELECT state FROM project_definitions WHERE name = ?",
                ("test-project",)
            ).fetchone()
            assert row is not None
            assert row["state"] == "deleted"

    def test_get_all_excludes_deleted(self, setup_env):
        """Only active definitions returned by get_all_project_definitions."""
        from config import ProjectDefinition
        r = _get_registry()
        # Add another project then delete it
        defn = ProjectDefinition(
            label="Temp Project", home_dir="projects/temp/",
        )
        r.save_project_definition("temp-proj", defn)
        r.delete_project_definition("temp-proj")

        defs = r.get_all_project_definitions()
        assert "temp-proj" not in defs
        assert "test-project" in defs  # Original still active

    def test_create_project_writes_to_db(self, setup_env):
        """create_project writes definition to DB."""
        tmp_path = setup_env
        r = _get_registry()
        r.create_project("db-test-proj", label="DB Test")

        defn = r.get_project_definition("db-test-proj")
        assert defn is not None
        assert defn.label == "DB Test"

    def test_get_project_definition_returns_none_for_missing(self, setup_env):
        """get_project_definition returns None for non-existent project."""
        r = _get_registry()
        assert r.get_project_definition("nonexistent") is None

    def test_save_project_definition_upsert(self, setup_env):
        """save_project_definition updates existing definition."""
        from config import ProjectDefinition
        r = _get_registry()

        # Update existing test-project
        defn = ProjectDefinition(
            label="Updated Label",
            home_dir="projects/test-project/",
            description="Updated description",
        )
        r.save_project_definition("test-project", defn)

        loaded = r.get_project_definition("test-project")
        assert loaded.label == "Updated Label"
        assert loaded.description == "Updated description"
