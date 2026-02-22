# Quaid Architecture Guide

Quaid is a persistent memory system for AI agents. It extracts personal facts from conversations, stores them in a local SQLite graph, retrieves them when relevant, and maintains quality through a nightly janitor pipeline. Everything runs locally -- no cloud memory services, no external databases.

Quaid works with any system that supports [MCP](https://modelcontextprotocol.io) (Claude Desktop, Claude Code, Cursor, Windsurf, etc.) and uses an adapter layer so host integrations remain isolated from core memory logic. It currently ships with a guided installer for [OpenClaw](https://github.com/openclaw/openclaw), which is the most mature integration path.

This document is for engineers who want to understand how the system works.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Extraction Pipeline](#2-extraction-pipeline)
3. [Retrieval Pipeline](#3-retrieval-pipeline)
4. [Database](#4-database)
5. [Janitor Pipeline](#5-janitor-pipeline)
6. [Error Handling & Resilience](#6-error-handling--resilience)
7. [Dual Learning System](#7-dual-learning-system)
8. [Projects System](#8-projects-system)
9. [Configuration](#9-configuration)
10. [MCP Server](#10-mcp-server)

---

## 1. System Overview

Quaid exposes its memory system through three interfaces: an **MCP server** (works with any MCP-compatible client), a **CLI** (standalone, no gateway needed), and an **OpenClaw plugin** (deepest integration with automatic lifecycle hooks).

### High-Level Architecture

```
     MCP Client             CLI               OpenClaw Gateway
    (Claude Desktop,    (quaid extract,          |
     Cursor, etc.)      quaid search, ...)   Lifecycle hooks
          |                  |               (compaction, reset,
          v                  v                agent turn)
    +----------+      +----------+               |
    |MCP Server|      | extract  |       +-------+-------+
    | (stdio)  |      | .py CLI  |       | adapters/     |
    |          |      |          |       | openclaw/     |
    |          |      |          |       | adapters/openclaw/index.ts      |
    +----+-----+      +----+-----+       | (TS plugin)   |
         |                 |             +-------+-------+
         +--------+--------+--------------------+
                  |
           +------v------+           +----------+
           |  Python API  |           | Retrieval|
           | (api.py)     |           | Pipeline |
           +--------------+           +----------+
                  |                        |
                  +------ SQLite DB -------+
                               |
                        +------v------+
                        |   Janitor   |
                        |  (nightly)  |
                        +-------------+
```

### Three Main Loops

**Extraction** -- Triggered by context compaction, session reset, direct CLI invocation (`quaid extract`), or MCP tool call (`memory_extract`). A deep-reasoning LLM call extracts structured facts and relationship edges from the conversation transcript. Facts enter the database as `pending` and are immediately available for recall. The janitor later reviews and graduates them to `active`, improving quality, but pending facts are never hidden from retrieval.

**Retrieval** -- Fires before each agent turn (OpenClaw), on `memory_recall` MCP tool call, or via `quaid search` CLI. The agent's query is classified by intent, expanded via HyDE, and searched across three channels (vector, FTS5, graph). Results are fused via RRF, reranked by a fast-reasoning LLM, and injected into the agent's context as `[MEMORY]`-prefixed messages.

**Maintenance** -- A nightly janitor pipeline reviews pending facts, deduplicates near-identical memories, resolves contradictions, decays stale memories, updates documentation, and runs structural health checks.

Almost every decision in the system is algorithm-assisted but ultimately arbitrated by an LLM appropriate for the task. This means Quaid naturally scales with AI models -- as reasoning capabilities improve, every decision in the pipeline gets better without code changes.

### Data Flow

```
Conversation messages
        |
        v
  [Extraction]  High-reasoning LLM extracts facts + edges + snippets + journal entries
        |
        +---> nodes table (status: pending)
        +---> edges table (source_fact_id links to originating fact)
        +---> *.snippets.md (bullet-point observations)
        +---> journal/*.journal.md (reflective diary entries)
        |
  [Janitor]  Nightly: review, dedup, decay, workspace audit
        |
        +---> nodes promoted to status: active
        +---> duplicates merged, contradictions resolved
        +---> snippets folded into SOUL.md / USER.md
        +---> journal distilled into core markdown
        |
  [Retrieval]  Per-turn: query -> search -> rerank -> inject
        |
        +---> [MEMORY] tagged messages in agent context
        +---> access_count++, storage_strength updated (Bjork model)
```

---

## 2. Extraction Pipeline

### Trigger Points

Extraction can be triggered from any of Quaid's three interfaces:

- **OpenClaw plugin** (`adapters/openclaw/index.ts`): Signal-driven extraction via command and lifecycle events. `before_compaction`/`before_reset` queue extraction signals, `command` maps slash commands (`/compact`, `/new`, `/reset`, `/restart`) to signals, and `agent_end` provides a fallback signal path when teardown timing races.
- **MCP server** (`mcp_server.py`): The `memory_extract` tool accepts a plain text transcript and runs the full pipeline.
- **CLI** (`extract.py`): `quaid extract <file>` accepts JSONL session files or plain text transcripts.

All three paths converge on the same extraction logic and produce identical results.

### Extraction Steps

1. **Transcript preparation** -- The input is normalized to a human-readable transcript. JSONL session files are parsed (handling both wrapped `{"type": "message", "message": {...}}` and direct `{"role": ..., "content": ...}` formats). Plain text is passed through as-is. In the OpenClaw path, queued memory notes (from the `memory_note` tool) are prepended.

2. **LLM extraction** -- A single deep-reasoning LLM call processes the transcript and produces:
   - **Facts**: Structured observations with name, category, confidence, speaker, knowledge_type
   - **Edges**: Relationships between entities (subject, relation, object)
   - **Soul snippets**: Bullet-point observations for core markdown files
   - **Journal entries**: Reflective diary paragraphs

3. **Fact storage** -- Each fact is stored via `memory_graph.store()`:
   - Content hash computed (SHA256) for exact-dedup before embedding
   - Embedding generated via Ollama (local, no API cost)
   - Semantic dedup check against existing facts (cosine similarity > 0.95)
   - If duplicate found: confirmation count incremented, confidence boosted
   - If new: stored with `status="pending"`, awaiting janitor review

4. **Edge creation** -- Edges are normalized via `_normalize_edge()` and stored with `source_fact_id` linking back to the originating fact. Entity nodes are created on-the-fly if they don't exist.

### Fact Schema

```python
@dataclass
class Node:
    id: str                    # UUID
    type: str                  # Person, Place, Project, Event, Fact, Preference, Concept
    name: str                  # The fact content / entity name
    confidence: float          # 0.0-1.0, starts at extraction_confidence
    status: str                # pending -> approved -> active
    owner_id: str              # Multi-user support
    knowledge_type: str        # fact, belief, preference, experience
    fact_type: str             # mutable, immutable, contextual
    speaker: str               # Who stated this (e.g. "Quaid", "Melina")
    source_type: str           # user, assistant, tool, import
    content_hash: str          # SHA256 of name text (fast dedup)
    keywords: str              # Space-separated search terms
    storage_strength: float    # Bjork model: cumulative encoding strength
    confirmation_count: int    # Times re-confirmed by extraction
    session_id: str            # Session that created this memory
    # ... plus temporal fields, access tracking, embedding
```

### Edge Normalization

Edges are normalized to canonical forms to prevent duplicates like `child_of(A, B)` and `parent_of(B, A)` coexisting. Normalization is maintained by the janitor pipeline and arbitrated by a deep-reasoning LLM when ambiguity exists:

```python
# Inverse map: flip subject/object AND rename relation
_INVERSE_MAP = {
    "child_of": "parent_of",       # child_of(A, B) -> parent_of(B, A)
    "owned_by": "owns",
    "led_to": "caused_by",         # led_to(A, B) -> caused_by(B, A)
    "caused": "caused_by",
    "resulted_in": "caused_by",
    "employs": "works_at",         # employs(Facebook, User) -> works_at(User, Facebook)
    "pet_of": "has_pet",
    # ...
}

# Synonym map: rename relation WITHOUT flipping direction
_SYNONYM_MAP = {
    "mother_of": "parent_of",      # mother_of(W, S) -> parent_of(W, S)
    "father_of": "parent_of",
    "married_to": "spouse_of",
    "likes": "prefers",
    "because_of": "caused_by",     # same direction
    # ...
}

# Symmetric relations: order by alphabetical entity name
_SYMMETRIC_RELATIONS = frozenset([
    "spouse_of", "partner_of", "sibling_of", "friend_of",
    "neighbor_of", "colleague_of", "related_to", "knows",
])
```

### Source Tracking

Every fact records where it came from:
- `source_type`: Whether the user, assistant, or a tool stated the fact
- `source_fact_id` on edges: Links relationship edges back to the fact that created them
- `session_id`: Which conversation session produced this memory

---

## 3. Retrieval Pipeline

Retrieval runs before each agent turn via the `before_agent_start` hook. The agent's message is used as a query against the memory graph.

### Pipeline Flow

```
User message
    |
    v
[1] Strip gateway metadata (channel info, user IDs)
    |
[2] Resolve entity aliases (e.g. short names -> canonical names)
    |
[3] Classify intent (WHO, WHEN, WHERE, WHAT, PREFERENCE, RELATION, WHY, GENERAL)
    |
[4] HyDE expansion: fast-reasoning LLM rephrases question as declarative statement
    |     "Where does Quaid's wife live?" -> "Quaid's wife lives in..."
    |
[5] Hybrid search (vector + FTS5 in parallel via ThreadPoolExecutor)
    |
[6] RRF fusion with intent-tuned weights
    |
[7] Composite scoring (relevance 60% + recency 20% + frequency 15%)
    |
[8] LLM reranker (graded 0-5 relevance scoring, single API call)
    |
[9] Competitive inhibition (reranker losers lose storage_strength)
    |
[10] Multi-pass: if top results < quality gate, try broader entity search
    |
[11] MMR diversity selection (avoid redundant results)
    |
[12] Graph expansion: BEAM search from top results to find related facts
    |
[13] Temporal contiguity: surface facts from same session
    |
[14] Bjork storage_strength update: hard retrievals strengthen more
    |
    v
[MEMORY]-tagged results injected into agent context
```

### Intent Classification

Regex-based pattern matching determines query intent, which tunes fusion weights and type boosting:

```python
_INTENT_PATTERNS = {
    "WHO":        {"patterns": [r"\bwho\b", r"\bfamily\b", ...], "type_boosts": {"Person": 1.3}},
    "WHEN":       {"patterns": [r"\bwhen\b", r"\bdate\b", ...],  "type_boosts": {"Event": 1.3}},
    "WHERE":      {"patterns": [r"\bwhere\b", r"\bcity\b", ...], "type_boosts": {"Place": 1.3}},
    "PREFERENCE": {"patterns": [r"\blike\b", r"\bfavorite\b"...], "type_boosts": {"Preference": 1.3}},
    "WHY":        {"patterns": [r"\bwhy\b", r"\bcause\b", ...],  "type_boosts": {"Event": 1.2}},
    # ...
}
```

### Three Search Channels

**1. Vector search (sqlite-vec ANN)**

Embeddings are generated by Ollama (default: `qwen3-embedding:8b`, 4096 dimensions). The query embedding is compared against all node embeddings via approximate nearest neighbors. An Ollama health check (200ms timeout, 30s cache) gates this channel -- if Ollama is down, retrieval falls back to FTS-only.

**2. FTS5 keyword search (BM25)**

SQLite FTS5 with Porter stemming and Unicode61 tokenization. Column weights: `name` at 2x, `keywords` at 1x. Catches proper nouns and exact terms that semantic search may miss.

**3. Graph traversal (BEAM search)**

Starting from nodes found by vector/FTS search, the graph is traversed via edges to find related facts. BEAM search keeps only the top-B candidates per hop level (default B=5, max depth=2), using adaptive scoring:

- Fast heuristic scoring considers edge weight, node confidence, intent alignment, and relation selectivity
- When candidates exceed beam width (truncation needed), the top 2*B candidates are re-scored via a fast-reasoning LLM reranker in a single batched API call
- Score decays by `hop_decay` (0.7) per level: a 2-hop result gets 0.49x the score of a direct hit

### RRF Fusion

Entity-focused queries (WHO, WHERE, RELATION) weight FTS heavily because proper nouns and exact names need string matching, not semantic similarity. Meaning-focused queries (PREFERENCE, WHY) weight vectors heavily because the user's phrasing may differ significantly from how the fact was stored.

Reciprocal Rank Fusion combines vector and FTS ranked lists. Weights are dynamic per intent:

```python
def _get_fusion_weights(intent):
    if intent in ("WHO", "WHERE", "RELATION"):
        return (0.5, 0.5)    # Entity queries: boost FTS for exact name match
    elif intent == "WHEN":
        return (0.4, 0.6)    # Temporal: strong FTS boost (date strings)
    elif intent in ("PREFERENCE", "WHY"):
        return (0.8, 0.2)    # Semantic meaning matters more
    else:
        return (0.7, 0.3)    # Default: vector-heavy

# RRF formula per result:
# score = weight / (k + rank)   where k=60 (configurable)
```

### LLM Reranker

After fusion, the top 20 candidates are sent to a fast-reasoning LLM in a single API call for graded relevance scoring (0-5 scale). The reranker score is blended with the original score at a configurable ratio. Benchmarks showed the LLM reranker contributes significantly to accuracy, so the blend favors the reranker score:

```
blended = 0.6 * (reranker_grade / 5.0) + 0.4 * original_score
```

### Competitive Inhibition

When the reranker scores a result 0-1 but the original search score was >= 0.65 (looked relevant but wasn't), the node's `storage_strength` is decremented by 0.02. This gradually deprioritizes misleading matches.

### Multi-Pass Retrieval

If the best result scores below the quality gate (default 0.70) and fewer results than requested were found, a second-pass search is triggered using extracted entity names and key terms from the query.

### Bjork Storage Strength

Every retrieval updates the accessed node's `storage_strength`:

```python
# Base increment + difficulty bonus (hard retrievals strengthen more)
ss_increment = 0.05 * (1.0 + 3.0 * difficulty)

# Difficulty factors:
#   - Low similarity score (hard to find via embedding)
#   - Multi-pass retrieval (found only on second pass)
#   - Graph traversal (found via edges, not direct search)
```

This models the Bjork desirable difficulty principle: memories that are harder to retrieve get strengthened more, making them easier to find next time.

The cognitive science behind this is Robert Bjork's research on desirable difficulties in learning: retrieval attempts that require more effort produce stronger and more durable memory traces. Without this mechanism, frequently-accessed 'easy' memories would dominate retrieval results while harder-to-find but equally important memories would gradually become unreachable. Storage strength acts as a counter-pressure to confidence decay -- even a memory that hasn't been accessed in months can maintain high retrievability if its past retrievals were effortful.

---

## 4. Database

### Storage Engine

SQLite with WAL mode, foreign keys enabled, and a busy timeout for concurrent access. The database is fully local -- no network dependencies.

Connection management is centralized in `lib/database.py`:
```python
@contextmanager
def get_connection(db_path=None):
    conn = sqlite3.connect(db_path or get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if mode.lower() != 'wal':
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    yield conn
    conn.close()
```

### Core Tables

```
nodes                   -- Memory entities (facts, persons, places, etc.)
edges                   -- Relationships between nodes
  UNIQUE(source_id, target_id, relation)
  source_fact_id FK -> nodes(id) ON DELETE SET NULL

nodes_fts               -- FTS5 virtual table (Porter stemming, name + keywords)
node_embeddings         -- sqlite-vec ANN index (float32 vectors)

contradictions          -- Detected conflicting facts (pending/resolved/false_positive)
dedup_log               -- All dedup decisions (for review)
decay_review_queue      -- Memories queued for human review instead of silent deletion

entity_aliases          -- Fuzzy name resolution (e.g. "Doug" -> "Douglas Quaid")
edge_keywords           -- LLM-generated trigger keywords per relation type
embedding_cache         -- Avoids recomputing embeddings for identical text

recall_log              -- Observability: every recall() call with latency, reranker stats
health_snapshots        -- Periodic DB health metrics (written by janitor)
doc_update_log          -- Documentation update audit trail
metadata                -- Key-value system state (schema_version, last_seed, etc.)
```

### FTS5 Configuration

```sql
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    name,
    keywords,
    content='nodes',
    content_rowid='rowid',
    tokenize='porter unicode61'
);
```

FTS is kept in sync via `AFTER INSERT/UPDATE/DELETE` triggers on the `nodes` table. The `keywords` column contains space-separated derived terms generated at extraction time, providing an additional signal channel beyond the raw fact text.

### Edge Constraints

```sql
CREATE TABLE edges (
    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    source_fact_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
    UNIQUE(source_id, target_id, relation)
);
```

The `UNIQUE` constraint means a relationship triple can only exist once. `INSERT OR REPLACE` is used for upserts. `ON DELETE CASCADE` ensures node deletions clean up associated edges. `source_fact_id` uses `SET NULL` so edge provenance is preserved even if the originating fact is later merged or deleted.

### Indexes

The schema maintains indexes on high-query-frequency columns: `owner_id + status` (compound, for owner-scoped queries), `content_hash` (exact dedup), `created_at` (temporal ordering), `accessed_at` (decay calculations), `session_id` (co-session lookups), and `source_fact_id` on edges (provenance joins).

### Concurrency

SQLite's WAL (Write-Ahead Logging) mode allows concurrent readers. Multiple recall queries can run simultaneously without blocking each other or blocking an in-progress extraction write. Writers still serialize -- only one write transaction at a time -- but the `busy_timeout` of 30 seconds means a blocked writer automatically retries rather than failing immediately.

In practice, extraction (write) and retrieval (read) can run simultaneously. The janitor is the only source of sustained writes, and it runs nightly when retrieval volume is low. The 64MB page cache (`cache_size = -64000`) keeps frequently-accessed embeddings and FTS indexes in memory, reducing disk I/O during concurrent access.

---

## 5. Janitor Pipeline (Nightly Maintenance)

The janitor (`janitor.py`) runs 17 tasks in a defined order, grouped by phase. It is designed to be triggered by the bot's heartbeat (which provides the API key), not standalone cron.

### Execution Order

The task numbering is historical -- tasks were added over time and the numbers reflect that. The execution order is what matters, not the labels.

**Phase 1: Preparation**

| Task | Purpose | LLM? |
|------|---------|------|
| embeddings | Backfill missing embeddings via Ollama | No |

**Phase 2: Memory Pipeline** (fail-fast -- if any task fails, remaining memory tasks are skipped)

| Task | Purpose | LLM? |
|------|---------|------|
| review | High-reasoning LLM reviews pending facts (approve/reject/rewrite) | High |
| temporal | Resolve relative dates ("last Tuesday") to absolute dates | No |
| dedup_review | Review recent dedup rejections (were they correct?) | High |
| duplicates + contradictions | Shared recall pass, then dedup (cosine > 0.85) + contradiction detection | Low |
| contradictions (resolve) | High-reasoning LLM resolves detected contradictions (keep_a/keep_b/merge) | High |
| decay | Ebbinghaus exponential confidence decay on old unused facts | No |
| decay_review | Review memories that decayed below threshold (delete/extend/pin) | High |

**Phase 3: Workspace & Docs** (independent of memory pipeline)

| Task | Purpose | LLM? |
|------|---------|------|
| workspace | Core markdown bloat monitoring + deep-reasoning LLM review | High |
| docs_staleness | Auto-update stale docs from git diffs (fast-reasoning pre-filter + deep-reasoning update) | Both |
| docs_cleanup | Clean bloated docs based on churn metrics | Low |
| snippets | Review soul snippets: FOLD (merge into file), REWRITE, or DISCARD | High |
| journal | Distill journal entries into core markdown, archive originals | High |

**Phase 4: Infrastructure** (always runs)

| Task | Purpose | LLM? |
|------|---------|------|
| rag | Reindex docs for RAG search + auto-discover new projects | No |
| tests | Run pytest suite (dev/CI only, gated by `QUAID_DEV=1`) | No |
| cleanup | Prune old logs, orphaned embeddings, stale lock files | No |
| update_check | Check for Quaid updates (version comparison) | No |

### Fail-Fast Behavior

Memory tasks (2 through 5) form a critical pipeline. If any memory task fails, all remaining memory tasks are skipped and fact graduation is blocked. Pending facts are left in place and picked up by the next successful janitor review. Non-memory tasks (workspace, docs, RAG) continue independently.

### Token-Based Batching

The `TokenBatchBuilder` groups items into batches that fit within the model's context window, accounting for both input tokens (prompt + items) and output tokens (response):

```
Available = context_window - system_prompt_tokens - output_tokens_per_item * batch_size
```

This prevents batch truncation where the model runs out of output tokens before processing all items in the batch.

### Prompt Versioning

Every LLM decision in the janitor is tagged with a prompt version: a SHA256 hash prefix of the prompt template. This enables:
- Tracking which prompt version produced which decisions
- Detecting when prompt changes cause decision drift
- Reproducing past janitor behavior for debugging

### Merge Safety

All three merge paths (dedup, contradiction resolution, review) use a shared `_merge_nodes_into()` helper that executes in a single transaction:
- Preserves the highest confidence from either node
- Sums `confirmation_count` from both nodes
- Migrates all edges from the losing node to the winner
- Sets winner status to `active`
- Inherits `owner_id` from the original nodes (not hardcoded)

### System Gates (Experimental)

Each task is gated by a system toggle in config. This feature is experimental and may change:

```python
_TASK_SYSTEM_GATE = {
    "review": "memory", "duplicates": "memory", "decay": "memory",
    "snippets": "journal", "journal": "journal",
    "docs_staleness": "projects", "rag": "projects",
    "workspace": "workspace",
    # tests, cleanup: always run (infrastructure)
}
```

### Ebbinghaus Decay

Memories that haven't been accessed within `threshold_days` (default: 30) undergo exponential confidence decay:

```
confidence *= 2^(-days_since_access / half_life)
```

The half-life is extended by access frequency (`access_bonus_factor * access_count`). Verified and pinned facts are protected from decay. Memories that decay below `minimum_confidence` (0.1) are queued for review rather than silently deleted.

---

## 6. Error Handling & Resilience

Quaid is designed to degrade gracefully rather than fail hard. The system encounters three main failure modes during operation.

### Anthropic API Unavailable

LLM calls in `llm_clients.py` use automatic retry with exponential backoff: 3 attempts with a 1-second base delay, doubling per retry. HTTP status codes 408, 429, 500, 502, 503, 504, and 529 trigger retries; other errors fail immediately. If all retries are exhausted, the function returns `(None, duration)` rather than raising -- callers check for `None` and skip LLM-dependent steps.

During extraction, an API failure means no facts are extracted for that session. The conversation transcript is not lost (it remains in the gateway's session file), but memories from that session will be missing until the next compaction or reset.

During the janitor, an API failure in any memory task (review, dedup, contradiction resolution) triggers the fail-fast mechanism: remaining memory tasks are skipped and fact graduation is blocked. Infrastructure tasks (workspace, docs, RAG) continue independently.

### Ollama Unavailable (Embeddings)

Before every embedding operation, `_ollama_healthy()` performs a 200ms health check against the Ollama API, cached for 30 seconds. If Ollama is unreachable:

- **During extraction:** Facts are stored without embeddings. The janitor's `embeddings` task (Phase 1) backfills missing embeddings on its next run.
- **During retrieval:** Vector search is skipped entirely. Retrieval falls back to FTS-only (keyword search), which still returns results but with lower recall quality. The fallback is transparent to the user.

### SQLite Lock Contention

WAL mode allows concurrent reads, but writes are serialized. The `busy_timeout` of 30 seconds means SQLite automatically retries a blocked write for up to 30 seconds before raising `OperationalError`. In practice, contention is rare -- the janitor (primary writer) runs nightly, and extraction writes are brief.

If a write does fail after the timeout, `get_connection()` catches the exception, rolls back the transaction, and re-raises. The calling code (extraction or janitor) handles the error at the task level, logging the failure and continuing with the next operation.

### General Strategy

The system follows a "store what you can, skip what you can't" philosophy. A failed embedding doesn't prevent fact storage. A failed API call doesn't crash the gateway. A failed janitor task doesn't corrupt existing data. The worst case for any single failure is a temporary reduction in recall quality, which self-heals on the next successful run.

---

## 7. Dual Learning System

Two complementary systems capture different types of learning from conversations, focused on three domains: understanding the **user**, developing the agent's own **self**-awareness, and tracking the **world** around it. They are intentionally separate -- snippets handle fast, tactical updates while journal handles slow, reflective synthesis.

### Snippets (Fast Path)

```
Conversation
    |
    v
High-reasoning LLM extracts bullet-point observations ("soul_snippets")
    |
    v
Written to *.snippets.md (per target file, e.g. SOUL.snippets.md)
    |
    v
Nightly janitor (task 1d-snippets):
    High-reasoning LLM reviews each snippet against the target file:
    - FOLD: merge into the file at the right location
    - REWRITE: snippet needs refinement before folding
    - DISCARD: redundant or low-value
    |
    v
Approved snippets merged into SOUL.md, USER.md, MEMORY.md
```

Snippet format (in `*.snippets.md`):
```markdown
## Compaction - 2026-02-12 14:30

- Prefers to work in focused 2-hour blocks
- Gets frustrated when interrupted mid-thought
```

### Journal (Slow Path)

```
Conversation
    |
    v
High-reasoning LLM extracts diary-style paragraphs ("journal_entries")
    |
    v
Written to journal/*.journal.md (newest entries at top)
    |
    v
Weekly (configurable) deep-reasoning LLM distillation:
    Reads accumulated journal entries for each target file,
    synthesizes themes, and proposes edits to core markdown
    |
    v
Distilled insights merged into core markdown
Processed entries archived to journal/archive/{FILE}-{YYYY-MM}.md
```

Journal format (in `journal/SOUL.journal.md`):
```markdown
## 2026-02-12 - Compaction

Today's conversation revealed a pattern I've been developing around...
Today's debugging session revealed a pattern worth noting...
```

### Why Two Systems?

- **Snippets** are for continuous small lessons that should update core files frequently. "User prefers dark mode" goes directly into USER.md.
- **Journal** is for long-term pattern recognition. A single journal entry about a debugging conversation isn't actionable, but after 10 entries about debugging style, the deep-reasoning LLM can synthesize: "The user approaches bugs systematically -- reproduces first, then bisects."

Both systems dedup by date+trigger per file (at most one Compaction entry and one Reset entry per day per target file).

---

## 8. Projects System

The projects system tracks documentation across the codebase and keeps it up to date automatically.

### Components

**Doc Registry** (`docs_registry.py`): SQLite-backed CRUD for documents and projects. Maps file paths to projects, tracks document metadata (type, title, last indexed), and provides CLI for management.

```bash
docs_registry.py create-project myproject --label "My Project"
docs_registry.py register path/to/doc.md --project myproject
docs_registry.py find-project path/to/file.py   # Which project owns this file?
docs_registry.py discover --project myproject    # Auto-discover docs in project dir
```

**Doc Auto-Update** (`docs_updater.py`): Detects when documentation has drifted from the code it describes. Uses a two-stage filter:
1. **Low-reasoning pre-filter**: Classifies git diffs as "trivial" (whitespace, comments) or "significant" (logic changes). Trivial diffs skip the expensive update step.
2. **High-reasoning update**: Reads the stale doc + relevant diffs, proposes targeted edits.

**RAG Search** (`docs_rag.py`): Chunks project documentation, embeds via Ollama, and provides semantic search. Chunks are sized by token count (default: 800 tokens with 100-token overlap) and split on section headers.

**Project Updater** (`project_updater.py`): Processes file change events and refreshes project documentation. Integrates with Claude Code hooks (PostToolUse tracks edited files, PreCompact stages update events).

### Auto-Discovery

Each project directory under `projects/` can contain:
- `PROJECT.md` -- Generated project documentation (scaffolded by `create-project`)
- `TOOLS.md` -- API docs and credentials (loaded into agent context via gateway glob)
- `AGENTS.md` -- Operational guide (loaded into agent context via gateway glob)

Projects are auto-discovered during RAG reindexing (janitor task 7).

---

## 9. Configuration

### Central Config File

All configuration lives in `config/memory.json` and is loaded into a hierarchy of Python dataclasses.

### Dataclass Hierarchy

```
MemoryConfig (root)
  +-- systems: SystemsConfig         # 4 toggleable system gates
  +-- models: ModelsConfig           # deep/fast model tiers + provider model-class mappings
  +-- capture: CaptureConfig         # Extraction settings
  +-- decay: DecayConfig             # Ebbinghaus decay parameters
  +-- janitor: JanitorConfig
  |     +-- opus_review: OpusReviewConfig
  |     +-- dedup: DedupConfig       # Similarity thresholds (0.85 / 0.95 / 0.98)
  |     +-- contradiction: ContradictionConfig
  +-- retrieval: RetrievalConfig
  |     +-- traversal: TraversalConfig  # BEAM search parameters
  +-- logging: LoggingConfig
  +-- docs: DocsConfig
  |     +-- core_markdown: CoreMarkdownConfig
  |     +-- journal: JournalConfig   # Snippets + journal settings
  +-- projects: ProjectsConfig
  +-- users: UsersConfig             # Owner identity mapping
  +-- database: DatabaseConfig
  +-- ollama: OllamaConfig           # Embedding model, URL, dimensions
  +-- rag: RagConfig                 # Chunk sizes, search limits
  +-- notifications: NotificationsConfig
```

### System Gates

Four toggleable systems control which features are active:

```python
@dataclass
class SystemsConfig:
    memory: bool = True       # Extract and recall facts from conversations
    journal: bool = True      # Track personality evolution via snippets + journal
    projects: bool = True     # Auto-update project docs from code changes
    workspace: bool = True    # Monitor core markdown file health
```

These gates are checked by both the TypeScript plugin (`isSystemEnabled()`) and the Python janitor (`_system_enabled_or_skip()`).

### Environment Overrides

| Variable | Purpose |
|----------|---------|
| `QUAID_HOME` | Root directory for standalone mode (default `~/quaid/`) |
| `adapter.type` (in `config/memory.json`) | Select adapter: `standalone` or `openclaw` (required) |
| `QUAID_OWNER` | Owner identity for MCP server and CLI (default `"default"`) |
| `CLAWDBOT_WORKSPACE` | Root workspace hint for OpenClaw integrations |
| `MEMORY_DB_PATH` | Override database path |
| `OLLAMA_URL` | Override Ollama endpoint |
| `QUAID_DEV` | Enable dev-only features (e.g. test task in janitor) |
| `QUAID_QUIET` | Suppress config loading messages |
| Gateway auth/provider config | Primary LLM auth source in OpenClaw mode (OAuth or API key, provider-specific) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Optional direct env vars (standalone/testing paths) |

### Config Loading

`get_config()` caches globally and uses a re-entrancy guard to break circular dependency chains:

```python
# load_config() -> get_db_path() -> _get_cfg() -> load_config()
# The re-entrancy guard returns MemoryConfig() defaults to break the cycle.
_config_loading: bool = False

def load_config() -> MemoryConfig:
    if _config is not None:
        return _config
    if _config_loading:
        return MemoryConfig()  # Break circular dependency
    _config_loading = True
    try:
        return _load_config_inner()
    finally:
        _config_loading = False
```

Config file search order:
1. `$QUAID_HOME/config/memory.json` (or `$CLAWDBOT_WORKSPACE/config/memory.json` when set)
2. `~/.quaid/memory-config.json`
3. `./memory-config.json`

### Known Gotcha

The `coreMarkdown.files` section has filename keys like `"SOUL.md"`. The `camelCase -> snake_case` conversion in the config loader would corrupt these to `"s_o_u_l.md"`. The loader uses `raw_config` directly for this section.

### Multi-Provider LLM Support

#### Abstraction Layer

`llm_clients.py` provides two functions that abstract over the underlying model:

```python
def call_deep_reasoning(prompt, max_tokens=4000, timeout=120, system_prompt=None):
    """High-reasoning model. Used for fact review, contradiction resolution,
    workspace audits, journal distillation."""

def call_fast_reasoning(prompt, max_tokens=500, timeout=30, system_prompt=None):
    """Low-reasoning model. Used for dedup verification, reranking,
    HyDE expansion, doc pre-filtering."""
```

Both return `(response_text, duration_seconds)`. Model selection is config-driven -- callers never reference specific model IDs.

#### Provider and Model-Class Support

```python
@dataclass
class ModelsConfig:
    llm_provider: str = "default"   # gateway active provider, or explicit provider id
    deep_reasoning: str = "default"
    fast_reasoning: str = "default"
    deep_reasoning_model_classes: Dict[str, str] = {...}
    fast_reasoning_model_classes: Dict[str, str] = {...}
```

#### API Key Resolution

Quaid routes LLM calls through its adapter/provider abstraction:
1. In OpenClaw mode, the adapter resolves provider + model tier and routes via gateway provider auth/state.
2. In standalone/testing flows, environment variables and local config can be used directly.
3. Callers outside the adapter/provider layer remain provider-agnostic and only request deep/fast reasoning tiers.

#### Prompt Caching

Prompt caching is used for system prompts that repeat across calls (e.g. the janitor review prompt). This yields approximately 90% token savings on the cached portion of repeated prompts. Currently supported with Anthropic's API; other providers may benefit from similar caching mechanisms.

#### Token Usage Tracking

Every LLM call accumulates into per-run counters (`_usage_input_tokens`, `_usage_output_tokens`, `_usage_cache_read_tokens`). The janitor resets these at run start and reports total cost at the end. Pricing is maintained per model ID.

### Notifications

Quaid includes a notification system (`notify.py`) that sends status updates for extraction, retrieval, janitor runs, and documentation changes. Notifications support four verbosity levels (quiet, normal, verbose, debug) with per-feature overrides, and are routed to the user's last active communication channel. To change the notification level, ask your agent or edit `config/memory.json`. For full details on verbosity presets, channel routing, and per-feature configuration, see [AI-REFERENCE.md](AI-REFERENCE.md).

---

## 10. MCP Server

The MCP server (`mcp_server.py`) exposes Quaid's memory system as tools over the [Model Context Protocol](https://modelcontextprotocol.io) stdio transport. This is the primary integration path for clients that support MCP (Claude Desktop, Claude Code, Cursor, Windsurf, etc.).

### Architecture

```
MCP Client (Claude Desktop, Cursor, etc.)
    |
    | JSON-RPC over stdio
    |
    v
mcp_server.py (FastMCP)
    |
    +-- memory_extract  -> extract.py -> llm_clients + memory_graph
    +-- memory_store    -> api.store()
    +-- memory_recall   -> api.recall()
    +-- memory_search   -> api.search()
    +-- memory_get      -> api.get_memory()
    +-- memory_forget   -> api.forget()
    +-- memory_create_edge -> api.create_edge()
    +-- memory_stats    -> api.get_graph().stats()
    +-- docs_search     -> docs_rag.search()
```

### Tools

| Tool | Purpose | API Cost |
|------|---------|----------|
| `memory_extract` | Full Opus-powered extraction from transcript | ~$0.05-0.20 |
| `memory_store` | Store a single fact | Free (local embedding only) |
| `memory_recall` | Full retrieval pipeline (HyDE + reranking) | ~$0.01 (reranker) |
| `memory_search` | Fast keyword search (no reranking) | Free |
| `memory_get` | Fetch a memory by ID | Free |
| `memory_forget` | Delete a memory | Free |
| `memory_create_edge` | Create a relationship edge | Free |
| `memory_stats` | Database statistics | Free |
| `docs_search` | Search project documentation via RAG | Free |

### Stdout Isolation

MCP uses stdout for JSON-RPC communication. The server redirects Python's `sys.stdout` to `sys.stderr` at startup to prevent stray print statements from corrupting the protocol stream. This is critical because several modules (`memory_graph.py`, `lib/embeddings.py`) print status messages during normal operation.

### Owner Identity

The `QUAID_OWNER` environment variable sets the owner identity for all operations. This maps to the `owner_id` field in the database, enabling multi-user setups where each user's memories are namespaced.

---

## Appendix: File Reference

The plugin lives in `plugins/quaid/` with Python modules for graph operations (`memory_graph.py`), extraction (`extract.py`), MCP server (`mcp_server.py`), nightly maintenance (`janitor.py`), dual learning (`soul_snippets.py`), and configuration (`config.py`). The OpenClaw TypeScript entry point (`adapters/openclaw/index.ts` / `adapters/openclaw/index.js`) registers gateway hooks and tools. Shared utilities live in `lib/`. Prompt templates live in `prompts/`.

For the complete file index with function signatures, database schema, CLI reference, and environment variables, see [AI-REFERENCE.md](AI-REFERENCE.md).
