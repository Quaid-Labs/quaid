# Live Test Coordinator

You are the **coordinator** for a Quaid live test run. Your job is to manage the
full run loop: wipe the remote, drive agent-driven install on each platform, run
the milestone suite, fix infrastructure blockers, and loop until a full suite
passes with zero new commits.

---

## VM Management (tart)

Live tests run on tart VMs cloned from a locked base snapshot. The base snapshot
has OC, CC, CDX, Homebrew, Python 3.10, Node, Telegram, and SSH pre-configured.
No Quaid installed — M0 tests the installer.

```bash
# Reset VM (clone from locked base, boot fresh)
tart delete quaid-livetest-run 2>/dev/null
tart clone quaid-livetest-base quaid-livetest-run
chmod +w ~/.tart/vms/quaid-livetest-run/disk.img
tart run quaid-livetest-run --no-graphics &
sleep 15 && VM_IP=$(tart ip quaid-livetest-run)

# SSH key setup (base has key baked in, but new clone may need it)
cat ~/.ssh/id_ed25519.pub | sshpass -p 'admin' ssh -o StrictHostKeyChecking=no admin@$VM_IP \
  'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys'

# Sync source (NOT to ~/quaid — separate dir)
rsync -az --exclude=node_modules --exclude=.git --exclude=__pycache__ \
  --exclude='*MagicMock*' --exclude='<MagicMock*' --exclude='~/' \
  --exclude='memory.db*' --exclude='.ci-local-logs' \
  --exclude='.pytest-home' --exclude='.tmp' --exclude='pytest-runner' \
  /path/to/quaidcode/dev/ admin@$VM_IP:/Users/admin/quaid-src/
```

**"Reset VM" always means clone from base — never snapshot current dirty state.**

To update the base snapshot (unlock, modify, re-lock):
```bash
chmod +w ~/.tart/vms/quaid-livetest-base/disk.img
tart run quaid-livetest-base --no-graphics &
# ... make changes (e.g. update platform versions) ...
tart stop quaid-livetest-base
chmod -w ~/.tart/vms/quaid-livetest-base/disk.img
```

### Platform version checks

Before starting a run, verify platform versions are current:
```bash
ssh admin@$VM_IP 'source ~/.zprofile
echo "Installed: OC=$(openclaw --version) CC=$(claude --version | head -1) CDX=$(codex --version | head -1)"
echo "Latest: OC=$(npm view openclaw version) CC=$(npm view @anthropic-ai/claude-code version) CDX=$(npm view @openai/codex version)"'
```
If outdated: update the base snapshot (not the run VM). Versions must be pinned in the snapshot.

## OC Interaction

OC live testing uses the TUI (`openclaw tui`) for all interaction.
Extraction is triggered by `/new` in the TUI — the adapter detects the
new session key in sessions.json and signals extraction for the old session.
After `/new`, send one follow-up message in the new session to ensure the
session key is written to sessions.json.

**Do NOT use tg-extract or any manual signal injection.** These bypass the
feature under test and poison reset-dedupe markers, preventing the adapter's
native `/new` detection from working on subsequent attempts.

If `/new` does not trigger extraction, that is a bug to investigate and fix —
not a condition to work around with manual signals.

## Post-first-M0: Global Livetest Config

After the first M0 clears, inject test-specific config overrides:
```bash
ssh admin@$VM_IP 'mkdir -p ~/.quaid/shared/config/global && \
  echo "{\"livetest\":{\"enableExtractionBufferLog\":true},\"capture\":{\"chunk_tokens\":1500}}" \
  > ~/.quaid/shared/config/global/config.json'
```
This sets: extraction buffer logging (for sanitizer audits) and chunk_tokens=1500
(triggers rolling extraction in normal test sessions; production default is 8000).
Do this once per run. Restart daemons after.

## CC Auth Token

When the CC installer asks for an auth token, provide the Yuni Anthropic OAuth
token. Write it to the path the installer specifies. NEVER write a placeholder.

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

## Transcript Hygiene Audit

During each platform run, require at least one sanitized-transcript audit after
the platform has produced real extracted sessions.

Goal:
- catch system or hook chatter that still survives adapter sanitization
- turn those lines into concrete adapter-filter candidates

Tester should inspect the parsed / sanitized transcript output and report:
- suspicious line(s)
- session inspected
- why the line appears to be system text
- whether it looks platform-specific or generic Quaid wrapper leakage

Use this as a routine live-test check, not only after a known failure.

### Confirm your coordinator pane address

Do this before spawning any testers. The script auto-detects the sending pane,
but you still need the address to pass to testers so they can post into your mailbox.

```bash
COORDINATOR_PANE=$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}')
TMUX_MSG_SENDER=coordinator \
  tests/livetest/scripts/tmux-msg.sh "$COORDINATOR_PANE" "coordinator pane verified: $COORDINATOR_PANE"
```

If the message appears in your pane, the address is correct. If the script errors
or the message does not arrive, you are not in tmux or the pane address is wrong —
resolve this before continuing (see README prerequisite).

Use your pane address as your mailbox address. Testers post routine STATUS and ISSUE
items there with `tests/livetest/scripts/tmux-mailbox.sh`. Do not proceed to session
setup until the self-test passes.

Quick mailbox self-check:

```bash
TMUX_MSG_SENDER=coordinator \
  tests/livetest/scripts/tmux-mailbox.sh post --kind STATUS "$COORDINATOR_PANE" \
  "coordinator mailbox self-test"
tests/livetest/scripts/tmux-mailbox.sh next "$COORDINATOR_PANE"
# copy the ID from the output, then ack it:
tests/livetest/scripts/tmux-mailbox.sh ack "$COORDINATOR_PANE" <message-id>
```

---

## Step 1 — Set Up the livetest tmux Session

`livetest` is the canonical **local** tmux session name for all live-test work.
This is a hard rule.

- Use one window per platform: `CC`, `OC`, `CDX`.
- Each platform window must be split into two panes.
- Left pane: local tester agent.
- Right pane: local SSH shell into the remote platform under test.
- Do **not** run tester agents on the remote host.
- Do **not** make a remote tmux session canonical for the run.

If the host under test crashes, wedges, or installs broken code, the local tester
must survive. Running the tester on the remote host violates that safety boundary.

Do not run a one-off lane in a differently named session. Operator attach paths
and monitoring screens depend on the local `tmux new-session -A -s livetest`
workflow continuing to work.

```bash
tmux has-session -t livetest 2>/dev/null || tmux new-session -d -s livetest -n CC
tmux list-windows -t livetest | grep -q 'CC$'  || tmux new-window -t livetest -n CC
tmux list-windows -t livetest | grep -q 'OC$'  || tmux new-window -t livetest -n OC
tmux list-windows -t livetest | grep -q 'CDX$' || tmux new-window -t livetest -n CDX

for win in CC OC CDX; do
  if [ "$(tmux list-panes -t livetest:$win | wc -l | tr -d ' ')" -lt 2 ]; then
    tmux split-window -h -t livetest:$win
  fi
  tmux select-layout -t livetest:$win even-horizontal
  tmux select-pane -t livetest:$win.0 -T "${win,,}-tester"
  tmux select-pane -t livetest:$win.1 -T "${win,,}-platform"
done
```

Scripts shipped with the livetest suite (relative to repo root):
- `tests/livetest/scripts/livetest-preflight.sh` — safety checks, wipe, platform start (run before every run)
- `tests/livetest/scripts/livetest-wipe.sh` — wipe Quaid from remote (called by preflight)
- `tests/livetest/scripts/livetest-platform-start.sh` — start platform services on remote (called by preflight)
- `tests/livetest/scripts/tmux-msg.sh` — direct pane messaging for urgent interrupts and self-tests
- `tests/livetest/scripts/tmux-mailbox.sh` — queue-backed mailbox for routine STATUS/ISSUE traffic
- `tests/livetest/scripts/livetest-nudge.sh` — keepalive nudge loop

Start a tester agent in each left pane using the CLI from your config
(default `codex --yolo`). Start it from the tester agent workspace so the
agent-local `AGENTS.md` is loaded, and keep repo paths explicit in the prompt:

```bash
tmux send-keys -t livetest:CC.0  "cd /path/to/quaidcode/util/agents/codex-livetester && TESTER_CLI" Enter
tmux send-keys -t livetest:OC.0  "cd /path/to/quaidcode/util/agents/codex-livetester && TESTER_CLI" Enter
tmux send-keys -t livetest:CDX.0 "cd /path/to/quaidcode/util/agents/codex-livetester && TESTER_CLI" Enter
```

On first message to each tester, send the contents of **both** the general skill
file and the platform-specific supplement as the opening context:

| Tester Pane | General | Platform supplement |
|-------------|---------|-------------------|
| `livetest:OC.0` | `TESTER.SKILL.md` | `TESTER.OC.md` |
| `livetest:CC.0` | `TESTER.SKILL.md` | `TESTER.CC.md` |
| `livetest:CDX.0` | `TESTER.SKILL.md` | `TESTER.CDX.md` |

Also include in the opening message:
- Which platform it is testing (OC, CC, or CDX)
- Its own tmux pane address (e.g. `livetest:OC.0`)
- **Your coordinator pane address** (from `tmux.coordinator_pane` in config)

The tester uses your pane address as the mailbox target for all routine STATUS
and ISSUE traffic. Without it, testers cannot post into your mailbox.

Start nudge loops for each tester window (keeps agents active during long runs):
```bash
LIVETEST_DIR=tests/livetest/scripts
$LIVETEST_DIR/livetest-nudge.sh -w livetest:CC.0  -r "Run N" &; CC_NUDGE=$!
$LIVETEST_DIR/livetest-nudge.sh -w livetest:OC.0  -r "Run N" &; OC_NUDGE=$!
$LIVETEST_DIR/livetest-nudge.sh -w livetest:CDX.0 -r "Run N" &; CDX_NUDGE=$!
echo "Nudge PIDs: CC=$CC_NUDGE OC=$OC_NUDGE CDX=$CDX_NUDGE"
```

Coordinator policy:
- The active coordinator owns these live-test nudge loops directly.
- Do not route tester nudge requests through window `5` / `claude-looper`.
- Window `5` is reserved for `main`-session monitoring, not `livetest:*` sessions.

Kill nudges at run end:
```bash
kill $CC_NUDGE $OC_NUDGE $CDX_NUDGE 2>/dev/null
```

Open the platform interaction panes (SSH to remote, start platforms after install):

```bash
# These are populated after M0 install — do not start platforms before install
tmux send-keys -t livetest:OC.1  "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CC.1  "ssh REMOTE_HOST" Enter
tmux send-keys -t livetest:CDX.1 "ssh REMOTE_HOST" Enter
```

If you find an active live-test lane running under a non-canonical **local**
tmux session name, rename that local session back to `livetest` before continuing.

If you find the tester itself running on the remote host, stop and correct it.
That setup is invalid and unsafe.

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

**Verify remote/platform source is up to date before launching tests:**

Do this **every run loop iteration**, immediately after the cleanup / wipe phase
and before you launch M0 or any tester work. Do not assume the remote checkout
is current just because local `canary` is current.

Minimum check:
```bash
LOCAL_SHA=$(cd /path/to/quaid && git rev-parse --short HEAD)
ssh REMOTE_HOST 'cd ~/quaidcode/dev && git rev-parse --short HEAD'
```

If the remote SHA differs, update it before launching that loop iteration:
```bash
ssh REMOTE_HOST 'cd ~/quaidcode/dev && git pull --ff-only origin canary'
```

After any remote code update, restart the relevant runtime/daemon before
testing so the host under test is actually running the updated code.

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

> Please install Quaid by following the local AI install guide exactly, including its mandatory first command:
> `~/quaidcode/dev/docs/AI-INSTALL.md`
>
> Use these parameters:
> - Adapter/platform: PLATFORM
> - Instance name: INSTANCE_NAME
> - Owner name: OWNER_NAME
>
> Quaid uses a fixed split layout: hidden `~/.quaid` plus visible `~/quaid`. Do not choose or pass a custom workspace path.
> The guide path is inside the local canary checkout, so use that checkout directly as the install source.
> Do not browse the web for install docs or source code during M0.
> Do not install a release build or any non-canary branch.
>
> Tell me when Quaid is installed and `quaid doctor` returns healthy.

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

**OC only** — ensure the OC gateway is running and has the expected models registered before the OC agent tries to install:
```bash
ssh REMOTE_HOST 'pgrep -f openclaw-gateway > /dev/null 2>&1 || (nohup openclaw gateway > /tmp/oc-gw.log 2>&1 &); for i in $(seq 1 30); do curl -sf http://localhost:18789/health > /dev/null 2>&1 && echo "Gateway ready" && break || sleep 2; done'
```

**OC only** — verify gateway models are registered (installer PINGs these before proceeding):
```bash
ssh REMOTE_HOST 'curl -sf http://localhost:18789/v1/models | python3 -c "import json,sys; ms=[m[\"id\"] for m in json.load(sys.stdin).get(\"data\",[])]; print(\"Models:\", ms)"'
```
Confirm `claude-haiku-4-5` (or equivalent fast lane model) appears in the list. If the model is missing, the installer will fail hard at model selection — add the model to the gateway config before proceeding.

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

**CDX only** — verify the environment is clean before the first install turn.
There must be no pre-existing Quaid Codex hooks before M0. If the first install
prompt shows `SessionStart hook: Quaid loading project context`, the wipe failed
and M0 is invalid.
```bash
ssh REMOTE_HOST 'python3 - <<PY
import json
from pathlib import Path
p = Path.home() / ".codex" / "hooks.json"
if not p.exists():
    print("No Codex hooks file")
    raise SystemExit(0)
try:
    data = json.loads(p.read_text())
except Exception as e:
    print(f"Unreadable hooks.json: {e}")
    raise SystemExit(1)
bad = []
for section in (data.get("hooks") or {}).values():
    for entry in section or []:
        for hook in (entry.get("hooks") or []):
            cmd = str(hook.get("command") or "")
            if "quaid" in cmd.lower():
                bad.append(cmd)
if bad:
    print("STALE_QUAID_HOOKS")
    for cmd in bad:
        print(cmd)
    raise SystemExit(2)
print("No Quaid Codex hooks")
PY'
```

### M0 pass criteria

After the platform reports completion:

1. **Survey and install messages visible** — capture the platform pane and confirm:
   - the mandatory pre-install survey appeared
   - the platform clearly stated it would install from canary
   - installer status messages appeared during execution
   ```bash
   tmux capture-pane -t livetest:OC -p | grep -i "quaid\|install\|hook\|schema\|ready\|error" | tail -20
   tmux capture-pane -t livetest:CC -p | grep -i "quaid\|install\|hook\|schema\|ready\|error" | tail -20
   tmux capture-pane -t livetest:CDX -p | grep -i "quaid\|install\|hook\|schema\|ready\|error" | tail -20
   ```
   Silent install with no messages, missing survey, ambiguous source provenance,
   or any pre-installed Quaid hook activity before install = M0 FAIL.

2. **Health check passes:**
   ```bash
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=OC_INSTANCE ~/.openclaw/extensions/quaid/quaid doctor 2>&1 | tail -5'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE WORKSPACE/modules/quaid/quaid doctor 2>&1 | tail -5'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE WORKSPACE/modules/quaid/quaid doctor 2>&1 | tail -5'
   ```

### Post-Install Examination (after M0 PASS, before config patching)

Run this immediately after each platform's M0 passes, before any post-install
config changes. This catches installer filesystem mistakes while the install is
fresh and unmodified.

**Spawn a Sonnet subagent** to SSH into the remote and verify the filesystem:

```
Examine the Quaid install on REMOTE_HOST for platform PLATFORM.

1. Verify both Quaid roots exist:
   ssh REMOTE_HOST 'ls -la ~/.quaid 2>&1; ls -la ~/quaid 2>&1'
   Expected: both directories exist. If either does not, report FAIL — the installer wrote
   to the wrong location.

2. Verify ~/.quaid has the expected hidden structure:
   ssh REMOTE_HOST 'find ~/.quaid -maxdepth 4 -type d | sort'
   Expected directories/files (at minimum):
   - ~/.quaid/modules/quaid/               (runtime code)
   - ~/.quaid/shared/config/               (shared config)
   - ~/.quaid/instances/INSTANCE/data/     (database)
   - ~/.quaid/instances/INSTANCE/logs/     (logs)
   - ~/.quaid/instances/INSTANCE/config.json (instance config)

3. Verify ~/quaid has the expected visible structure:
   ssh REMOTE_HOST 'find ~/quaid -maxdepth 4 | sort'
   Expected directories/files (at minimum):
   - ~/quaid/projects/
   - ~/quaid/instances/INSTANCE/
   - ~/quaid/instances/INSTANCE/journal/
   - ~/quaid/instances/INSTANCE/SOUL.md
   - ~/quaid/instances/INSTANCE/USER.md
   - ~/quaid/instances/INSTANCE/ENVIRONMENT.md

4. Verify instance config landed in the right place:
   ssh REMOTE_HOST 'cat ~/.quaid/instances/INSTANCE/config.json 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"models:\", d.get(\"models\",{})); print(\"capture:\", d.get(\"capture\",{}))"'
   Expected: models.fastReasoning and capture section present.

5. Verify shared platform config exists:
   ssh REMOTE_HOST 'ls -la ~/.quaid/shared/config/PLATFORM/ 2>&1'
   Expected: config.json exists.

6. Platform-specific checks:
   - OC: verify ~/.openclaw/extensions/quaid/ is a symlink or copy pointing to ~/.quaid/modules/quaid/
   - CC: verify ~/.claude/settings.json has Quaid hooks registered
   - CDX: verify ~/.codex/hooks.json has Quaid hooks registered

7. Verify NO stale flat or misplaced paths:
   ssh REMOTE_HOST 'ls -la ~/.quaid/config/config.json 2>&1; ls -la ~/quaid/shared/config 2>&1; ls -la ~/quaid/modules 2>&1'
   All three should be "No such file or directory". If any exist, the installer
   is writing to a stale path.

Report: PASS if all checks pass, FAIL with details for any violation.
```

If any check fails, the M0 result is downgraded to FAIL and the installer must
be fixed before proceeding to M1.

### Post-install coordinator steps (after M0 PASS, before M1)

**Write CC auth token** (required for daemon LLM calls):
```bash
TOKEN=$(cat CC_AUTH_TOKEN_FILE | tr -d '[:space:]')
ssh REMOTE_HOST "mkdir -p WORKSPACE/adaptors/claude-code && echo -n '$TOKEN' > WORKSPACE/adaptors/claude-code/.auth-token && chmod 600 WORKSPACE/adaptors/claude-code/.auth-token && echo 'Auth token written'"
```

**Overwrite deep lane with fast lane** on each silo (HARD RULE — see CLAUDE.md):
```bash
for INSTANCE in OC_INSTANCE CC_INSTANCE; do
  ssh REMOTE_HOST "python3 -c \"
import json; p = 'WORKSPACE/instances/$INSTANCE/config.json'
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
import json; p = 'WORKSPACE/instances/$INSTANCE/config.json'
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

- Monitor the mailbox, not just the pane scrollback. Handle one pending item at a time:
  `tests/livetest/scripts/tmux-mailbox.sh next "$COORDINATOR_PANE"`
- After you have handled an item, acknowledge it:
  `tests/livetest/scripts/tmux-mailbox.sh ack "$COORDINATOR_PANE" <message-id>`
- When an issue arrives: investigate → fix → commit → build runtime → deploy → tell
  tester to retry. Do not ask for a retry before the fix is deployed.
- Log every fix commit to `unreviewed-commits.md` immediately (do not batch).
- Do not fix recall quality issues (wrong facts, low scores, bad ranking). Those
  are benchmark scope — escalate separately.
- Before escalating a quality issue, require one stronger-model retry in a
  fresh visible session. If it passes only on the stronger model, record
  `PASS-WITH-NOTE`. If it still fails, then hand it to benchmark.
- If you authorize a targeted reseed/cleanup before rerunning a quality issue,
  require a contamination audit first. The tester must prove the scoped
  assistant/debug contaminant rows are actually gone before the rerun starts.

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

## Post-Test Examination (after all milestones, before end-of-run report)

Run this after all platforms have completed their milestone suites (M1–M13 + XP).
This catches system information leaking into user-visible logs and outputs.

**Spawn Sonnet subagents** (one per platform, in parallel) to audit the buffered
extraction logs. Each subagent reads the full log and reports any system info
that should not be visible to a user.

```
Audit the extraction buffer log for platform PLATFORM on REMOTE_HOST.

Read the full extraction buffer log:
  ssh REMOTE_HOST 'cat ~/.quaid/instances/INSTANCE/logs/daemon/extraction-buffer.log 2>/dev/null'

Also read the daemon log:
  ssh REMOTE_HOST 'cat ~/.quaid/instances/INSTANCE/logs/daemon/extraction-daemon.log 2>/dev/null'

And the rolling extraction log:
  ssh REMOTE_HOST 'cat ~/.quaid/instances/INSTANCE/logs/daemon/rolling-extraction.jsonl 2>/dev/null'

Scan every line for system information that should NOT appear in user-facing
logs or extraction output. Flag any of the following:

1. **API keys, tokens, or credentials** — any string that looks like an API key,
   bearer token, auth header, or secret. Includes partial keys.
2. **Internal file paths** — absolute paths from the host machine that reveal
   system layout (e.g. /Users/admin/quaid/..., /home/..., /opt/homebrew/...).
   Relative paths within the Quaid workspace are OK.
3. **Hook/system chatter** — lines that are clearly hook stderr, Python tracebacks,
   or system-level diagnostic messages that leaked through adapter sanitization.
   Examples: "hook.inject.session_transition_signal_written", Python import errors,
   Node.js stack traces.
4. **Extraction prompt leakage** — any text that looks like it came from the
   extraction system prompt or LLM instructions rather than the user conversation.
   Examples: "You are performing offline memory extraction", "Extract personal
   facts from the following transcript".
5. **Configuration dumps** — raw JSON config objects, model names with provider
   prefixes (e.g. "anthropic/claude-haiku-4-5"), gateway URLs, port numbers.
6. **Other agent/system metadata** — coordinator messages, tmux pane addresses,
   tester instructions, run numbers, or any content that reveals the test harness.

For each finding, report:
- The log file and line number
- The offending text (truncated to 200 chars if long)
- Classification (credential, path, hook chatter, prompt leak, config, metadata)
- Severity: CRITICAL (credentials/tokens), HIGH (prompt leaks, config dumps),
  MEDIUM (paths, hook chatter), LOW (metadata)

Report: CLEAN if no findings, or a structured list of findings sorted by severity.
```

Aggregate results across all three platform subagents. Any CRITICAL finding
blocks the run from being marked CLEAN. HIGH findings should be logged as
issues to fix before the next run. MEDIUM/LOW findings are informational but
should be tracked.

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
