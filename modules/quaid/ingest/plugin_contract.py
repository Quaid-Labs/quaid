"""Ingest plugin contract hooks."""

from __future__ import annotations

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext


class IngestPluginContract(PluginContractBase):
    def on_init(self, ctx: PluginHookContext) -> None:
        # Ingest module boot is lightweight; runtime workers load lazily.
        _ = ctx

    def on_config(self, ctx: PluginHookContext) -> None:
        # Reserved for plugin-specific config validation.
        _ = ctx

    def on_status(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"ingest": "core.extract", "ready": True}

    def on_dashboard(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        # TODO: dashboard integration surface.
        return {"panel": "ingest-core", "enabled": False}

    def on_maintenance(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"handled": False}

    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"ready": True}

    def on_health(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"healthy": True}


_CONTRACT = IngestPluginContract()


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
