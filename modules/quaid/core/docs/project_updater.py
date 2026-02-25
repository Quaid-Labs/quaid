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
from datastore.docsdb.registry import DocsRegistry
from core.docs.updater import update_doc_from_diffs, update_doc_from_transcript, get_doc_purposes, log_doc_update
from lib.runtime_context import get_workspace_dir
# llm_clients imported indirectly via docs_updater (update_doc_from_diffs calls Opus)

def _workspace() -> Path:
    return get_workspace_dir()


def _resolve_path(relative: str) -> Path:
    """Resolve a workspace-relative path to absolute."""
    p = Path(relative)
    if p.is_absolute():
        return p
    return _workspace() / relative


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
    _normalize_tools_paths(registry, project_name, cfg)

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
    _normalize_tools_paths(registry, project_name, cfg)
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

    # Keep registry current so file-list rendering reflects real project contents.
    try:
        registry.auto_discover(project_name)
    except Exception as e:
        print(f"  Warning: auto_discover failed for {project_name}: {e}")

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
    if in_dir:
        in_dir_text = "\n".join(f"- {d['file_path']}" for d in sorted(in_dir, key=lambda x: x["file_path"]))
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

    files_assets_block = (
        "## Files & Assets\n\n"
        "### In This Directory\n"
        "<!-- Auto-discovered — all files in this directory belong to this project -->\n"
        f"{in_dir_text}\n\n"
        "### External Files\n"
        f"{ext_text}\n"
    )

    # Canonical rewrite: replace the full Files & Assets block if present.
    # This handles malformed/missing subheadings from older project docs.
    files_assets_pattern = r"## Files & Assets\s*\n.*?(?=\n## |\Z)"
    if re.search(files_assets_pattern, content, flags=re.DOTALL):
        content = re.sub(
            files_assets_pattern,
            files_assets_block + "\n",
            content,
            flags=re.DOTALL,
        )
    else:
        # Insert directly after Overview when available, otherwise after first H1.
        overview_pattern = r"(## Overview\s*\n.*?)(?=\n## |\Z)"
        if re.search(overview_pattern, content, flags=re.DOTALL):
            content = re.sub(
                overview_pattern,
                rf"\1\n\n{files_assets_block}",
                content,
                count=1,
                flags=re.DOTALL,
            )
        else:
            lines = content.splitlines()
            if lines and lines[0].startswith("# "):
                content = "\n".join([lines[0], "", files_assets_block, "", *lines[1:]])
            else:
                content = files_assets_block + "\n" + content

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


def _normalize_tools_paths(registry: DocsRegistry, project_name: str, cfg) -> None:
    """Normalize path references in TOOLS.md to workspace-relative form.

    Example: PROJECT.md -> ~/projects/<project>/PROJECT.md
    """
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        return

    home_rel = str(defn.home_dir).strip("/").lstrip("./")
    home_abs = _resolve_path(defn.home_dir)
    if not home_abs.exists():
        return

    docs = registry.list_docs(project=project_name)
    tools_candidates = {d["file_path"] for d in docs if Path(d["file_path"]).name == "TOOLS.md"}
    default_tools = f"{home_rel}/TOOLS.md"
    if (home_abs / "TOOLS.md").exists():
        tools_candidates.add(default_tools)

    # Fast set for known in-project files.
    existing_rel = set()
    for fp in home_abs.rglob("*"):
        if fp.is_file():
            existing_rel.add(str(fp.relative_to(home_abs)).replace("\\", "/"))
    for core_name in ("PROJECT.md", "README.md", "TOOLS.md", "AGENTS.md"):
        if (home_abs / core_name).exists():
            existing_rel.add(core_name)

    def _normalize_code_span(token: str) -> str:
        t = token.strip()
        if not t:
            return token
        if t.startswith("~/") or t.startswith("/") or "://" in t:
            return token
        if t.startswith("./"):
            t = t[2:]
        if t.startswith("../"):
            return token
        if t in existing_rel:
            return f"~/{home_rel}/{t}"
        return token

    def _normalize_plain_named_files(text: str) -> str:
        for core_name in ("PROJECT.md", "README.md", "TOOLS.md", "AGENTS.md"):
            if core_name not in existing_rel:
                continue
            text = re.sub(
                rf"(^|[^A-Za-z0-9_./~-])({re.escape(core_name)})(?=$|[^A-Za-z0-9_./~-])",
                rf"\1~/{home_rel}/{core_name}",
                text,
            )
        # Normalize bare/relative file references in plain prose (non-code spans),
        # e.g. "See index.html and styles.css".
        allowed_ext = {
            "md", "txt", "rst", "html", "css", "js", "mjs", "cjs", "ts", "tsx",
            "jsx", "json", "yaml", "yml", "py", "sh", "toml", "sql", "graphql",
            "proto", "ini", "env", "csv", "xml",
        }
        def _path_repl(match: re.Match[str]) -> str:
            token = match.group(1)
            if token.startswith(("http://", "https://", "~/", "/")):
                return token
            # Avoid mutating already path-like multi-segment refs unless they
            # are known project-local paths discovered from disk.
            if "/" in token and token not in existing_rel:
                return token
            ext = token.rsplit(".", 1)[-1].lower() if "." in token else ""
            if token in existing_rel or ext in allowed_ext:
                return f"~/{home_rel}/{token}"
            return token

        text = re.sub(
            r"(?<![~/`\\w-])([A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8})(?![\\w/-])",
            _path_repl,
            text,
        )
        # Repair legacy malformed rewrites from earlier buggy normalization.
        text = re.sub(
            r"~/p~/projects/([^/\s]+)/rojects/\1/([A-Za-z0-9_.-]+)",
            r"~/projects/\1/\2",
            text,
        )
        return text

    def _inject_workspace_index(text: str) -> str:
        rel_files = sorted(existing_rel)
        key_files = []
        for preferred in (
            "PROJECT.md", "README.md", "TOOLS.md", "AGENTS.md", "package.json",
            "docker-compose.yml", "docs/api.md", "index.html", "styles.css",
        ):
            if preferred in rel_files and preferred not in key_files:
                key_files.append(preferred)
        for rel in rel_files:
            if len(key_files) >= 16:
                break
            if rel not in key_files and not rel.startswith("seeds/"):
                key_files.append(rel)

        lines = [
            "## Project Workspace",
            f"- Root: `~/{home_rel}/`",
            "- Key Files:",
        ]
        if key_files:
            lines.extend([f"  - `~/{home_rel}/{rel}`" for rel in key_files])
        else:
            lines.append(f"  - `~/{home_rel}/PROJECT.md`")
        block = "\n".join(lines) + "\n"

        section_pattern = r"## Project Workspace\s*\n.*?(?=\n## |\Z)"
        if re.search(section_pattern, text, flags=re.DOTALL):
            return re.sub(section_pattern, block, text, flags=re.DOTALL)

        # Insert after the first heading for predictability.
        heading_pattern = r"^(# .+\n)"
        if re.search(heading_pattern, text):
            return re.sub(heading_pattern, rf"\1\n{block}\n", text, count=1)
        return block + "\n" + text

    for rel_path in sorted(tools_candidates):
        abs_path = _resolve_path(rel_path)
        if not abs_path.exists():
            continue
        try:
            content = abs_path.read_text()
        except OSError:
            continue

        original = content

        # Rewrite path-like code spans to workspace-relative.
        content = re.sub(
            r"`([^`\n]+)`",
            lambda m: f"`{_normalize_code_span(m.group(1))}`",
            content,
        )
        # Rewrite plain mentions like "See PROJECT.md ..."
        content = _normalize_plain_named_files(content)
        # Ensure every TOOLS doc has an explicit project root + key file index.
        content = _inject_workspace_index(content)

        if content != original:
            tmp_path = abs_path.with_suffix(".tmp")
            try:
                tmp_path.write_text(content)
                tmp_path.replace(abs_path)
            except OSError as e:
                print(f"  Error writing TOOLS.md path normalization for {rel_path}: {e}", file=sys.stderr)
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
        from core.runtime.events import queue_delayed_notification
        for doc_path in updates_applied:
            message = (
                "[Quaid] 📋 Project Documentation Update\n"
                f"Project: {project_name}\n"
                f"Updated: `{Path(doc_path).name}`\n"
                f"Trigger: project-{trigger}"
            )
            queue_delayed_notification(
                message,
                kind="project_doc_update",
                priority="normal",
                source="project_updater",
            )
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
