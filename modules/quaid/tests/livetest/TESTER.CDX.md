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

**MANDATORY — always launch Codex in a FRESH process after Quaid install.**
The Quaid installer writes `~/.codex/hooks.json` and `~/.codex/config.toml`,
but Codex does not hot-reload those files. If Codex was already running when
the installer ran (e.g. the install agent was itself a Codex session), the
pre-existing process will NOT pick up the new hooks until you start a new
Codex process. A fresh `codex --yolo` launch after M0 finishes is the correct
sequence. If you re-use a pre-install Codex session for M1 seeding,
`hook-inject` will not fire, the seed will not produce a session transition
signal, and extraction will silently skip the turn.

**MANDATORY — verify model before any test messages:**
CDX should use installer defaults: fast=`gpt-5.4-mini`, deep=`gpt-5.4`. Do not
patch tiers mid-run. Verify from config before sending any milestone prompts:
```bash
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"WORKSPACE/instances/CDX_INSTANCE/config.json\")); \
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

**Always wait for the current turn to fully finish** before sending `/new` —
CDX disables `/new` while a task is still running.

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

---

## QUIRK: Stop Hook Returns None — check_session_transition Is the Extraction Path

**Do NOT fix `resolve_stop_hook_signal()` to always return a signal. This is intentional.**

The CDX Stop hook (`hook_codex_stop`) only writes a `session_end` signal when
it detects a lifecycle command (`/new`, `/restart`) in the
transcript. On a plain task completion (no lifecycle command), it returns `None`
and writes nothing. This is correct behavior.

**Why detection fails for `/new`:** CDX CLI intercepts lifecycle
commands before the Stop hook fires and strips them from the transcript. The
hook's transcript scan finds nothing and returns `None`.

**The intended extraction path is `check_session_transition`:** in Codex
0.118.0+, `/new` creates a new in-process thread (new `session_id`) without
restarting the process. `SessionStart` does not fire. Instead, `hook-inject`
(UserPromptSubmit) calls `adapter.check_session_transition()` on every message.
When the session_id changes (i.e., the first message arrives in the new thread),
the adapter writes a `session_end` signal for the session that just ended.

**Consequence for M1/M3:** after sending `/new`, you must send one follow-up
message in the new session (e.g. `Hello`) to trigger `hook-inject` and fire
`check_session_transition`. Do not just wait — no message means no hook fires
and extraction never starts.

**Hook trace marker:** `hook.inject.session_transition_signal_written` (not
`hook.codex.session_init.orphan_swept` — orphan sweep is removed).

CDX does not use `SessionTimeoutManager`, but the daemon still honors
`capture.inactivityTimeoutMinutes` through its idle-session timeout path.
That means CDX gets **timeout extraction** but **not timeout compaction**
(see M4 below).

---

## Session Commands

| Command | CDX equivalent | Notes |
|---------|---------------|-------|
| `/new` | `/new` | Primary extraction trigger (starts fresh session) |
| `/clear` | `/new` | CDX has no `/clear` — use `/new` instead |
| `/compact` | `/new` | No timeout compaction on CDX |

---

## Runtime Architecture

CDX now uses **direct provider auth** for Quaid service calls.
There is no Quaid-managed Codex app-server or shared broker in the active path.

Instance isolation comes from:
- per-instance `QUAID_INSTANCE`
- per-project `CODEX_PROJECT_DIR`
- per-lane auth token file at `WORKSPACE/adaptors/codex/.auth-token`

If CDX turns hang, investigate the configured provider/token path and the daemon,
not a Codex app-server sidecar.

---

## M4 — Timeout Extraction

**CDX does have timeout extraction, but not timeout compaction.**

CDX idle extraction comes from the daemon's timeout check, not from a Codex
session-timeout manager. So M4 still applies to CDX, but the expected signal is
**extraction only**.

**CDX M4 procedure:**
1. Set `capture.inactivityTimeoutMinutes` to `1` and restart the CDX daemon:
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid config set capture.inactivityTimeoutMinutes 1'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid daemon start 2>&1'
   ```
2. Start a fresh visible CDX session, state one memorable fact, then let the
   pane idle for >1 minute without `/new`.
3. Verify extraction fired:
   - daemon log shows timeout handling (`daemon-timeout` or equivalent timeout extraction path)
   - the fact is stored in DB / FTS
4. Restore the timeout and restart the daemon again:
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid config set capture.inactivityTimeoutMinutes 60'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid daemon start 2>&1'
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

The most common cause is a stale `~/.codex/hooks.json` on REMOTE_HOST that survived
the Step 0 wipe. The coordinator clears it with:
```bash
ssh REMOTE_HOST 'echo "{}" > ~/.codex/hooks.json && echo "CDX hooks cleared"'
```
After clearing, restart the CDX platform agent session (`/new` or restart
codex) so it picks up the empty hooks file before retrying the install prompt.

---

## Database and CLI

```bash
# DB
ssh REMOTE_HOST 'sqlite3 WORKSPACE/instances/CDX_INSTANCE/data/memory.db "SELECT COUNT(*) FROM nodes;"'

# CLI
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE \
  ~/.quaid/plugins/quaid/quaid recall "query" 2>&1'
```

---

## Milestone Notes

### M2 — Extraction via `/new`
Use `/new` (CDX has no `/clear`). Wait for the memorable turn to fully finish,
then send `/new`; `/new` is the extraction trigger for this milestone. Verify the
fact is stored after the session boundary. Do not gate on snippet or journal
output — that is discretionary and covered in M11.

### M3 — Rolling Extraction
CDX does not have `/compact` or `/clear`. After seeding and building context, use `/new`
as the extraction trigger. Verify `rolling-extraction.jsonl` has `rolling_stage`
and `rolling_flush` events the same as OC/CC.

### M4 — Timeout Extraction
See dedicated section above. CDX gets timeout extraction but no timeout compaction.

### M8 Phase 1 — Project Auto-Creation
CDX agents generally follow file-placement policy. If Phase 1 fails (agent
writes files without creating a project), report as ISSUE — do not rule
PASS-WITH-NOTE.

### M12 — Multi-Agent Silo Verification
CDX uses `codex-private-tmp-cdx-livetest` as the instance ID (set via `QUAID_INSTANCE`).
Runtime silo is at `~/.quaid/instances/codex-private-tmp-cdx-livetest/` (hidden).
Follow the CDX M12 procedure in LIVE-TEST-GUIDE.md. Never SKIP — all three platforms run M12.

### M13 — Multi-Instance Verification
CDX has no `make_instance` — isolation is verified by canary test between two
`QUAID_INSTANCE` values (`codex-private-tmp-cdx-livetest` and `codex-m13test`). Follow the CDX
M13 procedure in LIVE-TEST-GUIDE.md. Never SKIP — all three platforms run M13.
