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
- Maintainer changes land on `main` only through the guarded CI push flow
- Tag/release only from commits already on `main`

## Required GitHub Protections

Enable branch protection on `main`:

- Require status checks to pass before merging
- Require conversation resolution before merging
- Restrict force pushes
- Restrict branch deletion

Maintainer pushes do not require a PR review. CI is the gate.

Apply via script:

```bash
node scripts/github-protect-main.mjs --repo quaid-labs/quaid
```

## Release Flow

1. Verify local state and docs:

```bash
bash scripts/release-check.sh
```

2. Run CI-validating checks if release-impacting changes are large:

```bash
cd modules/quaid
npm run test:all
```

3. Build release tarball:

```bash
cd ~/quaidcode/dev
./scripts/build-release-tarball.sh
```

4. Create/publish release from `main` tag.

## Hotfix Flow

- Patch on `release/<major.minor>` when the fix is release-line-specific.
- Patch on `main` when the fix should move both development and the active release line forward.
- Use `./scripts/push-main.sh github` for maintainer pushes to `main`.
- Tag patched release from the release branch after the fix is validated there.

## Rollback

If a bad release lands:

- Re-point users to prior release tag/asset
- Revert commit(s) on `main` with the guarded maintainer push flow (no history rewrite)
- Publish patch release with rollback notes

## Operator Hygiene

- Keep auth/provider credentials out of git-tracked files
- Prefer GitHub noreply email for public attribution
- Keep release notes accurate and explicit about known limitations
- Keep benchmark checkpoint cutting procedures in a separate workspace outside the plugin repo.
