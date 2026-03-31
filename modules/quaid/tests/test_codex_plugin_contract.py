import types

import pytest

from adaptors.codex.plugin_contract import CodexAdapterPluginContract
from core.runtime.plugins import PluginHookContext, PluginManifest


def _ctx(adapter_type: str = "codex") -> PluginHookContext:
    return PluginHookContext(
        plugin=PluginManifest(
            plugin_api_version=1,
            plugin_id="codex.adapter",
            plugin_type="adapter",
            module="adaptors.codex.plugin_contract",
            display_name="Codex Adapter",
        ),
        config=types.SimpleNamespace(
            adapter=types.SimpleNamespace(type=adapter_type),
        ),
        plugin_config={},
        workspace_root=".",
    )


def test_codex_contract_on_init_sets_ready():
    contract = CodexAdapterPluginContract()
    contract.on_init(_ctx())
    status = contract.on_status(_ctx())
    assert status["ready"] is True
    assert "init_error" not in status


def test_codex_contract_rejects_wrong_adapter_type():
    contract = CodexAdapterPluginContract()
    with pytest.raises(ValueError, match="expected 'codex'"):
        contract.on_config(_ctx("openclaw"))
