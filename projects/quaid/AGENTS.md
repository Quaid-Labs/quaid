# Knowledge Layer — Operational Guide

## Three Layers

### Layer 1: Core Markdown (Always Loaded)
The root markdown files. Loaded every turn. Your foundation.
- **Cost:** Every token counts — loaded on every API call
- **Use for:** Essential context, personality, operational rules
- **Keep clean:** Janitor monitors for bloat

### Layer 2: RAG Documentation (`projects/`)
Detailed technical docs, searchable semantically:
```bash
python3 modules/quaid/docs_rag.py search "query"
```
- Project docs under `projects/<project>/` are auto-indexed nightly
- **Use for:** Implementation details, system architecture, reference docs
- **Rule:** If it's >10 lines of detail, it goes in `projects/<project>/`, not core files

### Layer 3: Memory Database
Personal facts about Quaid's world — people, places, preferences, relationships:
```bash
python3 modules/quaid/memory_graph.py search "query" --owner quaid
```
- ~424 facts (620 total nodes) with semantic embeddings (qwen3-embedding:8b)
- Graph edges connect entities (parent_of, has_pet, lives_at)
- Searched via `memory_recall` tool (hybrid search, RRF fusion, intent-aware)

## Memory Lifecycle

```
Conversation -> /compact or /reset -> Opus extracts facts+edges -> Store in DB
                                                         |
Nightly janitor -> Review -> Dedup -> Decay -> Graduate to 'active'
```

- **Extraction:** Opus analyzes transcript at compaction, extracts personal facts with relationships
- **Edges:** Created at extraction time, linked to source facts
- **Janitor:** Runs 4:30 AM — reviews pending facts, merges duplicates, decays stale memories (Ebbinghaus), monitors core files

**Extraction priority invariant:**
- User facts are first priority.
- Agent-action memories are second priority.
- Technical/project-state facts are third priority.
- Agent/technical extraction must never reduce baseline user-memory coverage.

## Using Memory

**When to search memory:**
- User mentions people (family, friends, staff)
- Questions about preferences or history
- References to past events or decisions
- Needs personal context to answer well

**How to search:**
```text
memory_recall query="Hauser birthday" options={graph:{expand:true}}
```
- Use **names** not pronouns ("Hauser" not "my sister")
- Add **context** ("Lori gift ideas" not "mom")
- Set `expandGraph: true` for relationship queries
- Prefer explicit `datastores` when you know the target source:
  - `vector_basic` for personal/life facts
  - `vector_technical` for technical/project facts
  - `graph` for relationship traversal
  - `journal` for reflective context
  - `project` for docs-derived project knowledge
- Use catch-all routing (`options.routing.enabled: true`) only when uncertain.
  - `total_recall` triggers an extra fast-LLM planning pass (better recall plan, higher cost/latency).
  - Routing output must be valid structured JSON; the router auto-retries once with validation feedback.
  - If the retry still fails, recall throws by default. Use a stronger fast model.
  - Default behavior is fail-open (`retrieval.routerFailOpen: true`): log loudly and continue with deterministic default recall plan if prepass fails.
  - Set `options.routing.failOpen: false` (or config `retrieval.routerFailOpen: false`) for strict fail-through behavior.
  - If the query is obvious, explicit `datastores` via plain recall is cheaper and usually faster.
  - Optional: `options.routing.reasoning: "deep"` for a higher-quality routing pass when you can afford extra cost/latency.
  - Optional: `options.routing.intent: "agent_actions"` when asking what the assistant/agent did or suggested.
  - Optional: `options.filters.docs: [...]` to constrain project-doc recall to specific docs.
  - Optional: `options.datastoreOptions.<store>` for store-local controls (for example, project-specific doc scope in `options.datastoreOptions.project`).

**Injector confidence policy:**
- Auto-injected memories are hints.
- If retrieval is not a clear high-confidence match, call `memory_recall` before committing to specifics.
- Do not treat injected context as final authority for names, dates, or relationship facts unless it is a direct/high-confidence hit.

- Project metadata for routing:
  - Put a one-line `Project Description: ...` at the top of each project `TOOLS.md`.
  - Keep deep details in `PROJECT.md` (long-form, not meant for always-loaded context).

**When to skip:** Pure coding tasks, general knowledge, short acks.

## Docs Retrieval Behavior (Current)

- `projects_search` is still the richest docs path:
  - RAG search over project docs
  - optional project filter
  - prepends matching `PROJECT.md` when project is known
  - includes staleness warnings
- `memory_recall` with `options.datastores: ["project"]` is lighter:
  - project-store recall only
  - no project bootstrap preface
  - no staleness report

## Dual Extraction: Snippets + Journal

Both systems extract entries at compaction/reset time:

- **Soul Snippets (fast path)** — Bullet-point observations to `*.snippets.md`. Nightly janitor FOLDs/REWRITEs/DISCARDs into core files (SOUL.md, USER.md, etc).
- **Journal (slow path)** — Diary paragraphs to `journal/*.journal.md`. Opus distills themes into core markdown. Old entries archived monthly.
