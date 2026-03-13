# Memory-Local Plugin Implementation — Total Recall (quaid)
<!-- PURPOSE: Implementation details: schema, modules, config, shared lib, CLI, hooks, test suite, projects system -->
<!-- SOURCES: datastore/memorydb/memory_graph.py, adaptors/openclaw/adapter.ts, datastore/docsdb/rag.py, config.py, datastore/docsdb/registry.py, datastore/docsdb/project_updater.py, config/memory.json -->

**Status:** Production Ready (updated 2026-02-08)
**Location:** `modules/quaid/`
**Codename:** Total Recall (quaid)

---

## What Was Built

### 1. SQLite Schema (`schema.sql`)

Graph database schema with:

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

---

### 2. Core Graph Operations (`memory_graph.py`)

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

**BEAM Search (graph traversal enhancement):**
- BEAM search replaces naive BFS for graph traversal, using scored frontier expansion
- **Adaptive LLM reranking** — during BEAM traversal, an LLM reranker scores candidate nodes for relevance to the original query, pruning low-value paths early
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

---

### 3. Initialization and Migrations

Schema initialization and migrations are performed by `memory_graph.py` at startup (`init_database()` path), not by a separate `seed.py` script.

---

### 4. Plugin Entry Point (`adaptors/openclaw/adapter.ts`)

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

---

### 5. Memory Recall — Agent-Driven (2026-02-06)

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

---

### 5b. Auto-Injection Pipeline (Optional)

> The following describes the automatic injection pipeline used when auto-inject is enabled.

The injection pipeline runs before agent start, filtering and reranking memories before injection. Agent-driven recall via the `memory_recall` tool remains the primary path.

---

### 6. Auto-Capture (agent_end) — Inactivity Timeout

> **Per-message classifier is deprecated.** The inactivity-timeout extraction remains active when capture is enabled.

The previous per-message approach (Haiku classifier on each message pair) was:
- Expensive: called on every `agent_end`, including heartbeats
- Low context: only saw the last user + assistant message pair
- Noisy: extracted many system/infrastructure facts that required janitor cleanup

**Current extraction** happens in `before_compaction` and `before_reset` hooks via Opus (full transcript), plus inactivity-timeout extraction when enabled. **Combined fact+edge extraction** performs both fact extraction and relationship detection in a chunked deep-reasoning loop for efficiency and context-window safety.

Capture timeout config keys:
- `capture.inactivity_timeout_minutes` (default `120`): minutes of inactivity before timeout extraction runs (`0` disables timeout extraction).
- `capture.auto_compaction_on_timeout` (default `true`): when timeout extraction runs, whether to trigger gateway compaction automatically after extraction.

---

### 7. Shared Library (`lib/`)

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
| `datastore/memorydb/archive_store.py` | `archive_node(node, reason)` / `search_archive(query)` — datastore-owned archive writes/reads for `data/memory_archive.db` (`lib/archive.py` remains a compatibility shim). |

All shared library modules use `__all__` exports to define explicit public API boundaries, ensuring clean module interfaces and preventing accidental coupling to internal helpers.

**Provider/adapter pattern:** LLM and embedding functionality is routed through adapters/providers. The core memory pipeline does not choose providers directly; it requests `deep_reasoning` or `fast_reasoning` and the adapter resolves provider + model from config and gateway state.

**Ingest runtime bridge (`core/ingest_runtime.py`):** Runtime-safe ingest entrypoints that keep core decoupled from ingest module internals. Three public functions:
- `run_docs_ingest(transcript_path, label, session_id)` — triggers docs ingest pipeline
- `run_session_logs_ingest(*, session_id, owner_id, label, ...)` — records session log metadata
- `run_extract_from_transcript(*, transcript, owner_id, label, dry_run)` — runs fact extraction on a transcript string

Core orchestrators import ingest via this bridge rather than importing `ingest.*` modules directly.

**Global LLM scheduler (`core/llm/scheduler.py`):** Singleton `GlobalLlmScheduler` that centralizes timeout-driven throttling and adaptive concurrency for all LLM-parallel call sites. Key behaviors:
- `run_map(workload_key, items, fn, configured_workers, ...)` — parallel map with per-workload adaptive concurrency caps
- **Timeout + backoff:** On timeout, halves the worker cap for that workload key and retries remaining items (up to `timeout_retries` attempts; raises `TimeoutError` after exhaustion)
- **Slow release:** After successful completion, increments the cap back toward `configured_workers` by 1
- `QUAID_GLOBAL_LLM_MAX_WORKERS` env var — overrides the global thread pool ceiling (default 32)
- Singleton access via `get_global_llm_scheduler()`; `reset_global_llm_scheduler()` shuts down and clears it (called at process exit via `atexit`)

### 8. Configuration System

**Config layer architecture — three-layer merge chain:**

Config is loaded by merging three sources (highest priority wins, then falls through):

| Priority | Path | Purpose |
|----------|------|---------|
| **1 (highest)** | `QUAID_HOME/<instance>/config/memory.json` | Per-instance overrides (identity, capture timeouts, domain preferences) |
| **2** | `QUAID_HOME/shared/config/memory.json` | Machine-wide shared settings (embeddings model, Ollama URL) |
| **3 (lowest)** | Built-in defaults | Baseline defaults compiled into `config.py` |

Rules:
- The shared config is written by the first installer; subsequent instances on the same machine inherit it automatically.
- Embeddings config (`ollama.*`, `embeddings.*`) must live in shared config so all instances use the same model and produce comparable embeddings.
- Instance config holds per-adapter settings that should differ between instances (identity, session timeouts, retrieval preferences).
- A local `./memory-config.json` in the cwd can override everything (rarely used; intended for local dev/testing only).

**Config CLI commands:**
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

**`core.parallel` config** (controls lifecycle routine concurrency and resource locking):

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

**Embedding model config (machine-wide / shared):**

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

**Provider/model config**:
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

**Search config** (includes BEAM and reranker settings):
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

**Retrieval config** (updated):
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

**User identity config** includes `personNodeName`:
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

**Bootstrap file monitoring** — workspace monitoring uses a built-in bootstrap file set (`AGENTS.md`, `SOUL.md`, `TOOLS.md`, `USER.md`, `MEMORY.md`, `IDENTITY.md`, `HEARTBEAT.md`, `TODO.md`, `PROJECT.md`) plus adapter-provided additions. There is no `janitor.bootstrapFiles` config key.

**Logging config:**
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

`default` model resolution is adapter/gateway-driven:

- Tier requests (`deep_reasoning`/`fast_reasoning`) are resolved in `adaptors/openclaw/adapter.ts`.
- Provider resolution uses `models.llmProvider` + active gateway provider state.
- Tier model resolution uses `models.deepReasoning` / `models.fastReasoning`; if either is `default`, Quaid looks up `models.deepReasoningModelClasses` / `models.fastReasoningModelClasses`.
- Missing provider mappings fail loudly (no implicit hardcoded provider fallback).

See `projects/quaid/reference/adapter-provider-architecture.md` for the canonical provider/model flow.

---

### 9. Instance Management

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
