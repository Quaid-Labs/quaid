# Directory Standard

Canonical file layout for Quaid installations. All code must reference this
document as the single source of truth for where files live.

**Status**: Draft — established 2026-03-11

---

## Overview

Quaid organizes data into five layers:

| # | Layer | What | Who writes | Who reads |
|---|-------|------|-----------|-----------|
| 1 | Base context | Platform's native context files (CLAUDE.md, OC's SOUL/USER/ENVIRONMENT) | Human + LLM | Platform + Quaid (slim only) |
| 2 | Generated identity | Quaid's per-instance identity (snippets, journals) | Quaid janitor | Platform (via injection) |
| 3 | Project docs | Documentation indexed into docsdb/RAG | LLM + human | Quaid retrieval |
| 4 | Project context | Injected markdown (TOOLS.md, AGENTS.md) | Quaid janitor | Platform (via bootstrap/hooks) |
| 5 | Subject matter | The actual project files (code, scripts, plans) | Human + LLM | Shadow git tracker |

---

## Layer 1: Base Context Files

**What**: The platform's native personality/instruction files. These exist
independently of Quaid — the platform created them or the user wrote them.

**Quaid's role**: Slim only. Quaid monitors these for bloat and trims them
during janitor runs. Quaid does NOT create, move, or manage these files.

**Adapter contract**: Each adapter implements `get_base_context_files()` which
returns paths to the platform's context files so the janitor knows where they
are.

| Adapter | Files | Location |
|---------|-------|----------|
| Claude Code | `CLAUDE.md` | Project working directory (user's cwd) |
| OpenClaw | `SOUL.md`, `USER.md`, `ENVIRONMENT.md`, `IDENTITY.md`, `HEARTBEAT.md`, `TODO.md` | OC workspace root (`~/.openclaw/workspace/`) |
| Codex | `AGENTS.md` | Project working directory (user's cwd) |
| Future adapter | Whatever the platform uses | Adapter declares it |

**Rules**:
- Quaid never moves these files.
- Quaid never creates these files (the platform or user does).
- Quaid only edits them for slimming (removing outdated/bloated content).
- The janitor gets the file list from the adapter, not from config.

---

## Layer 2: Generated Identity

**What**: Quaid's own per-instance identity output — personality snippets,
journal distillations, memory summaries. This is what Quaid "learns" about
the user and itself over time.

**Location**: Derived via `adapter.identity_dir()`. Identity files now live in
the visible root under `QUAID_VISIBLE_HOME/instances/<instance>/`. The adapter
provides its `adapter_id` string; core computes the path. (`quaid_identity_dir()`
is a deprecated utility alias — prefer `adapter.identity_dir()` in new code.)

| Adapter | adapter_id | Identity dir |
|---------|-----------|-------------|
| Claude Code | `claude-code` | `QUAID_VISIBLE_HOME/instances/claude-code-*/` |
| OpenClaw | `openclaw` | `QUAID_VISIBLE_HOME/instances/openclaw-*/` |
| Codex | `codex` | `QUAID_VISIBLE_HOME/instances/codex-*/` |
| Standalone | `standalone` | `QUAID_VISIBLE_HOME/instances/standalone-*/` |

**Contents**:
```
SOUL.md                   # Generated personality traits
SOUL.snippets.md          # Pending personality snippets
USER.md                   # Generated user profile
USER.snippets.md          # Pending user snippets
ENVIRONMENT.md            # Learned behaviors and environment context
ENVIRONMENT.snippets.md   # Pending environment snippets
journal/                  # Journal markdown
```

**Rules**:
- Each adapter instance gets its own identity silo.
- The janitor writes here, not to `QUAID_HOME` root.
- Identity is NOT shared across adapters. If the same person uses CC and OC,
  each adapter builds its own identity from its own conversations.
- Identity files are injected into the LLM context by the adapter's hooks
  (CC: `hooks.py` → `.claude/rules/`, OC: ExtraBootstrapFiles).

**Why per-instance**: Different platforms have different conversation styles,
different context windows, different capabilities. The identity Quaid builds
should be tuned to the platform it's operating on. A shared identity would
be a lowest-common-denominator compromise.

---

## Layer 3: Project Docs

**What**: Documentation that gets indexed into docsdb for RAG retrieval.
Architecture docs, API references, meeting notes, specs — anything the LLM
might need to recall during conversations.

**Location**: `QUAID_VISIBLE_HOME/projects/<name>/docs/`

```
projects/
  japan-trip/
    PROJECT.md          # Project metadata (auto-generated)
    docs/
      itinerary.md
      hotel-research.md
      budget.md
  myapp/
    PROJECT.md
    docs/
      architecture.md
      api-reference.md
```

**Rules**:
- Quaid's docsdb indexes these directly from the visible projects root.
- The LLM creates and manages these files via Quaid's tools.
- The human can also edit these directly (they live in a visible location).
- Project docs are shared across adapters — if CC and OC both work on
  "myapp," they share the same docs via docsdb.

---

## Layer 4: Project Context (TOOLS.md, AGENTS.md)

**What**: Injected markdown that becomes part of the LLM's system context
every session. Tool references, behavioral rules, project-specific
instructions.

**Canonical location**: `QUAID_VISIBLE_HOME/projects/<name>/TOOLS.md` and
`QUAID_VISIBLE_HOME/projects/<name>/AGENTS.md`

**Injection path varies by adapter**:

| Adapter | How injected | Sync needed? |
|---------|-------------|-------------|
| Claude Code | `hooks.py` reads directly from `QUAID_VISIBLE_HOME/projects/` | No — reads from canonical location |
| OpenClaw | ExtraBootstrapFiles hook reads from OC workspace | **Yes** — files must be copied inside workspace boundary |

### OC Sync Engine

OpenClaw's `ExtraBootstrapFiles` hook enforces a workspace boundary —
files must resolve (via `realpath`) inside `~/.openclaw/workspace/`.
Quaid's canonical project location is outside this boundary.

**Solution**: A sync engine in core copies bootstrap-eligible files from the
canonical location to the OC workspace. The adapter requests this service.

```
Canonical:  QUAID_VISIBLE_HOME/projects/myapp/TOOLS.md
     Sync:  ~/.openclaw/workspace/plugins/quaid/projects/myapp/TOOLS.md  (copy)
```

See [Project System Spec](../projects/quaid/reference/projects-reference.md) for sync engine details.

**Rules**:
- Canonical location is always `QUAID_VISIBLE_HOME/projects/<name>/`.
- OC workspace copies are read-only shadows. Never edit there.
- A `README.md` in the OC sync target explains where canonical files live.
- CC never needs sync — it reads directly.
- The sync engine lives in core (reusable) and is triggered by the daemon.

---

## Layer 5: Subject Matter

**What**: The actual project the human is working on — source code, scripts,
documents, creative work. This is the user's stuff, not Quaid's.

**Location**: Wherever the human puts it. `~/code/myapp/`, `~/Documents/Japan
Trip/`, a git repo, a shared drive — Quaid doesn't dictate this.

**Quaid's role**: Track changes via shadow git (see
[Project System Spec](../projects/quaid/reference/projects-reference.md#4-shadow-git)). Index
relevant files into docsdb. Never move, rename, or restructure the user's
files.

**Registration**: The LLM registers a source root with a project via CLI or API:

```bash
quaid project create myapp --source-root ./myapp
quaid project update myapp --source-root ./myapp  # add later
```

**Rules**:
- Quaid never creates files in the user's source directory.
- Quaid never creates `.git` or unrelated dotfiles in user space.
- Shadow git metadata lives in `QUAID_HOME/.git-tracking/<project>/`.
- The human and LLM edit files however they want — Quaid detects changes
  on the next daemon tick via shadow git diff.

---

## Full Directory Tree

```
QUAID_HOME/                              # e.g. ~/.quaid/
  shared/
    config/
      global/config.json                 # Hidden machine-wide shared config
      <platform>/config.json             # Hidden platform-shared config
  instances/
    <instance>/
      config.json                        # Hidden per-instance config
      data/                              # Hidden DB + daemon state
      logs/                              # Hidden logs
  runtime/                               # Hidden shared runtime state
  adaptors/                              # Hidden adapter manifests
  plugins/                               # Hidden installed plugin/runtime
  modules/                               # Hidden runtime code
  .git-tracking/
    <project>/                           # Layer 5: shadow git metadata
      HEAD
      objects/
      refs/
      info/
        exclude                          # LLM-managed ignore patterns

QUAID_VISIBLE_HOME/                      # e.g. ~/quaid/
  instances/
    <instance>/
      SOUL.md                            # Layer 2: generated identity
      USER.md
      ENVIRONMENT.md
      *.snippets.md
      journal/
  projects/
    <name>/
      PROJECT.md                         # Project metadata
      TOOLS.md                           # Layer 4: project context
      AGENTS.md                          # Layer 4: project context
      docs/                              # Layer 3: project docs
        *.md
```

---

## Adapter Workspace Views

### Claude Code

CC has no workspace boundary. It reads visible project and identity files
directly from Quaid's split roots.

```
QUAID_VISIBLE_HOME/
  instances/<instance>/    → hooks.py reads identity from here
  projects/<name>/         → hooks.py reads TOOLS.md/AGENTS.md from here
  projects/<name>/docs/    → docsdb indexes from here
```

Base context file: `CLAUDE.md` lives in the user's project cwd. The CC
adapter returns this path from `get_base_context_files()`.

### OpenClaw

OC enforces a workspace boundary (`~/.openclaw/workspace/`). Bootstrap files
must be inside this boundary.

```
~/.openclaw/workspace/
  SOUL.md, USER.md, ENVIRONMENT.md   → Layer 1: base context (OC-native)
  plugins/quaid/
    projects/<name>/
      TOOLS.md                   → Layer 4: synced copy (read-only)
      AGENTS.md                  → synced copy (read-only)
      README.md                  → "Canonical files at QUAID_VISIBLE_HOME/projects/<name>/"

QUAID_VISIBLE_HOME/
  instances/<instance>/          → Layer 2: generated identity
  projects/<name>/               → Layer 3+4: canonical project files
```

The sync engine copies Layer 4 files from `QUAID_VISIBLE_HOME` into the OC workspace
on each daemon tick.

---

## Migration Notes

### From current layout (pre-standard)

Current state:
- Hidden runtime state lives under `QUAID_HOME`
- Visible identity and journals live under `QUAID_VISIBLE_HOME/instances/<instance>/`
- Canonical project docs live under `QUAID_VISIBLE_HOME/projects/`

Migration steps:
1. Keep config/data/logs in hidden `QUAID_HOME/instances/<instance>/`
2. Keep identity/snippets/journal in visible `QUAID_VISIBLE_HOME/instances/<instance>/`
3. Update janitor to use `adapter.identity_dir()` / `adapter.journal_dir()`
4. Update hooks to read from visible identity paths
5. Keep project docs canonical in `QUAID_VISIBLE_HOME/projects/`

### Future adapters

Any new adapter must:
1. Implement `identity_dir()` returning its silo path
2. Implement `get_base_context_files()` returning platform context file paths
3. Declare sync requirements in its plugin manifest
4. Register with the project registry on install

---

## References

- [Design Principles](DESIGN-PRINCIPLES.md) — why this layout exists
- [Project System Spec](../projects/quaid/reference/projects-reference.md) — shadow git, sync engine
- [Plugin System](PLUGIN-SYSTEM.md) — adapter contracts
- [Multi-User Memory Spec](MULTI-USER-MEMORY-SPEC.md) — future identity splitting
