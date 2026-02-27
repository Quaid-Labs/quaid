"""Sync projects/quaid/TOOLS.md domain list block from runtime domains."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict

START_MARKER = "<!-- AUTO-GENERATED:DOMAIN-LIST:START -->"
END_MARKER = "<!-- AUTO-GENERATED:DOMAIN-LIST:END -->"


def _workspace_root() -> Path:
    for env in ("QUAID_HOME", "CLAWDBOT_WORKSPACE"):
        value = os.getenv(env, "").strip()
        if value:
            return Path(value)
    return Path.cwd()


def sync_tools_domain_block(domains: Dict[str, str], workspace: Path | None = None) -> bool:
    """Rewrite the TOOLS.md domain list block from provided domains.

    Returns True when a file update is applied, else False.
    """
    root = workspace or _workspace_root()
    tools_path = root / "projects" / "quaid" / "TOOLS.md"
    if not tools_path.exists():
        return False

    text = tools_path.read_text(encoding="utf-8")
    if START_MARKER not in text or END_MARKER not in text:
        return False

    body_lines = [
        "Available domains (from `config/memory.json -> retrieval.domains`):"
    ]
    for key, desc in domains.items():
        body_lines.append(f"- `{key}`: {str(desc or '').strip()}")
    replacement = f"{START_MARKER}\n" + "\n".join(body_lines) + f"\n{END_MARKER}"

    pattern = re.compile(
        rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        flags=re.DOTALL,
    )
    updated = pattern.sub(replacement, text, count=1)
    if updated == text:
        return False

    tools_path.write_text(updated, encoding="utf-8")
    return True

