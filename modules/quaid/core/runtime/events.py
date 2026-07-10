#!/usr/bin/env python3
"""Quaid event bus (queue-backed, adapter-agnostic).

Provides a small extensible event interface for:
- emitting runtime/lifecycle events (new/reset/compaction/timeout/etc.)
- queuing delayed notifications/requests
- processing pending events via registered handlers
"""

from __future__ import annotations

import base64
import argparse
import contextlib
import json
import logging
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from core.ingest_runtime import run_docs_ingest
from core.runtime.paths import get_runtime_root
from lib.runtime_context import queue_deferred_notice
from lib.runtime_context import get_quaid_home
from lib.runtime_context import get_sessions_dir
from lib.runtime_context import get_visible_quaid_home
from lib.runtime_context import get_visible_workspace_dir
from lib.runtime_context import get_workspace_dir

Event = Dict[str, Any]
EventHandler = Callable[[Event], Dict[str, Any]]
RequestEventHandler = Callable[[Event], Dict[str, Any]]
logger = logging.getLogger(__name__)
MAX_EVENT_QUEUE = 2000
MAX_HISTORY_JSONL_BYTES = 5 * 1024 * 1024
HISTORY_TRIM_TARGET_BYTES = 2 * 1024 * 1024
DEFAULT_PENDING_EVENT_MAX_AGE_SECONDS = 6 * 60 * 60
PROCESSING_EVENT_STALE_SECONDS = 15 * 60
EVENT_ENVELOPE_SCHEMA_VERSION = 1
EVENT_CLASSES = {"domain", "request"}
DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT = "docs.project_maintenance_observed"
DOCS_PROJECT_UPDATE_REQUEST_EVENT = "docs.project_update.request.v1"
SESSION_INGEST_LOG_REQUEST_EVENT = "session.ingest_log.request.v1"
MEMORY_EXTRACTION_PUBLISH_REQUEST_EVENT = "memory.extraction_publish.request.v1"
EVOLUTION_SNIPPET_JOURNAL_WRITE_REQUEST_EVENT = "evolution.snippet_journal_write.request.v1"
EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT = "evolution.snippet_write.request.v1"
EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT = "evolution.journal_write.request.v1"
LIFECYCLE_EVENT_TO_DAEMON_SIGNAL = {
    "session.reset": "reset",
    "session.compaction": "compaction",
    "session.timeout": "timeout",
    "session.agent_end": "session_end",
}

EVENT_REGISTRY: List[Dict[str, Any]] = [
    {
        "name": "session.new",
        "description": "Session/new command observed.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "session.reset",
        "description": "Session/reset command observed.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "session.compaction",
        "description": "Compaction workflow observed.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "session.timeout",
        "description": "Inactivity timeout extraction signal observed.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "session.agent_start",
        "description": "Agent lifecycle start observed.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "session.agent_end",
        "description": "Agent lifecycle end observed.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "notification.delayed",
        "description": "Queue deferred operator notice for later explicit retrieval.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "passive",
        "delivery_notes": "Buffered until the active agent explicitly drains deferred notices.",
    },
    {
        "name": "memory.force_compaction",
        "description": "Request compaction via deferred notice queue.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "passive",
        "delivery_notes": "Compaction requests are buffered for later explicit operator handling.",
    },
    {
        "name": "docs.ingest_transcript",
        "description": "Run docs ingestion pipeline from a transcript file path.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT,
        "description": "Handle project-docs supervisor docs maintenance tick through DocsDB authority.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "session.ingest_log",
        "description": "Index lifecycle session transcript into datastore-owned session log RAG.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": SESSION_INGEST_LOG_REQUEST_EVENT,
        "description": "Request SessionDB-owned session transcript ingest with MemoryDB session_chunks projection.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": MEMORY_EXTRACTION_PUBLISH_REQUEST_EVENT,
        "description": "Request MemoryDB-owned extraction fact/source publish work.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": EVOLUTION_SNIPPET_JOURNAL_WRITE_REQUEST_EVENT,
        "description": "Request InsightDB-owned snippet/journal markdown writes.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": EVOLUTION_SNIPPET_WRITE_REQUEST_EVENT,
        "description": "Request InsightDB-owned snippet-only markdown writes.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": EVOLUTION_JOURNAL_WRITE_REQUEST_EVENT,
        "description": "Request InsightDB-owned journal-only markdown writes.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "janitor.run_completed",
        "description": "Process janitor completion payload and queue user-facing notifications.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "recall.memory.request.v1",
        "description": "Request memory/fact/session recall candidates from a manifested datastore.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "recall.graph.request.v1",
        "description": "Request graph traversal recall candidates from a manifested datastore.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "recall.docs.request.v1",
        "description": "Request document recall candidates from a manifested datastore.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "recall.project_context.request.v1",
        "description": "Request project-scoped context from a manifested datastore.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "recall.journal.request.v1",
        "description": "Request journal/evolution recall candidates from a manifested datastore.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "datastore.validate.request.v1",
        "description": "Request datastore schema, index, and artifact validation from manifested datastores.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "datastore.explain.request.v1",
        "description": "Request datastore result/provenance explanation from manifested datastores.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "project.worker_specs.request.v1",
        "description": "Request project worker specifications from manifested datastore policy.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": DOCS_PROJECT_UPDATE_REQUEST_EVENT,
        "description": "Request DocsDB-owned project-doc apply/index work for the project-doc worker.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
    {
        "name": "maintenance.run.request.v1",
        "description": "Request datastore-owned maintenance routines from manifested datastores.",
        "fireable": True,
        "processable": False,
        "listenable": True,
        "delivery_mode": "request",
    },
]

_EVENT_NAME_ALIASES: Dict[str, str] = {
    # Adapter hook names map to canonical Quaid runtime events.
    "before_agent_start": "session.agent_start",
    "agent_end": "session.agent_end",
    "before_compaction": "session.compaction",
    "before_reset": "session.reset",
}

# OpenClaw exposes a few gateway hook event names that do not map 1:1 to the
# runtime event bus names in EVENT_REGISTRY. Treat these as valid declarations
# so standalone Python entrypoints (janitor/extract) do not fail config load.
_ADAPTER_NATIVE_EVENTS: set[str] = {
    "session",
    "session:compact:before",
    "command",
    "message",
    "message_received",
    "message:preprocessed",
    "before_prompt_build",
    "before_agent_reply",
    "session_end",
}


def _canonical_event_name(name: str) -> str:
    token = str(name or "").strip()
    if not token:
        return token
    # Adapter command-scoped events are dynamic (`command:<action>`). Canonicalize
    # known lifecycle actions to runtime event names so adapters can safely declare
    # command hooks in their event contract.
    if token.startswith("command:"):
        action = token.split(":", 1)[1].strip().lower()
        if action in {"new"}:
            return "session.new"
        if action in {"reset", "restart"}:
            return "session.reset"
        if action in {"compact", "compaction"}:
            return "session.compaction"
    return _EVENT_NAME_ALIASES.get(token, token)


def validate_declared_event_contract(
    *,
    registry: Any,
    slots: Dict[str, Any],
    strict: bool = True,
) -> List[str]:
    """Validate manifest-declared events resolve to known runtime events.

    This bridges adapter hook names to canonical runtime event names so mixed
    ecosystems can declare either form safely.
    """
    from core.runtime.plugins import collect_declared_exports

    declared = collect_declared_exports(
        registry=registry,
        slots=slots,
        surface="events",
        strict=strict,
    )
    known = {
        str(item.get("name", "")).strip()
        for item in EVENT_REGISTRY
        if isinstance(item, dict)
    }
    errors: List[str] = []
    for plugin_id, exported in declared.items():
        for raw_name in exported:
            if raw_name in _ADAPTER_NATIVE_EVENTS:
                continue
            canonical = _canonical_event_name(raw_name)
            if canonical in known:
                continue
            errors.append(
                f"Plugin '{plugin_id}' declares unknown event '{raw_name}' "
                f"(canonical '{canonical}')"
            )
    if strict and errors:
        raise ValueError("Event contract validation failed: " + "; ".join(errors))
    return errors


def _now() -> str:
    return _now_datetime().isoformat()


def _now_datetime() -> datetime:
    override = os.environ.get("QUAID_NOW", "").strip()
    if override:
        try:
            value = datetime.fromisoformat(override.replace("Z", "+00:00"))
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        except ValueError:
            logger.warning("Invalid QUAID_NOW=%r", override)
            raise ValueError(f"Invalid QUAID_NOW={override!r}") from None
    return datetime.now(timezone.utc)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _event_paths() -> Dict[str, Path]:
    root = get_workspace_dir()
    runtime = get_runtime_root(root)
    events_dir = runtime / "events"
    notes_dir = runtime / "notes"
    return {
        "queue": events_dir / "queue.json",
        "history_jsonl": events_dir / "history.jsonl",
        "delayed_llm_requests": notes_dir / "delayed-llm-requests.json",
    }


def _is_fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled

        return bool(is_fail_hard_enabled())
    except Exception as exc:
        logger.warning(
            "Failed to import is_fail_hard_enabled; defaulting to fail-hard=True: %s",
            exc,
        )
        return True


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _allowed_transcript_roots() -> List[Path]:
    roots: List[Path] = []
    resolvers = (
        get_workspace_dir,
        get_visible_workspace_dir,
        get_quaid_home,
        get_visible_quaid_home,
        get_sessions_dir,
    )
    for resolver in resolvers:
        try:
            candidate = resolver()
        except Exception as exc:
            logger.debug("Failed resolving transcript root via %s: %s", resolver, exc)
            continue
        if candidate is None:
            continue
        try:
            root = Path(candidate).expanduser().resolve()
        except OSError as exc:
            logger.debug("Failed resolving transcript root %s: %s", candidate, exc)
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _resolve_managed_transcript_path(raw_path: str, *, required: bool) -> Optional[str]:
    value = str(raw_path or "").strip()
    if not value:
        if required:
            raise ValueError("transcript_path is required")
        return None
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except OSError as exc:
        if required or _is_fail_hard_enabled():
            raise
        logger.warning("Failed resolving transcript path %s: %s", value, exc)
        return None
    try:
        is_file = path.is_file()
    except OSError as exc:
        if required or _is_fail_hard_enabled():
            raise
        logger.warning("Failed statting transcript path %s: %s", path, exc)
        return None
    if not is_file:
        if required:
            raise FileNotFoundError(str(path))
        return None
    roots = _allowed_transcript_roots()
    if not any(_path_within(path, root) for root in roots):
        message = f"transcript path is outside Quaid-managed roots: {path}"
        if required or _is_fail_hard_enabled():
            raise PermissionError(message)
        logger.warning(message)
        return None
    return str(path)


def _request_handler_fail_hard_enabled(datastore_id: str) -> bool:
    try:
        from core.datastore_registry import is_datastore_fail_hard_enabled

        return bool(
            is_datastore_fail_hard_enabled(
                datastore_id,
                global_fail_hard=_is_fail_hard_enabled(),
            )
        )
    except Exception as exc:
        if _is_fail_hard_enabled():
            raise RuntimeError(
                "Failed to resolve request handler fail-hard policy while fail-hard mode is enabled"
            ) from exc
        logger.error("Failed to resolve request handler fail-hard policy for %s: %s", datastore_id, exc)
        return False


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed reading JSON file %s: %s", path, exc)
        if _is_fail_hard_enabled():
            raise RuntimeError(
                f"Failed reading JSON file while fail-hard mode is enabled: {path}"
            ) from exc
        return default


def _write_json(path: Path, payload: Any) -> None:
    _ensure_parent(path)
    # Atomic write to avoid truncation races.
    tmp_path: Optional[Path] = None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as tmp:
        tmp.write(json.dumps(payload, indent=2))
        tmp.flush()
        tmp_path = Path(tmp.name)
    try:
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    try:
        os.chmod(path, 0o600)
    except Exception as exc:
        logger.warning("Failed to apply chmod 600 to %s: %s", path, exc)
        if _is_fail_hard_enabled():
            raise RuntimeError(
                f"Failed to chmod event-bus JSON file while fail-hard mode is enabled: {path}"
            ) from exc


def _append_jsonl(path: Path, payload: Any) -> None:
    _ensure_parent(path)
    with _file_lock(_lock_path(path)):
        try:
            if path.exists() and path.stat().st_size > MAX_HISTORY_JSONL_BYTES:
                keep = path.read_bytes()[-HISTORY_TRIM_TARGET_BYTES:]
                newline = keep.find(b"\n")
                if newline >= 0:
                    keep = keep[newline + 1 :]
                path.write_bytes(keep)
        except Exception as exc:
            logger.warning("Failed trimming history file %s; appending new entry anyway: %s", path, exc)
            if _is_fail_hard_enabled():
                raise RuntimeError(
                    f"Failed trimming event history while fail-hard mode is enabled: {path}"
                ) from exc
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextlib.contextmanager
def _file_lock(path: Path):
    _ensure_parent(path)
    lock_handle = open(path, "a+", encoding="utf-8")
    lock_acquired = False
    try:
        try:
            import fcntl  # type: ignore
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            lock_acquired = True
        except Exception as exc:
            logger.warning("Failed to acquire event file lock %s: %s", path, exc)
            if _is_fail_hard_enabled():
                raise RuntimeError(f"Failed to acquire event file lock while fail-hard mode is enabled: {path}") from exc
        yield
    finally:
        if lock_acquired:
            body_exc_type = sys.exc_info()[0]
            try:
                import fcntl  # type: ignore
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            except Exception as exc:
                logger.warning("Failed to release event file lock %s: %s", path, exc)
                if _is_fail_hard_enabled() and body_exc_type is None:
                    lock_handle.close()
                    raise RuntimeError(f"Failed to release event file lock while fail-hard mode is enabled: {path}") from exc
        lock_handle.close()


def _read_modify_write_json(path: Path, default: Any, mutator: Callable[[Any], Any]) -> Any:
    with _file_lock(_lock_path(path)):
        current = _read_json(path, default)
        updated = mutator(current)
        _write_json(path, updated)
        return updated


def _next_event_id(name: str, ts: str) -> str:
    raw = f"{name}:{ts}".encode("utf-8")
    prefix = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:18]
    return f"evt-{prefix}-{uuid.uuid4().hex[:10]}"


def _next_correlation_id(name: str, ts: str) -> str:
    raw = f"correlation:{name}:{ts}".encode("utf-8")
    prefix = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:18]
    return f"corr-{prefix}-{uuid.uuid4().hex[:10]}"


def _parse_event_timestamp(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_age_seconds(event: Event, *, now_dt: datetime) -> Optional[float]:
    created_at = _parse_event_timestamp(event.get("created_at"))
    if created_at is None:
        return None
    return (now_dt - created_at).total_seconds()


def _event_class_for_name(name: str) -> str:
    parts = [part.strip().lower() for part in str(name or "").split(".") if part.strip()]
    return "request" if "request" in parts else "domain"


def _trace_event(event: Event) -> Dict[str, Any]:
    return {
        key: event.get(key)
        for key in (
            "id",
            "name",
            "event_type",
            "event_class",
            "schema_version",
            "correlation_id",
            "idempotency_key",
            "status",
        )
        if event.get(key) is not None
    }


def _make_event_envelope(
    *,
    name: str,
    payload: Optional[Dict[str, Any]],
    source: str,
    session_id: Optional[str],
    owner_id: Optional[str],
    priority: str,
    instance_id: Optional[str] = None,
    project_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    provenance: Optional[Dict[str, Any]] = None,
    event_class: Optional[str] = None,
    schema_version: int = EVENT_ENVELOPE_SCHEMA_VERSION,
    created_at: Optional[str] = None,
) -> Event:
    name = str(name or "").strip()
    ts = str(created_at or _now())
    resolved_class = str(event_class or _event_class_for_name(name)).strip().lower()
    event: Event = {
        "id": _next_event_id(name, ts),
        "name": name,
        "event_type": name,
        "event_class": resolved_class,
        "schema_version": schema_version,
        "payload": payload or {},
        "source": str(source or "unknown"),
        "priority": str(priority or "normal"),
        "created_at": ts,
        "provenance": provenance if isinstance(provenance, dict) else {},
        "status": "pending",
        "instance_id": str(instance_id) if instance_id else None,
        "project_id": str(project_id) if project_id else None,
        "session_id": str(session_id) if session_id else None,
        "owner_id": str(owner_id) if owner_id else None,
        "correlation_id": str(correlation_id) if correlation_id else None,
        "idempotency_key": str(idempotency_key) if idempotency_key else None,
        "duplicate": False,
        "duplicate_of": None,
    }
    if not event["correlation_id"] and resolved_class == "request":
        event["correlation_id"] = _next_correlation_id(name, ts)
    return event


def validate_event_envelope(event: Event) -> List[str]:
    errors: List[str] = []
    if not isinstance(event, dict):
        return ["event envelope must be an object"]
    name = str(event.get("name") or "").strip()
    event_type = str(event.get("event_type") or "").strip()
    if not name:
        errors.append("name is required")
    if not event_type:
        errors.append("event_type is required")
    if name and event_type and name != event_type:
        errors.append("name and event_type must match during M1 compatibility")
    if event.get("schema_version") != EVENT_ENVELOPE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {EVENT_ENVELOPE_SCHEMA_VERSION}")
    event_class = str(event.get("event_class") or "").strip().lower()
    if event_class not in EVENT_CLASSES:
        errors.append("event_class must be domain or request")
    if event_class == "request" and not str(event.get("correlation_id") or "").strip():
        errors.append("request events require correlation_id")
    if not str(event.get("source") or "").strip():
        errors.append("source is required")
    if not str(event.get("created_at") or "").strip():
        errors.append("created_at is required")
    if not isinstance(event.get("payload"), dict):
        errors.append("payload must be an object")
    if not isinstance(event.get("provenance"), dict):
        errors.append("provenance must be an object")
    return errors


def _enforce_broker_envelope(event: Event) -> None:
    errors = validate_event_envelope(event)
    if not errors:
        return
    message = "; ".join(errors)
    if _is_fail_hard_enabled():
        raise RuntimeError(
            f"Invalid event envelope while fail-hard mode is enabled: {message}"
        )
    logger.error("Invalid event envelope: %s", message)
    event["validation_errors"] = errors


def _enqueue_event(event: Event) -> Event:
    paths = _event_paths()
    ts = str(event.get("created_at") or _now())
    idempotency_key = str(event.get("idempotency_key") or "").strip()
    event_type = str(event.get("event_type") or event.get("name") or "").strip()
    stored_event: Event = event
    deduped = False
    dropped = 0

    def _mutate(payload: Any) -> Any:
        nonlocal stored_event, deduped, dropped
        queue_payload = payload if isinstance(payload, dict) else {"version": 1, "events": []}
        events = queue_payload.get("events")
        if not isinstance(events, list):
            events = []
        if idempotency_key:
            for existing in events:
                if not isinstance(existing, dict):
                    continue
                existing_key = str(existing.get("idempotency_key") or "").strip()
                existing_type = str(existing.get("event_type") or existing.get("name") or "").strip()
                existing_status = str(existing.get("status") or "pending").strip().lower()
                if existing_status not in {"pending", "processing"}:
                    continue
                existing_session_id = str(existing.get("session_id") or "").strip()
                event_session_id = str(event.get("session_id") or "").strip()
                if existing_session_id and event_session_id and existing_session_id != event_session_id:
                    continue
                existing_owner_id = str(existing.get("owner_id") or "").strip()
                event_owner_id = str(event.get("owner_id") or "").strip()
                if existing_owner_id and event_owner_id and existing_owner_id != event_owner_id:
                    continue
                if existing_key == idempotency_key and existing_type == event_type:
                    stored_event = dict(existing)
                    stored_event["duplicate"] = True
                    stored_event["duplicate_of"] = existing.get("id")
                    deduped = True
                    return {"version": 1, "events": events}
        events.append(event)
        if len(events) > MAX_EVENT_QUEUE:
            dropped = len(events) - MAX_EVENT_QUEUE
            logger.warning("Event queue overflow: dropped %d oldest event(s)", dropped)
            if _is_fail_hard_enabled():
                raise RuntimeError(
                    f"Event queue overflow while fail-hard mode is enabled: dropped {dropped} event(s)"
                )
            events = events[-MAX_EVENT_QUEUE:]
        return {"version": 1, "events": events}

    _read_modify_write_json(paths["queue"], {"version": 1, "events": []}, _mutate)
    _append_jsonl(
        paths["history_jsonl"],
        {"ts": ts, "op": "dedupe" if deduped else "emit", "event": stored_event, "dropped": dropped},
    )
    if dropped:
        stored_event["dropped"] = dropped
    return stored_event


def _queue_delayed_llm_request(message: str, kind: str = "janitor", priority: str = "normal", source: str = "quaid_events") -> bool:
    message = str(message or "").strip()
    if not message:
        return False
    return queue_deferred_notice(
        message,
        kind=kind,
        priority=priority,
        source=source,
    )


def _daemon_signal_requested(payload: Dict[str, Any]) -> bool:
    daemon_signal = payload.get("daemon_signal") if isinstance(payload, dict) else None
    return isinstance(daemon_signal, dict) and daemon_signal.get("enabled") is True


def _maybe_queue_lifecycle_daemon_signal(event: Event, *, session_id: str) -> Optional[Dict[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    daemon_signal = payload.get("daemon_signal") if isinstance(payload.get("daemon_signal"), dict) else {}
    if daemon_signal.get("enabled") is not True:
        return None

    signal_type = LIFECYCLE_EVENT_TO_DAEMON_SIGNAL.get(str(event.get("name") or ""))
    if not signal_type:
        return None

    if not str(session_id or "").strip():
        raise ValueError("payload.session_id is required for daemon_signal bridge")

    transcript_path = _resolve_managed_transcript_path(
        str(daemon_signal.get("transcript_path") or payload.get("transcript_path") or "").strip(),
        required=True,
    )

    meta: Dict[str, Any] = {
        "bridge": "event_lifecycle_bridge",
        "lifecycle_event_id": event.get("id"),
        "lifecycle_event_name": event.get("name"),
    }
    reason = str(daemon_signal.get("reason") or "").strip()
    if reason:
        meta["reason"] = reason
    source = str(daemon_signal.get("source") or "").strip()
    if source:
        meta["source"] = source

    from core.extraction_daemon import write_signal

    signal_path = write_signal(
        signal_type=signal_type,
        session_id=session_id,
        transcript_path=str(transcript_path),
        adapter=str(event.get("source") or ""),
        meta=meta,
    )
    return {
        "daemon_signal_queued": True,
        "daemon_signal_type": signal_type,
        "signal_name": signal_path.name,
    }


def _default_reset_transcript_path(event: Event, *, session_id: str) -> Optional[str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if str(event.get("name") or "") != "session.reset":
        return None
    if _daemon_signal_requested(payload):
        return None
    if not str(session_id or "").strip():
        return None

    return _resolve_managed_transcript_path(
        str(payload.get("reset_transcript_path") or "").strip(),
        required=False,
    )


def _maybe_queue_default_reset_signal(event: Event, *, session_id: str) -> Optional[Dict[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    transcript_path = _default_reset_transcript_path(event, session_id=session_id)
    if not transcript_path:
        return None

    meta: Dict[str, Any] = {
        "bridge": "event_lifecycle_default_reset_bridge",
        "lifecycle_event_id": event.get("id"),
        "lifecycle_event_name": event.get("name"),
    }
    for key in ("adapter", "source", "reason", "reset_transcript_source"):
        value = str(payload.get(key) or "").strip()
        if value:
            meta[key] = value

    from core.extraction_daemon import write_signal

    signal_path = write_signal(
        signal_type="reset",
        session_id=session_id,
        transcript_path=transcript_path,
        adapter=str(event.get("source") or ""),
        meta=meta,
    )
    return {
        "daemon_signal_queued": True,
        "daemon_signal_type": "reset",
        "signal_name": signal_path.name,
        "daemon_signal_default": True,
    }


def _default_agent_end_transcript_path(event: Event, *, session_id: str) -> Optional[str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if str(event.get("name") or "") != "session.agent_end":
        return None
    if _daemon_signal_requested(payload):
        return None
    if not str(session_id or "").strip():
        return None

    return _resolve_managed_transcript_path(
        str(payload.get("transcript_path") or "").strip(),
        required=False,
    )


def _maybe_queue_default_agent_end_signal(event: Event, *, session_id: str) -> Optional[Dict[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    transcript_path = _default_agent_end_transcript_path(event, session_id=session_id)
    if not transcript_path:
        return None

    meta: Dict[str, Any] = {
        "bridge": "event_lifecycle_default_bridge",
        "lifecycle_event_id": event.get("id"),
        "lifecycle_event_name": event.get("name"),
    }
    for key in ("adapter", "source"):
        value = str(payload.get(key) or "").strip()
        if value:
            meta[key] = value

    from core.extraction_daemon import write_signal

    signal_path = write_signal(
        signal_type="session_end",
        session_id=session_id,
        transcript_path=transcript_path,
        adapter=str(event.get("source") or ""),
        meta=meta,
    )
    return {
        "daemon_signal_queued": True,
        "daemon_signal_type": "session_end",
        "signal_name": signal_path.name,
        "daemon_signal_default": True,
    }


def _default_timeout_transcript_path(event: Event, *, session_id: str) -> Optional[str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if str(event.get("name") or "") != "session.timeout":
        return None
    if _daemon_signal_requested(payload):
        return None
    if not str(session_id or "").strip():
        return None

    return _resolve_managed_transcript_path(
        str(payload.get("transcript_path") or "").strip(),
        required=False,
    )


def _maybe_queue_default_timeout_signal(event: Event, *, session_id: str) -> Optional[Dict[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    transcript_path = _default_timeout_transcript_path(event, session_id=session_id)
    if not transcript_path:
        return None

    meta: Dict[str, Any] = {
        "bridge": "event_lifecycle_default_timeout_bridge",
        "lifecycle_event_id": event.get("id"),
        "lifecycle_event_name": event.get("name"),
    }
    for key in ("adapter", "source"):
        value = str(payload.get(key) or "").strip()
        if value:
            meta[key] = value

    from core.extraction_daemon import write_signal

    signal_path = write_signal(
        signal_type="timeout",
        session_id=session_id,
        transcript_path=transcript_path,
        adapter=str(event.get("source") or ""),
        meta=meta,
    )
    return {
        "daemon_signal_queued": True,
        "daemon_signal_type": "timeout",
        "signal_name": signal_path.name,
        "daemon_signal_default": True,
    }


def _default_compaction_transcript_path(event: Event, *, session_id: str) -> Optional[str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if str(event.get("name") or "") != "session.compaction":
        return None
    if _daemon_signal_requested(payload):
        return None
    if not str(session_id or "").strip():
        return None

    return _resolve_managed_transcript_path(
        str(payload.get("transcript_path") or "").strip(),
        required=False,
    )


def _maybe_queue_default_compaction_signal(event: Event, *, session_id: str) -> Optional[Dict[str, Any]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    transcript_path = _default_compaction_transcript_path(event, session_id=session_id)
    if not transcript_path:
        return None

    meta: Dict[str, Any] = {
        "bridge": "event_lifecycle_default_compaction_bridge",
        "lifecycle_event_id": event.get("id"),
        "lifecycle_event_name": event.get("name"),
    }
    for key in ("adapter", "source"):
        value = str(payload.get(key) or "").strip()
        if value:
            meta[key] = value

    from core.extraction_daemon import write_signal

    signal_path = write_signal(
        signal_type="compaction",
        session_id=session_id,
        transcript_path=transcript_path,
        adapter=str(event.get("source") or ""),
        supports_compaction_control=payload.get("supports_compaction_control") is True,
        meta=meta,
    )
    return {
        "daemon_signal_queued": True,
        "daemon_signal_type": "compaction",
        "signal_name": signal_path.name,
        "daemon_signal_default": True,
    }


def _wake_daemon_after_lifecycle_signal(signal_result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not signal_result or signal_result.get("daemon_signal_queued") is not True:
        return None
    if not str(signal_result.get("signal_name") or "").strip():
        return None

    from core.extraction_daemon import ensure_alive

    pid = ensure_alive()
    try:
        daemon_pid = int(pid)
    except (TypeError, ValueError):
        daemon_pid = -1
    if daemon_pid <= 0:
        raise RuntimeError(f"daemon ensure_alive returned invalid pid: {pid}")
    return {
        "daemon_wake_attempted": True,
        "daemon_wake_succeeded": True,
        "daemon_wake_pid": daemon_pid,
    }


def _handle_session_lifecycle(event: Event) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    session_id = str(event.get("session_id") or payload.get("session_id") or "").strip()
    result = {"status": "acknowledged", "event": event.get("name")}
    if not session_id:
        if _daemon_signal_requested(payload):
            try:
                _maybe_queue_lifecycle_daemon_signal(event, session_id=session_id)
            except Exception as exc:
                if _is_fail_hard_enabled():
                    raise
                logger.warning("Lifecycle daemon signal bridge failed: %s", exc, exc_info=True)
                result["daemon_signal_queued"] = False
                result["daemon_signal_error"] = str(exc)
        result["persisted"] = False
        return result
    persisted = None
    try:
        from core.plugins.sessiondb_contract import record_session_lifecycle_observation

        persisted = record_session_lifecycle_observation(event)
    except Exception as exc:
        if _is_fail_hard_enabled():
            raise
        logger.warning("SessionDB lifecycle observation persistence failed: %s", exc, exc_info=True)
        result["persisted"] = False
        if (
            not _daemon_signal_requested(payload)
            and not _default_reset_transcript_path(event, session_id=session_id)
            and not _default_agent_end_transcript_path(event, session_id=session_id)
            and not _default_timeout_transcript_path(event, session_id=session_id)
            and not _default_compaction_transcript_path(event, session_id=session_id)
        ):
            return result
    if isinstance(persisted, dict) and persisted.get("persisted"):
        result["persisted"] = True
        result["datastore_id"] = "sessiondb"
        result["inserted"] = bool(persisted.get("inserted"))
    else:
        result["persisted"] = False

    try:
        if _daemon_signal_requested(payload):
            daemon_signal_result = _maybe_queue_lifecycle_daemon_signal(event, session_id=session_id)
        elif str(event.get("name") or "") == "session.reset":
            daemon_signal_result = _maybe_queue_default_reset_signal(event, session_id=session_id)
        elif str(event.get("name") or "") == "session.compaction":
            daemon_signal_result = _maybe_queue_default_compaction_signal(event, session_id=session_id)
        elif str(event.get("name") or "") == "session.timeout":
            daemon_signal_result = _maybe_queue_default_timeout_signal(event, session_id=session_id)
        elif str(event.get("name") or "") == "session.agent_end":
            daemon_signal_result = _maybe_queue_default_agent_end_signal(event, session_id=session_id)
        else:
            daemon_signal_result = None
    except Exception as exc:
        if _is_fail_hard_enabled():
            raise
        logger.warning("Lifecycle daemon signal bridge failed: %s", exc, exc_info=True)
        result["daemon_signal_queued"] = False
        if not _daemon_signal_requested(payload):
            result["daemon_signal_default"] = True
        result["daemon_signal_error"] = str(exc)
        return result
    if daemon_signal_result:
        result.update(daemon_signal_result)
        try:
            wake_result = _wake_daemon_after_lifecycle_signal(daemon_signal_result)
        except Exception as exc:
            if _is_fail_hard_enabled():
                raise
            logger.warning("Lifecycle daemon wake failed: %s", exc, exc_info=True)
            result["daemon_wake_attempted"] = True
            result["daemon_wake_succeeded"] = False
            result["daemon_wake_error"] = str(exc)
            return result
        if wake_result:
            result.update(wake_result)
    return result


def _handle_delayed_notification(event: Event) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    message = str(payload.get("message") or "").strip()
    kind = str(payload.get("kind") or "janitor").strip() or "janitor"
    priority = str(payload.get("priority") or "normal").strip() or "normal"
    if not message:
        return {"status": "failed", "error": "payload.message is required"}
    queued = _queue_delayed_llm_request(message=message, kind=kind, priority=priority, source="event.notification.delayed")
    return {"status": "queued" if queued else "duplicate", "queued": queued}


def _handle_force_compaction(event: Event) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    reason = str(payload.get("reason") or "Requested via event interface").strip()
    message = f"[Quaid] Maintenance request: run compaction now. Reason: {reason}"
    queued = _queue_delayed_llm_request(message=message, kind="compaction", priority="high", source="event.memory.force_compaction")
    return {"status": "queued" if queued else "duplicate", "queued": queued}


def _handle_docs_ingest_transcript(event: Event) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    transcript_path = str(payload.get("transcript_path") or "").strip()
    label = str(payload.get("label") or "Unknown").strip() or "Unknown"
    session_id = payload.get("session_id")
    if not transcript_path:
        return {"status": "failed", "error": "payload.transcript_path is required"}
    try:
        managed_path = _resolve_managed_transcript_path(transcript_path, required=True)
        result = run_docs_ingest(
            Path(str(managed_path)),
            label,
            str(session_id) if session_id else None,
        )
        if isinstance(result, dict) and str(result.get("status") or "").lower() == "error":
            return {"status": "failed", "result": result}
        return {"status": "processed", "result": result}
    except Exception as e:  # pragma: no cover
        logger.error("Docs transcript ingest event failed: %s", e, exc_info=True)
        if _is_fail_hard_enabled():
            raise
        return {"status": "failed", "error": str(e)}


def _handle_docs_project_maintenance_observed(event: Event) -> Dict[str, Any]:
    from core.plugins.docsdb_contract import handle_project_docs_maintenance_event

    return handle_project_docs_maintenance_event(event)


def _handle_session_ingest_log(event: Event) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    session_id = str(payload.get("session_id") or "").strip()

    if not session_id:
        return {"status": "failed", "error": "payload.session_id is required"}

    from core.plugins.sessiondb_contract import run_session_ingest_payload

    result = run_session_ingest_payload(payload)
    if isinstance(result, dict) and str(result.get("status") or "").lower() in {"failed", "error"}:
        return {"status": "failed", "result": result}
    return {"status": "processed", "result": result}


def _handle_janitor_run_completed(event: Event) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    applied_changes = payload.get("applied_changes") if isinstance(payload.get("applied_changes"), dict) else {}
    today_memories = payload.get("today_memories") if isinstance(payload.get("today_memories"), list) else []
    try:
        from config import get_config
        from core.runtime.notify import format_janitor_summary_message, format_daily_memories_message

        cfg = get_config()
        queued = 0
        if cfg.notifications.should_notify("janitor", detail="summary"):
            summary = format_janitor_summary_message(metrics, applied_changes)
            if _queue_delayed_llm_request(
                message=summary,
                kind="janitor_summary",
                priority="normal",
                source="event.janitor.run_completed",
            ):
                queued += 1

        if cfg.notifications.should_notify("janitor", detail="full"):
            digest = format_daily_memories_message(today_memories)
            if digest and _queue_delayed_llm_request(
                message=digest,
                kind="janitor_daily_digest",
                priority="low",
                source="event.janitor.run_completed",
            ):
                queued += 1

        return {"status": "processed", "queued": queued}
    except Exception as e:  # pragma: no cover
        logger.error("Janitor completion event handler failed: %s", e, exc_info=True)
        if _is_fail_hard_enabled():
            raise
        return {"status": "failed", "error": str(e)}


EVENT_HANDLERS: Dict[str, EventHandler] = {
    "notification.delayed": _handle_delayed_notification,
    "memory.force_compaction": _handle_force_compaction,
    "docs.ingest_transcript": _handle_docs_ingest_transcript,
    DOCS_PROJECT_MAINTENANCE_OBSERVED_EVENT: _handle_docs_project_maintenance_observed,
    "session.ingest_log": _handle_session_ingest_log,
    "janitor.run_completed": _handle_janitor_run_completed,
    "session.new": _handle_session_lifecycle,
    "session.reset": _handle_session_lifecycle,
    "session.compaction": _handle_session_lifecycle,
    "session.timeout": _handle_session_lifecycle,
    "session.agent_start": _handle_session_lifecycle,
    "session.agent_end": _handle_session_lifecycle,
}
_EVENT_HANDLERS_LOCK = Lock()
_REQUEST_EVENT_HANDLERS: Dict[str, List[Dict[str, Any]]] = {}
_REQUEST_EVENT_HANDLERS_LOCK = Lock()


def register_event_handler(name: str, handler: EventHandler, *, force: bool = False) -> None:
    event_name = str(name or "").strip()
    if not event_name:
        raise ValueError("event handler name is required")
    event_name = _canonical_event_name(event_name)
    capability = get_event_capability(event_name)
    if capability is None:
        raise ValueError(f"event handler name is not registered: {event_name}")
    if str(capability.get("delivery_mode") or "").strip().lower() == "request" or capability.get("processable") is False:
        raise ValueError(f"event handler name is not an active processable event: {event_name}")
    if not callable(handler):
        raise TypeError(f"Event handler {event_name} is not callable")
    with _EVENT_HANDLERS_LOCK:
        existing = EVENT_HANDLERS.get(event_name)
        if existing is not None and existing is not handler and not force:
            logger.warning(
                "register_event_handler skipped overwrite for '%s' (pass force=True to replace)",
                event_name,
            )
            return
        if existing is not None and existing is not handler and force:
            logger.warning("register_event_handler overwriting existing handler for '%s'", event_name)
        EVENT_HANDLERS[event_name] = handler


def register_request_handler(
    event_type: str,
    handler: RequestEventHandler,
    *,
    datastore_id: str,
    force: bool = False,
) -> None:
    request_type = str(event_type or "").strip()
    target_id = str(datastore_id or "").strip()
    if not request_type:
        raise ValueError("request event_type is required")
    if not target_id:
        raise ValueError("request datastore_id is required")
    capability = get_event_capability(request_type)
    if capability is None or str(capability.get("delivery_mode") or "").strip().lower() != "request":
        raise ValueError(f"request event_type is not registered as request: {request_type}")
    try:
        from core.datastore_registry import get_datastore_manifest

        manifest = get_datastore_manifest(target_id)
    except Exception as exc:
        if _is_fail_hard_enabled():
            raise RuntimeError(
                "Failed to validate request handler datastore manifest while fail-hard mode is enabled"
            ) from exc
        logger.error("Failed to validate request handler datastore manifest: %s", exc)
        manifest = None
    if manifest is None:
        raise ValueError(f"request datastore is not registered: {target_id}")
    manifest_handlers = set(manifest.get("request_handlers") or [])
    if request_type not in manifest_handlers:
        raise ValueError(f"datastore {target_id} does not declare request handler {request_type}")
    if not callable(handler):
        raise TypeError(f"Request handler {request_type}/{target_id} is not callable")

    with _REQUEST_EVENT_HANDLERS_LOCK:
        handlers = list(_REQUEST_EVENT_HANDLERS.get(request_type) or [])
        existing_index = next(
            (
                index
                for index, registration in enumerate(handlers)
                if str(registration.get("datastore_id") or "") == target_id
            ),
            None,
        )
        registration = {"datastore_id": target_id, "handler": handler}
        if existing_index is not None and not force:
            logger.warning(
                "register_request_handler skipped overwrite for '%s' datastore '%s' (pass force=True to replace)",
                request_type,
                target_id,
            )
            return
        if existing_index is not None:
            handlers[existing_index] = registration
        else:
            handlers.append(registration)
        _REQUEST_EVENT_HANDLERS[request_type] = handlers


def get_event_registry() -> List[Dict[str, Any]]:
    return [dict(item) for item in EVENT_REGISTRY]


def get_event_capability(name: str) -> Optional[Dict[str, Any]]:
    target = str(name or "").strip()
    if not target:
        return None
    for entry in EVENT_REGISTRY:
        if str(entry.get("name") or "") == target:
            return dict(entry)
    return None


def emit_event(
    name: str,
    payload: Optional[Dict[str, Any]] = None,
    source: str = "unknown",
    session_id: Optional[str] = None,
    owner_id: Optional[str] = None,
    priority: str = "normal",
    instance_id: Optional[str] = None,
    project_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("event name is required")
    event = _make_event_envelope(
        name=name,
        payload=payload,
        source=source,
        session_id=session_id,
        owner_id=owner_id,
        priority=priority,
        instance_id=instance_id,
        project_id=project_id,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
        provenance=provenance,
    )
    _enforce_broker_envelope(event)
    return _enqueue_event(event)


class EventBroker:
    """Thin M1 broker facade over the existing queue-backed event system."""

    def emit(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        source: str = "unknown",
        session_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        priority: str = "normal",
        instance_id: Optional[str] = None,
        project_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        provenance: Optional[Dict[str, Any]] = None,
        event_class: Optional[str] = None,
        schema_version: int = EVENT_ENVELOPE_SCHEMA_VERSION,
    ) -> Event:
        name = str(event_type or "").strip()
        if not name:
            raise ValueError("event_type is required")
        ts = _now()
        resolved_class = str(event_class or _event_class_for_name(name)).strip().lower()
        resolved_correlation = correlation_id
        if resolved_class == "request" and not resolved_correlation:
            resolved_correlation = _next_correlation_id(name, ts)
        event = _make_event_envelope(
            name=name,
            payload=payload,
            source=source,
            session_id=session_id,
            owner_id=owner_id,
            priority=priority,
            instance_id=instance_id,
            project_id=project_id,
            correlation_id=resolved_correlation,
            idempotency_key=idempotency_key,
            provenance=provenance,
            event_class=resolved_class,
            schema_version=schema_version,
            created_at=ts,
        )
        _enforce_broker_envelope(event)
        queued = _enqueue_event(event)
        _append_jsonl(
            _event_paths()["history_jsonl"],
            {
                "ts": _now(),
                "op": "broker.duplicate" if queued.get("duplicate") else "broker.emitted",
                "event": _trace_event(queued),
            },
        )
        return queued

    def dispatch(self, limit: int = 20, names: Optional[List[str]] = None) -> Dict[str, Any]:
        result = process_events(limit=limit, names=names)
        history_path = _event_paths()["history_jsonl"]
        for detail in result.get("details") or []:
            if not isinstance(detail, dict):
                continue
            trace = {
                "id": detail.get("id"),
                "name": detail.get("name"),
                "status": detail.get("status"),
            }
            _append_jsonl(history_path, {"ts": _now(), "op": "broker.dispatched", "event": trace})
            if str(detail.get("status") or "") == "failed":
                _append_jsonl(history_path, {"ts": _now(), "op": "broker.failed", "event": trace})
            else:
                _append_jsonl(history_path, {"ts": _now(), "op": "broker.acked", "event": trace})
        return result

    def request(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        source: str = "unknown",
        session_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        priority: str = "normal",
        instance_id: Optional[str] = None,
        project_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        provenance: Optional[Dict[str, Any]] = None,
        schema_version: int = EVENT_ENVELOPE_SCHEMA_VERSION,
    ) -> Dict[str, Any]:
        name = str(event_type or "").strip()
        if not name:
            raise ValueError("event_type is required")
        ts = _now()
        event = _make_event_envelope(
            name=name,
            payload=payload,
            source=source,
            session_id=session_id,
            owner_id=owner_id,
            priority=priority,
            instance_id=instance_id,
            project_id=project_id,
            correlation_id=correlation_id or _next_correlation_id(name, ts),
            idempotency_key=idempotency_key,
            provenance=provenance,
            event_class="request",
            schema_version=schema_version,
            created_at=ts,
        )
        _enforce_broker_envelope(event)
        history_path = _event_paths()["history_jsonl"]
        _append_jsonl(history_path, {"ts": _now(), "op": "broker.requested", "event": _trace_event(event)})

        with _REQUEST_EVENT_HANDLERS_LOCK:
            registrations = list(_REQUEST_EVENT_HANDLERS.get(name) or [])

        if not registrations:
            message = f"No request handler registered for {name}"
            if _is_fail_hard_enabled():
                raise RuntimeError(
                    f"Request handler missing while fail-hard mode is enabled: {message}"
                )
            logger.error(message)
            _append_jsonl(history_path, {"ts": _now(), "op": "broker.request_failed", "event": _trace_event(event), "error": message})
            return {
                "status": "failed",
                "error": message,
                "event": _trace_event(event),
                "handler_count": 0,
                "failed": 1,
                "responses": [],
            }

        responses: List[Dict[str, Any]] = []
        failed = 0
        fail_hard_errors: List[str] = []
        first_fail_hard_exception: Optional[BaseException] = None
        for registration in registrations:
            datastore_id = str(registration.get("datastore_id") or "").strip()
            handler = registration.get("handler")
            try:
                result = handler(event)  # type: ignore[misc]
            except Exception as exc:
                if first_fail_hard_exception is None:
                    first_fail_hard_exception = exc
                failed += 1
                message = str(exc)
                _append_jsonl(
                    history_path,
                    {"ts": _now(), "op": "broker.request_failed", "event": _trace_event(event), "handler": datastore_id, "error": message},
                )
                if _request_handler_fail_hard_enabled(datastore_id):
                    fail_hard_errors.append(f"{datastore_id}: {message}")
                logger.error("Request handler %s/%s failed: %s", name, datastore_id, message)
                responses.append({
                    "datastore_id": datastore_id,
                    "status": "failed",
                    "error": message,
                    "result": {},
                })
                continue

            if result is None:
                result_payload: Dict[str, Any] = {}
            elif isinstance(result, dict):
                result_payload = dict(result)
            else:
                result_payload = {
                    "status": "failed",
                    "error": f"{name}/{datastore_id} returned non-object response",
                }
            status = str(result_payload.get("status") or "ok").strip().lower() or "ok"
            response = {
                "datastore_id": datastore_id,
                "status": status,
                "result": result_payload,
            }
            responses.append(response)
            if status in {"failed", "error", "nacked"}:
                failed += 1
                message = str(result_payload.get("error") or f"{name}/{datastore_id} returned {status}")
                _append_jsonl(
                    history_path,
                    {"ts": _now(), "op": "broker.request_failed", "event": _trace_event(event), "handler": datastore_id, "error": message},
                )
                if _request_handler_fail_hard_enabled(datastore_id):
                    fail_hard_errors.append(f"{datastore_id}: {message}")
            else:
                _append_jsonl(
                    history_path,
                    {"ts": _now(), "op": "broker.request_acked", "event": _trace_event(event), "handler": datastore_id},
                )

        if fail_hard_errors:
            raise RuntimeError(
                "Request handler failed while fail-hard mode is enabled: "
                + "; ".join(fail_hard_errors)
            ) from first_fail_hard_exception

        if failed == 0:
            status = "ok"
        elif failed < len(registrations):
            status = "partial"
        else:
            status = "failed"
        return {
            "status": status,
            "event": _trace_event(event),
            "handler_count": len(registrations),
            "failed": failed,
            "responses": responses,
        }


def get_event_broker() -> EventBroker:
    return EventBroker()


def emit_broker_event(event_type: str, payload: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Event:
    return get_event_broker().emit(event_type, payload, **kwargs)


def dispatch_broker_events(limit: int = 20, names: Optional[List[str]] = None) -> Dict[str, Any]:
    return get_event_broker().dispatch(limit=limit, names=names)


def request_broker_event(event_type: str, payload: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
    return get_event_broker().request(event_type, payload, **kwargs)


def list_events(status: str = "pending", limit: int = 50) -> List[Event]:
    paths = _event_paths()
    queue_payload = _read_json(paths["queue"], {"version": 1, "events": []})
    events = queue_payload.get("events") if isinstance(queue_payload, dict) else []
    if not isinstance(events, list):
        return []
    status = str(status or "pending").strip().lower()
    if status not in {"pending", "processing", "processed", "failed", "expired", "all"}:
        status = "pending"
    filtered = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if status != "all" and str(event.get("status", "pending")).lower() != status:
            continue
        filtered.append(event)
    filtered.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
    return filtered[: max(1, min(int(limit), 500))]


def process_events(
    limit: int = 20,
    names: Optional[List[str]] = None,
    max_age_seconds: Optional[int] = DEFAULT_PENDING_EVENT_MAX_AGE_SECONDS,
) -> Dict[str, Any]:
    paths = _event_paths()
    claimed: List[Dict[str, Any]] = []
    name_filter = {str(n).strip() for n in (names or []) if str(n).strip()}
    limit_count = max(1, min(int(limit), 500))
    processed = 0
    failed = 0
    expired = 0
    touched = 0
    skipped = 0
    details: List[Dict[str, Any]] = []
    outcomes: Dict[str, Dict[str, Any]] = {}
    fail_hard_errors: List[str] = []
    first_fail_hard_exception: Optional[BaseException] = None

    with _EVENT_HANDLERS_LOCK:
        handler_snapshot = dict(EVENT_HANDLERS)

    max_age = int(max_age_seconds) if max_age_seconds is not None else 0
    now_dt = _now_datetime()

    def _claim(payload: Any) -> Any:
        nonlocal processed, skipped, touched, expired
        queue_payload = payload if isinstance(payload, dict) else {"version": 1, "events": []}
        events = queue_payload.get("events")
        if not isinstance(events, list):
            events = []
        selected = 0
        for index, event in enumerate(events):
            if selected >= limit_count:
                break
            if not isinstance(event, dict):
                continue
            event_status = str(event.get("status") or "pending").strip().lower()
            if event_status == "processing":
                started_at = _parse_event_timestamp(event.get("processing_started_at"))
                if (
                    started_at is not None
                    and (now_dt - started_at).total_seconds() >= PROCESSING_EVENT_STALE_SECONDS
                ):
                    event["status"] = "pending"
                    event.pop("processing_started_at", None)
                    event_status = "pending"
                else:
                    continue
            if event_status != "pending":
                continue
            if name_filter and str(event.get("name") or "") not in name_filter:
                skipped += 1
                continue
            selected += 1
            touched += 1
            age_seconds = _event_age_seconds(event, now_dt=now_dt)
            if max_age > 0 and age_seconds is not None and age_seconds > max_age:
                event["status"] = "expired"
                event["processed_at"] = _now()
                event["result"] = {
                    "status": "expired",
                    "reason": "event_exceeded_max_age",
                    "max_age_seconds": max_age,
                    "age_seconds": int(age_seconds),
                }
                expired += 1
                details.append({"id": event.get("id"), "name": event.get("name"), "status": "expired", "result": event["result"]})
                continue
            event_name = str(event.get("name") or "")
            handler = handler_snapshot.get(event_name)
            if not handler:
                event["status"] = "processed"
                event["processed_at"] = _now()
                event["result"] = {"status": "ignored", "reason": "no_handler"}
                processed += 1
                details.append({"id": event.get("id"), "name": event.get("name"), "status": "ignored"})
                continue
            event["status"] = "processing"
            event["processing_started_at"] = _now()
            event_id = str(event.get("id") or f"index:{index}")
            claim_key = f"{index}:{event_id}"
            claimed.append(
                {
                    "claim_key": claim_key,
                    "id": event_id,
                    "index": index,
                    "event": dict(event),
                    "handler": handler,
                }
            )
        return {"version": 1, "events": events}

    _read_modify_write_json(paths["queue"], {"version": 1, "events": []}, _claim)

    for item in claimed:
        event = dict(item["event"])
        handler = item["handler"]
        claim_key = str(item["claim_key"])
        event_name = str(event.get("name") or "")
        event["status"] = "pending"
        processed_at = _now()
        try:
            raw_result = handler(event)
            if raw_result is None:
                result: Dict[str, Any] = {}
            elif isinstance(raw_result, dict):
                result = raw_result
            else:
                result = {
                    "status": "failed",
                    "error": f"handler {event_name} returned non-object response",
                }
            result_status = str(result.get("status") or "ok").lower()
            if result_status in {"failed", "error", "nacked"}:
                failed += 1
                outcome = {
                    "status": "failed",
                    "processed_at": processed_at,
                    "result": result,
                }
                outcomes[claim_key] = outcome
                details.append({"id": event.get("id"), "name": event.get("name"), "status": "failed", "result": result})
                if _is_fail_hard_enabled():
                    err_msg = str(result.get("error") or f"handler {event_name} returned {result_status} status")
                    fail_hard_errors.append(err_msg)
            else:
                processed += 1
                outcome = {
                    "status": "processed",
                    "processed_at": processed_at,
                    "result": result,
                }
                outcomes[claim_key] = outcome
                details.append({"id": event.get("id"), "name": event.get("name"), "status": "processed", "result": result})
        except Exception as e:  # pragma: no cover
            logger.error("Event handler %s failed for event %s: %s", event_name, event.get("id"), e, exc_info=True)
            if first_fail_hard_exception is None:
                first_fail_hard_exception = e
            failed += 1
            outcome = {
                "status": "failed",
                "processed_at": processed_at,
                "result": {"status": "failed", "error": str(e)},
            }
            outcomes[claim_key] = outcome
            details.append({"id": event.get("id"), "name": event.get("name"), "status": "failed", "error": str(e)})
            if _is_fail_hard_enabled():
                fail_hard_errors.append(str(e))

    if claimed:
        def _commit(payload: Any) -> Any:
            queue_payload = payload if isinstance(payload, dict) else {"version": 1, "events": []}
            events = queue_payload.get("events")
            if not isinstance(events, list):
                events = []
            for item in claimed:
                event_id = str(item["id"])
                claim_key = str(item["claim_key"])
                outcome = outcomes.get(claim_key)
                if not outcome:
                    continue
                index = int(item["index"])
                target = None
                if 0 <= index < len(events) and isinstance(events[index], dict):
                    candidate = events[index]
                    if str(candidate.get("id") or f"index:{index}") == event_id:
                        target = candidate
                if target is None:
                    for event in events:
                        if isinstance(event, dict) and str(event.get("id") or "") == event_id:
                            target = event
                            break
                if target is None:
                    continue
                target["status"] = outcome["status"]
                target["processed_at"] = outcome["processed_at"]
                target["result"] = outcome["result"]
                target.pop("processing_started_at", None)
            return {"version": 1, "events": events}

        _read_modify_write_json(paths["queue"], {"version": 1, "events": []}, _commit)

    _append_jsonl(paths["history_jsonl"], {
        "ts": _now(),
        "op": "process",
        "summary": {"processed": processed, "failed": failed, "expired": expired, "touched": touched, "skipped": skipped},
    })
    if fail_hard_errors:
        raise RuntimeError(
            "Event handler failed while fail-hard mode is enabled: "
            + "; ".join(fail_hard_errors)
        ) from first_fail_hard_exception
    return {
        "processed": processed,
        "failed": failed,
        "expired": expired,
        "touched": touched,
        "skipped": skipped,
        "details": details,
    }


def queue_delayed_notification(
    message: str,
    *,
    kind: str = "janitor",
    priority: str = "normal",
    source: str = "quaid_runtime",
) -> Dict[str, Any]:
    """Canonical path for delayed notifications."""
    event = emit_event(
        name="notification.delayed",
        payload={
            "message": str(message or ""),
            "kind": str(kind or "janitor"),
            "priority": str(priority or "normal"),
        },
        source=str(source or "quaid_runtime"),
    )
    processed = process_events(limit=1, names=["notification.delayed"])
    processed_details = processed.get("details") if isinstance(processed, dict) else []
    delivered = any(
        isinstance(detail, dict) and str(detail.get("id") or "") == str(event.get("id") or "")
        for detail in (processed_details or [])
    )
    return {
        "status": "processed" if delivered else "queued_not_processed",
        "event": event,
        "processed": processed,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Quaid event bus")
    subparsers = parser.add_subparsers(dest="command")

    emit_p = subparsers.add_parser("emit", help="Emit an event")
    emit_p.add_argument("--name", required=True, help="Event name")
    emit_p.add_argument("--payload", default="{}", help="JSON payload object")
    emit_p.add_argument("--source", default="cli", help="Source label")
    emit_p.add_argument("--session-id", default=None, help="Optional session ID")
    emit_p.add_argument("--owner-id", default=None, help="Optional owner ID")
    emit_p.add_argument("--priority", default="normal", help="Priority")
    emit_p.add_argument("--dispatch", default="auto", choices=["auto", "immediate", "queued"], help="Dispatch mode")

    list_p = subparsers.add_parser("list", help="List events")
    list_p.add_argument("--status", default="pending", choices=["pending", "processed", "failed", "all"])
    list_p.add_argument("--limit", type=int, default=20)

    process_p = subparsers.add_parser("process", help="Process pending events")
    process_p.add_argument("--limit", type=int, default=20)
    process_p.add_argument("--name", action="append", default=[], help="Event name filter (repeatable)")

    subparsers.add_parser("capabilities", help="List event capabilities")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "emit":
        try:
            payload = json.loads(args.payload) if args.payload else {}
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
        except Exception as e:
            print(json.dumps({"status": "error", "error": f"invalid payload: {e}"}))
            return 1

        event = emit_event(
            name=args.name,
            payload=payload,
            source=args.source,
            session_id=args.session_id,
            owner_id=args.owner_id,
            priority=args.priority,
        )
        dispatch_mode = str(args.dispatch or "auto").strip().lower()
        if dispatch_mode not in {"auto", "immediate", "queued"}:
            dispatch_mode = "auto"
        cap = get_event_capability(args.name) or {}
        delivery_mode = str(cap.get("delivery_mode") or "active").strip().lower()
        should_process = dispatch_mode == "immediate" or (dispatch_mode == "auto" and delivery_mode == "active")
        processed = process_events(limit=1, names=[args.name]) if should_process else None
        print(json.dumps({
            "status": "ok",
            "event": event,
            "delivery_mode": delivery_mode,
            "dispatch": dispatch_mode,
            "processed": processed,
        }))
        return 0

    if args.command == "list":
        print(json.dumps({"status": "ok", "events": list_events(status=args.status, limit=args.limit)}))
        return 0

    if args.command == "process":
        print(json.dumps({"status": "ok", **process_events(limit=args.limit, names=list(args.name or []))}))
        return 0

    if args.command == "capabilities":
        print(json.dumps({"status": "ok", "events": get_event_registry()}))
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
