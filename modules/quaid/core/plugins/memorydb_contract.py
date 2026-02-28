"""MemoryDB plugin contract hooks.

Domain lifecycle is datastore-owned: schema/table sync and TOOLS domain block sync
are implemented here and invoked by core plugin contract execution.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Dict

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext
from lib.database import get_connection
from lib.tools_domain_sync import sync_tools_domain_block

_DEFAULT_DOMAIN_DESCRIPTIONS = {
    "personal": "identity, preferences, relationships, life events",
    "technical": "code, infra, APIs, architecture",
    "project": "project status, tasks, files, milestones",
    "work": "job/team/process decisions not deeply technical",
    "health": "training, injuries, routines, wellness",
    "finance": "budgeting, purchases, salary, bills",
    "travel": "trips, moves, places, logistics",
    "schedule": "dates, appointments, deadlines",
    "research": "options considered, comparisons, tradeoff analysis",
    "household": "home, chores, food planning, shared logistics",
    "legal": "contracts, policy, and regulatory constraints",
}


def _resolve_db_path(ctx: PluginHookContext) -> Path:
    env_db_path = str(os.environ.get("MEMORY_DB_PATH", "")).strip()
    if env_db_path:
        return Path(env_db_path)
    db_path = str(getattr(getattr(ctx.config, "database", None), "path", "data/memory.db") or "data/memory.db")
    p = Path(db_path)
    if p.is_absolute():
        return p
    return Path(ctx.workspace_root) / p


def _resolve_domains(ctx: PluginHookContext) -> Dict[str, str]:
    plugin_domains = ctx.plugin_config.get("domains")
    if isinstance(plugin_domains, dict) and plugin_domains:
        out = {str(k).strip(): str(v or "").strip() for k, v in plugin_domains.items() if str(k).strip()}
        if out:
            return out
    retrieval_domains = getattr(getattr(ctx.config, "retrieval", None), "domains", {}) or {}
    if isinstance(retrieval_domains, dict) and retrieval_domains:
        return {str(k).strip(): str(v or "").strip() for k, v in retrieval_domains.items() if str(k).strip()}
    return dict(_DEFAULT_DOMAIN_DESCRIPTIONS)


def _sync_domains(ctx: PluginHookContext) -> None:
    domains = _resolve_domains(ctx)
    db_path = _resolve_db_path(ctx)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS domain_registry (
                domain TEXT PRIMARY KEY,
                description TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS node_domains (
                node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                domain TEXT NOT NULL REFERENCES domain_registry(domain) ON DELETE RESTRICT,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (node_id, domain)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_node_domains_domain_node ON node_domains(domain, node_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_node_domains_node_domain ON node_domains(node_id, domain)")
        for domain_id, description in domains.items():
            conn.execute(
                """
                INSERT INTO domain_registry(domain, description, active)
                VALUES (?, ?, 1)
                ON CONFLICT(domain) DO UPDATE SET
                  description = excluded.description,
                  active = 1,
                  updated_at = datetime('now')
                """,
                (domain_id, description),
            )
        if domains:
            placeholders = ",".join("?" for _ in domains)
            conn.execute(
                f"UPDATE domain_registry SET active = 0, updated_at = datetime('now') WHERE domain NOT IN ({placeholders})",
                tuple(domains.keys()),
            )
    sync_tools_domain_block(domains=domains, workspace=Path(ctx.workspace_root))


class MemoryDbPluginContract(PluginContractBase):
    def on_init(self, ctx: PluginHookContext) -> None:
        _sync_domains(ctx)

    def on_config(self, ctx: PluginHookContext) -> None:
        _sync_domains(ctx)

    def on_status(self, ctx: PluginHookContext) -> dict:
        db_path = _resolve_db_path(ctx)
        try:
            with get_connection(db_path) as conn:
                row = conn.execute("SELECT COUNT(*) AS c FROM domain_registry WHERE active = 1").fetchone()
            return {"active_domains": int(row[0] if row else 0)}
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
