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
- Host: `alfie.local`
- OpenClaw workspace: `~/quaid`
- Live interaction pane: local tmux pane `main:99`

## Start Condition

Do not begin milestone testing against an existing live Quaid install.

Before any run:
- verify the repo checkout is on `canary`
- preview the current install and runtime paths
- uninstall any existing Quaid install
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
  ~/quaid/dev/ alfie.local:~/quaid/dev/
```

Verify branch on the local source checkout:

```bash
cd ~/quaid/dev && git branch --show-current && git rev-parse --short HEAD
```

Pass only if the branch is exactly `canary`.

### OpenClaw on alfie.local

Preview first:

```bash
ssh alfie.local 'openclaw plugins list 2>/dev/null | grep quaid || true'
ssh alfie.local 'ls -ld ~/quaid ~/quaid/openclaw ~/quaid/shared 2>/dev/null || true'
```

Uninstall existing OC plugin if present:

```bash
ssh alfie.local 'openclaw plugins uninstall quaid 2>/dev/null || rm -rf ~/.openclaw/extensions/quaid; echo done'
```

Install with the installer script on `alfie`, using the synced local tree.
Use `QUAID_TEST_MOCK_MIGRATION=1` to skip LLM-based migration of existing
workspace files (SOUL.md, USER.md, etc.) — without it the installer runs 5
sequential deep-reasoning calls that block M0 for several minutes:

```bash
ssh alfie.local 'cd ~/quaid/dev && QUAID_INSTALL_AGENT=1 QUAID_TEST_MOCK_MIGRATION=1 QUAID_OWNER_NAME="Solomon" QUAID_INSTANCE=openclaw node setup-quaid.mjs --agent --workspace "/Users/clawdbot/quaid" --source local'
```

### Claude Code on alfie.local

Clear old hooks if present, then reinstall with the installer script:

```bash
ssh alfie.local 'python3 - <<\"PY\"
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
ssh alfie.local 'cd ~/quaid/dev && QUAID_INSTALL_AGENT=1 QUAID_TEST_MOCK_MIGRATION=1 QUAID_OWNER_NAME="Solomon" QUAID_INSTANCE=claude-code QUAID_INSTALL_CLAUDE_CODE=1 node setup-quaid.mjs --agent --claude-code --workspace "/Users/clawdbot/quaid" --source local'
```

### Post-install verification

```bash
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid doctor 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid health 2>&1'
ssh alfie.local 'cat ~/.claude/settings.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(sorted(d.get(\"hooks\", {}).keys()))"'
ssh alfie.local 'ls -l ~/quaid/openclaw/identity/SOUL.md ~/quaid/claude-code/identity/SOUL.md 2>/dev/null || true'
```

If either instance-local `identity/SOUL.md` is missing, seed it from the shared
root file before running janitor `--apply`:

```bash
ssh alfie.local 'python3 - <<\"PY\"
from pathlib import Path
src = Path("/Users/clawdbot/quaid/SOUL.md")
for dst in [
    Path("/Users/clawdbot/quaid/openclaw/identity/SOUL.md"),
    Path("/Users/clawdbot/quaid/claude-code/identity/SOUL.md"),
]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        dst.write_text(src.read_text())
        print(f"created {dst}")
PY'
```

## Execution Model

### Phase Start Reset

At the start of each live interaction phase, reset local tmux pane `main:99`
so the interface is fresh and visible.

- OpenClaw phase start: kill and restart pane `main:99`, `ssh alfie.local`,
  then launch `openclaw tui`
- Claude Code phase start: kill and restart pane `main:99`, `ssh alfie.local`,
  then launch `claude`

Recommended pattern:

```bash
tmux respawn-pane -k -t main:99 'zsh -il'
tmux send-keys -t main:99 "ssh alfie.local" Enter
```

Then launch the subject under test:

```bash
tmux send-keys -t main:99 "openclaw tui" Enter
# or
tmux send-keys -t main:99 "cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code claude --dangerously-skip-permissions" Enter
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
tmux send-keys -t main:99 "ssh alfie.local" Enter
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
SSH to `alfie.local`, and launch `claude` from there.

```bash
tmux respawn-pane -k -t main:99 'zsh -il'
tmux send-keys -t main:99 "ssh alfie.local" Enter
tmux send-keys -t main:99 "cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code claude --dangerously-skip-permissions" Enter
```

Read replies with:

```bash
tmux capture-pane -t main:99 -p | tail -30
```

**Important:** For this live test flow, end the visible CC session with
`/exit` in pane `99` to return cleanly to the remote shell. After each CC
session end, explicitly verify that extraction happened by checking
`~/quaid/claude-code/data/extraction-signals/`, the CC daemon log, or the
shared DB at `~/quaid/data/memory.db`. If a session ends cleanly but no
`session_end` signal appears, do not assume extraction fired.

Current live-test fallback on `claude` `2.1.76`:

1. Find the real CC transcript under `~/.claude/projects/-Users-clawdbot-quaid/`.
2. Write a manual `session_end` signal against that real transcript.
3. Verify the shared DB at `~/quaid/data/memory.db`.

Example:

```bash
ssh alfie.local 'python3 - <<\"PY\"
import sys
sys.path.insert(0, \"/Users/clawdbot/quaid/plugins/quaid\")
from core.extraction_daemon import write_signal
p = write_signal(
    signal_type=\"session_end\",
    session_id=\"<real-cc-session-id>\",
    transcript_path=\"/Users/clawdbot/.claude/projects/-Users-clawdbot-quaid/<real-cc-session-id>.jsonl\",
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
ssh alfie.local 'wc -l ~/.claude/rules/quaid-projects.md && sed -n "1,220p" ~/.claude/rules/quaid-projects.md'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code ~/.openclaw/extensions/quaid/quaid project list 2>&1'
ssh alfie.local 'find ~/quaid/shared/projects -maxdepth 3 -type f | sort'
ssh alfie.local 'python3 - <<\"PY\"
import json
from pathlib import Path
p = Path(\"/Users/clawdbot/quaid/project-registry.json\")
if p.exists():
    print(json.dumps(json.loads(p.read_text()), indent=2))
PY'
```

Pass only if `~/.claude/rules/quaid-projects.md` includes project sections like
`--- quaid/TOOLS.md ---` and `--- quaid/AGENTS.md ---`. If it only contains
`USER.md` / `MEMORY.md` projections, CC project CRUD is not being tested
against a valid shared-project bootstrap state yet. Also verify the global
registry entry for `quaid` points at `$QUAID_HOME/shared/projects/quaid`, not
an instance-local path such as `$QUAID_HOME/openclaw/projects/quaid`.

For CC `/compact`, the proof token should store from the visible live run
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

### M1: Extraction via `/new`

> **STATUS: Should now work for OC.** Previous "KNOWN BROKEN" label was based
> on incorrect analysis. OC's `performGatewaySessionReset` updates `sessions.json`
> with a new UUID AND calls `archiveSessionTranscriptsForSession` (creating a
> `.reset.*` backup) for both `/new` and `/reset`. The key-transition watcher in
> `tickSessionIndex` detects the sessionId change and arms a targeted orphan
> check. Run for both OC and CC.

Seed a distinctive `PROOFNEW-<timestamp>` fact, then trigger `/new`.

Pass:
- the fact is stored after the lifecycle boundary
- DB or recall output shows the new token

### M2: Extraction via `/reset`

Seed a distinctive `PROOFRESET-<timestamp>` fact, then trigger `/reset`.

Pass:
- the fact is stored from the pre-reset session

### M3: Extraction via `/compact`

Seed a distinctive `PROOFCOMPACT-<timestamp>` fact, build some conversation
context, then trigger `/compact`.

Pass:
- the fact is stored
- logs or hook trace show the compaction signal

### M4: Timeout Extraction

Temporarily set `capture.inactivityTimeoutMinutes` to `1`, then **restart
OpenClaw** so the new value takes effect (the SessionTimeoutManager reads the
timeout only at plugin init — a config-only change without restart has no
effect):

```bash
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
# Then restart OpenClaw on alfie.
```

After restart, create unextracted session content, let the session idle for
>1 minute, then verify timeout extraction fires.

After the test, restore the timeout and restart again:

```bash
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
# Then restart OpenClaw on alfie.
```

Pass:
- the timeout fact is extracted with no explicit lifecycle command
- for Claude Code, verify `quaid daemon status` points at the correct
  instance root before idling:
  - `instance_root: /Users/clawdbot/quaid/claude-code`
  - `log_file: /Users/clawdbot/quaid/claude-code/logs/daemon/extraction-daemon.log`
  - `pid_file: /Users/clawdbot/quaid/claude-code/data/extraction-daemon.pid`

### M5: Memory Injection

Create or reuse a distinctive fact such as:

```bash
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid store "Baxter is a golden retriever who loves tennis balls" 2>&1'
```

Then ask the agent naturally:

- `What do you know about Baxter?`

Pass:
- the answer includes the stored fact
- the answer appears to come from injected recall rather than a fresh explicit
  browse/search step

### M6: Deliberate Recall

Ask natural questions that require actual retrieval:

- `Tell me everything you remember about my niece.`
- `What does my family do for holidays?`

Pass:
- the agent surfaces the correct stored facts
- the answers are materially grounded in memory, not generic filler

### M7: Graph Traversal Verification

Run shell or DB verification to confirm the expected relationship edges exist.

Pass:
- the expected edges are present

### M8: Full Project System CRUD

This is a capability test. Do not tell the agent the exact command names.

Prepare a source root:

```bash
ssh alfie.local 'mkdir -p /tmp/quaid-live-src && printf "print(\"hello\")\n" > /tmp/quaid-live-src/main.py'
```

Ask the agent naturally:

- `Can you create a project named live-test for /tmp/quaid-live-src with a short description?`
- `Can you show me what you know about the live-test project?`
- `Can you update that project's description so it is clearly marked as a live test project?`

Modify the source, then ask naturally:

```bash
ssh alfie.local 'printf "print(\"modified\")\n" > /tmp/quaid-live-src/main.py'
```

- `Can you check what changed in the live-test project since the last snapshot?`
- `Can you sync the live-test project?`
- `Can you delete the live-test project?`

Verify from shell:

```bash
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid project list 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid project show live-test 2>&1 || true'
ssh alfie.local 'test -f /tmp/quaid-live-src/main.py && echo source_still_exists'
```

Pass:
- create works
- show works
- update works
- snapshot/sync work
- delete removes the project but not the source directory

### M9: Janitor

Run:

```bash
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid janitor --task all --dry-run 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid janitor --task all --apply --approve 2>&1'
```

Pass:
- janitor completes
- `checkpoint-all.json` exists afterward with `status: completed`
- `janitor-stats.json` reports `success: true`

### M10: Docs and Health

Run:

```bash
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid health 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid stats 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid docs list 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid docs check 2>&1'
```

Pass:
- health is good enough to continue
- stats are sensible
- docs commands run successfully

## Cross-Platform Project Linking Test

Run this only after both OpenClaw and Claude Code have passed M1-M10.

This is explicitly a user-behavior test. The agent should be able to discover
how to link and use the project without being given function names.

### Phase 1: Create the project and add a doc in OpenClaw

Prepare a source root:

```bash
ssh alfie.local 'mkdir -p /tmp/quaid-cross-src && cat > /tmp/quaid-cross-src/main.py <<\"PY\"
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
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid project show cross-live-test 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid docs list --project cross-live-test 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid recall "north pier beacon" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
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
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code ~/.openclaw/extensions/quaid/quaid project show cross-live-test 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code ~/.openclaw/extensions/quaid/quaid docs list --project cross-live-test 2>&1'
ssh alfie.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code ~/.openclaw/extensions/quaid/quaid recall "Ember Glass" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
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

After all milestones and the cross-platform project linking test:

```bash
ssh alfie.local 'sqlite3 ~/quaid/data/memory.db "SELECT COUNT(*) FROM nodes; SELECT COUNT(*) FROM edges;"'
ssh alfie.local 'sqlite3 ~/quaid/data/memory.db "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL;"'
ssh alfie.local 'ls ~/quaid/journal/'
ssh alfie.local 'cat ~/quaid/USER.snippets.md 2>/dev/null'
ssh alfie.local 'ls -lt ~/quaid/logs/ | head -20'
ssh alfie.local 'cat ~/quaid/config/memory.json | python3 -m json.tool | head -20'
ssh alfie.local 'cat ~/quaid/data/circuit-breaker.json 2>/dev/null'
ssh alfie.local 'cat ~/quaid/logs/janitor/checkpoint-all.json 2>/dev/null'
```

Also audit the markdown artifacts directly, not just their existence:

```bash
ssh alfie.local 'for f in /Users/clawdbot/quaid/{SOUL,USER,IDENTITY,TOOLS,AGENTS,PROJECT,MEMORY}.md; do echo "===== $f"; ls -l "$f" 2>/dev/null || true; sed -n "1,80p" "$f" 2>/dev/null || true; echo; done'
ssh alfie.local 'find /Users/clawdbot/quaid/shared/projects/live-test -maxdepth 2 -type f | sort | while read f; do echo "===== $f"; wc -l "$f"; sed -n "1,80p" "$f"; echo; done'
ssh alfie.local 'for f in /Users/clawdbot/quaid/openclaw/SOUL.snippets.md /Users/clawdbot/quaid/claude-code/SOUL.snippets.md /Users/clawdbot/quaid/openclaw/journal/SOUL.journal.md /Users/clawdbot/quaid/openclaw/journal/USER.journal.md /Users/clawdbot/quaid/openclaw/journal/MEMORY.journal.md /Users/clawdbot/quaid/claude-code/journal/SOUL.journal.md /Users/clawdbot/quaid/claude-code/journal/USER.journal.md /Users/clawdbot/quaid/claude-code/journal/MEMORY.journal.md; do echo "===== $f"; wc -l "$f" 2>/dev/null || true; sed -n "1,60p" "$f" 2>/dev/null || true; echo; done'
ssh alfie.local 'find /Users/clawdbot/quaid/shared/projects -name "PROJECT.log" -o -name "*.log" | sort | while read f; do echo "===== $f"; wc -l "$f"; sed -n "1,60p" "$f"; echo; done'
```

Pass criteria:
- root markdown files are present and not obviously malformed or empty placeholders
- the live-test project docs are coherent and point at the correct shared paths
- if the project is expected to have `TOOLS.md`, `AGENTS.md`, or a richer
  `PROJECT.md`, verify that explicitly and record any missing file as a finding
- snippets and journals look structurally sane and consistent with the run
- project logs, if present, are readable and correspond to real actions taken

## Final Closeout

When the run is done:

1. Restore any temporary config changes such as timeout or notification
   verbosity.
2. Restore the normal adapter config if it was switched.
3. Send one final summary to `claude-dev`.
