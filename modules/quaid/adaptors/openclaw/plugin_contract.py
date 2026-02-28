"""OpenClaw adapter plugin contract hooks."""

from __future__ import annotations

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext


class OpenClawAdapterPluginContract(PluginContractBase):
    def on_init(self, ctx: PluginHookContext) -> None:
        # Adapter init is intentionally light-weight; adapter runtime is lazy-loaded.
        _ = ctx

    def on_config(self, ctx: PluginHookContext) -> None:
        # Keep this strict and explicit: openclaw adapter slot should pair with adapter.type=openclaw.
        adapter_type = str(getattr(getattr(ctx.config, "adapter", None), "type", "") or "").strip().lower()
        if adapter_type and adapter_type != "openclaw":
            raise ValueError(
                f"openclaw adapter plugin active but adapter.type={adapter_type!r} (expected 'openclaw')"
            )

    def on_status(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"adapter": "openclaw", "ready": True}

    def on_dashboard(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        # TODO: dashboard integration surface.
        return {"panel": "adapter-openclaw", "enabled": False}

    def on_maintenance(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"handled": False}

    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"ready": True}

    def on_health(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"healthy": True}


_CONTRACT = OpenClawAdapterPluginContract()


def on_init(ctx: PluginHookContext) -> None:
    _CONTRACT.on_init(ctx)


def on_config(ctx: PluginHookContext) -> None:
    _CONTRACT.on_config(ctx)


def on_status(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_status(ctx)


def on_dashboard(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_dashboard(ctx)


def on_maintenance(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_maintenance(ctx)


def on_tool_runtime(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_tool_runtime(ctx)


def on_health(ctx: PluginHookContext) -> dict:
    return _CONTRACT.on_health(ctx)
