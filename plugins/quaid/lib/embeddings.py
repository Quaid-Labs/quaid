"""Shared embedding utilities for memory system."""

import hashlib
import json
import os
import struct
import sys
import urllib.request
import urllib.error
from typing import List, Optional


def _mock_embedding(text: str) -> List[float]:
    """Deterministic fake embedding for testing. Returns 128-dim normalized vector."""
    h = hashlib.md5(text.encode()).digest()
    raw = [float(b) / 255.0 for b in h] * 8  # 16 bytes * 8 = 128-dim
    magnitude = sum(x * x for x in raw) ** 0.5
    return [x / magnitude for x in raw] if magnitude > 0 else raw


def _ollama_url() -> str:
    """Get Ollama URL from config (lazy to avoid import cycle at module level)."""
    try:
        from .config import get_ollama_url
        return get_ollama_url()
    except Exception:
        return "http://localhost:11434"


def _embedding_model() -> str:
    """Get embedding model name from config."""
    from .config import get_embedding_model
    return get_embedding_model()


def get_embedding(text: str) -> Optional[List[float]]:
    """Get embedding from Ollama. Set MOCK_EMBEDDINGS=1 to use deterministic fakes for testing."""
    if os.environ.get("MOCK_EMBEDDINGS"):
        return _mock_embedding(text)
    try:
        model = _embedding_model()
        url = _ollama_url()
        data = json.dumps({
            "model": model,
            "input": text,
            "keep_alive": -1
        }).encode('utf-8')

        req = urllib.request.Request(
            f"{url}/api/embed",
            data=data,
            headers={'Content-Type': 'application/json'}
        )

        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode('utf-8'))
            embeddings = result.get("embeddings", [])
            if embeddings:
                return embeddings[0]
    except Exception as e:
        print(f"Embedding error: {e}", file=sys.stderr)
    return None


def pack_embedding(embedding: List[float]) -> bytes:
    """Pack embedding as binary blob."""
    return struct.pack(f'{len(embedding)}f', *embedding)


def unpack_embedding(blob: bytes) -> List[float]:
    """Unpack embedding from binary blob."""
    count = len(blob) // 4  # 4 bytes per float
    return list(struct.unpack(f'{count}f', blob))
