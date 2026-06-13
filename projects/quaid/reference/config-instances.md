# Quaid Config & Instance Reference

Technical reference for the Quaid instance model and configuration system.
Covers instance identity, the config merge chain, the config CLI, and the
split visible/hidden Quaid home layout.

For a generated key-by-key defaults table sourced directly from
`modules/quaid/config.py`, see [`config-reference.md`](./config-reference.md).

---

## 1. Instance Model

### What is a Quaid instance?

A Quaid instance is an isolated memory silo identified by a short string (the
instance ID). Hidden runtime state lives under
`QUAID_HOME/instances/<instance_id>/`. Visible identity and journal files live
under `QUAID_VISIBLE_HOME/instances/<instance_id>/`.

Two processes with the same `QUAID_HOME` and `QUAID_INSTANCE` share memory.
Two processes with different `QUAID_INSTANCE` values are fully isolated, even
on the same machine with the same `QUAID_HOME`.

### Instance ID rules

Defined in `lib/instance.py` (`validate_instance_id`):

- Must start with an alphanumeric character.
- May contain `[a-zA-Z0-9._-]`, max 64 characters.
- Cannot be a reserved name (see below).

For path-derived adapter instances, Quaid derives the slug from the resolved
project directory basename plus a short hash of the resolved full path. The
hash prevents similarly named paths such as `my_project`, `my-project`, and
`my.project` from sharing a memory silo, and the generated slug is capped so
the full prefixed instance ID still satisfies the 64-character limit.

Reserved names that may not be used as instance IDs:

```
shared  projects  config  data  logs  temp  tmp  quaid  plugins  lib  core
docs  assets  release  scripts  test  tests  benchmark  node_modules
```

### Platform prefix ownership contract

Each platform adapter owns a namespace prefix. The installer (`setup-quaid.mjs`)
enforces this via `_assertInstancePrefix(instanceId, platformPrefix)` before
writing any instance into a gateway config:

| Platform | Required prefix |
|----------|----------------|
| OpenClaw | `openclaw-` |
| Claude Code | `claude-code-` |
| Codex | `codex-` |

A gateway config writer must reject any instance whose ID does not start with
its platform's prefix. This prevents cross-platform contamination — for example,
a CC install cannot accidentally seed a `claude-code-*` instance name into the
OC gateway's env vars. Future platforms should follow the same pattern with
their own prefix namespace.

### Instance detection

`lib/instance.instance_id()` reads `QUAID_INSTANCE` from the environment.
It raises `InstanceError` if the variable is unset or invalid — there is no
implicit default. The CLI entrypoint and adapter hooks are responsible for
setting `QUAID_INSTANCE` before invoking any Quaid code.

```
QUAID_INSTANCE=claude-code quaid recall "query"
```

### What counts as an initialized instance?

`lib/instance.list_instances()` considers a directory under
`QUAID_HOME/instances/` an instance if and only if it contains `config.json`.
Directories that
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

### Instance paths

Hidden instance root:

```
QUAID_HOME/instances/<instance_id>/
```

Visible instance root:

```
QUAID_VISIBLE_HOME/instances/<instance_id>/
```

Computed by:

- `lib/instance.instance_root()` = hidden root
- `lib/instance.visible_instance_root()` = visible root

Per-instance paths derive from those roots:

```text
QUAID_HOME/instances/<instance_id>/
  config.json             instance-specific config
  data/memory.db          SQLite database
  data/memory_archive.db  archive database
  data/extraction-signals/ async extraction signal files
  logs/                   janitor stats, extraction logs

QUAID_VISIBLE_HOME/instances/<instance_id>/
  SOUL.md                 Quaid-managed identity file
  USER.md
  ENVIRONMENT.md
  *.snippets.md
  journal/                journal entries for distillation
```

### Identity seed vs. live identity

`projects/quaid/SOUL.md`, `projects/quaid/USER.md`, and
`projects/quaid/ENVIRONMENT.md` are base templates shipped with Quaid. They are
seed material, not the live writable copies.

For a real instance, the writable files live under:

```text
QUAID_VISIBLE_HOME/instances/<instance_id>/
  SOUL.md
  USER.md
  ENVIRONMENT.md
```

At install or workspace bootstrap time, the base templates are copied into the
visible instance directory. After that:

- janitor distillation and snippet compaction update the visible instance
  identity files
- `projects/quaid/*.md` remain stable base templates / companion context
- hook injection reads the per-instance identity files, not the project bases

If `SOUL.md`, `USER.md`, or `ENVIRONMENT.md` are
missing, runtime may seed them from the corresponding `projects/quaid/` base
files, but it should never treat the project copies as the canonical writable
targets.

---

## 2. Config Layer Merge Chain

### The three search paths

`config.py`'s `_config_paths()` returns paths in **highest-priority-first**
order. The loader iterates them in **reverse** (lowest first) and deep-merges
each file that exists:

| Priority | Path | Purpose |
|---|---|---|
| 0 (highest) | `QUAID_HOME/instances/<instance>/config.json` | Per-instance overrides |
| 1 | `QUAID_HOME/shared/config/<platform>/config.json` | Platform-specific shared settings |
| 2 (lowest) | `QUAID_HOME/shared/config/global/config.json` | Machine-wide global shared settings |

The `_workspace_root()` used in `_config_paths()` resolves to
`get_adapter().instance_root()` via `lib/runtime_context.get_workspace_dir()`.

### Deep merge semantics

Layers are merged with `_deep_merge_dicts(base, override)`: nested dicts are
merged recursively; scalar and list values in higher-priority layers overwrite
lower-priority values entirely. camelCase keys are normalized to snake_case
after merging.

### Which settings belong at which layer

**Instance config** (`QUAID_HOME/instances/<instance>/config.json`):
- `adapter.type` — which adapter class to instantiate
- `models.llmProvider`, `models.deepReasoning`, `models.fastReasoning` — LLM routing
- `janitor.*`, `retrieval.*`, `capture.*`, `decay.*` — instance-specific tuning
- `plugins.slots.*` — which plugins are active
- `users.*`, `notifications.*`, `logging.*`

**Platform shared config** (`QUAID_HOME/shared/config/<platform>/config.json`):
- Platform-specific overrides that apply to all instances of a given adapter type.
- Use to set provider or model defaults that differ per platform (e.g. Claude Code vs Codex).

**Global shared config** (`QUAID_HOME/shared/config/global/config.json`):
- `ollama.url` — Ollama server URL (shared across all instances on the machine)
- `ollama.embeddingModel` — embedding model name
- `ollama.embeddingDim` — embedding vector dimension

**Why embeddings must be shared:** All instances on the same machine that
share a `QUAID_HOME` must use identical embedding models. Embedding vectors
are stored in `vec_nodes` and are model-specific — mixing models produces
incompatible vector spaces. Placing `ollama.*` in global shared config enforces
consistency. Changing `embeddingModel` requires re-embedding all nodes (see
the warning block in `config.json`).

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
  "logging":       { "level": "info", ... }
}
```

### Database path resolution

`database.path` defaults to `"data/memory.db"` (relative). Resolution in
`lib/config.py`:

```python
p = Path(str(cfg.database.path)).expanduser()
return p if p.is_absolute() else _workspace_root() / p
```

`_workspace_root()` calls `get_adapter().instance_root()` (the hidden
per-instance silo root, e.g. `QUAID_HOME/instances/claude-code-main/`). With
the default relative path this produces
`QUAID_HOME/instances/<instance_id>/data/memory.db`.

Each instance should set an explicit absolute path, or a path relative to its
instance directory. For example, two instances would use their respective instance roots:
`QUAID_HOME/instances/claude-code-main/data/memory.db` and
`QUAID_HOME/instances/openclaw-main/data/memory.db`.
Separate databases mean instances do not share memory — cross-instance recall
requires the global project registry and canonical projects directory.

---

## 4. Config files

During prerelease, `quaid config` is deprecated except for `config path` and the
compatibility `config set-auth` route. Edit layered JSON files directly, and use
`quaid auth refresh` for credentials. The older `config_cli.py` / `config_cli.mjs`
helpers still exist in the tree for post-launch CLI rebuild work, but active
operator docs should not rely on `quaid config show` or `quaid config edit`.

### Target selection

`quaid config path` accepts `--shared` / `--instance <id>` flags (mutually exclusive).
For direct JSON edits, use the same target mapping:

| Flag | Config file targeted |
|---|---|
| `--shared` | `QUAID_HOME/shared/config/global/config.json` |
| `--instance <id>` | `QUAID_HOME/instances/<id>/config.json` |
| (neither, `QUAID_INSTANCE` set) | `QUAID_HOME/instances/<QUAID_INSTANCE>/config.json` |
| (neither, `QUAID_INSTANCE` unset) | `QUAID_HOME/shared/config/global/config.json` |

### Active command reference

```bash
# Print the path to the active config file
quaid config path
quaid config path --shared
quaid config path --instance openclaw

# Set a single key by editing JSON directly (recommended)
python3 - <<'PY'
import json
from pathlib import Path
p = Path.home() / ".quaid" / "instances" / "claude-code" / "config.json"
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault("capture", {})["inactivityTimeoutMinutes"] = 30
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2))
print(f"updated {p}")
PY

# Store/refresh a shared provider credential
quaid auth refresh --kind anthropic_oauth <token>
```

### Direct JSON edit notes

- Edit only the layer you intend to override (instance > platform > global).
- Nested objects should be merged, not replaced wholesale.
- Keep defaults in global/platform layers; avoid inlining full default trees into instance config.

### Deprecated command notes

- `quaid config show` and `quaid config edit` currently fail with a deprecation
  error and instructions for direct JSON edits.
- `quaid config set <key> <value>` is deprecated; use direct JSON edits.
- `quaid config set-auth <token>` remains as a compatibility route, but
  `quaid auth refresh <token>` is the preferred credential command.

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
`config.json`, it appears as `(current — not yet initialised)`.

The JSON form (`--json`) returns:
```json
{"home": "/path/to/QUAID_HOME", "current": "claude-code", "instances": ["openclaw", "claude-code"]}
```

---

## 6. Shared State at Root Level

The following files and directories are shared across all instances:

| Path | Purpose |
|---|---|
| `QUAID_HOME/shared/config/global/config.json` | Machine-wide shared config (embeddings, Ollama) |
| `QUAID_HOME/shared/config/<platform>/config.json` | Platform-scoped shared overrides |
| `QUAID_VISIBLE_HOME/projects/` | Canonical project directories for shared projects |
| `QUAID_HOME/project-registry.json` | Global project registry cross-instances (`lib/instance.shared_registry_path()`) |
| `QUAID_HOME/.env` | API key fallback file (loaded when `failHard=false`) |

The canonical projects directory is returned by both
`lib/instance.shared_projects_dir()` and `QuaidAdapter.projects_dir()`. Projects
created by any instance on the machine are registered in the global registry
so they remain discoverable across instance boundaries.

---

## 7. Per-Instance State

Hidden instance state (`QUAID_HOME/instances/<instance>/`):

| Path | Purpose |
|---|---|
| `config.json` | Instance-specific config (highest-priority layer) |
| `data/memory.db` | SQLite memory database (nodes, edges, FTS, doc_registry, doc_chunks, vec_nodes, vec_doc_chunks) |
| `data/memory_archive.db` | Archive database for graduated/decayed memories |
| `data/extraction-signals/` | Signal files for async extraction daemon |
| `data/rolling-extraction/` | Rolling extraction staged state per session (raw facts/carryover awaiting final flush) |
| `data/cc-pending-notifications.jsonl` | Deferred notifications queue (Claude Code adapter) |
| `logs/` | Janitor stats, extraction logs, including rolling daemon telemetry under `logs/daemon/` |

Visible instance state (`QUAID_VISIBLE_HOME/instances/<instance>/`):

| Path | Purpose |
|---|---|
| `SOUL.md` / `USER.md` / `ENVIRONMENT.md` | Quaid-managed identity files |
| `*.snippets.md` | Pending snippet staging files |
| `journal/` | Journal entries awaiting distillation into core markdown |

The `data_dir()`, `config_dir()`, and `logs_dir()` methods derive from
`instance_root()` (hidden). `identity_dir()` and `journal_dir()` derive from
`visible_instance_root()`.

---

## 8. Multi-Instance Setup Patterns

### Same QUAID_HOME, multiple instances

This is the standard setup on a single machine where OpenClaw, Claude Code, and Codex
share the same memory workspace:

```
QUAID_HOME=/your/.quaid
QUAID_VISIBLE_HOME=/your/quaid

/your/.quaid/
  shared/
    config/global/config.json   ← shared embeddings/Ollama config
  instances/
    openclaw-main/
      config.json               ← openclaw instance config
      data/memory.db            ← openclaw's private memory DB
    claude-code-main/
      config.json               ← claude-code instance config
      data/memory.db            ← claude-code's private memory DB

/your/quaid/
  projects/
    <project-name>/             ← shared project canonical dirs
  instances/
    openclaw-main/
      SOUL.md
      USER.md
      ENVIRONMENT.md
      journal/
    claude-code-main/
      SOUL.md
      USER.md
      ENVIRONMENT.md
      journal/
```

Each instance has its own isolated database. The shared project registry
ensures that `quaid recall` (docs store) and `quaid registry list` see the same
projects from any instance. Embeddings must use the same model (enforced by
`shared/config/global/config.json`) so that cross-instance doc search produces
comparable vectors.

### Separate QUAID_HOME per adapter

Some deployments maintain a separate hidden `QUAID_HOME` silo:

```
QUAID_HOME=/your/.quaid-claudecode   (Claude Code adapter)
QUAID_HOME=/your/.quaid-agents       (OpenClaw agent instances)
```

These silos do not share a project registry or databases. Projects are not
automatically visible across silos. Each silo maintains its own
`shared/config/global/config.json` for embeddings consistency within that silo.

### Separate machines

Each machine has its own `QUAID_HOME`. There is no built-in sync mechanism.
Projects and memories are local to each machine.

---

## 9. Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `QUAID_HOME` | Hidden root directory containing runtime state and all instances | `~/.quaid` |
| `QUAID_VISIBLE_HOME` | Visible root directory containing instance markdown and projects | `~/quaid` |
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

The adapter type is read from `config.json` at startup by
`lib/adapter._read_adapter_type_from_config()`. Accepted formats:

```json
{ "adapter": "openclaw" }
{ "adapter": { "type": "claude-code" } }
{ "adapter": { "kind": "standalone" } }
```

Search path for adapter config (priority order):

1. `QUAID_HOME/instances/<QUAID_INSTANCE>/config.json`
2. `QUAID_HOME/instances/<adapter>-<path-slug>-<path-hash>/config.json` when `QUAID_INSTANCE` is unset and a supported project-dir env var such as `CLAUDE_PROJECT_DIR` is set
3. `QUAID_HOME/config/config.json` (legacy flat layout)
4. `QUAID_WORKSPACE/config/config.json` or `CLAWDBOT_WORKSPACE/config/config.json` (legacy compatibility)
5. `./config/config.json` (cwd legacy fallback)
6. `./memory-config.json` (cwd legacy fallback)

The first file found that contains a non-empty `adapter.type` wins. Quaid
fails with a descriptive error if no adapter type can be resolved.

Built-in adapter types:

| Type string | Class | Use case |
|---|---|---|
| `standalone` | `StandaloneAdapter` | Direct API, no gateway |
| `claude-code` | `ClaudeCodeAdapter` | Claude Code sessions |
| `openclaw` | `OpenClawAdapter` | OpenClaw gateway |
| `codex` | `CodexAdapter` | Codex sessions |

---

## 11. Key Source Files

| File | Role |
|---|---|
| `lib/instance.py` | Instance identity: `instance_id()`, `quaid_home()`, `list_instances()`, `shared_*` paths |
| `lib/adapter.py` | Abstract `QuaidAdapter`, `StandaloneAdapter`, adapter singleton management |
| `lib/runtime_context.py` | Path/provider accessors that route through the active adapter |
| `lib/config.py` | `get_db_path()`, `get_ollama_url()`, `get_embedding_model()` — thin wrappers over config + adapter |
| `config.py` | `_config_paths()`, `_load_config_inner()`, `load_config()`, all `*Config` dataclasses |
| `config_cli.py` | Deprecated config helper retained for post-launch CLI rebuild |
| `adaptors/claude_code/adapter.py` | `ClaudeCodeAdapter` — `quaid_home()`, `adapter_id()`, `get_sessions_dir()` |
| `quaid` (shell script) | CLI entrypoint; sets `PYTHONPATH`, syncs `QUAID_HOME`/`CLAWDBOT_WORKSPACE` |
