# Memory System Reference
<!-- PURPOSE: Consolidated reference: architecture, implementation, schema, and configuration for the Quaid memory system -->
<!-- SOURCES: memory-system-design.md, memory-local-implementation.md, memory-schema.md -->

This document is the single authoritative reference for the Quaid memory system. It covers system architecture and design decisions (formerly `memory-system-design.md`), full implementation details for all modules, hooks, and shared libraries (formerly `memory-local-implementation.md`), and the complete SQLite schema DDL for all tables (formerly `memory-schema.md`). For day-to-day operations see `memory-operations-guide.md`. For deduplication internals see `memory-deduplication-system.md`.

**Status:** Production Ready (updated 2026-02-08)
**Location:** `modules/quaid/`
**Codename:** Total Recall (quaid)
*Design doc created: 2026-01-31 | Updated: 2026-02-08 | Status: Phase 6 complete — search batches 1-4, Ebbinghaus decay, projects system, append-only project logs*

---

## 1. System Overview

### Architecture Summary

The knowledge layer is a graph-based personal knowledge base using SQLite + Ollama embeddings, fully local for storage and search. LLM calls are provider-agnostic at core level and are resolved through the adapter/provider layer (gateway + config-driven deep/fast model tiers). Ollama is used for embeddings only.

**Key design decisions:**
- Local-first: Ollama for embeddings only (`qwen3-embedding:8b`, 4096-dim), SQLite for storage
- Graph structure: nodes (facts, people, preferences) + edges (relationships)
- Hybrid search: semantic similarity + full-text keyword search with proper noun boosting
- Privacy-aware: per-fact privacy tiers (private/shared/public) with owner-based filtering
- Nightly maintenance: automated janitor pipeline for dedup, decay, docs/project upkeep
- Config-driven: all model IDs, paths, and settings in `config/memory.json`

### Three-Layer Architecture

The system uses three tiers with distinct purposes:

| Layer | Storage | Loaded When | Purpose | Examples |
|-------|---------|-------------|---------|----------|
| **Markdown** | SOUL.md, USER.md, MEMORY.md, AGENTS.md, TOOLS.md, CONSTITUTION.md, PROJECT.md | Every context (always injected) | Core instructions, identity, system pointers | "Alfie's personality", "Quaid's core facts", "System tool locations" |
| **RAG** | `projects/<project>/` docs | Searched when topically relevant | Reference documentation, system architecture | "Knowledge layer design", "Janitor pipeline reference", "Spark agent planning" |
| **Memory DB** | `data/memory.db` | Searched per-message via recall pipeline | Personal facts from conversations | "Quaid prefers dark mode", "Melina's birthday is Oct 12", "Quaid chose SQLite for simplicity" |

**What belongs in Memory (the DB):**
- Personal facts about people: names, relationships, birthdays, health, locations
- Preferences and opinions: likes, dislikes, communication styles
- Personal decisions with reasoning: "Quaid chose X because Y"
- Life events: trips, milestones, purchases, diagnoses
- Relationships: family, friends, colleagues, pets

**What does NOT belong in Memory:**
- System architecture ("The knowledge layer uses SQLite with WAL mode") → RAG docs (`projects/<project>/`)
- Infrastructure knowledge ("Ollama runs on port 11434") → RAG docs (`projects/<project>/`)
- Operational rules for AI agents ("Alfie should check AGENTS.md on wake") → markdown core files
- Tool/config descriptions ("The janitor has a dedup threshold of 0.85") → RAG docs (`projects/<project>/`)

### Recall Pipeline Architecture

```
Agent calls memory_recall with crafted query
     │
     ▼
Intent classification (WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY/PROJECT)
     │
     ▼
HyDE query expansion (route_query) — rephrases question as declarative statement
     │  for better vector-space alignment; skipped for short queries (<15 chars);
     │  falls back to original query on LLM failure
     ▼
Parallel search (ThreadPoolExecutor)
  ├── Semantic: cosine similarity on qwen3-embedding:8b (4096-dim)
  └── BM25: FTS5 full-text search with proper noun weighting
     │
     ▼
RRF Fusion (k=60, dynamic weights via _get_fusion_weights(intent))
     │
     ▼
Content hash pre-filter (SHA256 exact dedup)
     │
     ▼
Composite scoring (60% relevance + 20% recency + 15% frequency + 5% confidence)
     │
     ▼
Temporal validity filtering (expired/future penalties)
     │
     ▼
MMR diversity (lambda=0.7)
     │
     ▼
Multi-hop graph traversal (depth=2, score decay 0.7^depth)
     │
     ▼
Privacy filter + session dedup
     │
     ▼
Access tracking (increment access_count on returned results)
     │
     ▼
Agent receives results with similarity %, extraction_confidence
     │
     ▼
[on /compact or /reset] Opus extraction → personal facts+edges → status: pending
                                       → soul_snippets → .snippets.md staging files
                                       → journal_entries → journal/*.journal.md diary files
```

> **Note:** Recall is agent-driven via `memory_recall` tool (Feb 2026). Auto-injection is optional (gated by config/env). Auto-capture via per-message classifier is deprecated, but inactivity-timeout extraction still runs when capture is enabled. Memory extraction happens at compaction/reset via Opus with combined fact+edge+snippet+journal extraction. Soul snippets are observations written to `.snippets.md` staging files, reviewed by janitor Task 1d-snippets, and folded into core markdown files (default SOUL.md, USER.md, MEMORY.md; AGENTS.md optional via `docs.journal.targetFiles`). Journal entries are diary-style paragraphs written to `journal/*.journal.md`, distilled by janitor Task 1d-journal into core markdown themes, then archived to `journal/archive/`.

### Privacy Tiers

| Tier | Description | Visibility |
|------|-------------|------------|
| `public` | Non-personal general facts | All owners |
| `shared` | Household knowledge (default) | All owners |
| `private` | Secrets, surprises, finances, health | Owner only |

Privacy is classified per-fact during extraction (Opus at compaction/reset). Default is `shared`. The recall pipeline filters private memories to only the owning user.

### Search System (Batches 1-4, Feb 2026)

Multi-stage pipeline with RRF fusion, HyDE query expansion, intent awareness, and diversity:

1. **Intent classification** — categorizes query as WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY/PROJECT
2. **HyDE query expansion** — `route_query()` rephrases the question as a declarative statement before embedding (Hypothetical Document Embedding). Embedding an answer-like statement is closer in vector space to stored facts than the raw question. Skipped for queries < 15 chars; falls back to original query on LLM failure. Controlled by `retrieval.use_hyde` (default true).
3. **Parallel search** — BM25 (FTS5) + semantic (cosine) run concurrently
4. **RRF fusion** — Reciprocal Rank Fusion combines results (k=60, weights dynamic per intent via `_get_fusion_weights()`)
5. **Content hash pre-filter** — SHA256 exact-dedup removes identical results
6. **Composite scoring** — 60% relevance + 20% recency + 15% access frequency + 5% extraction confidence
7. **Temporal validity** — expired facts penalized, future facts deprioritized
8. **MMR diversity** — Maximal Marginal Relevance (lambda=0.7) prevents redundant results
9. **Multi-hop traversal** — bidirectional graph traversal, depth=2, hop score decay 0.7^depth
10. **Access tracking** — increments access_count and accessed_at on returned results

### Decay System (Ebbinghaus)

- **Formula:** `R = 2^(-t / half_life)` with access-scaled half-life
- `half_life = 60d × (1 + 0.15 × access_count) × (2 if verified)`
- Pinned memories never decay; frequently accessed memories decay slower
- Below threshold: queued for Opus review (DELETE/EXTEND/PIN), not silently deleted

### Data Quality

- **Content hash** (SHA256) for instant exact-dedup at store time
- **Embedding cache** (DB-backed) avoids redundant Ollama calls
- **Fact versioning** via `supersede_node()` — chains old→new facts
- **KB health metrics** — coverage, staleness, duplicate detection stats

### Implementation Phases (Historical)

#### Phase 1: LanceDB + Local Routing (DONE, Retired)
- [x] LanceDB with OpenAI embeddings — replaced by local system
- [x] Regex-based auto-capture — replaced by Haiku classifier
- [x] ~19 seeded memories — now ~616 active nodes, ~205 edges (all graduated)

#### Phase 2: Graph Store (DONE)
- [x] SQLite schema with nodes, edges, FTS5, metadata
- [x] Ollama embeddings (originally nomic-embed-text 768-dim, now qwen3-embedding:8b 4096-dim)
- [x] Entity extraction via Claude Haiku
- [x] Relationship extraction via janitor edges task
- [x] Hybrid search (semantic + FTS + edge traversal)

#### Phase 3: Custom Memory Plugin (DONE, Production)
- [x] Plugin skeleton with Clawdbot hooks
- [x] Hybrid search replaces LanceDB vector-only
- [x] Privacy tier filtering (per-fact classification)
- [x] User identity mapping via `config/memory.json`
- [x] Haiku reranker for recall relevance
- [x] Session dedup + compaction time-gate
- [x] LanceDB plugin disabled

#### Phase 4: Local-Only (DONE)
- [x] Ollama replaces OpenAI for embeddings
- [x] Zero external API dependency for storage/search
- [x] Anthropic API only used for extraction (Opus) and reranking (Haiku)

#### Phase 5: Quality & Governance (DONE)
- [x] Per-message auto-capture deprecated → event-based extraction (compaction/reset) + inactivity-timeout extraction
- [x] Config-driven model selection — no hardcoded model IDs
- [x] Fail-fast pipeline guard with graduation blocking
- [x] Temporal resolution (regex-based, no LLM)
- [x] Edge normalization (inverse/synonym maps, symmetric ordering)
- [x] Smart dedup with token-recall + Haiku verification
- [x] Decay review queue (Opus review instead of silent deletion)
- [x] Three-layer architecture: Markdown / RAG / Memory DB
- [x] LLM prompts scoped to personal facts only

#### Phase 6: Search & Retrieval Overhaul (DONE)
- [x] Batch 1: RRF fusion, BM25 via FTS5, composite scoring (60/20/15/5), MMR diversity
- [x] Batch 2: Content hash dedup, embedding cache, fact versioning, KB health metrics
- [x] Batch 3: Intent classification (WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY/PROJECT), temporal validity filtering
- [x] Batch 4: Ebbinghaus exponential decay, multi-hop traversal (depth=2), access tracking, parallel search
- [x] Combined fact+edge extraction at capture time (single LLM call)
- [x] Token-based janitor batching (context window-aware)
- [x] Agent-driven recall (auto-injection optional via config/env)
- [x] Projects system: registry, event processing, auto-discover
- [x] Mock embeddings for testing (`MOCK_EMBEDDINGS=1`)
- [x] 1400+ tests in default gate (1224 selected pytest + 222 vitest)

### Recent Capabilities (Feb 2026)

- **Search pipeline overhaul** (Batches 1-4): RRF fusion, BM25, composite scoring, MMR diversity, intent classification, temporal validity filtering, Ebbinghaus decay, multi-hop traversal, access tracking, parallel search
- **Agent-driven recall**: Auto-injection is optional; agent calls `memory_recall` tool with crafted queries
- **Combined fact+edge extraction**: Single Opus call extracts both facts and relationships at compaction/reset
- **Content hash dedup**: SHA256 pre-filter catches exact duplicates before embedding comparison
- **Embedding cache**: DB-backed cache avoids redundant Ollama calls
- **Fact versioning**: `supersede_node()` chains old→new facts with history tracking
- **Ebbinghaus decay**: `R = 2^(-t/half_life)` with access-scaled half-life (frequently accessed = slower decay)
- **Token-based batching**: `TokenBatchBuilder` dynamically packs items based on model context window
- **Projects system**: Registry CRUD, event-driven updates, auto-discover, 5 projects
- **Project history indexing**: append-only `PROJECT.log` per project is indexed by RAG (no truncation) for searchable change context
- **Mock embeddings**: `MOCK_EMBEDDINGS=1` env var for testing without Ollama
- **1400+ tests in default gate**: 1224 selected pytest + 222 vitest, all passing
- **Journal system**: Diary-style entries written to `journal/*.journal.md`, distilled into core markdown themes by janitor, archived monthly to `journal/archive/`. Two modes: `distilled` (default, token-efficient) and `full` (richer self-awareness)
- **Config-driven models**: All model IDs read from `config/memory.json`
- **Fail-fast pipeline**: `memory_pipeline_ok` flag — if any memory task fails, graduation blocked
- **Temporal resolution**: Regex-based date resolver (Task 2a)
- **Edge normalization**: Inverse flipping, synonym resolution, symmetric alphabetical ordering
- **Smart dedup**: Token-recall + Haiku verification in gray zone (0.88-0.98)
- **Graduation**: `pending → approved → active` lifecycle, all facts now graduated

### Design Notes

- Telegram has ~52 char width limit for code blocks — avoid wide ASCII diagrams
- LLM credentials resolved by adapter/gateway provider auth (OAuth/API key), not by core modules
- All paths, models, and settings are centralized in `config/memory.json` — see `config.py` for dataclass definitions
- Database path: `config.database.path` (default: `data/memory.db`, SQLite + WAL)
- Archive DB: `config.database.archivePath` (default: `data/memory_archive.db`)
- Embeddings: `config.ollama.url` (default: `http://localhost:11434`) with `config.ollama.embeddingModel` (default: `qwen3-embedding:8b`, 4096-dim)
- Shared library in `modules/quaid/lib/` — config, database, embeddings, similarity, tokens, archive
- Env var overrides for testing: `MEMORY_DB_PATH`, `MEMORY_ARCHIVE_DB_PATH`, `OLLAMA_URL`, `CLAWDBOT_WORKSPACE`

### Answered Design Questions

1. **Token budget:** Dynamic K based on node count (clamped by config); optional LLM reranker for relevance
2. **Capture quality:** Opus extraction at compaction/reset events with strict personal-facts-only criteria. Nightly janitor cleans any remaining noise.
3. **Graph initialization:** Database schema is initialized by `memory_graph.py` startup/migration paths, then continuously enriched by event-based extraction
4. **Stale-fact handling:** Supersession + temporal normalization + recency-weighted retrieval are the active conflict controls; contradiction task name remains as a compatibility no-op in current janitor runs

---

## 2. Implementation Guide

### 2.1 SQLite Schema Overview (`schema.sql`)

Graph database schema with the following tables (full DDL in [Section 3](#3-schema-reference)):

**Nodes table:**
- `id` (UUID), `type`, `name`, `attributes` (JSON)
- `embedding` (4096-dim float32 blob for qwen3-embedding:8b)
- `verified` (0=auto-extracted, 1=confirmed), `pinned` (never decays)
- `confidence` (0-1 score), `extraction_confidence` (0-1, how confident the classifier was)
- `source`, `source_id` (provenance tracking), `speaker` (who stated the fact)
- `privacy` (private/shared/public), `owner_id` (multi-user)
- `session_id` (session where this memory was created)
- `valid_from`, `valid_until` (temporal validity)
- Access tracking: `created_at`, `updated_at`, `accessed_at`, `access_count`
- `keywords` (space-separated derived search terms, generated at extraction for FTS vocabulary bridging)
- Lifecycle: `status` (pending/approved/active), `deleted_at`/`deletion_reason` (legacy, unused)

**Edges table:**
- `id`, `source_id`, `target_id`, `relation`
- `attributes` (JSON), `weight`, temporal fields

**Contradictions table:**
- `id`, `node_a_id`, `node_b_id`, `explanation`
- `status` (pending/resolved/false_positive), `resolution`, `resolution_reason`
- `detected_at`, `resolved_at`

**Edge Keywords table:**
- `relation` (PRIMARY KEY), `keywords` (JSON array of trigger words)
- `description`, `created_at`, `updated_at`
- Used for graph expansion triggers during search

**Project Definitions table** (managed by `datastore/docsdb/registry.py`):
- `name` (PRIMARY KEY), `label`, `home_dir`, `source_roots` (JSON array), `patterns`, `exclude`
- `state` (active/archived/deleted), `auto_index`
- Source of truth for project config (migrated from JSON)

**Indexes:** Full-text search (FTS5 over `name` + `keywords`), type/privacy/verified indexes

**Metadata table:** Schema version, embedding model info, last seed timestamp

### 2.2 Core Graph Operations (`memory_graph.py`)

Python module providing:

**Module exports:** Defines `__all__` for explicit public API boundaries, clarifying which functions are intended for external use vs. internal implementation details.

**Embedding:**
- `get_embedding(text)` — calls Ollama qwen3-embedding:8b (4096-dim, 75.22 MTEB score). Supports `MOCK_EMBEDDINGS=1` env var for testing (deterministic 128-dim vectors from content hash).
- `cosine_similarity(a, b)` — vector similarity (numpy-accelerated)
- Embedding cache: DB-backed, avoids redundant Ollama calls for previously seen text
- Binary packing/unpacking for SQLite storage

**Node operations:**
- `add_node()`, `get_node()`, `find_node_by_name()`, `delete_node()`
- `hard_delete_node()` — full cascading delete: removes edges, contradictions, decay_review_queue entries, dedup_log references, and the node itself
- Auto-embeds on add

**Edge operations:**
- `add_edge()`, `get_edges(node_id, direction)`
- Entity nodes created via `create_edge` bypass review (stored directly as active). Entity type inferred from relation via `_infer_entity_type()` (e.g., `works_at` → Organization, `lives_in` → Place, `has_pet` → Pet). Owner propagated from `--owner` flag.

**Search Pipeline (Batches 1-4, Feb 2026):**

The search system uses a multi-stage pipeline with RRF fusion:

1. **Intent classification** — `classify_intent(query)` categorizes as WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY/PROJECT to adjust scoring weights
2. **Parallel search** — `search_hybrid()` runs BM25 (FTS5) and semantic (cosine) concurrently via `ThreadPoolExecutor(max_workers=2)`
3. **RRF fusion** — Reciprocal Rank Fusion (k=60) combines results with dynamic fusion weights via `_get_fusion_weights(intent)`: default vector 0.7 / FTS 0.3, but WHO/WHERE/RELATION boost FTS to 0.5/0.5, WHEN boosts FTS to 0.6, PREFERENCE/WHY boost vector to 0.8, PROJECT uses 0.6/0.4 (moderate FTS boost for tech terms)
4. **Composite scoring** — Base weighted score is 60% relevance + 20% recency + 15% access frequency, then additive bonuses apply (confidence, confirmation count, temporal metadata, storage strength)
5. **Temporal validity filtering** — Expired facts penalized, future facts deprioritized
6. **MMR diversity** — Maximal Marginal Relevance (lambda=0.7) prevents redundant results
7. **Multi-hop traversal** — `get_related_bidirectional()` with depth=2, hop score decay 0.7^depth
8. **Access tracking** — `_update_access()` increments access_count and accessed_at for returned results
9. **Domain filtering** — Recall applies domain-map filters (for example `{all:true}` or `{technical:true}`), preventing unrelated domains from displacing relevant memories in rankings

**HyDE — Hypothetical Document Embedding (query expansion):**
- `route_query(query)` transforms a question into a declarative statement before embedding (e.g. "where does Alice live?" → "Alice lives in..."). The embedding of an answer is closer to stored facts in vector space than the raw question.
- Skips expansion for queries shorter than 15 characters (proper nouns, single words).
- Falls back to original query if the LLM call fails or times out.
- Controlled by `retrieval.use_hyde` config flag (default true). Disabled automatically when no embeddings provider is available.
- Configurable timeout via `retrieval.hyde_timeout_ms` (default 15000ms) and retries via `retrieval.hyde_max_retries` (default 1).

**BEAM Search (graph traversal enhancement):**
- BEAM search replaces naive BFS for graph traversal, using scored frontier expansion
- **Adaptive LLM reranking** — during BEAM traversal, an LLM reranker scores candidate nodes for relevance to the original query, pruning low-value paths early
- `scoring_mode` parameter exists on `beam_search_graph()` but is effectively **unused** in production: scoring is always adaptive (heuristic first, conditional LLM reranker when candidates exceed beam_width). The `"llm"` branch is dead code — never passed by any caller.
- BFS fallback: if BEAM search fails or encounters errors, falls back to standard BFS traversal for robustness
- **Fact quality metric** — nodes include a quality score used during BEAM expansion to prioritize high-quality facts

Key search functions:
- `search_semantic(query, ...)` — cosine similarity on qwen3-embedding:8b embeddings with bounded fallback (200 rows when 0 FTS results)
- `search_fts(query)` — BM25 via FTS5 with proper noun prioritization
- `search_hybrid()` — parallel semantic + FTS with RRF fusion and composite scoring
- `search_graph_aware(query, ...)` — enhanced search with pronoun resolution, entity detection, and bidirectional edge traversal
- Session filtering: excludes current-session memories to prevent feedback loops
- Compaction time-gate: after compaction, allows pre-compaction same-session memories back in

**Entity matching:**
- Case-insensitive entity detection in queries
- Partial/fuzzy matching for entity names to handle variations
- Owner pronoun resolution ("my", "our") maps to owner's Person node
- FTS5 sync safety net: ensures FTS index stays consistent with nodes table

**Graph traversal:**
- `get_related(node_id, relation, depth)` — BFS traversal. Top 3 results get depth-2 traversal; related nodes scored with 0.7^depth decay.
- `get_related_bidirectional(node_id, relations, depth)` — BFS traversal of BOTH inbound and outbound edges (default depth=2, max_results=20). Returns `(node, relation, direction, depth)` tuples. Early stop when max_results reached.
- **BEAM search** — scored frontier expansion with adaptive LLM reranking for higher-quality graph traversal; falls back to BFS on error.

**Edge Keywords (for graph expansion triggers):**
- `get_edge_keywords()` — retrieves all relation→keywords mappings from DB
- `get_all_edge_keywords_flat()` — cached flattened set of all keywords for fast lookup
- `store_edge_keywords(relation, keywords, description)` — stores trigger keywords for a relation type
- `generate_keywords_for_relation(relation)` — uses LLM to generate keywords for new relation types
- `invalidate_edge_keywords_cache()` — clears cache after adding new keywords

**High-level API:**
- `recall(query, limit, privacy, owner_id, current_session_id, compaction_time, date_from, date_to)` — returns results with `extraction_confidence`, `created_at`, `valid_from`, `valid_until`, `privacy`, `owner_id`, `domains`, `project`. Runs hybrid search + raw FTS on unrouted query to catch proper nouns. Results pass through `_sanitize_for_context()` which strips injection patterns from recalled text before it enters the agent's context window. Recalled facts are tagged with `[MEMORY]` prefix in output. **Date range filtering:** optional `date_from` and `date_to` parameters (YYYY-MM-DD) filter results by `created_at` date, applied before limit truncation (so limit returns N results within range). Results without dates are included by default. **Domain filtering:** results are filtered by the provided domain map when present.
- `store(text, category, verified, privacy, source, owner_id, session_id, extraction_confidence, speaker, status, keywords, source_type)` — creates nodes with dedup (auto-reject >=0.98, LLM-review gray zone 0.88-0.98, fallback threshold 0.95; FTS bounded to LIMIT 500). Validates owner is present. **Enforces 3-word minimum** for facts to prevent storing meaningless fragments. Optional `keywords` parameter stores derived search terms for FTS vocabulary bridging. Optional `source_type` (user/assistant/tool/import) stored in attributes JSON; assistant-inferred facts get 0.9x confidence multiplier.
- `store_contradiction(node_a_id, node_b_id, explanation)` — persists janitor-detected contradictions
- `forget(query, node_id)` — deletes by query or ID
- `get_stats()` — node/edge counts, type breakdown

**Recall result fields:**

Each result dict from `recall()` includes:
- `text`, `category`, `similarity`, `confidence`, `source`, `id`
- `extraction_confidence`, `created_at`, `valid_from`, `valid_until`
- `privacy`, `owner_id`
- `domains` — list of domain tags attached to the memory (for example `["technical"]`)
- `project` — project name from node attributes (if applicable)
- `_multi_pass` — whether result came from multi-pass broadened search
- Graph results additionally include: `via_relation`, `hop_depth`, `graph_path`, `direction`, `source_name`
- Co-session results include: `via_relation: "co_session"`, `hop_depth: 0`

**LLM/Embeddings provider architecture:**
- Core Quaid code is provider-agnostic. Only the adapter/provider layer and config are provider-aware.
- LLM calls route through the OpenClaw gateway adapter (`/plugins/quaid/llm`) and are resolved by model tier (`deep_reasoning`/`fast_reasoning`), not by hardcoded provider branches in core logic.
- Provider/model selection is fully config-driven via `models.llmProvider`, tier settings, and `models.fastReasoningModelClasses` / `models.deepReasoningModelClasses` in `config/memory.json`.

**Unused / dead code:**
- `datastore/memorydb/semantic_clustering.py` — groups Fact nodes into semantic buckets (people, places, preferences, technology, events) to reduce O(n²) contradiction checking. Has no production callers: no production module imports it. It is tested by `tests/test_semantic_clustering.py` but never invoked from `maintenance_ops.py` or any other runtime path. If contradiction checking at scale is needed in the future, this module provides a ready scaffold.

**Data sanitization:**
- All personal names are scrubbed from code comments, prompts, and docstrings to support safe public release
- Adapter layer includes sanitization hooks for release preparation

**Robustness (bug bash hardening):**
- Extensively hardened via 6 rounds of bug bashes: 8 bugs (stress testing) + 18 bugs (deep bug bash #1-2) + 16 bugs (deep bug bash #3) + 11 bugs (bug bash #4) + 5 bugs (bug bash #5: edge creation, retrieval filtering, metadata pass-through) + 10 bugs (L-scale bug bash: confidence flag, entity type inference, edge creation for deduped facts, owner passing, recall owner filter, dedup FTS bounds, date filter ordering, CLAWDBOT_WORKSPACE env, SQL LIKE injection) = **68 total bugs fixed**
- Defensive handling of edge cases: missing/null fields, malformed inputs, concurrent access patterns, type coercion errors, boundary conditions
- Graceful error handling throughout search and store pipelines to prevent crashes on unexpected data
- Data validation hardened across all input paths

**CLI:**
```bash
python3 memory_graph.py stats
python3 memory_graph.py search "query" --owner <user> --limit 50 \
  [--current-session-id ID] [--compaction-time ISO] \
  [--date-from YYYY-MM-DD] [--date-to YYYY-MM-DD]
python3 memory_graph.py search-graph-aware "query" --owner <user> --limit 50 --json
python3 memory_graph.py store "text" --owner <user> --category fact \
  [--confidence 0.9] [--extraction-confidence 0.9] [--session-id ID] \
  [--privacy shared] [--speaker "User"] [--status pending] \
  [--keywords "space separated search terms"]
python3 memory_graph.py forget --id <uuid>
```

**Search output format:** `[similarity] [category](date)[flags][C:confidence] text |ID:uuid|T:created_at|VF:valid_from|VU:valid_until|P:privacy|O:owner_id`

- Date shown in parentheses after category (extracted from `created_at`)
- `[superseded]` flag shown when `valid_until` is set
- `VF:` and `VU:` fields in the metadata suffix carry temporal validity timestamps

**Graph-aware search JSON output:**
```json
{
  "direct_results": [...],
  "graph_results": [{"id": "...", "name": "...", "relation": "sibling_of", "direction": "out", "source_name": "OwnerNode"}],
  "source_breakdown": {"vector_count": 5, "graph_count": 3, "pronoun_resolved": true, "owner_person": "Owner Name"}
}
```

### 2.3 Initialization and Migrations

Schema initialization and migrations are performed by `memory_graph.py` at startup (`init_database()` path), not by a separate `seed.py` script.

### 2.4 Plugin Entry Point (`adaptors/openclaw/adapter.ts`)

OpenClaw plugin (Total Recall / quaid) that:

**On load:**
- Creates `data/` dir if needed
- Initializes database/tables and runtime state if no database exists
- Loads user identity config from `config/memory.json`
- Logs stats
- **Gateway restart recovery:** Detects unextracted sessions from before the restart and auto-recovers missed memories. Checks for sessions with messages but no corresponding extraction event, then triggers extraction for each.

**Hooks:**
- `before_agent_start` — optional auto-injection pipeline (gated by config/env)
- `agent_end` — inactivity-timeout extraction (per-message classifier deprecated)
- `before_compaction` — extracts all personal facts from full transcript via Opus before context is compacted. Records compaction timestamp and resets injection dedup list. **Combined fact+edge extraction runs across transcript chunks with carry-forward context.** Enforces 3-word minimum on extracted facts. Generates derived keywords per fact for FTS vocabulary bridging. Extracts causal edges (`caused_by`, `led_to`) when causal links are clearly stated. **Also extracts soul snippets** — observations destined for core markdown files (default targets: SOUL.md, USER.md, MEMORY.md; AGENTS.md optional via config). Snippets are written to `.snippets.md` staging files for janitor review (Task 1d).
- `before_reset` — same extraction as compaction, triggered on `/new` or `/reset`

**LLM timeouts:**
- LLM call timeouts set to 600s to accommodate async janitor workloads and long-running extraction tasks

**LLM call routing:**
- LLM calls can be routed through a gateway proxy for OAuth authentication, enabling centralized credential management across plugin operations

**Extraction notifications:**
- User receives notification when facts are extracted during compaction/reset
- Notification includes detailed listing of each extracted fact
- Format: "📝 Memory extraction complete: X facts stored" followed by bullet list

**Session ID:**
- Prefers `ctx.sessionId` (gateway UUID) from the Clawdbot plugin context
- Falls back to MD5 hash of first message timestamp for backward compat

**User ID Resolution:**
- Loads `config/memory.json` with user identity mappings
- `resolveOwner(speaker, channel)` — matches speaker name or channel to configured user IDs
- Falls back to `defaultOwner` from config
- `personNodeName` field maps user to their Person node in the graph

**Tools registered:**
- `memory_recall` — search memories with **dynamic retrieval limit K** (see below), uses graph-aware search. **Waits for in-flight extraction** (up to 60s timeout) before querying, ensuring freshly extracted facts from compaction/reset are immediately queryable. Uses a strict `query + options` contract (`options.graph`, `options.routing`, `options.filters`, `options.ranking`, `options.datastoreOptions`) including date filtering via `options.filters.dateFrom`/`options.filters.dateTo`. Results include dates showing when each fact was recorded, and `[superseded]` markers for facts with `validUntil` set.
- `memory_store` — queues a memory note for deferred extraction on compaction/reset (no immediate DB write)
- `memory_forget` — delete by query or ID
- `projects_search` — RAG search with optional `project` filter + staleness warnings
- `docs_list` — list docs by project/type via registry
- `docs_read` — read doc by path or title
- `docs_register` — register doc to project

**Knowledge-store registry (core-owned):**
- Store metadata and default selection now live in `core/knowledge-stores.ts` (`.js` runtime pair).
- Orchestrator consumes that registry for normalization/routing defaults instead of hardcoded store lists.
- Adapter consumes registry-rendered guidance text for `memory_recall` tool instructions so store docs stay aligned with runtime behavior.

**Extraction prioritization (bug-bash update):**
- Prompt now enforces explicit priority order:
  1. user facts
  2. agent actions/suggestions
  3. technical/project-state facts
- Guardrail: agent/technical extraction must not reduce user-fact coverage.
- `project_create` — scaffold new project with PROJECT.md

**Dynamic retrieval limit K:**

The `memory_recall` tool computes the retrieval limit dynamically based on the total number of nodes in the graph, rather than using a fixed default. The formula is:

```
K = round(11.5 * ln(N) - 61.7)
```

where `N` is the total node count. The result is clamped to runtime bounds [5, 40]. This ensures the retrieval window scales logarithmically as the memory graph grows — small graphs don't over-fetch, and large graphs don't under-fetch. The dynamic K is used as the default when the agent doesn't specify an explicit limit. Node count is cached for 5 minutes and refreshed periodically.

**TypeScript strictness:**
- All `catch` clauses use explicit `err: unknown` typing (Wave 1 TS lint cleanup)
- Unused error variables use bare `catch` without binding

**Python bridge:**
- Spawns `memory_graph.py` commands via child process
- For graph-aware search: parses JSON output with `direct_results`, `graph_results`, and `source_breakdown`
- Legacy format: `[sim] [cat](date)[flags][C:conf] text |ID:id|T:created|VF:valid_from|VU:valid_until|P:privacy|O:owner` (updated to include temporal validity fields)

**Result types:**
- `MemoryResult` includes `relation`, `direction`, `sourceName`, `via`, `validFrom`, and `validUntil` fields for graph results and temporal validity tracking
- `via: "vector"` for semantic/FTS matches, `via: "graph"` for edge-traversal results

### 2.5 Memory Recall — Agent-Driven (2026-02-06)

> **Auto-injection is optional.** The `before_agent_start` handler runs only when enabled via config/env. Memory recall remains agent-driven via the `memory_recall` tool.

**Why optional:** Automatic injection on every message can produce low-quality matches. Short messages like "ok", "B", "yes" have meaningless embeddings that match random facts. The agent understands context better than any heuristic.

**New approach:**
1. Agent sees user message mentioning people/relationships/preferences
2. Agent decides to call `memory_recall` with a crafted query
3. Agent uses specific names and topics for queries
4. Graph expansion via `options.graph.expand: true` for relationship queries
5. Date range filtering via `options.filters.dateFrom`/`options.filters.dateTo` for time-scoped queries
6. User gets notification showing what was retrieved

**Tool guidance in description:**
- USE WHEN: User mentions people, asks about preferences/history, references past events
- SKIP WHEN: Pure coding tasks, general knowledge, short acks
- QUERY TIPS: Use entity names, add context, be specific. For project technical details, memory_recall can help; use projects_search for architecture/reference docs.
- `options.filters.dateFrom`/`options.filters.dateTo`: Use YYYY-MM-DD format to filter memories by date range

**Result quality signals:**
- Similarity % for each match
- `extraction_confidence` showing classifier reliability
- Date of recording shown per result
- `[superseded]` marker on facts that have been replaced
- Low-quality warning when `avg_similarity < 45%` AND `max_similarity < 55%`
- "No results" guidance to refine query

**To enable/disable auto-injection:** Set `MEMORY_AUTO_INJECT=1` env var or `retrieval.autoInject: true/false` in config.

### 2.5b Auto-Injection Pipeline (Optional)

> The following describes the automatic injection pipeline used when auto-inject is enabled.

The injection pipeline runs before agent start, filtering and reranking memories before injection. Agent-driven recall via the `memory_recall` tool remains the primary path.

### 2.6 Auto-Capture (agent_end) — Inactivity Timeout

> **Per-message classifier is deprecated.** The inactivity-timeout extraction remains active when capture is enabled.

The previous per-message approach (Haiku classifier on each message pair) was:
- Expensive: called on every `agent_end`, including heartbeats
- Low context: only saw the last user + assistant message pair
- Noisy: extracted many system/infrastructure facts that required janitor cleanup

**Current extraction** happens in `before_compaction` and `before_reset` hooks via Opus (full transcript), plus inactivity-timeout extraction when enabled. **Combined fact+edge extraction** performs both fact extraction and relationship detection in a chunked deep-reasoning loop for efficiency and context-window safety.

Capture timeout config keys:
- `capture.inactivity_timeout_minutes` (default `120`): minutes of inactivity before timeout extraction runs (`0` disables timeout extraction).
- `capture.auto_compaction_on_timeout` (default `true`): when timeout extraction runs, whether to trigger gateway compaction automatically after extraction.

### 2.7 Shared Library (`lib/`)

Centralized modules extracted from duplicated code across `datastore/memorydb/memory_graph.py`, `datastore/docsdb/rag.py`, and `core/lifecycle/janitor.py`:

| Module | Purpose |
|--------|---------|
| `lib/adapter.py` | Platform adapter layer — `QuaidAdapter` ABC with `StandaloneAdapter` (`~/quaid/`) and `OpenClawAdapter`. All modules route through `get_adapter()` for paths, notifications, credentials, sessions. Adapter selection is config-driven via `config/memory.json` (`adapter.type`). `QUAID_HOME`/`CLAWDBOT_WORKSPACE` are path hints for locating config. Tests use `set_adapter()`/`reset_adapter()` for isolation. **Includes sanitization hooks** for scrubbing personal data during release preparation. |
| `lib/config.py` | DB paths, Ollama URL, embedding params — reads from `config/memory.json` via `config.py`. Env var overrides: `MEMORY_DB_PATH`, `MEMORY_ARCHIVE_DB_PATH`, `OLLAMA_URL`. Path resolution delegated to `lib/adapter.py`. |
| `lib/database.py` | `get_connection(db_path)` — SQLite factory with Row + FK enforcement |
| `lib/embeddings.py` | `get_embedding(text)`, `pack_embedding()`, `unpack_embedding()` — embedding calls routed through configured provider (Ollama by default) |
| `lib/similarity.py` | `cosine_similarity(a, b)` — vector comparison |
| `lib/tokens.py` | `extract_key_tokens(text)` — noun/name extraction for dedup recall |
| `lib/batch_utils.py` | Batch processing utilities for LLM calls. Two patterns: **parallel batching** (`parallel_batch`) — splits items into token-sized chunks processed concurrently; **waterfall batching** (`waterfall_batch`) — serial cascading distillation where each batch's output is carryover context for the next. Also exports `chunk_by_tokens`, `chunk_text_by_tokens`, `ChunkResult`, `DEFAULT_CHUNK_TOKENS` (8000 tokens). Truncation is banned; these helpers enforce that invariant. |
| `lib/instance.py` | Zero-dependency instance resolution. Reads `QUAID_HOME` and `QUAID_INSTANCE` env vars only. Public API: `quaid_home()`, `instance_id()`, `instance_root()`, `shared_dir()`, `shared_projects_dir()`, `shared_registry_path()`, `shared_config_path()`, `list_instances()`, `instance_exists()`, `require_instance_exists()`, `validate_instance_id()`. Instance names must match `[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}` and not be reserved words. |
| `lib/domain_text.py` | Domain ID and description normalization. `normalize_domain_id(value)` lowercases, strips non-alphanumeric chars, applies aliases (`"projects"` → `"project"`, `"family"` → `"personal"`), and returns `None` on invalid input. `sanitize_domain_description(value, *, max_chars=200, allow_truncate=False)` normalizes unicode, strips control characters, and raises `ValueError` when the text exceeds `max_chars` (default behavior). **The default is `allow_truncate=False` — callers must not rely on silent trimming.** Pass `allow_truncate=True` only when reading pre-existing DB rows that may predate the 200-char limit. Also raises `ValueError` on descriptions containing injection-like patterns (e.g., "ignore all previous instructions", "system prompt"). |
| `datastore/memorydb/archive_store.py` | `archive_node(node, reason)` / `search_archive(query)` — datastore-owned archive writes/reads for `data/memory_archive.db` (`lib/archive.py` remains a compatibility shim). |
| `lib/fail_policy.py` | `is_fail_hard_enabled()` — reads `retrieval.fail_hard` from config; defaults to `True` when config is unavailable. Used throughout the codebase to gate fallback behavior. Never implement fallback-switching via env vars in product flows — use this helper. |
| `lib/providers.py` | Provider ABCs and concrete LLM/embeddings implementations. Concrete LLM providers: `AnthropicLLMProvider` (direct API key), `ClaudeCodeLLMProvider` (wraps `claude -p` CLI / subscription), `TestLLMProvider` (canned responses). Embeddings: `OllamaEmbeddingsProvider` (local Ollama), `MockEmbeddingsProvider` (MD5 deterministic vectors). All providers share `LLMResult` dataclass. Platform adapters produce provider instances; callers never instantiate providers directly. |
| `lib/llm_clients.py` | Unified LLM call interface for janitor and workspace tasks. Key functions: `call_fast_reasoning(prompt, ...)`, `call_deep_reasoning(prompt, ...)`, `parse_json_response(text)` (strips markdown fences). LLM calls are routed through the adapter's `LLMProvider` — no API key management here. Token usage accumulator (`_usage_input_tokens`, `_usage_output_tokens`, `_usage_calls`) resets per janitor run; read at end for cost reporting. Timeout env var overrides: `QUAID_DEEP_REASONING_TIMEOUT` (default 600s), `QUAID_FAST_REASONING_TIMEOUT` (default 120s). |
| `lib/worker_pool.py` | Shared `ThreadPoolExecutor` registry. `run_callables(callables, *, max_workers, pool_name, timeout_seconds, return_exceptions)` — parallel execution with deterministic output ordering. Named pools are cached (created once, reused across calls). `shutdown_worker_pools()` registered via `atexit`. Used by search and lifecycle prepass for bounded parallelism outside of the LLM scheduler. |
| `lib/delayed_requests.py` | Delayed-request queue for passive user-facing notifications. Datastore and core callers enqueue items without importing runtime event modules. Queue persisted to `.quaid/runtime/notes/delayed-llm-requests.json` under the workspace dir. Provides file-lock-safe enqueue/dequeue. Used to deliver janitor summaries after the LLM session returns. |
| `lib/runtime_context.py` | Runtime context port that isolates adapter access behind a single module. Lifecycle, datastore, and ingestor code imports path/provider/session helpers from here rather than touching adapter internals directly. Key functions: `get_workspace_dir()` (active instance root), `get_data_dir()`, `get_logs_dir()`, `get_llm_provider()`, `get_last_channel()`, `send_notification()`, `get_install_url()`. |

All shared library modules use `__all__` exports to define explicit public API boundaries, ensuring clean module interfaces and preventing accidental coupling to internal helpers.

**Provider/adapter pattern:** LLM and embedding functionality is routed through adapters/providers. The core memory pipeline does not choose providers directly; it requests `deep_reasoning` or `fast_reasoning` and the adapter resolves provider + model from config and gateway state.

### 2.8 Ingest Runtime Bridge

**`core/ingest_runtime.py`:** Runtime-safe ingest entrypoints that keep core decoupled from ingest module internals. Three public functions:
- `run_docs_ingest(transcript_path, label, session_id)` — triggers docs ingest pipeline
- `run_session_logs_ingest(*, session_id, owner_id, label, ...)` — records session log metadata
- `run_extract_from_transcript(*, transcript, owner_id, label, dry_run)` — runs fact extraction on a transcript string

Core orchestrators import ingest via this bridge rather than importing `ingest.*` modules directly.

### 2.9 Global LLM Scheduler

**`core/llm/scheduler.py`:** Singleton `GlobalLlmScheduler` that centralizes timeout-driven throttling and adaptive concurrency for all LLM-parallel call sites. Key behaviors:
- `run_map(workload_key, items, fn, configured_workers, ...)` — parallel map with per-workload adaptive concurrency caps
- **Timeout + backoff:** On timeout, halves the worker cap for that workload key and retries remaining items (up to `timeout_retries` attempts; raises `TimeoutError` after exhaustion)
- **Slow release:** After successful completion, increments the cap back toward `configured_workers` by 1
- `QUAID_GLOBAL_LLM_MAX_WORKERS` env var — overrides the global thread pool ceiling (default 32)
- Singleton access via `get_global_llm_scheduler()`; `reset_global_llm_scheduler()` shuts down and clears it (called at process exit via `atexit`)

### 2.10 Log Rotation

**`core/log_rotation.py`:** Token-budget-based rotation for append-only log files (`PROJECT.log`, journal entries). Never splits an entry; always keeps at least the most recent entry.
- `rotate_log_file(log_file, archive_dir, token_budget)` — keeps recent entries within the budget, archives older entries into monthly files under `log/<YYYY-MM>.log`. Returns `(archived_count, kept_count)`.
- `rotate_project_logs(projects_dir, **kwargs)` — rotates `PROJECT.log` for all projects under `QUAID_HOME/projects/`. Call after distillation.
- `rotate_journal_logs(journal_dir, **kwargs)` — rotates `*.journal.md` files; archives into `journal/archive/<stem>/`.
- Default token budget: 4000 tokens (configurable via `projects.logTokenBudget` in config). Token estimate uses ~4 chars/token (conservative). Rotation is triggered after distillation, not on daemon ticks.

### 2.11 Datastore Facade

**`datastore/facade.py`:** Narrow re-export surface for non-datastore modules (adapter, CLI, tests). Exposes `store_memory`, `recall_memories`, `recall_memories_fast`, `search_memories`, `datastore_stats`, `list_memory_domains`, `register_memory_domain`, `forget_memory`, `get_memory_by_id`, `create_edge` — all delegating to `datastore.memorydb.memory_graph`. Janitor and datastore-owned maintenance routines import datastore internals directly; only external callers use this facade.

### 2.12 Subagent Registry

**`core/subagent_registry.py`:** Tracks parent/child session relationships so subagent transcripts merge into the parent session's extraction. Adapter-agnostic — both Claude Code and OpenClaw write via lifecycle hooks.
- Storage: JSON files per parent session under `QUAID_HOME/data/subagent-registry/`; file-locked for concurrent write safety.
- `register(parent_session_id, child_id, child_transcript_path, child_type, metadata)` — called on `SubagentStart` (CC) / `subagent_spawned` (OC)
- `mark_complete(parent_session_id, child_id, transcript_path)` — called on `SubagentStop` / `subagent_ended`; late registration handled inline
- `get_harvestable(parent_session_id)` — returns completed, un-harvested children with transcript paths
- `mark_harvested(parent_session_id, child_id)` — stamps `harvested_at` after extraction
- `is_registered_subagent(session_id)` — scans all registries; used by the daemon to suppress standalone timeout extraction for registered subagents
- `cleanup_old_registries(max_age_hours=48)` — removes stale registry files

### 2.13 Compatibility + Circuit Breaker

**`core/compatibility.py`:** Watches the host platform version and disables Quaid operations when an incompatible host is detected. Three circuit breaker states: `normal`, `degraded` (extraction/storage disabled, recall works), `safe_mode` (all operations disabled).
- `VersionWatcher` — daemon-integrated class; `tick()` does a cheap mtime check on the host binary, triggers full version check when mtime changes or per adaptive interval. On state change, writes `circuit-breaker.json` and notifies the user. Adaptive check intervals: 24h (normal/compatible), 1h (untested or safe_mode), 6h (degraded).
- `JanitorScheduler` — daemon-owned janitor scheduling; replaces external cron. Runs janitor at a configured hour (default 4am) within a configurable window. Catch-up logic: if checkpoint is >24h old, runs regardless of window. Config keys: `janitor.scheduled_hour`, `janitor.window_hours`. Skips if circuit breaker disallows writes.
- `read_circuit_breaker(data_dir)` / `write_circuit_breaker(data_dir, state)` / `clear_circuit_breaker(data_dir)` — file-based state persistence at `data/circuit-breaker.json`
- `check_write_allowed(data_dir)` / `check_read_allowed(data_dir)` — entry-point guards for critical operations
- `evaluate_compatibility(host_info, quaid_version, matrix)` — evaluates against the compatibility matrix (fetched from GitHub, cached at `data/compatibility-matrix.json`). Global kill switch in matrix triggers `safe_mode` immediately.
- `preflight_compatibility_check(host_platform, host_version, quaid_version)` — pre-install check for install scripts; returns `{ok, status, message, fix}` dict
- `notify_on_use_if_degraded(data_dir)` — session-init guard; emits once-per-30-min warning when state is degraded/safe_mode

### 2.14 Post-Extraction Docs Hook

**`core/docs_updater_hook.py`:** Runs after extraction to update project docs (`TOOLS.md`, `AGENTS.md`) based on shadow git diffs. Uses a classify → gate → update pipeline to avoid unnecessary LLM calls.
- `update_project_docs(snapshots, extraction_result, dry_run)` — entry point; iterates over project snapshots from `project_registry.snapshot_all_projects()`. Trivial diffs (confidence ≥ 0.7) are skipped without LLM. Borderline cases gated by `call_fast_reasoning`. Significant changes updated by `call_deep_reasoning`.
- Edit format: `<<<EDIT\nSECTION: ...\nOLD: ...\nNEW: ...\n>>>` blocks; parsed and applied via `datastore.docsdb.updater.apply_edit_blocks`.
- Metrics returned: `projects_checked`, `docs_updated`, `docs_skipped`, `trivial_skipped`, `errors`.

### 2.15 Soul Snippets and Journal

**`datastore/notedb/soul_snippets.py`:** Dual extraction system producing both fast-path snippets and slow-path journal entries at compaction/reset.
- **Snippets (fast path):** Bullet-point observations written to `*.snippets.md` staging files in the identity dir. Nightly janitor reviews each snippet with `FOLD` (integrate into core file), `REWRITE` (synthesize), or `DISCARD` decisions. Keeps `SOUL.md`, `USER.md`, `MEMORY.md` current day-to-day. Target files configurable; `AGENTS.md` is optional via config.
- **Journal (slow path):** Diary-style paragraphs written to `journal/*.journal.md`. Opus distillation runs weekly, synthesizing themes into core markdown. Old journal entries archived monthly.
- Entry points: `run_soul_snippets_review()` — nightly snippet FOLD/REWRITE/DISCARD (janitor Task 1d-snippets); `run_journal_distillation()` — weekly Opus distillation (janitor Task 1d-journal).
- Protected regions in core markdown files are skipped during writes (via `lib/markdown.strip_protected_regions`).

### 2.16 Plugin Contract Architecture

**`core/contracts/`, `core/plugins/`:** Defines the protocol surfaces that all datastores implement.
- `core/contracts/plugin_contract.py` — `PluginContractBase` ABC with seven executable surfaces: `on_init`, `on_config`, `on_status`, `on_dashboard`, `on_maintenance`, `on_tool_runtime`, `on_health`.
- `core/contracts/memory.py` — `MemoryServicePort` Protocol (structural typing) defining the store/recall/search/create_edge/forget/stats/domain API that all memory service implementations must satisfy.
- `core/plugins/memorydb_contract.py` — MemoryDB contract. Notable: domain lifecycle (schema/table sync and TOOLS domain block sync) is datastore-owned and implemented here, invoked by core plugin contract execution. Uses `lib/domain_runtime.publish_domains_to_runtime_config` and `lib/tools_domain_sync.sync_tools_domain_block`.
- `core/plugins/docsdb_contract.py` — DocsDB contract; handles docs workspace init (`projects/`, `temp/`, `scratch/` directory initialization).
- `core/plugins/notedb_contract.py` — NoteDB contract; minimal stub (all hooks return `ready: True`; dashboard disabled).
- `core/services/memory_service.py` — `DatastoreMemoryService` class implementing `MemoryServicePort`; core-side composition point wrapping `datastore.facade` behind identity enforcement (`identity_runtime` assertion, privacy policy, write contract).

---

## 3. Schema Reference

> Auto-generated from `modules/quaid/datastore/memorydb/schema.sql` — 2026-02-27
> Schema version: 6 | Embedding model: qwen3-embedding:8b (4096-dim)

### 3.1 Nodes — All Memory Entities

```sql
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,                    -- UUID
    type TEXT NOT NULL,                     -- Person, Place, Project, Event, Fact, Preference, Concept
    name TEXT NOT NULL,                     -- Display name / main content
    attributes TEXT DEFAULT '{}',           -- JSON blob for type-specific data
    embedding BLOB,                         -- 4096-dim float32 array (qwen3-embedding:8b)

    -- Verification and quality
    verified INTEGER DEFAULT 0,             -- 0=auto-extracted, 1=user confirmed (slower decay)
    pinned INTEGER DEFAULT 0,              -- 1=core facts that never decay
    confidence REAL DEFAULT 0.5,            -- 0-1 confidence score
    source TEXT,                            -- Where this came from (file, message, etc.)
    source_id TEXT,                         -- Message ID or file path

    -- Multi-user support
    owner_id TEXT,                          -- Who owns this memory (null = shared/legacy)
    actor_id TEXT,                          -- Canonical entity that asserted/performed this memory event
    subject_entity_id TEXT,                 -- Canonical entity this memory is primarily about

    -- Privacy tiers
    privacy TEXT DEFAULT 'shared' CHECK(privacy IN ('private', 'shared', 'public')),

    -- Session and provenance tracking
    session_id TEXT,                        -- Session where this memory was created (for dedup)
    source_channel TEXT,                    -- Channel/source type (telegram/discord/slack/dm/etc.)
    source_conversation_id TEXT,            -- Stable conversation/thread/group identifier
    source_author_id TEXT,                  -- External speaker/author handle or ID
    speaker_entity_id TEXT,                 -- Canonical entity that produced the source utterance
    conversation_id TEXT,                   -- Canonical conversation/thread identifier (normalized)
    visibility_scope TEXT DEFAULT 'source_shared', -- private_subject/source_shared/global_shared/system
    sensitivity TEXT DEFAULT 'normal',      -- normal/restricted/secret
    provenance_confidence REAL DEFAULT 0.5, -- confidence in ownership/attribution chain

    -- Classification
    fact_type TEXT DEFAULT 'unknown',       -- Subcategory (e.g. financial, health, family)
    knowledge_type TEXT DEFAULT 'fact' CHECK(knowledge_type IN ('fact', 'belief', 'preference', 'experience')),
    extraction_confidence REAL DEFAULT 0.5, -- 0-1: how confident the classifier was
    speaker TEXT,                           -- Who stated this fact (e.g. "Quaid", "Hauser")

    -- Temporal validity
    valid_from TEXT,                        -- ISO8601 datetime
    valid_until TEXT,                       -- ISO8601 datetime (null = still valid)

    -- Content integrity
    content_hash TEXT,                      -- SHA256 of name text (fast exact-dedup pre-filter)
    superseded_by TEXT,                     -- ID of node that replaced this one (fact versioning)
    keywords TEXT,                          -- Space-separated derived search terms (generated at extraction)

    -- Lifecycle
    status TEXT DEFAULT 'approved',         -- pending → approved → active
    deleted_at TEXT DEFAULT NULL,           -- Legacy (unused, kept for migration compat)
    deletion_reason TEXT DEFAULT NULL,      -- Legacy (unused, kept for migration compat)

    -- Metadata
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    accessed_at TEXT DEFAULT (datetime('now')),
    access_count INTEGER DEFAULT 0,
    storage_strength REAL DEFAULT 0.0,       -- Bjork: cumulative encoding strength (never decreases)
    confirmation_count INTEGER DEFAULT 0,    -- How many times re-confirmed by extraction
    last_confirmed_at TEXT                   -- When last confirmed
);
```

**Key columns:**
- `pinned=1` — core facts immune to decay
- `status` — lifecycle: `pending` → `approved` (via janitor review) → `active` (via graduation)
- `privacy` — defaults to `'shared'`, NOT `'private'`
- `owner_id` — all facts should have an owner; null is legacy data
- `session_id` — used for dedup (skip facts from same session)
- `knowledge_type` — epistemological classification: `fact`, `belief`, `preference`, or `experience`; defaults to `'fact'`
- `content_hash` — SHA-256 of normalized content; enables O(1) exact-match dedup before embedding comparison
- `superseded_by` — fact versioning; points to the node that replaced this one
- `confirmation_count` — incremented each time extraction re-confirms this fact; used to boost confidence
- `last_confirmed_at` — timestamp of most recent re-confirmation; useful for staleness checks
- `keywords` — space-separated derived search terms generated at extraction time; indexed by FTS5 for vocabulary bridging (e.g., "health stomach gastric" for a fact about digestive symptoms)

**Attributes JSON blob** (`attributes` column) may contain:
- `source_type` — `"user"` / `"assistant"` / `"tool"` / `"import"` — indicates how the fact was obtained. Assistant-inferred facts get a 0.9x confidence multiplier at store time. Used by janitor review for differential trust policies.

**Special node types:**
- Standard: `Person`, `Place`, `Project`, `Event`, `Fact`, `Preference`, `Concept`, `Organization`
- `resolution_summary` — Created by the janitor MERGE operation. Contains consolidated text from two or more merged facts. Linked to the originals via `resolved_from` edges for provenance.

**Special edge relations:**
- `resolved_from` — Links a `resolution_summary` node back to the original facts that were merged to create it. Direction: `resolution_summary --resolved_from--> original_fact`.

### 3.2 Edges — Relationships Between Nodes

```sql
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,                    -- UUID
    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,                 -- lives_at, works_on, knows, owns, prefers, etc.
    attributes TEXT DEFAULT '{}',           -- JSON for relationship metadata
    weight REAL DEFAULT 1.0,               -- Relationship strength
    source_fact_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,  -- Fact that created this edge

    -- Temporal
    valid_from TEXT,
    valid_until TEXT,

    -- Metadata
    created_at TEXT DEFAULT (datetime('now')),

    UNIQUE(source_id, target_id, relation)
);
```

**Key constraints:**
- `ON DELETE CASCADE` on source_id/target_id — deleting a node auto-deletes its edges
- `ON DELETE SET NULL` on source_fact_id — deleting source fact preserves edge but clears link
- `UNIQUE(source_id, target_id, relation)` — one edge per direction per relation type; use INSERT OR REPLACE

**Edge normalization** (enforced by janitor):
- Inverse map: `child_of` → `parent_of`, `led_to`/`caused`/`resulted_in`/`triggered` → `caused_by` (FLIP direction)
- Synonym map: `mother_of` → `parent_of`, `because_of`/`due_to` → `caused_by` (SAME direction)
- Symmetric relations: alphabetical ordering (e.g. `sibling_of(A,B)` where A < B)

### 3.3 FTS5 — Full-Text Search

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name,
    keywords,
    content='nodes',
    content_rowid='rowid',
    tokenize='porter unicode61'
);
```

**Sync triggers** keep FTS in sync with nodes (both `name` and `keywords` columns):
```sql
CREATE TRIGGER nodes_ai AFTER INSERT ON nodes ...   -- INSERT INTO nodes_fts(rowid, name, keywords)
CREATE TRIGGER nodes_ad AFTER DELETE ON nodes ...    -- DELETE via rowid
CREATE TRIGGER nodes_au AFTER UPDATE ON nodes ...    -- DELETE old + INSERT new (name, keywords)
```

The `keywords` column enables vocabulary bridging — FTS queries can match derived terms not present in the original fact text (e.g., searching "health problems" can match a fact with keywords "health stomach gastric medical").

Used by `search_fts()` for keyword matching. **Does NOT filter by `owner_id`** — caller must post-filter.

### 3.4 Embedding Cache — Cached Embedding Vectors

```sql
CREATE TABLE IF NOT EXISTS embedding_cache (
    text_hash TEXT PRIMARY KEY,             -- SHA-256 of the input text
    embedding BLOB NOT NULL,                -- Cached embedding vector (float32 array)
    model TEXT NOT NULL,                    -- Model used to generate embedding
    created_at TEXT DEFAULT (datetime('now'))
);
```

Avoids redundant embedding API calls for identical text. Keyed on `text_hash` (SHA-256 of raw input text) + `model` for cache invalidation on model changes.

### 3.5 Entity Aliases — Fuzzy Name Matching

```sql
CREATE TABLE IF NOT EXISTS entity_aliases (
    id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,           -- The alternate name (e.g., "Sol", "Mom")
    canonical_name TEXT NOT NULL,  -- The canonical name (e.g., "Douglas Quaid")
    canonical_node_id TEXT,        -- Optional: link to the Person/entity node
    owner_id TEXT,                 -- Owner who defined this alias
    entity_id TEXT,                -- Canonical identity entity ID
    platform TEXT,                 -- telegram/discord/openclaw/etc.
    source_id TEXT,                -- Canonical source/group/thread scope
    handle TEXT,                   -- Source-native handle
    display_name TEXT,             -- Human-facing display name for this alias
    confidence REAL DEFAULT 1.0,   -- Alias link confidence
    updated_at TEXT DEFAULT (datetime('now')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(alias, owner_id)
);
```

Maps alternate names and nicknames to canonical entity names. `canonical_node_id` optionally links to the corresponding Person/entity node. Scoped per `owner_id` — the same alias can map to different canonical names for different users.

### 3.6 Domain Registry and Node Mapping

```sql
CREATE TABLE IF NOT EXISTS domain_registry (
    domain TEXT PRIMARY KEY,
    description TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS node_domains (
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    domain TEXT NOT NULL REFERENCES domain_registry(domain) ON DELETE RESTRICT,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (node_id, domain)
);
```

Domain tags are normalized through `domain_registry` and attached to each node via `node_domains`.

### 3.7 Identity and Source Model

```sql
CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL DEFAULT 'unknown'
        CHECK(entity_type IN ('human', 'agent', 'org', 'system', 'unknown')),
    canonical_name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL DEFAULT 'dm'
        CHECK(source_type IN ('dm', 'group', 'thread', 'workspace')),
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    parent_source_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_participants (
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'member'
        CHECK(role IN ('member', 'owner', 'agent', 'observer')),
    active_from TEXT DEFAULT (datetime('now')),
    active_to TEXT,
    PRIMARY KEY (source_id, entity_id, active_from)
);

CREATE TABLE IF NOT EXISTS identity_handles (
    id TEXT PRIMARY KEY,
    owner_id TEXT,
    source_channel TEXT NOT NULL,
    conversation_id TEXT,
    handle TEXT NOT NULL,
    canonical_entity_id TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(owner_id, source_channel, conversation_id, handle)
);

CREATE TABLE IF NOT EXISTS identity_credentials (
    credential_id TEXT PRIMARY KEY,
    entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    credential_type TEXT NOT NULL DEFAULT 'api_token'
        CHECK(credential_type IN ('api_token', 'oauth', 'pubkey', 'password', 'external')),
    key_id TEXT,
    metadata TEXT DEFAULT '{}',
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS identity_sessions (
    session_auth_id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
    entity_id TEXT REFERENCES entities(entity_id) ON DELETE SET NULL,
    authn_method TEXT NOT NULL DEFAULT 'unknown',
    trust_level INTEGER NOT NULL DEFAULT 0,
    issued_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS delegation_grants (
    grant_id TEXT PRIMARY KEY,
    grantor_entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    grantee_entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    scope TEXT NOT NULL DEFAULT 'none',
    constraints TEXT DEFAULT '{}',
    issued_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS trust_assertions (
    assertion_id TEXT PRIMARY KEY,
    source_entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    assertion_type TEXT NOT NULL DEFAULT 'identity_link'
        CHECK(assertion_type IN ('identity_link', 'trust_delegate', 'ownership')),
    confidence REAL DEFAULT 0.5,
    evidence TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS policy_audit_log (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT DEFAULT (datetime('now')),
    policy_name TEXT NOT NULL,
    decision_action TEXT NOT NULL,
    viewer_entity_id TEXT,
    row_id TEXT,
    context TEXT DEFAULT '{}'
);
```

### 3.8 Contradictions — Detected Conflicting Facts

```sql
CREATE TABLE IF NOT EXISTS contradictions (
    id TEXT PRIMARY KEY,
    node_a_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    node_b_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    explanation TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'resolved', 'false_positive')),
    resolution TEXT,           -- 'keep_a', 'keep_b', 'keep_both', 'merge'
    resolution_reason TEXT,
    detected_at TEXT DEFAULT (datetime('now')),
    resolved_at TEXT,
    UNIQUE(node_a_id, node_b_id)
);
```

### 3.9 Dedup Log — Tracks Dedup Decisions for Review

```sql
CREATE TABLE IF NOT EXISTS dedup_log (
    id TEXT PRIMARY KEY,
    new_text TEXT NOT NULL,
    existing_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
    existing_text TEXT NOT NULL,
    similarity REAL NOT NULL,
    decision TEXT NOT NULL,  -- auto_reject, llm_reject, llm_accept, fallback_reject
    llm_reasoning TEXT,
    review_status TEXT DEFAULT 'unreviewed'
        CHECK(review_status IN ('unreviewed', 'confirmed', 'reversed')),
    review_resolution TEXT,
    reviewed_at TEXT,
    owner_id TEXT,
    source TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

**Note:** Column is `existing_node_id`, NOT `kept_id` or `removed_id`.

### 3.10 Decay Review Queue — Low-Confidence Facts Queued for Review

```sql
CREATE TABLE IF NOT EXISTS decay_review_queue (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    node_text TEXT NOT NULL,
    node_type TEXT,
    confidence_at_queue REAL NOT NULL,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    verified INTEGER DEFAULT 0,
    created_at_node TEXT,
    decision TEXT,  -- null (pending), delete, extend, pin
    decision_reason TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'reviewed')),
    queued_at TEXT DEFAULT (datetime('now')),
    reviewed_at TEXT
);
```

### 3.11 Recall Log — Recall Observability

```sql
CREATE TABLE IF NOT EXISTS recall_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    owner_id TEXT,
    intent TEXT,                          -- WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY/PROJECT
    results_count INTEGER,
    avg_similarity REAL,
    top_similarity REAL,
    multi_pass_triggered INTEGER DEFAULT 0,
    fts_fallback_used INTEGER DEFAULT 0,
    reranker_used INTEGER DEFAULT 0,
    reranker_changes INTEGER DEFAULT 0,   -- How many results changed position
    reranker_top1_changed INTEGER DEFAULT 0,  -- Whether #1 result changed after reranking
    reranker_avg_displacement REAL,           -- Average absolute rank shift
    graph_discoveries INTEGER DEFAULT 0,
    latency_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
```

Tracks every `recall()` call for observability. Used by the dashboard and janitor cleanup (Task 9 prunes entries older than 90 days).

### 3.12 Health Snapshots — Periodic DB Health Metrics

```sql
CREATE TABLE IF NOT EXISTS health_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    total_nodes INTEGER,
    total_edges INTEGER,
    avg_confidence REAL,
    nodes_by_status TEXT,          -- JSON
    confidence_distribution TEXT,  -- JSON (buckets: 0.0-0.3, 0.3-0.5, 0.5-0.7, 0.7-0.9, 0.9-1.0)
    staleness_distribution TEXT,   -- JSON (0-7d, 7-30d, 30-90d, 90d+)
    orphan_count INTEGER,
    embedding_coverage REAL,      -- percentage
    created_at TEXT DEFAULT (datetime('now'))
);
```

Stores periodic snapshots of knowledge base health indicators. Written by the janitor after each run. Tracks node counts, edge counts, average confidence, status distribution, staleness distribution, orphan counts, and embedding coverage. Pruned by Task 9 (entries older than 180 days).

### 3.13 Metadata — System State

```sql
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Seeded values:
-- schema_version = '6'
-- embedding_model = 'qwen3-embedding:8b'
-- embedding_dim = '4096'
```

### 3.14 Janitor Metadata and Runs

Created by `datastore/memorydb/maintenance_ops.py` `init_janitor_metadata()` — NOT in schema.sql.

```sql
CREATE TABLE IF NOT EXISTS janitor_metadata (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Used by the janitor to persist key-value state across runs (e.g. last workspace check hash).

```sql
CREATE TABLE IF NOT EXISTS janitor_runs (
    id INTEGER PRIMARY KEY,
    task_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    memories_processed INTEGER DEFAULT 0,
    actions_taken INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',       -- running, completed, failed
    skipped_tasks_json TEXT,
    carryover_json TEXT,
    stage_budget_json TEXT,
    checkpoint_path TEXT,
    task_summary_json TEXT
);
```

Tracks each janitor task execution. Used for incremental mode — `get_last_task_run()` queries `MAX(completed_at)` for a given task to determine what's changed since the last run.

### 3.15 Edge Keywords — Graph Expansion Triggers

```sql
CREATE TABLE IF NOT EXISTS edge_keywords (
    relation TEXT PRIMARY KEY,
    keywords TEXT NOT NULL,  -- JSON array of trigger keywords
    description TEXT,        -- Human-readable description of this relation
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

LLM-generated keywords map edge types to natural language triggers. Used by `should_expand_graph()` to decide when recall needs graph traversal.

### 3.16 Doc Registry — Project and Document Tracking

Managed by `datastore/docsdb/registry.py` `ensure_table()` — NOT in schema.sql.

> **Location:** `QUAID_HOME/<instance>/data/memory.db`. On example.local, both OC and CC share
> `QUAID_HOME=/Users/owner/quaid`, so `doc_registry` and `doc_chunks` are in
> `QUAID_HOME/data/memory.db` and are shared across instances unless `QUAID_HOME` differs.

```sql
CREATE TABLE IF NOT EXISTS doc_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL DEFAULT 'default',
    asset_type TEXT NOT NULL DEFAULT 'doc',
    title TEXT,
    description TEXT,
    tags TEXT DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'active',
    auto_update INTEGER DEFAULT 0,
    source_files TEXT,                    -- JSON array: files this doc tracks
    last_indexed_at TEXT,
    last_modified_at TEXT,
    registered_at TEXT NOT NULL DEFAULT (datetime('now')),
    registered_by TEXT DEFAULT 'system',

    -- Identity/source scope context (added via ALTER TABLE on existing DBs)
    source_channel TEXT,
    source_conversation_id TEXT,
    source_author_id TEXT,
    speaker_entity_id TEXT,
    subject_entity_id TEXT,
    conversation_id TEXT,
    visibility_scope TEXT DEFAULT 'source_shared',
    sensitivity TEXT DEFAULT 'normal',
    participant_entity_ids TEXT DEFAULT '[]',
    provenance_confidence REAL
);
```

In-directory files auto-belong to their project via `homeDir` matching. Registry entries are for: external files (fast path→project lookup), docs with `source_files` tracking (mtime staleness), and files needing explicit metadata.

### 3.17 Doc Chunks — RAG Embedding Storage

Managed by `datastore/docsdb/rag.py` `_ensure_schema()` — NOT in schema.sql.

> **Location:** Same `memory.db` as `doc_registry`. Shared across instances when `QUAID_HOME` is shared.

```sql
CREATE TABLE IF NOT EXISTS doc_chunks (
    id TEXT PRIMARY KEY,                    -- source_file:chunk_index
    source_file TEXT NOT NULL,              -- Full path to source file
    chunk_index INTEGER NOT NULL,           -- 0-based chunk number within file
    content TEXT NOT NULL,                  -- Chunk text content
    section_header TEXT,                    -- Extracted markdown header (optional)
    embedding BLOB NOT NULL,                -- float32 array, dim from config (ollama.embeddingDim)

    -- Metadata
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),

    UNIQUE(source_file, chunk_index)
);
```

Used by the RAG search pipeline (`quaid docs search`). Indexing by one adapter makes chunks
searchable by both adapters on the same machine (shared DB). Reindex with:
`python3 datastore/docsdb/rag.py reindex --all`

### 3.18 Project Definitions — Project Configuration (DB)

Managed by `datastore/docsdb/registry.py` `ensure_table()` — NOT in schema.sql. Source of truth for project definitions (migrated from JSON config).

```sql
CREATE TABLE IF NOT EXISTS project_definitions (
    name TEXT PRIMARY KEY,                    -- Kebab-case identifier (e.g., "quaid")
    label TEXT NOT NULL,                      -- Human-readable name
    home_dir TEXT NOT NULL,                   -- Workspace-relative path
    source_roots TEXT DEFAULT '[]',           -- JSON array of paths
    auto_index INTEGER DEFAULT 1,
    patterns TEXT DEFAULT '["*.md"]',         -- JSON array of glob patterns
    exclude TEXT DEFAULT '["*.db","*.log","*.pyc","__pycache__/"]',  -- JSON array
    description TEXT DEFAULT '',
    state TEXT DEFAULT 'active' CHECK(state IN ('active', 'archived', 'deleted')),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
```

Seeded from `config/memory.json → projects.definitions` on first run (if table is empty). After seeding, the DB is the source of truth — JSON is ignored. `config.py` loads definitions from this table with a JSON fallback for fresh installs.

### 3.19 Doc Update Log — Documentation Update Audit Trail

```sql
CREATE TABLE IF NOT EXISTS doc_update_log (
    id INTEGER PRIMARY KEY,
    doc_path TEXT NOT NULL,
    source_files TEXT,              -- JSON array of source paths
    staleness_score REAL,
    change_summary TEXT,
    commit_hash TEXT,               -- git commit that fixed it
    agent_id TEXT DEFAULT 'unknown',
    timestamp TEXT DEFAULT (datetime('now'))
);
```

Replaces the old `data/docs-update-log.json` file for concurrent safety and queryability. Records each doc auto-update with the triggering source files, staleness score, and a summary of what changed. Used by `detect_drift_from_git()` in Task 1b to track drift between source files and their corresponding documentation.

### 3.20 Indexes

```sql
-- Nodes
CREATE INDEX idx_nodes_type ON nodes(type);
CREATE INDEX idx_nodes_privacy ON nodes(privacy);
CREATE INDEX idx_nodes_verified ON nodes(verified);
CREATE INDEX idx_nodes_pinned ON nodes(pinned);
CREATE INDEX idx_nodes_source ON nodes(source);
CREATE INDEX idx_nodes_owner ON nodes(owner_id);
CREATE INDEX idx_nodes_owner_status ON nodes(owner_id, status);
CREATE INDEX idx_nodes_actor ON nodes(actor_id);
CREATE INDEX idx_nodes_subject_entity ON nodes(subject_entity_id);
CREATE INDEX idx_nodes_session ON nodes(session_id);
CREATE INDEX idx_nodes_source_conversation ON nodes(source_conversation_id);
CREATE INDEX idx_nodes_conversation ON nodes(conversation_id, created_at);
CREATE INDEX idx_nodes_speaker_entity ON nodes(speaker_entity_id, status);
CREATE INDEX idx_nodes_visibility_scope ON nodes(visibility_scope, sensitivity, status);
CREATE INDEX idx_nodes_subject_status_updated ON nodes(subject_entity_id, status, updated_at);
CREATE INDEX idx_nodes_source_status_updated ON nodes(source_conversation_id, status, updated_at);
CREATE INDEX idx_nodes_status ON nodes(status);
CREATE INDEX idx_nodes_accessed ON nodes(accessed_at);
CREATE INDEX idx_nodes_confidence ON nodes(confidence);
CREATE INDEX idx_nodes_content_hash ON nodes(content_hash);
CREATE INDEX idx_nodes_created ON nodes(created_at);

-- Edges
CREATE INDEX idx_edges_source ON edges(source_id);
CREATE INDEX idx_edges_target ON edges(target_id);
CREATE INDEX idx_edges_relation ON edges(relation);
CREATE INDEX idx_edges_source_fact ON edges(source_fact_id);

-- Embedding cache
CREATE INDEX idx_embedding_cache_model ON embedding_cache(model);

-- Entity aliases
CREATE INDEX idx_aliases_alias ON entity_aliases(alias);
CREATE INDEX idx_aliases_canonical ON entity_aliases(canonical_name);

-- Support tables
CREATE INDEX idx_contradictions_status ON contradictions(status);
CREATE INDEX idx_dedup_log_review ON dedup_log(review_status);
CREATE INDEX idx_decay_queue_status ON decay_review_queue(status);
CREATE INDEX idx_recall_log_created ON recall_log(created_at);

-- Doc registry (managed by ensure_table())
CREATE INDEX idx_doc_registry_project ON doc_registry(project);
CREATE INDEX idx_doc_registry_state ON doc_registry(state);
```

### 3.21 PRAGMA Settings

Set in `lib/database.get_connection()`:
- `PRAGMA foreign_keys = ON` — enables cascade deletes
- `PRAGMA busy_timeout = 30000` — 30s wait for concurrent access
- `PRAGMA journal_mode = WAL` — write-ahead logging for concurrent reads
- `PRAGMA synchronous = NORMAL` — safe with WAL, faster than FULL
- `PRAGMA cache_size = -64000` — 64MB page cache
- `PRAGMA temp_store = MEMORY` — temp tables in memory
- Row factory: `sqlite3.Row` for named column access

---

## 4. Configuration Reference

### 4.1 Config Layer Architecture — Four-Layer Merge Chain

Config is loaded by `_load_config_inner()` in `config.py`. `_config_paths()` returns four paths in highest-priority-first order; the loader iterates them in reverse (lowest first) and deep-merges each file that exists:

| Priority | Path | Purpose |
|----------|------|---------|
| **0 (highest)** | `QUAID_HOME/<instance>/config/memory.json` | Per-instance overrides (identity, capture timeouts, domain preferences) |
| **1** | `QUAID_HOME/shared/config/memory.json` | Machine-wide shared settings (embeddings model, Ollama URL) |
| **2** | `~/.quaid/memory-config.json` | User-level fallback (rarely used) |
| **3 (lowest)** | `./memory-config.json` | Local cwd override (dev/testing only) |

Rules:
- The shared config is written by the first installer; subsequent instances on the same machine inherit it automatically.
- Embeddings config (`ollama.*`, `embeddings.*`) must live in shared config so all instances use the same model and produce comparable embeddings.
- Instance config holds per-adapter settings that should differ between instances (identity, session timeouts, retrieval preferences).
- A local `./memory-config.json` in the cwd can override everything (rarely used; intended for local dev/testing only).
- Missing layers are silently skipped; only files that exist are merged.
- Deep merge semantics: nested dicts are merged recursively; scalar and list values in higher-priority layers overwrite lower-priority values entirely.

### 4.2 Config CLI Commands

```bash
quaid config show                          # Show effective merged config (instance + shared)
quaid config show --shared                 # Show shared config only
quaid config show --instance <id>          # Show specific instance's config
quaid config edit                          # Edit current instance config ($EDITOR)
quaid config edit --shared                 # Edit shared config (embeddings, Ollama URL)
quaid config edit --instance <id>          # Edit a specific instance's config
quaid config path                          # Show active config file path
quaid config set <dotted.key> <value>      # Set a key in current instance config
quaid config set <dotted.key> <value> --shared   # Set a key in shared config
```

### 4.3 Config Sections

**Config file:** `config/memory.json` — effective config with sections:

| Section | Controls |
|---------|----------|
| `models` | `llmProvider` (`default` or explicit provider), `embeddingsProvider`, `deepReasoning`, `fastReasoning`, `deepReasoningModelClasses`, `fastReasoningModelClasses` |
| `database` | `path` (main DB), `archivePath` (archive DB) |
| `ollama` | `url`, `embeddingModel`, `embeddingDim` |
| `docs` | `autoUpdateOnCompact`, `maxDocsPerUpdate`, `stalenessCheckEnabled`, `sourceMapping`, `updateTimeoutSeconds` |
| `rag` | `docsDir`, `chunkMaxTokens`, `chunkOverlapTokens`, `searchLimit`, `minSimilarity` |
| `search` | `semanticFallbackLimit`, `ftsBoostProperNouns`, BEAM search params, reranker config |
| `core.parallel` | Lifecycle prepass concurrency and locking — see below |
| `capture`, `decay`, `dedup`, `janitor`, `retrieval`, `logging`, `users` | Existing sections |
| `notifications` | Notification routing and channel config |
| `systems` | Feature gates, system-level toggles, bootstrap file monitoring |

### 4.4 `core.parallel` Config

Controls lifecycle routine concurrency and resource locking:

```json
{
  "core": {
    "parallel": {
      "enabled": true,
      "lifecyclePrepassWorkers": 3,
      "lifecyclePrepassTimeoutSeconds": 300,
      "lifecyclePrepassTimeoutRetries": 1,
      "lockEnforcementEnabled": true,
      "lockWaitSeconds": 120,
      "lockRequireRegistration": true
    }
  }
}
```

All keys accept snake_case aliases (e.g. `lifecycle_prepass_timeout_seconds`). Env var overrides:
- `QUAID_CORE_PARALLEL_MAP_TIMEOUT_SECONDS` — per-map timeout (overrides `lifecyclePrepassTimeoutSeconds`)
- `QUAID_CORE_PARALLEL_MAP_TIMEOUT_RETRIES` — per-map retry count (overrides `lifecyclePrepassTimeoutRetries`)
- `QUAID_GLOBAL_LLM_MAX_WORKERS` — global LLM thread pool ceiling (default 32; set on `GlobalLlmScheduler` at process start)

**Lock resources** — lifecycle routines declare `write_resources` at registration time. Recognized tokens:
- `db:memory` — resolves to the memory DB path; serializes DB writers
- `files:global` / `files` / `core_markdown` — global files lock (markdown and doc writes)
- `file:<path>` — per-file lock

Dry-run disables locking entirely.

### 4.5 Embedding Model Config (Machine-Wide / Shared)

Embeddings config lives in `QUAID_HOME/shared/config/memory.json` — written once on first install and inherited by all instances on the machine. All instances must use the same model so embeddings are cross-instance comparable.

```json
{
  "ollama": {
    "url": "http://localhost:11434",
    "embeddingModel": "qwen3-embedding:8b",
    "embeddingDim": 4096
  }
}
```

To change the embedding model: `quaid config edit --shared`

The embedding model was upgraded from nomic-embed-text (768-dim) → qwen3-embedding:0.6b → **qwen3-embedding:8b** (4096-dim, 75.22 MTEB score). The higher-dimensional embeddings provide significantly better semantic matching quality.

### 4.6 Provider and Model Config

```json
{
  "models": {
    "llmProvider": "default",
    "embeddingsProvider": "ollama",
    "fastReasoning": "default",
    "deepReasoning": "default",
    "fastReasoningModelClasses": {
      "openai": "gpt-5.1-codex-mini",
      "anthropic": "claude-haiku-4-5"
    },
    "deepReasoningModelClasses": {
      "openai": "gpt-5.3-codex",
      "anthropic": "claude-opus-4-6"
    }
  }
}
```

Resolution rules:
- If `fastReasoning`/`deepReasoning` are explicit model IDs, those are used.
- If either tier is `"default"`, Quaid resolves from `fastReasoningModelClasses` / `deepReasoningModelClasses` using the effective provider.
- Effective provider comes from `models.llmProvider` unless it is `"default"`, in which case gateway default provider/auth state is used.
- Unknown providers fail loudly (no silent Anthropic fallback), preserving abstraction and testability.

See `docs/INTERFACES.md#provider-abstraction-contract` for the canonical provider/model flow.

### 4.7 Search Config

Includes BEAM and reranker settings:

```json
{
  "search": {
    "reranker": {
      "provider": "llm",
      "model": "qwen2.5:7b",
      "topK": 20,
      "instruction": "Given a personal memory query, determine if this memory is relevant to the query",
      "notes": "provider: 'llm' uses fast-reasoning model via configured LLM provider, single batched call. 'ollama' uses 'model' field for local inference (needs model in RAM). model field only used when provider=ollama."
    },
    "rrfK": 60,
    "rerankerBlend": 0.5,
    "compositeRelevanceWeight": 0.6,
    "compositeRecencyWeight": 0.2,
    "compositeFrequencyWeight": 0.15,
    "multiPassGate": 0.7,
    "mmrLambda": 0.7,
    "coSessionDecay": 0.6,
    "recencyDecayDays": 90
  }
}
```

**BEAM search config:**
```json
{
  "retrieval": {
    "beam": {
      "beamWidth": 5,
      "maxDepth": 2,
      "hopDecay": 0.7,
      "notes": "Adaptive scoring: heuristic first (free), then LLM reranker only when candidates > beamWidth (truncation needed). hopDecay: score multiplier per hop depth (0.7^depth)."
    }
  }
}
```

### 4.8 Retrieval Config

```json
{
  "retrieval": {
    "default_limit": 5,
    "max_limit": 8,
    "min_similarity": 0.60,
    "notify_min_similarity": 0.85,
    "boost_recent": true,
    "boost_frequent": true,
    "max_tokens": 2000,
    "_note": "Recall notifications follow notifications.level / notifications.retrieval.verbosity"
  }
}
```

Dynamic K scaling is runtime logic in the adapter (not a config block): retrieval uses `round(11.5 * ln(N) - 61.7)` where `N` is active node count, clamped to `[5, 40]` with a 5-minute node-count cache. `default_limit`/`max_limit` still apply to request bounds.

### 4.9 User Identity Config

```json
{
  "users": {
    "defaultOwner": "<user_id>",
    "identities": {
      "<user_id>": {
        "channels": { "telegram": ["@handle"], "cli": ["*"] },
        "speakers": ["DisplayName", "Alias"],
        "personNodeName": "Full Name"
      }
    }
  }
}
```

### 4.10 Bootstrap File Monitoring

Workspace monitoring uses a built-in bootstrap file set (`AGENTS.md`, `SOUL.md`, `TOOLS.md`, `USER.md`, `MEMORY.md`, `IDENTITY.md`, `HEARTBEAT.md`, `TODO.md`, `PROJECT.md`) plus adapter-provided additions. There is no `janitor.bootstrapFiles` config key.

### 4.11 Logging Config

```json
{
  "logging": {
    "enabled": true,
    "level": "info",
    "retention_days": 7
  }
}
```

**Config loader:** `config.py` — Python dataclasses for all sections. Includes `SystemsConfig` for feature gates and system-level toggles. Includes merge helper for layered config resolution.

### 4.12 Instance Management

Quaid supports multiple isolated memory instances on the same machine (e.g. one for OpenClaw, one for Claude Code). Each instance has its own database, config, and logs while sharing embeddings config and the project registry.

**Environment variables:**
- `QUAID_HOME` — root directory containing all instances (default: `~/quaid`). Set once per machine.
- `QUAID_INSTANCE` — identifier for the active instance (required; e.g. `openclaw`, `claude-code`). Set by each adapter's runtime hooks — do NOT set globally in shell profile.

**Instance directory layout:**
```
QUAID_HOME/
├── <instance>/           # One directory per instance
│   ├── config/
│   │   └── memory.json   # Instance-specific config overrides
│   ├── data/
│   │   └── memory.db     # Instance SQLite database
│   └── logs/             # Instance logs
├── shared/
│   ├── config/
│   │   └── memory.json   # Machine-wide shared config (embeddings, Ollama)
│   └── projects/         # Shared project files
└── project-registry.json # Cross-instance project registry
```

**Key rules:**
- Multiple instances share `QUAID_HOME/shared/` and `QUAID_HOME/project-registry.json`.
- The shared config is written by the first installer; subsequent instances inherit it without overwriting.
- Instance names must be alphanumeric (may include `.`, `_`, `-`), max 64 chars, and cannot use reserved names (`shared`, `projects`, `config`, `data`, etc.).

**CLI commands:**
```bash
quaid instances list               # List all instances under QUAID_HOME (current marked with *)
quaid instances list --json        # JSON output: {home, current, instances:[...]}
```

**Module:** `lib/instance.py` — zero-dependency instance resolution. Functions: `quaid_home()`, `instance_id()`, `instance_root()`, `shared_dir()`, `list_instances()`, `shared_config_path()`.

---

## Related Docs

- `janitor-reference.md` — Nightly maintenance pipeline reference
- `memory-deduplication-system.md` — Store-time and nightly dedup
- `memory-operations-guide.md` — Day-to-day operational handbook
- `../../docs/INTERFACES.md#provider-abstraction-contract` — Provider abstraction contract
- `extraction-pipeline.md` — Full extraction pipeline reference
- `config-instances.md` — Config system and instance model details
- `projects-reference.md` — Projects system internals
- `rag-docs-system.md` — RAG and docs system
- `hooks-session-lifecycle.md` — Hook entry points and session lifecycle
