# Quaid Live Test Suite

The live test suite validates Quaid end-to-end on real platforms (OpenClaw, Claude
Code, and Codex CLI) installed on a remote host. It uses a coordinator-plus-testers
model where an AI agent drives each role.

---

## Architecture

```
Your machine (coordinator)
├── canonical tmux session: livetest
├── Coordinator agent — reads COORDINATOR.SKILL.md, manages the run loop
├── livetest:CC   — split window
│   ├── left pane  = local tester agent
│   └── right pane = local SSH shell into remote Claude Code lane
├── livetest:OC   — split window
│   ├── left pane  = local tester agent
│   └── right pane = local SSH shell into remote OpenClaw lane
└── livetest:CDX  — split window
    ├── left pane  = local tester agent
    └── right pane = local SSH shell into remote Codex lane

Remote host (platforms under test)
├── OpenClaw under test
├── Claude Code under test
└── Codex CLI under test
```

The coordinator manages the run loop: wipe, install, milestones, commit check,
repeat until a full suite passes with zero new commits. Tester agents execute
milestones on each platform and report back.

**Critical rule:** the tester agents do **not** run on the remote host. They run
locally, inside the local `livetest` tmux session. The visible platform panes are
also local tmux panes; they reach the remote host via `ssh`. Do not run a tester
agent inside a remote tmux session on the host under test.

### Why a dedicated remote host (required)

**The platforms under test must run on a separate machine from the coordinator.**
This is a hard requirement, not a suggestion.

The remote host will be wiped and reinstalled on every run — sometimes multiple
times per session. Between wipes it may be running broken, partially-installed,
or otherwise unstable code. Do not use a machine you care about.

Specific reasons this must be a separate machine:

- **Wipe safety**: `livetest-preflight.sh` performs a full destructive wipe of the
  Quaid workspace, extension directories, and session history before each run.
  It will refuse to run if the remote and local hostnames match, but there is
  no substitute for using a machine you can afford to nuke.
- **Isolation**: A platform crash, runaway extraction daemon, or model timeout on
  the test machine cannot affect the coordinator or tester agents on your machine.
- **Clean hook state**: CC and CDX write hook config and session history to `$HOME`
  paths. On a shared machine those collide with your live working session.
- **Correct silo routing**: Platform instances use `$HOME`-relative paths for
  their config. On a dedicated remote, each test silo is the only one, so
  instance routing is unambiguous.

A lightweight VM, cloud instance, or spare machine works fine. It only needs
the three platform CLIs installed, logged in, and reachable via SSH with key-based
auth (no passphrase prompt).

---

## Prerequisites

### On your machine (coordinator)

- `tmux` installed — **the coordinator must run inside a tmux pane.** The
  inter-agent messaging system (`scripts/tmux-msg.sh`) sends messages by
  writing to tmux panes. If the coordinator is not in a tmux session, testers
  cannot message it back and the run will stall. Start a tmux session before
  launching the coordinator agent.
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
   | `tmux.layout` | Must be `split-panes` for the canonical live-test topology |
   | `tmux.tester_side` | Must be `left` for the local tester pane |
   | `tmux.platform_side` | Must be `right` for the visible SSH-backed platform pane |
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

The coordinator creates and manages a canonical local tmux session named
`livetest`.

This is not optional. Even a single-lane run must use the `livetest` session so
operator screens and attach commands remain stable across runs.

Canonical attach command:
```bash
tmux new-session -A -s livetest
```

The coordinator creates and manages the local `livetest` tmux session:

| Window | Left Pane | Right Pane |
|--------|-----------|------------|
| `livetest:CC` | Local tester agent | Local SSH shell into remote `claude` lane |
| `livetest:OC` | Local tester agent | Local SSH shell into remote `openclaw` lane |
| `livetest:CDX` | Local tester agent | Local SSH shell into remote `codex` lane |

This split-pane layout is the canonical live-test topology. The left pane is
always the local tester agent. The right pane is always the visible SSH-backed
platform lane under test. Do not invert them.

Do not invent alternate session names such as `codex-live` for ad hoc single-lane
runs. Keep the session name canonical and use the same split-pane structure for
single-lane and full-suite runs.

Do not make a remote tmux session canonical. Remote tmux can be used for ad hoc
inspection if needed, but live-test runner/control panes must remain local.

---

## Scripts

Bundled scripts in `tests/livetest/scripts/`. All remote-touching scripts run
exclusively via SSH — they cannot accidentally affect the local machine.

| Script | Purpose |
|--------|---------|
| `livetest-preflight.sh` | **Run before every run.** Verifies remote ≠ local, checks SSH, wipes the remote, starts platform services. Hard-aborts if the remote host matches the local machine. |
| `livetest-wipe.sh` | Wipe Quaid from the remote. `--platform all` for full wipe, `--platform cc` for CC-only wipe while OC is live. Called by preflight; can also be run standalone. |
| `livetest-platform-start.sh` | Start platform services on the remote (OC gateway + health check). Called by preflight; can also be run standalone. |
| `tmux-msg.sh` | Send a message to a coordinator or tester pane. Handles quoting, copy-mode, and stale input clearing. |
| `livetest-nudge.sh` | Keepalive loop that periodically nudges a tester window. The active coordinator starts and owns one per tester at run start. Do not route these through window `5` / `claude-looper`. |

All scripts that touch the remote accept `--dry-run` to print SSH commands without
executing them, and `--config <path>` to override the default config location.

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

**Platform install silent / no output** — M0 explicitly checks that the
platform showed the pre-install survey, confirmed canary install provenance,
and emitted install status messages in the platform pane. Missing survey,
ambiguous source, or silent install is a failure signal, not a pass.

**CDX recall uses file browsing instead of Quaid** — Launch CDX with
`QUAID_INSTANCE=<instance_name> codex --yolo` so the agent's shell environment
inherits the instance identifier. Without it, autonomous `quaid recall` calls
search the wrong silo.
