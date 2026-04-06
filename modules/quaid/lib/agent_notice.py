from __future__ import annotations

import base64
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lib.adapter import get_adapter

logger = logging.getLogger(__name__)

_STATE_FILE = "agent-notice-state.json"
_DEFERRED_FILE = "delayed-llm-requests.json"
_STATE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DEFERRED_NOTICES = 500


def _normalize_severity(value: str) -> str:
    token = str(value or "").strip().lower()
    if token in {"info", "warning", "error"}:
        return token
    return "warning"


def _format_notice(message: str, *, severity: str, source: str) -> str:
    label = _normalize_severity(severity)
    prefix = f"[Quaid {label}]"
    source_token = str(source or "").strip()
    if source_token:
        prefix = f"{prefix} [{source_token}]"
    return f"{prefix} {message.strip()}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed reading JSON %s: %s", path, exc)
        return dict(default)
    if not isinstance(payload, dict):
        return dict(default)
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_parent(path)
    tmp_path = path.with_name(f"{path.name}.tmp-{int(time.time() * 1000)}")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    try:
        path.chmod(0o600)
    except Exception:
        pass


def _state_path() -> Path:
    return get_adapter().data_dir() / _STATE_FILE


def _deferred_path() -> Path:
    return get_adapter().instance_root() / ".runtime" / "notes" / _DEFERRED_FILE


def _deferred_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _load_state(path: Path) -> dict[str, float]:
    raw = _read_json(path, {})
    now = time.time()
    state: dict[str, float] = {}
    for key, value in raw.items():
        try:
            ts = float(value)
        except (TypeError, ValueError):
            continue
        if now - ts <= _STATE_RETENTION_SECONDS:
            state[str(key)] = ts
    return state


def _store_state(path: Path, state: dict[str, float]) -> None:
    _write_json(path, state)


def _request_id(kind: str, message: str) -> str:
    token = base64.b64encode(message.encode("utf-8")).decode("ascii")[:16]
    return f"{kind}-{token}"


def _priority_rank(priority: str) -> int:
    token = str(priority or "").strip().lower()
    if token == "high":
        return 2
    if token == "low":
        return 0
    return 1


def _sort_deferred_notices(notices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notices.sort(
        key=lambda item: (
            -_priority_rank(str(item.get("priority") or "normal")),
            str(item.get("created_at") or ""),
        )
    )
    return notices


def _trim_deferred_notices(notices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(notices) <= _MAX_DEFERRED_NOTICES:
        return notices

    delivered = [item for item in notices if isinstance(item, dict) and item.get("status") != "pending"]
    pending = [item for item in notices if isinstance(item, dict) and item.get("status") == "pending"]
    overflow = len(notices) - _MAX_DEFERRED_NOTICES

    if delivered:
        delivered = delivered[overflow:]
    else:
        pending = pending[overflow:]

    return delivered + pending


def notify_agent(
    message: str,
    *,
    severity: str = "warning",
    source: str = "",
    dedupe_key: Optional[str] = None,
    ttl_seconds: int = 3600,
    channel_override: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    """Send an operator-facing notice to the active agent."""
    text = str(message or "").strip()
    if not text:
        return False

    adapter = get_adapter()
    dedupe_token = str(dedupe_key or "").strip()
    state_path: Optional[Path] = None
    state: dict[str, float] = {}
    now = time.time()

    if dedupe_token and ttl_seconds > 0:
        try:
            state_path = _state_path()
            state = _load_state(state_path)
            last_sent = float(state.get(dedupe_token, 0.0) or 0.0)
            if last_sent and (now - last_sent) < ttl_seconds:
                return True
        except Exception as exc:
            logger.warning("Failed evaluating agent notice dedupe key=%s: %s", dedupe_token, exc)

    formatted = _format_notice(text, severity=severity, source=source)

    try:
        ok = bool(
            adapter.notify(
                formatted,
                channel_override=channel_override,
                dry_run=dry_run,
                force=force,
            )
        )
    except Exception as exc:
        logger.warning("Failed delivering agent notice: %s", exc)
        return False

    if ok and dedupe_token and ttl_seconds > 0 and not dry_run and state_path is not None:
        try:
            state[dedupe_token] = now
            _store_state(state_path, state)
        except Exception as exc:
            logger.warning("Failed recording agent notice dedupe key=%s: %s", dedupe_token, exc)

    return ok


def queue_deferred_notice(
    message: str,
    *,
    kind: str = "janitor",
    priority: str = "normal",
    source: str = "quaid",
    dedupe_key: Optional[str] = None,
) -> bool:
    text = str(message or "").strip()
    if not text:
        return False

    notice_kind = str(kind or "janitor").strip() or "janitor"
    notice_priority = str(priority or "normal").strip() or "normal"
    notice_source = str(source or "quaid").strip() or "quaid"
    request_id = _request_id(notice_kind, text)
    dedupe_token = str(dedupe_key or request_id).strip() or request_id
    path = _deferred_path()

    with _file_lock(_deferred_lock_path(path)):
        payload = _read_json(path, {"version": 1, "requests": []})
        requests = payload.get("requests")
        if not isinstance(requests, list):
            requests = []

        for item in requests:
            if not isinstance(item, dict):
                continue
            if item.get("status") != "pending":
                continue
            if str(item.get("dedupe_key") or item.get("id") or "").strip() == dedupe_token:
                return False

        requests.append(
            {
                "id": request_id,
                "dedupe_key": dedupe_token,
                "created_at": _now_iso(),
                "source": notice_source,
                "kind": notice_kind,
                "priority": notice_priority,
                "status": "pending",
                "message": text,
            }
        )
        _write_json(path, {"version": 1, "requests": _trim_deferred_notices(requests)})
        return True


def list_deferred_notices(
    *,
    status: str = "pending",
    limit: int = 50,
) -> list[dict[str, Any]]:
    path = _deferred_path()
    payload = _read_json(path, {"version": 1, "requests": []})
    requests = payload.get("requests")
    if not isinstance(requests, list):
        return []

    normalized_status = str(status or "pending").strip().lower()
    if normalized_status not in {"pending", "delivered", "all"}:
        normalized_status = "pending"

    notices: list[dict[str, Any]] = []
    for item in requests:
        if not isinstance(item, dict):
            continue
        item_status = str(item.get("status") or "pending").strip().lower()
        if normalized_status != "all" and item_status != normalized_status:
            continue
        notices.append(dict(item))

    return _sort_deferred_notices(notices)[: max(1, min(int(limit), 500))]


def drain_deferred_notices(*, limit: int = 50) -> list[dict[str, Any]]:
    path = _deferred_path()
    drained: list[dict[str, Any]] = []

    with _file_lock(_deferred_lock_path(path)):
        payload = _read_json(path, {"version": 1, "requests": []})
        requests = payload.get("requests")
        if not isinstance(requests, list):
            return []

        pending = [
            item for item in requests
            if isinstance(item, dict) and str(item.get("status") or "pending").strip().lower() == "pending"
        ]
        _sort_deferred_notices(pending)
        target_ids = {
            str(item.get("id") or "")
            for item in pending[: max(1, min(int(limit), 500))]
            if str(item.get("id") or "")
        }
        if not target_ids:
            return []

        drained_at = _now_iso()
        updated: list[dict[str, Any]] = []
        for item in requests:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id in target_ids and str(item.get("status") or "pending").strip().lower() == "pending":
                updated_item = dict(item)
                updated_item["status"] = "delivered"
                updated_item["delivered_at"] = drained_at
                updated.append(updated_item)
                drained.append(updated_item)
            else:
                updated.append(item)

        updated.sort(key=lambda item: str(item.get("created_at") or ""))
        _write_json(path, {"version": 1, "requests": _trim_deferred_notices(updated)})

    return _sort_deferred_notices(drained)


def get_deferred_notice_status(
    *,
    limit: int = 500,
    include_items: bool = False,
) -> dict[str, Any]:
    notices = list_deferred_notices(status="pending", limit=limit)
    kinds: dict[str, int] = {}
    priorities: dict[str, int] = {}
    for item in notices:
        kind = str(item.get("kind") or "unknown").strip() or "unknown"
        priority = str(item.get("priority") or "normal").strip() or "normal"
        kinds[kind] = kinds.get(kind, 0) + 1
        priorities[priority] = priorities.get(priority, 0) + 1

    payload = {
        "pending_count": len(notices),
        "kinds": kinds,
        "priorities": priorities,
    }
    if include_items:
        payload["items"] = notices
    return payload


def format_deferred_notice_hint() -> str:
    status = get_deferred_notice_status()
    pending_count = int(status.get("pending_count") or 0)
    if pending_count <= 0:
        return ""

    kinds = status.get("kinds") if isinstance(status.get("kinds"), dict) else {}
    top_kinds = sorted(kinds.items(), key=lambda item: (-int(item[1]), str(item[0])))
    kind_summary = ", ".join(f"{name}={count}" for name, count in top_kinds[:3]) or "unknown"
    notice_word = "notice" if pending_count == 1 else "notices"
    return (
        "<quaid_system_message>\n"
        f"Quaid has {pending_count} deferred maintenance {notice_word} waiting ({kind_summary}). "
        "These are buffered system notices. Do not retrieve or relay them unless you are confident a human user "
        "is active and can see the reply. If appropriate, inspect with `quaid notify --deferred-status` or fetch "
        "with `quaid notify --deferred-drain`, then summarize the results.\n"
        "</quaid_system_message>"
    )
