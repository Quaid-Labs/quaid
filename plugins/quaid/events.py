#!/usr/bin/env python3
"""Quaid event bus (queue-backed, adapter-agnostic).

Provides a small extensible event interface for:
- emitting runtime/lifecycle events (new/reset/compaction/timeout/etc.)
- queuing delayed notifications/requests
- processing pending events via registered handlers
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from lib.runtime_context import get_workspace_dir

Event = Dict[str, Any]
EventHandler = Callable[[Event], Dict[str, Any]]

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
        "description": "Queue delayed notification/request for later user-facing handling.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "passive",
        "delivery_notes": "Do not force immediate processing; handled asynchronously via delayed request flow.",
    },
    {
        "name": "memory.force_compaction",
        "description": "Request compaction via delayed request queue.",
        "fireable": True,
        "processable": True,
        "listenable": True,
        "delivery_mode": "passive",
        "delivery_notes": "Compaction requests are queued for later user/heartbeat handling.",
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _event_paths() -> Dict[str, Path]:
    root = get_workspace_dir()
    runtime = root / ".quaid" / "runtime"
    events_dir = runtime / "events"
    notes_dir = runtime / "notes"
    janitor_dir = root / "logs" / "janitor"
    return {
        "queue": events_dir / "queue.json",
        "history_jsonl": events_dir / "history.jsonl",
        "delayed_llm_requests": notes_dir / "delayed-llm-requests.json",
        "delayed_notifications": janitor_dir / "delayed-notifications.json",
    }


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    _ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _append_jsonl(path: Path, payload: Any) -> None:
    _ensure_parent(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _next_event_id(name: str, ts: str) -> str:
    raw = f"{name}:{ts}".encode("utf-8")
    return "evt-" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")[:24]


def _make_request_id(kind: str, message: str) -> str:
    return f"{kind}-" + base64.b64encode(message.encode("utf-8")).decode("ascii")[:16]


def _queue_delayed_llm_request(message: str, kind: str = "janitor", priority: str = "normal", source: str = "quaid_events") -> bool:
    message = str(message or "").strip()
    if not message:
        return False
    paths = _event_paths()
    payload = _read_json(paths["delayed_llm_requests"], {"version": 1, "requests": []})
    requests = payload.get("requests") if isinstance(payload, dict) else []
    if not isinstance(requests, list):
        requests = []
    rid = _make_request_id(kind, message)
    for item in requests:
        if isinstance(item, dict) and item.get("id") == rid and item.get("status") == "pending":
            return False
    requests.append({
        "id": rid,
        "created_at": _now(),
        "source": source,
        "kind": kind,
        "priority": priority,
        "status": "pending",
        "message": message,
    })
    payload = {"version": 1, "requests": requests}
    _write_json(paths["delayed_llm_requests"], payload)
    return True


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


EVENT_HANDLERS: Dict[str, EventHandler] = {
    "notification.delayed": _handle_delayed_notification,
    "memory.force_compaction": _handle_force_compaction,
    "session.new": _handle_session_lifecycle,
    "session.reset": _handle_session_lifecycle,
    "session.compaction": _handle_session_lifecycle,
    "session.timeout": _handle_session_lifecycle,
    "session.agent_start": _handle_session_lifecycle,
    "session.agent_end": _handle_session_lifecycle,
}


def register_event_handler(name: str, handler: EventHandler) -> None:
    EVENT_HANDLERS[str(name)] = handler


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
) -> Dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ValueError("event name is required")
    ts = _now()
    event: Event = {
        "id": _next_event_id(name, ts),
        "name": name,
        "payload": payload or {},
        "source": str(source or "unknown"),
        "priority": str(priority or "normal"),
        "created_at": ts,
        "status": "pending",
    }
    if session_id:
        event["session_id"] = str(session_id)
    if owner_id:
        event["owner_id"] = str(owner_id)

    paths = _event_paths()
    queue_payload = _read_json(paths["queue"], {"version": 1, "events": []})
    events = queue_payload.get("events") if isinstance(queue_payload, dict) else []
    if not isinstance(events, list):
        events = []
    events.append(event)
    _write_json(paths["queue"], {"version": 1, "events": events})
    _append_jsonl(paths["history_jsonl"], {"ts": ts, "op": "emit", "event": event})
    return event


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
    queue_payload = _read_json(paths["queue"], {"version": 1, "events": []})
    events = queue_payload.get("events") if isinstance(queue_payload, dict) else []
    if not isinstance(events, list):
        events = []
    name_filter = {str(n).strip() for n in (names or []) if str(n).strip()}
    processed = 0
    failed = 0
    touched = 0
    details: List[Dict[str, Any]] = []

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
        try:
            result = handler(event)
            result_status = str(result.get("status") or "ok").lower()
            event["processed_at"] = _now()
            event["result"] = result
            if result_status == "failed":
                event["status"] = "failed"
                failed += 1
                details.append({"id": event.get("id"), "name": event.get("name"), "status": "failed", "result": result})
            else:
                event["status"] = "processed"
                processed += 1
                details.append({"id": event.get("id"), "name": event.get("name"), "status": event["status"], "result": result})
        except Exception as e:  # pragma: no cover
            event["status"] = "failed"
            event["processed_at"] = _now()
            event["result"] = {"status": "failed", "error": str(e)}
            failed += 1
            details.append({"id": event.get("id"), "name": event.get("name"), "status": "failed", "error": str(e)})

    _write_json(paths["queue"], {"version": 1, "events": events})
    _append_jsonl(paths["history_jsonl"], {
        "ts": _now(),
        "op": "process",
        "summary": {"processed": processed, "failed": failed, "touched": touched},
    })
    return {"processed": processed, "failed": failed, "touched": touched, "details": details}
