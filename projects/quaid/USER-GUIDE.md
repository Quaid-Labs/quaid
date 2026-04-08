# Quaid User Guide (Quick Start)

This is the short, must-know guide for day-1 Quaid usage.

## What Quaid does

Quaid keeps long-lived memory across sessions, then injects only relevant context back into your agent.

## Project system basics

- Your real project files usually stay where they already live.
- Quaid tracks projects through a registry plus canonical project docs under `~/.quaid/projects/`.
- `projects/quaid/` in this repo is the built-in reference project for Quaid itself.
- Register project docs or source roots so Quaid can index and inject them during recall.
- The janitor is the normal maintenance loop: dedup, cleanup, docs refresh, and project hygiene.

## Where your Quaid files live

Quaid is instance-based. By default, Quaid keeps its own runtime state under `~/.quaid/`. Each instance has its own silo:

- `<QUAID_HOME>/<instance>/config/memory.json`: runtime config for that instance
- `<QUAID_HOME>/<instance>/data/memory.db`: memory database
- `<QUAID_HOME>/<instance>/identity/`: Quaid-managed identity files
- `<QUAID_HOME>/<instance>/logs/`: runtime and janitor logs
- `<QUAID_HOME>/projects/`: canonical project docs, registry metadata, and Quaid-managed project state
- `<QUAID_HOME>/shared/config/<platform>/memory.json`: platform-level shared overrides
- `<QUAID_HOME>/shared/config/global/memory.json`: machine-wide global shared settings (embeddings, Ollama)

Important:
- Model/provider overrides should be platform-scoped (`shared/config/<platform>/...`), not global.
- Embeddings settings live in the global shared config and must be consistent across all instances on a machine.
- Different platforms can have different providers and model lanes.
- `~/.quaid/` is Quaid's home, not a general-purpose workspace. Real project files can live elsewhere and be linked into projects.

## How most people use Quaid

- Most users interact with Quaid through their agent rather than by driving the CLI directly.
- If you want to change models, providers, notifications, or other behavior, ask the running agent to make the change so it preserves the correct platform and instance context.
- If something looks wrong, ask the agent to inspect Quaid health and logs rather than guessing at shell commands.

## Pro tips (advanced)

- Shared memory between agents (experimental):
  - You can symlink one instance directory to another to force shared state.
  - Do this only if you fully understand the blast radius.
- Migrate memory between machines/agents:
  - Move or copy the entire instance folder (`<QUAID_HOME>/<instance>/`).
  - Keep `config/`, `data/`, and `identity/` together.
  - After migration, ask your agent to verify the install before you rely on it.

## Safety notes

- Back up instance directories before major changes.
- If behavior looks wrong after edits or migration, start with the instance `logs/` directory and have your agent inspect the active Quaid instance.
