#!/usr/bin/env python3
"""
Mem0 adapter for the Quaid benchmark — optional third memory system.

Mem0 is a popular open-source memory layer that uses LLM extraction at
add-time and vector search at recall-time. This adapter makes it compatible
with the benchmark's pluggable search_fn interface.

Key differences from Quaid:
  - Mem0 distills memories from conversations via LLM (extraction at ingest)
  - Uses Qdrant (in-memory or on-disk) for vector storage
  - No graph traversal, no multi-pass retrieval, no reranking

Setup:
  pip install mem0ai

Config (in test.env or environment):
  MEM0_LLM_PROVIDER=anthropic       # or "ollama"
  MEM0_LLM_MODEL=claude-haiku-4-5   # or "llama3.2:latest"
  MEM0_EMBEDDER=ollama               # uses local Ollama embeddings
  MEM0_EMBED_MODEL=nomic-embed-text  # or qwen3-embedding:8b

Usage:
  from mem0_adapter import create_mem0, feed_mem0, make_mem0_search_fn

  m = create_mem0()
  feed_mem0(m, "Test User likes dark mode", user_id="benchmark")
  search_fn = make_mem0_search_fn(m)
  results = search_fn("What UI preferences?", limit=5)
"""
import os
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

_DIR = Path(__file__).resolve().parent

# Check if mem0 is available
try:
    from mem0 import Memory
    MEM0_AVAILABLE = True
except ImportError:
    MEM0_AVAILABLE = False


def is_available() -> bool:
    """Check if mem0 is installed and importable."""
    return MEM0_AVAILABLE


def create_mem0(
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    api_key: Optional[str] = None,
    embedder_provider: Optional[str] = None,
    embed_model: Optional[str] = None,
    ollama_url: Optional[str] = None,
) -> "Memory":
    """Create a configured Mem0 instance.

    Reads config from environment variables with explicit overrides.
    Default: Anthropic Haiku for LLM, Ollama for embeddings.
    """
    if not MEM0_AVAILABLE:
        raise ImportError(
            "mem0 is not installed. Run: pip install mem0ai"
        )

    # Resolve config from env with overrides
    _llm_provider = llm_provider or os.environ.get("MEM0_LLM_PROVIDER", "anthropic")
    _llm_model = llm_model or os.environ.get("MEM0_LLM_MODEL", "claude-haiku-4-5")
    _api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    _embedder = embedder_provider or os.environ.get("MEM0_EMBEDDER", "ollama")
    _embed_model = embed_model or os.environ.get("MEM0_EMBED_MODEL", "nomic-embed-text")
    _ollama_url = ollama_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")

    config: Dict = {
        "version": "v1.1",
    }

    # LLM config
    if _llm_provider == "anthropic":
        config["llm"] = {
            "provider": "anthropic",
            "config": {
                "model": _llm_model,
                "api_key": _api_key,
            },
        }
    elif _llm_provider == "ollama":
        config["llm"] = {
            "provider": "ollama",
            "config": {
                "model": _llm_model,
                "ollama_base_url": _ollama_url,
            },
        }

    # Embedder config
    if _embedder == "ollama":
        config["embedder"] = {
            "provider": "ollama",
            "config": {
                "model": _embed_model,
                "ollama_base_url": _ollama_url,
            },
        }

    # Use in-memory Qdrant (no server needed)
    config["vector_store"] = {
        "provider": "qdrant",
        "config": {
            "collection_name": "quaid_benchmark",
            "embedding_model_dims": 768,  # nomic-embed-text default
            "on_disk": False,  # in-memory for benchmark isolation
        },
    }

    return Memory.from_config(config)


def feed_mem0(
    memory: "Memory",
    fact_text: str,
    user_id: str = "benchmark",
) -> Dict:
    """Store a single fact in Mem0.

    Mem0 uses its own LLM to extract/distill memories from the input.
    This is a deliberate design choice — the benchmark tests Mem0's
    full pipeline (extraction + storage + retrieval).
    """
    messages = [{"role": "user", "content": fact_text}]
    result = memory.add(messages, user_id=user_id)
    return result


def feed_mem0_batch(
    memory: "Memory",
    facts: List[str],
    user_id: str = "benchmark",
) -> Dict:
    """Store multiple facts in Mem0.

    Returns summary dict with counts.
    """
    stored = 0
    errors = []
    for fact in facts:
        try:
            feed_mem0(memory, fact, user_id=user_id)
            stored += 1
        except Exception as e:
            errors.append({"fact": fact[:100], "error": str(e)})

    return {"stored": stored, "errors": errors, "total": len(facts)}


def make_mem0_search_fn(
    memory: "Memory",
    user_id: str = "benchmark",
) -> Callable:
    """Create a search_fn adapter for the benchmark's validate.py.

    Returns a function matching: (query: str, limit: int) -> List[Dict]
    where each dict has 'text' and 'similarity' keys.
    """

    def search_fn(query: str, limit: int) -> List[Dict]:
        try:
            result = memory.search(query=query, user_id=user_id, limit=limit)
        except Exception:
            return []

        # Mem0 v1.1 response format: {"results": [{"memory": "...", "score": 0.89, ...}]}
        results_list = result.get("results", []) if isinstance(result, dict) else result

        adapted = []
        for r in results_list:
            if isinstance(r, dict):
                adapted.append({
                    "text": r.get("memory", r.get("text", "")),
                    "similarity": r.get("score", 0.0),
                })

        return adapted[:limit]

    return search_fn


def get_mem0_stats(memory: "Memory", user_id: str = "benchmark") -> Dict:
    """Get memory count and basic stats from Mem0."""
    try:
        all_memories = memory.get_all(user_id=user_id)
        memories = all_memories.get("results", []) if isinstance(all_memories, dict) else all_memories
        return {
            "total_memories": len(memories),
            "sample": [m.get("memory", "")[:100] for m in memories[:5]] if memories else [],
        }
    except Exception as e:
        return {"error": str(e)}


def reset_mem0(memory: "Memory", user_id: str = "benchmark"):
    """Delete all memories for a user (clean slate for benchmark)."""
    try:
        memory.delete_all(user_id=user_id)
    except Exception:
        pass  # Best effort
