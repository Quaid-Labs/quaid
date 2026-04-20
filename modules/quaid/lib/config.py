"""Shared configuration constants — single source of truth.

All hardcoded DB paths, Ollama URLs, embedding params, etc. are centralized here.
Consumers import from lib.config instead of defining their own constants.

Environment variable overrides (for testing):
  MEMORY_DB_PATH       — overrides config database.path
  MEMORY_ARCHIVE_DB_PATH — overrides config database.archivePath
  OLLAMA_URL           — overrides config ollama.url
"""

import os
from pathlib import Path


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

    Respects OLLAMA_URL env var, then falls back to config.
    """
    env_url = os.environ.get("OLLAMA_URL")
    if env_url:
        return env_url
    return _get_cfg().ollama.url


def get_embedding_model() -> str:
    """Get the Ollama embedding model name."""
    return _get_cfg().ollama.embedding_model


def get_embedding_dim() -> int:
    """Get the embedding vector dimension."""
    return _get_cfg().ollama.embedding_dim
