"""Shared configuration constants — single source of truth.

All hardcoded DB paths, Ollama URLs, embedding params, etc. are centralized here.
Consumers import from lib.config instead of defining their own constants.

Environment variable overrides (for testing):
  MEMORY_DB_PATH       — overrides config database.path
  MEMORY_ARCHIVE_DB_PATH — overrides config database.archivePath
  OLLAMA_URL           — overrides config ollama.url
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List


logger = logging.getLogger(__name__)


def _workspace_root() -> Path:
    """Get hidden workspace root, preferring env home when no instance is active."""
    env_home = os.environ.get("QUAID_HOME", "").strip()
    env_instance = os.environ.get("QUAID_INSTANCE", "").strip()
    if env_home and not env_instance:
        return Path(env_home).expanduser().resolve()
    from lib.adapter import get_adapter
    adapter = get_adapter()
    instance_root = getattr(adapter, "instance_root", None)
    if not callable(instance_root):
        if env_home and env_instance:
            logger.warning(
                "Adapter %s lacks instance_root(); falling back to QUAID_HOME/instances/QUAID_INSTANCE",
                type(adapter).__name__,
            )
            return Path(env_home).expanduser().resolve() / "instances" / env_instance
        raise TypeError(
            f"Adapter instance_root() must be callable, got {type(adapter).__name__}"
        )
    root = instance_root()
    if isinstance(root, Path):
        return root
    if isinstance(root, os.PathLike):
        return Path(root)
    if isinstance(root, str):
        return Path(root)
    raise TypeError(
        f"Adapter instance_root() must return a path-like value, got {type(root).__name__}"
    )


def _get_cfg():
    """Lazy import to avoid circular dependency with config.py."""
    from config import get_config
    return get_config()


def _camel_to_snake(camel_str: str) -> str:
    result = []
    for idx, char in enumerate(str(camel_str or "")):
        if char.isupper() and idx > 0:
            result.append("_")
        result.append(char.lower())
    return "".join(result)


def _normalize_config_keys(value: Any) -> Any:
    """Normalize JSON keys without importing the full plugin-aware config loader."""
    if isinstance(value, dict):
        return {_camel_to_snake(k): _normalize_config_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_config_keys(v) for v in value]
    return value


def _deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _platform_from_instance_name_no_manifest(instance_name: str) -> str:
    name = str(instance_name or "").strip().lower().replace("_", "-")
    parts = [part for part in name.split("-") if part]
    if len(parts) >= 2 and parts[:2] == ["claude", "code"]:
        return "-".join(parts[:2])
    if name == "".join(["claude", "code"]):
        return "-".join(["claude", "code"])
    if name.startswith("standalone-") or name == "standalone":
        return "standalone"
    if "-" in name:
        return name.split("-", 1)[0] or "standalone"
    return name or "standalone"


def _platform_from_instance_name(instance_name: str) -> str:
    name = str(instance_name or "").strip().lower().replace("_", "-")
    compact_name = name.replace("-", "")
    for adapter_id, prefix in _adapter_prefix_rows():
        compact_adapter = adapter_id.replace("-", "")
        if name == adapter_id or compact_name == compact_adapter:
            return adapter_id
        if name == prefix or name.startswith(f"{prefix}-"):
            return adapter_id
    if name.startswith("standalone-") or name == "standalone":
        return "standalone"
    if "-" in name:
        return name.split("-", 1)[0] or "standalone"
    return name or "standalone"


def _adapter_prefix_rows() -> List[tuple[str, str]]:
    rows: List[tuple[str, str]] = []
    manifest_root = Path(__file__).resolve().parent.parent / "adaptors" / "manifests"
    try:
        manifests = sorted(manifest_root.glob("*.json"))
    except Exception as exc:
        logger.warning("Failed to list adapter manifests from %s: %s", manifest_root, exc)
        if _fail_hard_from_raw_lightweight_config():
            raise RuntimeError(f"Failed to list adapter manifests from {manifest_root}") from exc
        return rows
    for manifest_path in manifests:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load adapter manifest %s: %s", manifest_path, exc)
            if _fail_hard_from_raw_lightweight_config():
                raise RuntimeError(f"Failed to load adapter manifest {manifest_path}") from exc
            continue
        adapter_id = str(data.get("id") or manifest_path.stem).strip().lower()
        runtime = data.get("runtime") if isinstance(data, dict) else {}
        prefix = ""
        if isinstance(runtime, dict):
            prefix = str(runtime.get("instancePrefix") or "").strip().lower()
        prefix = prefix[:-1] if prefix.endswith("-") else prefix
        if adapter_id and prefix:
            rows.append((adapter_id, prefix))
    rows.sort(key=lambda item: len(item[1]), reverse=True)
    return rows


def _fail_hard_from_raw_lightweight_config() -> bool:
    """Read failHard directly without calling lib.fail_policy and recursing."""
    data: Dict[str, Any] = {}
    try:
        from lib.instance import quaid_home

        home = quaid_home()
        instance = os.environ.get("QUAID_INSTANCE", "").strip()
        explicit = os.environ.get("QUAID_ADAPTER_TYPE", "").strip()
        adapter_type = _adapter_type_from_instance_config(home, instance) if not explicit else ""
        platform = _platform_from_instance_name_no_manifest(explicit or adapter_type or instance)
        paths: List[Path] = []
        if instance:
            paths.append(home / "instances" / instance / "config.json")
        paths.append(home / "shared" / "config" / platform / "config.json")
        paths.append(home / "shared" / "config" / "global" / "config.json")
        for config_path in reversed(paths):
            if not config_path.is_file():
                continue
            try:
                parsed = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(parsed, dict):
                data = _deep_merge_dicts(data, _normalize_config_keys(parsed))
    except Exception:
        return True
    retrieval = data.get("retrieval", {})
    if not isinstance(retrieval, dict):
        return True
    if "fail_hard" in retrieval:
        return bool(retrieval.get("fail_hard"))
    return True


def _adapter_type_from_instance_config(home: Path, instance: str) -> str:
    instance_id = str(instance or "").strip()
    if not instance_id:
        return ""
    try:
        payload = json.loads(
            (home / "instances" / instance_id / "config.json").read_text(encoding="utf-8")
        )
    except Exception:
        return ""
    adapter = payload.get("adapter") if isinstance(payload, dict) else None
    if not isinstance(adapter, dict):
        return ""
    return str(adapter.get("type") or "").strip()


def _lightweight_platform_id(instance: str, home: Path | None = None) -> str:
    explicit = os.environ.get("QUAID_ADAPTER_TYPE", "").strip().lower()
    if explicit:
        return _platform_from_instance_name(explicit)
    if home is not None:
        adapter_type = _adapter_type_from_instance_config(home, instance)
        if adapter_type:
            return _platform_from_instance_name(adapter_type)
    return _platform_from_instance_name(instance)


def _lightweight_config_paths() -> List[Path]:
    """Return raw config layers without loading adapters or plugins.

    The full config loader initializes plugin runtime, which is too expensive
    for hook-time embedding setup. This mirrors config._config_paths() for the
    specific lightweight settings in this module.
    """
    from lib.instance import quaid_home

    home = quaid_home()
    instance = os.environ.get("QUAID_INSTANCE", "").strip()
    platform = _lightweight_platform_id(instance, home)
    paths: List[Path] = []
    if instance:
        paths.append(home / "instances" / instance / "config.json")
    paths.append(home / "shared" / "config" / platform / "config.json")
    paths.append(home / "shared" / "config" / "global" / "config.json")
    return paths


def _load_lightweight_config() -> Dict[str, Any]:
    raw_config: Dict[str, Any] = {}
    for config_path in reversed(_lightweight_config_paths()):
        if not config_path.is_file():
            continue
        try:
            parsed = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            from lib.fail_policy import is_fail_hard_enabled

            if is_fail_hard_enabled():
                raise
            logger.warning("Failed to parse lightweight config %s: %s", config_path, exc)
            continue
        except OSError as exc:
            from lib.fail_policy import is_fail_hard_enabled

            if is_fail_hard_enabled():
                raise
            logger.warning("Failed to read lightweight config %s: %s", config_path, exc)
            continue
        if isinstance(parsed, dict):
            raw_config = _deep_merge_dicts(raw_config, _normalize_config_keys(parsed))
    return raw_config


def _section_value(section: str, key: str, default: Any = None) -> Any:
    data = _load_lightweight_config()
    section_data = data.get(section, {})
    if not isinstance(section_data, dict):
        return default
    return section_data.get(key, default)


def _cross_instance_override_owner(path: Path) -> str | None:
    """Return the instance name when an override points at another instance DB."""
    instance = os.environ.get("QUAID_INSTANCE", "").strip()
    home = os.environ.get("QUAID_HOME", "").strip()
    if not instance or not home or str(path) == ":memory:":
        return None

    try:
        rel = path.expanduser().resolve(strict=False).relative_to(
            (Path(home).expanduser() / "instances").resolve(strict=False)
        )
    except (OSError, ValueError):
        return None

    parts = rel.parts
    if len(parts) >= 3 and parts[1] == "data" and parts[2] in {
        "memory.db",
        "memory.sqlite",
        "memory.sqlite3",
        "session.db",
        "session.sqlite",
        "session.sqlite3",
        "memory_archive.db",
        "memory_archive.sqlite",
        "memory_archive.sqlite3",
    }:
        owner = parts[0]
        if owner != instance:
            return owner
    return None


def _validated_memory_override(env_name: str) -> Path | None:
    raw = os.environ.get(env_name)
    if not raw:
        return None
    path = Path(raw).expanduser()
    owner = _cross_instance_override_owner(path)
    if owner is not None:
        instance = os.environ.get("QUAID_INSTANCE", "").strip()
        raise RuntimeError(
            f"{env_name} points at instance {owner!r} while QUAID_INSTANCE is "
            f"{instance!r}; refusing cross-instance memory database path {path}"
        )
    return path


def get_db_path() -> Path:
    """Get the main memory database path.

    Respects MEMORY_DB_PATH env var for testing, then falls back to config.
    """
    env_path = _validated_memory_override("MEMORY_DB_PATH")
    if env_path is not None:
        return env_path
    cfg = _get_cfg()
    p = Path(str(cfg.database.path)).expanduser()
    return p if p.is_absolute() else _workspace_root() / p


def get_db_path_lightweight() -> Path:
    """Get the main memory DB path without initializing adapters or plugins."""
    env_path = _validated_memory_override("MEMORY_DB_PATH")
    if env_path is not None:
        return env_path
    raw = str(_section_value("database", "path", "data/memory.db") or "data/memory.db").strip()
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    from lib.instance import instance_root

    return instance_root() / p


def get_session_db_path() -> Path:
    """Get the session datastore database path.

    SessionDB is a separate datastore from MemoryDB. Test setups that redirect
    MEMORY_DB_PATH get a sibling session.db by default so isolated test homes do
    not leak session rows into the developer instance.
    """
    env_path = _validated_memory_override("SESSION_DB_PATH")
    if env_path is not None:
        return env_path

    memory_override = _validated_memory_override("MEMORY_DB_PATH")
    if memory_override is not None:
        return memory_override.with_name("session.db")

    cfg = _get_cfg()
    raw = str(getattr(getattr(cfg, "database", None), "session_path", "") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p if p.is_absolute() else _workspace_root() / p
    return _workspace_root() / "data" / "session.db"


def get_archive_db_path() -> Path:
    """Get the archive database path.

    Respects MEMORY_ARCHIVE_DB_PATH env var for testing, then falls back to config.
    """
    env_path = _validated_memory_override("MEMORY_ARCHIVE_DB_PATH")
    if env_path is not None:
        return env_path
    cfg = _get_cfg()
    p = Path(str(cfg.database.archive_path)).expanduser()
    return p if p.is_absolute() else _workspace_root() / p


def get_docs_db_path() -> Path:
    """Get the shared docs database path.

    Docs RAG/index state is shared across instances to avoid per-instance
    reindex churn. Relative paths resolve from QUAID_HOME.
    """
    env_path = os.environ.get("DOCS_DB_PATH")
    if env_path:
        return Path(env_path).expanduser()

    # Test/override compatibility: when memory DB is explicitly redirected,
    # keep docs DB co-located unless DOCS_DB_PATH is also set.
    memory_override = _validated_memory_override("MEMORY_DB_PATH")
    if memory_override is not None:
        return memory_override.with_name("docs.db")

    cfg = _get_cfg()
    raw = str(getattr(getattr(cfg, "database", None), "docs_path", "") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
        from lib.instance import quaid_home as _quaid_home

        return _quaid_home() / p

    from lib.instance import quaid_home as _quaid_home

    return _quaid_home() / "shared" / "data" / "docs.db"


def get_ollama_url() -> str:
    """Get the Ollama API URL.

    Respects OLLAMA_URL env var, then falls back to raw config.
    """
    env_url = os.environ.get("OLLAMA_URL")
    if env_url:
        return env_url
    return str(_section_value("ollama", "url", "http://localhost:11434") or "http://localhost:11434")


def get_embedding_model() -> str:
    """Get the Ollama embedding model name."""
    return str(_section_value("ollama", "embedding_model", "nomic-embed-text") or "nomic-embed-text")


def get_embedding_dim() -> int:
    """Get the embedding vector dimension."""
    raw = _section_value("ollama", "embedding_dim", 768)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 768


def get_embeddings_provider_id() -> str:
    """Get the configured embeddings provider id without plugin initialization."""
    raw = _section_value("models", "embeddings_provider", "ollama")
    return str(raw or "ollama").strip().lower()


def get_retrieval_lightweight_config() -> SimpleNamespace:
    """Return retrieval settings without initializing adapters or plugins."""
    data = _load_lightweight_config()
    retrieval = data.get("retrieval", {})
    if not isinstance(retrieval, dict):
        retrieval = {}
    return SimpleNamespace(**retrieval)


def get_injection_timeout_ms(default: int = 3000) -> int:
    """Return retrieval.injection_timeout_ms without loading full config."""
    raw = getattr(get_retrieval_lightweight_config(), "injection_timeout_ms", default)
    try:
        return int(raw if raw is not None else default)
    except (TypeError, ValueError):
        return int(default)


def get_retrieval_rrf_k(default: int = 60) -> int:
    """Return retrieval.rrf_k without loading full config."""
    raw = getattr(get_retrieval_lightweight_config(), "rrf_k", default)
    try:
        return max(1, int(raw if raw is not None else default))
    except (TypeError, ValueError):
        return max(1, int(default))
