from core.plugins.docsdb_contract import DocsDbPluginContract
from core.runtime.plugins import PluginHookContext, PluginManifest


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
