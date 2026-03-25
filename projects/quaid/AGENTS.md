# Quaid — Operating Guide

Quaid is an active knowledge layer. It captures facts and project context from conversations, recalls them on demand, and maintains knowledge health nightly.

For full CLI reference see `TOOLS.md`. For doc index and architecture see `PROJECT.md`.

---

## Auto-Injected Memories

When a `<injected_memories>` block appears in your context, it contains facts automatically retrieved from past conversations. The user did not request this recall and is unaware these are being shown to you.

- For direct personal questions (names, relationships, pets, preferences, past events), answer from these memories when the match is exact or high-confidence — do not say you have no information when relevant facts appear here.
- Items marked (uncertain) have lower extraction confidence. Only run `memory_recall` if results are marked (uncertain) or the match seems only loosely related to the question.
- Dates shown are when the fact was recorded.
- Auto-injected memories are optimized for fast direct matches. They can miss answers that depend on query rewriting, graph traversal, or combining multiple stored relationships.
- If injected memories already clearly answer the question, answer directly. Do not ignore strong injected evidence just because explicit recall is available.
- Quaid may provide a runtime metadata block listing active domains and active graph relation types. If a question appears to depend on relationships, hierarchy, dependency structure, or other link-based reasoning, and injected memories do not clearly answer it, an explicit `quaid recall` may help.
- **Topic licensing:** knowing a sensitive detail does not make it on-topic. For light prompts, acknowledgments, or vague openings, do not volunteer private health, finances, conflicts, or emotionally loaded history unless the user clearly opens that topic.

---

## File Placement — MANDATORY RULES

**You MUST NOT write any file to `/tmp/`, `/var/tmp/`, `~/quaid/scratch/`, or any path outside a tracked quaid project.** No exceptions. "Temporary", "throwaway", "quick", or "hello world" files are NOT exempt — they go in the misc project.

**Before writing any file or delegating work to a sub-agent, pick the first matching rule:**

1. **Existing project owns this work** → place the file inside that project's directory.
2. **Throwaway / one-off / scratch / quick / hello-world** → write it to the misc project:
   ```bash
   # The misc project is pre-created. Write directly to its directory:
   ~/.openclaw/extensions/quaid/quaid project show misc--$QUAID_INSTANCE  # confirm path
   # Then write your file there, e.g.:
   # ~/quaid/projects/misc--$QUAID_INSTANCE/hello.py
   ```
   Always tell the user you wrote to misc and offer to promote to a real project.
3. **Durable new work** → create a project first, then write files:
   ```bash
   ~/.openclaw/extensions/quaid/quaid registry create-project <name> --source-roots <path>
   # THEN write files / spawn sub-agents
   ```

**Example — user asks for a throwaway script:**
> "Can you write a quick hello world script?"

Correct response:
```bash
# Step 1: confirm misc project path
~/.openclaw/extensions/quaid/quaid project show misc--$QUAID_INSTANCE
# Step 2: write the file inside that project
# e.g. write to ~/quaid/projects/misc--$QUAID_INSTANCE/hello.py
```
Tell the user: "I wrote it to the misc project at `~/quaid/projects/misc--$QUAID_INSTANCE/hello.py`."
Do NOT write to `/tmp/hello.py` or any other path.

**Example — user asks to build a new tool:**
> "I have a Python script. Can you build it into a proper CLI tool?"

Correct response:
```bash
# Step 1: create a project BEFORE doing any work or spawning sub-agents
~/.openclaw/extensions/quaid/quaid registry create-project my-cli-tool --source-roots /path/to/script
# Step 2: then proceed with the work
```

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
- **Soul snippets** (fast path) — bullet observations distilled into SOUL.md, USER.md, ENVIRONMENT.md by janitor.
- **Journal** (slow path) — diary paragraphs distilled by Opus monthly.

---

## Operating Rules

**Retrieval discipline**
- Every tracked project has its own `PROJECT.md` at `QUAID_HOME/projects/<project-name>/PROJECT.md`.
- If you are actively working on a project, load that project's `PROJECT.md` first. Treat it as the overview and navigation map before wandering the tree.
- Always use memory/project tools before claiming missing context.
- Treat auto-injected memories as hints — verify concrete claims (names, dates, versions) with explicit `quaid recall`.
- Use `quaid docs search` for codebase/architecture questions. Docs retrieval will try to infer the relevant project and include its `PROJECT.md` when possible.
- Use `quaid recall --docs` for a single pass across both memories and docs.

**Memory retention boundary**
- Only information you state explicitly in assistant messages is reliably retained as memory.
- Raw tool output, private reasoning, and unstated intermediate results may not be preserved.
- Project file writes may be tracked from actual filesystem changes, but important conclusions, decisions, explanations, and outcomes should still be stated explicitly if they are worth remembering.

**Interrupt policy**
- Complete the current task before starting a new one.
- Switch immediately only on explicit interruption (`wait`, `stop`, `cancel`).

**Fail-hard**
- Controlled by `retrieval.fail_hard` in `config/memory.json`.
- When `true`: never degrade silently — surface the error.
- When `false`: degrade with loud warnings/diagnostics.

**Project and file placement**

All files go inside a tracked quaid project. `/tmp/` is never acceptable, even for throwaway work.
- Misc project: `misc--$QUAID_INSTANCE` at `$QUAID_HOME/projects/misc--$QUAID_INSTANCE/` — use this for throwaway/one-off work. It is pre-created.
- New work: create a project first (`quaid registry create-project`), then write files.
- See the **File Placement — MANDATORY RULES** section above for decision tree and examples.

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
| `ENVIRONMENT.md` | Functional behaviors, environmental context, and shared history |
