# Instance Isolation Design

## Problem

Everything currently shares one `QUAID_HOME` (`~/quaid/`). One config, one
DB, one daemon. Switching adapters requires restart and risks silent data
corruption (daemon caches adapter, parses wrong transcript format).

## Core Concept

**INSTANCE_ID** — a short identifier (valid folder name) that uniquely
identifies a Quaid memory instance. Two terminals with the same INSTANCE_ID
share the same memory. Examples: `openclaw`, `claude-code`, `work`, `personal`.

```
QUAID_HOME/
├── shared/                         # Reserved
│   └── projects/                   # Cross-instance project docs
│       └── my-app/
│           ├── PROJECT.md
│           ├── TOOLS.md
│           └── AGENTS.md
├── <INSTANCE_ID>/                  # Per-instance silo
│   ├── config/memory.json          # Instance config (no adapter.type toggle)
│   ├── data/
│   │   ├── memory.db               # Own database
│   │   ├── extraction-signals/
│   │   ├── session-cursors/
│   │   ├── extraction-daemon.pid   # Own daemon
│   │   └── ...
│   ├── identity/
│   │   ├── USER.md
│   │   ├── SOUL.md
│   │   └── ENVIRONMENT.md
│   ├── journal/
│   ├── logs/
│   ├── *.snippets.md
│   └── project-registry.json       # Instance's view of projects
└── shared/
    └── project-registry.json       # Global registry (projects -> instances)
```

## INSTANCE_ID Rules

- Must be a valid directory name (no `/`, no whitespace, no `.` prefix)
- Must NOT be a reserved name: `shared`, `projects`, `config`, `data`,
  `logs`, `temp`, `tmp`, `quaid`, `plugins`, `lib`, `core`
- Conventional values: `openclaw`, `claude-code`, `personal`, `work`
- Passed via env: `QUAID_INSTANCE` (or `QUAID_ID`)
- Resolved: `QUAID_HOME / INSTANCE_ID` = instance root

## Environment Variables

| Var | Purpose | Example |
|-----|---------|---------|
| `QUAID_HOME` | Root dir (contains all instances) | `~/quaid` |
| `QUAID_INSTANCE` | Instance identifier (folder name) | `openclaw` |

Instance root = `$QUAID_HOME/$QUAID_INSTANCE`

## What Changes

### Daemon
- One daemon per instance (keyed by INSTANCE_ID)
- PID file: `<instance_root>/data/extraction-daemon.pid`
- No adapter caching bug — each daemon IS its adapter
- Signal dir: `<instance_root>/data/extraction-signals/`

### Config
- Per-instance: `<instance_root>/config/memory.json`
- No `adapter.type` toggle — the instance IS the adapter
- Adapter type derived from instance config or inferred from INSTANCE_ID

### Hooks (CC)
- `QUAID_HOME=/Users/x/quaid QUAID_INSTANCE=claude-code quaid hook-inject`
- Hook commands include both env vars

### OC Adapter
- `QUAID_INSTANCE=openclaw` set by OC plugin at boot
- Reads from `$QUAID_HOME/openclaw/`

### Shared Projects
- Live at `$QUAID_HOME/shared/projects/`
- Global registry at `$QUAID_HOME/shared/project-registry.json`
- Each project has `instances: ["openclaw", "claude-code"]`
- Sync engine copies from `shared/projects/` to OC workspace
- CC reads `shared/projects/` directly (via hook_session_init)

### Path Resolution (lib/adapter.py)
- `quaid_home()` → `$QUAID_HOME` (root)
- `instance_root()` → `$QUAID_HOME/$QUAID_INSTANCE`
- `data_dir()` → `instance_root/data/`
- `config_dir()` → `instance_root/config/`
- `identity_dir()` → `instance_root/identity/`
- `projects_dir()` → `$QUAID_HOME/shared/projects/` (shared!)
- `logs_dir()` → `instance_root/logs/`

### Key Passed Around
- `INSTANCE_ID` is the primary key in:
  - Project registry (`instances` list)
  - Daemon PID management
  - Notification routing
  - Sync engine (target resolution)
  - CLI commands (`quaid openclaw stats`)

## Installation

INSTANCE_ID is entered by the user during installation:

1. Installer prompts: "Enter an instance name (e.g. openclaw, claude-code, work):"
2. Validates against INSTANCE_ID rules (valid dir name, not reserved)
3. Scans `$QUAID_HOME/` for existing instance dirs and displays them:
   ```
   Existing instances:
     • openclaw (adapter: openclaw)
     • claude-code (adapter: claude-code)
   Enter instance name: ▌
   ```
4. If name matches an existing instance, warns and asks to confirm (join vs create new)
5. Creates instance dir structure under `$QUAID_HOME/<INSTANCE_ID>/`
6. Sets `QUAID_INSTANCE=<INSTANCE_ID>` in the adapter's env config

Detection of existing instances:
- List dirs in `$QUAID_HOME/` that are not reserved names and contain `config/memory.json`
- Each discovered instance shows its adapter type (from config)

## CLI

Instance is always the first argument:

```
quaid <instance> <command> [args...]
```

Examples:
- `quaid openclaw stats`
- `quaid claude-code project list`
- `quaid openclaw memory_store "some text"`
- `quaid claude-code hook-inject`

The instance must exist (`$QUAID_HOME/<instance>/config/memory.json` present)
or the CLI throws an error. No implicit defaults — instance is always explicit.

For hooks, `QUAID_INSTANCE` env var is also accepted (set by adapter at boot):
```
QUAID_INSTANCE=claude-code quaid hook-inject
```
If both CLI arg and env var are present, CLI arg wins.

## TODO: CC Slash Command for Instance Switching

CC supports custom slash commands via Skills (`.claude/skills/<name>/SKILL.md`).
Potential command: `/quaid:set-memory <instance_id>` to switch the active memory
instance mid-session.

**Open question:** Does switching instances mid-session risk mixing contexts?
The extraction daemon, pending notifications, and identity files would all shift.
May need a "session boundary" — flush pending state before switching. Or disallow
mid-session switching and require it at CC launch via env var only.

Investigate whether this is better as:
- A CC skill (`/quaid:set-memory`) that sets `QUAID_INSTANCE` for subsequent hooks
- An env var set before CC launch (simpler, no context mixing risk)
- A per-project `.claude/settings.json` config (auto-resolved, no user action)

## Reserved Names

```python
RESERVED_INSTANCE_NAMES = frozenset({
    "shared", "projects", "config", "data", "logs", "temp", "tmp",
    "quaid", "plugins", "lib", "core", "docs", "assets", "release",
    "scripts", "test", "tests", "benchmark", "node_modules",
})
```
