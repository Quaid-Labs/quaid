# Quaid Architecture Guide

Quaid is a memory system plugin for OpenClaw-compatible AI assistants. It extracts personal facts from conversations, stores them in a local SQLite graph, retrieves them when relevant, and maintains quality through a nightly janitor pipeline. Everything runs locally -- no cloud memory services, no external databases.

This document is for engineers who want to understand how the system works.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Extraction Pipeline](#2-extraction-pipeline)
3. [Retrieval Pipeline](#3-retrieval-pipeline)
4. [Database](#4-database)
5. [Janitor Pipeline](#5-janitor-pipeline)
6. [Dual Learning System](#6-dual-learning-system)
7. [Projects System](#7-projects-system)
8. [Configuration](#8-configuration)
9. [Multi-Provider LLM Support](#9-multi-provider-llm-support)
10. [Notifications](#10-notifications)

---

## 1. System Overview

Quaid sits between the OpenClaw gateway and the AI agent. The gateway exposes lifecycle hooks (compaction, reset, agent turn) that Quaid uses to inject and extract memories transparently.

### High-Level Architecture

```
                          OpenClaw Gateway
                               |
               +---------------+---------------+
               |                               |
        before_compaction                 before_agent_start
        before_reset                      (inject recalled memories)
               |                               |
        +------v------+                +-------v-------+
        |  Extraction  |                |   Retrieval   |
        |  Pipeline    |                |   Pipeline    |
        +--------------+                +---------------+
               |                               |
               +---------- SQLite DB ----------+
                               |
                        +------v------+
                        |   Janitor   |
                        |  (nightly)  |
                        +-------------+
```

### Three Main Loops

**Extraction** -- Fires at context compaction or session reset. A high-reasoning LLM call extracts structured facts and relationship edges from the conversation transcript. Facts enter the database as `pending` and await janitor review before graduating to `active`.

**Retrieval** -- Fires before each agent turn. The agent's query is classified by intent, expanded via HyDE, and searched across three channels (vector, FTS5, graph). Results are fused via RRF, reranked by a low-reasoning LLM, and injected into the agent's context as `[MEMORY]`-prefixed messages.

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

Extraction is triggered by two gateway hooks registered in `index.ts`:

- **`before_compaction`** -- When the agent's context is being compacted (too many tokens). The full message history is available before it gets summarized.
- **`before_reset`** -- When a session ends or the user starts a new conversation.

Both hooks call the same `extractMemoriesFromMessages()` function.

### Extraction Steps

1. **Message collection** -- The gateway provides the full message array. Any queued memory notes (from the `memory_note` tool) are prepended to the transcript.

2. **LLM extraction** -- A single high-reasoning LLM call processes the transcript and produces:
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

Edges are normalized to canonical forms to prevent duplicates like `child_of(A, B)` and `parent_of(B, A)` coexisting. Normalization is maintained by the janitor pipeline and arbitrated by a high-reasoning LLM when ambiguity exists:

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
[4] HyDE expansion: low-reasoning LLM rephrases question as declarative statement
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
- When candidates exceed beam width (truncation needed), the top 2*B candidates are re-scored via a low-reasoning LLM reranker in a single batched API call
- Score decays by `hop_decay` (0.7) per level: a 2-hop result gets 0.49x the score of a direct hit

### RRF Fusion

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

After fusion, the top 20 candidates are sent to a low-reasoning LLM in a single API call for graded relevance scoring (0-5 scale). The reranker score is blended with the original score at a configurable ratio. Benchmarks showed the LLM reranker contributes significantly to accuracy, so the blend favors the reranker score:

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

---

## 4. Database

### Storage Engine

SQLite with WAL mode, foreign keys enabled, and a busy timeout for concurrent access. The database is fully local -- no network dependencies.

Connection management is centralized in `lib/database.py`:
```python
@contextmanager
def get_connection(db_path=None):
    """Returns a connection with Row factory, FK enforcement, and WAL mode."""
    conn = sqlite3.connect(db_path or get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
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

---

## 5. Janitor Pipeline (Nightly Maintenance)

The janitor (`janitor.py`) runs 16 tasks in a defined order, grouped by phase. It is designed to be triggered by the bot's heartbeat (which provides the API key), not standalone cron.

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
| workspace | Core markdown bloat monitoring + high-reasoning LLM review | High |
| docs_staleness | Auto-update stale docs from git diffs (low-reasoning pre-filter + high-reasoning update) | Both |
| docs_cleanup | Clean bloated docs based on churn metrics | Low |
| snippets | Review soul snippets: FOLD (merge into file), REWRITE, or DISCARD | High |
| journal | Distill journal entries into core markdown, archive originals | High |

**Phase 4: Infrastructure** (always runs)

| Task | Purpose | LLM? |
|------|---------|------|
| rag | Reindex docs for RAG search + auto-discover new projects | No |
| tests | Run pytest suite (dev/CI only, gated by `QUAID_DEV=1`) | No |
| cleanup | Prune old logs, orphaned embeddings, stale lock files | No |

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

## 6. Dual Learning System

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
Weekly (configurable) high-reasoning LLM distillation:
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
- **Journal** is for long-term pattern recognition. A single journal entry about a debugging conversation isn't actionable, but after 10 entries about debugging style, the high-reasoning LLM can synthesize: "The user approaches bugs systematically -- reproduces first, then bisects."

Both systems dedup by date+trigger per file (at most one Compaction entry and one Reset entry per day per target file).

---

## 7. Projects System

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

## 8. Configuration

### Central Config File

All configuration lives in `config/memory.json` and is loaded into a hierarchy of Python dataclasses.

### Dataclass Hierarchy

```
MemoryConfig (root)
  +-- systems: SystemsConfig         # 4 toggleable system gates
  +-- models: ModelConfig            # LLM provider, model IDs, context windows
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
| `CLAWDBOT_WORKSPACE` | Root workspace path (critical for tests and isolated environments) |
| `MEMORY_DB_PATH` | Override database path |
| `OLLAMA_URL` | Override Ollama endpoint |
| `QUAID_DEV` | Enable dev-only features (e.g. test task in janitor) |
| `QUAID_QUIET` | Suppress config loading messages |
| `ANTHROPIC_API_KEY` | LLM API key (primary) |

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
1. `$CLAWDBOT_WORKSPACE/config/memory.json`
2. `~/.clawdbot/memory-config.json`
3. `./memory-config.json`

### Known Gotcha

The `coreMarkdown.files` section has filename keys like `"SOUL.md"`. The `camelCase -> snake_case` conversion in the config loader would corrupt these to `"s_o_u_l.md"`. The loader uses `raw_config` directly for this section.

---

## 9. Multi-Provider LLM Support

### Abstraction Layer

`llm_clients.py` provides two functions that abstract over the underlying model:

```python
def call_high_reasoning(prompt, max_tokens=4000, timeout=120, system_prompt=None):
    """High-reasoning model. Used for fact review, contradiction resolution,
    workspace audits, journal distillation."""

def call_low_reasoning(prompt, max_tokens=500, timeout=30, system_prompt=None):
    """Low-reasoning model. Used for dedup verification, reranking,
    HyDE expansion, doc pre-filtering."""
```

Both return `(response_text, duration_seconds)`. Model selection is config-driven -- callers never reference specific model IDs.

### Provider Support

```python
@dataclass
class ModelConfig:
    provider: str = "anthropic"     # "anthropic" or "openai" (OpenAI-compatible APIs)
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: Optional[str] = None  # Custom endpoint (e.g. OpenRouter, local server)
    low_reasoning: str = "claude-haiku-4-5"
    high_reasoning: str = "claude-opus-4-6"
```

### API Key Resolution

Quaid uses pass-through API keys from the main system agent -- it does not manage keys directly. Keys are resolved in priority order:
1. Environment variable (`ANTHROPIC_API_KEY`) -- typically set by the gateway or agent runtime
2. `.env` file in workspace root (fallback for standalone CLI use)

### Prompt Caching

Prompt caching is used for system prompts that repeat across calls (e.g. the janitor review prompt). This yields approximately 90% token savings on the cached portion of repeated prompts. Currently supported with Anthropic's API; other providers may benefit from similar caching mechanisms.

### Token Usage Tracking

Every LLM call accumulates into per-run counters (`_usage_input_tokens`, `_usage_output_tokens`, `_usage_cache_read_tokens`). The janitor resets these at run start and reports total cost at the end. Pricing is maintained per model ID.

---

## 10. Notifications

The notification system (`notify.py`) sends status updates to the user's last active communication channel.

### Notification Types

| Function | Trigger | Content |
|----------|---------|---------|
| `notify_memory_extraction()` | After extraction | Count of facts/edges extracted, janitor health check |
| `notify_memory_recall()` | After retrieval | Recalled memories with similarity scores |
| `notify_janitor_summary()` | After janitor run | Task results, token usage, cost breakdown |
| `notify_daily_memories()` | On scheduled trigger | Daily memory digest |
| `notify_doc_update()` | After doc auto-update | Which docs changed and why |
| `notify_docs_search()` | After RAG search | Search results with scores |

### Verbosity Levels

Notifications use a hierarchical verbosity system:

```python
@dataclass
class NotificationsConfig:
    level: str = "normal"  # quiet, normal, verbose, debug

    # Per-feature overrides:
    janitor: FeatureNotificationConfig    # default: summary
    extraction: FeatureNotificationConfig # default: off
    retrieval: FeatureNotificationConfig  # default: summary
```

Level presets:

| Level | Janitor | Extraction | Retrieval |
|-------|---------|------------|-----------|
| quiet | off | off | off |
| normal | summary | off | summary |
| verbose | summary | summary | summary |
| debug | full | full | full |

Per-feature overrides take precedence over the master level.

To change the notification level, ask your agent ("change notification level to quiet") or edit `config/memory.json` directly.

### Channel Routing

Notifications are delivered to the user's last active channel by default. Each feature can override to a specific channel:

```json
{
  "notifications": {
    "level": "normal",
    "janitor": { "verbosity": "summary", "channel": "telegram" },
    "extraction": { "verbosity": "off" }
  }
}
```

### Janitor Health Check

During extraction notifications, the system performs a lightweight DB check for the last successful janitor run. If the janitor hasn't run in 48+ hours, a warning is appended advising the user to check their heartbeat configuration.

---

## Appendix: File Map

| File | Purpose |
|------|---------|
| `index.ts` | Plugin entry point: gateway hooks, tool registration, TypeScript |
| `memory_graph.py` | Core graph operations: `store()`, `recall()`, `search_hybrid()`, `beam_search_graph()` |
| `janitor.py` | Nightly maintenance pipeline: 17 tasks, `run_task_optimized()` |
| `soul_snippets.py` | Dual snippet + journal system |
| `config.py` | Configuration dataclasses and loader |
| `llm_clients.py` | LLM abstraction: `call_high_reasoning()`, `call_low_reasoning()` |
| `notify.py` | User notification builders and delivery |
| `docs_registry.py` | Project/document CRUD and path resolution |
| `docs_updater.py` | Git-diff-based doc staleness detection and updates |
| `docs_rag.py` | RAG: chunking, embedding, semantic search over docs |
| `project_updater.py` | Background project event processor |
| `workspace_audit.py` | Core markdown bloat monitoring |
| `schema.sql` | Database DDL (authoritative schema) |
| `lib/config.py` | Path resolution, env overrides |
| `lib/database.py` | SQLite connection factory |
| `lib/embeddings.py` | Ollama embedding calls |
| `lib/similarity.py` | Cosine similarity |
| `lib/tokens.py` | Token counting, text comparison |
