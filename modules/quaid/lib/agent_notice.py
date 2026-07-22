from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lib.adapter import get_adapter
from lib.fail_policy import is_fail_hard_enabled

logger = logging.getLogger(__name__)

_STATE_FILE = "agent-notice-state.json"
_DEFERRED_FILE = "delayed-llm-requests.json"
_STATE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DEFERRED_NOTICES = 500
_NOTICE_SOURCE_RE = re.compile(r"^\s*\[Quaid(?: [^\]]+)?\]\s*\[([^\]]+)\]", flags=re.IGNORECASE)
_PROVIDER_RELAY_SOURCES = {"provider", "llm_config"}
_PROVIDER_RELAY_SENTENCE = "Quaid memory recall is currently degraded because its model provider failed."


def _trace_m15(event: str, **fields: Any) -> None:
    try:
        from lib.m15_trace import trace_m15

        trace_m15(event, **fields)
    except Exception:
        pass


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
    text = message.strip()
    if label == "error" and source_token.lower() in {"provider", "llm_config"}:
        text = format_provider_error_for_relay(text)
    return f"{prefix} {text}"


def format_provider_error_for_relay(message: str) -> str:
    text = re.sub(r"\s+", " ", str(message or "").strip())
    if not text:
        text = "provider error"
    detail = text
    max_detail = 160
    if len(detail) > max_detail:
        detail = f"{detail[: max_detail - 1].rstrip()}…"
    return (
        f"Tell the user: {_PROVIDER_RELAY_SENTENCE} "
        "I may answer without fresh Quaid memory until it recovers. "
        f"Detail: {detail}"
    )


def _contains_provider_relay_notice(notices: list[str]) -> bool:
    for notice in notices:
        if pending_notice_source(notice) in _PROVIDER_RELAY_SOURCES:
            return True
    return False


def format_pending_notice_relay(messages: list[str]) -> str:
    notices = [str(message or "").strip() for message in messages if str(message or "").strip()]
    if not notices:
        return ""
    body = "\n".join(f"• {message}" for message in notices)
    if _contains_provider_relay_notice(notices):
        return (
            "RESPONSE CONTRACT: Before answering anything else, your first sentence to the user must say: "
            f"\"{_PROVIDER_RELAY_SENTENCE}\" "
            "This is a current Quaid status for this turn, not a historical reminder; repeat that exact "
            "first sentence even if you already warned the user in an earlier response. "
            "Do not replace it with 'same as before' and do not omit it. "
            "Then continue with the user's request using any non-Quaid knowledge you have. "
            "Do not skip or paraphrase away this status sentence.\n\n"
            f"<quaid_system_message>\n{body}\n</quaid_system_message>"
        )
    return (
        "MANDATORY: Quaid has active notices for the human user. "
        "Begin your next response by relaying each notice below in plain language, "
        "then answer the user's current message.\n\n"
        f"<quaid_system_message>\n{body}\n</quaid_system_message>"
    )


def pending_notice_source(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    match = _NOTICE_SOURCE_RE.match(text)
    if not match:
        return ""
    return str(match.group(1) or "").strip().lower()


def should_persist_pending_notice(source: str) -> bool:
    token = str(source or "").strip().lower()
    return token in {"provider", "llm_config"}


def dedupe_pending_notice_messages(messages: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in list(messages or []):
        message = str(raw or "").strip()
        if not message or message in seen:
            continue
        seen.add(message)
        deduped.append(message)
    return deduped


def _bypass_active_error_dedupe(*, severity: str, source: str) -> bool:
    """Surface active model/provider failures every turn they actually fire."""
    return (
        _normalize_severity(severity) == "error"
        and str(source or "").strip().lower() in {"provider", "llm_config"}
    )


def _uses_turn_scoped_provider_notices(adapter: Any, *, severity: str, source: str) -> bool:
    """Avoid out-of-band provider messages on adapters that inject them per turn."""
    if _normalize_severity(severity) != "error":
        return False
    if str(source or "").strip().lower() not in {"provider", "llm_config"}:
        return False
    try:
        return adapter.get_capability("turn_scoped_provider_notices", False) is True
    except Exception as exc:
        logger.warning("_uses_turn_scoped_provider_notices failed: %s", exc)
        if is_fail_hard_enabled():
            raise
        return False


def _uses_turn_scoped_deferred_notices(adapter: Any) -> bool:
    """Adapters with prompt-visible relays should not finalize notices via side-channel sends."""
    try:
        return adapter.get_capability("turn_scoped_deferred_notices", False) is True
    except Exception as exc:
        logger.warning("_uses_turn_scoped_deferred_notices failed: %s", exc)
        if is_fail_hard_enabled():
            raise
        return False


def _now_iso() -> str:
    return _now_datetime().isoformat()


def _now_datetime() -> datetime:
    raw = os.environ.get("QUAID_NOW", "").strip()
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Invalid QUAID_NOW=%r", raw)
            raise ValueError(f"Invalid QUAID_NOW={raw!r}") from None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _now_epoch() -> float:
    return _now_datetime().timestamp()


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
        except Exception as exc:
            logger.warning("Deferred-notices file lock acquisition failed for %s: %s", path, exc)
        yield
    finally:
        try:
            import fcntl  # type: ignore

            fcntl.flock(handle, fcntl.LOCK_UN)
        except Exception as exc:
            logger.warning("Deferred-notices file lock release failed for %s: %s", path, exc)
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
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    try:
        path.chmod(0o600)
    except Exception as exc:
        logger.warning("Failed setting notice state file permissions for %s: %s", path, exc)
        pass


def _write_text_atomic(path: Path, text: str) -> None:
    _ensure_parent(path)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _state_path() -> Path:
    return get_adapter().data_dir() / _STATE_FILE


def _deferred_path() -> Path:
    return get_adapter().instance_root() / ".runtime" / "notes" / _DEFERRED_FILE


def _deferred_lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _load_state(path: Path) -> dict[str, float]:
    raw = _read_json(path, {})
    now = _now_epoch()
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
    """Send an operator-facing notice to the active agent.

    Returns True when the notice is either:
    - delivered immediately via adapter.notify(), or
    - queued as a deferred fallback after immediate delivery fails.
    """
    text = str(message or "").strip()
    if not text:
        return False

    adapter = get_adapter()
    dedupe_token = str(dedupe_key or "").strip()
    state_path: Optional[Path] = None
    state: dict[str, float] = {}
    now = _now_epoch()

    bypass_dedupe = _bypass_active_error_dedupe(severity=severity, source=source)

    if dedupe_token and ttl_seconds > 0 and not bypass_dedupe:
        try:
            state_path = _state_path()
            state = _load_state(state_path)
            last_sent = float(state.get(dedupe_token, 0.0) or 0.0)
            if last_sent and (now - last_sent) < ttl_seconds:
                return True
        except Exception as exc:
            logger.warning("Failed evaluating agent notice dedupe key=%s: %s", dedupe_token, exc)

    formatted = _format_notice(text, severity=severity, source=source)
    _trace_m15(
        "agent_notice.notify.start",
        severity=severity,
        source=source,
        dedupe_key=dedupe_token,
        ttl_seconds=ttl_seconds,
        force=force,
        dry_run=dry_run,
        message=formatted,
    )

    def _fallback_to_deferred() -> bool:
        if dry_run:
            _trace_m15("agent_notice.notify.deferred_fallback_skipped_dry_run")
            return False
        severity_token = _normalize_severity(severity)
        priority = "normal"
        if severity_token == "error":
            priority = "high"
        elif severity_token == "info":
            priority = "low"
        try:
            queued = queue_deferred_notice(
                formatted,
                kind="agent_notice",
                priority=priority,
                source=str(source or "agent_notice").strip() or "agent_notice",
                dedupe_key=f"notify-fallback:{dedupe_token}" if dedupe_token else None,
            )
            _trace_m15(
                "agent_notice.notify.deferred_fallback",
                queued=queued,
                priority=priority,
                dedupe_key=f"notify-fallback:{dedupe_token}" if dedupe_token else None,
            )
            return queued
        except Exception as deferred_exc:
            logger.warning("Failed queueing deferred fallback for agent notice: %s", deferred_exc)
            _trace_m15("agent_notice.notify.deferred_fallback_error", error=str(deferred_exc))
            if is_fail_hard_enabled():
                raise
            return False

    if _uses_turn_scoped_provider_notices(adapter, severity=severity, source=source):
        _trace_m15(
            "agent_notice.notify.deferred_turn_scoped_provider",
            adapter=str(adapter.adapter_id() if hasattr(adapter, "adapter_id") else ""),
            source=source,
            severity=severity,
            dedupe_key=dedupe_token,
        )
        return _fallback_to_deferred()

    try:
        ok = bool(
            adapter.notify(
                formatted,
                channel_override=channel_override,
                dry_run=dry_run,
                force=force,
            )
        )
        _trace_m15(
            "agent_notice.notify.adapter_result",
            ok=ok,
            adapter=str(adapter.adapter_id() if hasattr(adapter, "adapter_id") else ""),
        )
        if not ok:
            logger.warning("Agent notice delivery returned False; queueing deferred fallback.")
            if is_fail_hard_enabled():
                raise RuntimeError("Agent notice delivery returned False")
            return _fallback_to_deferred()
    except Exception as exc:
        logger.warning("Failed delivering agent notice: %s", exc)
        _trace_m15("agent_notice.notify.adapter_error", error=str(exc))
        if is_fail_hard_enabled():
            raise
        return _fallback_to_deferred()

    if ok and dedupe_token and ttl_seconds > 0 and not dry_run and state_path is not None and not bypass_dedupe:
        try:
            state[dedupe_token] = now
            _store_state(state_path, state)
        except Exception as exc:
            logger.warning("Failed recording agent notice dedupe key=%s: %s", dedupe_token, exc)

    return ok


def clear_pending_notices_by_source(*, sources: set[str] | list[str] | tuple[str, ...]) -> int:
    target_sources = {
        str(source or "").strip().lower()
        for source in (sources or [])
        if str(source or "").strip()
    }
    if not target_sources:
        return 0

    adapter = get_adapter()
    candidate_paths: list[Path] = []

    pending_getter = getattr(adapter, "_pending_notifications_path", None)
    if callable(pending_getter):
        try:
            candidate_paths.append(Path(pending_getter()))
        except Exception as exc:
            logger.warning("Failed resolving pending notification path: %s", exc)

    data_dir_getter = getattr(adapter, "data_dir", None)
    if callable(data_dir_getter):
        try:
            data_dir = Path(data_dir_getter())
            candidate_paths.extend(sorted(data_dir.glob("*-pending-notifications.jsonl")))
        except Exception as exc:
            logger.warning("Failed enumerating pending notification directory: %s", exc)

    seen_paths: set[Path] = set()
    total_removed = 0
    for pending_path in candidate_paths:
        pending_path = Path(pending_path)
        if pending_path in seen_paths:
            continue
        seen_paths.add(pending_path)
        if not pending_path.is_file():
            continue

        removed = 0
        kept_entries: list[dict[str, Any]] = []
        malformed_lines: list[str] = []
        try:
            for raw_line in pending_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    malformed_lines.append(line)
                    continue
                if not isinstance(entry, dict):
                    malformed_lines.append(line)
                    continue
                message = str(entry.get("message") or "").strip()
                entry_source = str(entry.get("source") or "").strip().lower() or pending_notice_source(message)
                if entry_source in target_sources:
                    removed += 1
                    continue
                kept_entries.append(entry)
        except Exception as exc:
            logger.warning("Failed reading pending notifications for cleanup: %s", exc)
            continue

        if removed <= 0:
            continue

        if kept_entries or malformed_lines:
            rows = [json.dumps(entry, sort_keys=True) for entry in kept_entries] + malformed_lines
            _write_text_atomic(pending_path, "\n".join(rows) + "\n")
        else:
            pending_path.unlink(missing_ok=True)
        total_removed += removed
        _trace_m15(
            "agent_notice.pending.clear_by_source",
            path=str(pending_path),
            removed=removed,
            kept=len(kept_entries),
            malformed=len(malformed_lines),
            sources=sorted(target_sources),
        )
    return total_removed


def clear_deferred_notices_by_source(
    *,
    sources: set[str] | list[str] | tuple[str, ...],
    statuses: set[str] | list[str] | tuple[str, ...] = ("pending", "delivered"),
) -> int:
    target_sources = {
        str(source or "").strip().lower()
        for source in (sources or [])
        if str(source or "").strip()
    }
    target_statuses = {
        str(status or "").strip().lower()
        for status in (statuses or [])
        if str(status or "").strip()
    }
    if not target_sources:
        return 0
    if not target_statuses:
        target_statuses = {"pending", "delivered"}

    path = _deferred_path()
    removed = 0
    with _file_lock(_deferred_lock_path(path)):
        payload = _read_json(path, {"version": 1, "requests": []})
        requests = payload.get("requests")
        if not isinstance(requests, list):
            return 0

        kept: list[Any] = []
        for item in requests:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            item_source = str(item.get("source") or "").strip().lower()
            item_status = str(item.get("status") or "pending").strip().lower() or "pending"
            if item_source in target_sources and item_status in target_statuses:
                removed += 1
                continue
            kept.append(item)

        if removed <= 0:
            return 0

        if kept:
            _write_json(path, {"version": 1, "requests": _trim_deferred_notices(kept)})
        else:
            path.unlink(missing_ok=True)

    _trace_m15(
        "deferred_notice.clear_by_source",
        path=str(path),
        removed=removed,
        sources=sorted(target_sources),
        statuses=sorted(target_statuses),
    )
    return removed


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
    _trace_m15(
        "deferred_notice.queue.start",
        path=str(path),
        kind=notice_kind,
        priority=notice_priority,
        source=notice_source,
        dedupe_key=dedupe_token,
        message=text,
    )

    with _file_lock(_deferred_lock_path(path)):
        payload = _read_json(path, {"version": 1, "requests": []})
        requests = payload.get("requests")
        if not isinstance(requests, list):
            requests = []

        for item in requests:
            if not isinstance(item, dict):
                continue
            if str(item.get("dedupe_key") or item.get("id") or "").strip() != dedupe_token:
                continue
            item_status = str(item.get("status") or "pending").strip().lower() or "pending"
            item_source = str(item.get("source") or notice_source).strip().lower() or notice_source.lower()
            if item_status == "pending":
                _trace_m15(
                    "deferred_notice.queue.deduped",
                    path=str(path),
                    dedupe_key=dedupe_token,
                    existing_id=str(item.get("id") or ""),
                    existing_status=item_status,
                )
                return False
            if item_status == "delivered" and not should_persist_pending_notice(item_source):
                _trace_m15(
                    "deferred_notice.queue.deduped",
                    path=str(path),
                    dedupe_key=dedupe_token,
                    existing_id=str(item.get("id") or ""),
                    existing_status=item_status,
                )
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
        _trace_m15(
            "deferred_notice.queue.inserted",
            path=str(path),
            id=request_id,
            dedupe_key=dedupe_token,
            pending_count=sum(
                1
                for item in requests
                if isinstance(item, dict) and str(item.get("status") or "pending").strip().lower() == "pending"
            ),
        )
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

    out = _sort_deferred_notices(notices)[: max(1, min(int(limit), 500))]
    _trace_m15(
        "deferred_notice.list",
        path=str(path),
        status=normalized_status,
        limit=limit,
        count=len(out),
    )
    return out


def drain_deferred_notices(*, limit: int = 50) -> list[dict[str, Any]]:
    path = _deferred_path()
    drained: list[dict[str, Any]] = []
    _trace_m15("deferred_notice.drain.start", path=str(path), limit=limit)

    with _file_lock(_deferred_lock_path(path)):
        payload = _read_json(path, {"version": 1, "requests": []})
        requests = payload.get("requests")
        if not isinstance(requests, list):
            return []

        pending = [
            item for item in requests
            if isinstance(item, dict) and str(item.get("status") or "pending").strip().lower() == "pending"
        ]
        pending = _sort_deferred_notices(pending)
        target_ids = {
            str(item.get("id") or "")
            for item in pending[: max(1, min(int(limit), 500))]
            if str(item.get("id") or "")
        }
        if not target_ids:
            _trace_m15("deferred_notice.drain.empty", path=str(path), pending_count=len(pending))
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

    out = _sort_deferred_notices(drained)
    _trace_m15(
        "deferred_notice.drain.delivered",
        path=str(path),
        count=len(out),
        ids=[str(item.get("id") or "") for item in out],
    )
    return out


def deliver_deferred_notices(
    *,
    limit: int = 50,
    channel_override: Optional[str] = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    path = _deferred_path()
    target = list_deferred_notices(status="pending", limit=limit)
    _trace_m15(
        "deferred_notice.deliver.start",
        path=str(path),
        limit=limit,
        target_count=len(target),
        dry_run=dry_run,
        channel_override=str(channel_override or ""),
    )
    if not target:
        return []

    adapter = get_adapter()
    if _uses_turn_scoped_deferred_notices(adapter):
        logger.warning(
            "Deferred notice immediate delivery skipped for turn-scoped adapter %s; "
            "pending notices will relay on the next user turn.",
            str(adapter.adapter_id() if hasattr(adapter, "adapter_id") else type(adapter).__name__),
        )
        _trace_m15(
            "deferred_notice.deliver.turn_scoped_skipped",
            path=str(path),
            limit=limit,
            target_count=len(target),
            adapter=str(adapter.adapter_id() if hasattr(adapter, "adapter_id") else ""),
        )
        return []

    try:
        from lib.runtime_context import send_notification as _send_notification
    except Exception as exc:
        logger.warning("Failed importing notification sender for deferred delivery: %s", exc)
        _trace_m15("deferred_notice.deliver.import_error", error=str(exc))
        if is_fail_hard_enabled():
            raise
        return []

    delivered_ids: set[str] = set()
    for item in target:
        item_id = str(item.get("id") or "").strip()
        message = str(item.get("message") or "").strip()
        if not item_id or not message:
            continue
        try:
            sent = bool(
                _send_notification(
                    message,
                    channel_override=channel_override,
                    dry_run=dry_run,
                )
            )
        except Exception as exc:
            logger.warning("Failed delivering deferred notice %s: %s", item_id, exc)
            _trace_m15("deferred_notice.deliver.error", id=item_id, error=str(exc))
            if is_fail_hard_enabled():
                raise
            continue
        if sent and not dry_run:
            delivered_ids.add(item_id)

    if not delivered_ids:
        _trace_m15("deferred_notice.deliver.none", path=str(path), dry_run=dry_run)
        return []

    delivered_at = _now_iso()
    delivered: list[dict[str, Any]] = []
    with _file_lock(_deferred_lock_path(path)):
        payload = _read_json(path, {"version": 1, "requests": []})
        requests = payload.get("requests")
        if not isinstance(requests, list):
            return []
        updated: list[dict[str, Any]] = []
        for item in requests:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id in delivered_ids and str(item.get("status") or "pending").strip().lower() == "pending":
                updated_item = dict(item)
                updated_item["status"] = "delivered"
                updated_item["delivered_at"] = delivered_at
                updated.append(updated_item)
                delivered.append(updated_item)
            else:
                updated.append(item)
        updated.sort(key=lambda item: str(item.get("created_at") or ""))
        _write_json(path, {"version": 1, "requests": _trim_deferred_notices(updated)})

    out = _sort_deferred_notices(delivered)
    _trace_m15(
        "deferred_notice.deliver.done",
        path=str(path),
        count=len(out),
        ids=[str(item.get("id") or "") for item in out],
    )
    return out


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
