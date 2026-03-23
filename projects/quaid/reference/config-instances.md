# Quaid Config & Instance Reference

Technical reference for the Quaid instance model and configuration system.
Covers instance identity, the config merge chain, the config CLI, and
per-instance vs. shared state layout.

---

## 1. Instance Model

### What is a Quaid instance?

A Quaid instance is an isolated memory silo identified by a short string (the
instance ID). Each instance has its own config, database, logs, and identity
files under `QUAID_HOME/<instance_id>/`.

Two processes with the same `QUAID_HOME` and `QUAID_INSTANCE` share memory.
Two processes with different `QUAID_INSTANCE` values are fully isolated, even
on the same machine with the same `QUAID_HOME`.

### Instance ID rules

Defined in `lib/instance.py` (`validate_instance_id`):

- Must start with an alphanumeric character.
- May contain `[a-zA-Z0-9._-]`, max 64 characters.
- Cannot be a reserved name (see below).

Reserved names that may not be used as instance IDs:

```
shared  projects  config  data  logs  temp  tmp  quaid  plugins  lib  core
docs  assets  release  scripts  test  tests  benchmark  node_modules
```

### Instance detection

`lib/instance.instance_id()` reads `QUAID_INSTANCE` from the environment.
It raises `InstanceError` if the variable is unset or invalid — there is no
implicit default. The CLI entrypoint and adapter hooks are responsible for
setting `QUAID_INSTANCE` before invoking any Quaid code.

```
QUAID_INSTANCE=claude-code quaid recall "query"
```

### What counts as an initialized instance?

`lib/instance.list_instances()` considers a directory under `QUAID_HOME` an
instance if and only if it contains `config/memory.json`. Directories that
exist but lack this file are ignored by `quaid instances list`.

### adapter_id vs instance_id

These are two different concepts:

| Function | Returns | Source |
|---|---|---|
| `lib/instance.instance_id()` | The name of the active silo, e.g. `"claude-code"` | `QUAID_INSTANCE` env |
| `QuaidAdapter.adapter_id()` | The adapter *type*, e.g. `"claude-code"`, `"openclaw"`, `"standalone"` | Hardcoded in adapter class |

`adapter_id` identifies the host platform. `instance_id` identifies which
memory silo is active. They often have similar values but are independent:
two different instances can both use the same adapter type — for example,
two separate `openclaw` installs sharing a machine.

The Claude Code adapter (`adaptors/claude_code/adapter.py`) returns
`"claude-code"` from `adapter_id()`. The OpenClaw adapter returns
`"openclaw"`. The base `StandaloneAdapter` returns `"standalone"`.

### instance_root() path

```
QUAID_HOME/<instance_id>/
```

Computed by `lib/instance.instance_root()` = `quaid_home() / instance_id()`.
All per-instance paths derive from this root:

```
QUAID_HOME/<instance_id>/
  config/memory.json      instance-specific config
  data/memory.db          SQLite database
  data/memory_archive.db  archive database
  data/extraction-signals/ async extraction signal files
  logs/                   janitor stats, extraction logs
  identity/               Quaid-managed identity files (SOUL.md, USER.md, etc.)
  journal/                journal entries for distillation
```

### Identity seed vs. live identity

`projects/quaid/SOUL.md`, `projects/quaid/USER.md`, and
`projects/quaid/ENVIRONMENT.md` are base templates shipped with Quaid. They are
seed material, not the live writable copies.

For a real instance, the writable files live under:

```text
QUAID_HOME/<instance_id>/identity/
  SOUL.md
  USER.md
  ENVIRONMENT.md
```

At install or workspace bootstrap time, the base templates are copied into the
instance `identity/` directory. After that:

- janitor distillation and snippet compaction update the instance `identity/`
  files
- `projects/quaid/*.md` remain stable base templates / companion context
- hook injection reads the per-instance identity files, not the project bases

If `identity/SOUL.md`, `identity/USER.md`, or `identity/ENVIRONMENT.md` are
missing, runtime may seed them from the corresponding `projects/quaid/` base
files, but it should never treat the project copies as the canonical writable
targets.

---

## 2. Config Layer Merge Chain

### The four search paths

`config.py`'s `_config_paths()` returns paths in **highest-priority-first**
order. The loader iterates them in **reverse** (lowest first) and deep-merges
each file that exists:

| Priority | Path | Purpose |
|---|---|---|
| 0 (highest) | `QUAID_HOME/<instance>/config/memory.json` | Per-instance overrides |
| 1 | `QUAID_HOME/shared/config/memory.json` | Machine-wide shared settings |
| 2 | `~/.quaid/memory-config.json` | User-level fallback (rarely used) |
| 3 (lowest) | `./memory-config.json` | Local cwd override (rarely used) |

The `_workspace_root()` used in `_config_paths()` resolves to
`get_adapter().instance_root()` via `lib/runtime_context.get_workspace_dir()`.

### Deep merge semantics

Layers are merged with `_deep_merge_dicts(base, override)`: nested dicts are
merged recursively; scalar and list values in higher-priority layers overwrite
lower-priority values entirely. camelCase keys are normalized to snake_case
after merging.

### Which settings belong at which layer

**Instance config** (`QUAID_HOME/<instance>/config/memory.json`):
- `adapter.type` — which adapter class to instantiate
- `models.llmProvider`, `models.deepReasoning`, `models.fastReasoning` — LLM routing
- `janitor.*`, `retrieval.*`, `capture.*`, `decay.*` — instance-specific tuning
- `plugins.slots.*` — which plugins are active
- `users.*`, `notifications.*`, `logging.*`

**Shared config** (`QUAID_HOME/shared/config/memory.json`):
- `ollama.url` — Ollama server URL (shared across all instances on the machine)
- `ollama.embeddingModel` — embedding model name
- `ollama.embeddingDim` — embedding vector dimension

**Why embeddings must be shared:** All instances on the same machine that
share a `QUAID_HOME` must use identical embedding models. Embedding vectors
are stored in `vec_nodes` and are model-specific — mixing models produces
incompatible vector spaces. Placing `ollama.*` in shared config enforces
consistency. Changing `embeddingModel` requires re-embedding all nodes (see
the warning block in `memory.json`).

### Config key format

Both camelCase and snake_case are accepted. The loader normalizes all keys to
snake_case via `_camel_to_snake()` during parsing. The `models` and `retrieval`
sections are validated against known-key sets; unknown keys emit a warning
(suppressed by `QUAID_QUIET=1`).

---

## 3. Top-Level Config Schema

The full config is a `MemoryConfig` dataclass. Key sections:

```json
{
  "adapter":       { "type": "claude-code" },
  "models":        { "llmProvider": "...", "deepReasoning": "...", ... },
  "ollama":        { "url": "...", "embeddingModel": "...", "embeddingDim": ... },
  "capture":       { "enabled": true, "inactivityTimeoutMinutes": 60, ... },
  "retrieval":     { "failHard": true, "autoInject": true, "useHyde": true, ... },
  "janitor":       { "enabled": true, "dryRun": false, ... },
  "decay":         { "enabled": true, "mode": "exponential", ... },
  "notifications": { "level": "normal", ... },
  "plugins":       { "strict": true, "slots": { "adapter": "...", "ingest": [], "dataStores": [] }, ... },
  "systems":       { "memory": true, "journal": true, "projects": true, "workspace": true },
  "docs":          { ... },
  "projects":      { ... },
  "users":         { "defaultOwner": "...", "identities": { ... } },
  "database":      { "path": "data/memory.db" },
  "logging":       { "level": "info", ... },
  "retrieval":     { ... }
}
```

### Database path resolution

`database.path` defaults to `"data/memory.db"` (relative). Resolution in
`lib/config.py`:

```python
p = Path(str(cfg.database.path)).expanduser()
return p if p.is_absolute() else _workspace_root() / p
```

`_workspace_root()` here calls `get_adapter().quaid_home()` (the QUAID_HOME
root). With the default relative path this produces `QUAID_HOME/data/memory.db`.

Each instance should set an explicit absolute path, or a path relative to
`QUAID_HOME` that is scoped to its instance directory. For example, two instances would use their respective instance roots:
`QUAID_HOME/claude-code/data/memory.db` and `QUAID_HOME/openclaw/data/memory.db`.
Separate databases mean instances do not share memory — cross-instance recall
requires the global project registry and canonical projects directory.

---

## 4. Config CLI

The `quaid config` command delegates to `config_cli.py` (Python) or
`config_cli.mjs` (Node.js if present; takes precedence).

### Target selection

All subcommands accept `--shared` / `--instance <id>` flags (mutually exclusive):

| Flag | Config file targeted |
|---|---|
| `--shared` | `QUAID_HOME/shared/config/memory.json` |
| `--instance <id>` | `QUAID_HOME/<id>/config/memory.json` |
| (neither, `QUAID_INSTANCE` set) | `QUAID_HOME/<QUAID_INSTANCE>/config/memory.json` |
| (neither, `QUAID_INSTANCE` unset) | `QUAID_HOME/shared/config/memory.json` |

### Command reference

```bash
# Show summary of effective config (current instance or shared if QUAID_INSTANCE unset)
quaid config show

# Show shared (machine-wide) config only
quaid config show --shared

# Show a specific instance's config
quaid config show --instance claude-code

# Print the path to the active config file
quaid config path
quaid config path --shared
quaid config path --instance openclaw

# Interactive editor (menu-driven)
quaid config edit
quaid config edit --shared
quaid config edit --instance claude-code

# Set a single key by dotted path (auto-coerces: int/float/bool/JSON/string)
quaid config set retrieval.fail_hard false
quaid config set models.llmProvider claude-code
quaid config set ollama.embeddingModel qwen3-embedding:8b --shared
quaid config set capture.inactivityTimeoutMinutes 30 --instance claude-code

# Store a long-lived auth token for the active adapter
quaid config set-auth <token>
```

### `config set` value coercion rules

Values are coerced by `parse_literal()`:

| Input | Result type |
|---|---|
| `true` / `false` | bool |
| Integer string, e.g. `30` | int |
| Float string, e.g. `0.85` | float |
| JSON object or array, e.g. `{"a":1}` | dict / list |
| `null` | None |
| Anything else | str |

### `config show` summary fields

The summary printed by `quaid config show` covers: LLM provider, deep/fast
reasoning models, embeddings model, notification level, fail_hard, identity
mode, strict privacy, core.parallel enabled plus distinct LLM/embedding worker
counts, idle timeout, and active plugin slots.

---

## 5. Instances CLI

```bash
# List all initialized instances under QUAID_HOME (marks current with *)
quaid instances list

# JSON output
quaid instances list --json
```

Output example:

```
Quaid home: /your/quaid/home

  openclaw
  claude-code  *
```

The `*` marker identifies the instance matching the current `QUAID_INSTANCE`
env var. If `QUAID_INSTANCE` is set but the directory does not yet have
`config/memory.json`, it appears as `(current — not yet initialised)`.

The JSON form (`--json`) returns:
```json
{"home": "/path/to/QUAID_HOME", "current": "claude-code", "instances": ["openclaw", "claude-code"]}
```

---

## 6. Shared State at QUAID_HOME Level

The following files and directories live at `QUAID_HOME/` root and are shared
across all instances under that home:

| Path | Purpose |
|---|---|
| `shared/config/memory.json` | Machine-wide shared config (embeddings, Ollama) |
| `projects/` | Canonical project directories for shared projects |
| `projects/project-registry.json` | Global project registry cross-instances (`lib/instance.shared_registry_path()`) |
| `.env` | API key fallback file (loaded when `failHard=false`) |

The canonical projects directory is returned by both
`lib/instance.shared_projects_dir()` and `QuaidAdapter.projects_dir()`. Projects
created by any instance on the machine are registered in the global registry
so they remain discoverable across instance boundaries.

---

## 7. Per-Instance State

Everything under `QUAID_HOME/<instance>/`:

| Path | Purpose |
|---|---|
| `config/memory.json` | Instance-specific config (highest-priority layer) |
| `data/memory.db` | SQLite memory database (nodes, edges, FTS, doc_registry, doc_chunks, vec_nodes, vec_doc_chunks) |
| `data/memory_archive.db` | Archive database for graduated/decayed memories |
| `data/extraction-signals/` | Signal files for async extraction daemon |
| `data/rolling-extraction/` | Rolling extraction staged state per session (raw facts/carryover awaiting final flush) |
| `data/cc-pending-notifications.jsonl` | Deferred notifications queue (Claude Code adapter) |
| `logs/` | Janitor stats, extraction logs, including rolling daemon telemetry under `logs/daemon/` |
| `identity/` | Quaid-managed identity files (SOUL.md, USER.md, ENVIRONMENT.md, *.snippets.md) |
| `journal/` | Journal entries awaiting distillation into core markdown |

The `data_dir()`, `config_dir()`, `logs_dir()`, and `identity_dir()` methods
on `QuaidAdapter` all derive from `instance_root()`.

---

## 8. Multi-Instance Setup Patterns

### Same QUAID_HOME, multiple instances

This is the standard setup on a single machine where OpenClaw and Claude Code
share the same memory workspace:

```
QUAID_HOME=/your/quaid/home

/your/quaid/home/
  shared/
    config/memory.json          ← shared embeddings/Ollama config
    project-registry.json       ← registry visible to all instances
    projects/                   ← shared project canonical dirs
  openclaw/
    config/memory.json          ← openclaw instance config
    data/memory.db              ← openclaw's private memory DB
    identity/                   ← SOUL.md, USER.md, etc.
  claude-code/
    config/memory.json          ← claude-code instance config
    data/memory.db              ← claude-code's private memory DB
    identity/
```

Each instance has its own isolated database. The shared project registry
ensures that `quaid docs search` and `quaid registry list` see the same
projects from any instance. Embeddings must use the same model (enforced by
`shared/config/memory.json`) so that cross-instance doc search produces
comparable vectors.

### Separate QUAID_HOME per adapter

Some adapters maintain a separate `QUAID_HOME` silo:

```
QUAID_HOME=/your/quaid/claudecode   (Claude Code adapter)
QUAID_HOME=/your/quaid/agents       (OpenClaw agent instances)
```

These silos do not share a project registry or databases. Projects are not
automatically visible across silos. Each silo maintains its own
`shared/config/memory.json` for embeddings consistency within that silo.

### Separate machines

Each machine has its own `QUAID_HOME`. There is no built-in sync mechanism.
Projects and memories are local to each machine.

---

## 9. Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `QUAID_HOME` | Root directory containing all instances | `~/quaid` |
| `QUAID_INSTANCE` | Active instance identifier | (required, no default) |
| `CLAWDBOT_WORKSPACE` | Alias for `QUAID_HOME` (backward compat) | — |
| `PYTHONPATH` | Set automatically by the `quaid` shell script to include `SCRIPT_DIR` | — |
| `QUAID_QUIET` | Suppress `[config]` log lines to stderr | unset |
| `QUAID_DISABLE_NOTIFICATIONS` | Suppress all notifications (except `force=True`) | unset |
| `QUAID_OWNER` | Override default owner ID for memory ownership | — |
| `MEMORY_DB_PATH` | Override `database.path` (testing) | — |
| `MEMORY_ARCHIVE_DB_PATH` | Override `database.archive_path` (testing) | — |
| `OLLAMA_URL` | Override `ollama.url` | — |

### QUAID_HOME / CLAWDBOT_WORKSPACE aliasing

The `quaid` shell script keeps these in sync:

```bash
if [[ -z "${QUAID_HOME:-}" && -n "${CLAWDBOT_WORKSPACE:-}" ]]; then
  export QUAID_HOME="$CLAWDBOT_WORKSPACE"
fi
if [[ -z "${CLAWDBOT_WORKSPACE:-}" && -n "${QUAID_HOME:-}" ]]; then
  export CLAWDBOT_WORKSPACE="$QUAID_HOME"
fi
```

Do NOT set `QUAID_HOME` or `QUAID_INSTANCE` globally in shell profile. Set
them per-invocation (adapter hooks, wrapper scripts) to avoid cross-instance
collisions.

---

## 10. Adapter Type Selection

The adapter type is read from `config/memory.json` at startup by
`lib/adapter._read_adapter_type_from_config()`. Accepted formats:

```json
{ "adapter": "openclaw" }
{ "adapter": { "type": "claude-code" } }
{ "adapter": { "kind": "standalone" } }
```

Search path for adapter config (priority order):

1. `QUAID_HOME/<QUAID_INSTANCE>/config/memory.json`
2. `QUAID_HOME/config/memory.json` (legacy flat layout)
3. `QUAID_WORKSPACE/config/memory.json` or `CLAWDBOT_WORKSPACE/config/memory.json`
4. `./config/memory.json` (cwd)
5. `./memory-config.json` (cwd)

The first file found that contains a non-empty `adapter.type` wins. Quaid
fails with a descriptive error if no adapter type can be resolved.

Built-in adapter types:

| Type string | Class | Use case |
|---|---|---|
| `standalone` | `StandaloneAdapter` | Direct API, no gateway |
| `claude-code` | `ClaudeCodeAdapter` | Claude Code sessions |
| `openclaw` | `OpenClawAdapter` | OpenClaw gateway |

---

## 11. Key Source Files

| File | Role |
|---|---|
| `lib/instance.py` | Instance identity: `instance_id()`, `quaid_home()`, `list_instances()`, `shared_*` paths |
| `lib/adapter.py` | Abstract `QuaidAdapter`, `StandaloneAdapter`, adapter singleton management |
| `lib/runtime_context.py` | Path/provider accessors that route through the active adapter |
| `lib/config.py` | `get_db_path()`, `get_ollama_url()`, `get_embedding_model()` — thin wrappers over config + adapter |
| `config.py` | `_config_paths()`, `_load_config_inner()`, `load_config()`, all `*Config` dataclasses |
| `config_cli.py` | `quaid config` subcommand implementation |
| `adaptors/claude_code/adapter.py` | `ClaudeCodeAdapter` — `quaid_home()`, `adapter_id()`, `get_sessions_dir()` |
| `quaid` (shell script) | CLI entrypoint; sets `PYTHONPATH`, syncs `QUAID_HOME`/`CLAWDBOT_WORKSPACE` |
