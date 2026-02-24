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
import json
import os
import sys
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
    {
        "name": "docs.ingest_transcript",
        "description": "Run docs ingestion pipeline from a transcript file path.",
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _event_paths() -> Dict[str, Path]:
    root = get_workspace_dir()
    runtime = root / ".quaid" / "runtime"
    events_dir = runtime / "events"
    notes_dir = runtime / "notes"
    return {
        "queue": events_dir / "queue.json",
        "history_jsonl": events_dir / "history.jsonl",
        "delayed_llm_requests": notes_dir / "delayed-llm-requests.json",
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


def _handle_docs_ingest_transcript(event: Event) -> Dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    transcript_path = str(payload.get("transcript_path") or "").strip()
    label = str(payload.get("label") or "Unknown").strip() or "Unknown"
    session_id = payload.get("session_id")
    if not transcript_path:
        return {"status": "failed", "error": "payload.transcript_path is required"}
    try:
        docs_ingest_mod = sys.modules.get("docs_ingest")
        if docs_ingest_mod is not None and hasattr(docs_ingest_mod, "_run"):
            _docs_ingest_run = docs_ingest_mod._run
        else:
            from ingest.docs_ingest import _run as _docs_ingest_run
        result = _docs_ingest_run(
            Path(transcript_path),
            label,
            str(session_id) if session_id else None,
        )
        if isinstance(result, dict) and str(result.get("status") or "").lower() == "error":
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
    "janitor.run_completed": _handle_janitor_run_completed,
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
