"""Claude Code adapter lifecycle maintenance registrations.

Stub module — Claude Code does not require adapter-specific workspace
auditing (CLAUDE.md is managed by CC natively). This module exists so
the janitor lifecycle loader does not error on import.
"""

from __future__ import annotations


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register Claude Code-specific lifecycle routines.

    CC does not need OC-style workspace auditing (CLAUDE.md is managed
    by Claude Code natively), but we register a workspace routine that
    returns immediately so the janitor lifecycle loader doesn't error.
    """

    def _run_workspace_audit(ctx):
        result = result_factory()
        result.data["workspace_phase"] = "skipped"
        result.logs.append("Workspace audit not applicable for claude_code adapter")
        return result

    registry.register("workspace", _run_workspace_audit)
