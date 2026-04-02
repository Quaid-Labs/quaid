# AI Install Guide

This guide is for AI agents running Quaid installation on behalf of a human.

## Human Instructions for Agent (Copy/Paste)

Use this exact minimal prompt shape when asking an AI agent to install Quaid:

1. Read `docs/AI-INSTALL.md` first and follow it exactly.
2. Install Quaid for me using this workspace, instance name, and owner name.
3. Tell me when install is complete and `quaid doctor` is healthy.

Do not duplicate the survey contract, defaults, or approval flow in the human's
prompt. Those are defined here and in `setup-quaid.mjs`, and the agent must
derive them from those sources.

The pre-install survey is mandatory for all AI/agent installs, including `--agent` mode.

Execution order is mandatory:

1. Read this guide and `setup-quaid.mjs`.
2. Reply in the conversation with the pre-install survey and selected values.
3. Wait for human approval or edits.
4. Only then run install.

Do not keep browsing, planning, or exploring code after you already have enough
information to present the survey. The survey is the next required
conversation output.

## Platform Selection Rule (Mandatory)

If the human did not explicitly specify a platform, install Quaid for the
platform currently running the agent/session that is following this guide.

Examples:
- if the agent is running inside Codex, install for Codex
- if the agent is running inside Claude Code, install for Claude Code
- if the agent is running inside OpenClaw, install for OpenClaw

Do not switch to some other platform on the machine just because it is also
installed or auto-detectable.

On hosts with multiple platforms installed, auto-detection can choose the wrong
target. In non-interactive or automated runs, explicitly force the intended
platform when needed (for example `--adapter codex` for Codex installs, or the
platform-specific force flag such as `--claude-code`).

## Source of Truth for Prompt Flow

Before running install, read the installer file directly:

- `setup-quaid.mjs`

That file defines:

- Which questions/prompts are asked
- Which defaults are used
- Which flags/env vars alter behavior
- Which fields belong in the human pre-install survey

If an agent needs to predict prompts or run non-interactively, it should use `setup-quaid.mjs` as the canonical reference.
In particular, agents should follow the `AGENT_SURVEY_CONTRACT` block in that file.

Do not maintain a separate survey template in agent memory.
Do not infer survey sections from internal installer functions.
If `setup-quaid.mjs` changes, the survey must change with it.

When presenting the survey or final install summary, keep it scoped to the
target platform only.

- Do not mention other installed platforms just because they exist on the host.
- Do not mention OpenClaw-specific routing, channels, `last_used`, gateway
  semantics, or other adapter-only details during Codex or Claude Code installs.
- If shared machine-wide config is being reused, describe only the effective
  value that the current target install will inherit. Do not drag prior platform
  context into the explanation.

## Recommended Entry Point

Use the Node installer directly instead of `curl | bash` when an agent is driving install:

```bash
git clone https://github.com/quaid-labs/quaid.git
cd quaid
node setup-quaid.mjs --agent --workspace "/absolute/path/to/workspace" --owner-name "<Person Name>"
```

`--workspace` is the safest way to avoid wrong workspace detection in non-interactive sessions.
`--owner-name` ensures memory ownership is tagged to the human (not a system account).

## Canary / Private Test Installs (No Public Release Required)

For pre-release validation, install directly from a branch or commit SHA:

```bash
node setup-quaid.mjs --agent \
  --workspace "/absolute/path/to/workspace" \
  --owner-name "<Person Name>" \
  --source github \
  --ref canary
```

Pin to an exact commit for reproducible tests:

```bash
node setup-quaid.mjs --agent \
  --workspace "/absolute/path/to/workspace" \
  --owner-name "<Person Name>" \
  --source github \
  --ref <commit-sha>
```

Artifact fallback (local file path or URL to a `.tar.gz` canary package):

```bash
node setup-quaid.mjs --agent \
  --workspace "/absolute/path/to/workspace" \
  --owner-name "<Person Name>" \
  --source artifact \
  --artifact "/path/to/quaid-plugin-<sha>.tar.gz"
```

## Instance ID (Silo Name)

The installer prompts for an **instance ID** — a short name for the memory silo (e.g. `claude-code`, `openclaw`, `codex`).
Two installs with the same ID share memory; different IDs get independent databases.
The ID becomes a subdirectory under `QUAID_HOME`: `<QUAID_HOME>/<instance-id>/`.

In agent/non-interactive mode the adapter-derived default is used without prompting.
To pre-set the instance ID and skip the prompt, set `QUAID_INSTANCE`.

## Shared Embeddings Config

On first install the installer writes a machine-wide embeddings config to:

```
<QUAID_HOME>/shared/config/memory.json
```

This file records the Ollama URL, embedding model, and embedding dimension so all instances on the same machine use the same model (required for cross-instance memory inspection). The rule is **first-install-wins**: if `shared/config/memory.json` already has an `ollama` block, subsequent installs skip writing it and inherit the existing setup.

To change the shared embedding model after install, edit `<QUAID_HOME>/shared/config/memory.json` directly and re-run `quaid doctor` to verify.

## Environment Variables (optional)

- `QUAID_HOME`: explicit workspace/home path override (highest priority). **Do NOT set this globally in your shell profile** (e.g., `~/.zshrc`) — the hooks/installer manage it per-invocation. Setting it globally causes all adapter instances to share the same silo, corrupting cross-adapter isolation.
- `QUAID_INSTANCE`: pre-set instance identifier; installer skips the instance prompt and uses this value directly (e.g. `openclaw`, `claude-code`, `codex`)
- `CLAWDBOT_WORKSPACE`: OpenClaw workspace hint (auto-detected when OpenClaw is installed)
- `QUAID_INSTALL_AGENT=1`: enable non-interactive installer defaults
- `QUAID_INSTALL_CLAUDE_CODE=1`: force installer into Claude Code adapter mode (equivalent to `--claude-code` flag)
- `QUAID_OWNER_NAME`: explicit human owner name for memory tagging
- `QUAID_INSTALL_SOURCE`: `local|github|artifact`
- `QUAID_INSTALL_REF`: git branch/tag/commit (for github source)
- `QUAID_INSTALL_GITHUB_REPO`: repo override (default `quaid-labs/quaid`)
- `QUAID_INSTALL_ARTIFACT`: local path or URL to `.tar.gz` (for artifact source)
- `QUAID_INSTALL_PROVIDER`: force LLM provider selection (e.g. `anthropic`, `claude-code`, `openai-compatible`)
- `QUAID_INSTALL_NOTIFY=0|1`: disable/enable installer progress notifications in agent mode
- `QUAID_INSTALL_NOTIFY_PROGRESS=0|1`: disable/enable step checkpoint notifications
- `QUAID_INSTALL_NOTIFY_COMPLETE=0|1`: disable/enable completion notification
- `QUAID_INSTALL_NOTIFY_CHANNEL`: force installer progress channel (for example `telegram`)
- `QUAID_INSTALL_NOTIFY_TARGET`: force installer progress target (for example `telegram:<chat_id>`)
- `QUAID_INSTALL_NOTIFY_ACCOUNT`: optional channel account override when using explicit channel/target

## Claude Code-specific Notes

- Pass `--claude-code` (or set `QUAID_INSTALL_CLAUDE_CODE=1`) to force the installer into Claude Code adapter mode.
  Without this flag, the installer auto-detects the platform. Provide it when running in a non-interactive or
  headless environment where auto-detection may be unreliable.
- The installer writes six hook entries to `~/.claude/settings.json`:
  `SessionStart`, `UserPromptSubmit`, `PreCompact`, `SessionEnd`, `SubagentStart`, `SubagentStop`.
- On macOS, the janitor is scheduled via a launchd plist (`~/Library/LaunchAgents/com.quaid.janitor.plist`).
  It runs `quaid janitor --task all --apply --time-budget 3600` at the configured hour (default 4:30 AM).
  Logs go to `<QUAID_HOME>/logs/janitor/launchd.log` and `launchd-err.log`.
  The plist embeds `QUAID_HOME` and `PYTHONPATH`. At runtime, the adapter reads the Claude Code OAuth token from `~/.claude/.credentials.json` (auto-refreshed by `hook-session-init`) or from `.auth-token` if pre-stored via `python3 config_cli.py set-auth` — no API key env var is needed.
  Check status: `launchctl list | grep quaid`
  Unload: `launchctl unload ~/Library/LaunchAgents/com.quaid.janitor.plist`
- The installer creates a per-instance identity directory at `<QUAID_HOME>/claude-code/identity/`
  for `USER.md`, `SOUL.md`, and `ENVIRONMENT.md`.

## OpenClaw-specific Notes

- Installer now attempts to auto-heal missing `agents.list` in `~/.openclaw/openclaw.json`.
- Installer sets `allowPromptInjection: true` in the plugin config so `before_prompt_build` can inject memory context into prompts.

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

The required execution order is strict:

- after reading this guide and `setup-quaid.mjs`, the next assistant reply must be the survey
- do not continue exploring source files instead of surfacing the survey
- do not run any install command before the survey is shown and the human approves
- if the prompt is thin and only says "follow AI-INSTALL.md", you still must produce the survey first

The list of required survey fields lives in `setup-quaid.mjs` under `AGENT_SURVEY_CONTRACT`.
Agents must derive the survey from that contract instead of reproducing a second field list here.

### Model Selection Guidance (Mandatory)

When discussing model choices with the user, explain the two-tier roles first:

- `Fast reasoning model`: cheaper/faster path for routing, reranking, and lightweight classification.
- `Deep reasoning model`: higher-quality path for extraction, review, and heavier synthesis work.

For supported provider lanes (Anthropic/OpenAI), Quaid provides suggested model defaults. In that case:

- include those deep/fast defaults in the pre-install survey
- let the user override if they want
- no extra model-selection discussion is required

### Notification Routing Guidance (Mandatory)

For OpenClaw installs, do not leave Quaid runtime notifications on implicit `last_used` during install.
The installer or agent should pin Quaid notifications to an explicit OpenClaw channel based on the active user route.

- include the planned notification routing channel in the pre-install survey
- if installer progress needs explicit delivery, set:
  - `QUAID_INSTALL_NOTIFY_CHANNEL`
  - `QUAID_INSTALL_NOTIFY_TARGET`
  - optionally `QUAID_INSTALL_NOTIFY_ACCOUNT`
- for Telegram-driven installs, the expected runtime channel is usually `telegram`
- the adapter is responsible for resolving that channel to the proper recent session target at send time

If the active OpenClaw user route cannot be determined, the agent must say so clearly before install instead of pretending notifications are guaranteed.

For non-OpenClaw installs:

- do not include notification routing channel in the survey
- do not mention OpenClaw channels, `last_used`, pending-route behavior, or
  other OpenClaw routing internals
- it is enough to report the notification verbosity/profile relevant to the
  target platform

If the user is using an unsupported provider/model lane for their gateway (for example Gemini, Kimi/K2.5, or other custom routes), Quaid does not provide suggested fast/deep defaults. In that case the agent must:

- tell the user manual fast/deep model selection is required
- discuss budget, latency, and quality tradeoffs briefly
- collect explicit deep and fast model IDs from the user before install

Only execute install after the user confirms or edits these values.
This is mandatory even when using `--agent` non-interactive mode.

For long-running installs, the agent must send a brief progress update before backgrounding:
"Install is running and may take 1-2 minutes; I'll report back when complete."

### Completion Notification (Mandatory)

Do not rely only on one long blocking process poll to detect completion.
On some runtimes (notably OpenClaw + Telegram), long polls can fail silently and the user receives no completion message.

Use one of these patterns:

- `Option A (recommended for OpenClaw/Telegram)`: run install in background, append `EXIT:<code>` to a log, and poll the log in short intervals. Send a channel message as soon as `EXIT:` appears.
- `Option B`: use short (15-20s) poll loops and explicitly post a completion summary when the process exits.

Never:

- Use a single blocking poll longer than 30s on Telegram/OpenClaw surfaces.
- Go silent after backgrounding.
- Assume polling wake-up is guaranteed across runtimes.

For embeddings specifically: agents must not silently default to degraded mode when Ollama is unavailable.
They must ask the user whether to install/start Ollama first, and only proceed degraded after explicit approval.

### macOS Memory Reporting Note

On macOS, installer RAM availability is estimated from `vm_stat` pages (`free + inactive + speculative + purgeable`), not just strictly free pages.
This can differ from Activity Monitor's displayed "free" number, and may differ from what users expect when cache/compression is active.
Agents should state which memory metric they used in the survey and confirm with the user before choosing a lower-tier embedding model.

When an AI agent runs install, it must explicitly report all selected options to the user, including values that were defaults.

Do not say only "install succeeded." Always include a compact options summary so the user can see what was chosen and what can be changed later.

Minimum required summary fields:

- Owner (`users.defaultOwner`)
- Adapter type (`adapter.type`)
- LLM provider (`models.llmProvider`) and selected deep/fast models (`models.deepReasoning`, `models.fastReasoning`)
- Notification settings (`notifications.level`, plus `notifications.janitor|extraction|retrieval.verbosity`)
- Notification routing channel (`notifications.<feature>.channel`)
- Embedding provider/model (`models.embeddingsProvider`, `ollama.embeddingModel`)
- Janitor apply mode/policies (`janitor.applyMode`, `janitor.approvalPolicies.*`)
- Janitor schedule choice (or explicit "not scheduled")

The summary should also tell the user where to edit settings:

- Interactive editor: `quaid config edit`
- Config file: `<QUAID_HOME>/<INSTANCE_ID>/config/memory.json` (use `quaid config path` to confirm the active path)

Do not present `memory`, `journal`, `projects`, or `workspace` as a survey field or configurable install choice.
Those systems are always on by policy and should only be described if the user explicitly asks.

## Verification

After install — OpenClaw adapter:

```bash
openclaw hooks list
```

After install — Claude Code adapter:

```bash
# Verify hooks are registered in ~/.claude/settings.json
cat ~/.claude/settings.json | python3 -c "import sys,json; h=json.load(sys.stdin).get('hooks',{}); print([k for k in h if 'quaid' in str(h[k]).lower()])"

# Run doctor
quaid doctor
```

Expected output from hooks check: `['SessionStart', 'UserPromptSubmit', 'PreCompact', 'SessionEnd', 'SubagentStart', 'SubagentStop']` (or similar — any subset present means hooks are wired).

If OpenClaw is unavailable, run standalone mode and set workspace via `--workspace`.
