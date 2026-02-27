"""Prompt-set registry for centralized, swappable prompt families."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, List, Optional


DEFAULT_PROMPT_SET_ID = "default"


@dataclass(frozen=True)
class PromptSetRecord:
    set_id: str
    prompts: Dict[str, str] = field(default_factory=dict)
    source: str = ""


_REGISTRY_LOCK = RLock()
_REGISTRY: Dict[str, PromptSetRecord] = {}
_BOOTSTRAPPED = False


def register_prompt_set(set_id: str, prompts: Dict[str, str], *, source: str = "") -> None:
    """Register or replace a prompt set.

    Prompt sets are keyed by ``set_id`` and contain stable prompt keys.
    """
    sid = str(set_id or "").strip()
    if not sid:
        raise ValueError("prompt set id is required")
    if not isinstance(prompts, dict):
        raise ValueError("prompts must be a dict")

    normalized: Dict[str, str] = {}
    for raw_key, raw_value in prompts.items():
        key = str(raw_key or "").strip()
        if not key:
            raise ValueError("prompt keys must be non-empty strings")
        text = str(raw_value or "")
        normalized[key] = text

    record = PromptSetRecord(set_id=sid, prompts=normalized, source=str(source or ""))
    with _REGISTRY_LOCK:
        _REGISTRY[sid] = record


def list_prompt_sets() -> List[str]:
    _ensure_bootstrap()
    with _REGISTRY_LOCK:
        return sorted(_REGISTRY.keys())


def validate_prompt_set_exists(set_id: str) -> None:
    """Raise if prompt set is unknown."""
    _ensure_bootstrap()
    sid = str(set_id or "").strip() or DEFAULT_PROMPT_SET_ID
    with _REGISTRY_LOCK:
        if sid in _REGISTRY:
            return
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none)"
    raise RuntimeError(
        f"Unknown prompt_set '{sid}'. Available prompt sets: {available}"
    )


def get_prompt(prompt_key: str, *, prompt_set: Optional[str] = None) -> str:
    """Resolve prompt text by key using selected prompt set.

    Resolution order:
    1) selected set
    2) default set (fallback for partial custom sets)
    """
    _ensure_bootstrap()
    key = str(prompt_key or "").strip()
    if not key:
        raise ValueError("prompt key is required")

    selected_set = str(prompt_set or _read_active_prompt_set()).strip() or DEFAULT_PROMPT_SET_ID
    with _REGISTRY_LOCK:
        selected = _REGISTRY.get(selected_set)
        default = _REGISTRY.get(DEFAULT_PROMPT_SET_ID)

    if selected is None:
        validate_prompt_set_exists(selected_set)
        raise RuntimeError(f"Prompt set '{selected_set}' was not available after validation")

    if key in selected.prompts:
        return selected.prompts[key]
    if default is not None and key in default.prompts:
        return default.prompts[key]

    raise KeyError(
        f"Prompt key '{key}' not found in prompt_set='{selected_set}'"
    )


def _read_active_prompt_set() -> str:
    try:
        from config import get_config

        configured = str(get_config().prompt_set or "").strip()
        return configured or DEFAULT_PROMPT_SET_ID
    except Exception:
        return DEFAULT_PROMPT_SET_ID


def _ensure_bootstrap() -> None:
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    with _REGISTRY_LOCK:
        if _BOOTSTRAPPED:
            return
        from . import default_set

        default_set.register()
        _BOOTSTRAPPED = True
