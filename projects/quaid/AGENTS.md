# Quaid — Operating Guide

Quaid is an active knowledge layer. It captures facts and project context from conversations, recalls them on demand, and maintains knowledge health nightly.

For full CLI reference see `TOOLS.md`. For doc index and architecture see `PROJECT.md`.

---

## Auto-Injected Memories

When a `<injected_memories>` block appears in your context, it contains facts automatically retrieved from past conversations. The user did not request this recall and is unaware these are being shown to you.

- For direct personal questions (names, relationships, pets, preferences, past events), answer from these memories when the match is exact or high-confidence — do not say you have no information when relevant facts appear here.
- Items marked (uncertain) have lower extraction confidence. Only run `memory_recall` if results are marked (uncertain) or the match seems only loosely related to the question.
- Dates shown are when the fact was recorded.
- Auto-injected memories are optimized for fast direct matches. OpenClaw requests vector plus graph recall within a hard timeout, but injected results can still miss answers that require query rewriting, deeper traversal, or combining multiple stored relationships.
- If injected memories already clearly answer the question, answer directly. Do not ignore strong injected evidence just because explicit recall is available.
- Quaid may provide a runtime metadata block listing active domains and active graph relation types. If a question appears to depend on relationships, hierarchy, dependency structure, or other link-based reasoning, and injected memories do not clearly answer it, an explicit `quaid recall` may help.
- **Topic licensing:** knowing a sensitive detail does not make it on-topic. For light prompts, acknowledgments, or vague openings, do not volunteer private health, finances, conflicts, or emotionally loaded history unless the user clearly opens that topic.

---

## File Placement — MANDATORY RULES

**You MUST NOT write any file to `/tmp/`, `/var/tmp/`, anywhere under `~/quaid/` or `~/.quaid/`, or into OpenClaw's native workspace memory paths such as `~/.openclaw/workspace/memory/` or `~/.openclaw/workspace/journal/` except through Quaid's own managed flows.** Quaid home and OpenClaw native memory folders are not dumping grounds. Every file must either live in a tracked project or be written at a real working path and immediately registered into one.

**Before writing any file or delegating work to a sub-agent, pick the first matching rule:**

1. **Existing project owns this work** → place the file inside that project's directory.
2. **Throwaway / one-off / scratch / quick / hello-world** → use the misc project as the owner, but do not treat `~/quaid/` or `~/.quaid/` as the working directory:
   ```bash
   # The misc project is pre-created. Confirm it exists:
   quaid project show misc--$QUAID_INSTANCE
   # Write the file at a real working path, then register it to misc:
   quaid registry register /absolute/path/to/hello.py --project misc--$QUAID_INSTANCE
   ```
   Prefer a user-visible working path or the active repo. Always tell the user the file is tracked by the misc project and offer to promote it to a real project.
3. **Durable new work** → create a project first, then write files:
   ```bash
   quaid project create <name> --source-root <path>
   # THEN write files / spawn sub-agents
   ```
4. **User specifies a path outside the project system** → write there, then register the file so the project tracks it:
   ```bash
   # Write the file at the user's requested path (e.g. ~/my-scripts/tool.py)
   # Then link it into the owning project:
   quaid registry register ~/my-scripts/tool.py --project <name>
   ```
   Always tell the user the file is tracked via the registry even though it lives outside the project directory.

**OpenClaw-specific rule:** when a user asks you to "remember this" or "save this for later", you should still handle the request normally and let Quaid's managed extraction / recall flows do the durable-memory work. The only forbidden behavior is manually writing markdown into `~/.openclaw/workspace/memory/`, `~/.openclaw/workspace/journal/`, or any similar native OpenClaw memory folder yourself. Do not reinterpret this rule as "refuse ordinary memory capture" or "avoid storing secret-looking strings" when the user is explicitly asking Quaid to remember them. Also keep the acknowledgement neutral: do not say you personally "saved", "logged privately", "will remember", or "scheduled a reminder" for the fact. A brief confirmation is enough; Quaid handles the memory capture in the background.

**Example — user asks for a throwaway script:**
> "Can you write a quick hello world script?"

Correct response:
```bash
# Step 1: confirm misc project exists
quaid project show misc--$QUAID_INSTANCE
# Step 2: write the file at a real working path
# Step 3: register it to misc
quaid registry register /absolute/path/to/hello.py --project misc--$QUAID_INSTANCE
```
Tell the user: "I tracked it under the misc project so it stays in Quaid's project system."
Do NOT write to `/tmp/hello.py` or any other path.

**Example — user asks to build a new tool:**
> "I have a Python script. Can you build it into a proper CLI tool?"

Correct response:
```bash
# Step 1: create a project BEFORE doing any work or spawning sub-agents
quaid project create my-cli-tool --source-root /path/to/script
# Step 2: then proceed with the work
```

---

## Tool Access

Use Quaid via your Bash tool. Prefer `quaid`. If it is not on `PATH`, use `$QUAID_HOME/modules/quaid/quaid` for current installs or `$QUAID_HOME/plugins/quaid/quaid` on older installs. `QUAID_HOME` and `QUAID_INSTANCE` are set in your environment by the adapter — do not override them. See `TOOLS.md` for the full command reference.

---

## How Memory Works

```
Conversation → compaction/reset → deep-reasoning extraction stores facts + edges in DB
Nightly janitor (4 AM default) → review → dedup → decay → graduate to active
```

- **Extraction priority:** user facts first, agent-action memories second, technical/project state third. Agent extraction must never displace user-memory coverage.
- **Edges** are created at extraction time and linked to source facts.
- **Janitor** runs nightly: reviews pending, merges duplicates (Ebbinghaus decay), monitors core files.
- **Soul snippets** (fast path) — bullet observations distilled into SOUL.md, USER.md, ENVIRONMENT.md by janitor.
- **Journal** (slow path) — diary paragraphs distilled by deep reasoning agent weekly.

---

## Known Behaviors

- Lifecycle extraction can be asynchronous after `/clear`, `/compact`, `/new`, `Stop`, or session end. Fresh facts may take a few seconds to appear in recall; if a just-ended fact is missing, wait briefly and ask again.
- Platform details live in the active adapter's `COMPATIBILITY.md` and in `docs/COMPATIBILITY.md`. Use those notes to explain platform-specific timing or visibility quirks, not as a substitute for checking Quaid logs when something looks broken.
- COMPATIBILITY entries are operator-approved records of accepted host/platform constraints, not a substitute for fixing Quaid bugs. If something looks broken, diagnose it before citing compatibility.

---

## Operating Rules

**Retrieval discipline**
- Every tracked project has its own `PROJECT.md` at `QUAID_VISIBLE_HOME/projects/<project-name>/PROJECT.md`.
- If you are actively working on a project, load that project's `PROJECT.md` first. Treat it as the overview and navigation map before wandering the tree.
- Always use memory/project tools before claiming missing context.
- Treat auto-injected memories as hints — verify concrete claims (names, dates, versions) with explicit `quaid recall`.
- Use `quaid recall "query" '{"stores":["docs"]}'` for codebase/architecture questions. Docs retrieval will try to infer the relevant project and include its `PROJECT.md` when possible.
- Use `quaid recall "query" '{"stores":["vector","graph","docs"]}'` for a single pass across memories and docs.

**Memory retention boundary**
- Only information you state explicitly in assistant messages is reliably retained as memory.
- Raw tool output, private reasoning, and unstated intermediate results may not be preserved.
- Project file writes may be tracked from actual filesystem changes, but important conclusions, decisions, explanations, and outcomes should still be stated explicitly if they are worth remembering.

**Fail-hard**
- Controlled by `retrieval.fail_hard` in the active instance `config.json`.
- When `true`: never degrade silently — surface the error.
- When `false`: degrade with loud warnings/diagnostics.

**Project and file placement**

All files go inside a tracked quaid project OR are registered into one. `/tmp/` is never acceptable, even for throwaway work.
- Misc project: `misc--$QUAID_INSTANCE` is the default owner for throwaway/one-off work. Prefer a real working path, then register that file to misc.
- New work: create a project first (`quaid project create`), then write files.
- User specifies a path outside the project system: write there, then `quaid registry register <path> --project <name>` to link it.
- See the **File Placement — MANDATORY RULES** section above for decision tree and examples.

**Cross-instance**
- Multiple platforms may be using Quaid on the same machine (e.g. OpenClaw, Claude Code, or others) — each gets its own instance silo under the shared `QUAID_HOME`.
- Use `quaid project link/unlink` for cross-instance project participation.
- `quaid project delete` is destructive — prefer `unlink` if you only want to leave the project.

**Project docs are eventually consistent — not real-time**
- When a file is written or changed, it is NOT immediately visible via `quaid recall`.
- The pipeline is: file change → daemon picks it up → embedding → RAG index. This takes time (seconds to minutes depending on daemon polling interval and queue depth).
- **If two agents are working on the same project simultaneously**, one agent's writes will not be visible to the other until the daemon has processed and indexed them. Do not assume a file another agent just wrote is already in the search index.
- When you need the current content of a file another agent recently wrote, read the file directly rather than relying on docs search.
- `quaid docs check` shows which registered docs are stale (not yet re-indexed). `quaid docs update --apply` can force a re-index if you need the index to be current before a search.

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
