# Quaid Live Test Guide

Instructions for an LLM agent to run a full live validation of Quaid against
a real OC or CC instance. ALL interaction with the target agent happens via
tmux message passing. ALL verification happens via shell commands and DB
queries from a separate pane. This is black-box testing — no direct function
calls, no imports, no mocks.

**Who runs this:** An LLM agent (e.g. claude-dev in window 4) driving tests
against a target agent window (e.g. OC in window 1, CC in window 4 on another
machine).

**Communication pattern:**
```
# Send message to target agent
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target-window> "<message>"

# Verify via shell (from tester's own pane)
sqlite3 $QUAID_HOME/data/memory.db "SELECT ..."
tail -20 /path/to/daemon.log
quaid recall "query"
```

---

## Prerequisites

- tmux session with tester pane and target agent pane
- SSH access to target machine if remote (example.local for OC)
- `tmux-msg.sh` available at `~/quaid/util/scripts/tmux-msg.sh`
- `sqlite3` for DB verification
- Know the target's QUAID_HOME path and log locations

## Required Start Condition

Do not begin milestone testing against an existing live Quaid install.

Before any run:
- Preview the current install target and runtime paths first.
- Uninstall Quaid if it is present.
- Reinstall Quaid cleanly.
- Verify the install is stable before starting M1.

Minimum stability check before M1:
- plugin/bundle exists in the expected runtime path
- `quaid doctor` or `quaid health` succeeds
- active DB/log paths are identified and match the running instance
- daemon starts cleanly
- one basic agent message succeeds without hanging

If the reinstall is skipped, note that the run is not a clean live install validation.

### Key paths

**OpenClaw (example.local):**
- QUAID_HOME: `~/.openclaw/workspace/`
- memory.db: `~/.openclaw/workspace/data/memory.db`
- Daemon log: `~/.openclaw/workspace/logs/daemon/extraction-daemon.log`
- Hook trace: `~/.openclaw/workspace/logs/quaid-hook-trace.jsonl`
- Sessions index: `~/.openclaw/agents/main/sessions/sessions.json`
- Plugin dir: `~/.openclaw/workspace/plugins/quaid/`

**Claude Code (local):**
- QUAID_HOME: set per instance (check `echo $QUAID_HOME`)
- memory.db: `$QUAID_HOME/data/memory.db`
- Daemon log: `$QUAID_HOME/logs/extraction-daemon.log`
- Hooks config: `~/.claude/settings.json`
- Identity: `$QUAID_HOME/claude-code/identity/`

---

## Milestone 0: Clean Install

The very first thing to validate — can Quaid install from scratch?

This milestone is mandatory unless the user explicitly tells you to skip clean
install validation for that run.

### OpenClaw

**Action (from shell, on target machine):**
```bash
# Uninstall if present
openclaw plugin uninstall quaid

# Install from canary
openclaw plugin install quaid --source github --branch canary
# OR local install:
openclaw plugin install quaid --source /path/to/quaid/dev/modules/quaid
```

**Verify:**
```bash
openclaw plugin list | grep quaid
ls ~/.openclaw/workspace/plugins/quaid/   # check bundle timestamp
QUAID_HOME=~/.openclaw/workspace quaid doctor
openclaw agent --agent main -m "installation smoke test" 2>&1 | tail -20
```

**Pass:** Plugin listed, doctor healthy, QUAID_HOME has config/, data/, logs/,
and a basic OC agent turn completes without hanging.

**Gotcha:** OC plugin install caches old builds. If behavior is stale, compare
the bundle file timestamp in `plugins/quaid/` against your commit time. Force
rebuild if stale.

### Claude Code

**Action (from shell, on target machine):**
```bash
# Remove existing hooks
# (manually edit ~/.claude/settings.json to remove quaid hook entries)

# Run bootstrap installer
cd ~/quaid/dev/modules/quaid && ./scripts/install-claude-code.sh

# Verify
cat ~/.claude/settings.json | python3 -c "import sys,json; h=json.load(sys.stdin).get('hooks',{}); print([k for k in h if 'quaid' in str(h[k]).lower()])"
quaid doctor
```

**Pass:** Hooks registered (UserPromptSubmit, SessionEnd, SubagentStart,
SubagentStop), doctor healthy, identity files present.

**Gotcha:** `CLAUDE_CODE_OAUTH_TOKEN` must be set for daemon extraction.
If extraction later fails with 403, re-seed via `claude setup-token`.

---

## Milestone 1: Extraction via /new

Tests that the core extraction pipeline works end-to-end.

**Step 1 — Seed a fact via tmux:**
```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Don't manually store this, this is a test of auto-extraction. My test token is PROOFNEW-$(date +%s). Remember that."
```

**Step 2 — Wait for agent to process the message** (~10s for agent to respond).

**Step 3 — Trigger extraction (adapter-specific):**

For OC — send `/new`:
```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> "/new"
```

For CC — end and restart the session:
```bash
# Send exit signal or wait for SessionEnd hook
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> "/exit"
# Then start a new session in the target pane
```

**Step 4 — Wait for extraction daemon** (~10-30s). Monitor:
```bash
# OC:
tail -5 ~/.openclaw/workspace/logs/daemon/extraction-daemon.log
# CC:
tail -5 $QUAID_HOME/logs/extraction-daemon.log
```

**Step 5 — Verify fact stored:**
```bash
# Via CLI
quaid recall "PROOFNEW"

# Via DB (more reliable)
sqlite3 $QUAID_HOME/data/memory.db "SELECT id, substr(name,1,80), created_at FROM nodes WHERE name LIKE '%PROOFNEW%';"
```

**Pass:** Fact containing PROOFNEW token appears in results, created_at is
after the tmux message timestamp.

**Gotcha (OC):** Daemon may extract from the wrong session after `/new`. The
fix uses `sessions.json.updatedAt` for session ranking. If the wrong session
is extracted, check `pickActiveInteractiveSession()` in adapter.ts.

---

## Milestone 2: Extraction via /reset

**Same pattern as Milestone 1** but use `/reset` instead of `/new`.

Seed token: `PROOFRESET-<ts>`

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Auto-extraction test. My reset token is PROOFRESET-$(date +%s)."
# Wait for response...
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> "/reset"
```

**Pass:** Fact stored from pre-reset session content.

**Gotcha:** `/reset` hook dispatch is the least reliable on OC. If native hook
doesn't fire, command-based detection (scanning transcript for `/reset` text)
is the fallback. Check hook trace:
```bash
tail -5 ~/.openclaw/workspace/logs/quaid-hook-trace.jsonl
```

---

## Milestone 3: Extraction via /compact

The most reliable extraction trigger on OC.

Seed token: `PROOFCOMPACT-<ts>`

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Auto-extraction test. My compact token is PROOFCOMPACT-$(date +%s)."
# Wait for response, then keep chatting to build up context...
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Tell me a long story about a dragon. Make it at least 500 words."
# Wait, then trigger compaction
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> "/compact"
```

**Pass:** Fact stored. `before_compaction` hook is the most reliable on OC.

---

## Milestone 4: Timeout Extraction

Tests that extraction fires on session inactivity without any explicit trigger.

Seed token: `PROOFTIMEOUT-<ts>`

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Auto-extraction test. My timeout token is PROOFTIMEOUT-$(date +%s)."
```

**Do NOT send any lifecycle command.** Wait for the configured timeout.

**Before testing:** Set `capture.inactivityTimeoutMinutes` to `1` in
`$QUAID_HOME/$QUAID_INSTANCE/config/memory.json` and restart the gateway so
the timeout fires in ~1 minute instead of the default 60. Reset it after testing.

You can also override via environment variable `SESSION_EXTRACT_TIMEOUT_MS`
(value in milliseconds) or `QUAID_SESSION_EXTRACT_TIMEOUT_MS`.

Monitor daemon log for extraction trigger. Then verify:
```bash
sqlite3 $QUAID_HOME/data/memory.db "SELECT id, substr(name,1,80), created_at FROM nodes WHERE name LIKE '%PROOFTIMEOUT%';"
```

**Pass:** Fact stored after inactivity timeout with no explicit trigger.

---

## Milestone 5: Memory Injection

Tests that previously-stored memories are injected into new sessions via
the `UserPromptSubmit` hook.

**Step 1 — Store a distinctive fact** (via CLI from shell, not tmux):
```bash
quaid store "Baxter is a golden retriever who loves tennis balls"
```

**Step 2 — Ask the agent about it via tmux:**
```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "What do you know about Baxter?"
```

**Step 3 — Observe the agent's response.** Capture it from the tmux pane:
```bash
tmux capture-pane -t <target> -p | tail -20
```

**Pass:** Agent mentions Baxter and golden retriever from injected context,
without making an explicit recall/search tool call. The hook pre-loaded it.

**Gotcha (CC):** Hook must return `hookSpecificOutput` wrapper format. If
injection silently fails, test the hook directly:
```bash
QUAID_HOME=$QUAID_HOME python3 -m adaptors.claude_code.hooks UserPromptSubmit "What about Baxter?"
```

---

## Milestone 6: Project System

All commands sent via tmux to the target agent, verified via shell.

### 6a: Create project

```bash
# Create a temp source dir
mkdir -p /tmp/quaid-live-src && echo "print('hello')" > /tmp/quaid-live-src/main.py

# Ask agent to create project
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Run: quaid project create live-test -d 'Live test project' -s /tmp/quaid-live-src"
```

**Verify:**
```bash
quaid project list | grep live-test
ls $QUAID_HOME/projects/live-test/PROJECT.md
```

**Pass:** Project in list, PROJECT.md exists with description.

### 6b: Shadow git snapshot

```bash
# Modify source
echo "print('modified')" > /tmp/quaid-live-src/main.py

# Ask agent to snapshot
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Run: quaid project snapshot live-test"
```

**Verify:** Capture agent output — should list main.py as modified with diff.

### 6c: Sync

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Run: quaid project sync"
```

**Verify (OC):**
```bash
ls ~/.openclaw/workspace/projects/live-test/
```

### 6d: Cleanup

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Run: quaid project delete live-test"

# Verify gone
quaid project list | grep live-test  # should be empty
ls /tmp/quaid-live-src/main.py       # source file still exists
```

**Pass:** Project deleted, source files untouched.

### 6e: Cross-Platform Shared Project Context (OC -> CC)

Run this only after **both OC and CC are operational**.

Goal: verify that CC can attach to a project that OC already created and answer
questions using **Quaid project context / project search**, not by directly
browsing the HDD first.

**Phase 1 — OC creates the project**

Use OC to create a project with distinctive source content:

```bash
mkdir -p /tmp/quaid-cross-src
cat > /tmp/quaid-cross-src/main.py <<'PY'
def harbor_status():
    return "North pier beacon is offline"
PY

TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <oc-target> \
  "Run: quaid project create cross-live-test -d 'Cross-instance project context test' -s /tmp/quaid-cross-src"
```

Verify from shell:
```bash
quaid project list | grep cross-live-test
quaid project show cross-live-test
```

**Phase 2 — CC loads the existing project naturally**

In CC, do **not** tell it to browse files directly. Ask:

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <cc-target> \
  "load the cross-live-test project that is already on this system"
```

Then ask a project-specific fact question:

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <cc-target> \
  "What is the harbor_status detail for that project?"
```

Then ask for provenance:

```bash
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <cc-target> \
  "how did you find that fact"
```

**Pass:**
- CC can identify and load the OC-created project.
- CC answers with the expected fact (for the example above: `North pier beacon is offline`).
- CC explains that it found the fact through Quaid project context, project recall,
  injected project memory, project search, or equivalent Quaid-mediated retrieval.

**Fail:**
- CC says it searched/read files from disk first instead of using project context.
- CC cannot find the OC-created project.
- CC cannot answer a fact that should already be available through the shared project system.

---

## Milestone 7: Docs + Maintenance

```bash
# Doc search
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Run: quaid docs search 'extraction pipeline'"

# Health
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Run: quaid doctor"

# Stats
TMUX_MSG_SENDER=tester TMUX_MSG_SOURCE=test ~/quaid/util/scripts/tmux-msg.sh <target> \
  "Run: quaid stats"
```

**Verify:** Capture outputs. Stats should show non-zero memory count from
earlier milestones.

---

## DB Verification Queries

Run these from the tester pane after any milestone:

```sql
-- Facts stored in this test run (use your PROOF* tokens)
SELECT id, text, created_at, source_session_id
FROM nodes WHERE name LIKE '%PROOF%' ORDER BY created_at DESC;

-- Recent extractions
SELECT * FROM extraction_log ORDER BY created_at DESC LIMIT 5;

-- Session tracking
SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 5;

-- Project registry
SELECT * FROM json_each(readfile('$QUAID_HOME/projects/project-registry.json'));
```

---

## Known Gotchas

| Issue | Adapter | Symptom | Mitigation |
|-------|---------|---------|------------|
| Session misrouting on /new | OC | Daemon extracts stale session | Verify `sessions.json.updatedAt` ranking in adapter.ts |
| Hook output format | CC | Memory injection silently fails | Must use `hookSpecificOutput` wrapper |
| OAuth token expiry | CC | Extraction fails with 403 | Re-seed via `claude setup-token` |
| /reset hook unreliable | OC | No extraction on /reset | Command-based detection is fallback; check hook trace |
| Plugin cache staleness | OC | Old behavior after reinstall | Compare bundle timestamp vs commit time |
| Transcript mtime vs updatedAt | OC | Wrong session selected | Fixed; verify adapter.ts uses updatedAt first |
| Subagent transcript merge | CC | Child facts missing in parent | Check subagent registry + parent extraction log |
| Large transcript chunking | Both | Late-session facts missing | Waterfall chunking with carryover carries context forward |
| Janitor event queue lag | Both | Project events delayed | Check staging path for queued events |
| tmux-msg delivery | Both | Agent doesn't respond | Verify target window number; check agent is active |
| OC hook registration | OC | Hooks never fire | Must use `api.on()` not `registerHook()` — check plugin build |

---

## Post-Test Cleanup

```bash
quaid project delete live-test 2>/dev/null
rm -rf /tmp/quaid-live-src

# Remove test facts
for id in $(sqlite3 $QUAID_HOME/data/memory.db "SELECT id FROM nodes WHERE name LIKE '%PROOF%';"); do
  quaid delete-node "$id"
done
```

---

## Run Log Template

Copy this to track results for each run:

```
Date: YYYY-MM-DD
Target: OC/CC @ hostname
Branch: canary @ <commit>
Tester: claude-dev @ window 4

M0 Install:     [ ] PASS / FAIL — notes:
M1 Extract/new: [ ] PASS / FAIL — notes:
M2 Extract/reset: [ ] PASS / FAIL — notes:
M3 Extract/compact: [ ] PASS / FAIL — notes:
M4 Timeout:     [ ] PASS / FAIL — notes:
M5 Injection:   [ ] PASS / FAIL — notes:
M6 Projects:    [ ] PASS / FAIL — notes:
M7 Docs/Health: [ ] PASS / FAIL — notes:

New gotchas discovered:
-
```
