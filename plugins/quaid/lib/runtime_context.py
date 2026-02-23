"""Runtime context port for path/provider/session access.

This isolates direct adapter access behind a single module so lifecycle,
datastore, and ingestor code does not import adapter internals directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from lib.adapter import get_adapter

if TYPE_CHECKING:
    from lib.adapter import QuaidAdapter
    from lib.providers import LLMProvider


def get_adapter_instance() -> "QuaidAdapter":
    return get_adapter()


def get_workspace_dir() -> Path:
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
    except Exception:
        return []


def get_llm_provider() -> "LLMProvider":
    return get_adapter().get_llm_provider()


def parse_session_jsonl(path: Path) -> str:
    return get_adapter().parse_session_jsonl(path)


def build_transcript(messages: List[Dict]) -> str:
    return get_adapter().build_transcript(messages)

