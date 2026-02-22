# Decision 002: Local Ollama Embeddings

**Date:** 2026-01-31  
**Status:** Accepted  
**Deciders:** Quaid, Alfie

## Context

Embedding generation is needed for semantic search in the memory system. Options were OpenAI embeddings API or local models via Ollama.

## Decision

**Use Ollama with `nomic-embed-text` model for all embeddings.**

## Rationale

1. **Cost** — Zero marginal cost per embedding. OpenAI charges per token.

2. **Speed** — After model warmup, embeddings generate in ~50ms locally vs 200-500ms API roundtrip.

3. **Privacy** — No data leaves the machine. Important for personal memory content.

4. **Availability** — Works offline. No rate limits. No API key management.

5. **Quality** — `nomic-embed-text` (137M params, 768-dim) is competitive with OpenAI ada-002 for our use case.

## Configuration

```bash
OLLAMA_KEEP_ALIVE=-1  # Models stay loaded permanently
OLLAMA_FLASH_ATTENTION=1  # Faster inference
OLLAMA_KV_CACHE_TYPE=q8_0  # Memory-efficient
```

Current VRAM usage: ~5.4GB (qwen2.5:7b + nomic-embed-text)

## Trade-offs Accepted

- Requires ~600MB VRAM for embedding model
- Slightly lower quality than OpenAI text-embedding-3-large (acceptable)
- Need to manage Ollama service (LaunchAgent handles this)

## Consequences

- Embedding generation is free and fast
- Can embed aggressively without cost concerns
- Auto-capture system can run on every message

## See Also

- [001-sqlite-over-cloud.md](./001-sqlite-over-cloud.md)
- TOOLS.md § Ollama
