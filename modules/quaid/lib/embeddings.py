"""Shared embedding utilities for memory system.

Manages an EmbeddingsProvider singleton and exposes the same public API
(get_embedding, pack_embedding, unpack_embedding) so callers don't change.

Provider resolution order:
  1. MOCK_EMBEDDINGS=1 env            → MockEmbeddingsProvider
  2. Adapter provides embeddings       → adapter's provider
  3. Default                           → OllamaEmbeddingsProvider (from config)
"""

import os
import struct
from typing import List, Optional

from lib.providers import (
    EmbeddingsProvider,
    MockEmbeddingsProvider,
    OllamaEmbeddingsProvider,
)


# ── Provider singleton ────────────────────────────────────────────────

_provider: Optional[EmbeddingsProvider] = None


def get_embeddings_provider() -> EmbeddingsProvider:
    """Get the current embeddings provider (auto-resolved on first call)."""
    global _provider
    if _provider is not None:
        return _provider

    # 1. Test mode
    if os.environ.get("MOCK_EMBEDDINGS"):
        _provider = MockEmbeddingsProvider()
        return _provider

    # 2. Check if the adapter provides embeddings
    try:
        from lib.adapter import get_adapter
        adapter = get_adapter()
        adapter_embed = adapter.get_embeddings_provider()
        if adapter_embed is not None:
            _provider = adapter_embed
            return _provider
    except Exception:
        pass

    # 3. Default: standalone Ollama
    try:
        from .config import get_ollama_url, get_embedding_model, get_embedding_dim
        _provider = OllamaEmbeddingsProvider(
            url=get_ollama_url(),
            model=get_embedding_model(),
            dim=get_embedding_dim(),
        )
    except Exception:
        _provider = OllamaEmbeddingsProvider()  # defaults

    return _provider


def set_embeddings_provider(provider: EmbeddingsProvider) -> None:
    """Override the embeddings provider (for tests)."""
    global _provider
    _provider = provider


def reset_embeddings_provider() -> None:
    """Reset to auto-detection (for tests / adapter reset)."""
    global _provider
    _provider = None


# ── Public API (unchanged signatures) ────────────────────────────────

def get_embedding(text: str) -> Optional[List[float]]:
    """Get embedding for text using the current provider.

    Set MOCK_EMBEDDINGS=1 to use deterministic fakes for testing.
    """
    return get_embeddings_provider().embed(text)


def pack_embedding(embedding: List[float]) -> bytes:
    """Pack embedding as binary blob."""
    return struct.pack(f'{len(embedding)}f', *embedding)


def unpack_embedding(blob: bytes) -> List[float]:
    """Unpack embedding from binary blob."""
    count = len(blob) // 4  # 4 bytes per float
    return list(struct.unpack(f'{count}f', blob))
