from types import SimpleNamespace

from core.plugins.memorydb_contract import MemoryDbPluginContract
from core.runtime.plugins import PluginHookContext, PluginManifest
from datastore.memorydb.system_context import build_system_context_metadata


def _ctx() -> PluginHookContext:
    manifest = PluginManifest(
        plugin_api_version=1,
        plugin_id="memorydb.core",
        plugin_type="datastore",
        module="core.plugins.memorydb_contract",
        display_name="MemoryDB",
    )
    return PluginHookContext(
        plugin=manifest,
        config=SimpleNamespace(),
        plugin_config={},
        workspace_root="/tmp/quaid-workspace",
    )


def test_memorydb_contract_get_system_context_metadata(monkeypatch):
    contract = MemoryDbPluginContract()
    seen: dict[str, str] = {}

    def _fake_builder(*, db_path=None):
        seen["db_path"] = str(db_path)
        return {"entries": [{"key": "ok", "label": "ok", "value": "delegated"}]}

    monkeypatch.setattr(
        "core.plugins.memorydb_contract.build_memorydb_system_context_metadata",
        _fake_builder,
    )

    payload = contract.get_system_context_metadata(_ctx())

    assert payload == {"entries": [{"key": "ok", "label": "ok", "value": "delegated"}]}
    assert seen["db_path"].endswith("/data/memory.db")


def test_build_memorydb_system_context_metadata(monkeypatch):
    monkeypatch.setattr(
        "datastore.memorydb.system_context.active_domains",
        lambda *, db_path=None: ["personal", "technical"],
    )
    monkeypatch.setattr(
        "datastore.memorydb.system_context.list_relation_types",
        lambda: ["neighbor_of", "parent_of"],
    )

    payload = build_system_context_metadata()

    assert payload == {
        "entries": [
            {
                "key": "domains",
                "label": "active domains",
                "value": "personal, technical",
                "order": 10,
            },
            {
                "key": "graph_relation_types",
                "label": "active graph relation types",
                "value": "neighbor_of, parent_of",
                "note": (
                    "Preinject does not cover graph structure or edge traversal. "
                    "If a query depends on these relations, use graph recall explicitly."
                ),
                "order": 20,
            },
        ]
    }
