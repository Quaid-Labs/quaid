# Live Test Coordinator

You are the **coordinator** for a Quaid live test run. Your job is to manage the
full run loop: wipe the remote, drive agent-driven install on each platform, run
the milestone suite, fix infrastructure blockers, and loop until a full suite
passes with zero new commits. You are the central authority for the live test,
the subagent testers cover surface but you are the final authority on if a
perceived break is real and how to fix it. You are the problem solver, they are
the problem finders.

---

## Iterative Live Testing — fail-fast with early clears + lagged regression

**When the run is iterative (not a full milestone validation) and a developer is
waiting on test results to keep building**, prioritize dev velocity by
structuring the test as:

1. **Dependency-only first.** Given feature E under test, identify E's
   dependencies (say A & B) and non-dependencies (C & D). Run A & B only — the
   minimum surface that can invalidate E's own result.
2. **Run E.** If E fails, bail immediately and report. Dev can fix without
   waiting on C & D.
3. **If E passes → early-report success** so the originator can proceed. In the
   same message, say regression tests on C & D are still running in parallel.
4. **Run C & D in the background** after the early-clear. When they finish,
   report final — including any regressions found. A regression after an
   early-clear should be routed the same way a fresh fail would be (patch
   request + re-test), and the originator notified that the early-clear is
   revoked.
5. **Edge-case time waste is acceptable.** Occasional re-runs on regressed
   C/D paths are no worse than always running the whole suite from scratch.
   The average path gets a major speedup; the worst path matches today's cost.

**When to use:** any iterative feature-under-patch
cycle where a dev is iterating on a single scope and waiting on results to
keep moving. NOT for ship validation (those run the full suite regardless).

**When not to use:** full-run validation, release gates, push-main gates. Those
keep the standard end-to-end sequence.

**Reporting format for early-clear:**
- Subject line: "EARLY-CLEAR: E PASS, regression on C+D running in background."
- Body: E result details, list of regression scope still outstanding, ETA if
  known, commit that patch originated from.

---

## VM Management (tart)

Live tests run on tart VMs cloned from a locked base snapshot. The base snapshot
has OC, CC, CDX, Homebrew, Python 3.10, Node, Telegram, Matrix, and SSH pre-configured.
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

If Matrix account metadata needs changes for a run (for example setting
`@quaid-test-bot:localhost` displayname), do it as a one-off manual update
while the base VM is unlocked.

### Platform version checks

Before an overnight loop, run presnapshot maintenance:
```bash
tests/livetest/scripts/livetest-presnapshot-preflight.sh --config tests/livetest/livetest-config.json
```
This clones the current base, applies slow platform CLI upgrades, bakes the
OpenClaw Matrix plugin required for Matrix bot replies (`openclaw >=2026.6.11`,
Matrix plugin pinned by default to `@openclaw/matrix@2026.6.1`), and refreshes
the base snapshot only if the clone actually changed. If nothing changed, it
destroys the clone and leaves the base untouched.

### Preflight architecture: platform updates fold into base, never per-run

**Principle.** Platform CLI updates (`claude`, `codex`, `openclaw`) are by far the
slowest part of preflight, and the cost grows as the base snapshot drifts
further from upstream. They are also rare relative to dev-tree changes.
Therefore: updates belong in the base image, NOT in per-run preflight.

**Required flow (in order, no exceptions):**

1. **Presnapshot preflight (base maintenance):** run
   `livetest-presnapshot-preflight.sh` before overnight loops or when
   `livetest-preflight.sh` warns about platform drift. It starts from the
   current base snapshot, applies platform upgrades, and auto-promotes the run
   disk into the locked base only when an upgrade changed the VM.

2. **Prerun preflight (every run):** run `livetest-preflight.sh`. This path does
   health checks, non-blocking platform version drift warnings, wipe, rsync,
   credential seeding, and platform start. It does **not** apply platform
   upgrades by default.

3. **If prerun preflight warns about drift:** do not burn 10–20 minutes inside
   the run. Finish or stop the current cycle based on urgency, then run
   `livetest-presnapshot-preflight.sh` so the next cycle starts from an updated
   base.

4. **Only use `livetest-preflight.sh --with-platform-upgrades` or
   `--platform-upgrades-only` through presnapshot maintenance.** Those flags are
   for base-image updates, not normal run setup.

**Why this is right:**
- Per-run preflight avoids the 10–20min platform-update path.
- Update cost is paid ONCE per platform release, not every run.
- Snapshot model fits naturally: base = "all CLIs up-to-date, no Quaid";
  preinstall snapshot (see "Pre-install snapshot" below) = "base + dev tree
  synced + creds written + platforms ready"; install runs against preinstall;
  failure restores preinstall.
- Removes the silent-update class of bug — platform bumps become a presnapshot
  event with version diffs visible before the base is refreshed.

**Pre-install snapshot (inner-loop optimization on top of the above):** after
preflight completes (clean, no updates pending) and before invoking M0 install,
the coordinator snapshots the VM as `quaid-livetest-preinstall`. If install
fails due to an installer bug:
1. Route to W1 with full repro evidence (per the installer-bugs-are-P0 policy).
2. Wait for the fix to land.
3. Restore from `quaid-livetest-preinstall` (`tart stop quaid-livetest-run` →
   `tart delete quaid-livetest-run` → `tart clone quaid-livetest-preinstall
   quaid-livetest-run` → `tart run`).
4. Re-rsync the latest dev tree (W1's fix is in the tree, not the snapshot).
5. Re-run M0 install only.

This keeps install-bug retries to ~5min instead of the full ~15min cycle. Drop
`quaid-livetest-preinstall` once M0 succeeds across all platforms.

**Implementation status (2026-04-20):** implemented in:
- `tests/livetest/scripts/livetest-presnapshot-preflight.sh` (clone base, run platform upgrades, auto-refresh base if changed)
- `tests/livetest/scripts/livetest-preflight.sh` (per-run safety checks + platform drift warning + wipe/sync/creds/start)
- `tests/livetest/scripts/livetest-refresh-base.sh` (promote run VM disk to locked base)
- `tests/livetest/scripts/livetest-snapshot-preinstall.sh` (capture preinstall VM snapshot)
- `tests/livetest/scripts/livetest-restore-preinstall.sh` (restore run VM + re-rsync dev tree)

## OC Interaction

OC live testing uses **Matrix DM** for all interaction — not the TUI.
The Matrix server (`ai.quaid.matrix-synapse`) and OpenClaw gateway
(`ai.openclaw.gateway`) run as persistent services on the VM; no launch step
is needed after M0.

Messages are sent via the canonical synced `matrix-send` helper on the VM:
```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "message text"'
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "/new"'
```

Extraction is triggered by `/new` sent as a Matrix DM message — this routes
through `handleSlashLifecycleFromMessage`, writing the session_end signal
directly (same code path as Telegram). After `/new`, send one follow-up
message to confirm the new session is active.

**Do NOT use tg-extract or any manual signal injection.** These bypass the
feature under test and poison reset-dedupe markers.

If `/new` does not trigger extraction, that is a bug to investigate and fix —
not a condition to work around with manual signals.

## Post-first-M0: Livetest Config Overrides (per-platform)

**GATE: this step is mandatory between M0 PASS and the first M1 brief.**
Run `scripts/livetest-postm0-config.sh <cc|oc|cdx|all>` and verify that
`capture.chunk_tokens` resolves to `1500` for every installed instance
before dispatching any tester to M1. Missing this step makes M2 Part B
(rolling extraction) impossible to pass because the 1500-token fixture
cannot cross the production default of 8000 tokens.

For `oc`/`all`, the same script also registers the VM Codex OAuth access token
with OpenClaw via `openclaw models auth paste-token --provider openai` before
restarting the gateway. Do not replace this with `auth-profiles.json` seeding
alone; OpenClaw 2026.6.11 requires the CLI registration to populate its SQLite
model-provider registry.



Write livetest overrides to the **per-platform** config files, not the global
config. Platform configs supersede global for that platform only, so mid-run
timing flips (e.g. dropping `inactivityTimeoutMinutes` for the M4 lane) don't
contaminate the other lanes. Global-only overrides were the reason earlier runs
had "settings getting stuck" on wrong milestones.

**MERGE, never overwrite.** The installer wrote `shared/config/<platform>/config.json`
during M0. Using `>` wipes the file and the next inject hook fails with
`RuntimeError: No model configured for tier deep`.

Files to merge into (apply the same overrides to each):

- `~/.quaid/shared/config/openclaw/config.json`
- `~/.quaid/shared/config/claude_code/config.json`
- `~/.quaid/shared/config/codex/config.json`

Overrides to apply at post-M0 (safe for all platforms, all milestones):

- `livetest.enableExtractionBufferLog: true` — sanitizer/extraction buffer audits.
- `capture.chunk_tokens: 1500` — livetest rolling-extraction standard
  (production default 8000; smaller so rolling fires inside a test session).

Do NOT apply `capture.inactivityTimeoutMinutes: 1` globally or run-wide.
It gets flipped to `1` **only on the platform currently running M4**, and
restored to `60` immediately after (see "M4 idle-timeout flip — per platform"
below).

Deep-merge Python one-liner, parameterized by platform:

```bash
for platform in openclaw claude_code codex; do
  ssh admin@$VM_IP "python3 << PYEOF
import json, os, sys
p = os.path.expanduser(f\"~/.quaid/shared/config/${platform}/config.json\")
existing = json.load(open(p)) if os.path.exists(p) else {}
overrides = {
    'livetest': {'enableExtractionBufferLog': True},
    'capture': {'chunk_tokens': 1500}
}
def merge(b, o):
    r = json.loads(json.dumps(b))
    for k, v in o.items():
        r[k] = merge(r.get(k, {}), v) if isinstance(v, dict) and isinstance(r.get(k), dict) else v
    return r
json.dump(merge(existing, overrides), open(p, 'w'), indent=2)
print('merged platform config:', p)
PYEOF
"
done
```

Restart daemons on each platform after the merge so the new config is loaded.

### M4 idle-timeout flip — per platform

M4 is the only milestone that tests the idle-extraction path. For that one
milestone only, on the **one platform** that is actively running M4:

1. Immediately before M4 on the active platform: set that platform's
   `capture.inactivityTimeoutMinutes` to `1`, restart the lane's daemon.
2. Run M4's idle-extraction probe.
3. Immediately after M4 (pass or PWN), restore that platform's
   `capture.inactivityTimeoutMinutes` to `60`, restart the daemon.

Do NOT flip the global config, and do NOT flip the config of any platform
that is not currently running M4. If the global value is ever set to `1`,
every lane's `/new` + hook turn-latency will race the timeout, and you'll
get "extraction via timeout rolling_flush, not session_end" on every lane —
which masks real lifecycle behavior and turns clean PASSes into PWN-note.

```bash
# Before M4 on platform $P (openclaw | claude_code | codex):
ssh admin@$VM_IP "python3 << PYEOF
import json, os
p = os.path.expanduser(f'~/.quaid/shared/config/${P}/config.json')
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault('capture', {})['inactivityTimeoutMinutes'] = 1
json.dump(d, open(p, 'w'), indent=2)
print('M4 idle=1 on', p)
PYEOF
"
# restart that platform's daemon...

# After M4 on platform $P:
ssh admin@$VM_IP "python3 << PYEOF
import json, os
p = os.path.expanduser(f'~/.quaid/shared/config/${P}/config.json')
d = json.load(open(p))
d.setdefault('capture', {})['inactivityTimeoutMinutes'] = 60
json.dump(d, open(p, 'w'), indent=2)
print('M4 idle restored to 60 on', p)
PYEOF
"
# restart that platform's daemon again...
```

### Rolling-threshold interpretation for tester reports

The rolling buffer counts POST-SANITIZATION transcript content (raw prompts
and agent responses with Quaid system/notification/context blocks stripped).
Do NOT use hook `context_emitted` length as the token limiter — that's
pre-sanitization and is always much larger than the actual buffered count.
Check `semantic_buffer_tokens` in the per-session rolling state file:
`~/.quaid/instances/<instance>/data/rolling-extraction/<session_id>.json`.

## Auth credentials (handled by preflight)

Both the CC `claude` CLI credential and the shared Quaid auth credential are
seeded by `livetest-preflight.sh` — steps `[7/8]` and `[7b/8]`. You do not copy
them manually. Preflight reads the active auth-source path from its config and
writes both the local `claude` credential onto the remote and the shared Quaid
credential into `~/.quaid/shared/auth/credentials.json`. Wipe order is correct:
preflight wipes `~/.quaid` first, then reseeds, so a post-preflight silo starts
with live credentials.

If preflight `[7b/8]` prints `empty token from stdin` or similar, fall back to
writing the credential manually via `ssh REMOTE_HOST "quaid auth refresh --kind
anthropic_oauth '$TOKEN'"` using the same token source preflight was pointed at.
Route the stdin bug to W1 as a preflight fix rather than making the manual
workaround load-bearing.

NEVER write a placeholder token.

---

## Before You Start

Read `tests/livetest/README.md` for the full architecture and prerequisites.

Read `tests/livetest/livetest-guide/` for the authoritative milestone definitions,
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
POST_OUT=$(TMUX_MSG_SENDER=coordinator \
  tests/livetest/scripts/tmux-mailbox.sh post --kind STATUS "$COORDINATOR_PANE" \
  "coordinator mailbox self-test")
echo "$POST_OUT"
# The first unread item is delivered inline to your pane. Copy the ID from POST_OUT,
# then mark it done:
tests/livetest/scripts/tmux-mailbox.sh done "$COORDINATOR_PANE" <message-id>
```

---

## Step 1 — Set Up the livetest tmux Session

`livetest` is the canonical **local** tmux session name for all live-test work.
This is a hard rule.

- Use one window per platform: `CC`, `OC`, `CDX`. Each has two panes — left
  pane runs the local tester agent, right pane holds a local SSH shell into the
  remote. Testers never run on the remote host.
- `livetest-session-init.sh` (preflight setup helper) creates the session,
  windows, and panes, launches the tester CLI in each left pane, opens the SSH
  shells on the right, and starts the nudge loops. It launches testers from
  `~/quaidcode/util/agents/codex-livetester`. Run it once at the start of a
  run, or use `--restart-testers` to kill/recreate lane windows between runs.
- On first message to each tester, send `TESTER.SKILL.md` plus the lane's
  `TESTER.{OC,CC,CDX}.md` supplement, the tester's own pane address, and your
  coordinator pane address (from `tmux.coordinator_pane` in config). Without
  the coordinator pane, testers can't post into your mailbox.

Scripts shipped with the livetest suite (relative to repo root):

- `tests/livetest/scripts/livetest-preflight.sh` — safety checks, wipe, rsync
  dev tree, seed auth credentials, start platform services.
- `tests/livetest/scripts/livetest-session-init.sh` — create local `livetest`
  tmux session with CC/OC/CDX windows + tester/ssh panes + nudge loops.
- `tests/livetest/scripts/livetest-wipe.sh` — wipe Quaid on remote (called by
  preflight).
- `tests/livetest/scripts/livetest-platform-start.sh` — start platform services
  on remote (called by preflight).
- `tests/livetest/scripts/livetest-refresh-base.sh` — refresh locked base VM
  image from an updated run VM.
- `tests/livetest/scripts/livetest-snapshot-preinstall.sh` /
  `livetest-restore-preinstall.sh` — snapshot/restore the post-preflight VM
  state around M0 install retries.
- `tests/livetest/scripts/tmux-msg.sh` — direct pane messaging.
- `tests/livetest/scripts/tmux-mailbox.sh` — queue-backed mailbox for routine
  STATUS/ISSUE traffic.
- `tests/livetest/scripts/livetest-nudge.sh` — keepalive nudge loop (started by
  session-init).

**Coordinator policy:** the active coordinator owns the live-test nudge loops
started by session-init. Do not route tester nudge requests through window `5`
(`claude-looper`); window `5` is reserved for `main`-session monitoring.

If you find an active live-test lane running under a non-canonical local tmux
session name, rename it back to `livetest` before continuing. If you find the
tester itself running on the remote host, stop and correct it — that setup is
invalid and unsafe.

---

## Step 1.5 — SUT Pane Safeguard (mandatory)

**Background.** Each tester window has two panes — the tester agent on `.0`
and an SSH-to-VM shell on `.1` that is the System Under Test target. Testers
drive the SUT with `tests/livetest/scripts/tmux-msg.sh --no-chrome` targeted at
`livetest:<lane>.1`. If `.1`'s ssh exits for any reason (idle disconnect,
network blip, accidental Ctrl-D, remote restart), the pane silently drops to a
local `zsh` prompt and every subsequent pane send hits the LOCAL dev box instead
of the VM.

This is exactly the contamination class that bit Run 125: OC tester's
`.1` had escaped its ssh at unknown time, so chunk fixtures and lifecycle
keys landed on the dev box's local shell. Local Quaid bits got polluted
(`~/.quaid`, `~/quaid`) and OC test results were invalid.

**Coordinator owns the SSH lifecycle, not the testers.** During M0 setup,
the coordinator opens the ssh in each `.1` pane explicitly and verifies it
before sending the lane brief. Testers only operate at `.0`.

**Verification helper:**

```bash
VM_IP="$VM_IP" tests/livetest/scripts/verify-tester-ssh.sh --all
# OK   livetest:0.1: ssh admin@192.168.64.110 (child pid=12938)
# OK   livetest:1.1: ssh admin@192.168.64.110 (child pid=92037)
# OK   livetest:2.1: ssh admin@192.168.64.110 (child pid=57975)
# Exit 0 = all clean. Exit 1 = at least one pane dropped local.
```

**When to run:**
1. **At M0 start**, after `livetest-session-init.sh` and after you open ssh
   into each `.1` pane. Must return exit 0 before any tester brief.
2. **Before every milestone brief** (M1 through GLOBAL). Treat a non-zero exit
   the same as a foundational milestone fail: halt the affected lane,
   re-establish ssh, re-verify, and only then re-brief.
3. **On any tester ISSUE that mentions empty extraction logs / no daemon
   activity / "no signal received"** — that pattern is the classic drop-local
   signature. Run the verifier before re-routing the issue to W1.

**On verifier failure:**
1. Run
   `tests/livetest/scripts/tmux-msg.sh --no-chrome livetest:<lane>.1 "ssh admin@${VM_IP}"`
   to re-establish the connection. Wait 3s, then re-run the verifier.
2. Capture the dashboard incident as a CONTAMINATION row with timestamp
   and the affected lane — historical results from that lane become suspect.
3. If you cannot determine when the drop happened, mark the in-flight run as
   contaminated and either restart from M0 or quarantine the affected lane's
   results until next clean run.

**Do not skip this check** — the cost of a 0.1s `verify-tester-ssh.sh` call
before each brief is vastly less than the cost of a contaminated run.

### Local-quaid presence check (post-M0 mandatory)

After every M0 install, verify that `~/quaid` and `~/.quaid` do NOT exist on
the **coordinator/dev box**. If they do, something contaminated the local
machine — either a tester pane dropped to local (see above), a stray hook
fired locally, or a vitest/pytest run leaked test fixture writes outside
its tmpdir.

```bash
# Run after M0 completes. Either ls fails = clean.
ls -la ~/quaid 2>&1 | head -2
ls -la ~/.quaid 2>&1 | head -2
```

If either directory exists post-M0:
1. Inspect what's in it (memory.db, instances/, logs/) to identify the source.
2. `trash ~/.quaid ~/quaid` (recoverable). Do NOT use `rm -rf`.
3. Investigate the source. Common culprits:
   - A pytest/vitest test that didn't isolate to tmpdir.
   - A tester pane that escaped ssh (use verify-tester-ssh.sh).
   - A leftover Quaid CLI shim at `/opt/homebrew/bin/quaid` calling the local install.
4. If you can't identify the source, surface as an immediate ISSUE — the run
   is potentially contaminated.

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
is current just because local `main` is current.

Minimum check:
```bash
LOCAL_SHA=$(cd /path/to/quaid && git rev-parse --short HEAD)
ssh REMOTE_HOST 'cd ~/quaidcode/dev && git rev-parse --short HEAD'
```

If the remote SHA differs, update it before launching that loop iteration:
```bash
ssh REMOTE_HOST 'cd ~/quaidcode/dev && git pull --ff-only origin main'
```

After any remote code update, restart the relevant runtime/daemon before
testing so the host under test is actually running the updated code.

**Run preflight (safety checks + platform updates + wipe/sync/start):**
```bash
tests/livetest/scripts/livetest-preflight.sh
```

The preflight script:
1. Verifies the remote host is not this machine (hard abort if they match)
2. Verifies SSH connectivity
3. Checks remote prerequisites (brew Python, etc.)
4. Updates platform CLIs and compares before/after versions
5. If updates were applied, exits early and instructs base-image refresh
6. If no updates were applied, wipes Quaid from the remote
7. Rsyncs the latest local dev tree to the remote
8. Starts platform services and waits for health

If preflight fails, do not proceed. Read the error output and fix the underlying
cause before continuing.

If preflight exits after step 4 due to applied updates:
1. Refresh the locked base image:
```bash
tests/livetest/scripts/livetest-refresh-base.sh --config tests/livetest/livetest-config.json
```
2. Re-run preflight.

Before M0 install, snapshot preinstall state:
```bash
tests/livetest/scripts/livetest-snapshot-preinstall.sh --config tests/livetest/livetest-config.json
```

If M0 install fails with an installer bug after that snapshot:
```bash
tests/livetest/scripts/livetest-restore-preinstall.sh --config tests/livetest/livetest-config.json
```
Then retry M0 install without re-running full preflight.

For a CC-only wipe (when OC is already live mid-run):
```bash
tests/livetest/scripts/livetest-preflight.sh --wipe-platform cc --skip-platform-start
```

---

## Step 3 — M0: Install

**M0 tests the installer itself.**

**Install mode depends on run type:**

| Mode | Preflight flag | Who installs | Install command | Notes |
|------|---------------|--------------|-----------------|-------|
| Normal dev run (default) | *(none)* | Tester agent reads `~/quaidcode/dev/docs/AI-INSTALL.md` | `QUAID_ALLOW_DEV_INSTALL=1 node setup-quaid.mjs --agent --all-platforms` | Tester follows guide exactly |
| Release verification | `--release-verify <tag>` | **Coordinator** runs curl install directly via SSH | `curl -fsSL https://raw.githubusercontent.com/quaid-labs/quaid/main/install.sh \| QUAID_VERSION=<tag> bash -s -- --agent --all-platforms` | Mimics real user install; no pre-staged code on VM; testers start at M1 |

Release verification runs are rare — only used to confirm a shipped release installs correctly end-to-end. Default runs always use the dev tree.

### Execution order (normal dev run)

1. Pick a lead platform (rotate each run or randomize). Run that platform's M0 alone first.
2. Once lead M0 passes, send start signals to the other two testers simultaneously.
3. M0 must pass on all platforms before M1 begins.

### Execution order (release verification)

1. Coordinator runs pre-install prep (write credentials to VM).
2. Coordinator runs the curl install via SSH — no tester involvement.
3. Coordinator verifies all platforms with `quaid doctor`.
4. If all healthy, brief testers directly at M1 (skip tester M0).

**Preferred: single-invocation all-platforms install.** The installer supports
`--all-platforms` (commit d9e5262d1) which installs OC, CC, and CDX sequentially
in one run. Use this when doing a clean full-suite install: send the lead tester
one install message that includes `Install All Available` / `--all-platforms` in
its framing. Only the first platform install prompts for credentials; subsequent
installs reuse the shared credential store.

### What to send each platform (dev run only)

Tell the platform pane:

> Please install Quaid by following the local AI install guide exactly, including its mandatory first command:
> `~/quaidcode/dev/docs/AI-INSTALL.md`
>
> Use these parameters:
> - Adapter/platform: All platforms (`Install All Available`)
> - Instance names: OC=INSTANCE_NAME_OC, CC=INSTANCE_NAME_CC, CDX=INSTANCE_NAME_CDX
> - Owner name: OWNER_NAME
>
> Quaid uses a fixed split layout: hidden `~/.quaid` plus visible `~/quaid`. Do not choose or pass a custom workspace path.
> The guide is inside the install source checkout — use that directory as the repo root.
> Do not browse the web for install docs or source code during M0.
>
> Tell me when all platforms are installed and `quaid doctor` returns healthy for each.

If installing individual platforms instead of all-at-once, send a separate message
to each tester pane specifying only its own platform and instance name.

**CC instance caveat:** Do NOT tell the CC tester to set `QUAID_INSTANCE` in their
shell. CC derives the instance from the transcript path — `/tmp/cc-livetest` →
`claude-code-cc-livetest-51aa91834f73`. Setting `QUAID_INSTANCE` in the shell has
no effect on where facts are stored. For coordinator verification: always use
`QUAID_INSTANCE=claude-code-cc-livetest-51aa91834f73 quaid recall <query>`.

**CDX instance caveat:** CDX also uses the adapter's path-derived instance for
its project directory. For the standard `/tmp/cdx-livetest` lane, the canonical
instance is `codex-cdx-livetest-b89008986acd` after resolving macOS `/tmp` to
`/private/tmp`. Do not substitute the legacy `codex-private-tmp-cdx-livetest`;
that creates a setup-only ghost silo while hooks write to the canonical hashed
runtime silo.

**CDX M1 lifecycle caveat:** CDX LIFECYCLE is `/new`, NOT `/clear`. Sending `/clear`
in the CDX CLI does not create a new rollout file or session_end signal — the canary
will never be extracted from the below-threshold session. After sending `/new`, also
send one follow-up message to trigger `hook-inject` and fire `check_session_transition`.
If `/new` hangs (pane wedge), restart the Codex CLI process via `tmux kill-window` +
relaunch via SSH. See TESTER.CDX.md for the full CDX lifecycle explanation.

**CC async extraction:** CC extraction fires asynchronously after `/exit` (not
inline like OC). Tell CC testers to wait **at least 2 minutes** after exiting a
session before checking recall or the DB. Checking immediately after `/exit` will
always show 0 nodes — this is not a failure.

**Delivery per platform:**

| Platform | How to send |
|----------|------------|
| OC | Via the OC agent CLI (`openclaw agent --agent main -m "..."`) |
| CC | `tmux-msg.sh --no-chrome livetest:CC "<message>"` |
| CDX | `tmux-msg.sh --no-chrome livetest:CDX "<message>"` |

### Release verify install (coordinator-driven, no tester involvement)

Pre-install prep, then install directly via SSH:

```bash
# 1. Write credentials to VM (see Pre-install coordinator prep section below)

# 2. Run the install
REMOTE="admin@192.168.64.77"
TAG="v0.3.1"
ssh "$REMOTE" "curl -fsSL https://raw.githubusercontent.com/quaid-labs/quaid/main/install.sh | QUAID_VERSION=$TAG bash -s -- --agent --all-platforms --owner-name 'Solomon Steadman'"

# 3. Verify
ssh "$REMOTE" "~/.quaid/plugins/quaid/quaid doctor"

# 4. Brief testers at M1 (skip tester M0)
```

`install.sh` downloads the release tarball from GitHub, extracts it, and runs `setup-quaid.mjs` with forwarded flags. No pre-staged code on the VM needed.

Do not provide specific command lines to the platform — let it read the guide.
Answer clarifying questions naturally. If it cannot complete the install,
that is an M0 FAIL — investigate the installer, fix, and retry.

### Pre-install coordinator prep

**All platforms** — write shared auth credentials to the remote BEFORE M0 install.
The installer checks for existing credentials and may bail if they are missing or wrong kind.
OC requires `codex_oauth` (openai-compatible provider). CC requires `anthropic_oauth`.
Write BOTH before install:
```bash
# anthropic_oauth — for CC platform Quaid daemon calls (long-lived Yuni key)
ANTH_TOKEN=$(cat ~/.tmp/cc-auth-token.txt | tr -d '[:space:]')

# codex_oauth — read from the VM's ~/.codex/auth.json (Codex CLI maintains this,
# auto-refreshes each session, longer valid window than coordinator OC auth-profiles).
# Do NOT read from coordinator's OC auth-profiles — those expire quickly and aren't refreshed.
CDX_TOKEN=$(ssh REMOTE_HOST 'python3 -c "
import json, pathlib
d = json.loads(pathlib.Path.home().joinpath(\".codex/auth.json\").read_text())
print(d[\"tokens\"][\"access_token\"])
"')

ssh REMOTE_HOST "python3 << 'PYEOF'
import json, pathlib, os
p = pathlib.Path.home() / '.quaid' / 'shared' / 'auth' / 'credentials.json'
p.parent.mkdir(parents=True, exist_ok=True)
d = json.loads(p.read_text()) if p.exists() else {}
d.setdefault('credentials', {})
d['credentials']['anthropic_oauth'] = {'token': '$ANTH_TOKEN'}
d['credentials']['codex_oauth'] = {'token': '$CDX_TOKEN'}
p.write_text(json.dumps(d, indent=2))
os.chmod(p, 0o600)
print('credentials.json: anthropic_oauth + codex_oauth written')
PYEOF
"

# Also inject codex_oauth into OC auth-profiles so the OC gateway picks it up at start.
# This prevents 401 errors from the OC deep provider during extraction.
ssh REMOTE_HOST "python3 << 'PYEOF'
import json, pathlib, os
profiles_path = pathlib.Path.home() / '.openclaw' / 'agents' / 'main' / 'agent' / 'auth-profiles.json'
if profiles_path.exists():
    profiles = json.loads(profiles_path.read_text())
    profiles.setdefault('profiles', {}).setdefault('openai-codex:default', {})['access_token'] = '$CDX_TOKEN'
    profiles.setdefault('lastGood', {})['openai-codex'] = 'openai-codex:default'
    profiles_path.write_text(json.dumps(profiles, indent=2))
    print('auth-profiles.json: openai-codex:default updated')
else:
    print('auth-profiles.json not found — OC not yet installed, skipping')
PYEOF
"
```

**OC only** — ensure the OC gateway is running and has the expected models registered before the OC agent tries to install:
```bash
~/quaidcode/dev/modules/quaid/tests/livetest/scripts/livetest-openclaw-gateway-restart.sh \
  --host REMOTE_HOST --start
```

**OC only** — check gateway model list (informational — the OC installer uses hardcoded defaults, not /v1/models):
```bash
ssh REMOTE_HOST 'curl -sf http://localhost:18789/v1/models | python3 -c "import json,sys; ms=[m[\"id\"] for m in json.load(sys.stdin).get(\"data\",[])]; print(\"Models:\", ms)"'
```
Note: gpt-5.4 and gpt-5.4-mini may not appear in /v1/models even on a healthy gateway. The OC adapter's `installer_validate_model_pair_live` returns `supported: False` (no live check). The installer uses hardcoded static defaults per provider and calls `installer_ensure_gateway_model_allowlist` to register models in `agents.defaults.models` after install. This /v1/models query is informational only — the install does NOT require these models to appear here.

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
   - the platform clearly stated it would install from main
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
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=OC_INSTANCE ~/.quaid/plugins/quaid/quaid doctor 2>&1 | tail -5'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CC_INSTANCE ~/.quaid/plugins/quaid/quaid doctor 2>&1 | tail -5'
   ssh REMOTE_HOST 'QUAID_HOME=WORKSPACE QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid doctor 2>&1 | tail -5'
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
   - ~/.quaid/plugins/quaid/               (runtime code — fresh installs use plugins/)
   - ~/.quaid/shared/config/               (shared config)
   - ~/.quaid/instances/INSTANCE/data/     (database)
   - ~/.quaid/instances/INSTANCE/logs/     (logs)
   - ~/.quaid/instances/INSTANCE/config.json (instance config)

3. Verify ~/quaid has the expected visible structure:
   ssh REMOTE_HOST 'find ~/quaid -maxdepth 4 | sort'
   Expected directories/files (at minimum):
   - ~/quaid/projects/
   - ~/quaid/dev/
   - ~/quaid/projects/quaid/

4. Verify instance config landed in the right place:
   ssh REMOTE_HOST 'cat ~/.quaid/instances/INSTANCE/config.json 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"instance.id:\", d.get(\"instance\",{}).get(\"id\")); print(\"adapter.type:\", d.get(\"adapter\",{}).get(\"type\")); print(\"models:\", d.get(\"models\",{}))"'
   Expected: `instance.id` and `adapter.type` present. `models` may be present by adapter, but platform/global defaults must remain layered (no inlined capture/retrieval/system defaults).

5. Verify shared platform config exists:
   ssh REMOTE_HOST 'ls -la ~/.quaid/shared/config/PLATFORM/ 2>&1'
   Expected: config.json exists.

6. Platform-specific checks:
   - OC: verify ~/.openclaw/extensions/quaid/ is a real directory copy containing the Quaid plugin files (not a symlink)
   - CC: verify ~/.claude/settings.json has Quaid hooks registered
     AND `verify-cc-session-capture.sh` passes for `/tmp/cc-livetest`; project
     settings may omit `QUAID_INSTANCE` when the path-derived instance matches
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

**Verify installer model policy** on each silo (HARD RULE — trust installer defaults):
```bash
for INSTANCE in OC_INSTANCE CC_INSTANCE; do
  ssh REMOTE_HOST "python3 -c \"
import json; p = 'WORKSPACE/instances/$INSTANCE/config.json'
with open(p) as f: d = json.load(f)
models = d.get('models', {})
print('$INSTANCE', 'fast=', models.get('fastReasoning'), 'deep=', models.get('deepReasoning'))
assert models.get('fastReasoning') in ('gpt-5.4-mini', 'claude-haiku-4-5')
assert models.get('deepReasoning') in ('gpt-5.4', 'claude-sonnet-4-6')
\""
done
```

**Apply per-platform livetest overrides** — see the "Post-first-M0: Livetest
Config Overrides (per-platform)" section above. Write `enableExtractionBufferLog`
and `chunk_tokens=1500` into each platform's config (OC, CC, CDX); do NOT write
`inactivityTimeoutMinutes` globally. The M4 idle-timeout flip is per-platform
and only around M4 on the lane running that milestone.

---

## Step 4 — Run milestones (Parallel)

Send start signals to all three tester windows after M0 passes on all platforms.
All three run simultaneously. The run is not complete until every lane has passed
every milestone in the guide.

The current milestone set lives under `tests/livetest/livetest-guide/`. Run
`ls tests/livetest/livetest-guide/` for the authoritative list — milestones get
added over time; do not hard-code a range in handoff prompts to testers. Per-
milestone definitions are in the individual files (`M0.md`, `M1.md`, ...). Tester
skill doc + platform supplements hold cross-cutting rules.

### The prime directive

A failure is a signal. Before writing any code in response to a failure, ask:

> "Does this fix make the system more correct, or does it make the test easier to pass?"

If the latter — stop. Wrong responses to failures:
- Relaxing a criterion because it is hard to satisfy
- Hardcoding values that mask a real derivation failure
- Skipping a safety check because it causes a timeout
- Ruling PASS-WITH-NOTE to avoid doing work

### Foundational-milestone FAIL halts the lane

Milestones are ordered foundational-first. **M1 (supervisor/monitor)** and
**M2 (extraction)** are load-bearing for every milestone below them.
A real FAIL at M1 or M2 **halts that lane**. Do not brief the tester past
the failed milestone. Every subsequent "PASS" against an empty / corrupt
DB is artifact of no content — recall returns nothing to rank, silo
separation holds trivially, notifications fire against empty state, and
you've generated false green signal that buries the actual blocker.

Concrete rules:

- On M1 or M2 FAIL, post HALT to the tester. Do not send the next-milestone
  brief.
- Set the dashboard for every downstream milestone to
  `INCONCLUSIVE-post-M<N>-fail`, not PASS. Do not carry forward prior
  readings — DB state has changed.
- Route bug URGENT, not async. Foundational bugs don't wait for "when
  convenient."
- Other lanes may continue in parallel. GLOBAL and XP still wait on all
  lanes at M7.
- After the fix lands, hot-deploy and **retry from the FAILED milestone**
  (usually M2 Part A), not from where the tester happened to be when
  halted.
- Quality PWNs at M3+ (recall ranking drift, agent-behavior initiative,
  minor doc-dated tolerances) are NOT covered by this rule — those are
  downstream quality notes that don't invalidate the foundation.

### Watchpoints — things to actively check for

- **Tester must STATUS after every milestone.** Every pass, PWN, or fail ruling
  results in one STATUS (or ISSUE) message to the coordinator mailbox. This is
  a MUST, not a SHOULD. If you advance a tester to the next milestone without
  having received a STATUS from it, something went wrong — either the tester
  made the ruling internally but did not post, or the mailbox notification did
  not surface. In either case, stop and reconcile before advancing.
- **Record dashboard timing per cell.** When briefing a lane for a milestone,
  run `tests/livetest/scripts/livetest-dashboard-cell.sh start <LANE> <MILESTONE>`.
  When grading it, run
  `tests/livetest/scripts/livetest-dashboard-cell.sh finish <LANE> <MILESTONE> <STATUS>`.
  The finish step writes cells like `PASS 5m` or `PASS-PWN 12m`; use
  `start --force` for explicit retests that should overwrite a closed cell.
- **Tester must not auto-advance.** After a milestone passes the tester must
  wait for coordinator `ACK + next milestone` before moving on. If you see a
  tester starting the next milestone on its own, rein it in and require
  explicit boundaries — otherwise you lose the ability to gate on fix-deploys
  between milestones.
- **CC requires a session-capture proof before M2.** A green launch screen is
  not enough. Before M2, require evidence that Claude created a fresh transcript
  under `~/.claude/projects/-tmp-cc-livetest/*.jsonl` after the first real user
  message. If no fresh JSONL exists, halt the CC lane immediately: hooks may be
  installed, but Quaid has no session input to extract.
- **Silence is not passing.** If a tester has been quiet past the expected
  duration for a milestone (see per-milestone expected windows in the guide),
  nudge it for a STATUS before assuming work is in flight. A common failure
  mode is the tester hangs on a subprocess and posts nothing.
- **Mailbox notification dedupe.** The mailbox suppresses repeat notifications
  until you ACK the prior item. If notifications go quiet unexpectedly, check
  `tmux-mailbox.sh status "$COORDINATOR_PANE"` — there may be an un-ACKed
  item holding the queue.

### Coordinator responsibilities during the run

- Monitor the mailbox, not just the pane scrollback. Handle one pending item at a time:
  the first unread item lands inline when the queue goes from empty to non-empty.
- Do not tail or inspect `tests/livetest/scripts/.tmux-mailbox/*` directly. Those files are
  internal storage, not the operator interface.
- If you think the mailbox is stalled, use the mailbox commands only:
  `tests/livetest/scripts/tmux-mailbox.sh status "$COORDINATOR_PANE"`
  then `tests/livetest/scripts/tmux-mailbox.sh next "$COORDINATOR_PANE"`.
- After you have handled an item, use one command to advance the queue:
  `tests/livetest/scripts/tmux-mailbox.sh reply "$COORDINATOR_PANE" <message-id> "retry now"`
- If no response is needed, mark it handled and pull the next item in one step:
  `tests/livetest/scripts/tmux-mailbox.sh done "$COORDINATOR_PANE" <message-id>`
- Start the mailbox watcher at run start so stalled queues get nudged automatically:
  `tests/livetest/scripts/tmux-mailbox.sh start-watch "$COORDINATOR_PANE"`
- Stop it only when the run is over:
  `tests/livetest/scripts/tmux-mailbox.sh stop-watch`
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

Run after all three platforms reach M16 PASS. Full procedure in
`tests/livetest/livetest-guide/` under "Cross-Platform Project Linking Test."

XP tests that all three platforms can share a project and recall each other's docs.

---

## Step 5b — GLOBAL (Janitor Review Cycle)

Run after ALL lanes complete XP. CDX is the sole apply lane; CC and OC stand down.
Full procedure in `tests/livetest/livetest-guide/GLOBAL.md`.

**🔥 GLOBAL apply command pitfall:** The apply command in the GLOBAL brief Step 4 MUST be:
```bash
ssh REMOTE_HOST "QUAID_HOME=~/.quaid QUAID_INSTANCE=CDX_INSTANCE ~/.quaid/plugins/quaid/quaid janitor --task all --apply --approve"
```

Do NOT omit `--apply --approve`. Using `quaid janitor --task all` alone (without the flags)
only queues a supervisor dry-run; the actual LLM review pass never runs and the tester sees
all-zeros stats in stdout. The tester must then poll `quaid janitor --status` or tail
`~/.quaid/instances/*/logs/janitor.log` for `janitor_complete` to get actual results.

The apply runs in the background and returns quickly with a request ID. Budget 5–30 min
for the background apply to complete depending on pending row volume.

---

## Step 6 — End-of-Run Check

```bash
cd /path/to/quaid && git log --oneline RUN_START_SHA..HEAD
```

### Case A — Zero new commits

Full suite passed with no code changes.

1. Push main:
   ```bash
   cd /path/to/quaid && ./scripts/push-main.sh github
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

**Before starting a new loop run: validate patches on the current VM first.**

The VM is already provisioned and all instances are warm. Do not burn a full
milestone suite to verify a single bug fix. Instead:

1. Direct W1 (codex-dev) and W3 (codex-bench) to reproduce each bug on the
   current VM and validate the fix in place — targeted, not full suite.
2. Only after targeted validation passes on the live VM: stage the commit.
3. Then proceed to the loop run for clean full-suite verification.

This applies to all bugs filed after the run — infrastructure bugs to W1,
recall/quality bugs to W3.

Once targeted validation is complete and commits are staged:

1. Build runtime:
   ```bash
   cd modules/quaid && npm run build:runtime
   ```
2. Push main (use `./scripts/push-main.sh github`, not raw `git push`).
3. Deploy to remote (rsync as above).
4. Log all new commits to `unreviewed-commits.md` under a new run section.
5. Print the end-of-run report (see **End-of-Run Report** below).
6. **Default behavior (`loop: false`):** Stop. Tell the user the run required
   commits and recommend a follow-up run to verify the fixes are clean.
7. **Loop mode only (`loop: true` in config):** Return to Step 2 and start the
   next run with the new HEAD as RUN_START_SHA.

---

## Post-Test Examination (after all milestones, before end-of-run report)

Run this after all platforms have completed every milestone in `tests/livetest/livetest-guide/` (run `ls` there for the current list) plus `XP.md`.
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
   prefixes (e.g. "anthropic/claude-sonnet-4-6"), gateway URLs, port numbers.
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
