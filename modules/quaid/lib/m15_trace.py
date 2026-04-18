"""Opt-in M15 trace sink for live-test notification forensics.

This module is intentionally side-effect-light. It writes only when an M15
prompt activates it, or when QUAID_M15_TRACE/QUAID_M15_TRACE_FILE is set.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

_ACTIVE_ENV = "_QUAID_M15_TRACE_ACTIVE"
_MAX_STRING = 2400


def _truthy(value: str | None) -> bool:
    token = str(value or "").strip().lower()
    return token not in {"", "0", "false", "no", "off"}


def activate_m15_trace_for_prompt(prompt: str) -> bool:
    """Enable tracing for this hook subprocess when the prompt is an M15 turn."""
    if is_m15_trace_active():
        return True
    text = str(prompt or "")
    if "m15" in text.lower():
        os.environ[_ACTIVE_ENV] = "1"
        return True
    return False


def is_m15_trace_active() -> bool:
    return (
        _truthy(os.environ.get(_ACTIVE_ENV))
        or _truthy(os.environ.get("QUAID_M15_TRACE"))
        or bool(str(os.environ.get("QUAID_M15_TRACE_FILE") or "").strip())
    )


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    return slug.strip("-._")[:96] or "unknown"


def m15_trace_path() -> str:
    explicit = str(os.environ.get("QUAID_M15_TRACE_FILE") or "").strip()
    if explicit:
        return explicit
    instance = _safe_slug(os.environ.get("QUAID_INSTANCE") or "unknown-instance")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"/tmp/m15-trace-{instance}-{stamp}.jsonl"


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            return value[:_MAX_STRING] + f"…<truncated {len(value) - _MAX_STRING} chars>"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= 80:
                out["__truncated_keys__"] = len(value) - idx
                break
            out[str(key)] = _safe_value(item)
        return out
    if isinstance(value, (list, tuple, set)):
        seq = list(value)
        out = [_safe_value(item) for item in seq[:80]]
        if len(seq) > 80:
            out.append({"__truncated_items__": len(seq) - 80})
        return out
    return str(value)


def trace_m15(event: str, **fields: Any) -> None:
    """Append one JSONL trace event if M15 tracing is active."""
    if not is_m15_trace_active():
        return
    try:
        path = Path(m15_trace_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": str(event or "unknown"),
            "pid": os.getpid(),
            "instance": os.environ.get("QUAID_INSTANCE", ""),
            "adapter_env": os.environ.get("QUAID_ADAPTER_TYPE", ""),
        }
        payload.update({str(key): _safe_value(value) for key, value in fields.items()})
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
    except Exception:
        # Telemetry must never affect hook/runtime behavior.
        return
