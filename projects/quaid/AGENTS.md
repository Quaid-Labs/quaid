# Memory System — Operational Guide

## Three Layers

### Layer 1: Core Markdown (Always Loaded)
The root markdown files. Loaded every turn. Your foundation.
- **Cost:** Every token counts — loaded on every API call
- **Use for:** Essential context, personality, operational rules
- **Keep clean:** Janitor monitors for bloat

### Layer 2: RAG Documentation (`projects/`)
Detailed technical docs, searchable semantically:
```bash
python3 plugins/quaid/docs_rag.py search "query"
```
- Project docs under `projects/<project>/` are auto-indexed nightly
- **Use for:** Implementation details, system architecture, reference docs
- **Rule:** If it's >10 lines of detail, it goes in `projects/<project>/`, not core files

### Layer 3: Memory Database
Personal facts about Quaid's world — people, places, preferences, relationships:
```bash
python3 plugins/quaid/memory_graph.py search "query" --owner quaid
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

## Using Memory

**When to search memory:**
- User mentions people (family, friends, staff)
- Questions about preferences or history
- References to past events or decisions
- Needs personal context to answer well

**How to search:**
```
memory_recall query="Hauser birthday" expandGraph=true
```
- Use **names** not pronouns ("Hauser" not "my sister")
- Add **context** ("Lori gift ideas" not "mom")
- Set `expandGraph: true` for relationship queries

**When to skip:** Pure coding tasks, general knowledge, short acks.

## Dual Extraction: Snippets + Journal

Both systems extract entries at compaction/reset time:

- **Soul Snippets (fast path)** — Bullet-point observations to `*.snippets.md`. Nightly janitor FOLDs/REWRITEs/DISCARDs into core files (SOUL.md, USER.md, etc).
- **Journal (slow path)** — Diary paragraphs to `journal/*.journal.md`. Opus distills themes into core markdown. Old entries archived monthly.
