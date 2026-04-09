# Quaid User Guide (Quick Start)

This is the short, must-know guide for day-1 Quaid usage.

## What Quaid does

Quaid keeps long-lived memory across sessions, then injects only relevant context back into your agent.

## Project system basics

- Your real project files usually stay where they already live.
- Quaid tracks projects through a hidden registry plus canonical project docs under `~/quaid/projects/`.
- `projects/quaid/` in this repo is the built-in reference project for Quaid itself.
- Register project docs or source roots so Quaid can index and inject them during recall.
- The janitor is the normal maintenance loop: dedup, cleanup, docs refresh, and project hygiene.

## Where your Quaid files live

Quaid uses a split layout:

- Hidden system home: `~/.quaid/` (`QUAID_HOME`)
- Visible user-facing home: `~/quaid/` (`QUAID_VISIBLE_HOME`)

Each instance uses both:

- `<QUAID_HOME>/instances/<instance>/config.json`: runtime config for that instance
- `<QUAID_HOME>/instances/<instance>/data/memory.db`: memory database
- `<QUAID_HOME>/instances/<instance>/logs/`: runtime and janitor logs
- `<QUAID_HOME>/project-registry.json`: cross-instance project registry
- `<QUAID_VISIBLE_HOME>/instances/<instance>/SOUL.md`: Quaid-managed identity file
- `<QUAID_VISIBLE_HOME>/instances/<instance>/USER.md`: Quaid-managed identity file
- `<QUAID_VISIBLE_HOME>/instances/<instance>/ENVIRONMENT.md`: Quaid-managed identity file
- `<QUAID_VISIBLE_HOME>/instances/<instance>/journal/`: journal files
- `<QUAID_VISIBLE_HOME>/projects/`: canonical project docs and Quaid-managed project state
- `<QUAID_HOME>/shared/config/<platform>/config.json`: platform-level shared overrides
- `<QUAID_HOME>/shared/config/global/config.json`: machine-wide global shared settings (embeddings, Ollama)

Important:
- Model/provider overrides should be platform-scoped (`shared/config/<platform>/...`), not global.
- Embeddings settings live in the global shared config and must be consistent across all instances on a machine.
- Different platforms can have different providers and model lanes.
- `~/quaid/` is Quaid's visible surface, not a general-purpose workspace. Real project files can live elsewhere and be linked into projects.
- `~/.quaid/` is the hidden system root. Do not hand-edit it unless you are debugging or doing maintenance.

## How most people use Quaid

- Most users interact with Quaid through their agent rather than by driving the CLI directly.
- If you want to change models, providers, notifications, or other behavior, ask the running agent to make the change so it preserves the correct platform and instance context.
- If something looks wrong, ask the agent to inspect Quaid health and logs rather than guessing at shell commands.

## Pro tips (advanced)

- Shared memory between agents (experimental):
  - You can symlink one instance directory to another to force shared state.
  - Do this only if you fully understand the blast radius.
- Migrate memory between machines/agents:
  - Move or copy both the hidden and visible instance folders:
    - `<QUAID_HOME>/instances/<instance>/`
    - `<QUAID_VISIBLE_HOME>/instances/<instance>/`
  - Keep `config.json`, `data/`, `logs/`, visible identity files, and `journal/` together.
  - After migration, ask your agent to verify the install before you rely on it.

## Safety notes

- Back up instance directories before major changes.
- If behavior looks wrong after edits or migration, start with the instance `logs/` directory and have your agent inspect the active Quaid instance.
