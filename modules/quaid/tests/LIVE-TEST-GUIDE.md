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
  `ssh example.local '...'`. NEVER run them directly on the local machine.
- Before any install or uninstall command, verify you are targeting alfie:
  `ssh example.local hostname` must return `alfie`.
- If you ever find yourself running `node setup-quaid.mjs` without an
  `ssh example.local` prefix, STOP immediately — you are on the wrong machine.

- Use this document as the source of truth for the live test procedure.
- Start from a clean install unless the user explicitly says to skip it.
- Run the live test from the `canary` branch. Verify the checkout before
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
  explicitly testing a slash command such as `/new`, `/reset`, or `/compact`.

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
- Required branch: `canary`
- Test guide: `~/quaid/dev/modules/quaid/tests/LIVE-TEST-GUIDE.md`
- Reference tool guide: `~/quaid/dev/projects/quaid/TOOLS.md`

Target machine:
- Host: `example.local`
- OpenClaw workspace: `~/quaid`
- Live interaction pane: local tmux pane `main:99`

## Start Condition

Do not begin milestone testing against an existing live Quaid install.

### Step 0 — Full wipe on alfie (mandatory)

Before any run, completely remove Quaid and all its data from alfie. Do not do
targeted cleanup — stale carryover files, queue events, DB nodes, and identity
files all contaminate test results in ways that are hard to trace. A full wipe
is faster and safer than surgical cleanup.

**Uninstall the plugin first to remove registry entries:**

```bash
ssh example.local 'openclaw plugins uninstall quaid 2>/dev/null; echo "OC uninstall done"'
```

**Then wipe the entire Quaid workspace and extension dir:**

```bash
ssh example.local 'rm -rf ~/quaid && rm -rf ~/.openclaw/extensions/quaid && echo "wipe done"'
```

> **WARNING**: This runs on example.local only — never on the local dev machine
> where the source repo lives.

**Clear OC session transcripts** (critical — stale sessions from prior runs trigger
extraction fan-out after reinstall, saturating the gateway and breaking `/reset` and
other hook-dependent milestones):

```bash
ssh example.local 'rm -rf ~/.openclaw/agents/main/sessions/ && echo "OC sessions cleared"'
```

**Clear CC adapter artifacts:**

```bash
ssh example.local 'rm -f ~/.claude/rules/quaid-projects.md && echo "CC rules cleared"'
```

### Remaining start checks

Before any run:
- verify the repo checkout is on `canary`
- reinstall Quaid cleanly with the installer script
- verify the install is stable before M1

Minimum stability check before M1:
- the install artifacts exist where expected
- `quaid doctor` or `quaid health` succeeds
- the active DB and log paths are identified
- the daemon starts cleanly
- one basic agent turn succeeds without hanging

If clean reinstall is skipped, note that the run is not a clean live install
validation.

## Installer-Based Clean Install

Use a source tree from the local test machine only. Do not involve `spark`.
Valid install sources for a live run are:

- local `~/quaid/dev` on this machine
- GitHub `openclaw` when that is the target under test
- Quaid `canary`

When using the local dev tree, sync the full tree from this machine to alfie
before running the installer. `setup-quaid.mjs` and `lib/` are at the root of
`~/quaid/dev`, not inside `modules/quaid/`:

```bash
rsync -av --checksum \
  --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.git/' --exclude='logs/' --exclude='.env*' \
  ~/quaid/dev/ example.local:~/quaid/dev/
```

Also sync the legacy plugin path — the installer (`--workspace ~/quaid`) falls
back to `~/quaid/plugins/quaid/` when `~/quaid/modules/quaid/` is absent, so
both locations must be up to date:

```bash
rsync -av --checksum \
  --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.git/' --exclude='logs/' \
  ~/quaid/dev/modules/quaid/ example.local:~/quaid/plugins/quaid/
```

Verify branch on the local source checkout:

```bash
cd ~/quaid/dev && git branch --show-current && git rev-parse --short HEAD
```

Pass only if the branch is exactly `canary`.

### OpenClaw on example.local

Preview first:

```bash
ssh example.local 'openclaw plugins list 2>/dev/null | grep quaid || true'
ssh example.local 'ls -ld ~/quaid ~/quaid/openclaw-main ~/quaid/shared 2>/dev/null || true'
```

Install with the installer script on `alfie`, using the synced local tree.
Use `QUAID_TEST_MOCK_MIGRATION=1` to skip LLM-based migration of existing
workspace files (SOUL.md, USER.md, etc.) — without it the installer runs 5
sequential deep-reasoning calls that block M0 for several minutes:

```bash
ssh example.local 'cd ~/quaid/dev && QUAID_INSTALL_AGENT=1 QUAID_TEST_MOCK_MIGRATION=1 QUAID_OWNER_NAME="Solomon" QUAID_INSTANCE=openclaw-main node setup-quaid.mjs --agent --workspace "/Users/owner/quaid" --source local'
```

### Claude Code on example.local

Clear old hooks if present, then reinstall with the installer script:

```bash
ssh example.local 'python3 - <<\"PY\"
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
ssh example.local 'cd ~/quaid/dev && QUAID_INSTALL_AGENT=1 QUAID_TEST_MOCK_MIGRATION=1 QUAID_OWNER_NAME="Solomon" QUAID_INSTANCE=claude-code-main QUAID_INSTALL_CLAUDE_CODE=1 node setup-quaid.mjs --agent --claude-code --workspace "/Users/owner/quaid" --source local'
```

### Post-install verification

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid doctor 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid health 2>&1'
ssh example.local 'cat ~/.claude/settings.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(sorted(d.get(\"hooks\", {}).keys()))"'
ssh example.local 'ls -l ~/quaid/openclaw-main/identity/SOUL.md ~/quaid/claude-code-main/identity/SOUL.md 2>/dev/null || true'
```

If either instance-local `identity/SOUL.md` is missing, seed it from the shared
root file before running janitor `--apply`:

```bash
ssh example.local 'python3 - <<\"PY\"
from pathlib import Path
src = Path("/Users/owner/quaid/SOUL.md")
for dst in [
    Path("/Users/owner/quaid/openclaw-main/identity/SOUL.md"),
    Path("/Users/owner/quaid/claude-code-main/identity/SOUL.md"),
]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        dst.write_text(src.read_text())
        print(f"created {dst}")
PY'
```

Seed the quaid project directory in both instance silos so `PROJECT.log` can
be written by extraction (the daemon looks for `projects/quaid/PROJECT.md`):

```bash
ssh example.local 'src=~/quaid/openclaw-main/projects/quaid/PROJECT.md; mkdir -p "$(dirname $src)"; [ -f "$src" ] || cp ~/quaid/projects/quaid/PROJECT.md "$src" && echo "seeded" || echo "already exists"'
ssh example.local 'src=~/quaid/claude-code-main/projects/quaid/PROJECT.md; mkdir -p "$(dirname $src)"; [ -f "$src" ] || cp ~/quaid/projects/quaid/PROJECT.md "$src" && echo "seeded" || echo "already exists"'
```

## Execution Model

### Phase Start Reset

At the start of each live interaction phase, reset local tmux pane `main:99`
so the interface is fresh and visible.

- OpenClaw phase start: kill and restart pane `main:99`, `ssh example.local`,
  then launch `openclaw tui`
- Claude Code phase start: kill and restart pane `main:99`, `ssh example.local`,
  then launch `claude`

Recommended pattern:

```bash
tmux respawn-pane -k -t main:99 'zsh -il'
tmux send-keys -t main:99 "ssh example.local" Enter
```

Then launch the subject under test:

```bash
tmux send-keys -t main:99 "openclaw tui" Enter
# or
tmux send-keys -t main:99 "cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main claude --dangerously-skip-permissions" Enter
```

Do this again whenever switching from the OpenClaw phase to the Claude Code
phase, or if the pane becomes contaminated and you need a clean visible session.

### OpenClaw

OC live interaction must be visible in local tmux pane `main:99`, just like
Claude Code. Do not use `/tmp/oc-send.sh` or any hidden SSH wrapper for live
milestones. The goal is to simulate a real user session.

Pattern:

```bash
tmux respawn-pane -k -t main:99 'zsh -il'
tmux send-keys -t main:99 "ssh example.local" Enter
tmux send-keys -t main:99 "openclaw tui" Enter
```

Then send normal user messages or slash commands directly in that pane:

```bash
tmux send-keys -t main:99 "message here" Enter
tmux send-keys -t main:99 "/new" Enter
tmux send-keys -t main:99 "/reset" Enter
tmux send-keys -t main:99 "/compact" Enter
tmux capture-pane -t main:99 -p | tail -30
```

Use SSH/CLI commands only for verification, DB queries, logs, config changes,
install, and uninstall. Do not use them to simulate the agent conversation.

### Claude Code

CC hooks require interactive mode. Run CC visibly in local tmux pane `main:99`,
SSH to `example.local`, and launch `claude` from there.

```bash
tmux respawn-pane -k -t main:99 'zsh -il'
tmux send-keys -t main:99 "ssh example.local" Enter
tmux send-keys -t main:99 "cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main claude --dangerously-skip-permissions" Enter
```

Read replies with:

```bash
tmux capture-pane -t main:99 -p | tail -30
```

**Important:** For this live test flow, end the visible CC session with
`/exit` in pane `99` to return cleanly to the remote shell. After each CC
session end, explicitly verify that extraction happened by checking
`~/quaid/claude-code-main/data/extraction-signals/`, the CC daemon log, or the
shared DB at `~/quaid/data/memory.db`. If a session ends cleanly but no
`session_end` signal appears, do not assume extraction fired.

Current live-test fallback on `claude` `2.1.76`:

1. Find the real CC transcript under `~/.claude/projects/-Users-clawdbot-quaid/`.
2. Write a manual `session_end` signal against that real transcript.
3. Verify the shared DB at `~/quaid/data/memory.db`.

Example:

```bash
ssh example.local 'python3 - <<\"PY\"
import sys
sys.path.insert(0, \"/Users/owner/quaid/plugins/quaid\")
from core.extraction_daemon import write_signal
p = write_signal(
    signal_type=\"session_end\",
    session_id=\"<real-cc-session-id>\",
    transcript_path=\"/Users/owner/.claude/projects/-Users-clawdbot-quaid/<real-cc-session-id>.jsonl\",
)
print(p)
PY'
```

Before running CC project/recall milestones, verify that SessionStart generated
real project guidance, not just identity projections. The current hook-session-
init path scans `~/quaid/shared/projects`, not `~/quaid/projects`, so the
shared project registry/sync state must already be correct.

Quick checks:

```bash
ssh example.local 'wc -l ~/.claude/rules/quaid-projects.md && sed -n "1,220p" ~/.claude/rules/quaid-projects.md'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid project list 2>&1'
ssh example.local 'find ~/quaid/shared/projects -maxdepth 3 -type f | sort'
ssh example.local 'python3 - <<\"PY\"
import json
from pathlib import Path
p = Path(\"/Users/owner/quaid/project-registry.json\")
if p.exists():
    print(json.dumps(json.loads(p.read_text()), indent=2))
PY'
```

Pass only if `~/.claude/rules/quaid-projects.md` includes project sections like
`--- quaid/TOOLS.md ---` and `--- quaid/AGENTS.md ---`. If it only contains
`USER.md` / `MEMORY.md` projections, CC project CRUD is not being tested
against a valid shared-project bootstrap state yet. Also verify the global
registry entry for `quaid` points at `$QUAID_HOME/shared/projects/quaid`, not
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

Run M1-M10 on OpenClaw first. After OpenClaw passes, run M1-M10 on Claude Code.
Then run M11 (artifact generation) once for each platform to verify snippets,
journals, and project logs are building from extractions.

### M1: Extraction via `/new`

> **OC TUI behavior:** OC TUI `/new` adds a brand-new key to `sessions.json`
> rather than updating an existing key's session ID. The adapter detects this
> via a new-key arrival branch in `tickSessionIndex`: when a new key appears,
> it signals any recently-active sessions with content immediately (within 1s).
> No follow-up message or `.reset.*` backup needed.
>
> **OC 2026.3.13+ note:** `/new` may no longer be intercepted as a built-in TUI
> slash command in this version — OC passes it through to the model as a user
> message and the model responds saying it doesn't know the command. In this
> case the `new_key_detected` path does NOT fire (no new sessions.json key), but
> the adapter's `handleSlashLifecycleFromMessage` path DOES detect `/new` in the
> message event and writes a ResetSignal for the pre-/new session. Extraction
> still fires. Check for `hook.message.command_detected` (command=new) in the
> hook trace instead of `session_index.new_key_detected`.

Procedure:
1. Tell the agent something memorable in natural conversation — pick a vivid,
   distinctive detail that would not already be in memory. For example:
   `"My neighbour just told me she won a regional chili cook-off last weekend
   using a smoked brisket recipe she's kept secret for twenty years."`
   Note the distinctive keyword(s) you'll search for (e.g. `chili cook-off`).
2. Wait for full idle.
3. Send `/new`.
   - **OC < 2026.3.13 (TUI intercepts):** sessions.json is NOT updated yet —
     visual-only switch. Send one message to the new session (e.g. `Hello`)
     to write the new key and trigger `new_key_detected`.
   - **OC 2026.3.13+ (TUI passes to model):** model will reply "no /new
     command". That's OK — adapter detects it via message event and fires
     ResetSignal immediately. No follow-up message needed.
4. Wait 30–60 seconds for extraction.
5. Check DB for the distinctive keyword.

Hook trace markers to confirm:
- **OC < 2026.3.13:** `session_index.new_key_detected` → `session_index.signal_queued` (source=new-key)
- **OC 2026.3.13+:** `hook.message.command_detected` (command=new) → `daemon.signal_written` (type=reset)

Pass:
- the fact is stored after the lifecycle boundary
- DB or recall output surfaces the distinctive detail

### M2: Extraction via `/reset`

Tell the agent something memorable in natural conversation, then trigger `/reset`.
Use a different distinctive detail than M1 — for example:
`"I just booked flights to Reykjavik for the aurora season in February."`

Pass:
- the fact is stored from the pre-reset session

### M3: Extraction via `/compact`

Tell the agent something memorable in natural conversation, build some
conversation context (a few exchanges), then trigger `/compact`.
Use a different distinctive detail — for example:
`"My sister started her ceramics studio this spring, she fires everything in a
wood-burning kiln she built herself."`

Pass:
- the fact is stored
- logs or hook trace show the compaction signal

### M4: Timeout Extraction

Temporarily set `capture.inactivityTimeoutMinutes` to `1`. The change must be
followed by a restart — both OC and CC cache config at startup:

**OC** — set config then restart OpenClaw:
```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
# Then restart OpenClaw on alfie.
```

**CC** — set config then restart the CC daemon:
```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'
```

After restart, start a fresh visible CC session in main:99, mention something
memorable (e.g. `"My morning run route goes along the canal towpath — about 8km."`)
then let the session idle for >1 minute without sending any further messages.

After the test, restore the timeout and restart again:

```bash
# OC
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
# Then restart OpenClaw on alfie.

# CC
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'
```

Pass:
- the timeout fact is extracted with no explicit lifecycle command
- for Claude Code, verify `quaid daemon status` points at the correct
  instance root before idling:
  - `instance_root: /Users/owner/quaid/claude-code-main`
  - `log_file: /Users/owner/quaid/claude-code-main/logs/daemon/extraction-daemon.log`
  - `pid_file: /Users/owner/quaid/claude-code-main/data/extraction-daemon.pid`

Verify extraction happened (use `name` column, not `text`):
```bash
ssh example.local 'QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid recall "canal towpath"'
# OR direct DB check:
ssh example.local python3 << 'EOF'
import sqlite3
con = sqlite3.connect("/Users/owner/quaid/data/memory.db")
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
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "Baxter is a golden retriever who loves tennis balls" 2>&1'
```

Start a fresh session and ask, framed so the agent knows what is being tested:

- `This is a test of auto memory injection. Without using any recall tool,
  what do you know about Baxter?`

Pass:
- the answer includes the stored fact
- the agent answers without making an explicit tool call to retrieve it —
  the fact appeared in its context automatically via the inject hook

Also test with a conversationally-extracted fact from M1–M4 (different topic
from Baxter so there is no overlap):

- `This is a test of auto memory injection. Without using any recall tool,
  what do you remember about my neighbour?`

Pass: the agent answers from injected context, no explicit recall tool call.

### M6: Deliberate Recall

This milestone tests that the agent can actively retrieve facts on demand,
independent of what was auto-injected.

Ask natural questions framed so the agent uses explicit recall rather than
relying on whatever arrived via auto-inject:

- `This is a test of memory recall. Please ignore any context that may have
  been auto-injected this session and use the quaid memory recall tool
  directly. What have I told you about my family?`
- `Same — use explicit quaid recall, not auto-inject. What do you know about
  my exercise habits or recent plans?`

Pass:
- the agent makes an explicit recall tool call in response
- the answers are materially grounded in stored memory
- the agent does not just repeat what was already in injected context

### M7: Graph Traversal Verification

This milestone tests both extraction-time edge creation AND the janitor's
retroactive edge backfill (`--task edges`).

**Phase 1 — Compound fact (tests extraction prompt):**

Seed a compound fact that contains two relationships in one sentence.
The extraction prompt now explicitly asks the LLM to extract ALL edges:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "David is married to Lisa and they have a son named Oliver" 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "David works at Google" 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid store "David is the user'"'"'s brother" 2>&1'
```

Check immediately whether both edges were extracted from the compound fact:

```bash
ssh example.local 'DB=~/quaid/data/memory.db && sqlite3 "$DB" "SELECT s.name, e.relation, t.name FROM edges e JOIN nodes s ON e.source_id=s.id JOIN nodes t ON e.target_id=t.id WHERE s.name IN (\"David\",\"Lisa\",\"Oliver\") OR t.name IN (\"David\",\"Lisa\",\"Oliver\") ORDER BY s.name, e.relation;"'
```

**Phase 2 — Janitor edge backfill (tests retroactive recovery):**

The backfill only processes facts that have **zero linked edges**. If Phase 1
created ANY edges for a fact (even wrong ones like `family_of`), the backfill
will skip that fact. Phase 2 is only useful when edges are completely absent.

If `spouse_of` is missing from Phase 1 (LLM picked only `parent_of`), that
is expected to be recoverable via backfill. Run the edge backfill task:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid janitor --task edges --apply 2>&1'
```

Re-check edges — all should now be present:

```bash
ssh example.local 'DB=~/quaid/data/memory.db && sqlite3 "$DB" "SELECT s.name, e.relation, t.name FROM edges e JOIN nodes s ON e.source_id=s.id JOIN nodes t ON e.target_id=t.id WHERE s.name IN (\"David\",\"Lisa\",\"Oliver\") OR t.name IN (\"David\",\"Lisa\",\"Oliver\") ORDER BY s.name, e.relation;"'
```

Expected edges after Phase 1 or Phase 2:
- David → Oliver: `parent_of` (ideal) OR `family_of` (acceptable — LLM may use coarser relation)
- David → Lisa: `spouse_of`
- David → User (or user's name): `sibling_of`
- David → Google: `works_at`

Known LLM edge quality issues (do NOT fail on these):
- `has_pet` may appear for Oliver — this is a hallucination from the LLM confusing
  "have a son" with "have a pet". Its presence does not affect pass/fail.
- `family_of` instead of `parent_of` is acceptable — both correctly represent the relationship.

Pass:
- relationship edges exist between David and Oliver after Phase 1 (any relation) = pass
- Phase 2 backfill working = pass if it creates edges for facts that had none
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
ssh example.local 'DB=~/quaid/data/memory.db; sqlite3 "$DB" "SELECT id, name FROM nodes WHERE LOWER(name) LIKE \"%niece%\" OR LOWER(name) LIKE \"%anne%\" OR LOWER(name) LIKE \"%diana%\" OR LOWER(name) LIKE \"%alice%\" ORDER BY created_at DESC LIMIT 20;"'
```

Delete each found node (replace `<id>` with actual IDs):

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid delete-node <id>'
```

Verify clean:

```bash
ssh example.local 'DB=~/quaid/data/memory.db; sqlite3 "$DB" "SELECT COUNT(*) FROM nodes WHERE LOWER(name) LIKE \"%diana%\" OR LOWER(name) LIKE \"%alice%\";"'
# Must return 0
```

Step 2 — Restart the extraction daemon so any patched files are loaded:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon stop 2>/dev/null; sleep 1; QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon start'
```

Step 3 — Start a completely fresh OC session for seeding.
Kill and restart pane `main:99` with a new named session so the transcript
is empty before seeding. **Do not retry within the same session** — each
retry appends to the transcript, which contaminate carry_facts.

```bash
# Kill current pane 99 content and start fresh session
ssh example.local 'openclaw tui --session oc-m7p3-$(date +%s)'
```

In the new session, tell the agent two facts naturally — do NOT say "niece":

- `My sister's name is Diana.`
- `Diana has a daughter named Alice.`

Then trigger `/reset` to extract those facts and start a new session.

**Verify edges before asking the agent** — if extraction went wrong, fix it
before wasting a session query:

```bash
ssh example.local 'DB=~/quaid/data/memory.db; sqlite3 "$DB" "SELECT s.name, e.relation, t.name FROM edges e JOIN nodes s ON e.source_id=s.id JOIN nodes t ON e.target_id=t.id WHERE s.name IN (\"Diana\",\"Alice\") OR t.name IN (\"Diana\",\"Alice\") ORDER BY s.name, e.relation;"'
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

This is a capability test. Do not tell the agent the exact command names.

Prepare a source root:

```bash
ssh example.local 'mkdir -p /tmp/quaid-live-src && printf "print(\"hello\")\n" > /tmp/quaid-live-src/main.py'
```

Ask the agent naturally:

- `Can you create a project named live-test for /tmp/quaid-live-src with a short description?`
- `Can you show me what you know about the live-test project?`
- `Can you update that project's description so it is clearly marked as a live test project?`

Ask the agent to list all projects:

- `Can you list all the projects you know about?`

Then delete the test project:

- `Can you delete the live-test project?`

Verify from shell:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid project list 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid project show live-test 2>&1 || true'
ssh example.local 'test -f /tmp/quaid-live-src/main.py && echo source_still_exists'
```

After project CRUD, trigger extraction to generate project logs. Tell the agent
naturally something about the session, then do `/reset`:

> "We've just tested project creation, show, list, update, and delete for the
> live-test project via the quaid CLI. This is part of the quaid live-test
> suite M8 run. Triggering a reset to capture project activity."

Then `/reset`. This gives the extraction LLM enough project context to include
a `project_logs` entry for the quaid project in the extraction JSON.

Check after extraction:

```bash
ssh example.local 'tail -20 ~/quaid/openclaw-main/projects/quaid/PROJECT.log 2>/dev/null || echo "(PROJECT.log absent — check if quaid project exists in instance)"'
```

Pass:
- create works
- show works (returns project details)
- list works (live-test appears in output)
- update works (description reflects the change)
- delete removes the project but not the source directory
- `projects/quaid/PROJECT.log` has at least one timestamped entry added during this session

### M9: Janitor

Before running, capture the pre-janitor artifact state:

```bash
# Record line counts so you can verify condensation happened
ssh example.local 'echo "OC SOUL.snippets:"; wc -l ~/quaid/openclaw-main/SOUL.snippets.md 2>/dev/null || echo "(absent)"; echo "OC USER.snippets:"; wc -l ~/quaid/openclaw-main/USER.snippets.md 2>/dev/null || echo "(absent)"; echo "OC SOUL.md:"; wc -l ~/quaid/openclaw-main/identity/SOUL.md 2>/dev/null || echo "(absent)"'
```

Run:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid janitor --task all --dry-run 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid janitor --task all --apply --approve 2>&1'
```

After the run, verify condensation:

```bash
# Stats: snippets_folded + snippets_rewritten + snippets_discarded should be > 0
ssh example.local 'cat ~/quaid/openclaw-main/logs/janitor-stats.json | python3 -c "import json,sys; d=json.load(sys.stdin); ac=d.get(\"applied_changes\",{}); print(\"success:\", d[\"success\"]); [print(f\"  {k}: {v}\") for k,v in ac.items() if \"snippet\" in k or \"journal\" in k or \"log_entries\" in k]"'
# Post-janitor snippet and identity state
ssh example.local 'echo "OC SOUL.snippets after:"; wc -l ~/quaid/openclaw-main/SOUL.snippets.md 2>/dev/null || echo "(empty/absent)"; echo "OC SOUL.md after:"; wc -l ~/quaid/openclaw-main/identity/SOUL.md 2>/dev/null'
ssh example.local 'cat ~/quaid/openclaw-main/identity/SOUL.md 2>/dev/null | head -40'
```

Pass:
- janitor completes
- `checkpoint-all.json` exists afterward with `status: completed`
- `janitor-stats.json` reports `success: true`
- `applied_changes` shows `snippets_folded + snippets_rewritten + snippets_discarded > 0` (snippets were reviewed)
- `SOUL.snippets.md` line count decreased or file was cleared (entries processed)
- if `snippets_folded > 0`, `identity/SOUL.md` grew (folded content arrived)

Fail:
- snippets review task skipped entirely (all three snippet counters remain 0 and snippets file unchanged)
- janitor exits with non-zero status

### M10: Docs and Health

Run:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid health 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid stats 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs list 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs check 2>&1'
```

Pass:
- health is good enough to continue
- stats are sensible
- docs commands run successfully

### M11: Snippet, Journal, and Project Log Generation

This milestone verifies that the extraction pipeline writes soul snippets,
user snippets, journal entries, and project logs to disk — not just facts to
the DB. Run it after M1-M10 so multiple extractions have accumulated artifacts.

**Pre-check: ensure the daemon has fresh config** (its project_definitions are
loaded at startup; if the daemon started while M9 janitor was running the DB
may be cached stale). Restart before triggering the trigger extraction:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'
```

Then do a fresh OC session + `/reset` to trigger a full extraction cycle.
Send a substantive message that mentions project context so the LLM generates
a `project_logs` entry for the quaid project:

> "We've been running M0-M11 of the live test suite for the quaid project on
> alfie. Snippets, journals, and project logs are all being validated.
> Triggering a reset to capture project activity for M11."

Then `/reset` and wait for the daemon to complete (check `tail -5` of daemon log
for `project logs seen=N written=M` — `written` should be ≥ 1).

**Snippets** (written per-extraction when the LLM includes `soul_snippets`):

```bash
# OC
ssh example.local 'echo "=== OC SOUL.snippets ==="; cat ~/quaid/openclaw-main/SOUL.snippets.md 2>/dev/null || echo "(absent)"'
ssh example.local 'echo "=== OC USER.snippets ==="; cat ~/quaid/openclaw-main/USER.snippets.md 2>/dev/null || echo "(absent)"'
# CC
ssh example.local 'echo "=== CC SOUL.snippets ==="; cat ~/quaid/claude-code-main/SOUL.snippets.md 2>/dev/null || echo "(absent — builds via CC extraction sessions)"'
ssh example.local 'echo "=== CC USER.snippets ==="; cat ~/quaid/claude-code-main/USER.snippets.md 2>/dev/null || echo "(absent)"'
```

Pass: OC `SOUL.snippets.md` has at least one entry. The section headers (e.g.
`## Compaction — YYYY-MM-DD`) should correspond to extraction events from this
run. CC snippets may be absent on first install — they build via CC sessions.

**Journal entries** (written when LLM includes `journal_entries`; discretionary):

```bash
ssh example.local 'echo "=== OC journals ==="; ls ~/quaid/openclaw-main/journal/ 2>/dev/null; for f in ~/quaid/openclaw-main/journal/*.journal.md; do echo "--- $f ---"; wc -l "$f" 2>/dev/null; sed -n "1,30p" "$f" 2>/dev/null; done'
ssh example.local 'echo "=== CC journals ==="; ls ~/quaid/claude-code-main/journal/ 2>/dev/null || echo "(absent)"; for f in ~/quaid/claude-code-main/journal/*.journal.md; do echo "--- $f ---"; wc -l "$f" 2>/dev/null; sed -n "1,30p" "$f" 2>/dev/null; done'
```

Pass: Journal directory exists. Presence of entries is correct but not required
— the LLM only writes journal entries when it finds genuinely new observations.
Empty journals on early test runs are expected. Structurally malformed files are
a failure.

**Project logs** (written when extraction includes `project_logs` entries):

```bash
ssh example.local 'echo "=== OC quaid PROJECT.log ==="; tail -30 ~/quaid/openclaw-main/projects/quaid/PROJECT.log 2>/dev/null || echo "(absent)"'
ssh example.local 'echo "=== CC quaid PROJECT.log ==="; tail -30 ~/quaid/claude-code-main/projects/quaid/PROJECT.log 2>/dev/null || echo "(absent)"'
```

Pass: `projects/quaid/PROJECT.log` exists and has at least one timestamped
entry from this test run — M8 includes a deliberate `/reset` to capture project
context. Entries are formatted `- [YYYY-MM-DDTHH:MM:SS] <text>`.

Fail:
- OC `SOUL.snippets.md` is absent or empty after 3+ extractions
- `projects/quaid/PROJECT.log` absent after M8's trigger step
- Any file is structurally malformed (broken JSON, truncated entries)

### M12: OC Multi-Agent Verification ✓ 2026-03-15

This milestone verifies that OpenClaw's multi-agent silo structure is correct
and that extraction signals route to the right agent's silo.

**Step 1 — list_agent_instance_ids returns multiple IDs including openclaw-main:**

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main \
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
ssh example.local '
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
ssh example.local '
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
ssh example.local '
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
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main \
  ~/.openclaw/extensions/quaid/quaid instances list 2>&1 || \
  echo "(instances list not available — check quaid version)"'
```

**Step 6 — extraction-daemon.pid exists for main agent (daemon running):**

```bash
ssh example.local '
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

### M13: CC Multi-Instance Verification ✓ 2026-03-15

This milestone verifies that `quaid claudecode make_instance` creates a properly
isolated Claude Code project instance with the correct silo structure and
project wiring.

**Step 1 — create test project dir:**

```bash
ssh example.local 'mkdir -p /tmp/quaid-m13-test && echo "created /tmp/quaid-m13-test"'
```

**Step 2 — run make_instance:**

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main \
  ~/.openclaw/extensions/quaid/quaid claudecode make_instance /tmp/quaid-m13-test m13test 2>&1'
```

Expected output includes:
- `Created silo: .../claude-code-m13test`
- `Instance ID:  claude-code-m13test`
- `Wrote QUAID_INSTANCE=claude-code-m13test to /tmp/quaid-m13-test/.claude/settings.json`

**Step 3 — verify silo created:**

```bash
ssh example.local 'ls ~/quaid/claude-code-m13test/ && echo "PASS: silo exists"'
```

**Step 4 — verify silo directory structure:**

```bash
ssh example.local '
silo="$HOME/quaid/claude-code-m13test"
for subdir in config data identity journal logs; do
  if [ -d "$silo/$subdir" ]; then
    echo "PASS: $silo/$subdir"
  else
    echo "FAIL: $silo/$subdir missing"
  fi
done
'
```

**Step 5 — verify config/memory.json has adapter type "claude-code":**

```bash
ssh example.local 'python3 -c "
import json
from pathlib import Path
cfg = json.loads(Path(\"$HOME/quaid/claude-code-m13test/config/memory.json\").read_text())
adapter_type = cfg.get(\"adapter\", {}).get(\"type\", \"\")
assert adapter_type == \"claude-code\", f\"Expected claude-code, got {adapter_type!r}\"
print(\"PASS: adapter.type =\", adapter_type)
"'
```

**Step 6 — verify .claude/settings.json written with correct QUAID_INSTANCE:**

```bash
ssh example.local 'python3 -c "
import json
from pathlib import Path
settings = json.loads(Path(\"/tmp/quaid-m13-test/.claude/settings.json\").read_text())
iid = settings.get(\"env\", {}).get(\"QUAID_INSTANCE\", \"\")
assert iid == \"claude-code-m13test\", f\"Expected claude-code-m13test, got {iid!r}\"
print(\"PASS: QUAID_INSTANCE =\", iid)
"'
```

**Step 7 — verify instance appears in quaid instances list:**

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-m13test \
  ~/.openclaw/extensions/quaid/quaid instances list 2>&1 | grep -i m13test || \
  echo "(instances list not available — check quaid version)"'
```

**Step 8 — dry-run creates no silo:**

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main \
  ~/.openclaw/extensions/quaid/quaid claudecode make_instance /tmp/quaid-m13-test m13test-dry --dry-run 2>&1'
ssh example.local 'test ! -d ~/quaid/claude-code-m13test-dry && echo "PASS: dry-run created no silo" || echo "FAIL: dry-run created silo"'
```

**Step 9 — cleanup:**

```bash
ssh example.local 'trash /tmp/quaid-m13-test 2>/dev/null || rm -rf /tmp/quaid-m13-test; echo "cleaned project dir"'
ssh example.local 'trash ~/quaid/claude-code-m13test 2>/dev/null || rm -rf ~/quaid/claude-code-m13test; echo "cleaned silo"'
```

Pass:
- `make_instance` prints the silo path and settings confirmation
- silo directories `config/`, `data/`, `identity/`, `journal/`, `logs/` all exist
- `config/memory.json` has `adapter.type == "claude-code"`
- `/tmp/quaid-m13-test/.claude/settings.json` has `env.QUAID_INSTANCE == "claude-code-m13test"`
- `quaid instances list` includes `claude-code-m13test`
- dry-run leaves no silo on disk

Fail:
- `make_instance` errors or produces no output
- any expected silo subdir is missing
- settings.json absent or contains wrong instance ID
- dry-run creates the silo

## Cross-Platform Project Linking Test

Run this only after both OpenClaw and Claude Code have passed M1-M10.

This is explicitly a user-behavior test. The agent should be able to discover
how to link and use the project without being given function names.

### Phase 1: Create the project and add a doc in OpenClaw

Prepare a source root:

```bash
ssh example.local 'mkdir -p /tmp/quaid-cross-src && cat > /tmp/quaid-cross-src/main.py <<\"PY\"
def harbor_status():
    return "North pier beacon is offline"
PY'
```

Ask OC naturally:

- `Can you create a project named cross-live-test for /tmp/quaid-cross-src?`
- `Do you see the existing cross-live-test project? Can we add a document to it?`
- `Please add a project document that says the north pier beacon is offline and the maintenance window starts at 02:15 UTC.`

Verify from shell:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid project show cross-live-test 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs list --project cross-live-test 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid recall "north pier beacon" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
```

Then ask OC:

- `What does the cross-live-test project doc say about the beacon?`

Pass:
- OC can retrieve the doc content through Quaid

### Phase 2: Link the same project in Claude Code and add a second doc

Ask CC naturally:

- `Do you see the existing cross-live-test project? Can we add a document to it?`
- `Please add another project document that says code word Ember Glass means pager escalation level 2.`

Verify from shell:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid project show cross-live-test 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid docs list --project cross-live-test 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main ~/.openclaw/extensions/quaid/quaid recall "Ember Glass" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
```

Pass:
- CC can use the existing project rather than needing a new one
- CC can add a doc and Quaid can recall it

### Phase 3: Cross-recall both directions

Ask CC:

- `What did the cross-live-test project say about the beacon?`

Ask OC:

- `What does the cross-live-test project say about Ember Glass?`

Optional provenance follow-up if needed:

- `How did you know that?`

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
- OC: `~/quaid/openclaw-main/` (`QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw-main`)
- CC: `~/quaid/claude-code-main/` (`QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code-main`)

```bash
# OC instance health
ssh example.local 'sqlite3 ~/quaid/data/memory.db "SELECT COUNT(*) FROM nodes; SELECT COUNT(*) FROM edges;"'
ssh example.local 'sqlite3 ~/quaid/data/memory.db "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL;"'
ssh example.local 'ls ~/quaid/openclaw-main/journal/'
ssh example.local 'cat ~/quaid/openclaw-main/USER.snippets.md 2>/dev/null'
ssh example.local 'ls -lt ~/quaid/openclaw-main/logs/ | head -20'
ssh example.local 'cat ~/quaid/openclaw-main/config/memory.json | python3 -m json.tool | head -20'
ssh example.local 'cat ~/quaid/openclaw-main/data/circuit-breaker.json 2>/dev/null'
ssh example.local 'cat ~/quaid/openclaw-main/logs/janitor/checkpoint-all.json 2>/dev/null'

# CC instance health
ssh example.local 'sqlite3 ~/quaid/claude-code-main/data/memory.db "SELECT COUNT(*) FROM nodes; SELECT COUNT(*) FROM edges;" 2>/dev/null || echo "CC DB not found"'
ssh example.local 'ls ~/quaid/claude-code-main/journal/ 2>/dev/null || echo "CC journal not found"'
```

Audit identity files (SOUL, USER, MEMORY — now live in `identity/` subdirectory):

```bash
# OC identity
ssh example.local 'for f in /Users/owner/quaid/openclaw-main/identity/{SOUL,USER,MEMORY}.md; do echo "===== $f"; ls -l "$f" 2>/dev/null || true; sed -n "1,80p" "$f" 2>/dev/null || true; echo; done'
# CC identity
ssh example.local 'for f in /Users/owner/quaid/claude-code-main/identity/{SOUL,USER,MEMORY}.md; do echo "===== $f"; ls -l "$f" 2>/dev/null || true; sed -n "1,80p" "$f" 2>/dev/null || true; echo; done'
```

Audit project docs and snippets/journals:

```bash
# OC project docs
ssh example.local 'find /Users/owner/quaid/openclaw-main/projects -maxdepth 3 -name "PROJECT.md" -o -name "TOOLS.md" -o -name "AGENTS.md" | sort | while read f; do echo "===== $f"; wc -l "$f" 2>/dev/null; sed -n "1,30p" "$f" 2>/dev/null; echo; done'
# CC project docs
ssh example.local 'find /Users/owner/quaid/claude-code-main/projects -maxdepth 3 -name "PROJECT.md" -o -name "TOOLS.md" -o -name "AGENTS.md" 2>/dev/null | sort | while read f; do echo "===== $f"; wc -l "$f" 2>/dev/null; sed -n "1,30p" "$f" 2>/dev/null; echo; done'
# Live-test project (shared or per-instance depending on test run)
ssh example.local 'find /Users/owner/quaid/shared/projects/live-test /Users/owner/quaid/openclaw-main/projects/live-test 2>/dev/null -maxdepth 2 -type f | sort | while read f; do echo "===== $f"; wc -l "$f"; sed -n "1,80p" "$f"; echo; done'
# Snippets and journals
ssh example.local 'for f in /Users/owner/quaid/openclaw-main/SOUL.snippets.md /Users/owner/quaid/openclaw-main/USER.snippets.md /Users/owner/quaid/claude-code-main/SOUL.snippets.md /Users/owner/quaid/claude-code-main/USER.snippets.md; do echo "===== $f"; wc -l "$f" 2>/dev/null || echo "(absent — builds via extraction)"; sed -n "1,60p" "$f" 2>/dev/null; echo; done'
ssh example.local 'for f in /Users/owner/quaid/openclaw-main/journal/SOUL.journal.md /Users/owner/quaid/openclaw-main/journal/USER.journal.md /Users/owner/quaid/openclaw-main/journal/MEMORY.journal.md /Users/owner/quaid/claude-code-main/journal/SOUL.journal.md /Users/owner/quaid/claude-code-main/journal/USER.journal.md /Users/owner/quaid/claude-code-main/journal/MEMORY.journal.md; do echo "===== $f"; wc -l "$f" 2>/dev/null || true; sed -n "1,60p" "$f" 2>/dev/null || true; echo; done'
# Project logs
ssh example.local 'find /Users/owner/quaid/openclaw-main/projects /Users/owner/quaid/claude-code-main/projects -name "PROJECT.log" 2>/dev/null | sort | while read f; do echo "===== $f"; wc -l "$f"; sed -n "1,60p" "$f"; echo; done'
```

Pass criteria:
- per-instance identity files (`identity/SOUL.md`, `identity/USER.md`, `identity/MEMORY.md`) are present for both OC and CC; not empty placeholders
- quaid project docs (`projects/quaid/PROJECT.md`, `TOOLS.md`, `AGENTS.md`) exist for both instances
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
