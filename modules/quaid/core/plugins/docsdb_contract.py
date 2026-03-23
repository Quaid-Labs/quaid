"""DocsDB datastore plugin contract hooks."""

from __future__ import annotations

from pathlib import Path

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext


def _ensure_project_workspace_dirs(ctx: PluginHookContext) -> None:
    root = Path(ctx.workspace_root)
    # canonical projects/, per-instance projects/staging/, temp/, scratch/ at workspace root — docsdb-owned
    (root / "projects").mkdir(parents=True, exist_ok=True)
    (root / "shared" / "projects").mkdir(parents=True, exist_ok=True)
    (root / "temp").mkdir(parents=True, exist_ok=True)
    (root / "scratch").mkdir(parents=True, exist_ok=True)
    # misc lives as a tracked project in projects/misc--{instance}/
    # Create the directory and auto-register it in the docsdb project registry
    # so agents can use it immediately without a manual create-project step.
    try:
        from lib.instance import instance_misc_dir
        misc_dir = instance_misc_dir()
        misc_dir.mkdir(parents=True, exist_ok=True)
        try:
            from datastore.docsdb.registry import ProjectRegistry
            from lib.config import get_db_path
            misc_name = misc_dir.name  # e.g. "misc--openclaw-main"
            try:
                rel_home = str(misc_dir.relative_to(root)) + "/"
            except ValueError:
                rel_home = str(misc_dir) + "/"
            reg = ProjectRegistry(get_db_path())
            desc = "Scratch pad for ephemeral and temporary files."
            try:
                reg.create_project(
                    name=misc_name,
                    home_dir=rel_home,
                    description=desc,
                )
            except ValueError:
                pass  # Already registered in SQLite — idempotent
            # Also register in global JSON registry so quaid global-registry
            # list and quaid project show can find it. Idempotent.
            try:
                from lib.project_registry import register as global_register
                global_register(
                    name=misc_name,
                    canonical_path=str(misc_dir),
                    description=desc,
                )
            except Exception:
                pass  # Global registry not available at this init stage
        except Exception:
            pass  # Registry not available at this init stage
    except Exception:
        pass  # Standalone/misconfigured — installer handles it


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
