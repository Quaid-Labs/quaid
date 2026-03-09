"""Global project registry — cross-instance project tracking.

Maps project names to canonical filesystem paths and tracks which adapter
instances (claude-code, openclaw, standalone) are linked to each project.

Registry file: <quaid_home>/project-registry.json

Schema:
    {
        "projects": {
            "quaid": {
                "canonical_path": "/Users/me/quaid/projects/quaid",
                "instances": ["claude-code", "openclaw"],
                "created_at": "2026-03-09T...",
                "description": "Knowledge layer project"
            }
        }
    }

Operations:
    - register: add or update a project entry
    - link: add an instance to a project's tracking list
    - unlink: remove an instance from a project's tracking list
    - lookup: find a project by name
    - list_all: list all registered projects
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _registry_path() -> Path:
    """Resolve the global registry file path."""
    try:
        from lib.adapter import get_adapter
        return get_adapter().quaid_home() / "project-registry.json"
    except Exception:
        home = os.environ.get("QUAID_HOME", "").strip()
        root = Path(home).resolve() if home else Path.home() / "quaid"
        return root / "project-registry.json"


def _load() -> Dict[str, Any]:
    """Load the registry, returning empty structure if missing."""
    p = _registry_path()
    if not p.is_file():
        return {"projects": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if "projects" not in data:
            data["projects"] = {}
        return data
    except (json.JSONDecodeError, OSError):
        return {"projects": {}}


def _save(data: Dict[str, Any]) -> None:
    """Write the registry atomically."""
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def _adapter_name() -> str:
    """Get current adapter instance name from config."""
    try:
        from lib.adapter import get_adapter
        return type(get_adapter()).__name__.replace("Adapter", "").lower()
    except Exception:
        return "standalone"


def register(
    name: str,
    canonical_path: str,
    description: str = "",
    link_current_instance: bool = True,
) -> Dict[str, Any]:
    """Register or update a project in the global registry.

    If the project already exists, updates its canonical_path and description.
    Optionally links the current adapter instance.

    Returns the project entry.
    """
    data = _load()
    now = datetime.now().isoformat()

    if name in data["projects"]:
        entry = data["projects"][name]
        entry["canonical_path"] = canonical_path
        if description:
            entry["description"] = description
        entry["updated_at"] = now
    else:
        entry = {
            "canonical_path": canonical_path,
            "instances": [],
            "created_at": now,
            "description": description,
        }
        data["projects"][name] = entry

    if link_current_instance:
        instance = _adapter_name()
        if instance not in entry.get("instances", []):
            entry.setdefault("instances", []).append(instance)

    _save(data)
    return entry


def link(name: str, instance: Optional[str] = None, create_symlink: bool = False) -> bool:
    """Add an instance to a project's tracking list.

    If create_symlink=True, creates a symlink in the current adapter's
    projects/ directory pointing to the canonical project path.

    Returns True if the link was added, False if already linked or project
    not found.
    """
    data = _load()
    if name not in data["projects"]:
        return False

    instance = instance or _adapter_name()
    entry = data["projects"][name]
    if instance in entry.get("instances", []):
        return False

    entry.setdefault("instances", []).append(instance)
    entry["updated_at"] = datetime.now().isoformat()
    _save(data)

    if create_symlink:
        _create_project_symlink(name, entry["canonical_path"])

    return True


def _create_project_symlink(name: str, canonical_path: str) -> None:
    """Create a symlink in the adapter's projects/ dir to the canonical path."""
    try:
        from lib.adapter import get_adapter
        projects_dir = get_adapter().projects_dir()
    except Exception:
        home = os.environ.get("QUAID_HOME", "").strip()
        root = Path(home).resolve() if home else Path.home() / "quaid"
        projects_dir = root / "projects"

    link_path = projects_dir / name
    canonical = Path(canonical_path)

    if link_path.exists():
        if link_path.is_symlink() and link_path.resolve() == canonical.resolve():
            return  # Already correct
        # Don't overwrite existing real directories
        if not link_path.is_symlink():
            return

    projects_dir.mkdir(parents=True, exist_ok=True)
    if link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(canonical)


def unlink(name: str, instance: Optional[str] = None) -> bool:
    """Remove an instance from a project's tracking list.

    Returns True if unlinked, False if not found or not linked.
    """
    data = _load()
    if name not in data["projects"]:
        return False

    instance = instance or _adapter_name()
    entry = data["projects"][name]
    instances = entry.get("instances", [])
    if instance not in instances:
        return False

    instances.remove(instance)
    entry["updated_at"] = datetime.now().isoformat()
    _save(data)
    return True


def lookup(name: str) -> Optional[Dict[str, Any]]:
    """Look up a project by name. Returns entry dict or None."""
    data = _load()
    return data["projects"].get(name)


def list_all() -> Dict[str, Dict[str, Any]]:
    """Return all registered projects."""
    return _load()["projects"]


def remove(name: str, force: bool = False) -> bool:
    """Remove a project from the global registry.

    If force=False and other instances are still tracking, raises ValueError.
    Returns True if removed.
    """
    data = _load()
    if name not in data["projects"]:
        return False

    entry = data["projects"][name]
    instances = entry.get("instances", [])
    current = _adapter_name()

    if not force and len(instances) > 1:
        others = [i for i in instances if i != current]
        raise ValueError(
            f"Project '{name}' is tracked by other instances: {others}. "
            f"Use unlink to remove from this instance, or force=True to delete globally."
        )

    del data["projects"][name]
    _save(data)
    return True


def tracking_instances(name: str) -> List[str]:
    """Return list of instances tracking a project."""
    entry = lookup(name)
    return entry.get("instances", []) if entry else []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Global project registry")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List all registered projects")
    p_list.add_argument("--json", action="store_true", help="Output as JSON")

    p_show = sub.add_parser("show", help="Show a project entry")
    p_show.add_argument("name", help="Project name")

    p_reg = sub.add_parser("register", help="Register a project")
    p_reg.add_argument("name", help="Project name")
    p_reg.add_argument("path", help="Canonical filesystem path")
    p_reg.add_argument("--description", default="", help="Project description")

    p_link = sub.add_parser("link", help="Link current instance to a project")
    p_link.add_argument("name", help="Project name")
    p_link.add_argument("--instance", help="Instance name (default: current adapter)")
    p_link.add_argument("--symlink", action="store_true", help="Create symlink in projects/ dir")

    p_unlink = sub.add_parser("unlink", help="Unlink instance from a project")
    p_unlink.add_argument("name", help="Project name")
    p_unlink.add_argument("--instance", help="Instance name (default: current adapter)")

    p_rm = sub.add_parser("remove", help="Remove project from registry")
    p_rm.add_argument("name", help="Project name")
    p_rm.add_argument("--force", action="store_true", help="Force remove even if other instances track it")

    args = parser.parse_args()

    if args.command == "list":
        projects = list_all()
        if args.json:
            print(json.dumps(projects, indent=2))
        elif not projects:
            print("No projects registered.")
        else:
            for name, entry in sorted(projects.items()):
                instances = ", ".join(entry.get("instances", []))
                desc = entry.get("description", "")
                path_str = entry.get("canonical_path", "?")
                print(f"  {name}: {path_str}")
                if desc:
                    print(f"    {desc}")
                if instances:
                    print(f"    instances: {instances}")

    elif args.command == "show":
        entry = lookup(args.name)
        if entry:
            print(json.dumps(entry, indent=2))
        else:
            print(f"Project '{args.name}' not found.", file=sys.stderr)
            sys.exit(1)

    elif args.command == "register":
        entry = register(args.name, args.path, args.description)
        print(f"Registered '{args.name}' -> {entry['canonical_path']}")

    elif args.command == "link":
        if link(args.name, args.instance, create_symlink=args.symlink):
            print(f"Linked '{args.name}' to {args.instance or _adapter_name()}")
            if args.symlink:
                print(f"  Symlink created in projects/ directory")
        else:
            print(f"Already linked or project not found.", file=sys.stderr)
            sys.exit(1)

    elif args.command == "unlink":
        if unlink(args.name, args.instance):
            print(f"Unlinked '{args.name}' from {args.instance or _adapter_name()}")
        else:
            print(f"Not linked or project not found.", file=sys.stderr)
            sys.exit(1)

    elif args.command == "remove":
        try:
            if remove(args.name, force=args.force):
                print(f"Removed '{args.name}' from registry.")
            else:
                print(f"Project '{args.name}' not found.", file=sys.stderr)
                sys.exit(1)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
