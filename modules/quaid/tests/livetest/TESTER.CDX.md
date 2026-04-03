# Tester Supplement — Codex (CDX)

Platform-specific notes for the CDX tester. Read this alongside `TESTER.SKILL.md`.

---

## Launch

After M0 install, start the CDX interaction pane:

```bash
tmux respawn-pane -k -t livetest:CDX 'zsh -il'
tmux send-keys -t livetest:CDX "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CDX "mkdir -p /tmp/cdx-livetest && cd /tmp/cdx-livetest && QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE codex --yolo" Enter
```

**MANDATORY — verify model before any test messages:**
CDX must use `gpt-5.4-mini` at Medium effort (with `reasoning_effort=none` for
fast lane — this model accepts `effort=none`, unlike `gpt-5.1-codex-mini`).
Both fast and deep lanes are set to this model (deep lane is overwritten with
the fast lane value — same rule as OC/CC). Verify from config before sending
any milestone prompts:
```bash
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"WORKSPACE/CDX_INSTANCE/config/memory.json\")); \
  print(\"fast:\", d[\"models\"][\"fastReasoning\"]); print(\"deep:\", d[\"models\"][\"deepReasoning\"])"'
```

---

## Sending Messages

```bash
tmux send-keys -t livetest:CDX "your message" Enter
sleep 10
tmux capture-pane -t livetest:CDX -p | tail -30
```

**Input quirk:** if text lands in the buffer without submitting, send a bare Enter:
```bash
tmux send-keys -t livetest:CDX "" Enter
```

Exit CDX with Ctrl+D or `/exit`.

**Always wait for the current turn to fully finish** before sending `/clear` —
CDX disables `/clear` while a task is still running.

---

## Extraction Model: Signal-Only Hook + Daemon

CDX extraction is daemon-driven.

- **Stop hook (signal only):** `hook_codex_stop` runs after every turn, but it
  should only write an extraction signal and wake the daemon. It does not own
  extraction or direct memory writes.
- **Daemon extraction:** The extraction daemon consumes those signals and owns
  rolling extraction, lifecycle flush, and publish. `rolling_stage` and
  `rolling_flush` events are written to `logs/daemon/rolling-extraction.jsonl`,
  same as OC/CC.

CDX does not use `SessionTimeoutManager`, but the daemon still honors
`capture.inactivityTimeoutMinutes` through its idle-session timeout path.
That means CDX gets **timeout extraction** but **not timeout compaction**
(see M4 below).

---

## Session Commands

| Command | CDX equivalent | Notes |
|---------|---------------|-------|
| `/reset` | `/clear` | **Never send `/reset` to CDX** |
| `/compact` | `/clear` | No timeout compaction |
| `/new` | `/new` | Starts a fresh session |

**Never send `/reset` to CDX.** CDX uses `/clear` for extraction triggers.

---

## App-Server Architecture

CDX uses a **single shared app-server broker** per Quaid home — one
`codex app-server` process serves all CDX instances:
- Broker socket: `WORKSPACE/shared/run/codex-app-server-broker.sock`
- Broker PID: `WORKSPACE/shared/run/codex-app-server-broker.pid`

Instance isolation is maintained per-turn via the `cwd` parameter in each
request. Memory hooks receive `QUAID_INSTANCE` from their registered env vars,
not from the app-server process.

If the broker is not running, CDX will start it automatically on first use.
If turns hang, check whether the broker process is healthy:
```bash
ssh REMOTE_HOST 'cat WORKSPACE/shared/run/codex-app-server-broker.pid 2>/dev/null \
  | xargs -I{} kill -0 {} 2>/dev/null && echo "broker running" || echo "broker not running"'
```

---

## M4 — Timeout Extraction

**CDX does have timeout extraction, but not timeout compaction.**

CDX idle extraction comes from the daemon's timeout check, not from a Codex
session-timeout manager. So M4 still applies to CDX, but the expected signal is
**extraction only**.

**CDX M4 procedure:**
1. Set `capture.inactivityTimeoutMinutes` to `1` and restart the CDX daemon:
   ```bash
   ssh REMOTE_HOST 'cd WORKSPACE && QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
   ssh REMOTE_HOST 'cd WORKSPACE && QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.openclaw/extensions/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'
   ```
2. Start a fresh visible CDX session, state one memorable fact, then let the
   pane idle for >1 minute without `/new` or `/clear`.
3. Verify extraction fired:
   - daemon log shows timeout handling (`daemon-timeout` or equivalent timeout extraction path)
   - the fact is stored in DB / FTS
4. Restore the timeout and restart the daemon again:
   ```bash
   ssh REMOTE_HOST 'cd WORKSPACE && QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.openclaw/extensions/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
   ssh REMOTE_HOST 'cd WORKSPACE && QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.openclaw/extensions/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.openclaw/extensions/quaid/quaid daemon start 2>&1'
   ```

**M4 PASS criteria (CDX):** Timeout fact extracted and stored with no explicit
lifecycle command. Note in STATUS: `"M4 PASS — timeout extraction verified (no compaction, expected for CDX)."`

---

## SessionStart Hook — First Session Cold Start

This must **not** appear on the first M0 install turn. Before Quaid is
installed there should be no Quaid Codex hooks at all.

If the first install prompt shows `SessionStart hook: Quaid loading project
context`, the environment is contaminated by a prior install or an incomplete
wipe. Report an ISSUE immediately. Do not wait for the hook to finish and do
not treat it as expected cold start behavior for M0.

The most common cause is a stale `~/.codex/hooks.json` on alfie that survived
the Step 0 wipe. The coordinator clears it with:
```bash
ssh alfie.local 'echo "{}" > ~/.codex/hooks.json && echo "CDX hooks cleared"'
```
After clearing, restart the CDX platform agent session (`/new` or restart
codex) so it picks up the empty hooks file before retrying the install prompt.

---

## Database and CLI

```bash
# DB
ssh REMOTE_HOST 'sqlite3 WORKSPACE/CDX_INSTANCE/data/memory.db "SELECT COUNT(*) FROM nodes;"'

# CLI
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE \
  WORKSPACE/plugins/quaid/quaid recall "query" 2>&1'
```

---

## Milestone Notes

### M2 — Extraction via `/clear`
Use `/clear` (not `/reset`). Wait for the memorable turn to fully finish, then
send `/clear`; `/clear` is the extraction trigger for this milestone. Verify the
fact is stored after the clear boundary. Do not gate on snippet or journal
output — that is discretionary and covered in M11.

### M3 — Rolling Extraction
CDX does not have `/compact`. After seeding and building context, use `/clear`
as the extraction trigger. Verify `rolling-extraction.jsonl` has `rolling_stage`
and `rolling_flush` events the same as OC/CC.

### M4 — Timeout Extraction
See dedicated section above. CDX gets timeout extraction but no timeout compaction.

### M8 Phase 1 — Project Auto-Creation
CDX agents generally follow file-placement policy. If Phase 1 fails (agent
writes files without creating a project), report as ISSUE — do not rule
PASS-WITH-NOTE.
