"""Shared helpers for PROJECT.md scaffolds and managed sections."""

from __future__ import annotations

import re
from typing import Iterable, Sequence

PROJECT_HOME_BEGIN = "<!-- BEGIN:PROJECT_HOME -->"
PROJECT_HOME_END = "<!-- END:PROJECT_HOME -->"
SOURCE_ROOTS_BEGIN = "<!-- BEGIN:SOURCE_ROOTS -->"
SOURCE_ROOTS_END = "<!-- END:SOURCE_ROOTS -->"
IN_DIR_FILES_BEGIN = "<!-- BEGIN:IN_DIR_FILES -->"
IN_DIR_FILES_END = "<!-- END:IN_DIR_FILES -->"
EXTERNAL_FILES_BEGIN = "<!-- BEGIN:EXTERNAL_FILES -->"
EXTERNAL_FILES_END = "<!-- END:EXTERNAL_FILES -->"
REGISTERED_DOCS_BEGIN = "<!-- BEGIN:REGISTERED_DOCS -->"
REGISTERED_DOCS_END = "<!-- END:REGISTERED_DOCS -->"
PROJECT_LOG_BEGIN = "<!-- BEGIN:PROJECT_LOG -->"
PROJECT_LOG_END = "<!-- END:PROJECT_LOG -->"

REGISTRY_MANAGED_MARKERS = (
    PROJECT_HOME_BEGIN,
    PROJECT_HOME_END,
    SOURCE_ROOTS_BEGIN,
    SOURCE_ROOTS_END,
    IN_DIR_FILES_BEGIN,
    IN_DIR_FILES_END,
    EXTERNAL_FILES_BEGIN,
    EXTERNAL_FILES_END,
    REGISTERED_DOCS_BEGIN,
    REGISTERED_DOCS_END,
)


def _bullet_lines(items: Iterable[str], *, empty: str) -> str:
    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return empty
    return "\n".join(f"- `{item}`" for item in cleaned)


def replace_managed_block(content: str, begin: str, end: str, body: str) -> str:
    """Replace a marker-delimited managed block."""
    replacement = f"{begin}\n{body.rstrip()}\n{end}"
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), flags=re.DOTALL)
    if pattern.search(content):
        return pattern.sub(replacement, content, count=1)
    return content.rstrip() + "\n\n" + replacement + "\n"


def has_registry_managed_markers(content: str) -> bool:
    """Return True when a PROJECT.md already carries the managed scaffold."""
    return all(marker in content for marker in REGISTRY_MANAGED_MARKERS)


def render_project_md_template(
    *,
    label: str,
    description: str,
    project_home: str,
    source_roots: Sequence[str] | None = None,
    exclude_patterns: Sequence[str] | None = None,
) -> str:
    """Render the canonical PROJECT.md scaffold.

    The scaffold is intentionally cross-domain: code projects, writing projects,
    travel plans, and other non-code work all use the same overview/pointers
    structure. Auto-managed sections are wrapped in markers so registry-backed
    refresh can replace them reliably.
    """
    source_root_lines = _bullet_lines(
        source_roots or [],
        empty="- `(none configured yet)`",
    )
    exclude_lines = _bullet_lines(
        exclude_patterns or [],
        empty="- `(none configured)`",
    )

    return f"""# Project: {label}

## What This Is
{description}

## Current State
- Status: active
- Keep this section distilled and current. Rewrite it as the project changes instead of appending daily notes.
- Use it to summarize the current frontier, major blockers, and what matters now.

## Start Here
- Read `What This Is` and `Current State` first.
- Use `Project Home`, `Source Roots`, and `In This Project Directory` to find the real working artifacts.
- Open the registered docs below when you need deeper detail.
- Keep full chronology in `PROJECT.log`, not in this file.

## Primary Artifacts

### Project Home
{PROJECT_HOME_BEGIN}
- `{project_home}`
{PROJECT_HOME_END}

### Source Roots
{SOURCE_ROOTS_BEGIN}
{source_root_lines}
{SOURCE_ROOTS_END}

### In This Project Directory
{IN_DIR_FILES_BEGIN}
<!-- Auto-discovered — project-owned files inside the canonical project directory -->
(none yet)
{IN_DIR_FILES_END}

### External Files
{EXTERNAL_FILES_BEGIN}
| File | Purpose | Auto-Update |
|------|---------|-------------|
{EXTERNAL_FILES_END}

## How To Work On It
- Start with this file for the overview and navigation map.
- Open the primary artifacts above to inspect the real working material.
- Use the registered docs below when you need deeper detail.
- Full chronology belongs in `PROJECT.log`, not in this file.

## Key Constraints and Decisions

## Where To Learn More

### Registered Docs
{REGISTERED_DOCS_BEGIN}
| Document | Why Read It | Auto-Update |
|----------|-------------|-------------|
{REGISTERED_DOCS_END}

## Related Projects

## Recent Major Changes
{PROJECT_LOG_BEGIN}
{PROJECT_LOG_END}

## Update Rules
- Keep `What This Is`, `Current State`, `How To Work On It`, and `Key Constraints and Decisions` distilled and current.
- Use `Recent Major Changes` for a short recent frontier only. Full operational history lives in `PROJECT.log`.
- Registry-backed sections (`Project Home`, `Source Roots`, `In This Project Directory`, `External Files`, `Registered Docs`) should be refreshed from the current registry/config state.

## Exclude
{exclude_lines}
"""
