# Release Workflow

Use this checklist before pushing release work to GitHub.

## Safety Baseline

Before public usage, treat `main` as immutable history:

- No force pushes to `main`
- No history rewrites after publish
- Merge through PR + passing CI

Configure GitHub protection:

```bash
node scripts/github-protect-main.mjs --repo quaid-labs/quaid
```

## Ownership Guard

Quaid release commits should be attributed to the release owner configured in
local git config, `.quaid-dev.local.json`, or explicit `QUAID_OWNER_*` env vars.

Validate ownership/attribution on local commits:

```bash
node scripts/release-owner-check.mjs
```

This check verifies:

- local git identity matches expected owner
- commit author/committer in the push range matches expected owner
- release owner email is public-safe (local `.local` emails are rejected)
- commit messages do not include blocked co-author/bot tags
- commit messages do not include local-only host or email markers

You can override expected values for a different release owner:

```bash
QUAID_OWNER_NAME="Your Name" \
QUAID_OWNER_EMAIL="you@users.noreply.github.com" \
node scripts/release-owner-check.mjs
```

If you keep a repo-local development config, set the release owner there:

```json
{
  "identity": {
    "releaseOwnerName": "Your Name",
    "releaseOwnerEmail": "you@users.noreply.github.com"
  }
}
```

## Pre-Push Checklist

Run:

```bash
bash scripts/release-check.sh
```

This runs:

1. docs consistency
2. release evidence check (`unit` + `ci` must be recorded against HEAD)
3. compatibility promotion (`openclaw` + `claude-code` live clears must exist, be install-verified, and match HEAD)
4. release metadata/version consistency
5. ownership/attribution verification
6. strict TypeScript/JavaScript runtime pair check
7. quaid release gate (targeted runtime regressions)

## Evidence Recording

Record unit and CI evidence after the corresponding gates pass:

```bash
node scripts/release-evidence.mjs record unit
node scripts/release-evidence.mjs record ci
```

Record host compatibility only after a full live clear:

```bash
node scripts/record-compatibility-clear.mjs --host openclaw --host-version 2026.3.23 --install-verified true
node scripts/record-compatibility-clear.mjs --host claude-code --host-version 2.1.72 --install-verified true
```

For live clears:

- `--install-verified true` means M0/install produced a clean silo with no manual patching.
- If a live run required a manual config patch, record it with `--install-verified false`; release promotion will block until the install path is re-cleared or manually accepted with a code change.
- Live clears write the current `HEAD` SHA into `compatibility.json` on `canary`.
- `bash scripts/release-check.sh` rewrites those SHA placeholders to the released Quaid version only when the clear SHA still matches release `HEAD`.

## E2E Policy

E2E is diagnostic coverage now, not release truth.

- Blocking release truth is: unit evidence, CI evidence, OpenClaw full live clear, Claude Code full live clear.
- Keep E2E for nightly or warning-only coverage until it is removed or rebuilt.

## Canary Pushes

For `canary`, use the lighter guarded push path:

```bash
bash scripts/push-canary.sh
```

This script:

- refuses pushes from non-`canary` branches
- refuses `main` pushes
- requires a clean worktree
- scans tracked files for local/private markers before push
- runs the canary-safe identity/docs/runtime checks
- pushes only to `github canary`

## Tarball Build

Build installer artifact locally:

```bash
./scripts/build-release-tarball.sh
```

## Optional: Git Hook

Install a local `pre-push` hook if you want release checks to run automatically:

```bash
cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
bash scripts/release-check.sh
HOOK
chmod +x .git/hooks/pre-push
```

Keep machine-specific release notes, hostnames, and operator details in local
config or untracked notes rather than tracked repo scripts.
