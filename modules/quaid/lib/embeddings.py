"""Shared embedding utilities for memory system.

Manages an EmbeddingsProvider singleton and exposes the same public API
(get_embedding, pack_embedding, unpack_embedding) so callers don't change.

Provider resolution order:
  1. MOCK_EMBEDDINGS=1 env            → MockEmbeddingsProvider
  2. models.embeddings_provider=ollama → OllamaEmbeddingsProvider (from config)
  3. Adapter/host provider requested   → adapter's provider
"""

import os
import logging
import struct
import threading
import inspect
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


def _configured_embeddings_provider_id() -> str:
    """Return the configured embeddings provider id without loading adapters."""
    try:
        from lib.config import get_embeddings_provider_id

        provider_id = get_embeddings_provider_id()
    except Exception as exc:
        if is_fail_hard_enabled():
            raise RuntimeError(
                "Failed to resolve configured embeddings provider while failHard is enabled."
            ) from exc
        logger.warning("Embedding provider config unavailable; defaulting to ollama: %s", exc)
        provider_id = "ollama"
    return str(provider_id or "ollama").strip().lower()


def _build_ollama_embeddings_provider() -> OllamaEmbeddingsProvider:
    from .config import get_ollama_url, get_embedding_model, get_embedding_dim

    return OllamaEmbeddingsProvider(
        url=get_ollama_url(),
        model=get_embedding_model(),
        dim=get_embedding_dim(),
    )


def _build_ollama_embeddings_provider_with_fallback(exc: Exception) -> OllamaEmbeddingsProvider:
    """Build configured Ollama provider, preserving failHard on config errors."""
    try:
        return _build_ollama_embeddings_provider()
    except Exception as ollama_exc:
        notify_agent(
            f"Quaid could not initialize the configured embeddings backend: {ollama_exc}",
            severity="error" if is_fail_hard_enabled() else "warning",
            source="embeddings",
            dedupe_key=f"embeddings-config:{type(ollama_exc).__name__}",
            ttl_seconds=1800,
        )
        if is_fail_hard_enabled():
            raise RuntimeError(
                "Failed to build configured Ollama embeddings provider while failHard is enabled."
            ) from ollama_exc
        logger.warning(
            "Configured Ollama embedding settings unavailable after adapter provider failure "
            "(%s); using default provider settings: %s",
            exc,
            ollama_exc,
        )
        return OllamaEmbeddingsProvider()


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

        provider_id = _configured_embeddings_provider_id()
        wants_ollama = provider_id in {"", "ollama", "local-ollama", "standalone-ollama"}

        # The default config is Ollama. Do not load the host adapter just to ask
        # whether it has an embeddings provider; live hooks run under tight
        # injection budgets and adapter/plugin discovery can exceed that budget.
        if wants_ollama:
            try:
                _provider = _build_ollama_embeddings_provider()
                return _provider
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

        # Adapter/host embeddings must be requested explicitly. If the adapter
        # cannot provide them under failHard, surface the misconfiguration instead
        # of silently falling back to a different provider.
        try:
            from lib.adapter import get_adapter

            adapter = get_adapter()
            adapter_embed = adapter.get_embeddings_provider()
            if adapter_embed is not None:
                _provider = adapter_embed
                return _provider
            raise RuntimeError(f"adapter did not provide embeddings provider '{provider_id}'")
        except Exception as exc:
            notify_agent(
                f"Quaid could not access the adapter embeddings provider: {exc}",
                severity="error" if is_fail_hard_enabled() else "warning",
                source="embeddings",
                dedupe_key=f"embeddings-adapter:{provider_id}:{type(exc).__name__}",
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
            _provider = _build_ollama_embeddings_provider_with_fallback(exc)

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

def _call_embed(provider: EmbeddingsProvider, text: str, timeout_s: Optional[float]) -> Optional[List[float]]:
    embed = provider.embed
    if timeout_s is None:
        return embed(text)
    try:
        params = inspect.signature(embed).parameters
        accepts_timeout = "timeout_s" in params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
        )
    except (TypeError, ValueError):
        accepts_timeout = True
    if accepts_timeout:
        return embed(text, timeout_s=timeout_s)
    return embed(text)


def get_embedding(text: str, *, timeout_s: Optional[float] = None) -> Optional[List[float]]:
    """Get embedding for text using the current provider.

    Set MOCK_EMBEDDINGS=1 to use deterministic fakes for testing.
    """
    return _call_embed(get_embeddings_provider(), text, timeout_s)


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
