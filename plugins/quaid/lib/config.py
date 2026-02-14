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

# Workspace root for resolving relative paths
_WORKSPACE_ROOT = Path(os.environ.get("CLAWDBOT_WORKSPACE", str(Path(__file__).resolve().parent.parent.parent.parent)))


def _get_cfg():
    """Lazy import to avoid circular dependency with config.py."""
    from config import get_config
    return get_config()


def get_db_path() -> Path:
    """Get the main memory database path.

    Respects MEMORY_DB_PATH env var for testing, then falls back to config.
    """
    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path:
        return Path(env_path)
    cfg = _get_cfg()
    p = cfg.database.path
    return Path(p) if p.startswith('/') else _WORKSPACE_ROOT / p


def get_archive_db_path() -> Path:
    """Get the archive database path.

    Respects MEMORY_ARCHIVE_DB_PATH env var for testing, then falls back to config.
    """
    env_path = os.environ.get("MEMORY_ARCHIVE_DB_PATH")
    if env_path:
        return Path(env_path)
    cfg = _get_cfg()
    p = cfg.database.archive_path
    return Path(p) if p.startswith('/') else _WORKSPACE_ROOT / p


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
