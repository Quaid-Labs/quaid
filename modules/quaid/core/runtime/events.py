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
from typing import Any, Callable, Dict, List, Optional

from lib.ingest_runtime import run_docs_ingest, run_session_logs_ingest
from lib.runtime_context import get_workspace_dir

Event = Dict[str, Any]
EventHandler = Callable[[Event], Dict[str, Any]]
logger = logging.getLogger(__name__)
MAX_EVENT_QUEUE = 2000
MAX_HISTORY_JSONL_BYTES = 5 * 1024 * 1024
HISTORY_TRIM_TARGET_BYTES = 2 * 1024 * 1024

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


def _append_jsonl(path: Path, payload: Any) -> None:
    _ensure_parent(path)
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


def _make_request_id(kind: str, message: str) -> str:
    return f"{kind}-" + base64.b64encode(message.encode("utf-8")).decode("ascii")[:16]


def _queue_delayed_llm_request(message: str, kind: str = "janitor", priority: str = "normal", source: str = "quaid_events") -> bool:
    message = str(message or "").strip()
    if not message:
        return False
    paths = _event_paths()
    rid = _make_request_id(kind, message)
    queued = False

    def _mutate(payload: Any) -> Any:
        nonlocal queued
        base = payload if isinstance(payload, dict) else {"version": 1, "requests": []}
        requests = base.get("requests")
        if not isinstance(requests, list):
            requests = []
        for item in requests:
            if isinstance(item, dict) and item.get("id") == rid and item.get("status") == "pending":
                queued = False
                return {"version": 1, "requests": requests}
        requests.append({
            "id": rid,
            "created_at": _now(),
            "source": source,
            "kind": kind,
            "priority": priority,
            "status": "pending",
            "message": message,
        })
        queued = True
        return {"version": 1, "requests": requests}

    _read_modify_write_json(paths["delayed_llm_requests"], {"version": 1, "requests": []}, _mutate)
    return queued


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
    def _mutate(payload: Any) -> Any:
        queue_payload = payload if isinstance(payload, dict) else {"version": 1, "events": []}
        events = queue_payload.get("events")
        if not isinstance(events, list):
            events = []
        events.append(event)
        if len(events) > MAX_EVENT_QUEUE:
            events = events[-MAX_EVENT_QUEUE:]
        return {"version": 1, "events": events}

    _read_modify_write_json(paths["queue"], {"version": 1, "events": []}, _mutate)
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
            try:
                result = handler(event)
                result_status = str(result.get("status") or "ok").lower()
                event["processed_at"] = _now()
                event["result"] = result
                if result_status == "failed":
                    event["status"] = "failed"
                    failed += 1
                    details.append({"id": event.get("id"), "name": event.get("name"), "status": "failed", "result": result})
                    if _is_fail_hard_enabled():
                        err_msg = str(result.get("error") or f"handler {event.get('name')} returned failed status")
                        raise RuntimeError(
                            f"Event handler failed while fail-hard mode is enabled: {err_msg}"
                        )
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
