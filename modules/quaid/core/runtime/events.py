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
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from core.ingest_runtime import run_docs_ingest, run_session_logs_ingest
from core.runtime.paths import get_runtime_root
from lib.runtime_context import queue_deferred_notice
from lib.runtime_context import get_workspace_dir

Event = Dict[str, Any]
EventHandler = Callable[[Event], Dict[str, Any]]
logger = logging.getLogger(__name__)
MAX_EVENT_QUEUE = 2000
MAX_HISTORY_JSONL_BYTES = 5 * 1024 * 1024
HISTORY_TRIM_TARGET_BYTES = 2 * 1024 * 1024
EVENT_ENVELOPE_SCHEMA_VERSION = 1
EVENT_CLASSES = {"domain", "request"}

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
        "name": "session.ingest_log",
        "description": "Index lifecycle session transcript into datastore-owned session log RAG.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
    {
        "name": "janitor.run_completed",
        "description": "Process janitor completion payload and queue user-facing notifications.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "active",
    },
]

_EVENT_NAME_ALIASES: Dict[str, str] = {
    # Adapter hook names map to canonical Quaid runtime events.
    "before_agent_start": "session.agent_start",
    "agent_end": "session.agent_end",
    "session_end": "session.reset",
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
    return datetime.now(timezone.utc).isoformat()


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
    except Exception:
        return True


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
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as tmp:
        tmp.write(json.dumps(payload, indent=2))
        tmp.flush()
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)
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
                data = path.read_text(encoding="utf-8")
                keep = data[-HISTORY_TRIM_TARGET_BYTES:]
                if "\n" in keep:
                    keep = keep.split("\n", 1)[1]
                path.write_text(keep, encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed trimming history file %s: %s", path, exc)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


@contextlib.contextmanager
def _file_lock(path: Path):
    _ensure_parent(path)
    lock_handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            import fcntl  # type: ignore
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
        except Exception:
            # Best-effort on non-POSIX environments.
            pass
        yield
    finally:
        try:
            import fcntl  # type: ignore
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_handle.close()


def _read_modify_write_json(path: Path, default: Any, mutator: Callable[[Any], Any]) -> Any:
    with _file_lock(_lock_path(path)):
        current = _read_json(path, default)
        updated = mutator(current)
        _write_json(path, updated)
        return updated


def _next_event_id(name: str, ts: str) -> str:
    raw = f"{name}:{ts}".encode("utf-8")
    return "evt-" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:24]


def _next_correlation_id(name: str, ts: str) -> str:
    raw = f"correlation:{name}:{ts}".encode("utf-8")
    return "corr-" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:24]


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
    }
    if instance_id:
        event["instance_id"] = str(instance_id)
    if project_id:
        event["project_id"] = str(project_id)
    if session_id:
        event["session_id"] = str(session_id)
    if owner_id:
        event["owner_id"] = str(owner_id)
    if correlation_id:
        event["correlation_id"] = str(correlation_id)
    elif resolved_class == "request":
        event["correlation_id"] = _next_correlation_id(name, ts)
    if idempotency_key:
        event["idempotency_key"] = str(idempotency_key)
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

    def _mutate(payload: Any) -> Any:
        nonlocal stored_event, deduped
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
                if existing_key == idempotency_key and existing_type == event_type:
                    stored_event = dict(existing)
                    stored_event["duplicate"] = True
                    stored_event["duplicate_of"] = existing.get("id")
                    deduped = True
                    return {"version": 1, "events": events}
        events.append(event)
        if len(events) > MAX_EVENT_QUEUE:
            events = events[-MAX_EVENT_QUEUE:]
        return {"version": 1, "events": events}

    _read_modify_write_json(paths["queue"], {"version": 1, "events": []}, _mutate)
    _append_jsonl(
        paths["history_jsonl"],
        {"ts": ts, "op": "dedupe" if deduped else "emit", "event": stored_event},
    )
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


def _handle_session_lifecycle(event: Event) -> Dict[str, Any]:
    return {"status": "acknowledged", "event": event.get("name")}


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
        result = run_docs_ingest(
            Path(transcript_path),
            label,
            str(session_id) if session_id else None,
        )
        if isinstance(result, dict) and str(result.get("status") or "").lower() == "error":
            return {"status": "failed", "result": result}
        return {"status": "processed", "result": result}
    except Exception as e:  # pragma: no cover
        return {"status": "failed", "error": str(e)}


def _handle_session_ingest_log(event: Event) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    session_id = str(payload.get("session_id") or "").strip()
    owner_id = str(payload.get("owner_id") or "default").strip() or "default"
    label = str(payload.get("label") or "unknown").strip() or "unknown"
    session_file = payload.get("session_file")
    transcript_path = payload.get("transcript_path")
    source_channel = str(payload.get("source_channel") or "").strip() or None
    conversation_id = str(payload.get("conversation_id") or "").strip() or None
    participant_ids_raw = payload.get("participant_ids")
    participant_aliases_raw = payload.get("participant_aliases")
    participant_ids = participant_ids_raw if isinstance(participant_ids_raw, list) else []
    participant_aliases = participant_aliases_raw if isinstance(participant_aliases_raw, dict) else {}
    message_count = int(payload.get("message_count") or 0)
    topic_hint = str(payload.get("topic_hint") or "").strip()

    if not session_id:
        return {"status": "failed", "error": "payload.session_id is required"}

    try:
        result = run_session_logs_ingest(
            session_id=session_id,
            owner_id=owner_id,
            label=label,
            session_file=str(session_file) if session_file else None,
            transcript_path=str(transcript_path) if transcript_path else None,
            source_channel=source_channel,
            conversation_id=conversation_id,
            participant_ids=[str(p).strip() for p in participant_ids if str(p).strip()],
            participant_aliases={str(k): str(v) for k, v in participant_aliases.items() if str(k).strip()},
            message_count=message_count,
            topic_hint=topic_hint,
        )
        if isinstance(result, dict) and str(result.get("status") or "").lower() in {"failed", "error"}:
            return {"status": "failed", "result": result}
        return {"status": "processed", "result": result}
    except Exception as e:  # pragma: no cover
        return {"status": "failed", "error": str(e)}


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
        return {"status": "failed", "error": str(e)}


EVENT_HANDLERS: Dict[str, EventHandler] = {
    "notification.delayed": _handle_delayed_notification,
    "memory.force_compaction": _handle_force_compaction,
    "docs.ingest_transcript": _handle_docs_ingest_transcript,
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


def register_event_handler(name: str, handler: EventHandler, *, force: bool = False) -> None:
    event_name = str(name or "").strip()
    if not event_name:
        raise ValueError("event handler name is required")
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


def get_event_broker() -> EventBroker:
    return EventBroker()


def emit_broker_event(event_type: str, payload: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Event:
    return get_event_broker().emit(event_type, payload, **kwargs)


def dispatch_broker_events(limit: int = 20, names: Optional[List[str]] = None) -> Dict[str, Any]:
    return get_event_broker().dispatch(limit=limit, names=names)


def list_events(status: str = "pending", limit: int = 50) -> List[Event]:
    paths = _event_paths()
    queue_payload = _read_json(paths["queue"], {"version": 1, "events": []})
    events = queue_payload.get("events") if isinstance(queue_payload, dict) else []
    if not isinstance(events, list):
        return []
    status = str(status or "pending").strip().lower()
    if status not in {"pending", "processed", "failed", "all"}:
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


def process_events(limit: int = 20, names: Optional[List[str]] = None) -> Dict[str, Any]:
    paths = _event_paths()
    events: List[Dict[str, Any]] = []
    name_filter = {str(n).strip() for n in (names or []) if str(n).strip()}
    processed = 0
    failed = 0
    touched = 0
    details: List[Dict[str, Any]] = []

    def _mutate(payload: Any) -> Any:
        nonlocal events, processed, failed, touched
        queue_payload = payload if isinstance(payload, dict) else {"version": 1, "events": []}
        events = queue_payload.get("events")
        if not isinstance(events, list):
            events = []
        for event in events:
            if processed >= max(1, min(int(limit), 500)):
                break
            if not isinstance(event, dict):
                continue
            if event.get("status") != "pending":
                continue
            if name_filter and str(event.get("name") or "") not in name_filter:
                continue
            touched += 1
            handler = EVENT_HANDLERS.get(str(event.get("name") or ""))
            if not handler:
                event["status"] = "processed"
                event["processed_at"] = _now()
                event["result"] = {"status": "ignored", "reason": "no_handler"}
                processed += 1
                details.append({"id": event.get("id"), "name": event.get("name"), "status": "ignored"})
                continue
            handler_reported_failed = False
            try:
                result = handler(event)
                result_status = str(result.get("status") or "ok").lower()
                event["processed_at"] = _now()
                event["result"] = result
                if result_status == "failed":
                    handler_reported_failed = True
                    event["status"] = "failed"
                    failed += 1
                    details.append({"id": event.get("id"), "name": event.get("name"), "status": "failed", "result": result})
                    if _is_fail_hard_enabled():
                        err_msg = str(result.get("error") or f"handler {event.get('name')} returned failed status")
                        raise RuntimeError(f"Event handler failed while fail-hard mode is enabled: {err_msg}")
                else:
                    event["status"] = "processed"
                    processed += 1
                    details.append({"id": event.get("id"), "name": event.get("name"), "status": event["status"], "result": result})
                continue
            except Exception as e:  # pragma: no cover
                if handler_reported_failed and event.get("status") == "failed":
                    # Handler explicitly reported failed; avoid double-counting in fail-hard raise path.
                    raise
                event["status"] = "failed"
                event["processed_at"] = _now()
                event["result"] = {"status": "failed", "error": str(e)}
                failed += 1
                details.append({"id": event.get("id"), "name": event.get("name"), "status": "failed", "error": str(e)})
                if _is_fail_hard_enabled():
                    raise RuntimeError(
                        "Event handler failed while fail-hard mode is enabled"
                    ) from e
        return {"version": 1, "events": events}

    _read_modify_write_json(paths["queue"], {"version": 1, "events": []}, _mutate)
    _append_jsonl(paths["history_jsonl"], {
        "ts": _now(),
        "op": "process",
        "summary": {"processed": processed, "failed": failed, "touched": touched},
    })
    return {"processed": processed, "failed": failed, "touched": touched, "details": details}


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
    return {"event": event, "processed": processed}


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
