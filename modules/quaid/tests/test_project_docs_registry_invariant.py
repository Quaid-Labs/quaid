"""Project/docs registry consistency invariants."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest


@pytest.fixture
def project_registry_env(tmp_path, monkeypatch):
    from lib.adapter import TestAdapter, reset_adapter, set_adapter

    quaid_home = tmp_path / ".quaid"
    visible_home = tmp_path / "quaid"
    monkeypatch.setenv("QUAID_HOME", str(quaid_home))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_home))
    monkeypatch.setenv("QUAID_INSTANCE", "benchrunner")
    monkeypatch.setenv("MEMORY_DB_PATH", str(quaid_home / "shared" / "data" / "docs.db"))
    adapter = TestAdapter(quaid_home)
    set_adapter(adapter)

    import datastore.docsdb.registry as docs_registry

    monkeypatch.setattr(docs_registry.tempfile, "gettempdir", lambda: "/__quaid_tmp_guard_for_tests__")
    try:
        yield {"quaid_home": quaid_home, "visible_home": visible_home}
    finally:
        reset_adapter()


def test_docs_registration_creates_canonical_project_entry(project_registry_env):
    from core.project_registry import get_project
    from datastore.docsdb.registry import DocsRegistry

    visible_home = project_registry_env["visible_home"]
    source_root = visible_home / "src" / "recipe-app"
    source_root.mkdir(parents=True)
    (source_root / "app.py").write_text("def cook(): pass\n", encoding="utf-8")
    project_dir = visible_home / "projects" / "recipe-app"
    project_dir.mkdir(parents=True)
    project_md = project_dir / "PROJECT.md"
    project_md.write_text("# Recipe App\n", encoding="utf-8")

    registry = DocsRegistry()
    registry.register(
        str(project_md),
        project="recipe-app",
        source_files=[str(source_root / "app.py")],
        registered_by="pytest",
    )

    entry = get_project("recipe-app")
    assert entry is not None
    assert entry["canonical_path"] == str(project_dir)
    assert entry["source_root"] == str(source_root)
    assert "benchrunner" in entry["instances"]


def test_project_names_are_normalized_to_lowercase(project_registry_env):
    from core.project_registry import get_project
    from datastore.docsdb.registry import DocsRegistry

    visible_home = project_registry_env["visible_home"]
    source_root = visible_home / "src" / "agentmsg-cdx"
    source_root.mkdir(parents=True)
    project_dir = visible_home / "projects" / "livetest-agentmsg-cdx"
    project_dir.mkdir(parents=True)
    project_md = project_dir / "PROJECT.md"
    project_md.write_text("# Agent Message CDX\n", encoding="utf-8")

    registry = DocsRegistry()
    registry.register(
        str(project_md),
        project="livetest-agentmsg-CDX",
        source_files=[str(source_root / "app.py")],
        registered_by="pytest",
    )

    docs = registry.list_docs(project="LIVETEST-AGENTMSG-CDX")
    assert docs and docs[0]["project"] == "livetest-agentmsg-cdx"
    assert get_project("livetest-agentmsg-CDX") is not None


def test_global_project_cleanup_paths_normalize_mixed_case_input(project_registry_env):
    from lib.project_registry import link, lookup, register, remove, unlink

    project_dir = project_registry_env["visible_home"] / "projects" / "mixed-cleanup"
    project_dir.mkdir(parents=True)

    register(
        name="Mixed-Cleanup",
        canonical_path=str(project_dir),
        link_current_instance=False,
    )
    assert lookup("MIXED-CLEANUP") is not None
    assert link("MIXED-CLEANUP", instance="benchrunner") is True
    # Only project names normalize; instance identifiers remain exact.
    assert unlink("mixed-cleanup", instance="BENCHRUNNER") is False
    assert unlink("MIXED-CLEANUP", instance="benchrunner") is True
    assert remove("MIXED-CLEANUP", force=True) is True
    assert lookup("mixed-cleanup") is None


def test_global_project_registry_corrupt_file_warns_when_fail_open(project_registry_env, monkeypatch, caplog):
    from lib import project_registry

    registry_file = project_registry_env["quaid_home"] / "project-registry.json"
    registry_file.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(project_registry, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level(logging.WARNING, logger="lib.project_registry"):
        data = project_registry._load()

    assert data == {"projects": {}, "deleted_projects": {}}
    assert "Failed to load project registry" in caplog.text


def test_global_project_registry_corrupt_file_raises_when_failhard(project_registry_env, monkeypatch):
    from lib import project_registry

    registry_file = project_registry_env["quaid_home"] / "project-registry.json"
    registry_file.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(project_registry, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Failed to load project registry") as excinfo:
        project_registry._load()

    assert isinstance(excinfo.value.__cause__, json.JSONDecodeError)


def test_create_project_symlink_adapter_failure_falls_back_when_fail_open(
    project_registry_env,
    monkeypatch,
    caplog,
):
    from lib import adapter as adapter_mod
    from lib import project_registry

    canonical = project_registry_env["visible_home"] / "canonical" / "demo"
    canonical.mkdir(parents=True)

    def _broken_adapter():
        raise RuntimeError("adapter unavailable")

    monkeypatch.setattr(adapter_mod, "get_adapter", _broken_adapter)
    monkeypatch.setattr(project_registry, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level(logging.WARNING, logger="lib.project_registry"):
        project_registry._create_project_symlink("demo", str(canonical))

    link_path = project_registry_env["visible_home"] / "projects" / "demo"
    assert link_path.is_symlink()
    assert link_path.resolve() == canonical.resolve()
    assert "Failed to resolve adapter projects dir" in caplog.text


def test_create_project_symlink_adapter_failure_raises_when_failhard(project_registry_env, monkeypatch):
    from lib import adapter as adapter_mod
    from lib import project_registry

    canonical = project_registry_env["visible_home"] / "canonical" / "demo"
    canonical.mkdir(parents=True)

    def _broken_adapter():
        raise RuntimeError("adapter unavailable")

    monkeypatch.setattr(adapter_mod, "get_adapter", _broken_adapter)
    monkeypatch.setattr(project_registry, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="adapter unavailable"):
        project_registry._create_project_symlink("demo", str(canonical))

    assert not (project_registry_env["visible_home"] / "projects" / "demo").exists()


def test_global_project_registry_timestamps_honor_quaid_now(project_registry_env, monkeypatch):
    from lib.project_registry import link, lookup, mark_deleted, register, rename, unlink

    project_dir = project_registry_env["visible_home"] / "projects" / "clocked"
    project_dir.mkdir(parents=True)

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:00:00Z")
    entry = register(
        name="Clocked",
        canonical_path=str(project_dir),
        link_current_instance=False,
    )
    assert entry["created_at"] == "2026-03-11T05:00:00+00:00"

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:01:00Z")
    assert link("CLOCKED", instance="benchrunner") is True
    assert lookup("clocked")["updated_at"] == "2026-03-11T05:01:00+00:00"

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:02:00Z")
    assert unlink("clocked", instance="benchrunner") is True
    assert lookup("clocked")["updated_at"] == "2026-03-11T05:02:00+00:00"

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:03:00Z")
    register(
        name="clocked",
        canonical_path=str(project_dir),
        description="updated",
        link_current_instance=False,
    )
    assert lookup("clocked")["updated_at"] == "2026-03-11T05:03:00+00:00"

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:04:00Z")
    renamed = rename("clocked", "clocked-renamed")
    assert renamed["updated_at"] == "2026-03-11T05:04:00+00:00"

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:05:00Z")
    mark_deleted("old-project")
    registry = json.loads(
        (project_registry_env["quaid_home"] / "project-registry.json").read_text(encoding="utf-8")
    )
    assert registry["deleted_projects"]["old-project"] == "2026-03-11T05:05:00+00:00"


def test_global_project_registry_rejects_malformed_quaid_now(project_registry_env, monkeypatch):
    from lib.project_registry import register

    project_dir = project_registry_env["visible_home"] / "projects" / "bad-clock"
    project_dir.mkdir(parents=True)
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        register(
            name="bad-clock",
            canonical_path=str(project_dir),
            link_current_instance=False,
        )


def test_docs_registry_project_definition_timestamps_honor_quaid_now(project_registry_env, monkeypatch):
    from config import ProjectDefinition
    from datastore.docsdb import registry as registry_mod
    from datastore.docsdb.registry import DocsRegistry
    from lib.database import get_connection

    registry = DocsRegistry(seed_projects=False)
    monkeypatch.setattr(registry_mod, "_reload_config_after_project_change", lambda _action: True)
    monkeypatch.setattr(registry, "_ensure_global_project_entry", lambda *_args, **_kwargs: True)

    def _definition(name: str, *, description: str = "clocked project"):
        return ProjectDefinition(
            label=name.replace("-", " ").title(),
            home_dir=f"projects/{name}/",
            source_roots=[],
            auto_index=True,
            patterns=["*.md"],
            exclude=[],
            description=description,
            state="active",
        )

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:00")
    with get_connection(registry.db_path) as conn:
        registry._write_project_definition_row_on_conn(conn, "clocked-docs", _definition("clocked-docs"))
    with get_connection(registry.db_path) as conn:
        row = conn.execute(
            "SELECT created_at, updated_at FROM project_definitions WHERE name = ?",
            ("clocked-docs",),
        ).fetchone()
        assert tuple(row) == ("2026-03-11T05:06:00+00:00", "2026-03-11T05:06:00+00:00")

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:07:00Z")
    with get_connection(registry.db_path) as conn:
        registry._write_project_definition_row_on_conn(
            conn,
            "clocked-docs",
            _definition("clocked-docs", description="updated"),
        )
    with get_connection(registry.db_path) as conn:
        row = conn.execute(
            "SELECT description, created_at, updated_at FROM project_definitions WHERE name = ?",
            ("clocked-docs",),
        ).fetchone()
        assert tuple(row) == ("updated", "2026-03-11T05:06:00+00:00", "2026-03-11T05:07:00+00:00")

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:08:00Z")
    registry.delete_project_definition("clocked-docs")
    with get_connection(registry.db_path) as conn:
        row = conn.execute(
            "SELECT state, updated_at FROM project_definitions WHERE name = ?",
            ("clocked-docs",),
        ).fetchone()
        assert tuple(row) == ("deleted", "2026-03-11T05:08:00+00:00")

    with get_connection(registry.db_path) as conn:
        registry._write_project_definition_row_on_conn(conn, "rename-old", _definition("rename-old"))
        conn.execute(
            """
            INSERT INTO doc_registry (file_path, project, asset_type, title, state, registered_by)
            VALUES (?, ?, 'doc', ?, 'active', 'pytest')
            """,
            ("projects/rename-old/PROJECT.md", "rename-old", "Project: Rename Old"),
        )

    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:09:00Z")
    monkeypatch.setattr(
        registry,
        "_get_config",
        lambda: SimpleNamespace(projects=SimpleNamespace(definitions={"rename-old": _definition("rename-old")})),
    )
    monkeypatch.setattr("lib.project_registry.rename", lambda *_args, **_kwargs: {"name": "rename-new"})
    monkeypatch.setattr("datastore.docsdb.project_updater.refresh_project_md", lambda _name: True)
    registry.rename_project("rename-old", "rename-new")
    with get_connection(registry.db_path) as conn:
        row = conn.execute(
            "SELECT state, updated_at FROM project_definitions WHERE name = ?",
            ("rename-old",),
        ).fetchone()
        assert tuple(row) == ("deleted", "2026-03-11T05:09:00+00:00")
        assert conn.execute(
            "SELECT updated_at FROM project_definitions WHERE name = ?",
            ("rename-new",),
        ).fetchone()[0] == "2026-03-11T05:09:00+00:00"


def test_docs_registry_project_definition_rejects_malformed_quaid_now(project_registry_env, monkeypatch):
    from config import ProjectDefinition
    from datastore.docsdb import registry as registry_mod
    from datastore.docsdb.registry import DocsRegistry
    from lib.database import get_connection

    registry = DocsRegistry(seed_projects=False)
    monkeypatch.setenv("QUAID_NOW", "not-a-date")
    monkeypatch.setattr(registry_mod, "_fail_hard_enabled", lambda: True)

    with get_connection(registry.db_path) as conn:
        with pytest.raises(RuntimeError, match="Invalid QUAID_NOW"):
            registry._write_project_definition_row_on_conn(
                conn,
                "bad-clock",
                ProjectDefinition(
                    label="Bad Clock",
                    home_dir="projects/bad-clock/",
                    source_roots=[],
                    auto_index=True,
                    patterns=["*.md"],
                    exclude=[],
                    description="bad clock",
                    state="active",
                ),
            )


def test_docs_registry_current_instance_fallback_logs_failure(project_registry_env, monkeypatch, caplog):
    import lib.instance as instance_mod
    from datastore.docsdb import registry as registry_mod

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setattr(instance_mod, "instance_id", lambda: (_ for _ in ()).throw(RuntimeError("instance unavailable")))

    with caplog.at_level(logging.DEBUG, logger="datastore.docsdb.registry"):
        assert registry_mod._current_quaid_instance_id() == ""

    assert "Failed resolving current Quaid instance id" in caplog.text
    assert "instance unavailable" in caplog.text


def test_project_list_reconciles_existing_docs_registry_project_rows(project_registry_env):
    from core.project_registry import list_projects
    from datastore.docsdb.registry import DocsRegistry
    from lib.database import get_connection

    visible_home = project_registry_env["visible_home"]
    source_root = visible_home / "src" / "recipe-app"
    source_root.mkdir(parents=True)
    project_dir = visible_home / "projects" / "recipe-app"
    project_dir.mkdir(parents=True)
    project_md = project_dir / "PROJECT.md"
    project_md.write_text("# Recipe App\n", encoding="utf-8")

    registry = DocsRegistry()
    with get_connection(registry.db_path) as conn:
        conn.execute(
            """
            INSERT INTO project_definitions
                (name, label, home_dir, source_roots, auto_index, patterns, exclude, description, state)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'active')
            """,
            (
                "recipe-app",
                "Recipe App",
                "projects/recipe-app/",
                json.dumps([str(source_root)]),
                json.dumps(["*.md"]),
                json.dumps([]),
                "Recipe benchmark app",
            ),
        )
        conn.execute(
            """
            INSERT INTO doc_registry (file_path, project, asset_type, title, state, registered_by)
            VALUES (?, ?, 'doc', ?, 'active', 'legacy-fixture')
            """,
            ("projects/recipe-app/PROJECT.md", "recipe-app", "Project: Recipe App"),
        )

    projects = list_projects()

    assert "recipe-app" in projects
    assert projects["recipe-app"]["canonical_path"] == str(project_dir)
    assert projects["recipe-app"]["source_root"] == str(source_root)
    assert projects["recipe-app"]["instances"] == []


def test_docs_reconcile_does_not_cross_link_current_instance(project_registry_env, monkeypatch):
    from datastore.docsdb.registry import DocsRegistry
    from lib.database import get_connection
    from lib.project_registry import lookup, register

    visible_home = project_registry_env["visible_home"]
    project_dir = visible_home / "projects" / "quaid-live-src"
    source_root = visible_home / "src" / "quaid-live-src"
    project_dir.mkdir(parents=True)
    source_root.mkdir(parents=True)

    monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
    register(
        name="quaid-live-src",
        canonical_path=str(project_dir),
        source_root=str(source_root),
        link_current_instance=True,
    )

    registry = DocsRegistry()
    with get_connection(registry.db_path) as conn:
        conn.execute(
            """
            INSERT INTO project_definitions
                (name, label, home_dir, source_roots, auto_index, patterns, exclude, description, state)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'active')
            """,
            (
                "quaid-live-src",
                "Quaid Live Src",
                "projects/quaid-live-src/",
                json.dumps([str(source_root)]),
                json.dumps(["*.md"]),
                json.dumps([]),
                "CDX-created test project",
            ),
        )

    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest")
    registry.reconcile_global_project_registry()

    entry = lookup("quaid-live-src")
    assert entry is not None
    assert entry["instances"] == ["codex-private-tmp-cdx-livetest"]


def test_project_registry_sync_does_not_cross_link_ambient_instance(project_registry_env, monkeypatch):
    from core.project_registry import create_project, get_project

    project_name = "misc--codex-private-tmp-cdx-m13-test"
    creating_instance = "codex-private-tmp-cdx-m13-test"

    # Auto-provision can create a derived instance while an older adapter
    # process is still the ambient QUAID_INSTANCE. The docs mirror must not
    # treat that ambient instance as an owner of the new misc project.
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest")
    create_project(
        project_name,
        description="Scratch pad for ephemeral and temporary files.",
        initial_instance=creating_instance,
    )

    entry = get_project(project_name)
    assert entry is not None
    assert entry["instances"] == [creating_instance]


def test_project_registry_sync_reuses_relative_project_md_registry_path(project_registry_env):
    from core.docs.updater import sync_project_visible_docs
    from core.project_registry import create_project
    from datastore.docsdb.registry import DocsRegistry

    entry = create_project("recipe-app", description="Recipe benchmark app")

    sync_project_visible_docs(
        "recipe-app",
        entry["canonical_path"],
        root_docs={"PROJECT.md", "TOOLS.md", "AGENTS.md"},
        protected_names={"PROJECT.log"},
    )

    registry = DocsRegistry()
    docs = registry.list_docs(project="recipe-app")
    project_md_docs = [row for row in docs if str(row.get("file_path")) == "projects/recipe-app/PROJECT.md"]

    assert len(project_md_docs) == 1
    assert [
        row
        for row in docs
        if str(row.get("file_path", "")).endswith("/projects/recipe-app/PROJECT.md")
    ] == []


def test_docs_registry_scopes_lists_and_reads_to_current_instance(project_registry_env, monkeypatch):
    from datastore.docsdb.registry import DocsRegistry
    from lib.database import get_connection
    from lib.project_registry import register

    visible_home = project_registry_env["visible_home"]
    cdx_project_dir = visible_home / "projects" / "quaid-live-src"
    cc_project_dir = visible_home / "projects" / "quaid-cli-tool"
    cdx_source_root = visible_home / "src" / "quaid-live-src"
    cc_source_root = visible_home / "src" / "quaid-cli-tool"
    cdx_project_dir.mkdir(parents=True)
    cc_project_dir.mkdir(parents=True)
    cdx_source_root.mkdir(parents=True)
    cc_source_root.mkdir(parents=True)

    cdx_doc = cdx_project_dir / "PROJECT.md"
    cc_doc = cc_project_dir / "PROJECT.md"
    shared_source = visible_home / "src" / "shared.py"
    cdx_doc.write_text("# Quaid Live Src\n", encoding="utf-8")
    cc_doc.write_text("# Quaid CLI Tool\n", encoding="utf-8")
    shared_source.parent.mkdir(parents=True, exist_ok=True)
    shared_source.write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
    register(
        name="quaid-live-src",
        canonical_path=str(cdx_project_dir),
        source_root=str(cdx_source_root),
        link_current_instance=True,
    )
    monkeypatch.setenv("QUAID_INSTANCE", "claude-code-private-tmp-cc-livetest")
    register(
        name="quaid-cli-tool",
        canonical_path=str(cc_project_dir),
        source_root=str(cc_source_root),
        link_current_instance=True,
    )

    monkeypatch.setenv("QUAID_INSTANCE", "codex-private-tmp-cdx-livetest")
    registry = DocsRegistry(seed_projects=False)
    with get_connection(registry.db_path) as conn:
        for name, label, project_dir, source_root, doc_path in (
            ("quaid-cli-tool", "Quaid CLI Tool", cc_project_dir, cc_source_root, cc_doc),
            ("quaid-live-src", "Quaid Live Src", cdx_project_dir, cdx_source_root, cdx_doc),
        ):
            conn.execute(
                """
                INSERT INTO project_definitions
                    (name, label, home_dir, source_roots, auto_index, patterns, exclude, description, state)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, 'active')
                """,
                (
                    name,
                    label,
                    f"projects/{name}/",
                    json.dumps([str(source_root)]),
                    json.dumps(["*.md"]),
                    json.dumps([]),
                    f"{label} test project",
                ),
            )
            conn.execute(
                """
                INSERT INTO doc_registry
                    (file_path, project, asset_type, title, state, registered_by, source_files, auto_update)
                VALUES (?, ?, 'doc', ?, 'active', 'test', ?, 1)
                """,
                (str(doc_path), name, "Project Guide", json.dumps([str(shared_source)])),
            )
        conn.execute(
            """
            INSERT INTO doc_registry (file_path, project, asset_type, title, state, registered_by)
            VALUES (?, ?, 'doc', ?, 'active', 'test')
            """,
            (str(cc_project_dir / "MISSING.md"), "quaid-cli-tool", "Hidden Missing"),
        )

    assert [doc["project"] for doc in registry.list_docs()] == ["quaid-live-src"]
    visible_project_names = {project["name"] for project in registry.list_projects()}
    assert "quaid-live-src" in visible_project_names
    assert "quaid-cli-tool" not in visible_project_names
    assert registry.get(str(cc_doc)) is None
    assert registry.find_project_by_source_file(str(shared_source)) == "quaid-live-src"

    # A hidden exact-title match must not shadow the visible project document.
    read_entry = registry.read("Project Guide")
    assert read_entry is not None
    assert read_entry["project"] == "quaid-live-src"
    assert read_entry["file_path"] == str(cdx_doc)

    gc_result = registry.gc(dry_run=False)
    assert gc_result["removed"] == []
    with get_connection(registry.db_path) as conn:
        hidden_row = conn.execute(
            "SELECT state FROM doc_registry WHERE project = ? AND file_path = ?",
            ("quaid-cli-tool", str(cc_project_dir / "MISSING.md")),
        ).fetchone()
    assert hidden_row is not None
    assert hidden_row["state"] == "active"


def test_project_show_reconciles_doc_registry_only_external_project(project_registry_env):
    from core.project_registry import get_project
    from datastore.docsdb.registry import DocsRegistry
    from lib.database import get_connection

    visible_home = project_registry_env["visible_home"]
    external_root = visible_home / "external-source" / "recipe-app"
    external_doc = external_root / "README.md"
    external_root.mkdir(parents=True)
    external_doc.write_text("# Recipe App\n", encoding="utf-8")
    project_dir = visible_home / "projects" / "recipe-app"

    registry = DocsRegistry()
    with get_connection(registry.db_path) as conn:
        conn.execute(
            """
            INSERT INTO doc_registry (file_path, project, asset_type, title, description, state, registered_by)
            VALUES (?, ?, 'doc', ?, ?, 'active', 'legacy-fixture')
            """,
            (
                str(external_doc),
                "recipe-app",
                "Recipe App README",
                "Orphan test row",
            ),
        )

    entry = get_project("recipe-app")

    assert entry is not None
    assert entry["canonical_path"] == str(project_dir)
    assert entry["source_root"] == str(external_doc.parent)
    assert entry["instances"] == []
    defn = registry.get_project_definition("recipe-app")
    assert defn is not None
    assert defn.home_dir == "projects/recipe-app/"
    assert defn.source_roots == [str(external_doc.parent)]
    assert (project_dir / "PROJECT.md").is_file()


def test_deleted_project_marker_blocks_docs_reconciliation(project_registry_env):
    from core.project_registry import get_project
    from datastore.docsdb.registry import DocsRegistry
    from lib.database import get_connection
    from lib.project_registry import mark_deleted

    visible_home = project_registry_env["visible_home"]
    external_root = visible_home / "external-source" / "recipe-app"
    external_doc = external_root / "README.md"
    external_root.mkdir(parents=True)
    external_doc.write_text("# Recipe App\n", encoding="utf-8")
    project_dir = visible_home / "projects" / "recipe-app"

    registry = DocsRegistry()
    with get_connection(registry.db_path) as conn:
        conn.execute(
            """
            INSERT INTO doc_registry (file_path, project, asset_type, title, description, state, registered_by)
            VALUES (?, ?, 'doc', ?, ?, 'active', 'legacy-fixture')
            """,
            (
                str(external_doc),
                "recipe-app",
                "Recipe App README",
                "Orphan test row",
            ),
        )

    mark_deleted("recipe-app")
    registry.reconcile_global_project_registry()

    assert get_project("recipe-app") is None
    assert registry.get_project_definition("recipe-app") is None
    assert not project_dir.exists()
