#!/usr/bin/env python3
"""CLI for the project system (project registry, shadow git, sync engine).

Usage:
    python3 project_registry_cli.py list
    python3 project_registry_cli.py create <name> [--description "..."] [--source-root /path]
    python3 project_registry_cli.py show <name>
    python3 project_registry_cli.py update <name> [--description "..."] [--source-root /path]
    python3 project_registry_cli.py delete <name>
    python3 project_registry_cli.py rename <old_name> <new_name>
    python3 project_registry_cli.py archive <name> [--yes]
    python3 project_registry_cli.py status <name>
    python3 project_registry_cli.py diff <name> [--stat|--full]
    python3 project_registry_cli.py snapshot [<name>]
    python3 project_registry_cli.py history <name> [file]
    python3 project_registry_cli.py show-version <name> <rev> <file>
    python3 project_registry_cli.py restore <name> <rev> <file> [--yes]
    python3 project_registry_cli.py sync
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# Ensure plugin root is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def add_json_argument(parser: argparse.ArgumentParser) -> None:
    """Allow both `--json command` and `command --json` forms."""
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output")


def _live_instance_ids() -> set[str]:
    """Best-effort current instance IDs from on-disk silos."""
    try:
        from lib.instance import list_instances
        return set(str(name or "").strip() for name in list_instances() if str(name or "").strip())
    except Exception:
        return set()


def _dedupe_instances(instances: Any) -> List[str]:
    ordered: List[str] = []
    seen: set[str] = set()
    for raw in list(instances or []):
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _instance_view(entry: Dict[str, Any], live_instances: set[str]) -> Dict[str, Any]:
    rendered = dict(entry or {})
    raw_instances = _dedupe_instances(rendered.get("instances", []))
    if live_instances:
        raw_instances = [name for name in raw_instances if name in live_instances]
    rendered["instances"] = raw_instances
    return rendered


def _current_instance_id() -> str:
    raw = os.environ.get("QUAID_INSTANCE", "").strip()
    if raw:
        return raw
    try:
        from lib.instance import instance_id
        return str(instance_id() or "").strip()
    except Exception:
        return ""


def _linked_to_current_instance(entry: Dict[str, Any]) -> bool:
    current = _current_instance_id()
    if not current:
        return True
    return current in _dedupe_instances((entry or {}).get("instances", []))


def _scope_projects_to_current_instance(projects: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    current = _current_instance_id()
    if not current:
        return dict(projects or {})
    return {
        name: entry
        for name, entry in (projects or {}).items()
        if current in _dedupe_instances((entry or {}).get("instances", []))
    }


def _project_visible_to_current_instance(project: Optional[Dict[str, Any]]) -> bool:
    return bool(project) and _linked_to_current_instance(project)


def _require_project_visible(name: str, project: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not _project_visible_to_current_instance(project):
        print(f"Project not found: {name}", file=sys.stderr)
        sys.exit(1)
    return project


def _print_existing_project_context(name: str, project: Optional[Dict[str, Any]]) -> None:
    """Explain create conflicts that are hidden by current-instance scoping."""
    if not project:
        return
    instances = _dedupe_instances(project.get("instances", []))
    current = _current_instance_id()
    print(
        "Project names are global across Quaid instances; `project create` "
        "does not create a per-instance duplicate.",
        file=sys.stderr,
    )
    if instances:
        print(
            f"Existing project '{name}' is linked to instance(s): {', '.join(instances)}",
            file=sys.stderr,
        )
    else:
        print(f"Existing project '{name}' is not linked to any instance.", file=sys.stderr)
    if current and current not in instances:
        print(
            f"Current instance '{current}' is not linked; run `quaid project link {name}` "
            "to attach it, or delete the stale project before recreating it.",
            file=sys.stderr,
        )


def cmd_list(args):
    from core.project_registry import list_projects
    projects = _scope_projects_to_current_instance(list_projects())
    names_only = bool(getattr(args, "names_only", False))
    if args.json:
        live_instances = _live_instance_ids()
        rendered = {
            name: _instance_view(entry, live_instances)
            for name, entry in projects.items()
        }
        print(json.dumps(rendered, indent=2))
        return
    if not projects:
        if not names_only:
            print("No projects registered.")
        return
    live_instances = _live_instance_ids()
    rendered = {
        name: _instance_view(entry, live_instances)
        for name, entry in projects.items()
    }
    if names_only:
        for name in sorted(rendered.keys()):
            print(name)
        return
    for name, entry in sorted(rendered.items()):
        src = entry.get("source_root") or "(no source root)"
        desc = entry.get("description", "")
        print(f"  {name}: {desc}")
        print(f"    source: {src}")
        print(f"    instances: {', '.join(entry.get('instances', []))}")


def cmd_create(args):
    from core.project_registry import create_project, get_project
    try:
        entry = create_project(
            args.name,
            description=args.description or "",
            source_root=args.source_root,
        )
        print(f"Created project: {args.name}")
        if args.json:
            print(json.dumps(entry, indent=2))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        if "already exists" in str(e).lower():
            try:
                _print_existing_project_context(args.name, get_project(args.name))
            except Exception as context_exc:
                print(f"Could not load existing project context: {context_exc}", file=sys.stderr)
        sys.exit(1)
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_show(args):
    from core.project_registry import get_project
    project = _require_project_visible(args.name, get_project(args.name))
    print(
        json.dumps(
            {args.name: _instance_view(project, _live_instance_ids())},
            indent=2,
        )
    )


def cmd_update(args):
    from core.project_registry import get_project, update_project
    _require_project_visible(args.name, get_project(args.name))
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


def cmd_link(args):
    from core.project_registry import link_project
    try:
        entry = link_project(args.name)
        instances = ", ".join(entry.get("instances", []))
        print(f"Linked to project '{args.name}' — instances: {instances}")
        if args.json:
            print(json.dumps(entry, indent=2))
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_unlink(args):
    from core.project_registry import unlink_project
    try:
        entry = unlink_project(args.name)
        instances = ", ".join(entry.get("instances", [])) or "(none)"
        print(f"Unlinked from project '{args.name}' — remaining instances: {instances}")
        if args.json:
            print(json.dumps(entry, indent=2))
    except (KeyError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_delete(args):
    from core.project_registry import _is_reserved_project_name, delete_project, get_project
    if not _is_reserved_project_name(args.name):
        _require_project_visible(args.name, get_project(args.name))
    try:
        delete_project(args.name)
        print(f"Deleted project: {args.name}")
    except (KeyError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_rename(args):
    from core.project_registry import get_project, rename_project
    _require_project_visible(args.old_name, get_project(args.old_name))
    try:
        result = rename_project(args.old_name, args.new_name)
        print(f"Renamed project '{args.old_name}' → '{args.new_name}'")
        if args.json:
            print(json.dumps(result, indent=2))
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_archive(args):
    from core.project_registry import get_project, archive_project
    if not args.yes:
        _require_project_visible(args.name, get_project(args.name))
        ans = input(f"Archive project '{args.name}'? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return
    else:
        _require_project_visible(args.name, get_project(args.name))
    try:
        result = archive_project(args.name)
        print(f"Archived project: {args.name}")
        if args.json:
            print(json.dumps(result, indent=2))
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    from core.project_registry import get_project
    from core.project_docs import project_status, format_status
    _require_project_visible(args.name, get_project(args.name))
    try:
        status = project_status(args.name)
        if args.json:
            print(json.dumps(status, indent=2))
        else:
            print(format_status(status))
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_diff(args):
    from core.project_registry import get_project
    from core.project_docs import project_diff, format_diff
    _require_project_visible(args.name, get_project(args.name))
    try:
        diff = project_diff(args.name, full=bool(getattr(args, "full", False)))
        if args.json:
            print(json.dumps(diff, indent=2))
        else:
            print(format_diff(diff))
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_snapshot(args):
    if args.name:
        from core.project_registry import get_project
        from core.shadow_git import ShadowGit
        from lib.adapter import get_adapter, quaid_tracking_dir
        from pathlib import Path

        project = _require_project_visible(args.name, get_project(args.name))
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


def _shadow_git_for_visible_project(name: str):
    from core.project_registry import get_project
    from core.shadow_git import ShadowGit
    from lib.adapter import get_adapter, quaid_tracking_dir
    from pathlib import Path

    project = _require_project_visible(name, get_project(name))
    source_root = project.get("source_root")
    if not source_root:
        print(f"No source root for {name}", file=sys.stderr)
        sys.exit(1)

    adapter = get_adapter()
    sg = ShadowGit(
        name,
        Path(source_root),
        tracking_base=quaid_tracking_dir(adapter.quaid_home()),
    )
    if not sg.initialized:
        print(f"No shadow git history for {name}", file=sys.stderr)
        sys.exit(1)
    return sg


def cmd_history(args):
    try:
        sg = _shadow_git_for_visible_project(args.name)
        entries = sg.history(args.file, limit=args.limit)
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps({"project": args.name, "file": args.file, "history": entries}, indent=2))
        return
    if not entries:
        target = f" for {args.file}" if args.file else ""
        print(f"No shadow git history for {args.name}{target}.")
        return
    for entry in entries:
        print(f"{entry['short']}  {entry['committed_at']}  {entry['subject']}")


def cmd_show_version(args):
    try:
        sg = _shadow_git_for_visible_project(args.name)
        data = sg.show_file(args.rev, args.file)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.buffer.write(data)


def cmd_restore(args):
    try:
        sg = _shadow_git_for_visible_project(args.name)
        if not args.yes and not sys.stdin.isatty():
            print("Error: --yes is required when restore is run non-interactively.", file=sys.stderr)
            sys.exit(1)
        if not args.yes:
            ans = input(f"Restore '{args.file}' in project '{args.name}' from {args.rev}? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("Aborted.")
                return
        restored = sg.restore_file(args.rev, args.file)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps({
            "project": args.name,
            "file": args.file,
            "rev": args.rev,
            "restored_path": str(restored),
        }, indent=2))
    else:
        print(f"Restored {args.file} from {args.rev} -> {restored}")


def main():
    parser = argparse.ArgumentParser(description="Quaid project system CLI")
    parser.add_argument("--json", action="store_true", help="JSON output")
    subparsers = parser.add_subparsers(dest="command")

    # list
    list_p = subparsers.add_parser("list", help="List all registered projects")
    list_p.add_argument(
        "--names-only",
        "--quiet",
        dest="names_only",
        action="store_true",
        help="Print one project name per line with no header/chatter",
    )
    add_json_argument(list_p)

    # create
    create_p = subparsers.add_parser("create", help="Create a new project")
    create_p.add_argument("name", help="Project name (stored lowercase)")
    create_p.add_argument("--description", "-d", help="Project description")
    create_p.add_argument("--source-root", "-s", help="Path to source files")
    add_json_argument(create_p)

    # show
    show_p = subparsers.add_parser("show", help="Show project details")
    show_p.add_argument("name", help="Project name")
    add_json_argument(show_p)

    # update
    update_p = subparsers.add_parser("update", help="Update project fields")
    update_p.add_argument("name", help="Project name")
    update_p.add_argument("--description", "-d", help="New description")
    update_p.add_argument("--source-root", "-s", help="New source root path")
    add_json_argument(update_p)

    # link
    link_p = subparsers.add_parser("link", help="Add current instance to a project's instances list")
    link_p.add_argument("name", help="Project name")
    add_json_argument(link_p)

    # unlink
    unlink_p = subparsers.add_parser("unlink", help="Remove current instance from a project's instances list")
    unlink_p.add_argument("name", help="Project name")
    add_json_argument(unlink_p)

    # delete
    delete_p = subparsers.add_parser("delete", help="Delete a project")
    delete_p.add_argument("name", help="Project name")

    # rename
    rename_p = subparsers.add_parser("rename", help="Rename a project")
    rename_p.add_argument("old_name", help="Current project name")
    rename_p.add_argument("new_name", help="New project name")
    add_json_argument(rename_p)

    # archive
    archive_p = subparsers.add_parser("archive", help="Archive a project")
    archive_p.add_argument("name", help="Project name")
    archive_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    add_json_argument(archive_p)

    # status
    status_p = subparsers.add_parser("status", help="Show project docs freshness/status")
    status_p.add_argument("name", help="Project name")
    add_json_argument(status_p)

    # diff
    diff_p = subparsers.add_parser("diff", help="Show pending project docs update diff")
    diff_p.add_argument("name", help="Project name")
    diff_p.add_argument("--stat", action="store_true", help="Show compact diff/stat output")
    diff_p.add_argument("--full", action="store_true", help="Show full git diff")
    add_json_argument(diff_p)

    # snapshot
    snap_p = subparsers.add_parser("snapshot", help="Take shadow git snapshot(s)")
    snap_p.add_argument("name", nargs="?", help="Project name (all if omitted)")

    # shadow git history
    history_p = subparsers.add_parser("history", help="Show shadow git history for a project or file")
    history_p.add_argument("name", help="Project name")
    history_p.add_argument("file", nargs="?", help="Optional project-relative file path")
    history_p.add_argument("--limit", type=int, default=20, help="Maximum commits to show")
    add_json_argument(history_p)

    show_version_p = subparsers.add_parser("show-version", help="Print a tracked file from a shadow git revision")
    show_version_p.add_argument("name", help="Project name")
    show_version_p.add_argument("rev", help="Shadow git commit/revision")
    show_version_p.add_argument("file", help="Project-relative file path")

    restore_p = subparsers.add_parser("restore", help="Restore a tracked file from a shadow git revision")
    restore_p.add_argument("name", help="Project name")
    restore_p.add_argument("rev", help="Shadow git commit/revision")
    restore_p.add_argument("file", help="Project-relative file path")
    restore_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    add_json_argument(restore_p)

    args = parser.parse_args()

    commands = {
        "list": cmd_list,
        "create": cmd_create,
        "show": cmd_show,
        "update": cmd_update,
        "link": cmd_link,
        "unlink": cmd_unlink,
        "delete": cmd_delete,
        "rename": cmd_rename,
        "archive": cmd_archive,
        "status": cmd_status,
        "diff": cmd_diff,
        "snapshot": cmd_snapshot,
        "history": cmd_history,
        "show-version": cmd_show_version,
        "restore": cmd_restore,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
