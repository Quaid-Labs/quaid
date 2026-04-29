import os

from lib.adapter import TestAdapter, reset_adapter, set_adapter

from core.plugins.docsdb_contract import DocsDbPluginContract
from core.runtime.plugins import PluginHookContext, PluginManifest
from datastore.docsdb.system_context import build_system_context_metadata


def _ctx(workspace_root: str) -> PluginHookContext:
    manifest = PluginManifest(
        plugin_api_version=1,
        plugin_id="docsdb.core",
        plugin_type="datastore",
        module="core.plugins.docsdb_contract",
        display_name="DocsDB",
    )
    return PluginHookContext(
        plugin=manifest,
        config=object(),
        plugin_config={},
        workspace_root=workspace_root,
    )


def test_docsdb_contract_on_init_ensures_project_workspace_dirs(tmp_path, monkeypatch):
    visible_root = tmp_path / "visible"
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_root))
    contract = DocsDbPluginContract()
    contract.on_init(_ctx(str(tmp_path)))

    assert (visible_root / "projects").is_dir()
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / "temp").exists()
    assert not (tmp_path / "scratch").exists()


def test_docsdb_contract_on_config_ensures_project_workspace_dirs(tmp_path, monkeypatch):
    visible_root = tmp_path / "visible"
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_root))
    contract = DocsDbPluginContract()
    contract.on_config(_ctx(str(tmp_path)))

    assert (visible_root / "projects").is_dir()
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / "temp").exists()
    assert not (tmp_path / "scratch").exists()


def test_docsdb_contract_get_system_context_metadata(monkeypatch, tmp_path):
    contract = DocsDbPluginContract()
    monkeypatch.setattr(
        "core.plugins.docsdb_contract.build_docsdb_system_context_metadata",
        lambda: {"entries": [{"key": "ok", "label": "ok", "value": "delegated"}]},
    )

    payload = contract.get_system_context_metadata(_ctx(str(tmp_path)))

    assert payload == {"entries": [{"key": "ok", "label": "ok", "value": "delegated"}]}


def test_project_docs_monitor_uses_supervisor_parent_without_reensure(monkeypatch):
    from core.plugins import docsdb_contract

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_SUPERVISOR_PID", str(os.getpid()))
    monkeypatch.setattr("core.project_registry.list_projects", lambda: {"demo": {}})
    monkeypatch.setattr(
        "core.project_docs.request_update",
        lambda name, *, reason, requested_by: {"project": name, "request_id": "req-1"},
    )

    def _unexpected_ensure():
        raise AssertionError("supervisor-owned janitor worker must not re-bootstrap supervisor")

    monkeypatch.setattr("core.project_docs.ensure_supervisor_alive", _unexpected_ensure)

    result = docsdb_contract._queue_project_docs_monitor_requests(reason="janitor", requested_by="pytest")

    assert result["requested"] == 1
    assert result["supervisor_pid"] == os.getpid()
    assert result["errors"] == []


def test_project_docs_monitor_materializes_queued_projects(tmp_path, monkeypatch):
    from core.plugins import docsdb_contract
    from core.project_registry import project_exists_raw
    from datastore.docsdb import project_log_queue

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_SUPERVISOR_PID", str(os.getpid()))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        project_log_queue.enqueue_project_logs(
            {"recipe-app": ["Queued project docs entry from transcript"]},
            trigger="Compaction",
        )
        monkeypatch.setattr("core.project_registry._sync_docs_registry_project", lambda *args, **kwargs: None)
        requests: list[str] = []
        monkeypatch.setattr(
            "core.project_docs.request_update",
            lambda name, *, reason, requested_by: requests.append(name) or {"project": name, "request_id": "req-1"},
        )

        result = docsdb_contract._queue_project_docs_monitor_requests(reason="janitor", requested_by="pytest")

        assert result["requested"] == 1
        assert requests == ["recipe-app"]
        assert project_exists_raw("recipe-app") is True
    finally:
        reset_adapter()


def test_build_docsdb_system_context_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr("datastore.docsdb.system_context.current_instance_id", lambda: "openclaw-main")
    monkeypatch.setattr(
        "datastore.docsdb.system_context.list_projects",
        lambda: {
            "quaid": {
                "canonical_path": str(tmp_path / "shared" / "projects" / "quaid"),
                "instances": ["openclaw-main", "claude-code-main"],
            },
            "other": {
                "canonical_path": str(tmp_path / "shared" / "projects" / "other"),
                "instances": ["claude-code-main"],
            },
            "misc--openclaw-main": {
                "canonical_path": str(tmp_path / "shared" / "projects" / "misc--openclaw-main"),
                "instances": ["openclaw-main"],
            },
        },
    )

    payload = build_system_context_metadata()

    assert payload == {
        "entries": [
            {
                "key": "linked_projects",
                "label": "linked projects",
                "value": (
                    f"quaid ({tmp_path / 'shared' / 'projects' / 'quaid'}); "
                    f"misc--openclaw-main ({tmp_path / 'shared' / 'projects' / 'misc--openclaw-main'})"
                ),
                "note": (
                    "Preinject does not cover project or docs detail. "
                    "MANDATORY ORDER: For project document questions, run docs recall before filesystem grep/cat "
                    "(for example: quaid recall \"<query>\" '{\"stores\":[\"docs\"],\"project\":\"<project-name>\"}'). "
                    "Use filesystem reads only when docs recall returns no relevant hits, weak hits, or only index/catalog rows; "
                    "for read-only one-fact lookups, read the catalog-listed file directly without linking."
                ),
                "order": 30,
            }
        ]
    }
