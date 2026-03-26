from types import SimpleNamespace

from core.plugins.memorydb_contract import MemoryDbPluginContract
from core.runtime.plugins import PluginHookContext, PluginManifest


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
    monkeypatch.setattr(
        "core.plugins.memorydb_contract._system_context_domains",
        lambda _ctx: ["personal", "technical"],
    )
    monkeypatch.setattr(
        "core.plugins.memorydb_contract.list_relation_types",
        lambda: ["neighbor_of", "parent_of"],
    )

    payload = contract.get_system_context_metadata(_ctx())

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
