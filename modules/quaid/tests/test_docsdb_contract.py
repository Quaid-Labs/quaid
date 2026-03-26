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


def test_docsdb_contract_on_init_ensures_project_workspace_dirs(tmp_path):
    contract = DocsDbPluginContract()
    contract.on_init(_ctx(str(tmp_path)))

    assert (tmp_path / "projects").is_dir()
    assert (tmp_path / "temp").is_dir()
    assert (tmp_path / "scratch").is_dir()


def test_docsdb_contract_on_config_ensures_project_workspace_dirs(tmp_path):
    contract = DocsDbPluginContract()
    contract.on_config(_ctx(str(tmp_path)))

    assert (tmp_path / "projects").is_dir()
    assert (tmp_path / "temp").is_dir()
    assert (tmp_path / "scratch").is_dir()


def test_docsdb_contract_get_system_context_metadata(monkeypatch, tmp_path):
    contract = DocsDbPluginContract()
    monkeypatch.setattr(
        "core.plugins.docsdb_contract.build_docsdb_system_context_metadata",
        lambda: {"entries": [{"key": "ok", "label": "ok", "value": "delegated"}]},
    )

    payload = contract.get_system_context_metadata(_ctx(str(tmp_path)))

    assert payload == {"entries": [{"key": "ok", "label": "ok", "value": "delegated"}]}


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
                    "If a query depends on these projects, files, paths, tests, bugs, or architecture docs, "
                    "use project recall explicitly."
                ),
                "order": 30,
            }
        ]
    }
