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

Quaid release commits must always use this exact public-safe identity:

- `Solomon Steadman <168413654+solstead@users.noreply.github.com>`

`scripts/release-owner-check.mjs` enforces that exact identity. Do not override
it with a different name or email in git config, `.quaid-dev.local.json`, or
`QUAID_OWNER_*` env vars.

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

If you keep a repo-local development config, it must use that same identity:

```bash
git config user.name "Solomon Steadman"
git config user.email "168413654+solstead@users.noreply.github.com"
node scripts/release-owner-check.mjs
```

```json
{
  "identity": {
    "releaseOwnerName": "Solomon Steadman",
    "releaseOwnerEmail": "168413654+solstead@users.noreply.github.com"
  }
}
```

If an unpushed commit was created with any other identity, amend or rewrite it
before any canary push or release step.

## Privacy Guard

Release and canary checks read local blocked markers from
`.quaid-dev.local.json` and fail if those values appear in:

- tracked files, or
- reachable git history

Populate `.quaid-dev.local.json` with any legacy leaked values under
`privacy.blockedStrings`, in addition to the live local values already present
in `paths`, `identity.telegramAllowFrom`, and `liveTest.remoteHost`.

Validate manually with:

```bash
node scripts/privacy-audit.mjs
```

If the privacy audit fails on reachable history, launch stays blocked until you:

1. rewrite the affected GitHub branch history,
2. force-push the rewritten branches, and
3. ask GitHub Support to purge cached blobs/search results for the removed data.

See [PRIVACY-HISTORY-SCRUB.md](./PRIVACY-HISTORY-SCRUB.md) for the rewrite and
support-purge runbook.

## Pre-Push Checklist

Run:

```bash
bash scripts/release-check.sh
```

Run this before the final release commit.
It may rewrite pending SHA-based compatibility rows in `compatibility.json`
to the concrete release version when the recorded live-clear SHA matches `HEAD`.

This runs:

1. docs consistency
2. privacy audit (tracked tree plus reachable git history against local blocked markers)
3. release evidence check (`unit` + `ci` + `xp` must be recorded against HEAD)
4. compatibility promotion (`openclaw` + `claude-code` live clears must exist, be install-verified, and match HEAD)
5. release metadata/version consistency
6. ownership/attribution verification
7. strict TypeScript/JavaScript runtime pair check
8. quaid release gate (targeted runtime regressions)

## Release Decision Flow

Treat release as a manual approval step, not as an automatic outcome of a green
script.

Flow:

1. Solomon says "let's do a release".
2. Run the full current test bar and fix issues as needed.
3. The full bar includes:
   - unit and CI gates
   - the full current live suite, as defined by [LIVE-TEST-GUIDE.md](../modules/quaid/tests/LIVE-TEST-GUIDE.md)
4. After the suite is green, compare the cleared live-test SHA against current
   `HEAD`.
5. Report the exact post-clear delta to Solomon as:
   - "ready for release, but these changes landed after the clear"
6. Solomon decides whether the delta is acceptable for release or whether more
   testing/work is required.

If Solomon approves a post-clear delta, record that local approval before the
final release check:

```bash
node scripts/release-approve-delta.mjs --notes "Solomon approved the post-clear release delta"
```

This writes `.release-approval.local.json` (ignored by git). It does not change
the cleared SHAs; it only tells the release checks that Solomon explicitly
approved releasing the current `HEAD` on top of those already-cleared commits.

Do not treat a partially cleared live run as release-ready.
Do not treat compatibility rows alone as release approval.

## Evidence Recording

Record unit and CI evidence after the corresponding gates pass:

```bash
node scripts/release-evidence.mjs record unit
node scripts/release-evidence.mjs record ci
node scripts/release-evidence.mjs record xp
```

Record host compatibility after the host lane is green and Solomon has reviewed
the clear run and accepted it as real:

```bash
node scripts/record-compatibility-clear.mjs --host openclaw --host-version 2026.3.23 --install-verified true
node scripts/record-compatibility-clear.mjs --host claude-code --host-version 2.1.72 --install-verified true
```

For live clears:

- `--install-verified true` means M0/install produced a clean silo with no manual patching.
- If a live run required a manual config patch, record it with `--install-verified false`; release promotion will block until the install path is re-cleared or manually accepted with a code change.
- Compatibility rows are only for host pairs:
  - `Quaid/OpenClaw`
  - `Quaid/Claude Code`
- XP is part of release readiness, but it does not create its own compatibility row.
- Live clears write the accepted cleared runtime SHA into `compatibility.json` on `canary`.
- If `HEAD` has already moved by the time the live lane records the clear, pass
  `--sha <cleared-runtime-sha>` so the row reflects what was actually tested.
- `bash scripts/release-check.sh` rewrites those SHA placeholders to the released Quaid version when the clear SHA still matches release `HEAD`, or when Solomon has locally approved the post-clear delta with `scripts/release-approve-delta.mjs`.
- Promoted rows keep a `validated_sha` marker, so future release runs can tell whether the current matrix is fresh or only reflects an older clear.
- If the cleared SHA is behind the intended release target, the live lane should
  record it after Solomon accepts the clear, then report the mismatch; release
  decides whether that delta needs a rerun or can be explicitly approved.

## E2E Policy

E2E is diagnostic coverage now, not release truth.

- Blocking release truth is:
  - unit evidence
  - CI evidence
  - XP evidence
  - a full current live-suite clear, as defined by [LIVE-TEST-GUIDE.md](../modules/quaid/tests/LIVE-TEST-GUIDE.md)
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
- refuses to push on top of a `canary` branch whose unique history still contains banned local/bot attribution
- scans tracked files for local/private markers before push
- runs the canary-safe identity/docs/runtime checks
- pushes only to `github canary`

## Tarball Build

Build installer artifact locally:

```bash
./scripts/build-release-tarball.sh
```

## Optional: Git Hook

Do not use `bash scripts/release-check.sh` as a generic `pre-push` hook for final
release work, because it can update tracked release metadata.

If you want a lightweight automated pre-push guard, use the non-mutating release
verification step instead:

```bash
cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
node scripts/release-verify.mjs
HOOK
chmod +x .git/hooks/pre-push
```

Keep machine-specific release notes, hostnames, and operator details in local
config or untracked notes rather than tracked repo scripts.
