"""Core plugin manifest loader and runtime registry."""

from __future__ import annotations

import json
import os
import re
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

_PLUGIN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
_PLUGIN_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_PLUGIN_TYPES = {"adapter", "ingest", "datastore"}
_PLUGIN_API_VERSION = 1
_PLUGIN_CONTRACT_REQUIRED = (
    "init",
    "config",
    "status",
    "dashboard",
    "maintenance",
    "tool_runtime",
    "health",
    "tools",
    "api",
    "events",
    "ingest_triggers",
    "auth_requirements",
    "migrations",
    "notifications",
)
_PLUGIN_CONTRACT_EXECUTABLE = (
    "init",
    "config",
    "status",
    "dashboard",
    "maintenance",
    "tool_runtime",
    "health",
)
_PLUGIN_CONTRACT_DECLARED = (
    "tools",
    "api",
    "events",
    "ingest_triggers",
    "auth_requirements",
    "migrations",
    "notifications",
)
_PLUGIN_CONTRACT_REQUIRED_SET = set(_PLUGIN_CONTRACT_REQUIRED)
_PLUGIN_CONTRACT_PARTITION_SET = set(_PLUGIN_CONTRACT_EXECUTABLE) | set(_PLUGIN_CONTRACT_DECLARED)
if _PLUGIN_CONTRACT_REQUIRED_SET != _PLUGIN_CONTRACT_PARTITION_SET:
    raise RuntimeError(
        "Plugin contract key partition mismatch: required keys must equal executable+declared keys"
    )
_DATASTORE_REQUIRED_CAPABILITIES = (
    "supports_multi_user",
    "supports_policy_metadata",
    "supports_redaction",
)
_RUNTIME_LOCK = Lock()
_RUNTIME_REGISTRY: Optional["PluginRegistry"] = None
_RUNTIME_ERRORS: List[str] = []
_RUNTIME_WARNINGS: List[str] = []
_CONTRACT_VALIDATION_LOCK = Lock()
_CONTRACT_VALIDATION_CACHE: set[Tuple[str, str]] = set()


@dataclass(frozen=True)
class PluginManifest:
    plugin_api_version: int
    plugin_id: str
    plugin_type: str
    module: str
    display_name: str
    capabilities: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    priority: int = 100
    enabled: bool = True
    source_path: str = ""


@dataclass(frozen=True)
class PluginRecord:
    manifest: PluginManifest
    owner_path: str


@dataclass(frozen=True)
class PluginHookContext:
    plugin: PluginManifest
    config: Any
    plugin_config: Dict[str, Any]
    workspace_root: str
    payload: Dict[str, Any] = field(default_factory=dict)


class PluginRegistry:
    """Strict plugin registry with single-registration conflict protection."""

    def __init__(self, api_version: int = _PLUGIN_API_VERSION) -> None:
        self.api_version = int(api_version)
        self._by_id: Dict[str, PluginRecord] = {}
        self._singletons: Dict[str, str] = {}  # slot -> plugin_id
        self._lock = Lock()

    def register(self, manifest: PluginManifest) -> None:
        with self._lock:
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
        with self._lock:
            if pid not in self._by_id:
                raise KeyError(f"Cannot activate unknown plugin: {pid}")
            existing = self._singletons.get(key)
            if existing and existing != pid:
                raise ValueError(
                    f"Singleton slot '{key}' already activated by '{existing}', cannot activate '{pid}'"
                )
            self._singletons[key] = pid

    def get(self, plugin_id: str) -> Optional[PluginRecord]:
        with self._lock:
            return self._by_id.get(str(plugin_id or "").strip())

    def list(self, plugin_type: Optional[str] = None) -> List[PluginRecord]:
        with self._lock:
            records = list(self._by_id.values())
        if plugin_type:
            pt = str(plugin_type).strip().lower()
            records = [r for r in records if r.manifest.plugin_type == pt]
        records.sort(key=lambda r: (r.manifest.priority, r.manifest.plugin_id))
        return records

    def singletons(self) -> Dict[str, str]:
        # Adapter is currently the only singleton slot; ingest/datastore slots
        # are intentionally multi-plugin lists validated at config load.
        with self._lock:
            return dict(self._singletons)


def validate_manifest_dict(payload: Dict[str, Any], *, source_path: str = "") -> PluginManifest:
    if not isinstance(payload, dict):
        raise ValueError("Manifest payload must be a JSON object")

    plugin_api_version = int(payload.get("plugin_api_version", payload.get("pluginApiVersion", 0)) or 0)
    plugin_id = str(payload.get("plugin_id", payload.get("id", "")) or "").strip()
    plugin_type = str(payload.get("plugin_type", payload.get("type", "")) or "").strip().lower()
    module = str(payload.get("module", "") or "").strip()
    capabilities = payload.get("capabilities", {})
    dependencies = payload.get("dependencies", [])
    raw_priority = payload.get("priority", 100)
    priority = int(100 if raw_priority is None else raw_priority)
    enabled_raw = payload.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise ValueError("Manifest enabled must be a boolean")
    enabled = enabled_raw

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
    if not _PLUGIN_MODULE_RE.match(module):
        raise ValueError(f"Invalid manifest module '{module}'")
    if not isinstance(capabilities, dict):
        raise ValueError("Manifest capabilities must be an object")
    contract = capabilities.get("contract", {})
    if not isinstance(contract, dict):
        raise ValueError("Manifest capabilities.contract must be an object")
    display_name = capabilities.get("display_name", capabilities.get("displayName", ""))
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("Manifest capabilities.display_name is required")
    display_name = display_name.strip()
    capabilities = dict(capabilities)
    capabilities["display_name"] = display_name
    for key in _PLUGIN_CONTRACT_REQUIRED:
        if key not in contract:
            raise ValueError(f"Manifest capabilities.contract missing required key: {key}")
        if not isinstance(contract.get(key), dict):
            raise ValueError(f"Manifest capabilities.contract.{key} must be an object")
    for executable_key in _PLUGIN_CONTRACT_EXECUTABLE:
        executable_spec = contract.get(executable_key, {})
        executable_mode = str(executable_spec.get("mode", "")).strip().lower()
        allowed_modes = {"hook"}
        if executable_key == "dashboard":
            allowed_modes.add("tbd")
        if executable_mode not in allowed_modes:
            allowed_list = "', '".join(sorted(allowed_modes))
            raise ValueError(
                f"Manifest capabilities.contract.{executable_key}.mode must be '{allowed_list}'"
            )
    for declared_key in _PLUGIN_CONTRACT_DECLARED:
        declared_spec = contract.get(declared_key, {})
        declared_mode = str(declared_spec.get("mode", "")).strip().lower()
        if declared_mode != "declared":
            raise ValueError(
                f"Manifest capabilities.contract.{declared_key}.mode must be 'declared'"
            )
        declared_exports = declared_spec.get("exports", [])
        if not isinstance(declared_exports, list):
            raise ValueError(
                f"Manifest capabilities.contract.{declared_key}.exports must be an array"
            )
        if any(not isinstance(item, str) or not item.strip() for item in declared_exports):
            raise ValueError(
                f"Manifest capabilities.contract.{declared_key}.exports must contain non-empty strings"
            )
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
        display_name=display_name,
        capabilities=capabilities,
        dependencies=deps,
        priority=priority,
        enabled=enabled,
        source_path=source_path,
    )


def _workspace_root() -> Path:
    env_root = (
        os.environ.get("QUAID_HOME", "").strip()
        or os.environ.get("CLAWDBOT_WORKSPACE", "").strip()
    )
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


def _resolve_plugin_paths(paths: List[str], *, workspace_root: Optional[Path] = None) -> List[Path]:
    root = (workspace_root or _workspace_root()).resolve()
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
    workspace_root: Optional[Path] = None,
) -> Tuple[List[PluginManifest], List[str]]:
    """Discover plugin manifests from configured plugin directories.

    Returns (manifests, errors). In strict mode, collects all malformed manifests
    and raises once after scanning completes.
    """
    resolved_paths = _resolve_plugin_paths(paths, workspace_root=workspace_root)
    allowed = {str(x).strip() for x in (allowlist or []) if str(x).strip()}
    manifests: List[PluginManifest] = []
    errors: List[str] = []
    for base in resolved_paths:
        if not base.exists():
            continue
        if not base.is_dir():
            msg = f"Plugin path is not a directory: {base}"
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
                errors.append(msg)
    if strict and errors:
        raise ValueError("Plugin manifest discovery failed: " + "; ".join(errors))
    return manifests, errors


def _validate_slot_selection(
    registry: PluginRegistry,
    *,
    plugin_id: str,
    expected_type: str,
    slot_label: str,
    strict: bool,
    errors: List[str],
) -> None:
    pid = str(plugin_id or "").strip()
    if not pid:
        return
    rec = registry.get(pid)
    if rec is None:
        msg = f"Plugin slot '{slot_label}' references unknown plugin_id '{pid}'"
        if strict:
            raise ValueError(msg)
        errors.append(msg)
        return
    if rec.manifest.plugin_type != expected_type:
        msg = (
            f"Plugin slot '{slot_label}' expected type '{expected_type}', "
            f"but '{pid}' is '{rec.manifest.plugin_type}'"
        )
        if strict:
            raise ValueError(msg)
        errors.append(msg)


def _validate_declared_export_conflicts(
    registry: PluginRegistry,
    *,
    slots: Dict[str, Any],
    strict: bool,
    errors: List[str],
) -> None:
    active_ids = _iter_active_plugin_ids(slots, registry=registry)
    for surface in _PLUGIN_CONTRACT_DECLARED:
        owners_by_export: Dict[str, List[str]] = {}
        for plugin_id in active_ids:
            rec = registry.get(plugin_id)
            if rec is None:
                continue
            contract = (rec.manifest.capabilities or {}).get("contract", {})
            if not isinstance(contract, dict):
                continue
            spec = contract.get(surface, {})
            if not isinstance(spec, dict):
                continue
            exports = spec.get("exports", [])
            if not isinstance(exports, list):
                continue
            for item in exports:
                token = str(item or "").strip()
                if not token:
                    continue
                owners_by_export.setdefault(token, []).append(plugin_id)
        conflicts = {
            export_name: owners
            for export_name, owners in owners_by_export.items()
            if len(owners) > 1
        }
        for export_name, owners in sorted(conflicts.items()):
            msg = (
                f"Duplicate declared {surface} export '{export_name}' "
                f"across active plugins: {', '.join(sorted(owners))}"
            )
            if strict:
                raise ValueError(msg)
            errors.append(msg)


def _validate_plugin_dependencies(
    registry: PluginRegistry,
    *,
    slots: Dict[str, Any],
    strict: bool,
    errors: List[str],
) -> None:
    active_ids = _iter_active_plugin_ids(slots, registry=registry)
    active_set = set(active_ids)
    for plugin_id in active_ids:
        rec = registry.get(plugin_id)
        if rec is None:
            continue
        for dep in rec.manifest.dependencies:
            dep_id = str(dep or "").strip()
            if not dep_id:
                continue
            dep_rec = registry.get(dep_id)
            if dep_rec is None:
                msg = (
                    f"Plugin '{plugin_id}' depends on unknown plugin_id '{dep_id}'"
                )
                if strict:
                    raise ValueError(msg)
                errors.append(msg)
                continue
            if dep_id not in active_set:
                msg = (
                    f"Plugin '{plugin_id}' depends on inactive plugin_id '{dep_id}'"
                )
                if strict:
                    raise ValueError(msg)
                errors.append(msg)


def initialize_plugin_runtime(
    *,
    api_version: int,
    paths: List[str],
    allowlist: Optional[List[str]] = None,
    strict: bool = True,
    slots: Optional[Dict[str, Any]] = None,
    workspace_root: Optional[str] = None,
) -> Tuple[PluginRegistry, List[str], List[str]]:
    """Build and cache plugin runtime registry from config."""
    registry = PluginRegistry(api_version=max(1, int(api_version or 1)))
    root: Optional[Path] = None
    if workspace_root:
        root = Path(str(workspace_root)).expanduser().resolve()
    manifests, errors = discover_plugin_manifests(
        paths=paths,
        allowlist=allowlist,
        strict=strict,
        workspace_root=root,
    )
    warnings: List[str] = []
    for manifest in manifests:
        contract = (manifest.capabilities or {}).get("contract", {})
        if isinstance(contract, dict):
            for surface in _PLUGIN_CONTRACT_DECLARED:
                spec = contract.get(surface, {})
                if not isinstance(spec, dict):
                    continue
                exports = spec.get("exports", [])
                if isinstance(exports, list) and len(exports) == 0:
                    warnings.append(
                        f"Plugin '{manifest.plugin_id}' declares surface '{surface}' with empty exports"
                    )
        try:
            registry.register(manifest)
        except Exception as exc:
            msg = f"Plugin registration failed for '{manifest.plugin_id}': {exc}"
            if strict:
                raise ValueError(msg) from exc
            warnings.append(msg)
    slot_data = slots or {}
    adapter_slot = str(slot_data.get("adapter", "")).strip()
    _validate_slot_selection(
        registry,
        plugin_id=adapter_slot,
        expected_type="adapter",
        slot_label="adapter",
        strict=strict,
        errors=errors,
    )
    adapter_rec = registry.get(adapter_slot) if adapter_slot else None
    # Adapter remains the only singleton because it is the sole runtime
    # type with an exclusive active slot. Ingest/datastore slots are lists.
    if adapter_slot and adapter_rec and adapter_rec.manifest.plugin_type == "adapter":
        try:
            registry.activate_singleton("adapter", adapter_slot)
        except Exception as exc:
            msg = f"Adapter singleton activation failed for '{adapter_slot}': {exc}"
            if strict:
                raise ValueError(msg) from exc
            errors.append(msg)
    for idx, plugin_id in enumerate(slot_data.get("ingest", []) or []):
        _validate_slot_selection(
            registry,
            plugin_id=str(plugin_id),
            expected_type="ingest",
            slot_label=f"ingest[{idx}]",
            strict=strict,
            errors=errors,
        )
    for idx, plugin_id in enumerate(slot_data.get("datastores", []) or []):
        _validate_slot_selection(
            registry,
            plugin_id=str(plugin_id),
            expected_type="datastore",
            slot_label=f"datastores[{idx}]",
            strict=strict,
            errors=errors,
        )
    _validate_declared_export_conflicts(
        registry,
        slots=slot_data,
        strict=strict,
        errors=errors,
    )
    _validate_plugin_dependencies(
        registry,
        slots=slot_data,
        strict=strict,
        errors=errors,
    )

    with _RUNTIME_LOCK:
        global _RUNTIME_REGISTRY, _RUNTIME_ERRORS, _RUNTIME_WARNINGS
        _RUNTIME_REGISTRY = registry
        _RUNTIME_ERRORS = list(errors)
        _RUNTIME_WARNINGS = list(warnings)
    return registry, list(errors), list(warnings)


def reset_plugin_runtime() -> None:
    with _RUNTIME_LOCK:
        global _RUNTIME_REGISTRY, _RUNTIME_ERRORS, _RUNTIME_WARNINGS
        _RUNTIME_REGISTRY = None
        _RUNTIME_ERRORS = []
        _RUNTIME_WARNINGS = []
    with _CONTRACT_VALIDATION_LOCK:
        _CONTRACT_VALIDATION_CACHE.clear()


def _iter_active_plugin_ids(
    slots: Optional[Dict[str, Any]],
    *,
    registry: Optional[PluginRegistry] = None,
) -> List[str]:
    data = slots or {}
    ids: List[str] = []
    adapter_id = str(data.get("adapter", "")).strip()
    if adapter_id:
        ids.append(adapter_id)
    for value in data.get("ingest", []) or []:
        pid = str(value or "").strip()
        if pid:
            ids.append(pid)
    for value in data.get("datastores", []) or []:
        pid = str(value or "").strip()
        if pid:
            ids.append(pid)
    # deterministic de-dupe while preserving order
    seen = set()
    out: List[str] = []
    for pid in ids:
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
    if not registry:
        return out

    def _sort_key(plugin_id: str) -> Tuple[int, str]:
        record = registry.get(plugin_id)
        if not record:
            return (10**9, plugin_id)
        return (int(record.manifest.priority), record.manifest.plugin_id)

    out.sort(key=_sort_key)
    return out


def _resolve_hook_callable(manifest: PluginManifest, handler_ref: str) -> Callable[[PluginHookContext], Any]:
    token = str(handler_ref or "").strip()
    if not token:
        raise ValueError("hook handler reference is empty")
    manifest_module = str(manifest.module or "").strip()
    if ":" in token:
        module_ref, func_name = token.split(":", 1)
        module_ref = module_ref.strip()
        func_name = func_name.strip()
        if not _PLUGIN_MODULE_RE.match(module_ref):
            raise ValueError(
                f"hook handler '{token}' for plugin '{manifest.plugin_id}' has invalid module '{module_ref}'"
            )
        if not _module_in_manifest_namespace(manifest, module_ref):
            manifest_pkg = manifest_module.rsplit(".", 1)[0] if "." in manifest_module else manifest_module
            raise ValueError(
                f"hook handler '{token}' for plugin '{manifest.plugin_id}' must resolve inside '{manifest_pkg}'"
            )
        target_mod = importlib.import_module(module_ref)
        fn = getattr(target_mod, func_name, None)
    else:
        target_mod = importlib.import_module(manifest_module)
        fn = getattr(target_mod, token, None)
    if not callable(fn):
        raise ValueError(
            f"hook handler '{token}' for plugin '{manifest.plugin_id}' is not callable"
        )
    return fn


def _contract_module_for_handler(manifest: PluginManifest, handler_ref: str) -> str:
    token = str(handler_ref or "").strip()
    if ":" in token:
        module_ref, _ = token.split(":", 1)
        module_ref = module_ref.strip()
        if module_ref:
            return module_ref
    return manifest.module


def _module_in_manifest_namespace(manifest: PluginManifest, module_ref: str) -> bool:
    manifest_module = str(manifest.module or "").strip()
    module_token = str(module_ref or "").strip()
    if not manifest_module or not module_token:
        return False
    manifest_pkg = manifest_module.rsplit(".", 1)[0] if "." in manifest_module else manifest_module
    return (
        module_token == manifest_module
        or module_token == manifest_pkg
        or module_token.startswith(f"{manifest_pkg}.")
    )


def _validate_contract_instance(manifest: PluginManifest, handler_ref: str = "") -> None:
    contract_module = _contract_module_for_handler(manifest, handler_ref)
    if not _PLUGIN_MODULE_RE.match(contract_module):
        raise ValueError(
            f"plugin '{manifest.plugin_id}' handler module '{contract_module}' is invalid"
        )
    if not _module_in_manifest_namespace(manifest, contract_module):
        raise ValueError(
            f"plugin '{manifest.plugin_id}' handler module '{contract_module}' is outside '{manifest.module}' namespace"
        )
    cache_key = (manifest.plugin_id, contract_module)
    with _CONTRACT_VALIDATION_LOCK:
        if cache_key in _CONTRACT_VALIDATION_CACHE:
            return
    mod = importlib.import_module(contract_module)
    contract_obj = getattr(mod, "_CONTRACT", None)
    if contract_obj is None:
        raise ValueError(
            f"plugin '{manifest.plugin_id}' module '{contract_module}' must export _CONTRACT implementing PluginContractBase"
        )
    try:
        from core.contracts.plugin_contract import PluginContractBase
    except Exception as exc:
        raise ValueError(
            f"plugin '{manifest.plugin_id}' failed loading PluginContractBase: {exc}"
        ) from exc
    if not isinstance(contract_obj, PluginContractBase):
        raise ValueError(
            f"plugin '{manifest.plugin_id}' _CONTRACT must inherit PluginContractBase"
        )
    for method_name in (
        "on_init",
        "on_config",
        "on_status",
        "on_dashboard",
        "on_maintenance",
        "on_tool_runtime",
        "on_health",
    ):
        if not callable(getattr(contract_obj, method_name, None)):
            raise ValueError(
                f"plugin '{manifest.plugin_id}' _CONTRACT missing callable {method_name}()"
            )
    with _CONTRACT_VALIDATION_LOCK:
        _CONTRACT_VALIDATION_CACHE.add(cache_key)


def run_plugin_contract_surface(
    *,
    registry: PluginRegistry,
    slots: Optional[Dict[str, Any]],
    surface: str,
    config: Any,
    plugin_config: Optional[Dict[str, Any]] = None,
    workspace_root: Optional[str] = None,
    strict: bool = True,
    skip_plugin_ids: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    errors, warnings, _ = run_plugin_contract_surface_collect(
        registry=registry,
        slots=slots,
        surface=surface,
        config=config,
        plugin_config=plugin_config,
        workspace_root=workspace_root,
        strict=strict,
        skip_plugin_ids=skip_plugin_ids,
    )
    return errors, warnings


def run_plugin_contract_surface_collect(
    *,
    registry: PluginRegistry,
    slots: Optional[Dict[str, Any]],
    surface: str,
    config: Any,
    plugin_config: Optional[Dict[str, Any]] = None,
    workspace_root: Optional[str] = None,
    strict: bool = True,
    payload: Optional[Dict[str, Any]] = None,
    skip_plugin_ids: Optional[List[str]] = None,
) -> Tuple[List[str], List[str], List[Tuple[str, Any]]]:
    key = str(surface or "").strip()
    if key not in _PLUGIN_CONTRACT_EXECUTABLE:
        raise ValueError(f"Unknown contract surface: {surface}")
    errors: List[str] = []
    warnings: List[str] = []
    results: List[Tuple[str, Any]] = []
    cfg_map = plugin_config if isinstance(plugin_config, dict) else {}
    root = str(workspace_root or _workspace_root())
    raw_payload = payload if isinstance(payload, dict) else {}
    skip_ids = {str(pid).strip() for pid in (skip_plugin_ids or []) if str(pid).strip()}

    for plugin_id in _iter_active_plugin_ids(slots, registry=registry):
        if plugin_id in skip_ids:
            continue
        record = registry.get(plugin_id)
        if not record:
            msg = f"Plugin '{plugin_id}' is active in slots but missing from runtime registry"
            if strict:
                errors.append(msg)
                break
            warnings.append(msg)
            continue
        contract = (record.manifest.capabilities or {}).get("contract", {})
        if not isinstance(contract, dict):
            continue
        surface_spec = contract.get(key, {})
        if not isinstance(surface_spec, dict):
            continue
        mode = str(surface_spec.get("mode", "")).strip().lower()
        if mode != "hook":
            continue
        handler_ref = str(surface_spec.get("handler", "")).strip()
        if not handler_ref:
            msg = f"Plugin '{plugin_id}' {key} hook missing handler declaration"
            if strict:
                errors.append(msg)
                break
            else:
                warnings.append(msg)
            continue
        try:
            _validate_contract_instance(record.manifest, handler_ref)
            fn = _resolve_hook_callable(record.manifest, handler_ref)
            ctx = PluginHookContext(
                plugin=record.manifest,
                config=config,
                plugin_config=dict(cfg_map.get(plugin_id, {}) or {}),
                workspace_root=root,
                payload=dict(raw_payload),
            )
            result = fn(ctx)
            results.append((plugin_id, result))
        except Exception as exc:
            msg = (
                f"Plugin '{plugin_id}' {key} hook failed "
                f"({handler_ref or 'missing-handler'}): {exc}"
            )
            if strict:
                errors.append(msg)
                break
            else:
                warnings.append(msg)

    return errors, warnings, results


def get_runtime_registry() -> Optional[PluginRegistry]:
    with _RUNTIME_LOCK:
        return _RUNTIME_REGISTRY


def get_runtime_errors() -> List[str]:
    with _RUNTIME_LOCK:
        return list(_RUNTIME_ERRORS)


def get_runtime_warnings() -> List[str]:
    with _RUNTIME_LOCK:
        return list(_RUNTIME_WARNINGS)


def collect_declared_exports(
    *,
    registry: PluginRegistry,
    slots: Optional[Dict[str, Any]],
    surface: str,
    strict: bool = False,
) -> Dict[str, List[str]]:
    key = str(surface or "").strip()
    if key not in _PLUGIN_CONTRACT_DECLARED:
        raise ValueError(f"Unsupported declaration surface: {surface}")
    out: Dict[str, List[str]] = {}
    for plugin_id in _iter_active_plugin_ids(slots, registry=registry):
        record = registry.get(plugin_id)
        if not record:
            if strict:
                raise ValueError(
                    f"Plugin '{plugin_id}' is active in slots but missing from runtime registry"
                )
            continue
        contract = (record.manifest.capabilities or {}).get("contract", {})
        if not isinstance(contract, dict):
            continue
        spec = contract.get(key, {})
        if not isinstance(spec, dict):
            continue
        exports = spec.get("exports", [])
        if not isinstance(exports, list):
            continue
        out[plugin_id] = [str(item).strip() for item in exports if str(item).strip()]
    return out
