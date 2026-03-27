#!/usr/bin/env python3
"""Apply a runtime profile to OpenClaw + Quaid non-interactively.

Usage:
  python3 scripts/runtime/apply-runtime-profile.py \
    --profile config/runtime-profile.local.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _deep_merge_dict(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge_dict(dst[key], value)
        else:
            dst[key] = value
    return dst


def _code_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resolve_path(value: str, base_dir: Path) -> str:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    return str((base_dir / candidate).resolve())


def _resolve_config_path(raw: str, base_dir: Path) -> Path:
    return Path(_resolve_path(raw, base_dir))


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value:
        text = str(entry).strip()
        if text:
            out.append(text)
    return out


def _load_local_config(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.exists():
        return {}
    data = _load_json(path)
    return data if isinstance(data, dict) else {}


def _resolve_dev_root(local_cfg: Dict[str, Any], code_root: Path) -> Path:
    paths_cfg = local_cfg.get("paths")
    if isinstance(paths_cfg, dict):
        raw = paths_cfg.get("devRoot")
        if isinstance(raw, str) and raw.strip():
            return Path(_resolve_path(raw.strip(), code_root))
    return code_root


def _apply_local_dev_overrides(profile: Dict[str, Any], local_cfg: Dict[str, Any]) -> None:
    if not local_cfg:
        return

    paths_cfg = local_cfg.get("paths") if isinstance(local_cfg.get("paths"), dict) else {}
    identity_cfg = local_cfg.get("identity") if isinstance(local_cfg.get("identity"), dict) else {}

    runtime_workspace = str(paths_cfg.get("runtimeWorkspace", "")).strip()
    if runtime_workspace:
        profile.setdefault("runtime", {})["workspace"] = runtime_workspace
        openclaw_cfg = profile.setdefault("openclaw", {})
        openclaw_cfg.setdefault("agentDefaults", {})["workspace"] = runtime_workspace
        for agent_cfg in openclaw_cfg.get("agentList", []):
            if isinstance(agent_cfg, dict):
                agent_cfg["workspace"] = runtime_workspace
        profile.setdefault("quaid", {})["configPath"] = str(Path(runtime_workspace) / "config" / "memory.json")
        profile.setdefault("secrets", {})["writeEnvFile"] = str(Path(runtime_workspace) / ".env")

    owner_id = str(identity_cfg.get("defaultOwnerId", "operator")).strip() or "operator"
    speakers = _normalize_string_list(identity_cfg.get("speakers"))
    person_node_name = str(identity_cfg.get("personNodeName", "")).strip()
    if not person_node_name:
        person_node_name = speakers[0] if speakers else "Test Operator"
    if not speakers:
        speakers = [person_node_name]
    user_summary = str(
        identity_cfg.get(
            "userSummary",
            f"Primary operator: {person_node_name}. The operator is a software developer.",
        )
    ).strip()
    telegram_allow = _normalize_string_list(identity_cfg.get("telegramAllowFrom"))

    profile.setdefault("runtime", {}).setdefault("coreMarkdown", {})["USER.md"] = f"# User\n\n{user_summary}\n"

    openclaw_cfg = profile.setdefault("openclaw", {})
    openclaw_cfg.setdefault("telegram", {})
    if telegram_allow:
        openclaw_cfg["telegram"]["allowFrom"] = telegram_allow

    quaid_cfg = profile.setdefault("quaid", {})
    users_cfg = quaid_cfg.setdefault("users", {})
    users_cfg["defaultOwner"] = owner_id
    channels: Dict[str, Any] = {"cli": ["*"]}
    if telegram_allow:
        channels["telegram"] = telegram_allow
    identity_entry: Dict[str, Any] = {
        "channels": channels,
        "speakers": speakers,
        "personNodeName": person_node_name,
    }
    users_cfg["identities"] = {owner_id: identity_entry}


def _provider_from_profile(profile_id: str, profile: Dict[str, Any]) -> str:
    provider = profile.get("provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    return profile_id.split(":", 1)[0]


def _normalize_mode(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    mode = value.strip().lower()
    if mode == "api_key":
        return "token"
    return mode


def _resolve_auth_selector(auth_provider: Optional[str], auth_path: Optional[str]) -> Dict[str, Optional[str]]:
    if auth_path:
        mapping = {
            "openai-oauth": {"family": "openai", "mode": "oauth", "path": "openai-oauth"},
            "openai-api": {"family": "openai", "mode": "token", "path": "openai-api"},
            "anthropic-oauth": {"family": "anthropic", "mode": "oauth", "path": "anthropic-oauth"},
            "anthropic-api": {"family": "anthropic", "mode": "token", "path": "anthropic-api"},
        }
        return mapping[auth_path]
    if auth_provider == "openai":
        return {"family": "openai", "mode": None, "path": "openai-any"}
    if auth_provider == "anthropic":
        return {"family": "anthropic", "mode": None, "path": "anthropic-any"}
    return {"family": None, "mode": None, "path": "any"}


def _provider_matches_family(provider: str, family: Optional[str]) -> bool:
    if family is None:
        return True
    if family == "openai":
        return provider in {"openai", "openai-codex"}
    return provider == family


def _filter_profiles(profiles: Dict[str, Any], selector: Dict[str, Optional[str]]) -> Dict[str, Any]:
    family = selector.get("family")
    mode = selector.get("mode")
    path = str(selector.get("path") or "")
    filtered: Dict[str, Any] = {}
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        provider = _provider_from_profile(str(profile_id), profile)
        if not _provider_matches_family(provider, family):
            continue
        profile_key = str(profile_id).lower()
        profile_mode = _normalize_mode(profile.get("mode", profile.get("type")))
        if mode is not None:
            mode_ok = profile_mode == mode
            # Anthropic "oauth" is often represented as token/manual profile naming.
            if not mode_ok and path == "anthropic-oauth":
                mode_ok = (
                    profile_key.endswith(":manual")
                    or "oauth" in profile_key
                    or profile_key.endswith(":claude-cli")
                )
            if mode_ok and path == "anthropic-api":
                if (
                    profile_key.endswith(":manual")
                    or "oauth" in profile_key
                    or profile_key.endswith(":claude-cli")
                ):
                    mode_ok = False
            if not mode_ok:
                continue
        filtered[str(profile_id)] = dict(profile)
    return filtered


def _auth_store_path(openclaw_cfg: Dict[str, Any], config_path: Path) -> Path:
    explicit = openclaw_cfg.get("authProfileStorePath")
    if isinstance(explicit, str) and explicit.strip():
        resolved = Path(explicit).expanduser()
        # OpenClaw runtime resolves provider auth from agentDir/auth-profiles.json.
        # If profile points at agents/main/auth-profiles.json, normalize to
        # agents/main/agent/auth-profiles.json to avoid path mismatch flakes.
        if (
            resolved.name == "auth-profiles.json"
            and resolved.parent.name == "main"
            and resolved.parent.parent.name == "agents"
        ):
            return resolved.parent / "agent" / "auth-profiles.json"
        return resolved
    return config_path.parent / "agents" / "main" / "agent" / "auth-profiles.json"


def _ensure_dirs(root: Path, entries: list[str]) -> None:
    for entry in entries:
        (root / entry).mkdir(parents=True, exist_ok=True)


def _apply_runtime(runtime_cfg: Dict[str, Any], dev_root: Path) -> Path:
    workspace = _resolve_config_path(str(runtime_cfg["workspace"]), dev_root)
    workspace.mkdir(parents=True, exist_ok=True)
    _ensure_dirs(workspace, runtime_cfg.get("createDirs", []))
    for filename, content in runtime_cfg.get("coreMarkdown", {}).items():
        out = workspace / filename
        out.write_text(content, encoding="utf-8")
    return workspace


def _apply_openclaw(
    openclaw_cfg: Dict[str, Any],
    workspace: Path,
    selector: Dict[str, Optional[str]],
    selector_label: Optional[str],
    dev_root: Path,
) -> None:
    config_path = _resolve_config_path(
        str(openclaw_cfg.get("configPath", "~/.openclaw/openclaw.json")),
        dev_root,
    )
    existing = _load_json(config_path) if config_path.exists() else {}

    existing.setdefault("agents", {})
    existing["agents"].setdefault("defaults", {})
    existing["agents"].setdefault("list", [])
    existing.setdefault("channels", {})
    existing.setdefault("auth", {})
    existing["auth"].setdefault("profiles", {})
    existing.setdefault("gateway", {})
    existing.setdefault("plugins", {})

    defaults = dict(openclaw_cfg.get("agentDefaults", {}))
    provider_defaults = openclaw_cfg.get("providerDefaults", {})
    family = selector.get("family")
    if (
        family is not None
        and isinstance(provider_defaults, dict)
        and isinstance(provider_defaults.get(family), dict)
    ):
        defaults.update(provider_defaults[family])

    default_workspace = defaults.get("workspace")
    if isinstance(default_workspace, str) and default_workspace.strip():
        resolved_default_workspace = _resolve_path(default_workspace, dev_root)
    else:
        resolved_default_workspace = str(workspace)
    existing["agents"]["defaults"]["workspace"] = resolved_default_workspace
    if defaults.get("modelPrimary"):
        existing["agents"]["defaults"].setdefault("model", {})
        existing["agents"]["defaults"]["model"]["primary"] = defaults["modelPrimary"]
    if defaults.get("modelFallbacks"):
        existing["agents"]["defaults"].setdefault("model", {})
        existing["agents"]["defaults"]["model"]["fallbacks"] = list(defaults["modelFallbacks"])

    profile_agents = openclaw_cfg.get("agentList", [])
    by_id = {
        str(agent.get("id")): agent for agent in existing["agents"].get("list", [])
        if isinstance(agent, dict) and agent.get("id")
    }
    for agent_cfg in profile_agents:
        agent_id = str(agent_cfg["id"])
        merged = by_id.get(agent_id, {"id": agent_id})
        if agent_cfg.get("name"):
            merged["name"] = agent_cfg["name"]
        if agent_cfg.get("workspace"):
            merged["workspace"] = _resolve_path(str(agent_cfg["workspace"]), dev_root)
        else:
            merged["workspace"] = str(workspace)
        if family is not None and defaults.get("modelPrimary"):
            merged.setdefault("model", {})
            merged["model"]["primary"] = defaults["modelPrimary"]
        elif agent_cfg.get("modelPrimary"):
            merged.setdefault("model", {})
            merged["model"]["primary"] = agent_cfg["modelPrimary"]
        if "default" in agent_cfg:
            merged["default"] = bool(agent_cfg["default"])
        by_id[agent_id] = merged
    if by_id:
        existing["agents"]["list"] = list(by_id.values())

    telegram = openclaw_cfg.get("telegram")
    if isinstance(telegram, dict):
        existing["channels"]["telegram"] = telegram

    auth_profiles = openclaw_cfg.get("authProfiles", {})
    selected_profiles: Dict[str, Any] = {}
    if isinstance(auth_profiles, dict):
        selected_profiles = _filter_profiles(auth_profiles, selector)
        # Keep selected provider family deterministic when switching.
        for profile_id, profile in list(existing["auth"]["profiles"].items()):
            if not isinstance(profile, dict):
                continue
            provider = _provider_from_profile(str(profile_id), profile)
            if _provider_matches_family(provider, family):
                existing["auth"]["profiles"].pop(profile_id, None)
        existing["auth"]["profiles"].update(selected_profiles)

    if family is not None and not selected_profiles:
        detail = selector_label or family
        raise SystemExit(
            f"No OpenClaw authProfiles matched selector '{detail}'. "
            "Add matching authProfiles entries in the runtime profile."
        )

    auth_order = openclaw_cfg.get("authOrder", {})
    if isinstance(auth_order, dict):
        existing["auth"].setdefault("order", {})

        # remove stale order entries for selected family
        for provider_key in list(existing["auth"]["order"].keys()):
            if _provider_matches_family(provider_key, family):
                existing["auth"]["order"].pop(provider_key, None)

        selected_order = auth_order
        if family is not None:
            selected_order = {}
            for provider_key, profile_ids in auth_order.items():
                if _provider_matches_family(provider_key, family) and isinstance(profile_ids, list):
                    ids = [pid for pid in profile_ids if pid in selected_profiles]
                    if ids:
                        selected_order[provider_key] = ids
        for provider, profile_ids in selected_order.items():
            if isinstance(profile_ids, list):
                existing["auth"]["order"][provider] = profile_ids

    gateway_cfg = openclaw_cfg.get("gateway", {})
    if isinstance(gateway_cfg, dict):
        # Ensure gateway response APIs are available by default for Quaid LLM passthrough.
        effective_gateway_cfg: Dict[str, Any] = {}
        _deep_merge_dict(
            effective_gateway_cfg,
            {
                "http": {
                    "endpoints": {
                        "responses": {"enabled": True},
                        "chatCompletions": {"enabled": True},
                    }
                }
            },
        )
        _deep_merge_dict(effective_gateway_cfg, gateway_cfg)
        _deep_merge_dict(existing["gateway"], effective_gateway_cfg)

    plugins_cfg = openclaw_cfg.get("plugins")
    if isinstance(plugins_cfg, dict):
        for k in ("allow", "load", "slots", "entries", "installs"):
            if k in plugins_cfg:
                existing["plugins"][k] = plugins_cfg[k]

    commands_cfg = openclaw_cfg.get("commands")
    if isinstance(commands_cfg, dict):
        existing.setdefault("commands", {})
        _deep_merge_dict(existing["commands"], commands_cfg)

    _write_json(config_path, existing)

    auth_profile_credentials = openclaw_cfg.get("authProfileCredentials", {})
    if isinstance(auth_profile_credentials, dict) and auth_profile_credentials:
        auth_store = {"version": 1, "profiles": {}}
        auth_store_path = _auth_store_path(openclaw_cfg, config_path)
        if auth_store_path.exists():
            loaded = _load_json(auth_store_path)
            if isinstance(loaded, dict):
                auth_store.update(loaded)
        auth_store.setdefault("profiles", {})
        if not isinstance(auth_store["profiles"], dict):
            auth_store["profiles"] = {}

        selected_credentials = _filter_profiles(auth_profile_credentials, selector)
        for profile_id, profile in list(auth_store["profiles"].items()):
            if not isinstance(profile, dict):
                continue
            provider = _provider_from_profile(str(profile_id), profile)
            if _provider_matches_family(provider, family):
                auth_store["profiles"].pop(profile_id, None)
        auth_store["profiles"].update(selected_credentials)

        # Reset provider-level runtime auth pointers/counters when switching mode.
        if family is not None:
            canonical_provider = "openai-codex" if family == "openai" else family
            selected_ids = list(selected_credentials.keys())

            last_good = auth_store.get("lastGood")
            if isinstance(last_good, dict):
                if selected_ids:
                    last_good[canonical_provider] = selected_ids[0]
                else:
                    last_good.pop(canonical_provider, None)

            usage_stats = auth_store.get("usageStats")
            if isinstance(usage_stats, dict):
                for key in list(usage_stats.keys()):
                    if isinstance(key, str) and key.startswith(f"{canonical_provider}:"):
                        usage_stats.pop(key, None)

        _write_json(auth_store_path, auth_store)
        os.chmod(auth_store_path, 0o600)


def _apply_quaid(quaid_cfg: Dict[str, Any], dev_root: Path) -> None:
    if not quaid_cfg.get("enabled", True):
        return
    template_path = _resolve_config_path(str(quaid_cfg["templatePath"]), dev_root)
    config_path = _resolve_config_path(str(quaid_cfg["configPath"]), dev_root)

    if template_path.exists():
        config = _load_json(template_path)
    else:
        # Allow self-contained test bootstrap even when no checked-in
        # memory config template exists in the current workspace.
        config = {}
    config.setdefault("adapter", {})
    config["adapter"]["type"] = quaid_cfg.get("adapterType", "openclaw")
    config.setdefault("models", {})
    config.setdefault("retrieval", {})
    config.setdefault("plugins", {})
    # Keep runtime-preflight stable even when a minimal/empty template is used.
    config["retrieval"].setdefault("maxLimit", 20)
    config["models"].setdefault(
        "fastReasoningModelClasses",
        {
            "openai": "gpt-5.1-codex-mini",
            "anthropic": "claude-haiku-4-5",
            "openai-compatible": "gpt-4.1-mini",
        },
    )
    config["models"].setdefault(
        "deepReasoningModelClasses",
        {
            "openai": "gpt-5.3-codex",
            "anthropic": "claude-haiku-4-5",
            "openai-compatible": "gpt-4.1",
        },
    )

    for section in ("models", "ollama", "users", "projects", "notifications", "retrieval"):
        updates = quaid_cfg.get(section)
        if isinstance(updates, dict):
            config.setdefault(section, {})
            config[section].update(updates)

    plugin_cfg = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    if not isinstance(plugin_cfg, dict):
        plugin_cfg = {}
    plugin_paths = plugin_cfg.get("paths")
    normalized_paths = [str(p).strip() for p in plugin_paths] if isinstance(plugin_paths, list) else []
    normalized_paths = [p for p in normalized_paths if p]
    if "modules/quaid" not in normalized_paths:
        normalized_paths.insert(0, "modules/quaid")
    plugin_cfg["paths"] = normalized_paths
    config["plugins"] = plugin_cfg

    models = config.get("models", {})
    provider_model_classes = models.get("providerModelClasses")
    if isinstance(provider_model_classes, list):
        deep_map = dict(models.get("deepReasoningModelClasses") or {})
        fast_map = dict(models.get("fastReasoningModelClasses") or {})
        for entry in provider_model_classes:
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider", "")).strip()
            if not provider:
                continue
            deep = str(entry.get("deepReasoning", "")).strip()
            fast = str(entry.get("fastReasoning", "")).strip()
            if deep:
                deep_map[provider] = deep
            if fast:
                fast_map[provider] = fast
        if deep_map:
            models["deepReasoningModelClasses"] = deep_map
        if fast_map:
            models["fastReasoningModelClasses"] = fast_map

    _write_json(config_path, config)


def _apply_secrets(secrets_cfg: Dict[str, Any], dev_root: Path) -> None:
    env_path = secrets_cfg.get("writeEnvFile")
    env_map = secrets_cfg.get("env", {})
    if not env_path or not isinstance(env_map, dict) or not env_map:
        return
    out_path = _resolve_config_path(str(env_path), dev_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in env_map.items()]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(out_path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        required=True,
        help="Path to runtime profile JSON.",
    )
    parser.add_argument(
        "--auth-provider",
        choices=("anthropic", "openai"),
        default=None,
        help="Legacy selector: provider family to load from profile (anthropic or openai).",
    )
    parser.add_argument(
        "--auth-path",
        choices=("openai-oauth", "openai-api", "anthropic-oauth", "anthropic-api"),
        default=None,
        help="Exact auth path to load (provider + auth mode).",
    )
    parser.add_argument(
        "--local-config",
        default=None,
        help="Optional repo-local dev config JSON (defaults to .quaid-dev.local.json at devRoot if present).",
    )
    args = parser.parse_args()

    selector = _resolve_auth_selector(args.auth_provider, args.auth_path)
    code_root = _code_root()
    profile_path = _resolve_config_path(args.profile, code_root)
    profile = _load_json(profile_path)
    local_config_arg = args.local_config or os.environ.get("QUAID_DEV_LOCAL_CONFIG", "")
    local_config_path = _resolve_config_path(local_config_arg, code_root) if local_config_arg else (code_root / ".quaid-dev.local.json")
    local_cfg = _load_local_config(local_config_path)
    dev_root = _resolve_dev_root(local_cfg, code_root)
    _apply_local_dev_overrides(profile, local_cfg)

    workspace = _apply_runtime(profile["runtime"], dev_root)
    selector_label = args.auth_path or args.auth_provider
    _apply_openclaw(profile["openclaw"], workspace, selector, selector_label, dev_root)
    _apply_quaid(profile["quaid"], dev_root)
    _apply_secrets(profile.get("secrets", {}), dev_root)

    print(f"Applied runtime profile: {profile_path}")
    if local_cfg:
        print(f"Loaded local dev config: {local_config_path}")
    print(f"Dev root: {dev_root}")
    print(f"Workspace: {workspace}")
    provider_msg = selector_label if selector_label else "profile default"
    print("Updated: OpenClaw config, Quaid config, core markdown, optional .env")
    print(f"Auth selector loaded: {provider_msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
