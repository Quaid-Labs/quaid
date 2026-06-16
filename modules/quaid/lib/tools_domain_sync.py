"""Sync projects/quaid/TOOLS.md domain list block from runtime domains."""

from __future__ import annotations

import os
import logging
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict

from lib.domain_text import (
    normalize_domain_id,
    sanitize_domain_description,
)

logger = logging.getLogger(__name__)

START_MARKER = "<!-- AUTO-GENERATED:DOMAIN-LIST:START -->"
END_MARKER = "<!-- AUTO-GENERATED:DOMAIN-LIST:END -->"


def _workspace_root() -> Path:
    for env in ("QUAID_HOME", "OPENCLAW_WORKSPACE"):
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
        "Available domains (from datastore `domain_registry` active rows):"
    ]
    cleaned: Dict[str, str] = {}
    for key, desc in (domains or {}).items():
        domain_id = normalize_domain_id(key)
        if not domain_id:
            continue
        try:
            cleaned[domain_id] = sanitize_domain_description(desc)
        except ValueError as exc:
            logger.warning("Skipping domain %r with invalid description: %s", domain_id, exc)
            continue
    for key in sorted(cleaned.keys()):
        body_lines.append(f"- `{key}`: {cleaned[key]}")
    replacement = f"{START_MARKER}\n" + "\n".join(body_lines) + f"\n{END_MARKER}"

    pattern = re.compile(
        rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}",
        flags=re.DOTALL,
    )
    updated = pattern.sub(replacement, text, count=1)
    if updated == text:
        return False

    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(tools_path.parent)) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(updated)
        tmp_path.replace(tools_path)
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    return True
