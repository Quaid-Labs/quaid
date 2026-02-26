"""OpenClaw-specific lifecycle maintenance registrations.

This module owns maintenance registrations that are specific to OpenClaw
runtime behavior/policies (for example, core-markdown workspace auditing).
"""

from __future__ import annotations

def register_lifecycle_routines(registry, result_factory) -> None:
    """Register OpenClaw-specific lifecycle routines."""

    def _run_workspace_audit(ctx):
        result = result_factory()

        # Workspace markdown audit is OpenClaw-specific policy.
        # If a non-OpenClaw adapter is active, skip cleanly.
        adapter_type = str(getattr(getattr(ctx.cfg, "adapter", None), "type", "") or "").strip().lower()
        if adapter_type and adapter_type != "openclaw":
            result.data["workspace_phase"] = "skipped"
            result.logs.append(f"Workspace audit skipped for adapter type '{adapter_type}'")
            return result

        try:
            from core.lifecycle import workspace_audit
            audit_result = workspace_audit.run_workspace_check(dry_run=ctx.dry_run)
            phase = audit_result.get("phase", "unknown")
            result.data["workspace_phase"] = phase

            bloat_stats = audit_result.get("bloat_stats", {})
            bloated = [name for name, stats in bloat_stats.items() if stats.get("over_limit")]
            if bloated:
                result.data["bloated_files"] = bloated
                result.logs.append(f"Files over limit: {', '.join(bloated)}")
                for name in bloated:
                    stats = bloat_stats[name]
                    result.logs.append(
                        f"  {name}: {stats.get('lines', 0)}/{stats.get('maxLines', 0)} lines"
                    )

            if phase == "apply":
                result.metrics["workspace_moved_to_docs"] = int(audit_result.get("moved_to_docs", 0))
                result.metrics["workspace_moved_to_memory"] = int(audit_result.get("moved_to_memory", 0))
                result.metrics["workspace_trimmed"] = int(audit_result.get("trimmed", 0))
                result.metrics["workspace_bloat_warnings"] = int(audit_result.get("bloat_warnings", 0))
                result.metrics["workspace_project_detected"] = int(audit_result.get("project_detected", 0))
                result.logs.append(f"{'Would apply' if ctx.dry_run else 'Applied'} review decisions:")
                result.logs.append(f"  Moved to docs: {result.metrics['workspace_moved_to_docs']}")
                result.logs.append(f"  Moved to memory: {result.metrics['workspace_moved_to_memory']}")
                result.logs.append(f"  Trimmed: {result.metrics['workspace_trimmed']}")
                result.logs.append(f"  Bloat warnings: {result.metrics['workspace_bloat_warnings']}")
                if result.metrics["workspace_project_detected"] > 0:
                    result.logs.append(
                        "  Project content detected: "
                        f"{result.metrics['workspace_project_detected']} (queued for agent review)"
                    )
            elif phase == "no_changes":
                result.logs.append("No workspace files changed since last run")
            elif phase == "error":
                result.errors.append(f"Workspace audit error: {audit_result.get('error', 'unknown')}")
        except RuntimeError as exc:
            result.errors.append(f"Workspace audit skipped (API error): {exc}")
        except Exception as exc:
            result.errors.append(f"Workspace audit failed: {exc}")
        return result

    registry.register("workspace", _run_workspace_audit)
