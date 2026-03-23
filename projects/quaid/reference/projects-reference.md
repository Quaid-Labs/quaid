# Projects System Reference

**Updated:** 2026-03-13
**Status:** Authoritative consolidated reference

This document is the single reference for the Quaid projects system. It covers
architecture, both registries, the full lifecycle, shadow git, the sync engine,
the project updater, the CLI, cross-instance workflow, and key invariants.

Source modules covered here:
- `core/project_registry.py`
- `core/project_registry_cli.py`
- `core/shadow_git.py`
- `core/sync_engine.py`
- `datastore/docsdb/registry.py` (`DocsRegistry`)
- `datastore/docsdb/project_updater.py`
- `lib/project_registry.py`

---

## 1. Overview

A "project" in Quaid is a named container for context that belongs to a specific
subject domain — a codebase, a trip, a research area, etc. It is distinct from
the memory system (which captures personal facts and conversation history).

A project holds:
- **Project docs** (Layer 3): documentation indexed into docsdb/RAG
- **Project context** (Layer 4): `TOOLS.md`/`AGENTS.md` injected into LLM context
- **Subject matter tracking** (Layer 5): shadow git monitoring of the user's
  actual project files (code, scripts, plans, etc.)

The human never manages projects directly. The LLM creates, configures, and
maintains projects through Quaid's tools. The human just talks.

### How projects differ from memory

The memory system (`memory.db`, `quaid recall`) stores personal facts, relationships,
and conversation history. It is agent-centric.

The project system (`project-registry.json`, `quaid project`) stores structured
context about external subject domains — what files exist, what documentation to keep
current, which adapters participate. It is workspace-centric.

### Key concepts

- **Canonical path**: where Quaid's project metadata lives (`QUAID_HOME/projects/<name>/`)
- **Source root**: where the user's actual project files live (optional)
- **Instance**: an adapter (OpenClaw or Claude Code) linked to the project
- **Shadow git**: invisible git tracking of `source_root` changes
- **Doc registry**: the SQLite record of which files belong to a project

---

## 2. Architecture

### 2.1 Two Registries — Critical Distinction

Quaid has **two separate registries** that serve different purposes and store
data in different places. Confusing them is a common source of bugs.

#### Project Registry

**Purpose:** Tracks project metadata — identity, instances, source roots, timestamps.

**Authoritative store:** `QUAID_HOME/projects/project-registry.json` (JSON file, shared across instances)

**Mirror:** SQLite `project_definitions` table in `memory.db` (seeded from JSON on first
`DocsRegistry` instantiation; kept in sync by `DocsRegistry.save_project_definition()`)

**Managed by:**
- `core/project_registry.py` — canonical implementation, called from daemons and CLI
- `lib/project_registry.py` — simpler reader/writer used for global registry queries
  (`quaid global-registry list`)

**Schema (one entry in the `projects` dict):**
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
plus atomic rename (`project-registry.tmp` → `project-registry.json`). The lock is acquired
on the **canonical file** (`project-registry.json`) via `O_CREAT | O_RDWR`, not on the `.tmp`
file. This is critical: locking `.tmp` is ineffective because each writer creates its own
uniquely-named temp file, so they never contend on the same fd across the full
read-modify-write cycle. Always lock the canonical path. The `lib/project_registry.py`
`_save()` uses atomic rename **without** file locking — acceptable for low-contention
global registry queries; high-frequency writes should go through `core/project_registry.py`.

#### Doc Registry

**Purpose:** Tracks individual files registered to projects, content hashes, source
mappings, and auto-update metadata.

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

#### Design Principle

The project registry answers: "What projects exist and who owns them?"

The doc registry answers: "Which files belong to a project, and what do they track?"

They are linked by project name only. There is no foreign key enforcement —
`delete_project()` in `core/project_registry.py` manually cleans both stores.

### 2.2 Canonical Paths and QUAID_HOME Layout

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
        log/
          YYYY-MM.log            # Monthly archive files
        docs/                    # Project documentation
    tracking/
      <name>/                    # Shadow git bare repo (git_dir)
    projects/staging/
      *.json                     # Queued updater events
      failed/                    # Events that could not be processed
```

### 2.3 Instances List

The `instances` field in a project entry records which adapter instances have
linked to the project. Values are `instance_id()` strings (e.g. `"claudecode-abc123"`),
NOT `adapter_id()` strings — a distinction that matters for cross-instance recognition
(see Section 9).

Rules:
- Project names are unique, lowercase, kebab-case (validated against `r"^[a-z0-9][a-z0-9-]*$"`).
- `source_root` is optional — some projects are doc-only (e.g. trip planning where Quaid creates all the documents).
- `instances` tracks which adapters have this project active. Used by the sync engine to know where to copy files.
- Both OC and CC read/write the same `project-registry.json` on a shared machine. File locking prevents corruption.

### 2.4 Project Directory Convention

Recommended layout for Quaid-managed projects:

```
projects/
├── my-app/
│   ├── PROJECT.md      # Auto-maintained project overview
│   ├── PROJECT.log     # Append-only history
│   ├── TOOLS.md        # Tools/APIs this project uses (loaded by bot)
│   ├── AGENTS.md       # Agent-specific instructions (loaded by bot)
│   └── docs/           # Additional tracked documentation
├── another-project/
│   └── PROJECT.md
└── staging/            # Reserved for Quaid internal use
```

Projects in `projects/` are auto-discovered by the janitor's RAG indexing task.
Projects elsewhere in the workspace need manual registration via `quaid project create`
and `quaid registry register <path> --project <name>` for any external files to index.

---

## 3. Lifecycle

### 3.1 `create_project(name, description, source_root)`

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
4. Builds entry dict with `instance_id()` as the first element of `instances`. Uses
   `lib.instance.instance_id`, NOT `adapter_id()` — see Section 9.
5. Creates `canonical/` and `canonical/docs/` directories.
6. Writes `PROJECT.md` template with name, description, and `created_at` timestamp.
7. If `source_root` provided: initializes `ShadowGit` and calls `sg.snapshot()` for
   initial baseline. Failure here is warned but does not abort project creation.
8. Writes entry to `project-registry.json` (atomic with file lock).
9. Calls `sync_all_projects()` — propagates new project dir to adapters that need sync
   (e.g. OpenClaw). Failure is warned but does not abort.

Returns the entry dict.

**Note on `DocsRegistry.create_project()`:** The `DocsRegistry` class in
`datastore/docsdb/registry.py` has its own `create_project()` method used by
`quaid registry create-project` (legacy path). It scaffolds the directory, writes a
richer `PROJECT.md` template, saves to `project_definitions` SQLite table, and
optionally patches `config/memory.json`. The two code paths differ slightly in template
format and JSON config patching. `quaid project create` (the canonical interface) uses
`core/project_registry.py`.

### 3.2 `link_project(name)`

**Location:** `core/project_registry.py`

```python
def link_project(name: str) -> Dict[str, Any]:
```

- Adds `instance_id()` to the project's `instances` list.
- Idempotent — no-op if the instance is already listed.
- Updates `updated_at` timestamp on change.
- Raises `KeyError` if project not found.

Use when a second adapter wants to participate in an existing project without taking
ownership (e.g. CC linking to an OC-created project).

### 3.3 `unlink_project(name)`

**Location:** `core/project_registry.py`

```python
def unlink_project(name: str) -> Dict[str, Any]:
```

- Removes `instance_id()` from the `instances` list.
- Idempotent — no-op if not currently listed.
- Updates `updated_at` timestamp on change.
- Does NOT delete the project or any files. An unlinked project remains in the
  registry; it just won't be returned to that instance.

### 3.4 `update_project(name, **updates)`

**Location:** `core/project_registry.py`

```python
def update_project(name: str, **updates: Any) -> Dict[str, Any]:
```

Allowed fields: `source_root`, `description`, `instances`. All other keys are silently
ignored. Raises `KeyError` if not found.

Called by `quaid project update <name> --description "..." --source-root /path`.

### 3.5 `delete_project(name)`

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

Note: synced workspace copies (OC) are cleaned up lazily by `_cleanup_stale_targets()`
in the sync engine on the next daemon tick.

---

## 4. Shadow Git

**Location:** `core/shadow_git.py`

### 4.1 Purpose

Track changes in the user's project files without putting any git artifacts
in the user's directory. Uses `--git-dir`/`--work-tree` split (same pattern as yadm/vcsh).

This enables:
- Detecting added/modified/deleted/renamed files between sessions
- Triggering docsdb reindexing for changed files
- Providing change context to the LLM ("these files changed since last time")

```
QUAID_HOME/.git-tracking/<project>/     # Git metadata (invisible to user)
  HEAD
  config
  objects/
  refs/
  info/
    exclude                              # Ignore patterns (LLM-managed)

User's project dir (source_root)         # Work tree (user's actual files)
  src/
  docs/
  package.json
  ...
```

Git commands use `--git-dir` and `--work-tree` to separate storage from the tracked directory:

```bash
git --git-dir=QUAID_HOME/.git-tracking/myapp \
    --work-tree=/home/user/code/myapp \
    status
```

### 4.2 Constructor

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

### 4.3 Key Properties and Methods

```python
@property
def initialized(self) -> bool:
    # True if git_dir/HEAD exists (file check, no git operations)
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

### 4.4 Return Types

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

### 4.5 Lifecycle Integration

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

Shadow git snapshots are triggered by **extraction events**, not daemon ticks. The
extraction event is the natural boundary — it's when new conversation context is captured
and when we want to know what the codebase looked like during that conversation.

```
Extraction event fires:
  1. Extract facts from conversation (existing)
  2. Extract project logs (existing)
  3. Shadow git snapshot (NEW):
     a. git add -A                 # Stage everything
     b. git commit -m "snapshot"   # Record state
     c. git diff HEAD~1..HEAD      # What exactly changed?
  4. Pass project logs + git diff to docs updater (NEW):
     → Docs updater decides: create/update/archive docs
  5. Rotate logs after successful distillation (token-budget-based)
  6. Sync project context files to adapter workspaces

Janitor (nightly):
  1. Consolidate any remaining project logs
  2. Sanity check all docs (staleness, bloat)
  3. Rotate logs after distillation
  4. Most of the time: nothing to do because daemon handled it
```

### 4.6 Default Ignore Patterns

`_DEFAULT_EXCLUDES` is written to `git_dir/info/exclude` at `init()` time. It covers:
secrets, dependencies, IDE artifacts, databases, media files, and Quaid internals. These
are hardcoded and cannot be removed via `add_ignore_patterns()`.

**Secrets (never track regardless of project type):**

```gitignore
.env
.env.*
*.pem
*.key
*.p12
*.pfx
*.keystore
*.jks
.credentials*
credentials.json
secrets.json
**/secret/**
**/secrets/**
.aws/
.ssh/
*.gpg
.netrc
.npmrc
.pypirc
token.txt
auth-token
.auth-token
```

**Dependencies and build artifacts:**

```gitignore
# Node
node_modules/
.npm/
package-lock.json
yarn.lock
pnpm-lock.yaml

# Python
__pycache__/
*.pyc
*.pyo
.venv/
venv/
env/
.eggs/
*.egg-info/
dist/
build/
*.whl

# Rust
target/

# Go
vendor/

# Java/JVM
*.class
*.jar
*.war
.gradle/
.m2/

# Ruby
.bundle/
vendor/bundle/

# General build
out/
_build/
.build/
cmake-build*/
```

**IDE and OS:**

```gitignore
.idea/
.vscode/
*.swp
*.swo
*~
.project
.classpath
.settings/
*.sublime-*
.fleet/
.DS_Store
Thumbs.db
desktop.ini
*.lnk
```

**Databases and large files:**

```gitignore
*.db
*.sqlite
*.sqlite3
*.db-shm
*.db-wal
*.zip
*.tar
*.tar.gz
*.tgz
*.rar
*.7z
*.dmg
*.iso
*.exe
*.dll
*.so
*.dylib
*.bin
*.dat
*.mp4
*.mp3
*.wav
*.avi
*.mov
*.mkv
*.flac
*.psd
*.ai
*.raw
*.cr2
*.nef
*.tiff
```

**Quaid internals (never track in shadow git):**

```gitignore
.quaid/
.git-tracking/
*.snippets.md
```

**LLM-managed additions:**

The LLM adds project-specific patterns by calling:

```python
shadow_git.add_ignore_patterns([
    "data/raw/*.csv",      # Large data files for this specific project
    "experiments/",        # Temporary experiment outputs
])
```

These are appended to `info/exclude` after the defensive defaults. The LLM decides
what to add by inspecting the project directory structure.

---

## 5. Sync Engine

**Location:** `core/sync_engine.py`

### 5.1 Purpose

Copy project bootstrap files (`TOOLS.md`, `AGENTS.md`, etc.) from the canonical
location (`QUAID_HOME/projects/<name>/`) to adapter workspaces that have boundary
constraints. Claude Code reads directly from `QUAID_HOME`. OpenClaw requires copies
inside its workspace boundary.

### 5.2 Why It Exists

OpenClaw's `ExtraBootstrapFiles` hook enforces a workspace boundary via
`openBoundaryFile()` → `realpathSync()`. Files must resolve inside
`~/.openclaw/workspace/`. Quaid's canonical project location is outside this boundary.

Tested on OC 2026.3.7 (Node 25.6.1):
- Symlinked directories: `fs.glob()` does NOT traverse them
- Symlinked files (target outside workspace): boundary guard rejects them
- Symlinked files (target inside workspace): works
- Direct absolute paths outside workspace: boundary guard rejects them
- **Copies inside workspace**: works (this is what the sync engine does)

### 5.3 Architecture

```
                    QUAID_HOME/projects/myapp/TOOLS.md  (canonical)
                              |
                    Sync Engine (core)
                    /                    \
          [CC: direct read]     [OC: copy to workspace]
                                         |
                    ~/.openclaw/workspace/plugins/quaid/projects/myapp/TOOLS.md
```

### 5.4 Sync Rules

1. **One-directional**: canonical → adapter workspace. Never the reverse.
2. **mtime-based**: only copy if canonical is newer than the workspace copy.
3. **Daemon-triggered**: runs on each daemon tick for projects with OC instances.
4. **Bootstrap files only**: `TOOLS.md`, `AGENTS.md`, and other files matching
   `SYNCABLE_NAMES`. Not the full project dir. `PROJECT.md` and doc content in
   `docs/` are not synced (they are either read directly or indexed separately).
5. **Read-only marker**: synced directories contain a `README.md` explaining
   where the canonical files live and that local edits will be overwritten.
6. **Adapter-requested**: the adapter declares it needs sync via its plugin
   contract. Core provides the service. Adapters that read directly (CC)
   don't request it.

### 5.5 `sync_project(canonical_dir, target_dir, project_name)`

```python
SYNCABLE_NAMES = frozenset({
    "TOOLS.md", "AGENTS.md", "SOUL.md", "USER.md",
    "ENVIRONMENT.md", "IDENTITY.md", "HEARTBEAT.md", "TODO.md",
})

def sync_project(canonical_dir: Path, target_dir: Path, project_name: str) -> SyncResult:
    """Sync one project's bootstrap files from canonical to target."""
    # For each name in SYNCABLE_NAMES:
    #   - If canonical file missing and target exists: remove target
    #   - If target mtime >= canonical mtime: skip
    #   - Otherwise: copy with shutil.copy2 (preserves mtime)
    # Writes a README.md in the target project dir pointing to canonical location.
    # Returns SyncResult(project, copied, skipped, removed, errors)
```

### 5.6 `sync_all_projects()`

```python
def sync_all_projects() -> List[SyncResult]:
    """Sync all registered projects to all adapters that need it.

    Calls adapter.get_context_sync_target(); if None, adapter reads directly
    (no sync needed). Iterates canonical projects dir and syncs each project.
    Also cleans up stale target dirs for projects that no longer exist.
    """
```

### 5.7 Adapter Contract

```python
class QuaidAdapter:
    def get_context_sync_target(self) -> Optional[Path]:
        """Return the directory where bootstrap files should be synced.

        Returns None if this adapter reads directly from QUAID_HOME
        (no sync needed). Returns a path if files must be copied into
        the adapter's workspace.
        """
        return None  # Default: no sync needed

class OpenClawAdapter(QuaidAdapter):
    def get_context_sync_target(self) -> Optional[Path]:
        return Path.home() / ".openclaw" / "workspace" / "plugins" / "quaid" / "projects"

class ClaudeCodeAdapter(QuaidAdapter):
    def get_context_sync_target(self) -> Optional[Path]:
        return None  # CC reads directly from QUAID_HOME
```

### 5.8 When `sync_all_projects()` Is Called

- After `create_project()` — to immediately propagate the new project dir.
- On daemon tick (extraction daemon main loop).
- Manually via `quaid project sync`.

### 5.9 Stale Target Cleanup

After syncing, `_cleanup_stale_targets()` removes synced project dirs in the target
that no longer have a corresponding canonical source. This fires when a project is
deleted and a sync cycle runs.

---

## 6. Project Updater

**Location:** `datastore/docsdb/project_updater.py`

**Purpose:** Background processor that updates stale docs based on extraction events
and source file changes. Spawned as a subprocess by compact/reset hooks.

### 6.1 Event Model

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

### 6.2 `process_event(event_path)`

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

### 6.3 `process_all_events()`

```python
def process_all_events() -> Dict:
```

Processes all `staging/*.json` files in chronological order (sorted by filename).
Failed events move to `staging/failed/`. Caps `failed/` at 20 entries.

### 6.4 Gating Cascade: mtime → rule-based → Haiku → Opus

The updater applies a cost-escalating decision cascade before calling an expensive LLM:

1. **mtime check** — skip if doc is newer than all its `source_files`. Free.
2. **Rule-based check** — skip if no meaningful content changed (e.g. only whitespace or
   comments). Cheap.
3. **Haiku** — for ambiguous cases, ask the small model if an update is needed. Cheap LLM.
4. **Opus** — only when a real update is confirmed necessary. Full rewrite.

This gate prevents unnecessary Opus calls when the doc is still accurate.

### 6.5 `evaluate_doc_health(project_name, dry_run=False)`

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

New docs suggested by the LLM must be placed under the `docs/` subdirectory. The prompt
enforces this as a rule (`"docs/architecture.md"`, not `"architecture.md"`).
Auto-created docs are scaffolded with an `<!-- Auto-created by project updater -->` comment.

### 6.6 `append_project_logs(project_logs, trigger, date_str, dry_run)`

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
  (human-visible recent frontier section).

Normalizes entries (strips leading bullets, session prefixes, collapses whitespace).
Deduplicates within the batch via `dict.fromkeys()`. Returns metrics dict.

`PROJECT.log` is append-only — `append_project_logs()` never truncates or rewrites it.
The marker-based section in `PROJECT.md` is a bounded recent window meant to show the
current frontier, not the full operational chronology.

### 6.7 Project Log Rotation

`PROJECT.log` is append-only and grows unbounded over time. Log rotation keeps a
bounded recent window and archives older entries.

**Layout:**
```
projects/<name>/
  PROJECT.log              # Recent entries only (last 7 days or last 100 entries)
  log/
    2026-03.log            # March 2026 archive
    2026-02.log            # February 2026 archive
    ...
```

**Rotation rules:**
1. Rotation is triggered **after distillation**, not on daemon ticks.
2. The recent window is bounded by a **token budget** (config:
   `projects.logTokenBudget`, default: 4000 tokens). Never split an entry.
3. Entries beyond the token budget are moved to `log/YYYY-MM.log` archives.
4. Keep `PROJECT.log` as the "recent" window the janitor/docs-updater reads.
5. Archives are append-only — once written, never modified.
6. If the file overflows before janitor can distill, the janitor chunks it for
   processing — **no truncation**.

**Implementation:** `core/log_rotation.py`, `rotate_log_file()`. Called from the janitor
after distillation (Task 6 in `janitor.py`), not from the daemon loop. The
`evaluate_doc_health()` function only needs the recent log.

**Historical log RAG indexing:** Historical project logs are indexed with metadata
marking them as historical:

```python
docsdb.index(
    path="projects/myapp/log/2026-02.log",
    metadata={
        "type": "project_log_archive",
        "project": "myapp",
        "period": "2026-02",
        "is_historical": True,
        "description": "Historical project log for myapp, February 2026",
    }
)
```

Retrieval wraps historical entries with a temporal marker:
```
[HISTORICAL — February 2026] These are archived project log entries,
not current state. Use for context on past decisions and approaches.
```

All log entries carry ISO timestamps (`[2026-03-11T10:00:00]`). Archive files are
named by month. Between the two, the LLM always knows when something happened.

### 6.8 Watchdog

`process_event()` is wrapped in `_run_with_watchdog()` when invoked from CLI. Default
timeout is 900s (configurable via `QUAID_PROJECT_UPDATER_WATCHDOG_SECONDS`). Uses
`signal.SIGALRM` (POSIX only). On timeout, event moves to `failed/`.

---

## 7. CLI Reference

### Environment Setup

```bash
export QUAID_HOME=/path/to/your/quaid-home
export QUAID_INSTANCE=openclaw   # or claude-code
```

### 7.1 Project Registry (`quaid project`)

#### List projects
```bash
quaid project list
quaid project list --json
```

#### Create a project
```bash
quaid project create <name> [--description "Display Name"] [--source-root /path]
```
Creates `QUAID_HOME/projects/<name>/` with `PROJECT.md` and `docs/` subdir.
Registers the project in `QUAID_HOME/projects/project-registry.json` and the SQLite
`project_definitions` table. Also triggers `sync_all_projects()`.

Note: The older `quaid registry create-project <name> --label "..."` command still
works (routes through `datastore/docsdb/registry.py`) but `quaid project create` is
the canonical interface.

#### Show a project
```bash
quaid project show <name>
```
Prints JSON with `canonical_path`, `instances`, `created_at`, `description`.

#### Update a project
```bash
quaid project update <name> --description "..." --source-root /path
```

#### Link current instance to an existing project
```bash
quaid project link <name>
```
Adds the current `QUAID_INSTANCE` to the project's `instances` list. Idempotent.
Use when a second adapter wants to participate in a project created by another adapter.

#### Unlink current instance from a project
```bash
quaid project unlink <name>
```
Removes the current `QUAID_INSTANCE` from the project's `instances` list. Idempotent.
Does not delete the project or its files.

#### Delete a project
```bash
quaid project delete <name>
```
Destructive. Removes from `project-registry.json`, removes `project_definitions` row
from `memory.db`, removes all `doc_registry` rows for this project from `memory.db`,
removes canonical project directory, destroys shadow git tracking if configured.
Does NOT touch the user's `source_root` directory.
Always verify with `quaid project show <name>` before deleting.

#### Snapshot (shadow git)
```bash
quaid project snapshot [<name>]   # All projects or named project
```

#### Sync to adapter workspaces
```bash
quaid project sync
```
Pushes bootstrap files to adapter workspaces. Same as `sync_all_projects()`.

#### Full function table for `core/project_registry_cli.py`

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

### 7.2 Doc Registry (`quaid registry` / `quaid docs`)

#### List registered docs
```bash
quaid registry list
quaid docs list --project <name>
```

#### Register a doc to a project
```bash
quaid registry register <file_path> --project <name> --description "..."
```
Accepts absolute paths or paths relative to the workspace root.

#### Search project docs (semantic RAG)
```bash
quaid docs search "query" --project <name>
quaid docs search "query"                   # search all projects
```
Requires embeddings (Ollama running locally or on a configured embedding server). Returns ranked chunks with similarity scores.

#### Check for stale docs
```bash
quaid docs check
```

#### Update stale docs
```bash
quaid docs update --apply
```

#### Reindex all docs (force embeddings refresh)
```bash
cd <quaid_module_root> && python3 datastore/docsdb/rag.py reindex --all
```
Scans `docs/` dir, top-level workspace `.md` files, and all `doc_registry` entries.

#### Evaluate doc health
```bash
quaid updater doc-health <project>
quaid updater doc-health <project> --dry-run
```
Deep LLM analysis producing create/update/archive decision lists.

### 7.3 Global Registry (`quaid global-registry`)

```bash
quaid global-registry list          # All projects (cross-instance view)
quaid global-registry show <name>
quaid global-registry register <name> <path>
quaid global-registry link <name>   # --instance, --symlink
quaid global-registry unlink <name> # --instance
quaid global-registry remove <name> # --force overrides multi-instance guard
quaid global-registry rename <name> <new>
```

Reads `QUAID_HOME/projects/project-registry.json`. When OC and CC run on the same machine and
share a `QUAID_HOME`, this shows the complete cross-adapter project list.

**`quaid global-registry list`** calls `lib/project_registry.list_all()`:
```python
def list_all() -> Dict[str, Dict[str, Any]]:
    return _load()["projects"]
```

Note: `lib/project_registry.link()` has an optional `create_symlink=True` parameter
that creates a filesystem symlink in the adapter's `projects/` dir pointing to the
canonical project path. This is not exposed in `core/project_registry.link_project()`.

---

## 8. Cross-Instance Workflow

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

### Step-by-step example

```bash
# OC creates a project
QUAID_INSTANCE=openclaw quaid project create my-proj --description "My Project"

# OC registers a doc
QUAID_INSTANCE=openclaw quaid registry register /path/to/doc.md --project my-proj

# CC sees it (same project-registry.json)
QUAID_INSTANCE=claude-code quaid global-registry list | grep my-proj

# CC links to it
QUAID_INSTANCE=claude-code quaid project link my-proj

# CC registers its own doc
QUAID_INSTANCE=claude-code quaid registry register /path/to/cc-doc.md --project my-proj

# Both adapters can now search all docs
QUAID_INSTANCE=openclaw quaid docs search "query" --project my-proj    # sees CC doc too
QUAID_INSTANCE=claude-code quaid docs search "query" --project my-proj  # sees OC doc too

# CC leaves the project (without deleting it)
QUAID_INSTANCE=claude-code quaid project unlink my-proj

# OC deletes the project entirely (unlinks all, removes files and DB rows)
QUAID_INSTANCE=openclaw quaid project delete my-proj
```

### Multi-adapter identity and divergence

If the same human uses CC and OC, each adapter builds its own identity (Layer 2).
Over time these diverge. **Current decision:** Divergence is acceptable. Each adapter's
identity is tuned to its platform. Revisit when multi-user spec is implemented.

### GitHub / Repository Inclusion

People commit `CLAUDE.md` to their GitHub repo so collaborators get the same project
context. The analogous Quaid approach for project files:

1. **Copy on publish:** The LLM copies relevant files (`TOOLS.md`, `AGENTS.md`, selected
   docs) from `QUAID_HOME/projects/<name>/` into the repo. These become static files in
   the repo, like `CLAUDE.md` today.
2. **Future: `quaid export`:** A CLI command that exports a project's context files to a
   target directory, formatted for repo inclusion. One-shot copy, not a live sync.
3. **Future: `.quaid.json` manifest:** A file committed to the repo that tells Quaid
   "this repo has project context — import it on clone."

**Current decision:** Manual export only. Build `quaid export` when there's demand.
The `.quaid.json` manifest is a future consideration.

---

## 9. Key Invariants and Gotchas

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
deletes the project, CC's `doc_registry` rows become orphaned. The janitor does
not currently clean these up automatically.

**Shadow git and missing `source_root`:** `snapshot_all_projects()` logs a warning
and skips any project whose `source_root` path does not exist on disk. It does not
raise. Safe to call even if some source roots have moved.

**Sync is one-way and mtime-gated:** `sync_project()` only copies canonical → target,
never the reverse. Edits made in the sync target will be silently overwritten on the
next sync cycle. The `README.md` written into each target dir warns about this.

**`PROJECT.log` is append-only.** `append_project_logs()` never truncates or rewrites
`PROJECT.log`. The marker-based section in `PROJECT.md` (`<!-- BEGIN:PROJECT_LOG -->`)
is a separate bounded recent frontier, not the full history. Log rotation via
`core.log_rotation.rotate_project_logs()` archives old entries; `PROJECT.log`
itself is never in-place trimmed.

**`_save_registry()` locking target:** The file lock must be acquired on the canonical
`project-registry.json`, not the `.tmp` staging file. Locking `.tmp` is ineffective
because each writer creates its own `.tmp` and they never contend on the same fd.
This was a fixed bug — always lock the canonical path.

**`evaluate_doc_health` placement constraint:** New docs suggested by the LLM must
be placed under the `docs/` subdirectory. The prompt enforces this.
Auto-created docs are scaffolded with an `<!-- Auto-created by project updater -->` comment.

**Shadow git performance on large repos:** If a user's project has 100k+ files,
`git status` might be slow. Mitigations: aggressive ignore patterns, enabling
`core.fsmonitor` on macOS/Linux, snapshots only run once per extraction event not on
every file change. Not a concern until proven slow in practice.

**`quaid global-registry` vs `quaid project show`:** Both read the same
`project-registry.json` — there is no divergence in data source. They differ only in
formatting and which sub-module formats the output.

**Doc registry is per-instance:** Shared project registry, separate doc registries.
Each adapter's `doc_registry` table is in its own `memory.db`. Indexing by one adapter
does not automatically make docs searchable by the other, even though both adapters use
the same Ollama embeddings backend.

---

## 10. Base Context File Contract (Adapter Reference)

Adapters can expose platform-native context files for janitor monitoring via
`get_base_context_files()`. These are Layer 1 files — Quaid didn't create them,
but monitors and trims them to prevent bloat.

```python
class QuaidAdapter:
    def get_base_context_files(self) -> Dict[str, Dict[str, Any]]:
        """Return platform-native context files for janitor monitoring.

        Returns a dict mapping file paths to monitoring config:
        {
            "/path/to/CLAUDE.md": {
                "purpose": "Project instructions and rules",
                "maxLines": 500,
            }
        }
        """
        return {}
```

ClaudeCode adapter monitors `CLAUDE.md` (`maxLines: 500`).
OpenClaw adapter monitors `SOUL.md` (80), `USER.md` (150), `ENVIRONMENT.md` (100),
`IDENTITY.md` (20), `HEARTBEAT.md` (50), `TODO.md` (150) from `~/.openclaw/workspace/`.

---

## References

- `projects/quaid/operations/project_onboarding.md` — workflow guide for discovering
  and registering projects in a new installation (do not duplicate here; that is a
  workflow doc, not a reference)
- `projects/quaid/reference/rag-docs-system.md` — RAG indexing pipeline, chunking, search
- `projects/quaid/reference/janitor-reference.md` — janitor task schedule and maintenance
- `projects/quaid/reference/memory-reference.md` — hooks and instance management
- `docs/TOOLS.md` — combined CLI reference for all quaid commands
