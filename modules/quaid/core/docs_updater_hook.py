"""Post-extraction docs updater hook.

After extraction, takes shadow git diffs and project logs, classifies
the change scope, and routes to the existing docs updater to update
project-level documentation (TOOLS.md, AGENTS.md, etc.).

Uses the classify → gate → update pipeline from datastore.docsdb.updater
to avoid unnecessary LLM calls for trivial changes.

See docs/PROJECT-SYSTEM-SPEC.md#extraction-event-integration.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.fail_policy import is_fail_hard_enabled

logger = logging.getLogger(__name__)

# Root-level project docs that can be auto-updated from source diffs/logs.
# PROJECT.log remains append-only and must never be edited by this path.
_UPDATABLE_ROOT_DOCS = {"PROJECT.md", "TOOLS.md", "AGENTS.md"}
_FAST_GATE_MAX_TOKENS = 200
_DEFAULT_MAX_DOCS_PER_RUN = 12
_PROJECT_MD_MANAGED_SECTIONS = {
    "project home",
    "source roots",
    "in this project directory",
    "external files",
    "registered docs",
    "recent major changes",
}
_PROJECT_MD_MANAGED_MARKERS = (
    ("<!-- BEGIN:PROJECT_HOME -->", "<!-- END:PROJECT_HOME -->"),
    ("<!-- BEGIN:SOURCE_ROOTS -->", "<!-- END:SOURCE_ROOTS -->"),
    ("<!-- BEGIN:IN_DIR_FILES -->", "<!-- END:IN_DIR_FILES -->"),
    ("<!-- BEGIN:EXTERNAL_FILES -->", "<!-- END:EXTERNAL_FILES -->"),
    ("<!-- BEGIN:REGISTERED_DOCS -->", "<!-- END:REGISTERED_DOCS -->"),
    ("<!-- BEGIN:PROJECT_LOG -->", "<!-- END:PROJECT_LOG -->"),
)


class DocsEditBlockMismatch(RuntimeError):
    """Raised when the LLM produced edit blocks that do not apply cleanly."""


def _log_preview(value: Any, limit: int = 1200) -> str:
    text = str(value or "").replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("[docs-hook] Invalid %s=%r; using default %d", name, raw, default)
        return default
    return max(minimum, value)


def _max_docs_per_run() -> int:
    return _env_int("QUAID_DOCS_HOOK_MAX_DOCS_PER_RUN", _DEFAULT_MAX_DOCS_PER_RUN, minimum=1)


def _split_git_diff_sections(diff_text: str) -> List[str]:
    text = str(diff_text or "")
    if not text:
        return []
    sections: List[str] = []
    current: List[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            sections.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("".join(current))
    return sections


def _bounded_snapshot_diff(diff_text: str) -> str:
    from datastore.docsdb.updater import (
        _max_diff_file_bytes,
        _max_diff_total_bytes,
        _truncate_text_bytes,
    )

    sections = _split_git_diff_sections(diff_text)
    if not sections:
        return ""

    max_file = _max_diff_file_bytes()
    max_total = _max_diff_total_bytes()
    bounded: List[str] = []
    total = 0

    for section_index, section in enumerate(sections):
        clipped, _ = _truncate_text_bytes(section, max_file)
        section_bytes = len(clipped.encode("utf-8", errors="replace"))
        if total + section_bytes > max_total:
            remaining = max_total - total
            if remaining > 512:
                tail, _ = _truncate_text_bytes(clipped, remaining)
                bounded.append(tail)
            skipped = len(sections) - section_index
            bounded.append(
                "\n### Snapshot diff prompt limit reached\n"
                f"- safety_mode: catalog_only_tail\n"
                f"- max_total_diff_bytes: {max_total}\n"
                f"- remaining_diff_sections_not_expanded: {max(0, skipped)}\n"
                "- note: omitted diff sections must be treated as catalog-only until a scoped update drills into them.\n"
            )
            break
        bounded.append(clipped)
        total += section_bytes

    return "\n\n".join(part.rstrip("\n") for part in bounded if part)


def _parse_edit_block_fields(edit_block: str) -> Dict[str, str]:
    lines = str(edit_block or "").strip().split("\n")
    parsed: Dict[str, str] = {"section": "", "old": "", "new": ""}
    for i, line in enumerate(lines):
        if line.startswith("SECTION:"):
            parsed["section"] = line[len("SECTION:"):].strip()
        elif line.startswith("OLD:"):
            old_parts = [line[len("OLD:"):].strip()]
            j = i + 1
            while j < len(lines) and not lines[j].startswith("NEW:"):
                old_parts.append(lines[j])
                j += 1
            parsed["old"] = "\n".join(old_parts).strip()
        elif line.startswith("NEW:"):
            new_parts = [line[len("NEW:"):].strip()]
            new_parts.extend(lines[i + 1:])
            parsed["new"] = "\n".join(new_parts).strip()
            break
    return parsed


def _text_overlaps_project_md_managed_block(doc_text: str, needle: str) -> bool:
    if not needle or needle == "ADD":
        return False
    spans = []
    for begin, end in _PROJECT_MD_MANAGED_MARKERS:
        start = doc_text.find(begin)
        stop = doc_text.find(end, start + len(begin)) if start >= 0 else -1
        if start >= 0 and stop >= 0:
            spans.append((start, stop + len(end)))
    if not spans:
        return False
    start = doc_text.find(needle)
    while start >= 0:
        stop = start + len(needle)
        if any(start >= span_start and stop <= span_stop for span_start, span_stop in spans):
            return True
        start = doc_text.find(needle, start + 1)
    return False


def _project_md_managed_edit_reason(doc_text: str, edit_block: str) -> Optional[str]:
    parsed = _parse_edit_block_fields(edit_block)
    section = parsed["section"].strip().lower().lstrip("#").strip()
    if section in _PROJECT_MD_MANAGED_SECTIONS:
        return f"section={parsed['section']!r}"
    old_text = parsed["old"]
    new_text = parsed["new"]
    combined = f"{old_text}\n{new_text}"
    for begin, end in _PROJECT_MD_MANAGED_MARKERS:
        if begin in combined or end in combined:
            return f"marker={begin}"
    if _text_overlaps_project_md_managed_block(doc_text, old_text):
        return "old text is inside a registry-managed marker block"
    return None


def _filter_project_md_managed_edits(doc_name: str, doc_text: str, edits: List[str]) -> List[str]:
    if doc_name != "PROJECT.md":
        return edits
    kept = []
    for index, edit_block in enumerate(edits, start=1):
        reason = _project_md_managed_edit_reason(doc_text, edit_block)
        if not reason:
            kept.append(edit_block)
            continue
        parsed = _parse_edit_block_fields(edit_block)
        logger.warning(
            "[docs-hook] Ignoring PROJECT.md LLM edit targeting registry-managed marker block #%d (%s); OLD=%r NEW=%r",
            index,
            reason,
            _log_preview(parsed["old"]),
            _log_preview(parsed["new"]),
        )
    return kept


def _unmatched_edit_blocks(doc_text: str, edits: List[str]) -> List[Dict[str, str]]:
    updated = doc_text
    unmatched: List[Dict[str, str]] = []
    for index, edit_block in enumerate(edits, start=1):
        parsed = _parse_edit_block_fields(edit_block)
        old_text = parsed["old"]
        new_text = parsed["new"]
        if old_text == "ADD" and new_text:
            updated = updated.rstrip() + "\n\n" + new_text
        elif old_text and new_text and old_text in updated:
            updated = updated.replace(old_text, new_text, 1)
        else:
            parsed["index"] = str(index)
            unmatched.append(parsed)
    return unmatched


def _normalize_heading(value: str) -> str:
    return str(value or "").strip().lstrip("#").strip().casefold()


def _apply_project_md_empty_section_edits(
    doc_text: str,
    edits: List[str],
) -> tuple[str, List[str], int]:
    updated = doc_text
    remaining = []
    applied = 0

    for edit_block in edits:
        parsed = _parse_edit_block_fields(edit_block)
        if parsed["old"] != "(empty)" or not parsed["section"].strip() or not parsed["new"].strip():
            remaining.append(edit_block)
            continue

        section_name = _normalize_heading(parsed["section"])
        match = None
        for candidate in re.finditer(r"^(#{1,6})[ \t]+(.+?)[ \t]*\n", updated, flags=re.MULTILINE):
            if _normalize_heading(candidate.group(2)) == section_name:
                match = candidate
                break
        if match is None:
            remaining.append(edit_block)
            continue

        level = len(match.group(1))
        next_heading = re.search(
            rf"^#{{1,{level}}}[ \t]+.+?[ \t]*\n",
            updated[match.end():],
            flags=re.MULTILINE,
        )
        content_start = match.end()
        content_end = len(updated) if next_heading is None else content_start + next_heading.start()
        if updated[content_start:content_end].strip():
            remaining.append(edit_block)
            continue

        replacement = "\n" + parsed["new"].strip() + "\n\n"
        updated = updated[:content_start] + replacement + updated[content_end:]
        applied += 1

    return updated, remaining, applied


def update_project_docs(
    snapshots: List[Dict[str, Any]],
    extraction_result: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    force_project: Optional[str] = None,
) -> Dict[str, Any]:
    """Update project docs based on shadow git diffs and extraction context.

    Called as a post-extraction hook. For each project with changes:
    1. Classify the diff (trivial vs significant)
    2. Gate borderline cases with Fast Reasoning
    3. Update docs with Deep Reasoning only for significant changes

    Args:
        snapshots: Output from project_registry.snapshot_all_projects()
            Each entry has: project, is_initial, diff, changes
        extraction_result: Output from extract_from_transcript() (optional)
            Used to provide project log context alongside the diff.
        dry_run: If True, log what would be updated without writing.

    Returns:
        Dict with metrics: projects_checked, docs_updated, docs_skipped, errors
    """
    from datastore.docsdb.updater import classify_doc_change

    metrics = {
        "projects_checked": 0,
        "docs_updated": 0,
        "docs_skipped": 0,
        "trivial_skipped": 0,
        "errors": 0,
    }

    if not snapshots and not force_project:
        return metrics

    # Get project logs from extraction result if available
    project_logs = {}
    if extraction_result:
        project_logs = extraction_result.get("project_logs", {})

    snapshots_by_project = {
        str(snapshot.get("project") or "").strip(): snapshot
        for snapshot in snapshots
        if str(snapshot.get("project") or "").strip()
    }
    project_names = list(snapshots_by_project.keys())
    if force_project and force_project not in project_names:
        project_names.append(force_project)

    for project_name in project_names:
        snapshot = snapshots_by_project.get(project_name, {})
        diff_text = snapshot.get("diff", "")
        changes = snapshot.get("changes", [])
        project_log = project_logs.get(project_name, [])

        if not diff_text and not changes and not project_log:
            continue

        metrics["projects_checked"] += 1

        # Classify the change scope
        classification = classify_doc_change(diff_text) if diff_text else {
            "classification": "significant",
            "confidence": 0.5,
            "reasons": ["no diff available, only file list"],
        }

        if classification["classification"] == "trivial" and classification["confidence"] >= 0.7:
            logger.info(
                "[docs-hook] %s: trivial changes, skipping docs update (%s)",
                project_name, ", ".join(classification.get("reasons", [])),
            )
            metrics["trivial_skipped"] += 1
            continue

        _update_project(
            project_name, diff_text, changes, project_log,
            classification, dry_run, metrics,
        )

    logger.info(
        "[docs-hook] checked=%d updated=%d skipped=%d trivial=%d errors=%d",
        metrics["projects_checked"], metrics["docs_updated"],
        metrics["docs_skipped"], metrics["trivial_skipped"],
        metrics["errors"],
    )
    return metrics


def _update_project(
    project_name: str,
    diff_text: str,
    changes: List[Dict],
    project_log: List[str],
    classification: Dict[str, Any],
    dry_run: bool,
    metrics: Dict[str, int],
) -> None:
    """Update docs for a single project."""
    from core.project_registry import get_project

    project = get_project(project_name)
    if not project:
        logger.warning("[docs-hook] Project not found in registry: %s", project_name)
        return

    canonical = Path(project["canonical_path"])
    if not canonical.is_dir():
        return

    # Find which docs exist for this project. The updater may edit the
    # project-facing docs surface, but PROJECT.log is append-only and excluded.
    docs_to_update = []
    for doc_name in sorted(_UPDATABLE_ROOT_DOCS):
        doc_path = canonical / doc_name
        if doc_path.is_file():
            docs_to_update.append(doc_path)
    docs_dir = canonical / "docs"
    if docs_dir.is_dir():
        for doc_path in sorted(docs_dir.rglob("*.md")):
            if doc_path.name == "PROJECT.log":
                continue
            docs_to_update.append(doc_path)

    if not docs_to_update:
        metrics["docs_skipped"] += 1
        return

    max_docs = _max_docs_per_run()
    if len(docs_to_update) > max_docs:
        omitted = len(docs_to_update) - max_docs
        logger.warning(
            "[docs-hook] %s: limiting docs update run to %d docs; %d docs omitted",
            project_name,
            max_docs,
            omitted,
        )
        docs_to_update = docs_to_update[:max_docs]
        metrics["docs_skipped"] += omitted

    # Build context for the LLM
    context = _build_update_context(
        project_name, diff_text, changes, project_log,
    )

    for doc_path in docs_to_update:
        try:
            success = _update_single_doc(
                doc_path, context, classification, dry_run,
            )
            if success:
                metrics["docs_updated"] += 1
            else:
                metrics["docs_skipped"] += 1
        except DocsEditBlockMismatch as e:
            logger.error("[docs-hook] Failed to update %s: %s", doc_path, e)
            metrics["errors"] += 1
            if is_fail_hard_enabled():
                raise
        except Exception as e:
            if is_fail_hard_enabled():
                raise
            logger.warning("[docs-hook] Failed to update %s: %s", doc_path, e)
            metrics["errors"] += 1


def _build_update_context(
    project_name: str,
    diff_text: str,
    changes: List[Dict],
    project_log: List[str],
) -> str:
    """Build the context string for the docs update LLM call."""
    parts = [f"# Project: {project_name}\n"]

    # File change summary
    if changes:
        parts.append("## Files Changed")
        for c in changes:
            status_label = {
                "A": "added", "M": "modified", "D": "deleted", "R": "renamed",
            }.get(c["status"], c["status"])
            line = f"- {c['path']} ({status_label})"
            if c.get("old_path"):
                line += f" (was: {c['old_path']})"
            parts.append(line)
        parts.append("")

    # Git diff
    if diff_text:
        bounded_diff = _bounded_snapshot_diff(diff_text)
        parts.append("## Code Diff")
        parts.append("```diff")
        parts.append(bounded_diff)
        parts.append("```\n")

    # Project log entries from this extraction
    if project_log:
        parts.append("## Recent Project Activity")
        for entry in project_log:
            parts.append(f"- {entry}")
        parts.append("")

    return "\n".join(parts)


def _update_single_doc(
    doc_path: Path,
    context: str,
    classification: Dict[str, Any],
    dry_run: bool,
) -> bool:
    """Update a single doc file using the LLM.

    Uses the same edit format as the existing docs updater.
    Returns True if the doc was updated (or would be in dry_run).
    """
    from lib.llm_clients import call_deep_reasoning, call_fast_reasoning, parse_json_response

    from datastore.docsdb.updater import _max_doc_prompt_bytes, _read_prompt_doc

    current_doc, doc_capped = _read_prompt_doc(doc_path)
    doc_name = doc_path.name
    if doc_capped:
        logger.warning(
            "[docs-hook] Skipping %s: current doc exceeds prompt safety cap (%d bytes)",
            doc_path,
            _max_doc_prompt_bytes(),
        )
        return False

    # For borderline classification, use Fast Reasoning as a cheap gate
    if classification.get("confidence", 1.0) < 0.6:
        try:
            gate_prompt = (
                f"Does this change require updating {doc_name}?\n\n"
                f"{context}\n\n"
                f"Current {doc_name} starts with:\n{current_doc[:500]}\n\n"
                "Answer YES or NO with a one-sentence reason."
            )
            gate_response, _ = call_fast_reasoning(
                gate_prompt,
                max_tokens=_FAST_GATE_MAX_TOKENS,
                timeout=10,
                system_prompt="Answer with YES or NO first, then one short sentence.",
            )
            if gate_response and gate_response.strip().upper().startswith("NO"):
                logger.info("[docs-hook] Fast Reasoning gate: skip %s — %s", doc_name, gate_response.strip())
                return False
        except Exception as e:
            if is_fail_hard_enabled():
                raise
            logger.warning("[docs-hook] Fast Reasoning gate failed for %s: %s", doc_name, e)

    # Call Deep Reasoning for the actual update
    system_prompt = (
        f"You are updating {doc_name} for a software project based on recent code changes.\n\n"
        "Analyze the changes and output ONLY the specific edits needed. "
        "Use this format for each edit:\n\n"
        "<<<EDIT\n"
        "SECTION: [section heading or 'new section after X']\n"
        "OLD: [exact text to replace, or 'ADD' for new content]\n"
        "NEW: [replacement text]\n"
        ">>>\n\n"
        "After all edits, add a one-line summary:\n"
        "<<<SUMMARY: brief description of what was updated >>>\n\n"
        "Be surgical — only edit what changed. "
        "If nothing needs updating, respond with: NO_CHANGES_NEEDED"
    )
    if doc_name == "PROJECT.md":
        system_prompt += (
            "\n\nPROJECT.md contains registry-managed marker blocks such as "
            "Project Home, Source Roots, In This Project Directory, External Files, "
            "Registered Docs, and Recent Major Changes. Do not edit those marker "
            "blocks. They are refreshed deterministically before and after this LLM "
            "update. If only those sections need changes, respond with: "
            "NO_CHANGES_NEEDED\n\n"
            "For editable PROJECT.md sections that are truly empty, use OLD: (empty). "
            "Do not use OLD: (empty) when the section already contains text."
        )

    user_message = (
        f"## Current {doc_name}:\n\n{current_doc}\n\n"
        f"## Changes to incorporate:\n\n{context}"
    )

    if dry_run:
        logger.info("[docs-hook] [DRY RUN] Would update %s", doc_path)
        return True

    response, duration = call_deep_reasoning(
        prompt=user_message,
        system_prompt=system_prompt,
        max_tokens=4000,
        timeout=300.0,
    )

    if not response:
        logger.warning("[docs-hook] Empty LLM response for %s (%.1fs)", doc_name, duration)
        return False

    if "NO_CHANGES_NEEDED" in response:
        logger.info("[docs-hook] No changes needed for %s", doc_name)
        return False

    # Parse and apply edits using the shared edit block parser
    edits = re.findall(r'<<<EDIT\s*\n(.*?)>>>', response, re.DOTALL)
    if not edits:
        logger.info("[docs-hook] No valid edits parsed for %s", doc_name)
        return False

    edits = _filter_project_md_managed_edits(doc_name, current_doc, edits)
    if not edits:
        logger.info("[docs-hook] Only registry-managed PROJECT.md edits were proposed for %s", doc_name)
        return False

    from datastore.docsdb.updater import apply_edit_blocks
    pre_applied = 0
    if doc_name == "PROJECT.md":
        current_for_regular_edits, edits, pre_applied = _apply_project_md_empty_section_edits(
            current_doc,
            edits,
        )
    else:
        current_for_regular_edits = current_doc
    updated, applied, unmatched = apply_edit_blocks(current_for_regular_edits, edits)
    applied += pre_applied

    if unmatched:
        message = f"{unmatched} edit block(s) did not match {doc_name} content"
        for failed in _unmatched_edit_blocks(current_for_regular_edits, edits):
            logger.warning(
                "[docs-hook] Unmatched edit block for %s #%s SECTION=%r OLD=%r NEW=%r",
                doc_name,
                failed.get("index", "?"),
                _log_preview(failed.get("section")),
                _log_preview(failed.get("old")),
                _log_preview(failed.get("new")),
            )
        logger.warning("[docs-hook] %s", message)
        raise DocsEditBlockMismatch(message)

    if applied > 0 and updated != current_doc:
        doc_path.write_text(updated, encoding="utf-8")
        logger.info("[docs-hook] Updated %s: %d edit(s) (%.1fs)", doc_path, applied, duration)
        return True

    logger.info("[docs-hook] Edits didn't match %s content", doc_name)
    return False
