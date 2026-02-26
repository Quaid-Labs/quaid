"""Core plugin manifest loader and registry (phase-1 foundation).

This module intentionally seeds strict plugin contracts without changing active
runtime behavior yet. Current built-ins can migrate onto this registry in later
phases.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lib.runtime_context import get_workspace_dir

_PLUGIN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_PLUGIN_TYPES = {"adapter", "ingest", "datastore"}
_PLUGIN_API_VERSION = 1
_DATASTORE_REQUIRED_CAPABILITIES = (
    "supports_multi_user",
    "supports_policy_metadata",
    "supports_redaction",
)


@dataclass(frozen=True)
class PluginManifest:
    plugin_api_version: int
    plugin_id: str
    plugin_type: str
    module: str
    entrypoint: str = "register"
    capabilities: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    priority: int = 100
    enabled: bool = True
    source_path: str = ""


@dataclass(frozen=True)
class PluginRecord:
    manifest: PluginManifest
    owner_path: str


class PluginRegistry:
    """Strict plugin registry with single-registration conflict protection."""

    def __init__(self, api_version: int = _PLUGIN_API_VERSION) -> None:
        self.api_version = int(api_version)
        self._by_id: Dict[str, PluginRecord] = {}
        self._singletons: Dict[str, str] = {}  # slot -> plugin_id

    def register(self, manifest: PluginManifest) -> None:
        if manifest.plugin_api_version != self.api_version:
            raise ValueError(
                f"Plugin '{manifest.plugin_id}' api version mismatch: "
                f"manifest={manifest.plugin_api_version}, runtime={self.api_version}"
            )
        existing = self._by_id.get(manifest.plugin_id)
        if existing:
            if existing.manifest == manifest:
                return
            raise ValueError(f"Plugin id conflict: '{manifest.plugin_id}' already registered")
        self._by_id[manifest.plugin_id] = PluginRecord(
            manifest=manifest,
            owner_path=manifest.source_path or "",
        )

    def activate_singleton(self, slot: str, plugin_id: str) -> None:
        key = str(slot or "").strip()
        pid = str(plugin_id or "").strip()
        if not key or not pid:
            raise ValueError("slot and plugin_id are required for singleton activation")
        if pid not in self._by_id:
            raise KeyError(f"Cannot activate unknown plugin: {pid}")
        existing = self._singletons.get(key)
        if existing and existing != pid:
            raise ValueError(
                f"Singleton slot '{key}' already activated by '{existing}', cannot activate '{pid}'"
            )
        self._singletons[key] = pid

    def get(self, plugin_id: str) -> Optional[PluginRecord]:
        return self._by_id.get(str(plugin_id or "").strip())

    def list(self, plugin_type: Optional[str] = None) -> List[PluginRecord]:
        records = list(self._by_id.values())
        if plugin_type:
            pt = str(plugin_type).strip().lower()
            records = [r for r in records if r.manifest.plugin_type == pt]
        records.sort(key=lambda r: (r.manifest.priority, r.manifest.plugin_id))
        return records

    def singletons(self) -> Dict[str, str]:
        return dict(self._singletons)


def validate_manifest_dict(payload: Dict[str, Any], *, source_path: str = "") -> PluginManifest:
    if not isinstance(payload, dict):
        raise ValueError("Manifest payload must be a JSON object")

    plugin_api_version = int(payload.get("plugin_api_version", payload.get("pluginApiVersion", 0)) or 0)
    plugin_id = str(payload.get("plugin_id", payload.get("id", "")) or "").strip()
    plugin_type = str(payload.get("plugin_type", payload.get("type", "")) or "").strip().lower()
    module = str(payload.get("module", "") or "").strip()
    entrypoint = str(payload.get("entrypoint", "register") or "register").strip()
    capabilities = payload.get("capabilities", {})
    dependencies = payload.get("dependencies", [])
    priority = int(payload.get("priority", 100) or 100)
    enabled = bool(payload.get("enabled", True))

    if plugin_api_version <= 0:
        raise ValueError("Manifest missing plugin_api_version")
    if not plugin_id:
        raise ValueError("Manifest missing plugin_id")
    if not _PLUGIN_ID_RE.match(plugin_id):
        raise ValueError(f"Invalid plugin_id '{plugin_id}'")
    if plugin_type not in _PLUGIN_TYPES:
        raise ValueError(
            f"Invalid plugin_type '{plugin_type}'. Expected one of: {sorted(_PLUGIN_TYPES)}"
        )
    if not module:
        raise ValueError("Manifest missing module")
    if not isinstance(capabilities, dict):
        raise ValueError("Manifest capabilities must be an object")
    if plugin_type == "datastore":
        missing = [k for k in _DATASTORE_REQUIRED_CAPABILITIES if k not in capabilities]
        if missing:
            raise ValueError(
                "Datastore manifest missing required capabilities: " + ", ".join(missing)
            )
        invalid = [k for k in _DATASTORE_REQUIRED_CAPABILITIES if not isinstance(capabilities.get(k), bool)]
        if invalid:
            raise ValueError(
                "Datastore manifest capability flags must be booleans: " + ", ".join(invalid)
            )
    if not isinstance(dependencies, list):
        raise ValueError("Manifest dependencies must be an array")

    deps = [str(d).strip() for d in dependencies if str(d).strip()]

    return PluginManifest(
        plugin_api_version=plugin_api_version,
        plugin_id=plugin_id,
        plugin_type=plugin_type,
        module=module,
        entrypoint=entrypoint,
        capabilities=capabilities,
        dependencies=deps,
        priority=priority,
        enabled=enabled,
        source_path=source_path,
    )


def _resolve_plugin_paths(paths: List[str]) -> List[Path]:
    root = get_workspace_dir()
    out: List[Path] = []
    for raw in paths:
        token = str(raw or "").strip()
        if not token:
            continue
        p = Path(token)
        if not p.is_absolute():
            p = root / p
        out.append(p)
    # deterministic de-dup
    unique = []
    seen = set()
    for p in out:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def discover_plugin_manifests(
    *,
    paths: List[str],
    allowlist: Optional[List[str]] = None,
    strict: bool = True,
) -> Tuple[List[PluginManifest], List[str]]:
    """Discover plugin manifests from configured plugin directories.

    Returns (manifests, errors). In strict mode, malformed manifests raise.
    """
    resolved_paths = _resolve_plugin_paths(paths)
    allowed = {str(x).strip() for x in (allowlist or []) if str(x).strip()}
    manifests: List[PluginManifest] = []
    errors: List[str] = []
    for base in resolved_paths:
        if not base.exists():
            continue
        if not base.is_dir():
            msg = f"Plugin path is not a directory: {base}"
            if strict:
                raise ValueError(msg)
            errors.append(msg)
            continue
        for manifest_path in sorted(base.rglob("plugin.json")):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = validate_manifest_dict(payload, source_path=str(manifest_path))
                if allowed and manifest.plugin_id not in allowed:
                    continue
                if not manifest.enabled:
                    continue
                manifests.append(manifest)
            except Exception as exc:
                msg = f"Invalid plugin manifest {manifest_path}: {exc}"
                if strict:
                    raise ValueError(msg) from exc
                errors.append(msg)
    return manifests, errors
