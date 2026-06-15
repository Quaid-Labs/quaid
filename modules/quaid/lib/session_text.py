"""Session transcript parsing and microchunk helpers."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from lib.tokens import estimate_tokens

# Intentional SessionDB default: microchunks are cheap recall probes, not the
# older coarse transcript evidence chunks. Keep them small enough to inject as
# "possible match" context and expand by pair_id only when needed.
DEFAULT_MICROCHUNK_TOKENS = 40
_ROLE_RE = re.compile(r"^\s*(?:\[[^\]]+\]\s*)?([^\s:][^:\r\n]{0,80})\s*:\s*(.*)$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalize_role(role: str) -> Optional[str]:
    raw = _clean(role).casefold()
    if not raw:
        return None
    candidates = [raw]
    if "/" in raw:
        candidates.append(raw.rsplit("/", 1)[-1].strip())
    for candidate in candidates:
        text = re.sub(r"[\s_-]+", " ", candidate).strip()
        if text in {"user", "human", "operator"}:
            return "user"
        if text in {"assistant", "agent", "bot"}:
            return "assistant"
    return None


def _append_bounded(out: List[str], piece: str, max_tokens: int) -> None:
    text = _clean(piece)
    if not text:
        return
    if estimate_tokens(text) <= max_tokens:
        out.append(text)
        return

    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    if len(sentences) > 1:
        current: List[str] = []
        for sentence in sentences:
            candidate = " ".join([*current, sentence]).strip()
            if current and estimate_tokens(candidate) > max_tokens:
                _append_bounded(out, " ".join(current), max_tokens)
                current = [sentence]
            else:
                current.append(sentence)
        if current:
            _append_bounded(out, " ".join(current), max_tokens)
        return

    words = text.split()
    if len(words) > 1:
        current_words: List[str] = []
        for word in words:
            candidate = " ".join([*current_words, word]).strip()
            if current_words and estimate_tokens(candidate) > max_tokens:
                out.append(" ".join(current_words).strip())
                current_words = [word]
            else:
                current_words.append(word)
        if current_words:
            out.append(" ".join(current_words).strip())
        return

    current = ""
    for char in text:
        candidate = current + char
        if current and estimate_tokens(candidate) > max_tokens:
            out.append(current)
            current = char
        else:
            current = candidate
    if current:
        out.append(current)


def split_microchunks(text: str, max_tokens: int = DEFAULT_MICROCHUNK_TOKENS) -> List[str]:
    """Split text at newlines, then sentences, then token-sized fragments."""
    normalized = _clean(text)
    if not normalized:
        return []
    try:
        limit = int(max_tokens)
    except Exception:
        limit = DEFAULT_MICROCHUNK_TOKENS
    limit = max(8, min(limit, 256))

    out: List[str] = []
    current: List[str] = []
    for line in normalized.splitlines():
        line = line.strip()
        if not line:
            if current:
                _append_bounded(out, "\n".join(current), limit)
                current = []
            continue
        candidate = "\n".join([*current, line]).strip()
        if current and estimate_tokens(candidate) > limit:
            _append_bounded(out, "\n".join(current), limit)
            current = [line]
        else:
            current.append(line)
    if current:
        _append_bounded(out, "\n".join(current), limit)
    return out or [normalized]


def parse_message_pairs(transcript: str) -> List[Dict[str, str]]:
    """Parse role-prefixed transcript text into user/assistant pairs."""
    messages: List[Dict[str, str]] = []
    current_role: Optional[str] = None
    current_lines: List[str] = []

    def flush() -> None:
        nonlocal current_role, current_lines
        if current_role and current_lines:
            body = "\n".join(line.rstrip() for line in current_lines).strip()
            if body:
                messages.append({"role": current_role, "text": body})
        current_role = None
        current_lines = []

    for raw_line in str(transcript or "").splitlines():
        match = _ROLE_RE.match(raw_line)
        if match:
            role = _normalize_role(match.group(1))
            if role:
                flush()
                current_role = role
                current_lines = [match.group(2).strip()] if match.group(2).strip() else []
                continue
        if current_role:
            current_lines.append(raw_line)
    flush()

    pairs: List[Dict[str, str]] = []
    current_user = ""
    current_assistant: List[str] = []
    for message in messages:
        role = message["role"]
        text = message["text"]
        if role == "user":
            if current_user:
                pairs.append({"user_text": current_user, "assistant_text": "\n".join(current_assistant).strip()})
            current_user = text
            current_assistant = []
        elif role == "assistant" and current_user:
            current_assistant.append(text)
    if current_user:
        pairs.append({"user_text": current_user, "assistant_text": "\n".join(current_assistant).strip()})
    if not pairs and _clean(transcript):
        pairs.append({"user_text": _clean(transcript), "assistant_text": ""})
    return pairs
