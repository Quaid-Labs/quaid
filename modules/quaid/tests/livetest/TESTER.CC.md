# Tester Supplement — Claude Code (CC)

Platform-specific notes for the CC tester. Read this alongside `TESTER.SKILL.md`.

---

## Launch

After M0 install, start the CC interaction pane:

```bash
tmux respawn-pane -k -t livetest:CC 'zsh -il'
tmux send-keys -t livetest:CC "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CC "mkdir -p /tmp/cc-livetest && cd /tmp/cc-livetest && QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE CLAUDE_PROJECT_DIR=/tmp/cc-livetest claude --dangerously-skip-permissions" Enter
```

**MANDATORY — set model before any test messages:**
```
/model
# Select claude-haiku-4-5 (fast lane model)
# Never run CC milestones on Opus — too expensive
```
Do not send any test messages until the model is confirmed.

---

## Sending Messages

```bash
tmux send-keys -t livetest:CC "your message" Enter
sleep 10
tmux capture-pane -t livetest:CC -p | tail -30
```

**Always exit with `/exit`** — never Ctrl+C. Ctrl+C bypasses the SessionEnd
hook and extraction will not fire.

`claude -p` (print mode) does not trigger hooks — always use interactive mode.

---

## Extraction Triggers

| Trigger | How | Notes |
|---------|-----|-------|
| Session end | `/exit` in CC pane | Fires SessionEnd hook → daemon extraction |
| Compaction | `/compact` | Fires PreCompact hook → daemon extraction |
| Timeout | inactivity > `capture.inactivityTimeoutMinutes` | Fires daemon-compaction signal (source: timeout_extract) |
| Rolling | session crosses `capture.chunk_tokens` threshold | Daemon polls and fires rolling_stage automatically |

After `/exit` or `/compact`, wait **30–60 seconds** before checking the DB.

---

## Timeout Extraction (M4)

CC has **timeout extraction** but not timeout compaction. When the inactivity
timeout fires, the SessionTimeoutManager writes a `compaction` signal with
`source: timeout_extract`. The daemon processes this as a compaction signal —
facts are extracted and stored. The session itself is not compacted (only OC
supports forced compaction).

**M4 procedure for CC:**

1. Set timeout to 1 minute and restart the daemon:
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
     ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
     ~/.openclaw/extensions/quaid/quaid daemon stop 2>&1; sleep 2; \
     QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
     ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'
   ```

2. Start a fresh CC session in `livetest:CC` from `/tmp/cc-livetest`. Tell CC
   something memorable, then **let it idle for >1 minute** with no further input.

3. Verify extraction fired (daemon log shows `[daemon-compaction]` with
   `source: timeout_extract` — not `daemon-timeout`):
   ```bash
   ssh REMOTE_HOST 'tail -20 WORKSPACE/CC_INSTANCE/logs/daemon/extraction-daemon.log \
     2>/dev/null | grep -i "timeout\|compaction\|timeout_extract"'
   ```

4. Restore and restart:
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
     ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
     ~/.openclaw/extensions/quaid/quaid daemon stop 2>&1; sleep 2; \
     QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
     ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'
   ```

**M4 PASS criteria (CC):** Timeout fact extracted and stored. Daemon log shows
`timeout_extract` signal processed. Note in STATUS: "M4 PASS — timeout
extraction verified (no compaction, expected for CC)."

---

## Daemon Management

CC runs its own extraction daemon. Check status with:
```bash
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
  ~/.openclaw/extensions/quaid/quaid daemon status 2>&1'
```

Verify instance root, log file, and pid file all point to `CC_INSTANCE`.

---

## Auth Token

CC requires a long-lived OAuth token at `WORKSPACE/config/adapters/claude-code/.auth-token`.
Without it the daemon falls back to `claude -p` subprocess calls and triggers
a hook storm (many concurrent hooks.py processes). If you see this, check:
```bash
ssh REMOTE_HOST 'pgrep -c -f hooks.py 2>/dev/null || echo 0'
```
More than 3 concurrent hooks.py processes = hook storm. Report to coordinator immediately.

---

## Instance Isolation

`QUAID_INSTANCE` is pinned **per project dir**, not globally. It is set in
`/tmp/cc-livetest/.claude/settings.json`, not in `~/.claude/settings.json`.

Verify:
```bash
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"/tmp/cc-livetest/.claude/settings.json\")); print(d.get(\"env\",{}).get(\"QUAID_INSTANCE\",\"MISSING\"))"'
# Expected: CC_INSTANCE
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"$HOME/.claude/settings.json\")); print(d.get(\"env\",{}).get(\"QUAID_INSTANCE\",\"(absent — correct)\"))"'
# Expected: absent
```

---

## Database and CLI

```bash
# DB
ssh REMOTE_HOST 'sqlite3 WORKSPACE/CC_INSTANCE/data/memory.db "SELECT COUNT(*) FROM nodes;"'

# CLI
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
  ~/.openclaw/extensions/quaid/quaid recall "query" 2>&1'
```

---

## Milestone Notes

### M3 — Rolling Extraction + `/compact`
CC supports `/compact` directly. After seeding and building up >1500 tokens,
verify rolling-extraction.jsonl has `rolling_stage` events, then send `/compact`.

### M4 — Timeout Extraction
See dedicated section above. CC gets timeout extraction (no compaction). PASS
with note on the no-compaction behaviour.

### M8 — Project CRUD
For Phase 1, switch model to Sonnet or better before the work directive.
Haiku does not reliably follow file-placement policy. Run `/model` first.

### M13 — Multi-Instance Verification
CC-only milestone. Verifies `quaid claudecode make_instance` creates a properly
isolated silo. Follow the guide exactly — includes a cross-project spillover proof.
