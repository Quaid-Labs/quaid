# Memory System — Tools & Config

## memory_recall Tool

- **Search:** Hybrid (FTS + semantic in parallel), RRF fusion, intent-aware query classification
- **Scoring:** Composite (60% relevance + 20% recency + 15% frequency + confidence/bonuses), MMR diversity
- **Graph:** Multi-hop traversal (depth=2) for relationship queries via `expandGraph: true`
- **Default:** Searches memory only (use `docs_search` for docs/RAG)
- **Note:** Full gateway restart required after plugin changes (SIGUSR1 doesn't reload TS source)

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

Use `docs_search` tool with `project` filter for project-scoped searches.
