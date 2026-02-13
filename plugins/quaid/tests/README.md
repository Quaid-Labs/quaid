# Tests

Unit and integration tests for the Quaid memory plugin.

## Running Tests

**Python tests** (pytest):

```bash
cd plugins/quaid
python3 -m pytest tests/ -v
```

**TypeScript tests** (vitest):

```bash
cd plugins/quaid
npx vitest run
```

## What's Covered

| Area | Files | What it tests |
|------|-------|---------------|
| **Core memory** | `test_store_recall.py`, `test_search.py` | Store/recall pipeline, hybrid search ranking |
| **Graph traversal** | `test_graph_traversal.py`, `test_beam_search.py` | Relationship traversal, BEAM search with LLM reranking |
| **Deduplication** | `test_merge_nodes.py`, `test_chunk1_improvements.py`, `test_chunk2_improvements.py` | Merge safety (confidence, edges, status), dedup detection |
| **Edge normalization** | `test_edge_normalization.py` | Inverse/synonym/symmetric relation handling |
| **Janitor pipeline** | `test_batch2_data_quality.py`, `test_batch3_smart_retrieval.py`, `test_batch4_decay_traversal.py` | Review, retrieval, decay tasks |
| **Docs system** | `test_docs_registry.py`, `test_docs_updater.py`, `test_docs_rag.py` | Project registry, staleness detection, RAG search |
| **Snippets & journal** | `test_soul_snippets.py` | FOLD/REWRITE/DISCARD review, journal distillation |
| **Workspace audit** | `test_workspace_audit.py`, `test_protected_regions.py` | Bloat monitoring, protected region enforcement |
| **Regression** | `test_golden_recall.py` | Golden dataset (30 facts, 20 queries, 10 adversarial) |
| **Invariants** | `test_invariants.py` | 8 structural lifecycle checks |
| **Plugin hooks** | `*.test.ts` | Store, recall, dedup, decay, sessions, RAG, embeddings |

## Requirements

- Python 3.10+
- Node.js 18+ (for TypeScript tests)
- No API keys needed -- all LLM calls are mocked
