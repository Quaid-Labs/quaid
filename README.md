
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

Quaid is a memory plugin for [OpenClaw](https://github.com/openclaw/openclaw) that gives your AI agent persistent, long-term memory. Every conversation your agent has gets distilled into facts, relationships, and preferences -- stored locally in a SQLite graph database on your machine. When your agent needs context, Quaid finds the right memories and injects them automatically.

No cloud storage. No third-party data pipelines. Your memories stay on your hardware, embedded locally with Ollama, and retrieved with a hybrid search system that combines vector similarity, full-text keyword matching, and graph traversal.

Quaid also runs a nightly maintenance cycle -- a "janitor" pipeline that reviews new memories, merges duplicates, resolves contradictions, and lets stale facts decay naturally. Your agent's memory stays clean and current without any manual work.

---

## Benchmark Results

Quaid was evaluated on the [LoCoMo benchmark](https://github.com/snap-research/locomo) -- 10 long conversations with 1,540 scored question-answer pairs testing memory extraction, temporal reasoning, and multi-hop recall.

| System | Accuracy |
|--------|----------|
| **Quaid + Opus** | **75.0%** |
| **Quaid + Journal** | **74.5%** |
| **Quaid + Haiku** | **70.3%** |
| Mem0 (guided) | 68.9% |
| Mem0 | 66.9% |
| Zep | 66.0% |
| LangMem | 58.1% |
| OpenAI Memory | 52.9% |

> Mem0, Zep, LangMem, and OpenAI numbers are from their [April 2025 paper](https://arxiv.org/abs/2504.01094).
> Full-context baselines: Haiku 79.6%, Opus 86.6%.
> Full methodology and per-category breakdowns: [docs/BENCHMARKS.md](docs/BENCHMARKS.md)

---

## Features

- **Automatic fact extraction** -- Facts and relationships pulled from conversations at compaction, no manual tagging
- **Semantic + keyword search** -- Hybrid retrieval combining vector embeddings, full-text search, and graph traversal
- **Nightly maintenance** -- Janitor pipeline deduplicates, resolves contradictions, and decays stale memories
- **Personality evolution** -- Journal and snippet systems let your agent develop and refine its understanding over time
- **Project tracking** -- Monitors your codebase docs and keeps them up to date from git changes
- **Multi-provider** -- Anthropic recommended; any OpenAI-compatible API also supported
- **Local embeddings** -- Ollama runs on your machine -- free, private, no data leaves your network

---

## How It Works

```
Conversation --> compaction --> LLM extracts facts --> SQLite graph DB
Agent query  --> hybrid search --> LLM reranker   --> context injection
Nightly      --> janitor reviews, deduplicates, decays old memories
```

Facts are extracted automatically when your agent compacts or resets a conversation. At recall time, Quaid classifies the query intent, generates a hypothetical answer for better vector matching, searches across multiple signals, and uses an LLM reranker to surface the most relevant memories.

The nightly janitor handles the rest: reviewing pending facts, merging duplicates, detecting contradictions, decaying old memories, and keeping your documentation in sync.

---

## Cost

| Component | Cost |
|-----------|------|
| Fact extraction | ~$0.05--0.20 per compaction (Opus) |
| Memory recall | ~$0.01 per query (Haiku reranker) |
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

---

## Learn More

- [Architecture Guide](docs/ARCHITECTURE.md) -- How Quaid works under the hood
- [AI Agent Reference](docs/AI-REFERENCE.md) -- Complete system index for AI assistants
- [Roadmap](ROADMAP.md) -- What's coming next

---

## Author

**Solomon Steadman** -- [@steadman](https://x.com/steadman) | [github.com/solstead](https://github.com/solstead)

## License

MIT -- see [LICENSE](LICENSE).
