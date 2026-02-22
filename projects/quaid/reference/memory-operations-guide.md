# Memory & Documentation System

> Migrated from `TOOLS.md` on 2026-02-06
> Reason: Detailed operational guide with CLI examples - too long for every turn. Keep just the three-layer summary and tool table, link to this doc

## Memory & Documentation System

This is the core system that gives you persistent understanding of Quaid's world
and the systems you operate. Without it, every conversation starts from zero.

### How It Works (Three Layers)

1. **Markdown** (always loaded): SOUL.md, USER.md, TOOLS.md, HEARTBEAT.md — your identity
   and operational context, loaded every turn. This is "who you are."

2. **Docs/RAG** (searched on demand): `docs/<project>/` docs with detailed reference docs.
   Searched semantically via the `docs_search` tool or `docs_rag.py`. This is
   "how systems work internally" — architecture, schemas, pipelines.

3. **Memory DB** (semantic recall per message): SQLite graph of personal facts about
   Quaid, family, friends, pets. Queried via `memory_recall` when the agent needs
   personal context (auto-injection is optional via config/env).
   This is "what you know about the people in your life."

**Key distinction:** Memory = personal facts ONLY. System architecture goes in docs/RAG.
Operational rules go in markdown. This boundary is enforced by every LLM prompt.

### Memory Lifecycle

Facts flow: **capture → pending → review (Opus) → approved → dedup/edges → active**

- Capture happens at compact/reset (Opus extracts facts from the conversation transcript)
- Nightly janitor (Sandman, 4:30 AM) handles dedup, decay, contradiction detection, and infra tasks
- Decay: Ebbinghaus exponential with access-scaled half-life; below threshold queues for Opus review. Pinned facts never decay.

### Documentation Auto-Update

Docs stay current through three paths:

1. **Compact/Reset** — When you modify source files and the session compacts, the plugin
   detects which monitored files changed and auto-updates affected docs using the transcript
2. **On-demand** — When `docs_search` finds stale docs, it warns you with which sources changed
3. **Nightly janitor** (Task 1b) — Compares source mtimes to doc mtimes, updates stale docs
   from git diffs

Source-to-doc mappings live in `config/memory.json → docs.sourceMapping`.

### Operate

**Tools (registered in gateway):**
| Tool | Purpose |
|------|---------|
| `memory_recall` | Semantic search (tool-driven; auto-injection optional) |
| `memory_store` | Manual storage with dedup |
| `memory_forget` | Delete by ID or query |
| `docs_search` | Search docs via RAG + staleness warnings |

**CLI:**
```bash
