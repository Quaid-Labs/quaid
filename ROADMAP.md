# Quaid Roadmap

> *"The best thing about the future is that it comes one day at a time."*

## Now (v0.1 — Alpha)

What ships today:

- Hybrid retrieval: sqlite-vec ANN + FTS5 + graph traversal, fused with RRF
- LLM extraction at compaction/reset (facts + edges in one pass)
- 17-task nightly janitor (review, dedup, contradiction, decay, doc updates)
- Dual learning: snippets (fast, continuous) + journal (slow, reflective)
- Projects system with auto-discovery and doc tracking
- Local embeddings via Ollama
- Multi-provider LLM support (Anthropic recommended, OpenAI-compatible experimental)

## Next (v0.2)

### Cloud Embedding Support
API-based embeddings (OpenAI `text-embedding-3-small`, Cohere, etc.) as an
alternative to Ollama. Enables Quaid on machines without a GPU.
- Backend: `embeddings.py` OpenAI client
- Installer: uncomment cloud option, dual API key passthrough in heartbeat
- Re-embed migration tool

### LongMemEval Benchmark
Run the full [LongMemEval](https://github.com/xiaowu0162/LongMemEval) evaluation
(ICLR 2025). 500 QA pairs, 7 question types, 19,195 sessions. Code is written
and smoke-tested. Currently in an intense A/B testing period on LoCoMo to find
feature stability before committing to a longer benchmark run.
- Publish results alongside LoCoMo numbers
- Compare vs: Emergence AI (86%), Supermemory (81.6%), Zep+GPT-4o (71.2%)

### Purpose-Built Benchmark
No existing benchmark fully evaluates long-running personal memory systems -- LoCoMo
comes closest, but the field needs a dataset that tests the full lifecycle: multi-session
accumulation, fact evolution over time, maintenance decisions, and project-level awareness.
Building one is a priority, but creating a rigorous, peer-reviewable dataset is costly and
time-intensive. Until then, we continue to evaluate and optimize against LoCoMo.
- Full lifecycle testing (extraction → janitor → recall across weeks)
- Evolution chains (facts that change over time)
- Multi-session context accumulation
- Adversarial queries (plausible but wrong)

### Claude Code Project Integration
Bidirectional project awareness between Claude Code and the OpenClaw agent.
Today Claude Code tracks file edits (PostToolUse hook) and stages project
events at compaction (PreCompact hook), but there's no shared project context.
- Claude Code reads project docs (PROJECT.md, TOOLS.md, AGENTS.md) for context
- Shared project event bus — both agents see file changes, doc updates, project creation
- Coordinated compaction — Claude Code's context feeds into the same memory pipeline
- Project-scoped tool registration (Claude Code gets project-specific tools)
- Use case: Claude Code handles implementation, OpenClaw agent handles communication and scheduling, both share project state through Quaid

## Later (v0.3+)

### Multi-Owner Memory
Multiple owners sharing a single Quaid instance. Each owner's facts are
namespaced and isolated, but the graph structure is shared.
- Owner-scoped queries (already partially implemented via `owner_id`)
- Per-owner config overrides
- Owner management CLI (`quaid owners list/add/remove`)
- Cross-owner fact sharing (opt-in)

### Multi-Agent Shared Memory
Multiple agents reading from and writing to the same memory graph.
- Agent identity tracking (`source_agent` field)
- Agent-scoped views (each agent sees relevant subset)
- Conflict resolution for concurrent writes
- Shared vs. private fact classification
- Use case: team of specialized agents (coder, researcher, ops) sharing context

### SentenceWindow RAG
Chunking strategy for doc indexing: instead of whole-file chunks, use
overlapping sentence windows for finer-grained retrieval. Better recall
on specific passages within large documents.

### Graph Visualization
Web-based explorer for the knowledge graph:
- Node/edge browser with search
- Cluster visualization (related facts grouped)
- Timeline view (facts by creation date)
- Confidence heatmap (strong vs. decaying memories)

### Memory Import/Export
Interop with other memory systems:
- Import from: Mem0, Zep, ChatGPT memory export, plain JSON
- Export to: JSON, CSV, Markdown, Neo4j
- Migration wizard in installer

### MCP Server
Expose Quaid as a [Model Context Protocol](https://modelcontextprotocol.io)
server. Any MCP-compatible client can store and recall memories.
- `memory_store`, `memory_recall`, `memory_search` tools
- Stateless server, SQLite backend
- Works with Claude Desktop, Cursor, Windsurf, etc.

### Privacy Controls
PII detection and memory redaction:
- Auto-detect sensitive data (emails, phone numbers, API keys)
- Redact or flag for review before storage
- GDPR-style "right to forget" (`quaid forget --owner X`)
- Encrypted at-rest option for memory.db

## Someday / Maybe

- **NVIDIA Spark integration** — hardware-accelerated embedding and inference
- **Encrypted cloud backup** — S3/GCS backup with client-side encryption
- **Memory compression** — summarize old memories into fewer, denser nodes
- **Webhook events** — notify external systems on memory changes
- **Plugin API** — let other OpenClaw plugins read/write Quaid's memory
- **Confidence calibration** — tune decay/boost parameters from benchmark feedback

---

*Have ideas? Open an issue at [github.com/rekall-inc/quaid](https://github.com/rekall-inc/quaid).*
