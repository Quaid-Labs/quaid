# Tester Supplement — OpenClaw (OC)

Platform-specific notes for the OC tester. Read this alongside `TESTER.SKILL.md`.

---

## Launch

After M0 install, keep the OC gateway running on the test VM and start the Telegram poller locally for inbound OC replies:

```bash
ssh REMOTE_HOST 'curl -sf http://localhost:18789/health && echo "ok" || echo "down"'
nohup ~/quaidcode/util/scripts/tg-poll --config ~/quaidcode/util/scripts/.tg-livetest-config --filter-from Bertrand_clawdbot_bot > /tmp/tg-poll-oc.log 2>&1 &
echo $! > /tmp/tg-poll-oc.pid
```

OC runs headlessly for livetest. No TUI pane is required.

---

## Telegram Setup

Config: `~/quaidcode/util/scripts/.tg-livetest-config`

- Livetester bot: `@Quaid_livetester_34726jfhs_bot`
- OC bot: `@Bertrand_clawdbot_bot`
- Group chat_id: `-5221680718`

Start daemon:
```bash
nohup ~/quaidcode/util/scripts/tg-poll --config ~/quaidcode/util/scripts/.tg-livetest-config --filter-from Bertrand_clawdbot_bot > /tmp/tg-poll-oc.log 2>&1 &
echo $! > /tmp/tg-poll-oc.pid
```

Stop daemon:
```bash
kill $(cat /tmp/tg-poll-oc.pid)
```

Send:
```bash
~/quaidcode/util/scripts/tg --config ~/quaidcode/util/scripts/.tg-livetest-config "message"
```

Receive:
- replies arrive automatically in poller output as tagged lines:
  `[telegram:Livetest] Bertrand_clawdbot_bot: <text>`
- no explicit `tg recv` call is needed.

---

## Sending Messages

Send all OC messages via Telegram:
```bash
~/quaidcode/util/scripts/tg --config ~/quaidcode/util/scripts/.tg-livetest-config "your message"
~/quaidcode/util/scripts/tg --config ~/quaidcode/util/scripts/.tg-livetest-config "/clear"
~/quaidcode/util/scripts/tg --config ~/quaidcode/util/scripts/.tg-livetest-config "/new"
~/quaidcode/util/scripts/tg --config ~/quaidcode/util/scripts/.tg-livetest-config "/compact"
```

Lifecycle commands (`/clear`, `/new`, `/compact`) are sent as plain Telegram messages.

**Avoid apostrophes** in OC messages — use "do not" instead of "don't".

Replies arrive automatically in `tg-poll` stdout tagged as:
`[telegram:Livetest] Bertrand_clawdbot_bot: <text>`

No tmux capture is needed for replies.

---

## Extraction Triggers

| Trigger | How | Notes |
|---------|-----|-------|
| New session | `/new` | Lifecycle note below |
| Session clear | `/clear` | Extracts current session |
| Compaction | `/compact` | Extracts + compacts |
| Timeout | inactivity > `capture.inactivityTimeoutMinutes` | Daemon-compaction signal (source: timeout_extract) |
| Rolling | session crosses `capture.chunk_tokens` threshold | Daemon polls automatically |

After any extraction trigger, wait **30–60 seconds** before checking the DB.

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
# OR instance-local:
ssh REMOTE_HOST 'sqlite3 WORKSPACE/OC_INSTANCE/data/memory.db "SELECT COUNT(*) FROM nodes;"'

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
Send `/new` as a Telegram message (see Lifecycle Command Note above). Check hook
trace for the lifecycle marker. FTS direct check is the primary verification — use
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
