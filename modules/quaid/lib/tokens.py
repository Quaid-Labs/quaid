"""Shared token extraction and text comparison utilities."""

import re
import unicodedata
from typing import List

# Kept as a compatibility export for older callers. Shared memory tokenization
# must not apply language-specific stopword policy.
STOPWORDS = frozenset()


def _has_compact_script_char(text: str) -> bool:
    for ch in str(text or ""):
        name = unicodedata.name(ch, "")
        if name.startswith(("CJK ", "HIRAGANA", "KATAKANA", "HANGUL")):
            return True
    return False


def _normalized_alnum_key(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def _normalized_words(text: str) -> List[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[^\w\s]", "", normalized, flags=re.UNICODE).split()


def estimate_tokens(text: str) -> int:
    """Estimate token count for a text string.

    Uses ~4 chars per token for ASCII and ~1.5 chars per token for CJK/emoji.
    Conservative enough for budget calculations.
    """
    if not text:
        return 1
    # Count CJK/emoji characters separately (they tokenize ~1-2 chars per token)
    cjk_count = sum(1 for c in text if ord(c) > 0x2E80)
    ascii_count = len(text) - cjk_count
    return max(1, ascii_count // 4 + (cjk_count * 2) // 3)


def extract_key_tokens(text: str, min_length: int = 3, max_tokens: int = 8) -> List[str]:
    """Extract structurally significant tokens from text."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    words = re.findall(r"[^\W\d_][\w_-]*", normalized, flags=re.UNICODE)
    seen = set()
    tokens = []
    for w in words:
        token_min_length = min(min_length, 2) if not w.isascii() else min_length
        if len(w) >= token_min_length and w not in seen:
            seen.add(w)
            tokens.append(w)
            if len(tokens) >= max_tokens:
                break
    return tokens


def is_subset_overlap_candidate(text_a: str, text_b: str) -> bool:
    """Heuristic: one text is a likely strict subset of the other.

    Intended for candidate expansion only; merge decisions should remain
    guarded by higher-level checks (for example LLM verification).
    """
    a = str(text_a or "").strip()
    b = str(text_b or "").strip()
    if not a or not b or a == b:
        return False
    if len(a.split()) < 3 or len(b.split()) < 3:
        return False

    tokens_a = set(extract_key_tokens(a, max_tokens=24))
    tokens_b = set(extract_key_tokens(b, max_tokens=24))
    if not tokens_a or not tokens_b:
        return False

    if len(tokens_a) <= len(tokens_b):
        smaller, larger = tokens_a, tokens_b
    else:
        smaller, larger = tokens_b, tokens_a
    if len(smaller) >= len(larger):
        return False

    overlap = len(smaller & larger)
    if overlap < 3:
        return False
    coverage = overlap / max(len(smaller), 1)
    return coverage >= 0.8


def texts_are_near_identical(a: str, b: str) -> bool:
    """Check if two texts are near-identical strings (not just similar embeddings).

    Catches two embedding blind spots:
    1. Different proper nouns in identical structure ("sister is Beth" vs
       "sister is Jane") -- caught by word-set comparison.
    2. Word order reversals that change meaning ("A gave B ring" vs "B gave A
       ring") -- caught by word-order comparison.

    Only returns True if both the word content AND word order are near-identical.
    """
    if _has_compact_script_char(a) or _has_compact_script_char(b):
        key_a = _normalized_alnum_key(a)
        key_b = _normalized_alnum_key(b)
        if key_a and key_a == key_b:
            return True

    # Normalize: lowercase, strip punctuation, collapse whitespace
    words_a = _normalized_words(a)
    words_b = _normalized_words(b)

    # Check 1: word sets must match (catches different proper nouns)
    set_a = set(words_a)
    set_b = set(words_b)
    trivial = {'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be',
               'to', 'of', 'in', 'for', 'and', 'or', 'that', 'this'}
    meaningful_diff = set_a.symmetric_difference(set_b) - trivial
    if meaningful_diff:
        return False

    # Check 2: word order must be similar (catches subject/object swaps)
    # Extract non-trivial words in order for comparison
    content_a = [w for w in words_a if w not in trivial]
    content_b = [w for w in words_b if w not in trivial]
    if not content_a and not content_b and (words_a or words_b):
        return False  # Both texts are all stopwords — can't determine similarity
    if content_a == content_b:
        return True

    # If content words are reordered, check if the entity positions changed.
    # "Alice gave Bob ring" vs "Bob gave Alice ring" -- proper nouns swapped.
    # Find words that appear in both but at different relative positions.
    if len(content_a) == len(content_b) and set(content_a) == set(content_b):
        # Same content words, different order -- check if proper nouns moved
        # (capitalized words in original text are likely entities)
        orig_words_a = re.sub(r'[^\w\s]', '', unicodedata.normalize("NFKC", str(a or "")), flags=re.UNICODE).split()
        orig_words_b = re.sub(r'[^\w\s]', '', unicodedata.normalize("NFKC", str(b or "")), flags=re.UNICODE).split()
        caps_a = [w.lower() for w in orig_words_a if w[0].isupper()]
        caps_b = [w.lower() for w in orig_words_b if w[0].isupper()]
        if caps_a != caps_b:
            # Proper nouns appear in different order -- likely different meaning
            return False

    return True
