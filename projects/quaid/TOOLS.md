# Knowledge Layer — Tools & Config

## memory_recall Tool

- **Search:** Hybrid (FTS + semantic in parallel), RRF fusion, intent-aware query classification
- **Scoring:** Composite (60% relevance + 20% recency + 15% frequency + confidence/bonuses), MMR diversity
- **Graph:** Multi-hop traversal (depth=2) for relationship queries via `expandGraph: true`
- **Store-aware recall:** `options.stores` + `options.routing.enabled` let you query specific knowledge stores or use a routed catch-all plan
- **API shape:** `memory_recall(query, options)` where `options` contains `stores`, `graph`, `routing`, `filters`, `ranking`
- **Note:** Full gateway restart required after plugin changes (SIGUSR1 doesn't reload TS source)

### Knowledge Stores

- `vector_basic`: personal/life facts, preferences, relationships
- `vector_technical`: technical/project-state facts (tests, versions, architecture decisions, bugs/fixes)
- `graph`: relationship traversal and connected entity edges
- `journal`: distilled reflective signal from journal files
- `project`: project documentation recall from docs RAG index

### Project Description Contract

- Each project `TOOLS.md` should include a one-line top-level description:
  - `Project Description: <one sentence>`
- This short line is used by `total_recall` routing context.
- Keep `PROJECT.md` as long-form overview/reference (not token-capped runtime context).

### total_recall Guidance

- Think of `total_recall` as the catch-all recall path.
- `total_recall` always runs one fast reasoning planning pass (query cleanup + store routing).
- This improves hit quality, but it adds one extra fast-LLM call.
- If you already know the target stores, use plain recall with explicit stores (no planner pass).

### Practical Call Patterns

- Known relationship question:
  - `options: { stores: ["vector_basic","graph"], routing: { enabled: false } }`
- Known technical question:
  - `options: { stores: ["vector_technical","project"], routing: { enabled: false } }`
- Ambiguous question:
  - use `total_recall` (planner enabled by design)
  - `options: { routing: { enabled: true, reasoning: "fast" } }`
- Agent-action question:
  - `options: { routing: { enabled: true, intent: "agent_actions" } }`

### Injector Policy

- Auto-injected context is a hint layer.
- If not a direct/high-confidence hit, run explicit `memory_recall` before answering with specifics.
- Prefer explicit recall for names, dates, relationships, and agent-action attribution.

## CLI Commands

```bash
cd plugins/quaid

# Memory search (hybrid: FTS + semantic, RRF fusion, intent-aware)
python3 memory_graph.py search "query" --owner quaid --limit 50 --min-similarity 0.6
python3 memory_graph.py search-all "query" --owner quaid --limit 5
python3 memory_graph.py search-graph "query" --owner quaid  # Hybrid search + graph expansion
python3 memory_graph.py store "text" --owner quaid --category fact
python3 memory_graph.py stats
python3 memory_graph.py health             # KB health metrics

# Edges & versioning
python3 memory_graph.py get-edges <node_id>
python3 memory_graph.py create-edge "Subject" "relation" "Object"
python3 memory_graph.py fact-history <node_id>  # Versioning chain

# Documentation
python3 docs_rag.py search "query"
python3 docs_updater.py check              # Show stale docs
python3 docs_updater.py update-stale --apply  # Update all stale docs
python3 docs_updater.py changelog          # View doc update history
python3 docs_updater.py cleanup-check      # Check which docs need cleanup
python3 docs_updater.py cleanup --apply    # Clean up bloated docs

# Janitor
python3 janitor.py --task all --dry-run    # Full pipeline (dry run)
python3 janitor.py --task docs_staleness   # Just doc staleness check
python3 janitor.py --task review --apply   # Run Opus review

# Projects
python3 docs_registry.py list --project quaid
python3 docs_registry.py find-project <path>
python3 docs_registry.py create-project <name> --label "Label"
python3 project_updater.py process-all
python3 project_updater.py check
```

## Config

`config/memory.json` — models, database, ollama, RAG, search, docs, decay, janitor, projects

### Provider Switch Validation (Default Model Mode)

Use this to validate abstraction correctness (provider changes, model stays default):

1. Set:
```json
{
  "models": {
    "llmProvider": "default",
    "fastReasoning": "default",
    "deepReasoning": "default"
  }
}
```
2. Ensure both maps include at least `openai` and `anthropic`:
`models.deepReasoningModelClasses`, `models.fastReasoningModelClasses`.
3. Restart gateway (plugin runtime is `adapters/openclaw/index.js`; full restart required).
4. Test route:
```bash
curl -sS -X POST http://127.0.0.1:18789/plugins/quaid/llm \
  -H 'content-type: application/json' \
  --data '{"system_prompt":"Return JSON only.","user_message":"Reply with {\"ok\":true}","model_tier":"fast","max_tokens":64}'
```
5. Confirm logs show:
- `[quaid][llm] request tier=... provider=... model=...`
- Provider switches as auth/default provider changes, without code edits.

Expected behavior:
- Core flows (`/new`, `/reset`, `/compact`, timeout extraction, recall reranker) continue working while provider changes.
- Unsupported/missing provider class entries return explicit errors (no hidden fallback).

## Reference Docs

| Doc | Purpose |
|-----|---------|
| `projects/quaid/reference/memory-system-design.md` | Architecture overview, three-layer system, lifecycle |
| `projects/quaid/reference/memory-local-implementation.md` | Implementation: modules, config, shared lib, hooks, projects |
| `projects/quaid/reference/janitor-reference.md` | Nightly pipeline: tasks, thresholds, fail-fast, costs |
| `projects/quaid/reference/memory-schema.md` | Database schema: nodes, edges, FTS5, indexes, doc_registry |
| `projects/quaid/reference/memory-deduplication-system.md` | Dedup pipeline: thresholds, Haiku verification |
| `projects/quaid/reference/memory-operations-guide.md` | User-facing operations guide |

Use `projects_search` tool with `project` filter for project-scoped searches.

## projects_search vs recall(store=project)

- `projects_search` (current behavior):
  - runs `docs_rag.py search` (optional `project` filter)
  - if `project` is provided, prepends that project's `PROJECT.md` (when configured)
  - appends docs staleness warnings from `docs_updater.py check --json`
  - returns a human-readable block optimized for agent use

- `memory_recall` with `options.stores: ["project"]`:
  - uses project store recall path only (RAG matches)
  - supports `docs` filter (doc name/path fragments) for explicit scope
  - does not prepend `PROJECT.md`
  - does not include docs staleness warnings

- Conclusion:
  - They overlap, but are not equivalent today.
  - Keep `projects_search` as the richer docs workflow until project-store recall is upgraded to include project bootstrap + staleness metadata.
