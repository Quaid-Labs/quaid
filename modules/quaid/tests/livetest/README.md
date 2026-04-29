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
agent inside a remote tmux session on the host under test. The reason is that the
host under test may be faulty, and thel local environment is stable. We need our
testers running from a stable environment.

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
auth (no passphrase prompt). VM is reccomended

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
- This repo checked out on `main`

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
   | `remote.workspace` | Quaid workspace root on the remote. Use an absolute path, not `~` shorthand. |
   | `owner_name` | Your name — written into the Quaid identity files at install time |
   | `tester.cli` | Command used to start tester agents (default `codex --yolo`) |
   | `tester.model` | Model for tester agents (default `gpt-5.4-mini`) |
   | `tester.effort` | Reasoning effort for tester agents (default `medium`) |
   | `platforms.cc.auth_token_file` | Path to a file containing the Anthropic token Quaid should write into `~/.quaid/shared/auth/credentials.json` for CC daemon calls. |
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

CC has two separate auth requirements:

1. The interactive Claude CLI itself needs a valid local `~/.claude/.credentials.json`
   on the coordinator. Preflight copies that file to the run VM for real CC sessions.
   If the coordinator copy is missing, expired, or too close to expiry, preflight
   fails before launch. Default minimum remaining lifetime is 90 minutes; override
   only for emergency short runs with `LIVETEST_CC_OAUTH_MIN_TTL_SECONDS=0`.

2. Quaid's CC daemon needs an Anthropic token in
   `~/.quaid/shared/auth/credentials.json`. Set `platforms.cc.auth_token_file` in
   your config to a file containing that token (plain text, first line used).

Do not confuse those two surfaces. Refreshing the CLI login fixes `401` at CC
session start; `platforms.cc.auth_token_file` fixes Quaid's own Anthropic calls.

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
| `livetest-presnapshot-preflight.sh` | **Run before overnight loops or when platform drift is suspected.** Clones the current base VM, applies slow platform CLI upgrades plus final harness cleanup such as stale OpenClaw silo pruning, and refreshes the base snapshot only if maintenance changed the clone. |
| `livetest-preflight.sh` | **Run before every run.** Verifies remote ≠ local, checks SSH, warns on platform version drift without upgrading, wipes the remote, syncs the dev tree, seeds credentials, and starts platform services. Hard-aborts if the remote host matches the local machine. |
| `livetest-session-init.sh` | Create the canonical local `livetest` tmux session/windows, launch tester panes, open SSH panes to the remote, and start tester nudge loops. |
| `livetest-wipe.sh` | Wipe Quaid from the remote. `--platform all` for full wipe, `--platform cc` for CC-only wipe while OC is live. Called by preflight; can also be run standalone. |
| `livetest-platform-start.sh` | Start platform services on the remote (OC gateway + health check). Called by preflight; can also be run standalone. |
| `verify-cc-session-capture.sh` | Verify the CC lane created a real Claude transcript on the remote (hooks present, project instance pinned, fresh `~/.claude/projects/.../*.jsonl`, hook trace exists) before treating M2 as a runtime extraction issue. |
| `livetest-dashboard.sh` | Serve a local live-test dashboard at `dashboard.html`, reading `dashboard.log` (title + CSV matrix + notes). |
| `livetest-dashboard-new-run.sh` | Create/reset `dashboard.log` from `dashboard_template.log` for a new run. |
| `livetest-dashboard-autostart-install.sh` | Install/load a user LaunchAgent so dashboard starts automatically on login/system start (macOS). |
| `livetest-dashboard-autostart-uninstall.sh` | Unload/remove the dashboard LaunchAgent (macOS). |
| `tmux-msg.sh` | Direct pane message delivery. Use normal mode for inter-agent messages and `--no-chrome` for CC/CDX user-visible test content. |
| `tmux-mailbox.sh` | Queue-backed mailbox for routine STATUS/ISSUE traffic. The first unread item is delivered inline when a queue goes from empty to non-empty; the coordinator then uses `reply` or `done` to acknowledge the current item and pull the next one. Mailbox data lives in `tests/livetest/scripts/.tmux-mailbox/` and is gitignored. |
| `livetest-nudge.sh` | Keepalive loop that periodically nudges a tester window. The active coordinator starts and owns one per tester at run start. Do not route these through window `5` / `claude-looper`. |
| `autonomous_mode.sh` | General-purpose nudge loop for any pane (`main:N.0` preferred). Writes structured telemetry to `/tmp/autonomous_mode_<target>.status.json`. |

All scripts that touch the remote accept `--dry-run` to print SSH commands without
executing them, and `--config <path>` to override the default config location.

---

## Live Dashboard

Use the lightweight dashboard to monitor run progress from `dashboard.log`.

Start it:

```bash
cd ~/quaidcode/dev/modules/quaid
tests/livetest/scripts/livetest-dashboard.sh
```

Open:

```text
http://127.0.0.1:8766/dashboard.html
```

Dashboard data files:
- `tests/livetest/dashboard.log` (active run file, gitignored)
- `tests/livetest/dashboard_template.log` (template copied for each new run)

Expected log format:
- First non-empty line: run title (for example `Run 110 - Frozen Validation`)
- Then a CSV matrix:
  - First column = milestone label
  - Remaining columns = platform columns (dynamic N columns from CSV header)
- Optional notes section starts at a line matching one of:
  - `---`
  - `Notes:`
  - `[notes]`
  - `## Notes`

Start a fresh run file from the template:

```bash
cd ~/quaidcode/dev/modules/quaid
tests/livetest/scripts/livetest-dashboard-new-run.sh --force --title "Run 110 - Frozen Validation"
```

CSV format:
- Header must be `milestone,<platform1>,<platform2>,...`
- Use one row per milestone
- Current default template includes `M1`-`M16` plus `XP1`-`XP3`
- Dashboard UI shows short built-in captions for `M1`-`M16` and `XP1`-`XP3`
- Optional `#` comment lines are ignored (template uses these for milestone hints)
- Status text is freeform (`PASS`, `FAIL`, `RUNNING`, `BLOCKED`, etc.)
- Notes go after `---` in freeform text

Example:

```text
Run XXX - Frozen Validation
milestone,OC,CC,CDX
M1,PASS,IN_PROGRESS,RUNNING
M2,,,
M3,BLOCKED - waiting on provider fix,PASS,PASS
M4,,,
M5,,,
M6,,,
M7,,,
M8,,,
M9,,,
M10,,,
M11,,,
M12,,,
M13,,,
M14,,,
M15,,,
M16,,,
XP1,,,
XP2,,,
XP3,,,
---
Notes:
- M10 blocked on docs update timeout in CC
- Waiting for W6 review on fix commit abc123
```

Template files:
- `tests/livetest/dashboard_template.log` (primary)
- `tests/livetest/current_run.log.example` (legacy compatibility)

Autostart on macOS (user LaunchAgent):

```bash
cd ~/quaidcode/dev/modules/quaid
tests/livetest/scripts/livetest-dashboard-autostart-install.sh --port 8766
```

Remove autostart:

```bash
cd ~/quaidcode/dev/modules/quaid
tests/livetest/scripts/livetest-dashboard-autostart-uninstall.sh
```

---

## Autonomous Mode (Nudge)

Use explicit pane targets first (for example, `main:3.0`, `main:4.0`).
Alias names are optional and loaded from local config:
`tests/livetest/scripts/.tmux-targets.json` (gitignored).
Use `tests/livetest/scripts/.tmux-targets.example.json` as the template.

Launch (default): script auto-detaches with `nohup` and survives transient
launch shells:

```bash
LIVETEST_DIR=~/quaidcode/dev/modules/quaid/tests/livetest/scripts
"$LIVETEST_DIR/autonomous_mode.sh" -w main:3.0 -t 300
"$LIVETEST_DIR/autonomous_mode.sh" -w main:4.0 -t 300
```

Foreground/debug mode:

```bash
"$LIVETEST_DIR/autonomous_mode.sh" -w main:3.0 -t 300 -f
```

Stop loops:

```bash
kill "$(cat /tmp/autonomous_mode_main_3.0_.pid)" "$(cat /tmp/autonomous_mode_main_4.0_.pid)"
```

Telemetry/debug files:
- PID: `/tmp/autonomous_mode_<target>.pid`
- Log: `/tmp/autonomous_mode_<target>.log`
- Trace: `/tmp/autonomous_mode_<target>.trace.log`
- Status JSON: `/tmp/autonomous_mode_<target>.status.json`

The status JSON is the first place to check for drops (`state`, `stop_reason`,
`exit_code`, `last_send_rc`, `last_outcome`).

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
     the main-branch AI install guide, verifies install quality.
   - **M1+** — Testers run the milestone suite on all three platforms in
     parallel (after M0 passes).
   - **XP** — Cross-platform project linking test (after all platforms finish Milestones).
   - **Commit check** — If any commits were made during the run, the loop repeats.
     The run is only complete when a full suite passes with zero new commits. After every
     cycle the coordinator should generate a report of all commits made and any ongoing issues,
     this should be sent as an attachment if telegram configured

4. On completion the deploys to the remote, and sends a notification if configured.

---

## Milestone Summary

Full milestone definitions are in `tests/livetest/livetest-guide/`.

| Milestone | What it tests |
|-----------|---------------|
| M0 | Agent-driven install from main |
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
| M13 | Distillation and identity-file quality checks |
| M14 | Docs-first guidance and retrieval behavior |
| M15 | Failure-signal quality (provider/router warning hygiene) |
| M16 | System-context refresh on lifecycle events |
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
platform showed the pre-install survey, confirmed main-branch install provenance,
and emitted install status messages in the platform pane. Missing survey,
ambiguous source, or silent install is a failure signal, not a pass. Do not
paper over a silent installer by asking the platform agent to narrate progress.

**CDX recall uses file browsing instead of Quaid** — Launch CDX with
`QUAID_INSTANCE=<instance_name> codex --yolo` so the agent's shell environment
inherits the instance identifier. Without it, autonomous `quaid recall` calls
search the wrong silo.
