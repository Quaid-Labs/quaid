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
    """Get workspace root from adapter (lazy to avoid circular import at module load)."""
    from lib.adapter import get_adapter
    root = get_adapter().instance_root()
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


def _platform_from_instance_name(instance_name: str) -> str:
    name = str(instance_name or "").strip().lower()
    if name.startswith("claude-code-") or name == "claude-code":
        return "claude-code"
    if name.startswith("codex-") or name == "codex":
        return "codex"
    if name.startswith("openclaw-") or name == "openclaw":
        return "openclaw"
    if name.startswith("standalone-") or name == "standalone":
        return "standalone"
    if "-" in name:
        return name.split("-", 1)[0] or "standalone"
    return name or "standalone"


def _lightweight_platform_id(instance: str) -> str:
    explicit = os.environ.get("QUAID_ADAPTER_TYPE", "").strip().lower()
    if explicit:
        return explicit
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
    platform = _lightweight_platform_id(instance)
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
        return memory_override

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


def get_injection_timeout_ms(default: int = 8000) -> int:
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
