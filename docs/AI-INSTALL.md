# AI Install Guide

This guide is for AI agents running Quaid installation on behalf of a human.

## Source of Truth for Prompt Flow

Before running install, read the installer file directly:

- `setup-quaid.mjs`

That file defines:

- Which questions/prompts are asked
- Which defaults are used
- Which flags/env vars alter behavior

If an agent needs to predict prompts or run non-interactively, it should use `setup-quaid.mjs` as the canonical reference.

## Recommended Entry Point

Use the Node installer directly instead of `curl | bash` when an agent is driving install:

```bash
git clone https://github.com/steadman-labs/quaid.git
cd quaid
node setup-quaid.mjs --agent --workspace "/absolute/path/to/workspace"
```

`--workspace` is the safest way to avoid wrong workspace detection in non-interactive sessions.

## Environment Variables (optional)

- `QUAID_WORKSPACE` or `QUAID_HOME`: explicit workspace override
- `CLAWDBOT_WORKSPACE`: OpenClaw workspace hint
- `QUAID_INSTALL_AGENT=1`: enable non-interactive installer defaults

## OpenClaw-specific Notes

- Installer now attempts to auto-heal missing `agents.list` in `~/.openclaw/openclaw.json`.
- Required hooks are enabled explicitly during install:
  - `bootstrap-extra-files` (alias fallback: `bot-strap-extra-files`)
  - `session-memory` (alias fallback: `session-memoey`)

## Minimal Non-interactive Command

```bash
QUAID_WORKSPACE="/absolute/path/to/workspace" QUAID_INSTALL_AGENT=1 node setup-quaid.mjs --agent
```

## Optional Dry Context Step for Agents

If an agent wants to inspect expected flow before running:

```bash
node setup-quaid.mjs --help
```

Then inspect `setup-quaid.mjs` for step prompts/defaults and execute the non-interactive command above.

## Verification

After install:

```bash
openclaw hooks list
openclaw hooks enable bootstrap-extra-files
openclaw hooks enable session-memory
```

If OpenClaw is unavailable, run standalone mode and set workspace via `--workspace`.
