"""Shared embedding utilities for memory system.

Manages an EmbeddingsProvider singleton and exposes the same public API
(get_embedding, pack_embedding, unpack_embedding) so callers don't change.

Provider resolution order:
  1. MOCK_EMBEDDINGS=1 env            → MockEmbeddingsProvider
  2. Adapter provides embeddings       → adapter's provider
  3. Default                           → OllamaEmbeddingsProvider (from config)
"""

import os
import logging
import struct
import threading
from typing import List, Optional, Sequence, Any

from lib.fail_policy import is_fail_hard_enabled
from lib.runtime_context import notify_agent
from lib.worker_pool import run_callables
from lib.providers import (
    EmbeddingsProvider,
    MockEmbeddingsProvider,
    OllamaEmbeddingsProvider,
)


# ── Provider singleton ────────────────────────────────────────────────

_provider: Optional[EmbeddingsProvider] = None
_provider_lock = threading.Lock()
logger = logging.getLogger(__name__)


def get_embeddings_provider() -> EmbeddingsProvider:
    """Get the current embeddings provider (auto-resolved on first call)."""
    global _provider
    if _provider is not None:
        return _provider
    with _provider_lock:
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
        except Exception as exc:
            notify_agent(
                f"Quaid could not access the adapter embeddings provider: {exc}",
                severity="error" if is_fail_hard_enabled() else "warning",
                source="embeddings",
                dedupe_key=f"embeddings-adapter:{type(exc).__name__}",
                ttl_seconds=1800,
            )
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "Failed to resolve adapter embeddings provider while failHard is enabled."
                ) from exc
            logger.warning(
                "Adapter embeddings provider unavailable; falling back to standalone Ollama provider: %s",
                exc,
            )

        # 3. Default: standalone Ollama
        try:
            from .config import get_ollama_url, get_embedding_model, get_embedding_dim
            _provider = OllamaEmbeddingsProvider(
                url=get_ollama_url(),
                model=get_embedding_model(),
                dim=get_embedding_dim(),
            )
        except Exception as exc:
            notify_agent(
                f"Quaid could not initialize the configured embeddings backend: {exc}",
                severity="error" if is_fail_hard_enabled() else "warning",
                source="embeddings",
                dedupe_key=f"embeddings-config:{type(exc).__name__}",
                ttl_seconds=1800,
            )
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "Failed to build configured Ollama embeddings provider while failHard is enabled."
                ) from exc
            logger.warning(
                "Configured Ollama embedding settings unavailable; using default provider settings: %s",
                exc,
            )
            _provider = OllamaEmbeddingsProvider()  # defaults

        return _provider


def set_embeddings_provider(provider: EmbeddingsProvider) -> None:
    """Override the embeddings provider (for tests)."""
    global _provider
    with _provider_lock:
        _provider = provider


def reset_embeddings_provider() -> None:
    """Reset to auto-detection (for tests / adapter reset)."""
    global _provider
    with _provider_lock:
        _provider = None


# ── Public API (unchanged signatures) ────────────────────────────────

def get_embedding(text: str) -> Optional[List[float]]:
    """Get embedding for text using the current provider.

    Set MOCK_EMBEDDINGS=1 to use deterministic fakes for testing.
    """
    return get_embeddings_provider().embed(text)


def _embedding_parallel_workers(task_name: str = "embeddings", default: int = 4) -> int:
    """Resolve bounded worker count for embedding tasks."""
    try:
        from config import get_config

        cfg = get_config()
        parallel = getattr(getattr(cfg, "core", None), "parallel", None)
        if parallel is None or not getattr(parallel, "enabled", True):
            return 1
        workers = int(getattr(parallel, "embedding_workers", default) or default)
        task_workers = getattr(parallel, "task_workers", {}) or {}
        override = None
        if isinstance(task_workers, dict):
            for key in (task_name, task_name.upper(), task_name.lower()):
                if key in task_workers:
                    override = task_workers.get(key)
                    break
        raw = override if override is not None else workers
        return max(1, min(int(raw), 16))
    except Exception as exc:
        logger.warning("embedding worker config parse failed for task=%s: %s", task_name, exc)
        return max(1, int(default))


def get_embeddings(
    texts: Sequence[str],
    *,
    max_workers: Optional[int] = None,
    pool_name: str = "embeddings",
    task_name: str = "embeddings",
    return_exceptions: bool = False,
) -> List[Any]:
    """Get embeddings for many texts with a bounded worker pool.

    Results preserve input ordering. Duplicate texts are computed once and
    fanned back out to all matching positions.
    """
    items = list(texts or [])
    if not items:
        return []

    positions: dict[str, List[int]] = {}
    unique_items: List[str] = []
    for idx, text in enumerate(items):
        key = str(text or "")
        bucket = positions.get(key)
        if bucket is None:
            positions[key] = [idx]
            unique_items.append(key)
        else:
            bucket.append(idx)

    def _fan_out(unique_results: Sequence[Any]) -> List[Any]:
        out: List[Any] = [None] * len(items)
        for text, result in zip(unique_items, unique_results):
            for idx in positions.get(text, []):
                out[idx] = result
        return out

    provider = get_embeddings_provider()
    embed_many = getattr(provider, "embed_many", None)
    if callable(embed_many):
        try:
            out = list(embed_many(unique_items))
            if len(out) == len(unique_items):
                return _fan_out(out)
        except Exception:
            if not return_exceptions:
                raise
            return [None] * len(items)

    worker_count = (
        _embedding_parallel_workers(task_name)
        if max_workers is None
        else max(1, int(max_workers))
    )
    calls = [(lambda chunk_text=text: (lambda: provider.embed(chunk_text)))() for text in unique_items]
    unique_results = run_callables(
        calls,
        max_workers=min(worker_count, len(unique_items)),
        pool_name=pool_name,
        return_exceptions=return_exceptions,
    )
    return _fan_out(unique_results)


def pack_embedding(embedding: List[float]) -> bytes:
    """Pack embedding as binary blob."""
    return struct.pack(f'{len(embedding)}f', *embedding)


def unpack_embedding(blob: bytes) -> List[float]:
    """Unpack embedding from binary blob."""
    count = len(blob) // 4  # 4 bytes per float
    return list(struct.unpack(f'{count}f', blob))
