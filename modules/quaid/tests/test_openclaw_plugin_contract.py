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
    called = {"stats": 0}

    class _Svc:
        def stats(self):
            called["stats"] += 1
            return {"ok": True}

    monkeypatch.setattr(
        "core.services.memory_service.get_memory_service",
        lambda: _Svc(),
    )
    contract = OpenClawAdapterPluginContract()
    contract.on_init(_ctx())
    status = contract.on_status(_ctx())
    assert called["stats"] == 1
    assert status["ready"] is True
    assert "init_error" not in status


def test_openclaw_contract_on_init_surfaces_bootstrap_failures(monkeypatch):
    class _Svc:
        def stats(self):
            raise RuntimeError("bootstrap failed")

    monkeypatch.setattr(
        "core.services.memory_service.get_memory_service",
        lambda: _Svc(),
    )
    contract = OpenClawAdapterPluginContract()
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        contract.on_init(_ctx())
    status = contract.on_status(_ctx())
    assert status["ready"] is False
    assert "bootstrap failed" in str(status.get("init_error", ""))
