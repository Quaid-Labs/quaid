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
    assert unlink("mixed-cleanup", instance="BENCHRUNNER") is False
    assert unlink("MIXED-CLEANUP", instance="benchrunner") is True
    assert remove("MIXED-CLEANUP", force=True) is True
    assert lookup("mixed-cleanup") is None


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
