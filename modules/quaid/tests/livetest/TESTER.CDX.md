# Tester Supplement — Codex (CDX)

Platform-specific notes for the CDX tester. Read this alongside `TESTER.SKILL.md`.

---

## Lane variables

Milestone files reference these; export them once at session start:

```bash
export LANE=cdx
export LANE_UPPER=CDX
export INSTANCE=codex-private-tmp-cdx-livetest
export QCLI=~/.quaid/plugins/quaid/quaid
export SILO=~/.quaid/instances/codex-private-tmp-cdx-livetest
export LIFECYCLE="/new"  # CDX has no /clear or /compact hook
```

`SEND` mechanism: write directly into the CDX tmux pane with
`tmux send-keys -t livetest:CDX.1 "<text>" Enter`.

**CDX extraction window:** CDX extracts via rollout + session-transition
hooks after the next lifecycle signal; wait ~2 min after `/new` before
checking the DB.

**CDX hook trace markers:** look for `hook.session.transition` on `/new` in
`$SILO/logs/daemon/extraction-daemon.log`.

**M7 (System Context Refresh) variant:** CDX has no compaction hook. M7's
refresh trigger on CDX is `/new` instead of `/compact`. Use the same
canary-append procedure, just fire `/new` where the milestone says to fire
the refresh trigger.

---

## Launch

After M0 install, start the CDX interaction pane:

```bash
tmux respawn-pane -k -t livetest:CDX.1 'zsh -il'
tmux send-keys -t livetest:CDX.1 "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CDX.1 "mkdir -p /tmp/cdx-livetest && cd /tmp/cdx-livetest && QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE codex --yolo" Enter
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

**MANDATORY — verify effective models before any test messages:**
CDX may define models in the instance, platform, or global config layer. Do not
patch tiers mid-run. Verify the effective layered values are sane before sending
any milestone prompts:
```bash
ssh REMOTE_HOST 'python3 -c "import json, pathlib
home = pathlib.Path(\"WORKSPACE\")
paths = [
  home / \"shared/config/global/config.json\",
  home / \"shared/config/codex/config.json\",
  home / \"instances/CDX_INSTANCE/config.json\",
]
models = {}
for p in paths:
    if p.exists():
        d = json.loads(p.read_text())
        if isinstance(d.get(\"models\"), dict):
            models.update(d[\"models\"])
print(\"provider:\", models.get(\"llmProvider\"))
print(\"fast:\", models.get(\"fastReasoning\"))
print(\"deep:\", models.get(\"deepReasoning\"))
assert \"invalid-model\" not in json.dumps(models), models
assert models.get(\"fastReasoning\") in (\"gpt-5.4-mini\", \"claude-haiku-4-5\"), models
assert models.get(\"deepReasoning\") in (\"gpt-5.4\", \"claude-sonnet-4-5\"), models"'
```

---

## Sending Messages

The platform (Codex on the remote VM) runs in the **CDX.1** pane:

```bash
tmux send-keys -t livetest:CDX.1 "your message" Enter
sleep 10
tmux capture-pane -t livetest:CDX.1 -p | tail -30
```

**Input quirk (common):** Codex often stages text in the input buffer without
submitting it. After every `send-keys`, verify that the message actually submitted
by checking for a model response. If the text is staged but not submitted, send a
bare Enter:

```bash
tmux send-keys -t livetest:CDX.1 "" Enter
```

This must be sent by whoever is driving the pane — the tester agent (from CDX.0),
or the coordinator directly if the tester's delivery is failing.

Do not report a CDX "no reply" product failure until you have confirmed a Codex
session JSONL was created or updated under `~/.codex/sessions`. If the pane shows
the prompt text but no session file changed, the turn was not submitted; send the
bare Enter above and re-check.

Exit CDX with Ctrl+D or `/exit`.

**Always wait for the current turn to fully finish** before sending `/new` —
CDX disables `/new` while a task is still running.

**`/new` rollout verification sequence:** `/new` may display a welcome screen
and resume hint. That is normal and is not evidence of a wedge. The extraction
handoff is only testable after the first real prompt in the new session:

1. Send `/new`.
2. Send the first real prompt for the new session (for neutral transition
   checks, use `Hello`).
3. If the prompt text is staged but not submitted, send the documented bare
   Enter above.
4. Then verify a new rollout file exists under `~/.codex/sessions` and the
   Quaid rolling state/logs advanced.
5. If no new rollout file exists after the follow-up prompt was submitted,
   treat it as a real CDX pane wedge and restart Codex in the pane.

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

**Consequence for lifecycle tests:** after sending `/new`, you must send one
follow-up message in the new session (e.g. `Hello`) to trigger `hook-inject` and fire
`check_session_transition`. Do not just wait — no message means no hook fires
and extraction never starts.

**CDX does NOT have `quaid-hook-trace.jsonl`.** Do not check for this file — it is
OC-native and will always be absent from CDX instance log directories. Checking for
it and treating absence as a failure is a false-negative.

**How to verify CDX hook activity:**
- Check `logs/daemon/rolling-extraction.jsonl` for `rolling_stage` / `rolling_flush` events (primary check)
- Check `logs/daemon/extraction-daemon.log` for session timeout / signal processing lines
- Check `data/extraction-signals/` for pending signal files

**Hook trace marker (daemon log):** Look for session timeout or transition lines in
`extraction-daemon.log` (e.g. `idle for Ns with N unextracted lines, generating timeout signal`).
The marker `hook.inject.session_transition_signal_written` appears in OC hook traces only;
for CDX the equivalent evidence is a rollout file written to `data/rolling-extraction/`.

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
- shared provider credential registry at `WORKSPACE/shared/auth/credentials.json`

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
   ssh REMOTE_HOST 'python3 - <<\"PY\"
import json
from pathlib import Path
p = Path("WORKSPACE/CDX_INSTANCE/config.json")
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault("capture", {})["inactivityTimeoutMinutes"] = 1
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2))
print("CDX_INSTANCE inactivityTimeoutMinutes=1")
PY'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid daemon stop 2>&1; sleep 2; QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid daemon start 2>&1'
   ```
2. Start a fresh visible CDX session, state one memorable fact, then let the
   pane idle for >1 minute without `/new`.
3. Verify extraction fired:
   - daemon log shows timeout handling (`daemon-timeout` or equivalent timeout extraction path)
   - the fact is stored in DB / FTS
4. Restore the timeout and restart the daemon again:
   ```bash
   ssh REMOTE_HOST 'python3 - <<\"PY\"
import json
from pathlib import Path
p = Path("WORKSPACE/CDX_INSTANCE/config.json")
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault("capture", {})["inactivityTimeoutMinutes"] = 60
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2))
print("CDX_INSTANCE inactivityTimeoutMinutes=60")
PY'
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
then send `/new`, then send the first real prompt for the new session. If the
prompt stages without submitting, send the documented bare Enter. Only after that
follow-up prompt has submitted should you verify the rollout file and memory
state. Do not gate on snippet or journal output — that is discretionary and
covered in M11.

### M3 — Rolling Extraction
CDX does not have `/compact` or `/clear`. After seeding and building context, use `/new`
as the extraction trigger. Verify `rolling-extraction.jsonl` has `rolling_stage`
and `rolling_flush` events the same as OC/CC.

### M5 — Silo Isolation Across Sessions/Instances
When M5 asks CDX to end Session X with `LIFECYCLE`, send `/new`, then send the
first real Session Y recall prompt. If the recall prompt is staged but not
submitted, send the documented bare Enter. Verify rollout creation after the
recall prompt has submitted, not from the `/new` welcome screen alone. If the
submitted follow-up prompt still does not create a new rollout file, restart the
Codex pane before retrying that part.

### M7 — System Context Refresh
CDX uses `/new` as the M7 refresh trigger. Send `/new`, then send the canary
question (`What's the office plant named?`) as the first real prompt in the new
session. Apply the same bare-Enter and rollout-file checks above before grading
the answer. A welcome screen or resume hint immediately after `/new` is normal.

### M4 — Timeout Extraction
See dedicated section above. CDX gets timeout extraction but no timeout compaction.

### M8 Phase 1 — Project Auto-Creation
CDX agents generally follow file-placement policy. If Phase 1 fails (agent
writes files without creating a project), report as ISSUE — do not rule
PASS-WITH-NOTE.

### M5 Part A — Multi-Agent Silo Verification
CDX uses `codex-private-tmp-cdx-livetest` as the instance ID (set via `QUAID_INSTANCE`).
Runtime silo is at `~/.quaid/instances/codex-private-tmp-cdx-livetest/` (hidden).
Follow the CDX Part A procedure in `livetest-guide/M5.md`. Never SKIP — all three
platforms run M5.

### M5 Part B — Multi-Instance Verification
CDX has no `make_instance` — isolation is verified by canary test between two
`QUAID_INSTANCE` values (`codex-private-tmp-cdx-livetest` and `codex-m13test`).
Follow the CDX Part B procedure in `livetest-guide/M5.md`. Never SKIP — all
three platforms run M5.

### M16 — System Context Refresh on Lifecycle (CDX uses timeout, NOT /compact)

CDX has no `/compact` hook, so the system-context refresh test fires on the
daemon **idle-timeout** path instead of the compaction path used by OC/CC. This
is a documented compatibility difference (see `docs/COMPATIBILITY.md` →
"System-context refresh trigger").

**Before running M16 on CDX:** confirm `capture.inactivityTimeoutMinutes` is
small for the test (livetest defaults to 1 minute, but post-M4 / post-test
closeout may have restored it to 60). If the value is 60, set it back to 1 and
restart the daemon:

```bash
ssh REMOTE_HOST 'source ~/.zprofile >/dev/null 2>&1; \
  python3 - <<\"PY\"
import json
from pathlib import Path
p = Path.home() / \".quaid\" / \"shared\" / \"config\" / \"global\" / \"config.json\"
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault(\"capture\", {})[\"inactivityTimeoutMinutes\"] = 1
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2))
print(\"capture.inactivityTimeoutMinutes=1 in global config\")
PY
  && QUAID_HOME=$HOME/.quaid QUAID_INSTANCE=codex-private-tmp-cdx-livetest \
  quaid daemon stop && sleep 2 && \
  QUAID_HOME=$HOME/.quaid QUAID_INSTANCE=codex-private-tmp-cdx-livetest \
  quaid daemon start'
```

**CDX M16 procedure:**

1. Start a fresh CDX session and exchange a few normal turns so initial system
   context is loaded.
2. From the coordinator side (do NOT ask the agent to do this), append a unique
   canary to the system-context markdown that the CDX system-context loader
   reads (e.g., `~/.quaid/instances/codex-private-tmp-cdx-livetest/SOUL.md` —
   pick the file that the platform's system-context injection actually
   includes). Same canary text as the main M16 procedure ("The office plant
   is named Bartholomew. It is a fiddle-leaf fig.").
3. **Do NOT send `/compact`.** Instead, leave the pane idle for >1 minute so
   the daemon idle-timeout fires.
4. After the timeout signal fires (verify in `extraction-daemon.log` for
   `daemon-timeout` line), send a fresh CDX prompt that depends on the canary:
   `What's the office plant named?`
5. Verify the agent answers `Bartholomew` from the refreshed context.

**Restore:** after PASS, set `capture.inactivityTimeoutMinutes` back to 60 if
that was the closeout value, and remove the canary lines from the identity
file.

**Pass:** agent answers from the refreshed system context after timeout.
**Fail:** agent has no knowledge of the canary OR retrieves it via a memory
recall path (means system context did NOT refresh on the timeout boundary).
