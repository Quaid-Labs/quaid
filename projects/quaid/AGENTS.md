# Quaid — Operating Guide

Quaid is an active knowledge layer. It captures facts and project context from conversations, recalls them on demand, and maintains knowledge health nightly.

For full CLI reference see `TOOLS.md`. For doc index and architecture see `PROJECT.md`.

---

## Tool Access

You have a **bash tool**. All `quaid` CLI commands must be run through it.

`QUAID_HOME` and `QUAID_INSTANCE` are set in your shell environment by the gateway — do not override them.

**Binary path** (quaid is not on PATH by default):
```
~/.openclaw/extensions/quaid/quaid <command>
```

**Examples:**
```bash
~/.openclaw/extensions/quaid/quaid recall "query"
~/.openclaw/extensions/quaid/quaid store "fact"
~/.openclaw/extensions/quaid/quaid project create <name> --source-root <path>
~/.openclaw/extensions/quaid/quaid project list
~/.openclaw/extensions/quaid/quaid project show <name>
~/.openclaw/extensions/quaid/quaid project delete <name>
~/.openclaw/extensions/quaid/quaid stats
~/.openclaw/extensions/quaid/quaid janitor --task all --dry-run
```

---

## How Memory Works

```
Conversation → compaction/reset → Opus extracts facts + edges → stored in DB
Nightly janitor (4 AM default) → review → dedup → decay → graduate to active
```

- **Extraction priority:** user facts first, agent-action memories second, technical/project state third. Agent extraction must never displace user-memory coverage.
- **Edges** are created at extraction time and linked to source facts.
- **Janitor** runs nightly: reviews pending, merges duplicates (Ebbinghaus decay), monitors core files.
- **Soul snippets** (fast path) — bullet observations distilled into SOUL.md, USER.md, MEMORY.md by janitor.
- **Journal** (slow path) — diary paragraphs distilled by Opus monthly.

---

## Operating Rules

**Retrieval discipline**
- Always use memory/project tools before claiming missing context.
- Treat auto-injected memories as hints — verify concrete claims (names, dates, versions) with explicit `quaid recall`.
- Use `quaid docs search` for codebase/architecture questions.
- Use `quaid recall --docs` for a single pass across both memories and docs.

**Interrupt policy**
- Complete the current task before starting a new one.
- Switch immediately only on explicit interruption (`wait`, `stop`, `cancel`).

**Fail-hard**
- Controlled by `retrieval.fail_hard` in `config/memory.json`.
- When `true`: never degrade silently — surface the error.
- When `false`: degrade with loud warnings/diagnostics.

**Project and file placement**

ALWAYS use the quaid CLI to create projects — never create files directly. A project is a registry entry, not a directory:
```bash
~/.openclaw/extensions/quaid/quaid registry create-project <name> --source-roots <path>
```

**Create a project whenever ANY of these are true:**
- User explicitly asks to create, start, initialize, or set up a project
- User asks you to "build", "make", "write", or "work on" something that will span multiple files or sessions (e.g. "build me a todo app", "let's start a new service", "help me design X")
- You are about to write code to a new directory that isn't part of an existing tracked project
- You are about to create any novel file (script, config, doc) that isn't clearly one-off scratch work
- User references a codebase, repo, or directory they're working on that has no existing project entry
- A task will produce artifacts that should persist and be recalled in future sessions

**Decision tree — for each file/task, pick the first match:**
1. Does an existing tracked project own this path/topic? → Place it there.
2. Is this clearly ephemeral? (quick one-liner, throwaway test, single calculation) → Use `scratch/$QUAID_INSTANCE/` and tell the user it's untracked.
3. Everything else → Create a project first, then proceed.

**Scratch is a tracked project in shared/projects**, named `scratch--{instance}`:
- Project name: `scratch--$QUAID_INSTANCE`  (e.g. `scratch--openclaw-main`)
- Path: `$QUAID_HOME/shared/projects/scratch--$QUAID_INSTANCE/`
- Use `quaid project show scratch--$QUAID_INSTANCE` to inspect it
- All standard project tooling works: doc health, PROJECT.log, `quaid project list`

When using scratch/temp, always tell the user the path is untracked and offer to register it as a project if the work becomes durable.

Place new files in an existing tracked project when possible. If a temp/scratch artifact becomes durable, move it into a tracked project.

**Cross-instance**
- When OC and CC share a machine, both use the same `QUAID_HOME`.
- Use `quaid project link/unlink` for cross-instance project participation.
- `quaid project delete` is destructive — prefer `unlink` if you only want to leave the project.

---

## Core Files (always loaded)

| File | Role |
|------|------|
| `AGENTS.md` | This guide |
| `TOOLS.md` | CLI reference |
| `PROJECT.md` | Doc index and architecture map |
| `SOUL.md` | Quaid's reflective identity |
| `USER.md` | User understanding and patterns |
| `MEMORY.md` | Shared-moment and world-understanding context |
