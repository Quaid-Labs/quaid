# Quaid Live Test Guide

Full procedure for running a 3-platform live validation of Quaid. All commands
use placeholders defined in `livetest-config.json`. Keep private hostnames,
auth tokens, and operator identities in an untracked companion file such as
`LIVE-TEST-GUIDE.local.md`.

## Placeholders

These are read from `tests/livetest/livetest-config.json`:

| Placeholder | Meaning |
|-------------|---------|
| `REMOTE_HOST` | Test VM/host (SSH target) |
| `WORKSPACE` | Quaid home on remote (`~/quaid`) |
| `OC_INSTANCE` | OC silo name (e.g. `openclaw-livetest`) |
| `CC_INSTANCE` | CC silo name (e.g. `claude-code-livetest`) |
| `CDX_INSTANCE` | CDX silo name (e.g. `codex-livetest`) |
| `OWNER_NAME` | Owner name for install |
| `DEV_ROOT` | Local dev tree root |
| `CC_PROJECT_DIR` | CC project directory on remote |
| `CDX_PROJECT_DIR` | CDX project directory on remote |
| `TESTER_CLI` | CLI to launch tester agents |

## Public Contract

- Run live validation from `canary`.
- Use real host adapters and visible interaction panes.
- Do not mock runtime behavior or patch code during the live run.
- Treat failures as product bugs, not as reasons to relax the milestone.

## Test Integrity Rules

This is black-box testing:
- No direct function calls, no imports into runtime codepaths.
- No mocks, no code edits during the live test.
- All live agent interaction must happen through a visible interaction pane to
  simulate a real user.

**No manual signal injection.** Do not manually write signal files
(`session_end`, `rolling`, or any other extraction signal) to make a milestone
pass. If the runtime is not writing the signal, that is the bug to fix.

**No test corruption.** A failure is a signal. Fix what is broken — do not make
the test easier to pass. Wrong responses to a failure:
- Relaxing a criterion because it is hard to satisfy
- Hardcoding values to force a specific identity
- Skipping safety checks because they fail
- Ruling PASS-WITH-NOTE to avoid doing work

**Investigate before deferring.** When a failure occurs: reproduce, diagnose,
fix. "Fix later" is only valid when fixing requires the operator. For anything
else, fix it now — you have maximum context while the break is live.

**Install prompt format.** The M0 install prompt must contain ONLY: the
AI-INSTALL.md guide path, the adapter/platform/instance/owner parameters, and a
one-line instruction. No agentic scaffolding, no survey templates, no multi-step
chains. The agent reads the guide and installs itself.

## Role Responsibilities

### Livetester

- Follow milestone steps in order, exactly as written.
- Report every failure and anomaly to the coordinator as an ISSUE message.
- Proactively surface diagnostic data (logs, DB counts, daemon status, pane
  captures) with every ISSUE.
- Pause and wait for coordinator go-ahead before proceeding past a failure.
- **Must not**: attempt fixes, write to DBs/signals/config (unless the milestone
  says to), inject signals, or mark PASS-WITH-NOTE to avoid blocking.

### Coordinator

- Owns test infrastructure: wipe, silo init, tmux setup, daemon lifecycle, deploy.
- Keeps livetesters running and on task. Stalled lanes are coordinator failures.
- Enforces role boundaries — watch for unauthorized writes or test corruption.
- Audits tester commands for local machine writes (all commands must target REMOTE_HOST).
- Maintains a live issue queue across all lanes.
- Pushes tests to completion. A run with commits requires a full restart from M0.
- Performs forensics independently (SSH to host, check DB/logs/config) before
  acting on tester reports.

---

## Step 0 — Environment Setup

### tmux Session

`livetest` is the canonical local tmux session name. One window per platform,
each split into two panes: left = local tester agent, right = SSH shell to remote.

```bash
tmux has-session -t livetest 2>/dev/null || tmux new-session -d -s livetest -n CC
tmux list-windows -t livetest | grep -q 'CC$'  || tmux new-window -t livetest -n CC
tmux list-windows -t livetest | grep -q 'OC$'  || tmux new-window -t livetest -n OC
tmux list-windows -t livetest | grep -q 'CDX$' || tmux new-window -t livetest -n CDX

for win in CC OC CDX; do
  if [ "$(tmux list-panes -t livetest:$win | wc -l | tr -d ' ')" -lt 2 ]; then
    tmux split-window -h -t livetest:$win
  fi
  tmux select-layout -t livetest:$win even-horizontal
done
```

**Do not run tester agents on the remote host.** If the remote crashes, the
local tester must survive.

### Confirm Coordinator Pane

```bash
COORDINATOR_PANE=$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}')
```

Pass this to every tester at boot so they can send STATUS/ISSUE messages back.

### Scripts

Shipped with the livetest suite (relative to repo root):
- `tests/livetest/scripts/livetest-preflight.sh` — safety checks, wipe, platform start
- `tests/livetest/scripts/livetest-wipe.sh` — wipe Quaid from remote
- `tests/livetest/scripts/livetest-platform-start.sh` — start platform services
- `tests/livetest/scripts/tmux-msg.sh` — inter-agent messaging
- `tests/livetest/scripts/livetest-nudge.sh` — keepalive nudge loop

### Start Tester Agents

Launch tester agents in each left pane from the tester agent workspace:

```bash
tmux send-keys -t livetest:CC.0  "cd /path/to/agents/codex-livetester && TESTER_CLI" Enter
tmux send-keys -t livetest:OC.0  "cd /path/to/agents/codex-livetester && TESTER_CLI" Enter
tmux send-keys -t livetest:CDX.0 "cd /path/to/agents/codex-livetester && TESTER_CLI" Enter
```

On first message to each tester, send the general skill file (`TESTER.SKILL.md`)
and the platform supplement (`TESTER.OC.md`, `TESTER.CC.md`, or `TESTER.CDX.md`),
plus the coordinator pane address.

### Start Nudge Loops

```bash
LIVETEST_DIR=tests/livetest/scripts
$LIVETEST_DIR/livetest-nudge.sh -w livetest:CC.0  -r "Run N" &; CC_NUDGE=$!
$LIVETEST_DIR/livetest-nudge.sh -w livetest:OC.0  -r "Run N" &; OC_NUDGE=$!
$LIVETEST_DIR/livetest-nudge.sh -w livetest:CDX.0 -r "Run N" &; CDX_NUDGE=$!
```

Kill at run end: `kill $CC_NUDGE $OC_NUDGE $CDX_NUDGE 2>/dev/null`

### Open Platform SSH Panes

```bash
tmux send-keys -t livetest:OC.1  "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CC.1  "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CDX.1 "ssh REMOTE_HOST" Enter
```

---

## Step 1 — Preflight: Wipe, Safety Check, Code Sync

Run at the start of every run.

### Record Run Start SHA

```bash
cd DEV_ROOT && git rev-parse HEAD
```

Save as `RUN_START_SHA`.

### Run Preflight Script

```bash
tests/livetest/scripts/livetest-preflight.sh
```

The script: verifies the remote host is not the local machine (hard abort),
verifies SSH, wipes Quaid from remote (all silos, hooks, sessions, extensions),
starts the OC gateway, and waits for health.

For a CC-only wipe when OC is already live:
```bash
tests/livetest/scripts/livetest-preflight.sh --wipe-platform cc --skip-platform-start
```

### Verify Remote Code Is Current

```bash
LOCAL_SHA=$(cd DEV_ROOT && git rev-parse --short HEAD)
ssh REMOTE_HOST 'cd WORKSPACE/dev && git rev-parse --short HEAD'
```

If different, update and restart:
```bash
ssh REMOTE_HOST 'cd WORKSPACE/dev && git pull --ff-only origin canary'
```

### Full Wipe Details

The preflight script handles this, but the manual steps are:

1. **Kill all extraction daemons** (stale daemons cache embedding model at
   startup — survivors corrupt vec_nodes schema):
   ```bash
   ssh REMOTE_HOST 'pkill -9 -f extraction_daemon.py 2>/dev/null'
   ```

2. **Uninstall the OC plugin:**
   ```bash
   ssh REMOTE_HOST 'openclaw plugins uninstall quaid 2>/dev/null'
   ```

3. **Wipe workspace and extensions:**
   ```bash
   ssh REMOTE_HOST 'rm -rf WORKSPACE && rm -rf ~/.openclaw/extensions/quaid'
   ```

4. **Clear OC session transcripts** (stale sessions trigger extraction fan-out):
   ```bash
   ssh REMOTE_HOST 'rm -rf ~/.openclaw/agents/main/sessions/'
   ```

5. **Clear CC hooks and project history:**
   ```bash
   ssh REMOTE_HOST 'rm -f ~/.claude/rules/quaid-projects.md'
   ssh REMOTE_HOST 'rm -rf ~/.claude/projects/-private-tmp-cc-livetest'
   ```

6. **Clear CDX hooks:**
   ```bash
   ssh REMOTE_HOST 'echo "{}" > ~/.codex/hooks.json'
   ```

### Platform Version Checks

Before starting a run, verify platform versions are current:
```bash
ssh REMOTE_HOST 'echo "OC=$(openclaw --version) CC=$(claude --version | head -1) CDX=$(codex --version | head -1)"'
```

Update via the base snapshot if outdated — not the run VM.

### Build and Sync Source

```bash
cd DEV_ROOT/modules/quaid && npm run build:runtime
rsync -av --checksum \
  --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.git/' --exclude='logs/' --exclude='.env*' \
  DEV_ROOT/ REMOTE_HOST:~/quaid/dev/
```

---

## Step 2 — M0: Agent-Driven Install

### Install Order

Roll a random order for the three platforms each run. Complete one M0 PASS
before starting the next platform. Fixed order masks order-dependent bugs.

### Tester Dry-Run Validation

Each tester runs `--dry-run` before the real install:
```bash
ssh REMOTE_HOST 'cd ~/quaid/dev && QUAID_INSTANCE=OC_INSTANCE node setup-quaid.mjs \
  --dry-run --adapter openclaw --owner-name OWNER_NAME --agent 2>&1 | tail -40'
```

Verify: `platform` matches, `workspace` is `WORKSPACE`, `instanceId` matches.

### Coordinator Pre-Install Prep

**OC only** — ensure gateway is running and models are registered:
```bash
ssh REMOTE_HOST 'curl -sf http://localhost:18789/health && echo "ok" || echo "down"'
ssh REMOTE_HOST 'curl -sf http://localhost:18789/v1/models | python3 -c \
  "import json,sys; print([m[\"id\"] for m in json.load(sys.stdin).get(\"data\",[])])"'
```

**OC only** — disable exec approval gates for unattended install:
```bash
ssh REMOTE_HOST 'openclaw config set tools.exec.host gateway && \
  openclaw config set tools.exec.security full && \
  openclaw config set tools.exec.ask off && openclaw gateway restart'
```

**CC only** — clear stale Quaid hooks from `~/.claude/settings.json`.

**CC only** — write auth token before install (installer blocks until this exists):
```bash
ssh REMOTE_HOST "mkdir -p WORKSPACE/config/adapters/claude-code && \
  echo -n 'AUTH_TOKEN' > WORKSPACE/config/adapters/claude-code/.auth-token && \
  chmod 600 WORKSPACE/config/adapters/claude-code/.auth-token"
```

**CDX only** — verify no stale Quaid hooks in `~/.codex/hooks.json`.

### Agent-Driven Install Message

Send to each platform:

> Please install Quaid by following the AI install guide:
> `~/quaid/dev/docs/AI-INSTALL.md`
>
> Use these parameters:
> - Adapter/platform: PLATFORM
> - Instance name: INSTANCE_NAME
> - Owner name: OWNER_NAME
>
> Quaid installs into `~/quaid`; do not pass a custom workspace path.
> Tell me when Quaid is installed and `quaid doctor` returns healthy.

### Post-Install Examination

After each platform's M0 passes, verify the filesystem (see
`COORDINATOR.SKILL.md` for the full subagent prompt):

1. **`~/quaid` exists** — Quaid installs into the visible `~/quaid` home.
2. **`~/quaid` has correct structure**: `modules/quaid/`, `shared/config/`,
   `instances/INSTANCE/config/`, `instances/INSTANCE/data/`.
3. **Instance config has models and capture sections.**
4. **Shared platform config exists** at `~/quaid/shared/config/PLATFORM/`.
5. **Platform hooks registered** (CC: settings.json, CDX: hooks.json, OC:
   extensions symlink).
6. **No stale legacy paths** (`~/.quaid`, `~/quaid/config/memory.json`).

### Post-Install Coordinator Steps

**Overwrite deep lane with fast lane** (all silos — HARD RULE):
```bash
ssh REMOTE_HOST "python3 -c \"
import json; p = 'WORKSPACE/instances/INSTANCE/config/memory.json'
with open(p) as f: d = json.load(f)
d['models']['deepReasoning'] = d['models']['fastReasoning']
with open(p, 'w') as f: json.dump(d, f, indent=2)
print('deep set to', d['models']['fastReasoning'])
\""
```

**Set live-test chunk_tokens** (all silos — lowers rolling threshold for short tests):
```bash
ssh REMOTE_HOST "python3 -c \"
import json; p = 'WORKSPACE/instances/INSTANCE/config/memory.json'
with open(p) as f: d = json.load(f)
d.setdefault('capture', {})['chunk_tokens'] = 1500
with open(p, 'w') as f: json.dump(d, f, indent=2)
\""
```

**CDX only** — respawn platform pane after M0 (pre-install sessions have no
cursor and are invisible to orphan sweep):
```bash
tmux respawn-pane -k -t livetest:CDX 'zsh -il'
tmux send-keys -t livetest:CDX "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CDX "mkdir -p CDX_PROJECT_DIR && cd CDX_PROJECT_DIR && \
  QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE codex --yolo" Enter
```

---

## Execution Model — Platform Interaction

### OpenClaw (OC)

OC runs headlessly or via TUI. For TUI:
```bash
ssh REMOTE_HOST 'openclaw tui'
```

For Telegram-based runs, send messages via the testbox bot and receive via
`tg-poll`. See `TESTER.OC.md` for Telegram setup.

Lifecycle commands (`/new`, `/clear`, `/compact`) are sent as normal messages
(TUI or Telegram text).

### Claude Code (CC)

CC requires interactive mode for hooks to fire:
```bash
ssh REMOTE_HOST "mkdir -p CC_PROJECT_DIR && cd CC_PROJECT_DIR && \
  QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
  CLAUDE_PROJECT_DIR=CC_PROJECT_DIR claude --dangerously-skip-permissions"
```

**Set model before any interaction** — run `/model` and select the fast lane
model. Never run CC milestones on the most expensive model tier.

End sessions with `/exit` — never Ctrl+C (bypasses SessionEnd hook).

### Codex (CDX)

```bash
ssh REMOTE_HOST "mkdir -p CDX_PROJECT_DIR && cd CDX_PROJECT_DIR && \
  QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE codex --yolo"
```

CDX only has `/new` — no `/clear` or `/compact`. `/new` is disabled while a
task is running; wait for idle.

### Verification Commands (all platforms)

**DB check:**
```bash
ssh REMOTE_HOST 'sqlite3 WORKSPACE/instances/INSTANCE/data/memory.db \
  "SELECT rowid, name FROM nodes_fts WHERE nodes_fts MATCH '\''KEYWORD'\'' LIMIT 3;"'
```

**Daemon log:**
```bash
ssh REMOTE_HOST 'tail -20 WORKSPACE/instances/INSTANCE/logs/daemon/extraction-daemon.log'
```

**Rolling extraction log:**
```bash
ssh REMOTE_HOST 'cat WORKSPACE/instances/INSTANCE/logs/daemon/rolling-extraction.jsonl | tail -5'
```

**CLI recall:**
```bash
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE \
  QUAID_CLI recall "query" 2>&1'
```

Where `QUAID_CLI` is the quaid binary path on the remote (varies by platform —
OC: `~/.openclaw/extensions/quaid/quaid`, CC: `~/quaid/modules/quaid/quaid`,
CDX: same as OC or CC depending on install).

### Hot-Deploy (mid-test fix)

When deploying a fix during a live run, `scp` from the **local machine** to the
remote runtime path. Do not `ssh ... cp` — that copies the remote's own stale files.

```bash
# Build fresh
cd DEV_ROOT/modules/quaid && npm run build:runtime

# Deploy (OC adapter.js has two copies — both must match)
scp DEV_ROOT/modules/quaid/adaptors/openclaw/adapter.js \
    REMOTE_HOST:~/.openclaw/extensions/quaid/adaptors/openclaw/adapter.js

# Python hotfixes need no restart — imported fresh per-call
scp DEV_ROOT/modules/quaid/core/interface/hooks.py \
    REMOTE_HOST:~/.openclaw/extensions/quaid/core/interface/hooks.py
```

---

## Step 3 — M0–M15 Milestones (Parallel)

Three platforms (OC, CC, CDX) run M0–M15 in parallel. One platform runs M0
alone first (lead platform, rotated each run); the other two start after the
lead M0 passes. The run is not complete until all three platforms reach M15 PASS.

Platform lifecycle commands vary:
- **CC**: `/clear`, `/compact`, `/exit`
- **CDX**: `/new` (only — no `/clear` or `/compact`)
- **OC**: `/new`, `/clear`, `/compact` (via TUI or Telegram text)

### M0 — Agent-Driven Install

Tests the installer itself. See Step 2 above for the full procedure.

**Pass**: Platform self-installed, install messages visible in platform pane,
`quaid doctor` healthy.

**M0 sub-test (OC only, once per release)**: Unknown Provider Model
Clarification. Install with a non-standard provider, enter a misspelled model
name, verify the installer offers retry, re-enter correct names, verify PING
passes. Tests the full provider onboarding path including error recovery.

### M1 — Extraction via `/new`

Seed a distinctive fact, send `/new`, verify the fact is stored via FTS.

**Seed fact**: "My neighbour just told me she won a regional chili cook-off
last weekend using a smoked brisket recipe she's kept secret for twenty years."
**Keywords**: `chili`, `brisket`

**CDX quirk**: After `/new`, send one follow-up message (e.g. `Hello`) to
trigger `check_session_transition`. Extraction fires on that message, not `/new`.

**OC TUI quirk (2026.3.13+)**: `/new` may pass through to the model (model
replies "no /new command"). The adapter detects it via message event and fires
ResetSignal — no follow-up needed.

**Verification:**
```bash
ssh REMOTE_HOST 'sqlite3 WORKSPACE/instances/INSTANCE/data/memory.db \
  "SELECT rowid, name FROM nodes_fts WHERE nodes_fts MATCH '\''chili OR brisket'\'' LIMIT 3;"'
```

**Pass**: Fact stored after the lifecycle boundary; FTS finds keywords.

### M2 — Extraction via `/clear`

Seed a fact, send `/clear` (CDX: use `/new`), verify extraction.

**Seed fact**: "I just booked flights to Reykjavik for the aurora season in February."
**Keywords**: `Reykjavik`, `aurora`

**Pass**: Fact stored from the pre-clear session.

### M3 — Rolling Extraction + `/compact`

Seed a fact, build >1500 tokens of context (3–4 exchanges), verify
`rolling_stage` events, then send `/compact` (CDX: use `/new`) and verify
`rolling_flush`.

**Seed fact**: "My sister started her ceramics studio this spring, she fires
everything in a wood-burning kiln she built herself."
**Keywords**: `ceramics`, `kiln`

**Follow-up exchanges to build context:**
1. "What temperature range is typical for a wood-burning kiln for stoneware?"
2. "What should she watch for during reduction and cooling to avoid cracks?"
3. "Any practical checklist for loading and venting that style of kiln?"

**Before `/compact`** — verify rolling fired:
```bash
ssh REMOTE_HOST 'cat WORKSPACE/instances/INSTANCE/logs/daemon/rolling-extraction.jsonl | tail -5'
```
Expected: at least one `rolling_stage` event.

**After `/compact`** — verify flush:
```bash
ssh REMOTE_HOST 'cat WORKSPACE/instances/INSTANCE/logs/daemon/rolling-extraction.jsonl | \
  python3 -c "import sys,json; lines=[json.loads(l) for l in sys.stdin if l.strip()]; \
  print(f\"stages: {sum(1 for l in lines if l.get(\"event\")==\"rolling_stage\")}, \
  flushes: {sum(1 for l in lines if l.get(\"event\")==\"rolling_flush\")}\")"'
```

**Pass**: Fact stored, rolling_stage + rolling_flush events logged, rolling
state cleared after flush.

### M4 — Timeout Extraction

Set `capture.inactivityTimeoutMinutes` to 1, restart daemon, seed a fact, let
the session idle >1 minute. Verify extraction fires with no explicit lifecycle
command. Restore timeout to 60 after.

**Seed fact**: "My morning run route goes along the canal towpath — about 8km."
**Keywords**: `canal`, `towpath`

**Critical**: Seed AFTER any prior compaction/reset completes. If a lifecycle
command fires after seeding, the fact extracts via that signal (not timeout)
and the test is invalid.

**Signal naming**:
- OC/CC: `[daemon-compaction]` with `source: timeout_extract`
- CDX: `daemon-timeout` / timeout-extraction path

**Pass**: Timeout fact extracted. CDX: pass-with-note if extraction-only (no
compaction artifact — expected).

### M5 — Auto-Inject

Store a known fact via CLI, start a fresh session, ask a natural question.
Verify the agent answers from injected context without an explicit tool call.

```bash
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE \
  QUAID_CLI store "Baxter is a golden retriever who loves tennis balls" 2>&1'
```

**Query**: "What do you know about my dog Baxter?"

**Pass**: Agent answers with the stored fact, no explicit tool call.

### M6 — Deliberate Recall

Ask natural questions framed so the agent uses explicit `quaid recall` via
bash/shell tool, independent of auto-inject:

- "Please run `quaid recall "my family"` via your shell tool. What have I told
  you about my family?"
- "Same — run `quaid recall "exercise habits recent plans"`. What do you know
  about my exercise habits?"

**Pass**: Agent runs `quaid recall` and answers from stored memory.
**OC Telegram**: PASS-WITH-NOTE (no shell tool access).

### M7 — Graph Traversal Verification

**Phase 1 — Edge extraction**: Store four relationship facts via CLI:
```bash
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI store "David is the user'\''s brother" 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI store "David is married to Lisa" 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI store "David has a son named Oliver" 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI store "David works at Google" 2>&1'
```

Verify edges:
```bash
ssh REMOTE_HOST 'sqlite3 WORKSPACE/instances/INSTANCE/data/memory.db \
  "SELECT s.name, e.relation, t.name FROM edges e \
   JOIN nodes s ON e.source_id=s.id JOIN nodes t ON e.target_id=t.id \
   WHERE s.name IN (\"David\",\"Lisa\",\"Oliver\") OR t.name IN (\"David\",\"Lisa\",\"Oliver\") \
   ORDER BY s.name, e.relation;"'
```

**Phase 2 — Janitor edge backfill**: Store an attribute fact, run backfill:
```bash
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI store "David is 42 years old" 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI janitor --task edges --apply 2>&1'
```

**Phase 3 — Multi-hop traversal**: In a fresh session, seed:
- "My sister's name is Diana."
- "Diana has a daughter named Alice."

Extract, then ask: "Who is my niece?"

**Pre-flight**: Delete any stale Diana/Alice/niece nodes from prior runs. Start
a completely fresh session — do not retry within the same session (carry_facts
contamination).

**Pass**: Edges exist, backfill runs, multi-hop query answered correctly. Owner
entity in sibling edges must be the actual owner name (not "User").

**CDX quirk**: Fast lane model at `effort=none` generates zero edges. Temporarily
switch to a stronger model for M7, restore after.

### M8 — Full Project System CRUD

Capability test — do not tell the agent exact command names.

Prepare a source root:
```bash
ssh REMOTE_HOST 'mkdir -p ~/quaid-live-src && printf "print(\"hello\")\n" > ~/quaid-live-src/main.py'
```

**Phase 1 — Indirect trigger**: Send a work directive (do NOT mention "project"):
> "I have a Python script at ~/quaid-live-src/main.py. I want to add argument
> parsing and a few tests. Can you set up a project for this and make those changes?"

Agent must create a project BEFORE writing files.

**Phase 2 — Explicit CRUD**: "Can you show me the project?", "Update its
description", "List all projects."

**Phase 3 — Delete**: "Can you delete the project?" Source files must survive.

**Phase 4 — Scratch dir**: "Save a one-line hello world somewhere temporary."
Agent should use misc project tracking.

**Pass**: Agent creates project proactively, CRUD works, delete does not remove
source, misc tracking works.
**OC Telegram**: PASS-WITH-NOTE (no shell tool access for CLI commands).

**Model note**: Haiku does not reliably follow file-placement policy. Use Sonnet
or better for Phase 1.

### M9 — Janitor

```bash
# Dry-run must complete in ≤60s
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI janitor --task all --dry-run 2>&1'
# Apply — first run can take 15–30 min
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI janitor --task all --apply --approve 2>&1'
```

Verify condensation:
```bash
ssh REMOTE_HOST 'cat WORKSPACE/instances/INSTANCE/logs/janitor-stats.json | python3 -c \
  "import json,sys; d=json.load(sys.stdin); ac=d.get(\"applied_changes\",{}); \
  print(\"success:\", d[\"success\"]); \
  [print(f\"  {k}: {v}\") for k,v in ac.items() if \"snippet\" in k]"'
```

**Pass**: Janitor completes, `janitor-stats.json` success, `snippets_folded +
snippets_rewritten + snippets_discarded > 0`.

### M10 — Docs, Health, and Session CLI

```bash
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI health 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI doctor 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI stats 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI docs list 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI docs check 2>&1'
```

**New-doc indexing test:**
```bash
ssh REMOTE_HOST 'echo "# M10 test\nThe carillon clock rings at noon." > /tmp/m10-test-doc.md'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI registry register /tmp/m10-test-doc.md --project quaid 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI docs update --apply 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=INSTANCE QUAID_CLI recall "carillon clock" 2>&1'
```

**Session CLI test:**
```bash
ssh REMOTE_HOST 'sqlite3 WORKSPACE/instances/INSTANCE/data/memory.db \
  "SELECT session_id, updated_at, substr(topic_hint,1,80) FROM session_logs ORDER BY updated_at DESC LIMIT 3;"'
```

**Pass**: All CLI commands succeed, new doc indexed without janitor rag,
session logs populated.

### M11 — Snippet, Journal, and Project Log Generation

Restart daemon to refresh config, then send two messages and trigger extraction:

**Message 1 — personal** (triggers soul_snippets):
> "I really enjoy this kind of systematic validation work. It is satisfying when
> tests catch real edge cases."

**Message 2 — project context** (triggers project_logs):
> "We have been running the quaid live test suite. Snippets and journals are
> being validated now."

Then `/clear` (CDX: `/new`).

**Verify:**
```bash
ssh REMOTE_HOST 'cat WORKSPACE/instances/INSTANCE/USER.snippets.md 2>/dev/null || echo "(absent)"'
ssh REMOTE_HOST 'cat WORKSPACE/instances/INSTANCE/SOUL.snippets.md 2>/dev/null || echo "(absent)"'
ssh REMOTE_HOST 'ls WORKSPACE/instances/INSTANCE/journal/ 2>/dev/null || echo "(absent)"'
ssh REMOTE_HOST 'tail -20 WORKSPACE/projects/quaid/PROJECT.log 2>/dev/null || echo "(absent)"'
```

**Pass**: `USER.snippets.md` has entries (hard gate), `PROJECT.log` has
timestamped entries. `SOUL.snippets.md` absence on scripted test runs is
expected (not a failure — the LLM correctly withholds SOUL snippets for
transactional content).

### M12 — OC Multi-Agent Silo Verification (OC only)

1. `list_agent_instance_ids()` returns at least `[OC_INSTANCE]`.
2. Each agent has its own `data/` and `extraction-signals/` silo dir.
3. Signals land under the correct per-agent dir, not a shared path.
4. `quaid instances list` reports OC agent silos.
5. `extraction-daemon.pid` exists and points to a live process.

### M13 — CC Multi-Instance Verification (CC only)

1. Run `quaid claudecode make_instance /path/to/project m13test`.
2. Verify silo created with `config/`, `data/`, `identity/`, `journal/`, `logs/`.
3. Verify `config/memory.json` has `adapter.type == "claude-code"`.
4. Verify `.claude/settings.json` in the project dir has correct `QUAID_INSTANCE`.
5. Verify `quaid instances list` includes the new instance.
6. Verify dry-run (`--dry-run`) creates no silo.
7. **Cross-project spillover proof**: Store a canary fact from the new project
   dir, verify it is NOT visible from the original livetest project dir.

### M14 — Deferred Notification Surfacing

1. Write a synthetic deferred notice to `INSTANCE/.runtime/notes/delayed-llm-requests.json`.
2. Verify `quaid notify --deferred-status` shows 1 pending.
3. Send a natural turn (e.g. "Hey, what is up?").
4. Agent should drain the notice and relay it.
5. Verify 0 pending after drain.
6. Repeat for all three platforms.

### M15 — Provider Outage — Fast Notification Path

1. Record current model values.
2. Set `fastReasoning` and `deepReasoning` to `invalid-model-xyzzy`.
3. Send a turn requiring a fast LLM call (e.g. "What do you remember about my family?").
4. Agent must surface: `[Quaid error] [provider] Quaid could not access its fast language model provider`.
5. Restore correct model values.
6. Verify next turn works normally (no restart needed).

**Pass**: Error surfaced with tier and exception detail, recovery on next turn.

---

## Step 4 — Cross-Platform Project Linking Test (XP)

Run after all three platforms reach M13 PASS.

### Phase 1: Create project in OC

Prepare source root:
```bash
ssh REMOTE_HOST 'mkdir -p /tmp/cross-live-test-src && cat > /tmp/cross-live-test-src/main.py <<PY
def harbor_status():
    return "North pier beacon is offline"
PY'
```

Ask OC naturally:
- "Can you create a project named cross-live-test for /tmp/cross-live-test-src?"
- "Add a project document: the north pier beacon is offline, maintenance window
  starts at 02:15 UTC."

### Phase 2: Link in CC, add second doc

Ask CC:
- "Do you see the existing cross-live-test project?"
- "Add another doc: code word Ember Glass means pager escalation level 2."

### Cross-registration

Each adapter maintains its own docs index. Cross-register docs between instances:
```bash
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE QUAID_CLI registry register /tmp/cross-live-test-src/beacon-maintenance.md --project cross-live-test 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=OC_INSTANCE QUAID_CLI registry register /tmp/cross-live-test-src/codewords.md --project cross-live-test 2>&1'
```

Then `docs update --apply` on both instances.

### Phase 3: Cross-recall all directions

- CC: "Search the cross-live-test project docs for the north pier beacon."
- OC: "What does the cross-live-test project say about Ember Glass?"
- CDX: Both queries (after registering docs in CDX's registry too).

**Pass**: Each platform retrieves docs added by the other two, grounded in
Quaid project context (not raw disk browsing).

---

## Step 5 — Post-Test Examination

After all milestones complete, run log audits. See `COORDINATOR.SKILL.md` for
the full subagent prompts.

**Post-Test Log Audit**: Spawn subagents (one per platform) to read extraction
buffer logs, daemon logs, and rolling extraction logs. Flag:
- Leaked credentials or tokens (CRITICAL)
- Extraction prompt leakage (HIGH)
- Configuration dumps with provider prefixes (HIGH)
- Internal file paths (MEDIUM)
- Hook/system chatter that survived sanitization (MEDIUM)
- Test harness metadata (LOW)

CRITICAL findings block the run from being marked CLEAN.

---

## Step 6 — End-of-Run Check

```bash
cd DEV_ROOT && git log --oneline RUN_START_SHA..HEAD
```

### Case A — Zero new commits

Full suite passed with no code changes.

1. Push canary: `./scripts/push-canary.sh github`
2. Deploy to remote (rsync).
3. Print end-of-run report.
4. Stop (unless `loop: true`).

### Case B — One or more new commits

1. Build runtime: `cd modules/quaid && npm run build:runtime`
2. Push canary via the push script.
3. Deploy to remote.
4. Log all new commits to `unreviewed-commits.md`.
5. Print end-of-run report.
6. If `loop: false`: stop, recommend follow-up run.
7. If `loop: true`: return to Step 1, new HEAD as RUN_START_SHA.

### End-of-Run Report

```
=== LIVETEST RUN REPORT ===
Run N — YYYY-MM-DD

RESULT: CLEAN | REQUIRES FOLLOW-UP | FAILURES REMAIN

Platform results:
  OC:  PASS | FAIL | PASS-WITH-NOTE
  CC:  PASS | FAIL | PASS-WITH-NOTE
  CDX: PASS | FAIL | PASS-WITH-NOTE
  XP:  PASS | FAIL | SKIPPED

Issues fixed this run: N
  - <sha> <short description>

Commits made this run: N
  (none) | list of sha + subject

Next step:
  Suite clean — no action needed.
  | Follow-up run recommended to verify N fix commit(s).
  | Failures remain — see issues above before re-running.
===========================
```

---

## Notification Level Checks

Toggle config between milestones to verify notification verbosity:

1. After M3: set `notifications.extraction.verbosity` to `debug`
2. After M5: set `notifications.retrieval.verbosity` to `summary`
3. After M7: set notifications to `off`
4. After M9: restore original values

Verify by checking the next relevant event after each change.

## PASS-WITH-NOTE — Strict Criteria

Only valid when ALL of the following are true:
1. The failure is constrained by an external system API or data model.
2. All other steps of the milestone pass fully.
3. The tested function works end-to-end via a different path covered by passing
   steps.
4. A fix would require changing the external system, not just a code patch.

If you can imagine a code change that would fix it — write it.

## Loop Termination Contract

When running with `loop: true`:
- Only exit when a full suite (OC + CC + CDX + XP) passes with zero new commits.
- A run that passes but required commits → mandatory re-run, no exceptions.
- Do not exit early because the suite looks stable. Run it clean.

## Compatibility Update Rule

- Treat compatibility as a live-test output, not as a separate matrix promise.
- Only update `compatibility.json` after the full current live suite is green
  and the operator has reviewed the clear run.
- Record host clears separately for `Quaid/OpenClaw`, `Quaid/Claude Code`, and
  `Quaid/Codex`.
- XP is part of release readiness, but it does not replace host compatibility rows.
- Use `node scripts/record-compatibility-clear.mjs` to write the cleared SHA.
- Pass `--install-verified true` only if M0 completed cleanly without manual
  config patching.
- Do not update compatibility entries for partial clears, failed runs, or
  single-adapter-only validation.

## Platform Behavior Notes

### Unified Search Surface

`quaid search` and `quaid recall` are the primary search surfaces. There is no
`quaid docs search` alias by design — the platform intentionally unifies all
retrieval under one surface. Do not treat a missing `quaid docs search` CLI
alias as a test failure.

### Deferred Notices

Agents may encounter a `quaid notify --deferred-status` notice during a
milestone. A well-behaved agent drains it via `quaid notify --deferred-drain`
and relays the content. Acknowledge as a pass when the agent drains without
asking permission. Do not treat "asked before draining" as preferred behavior.

### QUAID_DAEMON Guard

The CC daemon and hook entry point both set `QUAID_DAEMON=1` on startup. This
tells the LLM provider to skip subprocess calls and route directly to OAuth. Without
this guard, daemon LLM calls spawn full CC sessions triggering hooks recursively
(hook storm). If many concurrent `hooks.py` PIDs appear, verify both locations
set `QUAID_DAEMON=1` before any LLM call.
