"""Shared delayed-request queue helper.

Datastore and core callers can enqueue passive user-facing notifications
without importing runtime event modules.
"""

from __future__ import annotations

import base64
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from lib.runtime_context import get_workspace_dir


def _notes_path() -> Path:
    return get_workspace_dir() / ".quaid" / "runtime" / "notes" / "delayed-llm-requests.json"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def _file_lock(path: Path):
    _ensure_parent(path)
    handle = open(path, "a+", encoding="utf-8")
    try:
        try:
            import fcntl  # type: ignore
            fcntl.flock(handle, fcntl.LOCK_EX)
        except Exception:
            pass
        yield
    finally:
        try:
            import fcntl  # type: ignore
            fcntl.flock(handle, fcntl.LOCK_UN)
        except Exception:
            pass
        handle.close()


def _read_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    if not isinstance(payload, dict):
        return dict(default)
    return payload


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    _ensure_parent(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _request_id(kind: str, message: str) -> str:
    token = base64.b64encode(message.encode("utf-8")).decode("ascii")[:16]
    return f"{kind}-{token}"


def queue_delayed_request(
    message: str,
    *,
    kind: str = "janitor",
    priority: str = "normal",
    source: str = "quaid",
) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    request_kind = str(kind or "janitor").strip() or "janitor"
    request_priority = str(priority or "normal").strip() or "normal"
    request_source = str(source or "quaid").strip() or "quaid"
    request_id = _request_id(request_kind, text)
    path = _notes_path()
    lock_path = path.with_suffix(path.suffix + ".lock")

    with _file_lock(lock_path):
        payload = _read_json(path, {"version": 1, "requests": []})
        requests = payload.get("requests")
        if not isinstance(requests, list):
            requests = []
        for item in requests:
            if isinstance(item, dict) and item.get("id") == request_id and item.get("status") == "pending":
                return False
        requests.append(
            {
                "id": request_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": request_source,
                "kind": request_kind,
                "priority": request_priority,
                "status": "pending",
                "message": text,
            }
        )
        _write_json(path, {"version": 1, "requests": requests})
        return True
