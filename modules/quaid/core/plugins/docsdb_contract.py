"""DocsDB datastore plugin contract hooks."""

from __future__ import annotations

from pathlib import Path

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext


def _ensure_project_workspace_dirs(ctx: PluginHookContext) -> None:
    # projects/ lives at workspace root (shared across instances via registry)
    root = Path(ctx.workspace_root)
    (root / "projects").mkdir(parents=True, exist_ok=True)
    # scratch/ and temp/ are per-instance: QUAID_HOME/{instance_id}/scratch|temp/
    # Use lib.instance helpers so the path is authoritative across the codebase.
    try:
        from lib.instance import instance_scratch_dir, instance_temp_dir
        instance_scratch_dir().mkdir(parents=True, exist_ok=True)
        instance_temp_dir().mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback for standalone/misconfigured instances: workspace-level dirs.
        for rel in ("scratch", "temp"):
            (root / rel).mkdir(parents=True, exist_ok=True)


class DocsDbPluginContract(PluginContractBase):
    def on_init(self, ctx: PluginHookContext) -> None:
        _ensure_project_workspace_dirs(ctx)

    def on_config(self, ctx: PluginHookContext) -> None:
        _ensure_project_workspace_dirs(ctx)

    def on_status(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"datastore": "docsdb", "ready": True}

    def on_dashboard(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"panel": "docsdb", "enabled": False}

    def on_maintenance(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"handled": False}

    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"ready": True}

    def on_health(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"healthy": True}


_CONTRACT = DocsDbPluginContract()


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
