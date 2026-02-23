# Maintainer Lifecycle (Post-User Safety)

This is the maintainer operating model for Quaid after public usage begins.

## Branch Model

- `main`: protected integration branch, always deployable
- `release/*`: optional short-lived stabilization branches when needed
- feature branches: contributor/maintainer work branches

## Hard Rules

- Do not force-push `main`
- Do not rewrite published history
- Do not delete `main`
- Merge via PR with passing CI
- Tag/release only from commits already on `main`

## Required GitHub Protections

Enable branch protection on `main`:

- Require pull request before merging
- Require status checks to pass before merging
- Require conversation resolution before merging
- Restrict force pushes
- Restrict branch deletion

Apply via script:

```bash
node scripts/github-protect-main.mjs --repo Steadman-Labs/quaid
```

## Release Flow

1. Verify local state and docs:

```bash
bash scripts/release-check.sh
```

2. Run CI-validating checks if release-impacting changes are large:

```bash
cd plugins/quaid
npm run test:all
```

3. Build release tarball:

```bash
cd /path/to/quaid/dev
./scripts/build-release-tarball.sh
```

4. Create/publish release from `main` tag.

## Hotfix Flow

- Branch from `main` (`hotfix/<topic>`)
- Minimal patch + tests + docs note
- PR merge to `main`
- Tag patched release

## Rollback

If a bad release lands:

- Re-point users to prior release tag/asset
- Revert commit(s) on `main` via PR (no history rewrite)
- Publish patch release with rollback notes

## Operator Hygiene

- Keep auth/provider credentials out of git-tracked files
- Prefer GitHub noreply email for public attribution
- Keep release notes accurate and explicit about known limitations
