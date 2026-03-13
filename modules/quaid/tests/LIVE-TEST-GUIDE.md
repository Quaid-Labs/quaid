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

## Core Rules

- Use this document as the source of truth for the live test procedure.
- Start from a clean install unless the user explicitly says to skip it.
- Run the live test from the `canary` branch. Verify the checkout before
  installing or testing.
- Use the installer script, not ad hoc install steps.
- Lower model cost before testing: try Haiku first, step up to Sonnet only if
  quality is too degraded to run the test reliably.
- Send ISSUE messages when something breaks or the environment is unclear.
- Do not send routine milestone status messages.
- After a fix, re-run the failed milestone. Do not mark it done without
  re-verification.
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
- OpenClaw agent wrapper: `/tmp/oc-send.sh`

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

The dev tree lives on spark (`~/quaid/dev`), not on alfie. Sync the full dev
tree to alfie before running the installer — `setup-quaid.mjs` and `lib/` are
at the root of `~/quaid/dev`, not inside `modules/quaid/`:

```bash
rsync -av --checksum \
  --exclude='node_modules/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.git/' --exclude='logs/' --exclude='.env*' \
  ~/quaid/dev/ example.local:~/quaid/dev/
```

Verify branch on spark (the source of truth):

```bash
cd ~/quaid/dev && git branch --show-current && git rev-parse --short HEAD
```

Pass only if the branch is exactly `canary`.

### OpenClaw on example.local

Preview first:

```bash
ssh example.local 'openclaw plugins list 2>/dev/null | grep quaid || true'
ssh example.local 'ls -ld ~/quaid ~/quaid/openclaw ~/quaid/shared 2>/dev/null || true'
```

Uninstall existing OC plugin if present:

```bash
ssh example.local 'openclaw plugins uninstall quaid 2>/dev/null || rm -rf ~/.openclaw/extensions/quaid; echo done'
```

Install with the installer script (run from spark, installs onto alfie).
Use `QUAID_TEST_MOCK_MIGRATION=1` to skip LLM-based migration of existing
workspace files (SOUL.md, USER.md, etc.) — without it the installer runs 5
sequential deep-reasoning calls that block M0 for several minutes:

```bash
ssh example.local 'cd ~/quaid/dev && QUAID_INSTALL_AGENT=1 QUAID_TEST_MOCK_MIGRATION=1 QUAID_OWNER_NAME="Solomon" QUAID_INSTANCE=openclaw node setup-quaid.mjs --agent --workspace "/Users/owner/quaid" --source local'
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
ssh example.local 'cd ~/quaid/dev && QUAID_INSTALL_AGENT=1 QUAID_TEST_MOCK_MIGRATION=1 QUAID_OWNER_NAME="Solomon" QUAID_INSTANCE=claude-code QUAID_INSTALL_CLAUDE_CODE=1 node setup-quaid.mjs --agent --claude-code --workspace "/Users/owner/quaid" --source local'
```

### Post-install verification

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid doctor 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid health 2>&1'
ssh example.local 'cat ~/.claude/settings.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(sorted(d.get(\"hooks\", {}).keys()))"'
```

## Execution Model

### OpenClaw

Run OC interactions through SSH plus `/tmp/oc-send.sh` on `example.local`:

```bash
ssh example.local '/tmp/oc-send.sh "message here"'
ssh example.local '/tmp/oc-send.sh "/new"'
ssh example.local '/tmp/oc-send.sh "/reset"'
ssh example.local '/tmp/oc-send.sh "/compact"'
```

Avoid apostrophes in shell-quoted messages.

### Claude Code

CC hooks require interactive mode. Run CC visibly in local tmux pane `main:99`,
SSH to `example.local`, and launch `claude` from there.

```bash
tmux send-keys -t main:99 "ssh example.local" Enter
tmux send-keys -t main:99 "cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code claude --dangerously-skip-permissions" Enter
```

Read replies with:

```bash
tmux capture-pane -t main:99 -p | tail -30
```

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
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
# Then restart OpenClaw on alfie.
```

After restart, create unextracted session content, let the session idle for
>1 minute, then verify timeout extraction fires.

After the test, restore the timeout and restart again:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
# Then restart OpenClaw on alfie.
```

Pass:
- the timeout fact is extracted with no explicit lifecycle command

### M5: Memory Injection

Create or reuse a distinctive fact such as:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid store "Baxter is a golden retriever who loves tennis balls" 2>&1'
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
ssh example.local 'mkdir -p /tmp/quaid-live-src && printf "print(\"hello\")\n" > /tmp/quaid-live-src/main.py'
```

Ask the agent naturally:

- `Can you create a project named live-test for /tmp/quaid-live-src with a short description?`
- `Can you show me what you know about the live-test project?`
- `Can you update that project's description so it is clearly marked as a live test project?`

Modify the source, then ask naturally:

```bash
ssh example.local 'printf "print(\"modified\")\n" > /tmp/quaid-live-src/main.py'
```

- `Can you check what changed in the live-test project since the last snapshot?`
- `Can you sync the live-test project?`
- `Can you delete the live-test project?`

Verify from shell:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid project list 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid project show live-test 2>&1 || true'
ssh example.local 'test -f /tmp/quaid-live-src/main.py && echo source_still_exists'
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
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid janitor --task all --dry-run 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid janitor --task all 2>&1'
```

Pass:
- janitor completes
- checkpoint file exists afterward

### M10: Docs and Health

Run:

```bash
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid health 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid stats 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid docs list 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid docs check 2>&1'
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
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid project show cross-live-test 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid docs list --project cross-live-test 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=openclaw ~/.openclaw/extensions/quaid/quaid recall "north pier beacon" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
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
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code ~/.openclaw/extensions/quaid/quaid project show cross-live-test 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code ~/.openclaw/extensions/quaid/quaid docs list --project cross-live-test 2>&1'
ssh example.local 'cd ~/quaid && QUAID_HOME=~/quaid QUAID_INSTANCE=claude-code ~/.openclaw/extensions/quaid/quaid recall "Ember Glass" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
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
ssh example.local 'sqlite3 ~/quaid/data/memory.db "SELECT COUNT(*) FROM nodes; SELECT COUNT(*) FROM edges;"'
ssh example.local 'sqlite3 ~/quaid/data/memory.db "SELECT COUNT(*) FROM nodes WHERE embedding IS NOT NULL;"'
ssh example.local 'ls ~/quaid/journal/'
ssh example.local 'cat ~/quaid/USER.snippets.md 2>/dev/null'
ssh example.local 'ls -lt ~/quaid/logs/ | head -20'
ssh example.local 'cat ~/quaid/config/memory.json | python3 -m json.tool | head -20'
ssh example.local 'cat ~/quaid/data/circuit-breaker.json 2>/dev/null'
ssh example.local 'cat ~/quaid/logs/janitor/checkpoint-all.json 2>/dev/null'
```

## Final Closeout

When the run is done:

1. Restore any temporary config changes such as timeout or notification
   verbosity.
2. Restore the normal adapter config if it was switched.
3. Send one final summary to `claude-dev`.
