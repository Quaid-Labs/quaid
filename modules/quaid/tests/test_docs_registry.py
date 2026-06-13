"""Tests for docs_registry.py — CRUD, path resolution, project operations."""

import json
import logging
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Point to a temp DB for isolation
_tmp_db = None


@pytest.fixture(autouse=True)
def setup_env(tmp_path, monkeypatch):
    """Set up isolated test environment."""
    global _tmp_db
    from lib.adapter import set_adapter, reset_adapter, TestAdapter
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    iroot = adapter.instance_root()

    _tmp_db = iroot / "test_registry.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(_tmp_db))
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(iroot))  # kept for backward compat

    # Create minimal config
    shared_projects_dir = tmp_path / "projects"
    shared_projects_dir.mkdir(parents=True, exist_ok=True)
    instance_projects = iroot / "projects"
    if not instance_projects.exists():
        instance_projects.symlink_to(shared_projects_dir, target_is_directory=True)
    (shared_projects_dir / "staging").mkdir(parents=True, exist_ok=True)

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
    (iroot / "config.json").write_text(json.dumps(config_data))

    # Create project directory
    (shared_projects_dir / "test-project").mkdir(parents=True, exist_ok=True)
    (iroot / "src").mkdir(exist_ok=True)
    from lib.project_registry import register as global_project_register
    global_project_register(
        name="test-project",
        canonical_path=str(shared_projects_dir / "test-project"),
        description="A test project",
    )

    # Patch config paths to use test config and WORKSPACE in docs_registry
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import config as config_mod
    monkeypatch.setattr(config_mod, "_config_paths", lambda: [iroot / "config.json"])
    config_mod.reload_config()

    yield iroot

    # Reset config cache and adapter so they don't pollute subsequent test files
    config_mod._config = None
    reset_adapter()


def _get_registry(tmp_path=None):
    from datastore.docsdb.registry import DocsRegistry
    return DocsRegistry(db_path=_tmp_db)


class TestFailHardPolicy:
    def test_fail_hard_policy_import_failure_fails_closed(self, setup_env, monkeypatch, caplog):
        from datastore.docsdb import registry as registry_mod

        real_import = __import__

        def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "lib.fail_policy":
                raise ImportError("missing fail policy")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr("builtins.__import__", blocked_import)

        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(RuntimeError, match="Failed to load fail-hard policy"):
                registry_mod._fail_hard_enabled()

        assert "Failed to load fail-hard policy" in caplog.text


class TestConfigReloadPolicy:
    def test_reload_config_failure_warns_when_fail_open(self, setup_env, monkeypatch, caplog):
        import config as config_mod
        from datastore.docsdb import registry as registry_mod

        def fail_reload():
            raise OSError("reload failed")

        monkeypatch.setattr(config_mod, "reload_config", fail_reload)
        monkeypatch.setattr(registry_mod, "_fail_hard_enabled", lambda: False)

        with caplog.at_level(logging.WARNING):
            assert registry_mod._reload_config_after_project_change("create_project") is False

        assert "Config reload skipped after create_project" in caplog.text

    def test_reload_config_failure_raises_when_fail_hard(self, setup_env, monkeypatch):
        import config as config_mod
        from datastore.docsdb import registry as registry_mod

        def fail_reload():
            raise OSError("reload failed")

        monkeypatch.setattr(config_mod, "reload_config", fail_reload)
        monkeypatch.setattr(registry_mod, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="Failed to reload config after delete_project"):
            registry_mod._reload_config_after_project_change("delete_project")


class TestEnsureTable:
    def test_idempotent(self, setup_env):
        """Table creation is idempotent."""
        r = _get_registry()
        r.ensure_table()
        r.ensure_table()  # Second call should not error

    def test_project_definition_write_handles_fresh_memory_connections(self, setup_env, monkeypatch):
        """Project-definition writes initialize schema on their active connection."""
        from config import ProjectDefinition
        from datastore.docsdb.registry import DocsRegistry

        r = DocsRegistry(db_path=Path(":memory:"), seed_projects=False)
        monkeypatch.setattr(r, "_ensure_global_project_entry", lambda *args, **kwargs: True)

        r.save_project_definition(
            "memory-proj",
            ProjectDefinition(label="Memory Project", home_dir="projects/memory-proj/"),
        )

    def test_register_handles_fresh_memory_connections(self, setup_env, monkeypatch):
        """Document registration initializes schema on its active connection."""
        from datastore.docsdb.registry import DocsRegistry

        r = DocsRegistry(db_path=Path(":memory:"), seed_projects=False)
        monkeypatch.setattr(r, "_ensure_global_project_entry", lambda *args, **kwargs: True)

        assert r.register("durable-docs/example.md", project="memory-proj") == 1

    def test_doc_registry_has_identity_columns(self, setup_env):
        """Forward-compatible identity/source columns are present."""
        r = _get_registry()
        from lib.database import get_connection
        with get_connection(r.db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(doc_registry)").fetchall()}
        required = {
            "source_channel",
            "source_conversation_id",
            "source_author_id",
            "speaker_entity_id",
            "subject_entity_id",
            "conversation_id",
            "visibility_scope",
            "sensitivity",
            "participant_entity_ids",
            "provenance_confidence",
        }
        assert required.issubset(cols)

    def test_seeded_project_definition_coerces_list_fields(self, setup_env):
        """Seed from config normalizes malformed list-like project fields."""
        cfg_path = setup_env / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["projects"]["definitions"]["test-project"]["sourceRoots"] = {"bad": "type"}
        cfg["projects"]["definitions"]["test-project"]["patterns"] = "not-a-list"
        cfg["projects"]["definitions"]["test-project"]["exclude"] = {"bad": "type"}
        cfg_path.write_text(json.dumps(cfg))

        r = _get_registry()
        pd = r.get_project_definition("test-project")
        assert pd is not None
        assert pd.source_roots == []
        assert pd.patterns == ["*.md"]
        assert pd.exclude == ["*.db", "*.log", "*.pyc", "__pycache__/"]


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

    def test_register_rejects_path_escape(self, setup_env):
        r = _get_registry()
        with pytest.raises(ValueError, match="outside workspace"):
            r.register("../../../escape.md", project="test-project")

    def test_register_rejects_unsafe_project_name(self, setup_env):
        r = _get_registry()
        visible_home = setup_env.parents[1]

        with pytest.raises(ValueError, match="Invalid project name"):
            r.register("docs/test.md", project="../../escape")

        assert not (visible_home.parent / "escape").exists()

    def test_register_rejects_overlong_project_name(self, setup_env):
        r = _get_registry()
        with pytest.raises(ValueError, match="Invalid project name"):
            r.register("docs/test.md", project="a" * 129)

    def test_read_rejects_corrupt_outside_registry_path(self, setup_env):
        from lib.database import get_connection

        r = _get_registry()
        outside = setup_env.parents[1].parent / "outside-secret.md"
        outside.write_text("secret", encoding="utf-8")
        try:
            with get_connection(r.db_path) as conn:
                r._ensure_doc_registry_table(conn)
                conn.execute(
                    """
                    INSERT INTO doc_registry (file_path, project, asset_type, title, registered_by)
                    VALUES (?, 'test-project', 'doc', 'Outside Secret', 'test')
                    """,
                    (str(outside),),
                )

            with pytest.raises(ValueError, match="outside workspace"):
                r.read(str(outside))
        finally:
            outside.unlink(missing_ok=True)

    def test_cli_register_relative_path_uses_current_directory(self, setup_env, monkeypatch, capsys):
        from datastore.docsdb import registry as registry_mod

        visible_home = setup_env.parents[1]
        source_root = visible_home / "projects" / "livetest-agentmsg-xp-src"
        source_root.mkdir(parents=True, exist_ok=True)
        doc_path = source_root / "STATUS.md"
        doc_path.write_text("# Status\n\nOC XP doc-add content.\n", encoding="utf-8")

        monkeypatch.chdir(source_root)
        monkeypatch.setattr(registry_mod.tempfile, "gettempdir", lambda: str(setup_env / "tmp"))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "registry.py",
                "register",
                "STATUS.md",
                "--project",
                "test-project",
                "--json",
            ],
        )

        registry_mod.main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["file_path"] == "projects/livetest-agentmsg-xp-src/STATUS.md"
        assert payload["indexing"] == "async"
        assert "project-docs supervisor" in payload["message"]

        r = _get_registry()
        entry = r.get(payload["file_path"])
        assert entry is not None
        assert r._resolve_path(entry["file_path"]).resolve() == doc_path.resolve()
        assert r.get("STATUS.md") is None

    def test_cli_register_rejects_missing_file_path(self, setup_env, monkeypatch, capsys):
        from datastore.docsdb import registry as registry_mod

        visible_home = setup_env.parents[1]
        source_root = visible_home / "projects" / "livetest-agentmsg-xp-src"
        source_root.mkdir(parents=True, exist_ok=True)

        monkeypatch.chdir(source_root)
        monkeypatch.setattr(registry_mod.tempfile, "gettempdir", lambda: str(setup_env / "tmp"))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "registry.py",
                "register",
                "api.py",
                "--project",
                "test-project",
            ],
        )

        with pytest.raises(SystemExit):
            registry_mod.main()

        captured = capsys.readouterr()
        assert "File not found: api.py" in captured.err

        r = _get_registry()
        assert r.list_docs() == []

    def test_register_upsert_same_project(self, setup_env):
        r = _get_registry()
        id1 = r.register("docs/test.md", project="proj-a", title="V1")
        id2 = r.register("docs/test.md", project="proj-a", title="V2")
        # Same row updated
        assert id1 == id2
        entry = r.get("docs/test.md")
        assert entry["project"] == "proj-a"
        assert entry["title"] == "V2"

    def test_register_rejects_cross_project_reassign(self, setup_env):
        r = _get_registry()
        r.register("docs/test.md", project="proj-a", title="V1")

        with pytest.raises(ValueError, match="Use move-file to reassign ownership explicitly"):
            r.register("docs/test.md", project="proj-b", title="V2")

    def test_register_with_source_files(self, setup_env):
        r = _get_registry()
        r.register("docs/api.md", project="test-project",
                    auto_update=True, source_files=["src/routes.js", "src/server.js"])
        entry = r.get("docs/api.md")
        assert entry["auto_update"] is True
        assert entry["source_files"] == ["src/routes.js", "src/server.js"]

    def test_register_with_identity_context(self, setup_env):
        r = _get_registry()
        r.register(
            "docs/identity.md",
            project="test-project",
            source_channel="telegram",
            source_conversation_id="group-42",
            source_author_id="operator-alias",
            speaker_entity_id="entity:fatman",
            subject_entity_id="entity:albert",
            conversation_id="conv:group-42",
            visibility_scope="source_shared",
            sensitivity="normal",
            participant_entity_ids=["entity:fatman", "entity:albert"],
            provenance_confidence=0.87,
        )
        entry = r.get("docs/identity.md")
        assert entry is not None
        assert entry["source_channel"] == "telegram"
        assert entry["source_conversation_id"] == "group-42"
        assert entry["source_author_id"] == "operator-alias"
        assert entry["speaker_entity_id"] == "entity:fatman"
        assert entry["subject_entity_id"] == "entity:albert"
        assert entry["conversation_id"] == "conv:group-42"
        assert entry["visibility_scope"] == "source_shared"
        assert entry["sensitivity"] == "normal"
        assert entry["participant_entity_ids"] == ["entity:fatman", "entity:albert"]
        assert entry["provenance_confidence"] == pytest.approx(0.87)


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


class TestTitleExtraction:
    def test_extract_title_logs_failure(self, setup_env, caplog):
        from datastore.docsdb.registry import DocsRegistry

        r = DocsRegistry(db_path=_tmp_db)
        caplog.set_level("WARNING")
        with patch("builtins.open", side_effect=OSError("no read")):
            title = r._extract_title(Path("/tmp/missing.md"))

        assert title is None
        assert "Failed to extract markdown title" in caplog.text


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
        assert "## Primary Artifacts" in content
        assert "## Where To Learn More" in content

    def test_atomic_write_text_keeps_existing_file_when_replace_fails(self, setup_env, monkeypatch):
        from datastore.docsdb import registry as registry_mod

        project_md = setup_env / "projects" / "test-project" / "PROJECT.md"
        project_md.write_text("existing content", encoding="utf-8")

        def _fail_replace(_src, _dst):
            raise OSError("replace failed")

        monkeypatch.setattr(registry_mod.os, "replace", _fail_replace)

        with pytest.raises(OSError, match="replace failed"):
            registry_mod._atomic_write_text(project_md, "new content")

        assert project_md.read_text(encoding="utf-8") == "existing content"
        assert list(project_md.parent.glob(f".{project_md.name}.*.tmp")) == []

    def test_project_md_lock_path_matches_updater(self, setup_env):
        from datastore.docsdb import registry as registry_mod
        from datastore.docsdb import updater

        project_md = setup_env / "projects" / "test-project" / "PROJECT.md"

        assert registry_mod._project_md_lock_path(project_md) == updater._project_md_lock_path(project_md)
        assert registry_mod._project_md_lock_path(project_md).name == ".PROJECT.md.lock"

    def test_project_md_lock_failure_raises_when_fail_hard(self, setup_env, monkeypatch):
        from datastore.docsdb import registry as registry_mod

        project_md = setup_env / "projects" / "test-project" / "PROJECT.md"
        project_md.write_text("existing content", encoding="utf-8")

        def _fail_flock(*_args):
            raise OSError("flock unavailable")

        monkeypatch.setitem(
            sys.modules,
            "fcntl",
            SimpleNamespace(LOCK_EX=1, LOCK_UN=2, flock=_fail_flock),
        )
        monkeypatch.setattr(registry_mod, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="Failed to acquire PROJECT.md lock"):
            registry_mod._atomic_write_text(project_md, "new content")

        assert project_md.read_text(encoding="utf-8") == "existing content"

    def test_write_text_if_missing_checks_existence_under_lock(self, setup_env, monkeypatch):
        from datastore.docsdb import registry as registry_mod

        project_md = setup_env / "projects" / "test-project" / "PROJECT.md"
        project_md.unlink(missing_ok=True)

        @contextmanager
        def _seed_during_lock(_path):
            project_md.write_text("seeded by racing writer", encoding="utf-8")
            yield

        monkeypatch.setattr(registry_mod, "_project_md_write_lock", _seed_during_lock)

        wrote = registry_mod._write_text_if_missing(project_md, "new scaffold")

        assert wrote is False
        assert project_md.read_text(encoding="utf-8") == "seeded by racing writer"

    def test_rejects_unsafe_project_name_before_scaffold(self, setup_env):
        r = _get_registry()
        visible_home = setup_env.parents[1]

        with pytest.raises(ValueError, match="Invalid project name"):
            r.create_project("../../escape")

        assert not (visible_home.parent / "escape").exists()


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

    def test_update_metadata_normalizes_identity_scope_fields(self, setup_env):
        r = _get_registry()
        r.register("docs/test.md")
        r.update_metadata(
            "docs/test.md",
            source_channel="  Telegram  ",
            source_conversation_id="  chat-1  ",
            source_author_id="  operator-alias  ",
        )
        entry = r.get("docs/test.md")
        assert entry["source_channel"] == "telegram"
        assert entry["source_conversation_id"] == "chat-1"
        assert entry["source_author_id"] == "operator-alias"


class TestUpdateTimestamps:
    def test_update_indexed_at(self, setup_env):
        r = _get_registry()
        r.register("docs/test.md")
        r.update_timestamps("docs/test.md", indexed_at="2026-02-07T12:00:00")
        entry = r.get("docs/test.md")
        assert entry["last_indexed_at"] == "2026-02-07T12:00:00"

    def test_reregister_clears_last_indexed_at(self, setup_env):
        r = _get_registry()
        r.register("docs/test.md", title="First")
        r.update_timestamps("docs/test.md", indexed_at="2026-02-07T12:00:00")
        assert r.get("docs/test.md")["last_indexed_at"] == "2026-02-07T12:00:00"

        # Re-registering should force a fresh index pass instead of preserving
        # a stale timestamp that can hide missing vec rows.
        r.register("docs/test.md", title="Second")
        entry = r.get("docs/test.md")
        assert entry["title"] == "Second"
        assert entry["last_indexed_at"] is None


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

    def test_archive_project_clears_docs_rag_chunks(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir(exist_ok=True)
        target_doc = docs_dir / "archive-me.md"
        target_doc.write_text("# Archive Me\ncontent", encoding="utf-8")
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_md.write_text("# Test Project\n", encoding="utf-8")

        r.register("docs/archive-me.md", project="test-project")

        with patch("datastore.docsdb.rag.DocsRAG.remove_chunks_for_path", return_value=1) as mock_remove:
            result = r.archive_project("test-project")

        assert result["rag_chunks_deleted"] == 2
        removed_paths = {args[0] for args, _kwargs in mock_remove.call_args_list}
        assert "docs/archive-me.md" in removed_paths
        assert str(project_md.resolve()) in removed_paths

    def test_archive_project_rag_cleanup_failure_raises_when_fail_hard(self, setup_env, monkeypatch):
        from datastore.docsdb import registry as registry_mod

        r = _get_registry()
        r.register("projects/test-project/PROJECT.md", project="test-project")

        def _fail_remove(*_args, **_kwargs):
            raise OSError("rag cleanup failed")

        monkeypatch.setattr("datastore.docsdb.rag.DocsRAG.remove_chunks_for_path", _fail_remove)
        monkeypatch.setattr(registry_mod, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="Docs RAG cleanup failed"):
            r.archive_project("test-project")

        assert len(r.list_docs(project="test-project", state="active")) == 1


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

        with patch("datastore.docsdb.registry.subprocess.run") as mock_run:
            mock_run.side_effect = _trash_side_effect
            result = r.delete_project("test-project")

        assert result["dir_deleted"] is True
        assert not (tmp_path / "projects" / "test-project").exists()

    def test_delete_project_clears_docs_rag_chunks(self, setup_env):
        tmp_path = setup_env
        r = _get_registry()
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir(exist_ok=True)
        target_doc = docs_dir / "linger.md"
        target_doc.write_text("# Linger\nremoved project content", encoding="utf-8")
        project_md = tmp_path / "projects" / "test-project" / "PROJECT.md"
        project_md.write_text("# Test Project\n", encoding="utf-8")

        r.register("docs/linger.md", project="test-project")

        def _trash_side_effect(cmd, **kwargs):
            target = Path(cmd[1])
            if target.exists():
                import shutil
                shutil.rmtree(target)
            return None

        with patch("datastore.docsdb.registry.subprocess.run") as mock_run, \
             patch("datastore.docsdb.rag.DocsRAG.remove_chunks_for_path", return_value=1) as mock_remove:
            mock_run.side_effect = _trash_side_effect
            result = r.delete_project("test-project")

        assert result["rag_chunks_deleted"] == 2
        removed_paths = {args[0] for args, _kwargs in mock_remove.call_args_list}
        assert "docs/linger.md" in removed_paths
        assert str(project_md.resolve()) in removed_paths


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
    """Tests for create_project writing to config.json."""

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

    def test_normalizes_project_name_to_lowercase(self, setup_env):
        r = _get_registry()
        r.create_project("New-Proj")
        assert r.get_project_definition("new-proj") is not None
        assert (setup_env / "projects" / "new-proj").is_dir()

    def test_rejects_empty_name(self, setup_env):
        r = _get_registry()
        with pytest.raises(ValueError, match="Project name is required"):
            r.create_project("")

    def test_preserves_markerized_seed_project_md(self, setup_env):
        from lib.project_templates import render_project_md_template

        tmp_path = setup_env
        r = _get_registry()
        project_dir = tmp_path / "projects" / "new-proj"
        project_dir.mkdir(parents=True, exist_ok=True)
        seeded = render_project_md_template(
            label="New Proj",
            description="Seeded project.",
            project_home="QUAID_HOME/projects/new-proj",
            source_roots=["modules/new-proj"],
            exclude_patterns=["*.tmp"],
        ).replace(
            "## Primary Artifacts",
            "## Start Here By Task\n- Open `docs/overview.md` first.\n\n## Primary Artifacts",
            1,
        )
        (project_dir / "PROJECT.md").write_text(seeded, encoding="utf-8")

        r.create_project("new-proj")

        content = (project_dir / "PROJECT.md").read_text(encoding="utf-8")
        assert "## Start Here By Task" in content
        assert "- Open `docs/overview.md` first." in content
        assert str(setup_env.parents[1] / "projects" / "new-proj") in content


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

    def test_normalizes_new_name(self, setup_env):
        r = _get_registry()
        r.register("docs/a.md", project="old-name")
        r.rename_project("old-name", "Renamed-Proj")
        assert r.get_project_definition("renamed-proj") is not None

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

    def test_rename_rolls_back_directory_when_db_update_fails(self, setup_env, monkeypatch):
        """rename_project does not leave filesystem moved when DB rename fails."""
        tmp_path = setup_env
        r = _get_registry()
        r.register("projects/test-project/PROJECT.md", project="test-project")

        old_dir = tmp_path / "projects" / "test-project"
        new_dir = tmp_path / "projects" / "renamed-proj"
        assert old_dir.exists()

        def _fail_project_definition_write(_conn, _name, _defn):
            raise sqlite3.OperationalError("forced project definition failure")

        monkeypatch.setattr(r, "_write_project_definition_row_on_conn", _fail_project_definition_write)

        with pytest.raises(sqlite3.OperationalError, match="forced project definition failure"):
            r.rename_project("test-project", "renamed-proj")

        assert old_dir.exists()
        assert not new_dir.exists()
        assert r.get_project_definition("test-project") is not None
        assert r.get_project_definition("renamed-proj") is None
        entry = r.get("projects/test-project/PROJECT.md")
        assert entry is not None
        assert entry["project"] == "test-project"

    def test_rename_updates_source_roots_refreshes_project_md_and_global_registry(self, setup_env, monkeypatch):
        """rename_project keeps source_roots/global registry in sync and refreshes PROJECT.md."""
        tmp_path = setup_env
        r = _get_registry()

        proj_dir = tmp_path / "projects" / "test-project"
        (proj_dir / "PROJECT.md").write_text(
            """# Project: Test Project

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
        )
        (proj_dir / "README.md").write_text("# Readme\n")
        r.register("projects/test-project/PROJECT.md", project="test-project")
        r.register("projects/test-project/README.md", project="test-project")

        captured = {}

        def fake_global_rename(old_name, new_name, canonical_path=None):
            captured["old_name"] = old_name
            captured["new_name"] = new_name
            captured["canonical_path"] = canonical_path
            return {"canonical_path": canonical_path}

        monkeypatch.setattr("lib.project_registry.rename", fake_global_rename)

        result = r.rename_project("test-project", "renamed-proj")
        assert result["renamed"] == 2

        new_defn = r.get_project_definition("renamed-proj")
        assert new_defn is not None
        assert new_defn.home_dir == "projects/renamed-proj/"
        assert new_defn.source_roots == ["src/"]

        renamed_project_md = (tmp_path / "projects" / "renamed-proj" / "PROJECT.md").read_text()
        assert "## What This Is" in renamed_project_md
        assert "- `PROJECT.md`" in renamed_project_md
        assert "- `README.md`" in renamed_project_md
        assert "projects/test-project/README.md" not in renamed_project_md

        assert captured == {
            "old_name": "test-project",
            "new_name": "renamed-proj",
            "canonical_path": str(setup_env.parents[1] / "projects" / "renamed-proj"),
        }

    def test_rename_rewrites_project_prefixed_source_roots(self, setup_env):
        """rename_project rewrites project-local source_roots to the new home."""
        r = _get_registry()
        defn = r.get_project_definition("test-project")
        assert defn is not None
        defn.source_roots = ["projects/test-project", "src/", "projects/test-project/docs"]
        r.save_project_definition("test-project", defn)
        r.register("projects/test-project/PROJECT.md", project="test-project")

        r.rename_project("test-project", "renamed-proj")
        renamed = r.get_project_definition("renamed-proj")
        assert renamed is not None
        assert renamed.source_roots == [
            "projects/renamed-proj",
            "src/",
            "projects/renamed-proj/docs",
        ]

    def test_rename_global_registry_failure_raises_when_fail_hard(self, setup_env, monkeypatch):
        from datastore.docsdb import registry as registry_mod

        r = _get_registry()
        r.register("projects/test-project/PROJECT.md", project="test-project")

        def _fail_global_rename(*_args, **_kwargs):
            raise RuntimeError("global registry unavailable")

        monkeypatch.setattr("lib.project_registry.rename", _fail_global_rename)
        monkeypatch.setattr(registry_mod, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="Failed to rename global project registry entry"):
            r.rename_project("test-project", "renamed-proj")

        assert r.get_project_definition("renamed-proj") is not None

    def test_delete_removes_global_registry_entry(self, setup_env, monkeypatch):
        """delete_project removes the matching global registry entry."""
        tmp_path = setup_env
        r = _get_registry()
        project_dir = tmp_path / "projects" / "test-project"
        (project_dir / "PROJECT.md").write_text("# Project: Test Project\n")
        r.register("projects/test-project/PROJECT.md", project="test-project")

        removed = {}

        def fake_global_remove(name, force=False):
            removed["name"] = name
            removed["force"] = force
            return True

        monkeypatch.setattr("lib.project_registry.remove", fake_global_remove)

        result = r.delete_project("test-project")
        assert result["deleted"] == 1
        assert removed == {"name": "test-project", "force": True}


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

    def test_get_project_definition_falls_back_when_json_list_columns_are_wrong_type(self, setup_env):
        from config import ProjectDefinition
        from lib.database import get_connection

        r = _get_registry()
        defn = ProjectDefinition(
            label="Bad JSON Project",
            home_dir="projects/bad-json/",
            source_roots=["src/"],
            patterns=["*.md"],
            exclude=["*.log"],
        )
        r.save_project_definition("bad-json-proj", defn)

        with get_connection(r.db_path) as conn:
            conn.execute(
                """
                UPDATE project_definitions
                SET source_roots = ?, patterns = ?, exclude = ?
                WHERE name = ?
                """,
                ('{"oops":"not-a-list"}', '{"patterns":"bad"}', '{"exclude":"bad"}', "bad-json-proj"),
            )

        loaded = r.get_project_definition("bad-json-proj")
        assert loaded is not None
        assert loaded.source_roots == []
        assert loaded.patterns == ["*.md"]
        assert loaded.exclude == []

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

    def test_save_project_definition_allows_docs_definition_description_update(self, setup_env):
        """Docs-side project definition edits should still update docs DB metadata."""
        from config import ProjectDefinition
        from lib.project_registry import register as global_project_register

        r = _get_registry()
        global_project_register(
            name="test-project",
            canonical_path=str(Path(setup_env).parent.parent / "projects" / "test-project"),
            description="existing global description",
            link_current_instance=False,
        )
        edited_defn = ProjectDefinition(
            label="Test Project",
            home_dir="projects/test-project/",
            source_roots=["src/"],
            auto_index=True,
            patterns=["*.md"],
            exclude=["*.log", "__pycache__/"],
            description="docs-side updated description",
            state="active",
        )
        r.save_project_definition("test-project", edited_defn, link_current_instance=False)

        loaded = r.get_project_definition("test-project")
        assert loaded.description == "docs-side updated description"

    def test_global_reconcile_preserves_cli_updated_description(self, setup_env):
        """Stale docs workers must not revert project update descriptions."""
        from config import ProjectDefinition
        from lib.project_registry import lookup as global_project_lookup
        from lib.project_registry import register as global_project_register

        r = _get_registry()
        canonical = Path(setup_env).parent.parent / "projects" / "test-project"
        global_project_register(
            name="test-project",
            canonical_path=str(canonical),
            description="agentmsg livetest fixture",
            link_current_instance=False,
        )
        global_project_register(
            name="test-project",
            canonical_path=str(canonical),
            description="updated livetest fixture",
            link_current_instance=False,
        )

        stale_defn = ProjectDefinition(
            label="Test Project",
            home_dir="projects/test-project/",
            source_roots=["src/"],
            auto_index=True,
            patterns=["*.md"],
            exclude=["*.log", "__pycache__/"],
            description="agentmsg livetest fixture",
            state="active",
        )
        r.save_project_definition("test-project", stale_defn, link_current_instance=False)

        assert r.get_project_definition("test-project").description == "agentmsg livetest fixture"
        r.reconcile_global_project_registry()
        assert global_project_lookup("test-project")["description"] == "updated livetest fixture"

    def test_global_reconcile_path_resolution_failure_raises_under_failhard(self, setup_env, monkeypatch):
        """failHard should not hide path-resolution failures during global sync."""
        from config import ProjectDefinition
        from datastore.docsdb import registry as registry_mod
        from lib import project_registry as global_project_registry

        r = _get_registry()
        canonical = Path(setup_env).parent.parent / "projects" / "test-project"
        global_project_registry.register(
            name="test-project",
            canonical_path=str(canonical),
            description="existing global description",
            link_current_instance=False,
        )
        stale_defn = ProjectDefinition(
            label="Test Project",
            home_dir="projects/test-project/",
            source_roots=["src/"],
            auto_index=True,
            patterns=["*.md"],
            exclude=["*.log", "__pycache__/"],
            description="docs-side description",
            state="active",
        )
        r.save_project_definition("test-project", stale_defn, link_current_instance=False)

        original_path = registry_mod.Path
        broken_existing_path = str(canonical) + "-broken"

        class _ExplodingPath(type(original_path())):
            def resolve(self, *args, **kwargs):
                if str(self) == broken_existing_path:
                    raise OSError("path resolution failed")
                return super().resolve(*args, **kwargs)

        monkeypatch.setattr(
            global_project_registry,
            "lookup",
            lambda name: {"canonical_path": broken_existing_path, "description": "existing global description"},
        )
        monkeypatch.setattr(registry_mod, "Path", _ExplodingPath)
        monkeypatch.setattr(registry_mod, "_fail_hard_enabled", lambda: True)

        with pytest.raises(RuntimeError, match="Failed resolving project registry path"):
            r.reconcile_global_project_registry()

    def test_save_project_definition_retries_transient_locked_db(self, setup_env):
        """Transient sqlite lock during project-definition save should retry and succeed."""
        from config import ProjectDefinition
        from datastore.docsdb import registry as registry_mod

        r = _get_registry()
        real_get_connection = registry_mod.get_connection
        calls = {"count": 0}

        @contextmanager
        def flaky_get_connection(db_path=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("database is locked")
            with real_get_connection(db_path) as conn:
                yield conn

        defn = ProjectDefinition(
            label="Retry Project",
            home_dir="projects/retry-proj/",
            description="Retried description",
        )

        with patch("datastore.docsdb.registry.get_connection", flaky_get_connection):
            with patch("datastore.docsdb.registry.time.sleep", lambda _s: None):
                r.save_project_definition("retry-proj", defn)

        loaded = r.get_project_definition("retry-proj")
        assert calls["count"] == 2
        assert loaded is not None
        assert loaded.description == "Retried description"
