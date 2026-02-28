"""Shared domain ID + description normalization helpers."""

from __future__ import annotations

import re
from typing import Optional

_DOMAIN_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_BLOCKED_DESC_PATTERNS = [
    re.compile(r"ignore\s+(all|any|previous|prior)\s+(instructions?|prompts?)", re.IGNORECASE),
    re.compile(r"system\s+prompt", re.IGNORECASE),
    re.compile(r"you\s+are\s+now", re.IGNORECASE),
]
_MARKER_TOKENS = (
    "<!-- AUTO-GENERATED:DOMAIN-LIST:START -->",
    "<!-- AUTO-GENERATED:DOMAIN-LIST:END -->",
)
MAX_DOMAIN_DESCRIPTION_CHARS = 200


def normalize_domain_id(value: object) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    norm = re.sub(r"[^a-z0-9_]+", "_", raw)
    norm = re.sub(r"_{2,}", "_", norm).strip("_")
    if not norm or not _DOMAIN_ID_RE.match(norm):
        return None
    return norm


def sanitize_domain_description(value: object, *, max_chars: int = MAX_DOMAIN_DESCRIPTION_CHARS) -> str:
    text = str(value or "").strip()
    text = text.replace("`", "'")
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = text.replace("<!--", "").replace("-->", "")
    text = re.sub(r"\s+", " ", text).strip()
    for marker in _MARKER_TOKENS:
        text = text.replace(marker, "")
    for pattern in _BLOCKED_DESC_PATTERNS:
        if pattern.search(text):
            raise ValueError("Domain description contains unsafe instruction-like content")
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text
