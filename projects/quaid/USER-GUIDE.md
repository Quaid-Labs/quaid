# Quaid User Guide (Quick Start)

This is the short, must-know guide for day-1 Quaid usage.

## What Quaid does

Quaid keeps long-lived memory across sessions, then injects only relevant context back into your agent.

## Project system basics

- Your projects live in `projects/`.
- `projects/quaid/` is the built-in reference project.
- Register project docs so Quaid can index and inject them during recall.
- The janitor is the normal maintenance loop: dedup, cleanup, docs refresh, and project hygiene.

## Where your Quaid files live

Quaid is instance-based. Each instance has its own silo:

- `<QUAID_HOME>/<instance>/config/memory.json`: runtime config for that instance
- `<QUAID_HOME>/<instance>/data/memory.db`: memory database
- `<QUAID_HOME>/<instance>/identity/`: Quaid-managed identity files
- `<QUAID_HOME>/<instance>/logs/`: runtime and janitor logs
- `<QUAID_HOME>/projects/`: shared project docs and project registry area
- `<QUAID_HOME>/shared/config/<platform>/memory.json`: platform-level shared overrides

Important:
- Model/provider overrides should be platform-scoped (`shared/config/<platform>/...`), not global.
- Different platforms can have different providers and model lanes.

## Commands you will actually use

- `quaid doctor` — health and wiring checks
- `quaid stats` — quick memory stats
- `quaid config edit` — edit current instance config
- `quaid config edit --platform-shared` — edit platform shared config

## Pro tips (advanced)

- Shared memory between agents (experimental):
  - You can symlink one instance directory to another to force shared state.
  - Do this only if you fully understand the blast radius.
- Migrate memory between machines/agents:
  - Move or copy the entire instance folder (`<QUAID_HOME>/<instance>/`).
  - Keep `config/`, `data/`, and `identity/` together.
  - Re-run `quaid doctor` after migration.

## Safety notes

- Back up instance directories before major changes.
- If behavior looks wrong after edits/migration, check `logs/` and run `quaid doctor`.
