# Quaid Live Test — Tart VM Setup Guide

Using a Tart VM on the local machine instead of a physical remote host (e.g. alfie)
gives a truly blank state for each run and instant wipe-by-destroy.

## Architecture

```
Coordinator (this machine)
  ├── tart VM: quaid-livetest-run  ←─ clone of base per run
  │     ├── openclaw (OC platform)
  │     ├── claude / claude-code (CC platform)
  │     └── codex (CDX platform)
  └── livetest-vm.sh manages VM lifecycle
```

Each run:
1. `livetest-vm.sh start` — clones base in ~5s, boots (~30s), waits for SSH
2. Patches `livetest-config.json` with the VM's IP
3. Run M0–M15 + XP normally (preflight, wipe, install, milestones)
4. `livetest-vm.sh stop` — destroys run clone, restores config

The base image holds credentials (OC login, Codex login) across runs.

---

## One-Time Base Image Setup

### Option A: Use existing test-openclaw VM (fastest)

If `test-openclaw` already has the CLIs and credentials installed:

```bash
# Inspect what's already there
tart list

# If it looks good, rename/clone it as the base
tart clone test-openclaw quaid-livetest-base
```

Then boot and verify:
```bash
tart run --no-graphics quaid-livetest-base &
# wait ~30s
VM_IP=$(tart ip quaid-livetest-base)
ssh admin@$VM_IP 'openclaw --version; codex --version; claude --version'
```

### Option B: Build from Sequoia OCI image (clean slate)

```bash
./livetest-vm.sh setup-base
# This clones ghcr.io/cirruslabs/macos-sequoia-base:latest → quaid-livetest-base
# Then prints setup instructions
```

After cloning, boot the VM with graphics to complete setup:
```bash
tart run quaid-livetest-base
```

Inside the VM (using the GUI), complete these steps:

**1. Enable SSH**
System Settings → General → Sharing → Remote Login → On

**2. Add coordinator SSH key**
```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
# Paste your coordinator machine's ~/.ssh/id_ed25519.pub (or id_rsa.pub):
echo 'ssh-ed25519 AAAA... your-key' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Test from coordinator:
```bash
VM_IP=$(tart ip quaid-livetest-base)
ssh admin@$VM_IP 'echo ok'
```

**3. Install platform CLIs**
```bash
# Python 3.10+ (macOS ships with 3.9 which is too old for the Quaid installer)
# brew installs its own python3 (latest); reinstall sqlite-vec after any brew python upgrade
brew install python3

# sqlite-vec (required by Quaid for vector retrieval)
# --break-system-packages needed for brew-managed Python (PEP 668)
python3 -m pip install --user sqlite-vec --break-system-packages

# OpenClaw
npm install -g openclaw@latest  # or follow openclaw install procedure

# Codex
npm install -g @openai/codex@latest
# or: brew install codex (if available)

# Claude Code
npm install -g @anthropic-ai/claude-code@latest
# or follow claude install guide
```

**4. Authenticate each CLI**
```bash
openclaw auth login   # or openclaw login
codex auth login      # or follow Codex OAuth flow
# claude uses the Anthropic API key; set ANTHROPIC_API_KEY or configure via CLI
```

**5. Create quaid workspace directory**
```bash
mkdir -p ~/.quaid ~/quaid
```

**6. Verify**
```bash
openclaw --version
codex --version
claude --version
openclaw status   # should show authenticated
```

**7. Shut down cleanly**
```bash
sudo shutdown -h now
```

The VM is now your credential snapshot. Any run that clones this base inherits all
auth state without re-login.

---

## Keeping the Base Up To Date

When platform CLIs release updates:

```bash
./livetest-vm.sh upgrade-base
```

This:
1. Clones the base to a temporary upgrade VM
2. Boots it and gives you an SSH prompt to do updates
3. On confirmation, saves the updated clone as the new base
4. Keeps the old base as a dated backup (delete when confirmed good)

Typical upgrade session:
```bash
ssh admin@<vm-ip>
openclaw update
npm install -g @openai/codex@latest
npm install -g @anthropic-ai/claude-code@latest
python3 -m pip install --user --upgrade sqlite-vec --break-system-packages
exit
```
Then press ENTER in the upgrade script.

Base image tracking note:
- Keep Codex on the current release; Quaid's Codex hook integration expects the native binary's CamelCase hook event names.
- Do not force-pin an older Codex build in the base image or preflight automation unless a newer release regresses hook behavior again.

---

## Per-Run Workflow

```bash
# Start a fresh VM (clones base, boots, patches config)
./tests/livetest/scripts/livetest-vm.sh start

# Run preflight (now targets VM IP automatically)
./tests/livetest/scripts/livetest-preflight.sh

# ... run milestones M0–M15 + XP ...

# Destroy the VM when done
./tests/livetest/scripts/livetest-vm.sh stop
```

Or just get the IP of the running VM:
```bash
./tests/livetest/scripts/livetest-vm.sh ip
```

Show status:
```bash
./tests/livetest/scripts/livetest-vm.sh status
```

---

## Safety Notes

- The VM is destroyed after each run — no contamination carries over
- `livetest-vm.sh stop` restores `remote.host` in `livetest-config.json` to the original value
- The preflight remote-≠-local safety check passes naturally: the VM has a different IP
  (192.168.64.x range) and a different hostname from the coordinator
- Do NOT run `livetest-vm.sh start` if a run VM is already active — it will destroy the stale VM first

---

## Existing VMs on This Machine

As of initial setup, the following Tart VMs exist:
- `test-openclaw` — prior test VM (OC testing)
- `test-openclaw-als` — prior test VM (ALS testing)
- `ghcr.io/cirruslabs/macos-sequoia-base:latest` — clean Sequoia OCI cache

The recommended base for live tests is `quaid-livetest-base` (create from one of the above).

---

## Troubleshooting

**VM takes >120s to get an IP:**
- Check `tart list` — is it in "running" state?
- Try `tart run quaid-livetest-run` (with graphics) to see boot errors

**SSH refused after IP appears:**
- SSH may not be enabled in the base image (complete step 1 in setup)
- Check firewall: `sudo /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate`

**"No such file" errors during preflight code sync:**
- The Quaid roots in the VM must exist: `ssh admin@<ip> 'mkdir -p ~/.quaid ~/quaid'`
- Re-run preflight; it will rsync the code on next attempt

**VM IP changes between boots:**
- Tart assigns IPs via DHCP; the IP changes each time
- This is fine — `livetest-vm.sh start` always reads the current IP and patches the config
