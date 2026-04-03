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
import logging
import os
import re
import signal
import sys
import time

logger = logging.getLogger(__name__)
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import get_config
from datastore.docsdb.registry import (
    DocsRegistry,
    _managed_project_sections,
    _populate_project_md_sections,
)
from datastore.docsdb.updater import update_doc_from_diffs, update_doc_from_transcript, get_doc_purposes, log_doc_update
from lib.delayed_requests import queue_delayed_request
from lib.project_templates import (
    EXTERNAL_FILES_BEGIN,
    EXTERNAL_FILES_END,
    has_registry_managed_markers,
    IN_DIR_FILES_BEGIN,
    IN_DIR_FILES_END,
    PROJECT_HOME_BEGIN,
    PROJECT_HOME_END,
    PROJECT_LOG_BEGIN,
    PROJECT_LOG_END,
    REGISTERED_DOCS_BEGIN,
    REGISTERED_DOCS_END,
    SOURCE_ROOTS_BEGIN,
    SOURCE_ROOTS_END,
    render_project_md_template,
    replace_managed_block,
)
from lib.runtime_context import get_workspace_dir, get_quaid_home, get_data_dir
# llm_clients imported indirectly via docs_updater (update_doc_from_diffs calls Deep Reasoning)
PROJECT_HISTORY_FILENAME = "PROJECT.log"

def _workspace() -> Path:
    return get_workspace_dir()


def _resolve_path(relative: str) -> Path:
    """Resolve a runtime path to absolute.

    Canonical project homes live at QUAID_HOME/projects/, but per-instance
    staging still lives at <instance>/projects/staging/.
    """
    p = Path(relative)
    if p.is_absolute():
        return p
    if relative == "projects" or (
        relative.startswith("projects/")
        and relative != "projects/staging"
        and not relative.startswith("projects/staging/")
    ):
        return get_quaid_home() / relative
    if relative.startswith("shared/"):
        return get_quaid_home() / relative
    return _workspace() / relative


def _resolve_project_home(home_dir: str) -> Path:
    """Resolve a project home_dir to an absolute path.

    Canonical project homes (`projects/...`) live under QUAID_HOME. Legacy
    `shared/...` homes also resolve under QUAID_HOME.
    """
    return _resolve_path(home_dir)


def process_event(event_path: str) -> Dict:
    """Process a single project event file.

    Steps:
    1. Read event JSON
    2. Resolve project from project_hint + files_touched
    3. Read PROJECT.md
    4. Run mtime staleness check on tracked source files
    5. Call Deep Reasoning for update decisions
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
    print(f"  Summary: {summary}")

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
    project_md_path = _resolve_project_home(defn.home_dir) / "PROJECT.md"
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

    # Call Deep Reasoning to decide what to update
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
            except Exception as move_err:
                print(
                    f"  Warning: failed to move event {event_file.name} into failed/: {move_err}",
                    file=sys.stderr,
                )

    # Cleanup: cap failed/ directory at 20 entries max
    try:
        failed_dir = staging_dir / "failed"
        if failed_dir.exists():
            failed_files = sorted(failed_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
            if len(failed_files) > 20:
                for old_file in failed_files[:-20]:
                    old_file.unlink()
                print(f"  Cleaned up {len(failed_files) - 20} old failed event(s)")
    except Exception as cleanup_err:
        print(f"  Warning: failed-event cleanup skipped: {cleanup_err}", file=sys.stderr)

    print(f"\nProcessed {processed} event(s), {errors} error(s)")
    return {"processed": processed, "errors": errors}


def refresh_project_md(project_name: str) -> bool:
    """Refresh registry-backed navigation sections inside PROJECT.md."""
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
    """Apply doc updates using Deep Reasoning for decision-making."""
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
                trigger, [], f"Session summary: {summary}",
                dry_run=True, success=True, chars_before=len(project_md_content),
                chars_after=len(project_md_content), notify=False,
            )

    return updates_applied


def evaluate_doc_health(
    project_name: str,
    dry_run: bool = False,
) -> Dict:
    """Decision matrix: evaluate which docs to create, update, or archive.

    One deep LLM call that examines:
    - PROJECT.md and current docs
    - Recent PROJECT.log entries
    - Files changed in recent sessions
    - Gaps: areas with code but no documentation

    Returns decisions:
    - create: new docs to scaffold for undocumented areas
    - update: existing docs needing refresh (extends staleness check)
    - archive: obsolete docs to soft-delete

    This is called from the janitor or manually — not on every event.
    """
    result = {
        "project": project_name,
        "create": [],
        "update": [],
        "archive": [],
        "dry_run": dry_run,
        "error": None,
    }

    cfg = get_config()
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        result["error"] = f"Project '{project_name}' not found"
        return result

    registry = DocsRegistry()
    project_dir = _resolve_path(defn.home_dir)
    project_md_path = project_dir / "PROJECT.md"

    if not project_md_path.exists():
        result["error"] = f"PROJECT.md not found at {project_md_path}"
        return result

    project_md = project_md_path.read_text()
    if len(project_md) > 5000:
        logger.warning(
            "PROJECT.md for %s is %d chars — consider pruning (expected <5000)",
            project_name, len(project_md),
        )

    # Gather existing docs
    docs = registry.list_docs(project=project_name)
    doc_listing = "\n".join(
        f"- {d['file_path']}: {d.get('description', '')}" for d in docs
    ) or "(no docs registered)"

    # Gather recent project log entries — read full file (rotation keeps it bounded).
    log_path = project_dir / PROJECT_HISTORY_FILENAME
    recent_log = ""
    if log_path.exists():
        recent_log = log_path.read_text(encoding="utf-8").strip()

    # Gather source roots for gap analysis
    source_roots = defn.source_roots or []
    source_listing = ", ".join(source_roots) or "(none configured)"

    # Build the decision prompt
    prompt = f"""You are a project documentation health evaluator.

Given the current state of the "{project_name}" project, decide what documentation actions are needed.

## Current PROJECT.md
{project_md}

## Registered Documents
{doc_listing}

## Recent Project Log (last 30 entries)
{recent_log or "(no log entries)"}

## Source Roots
{source_listing}

## Your Task
Analyze the project state and return a JSON object with these arrays:

- "create": docs that SHOULD exist but don't. Each entry: {{"path": "docs/suggested-name.md", "title": "Doc Title", "reason": "why this doc is needed"}}
- "update": existing docs that seem outdated based on log activity. Each entry: {{"path": "existing/path.md", "reason": "what needs updating"}}
- "archive": docs that appear obsolete or redundant. Each entry: {{"path": "existing/path.md", "reason": "why this can be archived"}}

Rules:
- New docs MUST be placed in the docs/ subdirectory (e.g., "docs/architecture.md", "docs/api-reference.md")
- Only suggest creating docs for areas with clear, sustained activity (not one-off mentions)
- Only suggest archiving docs that are clearly obsolete (missing source files, deprecated features)
- Be conservative — fewer, high-confidence decisions are better than many speculative ones
- Return empty arrays if no action is needed

Respond with JSON only, no markdown fences."""

    try:
        from lib.adapter import get_adapter
        provider = get_adapter().get_llm_provider()
        llm_result = provider.llm_call(
            system=prompt,
            user="Evaluate documentation health and return decisions as JSON.",
            tier="deep",
            max_tokens=1500,
        )
        output = str(llm_result.get("text", "")).strip()

        # Parse JSON from output
        json_match = re.search(r"\{[\s\S]*\}", output)
        if json_match:
            decisions = json.loads(json_match.group())
            result["create"] = decisions.get("create", [])
            result["update"] = decisions.get("update", [])
            result["archive"] = decisions.get("archive", [])
        else:
            result["error"] = "LLM returned non-JSON output"
            return result

    except Exception as e:
        result["error"] = f"LLM call failed: {e}"
        return result

    if dry_run:
        print(f"\n[doc-health] {project_name} (dry run):")
        for action in ("create", "update", "archive"):
            items = result[action]
            if items:
                print(f"  {action}:")
                for item in items:
                    print(f"    - {item.get('path', '?')}: {item.get('reason', '')}")
        return result

    # Apply create decisions: scaffold new docs
    for item in result["create"]:
        doc_path = item.get("path", "")
        title = item.get("title", "Untitled")
        if not doc_path:
            continue
        full_path = project_dir / doc_path
        if full_path.exists():
            continue  # Don't overwrite
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(f"# {title}\n\n<!-- Auto-created by project updater -->\n\n")
        # Register in doc registry
        try:
            rel = str(full_path.relative_to(_workspace()))
            registry.register(
                file_path=rel,
                project=project_name,
                asset_type="doc",
                title=title,
                registered_by="doc-health-evaluator",
            )
            print(f"  Created: {doc_path}")
        except Exception as e:
            print(f"  Created {doc_path} but registry failed: {e}")

    # Apply archive decisions: soft-delete in registry
    for item in result["archive"]:
        doc_path = item.get("path", "")
        if not doc_path:
            continue
        try:
            registry.unregister(doc_path)
            print(f"  Archived: {doc_path}")
        except Exception as e:
            print(f"  Archive failed for {doc_path}: {e}")

    return result


def _refresh_file_list(registry: DocsRegistry, project_name: str, cfg) -> None:
    """Refresh registry-backed navigation sections of PROJECT.md."""
    defn = cfg.projects.definitions.get(project_name)
    if not defn:
        return

    project_md_path = _resolve_path(defn.home_dir) / "PROJECT.md"
    if not project_md_path.exists():
        return

    content = project_md_path.read_text(encoding="utf-8")
    sections = _managed_project_sections(registry, project_name, defn)
    has_markers = has_registry_managed_markers(content)
    if has_markers:
        content = _populate_project_md_sections(
            content,
            project_home_body=sections["project_home"],
            source_roots_body=sections["source_roots"],
            in_dir_body=sections["in_dir"],
            external_body=sections["external"],
            registered_docs_body=sections["registered_docs"],
        )
    else:
        rebuilt = render_project_md_template(
            label=defn.label,
            description=defn.description or f"{defn.label} project.",
            project_home=str(_resolve_project_home(defn.home_dir)),
            source_roots=[str(registry._resolve_path(root)) for root in (defn.source_roots or [])],
            exclude_patterns=defn.exclude or [],
        )
        rebuilt = _populate_project_md_sections(
            rebuilt,
            project_home_body=sections["project_home"],
            source_roots_body=sections["source_roots"],
            in_dir_body=sections["in_dir"],
            external_body=sections["external"],
            registered_docs_body=sections["registered_docs"],
        )
        if PROJECT_LOG_BEGIN in content and PROJECT_LOG_END in content:
            match = re.search(
                re.escape(PROJECT_LOG_BEGIN) + r"(.*?)" + re.escape(PROJECT_LOG_END),
                content,
                flags=re.DOTALL,
            )
            if match:
                rebuilt = replace_managed_block(rebuilt, PROJECT_LOG_BEGIN, PROJECT_LOG_END, match.group(1).strip())
        content = rebuilt
        print("  Rebuilt PROJECT.md with canonical navigation scaffold")

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


def _project_md_recent_log_limit(default: int = 15) -> int:
    raw = os.getenv("QUAID_PROJECT_MD_RECENT_LIMIT", str(default))
    try:
        limit = int(raw)
    except Exception:
        limit = default
    return max(1, limit)




def append_project_logs(
    project_logs: Dict[str, List[str]],
    trigger: str = "Compaction",
    date_str: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Append project log bullets to per-project PROJECT.md files.

    Project logs are written under:
      ## Recent Major Changes
      <!-- BEGIN:PROJECT_LOG -->
      - YYYY-MM-DD [Trigger] note
      <!-- END:PROJECT_LOG -->
    """
    metrics = {
        "projects_seen": 0,
        "projects_updated": 0,
        "entries_seen": 0,
        "entries_written": 0,
        "projects_unknown": 0,
        "projects_missing_file": 0,
    }
    if not isinstance(project_logs, dict) or not project_logs:
        return metrics

    cfg = get_config()
    registry = DocsRegistry()
    today = date_str or datetime.now().strftime("%Y-%m-%d")
    marker_begin = PROJECT_LOG_BEGIN
    marker_end = PROJECT_LOG_END
    recent_limit = _project_md_recent_log_limit()
    session_prefix_re = re.compile(r"^\s*Session\s+\d+\s*(?:\([^)]*\))?\s*:\s*", flags=re.IGNORECASE)

    def _normalize_log_entry(raw: object) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        text = re.sub(r"^\s*[-*+]\s*", "", text)
        text = session_prefix_re.sub("", text)
        return re.sub(r"\s+", " ", text).strip()

    def _append_project_history_log(project_md_path: Path, entries: List[str]) -> int:
        """Append normalized project log entries to PROJECT.log (no dedupe/folding)."""
        normalized = [_normalize_log_entry(x) for x in entries]
        normalized = [x for x in normalized if x]
        if not normalized:
            return 0
        log_path = project_md_path.with_name(PROJECT_HISTORY_FILENAME)
        ts = datetime.now().isoformat(timespec="seconds")
        lines = [f"- [{ts}] {item}" for item in normalized]
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return len(lines)

    for project_name, raw_entries in project_logs.items():
        metrics["projects_seen"] += 1
        entries = [_normalize_log_entry(e) for e in (raw_entries or [])]
        entries = [e for e in entries if e]
        entries = list(dict.fromkeys(entries))
        metrics["entries_seen"] += len(entries)
        if not entries:
            continue

        defn = cfg.projects.definitions.get(project_name)
        if not defn:
            defn = registry.get_project_definition(project_name)
        if not defn:
            # Filesystem fallback: project may have been deleted from the
            # registry during the session but its directory still exists.
            candidate = get_quaid_home() / "projects" / project_name
            if (candidate / "PROJECT.md").exists():
                project_md = candidate / "PROJECT.md"
                print(f"[project-log] registry miss, using filesystem fallback: {project_md}")
            else:
                metrics["projects_unknown"] += 1
                print(f"[project-log] unknown project: {project_name}")
                continue
        else:
            project_md = _resolve_project_home(defn.home_dir) / "PROJECT.md"
            if not project_md.exists():
                metrics["projects_missing_file"] += 1
                print(f"[project-log] missing PROJECT.md: {project_md}")
                continue

        if not dry_run:
            _append_project_history_log(project_md, raw_entries or [])

        lines = [f"- {today} [{trigger}] {entry}" for entry in entries]
        content = project_md.read_text(encoding="utf-8")
        if marker_begin in content and marker_end in content:
            pattern = re.compile(
                re.escape(marker_begin) + r"(.*?)" + re.escape(marker_end),
                flags=re.DOTALL,
            )
            m = pattern.search(content)
            existing_lines = []
            if m and m.group(1).strip():
                existing_lines = [line.strip() for line in m.group(1).strip().splitlines() if line.strip()]
            body_lines = (existing_lines + lines)[-recent_limit:]
            body = "\n".join(body_lines)
            replacement = f"{marker_begin}\n{body}\n{marker_end}"
            updated = pattern.sub(lambda _m: replacement, content, count=1)
        else:
            updated = (
                content.rstrip()
                + "\n\n## Recent Major Changes\n"
                + f"{marker_begin}\n"
                + "\n".join(lines[-recent_limit:])
                + f"\n{marker_end}\n"
            )

        metrics["entries_written"] += len(lines)
        metrics["projects_updated"] += 1
        print(
            f"[project-log] project={project_name} entries={len(lines)} "
            f"file={project_md} dry_run={dry_run}"
        )
        if not dry_run:
            project_md.write_text(updated, encoding="utf-8")

    return metrics


def _notify_user(project_name: str, updates_applied: List[str], trigger: str) -> None:
    """Notify user about project updates."""
    if not updates_applied:
        return

    try:
        for doc_path in updates_applied:
            message = (
                "[Quaid] 📋 Project Documentation Update\n"
                f"Project: {project_name}\n"
                f"Updated: `{Path(doc_path).name}`\n"
                f"Trigger: project-{trigger}"
            )
            try:
                queue_delayed_request(
                    message,
                    kind="project_doc_update",
                    priority="normal",
                    source="project_updater",
                )
            except Exception:
                print("  Notification queue failed")
    except Exception as e:
        print(f"  Notification failed: {e}")


def _cleanup_event(event_file: Path) -> None:
    """Delete processed event file."""
    try:
        if event_file.exists():
            event_file.unlink()
    except Exception as e:
        print(f"  Failed to cleanup event file: {e}")


def _move_event_to_failed(event_file: Path) -> None:
    """Move an event file into failed/ for manual triage."""
    try:
        if not event_file.exists():
            return
        failed_dir = event_file.parent / "failed"
        failed_dir.mkdir(exist_ok=True)
        event_file.rename(failed_dir / event_file.name)
    except Exception:
        _cleanup_event(event_file)


def _watchdog_seconds(default_seconds: int = 900) -> int:
    raw = os.getenv("QUAID_PROJECT_UPDATER_WATCHDOG_SECONDS", str(default_seconds))
    try:
        seconds = int(raw)
    except Exception:
        seconds = default_seconds
    return max(0, seconds)


def _run_with_watchdog(fn, timeout_seconds: int, label: str):
    """Run fn with a hard POSIX alarm timeout when available."""
    if timeout_seconds <= 0 or os.name != "posix" or not hasattr(signal, "SIGALRM"):
        return fn()

    def _on_alarm(_signum, _frame):
        raise TimeoutError(f"{label} exceeded watchdog timeout ({timeout_seconds}s)")

    prev_handler = signal.getsignal(signal.SIGALRM)
    prev_timer = signal.setitimer(signal.ITIMER_REAL, float(timeout_seconds), 0.0)
    signal.signal(signal.SIGALRM, _on_alarm)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0, 0.0)
        signal.signal(signal.SIGALRM, prev_handler)
        if prev_timer and (prev_timer[0] > 0 or prev_timer[1] > 0):
            signal.setitimer(signal.ITIMER_REAL, prev_timer[0], prev_timer[1])


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
        event_file = Path(args.event_file)
        timeout_seconds = _watchdog_seconds()
        started = time.time()
        try:
            result = _run_with_watchdog(
                lambda: process_event(args.event_file),
                timeout_seconds,
                "project-updater process-event",
            )
        except TimeoutError as exc:
            _move_event_to_failed(event_file)
            print(f"Watchdog timeout: {exc}", file=sys.stderr)
            result = {
                "success": False,
                "project": None,
                "updates": 0,
                "trigger": "unknown",
                "error": "watchdog_timeout",
                "elapsed_seconds": round(time.time() - started, 3),
            }
        print(json.dumps(result, indent=2))

    elif args.command == "process-all":
        result = process_all_events()
        print(json.dumps(result, indent=2))

    elif args.command == "refresh-project-md":
        ok = refresh_project_md(args.project_name)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
