# Projects System — Internal Reference

**Updated:** 2026-03-13
**Status:** Authoritative internal reference

This document covers the full projects system: the two registries, project
lifecycle, doc registry operations, shadow git, the project updater, and the
sync engine. Read this before modifying any of:

- `core/project_registry.py`
- `core/project_registry_cli.py`
- `datastore/docsdb/registry.py` (`DocsRegistry`)
- `datastore/docsdb/project_updater.py`
- `core/shadow_git.py`
- `core/sync_engine.py`
- `lib/project_registry.py`

---

## 1. Two Registries — Critical Distinction

Quaid has **two separate registries** that serve different purposes and store
data in different places. Confusing them is a common source of bugs.

### 1.1 Project Registry

**Purpose:** Tracks project metadata — identity, instances, source roots, timestamps.

**Authoritative store:** `QUAID_HOME/project-registry.json` (JSON file)

**Mirror:** SQLite `project_definitions` table in `memory.db` (seeded from JSON on first
`DocsRegistry` instantiation; kept in sync by `DocsRegistry.save_project_definition()`)

**Managed by:**
- `core/project_registry.py` — canonical implementation, called from daemons and CLI
- `lib/project_registry.py` — simpler reader/writer used for global registry queries
  (`quaid global-registry list`)

**Schema (one entry in `projects` dict):**
```json
{
  "canonical_path": "/path/to/QUAID_HOME/instance/projects/<name>/",
  "source_root": "/path/to/user/source/files/",
  "instances": ["claude-code", "openclaw"],
  "created_at": "2026-03-09T...",
  "description": "Human-readable description",
  "updated_at": "2026-03-10T..."
}
```

**Write safety:** `_save_registry()` in `core/project_registry.py` uses `fcntl.LOCK_EX`
+ atomic rename (`project-registry.tmp` → `project-registry.json`). The lock is acquired on
the **canonical file** (`project-registry.json`) via `O_CREAT | O_RDWR`, not on the `.tmp`
file. This is critical: locking the `.tmp` file (a previous bug) did not serialize concurrent
writers because each writer created its own uniquely-named temp file. Locking the canonical
file forces all writers to queue through the same lock fd across the full read-modify-write
cycle. The `lib/project_registry.py` `_save()` uses atomic rename but **without** file
locking — acceptable for low-contention global registry queries; high-frequency writes should
go through `core/project_registry.py`.

### 1.2 Doc Registry

**Purpose:** Tracks individual files registered to projects, content hashes, source mappings,
and auto-update metadata.

**Authoritative store:** SQLite `doc_registry` table in `QUAID_HOME/<instance>/data/memory.db`

**No JSON counterpart** — SQLite only.

**Managed by:** `datastore/docsdb/registry.py` (`DocsRegistry` class)

**`doc_registry` table columns (key ones):**
| Column | Type | Notes |
|--------|------|-------|
| `file_path` | TEXT UNIQUE | Workspace-relative path |
| `project` | TEXT | Project name (FK to `project_definitions.name`) |
| `asset_type` | TEXT | `doc`, `config`, etc. |
| `title` | TEXT | Human-readable title |
| `description` | TEXT | Purpose description |
| `auto_update` | INTEGER | 1 = doc-updater will refresh on staleness |
| `source_files` | TEXT | JSON array of source paths this doc tracks |
| `state` | TEXT | `active`, `deleted` (soft-delete) |
| `last_indexed_at` | TEXT | Last vector index time |
| `last_modified_at` | TEXT | Last content update time |

### 1.3 Design Principle

The project registry answers: "What projects exist and who owns them?"

The doc registry answers: "Which files belong to a project, and what do they track?"

They are linked by project name only. There is no foreign key enforcement —
`delete_project()` in `core/project_registry.py` manually cleans both stores.

---

## 2. Project Lifecycle

### 2.1 `create_project(name, description, source_root)`

**Location:** `core/project_registry.py`

```python
def create_project(
    name: str,
    description: str = "",
    source_root: Optional[str] = None,
) -> Dict[str, Any]:
```

Steps executed in order:

1. Validates `name` against `r"^[a-z0-9][a-z0-9-]*$"` — must be lowercase kebab-case.
2. Checks `project-registry.json` for duplicate name; raises `ValueError` if found.
3. Resolves `canonical_path` as `quaid_projects_dir(adapter.quaid_home()) / name`.
4. Builds entry dict with `instance_id()` as the first element of `instances` (note: uses
   `lib.instance.instance_id`, NOT `adapter_id()` — a distinction fixed in commit 14d408fd).
5. Creates `canonical/` and `canonical/docs/` directories.
6. Writes `PROJECT.md` template with name, description, and `created_at` timestamp.
7. If `source_root` provided: initializes `ShadowGit` and calls `sg.snapshot()` for initial baseline.
   Failure here is warned but does not abort project creation.
8. Writes entry to `project-registry.json` (atomic with file lock).
9. Calls `sync_all_projects()` — propagates new project dir to adapters that need sync
   (e.g. OpenClaw). Failure is warned but does not abort.

Returns the entry dict.

**Note on `DocsRegistry.create_project()`:** The `DocsRegistry` class in
`datastore/docsdb/registry.py` has its own `create_project()` method which is the
older code path used by `quaid registry create-project`. It scaffolds the directory,
writes a richer PROJECT.md template, saves to `project_definitions` SQLite table,
and optionally patches `config/memory.json`. The two code paths differ slightly in
template format and JSON config patching — `DocsRegistry.create_project()` also writes
to `config/memory.json` for legacy config loading.

### 2.2 `link_project(name)`

**Location:** `core/project_registry.py`

```python
def link_project(name: str) -> Dict[str, Any]:
```

- Adds `instance_id()` to the project's `instances` list.
- Idempotent — no-op if the instance is already listed.
- Updates `updated_at` timestamp on change.
- Raises `KeyError` if project not found.

Use when a second adapter wants to participate in an existing project without taking
ownership (e.g. CC linking to an OC project).

### 2.3 `unlink_project(name)`

**Location:** `core/project_registry.py`

```python
def unlink_project(name: str) -> Dict[str, Any]:
```

- Removes `instance_id()` from the `instances` list.
- Idempotent — no-op if not currently listed.
- Updates `updated_at` timestamp on change.
- Does NOT delete the project or any files. An unlinked project remains in the registry;
  it just won't be returned to that instance.

### 2.4 `update_project(name, **updates)`

**Location:** `core/project_registry.py`

```python
def update_project(name: str, **updates: Any) -> Dict[str, Any]:
```

Allowed fields: `source_root`, `description`, `instances`. All other keys are silently
ignored. Raises `KeyError` if not found.

Called by `quaid project update <name> --description "..." --source-root /path`.

### 2.5 `delete_project(name)`

**Location:** `core/project_registry.py`

```python
def delete_project(name: str) -> None:
```

Performs full cleanup in this order:

1. Destroys shadow git tracking repo (`ShadowGit.destroy()` → `shutil.rmtree(git_dir)`).
   Only attempted if project has `source_root`. Failure is warned, not raised.
2. Deletes canonical project directory (`shutil.rmtree(canonical_path)`).
3. Removes entry from `project-registry.json` (atomic write).
4. SQLite cleanup: `DELETE FROM project_definitions WHERE name = ?` and
   `DELETE FROM doc_registry WHERE project = ?`.

**Does NOT touch the user's `source_root` directory.** Only Quaid's tracking artifacts
and the canonical project dir are removed.

Raises `KeyError` if project not found.

---

## 3. Doc Registry Operations

**Class:** `DocsRegistry` in `datastore/docsdb/registry.py`

### 3.1 Constructor

```python
DocsRegistry(db_path: Path = None)
```

Resolves `db_path` from `lib.config.get_db_path()` if not provided. Calls
`ensure_table()` which creates `doc_registry` and `project_definitions` tables if
missing and runs `_seed_projects_from_json()` on first use (empty table only).

### 3.2 `register()`

```python
def register(
    self,
    file_path: str,
    project: str = "default",
    asset_type: str = "doc",
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    auto_update: bool = False,
    source_files: Optional[List[str]] = None,
    registered_by: str = "system",
    # ... identity/scope fields omitted for brevity
) -> int:
```

Upserts into `doc_registry` using `INSERT ... ON CONFLICT(file_path) DO UPDATE`.
Returns the row ID. On conflict, preserves existing `title`, `description`,
`source_files`, and identity fields via `COALESCE`, and resets `state` to `active`.

### 3.3 `unregister(file_path)`

Soft-delete: sets `state = 'deleted'`. Does not remove the actual file.
Returns `True` if a row was updated.

### 3.4 `list_docs(project=None, asset_type=None, state='active')`

```python
def list_docs(
    self,
    project: Optional[str] = None,
    asset_type: Optional[str] = None,
    state: str = "active",
) -> List[Dict[str, Any]]:
```

Returns all active docs, optionally filtered by project or asset type. Results ordered
by `project, file_path`.

### 3.5 `find_project_for_path(file_path)`

```python
def find_project_for_path(self, file_path: str) -> Optional[str]:
```

Resolution order (cheapest first):
1. Check if path is inside a project's `home_dir` (config walk).
2. Direct lookup in `doc_registry` by `file_path`.
3. Check if path is under any project's `source_roots`.
4. Reverse lookup: check if path appears in any doc's `source_files` JSON array.

Returns project name or `None`.

### 3.6 `get_source_mappings(project=None)`

```python
def get_source_mappings(self, project: Optional[str] = None) -> Dict[str, List[str]]:
```

Returns `{doc_path: [source_path, ...]}` for all docs with `auto_update=1` and
non-null `source_files`. Used by the project updater's staleness check.

### 3.7 `update_timestamps(file_path, indexed_at=None, modified_at=None)`

Updates `last_indexed_at` and/or `last_modified_at`. Called by project updater after
a successful doc update.

### 3.8 `auto_discover(project_name)` / `sync_external_files(project_name)`

Available as CLI subcommands (`discover`, `sync-external`). `discover` scans the
project's `home_dir` for unregistered `.md` files. `sync-external` checks whether
registered external files still exist on disk.

---

## 4. Global Registry vs. `project show`

Both of these read the same file (`project-registry.json`) — there is no divergence
in data source.

**`quaid global-registry list`** calls `lib/project_registry.list_all()` which:

```python
def list_all() -> Dict[str, Dict[str, Any]]:
    return _load()["projects"]
```

`_load()` resolves the path via `get_adapter().quaid_home() / "project-registry.json"`,
with a fallback to `QUAID_HOME` env var.

**`quaid project show <name>`** calls `core/project_registry.get_project(name)`:

```python
def get_project(name: str) -> Optional[Dict[str, Any]]:
    return list_projects().get(name)
```

Where `list_projects()` calls `_load_registry()` from `core/project_registry.py`.

Both functions resolve `_registry_path()` using `get_adapter().quaid_home()`. They
read the same physical file. The only difference is formatting: `global-registry list`
uses the `lib/` formatter, `project show` dumps JSON directly.

---

## 5. Cross-Instance Project Sharing

When OpenClaw and Claude Code share the same `QUAID_HOME` (same machine):

- **`project-registry.json` is shared.** Both adapters read and write the same file.
  File locking in `core/project_registry._save_registry()` prevents corruption on
  concurrent writes.

- **`doc_registry` is per-instance.** Each adapter has its own `memory.db` at
  `QUAID_HOME/<instance>/data/memory.db`. Files registered by OC are not visible
  to CC's doc registry, and vice versa. However, both adapters use the same
  Ollama/embeddings backend, so once each indexes the same content they will
  produce equivalent embeddings.

- **`project link`** adds the calling instance's `instance_id()` to the shared
  `instances` list without transferring ownership or creating a new canonical dir.
  The canonical dir was created by the first instance (the `create` caller).

- **`project delete`** by either instance removes for ALL instances: it clears
  `instances`, deletes the canonical dir, and purges SQLite rows from the calling
  instance's `memory.db`. The other instance's `memory.db` is NOT purged — rows
  in the other instance's `doc_registry` become orphaned (project no longer exists
  in the JSON but the SQLite rows linger). This is a known gap; the janitor does
  not currently clean these up automatically.

- **Sync target:** Adapters declare whether they need file sync via
  `adapter.get_context_sync_target()`. Claude Code returns `None` (reads directly
  from `QUAID_HOME`). OpenClaw returns a workspace path — files are copied to it
  by `sync_engine.sync_all_projects()`.

---

## 6. Shadow Git

**Location:** `core/shadow_git.py`

**Purpose:** Track changes to user source files between extraction events without
leaving any git artifacts in the user's directory. Uses `--git-dir` / `--work-tree`
split (same pattern as yadm/vcsh).

### 6.1 Constructor

```python
ShadowGit(
    project_name: str,
    source_root: Path,
    tracking_base: Optional[Path] = None,
)
```

- `git_dir` is set to `tracking_base / project_name` (defaults to `QUAID_HOME/.git-tracking/<name>/`).
- `work_tree` is the user's source directory (`source_root.resolve()`).
- No git operations happen in `__init__`.

### 6.2 Key Properties and Methods

```python
@property
def initialized(self) -> bool:
    # True if git_dir/HEAD exists
```

```python
def init(self) -> None:
    # git init --bare git_dir
    # Writes _DEFAULT_EXCLUDES to git_dir/info/exclude
```

```python
def snapshot(self) -> Optional[SnapshotResult]:
    # Runs git status --porcelain
    # If changes: git add -A, git commit -m "snapshot <UTC-ts>"
    # Returns SnapshotResult(changes, commit_hash, is_initial)
    # Returns None if no changes
```

```python
def get_diff(self, commits_back: int = 1) -> Optional[str]:
    # git diff --find-renames HEAD~N..HEAD
    # Returns patch text for LLM consumption
```

```python
def get_tracked_files(self) -> List[str]:
    # git ls-files
```

```python
def add_ignore_patterns(self, patterns: List[str]) -> None:
    # Appends LLM-managed patterns to git_dir/info/exclude
    # Defaults in _DEFAULT_EXCLUDES cannot be overwritten
```

```python
def destroy(self) -> None:
    # shutil.rmtree(git_dir)
```

### 6.3 Default Excludes

`_DEFAULT_EXCLUDES` is written to `git_dir/info/exclude` at `init()` time. It covers:
secrets (`.env*`, `*.pem`, `*.key`, etc.), dependencies (`node_modules/`, `.venv/`, etc.),
IDE artifacts, databases, media files, and Quaid internals (`.quaid/`, `.git-tracking/`,
`*.snippets.md`). These are hardcoded and cannot be removed via `add_ignore_patterns()`.

### 6.4 Return Types

```python
@dataclass
class FileChange:
    status: str       # A=added, M=modified, D=deleted, R=renamed
    path: str
    old_path: Optional[str]  # Set for renames only

@dataclass
class SnapshotResult:
    changes: List[FileChange]
    commit_hash: Optional[str]
    is_initial: bool
```

### 6.5 Lifecycle Integration

Shadow git is triggered from two places:

- `create_project()` — calls `sg.init()` + `sg.snapshot()` for initial baseline.
- `snapshot_all_projects()` in `core/project_registry.py` — called by the extraction
  daemon after each successful extraction to capture what changed.

```python
def snapshot_all_projects() -> List[Dict[str, Any]]:
    # Iterates projects_with_source_root()
    # For each: ShadowGit.init() if needed, then snapshot()
    # Returns list of {project, is_initial, diff, changes}
```

---

## 7. Project Updater

**Location:** `datastore/docsdb/project_updater.py`

**Purpose:** Background processor that updates stale docs based on extraction events
and source file changes. Spawned as a subprocess by compact/reset hooks.

### 7.1 Event Model

Events are written as JSON files to `QUAID_HOME/<instance>/projects/staging/*.json`
by the plugin. Each event has:

```json
{
  "project_hint": "project-name",
  "files_touched": ["path/to/file.py", "..."],
  "summary": "Extraction session summary text",
  "trigger": "compact",
  "timestamp": "2026-03-13T..."
}
```

### 7.2 `process_event(event_path)`

```python
def process_event(event_path: str) -> Dict:
```

Steps:
1. Read and parse event JSON.
2. Resolve project via `_resolve_project()`: tries `project_hint` first (config lookup),
   then walks `files_touched` through `DocsRegistry.find_project_for_path()`.
   On failure: moves event to `staging/failed/` for manual triage.
3. Read `PROJECT.md` from the resolved project dir.
4. Run `_check_registry_staleness()`: compare mtime of each `auto_update=1` doc against
   its `source_files`. Returns list of `{doc_path, stale_sources, doc_mtime}`.
5. Call `_apply_updates()` to refresh stale docs using Opus.
6. Refresh `PROJECT.md` file-list section via `_refresh_file_list()`.
7. Queue user notification via `lib.delayed_requests.queue_delayed_request()`.
8. Delete event file.

### 7.3 `process_all_events()`

```python
def process_all_events() -> Dict:
```

Processes all `staging/*.json` files in chronological order (sorted by filename).
Failed events move to `staging/failed/`. Caps `failed/` at 20 entries.

### 7.4 `evaluate_doc_health(project_name, dry_run=False)`

```python
def evaluate_doc_health(project_name: str, dry_run: bool = False) -> Dict:
```

One deep LLM call that analyzes `PROJECT.md`, registered docs, recent `PROJECT.log`
entries, and source roots. Returns three decision lists:

- `create`: suggested new docs (must go in `docs/` subdir).
- `update`: existing docs needing refresh.
- `archive`: obsolete docs to soft-delete.

In non-dry-run mode: scaffolds new doc files, registers them, and calls
`registry.unregister()` on archive decisions. Does NOT auto-apply `update` decisions —
those remain in the return value for caller action.

Called by `quaid updater doc-health <project>`. Not called on every extraction event.

### 7.5 `append_project_logs(project_logs, trigger, date_str, dry_run)`

```python
def append_project_logs(
    project_logs: Dict[str, List[str]],
    trigger: str = "Compaction",
    date_str: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
```

Appends bullet entries to two places:
- `PROJECT.log` (append-only file, one ISO-timestamp line per entry) — the durable history.
- `PROJECT.md` inside `<!-- BEGIN:PROJECT_LOG --> ... <!-- END:PROJECT_LOG -->` markers
  (human-visible rolling log section).

Normalizes entries (strips leading bullets, session prefixes, collapses whitespace).
Deduplicates within the batch via `dict.fromkeys()`. Returns metrics dict.

### 7.6 Watchdog

`process_event()` is wrapped in `_run_with_watchdog()` when invoked from CLI. Default
timeout is 900s (configurable via `QUAID_PROJECT_UPDATER_WATCHDOG_SECONDS`). Uses
`signal.SIGALRM` (POSIX only). On timeout, event moves to `failed/`.

---

## 8. Sync Engine

**Location:** `core/sync_engine.py`

**Purpose:** Copy bootstrap-eligible project files from the canonical `QUAID_HOME/projects/`
location into adapter workspaces that have boundary constraints (OpenClaw). Adapters that
read directly from `QUAID_HOME` (Claude Code) skip sync entirely.

### 8.1 Sync Decision

```python
def sync_all_projects() -> List[SyncResult]:
    adapter = get_adapter()
    sync_target = adapter.get_context_sync_target()
    if sync_target is None:
        return []  # Adapter reads directly, no sync needed
```

Claude Code's adapter returns `None` for `get_context_sync_target()`. OpenClaw returns
its workspace projects directory.

### 8.2 `sync_project(canonical_dir, target_dir, project_name)`

```python
def sync_project(canonical_dir: Path, target_dir: Path, project_name: str) -> SyncResult:
```

- Iterates `SYNCABLE_NAMES` (the bootstrap file set):
  `TOOLS.md`, `AGENTS.md`, `SOUL.md`, `USER.md`, `MEMORY.md`, `IDENTITY.md`,
  `HEARTBEAT.md`, `TODO.md`.
- For each: if canonical file is newer than target (mtime comparison), copies with
  `shutil.copy2()` (preserves mtime). If canonical is absent, removes stale target copy.
- Writes a `README.md` in the target project dir pointing back to the canonical location.
- Returns `SyncResult(project, copied, skipped, removed, errors)`.

Only these named files are ever synced — `PROJECT.md` and doc content in `docs/` are
not synced (they are either read directly or indexed separately).

### 8.3 Stale Target Cleanup

After syncing, `_cleanup_stale_targets()` removes synced project dirs in the target
that no longer have a corresponding canonical source. This fires when a project is
deleted and a sync cycle runs.

### 8.4 When `sync_all_projects()` Is Called

- After `create_project()` — to immediately propagate the new project dir.
- On daemon tick (extraction daemon main loop).
- Manually via `quaid project sync`.

---

## 9. CLI Reference

### `core/project_registry_cli.py` (invoked as `quaid project ...`)

| Subcommand | Function | Notes |
|-----------|----------|-------|
| `list` | `list_projects()` | Human or JSON output |
| `create <name>` | `create_project()` | `--description`, `--source-root` |
| `show <name>` | `get_project()` | JSON output |
| `update <name>` | `update_project()` | `--description`, `--source-root` |
| `link <name>` | `link_project()` | Adds current instance |
| `unlink <name>` | `unlink_project()` | Removes current instance |
| `delete <name>` | `delete_project()` | Full cleanup |
| `snapshot [<name>]` | `snapshot_all_projects()` or single | Shadow git snapshot |
| `sync` | `sync_all_projects()` | Push to adapter workspaces |

### `lib/project_registry.py` (invoked as `quaid global-registry ...`)

| Subcommand | Function | Notes |
|-----------|----------|-------|
| `list` | `list_all()` | Same JSON file, different formatter |
| `show <name>` | `lookup()` | |
| `register <name> <path>` | `register()` | Upsert — updates existing |
| `link <name>` | `link()` | `--instance`, `--symlink` |
| `unlink <name>` | `unlink()` | `--instance` |
| `remove <name>` | `remove()` | `--force` overrides multi-instance guard |
| `rename <name> <new>` | `rename()` | Preserves metadata |

Note: `lib/project_registry.link()` has an optional `create_symlink=True` parameter
that creates a filesystem symlink in the adapter's `projects/` dir pointing to the
canonical project path. This is not exposed in `core/project_registry.link_project()`.

---

## 10. Key Invariants and Gotchas

**Instance identity in `create_project`:** Uses `lib.instance.instance_id()`, not
`adapter.adapter_id()`. These differ: `instance_id()` includes the adapter type and
instance slot; `adapter_id()` is just the adapter class name. Mixing them causes
instances to not recognize each other's projects. Commit 14d408fd fixed a bug where
the wrong function was called.

**`project_definitions` table seeding:** `DocsRegistry.ensure_table()` calls
`_seed_projects_from_json()` on every instantiation, but it is guarded by a
`COUNT(*) > 0` check — only runs when the table is empty. On established instances
the SQLite table is the source of truth, not `config/memory.json`.

**`delete_project` SQLite scope:** Only the calling instance's `memory.db` is
cleaned up. If OC and CC both have docs registered for the same project and OC
deletes the project, CC's `doc_registry` rows become orphaned.

**Shadow git and missing `source_root`:** `snapshot_all_projects()` logs a warning
and skips any project whose `source_root` path does not exist on disk. It does not
raise. Safe to call even if some source roots have moved.

**Sync is one-way and mtime-gated:** `sync_project()` only copies canonical → target,
never the reverse. Edits made in the sync target will be silently overwritten on the
next sync cycle. The README.md written into each target dir warns about this.

**`PROJECT.log` is append-only.** `append_project_logs()` never truncates or rewrites
`PROJECT.log`. The marker-based section in `PROJECT.md` (`<!-- BEGIN:PROJECT_LOG -->`)
is a separate rolling view — it accumulates entries over multiple sessions. Log rotation
via `core.log_rotation.rotate_project_logs()` (called at the end of janitor Task 1d-journal)
archives old entries; `PROJECT.log` itself is never in-place trimmed.

**`_save_registry()` locking target:** The file lock must be acquired on the canonical
`project-registry.json`, not the `.tmp` staging file. Locking `.tmp` is ineffective because
each writer creates its own `.tmp` and they never contend on the same fd. This was a fixed
bug — always lock the canonical path.

**`evaluate_doc_health` placement constraint:** New docs suggested by the LLM must
be placed under the `docs/` subdirectory. The prompt enforces this as a rule
(`"docs/architecture.md"`, not `"architecture.md"`). Auto-created docs are scaffolded
with an `<!-- Auto-created by project updater -->` comment.

---

## 11. File Locations Quick Reference

```
QUAID_HOME/
  project-registry.json          # Authoritative project metadata (shared across instances)
  project-registry.tmp           # Ephemeral — atomic write staging file
  <instance>/
    data/
      memory.db                  # doc_registry + project_definitions tables (per-instance)
    projects/
      <name>/
        PROJECT.md               # Project home file (auto-maintained)
        PROJECT.log              # Append-only history
        docs/                    # Project documentation
    tracking/
      <name>/                    # Shadow git bare repo (git_dir)
    projects/staging/
      *.json                     # Queued updater events
      failed/                    # Events that could not be processed
```

Source modules:
- `core/project_registry.py` — lifecycle CRUD for `project-registry.json`
- `core/project_registry_cli.py` — CLI (`quaid project ...`)
- `core/shadow_git.py` — `ShadowGit` class
- `core/sync_engine.py` — adapter workspace sync
- `datastore/docsdb/registry.py` — `DocsRegistry`, `doc_registry` SQLite CRUD
- `datastore/docsdb/project_updater.py` — background event processor
- `lib/project_registry.py` — global registry reader/writer (`quaid global-registry ...`)
