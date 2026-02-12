
```
   ██████    ██    ██   ██████   ██  ██████
  ██    ██   ██    ██  ██    ██  ██  ██   ██
  ██    ██   ██    ██  ████████  ██  ██   ██
  ██ ▄▄ ██   ██    ██  ██    ██  ██  ██   ██
   ██████    ▀██████▀  ██    ██  ██  ██████
      ▀▀
```

### Persistent long-term memory for AI agents

---

**Mac / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/rekall-inc/quaid/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/rekall-inc/quaid/main/install.ps1 | iex
```

**Manual (all platforms):**
```bash
node setup-quaid.mjs
```

---

## What is Quaid?

Quaid is a memory plugin for [OpenClaw](https://github.com/openclaw/openclaw) that gives your AI agent persistent, long-term memory across conversations. Instead of losing everything when a session ends, Quaid extracts the important parts -- facts, relationships, preferences, personality insights -- and stores them locally in a SQLite graph database on your machine.

When your agent starts a new conversation, Quaid finds the right memories and injects them automatically. Your agent remembers who you are, what you're working on, and how you like things done -- without re-explaining every time.

This dramatically reduces token use compared to stuffing full conversation history into context. In benchmarks, Quaid achieves 87% of full-context performance while injecting only the relevant memories -- typically a few thousand tokens instead of the entire transcript.

---

## Benchmark Results

Evaluated on the [LoCoMo benchmark](https://github.com/snap-research/locomo) -- 10 long conversations with 1,540 scored question-answer pairs testing memory extraction, temporal reasoning, and multi-hop recall.

| System | Accuracy |
|--------|----------|
| **Quaid** | **75.0%** |
| Mem0 (guided) | 68.9% |
| Mem0 | 66.9% |
| Zep | 66.0% |
| LangMem | 58.1% |
| OpenAI Memory | 52.9% |

> Quaid result uses recommended settings (Opus reasoning + Ollama embeddings).
> Mem0, Zep, LangMem, and OpenAI numbers are from their [April 2025 paper](https://arxiv.org/abs/2504.01094).
> Full-context baselines: Haiku 79.6%, Opus 86.6%.
> LoCoMo tests fact extraction and recall. It does not evaluate project intelligence, codebase awareness, or documentation tracking, which are additional Quaid capabilities.
> Full methodology and per-category breakdowns: [docs/BENCHMARKS.md](docs/BENCHMARKS.md)

---

## Features

- **Automatic fact extraction** -- Facts and relationships pulled from conversations at compaction, no manual tagging
- **Semantic + keyword search** -- Hybrid retrieval combining vector embeddings, full-text search, and graph traversal
- **Nightly maintenance** -- Janitor pipeline deduplicates, resolves contradictions, and decays stale memories
- **Personality evolution** -- Journal and snippet systems let your agent develop and refine its understanding over time
- **Project tracking** -- Monitors your codebase docs and keeps them up to date from git changes
- **Multi-provider** -- Anthropic recommended; any OpenAI-compatible API also supported
- **Local embeddings** -- Ollama runs on your machine for embedding generation. Fits on a 16GB Mac mini alongside the rest of the system.

---

## How It Works

Quaid is built around four systems that work together:

**Memory** -- The core fact store. Conversations are distilled into structured facts, relationships, and preferences stored in a SQLite graph database. Retrieval uses hybrid search (vector + keyword + graph traversal), LLM reranking, and intent-aware fusion to find the right memories at the right time.

**Journal & Personality** -- A dual learning system that develops your agent's understanding of the user, itself, and the world around it. Fast-path *snippets* capture small observations and fold them into core personality files. Slow-path *journal entries* accumulate over time and get distilled into deeper insights about patterns and preferences.

**Projects & Docs** -- Tracks your codebase documentation, auto-discovers project structure, and keeps docs up to date from git changes. Provides RAG search so your agent has context about what you're building, not just who you are.

**Workspace Maintenance** -- A nightly janitor pipeline that reviews pending facts, merges duplicates, resolves contradictions, decays stale memories, and monitors documentation health. Keeps everything clean without manual work.

### Three Memory Areas

These four systems create three distinct memory areas, each with different retrieval behavior:

- **Fact memory** (Memory system) -- User facts, relationships, preferences. Retrieved via hybrid search with LLM reranking -- only the most relevant facts are injected per query.
- **Core personality** (Journal & Personality) -- Deeper understanding of the user, the agent's own identity, and the world around it. Loaded as full context on every conversation -- always available, always current.
- **Project knowledge** (Projects & Docs) -- Codebase docs, project structure, tools. Available via RAG search -- full documents loaded when relevant.

### Extraction & Recall

At compaction or session reset, a high-reasoning LLM extracts structured facts, relationship edges, personality snippets, and journal entries from the conversation. At recall time, Quaid classifies the query intent, generates a hypothetical answer for better vector matching (HyDE), searches across multiple channels, and uses an LLM reranker to surface the most relevant memories.

### LLM-First Methodology

Almost every decision in Quaid is algorithm-assisted but ultimately arbitrated by an LLM appropriate for the task. The system splits work between a **high-reasoning LLM** (fact review, contradiction resolution, journal distillation) and a **low-reasoning LLM** (reranking, dedup verification, query expansion) to balance quality against cost.

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
# One-line install (Mac / Linux)
curl -fsSL https://raw.githubusercontent.com/rekall-inc/quaid/main/install.sh | bash
```

The installer walks you through setup: identity, model selection, embedding configuration, and system toggles. Takes about two minutes.

After install:

```bash
quaid doctor    # Verify everything is working
quaid stats     # See your memory database
quaid config    # Change settings anytime
```

Quaid conserves context across restarts -- when your agent compacts or resets, memories are extracted before the context is cleared. A full crash (kill -9, power loss) before compaction can cause memory loss for that session.

---

## Early Alpha

Quaid is in early alpha. The recommended configuration is **Anthropic** (Claude) for reasoning + **Ollama** for local embeddings. Other providers (OpenAI-compatible APIs, OpenRouter, Together) are supported but less thoroughly tested.

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

MIT -- see [LICENSE](LICENSE).
