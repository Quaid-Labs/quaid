# Tester Supplement — OpenClaw (OC)

Platform-specific notes for the OC tester. Read this alongside `TESTER.SKILL.md`.

OC is tested via **Matrix DM**, not the TUI. The Matrix server and OpenClaw
gateway are already running on the VM as persistent services — no launch step
needed. All OC messages are sent as Matrix DMs from the test bot to the OC bot.

---

## Setup Verification

After M0, confirm both services are running:

```bash
ssh REMOTE_HOST 'launchctl list | grep -E "matrix-synapse|openclaw.gateway"'
# expect: ai.quaid.matrix-synapse and ai.openclaw.gateway both listed

ssh REMOTE_HOST 'curl -sf http://127.0.0.1:8008/_matrix/client/versions > /dev/null && echo "matrix ok" || echo "matrix down"'
ssh REMOTE_HOST 'curl -sf http://localhost:18789/health && echo "gateway ok" || echo "gateway down"'
```

---

## Sending Messages

All OC messages are sent via the canonical synced helper on the VM:

```bash
# Send a message to OC
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "your message here"'

# Send a lifecycle command (new session, reset)
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/new"'
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/reset"'
```

Config is read from env or `.matrix-config` next to the helper. For VM compatibility,
the helper also falls back to `~/quaidcode/util/scripts/.matrix-config` if present:
- Homeserver: `http://127.0.0.1:8008`
- Room: `!fDTDMrzcdmaVKRnhxu:localhost`
- OC bot: `@openclaw-bot:localhost`

---

## Reading Replies

OC replies appear as `@openclaw-bot:localhost` messages in the Matrix room.
Read recent messages:

```bash
ssh REMOTE_HOST 'python3 - <<'"'"'PY'"'"'
import json, urllib.request, urllib.parse
TOKEN = "syt_cXVhaWQtdGVzdC1ib3Q_KDaHeayREqwbFoEiYSzB_0aLxMl"
ROOM  = "!fDTDMrzcdmaVKRnhxu:localhost"
room_enc = urllib.parse.quote(ROOM, safe="")
req = urllib.request.Request(
    f"http://127.0.0.1:8008/_matrix/client/v3/rooms/{room_enc}/messages?dir=b&limit=20",
    headers={"Authorization": f"Bearer {TOKEN}"}
)
with urllib.request.urlopen(req) as r:
    data = json.load(r)
for ev in reversed(data.get("chunk", [])):
    if ev.get("type") == "m.room.message":
        sender = ev.get("sender", "?")
        body = ev.get("content", {}).get("body", "")
        if body:
            print(f"{sender}: {body[:300]}")
PY
'
```

Wait for OC's response before proceeding to the next step. A typical response
arrives within 5–15 seconds of sending.

---

## Extraction Triggers

| Trigger | How | Notes |
|---------|-----|-------|
| New session | `matrix-send "/new"` | Routes through `handleSlashLifecycleFromMessage` — direct session_end signal |
| Timeout | inactivity > `capture.inactivityTimeoutMinutes` | Daemon-timeout signal |
| Rolling | session crosses `capture.chunk_tokens` threshold | Daemon polls automatically |

**Matrix `/new` goes through the same code path as Telegram `/new`** — it calls
`handleSlashLifecycleFromMessage` which writes the session_end signal directly.
This is more reliable than the TUI `/new` which relied on `sessions.json` change
detection.

After `/new`, send one follow-up message (e.g. `Hello`) so OC processes the
new-session handshake. Then wait **30–60 seconds** before checking the DB.

**Do NOT use `/reset` for extraction.** `/reset` truncates the transcript
before the daemon can read it. Use `/new`.

**Do NOT use tg-extract or any manual signal injection.** These bypass the
feature under test and poison reset-dedupe markers.

---

## Gateway

Check and restart the OC gateway:
```bash
ssh REMOTE_HOST 'curl -sf http://localhost:18789/health && echo "ok" || echo "down"'
ssh REMOTE_HOST 'pkill -f openclaw-gateway; sleep 2; \
  nohup openclaw gateway > /tmp/oc-gw.log 2>&1 &'
```

---

## Database and CLI

```bash
# Instance DB
ssh REMOTE_HOST 'sqlite3 ~/.quaid/instances/OC_INSTANCE/data/memory.db "SELECT COUNT(*) FROM nodes;"'

# FTS keyword check
ssh REMOTE_HOST 'sqlite3 ~/.quaid/instances/OC_INSTANCE/data/memory.db \
  "SELECT rowid, name FROM nodes_fts WHERE nodes_fts MATCH \"keyword\" LIMIT 10;"'

# CLI recall
ssh REMOTE_HOST 'QUAID_HOME=~/.quaid QUAID_INSTANCE=OC_INSTANCE \
  ~/.quaid/plugins/quaid/quaid recall "query" 2>&1'
```

---

## Milestone Notes

### M2 — Snippets and Persona

**Snippet path is in visible home, not hidden home.**
After extraction, snippets are written to:
```
~/quaid/instances/openclaw-main/USER.snippets.md
~/quaid/instances/openclaw-main/SOUL.snippets.md
```
Do NOT search `~/.quaid/` for snippet files — they will not be there.

Verify:
```bash
ssh REMOTE_HOST 'ls ~/quaid/instances/openclaw-main/*.snippets.md 2>/dev/null && \
  head -20 ~/quaid/instances/openclaw-main/USER.snippets.md'
```

**Wait for the full extraction window before checking.** Sessions with
conversation history can take 2–3 minutes to flush. Confirm flush
completed by checking `rolling-extraction.jsonl` for a `rolling_flush`
entry with nonzero `final_facts_stored` (see TESTER.SKILL.md extraction
wait section) before ruling FAIL on missing facts or snippets.

### M0 — Install
If the installer fails at model selection with a "gateway model rejected" or
"PING failed" error, the OC gateway does not have that model registered. Report
to coordinator with the exact model name that was rejected — do not retry the
install. The coordinator must resolve the gateway model configuration first.

After M0, post-M0 config (chunk_tokens, models) is applied to the OC instance
just as for CC and CDX. See `LIVE-TEST-GUIDE.md` post-M0 steps.

### M1 — Extraction via `/new`
Send a message to seed a memorable fact, build 2–3 exchanges, then:
```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/new"'
# Wait 3–5 seconds for OC to process the lifecycle command
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "Hello"'
```
Wait 60s, then verify via FTS direct check — use `sqlite3 ... nodes_fts` rather
than `quaid recall` for exact keyword lookup. Use `rowid` not `id` as the column.

### M4 — Timeout Extraction and Compaction

OC is the only platform with both. Procedure:

1. Set timeout to 1 minute and restart the gateway:
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=~/.quaid QUAID_INSTANCE=OC_INSTANCE \
     ~/.quaid/plugins/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
   ssh REMOTE_HOST 'pkill -f openclaw-gateway; sleep 2; \
     nohup openclaw gateway > /tmp/oc-gw.log 2>&1 &'
   ssh REMOTE_HOST 'for i in $(seq 1 30); do \
     curl -sf http://localhost:18789/health > /dev/null 2>&1 && echo "Gateway ready" && break \
     || sleep 2; done'
   ```

2. Send a memorable fact via Matrix, then let it idle for >1 minute.

3. Verify timeout extraction fired:
   ```bash
   ssh REMOTE_HOST 'grep -i "timeout\|timeout_extract\|daemon-compaction" \
     ~/.quaid/instances/OC_INSTANCE/logs/daemon.log 2>/dev/null | tail -5'
   ```

4. Restore and restart:
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=~/.quaid QUAID_INSTANCE=OC_INSTANCE \
     ~/.quaid/plugins/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
   # Restart gateway again.
   ```

**M4 PASS criteria (OC):** Timeout fact extracted and stored. Daemon log shows
`timeout_extract` signal processed.

### M7 Phase 3 — Multi-hop Graph Traversal
Owner entity in sibling edges must be the actual owner name (e.g. "Solomon"),
not "User" or "User's mom". First-person entity resolution is injection-based.
If sibling edge anchors to wrong entity, delete nodes and re-seed in a fresh
Matrix session — do not retry within the same session.

### M12 — Multi-Agent Silo Verification
Tests that each OC agent instance has its own silo with correct signal
routing. Follow the guide exactly.

### M13 — Multi-Instance Creation
OC creates new instances via the native agent system, not the installer:

```bash
ssh REMOTE_HOST 'source ~/.zprofile; openclaw agents add --help'
# Use openclaw agents add to create a test agent (e.g. m13test)
ssh REMOTE_HOST 'mkdir -p /tmp/oc-m13-workspace && source ~/.zprofile; openclaw agents add m13test --non-interactive --workspace /tmp/oc-m13-workspace'
```

Safety: never use `~/quaid` as the M13 test workspace. `openclaw agents delete`
prunes the agent workspace, so pointing at `~/quaid` can trash the visible Quaid home.

When OC creates a new agent, Quaid's adapter should detect it and
auto-create the instance silo. Verify:
1. New silo exists at `~/.quaid/instances/openclaw-m13test/`
2. Visible instance at `~/quaid/instances/openclaw-m13test/`
3. Store a canary fact via the new agent, verify it does NOT appear
   from the livetest instance

After the test, clean up:
```bash
ssh REMOTE_HOST 'source ~/.zprofile; openclaw agents delete m13test --force'
ssh REMOTE_HOST 'trash /tmp/oc-m13-workspace 2>/dev/null || rm -rf /tmp/oc-m13-workspace'
```
Note: `--force` is required in non-interactive (SSH) context. If that still fails,
manually remove `~/.openclaw/agents/m13test` and the Quaid silo at
`~/.quaid/instances/openclaw-m13test/`.

Do NOT re-run the installer for M13 — that overwrites the gateway
config and disrupts the active livetest instance.
