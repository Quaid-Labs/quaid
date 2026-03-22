"""Project registry — single source of truth for all Quaid projects.

Manages project-registry.json in QUAID_HOME. Tracks project metadata,
source roots, and adapter instances.

See docs/PROJECT-SYSTEM-SPEC.md#project-registry.
"""

import fcntl
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _registry_path() -> Path:
    """Path to the project registry file."""
    try:
        from lib.adapter import get_adapter
        return get_adapter().quaid_home() / "project-registry.json"
    except Exception:
        import os
        home = os.environ.get("QUAID_HOME", "").strip()
        root = Path(home).resolve() if home else Path.home() / "quaid"
        return root / "project-registry.json"


def _load_registry() -> Dict[str, Any]:
    """Load the registry file. Returns empty structure if missing/corrupt."""
    path = _registry_path()
    if not path.is_file():
        return {"projects": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "projects" not in data:
            return {"projects": {}}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read project registry: %s", e)
        return {"projects": {}}


def _save_registry(data: Dict[str, Any]) -> None:
    """Atomically write the registry file with file locking.

    Locking strategy: acquire an exclusive lock on the canonical file
    (creating it if absent) so that concurrent writers are serialised
    across the full read-modify-write cycle.  Write to a sibling .tmp
    file, fsync, then rename over the canonical path.  The lock is held
    until after the rename so no reader sees a half-written file and no
    second writer can slip in between write and rename.
    """
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(".tmp")
    # Open (or create) the canonical file to use as the lock target.
    # O_CREAT | O_RDWR so the fd is valid even on first run.
    lock_fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            tmp.rename(path)
        except OSError as e:
            logger.error("Failed to write project registry: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def list_projects() -> Dict[str, Dict[str, Any]]:
    """Return all registered projects."""
    return _load_registry().get("projects", {})


def get_project(name: str) -> Optional[Dict[str, Any]]:
    """Get a single project by name, or None if not found."""
    return list_projects().get(name)


def create_project(
    name: str,
    description: str = "",
    source_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a new project.

    Args:
        name: Project name (lowercase, kebab-case)
        description: Human-readable description
        source_root: Path to user's project files (optional)

    Returns:
        The project entry dict.

    Raises:
        ValueError: If project already exists or name is invalid.
    """
    import re
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
        raise ValueError(f"Invalid project name: {name!r} (must be lowercase kebab-case)")

    registry = _load_registry()
    if name in registry["projects"]:
        raise ValueError(f"Project already exists: {name}")

    from lib.adapter import get_adapter, quaid_projects_dir
    from lib.instance import instance_id as _instance_id
    adapter = get_adapter()
    canonical = quaid_projects_dir(adapter.quaid_home()) / name

    entry = {
        "canonical_path": str(canonical),
        "source_root": source_root,
        "instances": [_instance_id()],
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "description": description,
    }

    # Create the canonical directory structure
    canonical.mkdir(parents=True, exist_ok=True)
    (canonical / "docs").mkdir(exist_ok=True)

    # Write initial PROJECT.md
    project_md = canonical / "PROJECT.md"
    if not project_md.exists():
        project_md.write_text(
            f"# {name}\n\n{description}\n\n"
            f"Created: {entry['created_at']}\n",
            encoding="utf-8",
        )

    # Initialize shadow git if source_root provided
    if source_root:
        try:
            from core.shadow_git import ShadowGit
            from lib.adapter import quaid_tracking_dir
            sg = ShadowGit(
                name,
                Path(source_root),
                tracking_base=quaid_tracking_dir(adapter.quaid_home()),
            )
            sg.init()
            sg.snapshot()  # Initial baseline
            logger.info("Initialized shadow git for %s at %s", name, source_root)
        except Exception as e:
            logger.warning("Failed to init shadow git for %s: %s", name, e)

    registry["projects"][name] = entry
    _save_registry(registry)

    logger.info("Created project: %s", name)
    return entry


def update_project(name: str, **updates: Any) -> Dict[str, Any]:
    """Update fields on an existing project.

    Args:
        name: Project name
        **updates: Fields to update (source_root, description, instances)

    Returns:
        The updated project entry.

    Raises:
        KeyError: If project not found.
    """
    registry = _load_registry()
    if name not in registry["projects"]:
        raise KeyError(f"Project not found: {name}")

    allowed = {"source_root", "description", "instances"}
    for key, value in updates.items():
        if key in allowed:
            registry["projects"][name][key] = value

    _save_registry(registry)
    return registry["projects"][name]


def link_project(name: str) -> Dict[str, Any]:
    """Add the current QUAID_INSTANCE to a project's instances list.

    Used when a second adapter wants to participate in an existing project
    without taking ownership. Idempotent — safe to call if already linked.

    Args:
        name: Project name.

    Returns:
        The updated project entry.

    Raises:
        KeyError: If project not found.
    """
    registry = _load_registry()
    if name not in registry["projects"]:
        raise KeyError(f"Project not found: {name}")

    from lib.instance import instance_id as _instance_id
    instance = _instance_id()
    instances = registry["projects"][name].setdefault("instances", [])
    if instance not in instances:
        instances.append(instance)
        registry["projects"][name]["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        _save_registry(registry)
        logger.info("Linked instance %s to project %s", instance, name)
    return registry["projects"][name]


def unlink_project(name: str) -> Dict[str, Any]:
    """Remove the current QUAID_INSTANCE from a project's instances list.

    Inverse of link_project. Idempotent — safe to call if already unlinked.
    Does not delete the project or its files.

    Args:
        name: Project name.

    Returns:
        The updated project entry.

    Raises:
        KeyError: If project not found.
    """
    registry = _load_registry()
    if name not in registry["projects"]:
        raise KeyError(f"Project not found: {name}")

    from lib.instance import instance_id as _instance_id
    instance = _instance_id()
    instances = registry["projects"][name].get("instances", [])
    if instance in instances:
        instances.remove(instance)
        registry["projects"][name]["instances"] = instances
        registry["projects"][name]["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
        _save_registry(registry)
        logger.info("Unlinked instance %s from project %s", instance, name)
    return registry["projects"][name]


def delete_project(name: str) -> None:
    """Remove a project from the registry and clean up artifacts.

    Unlinks all instances, removes the canonical project directory, cleans up
    shadow git tracking, and purges project_definitions + doc_registry rows
    from the SQLite database. Does NOT touch the user's source_root directory.

    Args:
        name: Project name to delete.

    Raises:
        KeyError: If project not found.
    """
    registry = _load_registry()
    if name not in registry["projects"]:
        raise KeyError(f"Project not found: {name}")

    entry = registry["projects"][name]

    # Clean up shadow git tracking
    try:
        from core.shadow_git import ShadowGit
        from lib.adapter import get_adapter, quaid_tracking_dir
        adapter = get_adapter()
        source_root = entry.get("source_root")
        if source_root:
            sg = ShadowGit(
                name,
                Path(source_root),
                tracking_base=quaid_tracking_dir(adapter.quaid_home()),
            )
            sg.destroy()
    except Exception as e:
        logger.warning("Failed to destroy shadow git for %s: %s", name, e)

    # Clean up canonical project directory
    canonical = Path(entry.get("canonical_path", ""))
    if canonical.is_dir():
        import shutil
        shutil.rmtree(canonical)

    # Remove from registry
    del registry["projects"][name]
    _save_registry(registry)

    # Clean up SQLite: project_definitions + doc_registry entries
    try:
        from lib.database import get_connection
        from lib.config import get_db_path
        with get_connection(get_db_path()) as conn:
            conn.execute("DELETE FROM project_definitions WHERE name = ?", (name,))
            conn.execute("DELETE FROM doc_registry WHERE project = ?", (name,))
    except Exception as e:
        logger.warning("Failed to clean up DB entries for project %s: %s", name, e)

    logger.info("Deleted project: %s", name)


def projects_with_source_root() -> List[Dict[str, Any]]:
    """Return projects that have a source_root configured.

    Used by the extraction daemon to know which projects need
    shadow git snapshots after extraction events.
    """
    result = []
    for name, entry in list_projects().items():
        if entry.get("source_root"):
            result.append({"name": name, **entry})
    return result


def snapshot_all_projects() -> List[Dict[str, Any]]:
    """Take shadow git snapshots for all projects with source roots.

    Called after extraction events to capture the state of user files.

    Returns:
        List of snapshot results (project name, changes, is_initial).
    """
    from core.shadow_git import ShadowGit
    from lib.adapter import get_adapter, quaid_tracking_dir

    adapter = get_adapter()
    tracking_base = quaid_tracking_dir(adapter.quaid_home())
    results = []

    for proj in projects_with_source_root():
        name = proj["name"]
        source_root = Path(proj["source_root"])

        if not source_root.is_dir():
            logger.warning("Source root missing for %s: %s", name, source_root)
            continue

        try:
            sg = ShadowGit(name, source_root, tracking_base=tracking_base)
            if not sg.initialized:
                sg.init()

            snapshot = sg.snapshot()
            if snapshot:
                diff_text = sg.get_diff() or ""
                results.append({
                    "project": name,
                    "is_initial": snapshot.is_initial,
                    "diff": diff_text,
                    "changes": [
                        {"status": c.status, "path": c.path, "old_path": c.old_path}
                        for c in snapshot.changes
                    ],
                })
                logger.info(
                    "Shadow git snapshot for %s: %d changes",
                    name, len(snapshot.changes),
                )
        except Exception as e:
            logger.warning("Shadow git snapshot failed for %s: %s", name, e)

    return results
