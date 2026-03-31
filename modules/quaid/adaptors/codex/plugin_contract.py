"""Codex adapter plugin contract hooks."""

from __future__ import annotations

from typing import Optional

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext

_INIT_READY = False
_INIT_ERROR: Optional[str] = None


class CodexAdapterPluginContract(PluginContractBase):
    def on_init(self, ctx: PluginHookContext) -> None:
        global _INIT_READY, _INIT_ERROR
        _ = ctx
        _INIT_READY = True
        _INIT_ERROR = None

    def on_config(self, ctx: PluginHookContext) -> None:
        adapter_type = str(getattr(getattr(ctx.config, "adapter", None), "type", "") or "").strip().lower()
        if adapter_type and adapter_type not in ("codex",):
            raise ValueError(
                f"codex adapter plugin active but adapter.type={adapter_type!r} (expected 'codex')"
            )

    def on_status(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        out = {"adapter": "codex", "ready": bool(_INIT_READY)}
        if _INIT_ERROR:
            out["init_error"] = _INIT_ERROR
        return out

    def on_dashboard(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"panel": "adapter-codex", "enabled": False}

    def on_maintenance(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"handled": False}

    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"ready": True}

    def on_health(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"healthy": True}


_CONTRACT = CodexAdapterPluginContract()


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
