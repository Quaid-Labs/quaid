# Quaid Design Principles

> The human talks. The LLM operates. Quaid is infrastructure.

---

## Core Principle

No technical concept should ever surface to the human unless they specifically
ask for it. Quaid is invisible plumbing that makes the LLM's memory and project
management "just work."

The human user might not know how to code, might not understand file systems,
and definitely doesn't want to hear about git, sync engines, or directory
structures. They just talk to their LLM, and it remembers things really well.

---

## Who Operates Quaid?

| Role | Responsibility |
|------|---------------|
| **Human** | Talks naturally. Asks for things. Reviews output. |
| **LLM** | Operates Quaid's tools. Decides what to track, how to organize, when to update. |
| **Quaid** | Infrastructure. Extraction, indexing, sync, versioning. Silent background work. |
| **Adapter** | Bridge between Quaid and the host platform (Claude Code, OpenClaw, etc.). Owns auth, identity, platform-specific behavior. |

The LLM is Quaid's operator. It calls high-level APIs (`create_project`,
`track_directory`, `save_document`), and Quaid handles the plumbing underneath.

---

## Design Rules

### 1. Invisible Infrastructure

Everything Quaid does should be invisible to the human by default:

- **Git tracking**: Shadow git for change detection. No `.git` in user space.
  No git commands, messages, or concepts surfaced.
- **Sync engines**: Background file sync between adapters. Never mentioned.
- **Ignore patterns**: LLM-managed with defensive defaults. The human never
  sees `.gitignore` or exclude lists.
- **Directory structures**: The LLM tells the human "I saved your itinerary
  to your Japan Trip folder," not "I wrote to
  `QUAID_HOME/projects/japan-trip/docs/itinerary.md`."
- **Extraction**: Facts are extracted silently. The human doesn't know about
  daemons, cursors, or signal files.

### 2. Defensive Defaults, LLM-Managed Overrides

Ship with sane defaults that protect the human from mistakes:

- Default ignore patterns for `.env`, `.db`, `node_modules/`, etc.
- Default file size limits, directory depth limits.
- Default privacy rules (never index credentials, secrets, tokens).

The LLM can override these when appropriate, but the defaults should be safe
for someone who doesn't know what they're doing.

### 3. Adapter Owns Its Platform

Each adapter is the authority on its platform's behavior:

- **Auth**: The adapter owns token storage and retrieval.
- **Identity**: The adapter declares where base context files live.
- **Sync**: The adapter requests sync services from core when needed.
- **Capabilities**: The adapter declares what it can do via its plugin manifest.

Core provides services. Adapters consume them. No cross-adapter assumptions.

### 4. Central Source of Truth, Platform-Specific Views

One canonical location for shared data (projects, docs, registry). Each
platform gets a view appropriate to its constraints:

- Claude Code can read directly from the central store.
- OpenClaw needs copies inside its workspace boundary.
- Future adapters declare their constraints, core provides the right view.

### 5. Fail Loud, Degrade Gracefully

When something breaks:

- **To the operator (LLM)**: Loud, specific error messages. The LLM needs to
  know exactly what failed so it can fix it or work around it.
- **To the human**: Nothing. Or a gentle "I'm having trouble remembering
  something, give me a moment." Never stack traces, never technical jargon.

### 6. Build for Migration

Every schema, every directory layout, every config format will eventually
change. Design for it:

- Version everything (schemas, configs, plugin manifests).
- Keep layout decisions in one spec doc (see `DIRECTORY-STANDARD.md`).
- Never let layout decisions leak into multiple places without a single
  source of truth.
- When something moves, write a migration — don't leave compatibility shims
  that accumulate forever.

---

## The Vision

A person messages their LLM: "I'm planning a trip to Japan next month."

The LLM, backed by Quaid:
1. Creates a project (invisible to human).
2. Starts tracking relevant conversations (invisible).
3. Extracts facts about preferences, dates, constraints (invisible).
4. When the human says "put together an itinerary," the LLM has full context
   of everything discussed, writes the document, and delivers it however the
   human prefers — a folder on their desktop, an email, a shared doc.

The human never configures anything. Never sees a settings file. Never runs
a command. They just talk, and it works.

---

## References

- [Directory Standard](DIRECTORY-STANDARD.md) — canonical file layout
- [Project System Spec](PROJECT-SYSTEM-SPEC.md) — tracking, sync, shadow git
- [Architecture](ARCHITECTURE.md) — system architecture
- [Plugin System](PLUGIN-SYSTEM.md) — adapter/plugin contracts
