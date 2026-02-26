#!/usr/bin/env python3
"""
Documentation Auto-Updater

Detects stale docs by comparing source file mtimes to doc mtimes,
and updates docs using git diffs or conversation transcripts as context.
Also handles cleanup of bloated docs based on churn heuristics.

Update paths:
1. Compact/Reset: The assistant has full context → update from transcript
2. On-demand: Staleness detected when pulling docs → warn + offer rebuild
3. Janitor Task 1b: Nightly cache priming from git diffs

Cleanup triggers (heuristic-based):
- 10+ updates since last cleanup, OR
- 30%+ size growth since last cleanup

Usage:
  python3 docs_updater.py check [--json]           # Check for stale docs (with classification)
  python3 docs_updater.py update <doc_path> --apply
  python3 docs_updater.py update-stale --apply [--trivial-only]
  python3 docs_updater.py update-from-transcript --transcript /tmp/t.txt --apply
  python3 docs_updater.py classify-change --diff "..."  # Classify a diff as trivial/significant
  python3 docs_updater.py changelog [--json]       # View update history
  python3 docs_updater.py cleanup-check [--json]   # Check which docs need cleanup
  python3 docs_updater.py cleanup [doc_path] --apply  # Clean up bloated docs
"""

import argparse
import difflib
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import subprocess

from config import get_config
from lib.llm_clients import call_deep_reasoning, call_fast_reasoning
from lib.runtime_context import get_workspace_dir
logger = logging.getLogger(__name__)

def _workspace() -> Path:
    return get_workspace_dir()

def _changelog_path() -> Path:
    return _workspace() / "logs" / "docs-update-log.json"

def _cleanup_state_path() -> Path:
    return _workspace() / "logs" / "docs-cleanup-state.json"


def _queue_delayed_notification(message: str, kind: str, priority: str, source: str) -> None:
    payload = {
        "message": str(message),
        "kind": str(kind),
        "priority": str(priority),
    }
    events_py = Path(__file__).resolve().parents[2] / "core" / "runtime" / "events.py"
    try:
        subprocess.run(
            [
                sys.executable,
                str(events_py),
                "emit",
                "--name",
                "notification.delayed",
                "--payload",
                json.dumps(payload, ensure_ascii=False),
                "--source",
                source,
                "--dispatch",
                "queued",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Timed out queuing delayed notification", extra={"source": source, "kind": kind})

# Cleanup thresholds
CLEANUP_UPDATE_THRESHOLD = 10  # Trigger cleanup after this many updates
CLEANUP_GROWTH_THRESHOLD = 1.3  # Trigger cleanup if doc grew 30%+


@dataclass
class ChangelogEntry:
    timestamp: str
    doc_path: str
    trigger: str  # "compact", "janitor", "manual", "on-demand"
    sources: List[str]
    summary: str
    dry_run: bool
    success: bool
    chars_before: int
    chars_after: int


def _load_changelog() -> List[dict]:
    """Load existing changelog entries."""
    if not _changelog_path().exists():
        return []
    try:
        return json.loads(_changelog_path().read_text())
    except (json.JSONDecodeError, IOError):
        return []


def _save_changelog(entries: List[dict]) -> None:
    """Save changelog entries, keeping last 100."""
    entries = entries[-100:]  # Keep only last 100 entries
    _changelog_path().parent.mkdir(parents=True, exist_ok=True)
    _changelog_path().write_text(json.dumps(entries, indent=2))


def log_doc_update(
    doc_path: str,
    trigger: str,
    sources: List[str],
    summary: str,
    dry_run: bool,
    success: bool,
    chars_before: int,
    chars_after: int,
    notify: bool = True,
) -> None:
    """Log a documentation update to the changelog."""
    entry = {
        "timestamp": datetime.now().isoformat(),
        "doc_path": doc_path,
        "trigger": trigger,
        "sources": sources,
        "summary": summary,
        "dry_run": dry_run,
        "success": success,
        "chars_before": chars_before,
        "chars_after": chars_after,
    }
    entries = _load_changelog()
    entries.append(entry)
    _save_changelog(entries)

    # Track updates for cleanup heuristics (only for successful non-dry-run updates)
    if success and not dry_run and trigger != "cleanup":
        _increment_update_count(doc_path, chars_after)

    # Notify user of successful real updates (not dry-run, not cleanup, and actual changes made)
    actual_change = chars_before != chars_after
    if notify and success and not dry_run and trigger != "cleanup" and actual_change:
        try:
            cfg = get_config()
            if cfg.docs.notify_on_update:
                message = (
                    "[Quaid] 📋 Auto-Documentation Update\n"
                    f"Updated: `{Path(doc_path).name}`\n"
                    f"Trigger: {trigger}\n"
                    f"Changes: {summary}"
                )
                _queue_delayed_notification(
                    message,
                    kind="doc_update",
                    priority="normal",
                    source="docs_updater",
                )
        except Exception as e:
            logger.warning("Failed to notify user about doc update for %s: %s", doc_path, e)


def get_changelog(limit: int = 20) -> List[dict]:
    """Get recent changelog entries."""
    entries = _load_changelog()
    return entries[-limit:]


# --- Cleanup State Tracking ---

def _load_cleanup_state() -> Dict[str, dict]:
    """Load cleanup state for all docs."""
    if not _cleanup_state_path().exists():
        return {}
    try:
        return json.loads(_cleanup_state_path().read_text())
    except (json.JSONDecodeError, IOError):
        return {}


def _save_cleanup_state(state: Dict[str, dict]) -> None:
    """Save cleanup state."""
    _cleanup_state_path().parent.mkdir(parents=True, exist_ok=True)
    _cleanup_state_path().write_text(json.dumps(state, indent=2))


def _increment_update_count(doc_path: str, chars_after: int) -> None:
    """Increment the update count for a doc after an auto-update."""
    state = _load_cleanup_state()
    if doc_path not in state:
        state[doc_path] = {
            "last_cleanup": None,
            "chars_at_cleanup": chars_after,
            "updates_since_cleanup": 0,
        }
    state[doc_path]["updates_since_cleanup"] = state[doc_path].get("updates_since_cleanup", 0) + 1
    _save_cleanup_state(state)


def _reset_cleanup_state(doc_path: str, chars: int) -> None:
    """Reset cleanup state after a cleanup is performed."""
    state = _load_cleanup_state()
    state[doc_path] = {
        "last_cleanup": datetime.now().isoformat(),
        "chars_at_cleanup": chars,
        "updates_since_cleanup": 0,
    }
    _save_cleanup_state(state)


@dataclass
class CleanupInfo:
    doc_path: str
    updates_since_cleanup: int
    chars_at_cleanup: int
    current_chars: int
    growth_ratio: float
    reason: str  # "updates" or "growth" or "both"


def check_cleanup_needed() -> Dict[str, CleanupInfo]:
    """Check which docs need cleanup based on churn heuristics.

    Returns docs that have either:
    - updates_since_cleanup >= CLEANUP_UPDATE_THRESHOLD
    - current_chars / chars_at_cleanup >= CLEANUP_GROWTH_THRESHOLD
    """
    state = _load_cleanup_state()
    purposes = get_doc_purposes()
    needs_cleanup: Dict[str, CleanupInfo] = {}

    for doc_path in purposes.keys():
        doc_abs = _resolve_path(doc_path)
        if not doc_abs.exists():
            continue

        current_chars = len(doc_abs.read_text())
        doc_state = state.get(doc_path, {})
        updates = doc_state.get("updates_since_cleanup", 0)
        chars_at_cleanup = doc_state.get("chars_at_cleanup", current_chars)

        # Calculate growth ratio (protect against division by zero)
        growth_ratio = current_chars / chars_at_cleanup if chars_at_cleanup > 0 else 1.0

        # Check thresholds
        needs_update_cleanup = updates >= CLEANUP_UPDATE_THRESHOLD
        needs_growth_cleanup = growth_ratio >= CLEANUP_GROWTH_THRESHOLD

        if needs_update_cleanup or needs_growth_cleanup:
            if needs_update_cleanup and needs_growth_cleanup:
                reason = "both"
            elif needs_update_cleanup:
                reason = "updates"
            else:
                reason = "growth"

            needs_cleanup[doc_path] = CleanupInfo(
                doc_path=doc_path,
                updates_since_cleanup=updates,
                chars_at_cleanup=chars_at_cleanup,
                current_chars=current_chars,
                growth_ratio=growth_ratio,
                reason=reason,
            )

    return needs_cleanup


def cleanup_doc(doc_path: str, purpose: str, dry_run: bool = True) -> bool:
    """Clean up a doc by removing stale content and consolidating.

    Returns True on success.
    """
    doc_abs = _resolve_path(doc_path)
    if not doc_abs.exists():
        print(f"  Doc not found: {doc_abs}")
        return False

    current_doc = doc_abs.read_text()
    chars_before = len(current_doc)

    system_prompt = (
        "You are cleaning up technical documentation that has accumulated bloat "
        "from many iterative updates.\n\n"
        f"DOCUMENT PURPOSE: {purpose}\n\n"
        "Review the document and:\n"
        "1. Remove sections describing features/behavior that no longer exist\n"
        "2. Consolidate redundant explanations (say it once, clearly)\n"
        "3. Trim unnecessary verbosity while preserving accuracy\n"
        "4. Improve organization if sections have become disjointed\n\n"
        "IMPORTANT: Preserve all CURRENT, ACCURATE information. Only remove stale "
        "or redundant content. When in doubt, keep it.\n\n"
        "Return the COMPLETE cleaned document.\n\n"
        "Also include a one-line SUMMARY at the very end:\n"
        "<!-- CLEANUP_SUMMARY: brief description of what was cleaned -->"
    )

    user_message = f"DOCUMENT TO CLEAN ({doc_path}):\n\n{current_doc}"

    print(f"  Calling Opus to clean up {doc_path}...")
    response, duration = call_deep_reasoning(
        prompt=user_message,
        system_prompt=system_prompt,
        max_tokens=8000,
        timeout=300.0,
    )

    if not response:
        print(f"  LLM call failed for {doc_path} ({duration:.1f}s)")
        log_doc_update(doc_path, "cleanup", [], "LLM call failed",
                       dry_run, False, chars_before, 0)
        return False

    # Extract summary from response
    summary = "Cleaned up document"
    summary_match = re.search(r'<!-- CLEANUP_SUMMARY: (.+?) -->', response)
    if summary_match:
        summary = summary_match.group(1)
        response = re.sub(r'\n*<!-- CLEANUP_SUMMARY: .+? -->\n*', '', response).strip()

    chars_after = len(response)
    reduction = chars_before - chars_after
    reduction_pct = (reduction / chars_before * 100) if chars_before > 0 else 0

    print(f"  Opus responded in {duration:.1f}s")
    print(f"  Size: {chars_before} -> {chars_after} chars ({reduction:+d}, {reduction_pct:.1f}% reduction)")

    if dry_run:
        print(f"  [DRY RUN] Would clean up {doc_path}")
        log_doc_update(doc_path, "cleanup", [], f"[DRY RUN] {summary}",
                       dry_run, True, chars_before, chars_after)
        return True

    doc_abs.write_text(response)
    print(f"  Cleaned up {doc_path}")
    log_doc_update(doc_path, "cleanup", [], summary,
                   dry_run, True, chars_before, chars_after)

    # Reset cleanup state
    _reset_cleanup_state(doc_path, chars_after)

    return True


def cmd_cleanup_check(json_output: bool = False) -> Dict[str, CleanupInfo]:
    """CLI: check which docs need cleanup."""
    needs_cleanup = check_cleanup_needed()

    if json_output:
        out = {}
        for doc_path, info in needs_cleanup.items():
            out[doc_path] = {
                "updates_since_cleanup": info.updates_since_cleanup,
                "chars_at_cleanup": info.chars_at_cleanup,
                "current_chars": info.current_chars,
                "growth_ratio": round(info.growth_ratio, 2),
                "reason": info.reason,
            }
        print(json.dumps(out, indent=2))
    else:
        if not needs_cleanup:
            print("No docs need cleanup.")
        else:
            print(f"Found {len(needs_cleanup)} doc(s) needing cleanup:\n")
            for doc_path, info in needs_cleanup.items():
                reason_str = {
                    "updates": f"{info.updates_since_cleanup} updates",
                    "growth": f"{info.growth_ratio:.1f}x growth",
                    "both": f"{info.updates_since_cleanup} updates + {info.growth_ratio:.1f}x growth",
                }[info.reason]
                print(f"  {doc_path}")
                print(f"    Reason: {reason_str}")
                print(f"    Size: {info.chars_at_cleanup} -> {info.current_chars} chars")
                print()

    return needs_cleanup


def cmd_cleanup(doc_path: Optional[str] = None, dry_run: bool = True) -> int:
    """CLI: clean up docs that need it. Returns count of cleaned docs."""
    if doc_path:
        # Clean specific doc
        purposes = get_doc_purposes()
        purpose = purposes.get(doc_path, "")
        ok = cleanup_doc(doc_path, purpose, dry_run=dry_run)
        return 1 if ok else 0

    # Clean all docs that need it
    needs_cleanup = check_cleanup_needed()
    if not needs_cleanup:
        print("No docs need cleanup.")
        return 0

    purposes = get_doc_purposes()
    cleaned = 0

    for doc_path, info in needs_cleanup.items():
        print(f"\nCleaning {doc_path} ({info.reason})...")
        purpose = purposes.get(doc_path, "")
        ok = cleanup_doc(doc_path, purpose, dry_run=dry_run)
        if ok:
            cleaned += 1

    print(f"\n{'Would clean' if dry_run else 'Cleaned'} {cleaned}/{len(needs_cleanup)} doc(s)")
    return cleaned


@dataclass
class StalenessInfo:
    doc_path: str
    gap_hours: float
    stale_sources: List[str]
    doc_mtime: float
    latest_source_mtime: float
    change_classification: Optional[Dict[str, Any]] = None  # From classify_doc_change()


def classify_doc_change(diff_text: str) -> Dict[str, Any]:
    """Classify a doc-affecting change as trivial or significant.

    Trivial changes (auto-fixable): typo corrections, whitespace fixes,
    import path updates, version bumps, comment-only changes.

    Significant changes (need review): new features, API changes,
    architecture changes, removed functionality.

    Returns:
        Dict with keys: classification ("trivial"/"significant"),
        confidence (0-1), reasons (list of strings),
        lines_changed (int), trivial_signals (int), significant_signals (int).
    """
    if not diff_text or not diff_text.strip():
        return {"classification": "trivial", "confidence": 1.0, "reasons": ["empty diff"],
                "lines_changed": 0, "trivial_signals": 1, "significant_signals": 0}

    lines = diff_text.split('\n')
    added_lines = [l[1:] for l in lines if l.startswith('+') and not l.startswith('+++')]
    removed_lines = [l[1:] for l in lines if l.startswith('-') and not l.startswith('---')]

    total_changed = len(added_lines) + len(removed_lines)
    reasons: List[str] = []
    trivial_signals = 0
    significant_signals = 0

    # Size heuristic: very small changes are likely trivial
    if total_changed <= 5:
        trivial_signals += 1
        reasons.append(f"small change ({total_changed} lines)")
    elif total_changed > 50:
        significant_signals += 1
        reasons.append(f"large change ({total_changed} lines)")

    # Common trivial patterns
    trivial_patterns = [
        (r'^\s*$', 'whitespace-only'),
        (r'^\s*#', 'comment-only'),
        (r'^\s*//', 'comment-only'),
        (r'^\s*\*', 'comment-only'),
        (r'version.*\d+\.\d+', 'version bump'),
        (r'import\s+', 'import change'),
        (r'from\s+\S+\s+import', 'import change'),
        (r'require\(', 'import change'),
    ]

    # Significant patterns
    significant_patterns = [
        (r'def\s+\w+', 'new/changed function'),
        (r'class\s+\w+', 'new/changed class'),
        (r'async\s+function', 'new/changed function'),
        (r'export\s+(default\s+)?(function|class|const)', 'API change'),
        (r'CREATE\s+TABLE', 'schema change'),
        (r'ALTER\s+TABLE', 'schema change'),
        (r'DELETE|DROP|REMOVE', 'destructive change'),
    ]

    all_changed = added_lines + removed_lines

    for pattern, label in trivial_patterns:
        matches = sum(1 for l in all_changed if re.search(pattern, l, re.IGNORECASE))
        if matches > 0:
            trivial_signals += 1
            reasons.append(f"{label} ({matches} lines)")

    for pattern, label in significant_patterns:
        matches = sum(1 for l in all_changed if re.search(pattern, l, re.IGNORECASE))
        if matches > 0:
            significant_signals += 1
            reasons.append(f"{label} ({matches} lines)")

    # Character-level change detection for typo fixes
    if total_changed <= 10 and added_lines and removed_lines:
        for add, rem in zip(sorted(added_lines), sorted(removed_lines)):
            ratio = difflib.SequenceMatcher(None, add, rem).ratio()
            if ratio > 0.85:
                trivial_signals += 1
                reasons.append("typo-like edit (>85% similar)")
                break

    # Classify based on signal counts
    if significant_signals > trivial_signals:
        classification = "significant"
        confidence = min(1.0, 0.5 + significant_signals * 0.15)
    elif trivial_signals > 0 and significant_signals == 0:
        classification = "trivial"
        confidence = min(1.0, 0.5 + trivial_signals * 0.15)
    else:
        # Default to significant for safety
        classification = "significant"
        confidence = 0.5

    return {
        "classification": classification,
        "confidence": round(confidence, 2),
        "reasons": reasons,
        "lines_changed": total_changed,
        "trivial_signals": trivial_signals,
        "significant_signals": significant_signals,
    }


def _resolve_path(relative: str) -> Path:
    """Resolve a workspace-relative path to absolute."""
    return _workspace() / relative


def check_staleness() -> Dict[str, StalenessInfo]:
    """Check which docs are stale relative to their source files.

    Uses registry source_mappings first, then falls back to config sourceMapping.
    Registry takes precedence for any overlapping doc paths.

    Returns dict of doc_path -> StalenessInfo for stale docs only.
    """
    cfg = get_config()
    if not cfg.docs.staleness_check_enabled:
        return {}

    # Build doc_to_sources from both registry and config
    doc_to_sources: Dict[str, List[str]] = {}

    # 1. Registry source mappings (takes precedence)
    try:
        from datastore.docsdb.registry import DocsRegistry
        registry = DocsRegistry()
        registry_mappings = registry.get_source_mappings()
        for doc_path, sources in registry_mappings.items():
            doc_to_sources[doc_path] = sources
    except Exception:
        pass  # Registry not available, fall back to config only

    # 2. Config sourceMapping (fallback for unmigrated docs)
    source_mapping = cfg.docs.source_mapping
    if source_mapping:
        for src_path, mapping in source_mapping.items():
            for doc_path in mapping.docs:
                sources = doc_to_sources.setdefault(doc_path, [])
                if src_path not in sources:
                    sources.append(src_path)

    if not doc_to_sources:
        return {}

    stale: Dict[str, StalenessInfo] = {}

    for doc_path, source_paths in doc_to_sources.items():
        doc_abs = _resolve_path(doc_path)
        if not doc_abs.exists():
            continue

        doc_mtime = doc_abs.stat().st_mtime

        stale_sources = []
        latest_source_mtime = 0.0

        for src_path in source_paths:
            src_abs = _resolve_path(src_path)
            if not src_abs.exists():
                continue
            src_mtime = src_abs.stat().st_mtime
            if src_mtime > latest_source_mtime:
                latest_source_mtime = src_mtime
            if src_mtime > doc_mtime:
                stale_sources.append(src_path)

        if stale_sources:
            gap_seconds = latest_source_mtime - doc_mtime

            # Classify the change by gathering git diffs
            classification = None
            try:
                diff_sections = []
                for src in stale_sources:
                    diff = get_git_diff(src, doc_mtime)
                    if diff:
                        diff_sections.append(diff)
                if diff_sections:
                    combined_diff = "\n\n".join(diff_sections)
                    classification = classify_doc_change(combined_diff)
            except Exception:
                pass  # Classification is best-effort

            stale[doc_path] = StalenessInfo(
                doc_path=doc_path,
                gap_hours=gap_seconds / 3600.0,
                stale_sources=stale_sources,
                doc_mtime=doc_mtime,
                latest_source_mtime=latest_source_mtime,
                change_classification=classification,
            )

    return stale


def get_git_diff(source_path: str, since_mtime: float) -> str:
    """Get git log + diff for a source file since a given mtime.

    Returns combined context: commit messages + current diff.
    """
    src_abs = _resolve_path(source_path)
    if not src_abs.exists():
        return ""

    parts = []

    # Git log since the doc was last modified
    since_iso = datetime.fromtimestamp(since_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        log_output = subprocess.run(
            ["git", "log", "--oneline", f"--after={since_iso}", "--", source_path],
            capture_output=True, text=True, cwd=str(_workspace()), timeout=10
        )
        if log_output.returncode == 0 and log_output.stdout.strip():
            parts.append(f"### Commits for {source_path}:\n{log_output.stdout.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Git diff (staged + unstaged changes)
    try:
        diff_output = subprocess.run(
            ["git", "diff", "HEAD", "--", source_path],
            capture_output=True, text=True, cwd=str(_workspace()), timeout=10
        )
        if diff_output.returncode == 0 and diff_output.stdout.strip():
            # Truncate very large diffs
            diff_text = diff_output.stdout.strip()
            if len(diff_text) > 8000:
                diff_text = diff_text[:8000] + "\n... (truncated)"
            parts.append(f"### Diff for {source_path}:\n{diff_text}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return "\n\n".join(parts)


def get_doc_purposes() -> Dict[str, str]:
    """Return the doc purposes mapping from config."""
    return get_config().docs.doc_purposes


def map_sources_to_docs(changed_sources: List[str]) -> Dict[str, List[str]]:
    """Map changed source files to affected docs.

    Returns dict of doc_path -> list of changed source_paths.
    """
    cfg = get_config()
    result: Dict[str, List[str]] = {}

    for src in changed_sources:
        mapping = cfg.docs.source_mapping.get(src)
        if mapping:
            for doc_path in mapping.docs:
                result.setdefault(doc_path, []).append(src)

    return result


def _get_core_markdown_info(doc_path: str) -> Optional[Tuple[str, int]]:
    """Check if doc_path is a core markdown file. Returns (purpose, maxLines) or None."""
    cfg = get_config()
    if not hasattr(cfg.docs, 'core_markdown') or not cfg.docs.core_markdown.files:
        return None
    # Core markdown files are at workspace root — doc_path may be just "TOOLS.md"
    filename = os.path.basename(doc_path)
    file_info = cfg.docs.core_markdown.files.get(filename)
    if file_info:
        purpose = file_info.get('purpose', '')
        max_lines = file_info.get('maxLines', 200)
        return (purpose, max_lines)
    return None


def update_doc_from_diffs(
    doc_path: str,
    purpose: str,
    stale_sources: List[str],
    dry_run: bool = True,
    trigger: str = "janitor",
) -> bool:
    """Update a doc using git diffs as context.

    Used by janitor (nightly) and on-demand updates.
    Detects core markdown targets and uses line-limit-aware prompts.
    Returns True on success.
    """
    doc_abs = _resolve_path(doc_path)
    if not doc_abs.exists():
        print(f"  Doc not found: {doc_abs}")
        return False

    current_doc = doc_abs.read_text()
    chars_before = len(current_doc)
    doc_mtime = doc_abs.stat().st_mtime

    # Gather diffs for each stale source
    diff_sections = []
    for src in stale_sources:
        diff = get_git_diff(src, doc_mtime)
        if diff:
            diff_sections.append(diff)

    if not diff_sections:
        print(f"  No git diffs found for {doc_path}")
        return False

    all_diffs = "\n\n".join(diff_sections)

    # Smart staleness: classify diffs before calling Opus
    # Rule-based classifier catches trivial diffs (comments, whitespace, imports)
    classification = classify_doc_change(all_diffs)
    if classification["classification"] == "trivial" and classification["confidence"] >= 0.7:
        print(f"  Skipping {doc_path} — trivial changes: {', '.join(classification['reasons'])}")
        return True  # Success — no update needed

    # For borderline cases (low-confidence "significant"), use Haiku as a cheap gate
    if classification["confidence"] < 0.6:
        try:
            gate_prompt = (
                f"Does this code diff require updating the documentation at {doc_path}?\n"
                f"Doc purpose: {purpose}\n\n"
                f"Diff:\n{all_diffs[:2000]}\n\n"
                f"Answer YES or NO with a one-sentence reason."
            )
            gate_response, _ = call_fast_reasoning(gate_prompt, max_tokens=50, timeout=10)
            if gate_response and gate_response.strip().upper().startswith("NO"):
                print(f"  Haiku gate: skip {doc_path} — {gate_response.strip()}")
                return True
        except Exception:
            pass  # Gate is best-effort; proceed to Opus if it fails

    # Check if this is a core markdown file (TOOLS.md, AGENTS.md, etc.)
    core_info = _get_core_markdown_info(doc_path)
    if core_info:
        core_purpose, max_lines = core_info
        current_lines = len(current_doc.splitlines())
        print(f"  Core markdown detected: {doc_path} ({current_lines}/{max_lines} lines)")
        system_prompt = (
            "You are updating a CORE MARKDOWN file that loads on EVERY API turn. "
            "Token efficiency is critical.\n\n"
            f"FILE PURPOSE: {core_purpose}\n"
            f"LINE LIMIT: {max_lines} lines (currently {current_lines})\n\n"
            "RULES:\n"
            "1. Only modify SECTIONS affected by the git changes below\n"
            "2. Keep changes CONCISE — summaries only, no implementation details\n"
            "3. If the change needs detailed docs, add a reference like "
            "'**Detailed docs:** `docs/xyz.md`' instead of inline detail\n"
            "4. Do NOT exceed the line limit — trim verbose sections if needed\n"
            "5. Preserve existing structure, headings, and style\n"
            "6. Return the COMPLETE updated file\n\n"
            "Also include a one-line SUMMARY of changes at the very end in this format:\n"
            "<!-- CHANGE_SUMMARY: brief description of what was updated -->"
        )
    else:
        system_prompt = (
            "You are updating technical documentation that has become stale.\n\n"
            f"DOCUMENT PURPOSE: {purpose}\n\n"
            "Update the document to reflect the git changes shown below. "
            "Preserve existing structure and style. Only modify sections that are affected "
            "by the changes. Return the COMPLETE updated document.\n\n"
            "Also include a one-line SUMMARY of changes at the very end in this format:\n"
            "<!-- CHANGE_SUMMARY: brief description of what was updated -->"
        )

    user_message = (
        f"CURRENT DOCUMENT ({doc_path}):\n\n{current_doc}\n\n"
        f"GIT CHANGES SINCE DOC LAST UPDATED:\n\n{all_diffs}"
    )

    print(f"  Calling Opus to update {doc_path}...")
    response, duration = call_deep_reasoning(
        prompt=user_message,
        system_prompt=system_prompt,
        max_tokens=8000,
        timeout=300.0,  # 5 min - doc updates are large
    )

    if not response:
        print(f"  LLM call failed for {doc_path} ({duration:.1f}s)")
        log_doc_update(doc_path, trigger, stale_sources, "LLM call failed",
                       dry_run, False, chars_before, 0)
        return False

    # Extract summary from response
    summary = "Updated from git diffs"
    summary_match = re.search(r'<!-- CHANGE_SUMMARY: (.+?) -->', response)
    if summary_match:
        summary = summary_match.group(1)
        # Remove the summary comment from the doc
        response = re.sub(r'\n*<!-- CHANGE_SUMMARY: .+? -->\n*', '', response).strip()

    print(f"  Opus responded in {duration:.1f}s ({len(response)} chars)")

    chars_after = len(response)

    # Guard: if response is much smaller than original, LLM likely truncated
    if chars_before > 0 and chars_after < chars_before * 0.5 and chars_before > 500:
        print(f"  WARNING: Response ({chars_after} chars) is <50% of original ({chars_before} chars) — possible truncation, skipping write")
        log_doc_update(doc_path, trigger, stale_sources, "Skipped: suspected truncation",
                       dry_run, False, chars_before, chars_after)
        return False

    # Guard: core markdown line limit
    if core_info:
        _, max_lines = core_info
        response_lines = len(response.splitlines())
        if response_lines > max_lines:
            print(f"  WARNING: Response ({response_lines} lines) exceeds limit ({max_lines}) — trimming excess")
            lines = response.splitlines()
            response = "\n".join(lines[:max_lines])
            chars_after = len(response)

    if dry_run:
        print(f"  [DRY RUN] Would update {doc_path} ({chars_before} -> {chars_after} chars)")
        log_doc_update(doc_path, trigger, stale_sources, f"[DRY RUN] {summary}",
                       dry_run, True, chars_before, chars_after)
        return True

    doc_abs.write_text(response)
    print(f"  Updated {doc_path} ({chars_before} -> {chars_after} chars)")
    log_doc_update(doc_path, trigger, stale_sources, summary,
                   dry_run, True, chars_before, chars_after)

    # Sync modified timestamp to registry
    try:
        from datastore.docsdb.registry import DocsRegistry
        registry = DocsRegistry()
        registry.update_timestamps(doc_path, modified_at=datetime.now().isoformat())
    except Exception:
        pass

    return True


def detect_changed_sources_from_transcript(transcript: str) -> List[str]:
    """Use Haiku to detect which monitored source files were modified in a conversation."""
    cfg = get_config()
    monitored = list(cfg.docs.source_mapping.keys())
    if not monitored:
        return []

    prompt = (
        "You are analyzing a conversation transcript to determine which source files "
        "were modified during this session.\n\n"
        f"MONITORED FILES:\n{json.dumps(monitored, indent=2)}\n\n"
        f"CONVERSATION:\n{transcript[:6000]}\n\n"
        "Which of the monitored files were modified (created, edited, or had significant "
        "changes discussed and applied) in this conversation?\n\n"
        "Return JSON: {\"changed\": [\"path/to/file1\", \"path/to/file2\"]}\n"
        "If no monitored files were changed, return: {\"changed\": []}"
    )

    response, duration = call_fast_reasoning(prompt, max_tokens=300, timeout=30.0)
    if not response:
        print(f"  Haiku detection failed ({duration:.1f}s)")
        return []

    try:
        # Parse JSON from response
        json_str = response
        if "```" in response:
            match = __import__("re").search(r"```(?:json)?\s*([\s\S]*?)```", response)
            if match:
                json_str = match.group(1).strip()

        json_match = __import__("re").search(r"\{[\s\S]*\}", json_str)
        if json_match:
            data = json.loads(json_match.group(0))
            changed = data.get("changed", [])
            # Validate against monitored list
            return [f for f in changed if f in monitored]
    except (json.JSONDecodeError, AttributeError):
        pass

    return []


def update_doc_from_transcript(
    doc_path: str,
    purpose: str,
    transcript: str,
    dry_run: bool = True,
    trigger: str = "compact",
    changed_sources: Optional[List[str]] = None,
) -> bool:
    """Update a doc using conversation transcript as context.

    Used at compact/reset when the assistant has full context about what changed.
    Returns True on success.
    """
    doc_abs = _resolve_path(doc_path)
    if not doc_abs.exists():
        print(f"  Doc not found: {doc_abs}")
        return False

    current_doc = doc_abs.read_text()
    chars_before = len(current_doc)
    sources = changed_sources or []

    system_prompt = (
        "You are updating technical documentation based on recent code changes.\n\n"
        f"DOCUMENT PURPOSE: {purpose}\n\n"
        "Analyze the conversation to identify what changed, then output ONLY the specific "
        "edits needed. Use this format for each edit:\n\n"
        "<<<EDIT\n"
        "SECTION: [section heading or 'new section after X']\n"
        "OLD: [exact text to replace, or 'ADD' for new content]\n"
        "NEW: [replacement text]\n"
        ">>>\n\n"
        "After all edits, add a one-line summary:\n"
        "<<<SUMMARY: brief description of what was updated >>>\n\n"
        "Be surgical - only edit what changed. If nothing needs updating, respond with: NO_CHANGES_NEEDED"
    )

    # Truncate transcript if very long to stay within token limits
    trunc_transcript = transcript
    if len(transcript) > 30000:
        trunc_transcript = transcript[:30000] + "\n\n... (truncated)"

    user_message = (
        f"CURRENT DOCUMENT ({doc_path}):\n\n{current_doc}\n\n"
        f"CONVERSATION (what was changed and why):\n\n{trunc_transcript}"
    )

    print(f"  Calling LLM to analyze changes for {doc_path}...")
    response, duration = call_deep_reasoning(
        prompt=user_message,
        system_prompt=system_prompt,
        max_tokens=4000,  # Edits are smaller than full doc
        timeout=120.0,
    )

    if not response:
        print(f"  LLM call failed for {doc_path} ({duration:.1f}s)")
        log_doc_update(doc_path, trigger, sources, "LLM call failed",
                       dry_run, False, chars_before, 0)
        return False

    print(f"  LLM responded in {duration:.1f}s")

    # Extract summary from response
    summary = "Updated from transcript"
    summary_match = re.search(r'<<<SUMMARY:\s*(.+?)\s*>>>', response)
    if summary_match:
        summary = summary_match.group(1)

    if "NO_CHANGES_NEEDED" in response:
        print(f"  No changes needed for {doc_path}")
        log_doc_update(doc_path, trigger, sources, "No changes needed",
                       dry_run, True, chars_before, chars_before)
        return True

    # Parse and apply edits
    edits = re.findall(r'<<<EDIT\s*\n(.*?)>>>', response, re.DOTALL)
    if not edits:
        # Fallback: maybe it returned full doc (old behavior)
        if len(response) > 1000 and response.strip().startswith('#'):
            print(f"  Applying full replacement ({len(response)} chars)")
            chars_after = len(response)
            if not dry_run:
                doc_abs.write_text(response)
                print(f"  Updated {doc_path}")
            log_doc_update(doc_path, trigger, sources, f"Full replacement: {summary}",
                           dry_run, True, chars_before, chars_after)
            return True
        print(f"  No valid edits parsed from response")
        log_doc_update(doc_path, trigger, sources, "No valid edits parsed",
                       dry_run, False, chars_before, 0)
        return False

    updated_doc = current_doc
    applied = 0
    for edit_block in edits:
        lines = edit_block.strip().split('\n')
        old_text = None
        new_text = None
        for i, line in enumerate(lines):
            if line.startswith('OLD:'):
                old_parts = [line[4:].strip()]
                j = i + 1
                while j < len(lines) and not lines[j].startswith('NEW:'):
                    old_parts.append(lines[j])
                    j += 1
                old_text = '\n'.join(old_parts).strip()
            elif line.startswith('NEW:'):
                new_parts = [line[4:].strip()]
                for remaining in lines[i+1:]:
                    new_parts.append(remaining)
                new_text = '\n'.join(new_parts).strip()
                break

        if old_text == 'ADD' and new_text:
            updated_doc = updated_doc.rstrip() + '\n\n' + new_text
            applied += 1
        elif old_text and new_text and old_text in updated_doc:
            updated_doc = updated_doc.replace(old_text, new_text, 1)
            applied += 1

    chars_after = len(updated_doc)

    if applied > 0:
        if dry_run:
            print(f"  [DRY RUN] Would apply {applied} edit(s) to {doc_path}")
        else:
            doc_abs.write_text(updated_doc)
            print(f"  Applied {applied} edit(s) to {doc_path}")
            # Sync modified timestamp to registry
            try:
                from datastore.docsdb.registry import DocsRegistry
                registry = DocsRegistry()
                registry.update_timestamps(doc_path, modified_at=datetime.now().isoformat())
            except Exception:
                pass
        log_doc_update(doc_path, trigger, sources, f"{applied} edit(s): {summary}",
                       dry_run, True, chars_before, chars_after)
        return True
    else:
        print(f"  Could not match edits to doc content")
        log_doc_update(doc_path, trigger, sources, "Edits didn't match doc content",
                       dry_run, False, chars_before, 0)
        return False


def cmd_check(json_output: bool = False) -> Dict[str, StalenessInfo]:
    """CLI: check staleness and print results."""
    stale = check_staleness()

    if json_output:
        out = {}
        for doc_path, info in stale.items():
            entry: Dict[str, Any] = {
                "gap_hours": round(info.gap_hours, 1),
                "stale_sources": info.stale_sources,
                "doc_mtime": datetime.fromtimestamp(info.doc_mtime).isoformat(),
                "latest_source_mtime": datetime.fromtimestamp(info.latest_source_mtime).isoformat(),
            }
            if info.change_classification:
                entry["change_classification"] = info.change_classification
            out[doc_path] = entry
        print(json.dumps(out, indent=2))
    else:
        if not stale:
            print("All docs up-to-date with source files.")
        else:
            print(f"Found {len(stale)} stale doc(s):\n")
            for doc_path, info in stale.items():
                cls_label = ""
                if info.change_classification:
                    cls = info.change_classification["classification"]
                    conf = info.change_classification["confidence"]
                    cls_label = f" [{cls} ({conf:.0%} confidence)]"
                print(f"  {doc_path} ({info.gap_hours:.1f}h behind){cls_label}")
                for src in info.stale_sources:
                    print(f"    <- {src}")

    return stale


def cmd_update(doc_path: str, dry_run: bool = True) -> bool:
    """CLI: update a specific doc from git diffs."""
    stale = check_staleness()
    info = stale.get(doc_path)

    if not info:
        print(f"{doc_path} is up-to-date (or not in source mapping).")
        return False

    purposes = get_doc_purposes()
    purpose = purposes.get(doc_path, "")
    return update_doc_from_diffs(doc_path, purpose, info.stale_sources, dry_run=dry_run)


def cmd_update_stale(dry_run: bool = True, trivial_only: bool = False) -> int:
    """CLI: update all stale docs from git diffs. Returns count of updated docs.

    Args:
        dry_run: If True, don't actually write changes.
        trivial_only: If True, only auto-update docs with trivial changes.
            Significant changes will be skipped with a warning.
    """
    stale = check_staleness()
    if not stale:
        print("All docs up-to-date.")
        return 0

    purposes = get_doc_purposes()
    cfg = get_config()
    max_docs = cfg.docs.max_docs_per_update
    updated = 0
    skipped_significant = 0

    for doc_path, info in list(stale.items())[:max_docs]:
        # Check classification if trivial_only mode
        if trivial_only and info.change_classification:
            cls = info.change_classification.get("classification", "significant")
            if cls == "significant":
                conf = info.change_classification.get("confidence", 0)
                reasons = info.change_classification.get("reasons", [])
                print(f"  SKIPPED {doc_path} — significant change "
                      f"({conf:.0%} confidence): {', '.join(reasons)}")
                skipped_significant += 1
                continue

        purpose = purposes.get(doc_path, "")

        # Print classification info before updating
        if info.change_classification:
            cls = info.change_classification.get("classification", "unknown")
            conf = info.change_classification.get("confidence", 0)
            print(f"  [{cls} change, {conf:.0%} confidence] {doc_path}")

        ok = update_doc_from_diffs(doc_path, purpose, info.stale_sources, dry_run=dry_run)
        if ok:
            updated += 1

    action = "Would update" if dry_run else "Updated"
    print(f"\n{action} {updated}/{len(stale)} stale doc(s)")
    if skipped_significant:
        print(f"  Skipped {skipped_significant} doc(s) with significant changes "
              "(use without --trivial-only to update all)")
    return updated


def cmd_update_from_transcript(transcript_path: str, dry_run: bool = True, max_docs: int = 3) -> int:
    """CLI: update docs from a conversation transcript. Returns count updated."""
    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        print(f"Transcript not found: {transcript_path}")
        return 0

    transcript = transcript_file.read_text()
    if not transcript.strip():
        print("Empty transcript.")
        return 0

    print(f"Transcript: {len(transcript)} chars")

    # Detect which monitored files were changed
    changed = detect_changed_sources_from_transcript(transcript)
    if not changed:
        print("No monitored source files detected as changed in transcript.")
        return 0

    print(f"Detected changed sources: {changed}")

    # Map to affected docs
    affected = map_sources_to_docs(changed)
    if not affected:
        print("Changed sources don't map to any docs.")
        return 0

    purposes = get_doc_purposes()
    updated = 0

    for doc_path, doc_sources in list(affected.items())[:max_docs]:
        purpose = purposes.get(doc_path, "")
        ok = update_doc_from_transcript(
            doc_path, purpose, transcript,
            dry_run=dry_run, trigger="compact", changed_sources=doc_sources
        )
        if ok:
            updated += 1

    print(f"\n{'Would update' if dry_run else 'Updated'} {updated} doc(s) from transcript")
    return updated


def cmd_changelog(limit: int = 20, json_output: bool = False) -> None:
    """CLI: view recent changelog entries."""
    entries = get_changelog(limit)

    if json_output:
        print(json.dumps(entries, indent=2))
        return

    if not entries:
        print("No doc updates logged yet.")
        return

    print(f"Recent doc updates (last {len(entries)}):\n")
    for entry in reversed(entries):  # Most recent first
        ts = entry.get("timestamp", "")[:19]  # Trim microseconds
        doc = entry.get("doc_path", "?")
        trigger = entry.get("trigger", "?")
        summary = entry.get("summary", "")
        success = "✓" if entry.get("success") else "✗"
        dry_run = "[DRY]" if entry.get("dry_run") else ""
        chars = f"{entry.get('chars_before', 0)}→{entry.get('chars_after', 0)}"

        print(f"  {ts} {success} {dry_run:5} {trigger:8} {doc}")
        if summary:
            print(f"    {summary}")
        sources = entry.get("sources", [])
        if sources:
            print(f"    Sources: {', '.join(sources)}")
        print(f"    Chars: {chars}")
        print()


def cmd_classify_change(diff_text: str) -> Dict[str, Any]:
    """CLI: classify a diff as trivial or significant and print result as JSON."""
    result = classify_doc_change(diff_text)
    print(json.dumps(result, indent=2))
    return result


# =============================================================================
# Feature 11: Git-based drift detection and staleness scoring
# =============================================================================

@dataclass
class DriftReport:
    """Report of a doc that has drifted from its source files."""
    doc_path: str
    source_files: List[str]
    staleness_score: float
    commits_behind: int
    lines_changed: int
    days_stale: float
    classification: Optional[str] = None


def detect_drift_from_git(since_hours: int = 24) -> List[DriftReport]:
    """Detect ALL doc drift from git history. No hooks, no cooperation needed.

    Compares source-file last-commit-time vs doc-file last-commit-time
    for every configured source→doc mapping.
    """
    cfg = get_config()
    doc_to_sources: Dict[str, List[str]] = {}

    # Build mappings from registry + config
    try:
        from datastore.docsdb.registry import DocsRegistry
        registry = DocsRegistry()
        for doc_path, sources in registry.get_source_mappings().items():
            doc_to_sources[doc_path] = sources
    except Exception:
        pass

    source_mapping = cfg.docs.source_mapping
    if source_mapping:
        for src_path, mapping in source_mapping.items():
            for doc_path in mapping.docs:
                sources = doc_to_sources.setdefault(doc_path, [])
                if src_path not in sources:
                    sources.append(src_path)

    reports: List[DriftReport] = []
    for doc_path, source_paths in doc_to_sources.items():
        doc_abs = _resolve_path(doc_path)
        if not doc_abs.exists():
            continue

        # Get doc's last commit timestamp
        try:
            doc_commit_ts = subprocess.run(
                ["git", "log", "-1", "--format=%ct", "--", doc_path],
                capture_output=True, text=True, cwd=str(_workspace()), timeout=10
            ).stdout.strip()
            doc_commit_time = int(doc_commit_ts) if doc_commit_ts else 0
        except Exception:
            doc_commit_time = 0

        # Check each source file
        total_commits_behind = 0
        total_lines_changed = 0
        stale_sources = []

        for src_path in source_paths:
            src_abs = _resolve_path(src_path)
            if not src_abs.exists():
                continue

            try:
                src_commit_ts = subprocess.run(
                    ["git", "log", "-1", "--format=%ct", "--", src_path],
                    capture_output=True, text=True, cwd=str(_workspace()), timeout=10
                ).stdout.strip()
                src_commit_time = int(src_commit_ts) if src_commit_ts else 0
            except Exception:
                continue

            if src_commit_time > doc_commit_time:
                stale_sources.append(src_path)
                # Count commits source is ahead
                try:
                    if doc_commit_time > 0:
                        commit_hash = subprocess.run(
                            ["git", "log", "-1", "--format=%H", f"--until={doc_commit_time}", "--", src_path],
                            capture_output=True, text=True, cwd=str(_workspace()), timeout=10
                        ).stdout.strip()
                        if commit_hash:
                            count_out = subprocess.run(
                                ["git", "rev-list", "--count", f"{commit_hash}..HEAD", "--", src_path],
                                capture_output=True, text=True, cwd=str(_workspace()), timeout=10
                            ).stdout.strip()
                            total_commits_behind += int(count_out) if count_out else 1
                        else:
                            total_commits_behind += 1
                    else:
                        total_commits_behind += 1
                except Exception:
                    total_commits_behind += 1

                # Lines changed
                try:
                    stat_out = subprocess.run(
                        ["git", "diff", "--stat", "--", src_path],
                        capture_output=True, text=True, cwd=str(_workspace()), timeout=10
                    ).stdout
                    # Parse "N insertions, M deletions" from last line
                    for line in stat_out.strip().split("\n"):
                        nums = re.findall(r'(\d+)\s+(?:insertion|deletion)', line)
                        total_lines_changed += sum(int(n) for n in nums)
                except Exception:
                    pass

        if stale_sources:
            days_stale = (time.time() - doc_commit_time) / 86400 if doc_commit_time > 0 else 0
            score = _compute_staleness_score(
                total_commits_behind, total_lines_changed, days_stale
            )
            reports.append(DriftReport(
                doc_path=doc_path,
                source_files=stale_sources,
                staleness_score=score,
                commits_behind=total_commits_behind,
                lines_changed=total_lines_changed,
                days_stale=round(days_stale, 1),
            ))

    # Sort by staleness score descending
    reports.sort(key=lambda r: r.staleness_score, reverse=True)
    return reports


def _compute_staleness_score(commits_behind: int, lines_changed: int, days_stale: float) -> float:
    """Compute per-doc staleness score (0-100).

    Higher = more stale = should be updated first.
    """
    # Commits factor: each commit behind adds 10 points (max 40)
    commits_factor = min(commits_behind * 10, 40)
    # Lines factor: log scale (max 30)
    import math
    lines_factor = min(math.log1p(lines_changed) * 5, 30)
    # Days factor: each day adds 3 points (max 30)
    days_factor = min(days_stale * 3, 30)

    return round(commits_factor + lines_factor + days_factor, 1)


# =============================================================================
# Feature 11F: SQLite audit log (replace JSON changelogs)
# =============================================================================

def _get_audit_db_path() -> str:
    """Get path for the audit log database."""
    from lib.config import get_db_path
    return get_db_path()


def _ensure_audit_table():
    """Create doc_update_log table if it doesn't exist."""
    from lib.database import get_connection
    with get_connection(_get_audit_db_path()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS doc_update_log (
                id INTEGER PRIMARY KEY,
                doc_path TEXT NOT NULL,
                source_files TEXT,
                staleness_score REAL,
                change_summary TEXT,
                commit_hash TEXT,
                agent_id TEXT DEFAULT 'unknown',
                timestamp TEXT DEFAULT (datetime('now'))
            )
        """)


def log_doc_update_db(
    doc_path: str,
    source_files: List[str],
    staleness_score: float,
    change_summary: str,
    commit_hash: Optional[str] = None,
    agent_id: str = "unknown"
) -> None:
    """Log a doc update to SQLite audit table (concurrent-safe via WAL mode)."""
    try:
        _ensure_audit_table()
        from lib.database import get_connection
        with get_connection(_get_audit_db_path()) as conn:
            conn.execute("""
                INSERT INTO doc_update_log (doc_path, source_files, staleness_score, change_summary, commit_hash, agent_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (doc_path, json.dumps(source_files), staleness_score, change_summary, commit_hash, agent_id))
    except Exception as e:
        print(f"  Warning: audit log write failed: {e}", file=sys.stderr)


def get_update_log(limit: int = 50) -> List[Dict[str, Any]]:
    """Read recent entries from the SQLite audit log."""
    try:
        _ensure_audit_table()
        from lib.database import get_connection
        with get_connection(_get_audit_db_path()) as conn:
            rows = conn.execute(
                "SELECT * FROM doc_update_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception:
        return []


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register docs lifecycle maintenance routines."""

    def _run_docs_staleness(ctx):
        result = result_factory()
        try:
            stale = check_staleness()
            if not stale:
                result.logs.append("All docs up-to-date with source files")
                return result

            result.logs.append(f"Found {len(stale)} stale doc(s):")
            purposes = get_doc_purposes()
            updated = 0
            for doc_path, info in stale.items():
                result.logs.append(f"  {doc_path} ({info.gap_hours:.1f}h behind)")
                for src in info.stale_sources:
                    result.logs.append(f"    <- {src}")

                allow_apply = not ctx.dry_run
                if allow_apply and ctx.allow_doc_apply is not None:
                    allow_apply = ctx.allow_doc_apply(doc_path, "staleness update")
                if allow_apply:
                    ok = update_doc_from_diffs(
                        doc_path,
                        purposes.get(doc_path, ""),
                        info.stale_sources,
                        dry_run=False,
                    )
                    if ok:
                        updated += 1
            result.metrics["docs_updated"] = updated
        except Exception as exc:
            result.errors.append(f"Docs staleness failed: {exc}")
        return result

    def _run_docs_cleanup(ctx):
        result = result_factory()
        try:
            needs_cleanup = check_cleanup_needed()
            if not needs_cleanup:
                result.logs.append("No docs need cleanup")
                return result

            result.logs.append(f"Found {len(needs_cleanup)} doc(s) needing cleanup:")
            purposes = get_doc_purposes()
            cleaned = 0
            for doc_path, info in needs_cleanup.items():
                reason_str = {
                    "updates": f"{info.updates_since_cleanup} updates",
                    "growth": f"{info.growth_ratio:.1f}x growth",
                    "both": f"{info.updates_since_cleanup} updates + {info.growth_ratio:.1f}x growth",
                }[info.reason]
                result.logs.append(f"  {doc_path} ({reason_str})")
                allow_apply = not ctx.dry_run
                if allow_apply and ctx.allow_doc_apply is not None:
                    allow_apply = ctx.allow_doc_apply(doc_path, "cleanup")
                if allow_apply:
                    ok = cleanup_doc(doc_path, purposes.get(doc_path, ""), dry_run=False)
                    if ok:
                        cleaned += 1
            result.metrics["docs_cleaned"] = cleaned
        except Exception as exc:
            result.errors.append(f"Docs cleanup failed: {exc}")
        return result

    registry.register("docs_staleness", _run_docs_staleness)
    registry.register("docs_cleanup", _run_docs_cleanup)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Documentation Auto-Updater")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # check
    check_parser = subparsers.add_parser("check", help="Check for stale docs")
    check_parser.add_argument("--json", action="store_true", help="JSON output")

    # update <doc_path>
    update_parser = subparsers.add_parser("update", help="Update a specific doc from diffs")
    update_parser.add_argument("doc_path", help="Doc path (relative to workspace)")
    update_parser.add_argument("--apply", action="store_true", help="Apply changes")

    # update-stale
    stale_parser = subparsers.add_parser("update-stale", help="Update all stale docs")
    stale_parser.add_argument("--apply", action="store_true", help="Apply changes")
    stale_parser.add_argument("--trivial-only", action="store_true",
                              help="Only auto-update trivial changes; skip significant ones")

    # update-from-transcript
    transcript_parser = subparsers.add_parser("update-from-transcript",
                                               help="Update docs from conversation transcript")
    transcript_parser.add_argument("--transcript", required=True, help="Path to transcript file")
    transcript_parser.add_argument("--apply", action="store_true", help="Apply changes")
    transcript_parser.add_argument("--max-docs", type=int, default=3, help="Max docs to update")

    # changelog
    log_parser = subparsers.add_parser("changelog", help="View recent doc update history")
    log_parser.add_argument("--limit", type=int, default=20, help="Max entries to show")
    log_parser.add_argument("--json", action="store_true", help="JSON output")

    # cleanup-check
    cleanup_check_parser = subparsers.add_parser("cleanup-check", help="Check which docs need cleanup")
    cleanup_check_parser.add_argument("--json", action="store_true", help="JSON output")

    # classify-change
    classify_parser = subparsers.add_parser("classify-change",
                                             help="Classify a diff as trivial or significant")
    classify_parser.add_argument("--diff", help="Diff text (or reads from stdin if omitted)")

    # cleanup
    cleanup_parser = subparsers.add_parser("cleanup", help="Clean up bloated docs")
    cleanup_parser.add_argument("doc_path", nargs="?", help="Specific doc to clean (optional)")
    cleanup_parser.add_argument("--apply", action="store_true", help="Apply changes")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "check":
        cmd_check(json_output=args.json)
    elif args.command == "update":
        dry_run = not args.apply
        ok = cmd_update(args.doc_path, dry_run=dry_run)
        sys.exit(0 if ok else 1)
    elif args.command == "update-stale":
        dry_run = not args.apply
        count = cmd_update_stale(dry_run=dry_run, trivial_only=args.trivial_only)
        sys.exit(0 if count > 0 else 1)
    elif args.command == "update-from-transcript":
        dry_run = not args.apply
        count = cmd_update_from_transcript(
            args.transcript, dry_run=dry_run, max_docs=args.max_docs
        )
        sys.exit(0 if count > 0 else 1)
    elif args.command == "changelog":
        cmd_changelog(limit=args.limit, json_output=args.json)
    elif args.command == "cleanup-check":
        cmd_cleanup_check(json_output=args.json)
    elif args.command == "classify-change":
        if args.diff:
            diff_text = args.diff
        else:
            diff_text = sys.stdin.read()
        cmd_classify_change(diff_text)
    elif args.command == "cleanup":
        dry_run = not args.apply
        count = cmd_cleanup(doc_path=args.doc_path, dry_run=dry_run)
        sys.exit(0 if count > 0 else 1)
