# Repo Cleanup Log

Tracks repository hygiene actions while separating Quaid source from OpenClaw runtime artifacts.

## Completed

- Removed OpenClaw root markdown artifacts from repo root:
  - `AGENTS.md`, `HEARTBEAT.md`, `IDENTITY.md`, `MEMORY.md`, `SOUL.md`, `TOOLS.md`, `USER.md`
- Removed deprecated docs pointer file:
  - `docs/quaid/README.md`
- Removed artifact files that are not part of Quaid runtime or release templates:
  - `plugins/quaid/dashboard.html`
  - `projects/staging/dev-flow.md`
- Consolidated project docs so canonical docs live under `projects/quaid`.

## Relocated Outside `dev`

Moved GitHub-facing top-level docs to `~/quaid`:
- `~/quaid/README.md`
- `~/quaid/ROADMAP.md`
- `~/quaid/TODO.md`

Copied major GitHub reference docs to `~/quaid/docs/github/`:
- `AI-REFERENCE.md`
- `ARCHITECTURE.md`
- `BENCHMARKS.md`

## Pending Scrub Pass

- Sanitize personal/default-owner references in config/docs/tests where needed.
- Normalize stale path references to current `projects/quaid/reference/*` layout.
- Review release-template defaults for provider/model assumptions and naming drift.
- Decide whether `scripts/nightly-test/` remains in this repo or moves to dedicated infra repo.

## Rule

If a file is machine-local, user-local, gateway-runtime output, or migration scratch, it should not live in `dev` unless explicitly needed by build/release/test workflows.

## Dev/Test Policy

- `~/quaid/dev` is source-only (what gets committed/pushed).
- Run installs/tests in `~/quaid/test` (bootstrap/worktree runtime lane), not in `dev`.
- Keep dependency/runtime artifacts out of version control via root `.gitignore`.
