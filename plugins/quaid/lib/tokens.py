"""Shared token extraction and text comparison utilities."""

import re
from typing import List

# Union of stopwords from janitor.py and memory_graph.py
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall", "it", "its",
    "this", "that", "these", "those", "he", "she", "they", "them", "his",
    "her", "their", "my", "your", "our", "i", "me", "we", "you", "us",
    "not", "no", "very", "just", "also", "about", "up", "so", "if",
    "than", "too", "when", "what", "which", "who", "how", "all", "each",
    "any", "some", "such", "more", "other", "into", "over", "after",
    "before", "between", "out", "through", "during", "without", "again",
    "like", "likes", "using", "uses", "used", "want", "wants", "wanted",
    "prefer", "prefers", "preferred", "really", "much", "many", "often",
    # From memory_graph.py search_fts stopwords
    "tell", "know", "get", "going", "need", "think", "said", "make", "take",
    "must",
})


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
    """Extract significant tokens from text, filtering stopwords and short words."""
    words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9_-]*\b', text.lower())
    seen = set()
    tokens = []
    for w in words:
        if w not in STOPWORDS and len(w) >= min_length and w not in seen:
            seen.add(w)
            tokens.append(w)
            if len(tokens) >= max_tokens:
                break
    return tokens


def texts_are_near_identical(a: str, b: str) -> bool:
    """Check if two texts are near-identical strings (not just similar embeddings).

    Catches two embedding blind spots:
    1. Different proper nouns in identical structure ("sister is Melina" vs
       "sister is Lori") -- caught by word-set comparison.
    2. Word order reversals that change meaning ("A gave B ring" vs "B gave A
       ring") -- caught by word-order comparison.

    Only returns True if both the word content AND word order are near-identical.
    """
    # Normalize: lowercase, strip punctuation, collapse whitespace
    norm = lambda t: re.sub(r'[^\w\s]', '', t.lower()).split()
    words_a = norm(a)
    words_b = norm(b)

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
    # "User gave Melina ring" vs "Melina gave User ring" -- proper nouns swapped.
    # Find words that appear in both but at different relative positions.
    if len(content_a) == len(content_b) and set(content_a) == set(content_b):
        # Same content words, different order -- check if proper nouns moved
        # (capitalized words in original text are likely entities)
        orig_words_a = re.sub(r'[^\w\s]', '', a).split()
        orig_words_b = re.sub(r'[^\w\s]', '', b).split()
        caps_a = [w.lower() for w in orig_words_a if w[0].isupper()]
        caps_b = [w.lower() for w in orig_words_b if w[0].isupper()]
        if caps_a != caps_b:
            # Proper nouns appear in different order -- likely different meaning
            return False

    return True
