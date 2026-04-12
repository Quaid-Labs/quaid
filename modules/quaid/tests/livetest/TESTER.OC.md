# Tester Supplement — OpenClaw (OC)

Platform-specific notes for the OC tester. Read this alongside `TESTER.SKILL.md`.

---

## Launch

After M0 install, start the OC TUI in the platform pane:

```bash
tmux send-keys -t livetest:OC.1 "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:OC.1 "source ~/.zprofile; openclaw tui" Enter
```

Verify gateway health:
```bash
ssh REMOTE_HOST 'curl -sf http://localhost:18789/health && echo "ok" || echo "down"'
```

OC interaction happens through the TUI. Messages are sent via tmux send-keys
to the TUI pane. Replies appear in the TUI output.

---

## Sending Messages

Send all OC messages via the TUI (`openclaw tui`) in the platform pane:

```bash
tmux send-keys -t livetest:OC.1 "your message" Enter
tmux send-keys -t livetest:OC.1 "/new" Enter
tmux send-keys -t livetest:OC.1 "/reset" Enter
```

Read replies with:
```bash
tmux capture-pane -t livetest:OC.1 -p | tail -30
```

OC lifecycle commands: `/new` (new session) and `/reset` (reset current session).
OC does NOT have `/clear` or `/compact`.

---

## Extraction Triggers

| Trigger | How | Notes |
|---------|-----|-------|
| New session | `/new` | Creates new session key; adapter detects and signals old session |
| Timeout | inactivity > `capture.inactivityTimeoutMinutes` | Daemon-timeout signal |
| Rolling | session crosses `capture.chunk_tokens` threshold | Daemon polls automatically |

**Do NOT use `/reset` for extraction.** `/reset` truncates the session
transcript before the daemon can read it, destroying all conversation content.
Use `/new` instead — it creates a new session without wiping the old one.

After `/new`, send one follow-up message in the new session to ensure the
session key is written to sessions.json. The adapter detects the new key and
signals extraction for the old session.

After any extraction trigger, wait **30–60 seconds** before checking the DB.

**Do NOT use tg-extract or any manual signal injection.** These bypass the
feature under test and poison reset-dedupe markers.

---

## Lifecycle Command Note

Lifecycle commands are sent as Telegram messages; OC handles them via `handleSlashLifecycleFromMessage`.

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
# Instance-local:
ssh REMOTE_HOST 'sqlite3 WORKSPACE/instances/OC_INSTANCE/data/memory.db "SELECT COUNT(*) FROM nodes;"'

# CLI
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=OC_INSTANCE \
  ~/.openclaw/extensions/quaid/quaid recall "query" 2>&1'
```

---

## Milestone Notes

### M0 — Install
If the installer fails at model selection with a "gateway model rejected" or
"PING failed" error, the OC gateway does not have that model registered. Report
to coordinator with the exact model name that was rejected — do not retry the
install. The coordinator must resolve the gateway model configuration first.

### M1 — Extraction via `/new`
Send `/new` in the TUI, then send a follow-up message (e.g. `Hello`) in the
new session so the session key is written to sessions.json. The adapter
detects the new key and signals extraction for the old session. Wait 60s,
then verify via FTS direct check — use `sqlite3 ... nodes_fts` rather than
`quaid recall` for exact keyword lookup.

### M4 — Timeout Extraction and Compaction
OC is the only platform with both. See dedicated section above.

### M7 Phase 3 — Multi-hop Graph Traversal
Owner entity in sibling edges must be the actual owner name (e.g. "Solomon"),
not "User" or "User's mom". First-person entity resolution is injection-based.
If sibling edge anchors to wrong entity, delete nodes and re-seed in a fresh
session — do not retry within the same session.

### M12 — Multi-Agent Silo Verification
Tests that each OC agent instance has its own silo with correct signal
routing. Follow the guide exactly.

### M13 — Multi-Instance Creation
OC creates new instances via the native agent system, not the installer:

```bash
ssh REMOTE_HOST 'source ~/.zprofile; openclaw agents add --help'
# Use openclaw agents add to create a test agent (e.g. m13test)
```

When OC creates a new agent, Quaid's adapter should detect it and
auto-create the instance silo. Verify:
1. New silo exists at `~/.quaid/instances/openclaw-m13test/`
2. Visible instance at `~/quaid/instances/openclaw-m13test/`
3. Store a canary fact via the new agent, verify it does NOT appear
   from the livetest instance

After the test, clean up:
```bash
ssh REMOTE_HOST 'source ~/.zprofile; openclaw agents delete m13test --force'
```
Note: `--force` is required in non-interactive (SSH) context. If that still fails,
manually remove `~/.openclaw/agents/m13test` and the Quaid silo at
`~/.quaid/instances/openclaw-m13test/`.

Do NOT re-run the installer for M13 — that overwrites the gateway
config and disrupts the active livetest instance.
