# Knowledge Layer — Agent Operating Guide

## Operator Interrupt Policy

- If the operator sends a new request while you are in the middle of an active task, do not immediately switch tasks.
- Queue the new request and complete the current task first.
- Only switch immediately when the operator uses explicit interruption language (for example: `wait`).

## Fail-Hard Rule

- Fail-hard control is config-driven through `retrieval.failHard` / `retrieval.fail_hard` in `config/memory.json`.
- Do not implement fail-hard switching via env var toggles in product flows.
- When fail-hard is `true`, never degrade/fallback silently.
- When fail-hard is `false`, any fallback must emit loud warnings/diagnostics.

## Project Navigation (Start Here)

### 1) Core Markdown (always loaded context)
- `AGENTS.md`: this operational guide.
- `TOOLS.md`: tool behavior and parameter reference.
- `PROJECT.md`: architecture map and doc index.
- `MEMORY.md`, `USER.md`, `SOUL.md`, `CONSTITUTION.md`: long-lived behavioral memory/context.

### 2) Project docs (`projects/quaid/`)
- `operations/`: runbooks, release/checkpoint/testing procedures.
- `reference/`: deep technical docs and implementation details.
- Keep details here when they are too large/noisy for always-loaded files.

### 3) Runtime code map (`modules/quaid/`)
- `adaptors/openclaw/adapter.ts`: tool registration and runtime hook wiring.
- `adaptors/openclaw/adapter.py`: Python adaptor bridge and lifecycle wiring.
- `core/interface/mcp_server.py`: MCP tool surface.
- `datastore/memorydb/`: memory graph, schema, janitor maintenance.
- `datastore/docsdb/`: docs registry/search/update pipelines.
- `core/lifecycle/`: janitor/workspace lifecycle orchestration.

## Best Practices

### Documentation Discipline
- Keep `AGENTS.md` focused on navigation and operations.
- Keep `TOOLS.md` focused on tool contracts and parameter maps.
- Keep `PROJECT.md` as the “where things live” architecture index.
- Keep the `TOOLS.md` domain list aligned with `config/memory.json -> retrieval.domains`.
- When behavior changes, update docs in the same commit.

### Retrieval Discipline
- Use memory/project tools before claiming missing context.
- Treat auto-injected memory as hints; verify concrete claims via explicit recall.
- Prefer scoped retrieval (project/domain/date) when available to reduce noise.
- Use session transcript retrieval only when conversation continuity is required.

### Fail-Hard Discipline
- Fail-hard behavior is config-driven (`retrieval.failHard` / `retrieval.fail_hard`).
- Do not hide errors in strict mode.
- In non-strict mode, degraded behavior must log clearly.

### Change Discipline
- Make changes in `~/quaid/dev` only.
- Treat `~/quaid/benchmark-checkpoint` as a cut artifact, not a dev workspace.
- Keep benchmark/checkpoint operations in their own runbooks under `operations/`.
- Keep `npm run check:boundaries` green; dynamic imports (`__import__`, `importlib.import_module`) are boundary-checked the same as static imports.

### Plugin Contract Discipline
- Plugin runtime surfaces are contract-owned and manifest-declared.
- Executable contract hooks: `init`, `config`, `status`, `dashboard`, `maintenance`, `tool_runtime`, `health`.
- Declared contract surfaces: `tools`, `api`, `events`, `ingest_triggers`, `auth_requirements`, `migrations`, `notifications`.
- In strict mode (`config/memory.json -> plugins.strict=true`), undeclared tool/event registrations must fail fast.
- Datastore-specific behavior (for example domains/schema sync) belongs in datastore plugin contracts, not core one-offs.

## Memory Lifecycle

```
Conversation -> /compact or /reset -> Opus extracts facts+edges -> Store in DB

Nightly janitor (independent scheduler) -> Review -> Dedup -> Decay -> Graduate to 'active'
```

- **Extraction:** Opus analyzes transcript at compaction, extracts personal facts with relationships
- **Edges:** Created at extraction time, linked to source facts
- **Janitor:** Runs 4:30 AM — reviews pending facts, merges duplicates, decays stale memories (Ebbinghaus), monitors core files

**Extraction priority invariant:**
- User facts are first priority.
- Agent-action memories are second priority.
- Technical/project-state facts are third priority.
- Agent/technical extraction must never reduce baseline user-memory coverage.

## Retrieval Notes

- For exact tool parameter maps and usage patterns, refer to `projects/quaid/TOOLS.md`.
- Keep this file focused on operating rules and project navigation.

## Dual Extraction: Snippets + Journal

Both systems extract entries at compaction/reset time:

- **Soul Snippets (fast path)** — Bullet-point observations to `*.snippets.md`. Nightly janitor FOLDs/REWRITEs/DISCARDs into core files (default: SOUL.md, USER.md, MEMORY.md; AGENTS.md optional via config).
- **Journal (slow path)** — Diary paragraphs to `journal/*.journal.md`. Opus distills themes into core markdown. Old entries archived monthly.
