#!/usr/bin/env python3
"""CLI for the project system (project registry, shadow git, sync engine).

Usage:
    python3 project_registry_cli.py list
    python3 project_registry_cli.py create <name> [--description "..."] [--source-root /path]
    python3 project_registry_cli.py show <name>
    python3 project_registry_cli.py update <name> [--description "..."] [--source-root /path]
    python3 project_registry_cli.py delete <name>
    python3 project_registry_cli.py snapshot [<name>]
    python3 project_registry_cli.py sync
"""

import argparse
import json
import os
import sys

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def cmd_list(args):
    from core.project_registry import list_projects
    projects = list_projects()
    if not projects:
        print("No projects registered.")
        return
    if args.json:
        print(json.dumps(projects, indent=2))
        return
    for name, entry in sorted(projects.items()):
        src = entry.get("source_root") or "(no source root)"
        desc = entry.get("description", "")
        print(f"  {name}: {desc}")
        print(f"    source: {src}")
        print(f"    instances: {', '.join(entry.get('instances', []))}")


def cmd_create(args):
    from core.project_registry import create_project
    try:
        entry = create_project(
            args.name,
            description=args.description or "",
            source_root=args.source_root,
        )
        print(f"Created project: {args.name}")
        if args.json:
            print(json.dumps(entry, indent=2))
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_show(args):
    from core.project_registry import get_project
    project = get_project(args.name)
    if not project:
        print(f"Project not found: {args.name}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({args.name: project}, indent=2))


def cmd_update(args):
    from core.project_registry import update_project
    updates = {}
    if args.description is not None:
        updates["description"] = args.description
    if args.source_root is not None:
        updates["source_root"] = args.source_root
    if not updates:
        print("Nothing to update.", file=sys.stderr)
        sys.exit(1)
    try:
        entry = update_project(args.name, **updates)
        print(f"Updated project: {args.name}")
        if args.json:
            print(json.dumps(entry, indent=2))
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_delete(args):
    from core.project_registry import delete_project
    try:
        delete_project(args.name)
        print(f"Deleted project: {args.name}")
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_snapshot(args):
    if args.name:
        from core.project_registry import get_project
        from core.shadow_git import ShadowGit
        from lib.adapter import get_adapter, quaid_tracking_dir
        from pathlib import Path

        project = get_project(args.name)
        if not project:
            print(f"Project not found: {args.name}", file=sys.stderr)
            sys.exit(1)
        if not project.get("source_root"):
            print(f"No source root for {args.name}", file=sys.stderr)
            sys.exit(1)

        adapter = get_adapter()
        sg = ShadowGit(
            args.name,
            Path(project["source_root"]),
            tracking_base=quaid_tracking_dir(adapter.quaid_home()),
        )
        if not sg.initialized:
            sg.init()
        result = sg.snapshot()
        if result:
            print(f"Snapshot for {args.name}: {len(result.changes)} changes")
            for c in result.changes:
                print(f"  {c.status}\t{c.path}")
        else:
            print(f"No changes in {args.name}")
    else:
        from core.project_registry import snapshot_all_projects
        results = snapshot_all_projects()
        if not results:
            print("No changes detected across any projects.")
            return
        for snap in results:
            print(f"{snap['project']}: {len(snap['changes'])} changes")
            for c in snap["changes"]:
                print(f"  {c['status']}\t{c['path']}")


def cmd_sync(args):
    from core.sync_engine import sync_all_projects
    results = sync_all_projects()
    if not results:
        print("No projects to sync (adapter has no sync target).")
        return
    for sr in results:
        parts = []
        if sr.copied:
            parts.append(f"copied: {', '.join(sr.copied)}")
        if sr.removed:
            parts.append(f"removed: {', '.join(sr.removed)}")
        if sr.skipped:
            parts.append(f"skipped: {', '.join(sr.skipped)}")
        print(f"  {sr.project}: {'; '.join(parts) or 'up to date'}")


def main():
    parser = argparse.ArgumentParser(description="Quaid project system CLI")
    parser.add_argument("--json", action="store_true", help="JSON output")
    subparsers = parser.add_subparsers(dest="command")

    # list
    subparsers.add_parser("list", help="List all registered projects")

    # create
    create_p = subparsers.add_parser("create", help="Create a new project")
    create_p.add_argument("name", help="Project name (lowercase kebab-case)")
    create_p.add_argument("--description", "-d", help="Project description")
    create_p.add_argument("--source-root", "-s", help="Path to source files")

    # show
    show_p = subparsers.add_parser("show", help="Show project details")
    show_p.add_argument("name", help="Project name")

    # update
    update_p = subparsers.add_parser("update", help="Update project fields")
    update_p.add_argument("name", help="Project name")
    update_p.add_argument("--description", "-d", help="New description")
    update_p.add_argument("--source-root", "-s", help="New source root path")

    # delete
    delete_p = subparsers.add_parser("delete", help="Delete a project")
    delete_p.add_argument("name", help="Project name")

    # snapshot
    snap_p = subparsers.add_parser("snapshot", help="Take shadow git snapshot(s)")
    snap_p.add_argument("name", nargs="?", help="Project name (all if omitted)")

    # sync
    subparsers.add_parser("sync", help="Sync project files to adapter workspaces")

    args = parser.parse_args()

    commands = {
        "list": cmd_list,
        "create": cmd_create,
        "show": cmd_show,
        "update": cmd_update,
        "delete": cmd_delete,
        "snapshot": cmd_snapshot,
        "sync": cmd_sync,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
