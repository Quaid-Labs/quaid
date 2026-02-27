# Release Workflow

Use this checklist before pushing release work to GitHub.

## Safety Baseline

Before public usage, treat `main` as immutable history:

- No force pushes to `main`
- No history rewrites after publish
- Merge through PR + passing CI

Configure GitHub protection:

```bash
node scripts/github-protect-main.mjs --repo Steadman-Labs/quaid
```

## Ownership Guard

Quaid release commits should be attributed to:

- `user.name`: `solstead`
- `user.email`: `168413654+solstead@users.noreply.github.com`

Validate ownership/attribution on local commits:

```bash
node scripts/release-owner-check.mjs
```

This check verifies:

- local git identity matches expected owner
- commit author/committer in the push range matches expected owner
- commit messages do not include blocked co-author/bot tags

You can override expected values for a different release owner:

```bash
QUAID_OWNER_NAME="Your Name" \
QUAID_OWNER_EMAIL="you@users.noreply.github.com" \
node scripts/release-owner-check.mjs
```

## Pre-Push Checklist

Run:

```bash
bash scripts/release-check.sh
```

This runs:

1. docs consistency
2. release metadata/version consistency
3. ownership/attribution verification
4. strict TypeScript/JavaScript runtime pair check

## Tarball Build

Build installer artifact locally:

```bash
./scripts/build-release-tarball.sh
```

## Optional: Git Hook

This repo uses `core.hooksPath=git-hooks`. Ensure the tracked hook is executable:

```bash
chmod +x git-hooks/pre-push scripts/push-guard.sh
```

The push guard blocks accidental GitHub/main pushes by default:

- Allowed by default: `./scripts/push-backup.sh` (push to `checkpoint`)
- Blocked by default: `git push origin ...` / `git push github ...`
- Preferred release path: `./scripts/release-push.sh`
- One-shot manual override for intentional GitHub push:
  - `QUAID_RELEASE=1 git push github <refspec>`
