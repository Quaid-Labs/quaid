# Decision 005: Qwen 7B for Memory Classification

**Date:** 2026-02-01
**Status:** Superseded (2026-02-09)
**Deciders:** Quaid, Alfie

> **Superseded:** Auto-capture via local LLM was deprecated Feb 2026. Memory extraction now happens at compaction/reset via Opus (Anthropic API). `extractor.py` deleted. HyDE query expansion moved from local Ollama to Haiku API. Ollama is now used for embeddings only (`qwen3-embedding:8b`).

## Context

Auto-capture needs to classify each message: "Does this contain facts worth remembering?" Initially used qwen2.5:3b, but it had accuracy issues.

## Decision

**Use qwen2.5:7b for memory classification, despite higher resource usage.**

## Rationale

### The Problem with 3B
- High false positive rate — captured greetings, questions, acknowledgments
- Poor entity extraction — missed relationship context
- Inconsistent JSON output — parsing failures

### Why 7B Works Better
- **Accuracy** — Dramatically fewer false positives
- **Entity extraction** — Better at identifying WHO said WHAT
- **Structured output** — Reliable JSON formatting
- **Context understanding** — Grasps conversation flow

### Resource Impact
| Model | VRAM | Inference Time |
|-------|------|----------------|
| qwen2.5:3b | ~2GB | ~200ms |
| qwen2.5:7b | ~4.8GB | ~400ms |

Total with embeddings: ~5.4GB VRAM (Mac mini has 24GB unified memory — plenty)

## Configuration

```bash
# Keep model loaded to avoid cold start
OLLAMA_KEEP_ALIVE=-1
```

Prompt optimized for:
- Strict filtering (high bar for "worth remembering")
- Spell correction in context
- Speaker attribution from channel metadata

## Trade-offs Accepted

- 2.4x more VRAM usage
- 2x slower inference per message
- Still fast enough for real-time (<500ms)

## Consequences

- Memory database stays high-quality
- Less janitor cleanup needed
- Worth the extra resources

## See Also

- `modules/quaid/ingest/extract.py`
- `projects/quaid/reference/memory-local-implementation.md`
