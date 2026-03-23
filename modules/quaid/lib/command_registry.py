"""Command registry — structured routing entries for the tool hint planner.

Each entry describes a category of user intent and the actionable hint to
surface. Hint templates use {misc_path} and {instance}, resolved at call
time via resolve_command_registry().

Mirrors core/command-registry.ts. Eventually built programmatically from
datastore plugin contracts; for now a static list.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

COMMAND_REGISTRY: list[dict] = [
    {
        "id": "misc_project",
        "description": (
            "Throwaway, temp, quick, or hello-world files and scripts — "
            "anything the user explicitly wants to put somewhere temporary"
        ),
        "hint": "Throwaway file — write to: {misc_path}",
    },
    {
        "id": "create_project",
        "description": (
            "Durable work that should be tracked: essays, articles, reports, "
            "research notes, blog posts, video scripts, screenplays, outlines, "
            "travel plans, trip itineraries, project plans, or any multi-file "
            "long-lived work"
        ),
        "hint": "Durable work — create a project first: quaid registry create-project <name>",
    },
    {
        "id": "recall",
        "description": (
            "Searching or recalling memories, facts, preferences, relationships, "
            "project history, codebase details, architecture, tests, schemas, or "
            "anything the user wants to look up from stored knowledge."
        ),
        "hint": 'Search memories: quaid recall "<query>"',
    },
    {
        "id": "store",
        "description": (
            "Explicitly storing or saving a new fact, preference, decision, "
            "or memory for future recall"
        ),
        "hint": 'Store memory: quaid store "the fact"',
    },
]


def resolve_command_registry(
    entries: Optional[list[dict]] = None,
) -> list[dict]:
    """Return registry entries with {misc_path} and {instance} resolved.

    Resolves the misc project path from QUAID_HOME + QUAID_INSTANCE env vars,
    with a fallback scan of projects/misc--* if QUAID_INSTANCE is unset.
    """
    raw = entries if entries is not None else COMMAND_REGISTRY

    workspace = (
        os.environ.get("QUAID_HOME", "")
        or os.environ.get("CLAWDBOT_WORKSPACE", "")
    ).strip()
    instance = os.environ.get("QUAID_INSTANCE", "").strip()

    if not instance and workspace:
        projects_dir = Path(workspace) / "projects"
        try:
            found = next(
                (d.name for d in projects_dir.iterdir() if d.name.startswith("misc--")),
                None,
            )
            if found:
                instance = found.removeprefix("misc--")
        except Exception:
            pass

    misc_path = (
        str(Path(workspace) / "projects" / f"misc--{instance}")
        if workspace and instance
        else None
    )

    resolved = []
    for entry in raw:
        hint = entry["hint"]
        if misc_path:
            hint = hint.replace("{misc_path}", misc_path).replace("{instance}", instance)
        resolved.append({**entry, "hint": hint})
    return resolved
