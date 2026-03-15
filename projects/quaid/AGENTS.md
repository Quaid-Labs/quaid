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
- When asked to "create a project", ALWAYS use the quaid CLI — never create files directly. Projects are registry entries, not directories:
  ```bash
  ~/.openclaw/extensions/quaid/quaid registry create-project <name> --source-roots <path>
  ```
- Place new files in an existing tracked project when possible.
- If no project fits, register one first with the command above, then place files there.
- Temp/scratch files go in `temp/` or `scratch/` — tell the user these are untracked.
- Move durable temp files into a tracked project.

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
