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

### MCP Server

Expose Quaid as a [Model Context Protocol](https://modelcontextprotocol.io)
server. This is the highest-leverage change on the roadmap: it decouples Quaid
from OpenClaw and lets anyone with Claude Desktop, Cursor, Windsurf, or any
MCP-compatible client store and recall memories. The core Python API (`store()`,
`recall()`, `search()`) is already a clean interface — wrapping it in MCP tools
is a relatively small lift that transforms Quaid from "an OpenClaw plugin" to
"an agent memory system that works everywhere."
- `memory_store`, `memory_recall`, `memory_search` tools
- Stateless server, SQLite backend
- Works with Claude Desktop, Cursor, Windsurf, etc.
- OpenClaw plugin remains the primary integration (hooks, extraction, janitor)

### Close the Open-Domain Gap

Quaid wins by 11-23pp on single-hop, multi-hop, and temporal queries, but
trails on open-domain inference. The retrieval pipeline is optimized for
precision lookup (exact facts, graph traversal, entity resolution), but
open-domain questions require broader contextual reasoning. Journal injection
showed +5.4pp but at 2.7x token cost. The fix is intent-aware context expansion:
open-domain queries get a wider retrieval window (more results, lower similarity
threshold, more graph hops), while lookup queries stay tight. The intent
classification system is already in place.
- Per-intent retrieval parameters (already prototyped in A/B test battery)
- Semantic cluster summaries for GENERAL and WHY intents
- Routed retrieval: open-domain gets journal + synthesis prompt, others get standard

### Cloud Embedding Support

API-based embeddings (OpenAI `text-embedding-3-small`, Cohere, etc.) as an
alternative to Ollama. Enables Quaid on machines without a GPU or local model server.
- Backend: `embeddings.py` OpenAI client
- Installer: cloud embedding option, dual API key passthrough
- Re-embed migration tool (switch providers without rebuilding the index)
- Hot-swappable fallback: cloud embeddings if Ollama is down

### LongMemEval Benchmark

Run the full [LongMemEval](https://github.com/xiaowu0162/LongMemEval) evaluation
(ICLR 2025). 500 QA pairs, 7 question types, 19,195 sessions. Code is written
and smoke-tested. Two benchmarks is exponentially more credible than one,
especially since LongMemEval tests different things than LoCoMo. If Quaid's
multi-hop and temporal strengths carry over, the numbers could be competitive
with the leaders.
- Publish results alongside LoCoMo numbers
- Compare vs: Emergence AI (86%), Supermemory (81.6%), Zep+GPT-4o (71.2%)

### Graph Visualization

A web-based graph explorer isn't just a nice-to-have — it's the best way to
make Quaid tangible to someone who hasn't used it. The fact schema is rich
(confidence, storage strength, decay, confirmation count, temporal fields,
edges with normalized relations), and a visual rendering makes both debugging
and demos dramatically more effective. When someone asks "why didn't it
remember X?", tracing the retrieval path visually is far more useful than
log files.
- Node/edge browser with search
- Cluster visualization (related facts grouped)
- Timeline view (facts by creation date)
- Confidence heatmap (strong vs. decaying memories)
- Retrieval path tracing (why did this query return these results?)

### Memory Import from ChatGPT and Claude

"Bring your memories with you." Users who've accumulated months of ChatGPT
or Claude memories have a real switching cost — eliminating it makes Quaid
immediately valuable on day one. The extraction pipeline already handles
unstructured text; wrapping it to parse export formats is mostly glue code.
- Import from: ChatGPT memory export, Claude memory format
- Import from: Mem0, Zep, plain JSON
- Export to: JSON, CSV, Markdown, Neo4j
- Migration wizard in installer

### Benchmark CI

Turn the reproducible benchmark methodology into a GitHub Actions workflow.
Run LoCoMo on every significant PR to prevent regression and create a public
record of progress over time. Cost is modest (about $85 for a full eval run)
and can run on a smaller subset for PRs with full runs on merge to main.
- LoCoMo subset on PR (fast, catch regressions)
- Full LoCoMo + LongMemEval on merge to main
- Accuracy trend dashboard

## Later (v0.3+)

### Purpose-Built Benchmark

No existing benchmark fully evaluates long-running personal memory systems.
LoCoMo comes closest, but the field needs a dataset that tests the full
lifecycle: multi-session accumulation, fact evolution over time, maintenance
decisions, and project-level awareness. Building one is a priority, but creating
a rigorous, peer-reviewable dataset is costly and time-intensive.
- Full lifecycle testing (extraction, janitor, recall across weeks)
- Evolution chains (facts that change over time)
- Multi-session context accumulation
- Adversarial queries (plausible but wrong)

### Claude Code Project Integration

Bidirectional project awareness between Claude Code and the OpenClaw agent.
Today Claude Code tracks file edits (PostToolUse hook) and stages project
events at compaction (PreCompact hook), but there's no shared project context.
- Claude Code reads project docs (PROJECT.md, TOOLS.md, AGENTS.md) for context
- Shared project event bus
- Coordinated compaction
- Project-scoped tool registration

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

### SentenceWindow RAG

Chunking strategy for doc indexing: instead of whole-file chunks, use
overlapping sentence windows for finer-grained retrieval. Better recall
on specific passages within large documents.

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
