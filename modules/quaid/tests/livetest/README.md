# Quaid Live Test Suite

The live test suite validates Quaid end-to-end on real platforms (OpenClaw, Claude
Code, and Codex CLI) installed on a remote host. It uses a coordinator-plus-testers
model where an AI agent drives each role.

---

## Architecture

```
Your machine (coordinator)
├── Coordinator agent — reads COORDINATOR.SKILL.md, manages the run loop
├── livetest:OC-tester  — tester agent driving OC milestones
├── livetest:CC-tester  — tester agent driving CC milestones
└── livetest:CDX-tester — tester agent driving CDX milestones

Remote host (platforms under test)
├── OpenClaw (openclaw tui / openclaw agent)
├── Claude Code (claude --dangerously-skip-permissions)
└── Codex CLI (codex --yolo)
```

The coordinator manages the run loop: wipe, install, milestones, commit check,
repeat until a full suite passes with zero new commits. Tester agents execute
milestones on each platform and report back.

---

## Prerequisites

### On your machine (coordinator)

- `tmux` installed
- SSH access to the remote host (key-based, no passphrase prompt)
- The tester agent CLI available (`codex --yolo` by default — change in config)
- This repo checked out on `canary`

### On the remote host

All three platform CLIs must be installed and logged in before running the suite:

| Platform | CLI | Login requirement |
|----------|-----|------------------|
| OpenClaw | `openclaw` | Logged in; gateway starts cleanly |
| Claude Code | `claude` | Logged in with valid session |
| Codex CLI | `codex` | Logged in with valid OpenAI session |

Quaid does **not** need to be pre-installed on the remote — the live test installs
it as part of M0.

The remote host needs:
- `node` (v18+) and `npm`
- `python3` (3.10+)
- `sqlite3` CLI
- `git`
- `tmux` (optional — all platform interaction goes through SSH from your machine)

---

## Configuration

1. Copy the template:
   ```bash
   cp tests/livetest/livetest-config.template.json tests/livetest/livetest-config.json
   ```

2. Edit `livetest-config.json`:

   | Key | What to set |
   |-----|-------------|
   | `remote.host` | SSH hostname of your test machine (e.g. `testbox.local`) |
   | `remote.workspace` | Quaid workspace root on the remote (default `~/quaid`) |
   | `owner_name` | Your name — written into the Quaid identity files at install time |
   | `tester.cli` | Command used to start tester agents (default `codex --yolo`) |
   | `tester.model` | Model for tester agents (default `gpt-5.1-codex-mini`) |
   | `tester.effort` | Reasoning effort for tester agents (default `medium`) |
   | `platforms.cc.auth_token_file` | Path to a file containing your Anthropic API token (plain text, no newline). Required for the CC daemon's LLM calls. |
   | `tmux.coordinator_pane` | The tmux pane where the coordinator runs (default `main:4.0`) |

   `livetest-config.json` is gitignored — it will never be committed.

3. Verify SSH works:
   ```bash
   ssh your-test-machine.local 'echo ok'
   ```

---

## Auth Tokens and Keys

The CC platform daemon makes direct LLM calls using an Anthropic OAuth token.
Set `platforms.cc.auth_token_file` in your config to a file containing the token
(no newline, mode 600). The coordinator writes this to the remote after CC install.

No other keys need to be in the config file. Platform CLIs handle their own auth
through their normal login flows (already completed in the prerequisites step).

---

## tmux Session Layout

The coordinator creates and manages a `livetest` tmux session:

| Window | Name | Role |
|--------|------|------|
| `livetest:CC-tester` | CC-tester | Tester agent driving CC milestones |
| `livetest:CC` | CC | Visible CC interaction pane (SSH to remote, runs `claude`) |
| `livetest:OC-tester` | OC-tester | Tester agent driving OC milestones |
| `livetest:OC` | OC | Visible OC interaction pane (SSH to remote, runs `openclaw tui`) |
| `livetest:CDX-tester` | CDX-tester | Tester agent driving CDX milestones |
| `livetest:CDX` | CDX | Visible CDX interaction pane (SSH to remote, runs `codex --yolo`) |

Tester agents run in the `-tester` windows. Platform interaction happens in the
adjacent windows so the coordinator can observe each session live.

---

## Running a Live Test

1. Open the coordinator skill in your agent of choice:
   - **Claude Code:** Add `tests/livetest/COORDINATOR.SKILL.md` as a skill or
     paste its contents as the opening prompt.
   - **Codex or other:** Point the agent at the file at session start.

2. The coordinator reads your `livetest-config.json`, sets up the tmux session,
   and begins the run loop automatically.

3. The run loop:
   - **M0** — Wipes the remote, tells each platform to self-install Quaid from
     the canary AI install guide, verifies install quality.
   - **M1–M13** — Testers run the milestone suite on all three platforms in
     parallel (after M0 passes).
   - **XP** — Cross-platform project linking test (after all platforms reach M13).
   - **Commit check** — If any commits were made during the run, the loop repeats.
     The run is only complete when a full suite passes with zero new commits.

4. On completion the coordinator pushes canary, deploys to the remote, and
   sends a notification if configured.

---

## Milestone Summary

Full milestone definitions are in `tests/LIVE-TEST-GUIDE.md`.

| Milestone | What it tests |
|-----------|---------------|
| M0 | Agent-driven install from canary |
| M1 | Basic store and injection |
| M2 | Multi-turn extraction and graph edges |
| M3 | Compact / reset extraction trigger |
| M4 | Inactivity timeout extraction |
| M5 | Auto-inject into a new session |
| M6 | Deliberate recall (multi-hop graph) |
| M7 | Graph traversal and edge verification |
| M8 | Project system CRUD |
| M9 | Janitor maintenance pass |
| M10 | Docs registration and RAG search |
| M11 | Doc update pipeline |
| M12 | Cross-session recall stability |
| M13 | Full-suite smoke (store → compact → recall → graph) |
| XP | Cross-platform project linking and shared doc recall |

---

## What Gets Committed During a Run

The coordinator logs all commits made during a run in `unreviewed-commits.md` in
the agent workspace. Any commit triggers a mandatory re-run. Review committed
changes after the run completes.

---

## Troubleshooting

**SSH hangs on first command** — Check that key-based auth is set up and the
remote shell profile does not print output (common with `.zshrc` completion noise).

**Tester agent runs out of context** — Kill the tester window and relaunch with
the tester CLI from `livetest-config.json`. Send the tester its SKILL.md and the
current milestone on first message.

**Platform install silent / no output** — M0 explicitly checks that install
messages appeared in the platform pane. A silent install is a failure signal, not
a pass. Escalate to the coordinator for investigation.

**CDX recall uses file browsing instead of Quaid** — Launch CDX with
`QUAID_INSTANCE=<instance_name> codex --yolo` so the agent's shell environment
inherits the instance identifier. Without it, autonomous `quaid recall` calls
search the wrong silo.
