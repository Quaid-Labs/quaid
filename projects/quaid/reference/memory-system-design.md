# Memory System Design
<!-- PURPOSE: Architecture overview: three-layer system, lifecycle, design decisions, phases -->
<!-- SOURCES: (conceptual — not tied to specific source files) -->

*Created: 2026-01-31*
*Updated: 2026-02-08*
*Status: Phase 6 complete — search batches 1-4, Ebbinghaus decay, projects system*

## Overview

The knowledge layer is a graph-based personal knowledge base using SQLite + Ollama embeddings, fully local for storage and search. LLM calls are provider-agnostic at core level and are resolved through the adapter/provider layer (gateway + config-driven deep/fast model tiers). Ollama is used for embeddings only.

**Key design decisions:**
- Local-first: Ollama for embeddings only (`qwen3-embedding:8b`, 4096-dim), SQLite for storage
- Graph structure: nodes (facts, people, preferences) + edges (relationships)
- Hybrid search: semantic similarity + full-text keyword search with proper noun boosting
- Privacy-aware: per-fact privacy tiers (private/shared/public) with owner-based filtering
- Nightly maintenance: automated janitor pipeline for dedup, contradiction detection, decay
- Config-driven: all model IDs, paths, and settings in `config/memory.json`

---

## Three-Layer Architecture

The system uses three tiers with distinct purposes:

| Layer | Storage | Loaded When | Purpose | Examples |
|-------|---------|-------------|---------|----------|
| **Markdown** | SOUL.md, USER.md, TOOLS.md, HEARTBEAT.md | Every context (always injected) | Core instructions, identity, system pointers | "Alfie's personality", "Quaid's core facts", "System tool locations" |
| **RAG** | `docs/<project>/` docs | Searched when topically relevant | Reference documentation, system architecture | "Knowledge layer design", "Janitor pipeline reference", "Spark agent planning" |
| **Memory DB** | `data/memory.db` | Searched per-message via recall pipeline | Personal facts from conversations | "Quaid prefers dark mode", "Melina's birthday is Oct 12", "Quaid chose SQLite for simplicity" |

**What belongs in Memory (the DB):**
- Personal facts about people: names, relationships, birthdays, health, locations
- Preferences and opinions: likes, dislikes, communication styles
- Personal decisions with reasoning: "Quaid chose X because Y"
- Life events: trips, milestones, purchases, diagnoses
- Relationships: family, friends, colleagues, pets

**What does NOT belong in Memory:**
- System architecture ("The knowledge layer uses SQLite with WAL mode") → RAG docs (`docs/<project>/`)
- Infrastructure knowledge ("Ollama runs on port 11434") → RAG docs (`docs/<project>/`)
- Operational rules for AI agents ("Alfie should check HANDOFF.md on wake") → SOUL.md/HEARTBEAT.md
- Tool/config descriptions ("The janitor has a dedup threshold of 0.85") → RAG docs (`docs/<project>/`)

---

## Architecture

```
Agent calls memory_recall with crafted query
     │
     ▼
Intent classification (WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY)
     │
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

> **Note:** Recall is agent-driven via `memory_recall` tool (Feb 2026). Auto-injection is optional (gated by config/env). Auto-capture via per-message classifier is deprecated, but inactivity-timeout extraction still runs when capture is enabled. Memory extraction happens at compaction/reset via Opus with combined fact+edge+snippet+journal extraction. Soul snippets are observations written to `.snippets.md` staging files, reviewed by janitor Task 1d-snippets, and folded into core markdown files (SOUL.md, USER.md, MEMORY.md, AGENTS.md). Journal entries are diary-style paragraphs written to `journal/*.journal.md`, distilled by janitor Task 1d-journal into core markdown themes, then archived to `journal/archive/`.

---

## Privacy Tiers

| Tier | Description | Visibility |
|------|-------------|------------|
| `public` | Non-personal general facts | All owners |
| `shared` | Household knowledge (default) | All owners |
| `private` | Secrets, surprises, finances, health | Owner only |

Privacy is classified per-fact during extraction (Opus at compaction/reset). Default is `shared`. The recall pipeline filters private memories to only the owning user.

---

## Search System (Batches 1-4, Feb 2026)

### Search Pipeline
Multi-stage pipeline with RRF fusion, intent awareness, and diversity:

1. **Intent classification** — categorizes query as WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY
2. **Parallel search** — BM25 (FTS5) + semantic (cosine) run concurrently
3. **RRF fusion** — Reciprocal Rank Fusion combines results (k=60, weights dynamic per intent via `_get_fusion_weights()`)
4. **Content hash pre-filter** — SHA256 exact-dedup removes identical results
5. **Composite scoring** — 60% relevance + 20% recency + 15% access frequency + 5% extraction confidence
6. **Temporal validity** — expired facts penalized, future facts deprioritized
7. **MMR diversity** — Maximal Marginal Relevance (lambda=0.7) prevents redundant results
8. **Multi-hop traversal** — bidirectional graph traversal, depth=2, hop score decay 0.7^depth
9. **Access tracking** — increments access_count and accessed_at on returned results

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

---

## Implementation Phases (Historical)

### Phase 1: LanceDB + Local Routing (DONE, Retired)
- [x] LanceDB with OpenAI embeddings — replaced by local system
- [x] Regex-based auto-capture — replaced by Haiku classifier
- [x] ~19 seeded memories — now ~616 active nodes, ~205 edges (all graduated)

### Phase 2: Graph Store (DONE)
- [x] SQLite schema with nodes, edges, FTS5, metadata
- [x] Ollama embeddings (originally nomic-embed-text 768-dim, now qwen3-embedding:8b 4096-dim)
- [x] Entity extraction via Claude Haiku
- [x] Relationship extraction via janitor edges task
- [x] Hybrid search (semantic + FTS + edge traversal)

### Phase 3: Custom Memory Plugin (DONE, Production)
- [x] Plugin skeleton with Clawdbot hooks
- [x] Hybrid search replaces LanceDB vector-only
- [x] Privacy tier filtering (per-fact classification)
- [x] User identity mapping via `config/memory.json`
- [x] Haiku reranker for recall relevance
- [x] Session dedup + compaction time-gate
- [x] LanceDB plugin disabled

### Phase 4: Local-Only (DONE)
- [x] Ollama replaces OpenAI for embeddings
- [x] Zero external API dependency for storage/search
- [x] Anthropic API only used for extraction (Opus) and reranking (Haiku)

### Phase 5: Quality & Governance (DONE)
- [x] Per-message auto-capture deprecated → event-based extraction (compaction/reset) + inactivity-timeout extraction
- [x] Config-driven model selection — no hardcoded model IDs
- [x] Fail-fast pipeline guard with graduation blocking
- [x] Temporal resolution (regex-based, no LLM)
- [x] Edge normalization (inverse/synonym maps, symmetric ordering)
- [x] Smart dedup with token-recall + Haiku verification
- [x] Decay review queue (Opus review instead of silent deletion)
- [x] Three-layer architecture: Markdown / RAG / Memory DB
- [x] LLM prompts scoped to personal facts only

### Phase 6: Search & Retrieval Overhaul (DONE)
- [x] Batch 1: RRF fusion, BM25 via FTS5, composite scoring (60/20/15/5), MMR diversity
- [x] Batch 2: Content hash dedup, embedding cache, fact versioning, KB health metrics
- [x] Batch 3: Intent classification (WHO/WHEN/WHERE/WHAT/PREFERENCE/RELATION/WHY), temporal validity filtering
- [x] Batch 4: Ebbinghaus exponential decay, multi-hop traversal (depth=2), access tracking, parallel search
- [x] Combined fact+edge extraction at capture time (single LLM call)
- [x] Token-based janitor batching (context window-aware)
- [x] Agent-driven recall (auto-injection optional via config/env)
- [x] Projects system: registry, event processing, auto-discover
- [x] Mock embeddings for testing (`MOCK_EMBEDDINGS=1`)
- [x] 1108 tests (945 pytest + 163 vitest)

---

## Answered Questions (from original design)

1. **Token budget:** Dynamic K based on node count (clamped by config); optional LLM reranker for relevance
2. **Capture quality:** Opus extraction at compaction/reset events with strict personal-facts-only criteria. Nightly janitor cleans any remaining noise.
3. **Graph seeding:** Bootstrapped from workspace files via `seed.py`, continuously enriched by event-based extraction
4. **Contradiction handling:** Janitor Task 4 detects contradictions via token recall + Haiku, stores in `contradictions` table for resolution

---

## Recent Capabilities (Feb 2026)

- **Search pipeline overhaul** (Batches 1-4): RRF fusion, BM25, composite scoring, MMR diversity, intent classification, temporal validity filtering, Ebbinghaus decay, multi-hop traversal, access tracking, parallel search
- **Agent-driven recall**: Auto-injection is optional; agent calls `memory_recall` tool with crafted queries
- **Combined fact+edge extraction**: Single Opus call extracts both facts and relationships at compaction/reset
- **Content hash dedup**: SHA256 pre-filter catches exact duplicates before embedding comparison
- **Embedding cache**: DB-backed cache avoids redundant Ollama calls
- **Fact versioning**: `supersede_node()` chains old→new facts with history tracking
- **Ebbinghaus decay**: `R = 2^(-t/half_life)` with access-scaled half-life (frequently accessed = slower decay)
- **Token-based batching**: `TokenBatchBuilder` dynamically packs items based on model context window
- **Projects system**: Registry CRUD, event-driven updates, auto-discover, 5 projects
- **Mock embeddings**: `MOCK_EMBEDDINGS=1` env var for testing without Ollama
- **1108 tests**: 945 pytest + 163 vitest, all passing
- **Journal system**: Diary-style entries written to `journal/*.journal.md`, distilled into core markdown themes by janitor, archived monthly to `journal/archive/`. Two modes: `distilled` (default, token-efficient) and `full` (richer self-awareness)
- **Config-driven models**: All model IDs read from `config/memory.json`
- **Fail-fast pipeline**: `memory_pipeline_ok` flag — if any memory task fails, graduation blocked
- **Temporal resolution**: Regex-based date resolver (Task 2a)
- **Edge normalization**: Inverse flipping, synonym resolution, symmetric alphabetical ordering
- **Smart dedup**: Token-recall + Haiku verification in gray zone (0.88-0.98)
- **Graduation**: `pending → approved → active` lifecycle, all facts now graduated

---

## Related Docs

- `memory-local-implementation.md` — Full implementation details, pipeline, test suite
- `janitor-reference.md` — Nightly maintenance pipeline reference
- `memory-deduplication-system.md` — Store-time and nightly dedup
- `memory-schema.md` — Database schema reference
- `memory-system-comparison.md` — Comparison with other memory systems
- `spark-agents.md` — Agent registry (for future Spark integration)

---

## Notes

- Telegram has ~52 char width limit for code blocks — avoid wide ASCII diagrams
- LLM credentials resolved by adapter/gateway provider auth (OAuth/API key), not by core modules
- All paths, models, and settings are centralized in `config/memory.json` — see `config.py` for dataclass definitions
- Database path: `config.database.path` (default: `data/memory.db`, SQLite + WAL)
- Archive DB: `config.database.archivePath` (default: `data/memory_archive.db`)
- Embeddings: `config.ollama.url` (default: `http://localhost:11434`) with `config.ollama.embeddingModel` (default: `qwen3-embedding:8b`, 4096-dim)
- Shared library in `modules/quaid/lib/` — config, database, embeddings, similarity, tokens, archive
- Env var overrides for testing: `MEMORY_DB_PATH`, `MEMORY_ARCHIVE_DB_PATH`, `OLLAMA_URL`, `CLAWDBOT_WORKSPACE`
