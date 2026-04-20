"""Project/docs registry consistency invariants."""

from __future__ import annotations

import json

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
