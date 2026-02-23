# Release Workflow

Use this checklist before pushing release work to GitHub.

## Ownership Guard

Quaid release commits should be attributed to:

- `user.name`: `Solomon Steadman`
- `user.email`: `solstead@users.noreply.github.com`

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

## Optional: Git Hook

Install as a `pre-push` hook:

```bash
cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
set -euo pipefail
bash scripts/release-check.sh
HOOK
chmod +x .git/hooks/pre-push
```
