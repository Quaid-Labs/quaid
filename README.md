
```
   ██████    ██    ██   ██████   ██  ██████
  ██    ██   ██    ██  ██    ██  ██  ██   ██
  ██    ██   ██    ██  ████████  ██  ██   ██
  ██ ▄▄ ██   ██    ██  ██    ██  ██  ██   ██
   ██████    ▀██████▀  ██    ██  ██  ██████
      ▀▀
```

### Memory and project management plugin for [OpenClaw](https://github.com/openclaw/openclaw)

> **Early alpha** — launched February 2026, active daily development.

---

## Install

Paste this into a terminal. The guided installer walks you through setup in about two minutes.

**Mac / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/steadman-labs/quaid/main/install.sh | bash
```

**Windows (experimental):**
```powershell
irm https://raw.githubusercontent.com/steadman-labs/quaid/main/install.ps1 | iex
```

**Manual (all platforms):**
```bash
git clone https://github.com/steadman-labs/quaid.git
cd quaid && node setup-quaid.mjs
```

After install:

```bash
quaid doctor    # Verify everything is working
quaid stats     # See your memory database
quaid config    # View current settings
```

Quaid conserves context across restarts — when your agent compacts or resets, memories are extracted before the context is cleared. A full crash (kill -9, power loss) before compaction can cause memory loss for that session.

### Uninstall

```bash
quaid uninstall
```

Lists available backups, offers to restore your core markdown to its pre-install state, and removes the plugin. Your memory database is preserved by default.

---

## What is Quaid?

Quaid extracts the important parts of conversations — facts, relationships, preferences, personality insights — and stores them in a local SQLite graph database. A nightly janitor reviews, deduplicates, resolves contradictions, and decays stale memories so the graph stays clean. It also monitors your core markdown files for bloat and staleness, and tracks project documentation so your agent has a complete, current picture of everything it works on. In benchmarks, Quaid achieves 88% of full-context performance while injecting about 200 tokens of retrieved facts per query.

---

## Benchmark Results

Evaluated on the [LoCoMo benchmark](https://github.com/snap-research/locomo) (ACL 2024) using Mem0's exact evaluation methodology — same judge model (GPT-4o-mini), same prompt, same scoring. 10 long conversations, 1,540 scored question-answer pairs testing memory extraction, temporal reasoning, and multi-hop recall.

| System | Accuracy | Answer Model |
|--------|----------|-------------|
| **Quaid** | **70.3%** | Haiku |
| Mem0 (graphRAG) | 68.9% | GPT-4o-mini |
| Mem0 | 66.9% | GPT-4o-mini |
| Zep | 66.0% | GPT-4o-mini |
| LangMem | 58.1% | GPT-4o-mini |
| OpenAI Memory | 52.9% | GPT-4o-mini |

With Opus answering (recommended production config): **75.0%**

**Token efficiency:** Quaid retrieves about 10 relevant facts per query, averaging **about 200 tokens** of injected memory context. That's it. No raw transcript chunks, no bloated session logs. Embeddings are fully local (Ollama), so vector search has zero API cost. The only per-query API spend is a low-reasoning LLM reranker call (about $0.01).

> Mem0, Zep, LangMem, and OpenAI numbers are from their [April 2025 paper](https://arxiv.org/abs/2504.01094).
> Full-context baselines: Haiku 79.6%, Opus 86.6%.
>
> Full methodology and per-category breakdowns: [docs/BENCHMARKS.md](docs/BENCHMARKS.md)

LoCoMo evaluates personal fact recall — one of Quaid's three memory areas. The benchmark doesn't measure project documentation tracking, auto-doc refresh, or workspace context management, which have no equivalent in the other systems tested.

---

## How It Works

Quaid organizes knowledge into three areas, each with different retrieval behavior, and maintains them with four systems.

### Three memory areas

**Fact memory** — User facts, relationships, preferences, experiences. Retrieved via hybrid search (vector + keyword + graph traversal) with LLM reranking — only the most relevant facts are injected per query.

**Core personality** — Deeper understanding of the user, the agent's own identity, and the world around it. Loaded as full context on every conversation — always available, always current.

**Project knowledge** — Documentation, project structure, tool APIs. Available via RAG search — full documents loaded when relevant. Projects aren't just code — this covers any sustained effort: a codebase, an essay, a YouTube channel, a home renovation.

### Four systems

**Memory** — Conversations are distilled into structured facts, relationships, and preferences stored in a SQLite graph database. Retrieval uses hybrid search, LLM reranking, and intent-aware fusion to find the right memories at the right time.

**Journal & Personality** — A dual learning system. Fast-path *snippets* capture small observations and fold them into core personality files. Slow-path *journal entries* accumulate over time and get distilled into deeper insights — the kind of perceived, inferred understanding that makes an agent feel like it actually knows you.

**Projects & Docs** — Auto-discovers project structure, tracks documentation, and keeps docs current from git changes. Comprehensive docs beat partial docs — partial or outdated docs mislead the LLM. This also keeps system-level knowledge out of the memory graph, where it would pollute fact retrieval.

**Workspace Maintenance** — A nightly janitor pipeline that batches the day's work into a window where high-reasoning LLMs can curate memories economically. Reviews, deduplicates, resolves contradictions, decays stale memories, and monitors documentation health in bulk.

---

<details>
<summary><strong>Design Philosophy: LLM-First</strong></summary>

Almost every decision in Quaid is algorithm-assisted but ultimately arbitrated by an LLM appropriate for the task. The system splits work between a **high-reasoning LLM** (fact review, contradiction resolution, journal distillation) and a **low-reasoning LLM** (reranking, dedup verification, query expansion) to balance quality against cost and speed. The low-reasoning model isn't just cheaper — it's fast. Memory recall needs to feel instant, not take three seconds waiting on a premium model to rerank results.

Because the system leans heavily on LLM reasoning, Quaid naturally scales with AI models — as reasoning capabilities improve, every decision in the pipeline gets better without code changes.

</details>

---

## Cost

| Component | Cost |
|-----------|------|
| Fact extraction | $0.05–0.20 per compaction (high-reasoning LLM) |
| Memory recall | $0.01 per query (low-reasoning LLM reranker) |
| Nightly janitor | $1–5 per run |
| Embeddings | Free (Ollama, runs locally) |
| **Typical monthly total** | **$5–15 for active use** |

---

## Requirements

- [OpenClaw](https://github.com/openclaw/openclaw) gateway
- Python 3.10+
- SQLite 3.35+
- [Ollama](https://ollama.ai) (for local embeddings)
- An LLM API key (Anthropic recommended)

---

## Early Alpha

Quaid is in early alpha. The recommended configuration is **Anthropic** (Claude) for reasoning + **Ollama** for local embeddings. Other providers (OpenAI-compatible APIs, OpenRouter, Together) are supported but experimental and not fully tested.

The system is backed by over 1,000 unit tests (Python + TypeScript), 15 automated installer scenarios covering fresh installs, dirty upgrades, data preservation, migration, missing dependencies, and provider combinations, plus benchmark evaluation against [LoCoMo](docs/BENCHMARKS.md) and [LongMemEval](https://github.com/xiaowu0162/LongMemEval).

We're actively testing and refining the system against benchmarks and welcome collaboration. If you're interested in contributing, testing, or just have ideas — open an issue or reach out.

---

## Learn More

- [Architecture Guide](docs/ARCHITECTURE.md) — How Quaid works under the hood
- [AI Agent Reference](docs/AI-REFERENCE.md) — Complete system index for AI assistants
- [Benchmark Results](docs/BENCHMARKS.md) — Full LoCoMo evaluation with per-category breakdowns
- [Roadmap](ROADMAP.md) — What's coming next

---

## Author

**Solomon Steadman** —[@steadman](https://x.com/steadman) | [github.com/solstead](https://github.com/solstead)

## License

Apache 2.0 — see [LICENSE](LICENSE).
