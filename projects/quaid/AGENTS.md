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

**Project registration is a prerequisite for sustained work.** Before taking any action on a task — including delegating to a sub-agent — determine whether the work is durable. If the user's intent is to build, develop, or produce something that will persist, create a project first. The current file location is irrelevant; intent is what matters.

**Decision tree — pick the first match:**
1. Does an existing tracked project own this work? → Place it there.
2. Is this clearly a one-off or throwaway? → Use `misc--$QUAID_INSTANCE`.
3. Everything else → Create a project, then proceed.

**Misc is a tracked project in `shared/projects/`**, named `misc--$QUAID_INSTANCE` (e.g. `misc--openclaw-main`).
It lives at `$QUAID_HOME/shared/projects/misc--$QUAID_INSTANCE/` and appears in `quaid project list`.
Always tell the user when writing to misc, and offer to promote to a real project if the work becomes durable.

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
