"""Shared similarity utilities for memory system."""

from typing import List

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Calculate cosine similarity between two vectors.

    Uses numpy when available (~50-100x faster for 4096-dim vectors).
    Falls back to pure Python if numpy is not installed.
    """
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        # Dimension mismatch — likely a corrupted embedding; return 0 instead of wrong answer
        return 0.0

    if _HAS_NUMPY:
        va = np.asarray(a, dtype=np.float32)
        vb = np.asarray(b, dtype=np.float32)
        norm_a = np.linalg.norm(va)
        norm_b = np.linalg.norm(vb)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(va, vb) / (norm_a * norm_b))

    # Pure Python fallback
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
