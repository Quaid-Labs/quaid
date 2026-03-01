import types

import pytest

from adaptors.openclaw.plugin_contract import OpenClawAdapterPluginContract
from core.runtime.plugins import PluginHookContext, PluginManifest


def _ctx() -> PluginHookContext:
    return PluginHookContext(
        plugin=PluginManifest(
            plugin_api_version=1,
            plugin_id="openclaw.adapter",
            plugin_type="adapter",
            module="adaptors.openclaw.plugin_contract",
            display_name="OpenClaw Adapter",
        ),
        config=types.SimpleNamespace(
            adapter=types.SimpleNamespace(type="openclaw"),
        ),
        plugin_config={},
        workspace_root=".",
    )


def test_openclaw_contract_on_init_bootstraps_memory_service(monkeypatch):
    called = {"memory_service": 0}

    def _fail_if_called():
        called["memory_service"] += 1
        raise AssertionError("on_init must not touch memory service")

    monkeypatch.setattr("core.services.memory_service.get_memory_service", _fail_if_called)
    contract = OpenClawAdapterPluginContract()
    contract.on_init(_ctx())
    status = contract.on_status(_ctx())
    assert called["memory_service"] == 0
    assert status["ready"] is True
    assert "init_error" not in status


def test_openclaw_contract_on_init_remains_lightweight_without_datastore(monkeypatch):
    # Datastore path may not be import-safe during plugin bootstrap.
    monkeypatch.setattr(
        "core.services.memory_service.get_memory_service",
        lambda: (_ for _ in ()).throw(RuntimeError("should not be touched")),
    )
    contract = OpenClawAdapterPluginContract()
    contract.on_init(_ctx())
    status = contract.on_status(_ctx())
    assert status["ready"] is True
    assert "init_error" not in status
