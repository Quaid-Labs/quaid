# Quaid RAG and Docs System — Technical Reference

**Source files:**
- `datastore/docsdb/rag.py` — indexing, chunking, search (`DocsRAG`)
- `datastore/docsdb/registry.py` — doc and project registry (`DocsRegistry`)
- `datastore/docsdb/updater.py` — staleness detection and doc auto-update
- `core/project_docs.py` + `core/project_docs_supervisor.py` — supervisor-owned project docs refresh
- `datastore/docsdb/project_updater.py` — append-only PROJECT.log and PROJECT.md registry-section helpers

---

## 1. System Overview

The docs system has four tightly integrated components:

| Component | Class / Module | Storage | Purpose |
|-----------|---------------|---------|---------|
| Doc registry | `DocsRegistry` | `doc_registry` (SQLite) | Tracks which files belong to which projects; maps source files to docs |
| Project definitions | `DocsRegistry` | `project_definitions` (SQLite) | Canonical project config (seeded from instance `config.json`, then DB is source of truth) |
| RAG indexer | `DocsRAG` | `doc_chunks` + `vec_doc_chunks` (SQLite + sqlite-vec) | Chunks files, batches embeddings, serves bounded semantic search |
| Staleness detector / updater | `updater.py` | `logs/docs-update-log.json` | Detects when source code has drifted ahead of docs, calls the configured deep-reasoning model to rewrite |
| Project docs worker | `core/project_docs*.py` | `data/project-docs/` | Processes shadow-git and PROJECT.log deltas, updates visible project docs, syncs registry |

All components share a single SQLite database at `QUAID_HOME/instances/<instance>/data/memory.db`
(path from `lib/config.get_db_path()`). Packed chunk embeddings are stored in
`doc_chunks.embedding`, and when `sqlite-vec` is available a companion
`vec_doc_chunks` virtual table mirrors those embeddings for bounded KNN recall.

---

## 2. SQLite Schema

### `doc_chunks` — RAG index

Created by `DocsRAG._ensure_schema()`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | `{source_file}:{chunk_index}` |
| `source_file` | TEXT | Absolute path to the indexed file |
| `chunk_index` | INTEGER | 0-based position within the file |
| `content` | TEXT | Chunk text content |
| `section_header` | TEXT | First H1/H2/H3 header found in chunk (nullable) |
| `embedding` | BLOB | float32 array, packed for reuse and vec sync |
| `created_at` | TEXT | UTC ISO datetime |
| `updated_at` | TEXT | UTC ISO datetime — used by `needs_reindex()` / `needs_reindex_many()` |

Indexes: `idx_doc_chunks_source` (source_file), `idx_doc_chunks_updated` (updated_at).

Change detection: `needs_reindex_many()` batches `MAX(updated_at)` lookups for
many files at once, and `needs_reindex()` delegates to the same UTC mtime
comparison logic for one file. There is no SHA hash gate in the table; mtime is
still the authoritative staleness check.

### `vec_doc_chunks` — bounded semantic recall index

Created lazily by `DocsRAG._ensure_doc_vec_table()` when `sqlite-vec` is
available and the first document is indexed.

```sql
CREATE VIRTUAL TABLE vec_doc_chunks
USING vec0(
    chunk_id TEXT PRIMARY KEY,
    embedding float[DIM] distance_metric=cosine
);
```

Runtime also creates companion tables such as `vec_doc_chunks_rowids` and
`vec_doc_chunks_chunks00`. These are implementation tables owned by
`sqlite-vec`, not an additional app-level schema.

### `doc_registry` — registered documents

Created by `DocsRegistry.ensure_table()`.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `file_path` | TEXT UNIQUE | Workspace-relative or absolute path |
| `project` | TEXT | Owning project name (default: `'default'`) |
| `asset_type` | TEXT | `'doc'`, `'source'`, etc. |
| `title` | TEXT | Display title |
| `description` | TEXT | Purpose description (used by updater as `purpose` context) |
| `tags` | TEXT | JSON list |
| `state` | TEXT | `'active'` or `'deleted'` (soft-delete) |
| `auto_update` | INTEGER | 1 = participates in staleness checks via `get_source_mappings()` |
| `source_files` | TEXT | JSON list of source paths that drive this doc |
| `last_indexed_at` | TEXT | ISO datetime — set after successful RAG index |
| `last_modified_at` | TEXT | ISO datetime — set after successful doc update |
| `registered_at` | TEXT | ISO datetime |
| `registered_by` | TEXT | Who registered (e.g., `'system'`, `'create-project'`) |
| `source_channel` / `source_conversation_id` / ... | TEXT | Identity/provenance columns (additive, forward-compat) |

Indexes: project, state, asset_type, source scope, subject/state.

### `project_definitions` — project definitions

| Column | Type | Description |
|--------|------|-------------|
| `name` | TEXT PK | Project name (e.g., `'quaid'`) |
| `label` | TEXT | Human-readable label |
| `home_dir` | TEXT | Workspace-relative path to project root |
| `source_roots` | TEXT | JSON list of source root paths |
| `auto_index` | INTEGER | Whether project/docs maintenance may auto-discover files |
| `patterns` | TEXT | JSON list of glob patterns (default `["*.md"]`) |
| `exclude` | TEXT | JSON list of exclude patterns |
| `description` | TEXT | Project description |
| `state` | TEXT | `'active'`, `'archived'`, or `'deleted'` |
| `created_at` / `updated_at` | TEXT | ISO datetimes |

**Bootstrap:** On first instantiation (empty table), `_seed_projects_from_json()` reads instance `config.json` and imports `projects.definitions`. After seeding, the DB is the authoritative source; JSON is ignored.

---

## 3. Indexing Pipeline

### How a file gets from disk into searchable RAG

**Step 1: Registration**

```bash
quaid registry register <file_path> --project <name> --description "..."
```

This calls `DocsRegistry.register()`, inserting a row into `doc_registry`. The file is not yet indexed.

**Step 2: RAG maintenance trigger**

Automatic project-docs indexing is owned by the project-docs worker. After a
docs apply, the worker syncs visible project docs and reindexes the changed
registered docs for that project.

Manual/debug indexing still exists:

```bash
cd <module_root>
PYTHONPATH=. python3 datastore/docsdb/rag.py reindex [--all] [--dir <path>]
```

Janitor no longer exposes `--task rag`; its project-docs role is to queue async
monitor requests through `project_docs_monitor`.

**Step 3: Project-docs worker sync**

After applying project docs changes, the worker:

1. Registers new visible project docs under `PROJECT.md`, `TOOLS.md`,
   `AGENTS.md`, and `docs/**/*.md`.
2. Unregisters project-doc files deleted by the updater apply transaction.
3. Refreshes managed `PROJECT.md` registry/navigation sections.
4. Reindexes registered docs for the project through the docs datastore.

Project-doc workers, not janitor, own source/log-driven documentation updates.

**Step 4: batched staleness checks**

```python
def needs_reindex_many(self, file_paths: List[str]) -> Dict[str, bool]
```

Reads `st_mtime` from disk (UTC), batches `MAX(updated_at)` queries against
`doc_chunks`, and returns a per-file `True/False` map. `needs_reindex(file_path)`
is still available for one-off callers, but updater and project-docs paths use
the batched path so large reindex passes do not issue one SQL query per file. On
table-missing or stat errors, the implementation returns `True` (reindex when
in doubt).

**Step 5: `index_document(file_path)` — atomic index-and-replace**

```python
def index_document(self, file_path: str) -> int  # returns chunk count
```

1. Reads file content (UTF-8).
2. If file is in `log/*.log` (archive log), prepends a temporal context header via `_archive_temporal_header()`.
3. Calls `chunk_markdown(content)` to produce a list of text chunks.
4. Calls `_lib_get_embeddings(chunk_texts, pool_name="rag_embeddings", task_name="rag")`. Duplicate chunk texts are deduped in the shared embedding helper before provider calls.
5. If any chunk embedding fails, aborts without deleting old chunks (preserves stale but working index).
6. Only after all embeddings succeed: deletes old `doc_chunks` rows for this file, bulk-inserts new rows, and when `sqlite-vec` is available synchronizes `vec_doc_chunks` to exactly the new chunk ids for that file.
6. Calls `DocsRegistry.update_timestamps(file_path, indexed_at=now)` to sync `last_indexed_at`.

Returns the number of chunks created (0 on any failure).

---

## 4. Chunking Strategy

`DocsRAG.chunk_markdown(content, max_tokens=None)` uses header-boundary chunking:

- **Header splits:** Any line matching `^(#{1,3})\s+(.+)` (H1, H2, H3) triggers a chunk boundary. The header line starts the new chunk.
- **Token estimation:** `estimate_tokens(text)` uses `len(text) // 4` (4 chars ≈ 1 token). Not exact, but consistent.
- **Max chunk size:** Configurable via `config.rag.chunk_max_tokens`. Default: **800 tokens** (3200 chars).
- **Max chunks per document:** Configurable via `config.rag.max_chunks_per_document`. Default: **5000 chunks**. If exceeded, indexing aborts before embedding or replacing old chunks.
- **Overflow splitting:** When a chunk exceeds `max_tokens`, `_find_paragraph_break()` searches backward for an empty line. If no empty line is found, splits at 75% of the way through. The remainder starts a new chunk with a small overlap.
- **Overlap:** Configurable via `config.rag.chunk_overlap_tokens`. Default: **100 tokens**. Overlap is computed as `chunk_overlap_tokens // 10` lines.
- **Section header extraction:** `_extract_section_header(chunk_text)` scans lines for the first `#{1,3}` header and stores it as `section_header` in `doc_chunks`. Used in search result display.
- **Empty chunk filtering:** Any chunk where `chunk.strip()` is falsy is dropped.

Files scanned by `scan_docs_directory()`:
- `*.md` — all markdown files recursively
- `PROJECT.log` — current project log (append-only event log)
- `log/*.log` — archived monthly logs (with temporal context header injected)

---

## 5. Embedding Model and Ollama

- **Model:** `nomic-embed-text`
- **Dimensions:** 768 (float32)
- **Storage:** Packed as a float32 BLOB in `doc_chunks.embedding` via `lib/embeddings.py` helpers: `pack_embedding()` / `unpack_embedding()`
- **Batch path:** `lib/embeddings.get_embeddings()` dedupes repeated texts and
  prefers provider-side `embed_many()` when available. `OllamaEmbeddingsProvider`
  now implements `embed_many()` for multi-chunk RAG indexing. Default batch size
  is `16` (`OLLAMA_EMBED_BATCH_SIZE` override), and timeout-like batch failures
  split into smaller batches before the provider gives up. `OLLAMA_EMBED_TIMEOUT_S`
  controls the per-batch timeout.
- **Ollama URL:** Configured in `QUAID_HOME/shared/config/global/config.json` under `ollama.url`. Both OC and CC adapters on the same machine share the same Ollama instance.
- **Fail policy:** If `lib/fail_policy.is_fail_hard_enabled()` is `True` and
  embedding or vec-backed recall fails during search, `search_docs()` raises
  `RuntimeError` instead of silently degrading.

---

## 6. Search

`DocsRAG.search_docs(query, limit, min_similarity, project, docs)`:

```python
def search_docs(
    self,
    query: str,
    limit: int = 5,            # from config.rag.search_limit
    min_similarity: float = 0.3,  # from config.rag.min_similarity
    project: Optional[str] = None,
    docs: Optional[List[str]] = None,
) -> List[Dict]
```

**Algorithm:**

1. Embeds `query` via `_lib_get_embedding(query)`.
2. If `project` is set, builds SQL `LIKE` clauses for:
   - The project's `home_dir` (from `_get_project_paths()`)
   - Each of the project's `source_roots`
   - All file paths registered for that project in `doc_registry` (via `DocsRegistry().list_docs(project=...)`)
3. If `docs` filter is set (`--docs` flag), adds additional `LIKE` clauses matching basename/fragment against `source_file`.
4. If `sqlite-vec` is available and `vec_doc_chunks` exists, runs a bounded KNN
   query first:
   - `k = max(64, limit * 16)`
   - joins `vec_doc_chunks` back to `doc_chunks`
   - applies the same project/docs SQL filters to the joined rows
5. If vec is unavailable, or vec recall fails while failHard is disabled,
   falls back to the legacy row-scan path over `doc_chunks`.
6. For vec-backed rows, converts `distance` to a bounded cosine-like similarity.
   For fallback rows, unpacks `doc_chunks.embedding` and computes cosine in
   Python.
7. Applies `min_similarity`, reranks with `_docs_rank_score(...)`, and returns
   `results[:limit]`.

**Return format:**
```python
{
    "content": str,         # Full chunk text (no truncation)
    "source": str,          # Absolute file path
    "section_header": str,  # H1/H2/H3 header in chunk (or None)
    "similarity": float,    # Rounded to 3 decimal places
    "chunk_index": int,
}
```

**CLI search invocation:**
```bash
quaid recall "query" '{"stores":["docs"]}'
quaid recall "query" '{"stores":["docs"],"project":"quaid"}'
```

`quaid recall "query"` (without store filter) combines memory recall and docs search in a single call.

---

## 7. Staleness Detection

`updater.check_staleness()` builds a complete view of which docs are out of date relative to their tracked source files.

**Source-to-doc mapping resolution (two sources, registry takes precedence):**

1. `DocsRegistry.get_source_mappings()` — queries `doc_registry` for rows with `auto_update=1` and `source_files IS NOT NULL`. Returns `{doc_path: [source_path, ...]}`.
2. `config.docs.source_mapping` — legacy config-file-based mapping, used as fallback for unmigrated docs.

**Staleness check logic (`check_staleness()`):**

For each `(doc_path, [source_paths])` pair:
1. Stat the doc file (`doc_mtime = doc_abs.stat().st_mtime`).
2. For each source path, compare `src_mtime > doc_mtime`. Collect `stale_sources`.
3. If any stale sources: compute `gap_hours`, gather git diffs via `get_git_diff(src, doc_mtime)`, classify the diff via `classify_doc_change()`.
4. Returns `Dict[str, StalenessInfo]` — only stale docs included.

**`StalenessInfo` fields:** `doc_path`, `gap_hours`, `stale_sources`, `doc_mtime`, `latest_source_mtime`, `change_classification`.

**Change classification (`classify_doc_change(diff_text)`):**

Heuristic signal-counting classifier:
- **Trivial signals:** whitespace-only, comment-only, version bumps, import changes, typo-like edits (>85% character similarity via `SequenceMatcher`), small change (<=5 lines).
- **Significant signals:** new/changed functions/classes, API exports, schema changes (`CREATE TABLE`, `ALTER TABLE`), destructive changes, large diffs (>50 lines).
- Classification: `"significant"` if `significant_signals > trivial_signals`, otherwise `"trivial"`. Defaults to `"significant"` on tie.

**Git diff collection (`get_git_diff(source_path, since_mtime)`):**

Runs two git commands with a budget timer (default 30s, configurable via `QUAID_DOCS_GIT_BUDGET_S`):
1. `git log --oneline --after=<since_iso> -- <source_path>` — commit messages since doc was last modified.
2. `git diff HEAD -- <source_path>` — current uncommitted diff.

Returns combined text or empty string. If git is unavailable or times out, the update path falls back to transcript-based update.

---

## 8. Doc Auto-Update

`update_doc_from_diffs(doc_path, purpose, stale_sources, dry_run, trigger)`:

1. Reads current doc content.
2. Calls `get_git_diff()` for each stale source.
3. Detects if the doc is a "core markdown" file (TOOLS.md, AGENTS.md, etc.) via `_get_core_markdown_info()` — if so, uses a line-limit-aware prompt.
4. Calls `call_deep_reasoning()` with the current doc + diffs + purpose as context.
5. Two skip-write guards run before writing: (a) if the response is less than 50% of the original size (suspected LLM truncation), skips write and returns `False`; (b) for core markdown files, if the response line count exceeds the configured `maxLines` limit, skips write and returns `False` — does **not** truncate. Both cases log to the changelog with `success=False`.
6. On success: atomically writes the new content via `_atomic_write_text()` (temp file + `os.replace()`).
7. Logs the update to `logs/docs-update-log.json` via `log_doc_update()`, which also tracks cleanup state.

`update_doc_from_transcript(doc_path, purpose, transcript, dry_run, trigger)`:
Used when no git diffs are available (e.g., untracked files). Provides the session transcript as context instead of diffs.

**Changelog:** `logs/docs-update-log.json` — rolling file, last 100 entries kept via `_save_changelog()` (uses a simple `entries[-100:]` slice). Each entry: `timestamp`, `doc_path`, `trigger`, `sources`, `summary`, `dry_run`, `success`, `chars_before`, `chars_after`. Design note: this uses a manual tail-slice rather than `core/log_rotation.py`. The two mechanisms are independent — `log_rotation.py` is intended for token-budget-driven archiving of append-only timestamped logs (PROJECT.log, journal); the changelog rolling trim is a fixed count cap on a JSON file. Migrating changelog to `log_rotation.py` is a known improvement but not yet done.

**Update triggers (values in `trigger` field):** `"compact"`, `"janitor"`, `"manual"`, `"on-demand"`, `"cleanup"`.

---

## 9. Cleanup (Bloat Prevention)

After repeated updates, docs can grow bloated. `updater.py` tracks cleanup state in `logs/docs-cleanup-state.json`.

**Thresholds:**
- `CLEANUP_UPDATE_THRESHOLD = 10` — trigger cleanup after 10 updates since last cleanup
- `CLEANUP_GROWTH_THRESHOLD = 1.3` — trigger cleanup if doc grew 30%+ since last cleanup

`check_cleanup_needed()` iterates all docs in `get_doc_purposes()`, computes `growth_ratio = current_chars / chars_at_cleanup`, and returns docs meeting either threshold with a `reason` of `"updates"`, `"growth"`, or `"both"`.

`cleanup_doc(doc_path, purpose, dry_run)` calls the configured deep-reasoning model (`call_deep_reasoning`, max 8000 tokens, 300s timeout) with instructions to remove stale/redundant content while preserving all current accurate information. On success, resets cleanup state via `_reset_cleanup_state()`.

---

## 10. Runtime Supervisor And Project Docs Monitors

Project docs updates are owned by the runtime supervisor and project-docs
workers, not by compact/reset event JSON files or inline janitor tasks.
Extraction appends durable bullets to `PROJECT.log`; the supervisor-owned
project-docs worker reads `PROJECT.log` through a hidden cursor, compares the
linked source tree through shadow git, applies doc edits, syncs the docs
registry/RAG, and advances cursors only after apply succeeds.

**Update flow:**

1. `quaid docs update <project>` writes a hidden force-update request under
   `QUAID_HOME/data/project-docs/requests/` and ensures the runtime supervisor
   is alive.
2. The runtime supervisor owns one worker per active project. Workers own their
   own tick loop and take a per-project update lock before applying changes.
3. The worker reads pending shadow-git changes plus `PROJECT.log` entries since
   the hidden cursor.
4. The docs updater may edit `PROJECT.md`, `TOOLS.md`, `AGENTS.md`, and
   `docs/**/*.md`. `PROJECT.log` is append-only and must not be edited.
5. After apply, the worker auto-discovers new visible project docs, unregisters
   docs deleted by updater apply, reindexes registered docs, then advances the
   hidden shadow/log cursors.

The old `doc-health`, `request-docs`, dirty queue, and staged project-event
processor paths were removed prelaunch. Missing registered docs are an anomaly
unless the docs updater apply transaction or project deletion performs the
removal.

**`append_project_logs(project_logs, trigger, date_str, dry_run)`** — appends compact/reset bullets to per-project files:
- Writes timestamped entries to `PROJECT.log` (append-only history file).
- Also writes dated `- YYYY-MM-DD [Trigger] entry` lines into the `<!-- BEGIN:PROJECT_LOG --> ... <!-- END:PROJECT_LOG -->` block in PROJECT.md.

Worker liveness is supervised with PID identity checks, per-project locks, and
heartbeat staleness. A stale worker is stopped and restarted by the supervisor.
`quaid project status <project>` exposes phase/progress and recent worker log
tail so async updates can run for a long time without being confused with true
stalls.

The same runtime supervisor also owns instance monitors and one-shot janitor
workers. See `projects/quaid/reference/runtime-supervisor.md` for process-group
teardown and benchmark cleanup guidance.

---

## 11. Manual reindex vs project-docs worker indexing

| | `python3 datastore/docsdb/rag.py reindex [--all]` | Project-docs worker |
|---|---|---|
| Trigger | Manual CLI | Supervisor-owned docs update |
| Scope | Directory or all registered docs depending on CLI args | One project after docs apply |
| Registry sync | None | Registers/unregisters visible project docs before indexing |
| Force flag | `--all` forces reindex of unchanged files | No force; always mtime-gated |
| Dry-run | Not supported | Not used in worker apply path |
| Approval | Not required | Controlled by project-docs update policy |

Both paths use `DocsRAG.needs_reindex()` for change detection (except
`reindex --all` which bypasses it). Both call the same
`DocsRAG.index_document()` and produce identical chunk rows. Project-doc
workers additionally sync visible project docs before indexing.

---

## 12. CLI Reference

```bash
# --- Registration ---
quaid registry register <file_path> --project <name> --description "..."
quaid registry list
quaid registry list --project <name>
quaid docs list
quaid docs list --project <name>
quaid project create <name> [--description "Label"] [--source-root /path]

# --- Indexing ---
# Manual/debug (from module root with PYTHONPATH=.):
python3 datastore/docsdb/rag.py reindex              # mtime-gated
python3 datastore/docsdb/rag.py reindex --all        # Force full reindex
python3 datastore/docsdb/rag.py stats                # Index statistics

# --- Search ---
quaid recall "query" '{"stores":["docs"]}'                          # Docs search
quaid recall "query" '{"stores":["docs"],"project":"<name>"}'       # Project-scoped
quaid recall "query"                                                # Memory + docs combined

# --- Staleness ---
quaid docs check                                     # Show stale doc/source pairs
quaid docs update --apply                            # Trigger deep-reasoning update on stale docs (exits 0 whether docs updated or already current)
quaid docs update --apply --trivial-only             # Only trivial changes

# --- Runtime supervisor / project docs monitors ---
quaid docs update <project>                          # Queue async force update
quaid docs update <project> --wait                   # Queue and wait for apply
quaid project status <project>                       # Fresh/stale, worker, cursor state
quaid project diff <project> [--full]                # Pending source/log delta
quaid supervisor status|ensure|stop                  # Runtime supervisor process tree

# --- Changelog ---
quaid docs changelog                                 # Recent doc update history
```

---

## 13. Configuration Keys (config.json)

| Key | Purpose |
|-----|---------|
| `rag.docs_dir` | Workspace-relative path to docs directory (Pass 1 scan root) |
| `rag.chunk_max_tokens` | Max tokens per chunk (default: 800) |
| `rag.chunk_overlap_tokens` | Overlap tokens at chunk splits (default: 100) |
| `rag.max_chunks_per_document` | Max chunks indexed from one document (default: 5000) |
| `rag.search_limit` | Default `--limit` for `docs search` (default: 5) |
| `rag.min_similarity` | Default minimum similarity threshold (default: 0.3) |
| `ollama.url` | Ollama server URL |
| `ollama.embeddingDim` | Embedding dimension (expected: 768 for nomic-embed-text) |
| `projects.enabled` | Whether project system is active |
| `projects.staging_dir` | Path to event queue directory |
| `projects.definitions.<name>` | Project definitions (seeded to DB; DB is source of truth after first run) |
| `docs.staleness_check_enabled` | Enable/disable mtime staleness checking |
| `docs.source_mapping` | Legacy config-based doc→source mapping (registry takes precedence) |
| `docs.doc_purposes` | Dict of doc_path → purpose string (used as LLM context) |
| `docs.notify_on_update` | Whether to queue user notifications on doc updates |
| `docs.core_markdown.files` | Core markdown files config (filename → purpose, maxLines) |
| `retrieval.failHard` / `retrieval.fail_hard` | Raise on embedding failure vs. silent empty return |

---

## 14. Key Invariants and Operational Notes

**Embedding safety:** `index_document()` collects all embeddings before deleting old chunks. If any embedding call fails (Ollama down, timeout), the old index is preserved intact. Never partial-indexes a file.

**Atomic writes:** All doc file updates in `updater.py` use `_atomic_write_text()` (temp file + `os.replace()`). `project_updater.py` uses the same pattern for PROJECT.md via `tmp_path.write_text() + tmp_path.replace()`.

**Soft deletes only:** `unregister()` sets `state='deleted'`; rows are never hard-deleted. `delete_project_definition()` sets `state='deleted'` on `project_definitions`.

**Path handling:** `doc_registry.file_path` can be workspace-relative or absolute. `DocsRAG.search_docs()` resolves both forms when building project filter paths by joining with `_workspace()`. `index_document()` syncs `last_indexed_at` for both the absolute path and its workspace-relative form.

**No truncation:** Search results return full chunk content. Index passes do not limit or truncate any file content before chunking.

**Fail-hard integration:** `search_docs()` checks `is_fail_hard_enabled()` before returning an empty result on embedding failure. When fail-hard is on, it raises instead of degrading silently.
