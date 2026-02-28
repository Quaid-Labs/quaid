"""Central plugin contract base for executable plugin surfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict

from core.runtime.plugins import PluginHookContext


class PluginContractBase(ABC):
    """Universal plugin contract surfaces shared by adapter/ingest/datastore."""

    @abstractmethod
    def on_init(self, ctx: PluginHookContext) -> None:
        """Run one-time or idempotent initialization tasks."""

    @abstractmethod
    def on_config(self, ctx: PluginHookContext) -> None:
        """Validate/apply plugin-specific config payload."""

    @abstractmethod
    def on_status(self, ctx: PluginHookContext) -> Dict[str, Any]:
        """Return lightweight runtime status payload."""

    @abstractmethod
    def on_dashboard(self, ctx: PluginHookContext) -> Dict[str, Any]:
        """Return dashboard integration payload (future surface)."""

    @abstractmethod
    def on_maintenance(self, ctx: PluginHookContext) -> Dict[str, Any]:
        """Run plugin-owned maintenance routine payloads."""

    @abstractmethod
    def on_tool_runtime(self, ctx: PluginHookContext) -> Dict[str, Any]:
        """Run tool runtime validation/bootstrap for adapter-facing tool surfaces."""

    @abstractmethod
    def on_health(self, ctx: PluginHookContext) -> Dict[str, Any]:
        """Return health probe payload for liveness/readiness checks."""
