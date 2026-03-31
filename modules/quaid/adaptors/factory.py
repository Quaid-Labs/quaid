"""Adapter factory to avoid concrete adaptor imports in lib.adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.adapter import QuaidAdapter


def create_adapter(kind: str) -> "QuaidAdapter":
    normalized = str(kind or "").strip().lower().replace("-", "_")
    if normalized == "openclaw":
        from adaptors.openclaw.adapter import OpenClawAdapter
        return OpenClawAdapter()
    if normalized == "codex":
        from adaptors.codex.adapter import CodexAdapter
        return CodexAdapter()
    if normalized in ("claude_code", "claudecode"):
        from adaptors.claude_code.adapter import ClaudeCodeAdapter
        return ClaudeCodeAdapter()
    raise RuntimeError(
        f"Unsupported adapter type for adaptors.factory: {kind!r}. "
        "Supported: 'openclaw', 'codex', 'claude-code'. "
        "'standalone' is handled in lib.adapter.get_adapter()."
    )
