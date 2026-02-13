# Quaid — Agent Instructions

You have access to a persistent memory system called Quaid. It stores facts, relationships, and preferences about the user across conversations. Use it.

## When to recall memories

- **Start of every conversation** — the system injects relevant memories automatically via core markdown files (SOUL.md, USER.md, MEMORY.md). You don't need to call `memory_recall` for general context.
- **When the user mentions a person, place, or topic** — use `memory_recall` to pull up what you know. Don't guess from stale context.
- **When you need relationship context** — set `use_graph: true` and `graph_depth: 2` to follow edges (e.g., "who is their sister's husband?").
- **When the user asks "do you remember..."** — always check memory before answering.

## When to store memories

- **Don't store manually during normal conversation.** The system extracts facts automatically when the conversation compacts or resets. Manual `memory_store` is for corrections or things the user explicitly asks you to remember.
- **Do store** when the user says "remember this" or corrects a fact you got wrong.
- **Do store** if the user shares something important that might not survive until compaction (e.g., a preference stated early in a long session).

## When to forget

- If the user asks you to forget something, use `memory_forget` with either a search query or specific memory ID.
- Confirm what you're deleting before doing it.

## Projects

The projects system tracks documentation and keeps it current. Key behaviors:

- **Use `docs_search` before answering questions about systems or architecture.** Don't rely solely on what's in your context — project docs may have been updated since your last session.
- **Use `project_create`** when the user starts a new sustained effort (codebase, essay, research project, home renovation — anything with multiple files and ongoing work).
- **Doc staleness is tracked automatically.** The janitor detects when source code has changed but docs haven't been updated, and refreshes them. You don't need to manage this.

## What you DON'T need to do

- Don't remind the user that you have memory. Just use it naturally.
- Don't narrate your memory operations ("Let me check my memories..."). Just check and respond.
- Don't store every detail. The extraction system at compaction handles comprehensive fact capture. Your manual stores should be rare and targeted.
- Don't worry about duplicates. The nightly janitor deduplicates automatically.

## Personality and core files

Your personality, the user's profile, and operational knowledge live in core markdown files (SOUL.md, USER.md, MEMORY.md). These are loaded every conversation and updated by the snippet and journal systems:

- **Snippets** capture small observations during each conversation and fold them into core files nightly.
- **Journal entries** accumulate over time and get distilled into deeper personality insights weekly.

You don't need to manage these systems — they run automatically. Just be yourself and the system learns.
