#!/usr/bin/env python3
"""
Project Updater — Background event processor for PROJECT.md and doc updates.

Runs as a background subprocess spawned from compact/reset hooks.
Processes event files written by the plugin to update project docs.

Usage:
  python3 project_updater.py process-event <event-file>
  python3 project_updater.py process-all
  python3 project_updater.py refresh-project-md <project-name>
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import get_config
from docs_registry import DocsRegistry
from docs_updater import update_doc_from_diffs, update_doc_from_transcript, get_doc_purposes, log_doc_update
# llm_clients imported indirectly via docs_updater (update_doc_from_diffs calls Opus)

WORKSPACE = Path(os.environ.get("CLAWDBOT_WORKSPACE", str(Path(__file__).resolve().parent.parent.parent)))


def _resolve_path(relative: str) -> Path:
    """Resolve a workspace-relative path to absolute."""
    p = Path(relative)
    if p.is_absolute():
        return p
    return WORKSPACE / relative


def process_event(event_path: str) -> Dict:
    """Process a single project event file.

    Steps:
    1. Read event JSON
    2. Resolve project from project_hint + files_touched
    3. Read PROJECT.md
    4. Run mtime staleness check on tracked source files
    5. Call Opus for update decisions
    6. Apply updates
    7. Check related projects for cascade
    8. Notify user
    9. Delete event file

    Returns dict with processing results.
    """
    # Consistent result template
    result = {
        "success": False,
        "project": None,
        "updates": 0,
        "trigger": None,
        "error": None,
    }

    event_file = Path(event_path)
    if not event_file.exists():
        print(f"Event file not found: {event_path}")
        result["error"] = "event_file_not_found"
        return result

    try:
        event = json.loads(event_file.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"Invalid event file {event_path}: {e}")
        _cleanup_event(event_file)
        result["error"] = f"invalid_event_json: {e}"
        return result
    project_hint = event.get("project_hint")
    files_touched = event.get("files_touched", [])
    summary = event.get("summary", "")
    trigger = event.get("trigger", "unknown")
    result["trigger"] = trigger

    print(f"Processing event: trigger={trigger}, project_hint={project_hint}")
    print(f"  Files touched: {len(files_touched)}")
    print(f"  Summary: {summary[:100]}...")

    registry = DocsRegistry()
    try:
        cfg = get_config()
    except Exception as e:
        print(f"  Failed to load config: {e}")
        _cleanup_event(event_file)
        result["error"] = f"config_load_failed: {e}"
        return result

    # Resolve project
    project_name = _resolve_project(registry, project_hint, files_touched)
    if not project_name:
        print(f"  Could not resolve project, moving to failed/")
        # Move to failed/ instead of deleting — allows manual retry
        try:
            failed_dir = event_file.parent / "failed"
            failed_dir.mkdir(exist_ok=True)
            event_file.rename(failed_dir / event_file.name)
        except Exception:
            _cleanup_event(event_file)
        result["error"] = "project_not_resolved"
        return result

    result["project"] = project_name
    print(f"  Resolved project: {project_name}")

    try:
        defn = cfg.projects.definitions.get(project_name)
    except (AttributeError, TypeError) as e:
        print(f"  Invalid config structure: {e}")
        _cleanup_event(event_file)
        result["error"] = f"invalid_config: {e}"
        return result
    if not defn:
        print(f"  Project '{project_name}' not in config definitions")
        _cleanup_event(event_file)
        result["error"] = "project_not_in_config"
        return result

    # Read PROJECT.md
    project_md_path = _resolve_path(defn.home_dir) / "PROJECT.md"
    project_md_content = ""
    if project_md_path.exists():
        project_md_content = project_md_path.read_text()

    # Check staleness of tracked docs via registry
    stale_docs = _check_registry_staleness(registry, project_name)

    # Decide what to update
    updates_needed = []
    if stale_docs:
        updates_needed.extend(stale_docs)
    if summary:
        updates_needed.append({"type": "summary", "content": summary})

    if not updates_needed and not summary:
        print(f"  Nothing to update for {project_name}")
        _cleanup_event(event_file)
        result["success"] = True
        return result

    # Call Opus to decide what to update
    updates_applied = _apply_updates(
        registry, project_name, project_md_content,
        summary, stale_docs, trigger, files_touched
    )

    # Refresh PROJECT.md file list
    _refresh_file_list(registry, project_name, cfg)

    # Notify user
    _notify_user(project_name, updates_applied, trigger)

    # Clean up event file
    _cleanup_event(event_file)

    result["success"] = True
    result["updates"] = len(updates_applied)
    return result


def process_all_events() -> Dict:
    """Process all queued events in staging directory chronologically."""
    cfg = get_config()
    staging_dir = _resolve_path(cfg.projects.staging_dir)

    if not staging_dir.exists():
        print("No staging directory")
        return {"processed": 0}

    event_files = sorted(staging_dir.glob("*.json"))
    if not event_files:
        print("No queued events")
        return {"processed": 0}

    print(f"Found {len(event_files)} queued event(s)")
    processed = 0
    errors = 0

    for event_file in event_files:
        try:
            result = process_event(str(event_file))
            if result.get("success"):
                processed += 1
            else:
                errors += 1
        except Exception as e:
            print(f"  Error processing {event_file.name}: {e}")
            errors += 1
            # Move failed event aside rather than deleting
            try:
                failed_dir = staging_dir / "failed"
                failed_dir.mkdir(exist_ok=True)
                event_file.rename(failed_dir / event_file.name)
            except Exception:
                pass

    # Cleanup: cap failed/ directory at 20 entries max
    try:
        failed_dir = staging_dir / "failed"
        if failed_dir.exists():
            failed_files = sorted(failed_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
            if len(failed_files) > 20:
                for old_file in failed_files[:-20]:
                    old_file.unlink()
                print(f"  Cleaned up {len(failed_files) - 20} old failed event(s)")
    except Exception:
        pass

    print(f"\nProcessed {processed} event(s), {errors} error(s)")
    return {"processed": processed, "errors": errors}


def refresh_project_md(project_name: str) -> bool:
    """Regenerate PROJECT.md file list section from directory scan.

    Preserves curated sections (Overview, Related Projects, Update Rules).
    Only auto-generates the Files & Assets section.
    """
    cfg = get_config()
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        print(f"Project '{project_name}' not found in config")
        return False

    registry = DocsRegistry()
    _refresh_file_list(registry, project_name, cfg)
    return True


# ============================================================================
# Internal helpers
# ============================================================================

def _resolve_project(
    registry: DocsRegistry,
    project_hint: Optional[str],
    files_touched: List[str],
) -> Optional[str]:
    """Resolve which project this event belongs to."""
    # Try hint first
    if project_hint:
        cfg = get_config()
        if project_hint in cfg.projects.definitions:
            return project_hint

    # Try files_touched
    for f in files_touched:
        project = registry.find_project_for_path(f)
        if project:
            return project

    return None


def _check_registry_staleness(
    registry: DocsRegistry,
    project_name: str,
) -> List[Dict]:
    """Check which docs in a project are stale (source newer than doc)."""
    mappings = registry.get_source_mappings(project=project_name)
    stale = []

    for doc_path, source_paths in mappings.items():
        doc_abs = _resolve_path(doc_path)
        if not doc_abs.exists():
            continue

        doc_mtime = doc_abs.stat().st_mtime
        stale_sources = []

        for src in source_paths:
            src_abs = _resolve_path(src)
            if src_abs.exists() and src_abs.stat().st_mtime > doc_mtime:
                stale_sources.append(src)

        if stale_sources:
            stale.append({
                "doc_path": doc_path,
                "stale_sources": stale_sources,
                "doc_mtime": doc_mtime,
            })

    return stale


def _apply_updates(
    registry: DocsRegistry,
    project_name: str,
    project_md_content: str,
    summary: str,
    stale_docs: List[Dict],
    trigger: str,
    files_touched: Optional[List[str]] = None,
) -> List[str]:
    """Apply doc updates using Opus for decision-making."""
    updates_applied = []

    # Update stale docs using existing docs_updater infrastructure
    if stale_docs:
        purposes = get_doc_purposes()
        for stale_info in stale_docs:
            doc_path = stale_info["doc_path"]
            sources = stale_info["stale_sources"]
            purpose = purposes.get(doc_path, "")

            # Also check registry for description
            entry = registry.get(doc_path)
            if entry and entry.get("description"):
                purpose = purpose or entry["description"]

            print(f"  Updating stale doc: {doc_path}")
            ok = update_doc_from_diffs(
                doc_path, purpose, sources,
                dry_run=False, trigger=trigger,
            )
            # Fallback: if no git diffs (untracked files), use event summary as transcript
            if not ok and summary:
                print(f"  No git diffs, falling back to transcript-based update")
                ok = update_doc_from_transcript(
                    doc_path, purpose, summary,
                    dry_run=False, trigger=trigger,
                )
            if ok:
                updates_applied.append(doc_path)
                now = datetime.now().isoformat()
                registry.update_timestamps(doc_path, modified_at=now)

    # If we have a summary but no stale docs, check if PROJECT.md itself needs updating
    if summary and not stale_docs:
        cfg = get_config()
        defn = cfg.projects.definitions.get(project_name)
        if defn and project_md_content:
            # Simple: append summary to PROJECT.md overview or notes
            print(f"  Summary captured for {project_name} (no stale docs)")
            # Don't auto-modify PROJECT.md with every summary — that would be noisy
            # Just log it for now
            log_doc_update(
                f"projects/{project_name}/PROJECT.md",
                trigger, [], f"Session summary: {summary[:100]}",
                dry_run=True, success=True, chars_before=len(project_md_content),
                chars_after=len(project_md_content), notify=False,
            )

    return updates_applied


def _refresh_file_list(registry: DocsRegistry, project_name: str, cfg) -> None:
    """Refresh the Files & Assets section of PROJECT.md."""
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        return

    project_md_path = _resolve_path(defn.home_dir) / "PROJECT.md"
    if not project_md_path.exists():
        return

    content = project_md_path.read_text()
    docs = registry.list_docs(project=project_name)

    # Use canonical real paths for reliable comparison (macOS /private/var symlinks)
    home_real = str(Path(os.path.realpath(_resolve_path(defn.home_dir)))).rstrip("/") + "/"

    # Classify files
    in_dir = []
    external = []
    for d in docs:
        file_real = str(Path(os.path.realpath(_resolve_path(d["file_path"]))))
        if file_real.startswith(home_real):
            in_dir.append(d)
        else:
            external.append(d)

    # Build in-directory listing
    in_dir_text = ""
    if in_dir:
        in_dir_text = "\n".join(f"- {d['file_path']}" for d in in_dir)
    else:
        in_dir_text = "(none yet)"

    # Build external files table
    ext_header = "| File | Purpose | Auto-Update |\n|------|---------|-------------|"
    ext_rows = []
    for d in external:
        purpose = d.get("description") or ""
        auto = "Yes" if d.get("auto_update") else "—"
        ext_rows.append(f"| {d['file_path']} | {purpose} | {auto} |")
    ext_text = ext_header + "\n" + "\n".join(ext_rows) if ext_rows else ext_header

    # Use HTML comment markers for reliable section replacement
    # (more robust than regex against markdown headings)
    original = content

    # Replace In This Directory section using marker
    content = re.sub(
        r"(<!-- Auto-discovered [^>]*-->)\n.*?(?=\n### External Files|\n<!-- BEGIN:external)",
        rf"\g<1>\n{in_dir_text}",
        content,
        flags=re.DOTALL,
    )

    # Fallback: try heading-based replacement if marker not found
    if content == original:
        content = re.sub(
            r"(### In This Directory\n).*?(\n### External Files)",
            rf"\g<1><!-- Auto-discovered — all files in this directory belong to this project -->\n{in_dir_text}\n\n\g<2>",
            content,
            flags=re.DOTALL,
        )

    # Replace External Files section
    original2 = content
    ext_pattern = r"(### External Files\n).*?(\n## Documents|\n## Related|\n## Update)"
    if re.search(ext_pattern, content, flags=re.DOTALL):
        content = re.sub(
            ext_pattern,
            rf"\g<1>{ext_text}\n\n\g<2>",
            content,
            flags=re.DOTALL,
        )
    else:
        print(f"  Warning: Could not update External Files section (headings not found)")

    # Atomic write: write to temp file, then rename
    tmp_path = project_md_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(content)
        tmp_path.replace(project_md_path)
    except OSError as e:
        print(f"  Error writing PROJECT.md: {e}", file=sys.stderr)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _notify_user(project_name: str, updates_applied: List[str], trigger: str) -> None:
    """Notify user about project updates."""
    if not updates_applied:
        return

    try:
        from notify import notify_doc_update
        for doc_path in updates_applied:
            notify_doc_update(doc_path, f"project-{trigger}", f"Updated as part of {project_name} project")
    except Exception as e:
        print(f"  Notification failed: {e}")


def _cleanup_event(event_file: Path) -> None:
    """Delete processed event file."""
    try:
        if event_file.exists():
            event_file.unlink()
    except Exception as e:
        print(f"  Failed to cleanup event file: {e}")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Project Updater")
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # process-event
    pe_p = subparsers.add_parser("process-event", help="Process a single event file")
    pe_p.add_argument("event_file", help="Path to event JSON file")

    # process-all
    subparsers.add_parser("process-all", help="Process all queued events")

    # refresh-project-md
    rp_p = subparsers.add_parser("refresh-project-md", help="Regenerate PROJECT.md file list")
    rp_p.add_argument("project_name", help="Project name")

    # check
    subparsers.add_parser("check", help="List pending events without processing")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "check":
        cfg = get_config()
        staging_dir = _resolve_path(cfg.projects.staging_dir)
        if not staging_dir.exists():
            print("No staging directory")
            return
        event_files = sorted(staging_dir.glob("*.json"))
        if not event_files:
            print("No pending events")
            return
        print(f"Pending events: {len(event_files)}")
        for f in event_files:
            try:
                data = json.loads(f.read_text())
                proj = data.get("project_hint", data.get("project", "?"))
                trigger = data.get("trigger", "?")
                ts = data.get("timestamp", "?")
                print(f"  {f.name}: project={proj} trigger={trigger} time={ts}")
            except Exception:
                print(f"  {f.name}: (unreadable)")

    elif args.command == "process-event":
        result = process_event(args.event_file)
        print(json.dumps(result, indent=2))

    elif args.command == "process-all":
        result = process_all_events()
        print(json.dumps(result, indent=2))

    elif args.command == "refresh-project-md":
        ok = refresh_project_md(args.project_name)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
