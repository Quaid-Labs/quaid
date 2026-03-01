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
node setup-quaid.mjs --agent --workspace "/absolute/path/to/workspace" --owner-name "<Person Name>"
```

`--workspace` is the safest way to avoid wrong workspace detection in non-interactive sessions.
`--owner-name` ensures memory ownership is tagged to the human (not a system account).

## Environment Variables (optional)

- `QUAID_WORKSPACE` or `QUAID_HOME`: explicit workspace override
- `CLAWDBOT_WORKSPACE`: OpenClaw workspace hint
- `QUAID_INSTALL_AGENT=1`: enable non-interactive installer defaults
- `QUAID_OWNER_NAME`: explicit human owner name for memory tagging

## OpenClaw-specific Notes

- Installer now attempts to auto-heal missing `agents.list` in `~/.openclaw/openclaw.json`.
- Required hooks are enabled explicitly during install:
  - `bootstrap-extra-files` (alias fallback: `bot-strap-extra-files`)
  - `session-memory` (alias fallback: `session-memoey`)

## Minimal Non-interactive Command

```bash
QUAID_WORKSPACE="/absolute/path/to/workspace" QUAID_OWNER_NAME="<Person Name>" QUAID_INSTALL_AGENT=1 node setup-quaid.mjs --agent
```

## Optional Dry Context Step for Agents

If an agent wants to inspect expected flow before running:

```bash
node setup-quaid.mjs --help
```

Then inspect `setup-quaid.mjs` for step prompts/defaults and execute the non-interactive command above.

## Agent Communication Requirement (Mandatory)

Before running install, the AI agent must run a short pre-install survey with the human.

Show all planned answers (including defaults) and ask: "Do you want to change any of these before I run install?"

Minimum survey fields:

- Owner name (`users.defaultOwner`) as a human name, not system username
- Workspace path
- Adapter type
- LLM provider + deep/fast models
- Embeddings provider/model
- Notifications level + per-feature verbosity
- Enabled systems
- Janitor apply mode/policies
- Janitor schedule choice
- Workspace file import choice

Only execute install after the user confirms or edits these values.

When an AI agent runs install, it must explicitly report all selected options to the user, including values that were defaults.

Do not say only "install succeeded." Always include a compact options summary so the user can see what was chosen and what can be changed later.

Minimum required summary fields:

- Owner (`users.defaultOwner`)
- Adapter type (`adapter.type`)
- LLM provider (`models.llmProvider`) and selected deep/fast models (`models.deepReasoning`, `models.fastReasoning`)
- Notification settings (`notifications.level`, plus `notifications.janitor|extraction|retrieval.verbosity`)
- Enabled systems (`systems.memory|journal|projects|workspace`)
- Embedding provider/model (`models.embeddingsProvider`, `ollama.embeddingModel`)
- Janitor apply mode/policies (`janitor.applyMode`, `janitor.approvalPolicies.*`)
- Janitor schedule choice (or explicit "not scheduled")

The summary should also tell the user where to edit settings:

- Interactive editor: `quaid config edit`
- Config file: `<workspace>/config/memory.json`

## Verification

After install:

```bash
openclaw hooks list
openclaw hooks enable bootstrap-extra-files
openclaw hooks enable session-memory
```

If OpenClaw is unavailable, run standalone mode and set workspace via `--workspace`.
