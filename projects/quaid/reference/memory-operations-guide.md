# Memory & Documentation System

> Migrated from `TOOLS.md` on 2026-02-06
> Reason: Detailed operational guide with CLI examples - too long for every turn. Keep just the three-layer summary and tool table, link to this doc

## Memory & Documentation System

This is the core system that gives you persistent understanding of Quaid's world
and the systems you operate. Without it, every conversation starts from zero.

### How It Works (Three Layers)

1. **Markdown** (always loaded): SOUL.md, USER.md, MEMORY.md, AGENTS.md, TOOLS.md, CONSTITUTION.md, PROJECT.md - your identity and operational context, loaded every turn.
2. **Docs/RAG** (searched on demand): `projects/<project>/` docs with deep technical references, queried with `projects_search` or `datastore/docsdb/rag.py`.
3. **Memory DB** (semantic recall): SQLite graph of user facts, preferences, and relationships queried via `memory_recall`.

**Key distinction:** Memory is for user/domain facts. Architecture and implementation details live in project docs.

### Memory Lifecycle

Facts flow: **capture -> pending -> review (Opus) -> approved -> dedup/edges -> active**

- Capture runs at compaction/reset from full transcript extraction.
- Nightly janitor handles dedup, contradiction handling, decay, docs upkeep, snippets, and journal tasks.
- Decay uses Ebbinghaus-style confidence drop; pinned facts never decay.

### Documentation Auto-Update

Docs stay current through three paths:

1. **Compact/Reset**: changed monitored files can trigger doc updates using session context.
2. **On-demand staleness check**: `projects_search` can flag stale docs.
3. **Nightly janitor**: compares source vs doc mtimes and applies updates from git diffs.

Source mappings live in `config/memory.json -> docs.sourceMapping`.

### Operate

**Tools (registered in gateway):**
| Tool | Purpose |
|------|---------|
| `memory_recall` | Semantic recall (tool-driven; auto-injection optional) |
| `memory_store` | Queue manual memory note for next extraction pass |
| `memory_forget` | Delete by ID or query |
| `projects_search` | Search docs via RAG + staleness warnings |

**CLI:**
```bash
cd modules/quaid

# Memory recall and graph-aware lookup
python3 datastore/memorydb/memory_graph.py search "query" --owner quaid --limit 50
python3 datastore/memorydb/memory_graph.py search-graph-aware "query" --owner quaid --limit 50 --json

# Memory write/delete
python3 datastore/memorydb/memory_graph.py store "text" --owner quaid --category fact
python3 datastore/memorydb/memory_graph.py forget --id <node_id>

# Docs and projects
python3 datastore/docsdb/rag.py search "query" --project quaid
python3 datastore/docsdb/registry.py list --project quaid

# Janitor lifecycle
python3 core/lifecycle/janitor.py --task all --dry-run
python3 core/lifecycle/janitor.py --task all --apply
```

### Retrieval Tips

- Prefer `projects_search` for architecture/process/codebase questions.
- Prefer `memory_recall` for user facts, decisions, and timelines.
- For relationship questions, use graph expansion (`search-graph-aware` or `options.graph.expand=true`).

### Domain Filters

- Default recall behavior is `{ "all": true }`.
- To scope results, set explicit domains (for example `{ "technical": true }`).
- If any explicit domain is true, `all` is ignored and only selected domains are returned.

### See Also

- `projects/quaid/TOOLS.md` - current tool contracts and parameter maps.
- `projects/quaid/AGENTS.md` - operating rules and project navigation.
- `projects/quaid/reference/memory-local-implementation.md` - implementation internals.
