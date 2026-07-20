# Tester Supplement — Codex (CDX)

Platform-specific notes for the CDX tester. Read this alongside `TESTER.SKILL.md`.
Milestone files in `livetest-guide/M*.md` are authoritative; this supplement
only adds lane-specific commands and interpretation. If a supplement conflicts
with a milestone guide, follow the guide and report the drift.

---

## Lane variables

Milestone files reference these; export them once at session start:

```bash
export LANE=cdx
export LANE_UPPER=CDX
export INSTANCE=codex-cdx-livetest-b89008986acd
export QCLI=~/.quaid/plugins/quaid/quaid
export SILO=~/.quaid/instances/codex-cdx-livetest-b89008986acd
export LIFECYCLE="/new"  # CDX has no /clear or /compact hook
```

`SEND` mechanism: send user-visible content into the CDX tmux pane with
`tmux-msg.sh --no-chrome` so no inter-agent prefix is injected:

```bash
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CDX.1 "<text>"
```

Raw `tmux send-keys` remains banned for milestone prompts and recovery turns.

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
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CDX.1 "ssh REMOTE_HOST"
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CDX.1 "mkdir -p /tmp/cdx-livetest && cd /tmp/cdx-livetest && QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE codex --yolo"
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

Preflight also seeds Codex project trust for the configured CDX project path and
its resolved realpath. This matters on macOS where `/tmp/cdx-livetest` resolves
to `/private/tmp/cdx-livetest`; both forms must be trusted before hooks fire.

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
assert models.get(\"llmProvider\") in (\"openai\", \"openai-compatible\"), models
assert models.get(\"fastReasoning\") == \"gpt-5.4-mini\", models
assert models.get(\"deepReasoning\") == \"gpt-5.4\", models"'
```

**CDX M6 Part B provider-error sentinel path:**
Use `~/.quaid/shared/config/codex/config.json` for the invalid model sentinel
described in `livetest-guide/M6.md`. Do **not** write the sentinel to an
instance-local `codex/config.json` under the CDX instance root: that file is not
part of Quaid's active model-resolution chain. The effective CDX chain is:

1. `~/.quaid/shared/config/global/config.json`
2. `~/.quaid/shared/config/codex/config.json`
3. `~/.quaid/instances/CDX_INSTANCE/config.json`

The Part B sentinel must modify the layer that currently provides
`models.fastReasoning` and `models.deepReasoning`; in normal livetest installs,
that is `shared/config/codex/config.json`.

---

## Sending Messages

The platform (Codex on the remote VM) runs in the **CDX.1** pane:

```bash
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CDX.1 "your message"
sleep 10
tmux capture-pane -t livetest:CDX.1 -p | tail -30
```

**Input quirk (common):** Codex often stages text in the input buffer without
submitting it. After every `send-keys`, verify that the message actually submitted
by checking for a model response. If the text is staged but not submitted, send a
bare Enter:

```bash
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CDX.1 ""
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

## XP Project-Link Contract

XP tests two separate behaviors:

- For a one-fact project lookup, the agent should answer without running
  `quaid project link`; direct file read or scoped project recall is acceptable.
- For durable project work, edits, API/tool use, or "start working on this
  project" phrasing, the agent should link the project before proceeding.

Do not mark "no auto-link" as a failure unless the prompt asked for durable
project engagement.

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

**M2 Part B residual timing:** CDX can publish the rolling threshold payload and
the post-`/new` residual payload separately. The first `rolling_flush` after
Chunk 2 may only contain the threshold-crossing facts. For the Chunk-2-only
markers (`Baxter`, `orange linen notebook`, `Emília Rosa`), keep polling until
the test session has a `rolling_flush` row with
`processing_signal_type=session_end`, or until 7 minutes have elapsed after the
follow-up prompt that materialized `/new`. Treat earlier empty DB checks as
"still extracting", not a failure.

For CDX one-shot sessions recovered from a frozen internal cursor, a
subthreshold recovered tail may wait for the rolling internal-cursor grace
window before the daemon writes `internal_cursor_unfrozen_flush`. Do not fail the
lane on an empty DB check before that quiet-window flush has had time to fire.

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
(tested in `livetest-guide/M2.md` Part C).

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

## Timeout Extraction (M2 Part C)

CDX idle extraction comes from the daemon's timeout check, not from a Codex
session-timeout manager. Timeout extraction is tested by `livetest-guide/M2.md`
Part C, not M4. CDX has no timeout compaction.

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
not part of M2 pass criteria unless the guide explicitly asks for it.

### M3 — Recall
Follow `livetest-guide/M3.md`. CDX does not have `/compact` or `/clear`; when
the guide requires a fresh post-lifecycle session before recall, use `/new`,
send the first real follow-up prompt, and apply the bare-Enter guard if the
prompt is staged. Do not grade rolling extraction itself as M3.

### M5 — Silo Isolation Across Sessions/Instances
When M5 asks CDX to end Session X with `LIFECYCLE`, send `/new`, then send the
first real Session Y recall prompt. If the recall prompt is staged but not
submitted, send the documented bare Enter. Verify rollout creation after the
recall prompt has submitted, not from the `/new` welcome screen alone. If the
submitted follow-up prompt still does not create a new rollout file, restart the
Codex pane before retrying that part.

### M7 — System Context Refresh on Lifecycle
CDX uses `/new` as the M7 refresh trigger. Send `/new`, then send the canary
question (`What's the office plant named?`) as the first real prompt in the new
session. Apply the same bare-Enter and rollout-file checks above before grading
the answer. A welcome screen or resume hint immediately after `/new` is normal.

### M4 — Project System and Docs CLI
Follow `livetest-guide/M4.md`. CDX has no lane-specific M4 replacement; do not
run timeout extraction as M4.

### M8 — Temporal Provenance
Follow `livetest-guide/M8.md`. CDX agents still need to follow file-placement
policy during all milestones, but project auto-creation is not the M8 objective
and must not be graded as M8.

### M5 Part A — Silo Isolation: Multi-Agent Silo Verification
CDX uses the path-derived instance ID for `/tmp/cdx-livetest`:
`codex-cdx-livetest-b89008986acd` (set via `QUAID_INSTANCE`). The adapter
derives the same ID after resolving the project dir symlink to `/private/tmp`.
Runtime silo is at `~/.quaid/instances/codex-cdx-livetest-b89008986acd/` (hidden).
Follow the CDX Part A procedure in `livetest-guide/M5.md`. Never SKIP — all three
platforms run M5.

### M5 Part B — Silo Isolation: Multi-Instance Verification
CDX has no `make_instance` — isolation is verified by canary test between two
`QUAID_INSTANCE` values (`codex-cdx-livetest-b89008986acd` and `codex-m13test`).
Follow the CDX Part B procedure in `livetest-guide/M5.md`. Never SKIP — all
three platforms run M5.
