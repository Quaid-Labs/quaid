# Quaid — Agent Operating Rules

Quaid is an active knowledge layer: it recalls stored facts, tracks project context, and maintains project docs. `TOOLS.md` is the callable CLI/API reference. `PROJECT.md` is the project overview, architecture map, and doc index.

## Memory And Recall

- Treat auto-injected memories as high-value hints, not invisible noise.
- For direct personal questions, answer from injected memories when the match is exact or high-confidence.
- Do not volunteer sensitive health, finance, conflict, or emotionally loaded history unless the user clearly opens that topic.
- Use explicit `quaid recall` before claiming missing context.
- Use `quaid recall "query" '{"stores":["docs"]}'` for codebase, architecture, API, or project-doc questions.
- Use `quaid recall "query" '{"stores":["vector","graph","docs"]}'` when both memory history and project docs may matter.
- Treat generated project docs and raw source as current-state evidence. For historical, session-scoped, motivation, bug-bash, or "what did the agent find/suggest" questions, prefer chronological memory evidence unless the question explicitly asks about current implementation.
- Verify concrete claims such as names, dates, versions, and status with explicit recall or direct files when the answer matters.

## Memory Retention Boundary

- Only information explicitly stated in assistant messages is reliably retained as memory.
- Raw tool output, private reasoning, and unstated intermediate conclusions may not be preserved.
- Project file writes may be tracked from filesystem changes, but durable decisions, outcomes, explanations, and status should still be stated explicitly.

## Project And File Placement

Never write files to `/tmp/`, `/var/tmp/`, `~/quaid/`, or `~/.quaid/` except through Quaid-managed flows.

Before writing or delegating work, choose the first matching rule:

1. Existing project owns the work: write inside that project or register the real working path to it.
2. Throwaway or scratch work: use `misc--$QUAID_INSTANCE` as owner, write at a real working path, then register the file.
3. Durable new work: create a project first with `quaid project create <name> --source-root <path>`, then write files or spawn agents.
4. User specifies an external path: write there, then `quaid registry register <path> --project <name>`.

Always tell the user when a file is tracked through the project registry.

## Project Docs Consistency

- When actively working on a project, read that project's `PROJECT.md` first for overview and navigation.
- `TOOLS.md` is only for callable tools/APIs/commands an agent can use. Keep it terse and role-pure.
- `AGENTS.md` is only for stable operating behavior and safety rules. Keep strategy, status, architecture narrative, and changelog material in `PROJECT.md` or focused docs.
- Project docs are eventually consistent. Recent writes may not be visible via docs recall until the supervisor/worker has indexed them.
- If another agent just wrote a file, read the file directly instead of relying on docs search.

## Fail-Hard

- `retrieval.fail_hard=true` means failures must surface loudly.
- Do not hide fail-hard retrieval, provider, datastore, or docs failures with silent fallbacks.
- If fail-hard trips in live work, fix or route the underlying issue.

## Deferred Notices

- Deferred notices are normal Quaid context from janitor/update flows.
- Do not ask permission to read them.
- Drain them only when a human user is actively present: `quaid notify --deferred-drain`.
- If pending notices are detected at the start of a human-facing task, drain and relay the result concisely.

## Cross-Instance Safety

- Multiple platforms may share `QUAID_HOME` while using separate `QUAID_INSTANCE` silos.
- Use `quaid project link` and `quaid project unlink` for cross-instance participation.
- Prefer `unlink` over destructive `project delete` unless deletion is explicitly intended.

## Always-Loaded Quaid Files

| File | Role |
|------|------|
| `TOOLS.md` | Callable CLI/API reference |
| `AGENTS.md` | Stable operating rules |
| `PROJECT.md` | Project overview and architecture map |
| `SOUL.md` | Quaid identity and long-term orientation |
| `USER.md` | User understanding and preferences |
| `ENVIRONMENT.md` | Environment and shared operational context |
