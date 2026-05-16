"""DocsDB datastore plugin contract hooks."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext
from datastore.docsdb.system_context import (
    build_system_context_metadata as build_docsdb_system_context_metadata,
)

logger = logging.getLogger(__name__)


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled
    except Exception:
        return False
    return bool(is_fail_hard_enabled())


def _supervisor_disabled() -> bool:
    raw = str(os.environ.get("QUAID_SUPERVISOR_DISABLE", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _supervisor_parent_pid() -> int | None:
    raw = str(os.environ.get("QUAID_SUPERVISOR_PID", "") or "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
        os.kill(pid, 0)
        return pid
    except Exception as exc:
        if _fail_hard_enabled():
            raise RuntimeError(f"invalid supervisor parent pid: {raw!r}") from exc
        logger.warning("Ignoring invalid supervisor parent pid %r: %s", raw, exc)
        return None


def _queue_project_docs_monitor_requests(*, reason: str, requested_by: str) -> Dict[str, Any]:
    """Queue async project-docs monitor refresh requests for registered projects.

    DocsDB owns this maintenance callout. The core project-docs module owns the
    supervisor/request primitives, so this bridge only asks for monitor work and
    never performs heavy docs writes or RAG indexing inline in janitor.
    """
    if _supervisor_disabled():
        msg = "project-docs supervisor is disabled; cannot queue monitor maintenance"
        if _fail_hard_enabled():
            raise RuntimeError(msg)
        logger.warning(msg)
        return {
            "requested": 0,
            "projects": [],
            "errors": [msg],
            "supervisor_pid": None,
            "skipped": True,
        }

    from core import project_docs
    from core.project_registry import list_projects

    project_docs.materialize_queued_projects()
    projects = list_projects()
    names = sorted(str(name) for name in projects.keys() if str(name or "").strip())
    supervisor_parent = _supervisor_parent_pid()
    result: Dict[str, Any] = {
        "requested": 0,
        "projects": [],
        "errors": [],
        "supervisor_pid": None,
        "skipped": False,
    }
    if not names:
        return result

    for name in names:
        try:
            request = project_docs.request_update(name, reason=reason, requested_by=requested_by)
            result["requested"] = int(result["requested"]) + 1
            result["projects"].append({"name": name, "request_id": request.get("request_id")})
        except Exception as exc:
            msg = f"failed to queue project-docs monitor request for {name}: {exc}"
            logger.warning(msg)
            result["errors"].append(msg)
            if _fail_hard_enabled():
                raise RuntimeError(msg) from exc

    if supervisor_parent is not None:
        result["supervisor_pid"] = supervisor_parent
    else:
        try:
            result["supervisor_pid"] = project_docs.ensure_supervisor_alive()
        except Exception as exc:
            msg = f"failed to ensure project-docs supervisor for monitor maintenance: {exc}"
            logger.warning(msg)
            result["errors"].append(msg)
            if _fail_hard_enabled():
                raise RuntimeError(msg) from exc

    return result


def run_project_docs_monitor_maintenance(ctx: Any, result_factory: Any) -> Any:
    """DocsDB maintenance routine that asynchronously kicks project docs monitors."""
    result = result_factory()
    if bool(getattr(ctx, "dry_run", False)):
        result.logs.append("Project-docs monitor request skipped (dry-run)")
        return result

    options = dict(getattr(ctx, "options", {}) or {})
    reason = str(options.get("reason") or "janitor-sanity").strip() or "janitor-sanity"
    requested_by = str(options.get("requested_by") or "janitor").strip() or "janitor"
    try:
        queued = _queue_project_docs_monitor_requests(reason=reason, requested_by=requested_by)
    except RuntimeError:
        raise
    except Exception as exc:
        if _fail_hard_enabled():
            raise RuntimeError("Project-docs monitor maintenance failed") from exc
        result.errors.append(f"Project-docs monitor maintenance failed: {exc}")
        return result

    errors = list(queued.get("errors") or [])
    result.metrics["project_docs_update_requests"] = int(queued.get("requested") or 0)
    result.metrics["project_docs_update_request_errors"] = len(errors)
    result.data["supervisor_pid"] = queued.get("supervisor_pid")
    result.data["project_docs_monitor"] = queued
    if errors:
        result.errors.extend(errors)
    result.logs.append(
        "Queued project-docs monitor refresh requests: "
        f"{result.metrics['project_docs_update_requests']}"
    )
    if queued.get("supervisor_pid"):
        result.logs.append(f"Project-docs supervisor PID: {queued.get('supervisor_pid')}")
    return result


def handle_project_docs_maintenance_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Handle supervisor docs-maintenance event through DocsDB authority."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    source = str(payload.get("source") or "").strip()
    tick_kind = str(payload.get("tick_kind") or "").strip()
    observed_at = str(payload.get("observed_at") or "").strip()
    requested_operations = payload.get("requested_operations")
    if source != "project-docs-supervisor":
        return {"status": "failed", "error": "payload.source must be project-docs-supervisor"}
    if tick_kind != "auto_register_and_stale_index":
        return {"status": "failed", "error": "payload.tick_kind must be auto_register_and_stale_index"}
    if not observed_at:
        return {"status": "failed", "error": "payload.observed_at is required"}
    if not isinstance(requested_operations, dict):
        return {"status": "failed", "error": "payload.requested_operations must be an object"}

    auto_register_requested = bool(requested_operations.get("auto_register", False))
    stale_index_requested = bool(requested_operations.get("stale_index", False))
    direct_result: Dict[str, Any] = {
        "auto_register_ran": auto_register_requested,
        "stale_index_ran": stale_index_requested,
        "registered": None,
        "indexed_one": None,
        "errors": [],
    }
    from core import project_docs

    def _listener_result() -> Dict[str, Any]:
        return {
            "mode": "authoritative",
            "datastore_id": "docsdb",
            "project": payload.get("project"),
            "observed_at": observed_at,
            "direct_result": direct_result,
        }

    try:
        if auto_register_requested:
            direct_result["registered"] = int(project_docs.auto_register_project_docs() or 0)
    except Exception as exc:
        logger.warning("project docs auto-register listener failed: %s", exc)
        direct_result["errors"].append({"tick": "auto_register", "error": str(exc)})
        if _fail_hard_enabled():
            return {"status": "failed", "error": str(exc), "listener_result": _listener_result()}

    try:
        if stale_index_requested:
            direct_result["indexed_one"] = bool(project_docs.index_one_stale_registered_doc())
    except Exception as exc:
        logger.warning("project docs stale-index listener failed: %s", exc)
        direct_result["errors"].append({"tick": "stale_index", "error": str(exc)})
        if _fail_hard_enabled():
            return {"status": "failed", "error": str(exc), "listener_result": _listener_result()}

    listener_result = _listener_result()
    if direct_result["errors"]:
        error = "; ".join(str(item.get("error") or "") for item in direct_result["errors"])
        return {"status": "failed", "error": error, "listener_result": listener_result}
    return {"status": "processed", "listener_result": listener_result}


def _ensure_project_workspace_dirs(ctx: PluginHookContext) -> None:
    _ = Path(ctx.workspace_root)
    from lib.instance import visible_projects_dir
    visible_projects_dir().mkdir(parents=True, exist_ok=True)
    # misc lives as a tracked project in projects/misc--{instance}/
    # Create the directory and auto-register it in the docsdb project registry
    # so agents can use it immediately without a manual create-project step.
    try:
        from lib.instance import instance_misc_dir
        misc_dir = instance_misc_dir()
        misc_dir.mkdir(parents=True, exist_ok=True)
        try:
            from datastore.docsdb.registry import ProjectRegistry
            from lib.config import get_docs_db_path
            misc_name = misc_dir.name  # e.g. "misc--openclaw-main"
            rel_home = f"projects/{misc_name}/"
            reg = ProjectRegistry(get_docs_db_path())
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
        payload = dict(ctx.payload or {})
        subtask = str(payload.get("subtask") or payload.get("stage") or "").strip().lower()
        if subtask not in {"project_docs_monitor", "docs_monitor", "project-docs-monitor"}:
            return {"handled": False}

        from core.lifecycle.janitor_lifecycle import RoutineContext, RoutineResult

        routine_ctx = RoutineContext(
            cfg=ctx.config,
            dry_run=bool(payload.get("dry_run", False)),
            workspace=Path(ctx.workspace_root),
            options={
                "reason": payload.get("reason") or "janitor-sanity",
                "requested_by": payload.get("requested_by") or "janitor",
            },
        )
        result = run_project_docs_monitor_maintenance(routine_ctx, RoutineResult)
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
        _ = ctx
        return {"healthy": True}

    def get_system_context_metadata(self, ctx: PluginHookContext) -> dict[str, object]:
        _ = ctx
        return build_docsdb_system_context_metadata()


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


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register DocsDB-owned janitor lifecycle callbacks."""
    registry.register(
        "project_docs_monitor",
        lambda ctx: run_project_docs_monitor_maintenance(ctx, result_factory),
    )
