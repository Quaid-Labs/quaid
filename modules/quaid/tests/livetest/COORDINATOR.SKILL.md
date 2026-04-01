# Live Test Coordinator

You are the **coordinator** for a Quaid live test run. Your job is to manage the
full run loop: wipe the remote, drive agent-driven install on each platform, run
the milestone suite, fix infrastructure blockers, and loop until a full suite
passes with zero new commits.

---

## Before You Start

Read `tests/livetest/README.md` for the full architecture and prerequisites.

Read `tests/LIVE-TEST-GUIDE.md` for the authoritative milestone definitions,
XP procedure, and platform-specific notes. Do not substitute memory of prior
runs for reading the current guide.

Load your config:
```bash
cat tests/livetest/livetest-config.json
```

All references to REMOTE_HOST, WORKSPACE, OWNER_NAME, INSTANCE_NAME, and
TESTER_CLI below are read from `livetest-config.json`. Substitute actual values
before running any command.

### Confirm your coordinator pane address

Do this before spawning any testers. The script auto-detects the sending pane,
but you still need the address to pass to testers so they can message you back.

```bash
COORDINATOR_PANE=$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}')
TMUX_MSG_SENDER=coordinator \
  tests/livetest/scripts/tmux-msg.sh "$COORDINATOR_PANE" "coordinator pane verified: $COORDINATOR_PANE"
```

If the message appears in your pane, the address is correct. If the script errors
or the message does not arrive, you are not in tmux or the pane address is wrong —
resolve this before continuing (see README prerequisite).

Pass `$COORDINATOR_PANE` to every tester at boot so they know where to send
STATUS and ISSUE messages. Do not proceed to session setup until the self-test passes.

---

## Step 1 — Set Up the livetest tmux Session

```bash
tmux has-session -t livetest 2>/dev/null || tmux new-session -d -s livetest -n CC-tester
tmux list-windows -t livetest | grep -q 'CC$'         || tmux new-window -t livetest -n CC
tmux list-windows -t livetest | grep -q 'OC-tester'   || tmux new-window -t livetest -n OC-tester
tmux list-windows -t livetest | grep -q 'OC$'         || tmux new-window -t livetest -n OC
tmux list-windows -t livetest | grep -q 'CDX-tester'  || tmux new-window -t livetest -n CDX-tester
tmux list-windows -t livetest | grep -q 'CDX$'        || tmux new-window -t livetest -n CDX
```

Scripts shipped with the livetest suite (relative to repo root):
- `tests/livetest/scripts/livetest-preflight.sh` — safety checks, wipe, platform start (run before every run)
- `tests/livetest/scripts/livetest-wipe.sh` — wipe Quaid from remote (called by preflight)
- `tests/livetest/scripts/livetest-platform-start.sh` — start platform services on remote (called by preflight)
- `tests/livetest/scripts/tmux-msg.sh` — inter-agent messaging
- `tests/livetest/scripts/livetest-nudge.sh` — keepalive nudge loop

Start a tester agent in each `-tester` window using the CLI from your config
(default `codex --yolo`). The tester's working directory should be the root of
this repo so it can read the skill file:

```bash
tmux send-keys -t livetest:CC-tester  "cd /path/to/quaid && TESTER_CLI" Enter
tmux send-keys -t livetest:OC-tester  "cd /path/to/quaid && TESTER_CLI" Enter
tmux send-keys -t livetest:CDX-tester "cd /path/to/quaid && TESTER_CLI" Enter
```

On first message to each tester, send the contents of `TESTER.SKILL.md` as the
opening context. Include:
- Which platform it is testing (OC, CC, or CDX)
- Its own tmux window name (e.g. `livetest:OC-tester`)
- **Your coordinator pane address** (from `tmux.coordinator_pane` in config)

The tester uses your pane address as the target for all STATUS and ISSUE messages.
Without it, testers cannot reach you.

Start nudge loops for each tester window (keeps agents active during long runs):
```bash
LIVETEST_DIR=tests/livetest/scripts
$LIVETEST_DIR/livetest-nudge.sh -w livetest:CC-tester  -r "Run N" &; CC_NUDGE=$!
$LIVETEST_DIR/livetest-nudge.sh -w livetest:OC-tester  -r "Run N" &; OC_NUDGE=$!
$LIVETEST_DIR/livetest-nudge.sh -w livetest:CDX-tester -r "Run N" &; CDX_NUDGE=$!
echo "Nudge PIDs: CC=$CC_NUDGE OC=$OC_NUDGE CDX=$CDX_NUDGE"
```

Kill nudges at run end:
```bash
kill $CC_NUDGE $OC_NUDGE $CDX_NUDGE 2>/dev/null
```

Open the platform interaction panes (SSH to remote, start platforms after install):

```bash
# These are populated after M0 install — do not start platforms before install
tmux send-keys -t livetest:OC  "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CC  "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CDX "ssh REMOTE_HOST" Enter
```

---

## Step 2 — Preflight: Pane Verify, Safety Check, Wipe, Platform Start

**Do this at the start of every run.** Two things happen here: you confirm your
own pane address, and you run the preflight script that wipes the remote and
starts platform services.

**Confirm coordinator pane:**
```bash
COORDINATOR_PANE=$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}')
TMUX_MSG_SENDER=coordinator \
  tests/livetest/scripts/tmux-msg.sh "$COORDINATOR_PANE" \
  "coordinator self-test: pane confirmed as $COORDINATOR_PANE — run starting"
```

If the message does not arrive, stop. You are not in tmux or the detected address
is wrong. Do not proceed until this passes.

**Record run start SHA:**
```bash
cd /path/to/quaid && git rev-parse HEAD
```
Save as RUN_START_SHA. Compare HEAD against this at run end.

**Run preflight (wipe + safety check + platform start):**
```bash
tests/livetest/scripts/livetest-preflight.sh
```

The preflight script:
1. Verifies the remote host is not this machine (hard abort if they match)
2. Verifies SSH connectivity
3. Wipes Quaid from the remote (all silos, hooks, sessions, extension dir)
4. Starts the OC gateway and waits for it to be healthy

If preflight fails, do not proceed. Read the error output and fix the underlying
cause before continuing.

For a CC-only wipe (when OC is already live mid-run):
```bash
tests/livetest/scripts/livetest-preflight.sh --wipe-platform cc --skip-platform-start
```

---

## Step 3 — M0: Agent-Driven Install

**M0 tests the installer itself.** Each platform agent reads the Quaid AI install
guide on canary and installs Quaid itself. Do not run the installer directly.

### Execution order

1. Pick a lead platform (rotate each run or randomize). Run that platform's M0 alone first.
2. Once lead M0 passes, send start signals to the other two testers simultaneously.
3. M0 must pass on all platforms before M1 begins.

### What to send each platform

Tell the platform pane:

> Please install Quaid by following the AI install guide on the canary branch:
> https://github.com/Quaid-Labs/quaid/blob/canary/docs/AI-INSTALL.md
>
> Use these parameters:
> - Workspace: WORKSPACE
> - Instance name: INSTANCE_NAME
> - Owner name: OWNER_NAME
>
> Read the guide, run the installer, and tell me when Quaid is installed and
> `quaid doctor` returns healthy.

**Delivery per platform:**

| Platform | How to send |
|----------|------------|
| OC | Via the OC agent CLI (`openclaw agent --agent main -m "..."`) |
| CC | tmux send-keys to `livetest:CC`, then Enter |
| CDX | tmux send-keys to `livetest:CDX`, then Enter |

Do not provide specific command lines to the platform — let it read the guide.
Answer clarifying questions naturally. If it cannot complete the install,
that is an M0 FAIL — investigate the installer, fix, and retry.

### Pre-install coordinator prep

**OC only** — ensure the OC gateway is running before the OC agent tries to install:
```bash
ssh REMOTE_HOST 'pgrep -f openclaw-gateway > /dev/null 2>&1 || (nohup openclaw gateway > /tmp/oc-gw.log 2>&1 &); for i in $(seq 1 30); do curl -sf http://localhost:18789/health > /dev/null 2>&1 && echo "Gateway ready" && break || sleep 2; done'
```

**CC only** — clear any stale Quaid hooks before install:
```bash
ssh REMOTE_HOST 'python3 - <<PY
import json; from pathlib import Path
p = Path.home() / ".claude/settings.json"
if p.exists():
    d = json.loads(p.read_text())
    h = d.get("hooks", {})
    for ev, entries in list(h.items()):
        h[ev] = [e for e in entries if "quaid" not in str(e).lower()]
    p.write_text(json.dumps(d, indent=2))
print("Cleared existing Quaid CC hooks")
PY'
```

### M0 pass criteria

After the platform reports completion:

1. **Install messages visible** — capture the platform pane and confirm installer
   status messages appeared:
   ```bash
   tmux capture-pane -t livetest:OC -p | grep -i "quaid\|install\|hook\|schema\|ready\|error" | tail -20
   tmux capture-pane -t livetest:CC -p | grep -i "quaid\|install\|hook\|schema\|ready\|error" | tail -20
   tmux capture-pane -t livetest:CDX -p | grep -i "quaid\|install\|hook\|schema\|ready\|error" | tail -20
   ```
   Silent install with no messages = M0 FAIL. Report this as an installer bug.

2. **Health check passes:**
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=OC_INSTANCE ~/.openclaw/extensions/quaid/quaid doctor 2>&1 | tail -5'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE ~/.openclaw/extensions/quaid/quaid doctor 2>&1 | tail -5'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE WORKSPACE/plugins/quaid/quaid doctor 2>&1 | tail -5'
   ```

### Post-install coordinator steps (after M0 PASS, before M1)

**Write CC auth token** (required for daemon LLM calls):
```bash
TOKEN=$(cat CC_AUTH_TOKEN_FILE | tr -d '[:space:]')
ssh REMOTE_HOST "mkdir -p WORKSPACE/config/adapters/claude-code && echo -n '$TOKEN' > WORKSPACE/config/adapters/claude-code/.auth-token && chmod 600 WORKSPACE/config/adapters/claude-code/.auth-token && echo 'Auth token written'"
```

**Overwrite deep lane with fast lane** on each silo (HARD RULE — see CLAUDE.md):
```bash
for INSTANCE in OC_INSTANCE CC_INSTANCE; do
  ssh REMOTE_HOST "python3 -c \"
import json; p = 'WORKSPACE/$INSTANCE/config/memory.json'
with open(p) as f: d = json.load(f)
fast = d['models']['fastReasoning']
d['models']['deepReasoning'] = fast
with open(p, 'w') as f: json.dump(d, f, indent=2)
print('deep lane set to', fast, 'for $INSTANCE')
\""
done
```

**Set live-test chunk_tokens** (lowers extraction threshold for short test turns):
```bash
for INSTANCE in OC_INSTANCE CC_INSTANCE CDX_INSTANCE; do
  ssh REMOTE_HOST "python3 -c \"
import json; p = 'WORKSPACE/$INSTANCE/config/memory.json'
with open(p) as f: d = json.load(f)
d.setdefault('capture', {})['chunk_tokens'] = 1500
with open(p, 'w') as f: json.dump(d, f, indent=2)
print('chunk_tokens=1500 for $INSTANCE')
\""
done
```

---

## Step 4 — Run M1–M13 (Parallel)

Send start signals to all three tester windows after M0 passes on all platforms.
All three run simultaneously. The run is not complete until all three reach M13 PASS.

For full milestone definitions, see `tests/LIVE-TEST-GUIDE.md`.

### The prime directive

A failure is a signal. Before writing any code in response to a failure, ask:

> "Does this fix make the system more correct, or does it make the test easier to pass?"

If the latter — stop. Wrong responses to failures:
- Relaxing a criterion because it is hard to satisfy
- Hardcoding values that mask a real derivation failure
- Skipping a safety check because it causes a timeout
- Ruling PASS-WITH-NOTE to avoid doing work

### Coordinator responsibilities during the run

- Monitor for ISSUE messages from testers.
- When an issue arrives: investigate → fix → commit → build runtime → deploy → tell
  tester to retry. Do not ask for a retry before the fix is deployed.
- Log every fix commit to `unreviewed-commits.md` immediately (do not batch).
- Do not fix recall quality issues (wrong facts, low scores, bad ranking). Those
  are benchmark scope — escalate separately.

**Infrastructure vs quality:**
- Infrastructure (your scope): crashes, timeouts, missing signals, wrong DB path,
  hook failures, daemon not starting, extraction never firing, wrong silo.
- Quality (not your scope): wrong facts recalled, low similarity scores, bad
  ranking, family graph gaps.

### PASS-WITH-NOTE — strict criteria

Only valid when ALL of the following are true:
1. The failure is constrained by an external system API or data model.
2. All other steps of the milestone pass fully.
3. The tested function works end-to-end via a different path covered by passing steps.
4. A fix would require changing the external system, not just a code patch.

If you can imagine a code change that would fix it — write it.

---

## Step 5 — XP (Cross-Platform Project Linking)

Run after all three platforms reach M13 PASS. Full procedure in
`tests/LIVE-TEST-GUIDE.md` under "Cross-Platform Project Linking Test."

XP tests that all three platforms can share a project and recall each other's docs.

---

## Step 6 — End-of-Run Check

```bash
cd /path/to/quaid && git log --oneline RUN_START_SHA..HEAD
```

### Case A — Zero new commits

Full suite passed with no code changes.

1. Push canary:
   ```bash
   cd /path/to/quaid && ./scripts/push-canary.sh github
   ```
2. Deploy to remote:
   ```bash
   rsync -a --exclude='__pycache__' --exclude='*.pyc' \
     modules/quaid/ REMOTE_HOST:WORKSPACE/plugins/quaid/
   rsync -a --exclude='__pycache__' --exclude='*.pyc' \
     modules/quaid/ REMOTE_HOST:~/.openclaw/extensions/quaid/
   ```
3. Print the end-of-run report (see **End-of-Run Report** below).
4. Stop. Do not start another run unless `loop: true` in `livetest-config.json`.

### Case B — One or more new commits

1. Build runtime:
   ```bash
   cd modules/quaid && npm run build:runtime
   ```
2. Push canary (use `./scripts/push-canary.sh github`, not raw `git push`).
3. Deploy to remote (rsync as above).
4. Log all new commits to `unreviewed-commits.md` under a new run section.
5. Print the end-of-run report (see **End-of-Run Report** below).
6. **Default behavior (`loop: false`):** Stop. Tell the user the run required
   commits and recommend a follow-up run to verify the fixes are clean.
7. **Loop mode only (`loop: true` in config):** Return to Step 2 and start the
   next run with the new HEAD as RUN_START_SHA.

---

## End-of-Run Report

Print a structured summary at the end of every run:

```
=== LIVETEST RUN REPORT ===
Run N — YYYY-MM-DD

RESULT: CLEAN | REQUIRES FOLLOW-UP | FAILURES REMAIN

Platform results:
  OC:  PASS | FAIL | PASS-WITH-NOTE
  CC:  PASS | FAIL | PASS-WITH-NOTE
  CDX: PASS | FAIL | PASS-WITH-NOTE
  XP:  PASS | FAIL | SKIPPED

Issues fixed this run: N
  - <sha> <short description>

Commits made this run: N
  (none) | list of sha + subject

Next step:
  Suite clean — no action needed.
  | Follow-up run recommended to verify N fix commit(s).
  | Failures remain — see issues above before re-running.
===========================
```

---

## Loop Termination Contract (loop mode only)

When running with `loop: true` in `livetest-config.json`:
- Only exit when a full suite (OC + CC + CDX + XP) passes with zero new commits.
- A run that passes but required commits → mandatory re-run, no exceptions.
- Do not exit early because the suite looks stable. Run it clean.

---

## Commit Logging Format

```markdown
## Run N — YYYY-MM-DD (theme)

| Commit | Date | Description |
|--------|------|-------------|
| `<sha>` | YYYY-MM-DD | <subject> |
```

Note commits that are superseded by a later commit in the same run.

---

## Safety Rules

- All install/uninstall/setup commands run via `ssh REMOTE_HOST '...'`. Never locally.
- Use `trash` over `rm` for local files.
- Do not push to the main branch. Canary only, via the push script.
- Do not modify `benchmark-checkpoint/` (read-only).
- Do not tune recall quality parameters (`minSimilarity`, `hopDecay`, ranking weights).
