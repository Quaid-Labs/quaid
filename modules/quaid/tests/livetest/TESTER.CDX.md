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
CDX must use `gpt-5.1-codex-mini` at Medium effort. Both fast and deep lanes
are set to this model (deep lane is overwritten with the fast lane value — same
rule as OC/CC). Verify from config before sending any milestone prompts:
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

## Extraction Model: Turn-Driven Only

CDX extraction is **entirely synchronous and turn-driven**. There is no daemon,
no background process, and no idle timeout mechanism.

- The `hook_codex_stop` hook fires after **every turn** and writes extracted
  facts immediately.
- Facts are available in the DB right after the turn completes — no 30–60s
  wait needed.
- CDX has no `SessionTimeoutManager` integration and no
  `capture.inactivityTimeoutMinutes` effect.

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

## M4 — Timeout Extraction: SKIP

**CDX does not have timeout extraction.** M4 is not applicable to CDX.

CDX extraction fires on every turn via `hook_codex_stop`. There is no idle
session monitor and setting `capture.inactivityTimeoutMinutes` has no effect
on CDX.

**CDX M4 action:** Skip M4 entirely. Send STATUS to coordinator:
`"STATUS: M4 SKIP — CDX has no timeout mechanism, extraction is turn-driven."`

If you want to verify per-turn extraction completeness as a substitute, confirm
after M1/M2 that facts from each individual turn appear in the DB promptly.
That is sufficient coverage for the CDX extraction path.

---

## SessionStart Hook — First Session Cold Start

On a fresh CDX silo, the first `codex --yolo` session may block for several
minutes while `quaid hook-session-init` runs 5 sequential LLM calls to
initialise context. This is expected on first launch only.

If the coordinator ran a post-install warm-up step (pre-running
`hook-session-init` with `QUAID_INSTALL_AGENT=1`), this delay should not occur.
If it does block: wait up to 15 minutes before reporting as an ISSUE.

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
Use `/clear` (not `/reset`). Verify the pre-clear session is extracted. Do not
gate on snippet or journal output — that is discretionary and covered in M11.

### M3 — Rolling Extraction
CDX has no extraction daemon, so there is no `rolling-extraction.jsonl` and no
`rolling_stage` / `rolling_flush` events. CDX extraction is entirely turn-driven
via `hook_codex_stop`.

**CDX M3 pass criterion:** After seeding a multi-turn context (5+ turns), send
`/clear`. Verify that the node count in the DB increased since before the session
began. If nodes were added, M3 PASS — per-turn extraction is the CDX equivalent
of rolling extraction.

Do NOT gate on `rolling-extraction.jsonl` for CDX — that file will never exist.

### M4 — Timeout Extraction
**SKIP.** See dedicated section above.

### M8 Phase 1 — Project Auto-Creation
CDX agents generally follow file-placement policy. If Phase 1 fails (agent
writes files without creating a project), report as ISSUE — do not rule
PASS-WITH-NOTE.
