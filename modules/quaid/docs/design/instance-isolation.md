# Instance Isolation Design

## Problem

Quaid can run against multiple host adapters and multiple user/work contexts on
the same machine. Runtime state, identity files, project links, daemons, and logs
must stay isolated per memory instance so one adapter or workspace cannot
silently read, write, or extract another instance's state.

## Core Concept

**INSTANCE_ID** is the silo identifier for one Quaid memory instance. Two
processes with the same `INSTANCE_ID` share the same Quaid memory state.
Conventional values include `openclaw`, `claude-code`, `codex`, `personal`, and
`work`.

The current layout has two roots:

- `QUAID_HOME`: hidden runtime root. Defaults to `~/.quaid`.
- `QUAID_VISIBLE_HOME`: user-facing markdown/project root. Defaults to the
  visible sibling of `QUAID_HOME` when `QUAID_HOME` starts with a dot, for
  example `~/.quaid` -> `~/quaid`.

```
QUAID_HOME/
├── instances/
│   └── <INSTANCE_ID>/              # Hidden per-instance silo
│       ├── config.json             # Instance config
│       ├── data/
│       │   ├── memory.db           # Instance memory database
│       │   ├── extraction-signals/
│       │   ├── session-cursors/
│       │   ├── extraction-daemon.pid
│       │   └── ...
│       └── logs/
├── project-registry.json           # Global project registry metadata
└── shared/
    └── config/                     # Platform-shared config, when present

QUAID_VISIBLE_HOME/
├── instances/
│   └── <INSTANCE_ID>/              # Visible per-instance markdown root
│       ├── USER.md
│       ├── SOUL.md
│       ├── ENVIRONMENT.md
│       ├── journal/
│       └── *.snippets.md
└── projects/                       # Cross-instance project docs/symlinks
    └── my-app/
        ├── PROJECT.md
        ├── TOOLS.md
        └── AGENTS.md
```

## INSTANCE_ID Rules

- Must be a valid directory name: no `/`, no whitespace, no `.` prefix.
- Must not be a reserved name: `shared`, `projects`, `config`, `data`, `logs`,
  `temp`, `tmp`, `quaid`, `plugins`, `lib`, `core`.
- Passed via env: `QUAID_INSTANCE`.
- Resolved hidden root: `$QUAID_HOME/instances/$QUAID_INSTANCE`.
- Resolved visible root: `$QUAID_VISIBLE_HOME/instances/$QUAID_INSTANCE`.

## Environment Variables

| Var | Purpose | Example |
|-----|---------|---------|
| `QUAID_HOME` | Hidden runtime root containing all instance silos | `~/.quaid` |
| `QUAID_VISIBLE_HOME` | Visible markdown/project root | `~/quaid` |
| `QUAID_INSTANCE` | Instance identifier | `openclaw` |

## What Changes Per Instance

### Daemon

- One daemon per instance, keyed by `QUAID_INSTANCE`.
- PID file: `<instance_root>/data/extraction-daemon.pid`.
- Signal dir: `<instance_root>/data/extraction-signals/`.
- Cursors and runtime extraction state live under `<instance_root>/data/`.

### Config

- Per-instance config: `<instance_root>/config.json`.
- Adapter type is derived from instance config or inferred from the selected
  adapter/instance path.

### Hooks

Hook commands select their instance through environment, for example:

```bash
QUAID_HOME=/Users/x/.quaid QUAID_VISIBLE_HOME=/Users/x/quaid \
  QUAID_INSTANCE=claude-code quaid hook-inject
```

Adapters set `QUAID_INSTANCE` for their own hook/runtime processes.

### OpenClaw Adapter

- `QUAID_INSTANCE` is selected by the OpenClaw plugin at boot.
- Hidden runtime state is read from `$QUAID_HOME/instances/<INSTANCE_ID>/`.
- Visible identity markdown is read from
  `$QUAID_VISIBLE_HOME/instances/<INSTANCE_ID>/`.

### Shared Projects

- Project docs/symlinks live at `$QUAID_VISIBLE_HOME/projects/`.
- Global project metadata lives at `$QUAID_HOME/project-registry.json`.
- Each project tracks linked instances in its registry entry.
- Hooks read visible project docs through `adapter.projects_dir()`.

### Path Resolution (`lib/adapter.py`)

| Method | Current path |
|--------|--------------|
| `quaid_home()` | `$QUAID_HOME` |
| `visible_home()` | `$QUAID_VISIBLE_HOME`, or visible sibling of `QUAID_HOME` |
| `instance_root()` | `$QUAID_HOME/instances/$QUAID_INSTANCE` |
| `visible_instance_root()` | `$QUAID_VISIBLE_HOME/instances/$QUAID_INSTANCE` |
| `data_dir()` | `instance_root()/data` |
| `config_dir()` | `instance_root()` |
| `identity_dir()` | `visible_instance_root()` |
| `projects_dir()` | `visible_home()/projects` |
| `logs_dir()` | `instance_root()/logs` |
| `journal_dir()` | `visible_instance_root()/journal` |

### Key Passed Around

`INSTANCE_ID` is the primary key for:

- Project registry instance links.
- Daemon PID management.
- Notification routing.
- Project-doc visibility and sync decisions.
- Hook/runtime command selection through `QUAID_INSTANCE`.

## Installation

INSTANCE_ID is selected during installation or derived by adapter bootstrap.
The installer/runtime path should:

1. Validate the name against the instance rules.
2. Check for an existing hidden config at
   `$QUAID_HOME/instances/<INSTANCE_ID>/config.json`.
3. Check for existing visible markdown at
   `$QUAID_VISIBLE_HOME/instances/<INSTANCE_ID>/`.
4. Create missing hidden and visible instance directories.
5. Persist `QUAID_INSTANCE=<INSTANCE_ID>` in the adapter's environment config.

Detection of existing instances:

- List directories under `$QUAID_HOME/instances/` that contain `config.json`.
- Show the adapter type from each instance config when available.

## CLI

The shipped CLI selects an instance through environment or adapter-derived
runtime configuration, not through a positional instance argument.

Examples:

```bash
QUAID_INSTANCE=openclaw quaid project list
QUAID_INSTANCE=claude-code quaid recall "what do you remember?"
QUAID_INSTANCE=codex quaid hook-inject
```

If `QUAID_INSTANCE` is missing, adapter bootstrap may derive it from the host
adapter config. Commands that require a specific instance should set
`QUAID_INSTANCE` explicitly.

## TODO: CC Slash Command for Instance Switching

CC supports custom slash commands via Skills (`.claude/skills/<name>/SKILL.md`).
Potential command: `/quaid:set-memory <instance_id>` to switch the active memory
instance mid-session.

**Open question:** Does switching instances mid-session risk mixing contexts?
The extraction daemon, pending notifications, and identity files would all shift.
May need a session boundary - flush pending state before switching. Or disallow
mid-session switching and require it at CC launch via env var only.

Investigate whether this is better as:

- A CC skill (`/quaid:set-memory`) that sets `QUAID_INSTANCE` for subsequent hooks.
- An env var set before CC launch.
- A per-project `.claude/settings.json` config.

## Reserved Names

```python
RESERVED_INSTANCE_NAMES = frozenset({
    "shared", "projects", "config", "data", "logs", "temp", "tmp",
    "quaid", "plugins", "lib", "core", "docs", "assets", "release",
    "scripts", "test", "tests", "benchmark", "node_modules",
})
```
