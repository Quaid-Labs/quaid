"""Adapter factory to avoid concrete adaptor imports in lib.adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.adapter import QuaidAdapter


def create_adapter(kind: str) -> "QuaidAdapter":
    normalized = str(kind or "").strip().lower()
    if normalized == "openclaw":
        from adaptors.openclaw.adapter import OpenClawAdapter
        return OpenClawAdapter()
    raise RuntimeError(
        f"Unsupported adapter type: {kind!r}. Expected 'standalone' or 'openclaw'."
    )
