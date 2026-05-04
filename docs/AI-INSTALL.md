# AI Install Guide

This guide is for AI agents installing Quaid on behalf of a human.

Your goal is not to explore the repo. Your goal is to run Quaid's installer with the correct platform, owner, and model settings after a short human-approved survey.

## Human Prompt To Give An Agent

If you are a human asking an AI agent to install Quaid, give it this prompt:

```text
Install Quaid for me.

First read and follow docs/AI-INSTALL.md exactly. Run the mandatory pre-install survey before installing anything. Use the platform/adapter for the agent you are currently running in unless I specify another one. Use my name as the owner name; if you do not know it, ask me before the survey. Show me the survey, wait for my approval, then install Quaid and run the health checks.
```

If the agent is reading this guide from GitHub instead of a local checkout, tell it to use the public guide:

```text
Read and follow https://github.com/quaid-labs/quaid/blob/main/docs/AI-INSTALL.md before installing Quaid.
```

## Mandatory First Action For Agents

Before browsing code or planning aloud, run the installer survey from the Quaid repo root:

```bash
cd "<repo-root-containing-this-guide>" && \
node setup-quaid.mjs --agent --dry-run --survey \
  --adapter <target-platform> \
  --owner-name "<owner-name>"
```

Use that survey output as your first conversational reply. Do not inspect adapter code, config files, project files, or unrelated docs before showing the survey.

If this guide is local inside an already-cloned Quaid checkout, that checkout is the install source. Run the command from that repo. Do not `git clone` manually.

Local dev checkout exception:

```bash
cd ~/quaidcode/dev && \
QUAID_ALLOW_DEV_INSTALL=1 node setup-quaid.mjs --agent --dry-run --survey \
  --adapter <target-platform> \
  --owner-name "<owner-name>"
```

Use `QUAID_ALLOW_DEV_INSTALL=1` only for intentionally synced dev-checkout installs such as live-test VMs. Do not collapse `~/quaidcode/dev` to `~/quaidcode`.

## Strict Execution Order

1. Read this guide.
2. Read `setup-quaid.mjs` only enough to follow `AGENT_SURVEY_CONTRACT`.
3. Run the survey command with `--agent --dry-run --survey`.
4. Send the survey output to the human as the next reply.
5. Wait for approval or edits.
6. Run the same install path without `--dry-run --survey`.
7. Run the platform health checks.
8. Send a compact completion summary with the selected options and health-check result.

Do not run install before approval. Do not continue exploring once you have enough information to show the survey.

## Pre-Survey Scope

Allowed before the survey:

- `docs/AI-INSTALL.md`
- `setup-quaid.mjs`
- minimal checks needed to fill the survey accurately, such as:
  - `command -v ollama`
  - `curl http://localhost:11434/api/tags`
  - `vm_stat`
  - `sysctl -n hw.memsize`

Do not run broad exploration before the survey:

- no `find`
- no broad repo search
- no adapter source inspection
- no config-file spelunking
- no unrelated docs browsing
- no exploratory Python snippets

If the prompt already gives the adapter/platform and owner name, run the survey command immediately after reading enough of this guide to know the command shape.

## Platform Selection

If the human did not specify a platform, install Quaid for the platform currently running the agent/session that is following this guide.

Examples:

- Agent running inside Codex -> `--adapter codex`
- Agent running inside Claude Code -> `--adapter claude-code`
- Agent running inside OpenClaw -> `--adapter openclaw`

Do not switch to another installed platform just because it is detected. On hosts with multiple platforms installed, pass the intended platform explicitly.

If the human wants Quaid wired into every detected host, use:

```bash
node setup-quaid.mjs --all-platforms
```

This runs the same per-platform install flow sequentially. Only the first platform prompts for shared credentials; later platforms reuse the shared credential store.

## Fixed Home Layout

Quaid uses a fixed split layout:

- hidden system home: `~/.quaid` (`QUAID_HOME`)
- visible user-facing home: `~/quaid` (`QUAID_VISIBLE_HOME`)

Do not ask the human to choose an install workspace. Do not pass a custom `--workspace` during normal installs. Do not present memory, journal, projects, or workspace as survey fields; those systems are always on.

## Survey Requirements

The survey fields live in `setup-quaid.mjs` under `AGENT_SURVEY_CONTRACT`. Use that as the source of truth.

The first assistant response must be the survey itself. Do not add planning text before it. Prefer the exact output from:

```bash
node setup-quaid.mjs --agent --dry-run --survey ...
```

Survey output must:

- keep the field order aligned with `AGENT_SURVEY_CONTRACT.fields`
- show selected values, including defaults
- include compatibility notices
- end with: `Do you want to change any of these before I run install?`

For non-OpenClaw installs, omit OpenClaw-only routing fields and do not mention OpenClaw channels, `last_used`, gateway routing, or pairing details.

## Model Selection

When discussing model choices, explain the two Quaid roles briefly:

- `Fast reasoning model`: cheaper/faster path for routing, reranking, and lightweight classification.
- `Deep reasoning model`: higher-quality path for extraction, review, and heavier synthesis.

For supported provider lanes, Quaid provides suggested defaults. Include those defaults in the survey and let the human override them.

If the target gateway uses an unsupported or custom provider/model lane, Quaid cannot infer safe defaults. Ask the human for explicit deep and fast model IDs before install.

## Embeddings And Ollama

Do not silently proceed in degraded mode when Ollama is unavailable. Ask whether to install/start Ollama first, and proceed degraded only after explicit approval.

On macOS, RAM availability is estimated from `vm_stat` pages (`free + inactive + speculative + purgeable`). If you mention memory constraints in the survey, say which metric you used.

## Notification Routing

For OpenClaw installs, include the runtime notification channel in the survey. If the installer detects an active route, report the explicit route. If it cannot, say plainly that the installer will fall back to `last_used`.

If explicit installer progress delivery is required, set:

- `QUAID_INSTALL_NOTIFY_CHANNEL`
- `QUAID_INSTALL_NOTIFY_TARGET`
- `QUAID_INSTALL_NOTIFY_ACCOUNT` when needed

For non-OpenClaw installs, report only the notification level/verbosity relevant to that platform.

## Running The Install

After the human approves the survey, run the same installer path without `--dry-run --survey`.

Recommended agent-driven release install:

```bash
node setup-quaid.mjs --agent \
  --owner-name "<Person Name>" \
  --source github
```

`--source github` fetches the latest release and manages its own temporary clone. Do not manually clone Quaid first.

For pre-release validation, pin a branch or commit:

```bash
node setup-quaid.mjs --agent \
  --owner-name "<Person Name>" \
  --source github \
  --ref <branch-or-commit>
```

Artifact install:

```bash
node setup-quaid.mjs --agent \
  --owner-name "<Person Name>" \
  --source artifact \
  --artifact "/path/to/quaid-plugin-<sha>.tar.gz"
```

## Long-Running Install Communication

Before starting a long install, say:

```text
Install is running and may take 1-2 minutes; I'll report back when complete.
```

Do not rely on one long blocking poll, especially on OpenClaw/Telegram. Use short polling loops or run the install in the background and watch a log that records `EXIT:<code>`. Send a completion message as soon as the process exits.

Never go silent after backgrounding an install.

## Platform Notes

### Claude Code

- Pass `--adapter claude-code` or `--claude-code` when auto-detection may be unreliable.
- The installer writes hooks to `~/.claude/settings.json` for session start, prompt submit, pre-compact, session end, and subagents.
- Quaid background calls read Anthropic credentials from `~/.quaid/shared/auth/credentials.json` or explicit env vars, not from Claude Code's private credential files.

### Codex

- Pass `--adapter codex` when auto-detection may be unreliable.
- Do not add `QUAID_INSTANCE` shell exports or shell-rc edits after install.

### OpenClaw

- Pass `--adapter openclaw` when auto-detection may be unreliable.
- The installer registers the Quaid plugin, writes runtime instance env into OpenClaw config, seeds current Matrix channel schema when applicable, and waits for the gateway to return online.
- OpenClaw installs require explicit Quaid background-call credentials. Anthropic is recommended in alpha; OpenAI lanes are available but experimental and benchmark materially below Anthropic for Quaid memory quality.

## Verification

After install, run the platform health checks that apply.

OpenClaw:

```bash
openclaw hooks list
quaid doctor
```

Claude Code:

```bash
cat ~/.claude/settings.json | python3 -c "import sys,json; h=json.load(sys.stdin).get('hooks',{}); print([k for k in h if 'quaid' in str(h[k]).lower()])"
quaid doctor
```

Codex:

```bash
quaid doctor
```

## Completion Summary

Do not say only `install succeeded`. Always include a compact summary of what was chosen.

Minimum summary fields:

- owner (`users.defaultOwner`)
- adapter type (`adapter.type`)
- LLM provider and selected deep/fast models
- embeddings provider/model
- notification level and per-feature verbosity
- notification routing channel for OpenClaw installs
- platform compatibility notices
- health-check result

Do not tell the user to edit Quaid config directly, add shell-profile exports, or add anything to shell rc files. If post-install config changes are needed, say:

```text
It is best to use your agents for any Quaid config changes.
```

Janitor behavior is automatic by default. Do not ask for separate permission to enable or run it unless the human explicitly asks to change janitor behavior.

## Useful Environment Variables

- `QUAID_HOME`: hidden runtime home; normal installs use `~/.quaid`
- `QUAID_VISIBLE_HOME`: visible user-facing home; normal installs use `~/quaid`
- `QUAID_INSTANCE`: explicit instance identifier override, when intentionally needed
- `QUAID_INSTALL_AGENT=1`: enable non-interactive installer defaults
- `QUAID_OWNER_NAME`: explicit human owner name
- `QUAID_INSTALL_SOURCE`: `local|github|artifact`
- `QUAID_INSTALL_REF`: git branch, tag, or commit for GitHub source
- `QUAID_INSTALL_GITHUB_REPO`: repo override, default `quaid-labs/quaid`
- `QUAID_INSTALL_ARTIFACT`: local path or URL to `.tar.gz`
- `QUAID_INSTALL_PROVIDER`: force LLM provider selection when supported by the adapter
- `QUAID_INSTALL_NOTIFY=0|1`: disable/enable installer progress notifications
- `QUAID_INSTALL_NOTIFY_PROGRESS=0|1`: disable/enable step checkpoint notifications
- `QUAID_INSTALL_NOTIFY_COMPLETE=0|1`: disable/enable completion notification
- `QUAID_INSTALL_NOTIFY_CHANNEL`: force installer progress channel
- `QUAID_INSTALL_NOTIFY_TARGET`: force installer progress target
- `QUAID_INSTALL_NOTIFY_ACCOUNT`: optional channel account override
