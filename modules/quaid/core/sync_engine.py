"""Sync engine — copies project bootstrap files to adapter workspaces.

Some adapters (e.g. OpenClaw) enforce workspace boundary constraints that
prevent reading files from outside their workspace. This engine copies
bootstrap-eligible files from the canonical QUAID_HOME/projects/ location
into the adapter's workspace on each daemon tick.

Adapters that read directly from QUAID_HOME (e.g. Claude Code) don't need
this — their get_sync_target() returns None.

See docs/PROJECT-SYSTEM-SPEC.md and docs/DIRECTORY-STANDARD.md.
"""

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Files eligible for sync — must match OC's VALID_BOOTSTRAP_NAMES
SYNCABLE_NAMES = frozenset({
    "TOOLS.md", "AGENTS.md", "SOUL.md", "USER.md",
    "MEMORY.md", "IDENTITY.md", "HEARTBEAT.md", "TODO.md",
})

_README_CONTENT = """\
# Synced Project Files — DO NOT EDIT HERE

These files are read-only copies managed by Quaid's sync engine.
Edits made here will be overwritten on the next sync cycle.

Canonical location: {canonical_path}

Edit the files there instead. Changes will be synced automatically.
"""


@dataclass
class SyncResult:
    project: str
    copied: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def sync_project(canonical_dir: Path, target_dir: Path,
                 project_name: str) -> SyncResult:
    """Sync one project's bootstrap files from canonical to target.

    Args:
        canonical_dir: QUAID_HOME/projects/<name>/
        target_dir: adapter workspace projects dir (e.g. ~/.openclaw/.../projects/)
        project_name: project name for subdirectory

    Returns:
        SyncResult with details of what was copied/skipped/removed.
    """
    result = SyncResult(project=project_name)
    dst_project = target_dir / project_name

    for fname in SYNCABLE_NAMES:
        src = canonical_dir / fname
        dst = dst_project / fname

        if not src.is_file():
            # Canonical file doesn't exist — clean up target if present
            if dst.is_file():
                try:
                    dst.unlink()
                    result.removed.append(fname)
                except OSError as e:
                    result.errors.append(f"remove {fname}: {e}")
            continue

        if dst.is_file():
            # Compare mtimes — only copy if canonical is newer
            try:
                if dst.stat().st_mtime >= src.stat().st_mtime:
                    result.skipped.append(fname)
                    continue
            except OSError:
                pass  # stat failed, copy anyway

        # Copy with preserved mtime
        try:
            dst_project.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result.copied.append(fname)
        except OSError as e:
            result.errors.append(f"copy {fname}: {e}")

    # Write README pointing to canonical location
    if dst_project.is_dir():
        readme = dst_project / "README.md"
        try:
            readme.write_text(
                _README_CONTENT.format(canonical_path=canonical_dir),
                encoding="utf-8",
            )
        except OSError:
            pass  # Non-critical

    return result


def sync_all_projects() -> List[SyncResult]:
    """Sync all registered projects to all adapters that need it.

    Called from the daemon loop on each tick.
    """
    from lib.adapter import get_adapter
    from lib.runtime_context import get_workspace_dir

    adapter = get_adapter()
    sync_target = adapter.get_context_sync_target()
    if sync_target is None:
        return []  # Adapter reads directly, no sync needed

    projects_dir = adapter.projects_dir()
    if not projects_dir.is_dir():
        return []

    results = []
    try:
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            result = sync_project(project_dir, sync_target, project_dir.name)
            if result.copied or result.removed:
                logger.info(
                    "[sync] %s: copied=%s removed=%s",
                    project_dir.name, result.copied, result.removed,
                )
            if result.errors:
                logger.warning("[sync] %s errors: %s", project_dir.name, result.errors)
            results.append(result)
    except OSError as e:
        logger.warning("[sync] Failed to iterate projects: %s", e)

    # Clean up target dirs for projects that no longer exist
    _cleanup_stale_targets(projects_dir, sync_target)

    return results


def _cleanup_stale_targets(canonical_projects: Path, target_dir: Path) -> None:
    """Remove synced project dirs that no longer have a canonical source."""
    if not target_dir.is_dir():
        return
    canonical_names = {
        d.name for d in canonical_projects.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    }
    for target_project in target_dir.iterdir():
        if not target_project.is_dir() or target_project.name.startswith("."):
            continue
        if target_project.name not in canonical_names:
            logger.info("[sync] Removing stale target: %s", target_project.name)
            try:
                shutil.rmtree(target_project)
            except OSError as e:
                logger.warning("[sync] Failed to remove %s: %s", target_project.name, e)
