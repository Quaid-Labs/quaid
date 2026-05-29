from types import SimpleNamespace

from core.contracts.plugin_contract import PluginContractBase
from core.plugins import sessiondb_contract
from core.plugins.sessiondb_contract import SessionDbPluginContract
from core.runtime.plugins import PluginHookContext, PluginManifest


def _ctx() -> PluginHookContext:
    manifest = PluginManifest(
        plugin_api_version=1,
        plugin_id="sessiondb.core",
        plugin_type="datastore",
        module="core.plugins.sessiondb_contract",
        display_name="SessionDB",
    )
    return PluginHookContext(
        plugin=manifest,
        config=SimpleNamespace(),
        plugin_config={},
        workspace_root="/tmp/quaid-workspace",
    )


def test_sessiondb_contract_implements_plugin_surface() -> None:
    contract = SessionDbPluginContract()
    ctx = _ctx()

    assert isinstance(sessiondb_contract._CONTRACT, PluginContractBase)
    assert contract.on_status(ctx) == {"datastore": "sessiondb", "ready": True}
    assert contract.on_dashboard(ctx) == {"panel": "sessiondb", "enabled": False}
    assert contract.on_maintenance(ctx) == {"handled": False}
    assert contract.on_tool_runtime(ctx) == {"ready": True}
    assert contract.on_health(ctx) == {
        "healthy": True,
        "status": {"datastore": "sessiondb", "ready": True},
    }


def test_sessiondb_contract_module_wrappers_delegate() -> None:
    ctx = _ctx()

    assert sessiondb_contract.on_status(ctx) == {"datastore": "sessiondb", "ready": True}
    assert sessiondb_contract.on_maintenance(ctx) == {"handled": False}
    assert sessiondb_contract.on_tool_runtime(ctx) == {"ready": True}
