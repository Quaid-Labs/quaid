"""Plugin contract health diagnostics."""

from __future__ import annotations

import json
from typing import Any, Dict


def collect_plugin_health() -> Dict[str, Any]:
    from config import get_config
    from lib.runtime_context import get_workspace_dir
    from core.runtime.plugins import (
        get_runtime_registry,
        initialize_plugin_runtime,
        run_plugin_contract_surface_collect,
    )

    cfg = get_config()
    plugins = getattr(cfg, "plugins", None)
    if not plugins or not bool(getattr(plugins, "enabled", False)):
        return {"enabled": False, "plugins": {}}

    registry = get_runtime_registry()
    init_errors: list[str] = []
    init_warnings: list[str] = []
    workspace_root = str(get_workspace_dir())
    if registry is None:
        registry, init_errors, init_warnings = initialize_plugin_runtime(
            api_version=int(getattr(plugins, "api_version", 1) or 1),
            paths=list(getattr(plugins, "paths", []) or []),
            allowlist=list(getattr(plugins, "allowlist", []) or []),
            strict=bool(getattr(plugins, "strict", True)),
            slots={
                "adapter": str(getattr(plugins.slots, "adapter", "") or ""),
                "ingest": list(getattr(plugins.slots, "ingest", []) or []),
                "datastores": list(getattr(plugins.slots, "datastores", []) or []),
            },
            workspace_root=workspace_root,
        )
    errors, warnings, results = run_plugin_contract_surface_collect(
        registry=registry,
        slots={
            "adapter": str(getattr(plugins.slots, "adapter", "") or ""),
            "ingest": list(getattr(plugins.slots, "ingest", []) or []),
            "datastores": list(getattr(plugins.slots, "datastores", []) or []),
        },
        surface="health",
        config=cfg,
        plugin_config=dict(getattr(plugins, "config", {}) or {}),
        workspace_root=workspace_root,
        strict=bool(getattr(plugins, "strict", True)),
    )
    health = {plugin_id: payload for plugin_id, payload in results}
    return {
        "enabled": True,
        "errors": list(init_errors) + list(errors),
        "warnings": list(init_warnings) + list(warnings),
        "plugins": health,
    }


def main() -> int:
    payload = collect_plugin_health()
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
