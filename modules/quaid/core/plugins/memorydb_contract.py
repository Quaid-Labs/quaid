"""MemoryDB plugin contract hooks.

Domain lifecycle is datastore-owned: schema/table sync and TOOLS domain block sync
are implemented here and invoked by core plugin contract execution.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Dict

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext
from datastore.memorydb.domain_registry import (
    apply_domain_set,
    load_active_domains,
    normalize_domain_map,
)
from lib.config import get_db_path
from lib.tools_domain_sync import sync_tools_domain_block

_PUBLISH_LOCK = threading.RLock()

def _resolve_db_path(ctx: PluginHookContext) -> Path:
    _ = ctx
    return get_db_path()


def _resolve_domains(ctx: PluginHookContext) -> Dict[str, str]:
    plugin_domains = ctx.plugin_config.get("domains")
    if isinstance(plugin_domains, dict) and plugin_domains:
        out = normalize_domain_map(plugin_domains)
        if out:
            return out
    return {}


def _publish_domains_to_runtime_config(ctx: PluginHookContext, domains: Dict[str, str]) -> None:
    retrieval = getattr(ctx.config, "retrieval", None)
    if retrieval is None:
        return
    with _PUBLISH_LOCK:
        setattr(retrieval, "domains", dict(domains))


def _sync_domains(ctx: PluginHookContext) -> None:
    explicit_domains = _resolve_domains(ctx)
    db_path = _resolve_db_path(ctx)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if explicit_domains:
        from lib.database import get_connection

        with get_connection(db_path) as conn:
            domains = apply_domain_set(conn, explicit_domains, deactivate_others=True)
    else:
        domains = load_active_domains(db_path, bootstrap_if_empty=True)
    if not domains:
        raise RuntimeError("memorydb domain registry is empty and could not be initialized")
    _publish_domains_to_runtime_config(ctx, domains)
    sync_tools_domain_block(domains=domains, workspace=Path(ctx.workspace_root))


class MemoryDbPluginContract(PluginContractBase):
    def on_init(self, ctx: PluginHookContext) -> None:
        _sync_domains(ctx)

    def on_config(self, ctx: PluginHookContext) -> None:
        _sync_domains(ctx)

    def on_status(self, ctx: PluginHookContext) -> dict:
        db_path = _resolve_db_path(ctx)
        try:
            domains = load_active_domains(db_path, bootstrap_if_empty=False)
            return {"active_domains": len(domains)}
        except sqlite3.OperationalError:
            return {"active_domains": 0}

    def on_dashboard(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        # TODO: future dashboard integration surface.
        return {"panel": "memorydb-domains", "enabled": False}

    def on_maintenance(self, ctx: PluginHookContext) -> dict:
        from core.lifecycle.datastore_runtime import get_graph
        from core.lifecycle.janitor_lifecycle import RoutineContext, RoutineResult
        from datastore.memorydb.maintenance import run_memory_graph_maintenance

        payload = dict(ctx.payload or {})
        stage = str(payload.get("subtask", payload.get("stage", "review")) or "review").strip().lower()
        max_items = int(payload.get("max_items", 0) or 0)
        llm_timeout_raw = float(payload.get("llm_timeout_seconds", 0) or 0)
        llm_timeout_seconds = llm_timeout_raw if llm_timeout_raw > 0 else 0.0
        dry_run = bool(payload.get("dry_run", False))
        graph = get_graph()
        routine_ctx = RoutineContext(
            cfg=ctx.config,
            dry_run=dry_run,
            workspace=Path(ctx.workspace_root),
            graph=graph,
            options={
                "subtask": stage,
                "max_items": max_items,
                "llm_timeout_seconds": llm_timeout_seconds,
            },
        )
        result = run_memory_graph_maintenance(routine_ctx, RoutineResult)
        return {
            "handled": True,
            "metrics": dict(result.metrics or {}),
            "errors": list(result.errors or []),
            "logs": list(result.logs or []),
            "data": dict(result.data or {}),
        }

    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:
        _ = ctx
        return {"ready": True}

    def on_health(self, ctx: PluginHookContext) -> dict:
        return {"healthy": True, "status": self.on_status(ctx)}


_CONTRACT = MemoryDbPluginContract()


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
