"""DocsDB datastore plugin contract hooks."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.contracts.plugin_contract import PluginContractBase
from core.runtime.plugins import PluginHookContext

logger = logging.getLogger(__name__)


def build_docsdb_system_context_metadata(*args: Any, **kwargs: Any) -> dict[str, object]:
    from datastore.docsdb.system_context import build_system_context_metadata

    return build_system_context_metadata(*args, **kwargs)


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled
    except ImportError:
        return True
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
        logger.warning("Ignoring invalid supervisor parent pid %r: %s", raw, exc)
        if _fail_hard_enabled():
            raise RuntimeError(f"invalid supervisor parent pid: {raw!r}") from exc
        return None


def _valid_linked_project_instances(entry: Dict[str, Any]) -> list[str]:
    from lib.instance import validate_instance_id

    linked: list[str] = []
    for raw in list(entry.get("instances") or []):
        try:
            linked.append(validate_instance_id(str(raw or "").strip()))
        except Exception as exc:
            logger.debug("Skipping invalid linked project instance %r: %s", raw, exc)
    return sorted(set(linked))


def _project_docs_monitor_skip_reason(name: str, entry: Dict[str, Any]) -> tuple[str, int]:
    if str(os.environ.get("QUAID_INSTANCE", "") or "").strip():
        return "", 0
    linked = _valid_linked_project_instances(entry)
    if len(linked) == 1:
        return "", len(linked)
    return (
        "cannot queue host-wide project-docs monitor request without a resolvable "
        f"QUAID_INSTANCE (valid_linked_instances={len(linked)}: {', '.join(linked) or 'none'})"
    ), len(linked)


def _queue_project_docs_monitor_requests(*, reason: str, requested_by: str) -> Dict[str, Any]:
    """Queue async project-docs monitor refresh requests for registered projects.

    DocsDB owns this maintenance callout. The core project-docs module owns the
    supervisor/request primitives, so this bridge only asks for monitor work and
    never performs heavy docs writes or RAG indexing inline in janitor.
    """
    if _supervisor_disabled():
        msg = "project-docs supervisor is disabled; cannot queue monitor maintenance"
        logger.warning(msg)
        if _fail_hard_enabled():
            raise RuntimeError(msg)
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
        "skipped_projects": [],
    }
    if not names:
        return result

    for name in names:
        try:
            entry = projects.get(name) or {}
            skip_reason, linked_count = _project_docs_monitor_skip_reason(name, entry)
            if skip_reason:
                log = logger.warning if linked_count == 0 else logger.info
                log("Skipping project-docs monitor request for %s: %s", name, skip_reason)
                result["skipped_projects"].append({"name": name, "reason": skip_reason})
                continue
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
        logger.warning("Project-docs monitor maintenance failed: %s", exc)
        if _fail_hard_enabled():
            raise RuntimeError("Project-docs monitor maintenance failed") from exc
        result.errors.append(f"Project-docs monitor maintenance failed: {exc}")
        return result

    errors = list(queued.get("errors") or [])
    skipped_projects = list(queued.get("skipped_projects") or [])
    result.metrics["project_docs_update_requests"] = int(queued.get("requested") or 0)
    result.metrics["project_docs_update_request_errors"] = len(errors)
    result.metrics["project_docs_update_requests_skipped"] = len(skipped_projects)
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


def _validate_project_name(project: Any) -> str:
    from core.project_docs import validate_project_name

    return validate_project_name(str(project or ""))


def _materialize_queued_project_docs_projects(project: Optional[str] = None) -> int:
    """Create durable projects for transcript-driven PROJECT.log queues."""
    from core.docs import updater as docs_updater
    from core.project_registry import create_project, project_deleted_raw, project_exists_raw

    queued = (
        docs_updater.queued_project_log_projects(_validate_project_name(project))
        if project
        else docs_updater.queued_project_log_projects()
    )
    created = 0
    for raw_name in queued:
        name = _validate_project_name(raw_name)
        if name == "quaid" or name.startswith("misc--"):
            logger.info("Skipping queued PROJECT.log auto-create for reserved project %s", name)
            continue
        if project_exists_raw(name):
            continue
        if project_deleted_raw(name):
            logger.info("Skipping queued PROJECT.log auto-create for deleted project %s", name)
            continue
        try:
            create_project(
                name,
                description="Project inferred from conversation continuity.",
            )
            created += 1
        except ValueError as exc:
            logger.info("Skipping queued PROJECT.log auto-create for %s: %s", name, exc)
        except Exception as exc:
            logger.warning("Failed auto-creating queued PROJECT.log project %s: %s", name, exc)
            if _fail_hard_enabled():
                raise
    return created


def _docsdb_project_entries(project: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    from core.project_registry import get_project as get_project_entry
    from core.project_registry import list_projects

    if project:
        name = _validate_project_name(project)
        return {name: get_project_entry(name)}
    return dict(list_projects())


def _sync_visible_project_docs_registry(project: str, entry: Dict[str, Any]) -> Dict[str, int]:
    from core.docs import updater as docs_updater
    from core.project_docs import PROJECT_LOG, UPDATABLE_ROOT_DOCS

    name = _validate_project_name(project)
    canonical_raw = str(entry.get("canonical_path") or "").strip()
    return docs_updater.sync_project_visible_docs(
        name,
        canonical_raw,
        root_docs=UPDATABLE_ROOT_DOCS,
        protected_names={PROJECT_LOG},
    )


def _auto_register_project_docs_via_docsdb(project: Optional[str] = None) -> Tuple[int, list[Dict[str, str]]]:
    """Register visible project docs through DocsDB-owned registry primitives."""
    _materialize_queued_project_docs_projects(project)
    registered = 0
    errors: list[Dict[str, str]] = []
    for name, entry in sorted(_docsdb_project_entries(project).items()):
        if not entry:
            continue
        try:
            result = _sync_visible_project_docs_registry(name, dict(entry))
            registered += int(result.get("registered") or 0)
        except Exception as exc:
            logger.warning("Project docs auto-register failed for %s: %s", name, exc)
            if _fail_hard_enabled():
                raise
            errors.append({"tick": "auto_register", "error": f"{name}: {exc}"})
    return registered, errors


def _index_one_stale_registered_doc_via_docsdb(project: Optional[str] = None) -> bool:
    """Index one stale registered doc through DocsDB-owned updater primitives."""
    from core.docs import updater as docs_updater

    return bool(docs_updater.index_one_stale_registered_doc(project=project))


def _empty_project_update_metrics() -> Dict[str, Any]:
    return {
        "projects_checked": 0,
        "docs_updated": 0,
        "docs_skipped": 0,
        "trivial_skipped": 0,
        "errors": 0,
    }


def _merge_registry_sync_counts(total: Dict[str, int], delta: Dict[str, Any]) -> None:
    for key in ("registered", "unregistered", "project_md_refreshed"):
        total[key] = int(total.get(key) or 0) + int(delta.get(key) or 0)


def handle_project_docs_update_request(event: Dict[str, Any]) -> Dict[str, Any]:
    """Handle project-doc worker apply/index work through DocsDB authority."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    source = str(payload.get("source") or "").strip()
    if source != "project-docs-worker":
        return {"status": "failed", "error": "payload.source must be project-docs-worker"}
    try:
        project = _validate_project_name(payload.get("project"))
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}

    snapshots = payload.get("snapshots")
    if snapshots is None:
        snapshots = []
    if not isinstance(snapshots, list):
        return {"status": "failed", "error": "payload.snapshots must be a list"}
    project_log_entries = payload.get("project_log_entries")
    if project_log_entries is None:
        project_log_entries = []
    if not isinstance(project_log_entries, list):
        return {"status": "failed", "error": "payload.project_log_entries must be a list"}
    request = payload.get("request")
    if request is None:
        request = {}
    if not isinstance(request, dict):
        return {"status": "failed", "error": "payload.request must be an object"}

    metrics = _empty_project_update_metrics()
    registry_sync: Dict[str, int] = {"registered": 0, "unregistered": 0, "project_md_refreshed": 0}
    indexed_docs = 0
    indexed_project_logs = 0
    dry_run = bool(payload.get("dry_run", False))

    from core.docs import updater as docs_updater
    from core.docs_updater_hook import update_project_docs
    from core.project_registry import get_project, get_project_raw

    entry: Dict[str, Any] = {}
    if not dry_run:
        entry = get_project_raw(project) or get_project(project)
        if not entry:
            raise KeyError(f"Project not found: {project}")
        # Refresh PROJECT.md registry-managed sections before the LLM sees the
        # document. Those marker blocks are deterministic, not LLM-owned.
        _merge_registry_sync_counts(registry_sync, _sync_visible_project_docs_registry(project, dict(entry)))

    if snapshots or project_log_entries or request:
        metrics = update_project_docs(
            list(snapshots),
            extraction_result={"project_logs": {project: list(project_log_entries)}},
            dry_run=dry_run,
            force_project=project,
        )

    if not dry_run:
        _merge_registry_sync_counts(registry_sync, _sync_visible_project_docs_registry(project, dict(entry)))
        try:
            from core.project_docs import PROJECT_LOG

            indexed_docs = int(
                docs_updater.update_registered_docs(
                    project=project,
                    dry_run=False,
                    protected_names={PROJECT_LOG},
                    index_project_logs_after=False,
                ) or 0
            )
            indexed_project_logs = int(docs_updater.index_project_logs(project=project) or 0)
        except Exception as exc:
            if _fail_hard_enabled():
                logger.warning("project-docs update index failed for %s: %s", project, exc)
                raise
            logger.warning("project-docs update index failed for %s (fail-soft): %s", project, exc)
            metrics["errors"] = int(metrics.get("errors", 0)) + 1
            metrics["index_error"] = str(exc)

    return {
        "status": "ok",
        "metrics": metrics,
        "registry_sync": registry_sync,
        "indexed_docs": indexed_docs,
        "indexed_project_logs": indexed_project_logs,
    }


def register_project_docs_update_request_handler() -> None:
    from core.runtime.events import DOCS_PROJECT_UPDATE_REQUEST_EVENT, register_request_handler

    register_request_handler(
        DOCS_PROJECT_UPDATE_REQUEST_EVENT,
        handle_project_docs_update_request,
        datastore_id="docsdb",
        force=True,
    )


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
    project = _validate_project_name(payload.get("project")) if payload.get("project") else None
    direct_result: Dict[str, Any] = {
        "auto_register_ran": auto_register_requested,
        "stale_index_ran": stale_index_requested,
        "registered": None,
        "indexed_one": None,
        "errors": [],
    }

    def _listener_result() -> Dict[str, Any]:
        return {
            "mode": "authoritative",
            "datastore_id": "docsdb",
            "project": project,
            "observed_at": observed_at,
            "direct_result": direct_result,
        }

    try:
        if auto_register_requested:
            registered, errors = _auto_register_project_docs_via_docsdb(project)
            direct_result["registered"] = registered
            direct_result["errors"].extend(errors)
    except Exception as exc:
        logger.warning("project docs auto-register listener failed: %s", exc)
        direct_result["errors"].append({"tick": "auto_register", "error": str(exc)})
        if _fail_hard_enabled():
            raise RuntimeError("project docs auto-register listener failed") from exc

    try:
        if stale_index_requested:
            direct_result["indexed_one"] = _index_one_stale_registered_doc_via_docsdb(project)
    except Exception as exc:
        logger.warning("project docs stale-index listener failed: %s", exc)
        direct_result["errors"].append({"tick": "stale_index", "error": str(exc)})
        if _fail_hard_enabled():
            raise RuntimeError("project docs stale-index listener failed") from exc

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
    if not str(os.environ.get("QUAID_INSTANCE", "") or "").strip():
        logger.debug("docsdb misc init skipped: no QUAID_INSTANCE context")
        return

    try:
        from lib.instance import instance_misc_dir
        misc_dir = instance_misc_dir()
        misc_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("docsdb misc workspace directory initialization failed: %s", exc)
        if _fail_hard_enabled():
            raise
        return

    try:
        from datastore.docsdb.registry import DocsRegistry
        from lib.config import get_docs_db_path
        misc_name = misc_dir.name  # e.g. "misc--openclaw-main"
        rel_home = f"projects/{misc_name}/"
        reg = DocsRegistry(get_docs_db_path())
        desc = "Scratch pad for ephemeral and temporary files."
        try:
            if not hasattr(reg, "get_project_definition") or reg.get_project_definition(misc_name) is None:
                reg.create_project(
                    name=misc_name,
                    home_dir=rel_home,
                    description=desc,
                    reload_config=False,
                    quiet=True,
                )
        except ValueError as exc:
            logger.debug("docsdb misc project create raised ValueError; assuming already registered: %s", exc)
    except Exception as exc:
        logger.warning("docsdb misc project registry initialization failed: %s", exc)
        if _fail_hard_enabled():
            raise
        return

    # Also register in global JSON registry so quaid global-registry
    # list and quaid project show can find it. Idempotent.
    try:
        from lib.project_registry import register as global_register
        global_register(
            name=misc_name,
            canonical_path=str(misc_dir),
            description=desc,
        )
    except Exception as exc:
        logger.warning("docsdb misc project global registry update failed: %s", exc)
        if _fail_hard_enabled():
            raise


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
