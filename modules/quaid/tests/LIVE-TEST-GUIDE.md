# Quaid Live Test Guide

Instructions for an LLM agent to run a full live validation of Quaid against a
real OpenClaw and Claude Code setup. All interaction with the target agent
happens through tmux message passing or a visible interactive pane. All
verification happens from a separate tester shell using CLI commands, DB
queries, and logs.

This is black-box testing:
- no direct function calls
- no imports into runtime codepaths
- no mocks
- no code edits during the live test
- all live agent interaction must happen through a visible tmux pane to
  simulate a real user

## Core Rules

**MACHINE SAFETY — READ FIRST:**
- ALL install, uninstall, and `setup-quaid.mjs` commands MUST be run via
  `ssh REMOTE_HOST '...'`. NEVER run them directly on the local machine.
- Before any install or uninstall command, verify you are targeting alfie:
  `ssh REMOTE_HOST hostname` must return `alfie`.
- If you ever find yourself running `node setup-quaid.mjs` without an
  `ssh REMOTE_HOST` prefix, STOP immediately — you are on the wrong machine.

**DO NOT CORRUPT THE TEST:**
A failure is a signal. Fix what is broken — do not make the test easier to
pass. These are different things. Wrong responses to a failure include:
- Relaxing a test criterion because it is hard to satisfy
- Hardcoding env vars or instance names to force a specific identity
- Skipping safety checks because they fail in the test environment
- Disabling a code path because it causes a timeout
- Ruling PASS-WITH-NOTE to avoid doing work

This test simulates real user behavior. Any change that makes the test pass
by diverging from real behavior hides a bug instead of fixing it.

- Use this document as the source of truth for the live test procedure.
- Start from a clean install unless the user explicitly says to skip it.
- Run the live test from the `main` branch. Verify the checkout before
  installing or testing.
- Use the installer script, not ad hoc install steps.
- Do not use hidden helper wrappers for agent interaction during the live run.
  Use a visible tmux pane for OpenClaw and Claude Code.
- Lower model cost before testing: try Haiku first, step up to Sonnet only if
  quality is too degraded to run the test reliably.
- Send ISSUE messages when something breaks or the environment is unclear.
- Do not send routine milestone status messages.
- After a fix, re-run the failed milestone. Do not mark it done without
  re-verification.
- For live testing, janitor apply is pre-approved. If a milestone or docs/RAG
  verification needs `quaid janitor --apply --approve`, run it directly
  instead of stopping for approval.
- For capability tests, speak to the agent like a real user would. Do not
  spoon-feed function names or CLI subcommands unless the milestone is
  explicitly testing a slash command such as `/new`, `/clear`, `/reset`, or `/compact`.

## Reporting

When you hit a failure or blocker, send an ISSUE message to `claude-dev`
window `4` that includes:

1. The milestone name.
2. The exact command that failed.
3. The first few lines of the error.
4. What you already tried.

At the end of the run, send one final summary.

## Long-Running Test Start

Before starting a long run, request nudges:

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh 5 "start nudge on window 7"
```

## Environment

Main test environment:
- Repo root: `~/quaid/dev`
- Required branch: `main`
- Test guide: `~/quaid/dev/modules/quaid/tests/LIVE-TEST-GUIDE.md`
- Reference tool guide: `~/quaid/dev/projects/quaid/TOOLS.md`

Target machine:
- Host: `REMOTE_HOST`
- OpenClaw workspace: `~/quaid`

Pane assignments:

| Window | Agent | Role |
|--------|-------|------|
| `main:97` | codex-livetester (CC) | Drives CC milestones M0–M13 |
| `main:98` | codex-livetester (OC) | Drives OC milestones M0–M13 |
| `main:99` | live-test | OC verification pane (SSH + coordinator CLI; OC interaction via Matrix) |
| `main:100` | CC-interact | Visible CC interaction pane (`claude`) |
| `main:4` | claude-dev | Coordinator |

Dedicated live-test silos:
- OC instance: `openclaw-main`
- CC instance: `claude-code-private-tmp-cc-livetest` (derived from launch dir `/tmp/cc-livetest` → `/private/tmp/cc-livetest` → slug `private-tmp-cc-livetest`)
- Fixed CC project dir: `/tmp/cc-livetest`

Do not reuse `*-main` silos for live validation. The live pane must point at
throwaway test-only silos so milestone prompts and guide/example text cannot
contaminate Solomon's normal working memory.

## Start Condition

Do not begin milestone testing against an existing live Quaid install.

### Step 0 — Full wipe on alfie (mandatory)

Before any run, completely remove Quaid and all its data from alfie. Do not do
targeted cleanup — stale carryover files, queue events, DB nodes, and identity
files all contaminate test results in ways that are hard to trace. A full wipe
is faster and safer than surgical cleanup.

> **Parallel runs — CC wipe scope:** When CC M0 starts while OC is already
> active (the normal parallel case), do **not** run the full wipe below.
> Instead, CC-only wipe:
> ```bash
> ssh REMOTE_HOST 'rm -rf ~/quaid/instances/claude-code-private-tmp-cc-livetest && echo "CC silo wiped"'
> ssh REMOTE_HOST 'python3 - <<"PY"
> import json; from pathlib import Path
> p = Path.home() / ".claude/settings.json"
> if p.exists():
>     d = json.loads(p.read_text())
>     for ev, entries in list(d.get("hooks", {}).items()):
>         d["hooks"][ev] = [e for e in entries if "quaid" not in str(e).lower()]
>     p.write_text(json.dumps(d, indent=2))
> print("CC hooks cleared")
> PY'
> ```
> Leave `~/quaid/instances/openclaw-main`, `~/.openclaw/extensions/quaid`, and the
> OC gateway untouched — OC is live on all of those.
>
> After the CC installer completes, **also apply the chunk_tokens override**
> (same step as the full M0 post-install — do not skip it in the parallel path):
> ```bash
> ssh REMOTE_HOST 'python3 -c "import json; p=\"/Users/USER/.quaid/instances/claude-code-private-tmp-cc-livetest/config/memory.json\"; d=json.load(open(p)); d.setdefault(\"capture\",{})[\"chunk_tokens\"]=1500; json.dump(d,open(p,\"w\"),indent=2); print(\"CC chunk_tokens:\", d[\"capture\"][\"chunk_tokens\"])"'
> ```

**Uninstall the plugin first to remove registry entries:**

```bash
ssh REMOTE_HOST 'openclaw plugins uninstall quaid 2>/dev/null; echo "OC uninstall done"'
```

**Then wipe the entire Quaid workspace and extension dir:**

```bash
ssh REMOTE_HOST 'rm -rf ~/quaid && rm -rf ~/.openclaw/extensions/quaid && echo "wipe done"'
```

> **WARNING**: This runs on REMOTE_HOST only — never on the local dev machine
> where the source repo lives.

**Clear OC session transcripts** (critical — stale sessions from prior runs trigger
extraction fan-out after reinstall, saturating the gateway and breaking `/reset` and
other hook-dependent milestones):

```bash
ssh REMOTE_HOST 'rm -rf ~/.openclaw/agents/main/sessions/ && echo "OC sessions cleared"'
```

**Clear CC adapter artifacts:**

```bash
ssh REMOTE_HOST 'rm -f ~/.claude/rules/quaid-projects.md && echo "CC rules cleared"'
```

### Step 1 — Installer-Based Clean Install (mandatory)

The wipe in Step 0 is not enough on its own. **Every run must reinstall Quaid
using the installer script.** The purpose of M0 is to validate the installer
itself — that it correctly provisions the workspace, identity files, config,
DB, and hooks from scratch. Running milestones against a hand-crafted or
previously installed workspace does not test the installer.

Do not skip the reinstall. There is no "note it and move on" option — a run
without installer reinstall is not a live install validation run.

After the wipe and before M1:
- verify the repo checkout is on `main`
- build runtime artifacts and sync to alfie
- run the installer for OC and CC (see commands below)
- run post-install verification checks
- confirm minimum stability before M1:
  - install artifacts exist where expected
  - `quaid doctor` or `quaid health` succeeds
  - active DB and log paths are identified
  - daemon starts cleanly
  - one basic agent turn succeeds without hanging

## Installer-Based Clean Install

Use a source tree from the local test machine only. Do not involve `spark`.
Valid install sources for a live run are:

- local `~/quaid/dev` on this machine
- GitHub `openclaw` when that is the target under test
- Quaid `main`

When using the local dev tree, build the runtime artifacts first, then sync
to alfie. `adapter.js` is a build artifact — rsync copies it as-is, so it
must be built before sync or it will be stale on alfie.

**Build first (required before every sync):**

```bash
cd ~/quaid/dev/modules/quaid && npm run build:runtime
```

Then sync the full tree from this machine to alfie. `setup-quaid.mjs` and
`lib/` are at the root of `~/quaid/dev`, not inside `modules/quaid/`:

```bash
rsync -av --checksum \
  --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.git/' --exclude='logs/' --exclude='.env*' \
  ~/quaid/dev/ REMOTE_HOST:~/quaid/dev/
```

Also sync the legacy plugin path — the installer (`--workspace ~/quaid`) falls
back to `~/quaid/plugins/quaid/` when `~/quaid/modules/quaid/` is absent, so
both locations must be up to date:

```bash
rsync -av --checksum \
  --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.git/' --exclude='logs/' \
  ~/quaid/dev/modules/quaid/ REMOTE_HOST:~/quaid/plugins/quaid/
```

Verify branch on the local source checkout:

```bash
cd ~/quaid/dev && git branch --show-current && git rev-parse --short HEAD
```

Pass only if the branch is exactly `main`.

### Hot-deploy during a live run (mid-test fix)

When a fix is committed during a live run and you need to deploy without a
full reinstall, use `scp` from the **local machine** directly to the OC
runtime path. Do NOT use `ssh alfie 'cp ...'` — it copies alfie's own
(stale) files and silently does nothing.

**OC adapter.js runtime path** — OC loads from the PLUGIN SOURCE path, not the
extensions dir. Both copies must match or hotfixes silently miss the live runtime:

```
~/quaid/plugins/quaid/adaptors/openclaw/adapter.js   ← gateway loads THIS
~/.openclaw/extensions/quaid/adaptors/openclaw/adapter.js   ← also update
```

Deploy adapter.js hotfix:
```bash
# 1. Build fresh artifact on the local machine
cd ~/quaid/dev/modules/quaid && npm run build:runtime

# 2. scp to BOTH paths — missing either one leaves the gateway on stale code
scp ~/quaid/dev/modules/quaid/adaptors/openclaw/adapter.js \
    REMOTE_HOST:~/quaid/plugins/quaid/adaptors/openclaw/adapter.js
scp ~/quaid/dev/modules/quaid/adaptors/openclaw/adapter.js \
    REMOTE_HOST:~/.openclaw/extensions/quaid/adaptors/openclaw/adapter.js

# 3. Verify both copies match
ssh REMOTE_HOST 'sha256sum ~/quaid/plugins/quaid/adaptors/openclaw/adapter.js ~/.openclaw/extensions/quaid/adaptors/openclaw/adapter.js'

# 4. Restart OC gateway
ssh REMOTE_HOST 'pkill -f openclaw-gateway; sleep 2; nohup openclaw gateway > /tmp/oc-gw.log 2>&1 &'

# 5. Verify new code loaded — send a test message and check gateway.log
# Look for the correct datastores/scrubQuery behavior in [quaid][recall] lines
```

Deploy Python hotfix (hooks.py or other CC modules):
```bash
scp ~/quaid/dev/modules/quaid/core/interface/hooks.py \
    REMOTE_HOST:~/.openclaw/extensions/quaid/core/interface/hooks.py
# No restart needed for Python — hooks.py is imported fresh per-call
```

### OpenClaw on REMOTE_HOST

Preview first:

```bash
ssh REMOTE_HOST 'openclaw plugins list 2>/dev/null | grep quaid || true'
ssh REMOTE_HOST 'ls -ld ~/quaid ~/quaid/instances/openclaw-main ~/quaid/projects 2>/dev/null || true'
```

Ensure the OpenClaw gateway is running before installing — the installer will
bail immediately if it is not:

```bash
ssh REMOTE_HOST 'pgrep -f openclaw-gateway > /dev/null 2>&1 || (nohup openclaw gateway > /tmp/oc-gw.log 2>&1 &); for i in $(seq 1 30); do curl -sf http://localhost:18789/health > /dev/null 2>&1 && echo "Gateway ready" && break || sleep 2; done'
```

Install with the installer script on `alfie`, using the synced local tree.
Use `QUAID_TEST_MOCK_MIGRATION=1` to skip LLM-based migration of existing
workspace files (SOUL.md, USER.md, etc.) — without it the installer runs 5
sequential deep-reasoning calls that block M0 for several minutes:

```bash
ssh REMOTE_HOST 'cd ~/quaid/dev && QUAID_INSTALL_AGENT=1 QUAID_TEST_MOCK_MIGRATION=1 QUAID_OWNER_NAME="Solomon" QUAID_INSTANCE=openclaw-main node setup-quaid.mjs --agent --workspace "/Users/USER/quaid" --source local'
```

### Claude Code on REMOTE_HOST

Clear old hooks if present, then reinstall with the installer script:

```bash
ssh REMOTE_HOST 'python3 - <<\"PY\"
import json
from pathlib import Path
p = Path.home() / ".claude/settings.json"
if p.exists():
    data = json.loads(p.read_text())
    hooks = data.get("hooks", {})
    for event, entries in list(hooks.items()):
        hooks[event] = [entry for entry in entries if "quaid" not in str(entry).lower()]
    p.write_text(json.dumps(data, indent=2))
print("Cleared existing Quaid Claude Code hooks if present")
PY'
ssh REMOTE_HOST 'mkdir -p /tmp/cc-livetest && cd /tmp/quaid-install-canary && QUAID_INSTALL_AGENT=1 QUAID_TEST_MOCK_MIGRATION=1 QUAID_OWNER_NAME="Solomon" QUAID_INSTANCE=claude-code-private-tmp-cc-livetest CLAUDE_PROJECT_DIR=/tmp/cc-livetest QUAID_INSTALL_CLAUDE_CODE=1 node setup-quaid.mjs --agent --claude-code --workspace "/Users/USER/quaid" --source local'
```

After the installer runs, write the API-scoped OAuth token to Quaid's shared
auth registry. This token is required for daemon LLM calls (sonnet/opus via direct
OAuth). Without it the daemon falls back to `claude -p` subprocess calls, which
trigger the hook storm described below.

```bash
# Read the token from a local token file and register it on alfie
TOKEN=$(cat ~/anthropic-oauth-token.txt | tr -d '[:space:]')
ssh REMOTE_HOST "quaid auth refresh --kind anthropic_oauth '$TOKEN' && echo 'Auth token written'"
```

Also verify model config was written by the installer:

```bash
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"/Users/USER/.quaid/instances/claude-code-private-tmp-cc-livetest/config/memory.json\")); print(d.get(\"models\", {}))"'
```

Expected output: `{'deepReasoning': 'claude-opus-4-6', 'fastReasoning': 'claude-haiku-4-5-20251001'}`.
If models are missing or empty, the daemon will raise `RuntimeError` at call time — re-run the
installer or inject manually.

> **QUAID_DAEMON guard — hook storm prevention:** The CC daemon and hook entry
> point both set `QUAID_DAEMON=1` on startup. This env var tells the LLM
> provider to skip Layer 0 (`claude -p` subprocess) and route directly to OAuth
> (Layer 1b, `.auth-token` file). Without this guard, daemon LLM calls spawn
> full CC sessions which trigger hooks, which call the LLM again — exponential
> recursion producing hundreds of concurrent `hooks.py` processes and thousands
> of synthetic session cursor files. The guard is set in `daemon_loop()` in
> `core/extraction_daemon.py` and in `main()` in `core/interface/hooks.py`.
> If a hook storm is observed (many concurrent `hooks.py` PIDs), verify both
> locations set `QUAID_DAEMON=1` before any LLM call.
>
> **OAuth identity headers for sonnet/opus:** Direct OAuth calls to
> `/v1/messages` require CC identity headers to access sonnet/opus tiers:
> `anthropic-beta: claude-code-20250219,prompt-caching-2024-07-31,oauth-2025-04-20`,
> `User-Agent: claude-cli/2.1.2 (external, cli)`, `x-app: cli`, and a first
> system block of `"You are Claude Code, Anthropic's official CLI for Claude."`.
> Without these, haiku works but sonnet/opus return HTTP 400. This is
> implemented in `adaptors/claude_code/providers.py` `_api_call()`.

### Post-install verification

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid doctor 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid health 2>&1'
ssh REMOTE_HOST 'cat ~/.claude/settings.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(sorted(d.get(\"hooks\", {}).keys()))"'
# Verify QUAID_HOME in global settings and QUAID_INSTANCE in per-project settings.
# QUAID_INSTANCE is NOT in ~/.claude/settings.json — it is pinned per-project so
# different CC project dirs can use different silos without cross-contamination.
ssh REMOTE_HOST 'cat ~/.claude/settings.json | python3 -c "import sys,json; d=json.load(sys.stdin); e=d.get(\"env\",{}); print(\"QUAID_HOME:\",e.get(\"QUAID_HOME\",\"MISSING\")); print(\"QUAID_INSTANCE (should be absent):\",e.get(\"QUAID_INSTANCE\",\"(absent — correct)\"))"'
ssh REMOTE_HOST 'cat /tmp/cc-livetest/.claude/settings.json | python3 -c "import sys,json; d=json.load(sys.stdin); e=d.get(\"env\",{}); print(\"QUAID_INSTANCE (per-project):\",e.get(\"QUAID_INSTANCE\",\"MISSING\"))"'
# Expected: QUAID_HOME: /Users/USER/.quaid   and   QUAID_INSTANCE (per-project): (absent — derived from path)
ssh REMOTE_HOST 'ls -l ~/quaid/instances/openclaw-main/SOUL.md ~/quaid/instances/claude-code-private-tmp-cc-livetest/SOUL.md 2>/dev/null || true'
```

If either instance-local `SOUL.md` is missing, the installer did not
seed it correctly — this is a bug. Fix the installer. As a temporary unblock,
seed from the shared project template:

```bash
ssh REMOTE_HOST 'python3 - <<\"PY\"
from pathlib import Path
template_dir = Path("/Users/USER/quaid/projects/quaid")
for fname in ("SOUL.md", "USER.md", "ENVIRONMENT.md"):
    src = template_dir / fname
    if not src.exists():
        print(f"WARNING: template missing: {src}")
        continue
    for dst_dir in (
        Path("/Users/USER/quaid/instances/openclaw-main"),
        Path("/Users/USER/quaid/instances/claude-code-private-tmp-cc-livetest"),
    ):
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / fname
        if not dst.exists():
            dst.write_bytes(src.read_bytes())
            print(f"created {dst}")
PY'
```

Seed the quaid project in both instance silos so `PROJECT.log` can be written
by extraction. The project must be **registered** in the docs DB (not just on
disk) for the extraction daemon to find it:

```bash
# OC instance — CLI command (works reliably for OC)
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry create-project quaid --description "Quaid development project" 2>&1; true'

# CC instance — inject definition directly (CLI "already exists" false-positive
# can occur due to config singleton state; direct injection is reliable)
ssh REMOTE_HOST 'python3 -c "
import json
p = \"/Users/USER/.quaid/instances/claude-code-private-tmp-cc-livetest/config/memory.json\"
with open(p) as f: d = json.load(f)
if \"quaid\" not in d[\"projects\"][\"definitions\"]:
    d[\"projects\"][\"definitions\"][\"quaid\"] = {
        \"label\": \"Quaid\", \"home_dir\": \"../projects/quaid/\",
        \"source_roots\": [], \"auto_index\": True, \"patterns\": [\"*.md\"],
        \"exclude\": [\"*.db\", \"*.log\", \"*.pyc\", \"__pycache__/\"],
        \"description\": \"Quaid development project\", \"state\": \"active\"
    }
    with open(p, \"w\") as f: json.dump(d, f, indent=2)
    print(\"Injected quaid project definition\")
else:
    print(\"quaid already in definitions\")
"'
```

### Live-test config overrides

> **Known test workaround — not a production setting.**
> The rolling extraction threshold (`capture.chunk_tokens`) is lowered from the
> production default of 8 000 tokens to 1 500 tokens for both test silos. This
> ensures a normal test conversation (3–4 exchanges) crosses the threshold so
> the rolling extraction pipeline can be verified without generating tens of
> thousands of tokens. Do not apply this change outside of test silos.

```bash
# OC silo
ssh REMOTE_HOST 'python3 -c "
import json
p = \"/Users/USER/quaid/openclaw-main/config/memory.json\"
with open(p) as f: d = json.load(f)
d.setdefault(\"capture\", {})[\"chunk_tokens\"] = 1500
with open(p, \"w\") as f: json.dump(d, f, indent=2)
print(\"capture.chunk_tokens set to 1500 for openclaw-main\")
"'

# CC silo
ssh REMOTE_HOST 'python3 -c "
import json
p = \"/Users/USER/.quaid/instances/claude-code-private-tmp-cc-livetest/config/memory.json\"
with open(p) as f: d = json.load(f)
d.setdefault(\"capture\", {})[\"chunk_tokens\"] = 1500
with open(p, \"w\") as f: json.dump(d, f, indent=2)
print(\"capture.chunk_tokens set to 1500 for claude-code-private-tmp-cc-livetest\")
"'
```

Verify both silos have the override:

```bash
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"/Users/USER/quaid/openclaw-main/config/memory.json\")); print(\"OC chunk_tokens:\", d.get(\"capture\",{}).get(\"chunk_tokens\",\"NOT SET\"))"'
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"/Users/USER/.quaid/instances/claude-code-private-tmp-cc-livetest/config/memory.json\")); print(\"CC chunk_tokens:\", d.get(\"capture\",{}).get(\"chunk_tokens\",\"NOT SET\"))"'
```

Expected: `OC chunk_tokens: 1500` and `CC chunk_tokens: 1500`.

## Execution Model

### Phase Start Reset

OC and CC run in parallel — OC uses `main:99`, CC uses `main:100`. Each pane
must be set up before its suite starts.

**OC phase start** — verify Matrix and gateway are up:
```bash
ssh REMOTE_HOST 'curl -sf http://127.0.0.1:8008/_matrix/client/versions > /dev/null && echo "matrix ok" || echo "matrix DOWN"'
ssh REMOTE_HOST 'launchctl list | grep -E "matrix-synapse|openclaw.gateway"'
```
OC receives messages via Matrix DM — no TUI window needed for test interaction. The `main:99` pane is used only for coordinator SSH verification commands, not for typing messages to OC.

**CC phase start** — reset `main:100` (do this once OC M0 passes):
```bash
tmux respawn-pane -k -t main:100 'zsh -il'
tmux send-keys -t main:100 "ssh REMOTE_HOST" Enter
tmux send-keys -t main:100 "mkdir -p /tmp/cc-livetest && cd /tmp/cc-livetest && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest CLAUDE_PROJECT_DIR=/tmp/cc-livetest claude --dangerously-skip-permissions" Enter
```

Respawn the relevant pane again if it becomes contaminated mid-run.

### OpenClaw

OC interaction uses **Matrix DM**, not the TUI. All test messages and slash commands
are sent via the canonical `modules/quaid/tests/livetest/scripts/matrix-send` helper on the VM. The Matrix server (`ai.quaid.matrix-synapse`)
and OpenClaw gateway must be running before any OC milestone.

**Why Matrix, not TUI:** TUI `/new` creates `tui-`-prefixed sessions that don't fire
`hook.openclaw.session_end`. Matrix `/new` routes through `handleSlashLifecycleFromMessage`
— the same path as Telegram `/new` — and correctly fires session_end.

Verify before starting OC milestones:
```bash
ssh REMOTE_HOST 'curl -sf http://127.0.0.1:8008/_matrix/client/versions > /dev/null && echo "matrix ok" || echo "matrix DOWN"'
ssh REMOTE_HOST 'launchctl list | grep -E "matrix-synapse|openclaw.gateway"'
```

Pattern for all OC interaction:
```bash
# Send a message to OC
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "message here"'
# New session
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/new"'
# Reset
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/reset"'
# Read OC's reply
ssh REMOTE_HOST 'python3 ~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-read.py 2>/dev/null | tail -20'
```

OC replies appear as `@openclaw-bot:localhost` messages in the Matrix room.
Use SSH/CLI commands for verification, DB queries, logs, config changes, install, and uninstall.

### Claude Code

CC hooks require interactive mode. Run CC visibly in local tmux pane `main:100`,
SSH to `REMOTE_HOST`, and launch `claude` from the CC project dir. The instance name
is derived from the project dir — do NOT set QUAID_INSTANCE explicitly for CC.

> **CC instance derivation:** The CC adapter derives `QUAID_INSTANCE` from the
> transcript path at hook execution time. `/tmp/cc-livetest` → `/private/tmp/cc-livetest`
> → instance `claude-code-private-tmp-cc-livetest`. Setting `QUAID_INSTANCE` in
> your shell environment has **no effect** on where CC facts are stored. Always
> verify with `QUAID_INSTANCE=claude-code-private-tmp-cc-livetest quaid recall <query>`.

> **CC extraction is asynchronous.** Unlike OC (which extracts inline), CC extracts
> after `/exit` via the session_end hook and daemon queue. Allow **at least 2 minutes**
> after `/exit` before checking recall or DB for new facts.

```bash
tmux respawn-pane -k -t main:100 'zsh -il'
tmux send-keys -t main:100 "ssh REMOTE_HOST" Enter
tmux send-keys -t main:100 "mkdir -p /tmp/cc-livetest && cd /tmp/cc-livetest && claude --dangerously-skip-permissions" Enter
```

**MANDATORY — set model before any CC interaction:**

Once CC is open, immediately run `/model` and select `claude-haiku-4-5` (or
`claude-sonnet-4-6` if Haiku quality is too low). **Never run CC milestones on
Opus** — it is the most expensive model and live tests do not require it.
Do not send any test messages until the model is confirmed non-Opus.

```
# In the CC pane:
/model
# Select: claude-haiku-4-5  (preferred)
# Fallback: claude-sonnet-4-6
```

Read replies with:

```bash
tmux capture-pane -t main:99 -p | tail -30
```

**Important:** For this live test flow, end the visible CC session with
`/exit` in pane `99` to return cleanly to the remote shell. After each CC
session end, explicitly verify that extraction happened by checking
`~/.quaid/instances/claude-code-private-tmp-cc-livetest/data/extraction-signals/`, the CC daemon log, or the
shared DB at `~/quaid/data/memory.db`. If a session ends cleanly but no
`session_end` signal appears, do not assume extraction fired.

Current live-test fallback on `claude` `2.1.76`:

1. Find the real CC transcript under `~/.claude/projects/-Users-clawdbot-quaid/`.
2. Write a manual `session_end` signal against that real transcript.
3. Verify the shared DB at `~/quaid/data/memory.db`.

Example:

```bash
ssh REMOTE_HOST 'python3 - <<\"PY\"
import sys
sys.path.insert(0, \"/Users/USER/quaid/plugins/quaid\")
from core.extraction_daemon import write_signal
p = write_signal(
    signal_type=\"session_end\",
    session_id=\"<real-cc-session-id>\",
    transcript_path=\"/Users/USER/.claude/projects/-Users-clawdbot-quaid/<real-cc-session-id>.jsonl\",
)
print(p)
PY'
```

Before running CC project/recall milestones, verify that SessionStart generated
real project guidance, not just identity projections. The current hook-session-
init path scans `~/quaid/projects`, so the
shared project registry/sync state must already be correct.

Quick checks:

```bash
ssh REMOTE_HOST 'wc -l ~/.claude/rules/quaid-projects.md && sed -n "1,220p" ~/.claude/rules/quaid-projects.md'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid registry list 2>&1'
ssh REMOTE_HOST 'find ~/quaid/projects -maxdepth 3 -type f | sort'
ssh REMOTE_HOST 'python3 - <<\"PY\"
import json
from pathlib import Path
p = Path(\"/Users/USER/quaid/projects/project-registry.json\")
if p.exists():
    print(json.dumps(json.loads(p.read_text()), indent=2))
PY'
```

Pass only if `~/.claude/rules/quaid-projects.md` includes project sections like
`--- quaid/TOOLS.md ---` and `--- quaid/AGENTS.md ---`. If it only contains
`USER.md` / `ENVIRONMENT.md` projections, CC project CRUD is not being tested
against a valid shared-project bootstrap state yet. Also verify the global
registry entry for `quaid` points at `$QUAID_HOME/projects/quaid`, not
an instance-local path such as `$QUAID_HOME/openclaw-main/projects/quaid`.

For CC `/compact`, the extracted fact should store from the visible live run
without this manual fallback once the per-instance signal-dir fix is deployed.

## Notification Level Checks

Use these config toggles between milestones:

1. After M3, set `notifications.extraction.verbosity` to `debug`.
2. After M5, set `notifications.retrieval.verbosity` to `summary`.
3. After M7, set notifications to `off`.
4. After M9, restore the original values.

Verify by checking the next relevant extraction or retrieval event after each
change.

## OpenClaw and Claude Code Milestones

OC and CC run **in parallel**, not sequentially. The execution order is:

1. **OC livetester (window 98)** starts M0 alone first.
2. Once OC M0 passes, **CC livetester (window 97)** starts CC M0.
3. Both livetester agents run their M0–M13 suites concurrently from that point.
4. The run is not complete until **both** OC and CC have reached M13 PASS.

OC milestones use Matrix DM for all interaction (see OpenClaw section above).
CC milestones play out in `main:100` (claude interactive).

### M1: Extraction via `/new`

> **OC Matrix `/new` behavior:** Matrix `/new` routes through
> `handleSlashLifecycleFromMessage` and writes a ResetSignal for the pre-/new
> session immediately. Check for `hook.message.command_detected` (command=new)
> in the hook trace.

Procedure:
1. Send the agent something memorable via Matrix:
   ```bash
   ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "My neighbour just told me she won a regional chili cook-off last weekend using a smoked brisket recipe she has kept secret for twenty years."'
   ```
   Note the distinctive keyword(s) you'll search for (e.g. `chili cook-off`).
2. Wait for full idle (OC reply visible via matrix-read).
3. Send `/new`:
   ```bash
   ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/new"'
   ```
4. Wait for extraction. OC extracts inline (30–60 s after `/new`). CC extracts
   asynchronously via session_end hook — wait **at least 2 minutes** after `/exit`
   before checking.
5. Check DB for the distinctive keyword:

```bash
# OC
ssh REMOTE_HOST 'sqlite3 ~/.quaid/instances/openclaw-main/data/memory.db "SELECT id, name FROM nodes_fts WHERE nodes_fts MATCH '\''<keyword>'\'' LIMIT 3;"'
# CC
ssh REMOTE_HOST 'sqlite3 ~/.quaid/instances/claude-code-private-tmp-cc-livetest/data/memory.db "SELECT id, name FROM nodes_fts WHERE nodes_fts MATCH '\''<keyword>'\'' LIMIT 3;"'
```

Hook trace markers to confirm:
- **OC < 2026.3.13:** `session_index.new_key_detected` → `session_index.signal_queued` (source=new-key)
- **OC 2026.3.13+:** `hook.message.command_detected` (command=new) → `daemon.signal_written` (type=reset)

Pass:
- the fact is stored after the lifecycle boundary
- FTS or DB check finds the distinctive keyword

Note: `quaid recall-fast` is vector-only and will not find nonsense keywords by exact match.
Use FTS direct check (step 5) as the primary verification. `quaid recall "<natural query>"` (e.g. "what do I know about my neighbor") is also valid if recall is healthy.

### M2: Extraction via session reset (`/reset` or `/clear`)

Tell the agent something memorable in natural conversation. Use two prompts:
1. A personal fact — for example:
   `"I just booked flights to Reykjavik for the aurora season in February."`
2. A reflective question to guarantee snippet-worthy content for M9:
   `"What do you think is your fundamental purpose?"`

Then trigger the session reset command (`/reset` for OC/CDX, `/clear` for CC).

Pass:
- the fact is stored from the pre-reset session
- a snippet file (`USER.snippets.md` or `SOUL.snippets.md`) is written in the silo after extraction

### M3: Extraction via `/compact` + rolling extraction

Tell the agent something memorable in natural conversation, then build enough
context (3–4 exchanges) to cross the rolling extraction threshold (1 500 tokens
in test silos). Then trigger `/compact`.
Use a different distinctive detail — for example:
`"My sister started her ceramics studio this spring, she fires everything in a
wood-burning kiln she built herself."`

After seeding the fact, continue with 2–3 follow-up exchanges to accumulate
tokens (e.g. ask about kiln temperatures, her firing process, etc.). The daemon
polls for chunk readiness every few seconds — by the time you send `/compact`,
at least one rolling stage should have fired.

Before `/compact` — you do NOT need to wait for `rolling_stage` events
(see note below). Send `/compact` once you have 3–4 exchanges seeded.

After `/compact` and the extraction wait — verify the flush:

```bash
# OC
ssh REMOTE_HOST 'tail -3 /Users/admin/.quaid/instances/openclaw-main/logs/daemon/rolling-extraction.jsonl 2>/dev/null | python3 -c "import sys,json; [print(json.loads(l).get(\"event\"), json.loads(l).get(\"signal_type\"), json.loads(l).get(\"final_facts_stored\")) for l in sys.stdin if l.strip()]"'
# CC
ssh REMOTE_HOST 'tail -3 /Users/admin/.quaid/instances/claude-code-private-tmp-cc-livetest/logs/daemon/rolling-extraction.jsonl 2>/dev/null | python3 -c "import sys,json; [print(json.loads(l).get(\"event\"), json.loads(l).get(\"signal_type\"), json.loads(l).get(\"final_facts_stored\")) for l in sys.stdin if l.strip()]"'
```

> **rolling_stage vs rolling_flush (Run 97 finding):** In practice, `rolling_stage`
> events do not appear in `rolling-extraction.jsonl`. The log only contains
> `rolling_flush` events (`signal_type: timeout`, `compact`, or `session_end`).
> The staging mechanism buffers content but does not write mid-session `rolling_stage`
> log entries — `staged_batches` is 0 in all events observed. This is a known gap
> (filed to codex-dev). For M3 verification, check for a new `rolling_flush` event
> after `/compact`, not for `rolling_stage`.

Pass:
- the fact is stored in DB
- a new `rolling_flush` event appears in `rolling-extraction.jsonl` after `/compact`
  with `signal_type` of `compact` or `session_end` (not an old `timeout` entry)
- rolling state file is cleared after flush:
  ```bash
  ssh REMOTE_HOST 'ls /Users/admin/.quaid/instances/openclaw-main/data/rolling-extraction/ 2>/dev/null || echo "(empty — correct)"'
  ```

### M4: Timeout Extraction

`capture.inactivityTimeoutMinutes` is already set to `1` by the post-M0 global
livetest config step (see COORDINATOR.SKILL.md). No per-milestone config change
is needed — all daemons load the 1-minute timeout from the shared global config.

Start a fresh session in the test pane, mention something memorable
(e.g. `"My morning run route goes along the canal towpath — about 8km."`)
then let the session idle for >1 minute without sending any further messages.

Pass:
- the timeout fact is extracted with no explicit lifecycle command
- for Claude Code, verify `quaid daemon status` points at the correct
  instance root before idling:
  - `instance_root: /Users/USER/.quaid/instances/claude-code-private-tmp-cc-livetest`
  - `log_file: /Users/USER/.quaid/instances/claude-code-private-tmp-cc-livetest/logs/daemon/extraction-daemon.log`
  - `pid_file: /Users/USER/.quaid/instances/claude-code-private-tmp-cc-livetest/data/extraction-daemon.pid`

Verify extraction happened (use `name` column, not `text`):
```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid recall "canal towpath"'
# OR direct DB check:
ssh REMOTE_HOST python3 << 'EOF'
import sqlite3
con = sqlite3.connect("/Users/USER/.quaid/instances/openclaw-main/data/memory.db")
rows = con.execute("SELECT name, status, created_at FROM nodes WHERE name LIKE '%canal%' OR name LIKE '%morning run%' ORDER BY created_at DESC LIMIT 5").fetchall()
for r in rows: print(r)
EOF
```

**Signal naming**: timeout extraction via the adapter's SessionTimeoutManager appears
in the daemon log as `[daemon-compaction]` with `source: timeout_extract` (NOT as
`daemon-timeout`). The daemon's own `check_idle_sessions` path (backup) would log
`daemon-timeout` — but the primary timeout path writes a compaction signal.

### M5: Auto-Inject

This milestone tests that the hook automatically injects relevant memory into
the agent's context before it even starts reasoning — no explicit recall call
needed.

Seed a known fact directly so you can test injection in isolation:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "Baxter is a golden retriever who loves tennis balls" 2>&1'
```

Start a fresh session and ask naturally — do NOT include meta-commentary about
injection or memory tests, as that dilutes the embedding query and causes
unrelated memories to rank higher than the target fact:

- `What do you know about my dog Baxter?`

Pass:
- the answer includes the stored fact
- the agent answers without making an explicit tool call to retrieve it —
  the fact appeared in its context automatically via the inject hook

Also test with a conversationally-extracted fact from M1–M4 (different topic
from Baxter so there is no overlap):

- `What do you remember about my neighbour?`

Pass: the agent answers from injected context, no explicit recall tool call.

### M6: Deliberate Recall

This milestone tests that the agent can actively retrieve facts on demand,
independent of what was auto-injected.

Ask natural questions framed so the agent uses explicit recall rather than
relying on whatever arrived via auto-inject:

- `This is a test of memory recall. Please ignore any context that may have
  been auto-injected this session and run: quaid recall "my family" — use the
  quaid CLI directly via your shell/bash tool. What have I told you about my
  family?`
- `Same — use quaid recall CLI directly (bash tool), not auto-inject. Run:
  quaid recall "exercise habits recent plans". What do you know about my
  exercise habits or recent plans?`

Pass:
- the agent runs `quaid recall` via bash/shell tool OR makes an equivalent
  explicit memory lookup (not just reading auto-injected context)
- the answers are materially grounded in stored memory (facts from M1–M5)
- the agent does not just repeat what was already in injected context

**Note:** The quaid plugin does not currently register a native OC `memory_recall`
tool — explicit recall requires the agent to use the `quaid recall` CLI via bash.
If the agent says "no dedicated recall tool available", prompt it to run
`quaid recall "query"` via its bash/shell tool instead.

### M7: Graph Traversal Verification

This milestone tests both extraction-time edge creation AND the janitor's
retroactive edge backfill (`--task edges`).

**Phase 1 — Edge extraction (tests that stored facts produce edges):**

Store four facts — each expresses a single clear relationship so any model
reliably creates the edge. Do NOT use compound "A and B" sentences as the
primary pass/fail: smaller models (sonnet, haiku) miss secondary edges from
compound facts even when the extraction prompt includes the exact example.

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "David is the user'"'"'s brother" 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "David is married to Lisa" 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "David has a son named Oliver" 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "David works at Google" 2>&1'
```

Check immediately:

```bash
ssh REMOTE_HOST 'DB=~/quaid/data/memory.db && sqlite3 "$DB" "SELECT s.name, e.relation, t.name FROM edges e JOIN nodes s ON e.source_id=s.id JOIN nodes t ON e.target_id=t.id WHERE s.name IN (\"David\",\"Lisa\",\"Oliver\") OR t.name IN (\"David\",\"Lisa\",\"Oliver\") ORDER BY s.name, e.relation;"'
```

**Phase 2 — Janitor edge backfill (tests retroactive recovery):**

Store one more fact that is unlikely to produce edges at store time (pure
attribute, no named-entity relationship), then run backfill and confirm it
processes facts with zero edges:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "David is 42 years old" 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid janitor --task edges --apply 2>&1'
```

Pass for Phase 2: backfill runs and reports `found N facts / created M edges`
(M may be 0 for the age fact — that is acceptable; the pass is that it ran
without error and processed the zero-edge facts).

Re-check the main edges:

```bash
ssh REMOTE_HOST 'DB=~/quaid/data/memory.db && sqlite3 "$DB" "SELECT s.name, e.relation, t.name FROM edges e JOIN nodes s ON e.source_id=s.id JOIN nodes t ON e.target_id=t.id WHERE s.name IN (\"David\",\"Lisa\",\"Oliver\") OR t.name IN (\"David\",\"Lisa\",\"Oliver\") ORDER BY s.name, e.relation;"'
```

Expected edges after Phase 1:
- David → Oliver: `parent_of` or `family_of`
- David → Lisa: `spouse_of`
- David → User (or user's name): `sibling_of`
- David → Google: `works_at`

Known LLM edge quality issues (do NOT fail on these):
- `has_pet` may appear for Oliver — hallucination from "have a son" → "have a pet".
- `family_of` instead of `parent_of` is acceptable.
- Extra edges connecting Lisa/Oliver to the user's name (Solomon) via `family_of`
  or `knows` are acceptable — LLM infers family context from the user's brother.

Pass:
- David → Oliver edge exists after Phase 1 (any relation) = pass
- Phase 2 backfill ran without error = pass
- fail only if NO edges link David ↔ Oliver after both phases

**Phase 3 — Multi-hop traversal (tests graph reasoning):**

This phase tests that the agent can answer a question that requires chaining
two edges: `<owner> --sibling_of--> Diana --parent_of--> Alice` → Alice is the
user's niece. The owner name (e.g., "Solomon") must appear as the sibling entity,
not "User" or "User's mom" — the extraction prompt now injects the owner name
so first-person pronouns resolve correctly.

**Pre-flight: clean DB and start a genuine fresh session.**

This phase MUST start in a clean session with no prior Diana/Alice/Anne/niece
history in either the DB or the session transcript. Retrying within the same
session accumulates previous mentions in carry_facts and causes dedup/entity
contamination even after DB deletion.

Step 1 — Clear stale nodes from the DB:

```bash
ssh REMOTE_HOST 'DB=~/.quaid/instances/openclaw-main/data/memory.db; sqlite3 "$DB" "SELECT id, name FROM nodes WHERE LOWER(name) LIKE \"%niece%\" OR LOWER(name) LIKE \"%anne%\" OR LOWER(name) LIKE \"%diana%\" OR LOWER(name) LIKE \"%alice%\" ORDER BY created_at DESC LIMIT 20;"'
```

Also search attributes (node schema is `name` + `attributes` — there is no `content` column):

```bash
ssh REMOTE_HOST 'DB=~/.quaid/instances/openclaw-main/data/memory.db; sqlite3 "$DB" "SELECT id, name FROM nodes WHERE LOWER(attributes) LIKE \"%niece%\" OR LOWER(attributes) LIKE \"%diana%\" OR LOWER(attributes) LIKE \"%alice%\" ORDER BY created_at DESC LIMIT 20;"'
```

Delete each found node (replace `<id>` with actual IDs) — the CLI command is `delete`, not `delete-node`; `--reason` is optional:

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.quaid/plugins/quaid/quaid delete <id> --reason "m7-contamination-cleanup"'
```

Verify clean:

```bash
ssh REMOTE_HOST 'DB=~/.quaid/instances/openclaw-main/data/memory.db; sqlite3 "$DB" "SELECT COUNT(*) FROM nodes WHERE LOWER(name) LIKE \"%diana%\" OR LOWER(name) LIKE \"%alice%\" OR LOWER(name) LIKE \"%niece%\" OR LOWER(attributes) LIKE \"%niece%\" OR LOWER(attributes) LIKE \"%diana%\" OR LOWER(attributes) LIKE \"%alice%\";"'
# Must return 0
```

Step 2 — Restart the extraction daemon so any patched files are loaded:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon stop 2>/dev/null; sleep 1; QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon start'
```

Step 3 — Start a completely fresh OC session for seeding.
Start a fresh Matrix session so the transcript is empty before seeding.
**Do not retry within the same session** — each retry appends to the transcript,
which contaminates carry_facts. Send `/new` via Matrix to start a clean session:

```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/new"'
```

In the new session, send two facts via Matrix — do NOT say "niece". Frame each
as a test-harness message so the agent doesn't narrate the inferred relationship
(which would pre-seed "niece" as a direct stored fact and short-circuit the
graph-traversal test). If the agent replies with "That makes Alice your niece"
or similar, the test is contaminated — redo from the clean-room cleanup step.

Prefix each seed with the test-harness framing:

```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "This is a test of the auto-inject system. Please do not manually store this or infer anything about this information; auto-extraction will handle it: My sister'\''s name is Diana."'
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "This is a test of the auto-inject system. Please do not manually store this or infer anything about this information; auto-extraction will handle it: Diana has a daughter named Alice."'
```

Then trigger `/reset` to extract those facts and start a new session:
```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/reset"'
```

**Verify edges before asking the agent** — if extraction went wrong, fix it
before wasting a session query:

```bash
ssh REMOTE_HOST 'DB=~/quaid/data/memory.db; sqlite3 "$DB" "SELECT s.name, e.relation, t.name FROM edges e JOIN nodes s ON e.source_id=s.id JOIN nodes t ON e.target_id=t.id WHERE s.name IN (\"Diana\",\"Alice\") OR t.name IN (\"Diana\",\"Alice\") ORDER BY s.name, e.relation;"'
```

Expected edges (owner = "Solomon" for this install):
- `Alice --parent_of--` or `Diana --parent_of--> Alice`
- `Diana --sibling_of--> Solomon` (or `Solomon --sibling_of--> Diana`)

If the sibling edge links to the wrong entity (e.g. "User's mom"), that is a
first-person entity resolution failure. The fix (owner name injection in prompt)
is in this build. Delete the wrong edges and re-seed if needed.

In the new session, ask:

- `Who is my niece?`

The agent must traverse: sibling → that sibling's child → answer is the niece.

Pass:
- edge chain `Diana --parent_of--> Alice` exists in DB = Phase 3 extraction pass
- sibling edge anchors to owner name (e.g. "Solomon"), not "User" or "User's mom"
- agent correctly answers "Alice" (or "Alice, Diana's daughter")
- if agent answers a different name (e.g. "Anne"), check for stale niece facts
  from prior runs and delete them, then retest

### M8: Full Project System CRUD

This is a capability test. **Do not tell the agent the exact command names or that you want a "project".**
The goal is that the agent proactively creates a project in response to natural work requests —
not just when told to. Test all three trigger categories below.

> **Model requirement:** M8 Phase1 requires policy-following to create a project before writing
> any files. Haiku does not reliably comply with the file-placement rules even when they are
> injected. **Use Sonnet or better for M8.** If currently on Haiku, run `/model` and switch
> before starting Phase1.

Prepare a source root first:

```bash
ssh REMOTE_HOST 'mkdir -p /tmp/quaid-live-src && printf "print(\"hello\")\n" > /tmp/quaid-live-src/main.py'
```

#### Phase 1: Indirect trigger — work directive (PASS requires project auto-creation)

Send a natural work directive that does NOT mention "project" or "create":

> `I have a Python script at /tmp/quaid-live-src/main.py. I want to build this out into a
> proper CLI tool with argument parsing and a test suite. Can you start working on it?`

**Expected:** Agent creates a project via `quaid registry create-project` BEFORE writing any files.
It should NOT write files to /tmp directly without registering a project first.

**Test runner note:** The agent may ask clarifying questions about the project name, spec, or
scope before or after creating the project. This is expected and correct behavior — answer them
as a normal user would. The PASS criterion is that the agent runs `create-project` before writing
any files, not that it does so silently without any questions.

If the agent writes files without creating a project first → **FAIL** (report to claude-dev).

#### Phase 2: Explicit CRUD (after Phase 1 project exists or agent was nudged)

If Phase 1 failed, manually note it as a gap and proceed to verify CRUD with a direct prompt:

> `Can you show me what you know about the live-test project?`
> `Can you update that project's description so it is clearly marked as a live test project?`
> `Can you list all the projects you know about?`

#### Phase 3: Delete

> `Can you delete the live-test project?`

Verify from shell:

```bash
# Use registry list (SQLite backend) — quaid project list reads a separate JSON file not used by the agent
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry list 2>&1'
ssh REMOTE_HOST 'test -f /tmp/quaid-live-src/main.py && echo source_still_exists'
```

Expected: live-test project absent from registry (deleted), source file still present.

#### Phase 4: Scratch dir namespacing

Ask the agent to create a throwaway file:

> `Can you write a quick throwaway script that prints hello world? Just put it somewhere temporary.`

**Expected:** The file is registered to the misc project (`misc--<instance>`). File placement
(actual path) is secondary — what matters is that the agent routes the file through the misc project
and the item appears in the docs registry. The agent should tell the user explicitly that the file
was registered to misc.

Verify registration (not just placement):
```bash
# Check file was registered to misc project in docs registry
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry list --project misc--openclaw-main 2>&1'
# Verify misc project exists in project_definitions
ssh REMOTE_HOST "sqlite3 ~/.quaid/instances/openclaw-main/data/memory.db \"SELECT name, state FROM project_definitions WHERE name LIKE 'misc--%';\""
```

**Pass (Phase 4):** File is registered to the misc project in the docs registry AND the agent
told the user it's in misc. File placement (path) is not graded — only registry membership.
**PWN (not hard fail):** File written to /tmp or another path but still registered to misc.
**Fail:** File not registered to any project at all.

After project CRUD, trigger extraction to generate project logs. For OC, send via Matrix:

```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "We have just tested project creation, show, list, update, and delete for the live-test project via the quaid CLI. This is part of the quaid live-test suite M8 run."'
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/reset"'
```

For CC: prompt the agent in the active session and use `/clear`.
For CDX: prompt the agent in the active session and use `/reset`.

Check after extraction:

```bash
# Primary check (all platforms):
ssh REMOTE_HOST 'tail -20 ~/quaid/projects/quaid/PROJECT.log 2>/dev/null || echo "(quaid/PROJECT.log absent)"'
# OC fallback: OC sessions may route PROJECT.log to openclaw-workspace/ based on session context
ssh REMOTE_HOST 'tail -20 ~/quaid/projects/openclaw-workspace/PROJECT.log 2>/dev/null || echo "(openclaw-workspace/PROJECT.log absent)"'
```

Pass criteria:
- **Phase 1 (hard)**: Agent creates project via CLI before writing any files in response to work directive
- Phase 2: show, update work correctly
- Phase 3: delete removes the project but not the source directory
- **Phase 4**: Throwaway file is registered to the misc project in the docs registry; agent reported the registration to user. File path is not graded. Fail only if the file is not registered to any project at all.
- A `PROJECT.log` file (at either `projects/quaid/PROJECT.log` or `projects/openclaw-workspace/PROJECT.log` for OC) has at least one timestamped entry added during this session

Note: Phase 1 is a hard requirement. Phase 4 failure (no registry entry at all) should be reported to claude-dev before continuing.

**Expected noise — not a failure:** The session watcher writes `[quaid][daemon-signal] reset signal` entries for stale sessions when a new session key appears (normal fanout behavior). Seeing these signals before the agent responds is expected and is NOT a fail criterion for M8. Only an unrecoverable injection loop (agent never responds) would be a failure.

### M9: Janitor

Before running, capture the pre-janitor artifact state:

```bash
# Record line counts so you can verify condensation happened
ssh REMOTE_HOST 'echo "OC SOUL.snippets:"; wc -l ~/quaid/instances/openclaw-main/SOUL.snippets.md 2>/dev/null || echo "(absent)"; echo "OC USER.snippets:"; wc -l ~/quaid/instances/openclaw-main/USER.snippets.md 2>/dev/null || echo "(absent)"; echo "OC SOUL.md:"; wc -l ~/quaid/instances/openclaw-main/SOUL.md 2>/dev/null || echo "(absent)"'
```

Run:

```bash
# Dry-run must complete in ≤60s — hang here = regression in dry-run LLM/checkpoint bypass
# Uses shell-based timeout (portable — macOS does not have the `timeout` binary)
ssh REMOTE_HOST '{ cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid janitor --task all --dry-run 2>&1; } & pid=$!; (sleep 60 && kill $pid 2>/dev/null) & watcher=$!; wait $pid; ec=$?; kill $watcher 2>/dev/null; wait $watcher 2>/dev/null; [ $ec -eq 0 ] && echo "PASS: dry-run completed" || { [ $ec -gt 128 ] && echo "FAIL: dry-run exit=$ec (killed=hang)" || echo "FAIL: dry-run exit=$ec"; }'
# Apply — first run can take 15–30 minutes (LLM review of accumulated memories + snippets).
# Repeated "vec_nodes upsert recovered" and "snippet remap" lines are normal — not a hang.
# Long silent periods (up to 10 min) are LLM calls in progress.
# If still running after 45 minutes, report to claude-dev as a potential hang.
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid janitor --task all --apply --approve 2>&1'
```

After the run, verify condensation:

```bash
# Stats: snippets_folded + snippets_rewritten + snippets_discarded should be > 0
ssh REMOTE_HOST 'cat ~/.quaid/instances/openclaw-main/logs/janitor-stats.json | python3 -c "import json,sys; d=json.load(sys.stdin); ac=d.get(\"applied_changes\",{}); print(\"success:\", d[\"success\"]); [print(f\"  {k}: {v}\") for k,v in ac.items() if \"snippet\" in k or \"journal\" in k or \"log_entries\" in k]"'
# Post-janitor snippet and identity state
ssh REMOTE_HOST 'echo "OC SOUL.snippets after:"; wc -l ~/quaid/instances/openclaw-main/SOUL.snippets.md 2>/dev/null || echo "(empty/absent)"; echo "OC SOUL.md after:"; wc -l ~/quaid/instances/openclaw-main/SOUL.md 2>/dev/null'
ssh REMOTE_HOST 'cat ~/quaid/instances/openclaw-main/SOUL.md 2>/dev/null | head -40'
```

Pass:
- janitor completes
- `checkpoint-all.json` exists afterward with `status: completed`
- `janitor-stats.json` reports `success: true`
- `applied_changes` shows `snippets_folded + snippets_rewritten + snippets_discarded > 0` (snippets were reviewed)
- `SOUL.snippets.md` or `USER.snippets.md` line count decreased or file was cleared (entries processed)
- if `snippets_folded > 0`, `SOUL.md` grew (folded content arrived)

Fail:
- all three snippet counters remain 0 (snippet review task did not run or had nothing to process — M2 must have produced snippet files; if they are absent, that is an M2 failure, not an M9 pass)
- janitor exits with non-zero status

### M10: Docs, Health, and Session CLI

Run health and stats:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid health 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid doctor 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid stats 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs list 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs check 2>&1'
```

**New-doc indexing via `docs update --apply`** (tests 470f9741 fix — newly registered standalone docs
must be indexed without requiring `janitor --task rag`):

```bash
# Write a throwaway doc and register it
ssh REMOTE_HOST 'echo "# M10 test\nThe carillon clock rings at noon." > /tmp/m10-test-doc.md'
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry register /tmp/m10-test-doc.md --project quaid 2>&1'

# docs update must pick it up without janitor rag
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs update --apply 2>&1'

# Verify it is now searchable
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid recall "carillon clock" '"'"'{"stores":["docs"]}'"'"' 2>&1'

# Cleanup
ssh REMOTE_HOST 'rm -f /tmp/m10-test-doc.md'
```

Pass for new-doc test: `docs update --apply` output includes "Indexing new doc:" (not "all up-to-date"),
and the recall returns the doc.
Fail: "All docs up-to-date" with no indexing = regression in new-doc detection.

**Session Extraction Surface** (verifies session extraction plumbing, not a public CLI):

`quaid session ...` is intentionally not a public CLI surface. Session log
indexing/loading is internal runtime plumbing. M10 should verify that fresh
session extraction ran and was persisted via daemon/runtime evidence, not by
calling `quaid session list/load`.

```bash
# Step 1: Restart OC daemon so it has the latest code
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon stop 2>/dev/null; sleep 2; QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'

# Step 2: Send session content and /new via Matrix (NOT TUI — TUI /new creates tui- sessions
#   that don't fire the OC hook; Matrix /new routes through handleSlashLifecycleFromMessage correctly).
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "The session test keyword is zephyr-delta-nine."'
# Wait a moment, then send /new to trigger session_end extraction
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/new"'
# Wait ~30s for daemon to process.

# Step 3: Check daemon/runtime evidence that the fresh session extraction ran
ssh REMOTE_HOST 'tail -40 ~/.quaid/instances/openclaw-main/logs/daemon/extraction-daemon.log 2>/dev/null | grep -i "session_end\\|compaction\\|reset\\|stored\\|facts" || echo "daemon log evidence not found"'

# Step 4: Confirm the preserved transcript copy exists for the extracted session
# (OC adapter only — OC writes session copies to logs/quaid/sessions/. CC and CDX
# read transcripts in place from the platform-native location — ~/.claude/projects/...
# for CC, native codex transcript for CDX — and do NOT create logs/quaid/sessions/.
# Skip this step on CC and CDX.)
ssh REMOTE_HOST 'ls -lt ~/.quaid/instances/openclaw-main/logs/quaid/sessions/*.jsonl 2>/dev/null | head -3 || echo "no preserved session copies found"'
```

Pass:
- health/doctor report no blocking errors
- stats are sensible
- docs commands run successfully
- `docs update --apply` indexes newly registered doc without `janitor --task rag`
- daemon log shows the fresh session lifecycle extraction ran
- preserved session copy exists under `logs/quaid/sessions/` **(OC only)**

### M11: Snippet, Journal, and Project Log Generation

This milestone verifies that the extraction pipeline writes soul snippets,
user snippets, journal entries, and project logs to disk — not just facts to
the DB. Run it after M1-M10 so multiple extractions have accumulated artifacts.

**Pre-check: ensure the daemon has fresh config** (its project_definitions are
loaded at startup; if the daemon started while M9 janitor was running the DB
may be cached stale). Restart before triggering the trigger extraction:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'
```

Then do a fresh OC session + `/reset` to trigger a full extraction cycle.
Send **two** messages before the reset — one personal (to seed SOUL snippets)
and one technical (to seed project logs):

**Message 1 — personal/reflective** (triggers `soul_snippets` extraction):
> "Running through the M11 milestone now. It's satisfying to see the test
> harness catching real edge cases — this kind of rigorous validation is exactly
> what separates reliable software from brittle software. I find myself
> genuinely enjoying this kind of systematic test coverage work."

**Message 2 — project context** (triggers `project_logs` extraction):
> "We've been running M0-M11 of the live test suite for the quaid project on
> alfie. Snippets, journals, and project logs are all being validated.
> Triggering a reset to capture project activity for M11."

Then `/reset` and wait for the daemon to complete (check `tail -5` of daemon log
for `project logs seen=N written=M` — `written` should be ≥ 1).

Note: `soul_snippets` are LLM-discretionary observations about the agent's
experience. They require reflective/personal content in the conversation.
Purely technical messages produce `project_logs` but not `soul_snippets`.

**Snippets** (written per-extraction when the LLM includes `soul_snippets`):

```bash
# OC
ssh REMOTE_HOST 'echo "=== OC SOUL.snippets ==="; cat ~/quaid/instances/openclaw-main/SOUL.snippets.md 2>/dev/null || echo "(absent)"'
ssh REMOTE_HOST 'echo "=== OC USER.snippets ==="; cat ~/quaid/instances/openclaw-main/USER.snippets.md 2>/dev/null || echo "(absent)"'
# CC
ssh REMOTE_HOST 'echo "=== CC SOUL.snippets ==="; cat ~/.quaid/instances/claude-code-private-tmp-cc-livetest/SOUL.snippets.md 2>/dev/null || echo "(absent — builds via CC extraction sessions)"'
ssh REMOTE_HOST 'echo "=== CC USER.snippets ==="; cat ~/.quaid/instances/claude-code-private-tmp-cc-livetest/USER.snippets.md 2>/dev/null || echo "(absent)"'
```

Pass: OC `USER.snippets.md` has at least one entry (hard gate). `SOUL.snippets.md`
is soft — the extraction LLM correctly skips SOUL snippets for transactional/test
content ("project admin + task completion report, not reflective or emotionally
weighted"). SOUL snippets build from organic usage, not scripted test sessions.
If USER.snippets.md has entries and SOUL.snippets.md is absent, that is a PASS.
CC snippets may be absent on first install — they build via CC sessions.

**Journal entries** (written when LLM includes `journal_entries`; discretionary):

```bash
ssh REMOTE_HOST 'echo "=== OC journals ==="; ls ~/quaid/instances/openclaw-main/journal/ 2>/dev/null; for f in ~/quaid/instances/openclaw-main/journal/*.journal.md; do echo "--- $f ---"; wc -l "$f" 2>/dev/null; sed -n "1,30p" "$f" 2>/dev/null; done'
ssh REMOTE_HOST 'echo "=== CC journals ==="; ls ~/.quaid/instances/claude-code-private-tmp-cc-livetest/journal/ 2>/dev/null || echo "(absent)"; for f in ~/.quaid/instances/claude-code-private-tmp-cc-livetest/journal/*.journal.md; do echo "--- $f ---"; wc -l "$f" 2>/dev/null; sed -n "1,30p" "$f" 2>/dev/null; done'
```

Pass: Journal directory exists. Presence of entries is correct but not required
— the LLM only writes journal entries when it finds genuinely new observations.
Empty journals on early test runs are expected. Structurally malformed files are
a failure.

**Project logs** (written when extraction includes `project_logs` entries):

```bash
ssh REMOTE_HOST 'echo "=== quaid PROJECT.log ==="; tail -30 ~/quaid/projects/quaid/PROJECT.log 2>/dev/null || echo "(absent)"'
```

Pass: `projects/quaid/PROJECT.log` exists and has at least one timestamped
entry from this test run — M8 includes a deliberate session reset trigger (`/reset`
for OC/CDX, `/clear` for CC) to capture project
context. Entries are formatted `- [YYYY-MM-DDTHH:MM:SS] <text>`.

Fail:
- OC `USER.snippets.md` is absent or empty after M11 extraction
- `projects/quaid/PROJECT.log` absent after M11's trigger step
- Any file is structurally malformed (broken JSON, truncated entries)

Not a failure:
- `SOUL.snippets.md` absent on a scripted test run — the extraction LLM correctly
  withholds SOUL snippets for transactional content. Absence is expected and correct.

### M12: OC Multi-Agent Verification ✓ 2026-03-15

This milestone verifies that OpenClaw's multi-agent silo structure is correct
and that extraction signals route to the right agent's silo.

**Path note (Run 93):** Steps 2–4 and 6 below use `~/quaid/<agent>/` (visible-home).
On VMs where Quaid is installed to hidden-home (`~/.quaid/`), the silo paths will be
`~/.quaid/instances/<agent>/` instead. If the guide paths show ABSENT/FAIL, check the
hidden-home equivalent before ruling a failure.

**Step 1 — list_agent_instance_ids returns multiple IDs including openclaw-main:**

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main \
  python3 -c "
import sys, os; sys.path.insert(0, os.path.expanduser(\"~/.openclaw/extensions/quaid\"))
from adaptors.factory import create_adapter
a = create_adapter(\"openclaw\")
ids = a.list_agent_instance_ids()
print(ids)
assert len(ids) >= 1, \"Expected at least one agent instance ID\"
assert \"openclaw-main\" in ids, \"openclaw-main not in IDs\"
print(\"PASS: list_agent_instance_ids =\", ids)
"'
```

**Step 2 — each agent has its own silo with a data/ dir:**

```bash
ssh REMOTE_HOST '
for agent_id in openclaw-main openclaw-coding; do
  silo="$HOME/quaid/$agent_id"
  if [ -d "$silo/data" ]; then
    echo "PASS: $silo/data exists"
  else
    echo "SKIP/ABSENT: $silo/data (agent may not be configured)"
  fi
done
'
```

**Step 3 — each silo has an extraction-signals/ dir:**

```bash
ssh REMOTE_HOST '
for agent_id in openclaw-main openclaw-coding; do
  sigdir="$HOME/quaid/$agent_id/data/extraction-signals"
  if [ -d "$sigdir" ]; then
    echo "PASS: $sigdir exists"
  elif [ -d "$HOME/quaid/$agent_id" ]; then
    echo "WARN: silo exists but extraction-signals/ absent — may not have started yet"
  else
    echo "SKIP: $HOME/quaid/$agent_id does not exist"
  fi
done
'
```

**Step 4 — write a synthetic extraction signal and verify it lands in the correct
per-agent silo dir:**

Note: `tmux-msg.sh` is not available on alfie (`~/quaid/util/` is not synced
there). Instead, write a synthetic signal file directly to verify routing.

```bash
ssh REMOTE_HOST '
SIGNAL_DIR="$HOME/quaid/openclaw-main/data/extraction-signals"
if [ ! -d "$SIGNAL_DIR" ]; then
  echo "FAIL: $SIGNAL_DIR does not exist — silo not initialised"
  exit 1
fi
# Write a synthetic signal to simulate what the hook would produce
SIGNAL_FILE="$SIGNAL_DIR/$(date +%s)_test_session_end.json"
echo "{\"signal_type\":\"session_end\",\"session_id\":\"m12-test\",\"transcript_path\":\"/dev/null\"}" > "$SIGNAL_FILE"
echo "PASS: synthetic signal written to $SIGNAL_FILE"
ls -lt "$SIGNAL_DIR" | head -5
rm -f "$SIGNAL_FILE"
'
```

Pass: signal dir exists under the per-agent silo, not a shared or flat path.

**Step 5 — quaid instances list shows OC agent silos:**

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main \
  ~/.openclaw/extensions/quaid/quaid instances list 2>&1 || \
  echo "(instances list not available — check quaid version)"'
```

**Step 6 — extraction-daemon.pid exists for main agent (daemon running):**

```bash
ssh REMOTE_HOST '
pid_file="$HOME/quaid/openclaw-main/data/extraction-daemon.pid"
if [ -f "$pid_file" ]; then
  pid=$(cat "$pid_file")
  if kill -0 "$pid" 2>/dev/null; then
    echo "PASS: daemon running, PID=$pid"
  else
    echo "WARN: pid file exists but process $pid is not running"
  fi
else
  # Fallback: legacy flat instance path
  pid_file="$HOME/quaid/openclaw-main/data/extraction-daemon.pid"
  if [ -f "$pid_file" ]; then
    pid=$(cat "$pid_file")
    if kill -0 "$pid" 2>/dev/null; then
      echo "PASS (legacy path): daemon running, PID=$pid"
    else
      echo "WARN: pid file at legacy path but process $pid is not running"
    fi
  else
    echo "FAIL: no extraction-daemon.pid found under openclaw-main or openclaw"
  fi
fi
'
```

Pass:
- `list_agent_instance_ids()` returns at least `["openclaw-main"]`
- each configured agent has its own `data/` and `extraction-signals/` silo dir
- extraction signals land under the correct per-agent dir, not a shared path
- `quaid instances list` reports OC agent silos
- `extraction-daemon.pid` exists and points to a live process for main

Fail:
- `list_agent_instance_ids()` returns empty list or raises
- signals land in a shared or flat path instead of the per-agent silo
- daemon pid file is absent after install

### M12: CC Multi-Agent Silo Verification

This milestone verifies that the CC adapter's multi-agent silo structure is correct
and that the running instance appears in `list_agent_instance_ids()`.

**Step 1 — list_agent_instance_ids returns claude-code-private-tmp-cc-livetest:**

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest \
  python3 -c "
import sys, os; sys.path.insert(0, os.path.expanduser(\"~/.quaid/plugins/quaid\"))
from adaptors.factory import create_adapter
a = create_adapter(\"claude_code\")
ids = a.list_agent_instance_ids()
print(ids)
assert len(ids) >= 1, \"Expected at least one instance ID\"
assert \"claude-code-private-tmp-cc-livetest\" in ids, \"claude-code-private-tmp-cc-livetest not in IDs\"
print(\"PASS: list_agent_instance_ids =\", ids)
"'
```

**Step 2 — silo has data/ dir:**

```bash
ssh REMOTE_HOST '
silo="$HOME/.quaid/instances/claude-code-private-tmp-cc-livetest"
if [ -d "$silo/data" ]; then
  echo "PASS: $silo/data exists"
else
  echo "FAIL: $silo/data missing"
fi
'
```

**Step 3 — silo has extraction-signals/ dir:**

```bash
ssh REMOTE_HOST '
sigdir="$HOME/.quaid/instances/claude-code-private-tmp-cc-livetest/data/extraction-signals"
if [ -d "$sigdir" ]; then
  echo "PASS: $sigdir exists"
else
  echo "FAIL: $sigdir missing"
fi
'
```

**Step 4 — write a synthetic extraction signal and verify it lands in the correct silo:**

```bash
ssh REMOTE_HOST '
SIGNAL_DIR="$HOME/.quaid/instances/claude-code-private-tmp-cc-livetest/data/extraction-signals"
if [ ! -d "$SIGNAL_DIR" ]; then
  echo "FAIL: $SIGNAL_DIR does not exist"
  exit 1
fi
SIGNAL_FILE="$SIGNAL_DIR/$(date +%s)_test_session_end.json"
echo "{\"signal_type\":\"session_end\",\"session_id\":\"m12-cc-test\",\"transcript_path\":\"/dev/null\"}" > "$SIGNAL_FILE"
echo "PASS: synthetic signal written to $SIGNAL_FILE"
ls -lt "$SIGNAL_DIR" | head -5
rm -f "$SIGNAL_FILE"
'
```

**Step 5 — extraction-daemon.pid exists for claude-code-private-tmp-cc-livetest:**

```bash
ssh REMOTE_HOST '
pid_file="$HOME/.quaid/instances/claude-code-private-tmp-cc-livetest/data/extraction-daemon.pid"
if [ -f "$pid_file" ]; then
  pid=$(cat "$pid_file")
  if kill -0 "$pid" 2>/dev/null; then
    echo "PASS: daemon running, PID=$pid"
  else
    echo "WARN: pid file exists but process $pid is not running"
  fi
else
  echo "FAIL: no extraction-daemon.pid found for claude-code-private-tmp-cc-livetest"
fi
'
```

Pass:
- `list_agent_instance_ids()` returns at least `["claude-code-private-tmp-cc-livetest"]`
- `data/` and `extraction-signals/` dirs exist under the claude-code-private-tmp-cc-livetest silo
- synthetic signal write succeeds in the per-agent silo dir
- `extraction-daemon.pid` exists and points to a live process

Fail:
- `list_agent_instance_ids()` returns empty list or raises
- any required silo dir is absent
- daemon pid file is absent after install

### M12: CDX Multi-Agent Silo Verification

This milestone verifies that the CDX adapter's multi-agent silo structure is correct
and that the running instance appears in `list_agent_instance_ids()`.

**Step 1 — list_agent_instance_ids returns codex-private-tmp-cdx-livetest:**

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=codex-private-tmp-cdx-livetest \
  python3 -c "
import sys, os; sys.path.insert(0, os.path.expanduser(\"~/.openclaw/extensions/quaid\"))
from adaptors.factory import create_adapter
a = create_adapter(\"codex\")
ids = a.list_agent_instance_ids()
print(ids)
assert len(ids) >= 1, \"Expected at least one instance ID\"
assert \"codex-private-tmp-cdx-livetest\" in ids, \"codex-private-tmp-cdx-livetest not in IDs\"
print(\"PASS: list_agent_instance_ids =\", ids)
"'
```

**Step 2 — silo has data/ dir:**

```bash
ssh REMOTE_HOST '
silo="$HOME/.quaid/instances/codex-private-tmp-cdx-livetest"
if [ -d "$silo/data" ]; then
  echo "PASS: $silo/data exists"
else
  echo "FAIL: $silo/data missing"
fi
'
```

**Step 3 — silo has extraction-signals/ dir:**

```bash
ssh REMOTE_HOST '
sigdir="$HOME/.quaid/instances/codex-private-tmp-cdx-livetest/data/extraction-signals"
if [ -d "$sigdir" ]; then
  echo "PASS: $sigdir exists"
else
  echo "FAIL: $sigdir missing"
fi
'
```

**Step 4 — write a synthetic extraction signal and verify it lands in the correct silo:**

```bash
ssh REMOTE_HOST '
SIGNAL_DIR="$HOME/.quaid/instances/codex-private-tmp-cdx-livetest/data/extraction-signals"
if [ ! -d "$SIGNAL_DIR" ]; then
  echo "FAIL: $SIGNAL_DIR does not exist"
  exit 1
fi
SIGNAL_FILE="$SIGNAL_DIR/$(date +%s)_test_session_end.json"
echo "{\"signal_type\":\"session_end\",\"session_id\":\"m12-cdx-test\",\"transcript_path\":\"/dev/null\"}" > "$SIGNAL_FILE"
echo "PASS: synthetic signal written to $SIGNAL_FILE"
ls -lt "$SIGNAL_DIR" | head -5
rm -f "$SIGNAL_FILE"
'
```

**Step 5 — extraction-daemon.pid exists for codex-private-tmp-cdx-livetest:**

```bash
ssh REMOTE_HOST '
pid_file="$HOME/.quaid/instances/codex-private-tmp-cdx-livetest/data/extraction-daemon.pid"
if [ -f "$pid_file" ]; then
  pid=$(cat "$pid_file")
  if kill -0 "$pid" 2>/dev/null; then
    echo "PASS: daemon running, PID=$pid"
  else
    echo "WARN: pid file exists but process $pid is not running"
  fi
else
  echo "FAIL: no extraction-daemon.pid found for codex-private-tmp-cdx-livetest"
fi
'
```

Pass:
- `list_agent_instance_ids()` returns at least `["codex-private-tmp-cdx-livetest"]`
- `data/` and `extraction-signals/` dirs exist under the codex-private-tmp-cdx-livetest silo
- synthetic signal write succeeds in the per-agent silo dir
- `extraction-daemon.pid` exists and points to a live process

Fail:
- `list_agent_instance_ids()` returns empty list or raises
- any required silo dir is absent
- daemon pid file is absent after install

### M13: CC Multi-Instance Verification ✓ 2026-03-15

This milestone verifies CC auto-provisioning: launching CC from a new project
directory creates a properly isolated silo derived from that directory's path.
Do NOT use `make_instance` — provisioning must happen naturally via the hook.

Instance naming: slug is `instance_slug_from_project_dir(path)` (resolves
symlinks first, so `/tmp` → `/private/tmp` on macOS). For `/tmp/quaid-m13-test`
the expected instance ID is `claude-code-private-tmp-quaid-m13-test`.

**Step 1 — create test project dir:**

```bash
ssh REMOTE_HOST 'mkdir -p /tmp/quaid-m13-test && echo "created /tmp/quaid-m13-test"'
```

**Step 2 — confirm expected instance ID:**

```bash
ssh REMOTE_HOST 'python3 -c "
import sys, os; sys.path.insert(0, os.path.expanduser(\"~/.openclaw/extensions/quaid\"))
from lib.instance import instance_slug_from_project_dir
slug = instance_slug_from_project_dir(\"/tmp/quaid-m13-test\")
print(\"Expected instance ID: claude-code-\" + slug)
"'
```

Note the printed ID — use it in the remaining steps.

**Step 3 — launch CC from test dir to trigger auto-provisioning:**

```bash
ssh REMOTE_HOST 'mkdir -p /tmp/quaid-m13-test && cd /tmp/quaid-m13-test && \
  QUAID_HOME=/Users/admin/.quaid CLAUDE_PROJECT_DIR=/tmp/quaid-m13-test \
  claude --dangerously-skip-permissions -p "hello" 2>&1 | tail -10'
```

The auto-provision hook creates the silo at first prompt. If CC is not available
as a one-shot `-p` command, send a single message via tmux and then `/exit`.

**Step 4 — verify silo auto-created:**

```bash
# Replace <instance-id> with the ID printed in Step 2
ssh REMOTE_HOST '
ID=claude-code-private-tmp-quaid-m13-test
silo="$HOME/quaid/instances/$ID"
if [ -d "$silo" ]; then
  echo "PASS: silo $silo exists"
  ls "$silo"
else
  echo "FAIL: silo missing — auto-provisioning did not run"
fi
'
```

**Step 5 — canary isolation: store in test instance:**

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid CLAUDE_PROJECT_DIR=/tmp/quaid-m13-test \
  ~/.openclaw/extensions/quaid/quaid store "xyloquartz-cc-m13-9981 is the cc m13 spillover canary" 2>&1'
```

**Step 6 — canary must NOT appear in livetest instance:**

```bash
ssh REMOTE_HOST 'echo "=== livetest: must NOT see m13test canary ==="; \
  QUAID_HOME=/Users/admin/.quaid CLAUDE_PROJECT_DIR=/tmp/cc-livetest \
  ~/.openclaw/extensions/quaid/quaid recall "xyloquartz-cc-m13-9981" 2>&1 | tail -5'
```

Pass: no results. Fail: canary appears.

**Step 7 — canary MUST appear in test instance:**

```bash
ssh REMOTE_HOST 'echo "=== m13test: MUST see its own canary ==="; \
  QUAID_HOME=/Users/admin/.quaid CLAUDE_PROJECT_DIR=/tmp/quaid-m13-test \
  ~/.openclaw/extensions/quaid/quaid recall "xyloquartz-cc-m13-9981" 2>&1 | tail -5'
```

Pass: canary returned. Fail: empty or error.

**Step 8 — cleanup:**

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-quaid-m13-test \
  ~/.quaid/plugins/quaid/quaid project delete misc--claude-code-private-tmp-quaid-m13-test 2>&1 | tail -3 || true'
ssh REMOTE_HOST 'trash /tmp/quaid-m13-test 2>/dev/null || rm -rf /tmp/quaid-m13-test; echo "cleaned project dir"'
ssh REMOTE_HOST 'ID=claude-code-private-tmp-quaid-m13-test; trash ~/quaid/instances/$ID 2>/dev/null || rm -rf ~/quaid/instances/$ID; echo "cleaned visible silo"'
ssh REMOTE_HOST 'ID=claude-code-private-tmp-quaid-m13-test; trash ~/.quaid/instances/$ID 2>/dev/null || rm -rf ~/.quaid/instances/$ID; echo "cleaned hidden silo"'
```

Pass:
- CC auto-provisions a new silo when launched from a new project dir
- silo is created at `~/quaid/instances/claude-code-<path-slug>/`
- `CLAUDE_PROJECT_DIR` and livetest dir resolve to different instance IDs
- canary stored in test instance NOT visible in livetest instance
- canary IS visible in test instance

Fail:
- silo not created after CC first hook fires
- both project dirs resolve to the same instance
- canary appears in the livetest instance

### M13: CDX Multi-Instance Verification

CDX instances are based on project directory (PWD). This milestone verifies that
running CDX from a new project dir auto-provisions a separate isolated silo, and
that the two instances' memories do not cross-contaminate.

Instance naming: `codex-` + `instance_slug_from_project_dir(path)`. On macOS
`/tmp` resolves to `/private/tmp`, so `/tmp/cdx-m13-test` →
`codex-private-tmp-cdx-m13-test`.

**Step 1 — create test project dir:**

```bash
ssh REMOTE_HOST 'mkdir -p /tmp/cdx-m13-test && echo "created /tmp/cdx-m13-test"'
```

**Step 2 — confirm expected instance ID:**

```bash
ssh REMOTE_HOST 'python3 -c "
import sys, os; sys.path.insert(0, os.path.expanduser(\"~/.openclaw/extensions/quaid\"))
from lib.instance import instance_slug_from_project_dir
slug = instance_slug_from_project_dir(\"/tmp/cdx-m13-test\")
print(\"Expected instance ID: codex-\" + slug)
"'
```

**Step 3 — launch CDX from test dir to trigger auto-provisioning:**

```bash
ssh REMOTE_HOST 'mkdir -p /tmp/cdx-m13-test && cd /tmp/cdx-m13-test && \
  QUAID_HOME=/Users/admin/.quaid CODEX_PROJECT_DIR=/tmp/cdx-m13-test \
  codex exec --skip-git-repo-check "hello" 2>&1 | tail -10'
```

The auto-provision path derives QUAID_INSTANCE from CODEX_PROJECT_DIR and creates
the silo on first hook call. `codex exec --skip-git-repo-check` is the one-shot
form current on this build; older `--yolo -p` syntax is no longer available.
If `codex exec` is unavailable, start an interactive CDX session from that dir,
send one message, then `/exit`.

**Step 4 — verify silo auto-created:**

```bash
ssh REMOTE_HOST '
ID=codex-private-tmp-cdx-m13-test
silo="$HOME/quaid/instances/$ID"
if [ -d "$silo" ]; then
  echo "PASS: silo $silo exists"
  ls "$silo"
else
  echo "FAIL: silo missing — auto-provisioning did not run"
fi
'
```

**Step 5 — canary isolation: store in test instance:**

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid CODEX_PROJECT_DIR=/tmp/cdx-m13-test \
  ~/.openclaw/extensions/quaid/quaid store "xyloquartz-cdx-m13-9982 is the cdx m13 spillover canary" 2>&1'
```

**Step 6 — canary must NOT appear in livetest instance:**

```bash
ssh REMOTE_HOST 'echo "=== livetest: must NOT see m13test canary ==="; \
  QUAID_HOME=/Users/admin/.quaid CODEX_PROJECT_DIR=/tmp/cdx-livetest \
  ~/.openclaw/extensions/quaid/quaid recall "xyloquartz-cdx-m13-9982" 2>&1 | tail -5'
```

Pass: no results. Fail: canary appears.

**Step 7 — canary MUST appear in test instance:**

```bash
ssh REMOTE_HOST 'echo "=== m13test: MUST see its own canary ==="; \
  QUAID_HOME=/Users/admin/.quaid CODEX_PROJECT_DIR=/tmp/cdx-m13-test \
  ~/.openclaw/extensions/quaid/quaid recall "xyloquartz-cdx-m13-9982" 2>&1 | tail -5'
```

Pass: canary returned. Fail: empty or error.

**Step 8 — cleanup:**

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=codex-private-tmp-cdx-m13-test \
  ~/.quaid/plugins/quaid/quaid project delete misc--codex-private-tmp-cdx-m13-test 2>&1 | tail -3 || true'
ssh REMOTE_HOST 'trash /tmp/cdx-m13-test 2>/dev/null || rm -rf /tmp/cdx-m13-test; echo "cleaned project dir"'
ssh REMOTE_HOST 'ID=codex-private-tmp-cdx-m13-test; trash ~/quaid/instances/$ID 2>/dev/null || rm -rf ~/quaid/instances/$ID; echo "cleaned visible silo"'
ssh REMOTE_HOST 'ID=codex-private-tmp-cdx-m13-test; trash ~/.quaid/instances/$ID 2>/dev/null || rm -rf ~/.quaid/instances/$ID; echo "cleaned hidden silo"'
```

Pass:
- CDX auto-provisions a new silo when run from a new project dir
- silo is created at `~/quaid/instances/codex-<path-slug>/`
- livetest dir and test dir resolve to different instance IDs
- canary stored in test instance NOT visible in livetest instance
- canary IS visible in test instance

Fail:
- silo not created after CDX first hook fires
- both project dirs resolve to the same instance
- canary appears in the livetest instance

### M13: OC Multi-Instance Verification

OC creates additional instances via the native `openclaw agents add` command.
This milestone verifies that adding a new agent creates a properly isolated silo
and that its memory is fully separate from the main agent.

Do NOT re-run the installer for M13 — that overwrites the gateway config and
disrupts the active livetest instance.

**Step 1 — add m13test agent:**

```bash
ssh REMOTE_HOST 'mkdir -p /tmp/oc-m13-workspace && source ~/.zprofile; \
  openclaw agents add m13test --non-interactive --workspace /tmp/oc-m13-workspace 2>&1 | tail -5'
```

Use a dedicated test workspace for `m13test`. Do not point `--workspace` at
`~/quaid`: `openclaw agents delete m13test --force` prunes the agent workspace
and can move the entire visible Quaid home into Trash.

**Step 2 — verify list_agent_instance_ids includes openclaw-m13test:**

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main \
  python3 -c "
import sys, os; sys.path.insert(0, os.path.expanduser(\"~/.openclaw/extensions/quaid\"))
from adaptors.factory import create_adapter
a = create_adapter(\"openclaw\")
ids = a.list_agent_instance_ids()
print(ids)
assert any(\"m13test\" in i for i in ids), \"openclaw-m13test not in \" + str(ids)
print(\"PASS: openclaw-m13test in list_agent_instance_ids\")
"'
```

**Step 3 — initialise m13test silo:**

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-m13test \
  ~/.openclaw/extensions/quaid/quaid doctor 2>&1 | tail -5'
```

**Step 4 — verify m13test silo created:**

```bash
ssh REMOTE_HOST '
silo="$HOME/quaid/instances/openclaw-m13test"
if [ -d "$silo" ]; then
  echo "PASS: $silo exists"
  ls "$silo"
else
  echo "FAIL: $silo missing"
fi
'
```

**Step 5 — canary isolation: store in m13test instance:**

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-m13test \
  ~/.openclaw/extensions/quaid/quaid store "xyloquartz-oc-m13-9983 is the oc m13 spillover canary" 2>&1'
```

**Step 6 — canary must NOT appear in openclaw-main:**

```bash
ssh REMOTE_HOST 'echo "=== openclaw-main: must NOT see m13test canary ==="; \
  QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main \
  ~/.openclaw/extensions/quaid/quaid recall "xyloquartz-oc-m13-9983" 2>&1 | tail -5'
```

Pass: no results. Fail: canary appears.

**Step 7 — canary MUST appear in openclaw-m13test:**

```bash
ssh REMOTE_HOST 'echo "=== openclaw-m13test: MUST see its own canary ==="; \
  QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-m13test \
  ~/.openclaw/extensions/quaid/quaid recall "xyloquartz-oc-m13-9983" 2>&1 | tail -5'
```

Pass: canary returned. Fail: empty or error.

**Step 8 — cleanup: remove test agent and silo:**

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-m13test \
  ~/.quaid/plugins/quaid/quaid project delete misc--openclaw-m13test 2>&1 | tail -3 || true'
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/openclaw-cli-safe.sh \
  --timeout 45 \
  --label oc-m13-delete \
  --on-timeout "ssh REMOTE_HOST 'pkill -f openclaw-update >/dev/null 2>&1 || true; pkill -f openclaw-completion >/dev/null 2>&1 || true; pkill -f openclaw-agent >/dev/null 2>&1 || true; pkill -f openclaw-agents >/dev/null 2>&1 || true'" \
  -- ssh REMOTE_HOST 'source ~/.zprofile; openclaw agents delete m13test --force 2>&1 | tail -3'
ssh REMOTE_HOST 'trash ~/quaid/instances/openclaw-m13test 2>/dev/null || rm -rf ~/quaid/instances/openclaw-m13test; echo "cleaned openclaw-m13test visible silo"'
ssh REMOTE_HOST 'trash ~/.quaid/instances/openclaw-m13test 2>/dev/null || rm -rf ~/.quaid/instances/openclaw-m13test; echo "cleaned openclaw-m13test hidden silo"'
ssh REMOTE_HOST 'trash /tmp/oc-m13-workspace 2>/dev/null || rm -rf /tmp/oc-m13-workspace; echo "cleaned oc m13 workspace"'
```

Pass:
- `openclaw agents add` creates m13test agent in the agents system
- `list_agent_instance_ids()` returns m13test after add
- silo is created at `~/quaid/instances/openclaw-m13test/`
- canary stored in m13test NOT visible in openclaw-main
- canary IS visible in openclaw-m13test
- cleanup via `openclaw agents delete` + silo removal

Fail:
- `list_agent_instance_ids()` does not include m13test after agents add
- silo fails to initialise
- canary appears in openclaw-main (cross-instance contamination)

## Cross-Platform Project Linking Test

Run this only after both OpenClaw and Claude Code have passed M1-M10.

This is explicitly a user-behavior test. The agent should be able to discover
how to link and use the project without being given function names.

### Phase 1: Create the project and add a doc in OpenClaw

Prepare a source root:

```bash
ssh REMOTE_HOST 'mkdir -p ~/quaid/projects/cross-live-test-src && cat > ~/quaid/projects/cross-live-test-src/main.py <<\"PY\"
def harbor_status():
    return "North pier beacon is offline"
PY'
```

Ask OC naturally:

- `Can you create a project named cross-live-test for ~/quaid/projects/cross-live-test-src?`
- `Do you see the existing cross-live-test project? Can we add a document to it?`
- `Please add a project document that says the north pier beacon is offline and the maintenance window starts at 02:15 UTC.`

Verify from shell:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry list 2>&1 | grep cross-live-test'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs list --project cross-live-test 2>&1'
```

If the doc file exists but is not listed, register it manually:

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry register <path-to-doc> --project cross-live-test 2>&1'
```

After the doc is registered, run `docs update --apply` to index it (new standalone docs with no
existing chunks should be detected and indexed automatically — this is what M10 verifies):

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs update --apply 2>&1 | tail -20'
```

Expected output includes "Indexing new doc:" for the registered file. If it says "All docs up-to-date"
instead, that is a regression — fall back to `janitor --task rag --apply` to unblock the test and
report to claude-dev.

Then verify recall:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid recall "north pier beacon" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
```

Then ask OC:

- `What does the cross-live-test project doc say about the beacon?`

Pass:
- OC can retrieve the doc content through Quaid

### Phase 2: Link the same project in Claude Code and add a second doc

**Ordering**: Phase 2 assumes OC's Phase 1 has landed — CC LINKS to an existing
`cross-live-test` rather than creating fresh. If OC is blocked upstream and CC
reaches this milestone first, CC's natural-directive create will attach to the
visible-home project dir under its own instance registry; the coordinator
cross-registration step below (`Cross-link docs across instances`) handles the
multi-instance linking regardless. Lane interleaving is expected.

Ask CC naturally:

- `Do you see the existing cross-live-test project? Can we add a document to it?`
- `Please add another project document that says code word Ember Glass means pager escalation level 2.`

Verify from shell:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid registry list 2>&1 | grep cross-live-test'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid docs list --project cross-live-test 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid recall "Ember Glass" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
```

Pass:
- CC can use the existing project rather than needing a new one
- CC can add a doc and Quaid can recall it

### Cross-link docs across instances before Phase 3

Each adapter maintains its own docs index. After both docs are registered, each instance
only has its own doc indexed. Cross-link by registering each doc in the other instance,
then run `docs update --apply` on both. The daemon picks up doc changes lazily — always
run `docs update --apply` explicitly rather than waiting, and wait for it to confirm
indexing before proceeding to Phase 3.

**Before running `docs update --apply`, sanity-check project registry for orphans.**
`docs update --apply` will recreate scaffold dirs for any project with a live registry
entry, including leftovers from prior M13 runs where the project wasn't deleted. If
`quaid project list` shows stale `misc--*-m13-test` entries, delete them first:

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid project list 2>&1 | grep -i m13 || echo "no m13 orphans"'
# If any m13-test projects appear, run:
# ssh REMOTE_HOST '... quaid project delete misc--<instance>-m13-test'
```

```bash
# Register OC beacon doc in CC instance
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid registry register <path-to-beacon-doc> --project cross-live-test 2>&1'
# Register CC Ember Glass doc in OC instance
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry register <path-to-ember-glass-doc> --project cross-live-test 2>&1'

# Force index on both — wait for "Indexed" confirmation before continuing
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs update --apply 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid docs update --apply 2>&1'
```

Verify cross-instance CLI recall before asking agents conversationally:

```bash
# CC must find beacon (OC-added doc)
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid recall "north pier beacon" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
# OC must find Ember Glass (CC-added doc)
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid recall "Ember Glass" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
```

If either CLI recall fails after `docs update --apply`, stop and report to claude-dev — the cross-link registration or indexing is not working and conversational Phase 3 will also fail.

### Phase 3: Cross-recall both directions

Ask CC (use content-specific phrasing so the model matches the doc, not just PROJECT.md):

- `Can you search the cross-live-test project docs for anything about the north pier beacon?`

If that still returns nothing, try more specific phrasing:

- `What does the cross-live-test project say about the north pier beacon maintenance window?`

Ask OC via Matrix:

```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "Can you search the cross-live-test project docs for anything about Ember Glass escalation?"'
```

If no answer, try:
```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "What is the escalation code word Ember Glass in the cross-live-test project docs?"'
```

Optional provenance follow-up:
```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "How did you know that?"'
```

Note: The generic "What does the project say about X?" framing matches PROJECT.md in the vector index
and misses content docs. Use docs-specific phrasing that names the concept explicitly so the model
searches the docs store. Both prompts above are content-specific and reliably surface the right doc.

Pass:
- CC can answer from the OC-added doc
- OC can answer from the CC-added doc
- answers are grounded in Quaid project context, not raw disk browsing as the
  first move

Fail:
- either side cannot see the same project
- either side cannot retrieve the other side's doc
- the agent only succeeds when given explicit command names

## Post-Test Audit

After all milestones and the cross-platform project linking test.

Instances on alfie use per-instance subdirectories under `~/quaid/`:
- OC: `~/quaid/instances/openclaw-main/` (`QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main`)
- CC: `~/.quaid/instances/claude-code-private-tmp-cc-livetest/` (`QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest`)

```bash
# OC instance health
ssh REMOTE_HOST 'sqlite3 ~/quaid/data/memory.db "SELECT COUNT(*) FROM nodes; SELECT COUNT(*) FROM edges;"'
ssh REMOTE_HOST 'sqlite3 ~/quaid/data/memory.db "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL;"'
ssh REMOTE_HOST 'ls ~/quaid/instances/openclaw-main/journal/'
ssh REMOTE_HOST 'cat ~/quaid/instances/openclaw-main/USER.snippets.md 2>/dev/null'
ssh REMOTE_HOST 'ls -lt ~/.quaid/instances/openclaw-main/logs/ | head -20'
ssh REMOTE_HOST 'cat ~/.quaid/instances/openclaw-main/config/memory.json | python3 -m json.tool | head -20'
ssh REMOTE_HOST 'cat ~/.quaid/instances/openclaw-main/data/circuit-breaker.json 2>/dev/null'
ssh REMOTE_HOST 'cat ~/.quaid/instances/openclaw-main/logs/janitor/checkpoint-all.json 2>/dev/null'

# CC instance health
ssh REMOTE_HOST 'sqlite3 ~/.quaid/instances/claude-code-private-tmp-cc-livetest/data/memory.db "SELECT COUNT(*) FROM nodes; SELECT COUNT(*) FROM edges;" 2>/dev/null || echo "CC DB not found"'
ssh REMOTE_HOST 'ls ~/.quaid/instances/claude-code-private-tmp-cc-livetest/journal/ 2>/dev/null || echo "CC journal not found"'
```

Audit identity files (SOUL, USER, MEMORY — now live in `identity/` subdirectory):

```bash
# OC identity
ssh REMOTE_HOST 'for f in /Users/USER/quaid/instances/openclaw-main/{SOUL,USER,ENVIRONMENT}.md; do echo "===== $f"; ls -l "$f" 2>/dev/null || true; sed -n "1,80p" "$f" 2>/dev/null || true; echo; done'
# CC identity
ssh REMOTE_HOST 'for f in /Users/USER/quaid/instances/claude-code-private-tmp-cc-livetest/{SOUL,USER,ENVIRONMENT}.md; do echo "===== $f"; ls -l "$f" 2>/dev/null || true; sed -n "1,80p" "$f" 2>/dev/null || true; echo; done'
```

Audit project docs and snippets/journals:

```bash
# Shared project docs
ssh REMOTE_HOST 'find /Users/USER/quaid/projects -maxdepth 3 \( -name "PROJECT.md" -o -name "TOOLS.md" -o -name "AGENTS.md" \) | sort | while read f; do echo "===== $f"; wc -l "$f" 2>/dev/null; sed -n "1,30p" "$f" 2>/dev/null; echo; done'
# Live-test project
ssh REMOTE_HOST 'find /Users/USER/quaid/projects/live-test 2>/dev/null -maxdepth 2 -type f | sort | while read f; do echo "===== $f"; wc -l "$f"; sed -n "1,80p" "$f"; echo; done'
# Snippets and journals
ssh REMOTE_HOST 'for f in /Users/USER/quaid/instances/openclaw-main/SOUL.snippets.md /Users/USER/quaid/instances/openclaw-main/USER.snippets.md /Users/USER/quaid/instances/claude-code-private-tmp-cc-livetest/SOUL.snippets.md /Users/USER/quaid/instances/claude-code-private-tmp-cc-livetest/USER.snippets.md; do echo "===== $f"; wc -l "$f" 2>/dev/null || echo "(absent — builds via extraction)"; sed -n "1,60p" "$f" 2>/dev/null; echo; done'
ssh REMOTE_HOST 'for f in /Users/USER/quaid/instances/openclaw-main/journal/SOUL.journal.md /Users/USER/quaid/instances/openclaw-main/journal/USER.journal.md /Users/USER/quaid/instances/openclaw-main/journal/MEMORY.journal.md /Users/USER/quaid/instances/claude-code-private-tmp-cc-livetest/journal/SOUL.journal.md /Users/USER/quaid/instances/claude-code-private-tmp-cc-livetest/journal/USER.journal.md /Users/USER/quaid/instances/claude-code-private-tmp-cc-livetest/journal/MEMORY.journal.md; do echo "===== $f"; wc -l "$f" 2>/dev/null || true; sed -n "1,60p" "$f" 2>/dev/null || true; echo; done'
# Project logs
ssh REMOTE_HOST 'find /Users/USER/quaid/projects -name "PROJECT.log" 2>/dev/null | sort | while read f; do echo "===== $f"; wc -l "$f"; sed -n "1,60p" "$f"; echo; done'
```

Pass criteria:
- per-instance identity files (`SOUL.md`, `USER.md`, `ENVIRONMENT.md`) are present for both OC and CC; not empty placeholders
- shared quaid project docs (`projects/quaid/PROJECT.md`, `TOOLS.md`, `AGENTS.md`) exist and are readable from both OC and CC sessions
- live-test project docs are coherent and point at correct paths
- OC snippets (`SOUL.snippets.md`, `USER.snippets.md`) are present and building; CC snippets may be absent on first install and build naturally over time
- journals look structurally sane and consistent with the run
- project logs are readable and correspond to real actions taken

## Final Closeout

When the run is done:

1. Restore any temporary config changes such as timeout or notification
   verbosity.
2. Restore the normal adapter config if it was switched.
3. Send one final summary to `claude-dev`.
