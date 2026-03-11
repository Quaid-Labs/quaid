# Project System Spec

How Quaid tracks projects, syncs files across adapters, and detects changes
in user workspaces.

**Status**: Draft — established 2026-03-11

---

## Overview

A "project" in Quaid is a container for:
- **Project docs** (Layer 3): documentation indexed into docsdb/RAG
- **Project context** (Layer 4): TOOLS.md/AGENTS.md injected into LLM context
- **Subject matter tracking** (Layer 5): shadow git monitoring of the user's
  actual project files (code, scripts, plans, etc.)

The human never manages projects directly. The LLM creates, configures, and
maintains projects through Quaid's tools. The human just talks.

---

## Project Registry

**Location**: `QUAID_HOME/project-registry.json`

```json
{
  "projects": {
    "japan-trip": {
      "canonical_path": "/home/solomon/quaid/projects/japan-trip",
      "source_root": "/home/solomon/Documents/Japan Trip",
      "instances": ["claudecode", "openclaw"],
      "created_at": "2026-03-11T10:00:00.000000",
      "description": "Japan trip planning — flights, hotels, itinerary"
    }
  }
}
```

| Field | Purpose |
|-------|---------|
| `canonical_path` | Where Quaid's project metadata lives (TOOLS.md, docs/) |
| `source_root` | Where the user's actual project files live (optional) |
| `instances` | Which adapter instances use this project |
| `created_at` | When the project was created |
| `description` | Human-readable description (LLM-generated) |

**Rules**:
- The registry is the single source of truth for all projects.
- Project names are unique, lowercase, kebab-case.
- `source_root` is optional — some projects are doc-only (e.g. trip planning
  where Quaid creates all the documents).
- `instances` tracks which adapters have this project active. Used by the
  sync engine to know where to copy files.

---

## Project Lifecycle

### Creation

The LLM calls `create_project(name, description, source_root=None)`:

1. Create `QUAID_HOME/projects/<name>/`
2. Create `PROJECT.md` with metadata
3. Create empty `docs/` subdirectory
4. Register in `project-registry.json`
5. If `source_root` provided, initialize shadow git tracking
6. If OC adapter is active, trigger sync to copy bootstrap files

### Tracking a source directory

When the LLM registers a `source_root`:

1. Initialize shadow git at `QUAID_HOME/.git-tracking/<name>/`
2. Set work tree to `source_root`
3. Apply default ignore patterns (see [Default Ignores](#default-ignore-patterns))
4. LLM reviews the directory and adds project-specific ignores if needed
5. Initial commit to establish baseline

### Deletion

The LLM calls `delete_project(name)`:

1. Remove from `project-registry.json`
2. Remove `QUAID_HOME/projects/<name>/`
3. Remove shadow git at `QUAID_HOME/.git-tracking/<name>/`
4. Remove synced copies from all adapter workspaces
5. Remove docsdb entries for this project
6. **Never touch `source_root`** — that's the user's files

---

## Shadow Git Tracking

### Purpose

Track changes in the user's project files without putting any git artifacts
in the user's directory. Enables:
- Detecting added/modified/deleted/renamed files between sessions
- Triggering docsdb reindexing for changed files
- Providing change context to the LLM ("these files changed since last time")

### Architecture

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

Git commands use `--git-dir` and `--work-tree` to separate storage from
the tracked directory:

```bash
git --git-dir=QUAID_HOME/.git-tracking/myapp \
    --work-tree=/home/user/code/myapp \
    status
```

### Extraction Event Integration

Shadow git snapshots are triggered by **extraction events**, not daemon
ticks. The extraction event is the natural boundary — it's when new
conversation context is captured and when we want to know what the
codebase looked like during that conversation.

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

The extraction daemon is the **primary** mechanism for keeping docs
up to date. The janitor is a nightly safety net that consolidates and
does final sanity checks. In many cases the janitor finds nothing to
do because the daemon already handled everything.

### Python Wrapper

```python
class ShadowGit:
    """Shadow git tracker for a project's source files."""

    def __init__(self, project_name: str, source_root: Path):
        self.git_dir = QUAID_HOME / ".git-tracking" / project_name
        self.work_tree = source_root

    def _git(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", f"--git-dir={self.git_dir}",
             f"--work-tree={self.work_tree}", *args],
            capture_output=True, text=True,
        )

    def init(self):
        """Initialize shadow git for this project."""
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._git("init", "--bare")
        self._apply_default_ignores()

    def snapshot(self) -> Optional[DiffResult]:
        """Snapshot current state, return diff if anything changed."""
        status = self._git("status", "--porcelain")
        if not status.stdout.strip():
            return None  # Nothing changed
        self._git("add", "-A")
        self._git("commit", "-m", f"snapshot {datetime.utcnow().isoformat()}")
        diff = self._git("diff", "--find-renames", "--name-status", "HEAD~1..HEAD")
        return parse_diff(diff.stdout)

    def get_tracked_files(self) -> List[str]:
        """List all tracked files."""
        result = self._git("ls-files")
        return result.stdout.strip().splitlines()
```

---

## Default Ignore Patterns

Defensive defaults applied to every shadow git. These protect against
indexing secrets, binaries, and noise. The LLM can add project-specific
patterns but cannot remove defensive defaults.

### Secrets & Credentials (never track)

```gitignore
# Secrets — NEVER track regardless of project type
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

### Dependencies & Build Artifacts

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

### IDE & OS

```gitignore
# IDE
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

# OS
.DS_Store
Thumbs.db
desktop.ini
*.lnk
```

### Databases & Large Files

```gitignore
# Databases
*.db
*.sqlite
*.sqlite3
*.db-shm
*.db-wal

# Large binary files
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

# Media (unless specifically needed)
*.mp4
*.mp3
*.wav
*.avi
*.mov
*.mkv
*.flac

# Images (large formats — small formats like .png/.svg may be relevant)
*.psd
*.ai
*.raw
*.cr2
*.nef
*.tiff
```

### Quaid Internal

```gitignore
# Quaid's own files (never track in shadow git)
.quaid/
.git-tracking/
*.snippets.md
```

### LLM-Managed Additions

The LLM adds project-specific patterns by calling:

```python
shadow_git.add_ignore_patterns([
    "data/raw/*.csv",      # Large data files for this specific project
    "experiments/",        # Temporary experiment outputs
])
```

These are written to `info/exclude` in the shadow git dir, appended after
the defensive defaults. The LLM decides what to add by inspecting the
project directory structure.

---

## Sync Engine

### Purpose

Copy project bootstrap files (TOOLS.md, AGENTS.md) from the canonical
location (`QUAID_HOME/projects/<name>/`) to adapter workspaces that require
it (currently: OpenClaw).

### Why It Exists

OpenClaw's `ExtraBootstrapFiles` hook enforces a workspace boundary via
`openBoundaryFile()` → `realpathSync()`. Files must resolve inside
`~/.openclaw/workspace/`. Quaid's canonical project location is outside
this boundary.

Tested on OC 2026.3.7 (Node 25.6.1):
- Symlinked directories: `fs.glob()` does NOT traverse them
- Symlinked files (target outside workspace): boundary guard rejects them
- Symlinked files (target inside workspace): works
- Direct absolute paths outside workspace: boundary guard rejects them
- **Copies inside workspace**: works (this is what the sync engine does)

Claude Code has no such boundary and reads directly from QUAID_HOME.

### Architecture

```
                    QUAID_HOME/projects/myapp/TOOLS.md  (canonical)
                              |
                    Sync Engine (core)
                    /                    \
          [CC: direct read]     [OC: copy to workspace]
                                         |
                    ~/.openclaw/workspace/plugins/quaid/projects/myapp/TOOLS.md
```

### Sync Rules

1. **One-directional**: canonical → adapter workspace. Never the reverse.
2. **mtime-based**: only copy if canonical is newer than the workspace copy.
3. **Daemon-triggered**: runs on each daemon tick for projects with OC instances.
4. **Bootstrap files only**: TOOLS.md, AGENTS.md, and other files matching
   `VALID_BOOTSTRAP_NAMES`. Not the full project dir.
5. **Read-only marker**: synced directories contain a `README.md` explaining
   where the canonical files live and that local edits will be overwritten.
6. **Adapter-requested**: the adapter declares it needs sync via its plugin
   contract. Core provides the service. Adapters that read directly (CC)
   don't request it.

### Sync Contract

```python
class SyncEngine:
    """Sync bootstrap files from canonical to adapter workspaces."""

    # Files eligible for sync (OC's VALID_BOOTSTRAP_NAMES equivalent)
    SYNCABLE_NAMES = {"TOOLS.md", "AGENTS.md", "SOUL.md", "USER.md",
                      "MEMORY.md", "IDENTITY.md", "HEARTBEAT.md", "TODO.md"}

    def sync_project(self, project_name: str, target_dir: Path) -> SyncResult:
        """Sync one project's bootstrap files to a target directory."""
        canonical = QUAID_HOME / "projects" / project_name
        result = SyncResult(project=project_name, copied=[], skipped=[], removed=[])

        for fname in self.SYNCABLE_NAMES:
            src = canonical / fname
            dst = target_dir / project_name / fname

            if not src.is_file():
                # Canonical file removed — clean up target
                if dst.is_file():
                    dst.unlink()
                    result.removed.append(fname)
                continue

            if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                result.skipped.append(fname)
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)  # preserves mtime
            result.copied.append(fname)

        self._write_readme(target_dir / project_name, canonical)
        return result

    def sync_all(self) -> List[SyncResult]:
        """Sync all projects to all adapters that need it."""
        results = []
        for project_name, project_info in registry.items():
            for adapter in project_info["instances"]:
                sync_target = get_adapter(adapter).get_context_sync_target()
                if sync_target is None:
                    continue  # Adapter reads directly, no sync needed
                results.append(self.sync_project(project_name, sync_target))
        return results
```

### Adapter Contract Addition

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

---

## Base Context File Contract

### Purpose

Let the janitor know which platform-native context files exist so it can
monitor and slim them. These are Layer 1 files — Quaid didn't create them,
but Quaid keeps them healthy.

### Adapter Contract

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

        The janitor will monitor these for bloat and slim them during
        maintenance runs. Quaid does NOT create or manage these files —
        only trims them.
        """
        return {}

class ClaudeCodeAdapter(QuaidAdapter):
    def get_base_context_files(self) -> Dict[str, Dict[str, Any]]:
        # CLAUDE.md lives in the user's project cwd
        claude_md = self._find_claude_md()
        if claude_md and claude_md.is_file():
            return {
                str(claude_md): {
                    "purpose": "Claude Code project instructions and rules",
                    "maxLines": 500,
                }
            }
        return {}

class OpenClawAdapter(QuaidAdapter):
    def get_base_context_files(self) -> Dict[str, Dict[str, Any]]:
        ws = Path.home() / ".openclaw" / "workspace"
        files = {}
        for name, purpose, max_lines in [
            ("SOUL.md", "Personality, vibe, interaction style", 80),
            ("USER.md", "About the user", 150),
            ("MEMORY.md", "Core memories loaded every session", 100),
            ("IDENTITY.md", "Name, avatar, minimal identity", 20),
            ("HEARTBEAT.md", "Periodic task instructions", 50),
            ("TODO.md", "Planning and task list", 150),
        ]:
            fpath = ws / name
            if fpath.is_file():
                files[str(fpath)] = {"purpose": purpose, "maxLines": max_lines}
        return files
```

---

## Project Log Rotation

### Problem

`PROJECT.log` is append-only — every janitor run appends timestamped entries.
Over weeks/months this file grows unbounded and becomes unwieldy for both
the LLM (context window waste) and the human (unreadable).

### Current State

`append_project_logs()` in `project_updater.py` appends lines like:
```
- [2026-03-11T10:00:00] Updated API endpoint documentation
- [2026-03-11T10:00:00] Added error handling for auth flow
```

No rotation, no archival. The file just grows.

### Solution: Log Rotation

Keep a small recent log that the janitor reads for context. Archive older
entries into dated files.

**Layout**:
```
projects/<name>/
  PROJECT.log              # Recent entries only (last 7 days or last 100 entries)
  log/
    2026-03.log            # March 2026 archive
    2026-02.log            # February 2026 archive
    ...
```

**Rotation rules**:
1. Rotation is triggered **after distillation**, not on daemon ticks.
2. The recent window is bounded by a **token budget** (config:
   `projects.logTokenBudget`, default: 4000 tokens). Never split an entry.
3. Entries beyond the token budget are moved to `log/YYYY-MM.log` archives.
4. Keep `PROJECT.log` as the "recent" window the janitor/docs-updater reads.
5. Archives are append-only — once written, never modified.
6. If the file overflows before janitor can distill (unlikely), the janitor
   chunks it for processing — **no truncation** (see CLAUDE.md code rules).

**Integration with janitor**:
- The janitor reads `PROJECT.log` (recent) for context when making decisions.
- When it needs historical context, it can read from `log/` archives.
- The `evaluate_doc_health()` function only needs the recent log — it doesn't
  need the full history to decide what docs need updating.

**Implementation**: See `core/log_rotation.py`. Token-budget-based rotation
using `rotate_log_file()`. Called from the janitor after distillation
(Task 6 in `janitor.py`), not from the daemon loop.

---

## GitHub / Repository Inclusion

### The Pattern

Today people commit `CLAUDE.md` to their GitHub repo so collaborators get
the same project context. If Quaid succeeds, people will want to do the same
with their project's `TOOLS.md`, `AGENTS.md`, and docs.

### Why Not Cohabitate?

We considered putting Quaid's project folder inside the user's source
directory (e.g. `~/code/myapp/.quaid/` or `~/code/myapp/projects/quaid/`).
This would make GitHub inclusion automatic. But it creates problems:

- Quaid's git tracking would collide with the repo's own git
- Quaid's daemon writes (doc updates, metadata) would create noise in the
  user's git status
- Multiple Quaid instances on different machines would conflict
- The user's repo structure is theirs — Quaid shouldn't impose layout

### Recommended Approach

Keep Quaid's project folder in QUAID_HOME. When the user (or LLM) wants to
include project context in a GitHub repo, it's a manual/intentional export:

1. **Copy on publish**: The LLM copies relevant files (TOOLS.md, AGENTS.md,
   selected docs) from `QUAID_HOME/projects/<name>/` into the repo. These
   become static files in the repo, like CLAUDE.md today.

2. **Future: `quaid export`**: A CLI command that exports a project's context
   files to a target directory, formatted for inclusion in a repo. This is
   a one-shot copy, not a live sync.

3. **Future: `.quaid.json` manifest**: A file committed to the repo that
   tells Quaid "this repo has project context — import it on clone." Similar
   to how `.nvmrc` tells nvm which Node version to use. This creates the
   reverse flow: repo → Quaid import.

This is analogous to how CLAUDE.md works today. Nobody expects CLAUDE.md to
auto-sync with some central store. You edit it in the repo, commit it, push.
Quaid's project files would work the same way when shared via GitHub.

**Current decision**: Manual export only. Build `quaid export` when there's
demand. The `.quaid.json` manifest is a future consideration.

---

## RAG Indexing of Project Log Archives

### Should historical logs be indexed?

**Yes** — historical project logs are valuable context. "We tried approach X
in February and it failed because Y" is exactly the kind of thing an LLM
needs to avoid repeating mistakes.

### How to avoid confusing current vs historical

Every log archive file is indexed with metadata that clearly marks it as
historical:

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

When retrieval returns historical log entries, the retrieval prompt wrapper
includes a temporal marker:

```
[HISTORICAL — February 2026] These are archived project log entries,
not current state. Use for context on past decisions and approaches.
```

### Timestamps are mandatory

All log entries already have ISO timestamps (`[2026-03-11T10:00:00]`).
The archive files are named by month (`2026-02.log`). Between these two,
the LLM always knows when something happened.

The recent `PROJECT.log` entries are indexed as current. The archived
entries under `log/` are indexed as historical. The distinction is clear
from the file path alone.

---

## Open Questions

### Multi-adapter identity convergence

If the same human uses CC and OC, each adapter builds its own identity
(Layer 2). Over time these diverge. Should there be a periodic reconciliation
step that merges insights? Or is divergence acceptable?

**Current decision**: Divergence is acceptable. Each adapter's identity is
tuned to its platform. Revisit when multi-user spec is implemented.

### Project context in ExtraBootstrapFiles registration

Currently the OC installer registers glob patterns like
`projects/*/TOOLS.md` in the `bootstrap-extra-files` hook config. When the
sync engine copies files to the workspace, they land at
`plugins/quaid/projects/<name>/TOOLS.md` which matches this glob.

If the glob patterns change (e.g. OC renames the hook), the sync engine
still works — it's the hook registration that needs updating. The installer
handles this.

### Shadow git performance on large repos

If a user's project has 100k+ files, `git status` might be slow. Mitigations:
- Aggressive ignore patterns reduce tracked files
- `core.fsmonitor` can be enabled for instant status on macOS/Linux
- The daemon only snapshots once per tick, not on every file change

Not a concern until proven slow in practice.

---

## References

- [Directory Standard](DIRECTORY-STANDARD.md) — canonical file layout
- [Design Principles](DESIGN-PRINCIPLES.md) — why invisibility matters
- [Architecture](ARCHITECTURE.md) — system architecture
