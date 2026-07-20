# Tester Supplement — Claude Code (CC)

Platform-specific notes for the CC tester. Read this alongside `TESTER.SKILL.md`.
Milestone files in `livetest-guide/M*.md` are authoritative; this supplement
only adds lane-specific commands and interpretation. If a supplement conflicts
with a milestone guide, follow the guide and report the drift.

---

## Lane variables

Milestone files reference these; export them once at session start:

```bash
export LANE=cc
export LANE_UPPER=CC
export INSTANCE=claude-code-cc-livetest-c44589bcb738
export QCLI=~/.quaid/plugins/quaid/quaid
export SILO=~/.quaid/instances/claude-code-cc-livetest-c44589bcb738
export LIFECYCLE="/clear"   # M2 Part A also uses /compact where supported
```

`SEND` mechanism: send user-visible content into the CC tmux pane with
`tmux-msg.sh --no-chrome` so no inter-agent prefix is injected:

```bash
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CC "<text>"
```

CC does not have a Matrix surface — messages go in the visible pane only.
Raw `tmux send-keys` remains banned for milestone prompts and recovery turns.

**CC extraction window:** CC extracts asynchronously via `session_end` after
`/exit` or `/clear`. Wait at least 2 minutes after the trigger before
checking the DB.

**CC hook trace markers:** Claude Code can handle `/clear` without sending a
`UserPromptSubmit` hook for the command text. For `/clear`, look for
`hook.session_init.session_transition_detected` and
`hook.session_init.session_transition_signal_written` with `command=/clear` in
`$SILO/logs/quaid-hook-trace.jsonl`, then confirm the daemon log shows a real
`[daemon-session_end]` for the ended session. For `/exit`, confirm the
SessionEnd hook wrote a `session_end` signal and the daemon processed it.

---

## Launch

After M0 install, start the CC interaction pane:

```bash
tmux respawn-pane -k -t livetest:CC 'zsh -il'
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CC "ssh REMOTE_HOST"
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CC "mkdir -p /Users/admin/cc-livetest && cd /Users/admin/cc-livetest && QUAID_HOME=WORKSPACE CLAUDE_PROJECT_DIR=/Users/admin/cc-livetest claude --dangerously-skip-permissions --model claude-sonnet-4-6"
```

**MANDATORY — always pass `--model claude-sonnet-4-6` as a launch flag.**
Do NOT use the in-session `/model` picker after launch. The `/model` command
writes `<command-name>/model</command-name>` + `local-command-stdout` blocks
into the session transcript before any real user turn arrives. The daemon's
internal-session classifier sees only system/meta content and freezes the
cursor as internal, which then silently skips extraction for every subsequent
user turn in that session. Launching with `--model` avoids touching the
transcript before the first real user prompt.

Never run CC milestones on the highest-cost model tier — too expensive. Do not send any test
messages until the launch has fully rendered the Welcome screen and the
bypass-permissions banner is visible.

---

## Sending Messages

```bash
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CC "your message"
sleep 10
tmux capture-pane -t livetest:CC -p | tail -30
```

**Always exit with `/exit`** — never Ctrl+C. Ctrl+C bypasses the SessionEnd
hook and extraction will not fire.

`claude -p` (print mode) does not trigger hooks — always use interactive mode.

**MANDATORY session-capture proof before M2:** after launch and after the first
real user message, verify that Claude actually created a live transcript file for
this project. If this file is missing, stop immediately and report FAIL to the
coordinator — Quaid will have nothing to extract and every downstream DB check
will be false signal.

```bash
ssh REMOTE_HOST 'cd ~/quaidcode/dev && bash modules/quaid/tests/livetest/scripts/verify-cc-session-capture.sh --remote localhost --project-dir /Users/admin/cc-livetest --instance claude-code-cc-livetest-c44589bcb738 --max-age-min 5'
```

Expected: `PASS`, including at least one fresh `*.jsonl` path. If it fails:
- you are not in a real interactive Claude session
- or Claude never started from `/Users/admin/cc-livetest`
- or the wrong command path was used
- or you checked the wrong transcript directory. Use the verifier script instead
  of hardcoding Claude's project-derived transcript path.

Do not continue to M2 until this is non-empty.

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

## Extraction Triggers

| Trigger | How | Notes |
|---------|-----|-------|
| Session end | `/exit` in CC pane | Fires SessionEnd hook → daemon extraction |
| Compaction | `/compact` | Fires PreCompact hook → daemon extraction |
| Timeout | inactivity > `capture.inactivityTimeoutMinutes` | Fires daemon-compaction signal (source: timeout_extract) |
| Rolling | session crosses `capture.chunk_tokens` threshold | Daemon polls and fires rolling_stage automatically |

After `/exit` or `/compact`, wait **30–60 seconds** before checking the DB.

CC startup transcripts can briefly contain only hook/system noise. If the daemon
logs `gained non-internal content past a frozen internal cursor` during M2 Part B,
continue the two-chunk rolling procedure. Recovered Chunk-1 content should remain
buffered while the transcript is active so Chunk 2 can cross the rolling
threshold. If `internal_cursor_unfrozen_flush` fires before Chunk 2 or inside the
active rolling window, route W1; that flush is only for quiet subthreshold
recovery tails.

For M2 Part B specifically, after Chunk 2 receives its `ACK` and the rolling
stage publishes, send the lane lifecycle command exactly:

```bash
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:CC "/clear"
```

Do not grade Chunk-2-only keywords until that `/clear` is visible in
`quaid-hook-trace.jsonl` as `hook.session_init.session_transition_signal_written`
with `command=/clear`, and the daemon has processed the resulting
`session_end`. The synthetic `rolling_stage_flush` is not the lifecycle drain;
it only publishes the already-staged rolling payload and may intentionally
preserve a subthreshold Chunk-2 semantic tail for `/clear`.

---

## Timeout Extraction (M2 Part C)

Timeout extraction is tested by `livetest-guide/M2.md` Part C, not M4. For CC,
the inactivity timeout writes a `compaction` signal with `source:
timeout_extract`; the daemon extracts and stores facts, but the visible session
is not compacted. Use the central M2 Part C marker and
`final_facts_stored >= 1` checks.

---

## Daemon Management

CC runs its own extraction daemon independent of OpenClaw. The `quaid` CLI
path is `~/.quaid/plugins/quaid/quaid` — the installed runtime. Do NOT use
`~/.openclaw/extensions/quaid/quaid` for CC daemon management; that path
only exists if OpenClaw is installed and would silently break on systems
without it.

Check status with:
```bash
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
  ~/.quaid/plugins/quaid/quaid daemon status 2>&1'
```

Verify instance root, log file, and pid file all point to `CC_INSTANCE`.

---

## Auth Tokens

CC needs two different auth surfaces to be healthy:

1. **Claude CLI session auth** from `~/.claude/.credentials.json` on the run VM.
   Preflight copies this from the coordinator and requires a safe remaining
   lifetime before launch. If it is expired or too close to expiry, preflight
   fails; if it expires anyway, `claude` fails with `401` before `SessionStart`,
   no transcript JSONL is created, and no hook trace appears. That is a run
   blocker: stop and ask the coordinator to refresh Claude auth + rerun preflight.

2. **Quaid shared Anthropic auth** in `WORKSPACE/shared/auth/credentials.json`
   for daemon LLM calls after hooks fire.

CC requires a long-lived Anthropic credential in the shared registry at `WORKSPACE/shared/auth/credentials.json`.
This can be an Anthropic OAuth token from `claude setup-token` or a standard Anthropic API key.

**For live tests:** use a pre-generated Anthropic token from the dev config:
```bash
# Copy from dev machine token file (coordinator provides the path)
scp clawdbot@testbench:~/quaidcode/anthtoken-sol.md /tmp/anthtoken.txt
ssh REMOTE_HOST "TOKEN=\$(cat /tmp/anthtoken.txt | tr -d '[:space:]') && quaid auth refresh --kind anthropic_oauth \"\$TOKEN\""
```

**NEVER** copy from `~/.claude/.credentials.json` — that is the CC OAuth subscription
token which auto-expires and is not reliable for Quaid extraction calls.

Without a valid token the daemon falls back to `claude -p` subprocess calls and triggers
a hook storm (many concurrent hooks.py processes). If you see this, check:
```bash
ssh REMOTE_HOST 'pgrep -c -f hooks.py 2>/dev/null || echo 0'
```
More than 3 concurrent hooks.py processes = hook storm. Report to coordinator immediately.

---

## Instance Isolation

`QUAID_INSTANCE` is **not global** for CC. Claude hooks live in the global
`~/.claude/settings.json`, but instance identity is project-scoped: either
explicitly pinned in `/Users/admin/cc-livetest/.claude/settings.json`, or derived by
Quaid from the resolved `CLAUDE_PROJECT_DIR` path. This lane uses
`/Users/admin/cc-livetest` and expects `claude-code-cc-livetest-c44589bcb738`.

Verify:
```bash
ssh REMOTE_HOST 'cd ~/quaidcode/dev && bash modules/quaid/tests/livetest/scripts/verify-cc-session-capture.sh --remote localhost --project-dir /Users/admin/cc-livetest --instance claude-code-cc-livetest-c44589bcb738 --max-age-min 5'
# Expected: PASS with either explicit project_instance=CC_INSTANCE or path-derived fallback
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"$HOME/.claude/settings.json\")); print(d.get(\"env\",{}).get(\"QUAID_INSTANCE\",\"(absent — correct)\"))"'
# Expected: absent
ssh REMOTE_HOST 'python3 -c "import json; d=json.load(open(\"$HOME/.claude/settings.json\")); print(sorted((d.get(\"hooks\") or {}).keys()))"'
# Expected: includes SessionStart, UserPromptSubmit, PreCompact, SessionEnd
```

---

## Database and CLI

```bash
# DB
ssh REMOTE_HOST 'sqlite3 WORKSPACE/instances/CC_INSTANCE/data/memory.db "SELECT COUNT(*) FROM nodes;"'

# CLI
ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE \
  ~/.quaid/plugins/quaid/quaid recall "query" 2>&1'
```

---

## Milestone Notes

### M3 — Recall
Follow `livetest-guide/M3.md`. CC supports `/compact` directly, but rolling
extraction and `/compact` are extraction/lifecycle details for milestones that
ask for them; do not grade rolling extraction as M3. M3's objective is recall
coverage across auto-inject, deliberate recall, graph traversal, and
date-bounded recall.

### M4 — Project System and Docs CLI
Follow `livetest-guide/M4.md`. CC has no lane-specific M4 replacement; do not
run timeout extraction as M4.

### M8 — Temporal Provenance
Follow `livetest-guide/M8.md`.

CC is launched with `--model claude-sonnet-4-6` (see Launch section above). Do NOT
use `/model` in-session — it writes model-switch metadata into the transcript before
the first real user turn, which freezes the cursor and silently skips extraction.
Sonnet is already active from launch; no model switch needed for M8.

### M5 Part A — Silo Isolation: Multi-Agent Silo Verification
CC uses `claude-code-cc-livetest-c44589bcb738` as the instance ID. Runtime silo is at
`~/.quaid/instances/claude-code-cc-livetest-c44589bcb738/` (hidden). Follow the CC
Part A procedure in `livetest-guide/M5.md`. Never SKIP — all three platforms run M5.

### M5 Part B — Silo Isolation: Multi-Instance Verification
CC Part B verifies **auto-provisioning** from a new project PWD creates a
properly isolated silo at first hook use.

**Do not** call `quaid claudecode make_instance` directly — it's hook-internal
only. To test multi-instance, just launch Claude in a new project dir and
trigger a hook; auto-provisioning happens automatically. The instance name is
derived from the absolute PWD (leading `/` stripped, `/` → `-`, prefixed with
`claude-code-`).

Follow the guide exactly — includes a cross-project spillover proof.
