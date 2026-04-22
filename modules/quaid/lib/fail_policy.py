"""Fail-hard policy helpers.

Centralizes how Quaid resolves fallback policy from config.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_fail_hard_enabled() -> bool:
    """Return True when fallback behavior must be disabled.

    Source of truth: config.json retrieval.fail_hard (or failHard alias).
    Defaults to True if config is unavailable.
    """
    try:
        from lib.config import (
            _deep_merge_dicts,
            _lightweight_config_paths,
            _normalize_config_keys,
        )
        import json

        data = {}
        for config_path in reversed(_lightweight_config_paths()):
            if not config_path.is_file():
                continue
            try:
                parsed = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "Failed to load fail-hard policy from lightweight config %s; defaulting to enabled: %s",
                    config_path,
                    exc,
                )
                return True
            if isinstance(parsed, dict):
                data = _deep_merge_dicts(data, _normalize_config_keys(parsed))

        retrieval = data.get("retrieval", {})
        if not isinstance(retrieval, dict):
            return True
        if "fail_hard" in retrieval:
            return bool(retrieval.get("fail_hard"))
    except Exception as exc:
        logger.warning("Failed to load fail-hard policy from lightweight config; defaulting to enabled: %s", exc)
        return True
    return True
