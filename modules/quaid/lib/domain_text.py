"""Shared domain ID + description normalization helpers."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

_DOMAIN_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_DOMAIN_ALIASES = {
    "projects": "project",
    "family": "personal",
    "families": "personal",
}
_BLOCKED_DESC_PATTERNS = [
    re.compile(r"ignore\s+(all|any|previous|prior)\s+(instructions?|prompts?)", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now", re.IGNORECASE),
]
MAX_DOMAIN_DESCRIPTION_CHARS = 200


def normalize_domain_id(value: object) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    norm = re.sub(r"[^a-z0-9_]+", "_", raw)
    norm = re.sub(r"_{2,}", "_", norm).strip("_")
    norm = _DOMAIN_ALIASES.get(norm, norm)
    if not norm or not _DOMAIN_ID_RE.match(norm):
        return None
    return norm


def sanitize_domain_description(
    value: object,
    *,
    max_chars: int = MAX_DOMAIN_DESCRIPTION_CHARS,
    allow_truncate: bool = False,
) -> str:
    """Sanitize a domain description string.

    Args:
        value: Raw description (any type — coerced to str).
        max_chars: Maximum allowed length after normalization.
        allow_truncate: If True, silently trim to max_chars (for reading
            existing DB rows that may predate this limit). If False (default),
            raise ValueError when the normalized text exceeds max_chars.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = text.replace("`", "'")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = text.replace("<!--", "").replace("-->", "")
    text = re.sub(r"\s+", " ", text).strip()
    for pattern in _BLOCKED_DESC_PATTERNS:
        if pattern.search(text):
            raise ValueError("Domain description contains unsafe instruction-like content")
    if len(text) > max_chars:
        if allow_truncate:
            text = text[:max_chars].rstrip()
        else:
            raise ValueError(
                f"Domain description too long ({len(text)} chars, max {max_chars}). "
                "Shorten the description before registering."
            )
    return text
