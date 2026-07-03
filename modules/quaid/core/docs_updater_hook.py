"""Post-extraction docs updater hook.

After extraction, takes shadow git diffs and project logs, classifies
the change scope, and routes to the existing docs updater to update
project-level documentation (TOOLS.md, AGENTS.md, etc.).

Uses the classify → gate → update pipeline from datastore.docsdb.updater
to avoid unnecessary LLM calls for trivial changes.

See docs/PROJECT-SYSTEM-SPEC.md#extraction-event-integration.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from lib.fail_policy import is_fail_hard_enabled

logger = logging.getLogger(__name__)

# Root-level project docs that can be auto-updated from source diffs/logs.
# PROJECT.log remains append-only and must never be edited by this path.
_UPDATABLE_ROOT_DOCS = {"PROJECT.md", "TOOLS.md", "AGENTS.md"}
_FAST_GATE_MAX_TOKENS = 200


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
    for doc_name in _UPDATABLE_ROOT_DOCS:
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
        parts.append("## Code Diff")
        parts.append("```diff")
        parts.append(diff_text)
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

    current_doc = doc_path.read_text(encoding="utf-8")
    doc_name = doc_path.name

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
            "NO_CHANGES_NEEDED"
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
    import re
    edits = re.findall(r'<<<EDIT\s*\n(.*?)>>>', response, re.DOTALL)
    if not edits:
        logger.info("[docs-hook] No valid edits parsed for %s", doc_name)
        return False

    from datastore.docsdb.updater import apply_edit_blocks
    updated, applied, unmatched = apply_edit_blocks(current_doc, edits)

    if unmatched:
        message = f"{unmatched} edit block(s) did not match {doc_name} content"
        if is_fail_hard_enabled():
            raise RuntimeError(message)
        logger.warning("[docs-hook] %s", message)
        return False

    if applied > 0 and updated != current_doc:
        doc_path.write_text(updated, encoding="utf-8")
        logger.info("[docs-hook] Updated %s: %d edit(s) (%.1fs)", doc_path, applied, duration)
        return True

    logger.info("[docs-hook] Edits didn't match %s content", doc_name)
    return False
