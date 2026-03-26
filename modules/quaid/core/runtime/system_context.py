"""Runtime system-context metadata aggregation and rendering."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List


def _active_slots(plugins: Any) -> Dict[str, Any]:
    slots = getattr(plugins, "slots", None)
    return {
        "adapter": str(getattr(slots, "adapter", "") or ""),
        "ingest": list(getattr(slots, "ingest", []) or []),
        "datastores": list(getattr(slots, "datastores", []) or []),
    }


def _normalize_metadata_entry(raw: Any, *, plugin_id: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"system-context entry from '{plugin_id}' must be an object")
    key = str(raw.get("key", "") or "").strip()
    label = str(raw.get("label", "") or "").strip()
    value = str(raw.get("value", "") or "").strip()
    note = str(raw.get("note", "") or "").strip()
    if not key:
        raise ValueError(f"system-context entry from '{plugin_id}' is missing key")
    if not label:
        raise ValueError(f"system-context entry '{key}' from '{plugin_id}' is missing label")
    if not value:
        raise ValueError(f"system-context entry '{key}' from '{plugin_id}' is missing value")
    try:
        order = int(raw.get("order", 100) or 100)
    except Exception as exc:
        raise ValueError(f"system-context entry '{key}' from '{plugin_id}' has invalid order") from exc
    return {
        "key": key,
        "label": label,
        "value": value,
        "note": note,
        "order": order,
        "plugin_id": plugin_id,
    }


def _normalize_metadata_payload(raw: Any, *, plugin_id: str) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict) and "entries" in raw:
        entries = raw.get("entries") or []
    elif isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = [raw]
    else:
        raise ValueError(f"system-context metadata from '{plugin_id}' must be an object or list")
    if not isinstance(entries, list):
        raise ValueError(f"system-context metadata entries from '{plugin_id}' must be a list")
    return [_normalize_metadata_entry(entry, plugin_id=plugin_id) for entry in entries if entry]


def collect_system_context_metadata(
    *,
    config: Any | None = None,
    workspace_root: str | None = None,
    strict: bool | None = None,
) -> Dict[str, Any]:
    from config import get_config
    from lib.instance import quaid_home
    from core.runtime.plugins import (
        collect_datastore_system_context_metadata,
        get_runtime_registry,
        initialize_plugin_runtime,
    )

    cfg = config or get_config()
    plugins = getattr(cfg, "plugins", None)
    if not plugins or not bool(getattr(plugins, "enabled", False)):
        return {"entries": []}

    strict_mode = bool(getattr(plugins, "strict", True)) if strict is None else bool(strict)
    resolved_root = str(workspace_root or quaid_home())
    registry = get_runtime_registry()
    init_errors: List[str] = []
    init_warnings: List[str] = []
    if registry is None:
        registry, init_errors, init_warnings = initialize_plugin_runtime(
            api_version=int(getattr(plugins, "api_version", 1) or 1),
            paths=list(getattr(plugins, "paths", []) or []),
            allowlist=list(getattr(plugins, "allowlist", []) or []),
            strict=strict_mode,
            slots=_active_slots(plugins),
            workspace_root=resolved_root,
        )

    errors, warnings, results = collect_datastore_system_context_metadata(
        registry=registry,
        slots=_active_slots(plugins),
        config=cfg,
        plugin_config=dict(getattr(plugins, "config", {}) or {}),
        workspace_root=resolved_root,
        strict=strict_mode,
    )

    all_errors = list(init_errors) + list(errors)
    all_warnings = list(init_warnings) + list(warnings)
    if strict_mode and all_errors:
        raise RuntimeError("System context metadata collection failed: " + "; ".join(all_errors))

    entries: List[Dict[str, Any]] = []
    for plugin_id, payload in results:
        entries.extend(_normalize_metadata_payload(payload, plugin_id=plugin_id))
    entries.sort(key=lambda entry: (int(entry.get("order", 100) or 100), str(entry.get("label", ""))))

    return {
        "entries": entries,
        "errors": all_errors,
        "warnings": all_warnings,
    }


def build_system_context_block(
    *,
    config: Any | None = None,
    workspace_root: str | None = None,
    strict: bool | None = None,
) -> str:
    payload = collect_system_context_metadata(config=config, workspace_root=workspace_root, strict=strict)
    entries = list(payload.get("entries") or [])
    if not entries:
        return ""

    lines: List[str] = ["[Quaid runtime]"]
    instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
    if instance:
        lines.append(f"instance: {instance}")
    for entry in entries:
        label = str(entry.get("label", "") or "").strip()
        value = str(entry.get("value", "") or "").strip()
        note = str(entry.get("note", "") or "").strip()
        if label and value:
            lines.append(f"{label}: {value}")
        if note:
            lines.append(f"runtime note: {note}")
    return "\n".join(lines)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render runtime system-context metadata")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a formatted block")
    args = parser.parse_args()

    payload = collect_system_context_metadata()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(build_system_context_block())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
