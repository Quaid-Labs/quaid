"""Shared domain ID + description normalization helpers."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

_MAX_DOMAIN_ID_CHARS = 64
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
    if value is None:
        return None
    raw = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    if not raw:
        return None
    chars = [ch if _is_domain_id_char(ch) else "_" for ch in raw]
    norm = re.sub(r"_{2,}", "_", "".join(chars)).strip("_")
    norm = _DOMAIN_ALIASES.get(norm, norm)
    if (
        not norm
        or len(norm) > _MAX_DOMAIN_ID_CHARS
        or not any(ch.isalpha() for ch in norm)
        or not all(_is_domain_id_char(ch) for ch in norm)
    ):
        return None
    return norm


def _is_domain_id_char(ch: str) -> bool:
    """Return True for Unicode identifier body chars used in domain IDs."""
    return ch == "_" or ch.isalnum() or unicodedata.category(ch).startswith("M")


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
