"""Codex adapter lifecycle maintenance registrations.

Codex does not need adapter-specific workspace auditing. This module exists so
the janitor lifecycle loader can resolve a Codex maintenance module without
marking the run as failed.
"""

from __future__ import annotations


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register Codex-specific lifecycle routines."""

    def _run_workspace_audit(ctx):
        result = result_factory()
        result.data["workspace_phase"] = "skipped"
        result.logs.append("Workspace audit not applicable for codex adapter")
        return result

    registry.register("workspace", _run_workspace_audit)
