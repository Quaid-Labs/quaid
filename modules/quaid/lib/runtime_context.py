"""Runtime context port for path/provider/session access.

This isolates direct adapter access behind a single module so lifecycle,
datastore, and ingestor code does not import adapter internals directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from lib.adapter import get_adapter
from lib.agent_notice import (
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


def get_adapter_instance() -> "QuaidAdapter":
    return get_adapter()


def get_workspace_dir() -> Path:
    """Return the active instance root directory.

    This is the per-instance silo (QUAID_HOME/QUAID_INSTANCE), not the
    QUAID_HOME root. Config, data, logs, and identity all resolve relative
    to this path.
    """
    return get_adapter().instance_root()


def get_quaid_home() -> Path:
    """Return the QUAID_HOME root (not the per-instance silo).

    Use this for paths that live at the QUAID_HOME level, e.g. projects/.
    """
    return get_adapter().quaid_home()


def get_data_dir() -> Path:
    return get_adapter().data_dir()


def get_logs_dir() -> Path:
    return get_adapter().logs_dir()


def get_repo_slug() -> str:
    return get_adapter().get_repo_slug()


def get_install_url() -> str:
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
    try:
        return get_adapter().get_llm_provider(model_tier=model_tier)
    except Exception as exc:
        tier = str(model_tier or "default").strip() or "default"
        try:
            _notify_agent(
                f"Quaid could not access its {tier} language model provider: {exc}",
                severity="error",
                source="provider",
                dedupe_key=f"llm-provider:{tier}:{type(exc).__name__}:{str(exc).strip()}",
                ttl_seconds=900,
            )
        except Exception as notify_exc:
            logger.warning("Failed surfacing provider access error to agent: %s", notify_exc)
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


def get_deferred_notice_status() -> Dict:
    return _get_deferred_notice_status()


def format_deferred_notice_hint() -> str:
    return _format_deferred_notice_hint()
