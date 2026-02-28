#!/usr/bin/env python3
"""
Workspace Audit Module - Single-pass Opus review of core markdown files.

This module:
1. Reads coreMarkdown config for file purposes and maxLines
2. Tracks file modification times vs last janitor run
3. On changes: reads file contents, checks for bloat, calls deep-reasoning LLM for review
4. Parses KEEP/MOVE_TO_PROJECT/MOVE_TO_MEMORY/TRIM decisions
5. MOVE_TO_PROJECT only detects and queues for agent review (no files moved)
"""

import json
import logging
import os
import re
import shutil
import fcntl
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from lib.llm_clients import call_deep_reasoning, parse_json_response
from config import get_config
from lib.runtime_context import get_workspace_dir, get_bootstrap_markdown_globs
from lib.fail_policy import is_fail_hard_enabled

# Configuration
def _workspace_dir() -> Path:
    return get_workspace_dir()

logger = logging.getLogger(__name__)
_MOVE_TO_DOCS_TARGET_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _workspace_review_timeout_seconds(cfg: Any, default_seconds: int = 120) -> float:
    """Resolve workspace audit LLM timeout from env/config with safe fallback."""
    raw_env = os.environ.get("QUAID_WORKSPACE_AUDIT_TIMEOUT_SECONDS")
    if raw_env is not None and str(raw_env).strip():
        try:
            parsed_env = float(raw_env)
            if parsed_env > 0:
                return parsed_env
        except (TypeError, ValueError):
            logger.warning("Invalid QUAID_WORKSPACE_AUDIT_TIMEOUT_SECONDS=%r; using config/default", raw_env)
    try:
        cfg_seconds = float(getattr(getattr(cfg, "docs", object()), "update_timeout_seconds", default_seconds))
        if cfg_seconds > 0:
            return cfg_seconds
    except (TypeError, ValueError):
        pass
    return float(default_seconds)

def _backup_dir() -> Path:
    return _workspace_dir() / "backups" / "workspace"

def _data_dir() -> Path:
    return _workspace_dir() / "logs" / "janitor"

def _docs_dir() -> Path:
    return _workspace_dir() / "docs"

# State files (lazy — use functions below)
def _mtime_tracker() -> Path:
    return _data_dir() / "workspace-mtimes.json"

def _review_decisions() -> Path:
    return _data_dir() / "workspace-review-decisions.json"

def _pending_project_review() -> Path:
    return _data_dir() / "pending-project-review.json"


def _queue_project_review(
    section: str,
    source_file: str,
    reason: str = "",
    project_hint: str = "",
    content_preview: str = "",
) -> None:
    """Append a detected project content finding to the pending review file.

    The janitor does NOT move content — it only detects and queues.
    The agent's heartbeat checks this file on the next active conversation
    and walks the user through what to do with it.
    """
    _data_dir().mkdir(parents=True, exist_ok=True)

    queue_path = _pending_project_review()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "section": section,
        "source_file": source_file,
        "project_hint": project_hint,
        "content_preview": content_preview,
        "reason": reason,
        "timestamp": datetime.now().isoformat(),
    }

    with open(queue_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read().strip()
            pending = json.loads(raw) if raw else []
            if not isinstance(pending, list):
                pending = []
        except (json.JSONDecodeError, IOError) as exc:
            logger.warning("Failed to read pending project review queue %s: %s", queue_path, exc)
            pending = []
        pending.append(entry)
        f.seek(0)
        f.truncate(0)
        json.dump(pending, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    logger.info(f"Queued project review: '{section}' in {source_file}")


def get_pending_project_reviews() -> List[Dict[str, Any]]:
    """Read the pending project review queue (non-destructive).

    Called by the agent/heartbeat when the user is next active.
    Returns the list of pending findings. Call clear_pending_project_reviews()
    only after the agent has successfully walked the user through all items.
    """
    if not _pending_project_review().exists():
        return []

    try:
        with open(_pending_project_review(), "r") as f:
            pending = json.load(f)
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("Failed to read pending project reviews %s: %s", _pending_project_review(), exc)
        return []

    return pending


def clear_pending_project_reviews() -> None:
    """Clear the pending project review queue after successful processing.

    Only call this after the agent has walked the user through all findings.
    If the agent crashes before calling this, the findings persist for retry.
    """
    _pending_project_review().unlink(missing_ok=True)


# Protected region helpers — canonical implementation in lib/markdown.py
from lib.markdown import strip_protected_regions, section_overlaps_protected


def _default_owner_id() -> str:
    try:
        return get_config().users.default_owner
    except Exception as exc:
        if is_fail_hard_enabled():
            raise RuntimeError("Unable to resolve workspace audit default owner from config") from exc
        logger.warning("Workspace audit default owner fallback to 'default': %s", exc)
        return "default"


def _sanitize_move_to_docs_target(raw_target: Any) -> Optional[str]:
    """Validate LLM-suggested docs target path before writing files."""
    target = str(raw_target or "").strip()
    if not target:
        return None
    if len(target) > 512 or "\x00" in target or "\\" in target:
        return None
    if target.startswith("/") or target.startswith("~") or re.match(r"^[A-Za-z]:", target):
        return None
    if not target.startswith("docs/"):
        return None
    if not _MOVE_TO_DOCS_TARGET_RE.fullmatch(target):
        return None
    parts = target.split("/")
    if any((not part) or part in {".", ".."} or len(part) > 128 for part in parts):
        return None
    return target


# Default maxLines for bootstrap files (project-level TOOLS.md, AGENTS.md, etc.)
_BOOTSTRAP_MAX_LINES = 100
_BOOTSTRAP_MONITORED_FILENAMES = {
    "AGENTS.md",
    "SOUL.md",
    "TOOLS.md",
    "USER.md",
    "MEMORY.md",
    "IDENTITY.md",
    "HEARTBEAT.md",
    "TODO.md",
    "PROJECT.md",
}


def get_monitored_files() -> Dict[str, Dict[str, Any]]:
    """
    Get monitored files from coreMarkdown config + gateway bootstrap globs.
    Returns dict mapping filename (or relative path) to {purpose, maxLines}.
    Includes both static core files and dynamically discovered bootstrap files.
    """
    import glob as globmod

    files = {}

    try:
        cfg = get_config()
        if hasattr(cfg, 'docs') and hasattr(cfg.docs, 'core_markdown'):
            core_md = cfg.docs.core_markdown
            if hasattr(core_md, 'files') and core_md.files:
                files.update(core_md.files)
    except Exception as e:
        logger.warning(f"Could not load coreMarkdown config: {e}")

    # Discover bootstrap files from gateway config (single source of truth)
    for glob_pattern in get_bootstrap_markdown_globs():
        # Reject absolute paths and traversal patterns to prevent escaping workspace
        if glob_pattern.startswith("/") or ".." in glob_pattern:
            logger.warning(f"Skipping unsafe bootstrap glob: {glob_pattern}")
            continue
        full_pattern = str(_workspace_dir() / glob_pattern)
        for match in sorted(globmod.glob(full_pattern)):
            rel_path = os.path.relpath(match, _workspace_dir())
            # Double-check: resolved path must be inside workspace
            if rel_path.startswith(".."):
                logger.warning(f"Skipping path outside workspace: {match}")
                continue
            base_name = Path(rel_path).name
            # Match runtime bootstrap behavior: only monitor known bootstrap file names.
            if base_name not in _BOOTSTRAP_MONITORED_FILENAMES:
                continue
            if rel_path not in files:
                files[rel_path] = {
                    "purpose": f"Project bootstrap file ({Path(rel_path).name})",
                    "maxLines": _BOOTSTRAP_MAX_LINES,
                }

    if not files:
        # Fallback to hardcoded list if config not available
        return {
            "AGENTS.md": {"purpose": "System operations, memory system, behavioral rules", "maxLines": 350},
            "SOUL.md": {"purpose": "Personality, vibe, values, interaction style", "maxLines": 80},
            "TOOLS.md": {"purpose": "API docs, tool definitions, credentials, configs", "maxLines": 350},
            "USER.md": {"purpose": "Biography and soul of users", "maxLines": 150},
            "MEMORY.md": {"purpose": "Core memories loaded every session", "maxLines": 100},
            "IDENTITY.md": {"purpose": "Name, avatar, minimal identity", "maxLines": 20},
            "HEARTBEAT.md": {"purpose": "Periodic task instructions", "maxLines": 50},
            "TODO.md": {"purpose": "Planning and task list", "maxLines": 150},
        }

    return files


def build_review_prompt(files_config: Dict[str, Dict[str, Any]]) -> str:
    """Build the Opus review prompt with file purposes."""

    file_purposes = "\n".join([
        f"- **{fname}**: {info.get('purpose', 'Unknown')} (max {info.get('maxLines', 'N/A')} lines)"
        for fname, info in files_config.items()
    ])

    return f"""You are reviewing core markdown files for an AI assistant's workspace.

These files load on EVERY API call, EVERY turn. Tokens are precious. Keep them focused.

## File Purposes
{file_purposes}

## Review Each Changed File For:

1. **BLOAT** - Content exceeding file's purpose or maxLines
   - Project-specific specs, API docs, tool definitions → should be in projects/ (MOVE_TO_PROJECT)
   - Queryable facts (phone numbers, dates, preferences) → should be in Memory DB

2. **OUTDATED INFO** - Facts that are no longer true
   - Old system states, retired features, wrong information

3. **MISPLACED CONTENT** - Content in wrong file
   - Personality stuff in TOOLS.md → should be in SOUL.md
   - Facts about the user in AGENTS.md → should be in USER.md or MEMORY.md
   - Project specs in TOOLS.md or AGENTS.md → should be in projects/ (MOVE_TO_PROJECT)

## Actions

- **KEEP** - Content is appropriate and belongs here
- **MOVE_TO_PROJECT** - Project specs, tool definitions, API docs for a specific project → flag for migration to a project directory. Set "project_hint" to a short description of what project this belongs to (e.g. "React frontend app", "memory plugin", "API server"). Note: content inside `<!-- protected -->` tags has already been excluded from your review — the user has opted out of migration for those sections.
- **MOVE_TO_MEMORY** - Personal facts → store in memory DB, remove from file
- **TRIM** - Outdated or redundant → suggest removal
- **FLAG_BLOAT** - File exceeds maxLines → warn but don't auto-fix

## Response Format

Respond with JSON only, no markdown fencing:
{{
  "reviewed_at": "ISO timestamp",
  "file_stats": {{
    "AGENTS.md": {{"lines": 288, "maxLines": 350, "over_limit": false}},
    "TOOLS.md": {{"lines": 320, "maxLines": 350, "over_limit": false}}
  }},
  "decisions": [
    {{
      "file": "TOOLS.md",
      "section": "Section Title",
      "action": "KEEP",
      "reason": "Essential quick-reference"
    }},
    {{
      "file": "TOOLS.md",
      "section": "My App API Reference",
      "action": "MOVE_TO_PROJECT",
      "project_hint": "React frontend app with Express API",
      "reason": "Project-specific API docs don't belong in core TOOLS.md"
    }},
    {{
      "file": "USER.md",
      "section": "Contact Info",
      "action": "MOVE_TO_MEMORY",
      "memory_type": "verified",
      "reason": "Queryable fact, not biographical"
    }},
    {{
      "file": "MEMORY.md",
      "section": "Old Project Status",
      "action": "TRIM",
      "reason": "Outdated, project completed"
    }},
    {{
      "file": "AGENTS.md",
      "action": "FLAG_BLOAT",
      "reason": "File is 400 lines, max is 350"
    }}
  ],
  "summary": "Brief summary of findings"
}}"""


def backup_workspace_files(files: Optional[List[str]] = None) -> Dict[str, str]:
    """
    Create timestamped backups of workspace files before modification.
    Returns dict mapping original path to backup path.
    """
    _backup_dir().mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")

    files_config = get_monitored_files()
    files_to_backup = files or list(files_config.keys())
    backups = {}

    for filename in files_to_backup:
        source = _workspace_dir() / filename
        if source.exists():
            # Flatten slashes for relative paths (e.g. projects/quaid/TOOLS.md → projects--quaid--TOOLS.md)
            flat_name = filename.replace("/", "--")
            backup_name = f"{flat_name}.{timestamp}.bak"
            backup_path = _backup_dir() / backup_name
            shutil.copy2(source, backup_path)
            backups[str(source)] = str(backup_path)
            logger.info(f"Backed up: {filename} -> {backup_name}")
            print(f"  Backed up: {filename} -> {backup_name}")

    return backups


def get_file_mtimes() -> Dict[str, float]:
    """Get current modification times for all monitored files."""
    files_config = get_monitored_files()
    mtimes = {}
    for filename in files_config.keys():
        filepath = _workspace_dir() / filename
        if filepath.exists():
            mtimes[filename] = filepath.stat().st_mtime
    return mtimes


def load_last_mtimes() -> Dict[str, float]:
    """Load previously recorded modification times."""
    if _mtime_tracker().exists():
        try:
            with open(_mtime_tracker(), 'r') as f:
                data = json.load(f)
                return data.get("mtimes", {})
        except Exception:
            pass
    return {}


def save_mtimes(mtimes: Dict[str, float]):
    """Save current modification times."""
    _data_dir().mkdir(parents=True, exist_ok=True)
    with open(_mtime_tracker(), "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump({
                "mtimes": mtimes,
                "updated_at": datetime.now().isoformat()
            }, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def detect_changed_files() -> List[str]:
    """
    Compare current mtimes with last recorded mtimes.
    Returns list of files that have changed.
    """
    current = get_file_mtimes()
    last = load_last_mtimes()

    changed = []
    for filename, mtime in current.items():
        last_mtime = last.get(filename, 0)
        if mtime > last_mtime:
            changed.append(filename)

    return changed


def get_file_line_counts() -> Dict[str, int]:
    """Get line counts for all monitored files."""
    files_config = get_monitored_files()
    counts = {}
    for filename in files_config.keys():
        filepath = _workspace_dir() / filename
        if filepath.exists():
            counts[filename] = len(filepath.read_text().splitlines())
    return counts


def check_bloat() -> Dict[str, Dict[str, Any]]:
    """
    Check all monitored files for bloat (exceeding maxLines).
    Returns dict of files with their stats.
    """
    files_config = get_monitored_files()
    line_counts = get_file_line_counts()

    stats = {}
    for filename, info in files_config.items():
        max_lines = info.get("maxLines", 999)
        actual_lines = line_counts.get(filename, 0)
        stats[filename] = {
            "lines": actual_lines,
            "maxLines": max_lines,
            "over_limit": actual_lines > max_lines,
            "purpose": info.get("purpose", "Unknown")
        }

    return stats


def _read_file_contents(changed_files: List[str]) -> Dict[str, str]:
    """Read contents of changed workspace files."""
    contents = {}
    for filename in changed_files:
        filepath = _workspace_dir() / filename
        if filepath.exists():
            contents[filename] = filepath.read_text()
    return contents


def apply_review_decisions(dry_run: bool = True,
                           decisions_data: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """
    Apply the review decisions made by the deep-reasoning LLM.

    Actions:
    - MOVE_TO_PROJECT: Detect project content, queue for agent review (no files modified)
    - MOVE_TO_DOCS: Extract section, write to docs/, replace with pointer
    - MOVE_TO_MEMORY: Extract facts, store in memory DB, remove section
    - TRIM: Remove section (with backup)
    - FLAG_BLOAT: Just log warning
    - KEEP: No action

    If decisions_data is provided, use it directly. Otherwise load from file.
    """
    from core.services.memory_service import get_memory_service

    if decisions_data is None:
        # Fallback: load from file
        if _review_decisions().exists():
            try:
                with open(_review_decisions(), 'r') as f:
                    decisions_data = json.load(f)
            except Exception:
                pass

    if not decisions_data:
        print("  No decisions data available")
        return {"moved_to_docs": 0, "moved_to_memory": 0, "trimmed": 0, "bloat_warnings": 0, "project_detected": 0, "errors": 0}

    decisions = decisions_data.get("decisions", [])
    if not decisions:
        print("  No decisions to apply")
        return {"moved_to_docs": 0, "moved_to_memory": 0, "trimmed": 0, "bloat_warnings": 0, "project_detected": 0, "errors": 0}

    stats = {"moved_to_docs": 0, "moved_to_memory": 0, "trimmed": 0, "bloat_warnings": 0, "project_detected": 0, "kept": 0, "errors": 0}

    # Backup before modifying
    if not dry_run:
        changed_files = list(set(d["file"] for d in decisions if "file" in d))
        backup_workspace_files(changed_files)

    # Group by file for batch processing
    by_file = {}
    for d in decisions:
        filename = d.get("file", "")
        if not filename:
            continue
        if filename not in by_file:
            by_file[filename] = []
        by_file[filename].append(d)

    for filename, file_decisions in by_file.items():
        filepath = _workspace_dir() / filename
        if not filepath.exists():
            continue

        locked_file = None
        try:
            if dry_run:
                content = filepath.read_text(encoding="utf-8")
            else:
                locked_file = open(filepath, "r+", encoding="utf-8")
                fcntl.flock(locked_file.fileno(), fcntl.LOCK_EX)
                content = locked_file.read()

            # Detect protected regions so we can skip operations within them
            _, protected_ranges = strip_protected_regions(content)

            # First pass: resolve positions in original content (before any modifications)
            # Then apply in reverse order to avoid position corruption
            resolved_ops = []  # [(start_pos, end_pos, decision, section_content)]

            for decision in file_decisions:
                action = decision.get("action", "KEEP")
                section = decision.get("section", "")
                reason = decision.get("reason", "")

                if action == "KEEP":
                    stats["kept"] += 1
                    continue

                if action == "FLAG_BLOAT":
                    print(f"  BLOAT WARNING: {filename} - {reason}")
                    logger.warning(f"BLOAT: {filename} - {reason}")
                    stats["bloat_warnings"] += 1
                    continue

                if not section:
                    continue

                # Find the section in the file (simple header matching)
                pattern = rf'^(#{{1,6}})\s+{re.escape(section)}\s*$'
                match = re.search(pattern, content, re.MULTILINE)

                if not match:
                    print(f"  Section not found: {section} in {filename}")
                    stats["errors"] += 1
                    continue

                # Find section boundaries
                start_pos = match.start()
                header_level = len(match.group(1))

                # Find next section of same or higher level
                next_section = re.search(
                    rf'^#{{1,{header_level}}}\s+',
                    content[match.end():],
                    re.MULTILINE
                )

                if next_section:
                    end_pos = match.end() + next_section.start()
                else:
                    end_pos = len(content)

                # Skip sections that overlap with protected regions
                if section_overlaps_protected(start_pos, end_pos, protected_ranges):
                    print(f"  Skipping protected section: {section} in {filename}")
                    continue

                section_content = content[start_pos:end_pos].strip()
                resolved_ops.append((start_pos, end_pos, decision, section_content, header_level))

            # Sort by start position descending so we modify from end of file backward
            resolved_ops.sort(key=lambda x: x[0], reverse=True)

            for start_pos, end_pos, decision, section_content, header_level in resolved_ops:
                action = decision.get("action", "KEEP")
                section = decision.get("section", "")
                reason = decision.get("reason", "")

                try:
                    if action == "MOVE_TO_PROJECT":
                        # Detection only — don't move anything.
                        # Queue the finding for the agent to discuss with the user
                        # on their next active conversation. The agent handles the
                        # actual move after getting user approval.
                        project_hint = decision.get("project_hint", section)

                        if dry_run:
                            print(f"  Detected project content: '{section}' in {filename} (hint: {project_hint})")
                        else:
                            print(f"  Detected project content: '{section}' in {filename}")
                            try:
                                _queue_project_review(
                                    section=section,
                                    source_file=filename,
                                    reason=reason,
                                    project_hint=project_hint,
                                    content_preview=section_content[:1000],
                                )
                            except Exception as e:
                                logger.warning(f"Failed to queue project review: {e}")

                        stats["project_detected"] = stats.get("project_detected", 0) + 1

                    elif action == "MOVE_TO_DOCS":
                        # Prefer MOVE_TO_PROJECT for project content
                        default_target = f"docs/{section.lower().replace(' ', '-')}.md"
                        target = _sanitize_move_to_docs_target(decision.get("target", default_target))
                        if target is None:
                            logger.error(
                                "Invalid MOVE_TO_DOCS target blocked: %r",
                                decision.get("target", default_target),
                            )
                            stats["errors"] = stats.get("errors", 0) + 1
                            continue
                        target_path = (_workspace_dir() / target).resolve()
                        # Prevent path traversal from LLM-controlled target
                        if not str(target_path).startswith(str(_workspace_dir().resolve())):
                            logger.error(f"Path traversal blocked: {target}")
                            stats["errors"] = stats.get("errors", 0) + 1
                            continue

                        if dry_run:
                            print(f"  Would move '{section}' from {filename} -> {target}")
                        else:
                            target_path.parent.mkdir(parents=True, exist_ok=True)

                            doc_content = f"# {section}\n\n"
                            doc_content += f"> Migrated from `{filename}` on {datetime.now().strftime('%Y-%m-%d')}\n"
                            doc_content += f"> Reason: {reason}\n\n"
                            doc_content += section_content

                            target_path.write_text(doc_content)

                            pointer = f"{'#' * header_level} {section}\n\n"
                            pointer += f"**Detailed docs:** `{target}`\n"

                            content = content[:start_pos] + pointer + content[end_pos:]

                            logger.info(f"MOVE_TO_DOCS: '{section}' from {filename} -> {target}")
                            print(f"  Moved '{section}' -> {target}")

                        stats["moved_to_docs"] += 1

                    elif action == "MOVE_TO_MEMORY":
                        memory_type = decision.get("memory_type", "verified")

                        if dry_run:
                            print(f"  Would store '{section}' from {filename} as {memory_type} memory")
                        else:
                            get_memory_service().store(
                                text=section_content[:2000],
                                category="fact",
                                source=f"workspace_audit:{filename}",
                                owner_id=_default_owner_id(),
                                verified=(memory_type == "verified"),
                                pinned=(memory_type == "pinned"),
                            )

                            content = content[:start_pos] + content[end_pos:]

                            logger.info(f"MOVE_TO_MEMORY: '{section}' from {filename} -> {memory_type} memory")
                            print(f"  Stored '{section}' as {memory_type} memory")

                        stats["moved_to_memory"] += 1

                    elif action == "TRIM":
                        if dry_run:
                            print(f"  Would trim '{section}' from {filename}: {reason}")
                        else:
                            content = content[:start_pos] + content[end_pos:]
                            logger.info(f"TRIM: '{section}' from {filename} - {reason}")
                            print(f"  Trimmed '{section}' from {filename}")

                        stats["trimmed"] += 1

                except Exception as e:
                    logger.error(f"Error processing '{section}' in {filename}: {e}")
                    print(f"  Error processing '{section}': {e}")
                    stats["errors"] += 1

            # Write modified file
            if not dry_run and locked_file is not None:
                locked_file.seek(0)
                locked_file.truncate(0)
                locked_file.write(content)
                locked_file.flush()
                os.fsync(locked_file.fileno())
                print(f"  Updated {filename}")
        finally:
            if locked_file is not None:
                try:
                    fcntl.flock(locked_file.fileno(), fcntl.LOCK_UN)
                except Exception as unlock_err:
                    logger.warning(f"Failed to unlock workspace file {filename}: {unlock_err}")
                try:
                    locked_file.close()
                except Exception as close_err:
                    logger.warning(f"Failed to close workspace file {filename}: {close_err}")

    # Update mtimes after successful application
    if not dry_run:
        save_mtimes(get_file_mtimes())

        # Clean up decisions file if it exists
        if _review_decisions().exists():
            _review_decisions().unlink()

        logger.info(f"WORKSPACE AUDIT COMPLETE: moved_to_docs={stats['moved_to_docs']}, "
                   f"moved_to_memory={stats['moved_to_memory']}, trimmed={stats['trimmed']}, "
                   f"bloat_warnings={stats['bloat_warnings']}, project_detected={stats.get('project_detected', 0)}, "
                   f"kept={stats['kept']}, errors={stats['errors']}")

    return stats


def run_workspace_check(dry_run: bool = True) -> Dict[str, Any]:
    """
    Main entry point for workspace audit — single-pass Opus review.

    1. Check bloat status first
    2. Detect changed files
    3. If changes found, read contents and call Opus for review
    4. Parse and apply decisions immediately
    """
    _cfg = get_config()
    max_tokens = _cfg.janitor.opus_review.max_tokens
    llm_timeout_seconds = _workspace_review_timeout_seconds(_cfg)
    files_config = get_monitored_files()

    print("\n" + "=" * 80)
    print("WORKSPACE AUDIT - Core Markdown Review")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    print(f"Monitoring: {', '.join(files_config.keys())}")
    print("=" * 80)

    _data_dir().mkdir(parents=True, exist_ok=True)

    # Check bloat status
    print("\n  Checking file sizes...")
    bloat_stats = check_bloat()
    bloated_files = [f for f, s in bloat_stats.items() if s["over_limit"]]

    for filename, stats in bloat_stats.items():
        status = "⚠️  OVER" if stats["over_limit"] else "✓"
        print(f"    {filename}: {stats['lines']}/{stats['maxLines']} lines {status}")

    if bloated_files:
        print(f"\n  ⚠️  {len(bloated_files)} file(s) over limit: {', '.join(bloated_files)}")

    # Check for a leftover decisions file (from a previous interrupted run)
    if _review_decisions().exists():
        print("\n  Found pending decisions file, applying...")
        stats = apply_review_decisions(dry_run)
        return {"phase": "apply", "bloat_stats": bloat_stats, **stats}

    # Detect changed files
    print("\n  Checking for changed files...")
    changed = detect_changed_files()

    if not changed and not bloated_files:
        print("  No changes and no bloat issues")
        # Still update mtimes so we don't re-check
        save_mtimes(get_file_mtimes())
        return {
            "phase": "no_changes",
            "checked_files": list(files_config.keys()),
            "bloat_stats": bloat_stats
        }

    # If we have bloated files but no changes, still review them
    files_to_review = list(set(changed + bloated_files))
    print(f"  Files to review: {', '.join(files_to_review)}")

    # Read file contents
    files_content = _read_file_contents(files_to_review)
    if not files_content:
        print("  Could not read any files")
        return {"phase": "error", "error": "no readable files", "bloat_stats": bloat_stats}

    # Build user message with file contents and line counts
    # Strip protected regions before sending to Opus for review
    user_parts = [f"Review the following {len(files_content)} core markdown files:\n"]
    for filename, content in files_content.items():
        stripped_content, _ = strip_protected_regions(content)
        line_count = len(stripped_content.splitlines())
        max_lines = files_config.get(filename, {}).get("maxLines", "N/A")
        purpose = files_config.get(filename, {}).get("purpose", "Unknown")
        user_parts.append(f"--- {filename} ({line_count} lines, max {max_lines}) ---")
        user_parts.append(f"Purpose: {purpose}")
        user_parts.append(f"{stripped_content}\n")
    user_message = "\n".join(user_parts)

    # Build prompt with file purposes
    review_prompt = build_review_prompt(files_config)

    # Call deep-reasoning model (config-driven, handles API key errors)
    print(f"  Calling deep-reasoning model for review of {len(files_content)} files...")
    response_text, duration = call_deep_reasoning(
        prompt=user_message,
        system_prompt=review_prompt,
        max_tokens=max_tokens,
        timeout=llm_timeout_seconds,
    )

    if not response_text:
        print(f"  Opus returned empty response after {duration:.1f}s")
        return {"phase": "error", "error": "empty API response", "bloat_stats": bloat_stats}

    print(f"  Received response in {duration:.1f}s")

    # Parse decisions
    decisions_data = parse_json_response(response_text)
    if not isinstance(decisions_data, dict) or "decisions" not in decisions_data:
        print(f"  Failed to parse Opus response as decisions JSON")
        logger.error(f"Invalid workspace review response: {response_text[:500]}")
        return {"phase": "error", "error": "invalid JSON response", "bloat_stats": bloat_stats}

    decision_count = len(decisions_data.get("decisions", []))
    print(f"  Parsed {decision_count} decisions")

    # Show summary if provided
    summary = decisions_data.get("summary", "")
    if summary:
        print(f"  Summary: {summary}")

    # Apply decisions directly
    stats = apply_review_decisions(dry_run, decisions_data=decisions_data)

    # Save mtimes after successful processing
    if not dry_run:
        save_mtimes(get_file_mtimes())

    return {"phase": "apply", "bloat_stats": bloat_stats, **stats}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Workspace Audit - Core Markdown Review")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--backup-only", action="store_true", help="Just create backups")
    parser.add_argument("--check-only", action="store_true", help="Only check for changes and bloat")
    parser.add_argument("--bloat", action="store_true", help="Check bloat status only")

    args = parser.parse_args()

    if args.backup_only:
        print("Creating backups...")
        backups = backup_workspace_files()
        print(f"Created {len(backups)} backups")
    elif args.bloat:
        print("Checking bloat status...")
        stats = check_bloat()
        for filename, s in stats.items():
            status = "⚠️  OVER" if s["over_limit"] else "✓"
            print(f"  {filename}: {s['lines']}/{s['maxLines']} lines {status}")
    elif args.check_only:
        changed = detect_changed_files()
        bloat = check_bloat()
        if changed:
            print(f"Changed files: {', '.join(changed)}")
        else:
            print("No files changed")
        bloated = [f for f, s in bloat.items() if s["over_limit"]]
        if bloated:
            print(f"Bloated files: {', '.join(bloated)}")
    else:
        result = run_workspace_check(dry_run=not args.apply)
        print(f"\nResult: {json.dumps(result, indent=2)}")
