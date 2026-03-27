#!/usr/bin/env python3
"""Interactive config CLI for Quaid memory config."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


def _workspace_root() -> Path:
    for env in ("QUAID_HOME", "CLAWDBOT_WORKSPACE"):
        value = os.getenv(env, "").strip()
        if value:
            return Path(value)
    return Path.cwd()


def _resolve_config_target(args: argparse.Namespace | None = None) -> tuple[Path, str]:
    """Resolve which config file to target based on flags and environment.

    Priority (first match wins):
      --shared flag          → QUAID_HOME/shared/config/memory.json
      --instance <id> flag   → QUAID_HOME/<id>/config/memory.json
      QUAID_INSTANCE env     → QUAID_HOME/<QUAID_INSTANCE>/config/memory.json
      (none)                 → QUAID_HOME/shared/config/memory.json  (default)

    Returns (config_path, label).
    """
    home = _workspace_root()

    shared_flag = getattr(args, "shared", False)
    instance_arg = getattr(args, "instance", None)

    if shared_flag or (not instance_arg and not os.getenv("QUAID_INSTANCE", "").strip()):
        return home / "shared" / "config" / "memory.json", "shared (machine-wide)"

    instance_id = instance_arg or os.getenv("QUAID_INSTANCE", "").strip()
    return home / instance_id / "config" / "memory.json", f"instance '{instance_id}'"


def _config_path(args: argparse.Namespace | None = None) -> Path:
    path, _ = _resolve_config_target(args)
    return path


def _load_config(path: Path, allow_missing: bool = False) -> dict[str, Any]:
    if not path.exists():
        if allow_missing:
            return {}
        raise FileNotFoundError(f"Config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def _run_config_callbacks_after_save() -> None:
    """Apply config callbacks (domain sync/contract checks) after writing config."""
    from config import reload_config
    reload_config()


def _active_plugin_ids(data: dict[str, Any]) -> list[str]:
    slots = _get(data, "plugins.slots", {})
    if not isinstance(slots, dict):
        return []
    ids: list[str] = []
    adapter = str(slots.get("adapter", "")).strip()
    if adapter:
        ids.append(adapter)
    ingest = slots.get("ingest", [])
    if isinstance(ingest, list):
        ids.extend(str(v).strip() for v in ingest if str(v).strip())
    stores = slots.get("dataStores", slots.get("datastores", slots.get("data_stores", [])))
    if isinstance(stores, list):
        ids.extend(str(v).strip() for v in stores if str(v).strip())
    out: list[str] = []
    seen = set()
    for pid in ids:
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
    return out


def _discover_plugin_manifests(path: Path, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    try:
        from core.runtime.plugins import discover_plugin_manifests
    except Exception:
        return {}
    plugin_paths = _get(data, "plugins.paths", [])
    if not isinstance(plugin_paths, list):
        plugin_paths = []
    if not plugin_paths:
        plugin_paths = ["plugins"]
    allowlist = _get(data, "plugins.allowList", _get(data, "plugins.allowlist", []))
    if not isinstance(allowlist, list):
        allowlist = []
    strict = bool(_get(data, "plugins.strict", True))
    try:
        manifests, _errors = discover_plugin_manifests(
            paths=[str(v) for v in plugin_paths],
            allowlist=[str(v) for v in allowlist if str(v).strip()],
            strict=strict,
            workspace_root=path.parent.parent,
        )
    except Exception:
        if strict:
            raise
        return {}
    return {
        m.plugin_id: {
            "plugin_id": m.plugin_id,
            "plugin_type": m.plugin_type,
            "module": m.module,
            "capabilities": dict(m.capabilities or {}),
        }
        for m in manifests
    }


def _coerce_prompt_value(raw: str, field_type: str) -> Any:
    value = raw.strip()
    if field_type == "boolean":
        return value.lower() in {"1", "true", "yes", "on"}
    if field_type == "integer":
        return int(value)
    if field_type == "number":
        return float(value)
    if field_type in {"object", "array", "json"}:
        parsed = json.loads(value)
        if field_type == "object" and not isinstance(parsed, dict):
            raise ValueError("Expected JSON object")
        if field_type == "array" and not isinstance(parsed, list):
            raise ValueError("Expected JSON array")
        return parsed
    return value


def _plugin_config_get(staged: dict[str, Any], plugin_id: str, default: Any = None) -> Any:
    return _get(staged, ["plugins", "config", plugin_id], default)


def _plugin_config_set(staged: dict[str, Any], plugin_id: str, value: Any) -> None:
    _set(staged, ["plugins", "config", plugin_id], value)


def _edit_plugin_config_schema(staged: dict[str, Any], path: Path, plugin_id: str, schema: dict[str, Any]) -> None:
    current = _plugin_config_get(staged, plugin_id, {})
    if not isinstance(current, dict):
        current = {}
    fields = schema.get("fields", [])
    if isinstance(fields, dict):
        fields = [{"key": k, **(v if isinstance(v, dict) else {})} for k, v in fields.items()]
    if not isinstance(fields, list):
        fields = []
    if not fields:
        print(f"No schema fields for {plugin_id}, falling back to JSON editor.")
        _edit_plugin_config_json(staged, plugin_id)
        return
    updated = dict(current)
    for field in fields:
        if not isinstance(field, dict):
            continue
        key = str(field.get("key", "")).strip()
        if not key:
            continue
        label = str(field.get("label", key)).strip() or key
        desc = str(field.get("description", "")).strip()
        field_type = str(field.get("type", "string")).strip().lower()
        default = field.get("default")
        enum_vals = field.get("enum", [])
        cur = updated.get(key, default)
        if desc:
            print(f"{label}: {desc}")
        if isinstance(enum_vals, list) and enum_vals:
            print(f"Allowed: {enum_vals}")
        raw = input(f"{plugin_id}.{key} [{json.dumps(cur)}]: ").strip()
        if not raw:
            continue
        try:
            coerced = _coerce_prompt_value(raw, field_type)
        except ValueError as err:
            print(f"Invalid value for {plugin_id}.{key}: {err} (keeping previous value)")
            continue
        if isinstance(enum_vals, list) and enum_vals and coerced not in enum_vals:
            print(
                f"Invalid value for {plugin_id}.{key}: must be one of {enum_vals} "
                "(keeping previous value)"
            )
            continue
        updated[key] = coerced
    _plugin_config_set(staged, plugin_id, updated)


def _edit_plugin_config_json(staged: dict[str, Any], plugin_id: str) -> None:
    current = _plugin_config_get(staged, plugin_id, {})
    print("Current plugin config:")
    print(json.dumps(current, indent=2))
    raw = input("New JSON object (blank to cancel): ").strip()
    if not raw:
        print("Cancelled")
        return
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Plugin config must be a JSON object")
    _plugin_config_set(staged, plugin_id, parsed)


def _segments(path: str | list[str]) -> list[str]:
    if isinstance(path, list):
        return [str(v) for v in path]
    return str(path).split(".")


def _get(data: dict[str, Any], dotted: str | list[str], default: Any = None) -> Any:
    cur: Any = data
    for seg in _segments(dotted):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


def _set(data: dict[str, Any], dotted: str | list[str], value: Any) -> None:
    parts = _segments(dotted)
    cur: Any = data
    for seg in parts[:-1]:
        if not isinstance(cur, dict):
            raise ValueError(f"Cannot set {dotted}: parent is not an object")
        if seg not in cur:
            cur[seg] = {}
        elif not isinstance(cur[seg], dict):
            raise ValueError(f"Cannot set {dotted}: intermediate path '{seg}' is not an object")
        cur = cur[seg]
    if not isinstance(cur, dict):
        raise ValueError(f"Cannot set {dotted}: parent is not an object")
    cur[parts[-1]] = value


def _print_summary(path: Path, data: dict[str, Any], label: str = "") -> None:
    header = f"Quaid Configuration — {label}" if label else "Quaid Configuration"
    print(header)
    print(str(path))
    print()

    print(f"provider:         {_get(data, 'models.llm_provider', _get(data, 'models.llmProvider', 'default'))}")
    print(f"deep reasoning:   {_get(data, 'models.deep_reasoning', _get(data, 'models.deepReasoning', 'default'))}")
    print(f"fast reasoning:    {_get(data, 'models.fast_reasoning', _get(data, 'models.fastReasoning', 'default'))}")
    print(f"embeddings model: {_get(data, 'ollama.embeddingModel', _get(data, 'ollama.embedding_model', 'qwen3-embedding:8b'))}")
    print(f"notify level:     {_get(data, 'notifications.level', 'normal')}")
    print(f"fail hard:        {_get(data, 'retrieval.fail_hard', _get(data, 'retrieval.failHard', True))}")
    print(f"identity mode:    {_get(data, 'identity.mode', 'single_user')}")
    print(f"strict privacy:   {_get(data, 'privacy.enforceStrictFilters', _get(data, 'privacy.enforce_strict_filters', True))}")
    print(f"core parallel:    {_get(data, 'core.parallel.enabled', True)}")
    print(f"llm workers:      {_get(data, 'core.parallel.llmWorkers', _get(data, 'core.parallel.llm_workers', 4))}")
    print(f"embedding workers:{_get(data, 'core.parallel.embeddingWorkers', _get(data, 'core.parallel.embedding_workers', 6))}")
    print(f"idle timeout:     {_get(data, 'capture.inactivity_timeout_minutes', _get(data, 'capture.inactivityTimeoutMinutes', 60))}m")
    print(f"plugin adapter:   {_get(data, 'plugins.slots.adapter', '(none)')}")
    print(f"plugin ingest:    {_get(data, 'plugins.slots.ingest', [])}")
    print(f"plugin stores:    {_get(data, 'plugins.slots.dataStores', _get(data, 'plugins.slots.datastores', []))}")
    print(f"plugin config ids:{list((_get(data, 'plugins.config', {}) or {}).keys())}")
    print()
    print("systems:")
    for key in ("memory", "journal", "projects", "workspace"):
        enabled = bool(_get(data, f"systems.{key}", True))
        print(f"  {key:<10} {'on' if enabled else 'off'}")


def _prompt_str(label: str, current: str) -> str:
    value = input(f"{label} [{current}]: ").strip()
    return value if value else current


def _prompt_int(label: str, current: int) -> int:
    raw = input(f"{label} [{current}]: ").strip()
    if not raw:
        return current
    return int(raw)


def _model_key_path(staged: dict[str, Any], snake: str, camel: str) -> str:
    models = _get(staged, "models", {})
    if isinstance(models, dict):
        if snake in models:
            return f"models.{snake}"
        if camel in models:
            return f"models.{camel}"
    return f"models.{snake}"


def _edit_plugin_config(staged: dict[str, Any], path: Path) -> None:
    active = _active_plugin_ids(staged)
    if active:
        print(f"Active plugins: {active}")
    manifests = _discover_plugin_manifests(path, staged)
    plugin_id = input("Plugin id (e.g. memorydb.core): ").strip()
    if not plugin_id:
        print("No plugin id provided")
        return
    manifest = manifests.get(plugin_id, {})
    capabilities = manifest.get("capabilities", {}) if isinstance(manifest, dict) else {}
    contract = capabilities.get("contract", {}) if isinstance(capabilities, dict) else {}
    cfg_spec = contract.get("config", {}) if isinstance(contract, dict) else {}
    schema = cfg_spec.get("schema", {}) if isinstance(cfg_spec, dict) else {}
    if isinstance(schema, dict) and schema:
        _edit_plugin_config_schema(staged, path, plugin_id, schema)
        return
    _edit_plugin_config_json(staged, plugin_id)


def interactive_edit(path: Path, data: dict[str, Any]) -> bool:
    staged = json.loads(json.dumps(data))

    while True:
        print("\nQuaid Config Editor")
        print("1. LLM provider")
        print("2. Deep reasoning model")
        print("3. Fast reasoning model")
        print("4. Embeddings model")
        print("5. Notification level")
        print("6. Idle timeout (minutes)")
        print("7. Fail hard (retrieval.fail_hard)")
        print("8. Core parallel enabled")
        print("9. Core LLM workers")
        print("10. Core embedding workers")
        print("11. Edit plugin config JSON")
        print("12. Show summary")
        print("13. Save and exit")
        print("0. Exit without saving")
        choice = input("Select: ").strip()

        try:
            if choice == "1":
                key = _model_key_path(staged, "llm_provider", "llmProvider")
                cur = str(_get(staged, key, "default"))
                _set(staged, key, _prompt_str(key, cur))
            elif choice == "2":
                key = _model_key_path(staged, "deep_reasoning", "deepReasoning")
                cur = str(_get(staged, key, "default"))
                _set(staged, key, _prompt_str(key, cur))
            elif choice == "3":
                key = _model_key_path(staged, "fast_reasoning", "fastReasoning")
                cur = str(_get(staged, key, "default"))
                _set(staged, key, _prompt_str(key, cur))
            elif choice == "4":
                cur = str(_get(staged, "ollama.embeddingModel", _get(staged, "ollama.embedding_model", "qwen3-embedding:8b")))
                _set(staged, "ollama.embeddingModel", _prompt_str("ollama.embeddingModel", cur))
            elif choice == "5":
                cur = str(_get(staged, "notifications.level", "normal"))
                _set(staged, "notifications.level", _prompt_str("notifications.level", cur))
            elif choice == "6":
                cur = int(_get(staged, "capture.inactivity_timeout_minutes", _get(staged, "capture.inactivityTimeoutMinutes", 60)))
                _set(staged, "capture.inactivity_timeout_minutes", _prompt_int("capture.inactivity_timeout_minutes", cur))
            elif choice == "7":
                cur = bool(_get(staged, "retrieval.fail_hard", _get(staged, "retrieval.failHard", True)))
                raw = _prompt_str("retrieval.fail_hard (true/false)", "true" if cur else "false").lower()
                val = raw in {"1", "true", "yes", "on"}
                _set(staged, "retrieval.fail_hard", val)
                _set(staged, "retrieval.failHard", val)
            elif choice == "8":
                cur = bool(_get(staged, "core.parallel.enabled", True))
                raw = _prompt_str("core.parallel.enabled (true/false)", "true" if cur else "false").lower()
                _set(staged, "core.parallel.enabled", raw in {"1", "true", "yes", "on"})
            elif choice == "9":
                cur = int(_get(staged, "core.parallel.llmWorkers", _get(staged, "core.parallel.llm_workers", 4)))
                _set(staged, "core.parallel.llmWorkers", _prompt_int("core.parallel.llmWorkers", cur))
            elif choice == "10":
                cur = int(_get(staged, "core.parallel.embeddingWorkers", _get(staged, "core.parallel.embedding_workers", 6)))
                _set(staged, "core.parallel.embeddingWorkers", _prompt_int("core.parallel.embeddingWorkers", cur))
            elif choice == "11":
                _edit_plugin_config(staged, path)
            elif choice == "12":
                _print_summary(path, staged)
            elif choice == "13":
                _save_config(path, staged)
                _run_config_callbacks_after_save()
                print(f"Saved: {path}")
                return True
            elif choice == "0":
                print("No changes saved")
                return False
            else:
                print("Invalid choice")
        except (ValueError, RuntimeError) as err:
            print(f"Invalid value: {err}")


def parse_literal(raw: str) -> Any:
    value = raw.strip()
    if value.lower() == "null":
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        if value.startswith("{") or value.startswith("["):
            return json.loads(value)
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
            return float(value)
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
    except ValueError:
        pass
    return value


def _add_target_args(p: argparse.ArgumentParser) -> None:
    """Add --shared / --instance targeting flags to a subparser."""
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--shared", action="store_true",
                     help="Target the machine-wide shared config (embeddings, ollama)")
    grp.add_argument("--instance", metavar="ID",
                     help="Target a specific instance config")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Quaid config helper",
        epilog=(
            "Target flags (mutually exclusive): --shared targets QUAID_HOME/shared/config/memory.json; "
            "--instance <id> targets QUAID_HOME/<id>/config/memory.json. "
            "When neither is given, QUAID_INSTANCE env is used, else shared is the default."
        ),
    )
    sub = parser.add_subparsers(dest="cmd")

    show_p = sub.add_parser("show", help="Show summary")
    _add_target_args(show_p)

    path_p = sub.add_parser("path", help="Print config path")
    _add_target_args(path_p)

    edit_p = sub.add_parser("edit", help="Interactive config editor")
    _add_target_args(edit_p)

    set_p = sub.add_parser("set", help="Set a dotted key path")
    set_p.add_argument("key", help="Dotted path (e.g. models.fastReasoning)")
    set_p.add_argument("value", help="Value (string/number/true/false/json)")
    _add_target_args(set_p)

    auth_p = sub.add_parser("set-auth", help="Store a long-lived auth token for the active adapter")
    auth_p.add_argument("token", nargs="?", help="Token value (omit to read from stdin)")

    args = parser.parse_args()
    cmd = args.cmd or "show"

    cfg_path, cfg_label = _resolve_config_target(args)

    if cmd == "path":
        print(str(cfg_path))
        return 0

    allow_missing = getattr(args, "shared", False) or (
        not getattr(args, "instance", None) and not os.getenv("QUAID_INSTANCE", "").strip()
    )
    try:
        data = _load_config(cfg_path, allow_missing=allow_missing)
    except Exception as err:
        print(str(err))
        return 1

    if cmd == "show":
        _print_summary(cfg_path, data, label=cfg_label)
        return 0

    if cmd == "edit":
        interactive_edit(cfg_path, data)
        return 0

    if cmd == "set":
        try:
            _set(data, args.key, parse_literal(args.value))
            _save_config(cfg_path, data)
            _run_config_callbacks_after_save()
        except Exception as err:
            print(f"Failed to set {args.key}: {err}")
            return 1
        print(f"Set {args.key} in {cfg_path}")
        return 0

    if cmd == "set-auth":
        token = args.token
        if not token:
            import sys as _sys
            token = _sys.stdin.read().strip()
        if not token:
            print("No token provided. Usage: quaid config set-auth <token>")
            return 1
        try:
            from lib.adapter import get_adapter
            adapter = get_adapter()
            stored_path = adapter.store_auth_token(token)
            print(f"Auth token stored at {stored_path}")
        except ValueError as err:
            print(f"Error: {err}")
            return 1
        except Exception as err:
            print(f"Failed to store auth token: {err}")
            return 1
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
