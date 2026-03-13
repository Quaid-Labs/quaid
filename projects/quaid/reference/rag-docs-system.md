# Quaid RAG and Docs System — Technical Reference

**Source files:**
- `datastore/docsdb/rag.py` — indexing, chunking, search (`DocsRAG`)
- `datastore/docsdb/registry.py` — doc and project registry (`DocsRegistry`)
- `datastore/docsdb/updater.py` — staleness detection and doc auto-update
- `datastore/docsdb/project_updater.py` — event-driven Opus-based project doc refresh

---

## 1. System Overview

The docs system has four tightly integrated components:

| Component | Class / Module | Storage | Purpose |
|-----------|---------------|---------|---------|
| Doc registry | `DocsRegistry` | `doc_registry` (SQLite) | Tracks which files belong to which projects; maps source files to docs |
| Project definitions | `DocsRegistry` | `project_definitions` (SQLite) | Canonical project config (seeded from `config/memory.json`, then DB is source of truth) |
| RAG indexer | `DocsRAG` | `doc_chunks` (SQLite) | Chunks files, generates embeddings, serves semantic search |
| Staleness detector / updater | `updater.py` | `logs/docs-update-log.json` | Detects when source code has drifted ahead of docs, calls Opus to rewrite |
| Project event processor | `project_updater.py` | `projects/<staging_dir>/` | Processes compact/reset event files, calls `update_doc_from_diffs`, refreshes PROJECT.md |

All components share a single SQLite database at `QUAID_HOME/db/` (path from `lib/config.get_db_path()`). Embeddings are stored in the same DB inside `doc_chunks`.

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
| `embedding` | BLOB | float32 array, 4096 dims (qwen3-embedding:8b) |
| `created_at` | TEXT | UTC ISO datetime |
| `updated_at` | TEXT | UTC ISO datetime — used by `needs_reindex()` |

Indexes: `idx_doc_chunks_source` (source_file), `idx_doc_chunks_updated` (updated_at).

Change detection: `needs_reindex()` compares the file's `st_mtime` (UTC) against `updated_at` of any existing chunk. If `file_time > indexed_time`, the file is reindexed. There is no SHA hash in the `doc_chunks` table; the `_get_file_hash()` method exists but is not used for the reindex check — mtime is the only gate.

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
| `auto_index` | INTEGER | Whether janitor auto-discovers files |
| `patterns` | TEXT | JSON list of glob patterns (default `["*.md"]`) |
| `exclude` | TEXT | JSON list of exclude patterns |
| `description` | TEXT | Project description |
| `state` | TEXT | `'active'`, `'archived'`, or `'deleted'` |
| `created_at` / `updated_at` | TEXT | ISO datetimes |

**Bootstrap:** On first instantiation (empty table), `_seed_projects_from_json()` reads `config/memory.json` and imports `projects.definitions`. After seeding, the DB is the authoritative source; JSON is ignored.

---

## 3. Indexing Pipeline

### How a file gets from disk into searchable RAG

**Step 1: Registration**

```bash
quaid registry register <file_path> --project <name> --description "..."
```

This calls `DocsRegistry.register()`, inserting a row into `doc_registry`. The file is not yet indexed.

**Step 2: RAG maintenance trigger**

Two equivalent paths both call `DocsRAG.reindex_all()` and `DocsRAG.index_document()`:

- **Janitor (automated):** `quaid janitor --task rag --apply [--approve]` → `_run_rag_maintenance(ctx)` registered at `registry.register("rag", ...)` in `rag.py`.
- **Manual:** `cd <module_root> && PYTHONPATH=. python3 datastore/docsdb/rag.py reindex [--all] [--dir <path>]`

**Step 3: Three-pass scan inside `_run_rag_maintenance()`**

The janitor performs three distinct passes:

1. **Pass 1 — workspace `docs/` directory:** `rag.reindex_all(cfg.rag.docs_dir)` — scans `*.md`, `PROJECT.log`, and `log/*.log` recursively under the configured docs directory.

2. **Pass 2 — project home directories:** For each project in `cfg.projects.definitions`, if `proj_dir.exists()`, calls `rag.reindex_all(str(proj_dir))` using the same scan logic. Covers project-owned markdown and logs.

3. **Pass 3 — `doc_registry` external files:** Enumerates all entries via `DocsRegistry().list_docs()`. For each entry whose `file_path` resolves to a real file, calls `rag.needs_reindex()` and indexes if stale. This covers files registered outside the scanned directories (e.g., source code files linked via `quaid registry register`).

Pre-pass: before the three indexing passes, janitor also:
- Calls `process_all_events()` (project_updater) to drain the event queue.
- Calls `docs_registry.auto_discover(proj_name)` for each project with `auto_index=True`.
- Calls `docs_registry.sync_external_files(proj_name)` for each project.

**Step 4: `needs_reindex(file_path)` check**

```python
def needs_reindex(self, file_path: str) -> bool
```

Reads `st_mtime` from disk (UTC), queries `MAX(updated_at)` from `doc_chunks` for that file. Returns `True` if file is newer than the stored index time, or if no chunks exist. On any exception, returns `True` (fail-safe reindex).

**Step 5: `index_document(file_path)` — atomic index-and-replace**

```python
def index_document(self, file_path: str) -> int  # returns chunk count
```

1. Reads file content (UTF-8).
2. If file is in `log/*.log` (archive log), prepends a temporal context header via `_archive_temporal_header()`.
3. Calls `chunk_markdown(content)` to produce a list of text chunks.
4. For each chunk: calls `_lib_get_embedding(chunk_text)` (Ollama). If any embedding fails, aborts without deleting old chunks (preserves stale but working index).
5. Only after all embeddings succeed: deletes old `doc_chunks` rows for this file, inserts new rows.
6. Calls `DocsRegistry.update_timestamps(file_path, indexed_at=now)` to sync `last_indexed_at`.

Returns the number of chunks created (0 on any failure).

---

## 4. Chunking Strategy

`DocsRAG.chunk_markdown(content, max_tokens=None)` uses header-boundary chunking:

- **Header splits:** Any line matching `^(#{1,3})\s+(.+)` (H1, H2, H3) triggers a chunk boundary. The header line starts the new chunk.
- **Token estimation:** `estimate_tokens(text)` uses `len(text) // 4` (4 chars ≈ 1 token). Not exact, but consistent.
- **Max chunk size:** Configurable via `config.rag.chunk_max_tokens`. Default: **800 tokens** (3200 chars).
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

- **Model:** `qwen3-embedding:8b`
- **Dimensions:** 4096 (float32)
- **Storage:** Packed as a float32 BLOB in `doc_chunks.embedding` via `lib/embeddings.py` helpers: `pack_embedding()` / `unpack_embedding()`
- **Ollama host:** Configured in `QUAID_HOME/config/memory.json` under `ollama.host`. Both OC and CC adapters on the same machine share the same Ollama instance.
- **Fail policy:** If `lib/fail_policy.is_fail_hard_enabled()` is `True` and embedding fails during search, `search_docs()` raises `RuntimeError` instead of returning an empty list.

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
4. Issues a single SQL query with the combined `WHERE` clause — avoids full table scan when project is known.
5. For each returned chunk: computes `cosine_similarity(query_embedding, chunk_embedding)` via `lib/similarity.py`.
6. Filters to `similarity >= min_similarity` (default 0.3).
7. Sorts by similarity descending, returns `results[:limit]`.

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
quaid docs search "query"
quaid docs search "query" --project quaid
quaid docs search "query" --project quaid --docs "architecture.md,api-reference.md"
```

The CLI search (`quaid hook-search`) combines memory recall and docs search in a single call.

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
4. Calls `call_deep_reasoning()` (Opus/`lib/llm_clients.py`) with the current doc + diffs + purpose as context.
5. On success: atomically writes the new content via `_atomic_write_text()` (temp file + `os.replace()`).
6. Logs the update to `logs/docs-update-log.json` via `log_doc_update()`, which also tracks cleanup state.

`update_doc_from_transcript(doc_path, purpose, transcript, dry_run, trigger)`:
Used when no git diffs are available (e.g., untracked files). Provides the session transcript as context instead of diffs.

**Changelog:** `logs/docs-update-log.json` — rolling file, last 100 entries kept. Each entry: `timestamp`, `doc_path`, `trigger`, `sources`, `summary`, `dry_run`, `success`, `chars_before`, `chars_after`.

**Update triggers (values in `trigger` field):** `"compact"`, `"janitor"`, `"manual"`, `"on-demand"`, `"cleanup"`.

---

## 9. Cleanup (Bloat Prevention)

After repeated updates, docs can grow bloated. `updater.py` tracks cleanup state in `logs/docs-cleanup-state.json`.

**Thresholds:**
- `CLEANUP_UPDATE_THRESHOLD = 10` — trigger cleanup after 10 updates since last cleanup
- `CLEANUP_GROWTH_THRESHOLD = 1.3` — trigger cleanup if doc grew 30%+ since last cleanup

`check_cleanup_needed()` iterates all docs in `get_doc_purposes()`, computes `growth_ratio = current_chars / chars_at_cleanup`, and returns docs meeting either threshold with a `reason` of `"updates"`, `"growth"`, or `"both"`.

`cleanup_doc(doc_path, purpose, dry_run)` calls Opus (`call_deep_reasoning`, max 8000 tokens, 300s timeout) with instructions to remove stale/redundant content while preserving all current accurate information. On success, resets cleanup state via `_reset_cleanup_state()`.

---

## 10. Project Event Processing

The `project_updater.py` module handles asynchronous project doc updates triggered by compact/reset hooks.

**Event flow:**

1. Compact/reset hook writes a JSON event file to `projects/<staging_dir>/` (path from `config.projects.staging_dir`).
2. Event JSON fields: `project_hint`, `files_touched`, `summary`, `trigger`.
3. During janitor RAG maintenance pass, `process_all_events()` is called.
4. `process_event(event_path)` resolves the project, checks registry staleness via `_check_registry_staleness()`, then calls `_apply_updates()`.
5. `_apply_updates()` calls `update_doc_from_diffs()` for each stale doc. Falls back to `update_doc_from_transcript()` if no git diffs.
6. After updates: `_refresh_file_list()` regenerates the "In This Directory" and "External Files" sections of PROJECT.md.
7. Event file is deleted on success; moved to `staging_dir/failed/` on error (capped at 20 entries).

**`evaluate_doc_health(project_name, dry_run)`** — deeper LLM-driven doc audit:
- Makes one `tier="deep"` LLM call with PROJECT.md, registered docs list, recent PROJECT.log entries, and source roots.
- Returns `create` / `update` / `archive` decision arrays.
- If not dry_run: scaffolds new doc files, registers them, soft-deletes archived docs.
- Called via `quaid updater doc-health <project> [--dry-run]`, not on every event.

**`append_project_logs(project_logs, trigger, date_str, dry_run)`** — appends compact/reset bullets to per-project files:
- Writes timestamped entries to `PROJECT.log` (append-only history file).
- Also writes dated `- YYYY-MM-DD [Trigger] entry` lines into the `<!-- BEGIN:PROJECT_LOG --> ... <!-- END:PROJECT_LOG -->` block in PROJECT.md.

**Watchdog:** `process-event` CLI path wraps execution in `_run_with_watchdog()` using POSIX `SIGALRM`. Timeout configurable via `QUAID_PROJECT_UPDATER_WATCHDOG_SECONDS` (default 900s).

---

## 11. reindex --all vs janitor --task rag

| | `python3 datastore/docsdb/rag.py reindex [--all]` | `quaid janitor --task rag --apply` |
|---|---|---|
| Trigger | Manual CLI | Automated (nightly or on-demand) |
| Pass 1 | `docs/` directory | `cfg.rag.docs_dir` |
| Pass 2 | Workspace root `*.md` files | Each project's `home_dir` |
| Pass 3 | `doc_registry` entries | `doc_registry` entries |
| Pre-pass | None | `process_all_events()`, `auto_discover()`, `sync_external_files()` |
| Force flag | `--all` forces reindex of unchanged files | No force; always mtime-gated |
| Dry-run | Not supported | Supported via `ctx.dry_run`; skips all indexing |
| Approval | Not required | Requires `--approve` if `janitor.applyMode=ask` |

Both paths use `DocsRAG.needs_reindex()` for change detection (except `reindex --all` which bypasses it). Both call the same `DocsRAG.index_document()` and produce identical chunk rows. The janitor path additionally processes project events and auto-discovers files before indexing.

---

## 12. CLI Reference

```bash
# --- Registration ---
quaid registry register <file_path> --project <name> --description "..."
quaid registry list
quaid registry list --project <name>
quaid docs list
quaid docs list --project <name>
quaid registry create-project <name> [--label "Label"] [--source-roots path1 path2]

# --- Indexing ---
quaid janitor --task rag --apply --approve            # Recommended: all three passes
# Manual (from module root with PYTHONPATH=.):
python3 datastore/docsdb/rag.py reindex              # mtime-gated
python3 datastore/docsdb/rag.py reindex --all        # Force full reindex
python3 datastore/docsdb/rag.py stats                # Index statistics

# --- Search ---
quaid docs search "query"
quaid docs search "query" --project <name>
quaid docs search "query" --project <name> --docs "file1.md,file2.md"
quaid hook-search "query"                            # Memory + docs combined

# --- Staleness ---
quaid docs check                                     # Show stale doc/source pairs
quaid docs update --apply                            # Trigger Opus update on stale docs
quaid docs update --apply --trivial-only             # Only trivial changes

# --- Cleanup ---
quaid docs cleanup-check                             # Which docs have update/growth bloat
quaid docs cleanup --apply                           # Clean all bloated docs via Opus
quaid docs cleanup <doc_path> --apply                # Clean specific doc

# --- Project doc health ---
quaid updater doc-health <project> --dry-run         # Preview create/update/archive decisions
quaid updater doc-health <project>                   # Apply decisions (scaffold, archive)

# --- Project event queue ---
python3 datastore/docsdb/project_updater.py check
python3 datastore/docsdb/project_updater.py process-all
python3 datastore/docsdb/project_updater.py refresh-project-md <project_name>

# --- Changelog ---
quaid docs changelog                                 # Recent doc update history
```

---

## 13. Configuration Keys (memory.json)

| Key | Purpose |
|-----|---------|
| `rag.docs_dir` | Workspace-relative path to docs directory (Pass 1 scan root) |
| `rag.chunk_max_tokens` | Max tokens per chunk (default: 800) |
| `rag.chunk_overlap_tokens` | Overlap tokens at chunk splits (default: 100) |
| `rag.search_limit` | Default `--limit` for `docs search` (default: 5) |
| `rag.min_similarity` | Default minimum similarity threshold (default: 0.3) |
| `ollama.host` | Ollama server URL |
| `ollama.embeddingDim` | Embedding dimension (expected: 4096 for qwen3-embedding:8b) |
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
