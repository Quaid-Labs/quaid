"""Core parallel runtime configuration and compatibility exports.

Provides:
- Strict core parallel config resolution.
"""

from __future__ import annotations

from typing import Any

from lib.resource_locks import MAX_THREAD_LOCK_CACHE, ResourceLockRegistry

__all__ = ["MAX_THREAD_LOCK_CACHE", "ResourceLockRegistry", "get_parallel_config"]


def get_parallel_config(cfg: Any) -> Any:
    """Resolve core parallel config strictly from cfg.core.parallel."""
    core = getattr(cfg, "core", None)
    core_parallel = getattr(core, "parallel", None) if core else None
    if core_parallel is None:
        raise RuntimeError("Missing required config: core.parallel")
    return core_parallel
