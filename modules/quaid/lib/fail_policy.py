"""Fail-hard policy helpers.

Centralizes how Quaid resolves fallback policy from config.
"""

from __future__ import annotations


def is_fail_hard_enabled() -> bool:
    """Return True when fallback behavior must be disabled.

    Source of truth: config/memory.json retrieval.fail_hard (or failHard alias).
    Defaults to True if config is unavailable.
    """
    try:
        from config import get_config

        retrieval = getattr(get_config(), "retrieval", None)
        if retrieval is None:
            return True
        # Dataclass field is fail_hard; config normalization handles aliases.
        if hasattr(retrieval, "fail_hard"):
            return bool(getattr(retrieval, "fail_hard"))
    except Exception:
        return True
    return True
