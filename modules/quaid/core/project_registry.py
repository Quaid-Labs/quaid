"""Project registry — single source of truth for all Quaid projects.

Manages project-registry.json in hidden QUAID_HOME. Tracks project metadata,
source roots, and adapter instances.

See docs/PROJECT-SYSTEM-SPEC.md#project-registry.
"""

import json
import logging
import os
import tempfile
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.project_templates import render_project_md_template
from lib.adapter import quaid_projects_dir, quaid_tracking_dir
from lib.project_registry_lock import registry_lock, registry_lock_path, registry_path

logger = logging.getLogger(__name__)


def _is_temp_canonical_path(path: Path) -> bool:
    try:
        resolved = Path(os.path.realpath(path.expanduser().resolve()))
        tmp_root = Path(os.path.realpath(tempfile.gettempdir()))
        return resolved == tmp_root or str(resolved).startswith(f"{tmp_root}{os.sep}")
    except Exception:
        return False


def _sync_docs_registry_project(
    name: str,
    *,
    description: str,
    source_root: Optional[str],
    canonical: Path,
) -> None:
    """Mirror project metadata into the docs-registry source of truth."""
    if _is_temp_canonical_path(canonical):
        logger.info(
            "Skipping docs-registry sync for temp project %s (%s)",
            name,
            canonical,
        )
        return
    from config import ProjectDefinition
    from datastore.docsdb.registry import DocsRegistry

    registry = DocsRegistry()
    existing = registry.get_project_definition(name)
    label = existing.label if existing else name.replace("-", " ").title()
    patterns = list(existing.patterns) if existing and existing.patterns else ["*.md"]
    exclude = list(existing.exclude) if existing and existing.exclude else ["*.db", "*.log", "*.pyc", "__pycache__/"]
    auto_index = existing.auto_index if existing is not None else True
    home_dir = f"projects/{name}/"
    source_roots = [source_root] if source_root else []
    defn = ProjectDefinition(
        label=label,
        home_dir=home_dir,
        source_roots=source_roots,
        auto_index=auto_index,
        patterns=patterns,
        exclude=exclude,
        description=description or (existing.description if existing else f"{label} project."),
        state="active",
    )
    registry.save_project_definition(name, defn)
    project_md = canonical / "PROJECT.md"
    if project_md.is_file():
        registry.register(
            file_path=str(project_md),
            project=name,
            asset_type="doc",
            title=f"Project: {label}",
            registered_by="project_registry_sync",
        )


def _registry_path() -> Path:
    """Path to the project registry file."""
    return registry_path()


def _resolve_quaid_home() -> Path:
    home = os.environ.get("QUAID_HOME", "").strip()
    if home:
        return Path(home).resolve()
    return _registry_path().parent


def _registry_lock_path() -> Path:
    return registry_lock_path()


def _registry_lock():
    return registry_lock()


def _visible_home_from_hidden(quaid_home: Path) -> Path:
    raw = os.environ.get("QUAID_VISIBLE_HOME", "").strip()
    if raw:
        return Path(raw).resolve()
    if quaid_home.name.startswith(".") and len(quaid_home.name) > 1:
        return quaid_home.with_name(quaid_home.name[1:])
    return quaid_home


def _safe_remove_project_dir(path: Path, allowed_roots: List[Path]) -> bool:
    """Best-effort guarded recursive delete for project directories."""
    try:
        resolved = path.resolve()
    except Exception:
        resolved = Path(os.path.realpath(str(path)))

    allowed = False
    for root in allowed_roots:
        try:
            root_resolved = root.resolve()
        except Exception:
            root_resolved = Path(os.path.realpath(str(root)))
        if resolved == root_resolved or str(resolved).startswith(str(root_resolved) + os.sep):
            allowed = True
            break
    if not allowed:
        logger.warning("Refusing to delete project dir outside allowed roots: %s", resolved)
        return False
    if not resolved.exists():
        return False
    if not resolved.is_dir():
        logger.warning("Refusing to delete non-directory project path: %s", resolved)
        return False
    shutil.rmtree(resolved)
    return True


def _misc_project_instance_id(project_name: str) -> Optional[str]:
    name = str(project_name or "").strip()
    if not name.startswith("misc--"):
        return None
    instance = name.removeprefix("misc--").strip()
    return instance or None


def _misc_auto_create_tombstone_path(quaid_home: Path, instance_id: str) -> Path:
    return quaid_home / "instances" / str(instance_id).strip() / "data" / "project-bootstrap" / "skip-auto-misc-project"


def mark_misc_auto_create_disabled(instance_id: str, *, quaid_home: Optional[Path] = None) -> Path:
    home = quaid_home.resolve() if quaid_home is not None else _resolve_quaid_home()
    marker = _misc_auto_create_tombstone_path(home, instance_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "instance_id": str(instance_id).strip(),
                "disabled_at": datetime.now(tz=timezone.utc).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return marker


def clear_misc_auto_create_disabled(instance_id: str, *, quaid_home: Optional[Path] = None) -> None:
    home = quaid_home.resolve() if quaid_home is not None else _resolve_quaid_home()
    marker = _misc_auto_create_tombstone_path(home, instance_id)
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


def is_misc_auto_create_disabled(instance_id: str, *, quaid_home: Optional[Path] = None) -> bool:
    home = quaid_home.resolve() if quaid_home is not None else _resolve_quaid_home()
    marker = _misc_auto_create_tombstone_path(home, instance_id)
    return marker.is_file()


def _is_path_for_deleted_project(raw_path: str, *, project_name: str, canonical: Optional[Path]) -> bool:
    text = str(raw_path or "").strip()
    if not text:
        return False
    if f"/{project_name}/" in text or text.endswith(f"/{project_name}") or text.endswith(f"/{project_name}/PROJECT.md"):
        return True
    if canonical is not None:
        try:
            c = canonical.resolve()
        except Exception:
            c = Path(os.path.realpath(str(canonical)))
        p = Path(text)
        if not p.is_absolute():
            return False
        try:
            r = p.resolve()
        except Exception:
            r = Path(os.path.realpath(str(p)))
        if r == c or str(r).startswith(str(c) + os.sep):
            return True
    return False


def _cleanup_pending_project_review_entries(
    *,
    quaid_home: Path,
    project_name: str,
    canonical: Optional[Path],
) -> int:
    """Remove stale pending-project-review entries for a deleted project."""
    instances_dir = quaid_home / "instances"
    if not instances_dir.is_dir():
        return 0
    removed = 0
    for child in sorted(instances_dir.iterdir()):
        queue_path = child / "logs" / "janitor" / "pending-project-review.json"
        if not queue_path.is_file():
            continue
        try:
            raw = json.loads(queue_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse pending project review queue %s: %s", queue_path, exc)
            continue
        if not isinstance(raw, list):
            continue

        keep: List[Any] = []
        before = len(raw)
        for entry in raw:
            if not isinstance(entry, dict):
                keep.append(entry)
                continue
            hint = str(entry.get("project_hint") or "").strip()
            source_file = str(entry.get("source_file") or "").strip()
            if hint == project_name or _is_path_for_deleted_project(source_file, project_name=project_name, canonical=canonical):
                removed += 1
                continue
            keep.append(entry)
        if len(keep) == before:
            continue
        if keep:
            queue_path.write_text(json.dumps(keep, indent=2) + "\n", encoding="utf-8")
        else:
            queue_path.unlink(missing_ok=True)
    return removed


def _cleanup_staged_project_events(
    *,
    quaid_home: Path,
    visible_home: Path,
    project_name: str,
    canonical: Optional[Path],
) -> int:
    """Remove queued/failed staging events for a deleted project."""
    staging_dirs: List[Path] = [
        visible_home / "projects" / "staging",
        visible_home / "projects" / "staging" / "failed",
    ]

    instances_dir = quaid_home / "instances"
    if instances_dir.is_dir():
        for child in sorted(instances_dir.iterdir()):
            if not child.is_dir():
                continue
            staging_dirs.extend(
                [
                    child / "projects" / "staging",
                    child / "projects" / "staging" / "failed",
                ]
            )

    seen_dirs: set[str] = set()
    removed = 0
    for staging_dir in staging_dirs:
        try:
            key = str(staging_dir.resolve())
        except Exception:
            key = str(staging_dir)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        if not staging_dir.is_dir():
            continue
        for event_file in sorted(staging_dir.glob("*.json")):
            try:
                event = json.loads(event_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            project_hint = str(event.get("project_hint") or event.get("project") or "").strip()
            if project_hint == project_name:
                event_file.unlink(missing_ok=True)
                removed += 1
                continue
            files_touched = event.get("files_touched") or []
            if isinstance(files_touched, list):
                should_remove = False
                for raw_path in files_touched:
                    if _is_path_for_deleted_project(str(raw_path or ""), project_name=project_name, canonical=canonical):
                        should_remove = True
                        break
                if should_remove:
                    event_file.unlink(missing_ok=True)
                    removed += 1
    return removed


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
    """Atomically write the registry file.

    Callers that do read-modify-write updates must hold _registry_lock()
    across the full mutation cycle so they do not race on stale reads.
    """
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(".tmp")
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


_DOCS_REGISTRY_RECONCILING = False
_DOCS_REGISTRY_RECONCILE_LOCK = threading.RLock()


def _reconcile_docs_registry_projects() -> None:
    """Repair docs-registry project rows that predate canonical registry writes."""
    global _DOCS_REGISTRY_RECONCILING
    with _DOCS_REGISTRY_RECONCILE_LOCK:
        if _DOCS_REGISTRY_RECONCILING:
            return
        _DOCS_REGISTRY_RECONCILING = True
        try:
            from datastore.docsdb.registry import DocsRegistry

            DocsRegistry(seed_projects=False).reconcile_global_project_registry()
        except Exception as exc:
            logger.warning("Docs/project registry reconciliation skipped: %s", exc)
            try:
                from lib.fail_policy import is_fail_hard_enabled
            except Exception:
                fail_hard = False
            else:
                fail_hard = bool(is_fail_hard_enabled())
            if fail_hard:
                raise
        finally:
            _DOCS_REGISTRY_RECONCILING = False


def list_projects() -> Dict[str, Dict[str, Any]]:
    """Return all registered projects."""
    _reconcile_docs_registry_projects()
    return _load_registry().get("projects", {})


def get_project(name: str) -> Optional[Dict[str, Any]]:
    """Get a single project by name, or None if not found."""
    _reconcile_docs_registry_projects()
    return _load_registry().get("projects", {}).get(name)


def create_project(
    name: str,
    description: str = "",
    source_root: Optional[str] = None,
    initial_instance: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a new project.

    Args:
        name: Project name (lowercase, kebab-case)
        description: Human-readable description
        source_root: Path to user's project files (optional)
        initial_instance: Instance ID to associate. If None, reads from
            QUAID_INSTANCE env var. Pass explicitly when calling from silo
            init where QUAID_INSTANCE is not yet in the environment.

    Returns:
        The project entry dict.

    Raises:
        ValueError: If project already exists or name is invalid.
    """
    import re
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
        raise ValueError(f"Invalid project name: {name!r} (must be lowercase kebab-case)")

    quaid_home = _resolve_quaid_home()
    canonical = quaid_projects_dir(quaid_home) / name
    tracking_base = quaid_tracking_dir(quaid_home)

    with _registry_lock():
        registry = _load_registry()
        if name in registry["projects"]:
            raise ValueError(f"Project already exists: {name}")
        if initial_instance is not None:
            current_instance = str(initial_instance).strip()
        else:
            from lib.instance import instance_id as _instance_id
            current_instance = _instance_id()

        entry = {
            "canonical_path": str(canonical),
            "source_root": source_root,
            "instances": [current_instance],
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
                render_project_md_template(
                    label=name.replace("-", " ").title(),
                    description=description or f"{name} project.",
                    project_home=str(canonical),
                    source_roots=[source_root] if source_root else [],
                    exclude_patterns=[],
                ),
                encoding="utf-8",
            )

        # Initialize shadow git if source_root provided
        if source_root:
            try:
                from core.shadow_git import ShadowGit
                sg = ShadowGit(
                    name,
                    Path(source_root),
                    tracking_base=tracking_base,
                )
                sg.init()
                sg.snapshot()  # Initial baseline
                logger.info("Initialized shadow git for %s at %s", name, source_root)
            except Exception as e:
                logger.warning("Failed to init shadow git for %s: %s", name, e)

        registry["projects"][name] = entry
        _save_registry(registry)
        misc_instance = _misc_project_instance_id(name)
        if misc_instance:
            clear_misc_auto_create_disabled(misc_instance, quaid_home=quaid_home)
    try:
        _sync_docs_registry_project(
            name,
            description=description,
            source_root=source_root,
            canonical=canonical,
        )
    except Exception as e:
        logger.warning("Failed to sync docs registry for %s: %s", name, e)

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
    with _registry_lock():
        registry = _load_registry()
        if name not in registry["projects"]:
            raise KeyError(f"Project not found: {name}")

        allowed = {"source_root", "description", "instances"}
        for key, value in updates.items():
            if key in allowed:
                registry["projects"][name][key] = value

        _save_registry(registry)
        entry = registry["projects"][name]
    try:
        _sync_docs_registry_project(
            name,
            description=str(entry.get("description") or ""),
            source_root=entry.get("source_root"),
            canonical=Path(entry.get("canonical_path", "")),
        )
    except Exception as e:
        logger.warning("Failed to sync docs registry update for %s: %s", name, e)
    return registry["projects"][name]


def link_project(name: str, *, instance_id: Optional[str] = None) -> Dict[str, Any]:
    """Add the current QUAID_INSTANCE to a project's instances list.

    Used when a second adapter wants to participate in an existing project
    without taking ownership. Idempotent — safe to call if already linked.

    Args:
        name: Project name.
        instance_id: Optional explicit instance ID. When omitted, reads the
            current QUAID_INSTANCE from the environment.

    Returns:
        The updated project entry.

    Raises:
        KeyError: If project not found.
    """
    with _registry_lock():
        registry = _load_registry()
        if name not in registry["projects"]:
            raise KeyError(f"Project not found: {name}")

        if instance_id is not None:
            instance = str(instance_id).strip()
        else:
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
    with _registry_lock():
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
    quaid_home = _resolve_quaid_home()
    visible_home = _visible_home_from_hidden(quaid_home)
    tracking_base = quaid_tracking_dir(quaid_home)
    entry: Dict[str, Any]
    canonical_path: Optional[Path] = None
    with _registry_lock():
        registry = _load_registry()
        if name not in registry["projects"]:
            raise KeyError(f"Project not found: {name}")

        entry = registry["projects"][name]
        raw_canonical = str(entry.get("canonical_path") or "").strip()
        if raw_canonical:
            canonical_path = Path(raw_canonical)

        # Clean up shadow git tracking
        try:
            from core.shadow_git import ShadowGit
            source_root = entry.get("source_root")
            if source_root:
                sg = ShadowGit(
                    name,
                    Path(source_root),
                    tracking_base=tracking_base,
                )
                sg.destroy()
        except Exception as e:
            logger.warning("Failed to destroy shadow git for %s: %s", name, e)

        # Clean up project directories (canonical + standard fallbacks).
        allowed_roots = [
            visible_home / "projects",
            quaid_home / "projects",
        ]
        candidate_dirs: List[Path] = [visible_home / "projects" / name, quaid_home / "projects" / name]
        if canonical_path is not None:
            candidate_dirs.insert(0, canonical_path)

        seen_dirs: set[str] = set()
        for candidate in candidate_dirs:
            try:
                key = str(candidate.resolve())
            except Exception:
                key = str(candidate)
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            try:
                _safe_remove_project_dir(candidate, allowed_roots)
            except Exception as e:
                logger.warning("Failed to remove project directory for %s (%s): %s", name, candidate, e)

        # Remove from registry
        del registry["projects"][name]
        _save_registry(registry)
    misc_instance = _misc_project_instance_id(name)
    if misc_instance:
        try:
            mark_misc_auto_create_disabled(misc_instance, quaid_home=quaid_home)
        except Exception as e:
            logger.warning(
                "Failed to persist misc auto-create tombstone for %s (%s): %s",
                name,
                misc_instance,
                e,
            )

    # Project docs workers are supervisor-owned. Deleting a project must stop
    # the worker and remove queued force-update state so deleted projects do
    # not resurrect on the next supervisor tick.
    try:
        from core import project_docs

        project_docs.stop_worker(name)
        for path in (
            project_docs.request_path(name),
            project_docs.state_path(name),
            project_docs.worker_heartbeat_path(name),
            project_docs.worker_pid_path(name),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception as e:
        logger.warning("Failed to clean up project docs worker state for %s: %s", name, e)

    # Clean up shared docs DB: project definitions, registry rows, and RAG chunks.
    try:
        from lib.database import get_connection
        from lib.config import get_docs_db_path
        from datastore.docsdb.rag import DocsRAG

        docs_db_path = get_docs_db_path()
        chunk_paths: List[str] = []
        with get_connection(docs_db_path) as conn:
            rows = conn.execute(
                "SELECT file_path FROM doc_registry WHERE project = ?",
                (name,),
            ).fetchall()
            chunk_paths.extend(str(r[0] or "").strip() for r in rows if str(r[0] or "").strip())
            conn.execute("DELETE FROM project_definitions WHERE name = ?", (name,))
            conn.execute("DELETE FROM doc_registry WHERE project = ?", (name,))
        canonical = str(entry.get("canonical_path") or "").strip()
        if canonical:
            chunk_paths.append(str(Path(canonical) / "PROJECT.md"))
        if chunk_paths:
            rag = DocsRAG(db_path=docs_db_path)
            seen: set[str] = set()
            for file_path in chunk_paths:
                key = str(file_path or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                rag.remove_chunks_for_path(key)
    except Exception as e:
        logger.warning("Failed to clean up DB entries for project %s: %s", name, e)

    # Clean up stale pending review hints and queued project events.
    try:
        removed_hints = _cleanup_pending_project_review_entries(
            quaid_home=quaid_home,
            project_name=name,
            canonical=canonical_path,
        )
        removed_events = _cleanup_staged_project_events(
            quaid_home=quaid_home,
            visible_home=visible_home,
            project_name=name,
            canonical=canonical_path,
        )
        if removed_hints or removed_events:
            logger.info(
                "Project delete cleanup for %s removed %d pending hints and %d staged events",
                name,
                removed_hints,
                removed_events,
            )
    except Exception as e:
        logger.warning("Failed post-delete queue cleanup for project %s: %s", name, e)

    logger.info("Deleted project: %s", name)


def rename_project(old_name: str, new_name: str) -> Dict[str, Any]:
    """Rename a project: update all registry entries, move directory, refresh config.

    Delegates to DocsRegistry which owns the full rename operation (DB rows,
    directory move, project_definitions update, global registry sync).

    Returns:
        {"renamed": count, "dir_moved": bool}

    Raises:
        ValueError: If old_name does not exist or new_name is already taken.
    """
    from datastore.docsdb.registry import DocsRegistry
    registry = DocsRegistry()
    return registry.rename_project(old_name, new_name)


def archive_project(name: str) -> Dict[str, Any]:
    """Archive a project: set all entries to archived state, move dir to archive/.

    Delegates to DocsRegistry which owns the archive operation.

    Returns:
        {"archived": count, "dir_moved": bool}

    Raises:
        ValueError: If project does not exist.
    """
    from datastore.docsdb.registry import DocsRegistry
    registry = DocsRegistry()
    return registry.archive_project(name)


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
