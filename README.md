
```
   ██████    ██    ██   ██████   ██  ██████
  ██    ██   ██    ██  ██    ██  ██  ██   ██
  ██    ██   ██    ██  ████████  ██  ██   ██
  ██ ▄▄ ██   ██    ██  ██    ██  ██  ██   ██
   ██████    ▀██████▀  ██    ██  ██  ██████
      ▀▀
```

### Persistent long-term memory for [OpenClaw](https://github.com/openclaw/openclaw) agents

Quaid is a memory plugin built for the OpenClaw AI agent framework. It could be adapted to other platforms, but OpenClaw is the native environment and the only one we test against.

---

## How to Install

**Mac / Linux:**
```bash
git clone https://github.com/steadman-labs/quaid.git
cd quaid
node setup-quaid.mjs
```

**Windows (experimental):**
```powershell
git clone https://github.com/steadman-labs/quaid.git
cd quaid
node setup-quaid.mjs
```

The guided installer walks you through setup in about two minutes.

---

## What is Quaid?

OpenClaw ships with a built-in memory system that stores conversation history as chunked logs. It works, but it's basic -- keyword search over raw transcripts, no understanding of what's important, no maintenance over time. As conversations accumulate, recall quality degrades and token costs grow. The same problem hits markdown files -- SOUL.md, USER.md, and other core files bloat over time with redundant or outdated content, eating context window on every conversation.

Quaid replaces that with structured, LLM-curated memory. Instead of logging everything, it extracts the important parts -- facts, relationships, preferences, personality insights -- and stores them in a local SQLite graph database. A nightly janitor reviews, deduplicates, resolves contradictions, and decays stale memories so the graph stays clean. It also monitors your core markdown files for bloat and staleness, keeping them slim and laser-focused so you're not wasting tokens on dead weight every session.

When your agent starts a new conversation, Quaid finds the right memories via hybrid search (vector + keyword + graph traversal) and injects only what's relevant. In benchmarks, Quaid achieves 94% of full-context performance while injecting ~200 tokens of retrieved facts per query -- not thousands of tokens of raw conversation history.

---

## Benchmark Results

Evaluated on the [LoCoMo benchmark](https://github.com/snap-research/locomo) (ACL 2024) using Mem0's exact evaluation methodology -- same judge model (GPT-4o-mini), same prompt, same scoring. 10 long conversations, 1,540 scored question-answer pairs testing memory extraction, temporal reasoning, and multi-hop recall.

| System | Accuracy | Notes |
|--------|----------|-------|
| **Quaid** | **75.0%** | ~200 tokens of memory injected per query |
| Mem0 (guided) | 68.9% | |
| Mem0 | 66.9% | |
| Zep | 66.0% | |
| LangMem | 58.1% | |
| OpenAI Memory | 52.9% | |

**Token efficiency:** Quaid retrieves ~10 relevant facts per query, averaging **~200 tokens** of injected memory context. That's it -- no raw transcript chunks, no bloated session logs. Embeddings are fully local (Ollama), so vector search has zero API cost. The only per-query API spend is a low-reasoning LLM reranker call (~$0.01).

> Quaid result uses recommended settings (high-reasoning LLM + Ollama embeddings).
> Mem0, Zep, LangMem, and OpenAI numbers are from their [April 2025 paper](https://arxiv.org/abs/2504.01094).
> Full-context baselines: low-reasoning LLM 79.6%, high-reasoning LLM 86.6%.
>
> **Understanding the LLM split:** In this benchmark, the "high-reasoning" vs "low-reasoning" result reflects which LLM synthesizes the answer from retrieved facts. In production, there's no separate answer step -- Quaid passes retrieved memories directly to whatever LLM the agent is already using. If your agent runs on a high-reasoning model (as most OpenClaw setups do), the 75.0% result is the relevant one. The low-reasoning result (70.3%) shows the lower bound when the answering model is weaker.
>
> Full methodology and per-category breakdowns: [docs/BENCHMARKS.md](docs/BENCHMARKS.md)

---

## Three Memory Areas

Quaid organizes knowledge into three distinct areas, each with different retrieval behavior:

**Fact memory** -- User facts, relationships, preferences, experiences. Retrieved via hybrid search with LLM reranking -- only the most relevant facts are injected per query. This is your agent's long-term factual recall.

**Core personality** -- Deeper understanding of the user, the agent's own identity, and the world around it. Loaded as full context on every conversation -- always available, always current. This is where your agent's personality, values, and operational knowledge live.

**Project knowledge** -- Documentation, project structure, tool APIs. Available via RAG search -- full documents loaded when relevant. This keeps detailed system knowledge out of core context (where it would waste tokens every turn) while giving the LLM access to the complete reference when it needs it. Projects aren't just code -- this covers any sustained effort: a codebase, an essay, a YouTube channel, a home renovation.

---

## How It Works

Quaid is built around four systems:

**Memory** -- The core fact store. Conversations are distilled into structured facts, relationships, and preferences stored in a SQLite graph database. Retrieval uses hybrid search (vector + keyword + graph traversal), LLM reranking, and intent-aware fusion to find the right memories at the right time.

**Journal & Personality** -- A dual learning system. Fast-path *snippets* capture small observations and fold them into core personality files. Slow-path *journal entries* accumulate over time and get distilled into deeper insights. The journal fills the gap between explicit raw facts and the kind of perceived, inferred understanding that makes an agent feel like it actually knows you.

**Projects & Docs** -- Tracks project documentation, auto-discovers project structure, and keeps docs current from git changes. Systems documentation needs to be comprehensive -- partial or outdated docs are worse than no docs, because they mislead the LLM. The doc refresh system ensures your agent always has a complete, current picture of every project. This also keeps system-level knowledge out of the memory graph, where it would pollute fact retrieval with implementation details.

**Workspace Maintenance** -- A nightly janitor pipeline that batches the day's work into a window where high-reasoning LLMs can curate memories economically. Instead of spending premium LLM calls on every fact as it arrives, the janitor reviews, deduplicates, resolves contradictions, decays stale memories, and monitors documentation health in bulk.

---

## Features

- **Automatic fact extraction** -- Facts and relationships pulled from conversations at compaction, no manual tagging
- **Semantic + keyword search** -- Hybrid retrieval combining vector embeddings, full-text search, and graph traversal
- **Nightly maintenance** -- Janitor pipeline deduplicates, resolves contradictions, and decays stale memories using high-reasoning LLMs
- **Personality evolution** -- Journal and snippet systems let your agent develop and refine its understanding over time
- **Project tracking** -- Curates and refreshes documentation for AI ingestion, helping your agent remember all parts of a project -- not just the files it recently touched
- **Multi-provider** -- Anthropic recommended. OpenAI-compatible APIs supported but experimental and not fully tested.
- **Local embeddings** -- Ollama runs on your machine for embedding generation. Fits on a 16GB Mac mini alongside the rest of the system.

---

## LLM-First Methodology

Almost every decision in Quaid is algorithm-assisted but ultimately arbitrated by an LLM appropriate for the task. The system splits work between a **high-reasoning LLM** (fact review, contradiction resolution, journal distillation) and a **low-reasoning LLM** (reranking, dedup verification, query expansion) to balance quality against cost and speed. The low-reasoning model isn't just cheaper -- it's fast. Memory recall needs to feel instant, not take three seconds waiting on a premium model to rerank results.

Because the system leans heavily on LLM reasoning, Quaid naturally scales with AI models -- as reasoning capabilities improve, every decision in the pipeline gets better without code changes.

---

## Cost

| Component | Cost |
|-----------|------|
| Fact extraction | ~$0.05--0.20 per compaction (high-reasoning LLM) |
| Memory recall | ~$0.01 per query (low-reasoning LLM reranker) |
| Nightly janitor | ~$1--5 per run |
| Embeddings | Free (Ollama, runs locally) |
| **Typical monthly total** | **~$5--15 for active use** |

---

## Requirements

- [OpenClaw](https://github.com/openclaw/openclaw) gateway
- Python 3.10+
- SQLite 3.35+
- [Ollama](https://ollama.ai) (for local embeddings)
- An LLM API key (Anthropic recommended)

---

## Quick Start

```bash
git clone https://github.com/steadman-labs/quaid.git
cd quaid
node setup-quaid.mjs
```

The guided installer walks you through setup: identity, model selection, embedding configuration, and system toggles. Takes about two minutes.

After install:

```bash
quaid doctor    # Verify everything is working
quaid stats     # See your memory database
quaid config    # Change settings anytime
```

Quaid conserves context across restarts -- when your agent compacts or resets, memories are extracted before the context is cleared. A full crash (kill -9, power loss) before compaction can cause memory loss for that session.

---

## Early Alpha

Quaid is in early alpha. The recommended configuration is **Anthropic** (Claude) for reasoning + **Ollama** for local embeddings. Other providers (OpenAI-compatible APIs, OpenRouter, Together) are supported but experimental and not fully tested.

The system is backed by over 1,000 unit tests (Python + TypeScript), 15 automated installer scenarios covering fresh installs, dirty upgrades, data preservation, migration, missing dependencies, and provider combinations, plus benchmark evaluation against [LoCoMo](docs/BENCHMARKS.md) and [LongMemEval](https://github.com/xiaowu0162/LongMemEval).

We're actively testing and refining the system against benchmarks and welcome collaboration. If you're interested in contributing, testing, or just have ideas -- open an issue or reach out.

---

## Learn More

- [Architecture Guide](docs/ARCHITECTURE.md) -- How Quaid works under the hood
- [AI Agent Reference](docs/AI-REFERENCE.md) -- Complete system index for AI assistants
- [Benchmark Results](docs/BENCHMARKS.md) -- Full LoCoMo evaluation with per-category breakdowns
- [Roadmap](ROADMAP.md) -- What's coming next

---

## Author

**Solomon Steadman** -- [@steadman](https://x.com/steadman) | [github.com/solstead](https://github.com/solstead)

## License

Apache 2.0 -- see [LICENSE](LICENSE).
