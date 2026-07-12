"""Runtime context port for path/provider/session access.

This isolates direct adapter access behind a single module so lifecycle,
datastore, and ingestor code does not import adapter internals directly.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from lib.adapter import get_adapter, peek_adapter
from lib.agent_notice import (
    deliver_deferred_notices as _deliver_deferred_notices,
    drain_deferred_notices as _drain_deferred_notices,
    format_deferred_notice_hint as _format_deferred_notice_hint,
    get_deferred_notice_status as _get_deferred_notice_status,
    list_deferred_notices as _list_deferred_notices,
    notify_agent as _notify_agent,
    queue_deferred_notice as _queue_deferred_notice,
)
from lib.fail_policy import is_fail_hard_enabled

if TYPE_CHECKING:
    from lib.adapter import ChannelInfo, QuaidAdapter
    from lib.providers import LLMProvider

logger = logging.getLogger(__name__)


def _trace_m15(event: str, **fields) -> None:
    try:
        from lib.m15_trace import trace_m15

        trace_m15(event, **fields)
    except Exception as exc:
        logger.debug("_trace_m15 failed for %s: %s", event, exc, exc_info=True)


def _env_quaid_home() -> Path | None:
    raw = str(os.environ.get("QUAID_HOME", "") or "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _env_visible_quaid_home() -> Path | None:
    explicit = str(os.environ.get("QUAID_VISIBLE_HOME", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    hidden = _env_quaid_home()
    if hidden is None:
        return None
    name = hidden.name
    if name.startswith(".") and len(name) > 1:
        return hidden.with_name(name[1:])
    return hidden


def _env_instance_root() -> Path | None:
    hidden = _env_quaid_home()
    instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
    if hidden is None or not instance:
        return None
    return hidden / "instances" / instance


def _env_visible_instance_root() -> Path | None:
    visible = _env_visible_quaid_home()
    instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
    if visible is None or not instance:
        return None
    return visible / "instances" / instance


def get_adapter_instance() -> "QuaidAdapter":
    return get_adapter()


def _active_adapter_instance() -> "QuaidAdapter | None":
    return peek_adapter()


def _adapter_path(adapter: object, method_name: str) -> Path | None:
    method = getattr(adapter, method_name, None)
    if not callable(method):
        return None
    value = method()
    return value if isinstance(value, Path) else Path(value)


def get_workspace_dir() -> Path:
    """Return the active hidden instance root directory.

    This is the per-instance hidden silo (QUAID_HOME/instances/QUAID_INSTANCE),
    not the visible root. Config, data, logs, and runtime state resolve here.
    """
    adapter = _active_adapter_instance()
    if adapter is not None:
        adapter_root = _adapter_path(adapter, "instance_root")
        if adapter_root is not None:
            return adapter_root
    env_root = _env_instance_root()
    if env_root is not None:
        return env_root
    env_home = _env_quaid_home()
    if env_home is not None:
        return env_home
    return get_adapter().instance_root()


def get_quaid_home() -> Path:
    """Return the hidden QUAID_HOME root (not the per-instance silo)."""
    adapter = _active_adapter_instance()
    if adapter is not None:
        adapter_home = _adapter_path(adapter, "quaid_home")
        if adapter_home is not None:
            return adapter_home
    env_home = _env_quaid_home()
    if env_home is not None:
        return env_home
    return get_adapter().quaid_home()


def get_visible_workspace_dir() -> Path:
    adapter = _active_adapter_instance()
    if adapter is not None:
        adapter_root = _adapter_path(adapter, "visible_instance_root")
        if adapter_root is not None:
            return adapter_root
    env_root = _env_visible_instance_root()
    if env_root is not None:
        return env_root
    env_home = _env_visible_quaid_home()
    if env_home is not None:
        return env_home
    return get_adapter().visible_instance_root()


def get_visible_quaid_home() -> Path:
    adapter = _active_adapter_instance()
    if adapter is not None:
        adapter_home = _adapter_path(adapter, "visible_home")
        if adapter_home is not None:
            return adapter_home
    env_home = _env_visible_quaid_home()
    if env_home is not None:
        return env_home
    return get_adapter().visible_home()


def get_projects_dir() -> Path:
    adapter = _active_adapter_instance()
    if adapter is not None:
        adapter_dir = _adapter_path(adapter, "projects_dir")
        if adapter_dir is not None:
            return adapter_dir
    env_home = _env_visible_quaid_home()
    if env_home is not None:
        return env_home / "projects"
    return get_adapter().projects_dir()


def get_identity_dir() -> Path:
    adapter = _active_adapter_instance()
    if adapter is not None:
        adapter_dir = _adapter_path(adapter, "identity_dir")
        if adapter_dir is not None:
            return adapter_dir
    env_root = _env_visible_instance_root()
    if env_root is not None:
        return env_root
    return get_adapter().identity_dir()


def get_data_dir() -> Path:
    adapter = _active_adapter_instance()
    if adapter is not None:
        adapter_dir = _adapter_path(adapter, "data_dir")
        if adapter_dir is not None:
            return adapter_dir
    env_root = _env_instance_root()
    if env_root is not None:
        return env_root / "data"
    env_home = _env_quaid_home()
    if env_home is not None:
        return env_home / "data"
    return get_adapter().data_dir()


def get_logs_dir() -> Path:
    adapter = _active_adapter_instance()
    if adapter is not None:
        adapter_dir = _adapter_path(adapter, "logs_dir")
        if adapter_dir is not None:
            return adapter_dir
    env_root = _env_instance_root()
    if env_root is not None:
        return env_root / "logs"
    env_home = _env_quaid_home()
    if env_home is not None:
        return env_home / "logs"
    return get_adapter().logs_dir()


def get_repo_slug() -> str:
    adapter = _active_adapter_instance()
    if adapter is not None:
        return adapter.get_repo_slug()
    env_home = _env_quaid_home()
    env_instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
    if env_home is not None and not env_instance:
        return "quaid-labs/quaid"
    return get_adapter().get_repo_slug()


def get_install_url() -> str:
    adapter = _active_adapter_instance()
    if adapter is not None:
        return adapter.get_install_url()
    env_home = _env_quaid_home()
    env_instance = str(os.environ.get("QUAID_INSTANCE", "") or "").strip()
    if env_home is not None and not env_instance:
        return f"https://raw.githubusercontent.com/{get_repo_slug()}/main/install.sh"
    return get_adapter().get_install_url()


def get_bootstrap_markdown_globs() -> List[str]:
    try:
        return get_adapter().get_bootstrap_markdown_globs()
    except Exception as exc:
        if is_fail_hard_enabled():
            raise RuntimeError("Failed to load bootstrap markdown globs") from exc
        logger.warning("Failed loading bootstrap markdown globs; using empty fallback: %s", exc)
        return []


def get_llm_provider(model_tier: Optional[str] = None) -> "LLMProvider":
    _trace_m15("runtime_context.get_llm_provider.entry", model_tier=model_tier)
    try:
        provider = get_adapter().get_llm_provider(model_tier=model_tier)
        _trace_m15(
            "runtime_context.get_llm_provider.ok",
            model_tier=model_tier,
            provider=provider.__class__.__name__,
        )
        return provider
    except Exception as exc:
        tier = str(model_tier or "default").strip() or "default"
        _trace_m15(
            "runtime_context.get_llm_provider.error",
            model_tier=model_tier,
            tier=tier,
            exc_type=type(exc).__name__,
            error=str(exc),
        )
        try:
            _queue_deferred_notice(
                f"Quaid could not access its {tier} language model provider: {exc}",
                kind="provider",
                priority="high",
                source="provider",
                dedupe_key=f"llm-provider:{tier}:{type(exc).__name__}:{str(exc).strip()}",
            )
        except Exception as notice_exc:
            logger.warning("Failed queuing provider access error as deferred notice: %s", notice_exc)
        raise


def parse_session_jsonl(path: Path) -> str:
    return get_adapter().parse_session_jsonl(path)


def build_transcript(messages: List[Dict]) -> str:
    return get_adapter().build_transcript(messages)


def get_sessions_dir() -> Optional[Path]:
    return get_adapter().get_sessions_dir()


def get_last_channel(session_key: str = "") -> Optional["ChannelInfo"]:
    return get_adapter().get_last_channel(session_key)


def send_notification(
    message: str,
    *,
    channel_override: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    return get_adapter().notify(
        message,
        channel_override=channel_override,
        dry_run=dry_run,
        force=force,
    )


def notify_agent(
    message: str,
    *,
    severity: str = "warning",
    source: str = "",
    dedupe_key: Optional[str] = None,
    ttl_seconds: int = 3600,
    channel_override: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    return _notify_agent(
        message,
        severity=severity,
        source=source,
        dedupe_key=dedupe_key,
        ttl_seconds=ttl_seconds,
        channel_override=channel_override,
        dry_run=dry_run,
        force=force,
    )


def queue_deferred_notice(
    message: str,
    *,
    kind: str = "janitor",
    priority: str = "normal",
    source: str = "quaid",
    dedupe_key: Optional[str] = None,
) -> bool:
    return _queue_deferred_notice(
        message,
        kind=kind,
        priority=priority,
        source=source,
        dedupe_key=dedupe_key,
    )


def list_deferred_notices(
    *,
    status: str = "pending",
    limit: int = 50,
) -> List[Dict]:
    return _list_deferred_notices(status=status, limit=limit)


def drain_deferred_notices(*, limit: int = 50) -> List[Dict]:
    return _drain_deferred_notices(limit=limit)


def deliver_deferred_notices(
    *,
    limit: int = 50,
    channel_override: Optional[str] = None,
    dry_run: bool = False,
) -> List[Dict]:
    return _deliver_deferred_notices(
        limit=limit,
        channel_override=channel_override,
        dry_run=dry_run,
    )


def get_deferred_notice_status(
    *,
    limit: int = 500,
    include_items: bool = False,
) -> Dict:
    return _get_deferred_notice_status(limit=limit, include_items=include_items)


def format_deferred_notice_hint() -> str:
    return _format_deferred_notice_hint()
