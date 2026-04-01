# Tester Supplement — OpenClaw (OC)

Platform-specific notes for the OC tester. Read this alongside `TESTER.SKILL.md`.

---

## Launch

After M0 install, start the OC interaction pane:

```bash
tmux respawn-pane -k -t livetest:OC 'zsh -il'
tmux send-keys -t livetest:OC "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:OC "openclaw tui" Enter
```

Respawn the pane again if it becomes contaminated mid-run.

---

## Sending Messages

**Via wrapper script** (preferred — cleans stale sessions before each call):
```bash
ssh REMOTE_HOST '/tmp/oc-send.sh "your message"'
ssh REMOTE_HOST '/tmp/oc-send.sh "/reset"'
```

**Avoid apostrophes** in OC messages — use "do not" instead of "don't".

**If the wrapper is missing:**
```bash
ssh REMOTE_HOST 'pkill -f openclaw-agent 2>/dev/null; sleep 1; \
  openclaw agent --agent main -m "message" > /tmp/oc-reply.txt 2>/tmp/oc-err.txt; \
  cat /tmp/oc-reply.txt'
```

For lifecycle commands (`/reset`, `/new`, `/compact`) during live milestones,
send directly to the tmux pane instead:
```bash
tmux send-keys -t livetest:OC "/reset" Enter
tmux capture-pane -t livetest:OC -p | tail -30
```

---

## Extraction Triggers

| Trigger | How | Notes |
|---------|-----|-------|
| New session | `/new` | See version quirks below |
| Session reset | `/reset` | Extracts pre-reset session |
| Compaction | `/compact` | Extracts + compacts |
| Timeout | inactivity > `capture.inactivityTimeoutMinutes` | Daemon-compaction signal (source: timeout_extract) |
| Rolling | session crosses `capture.chunk_tokens` threshold | Daemon polls automatically |

After any extraction trigger, wait **30–60 seconds** before checking the DB.

---

## `/new` Version Quirk

OC behaviour changed at version 2026.3.13:

**OC < 2026.3.13 (TUI intercepts `/new`):**
- TUI handles `/new` as a built-in command — does NOT pass it to the model
- Sessions.json is NOT updated immediately after `/new` — send one follow-up
  message (e.g. `Hello`) to write the new key and trigger `new_key_detected`
- Hook trace marker: `session_index.new_key_detected` → `session_index.signal_queued`

**OC ≥ 2026.3.13 (TUI passes `/new` to model):**
- Model replies saying it does not know the `/new` command — this is expected
- Adapter detects `/new` in the message event via `handleSlashLifecycleFromMessage`
  and writes a ResetSignal for the pre-/new session
- Extraction still fires — no follow-up message needed
- Hook trace marker: `hook.message.command_detected` (command=new)

Check the installed OC version to know which path applies:
```bash
ssh REMOTE_HOST 'openclaw --version 2>/dev/null || clawdbot --version 2>/dev/null'
```

---

## Timeout Extraction and Compaction (M4)

OC has both **timeout extraction** and **timeout compaction**. When the inactivity
timeout fires, the SessionTimeoutManager writes a `compaction` signal with
`source: timeout_extract`. OC also supports `/compact` for forced compaction.

**M4 procedure for OC:**

1. Set timeout to 1 minute and restart OpenClaw:
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=OC_INSTANCE \
     ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
   # Then restart OpenClaw on the remote host:
   ssh REMOTE_HOST 'pkill -f openclaw-gateway; sleep 2; \
     nohup openclaw gateway > /tmp/oc-gw.log 2>&1 &'
   # Wait for gateway to come back:
   ssh REMOTE_HOST 'for i in $(seq 1 30); do \
     curl -sf http://localhost:18789/health > /dev/null 2>&1 && echo "Gateway ready" && break \
     || sleep 2; done'
   ```

2. Start a fresh OC session in `livetest:OC`, tell the agent something memorable,
   then **let it idle for >1 minute** with no further messages.

3. Verify extraction fired:
   ```bash
   ssh REMOTE_HOST 'grep -i "timeout\|timeout_extract\|daemon-compaction" \
     WORKSPACE/OC_INSTANCE/logs/daemon.log 2>/dev/null | tail -5'
   ```

4. Restore and restart:
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=OC_INSTANCE \
     ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
   # Restart gateway again.
   ```

**M4 PASS criteria (OC):** Timeout fact extracted and stored. Daemon log shows
`timeout_extract` signal processed.

---

## Gateway

The OC gateway must be running before any OC agent interaction. Check and restart:
```bash
ssh REMOTE_HOST 'curl -sf http://localhost:18789/health && echo "ok" || echo "down"'
ssh REMOTE_HOST 'pkill -f openclaw-gateway; sleep 2; \
  nohup openclaw gateway > /tmp/oc-gw.log 2>&1 &'
```

---

## Database and CLI

```bash
# DB (shared across OC instances)
ssh REMOTE_HOST 'sqlite3 WORKSPACE/data/memory.db "SELECT COUNT(*) FROM nodes;"'
# OR instance-local:
ssh REMOTE_HOST 'sqlite3 WORKSPACE/OC_INSTANCE/data/memory.db "SELECT COUNT(*) FROM nodes;"'

# CLI
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=OC_INSTANCE \
  ~/.openclaw/extensions/quaid/quaid recall "query" 2>&1'
```

---

## Milestone Notes

### M1 — Extraction via `/new`
Apply the version quirk above. Check hook trace for the correct marker for the
installed OC version. FTS direct check is the primary verification — use
`sqlite3 ... nodes_fts` rather than `quaid recall` for exact keyword lookup.

### M4 — Timeout Extraction and Compaction
OC is the only platform with both. See dedicated section above.

### M7 Phase 3 — Multi-hop Graph Traversal
Owner entity in sibling edges must be the actual owner name (e.g. "Solomon"),
not "User" or "User's mom". First-person entity resolution is injection-based.
If sibling edge anchors to wrong entity, delete nodes and re-seed in a fresh
session — do not retry within the same session.

### M12 — Multi-Agent Silo Verification
OC-only milestone. Tests that each OC agent instance has its own silo with
correct signal routing. Follow the guide exactly.
