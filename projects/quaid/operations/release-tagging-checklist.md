# Release Tagging Checklist

Replace `<version>` below with the actual release tag (e.g. `v0.3.0-alpha`).

## 1) Pre-tag checks
- Run deterministic + integration + syntax gates.
- Run focused regression suite:
  - `tests/test_providers.py`
  - `tests/test_soul_snippets.py`
- Confirm a full live-suite clear exists for the release-target SHA, or that the
  post-clear delta has been explicitly approved.
- Confirm the configured `paths.devRoot` checkout is clean (`git status`).

## 2) Docs and messaging
- Confirm README + roadmap match current release posture.
- Confirm known limitations are explicit:
  - parallel session edge cases
  - multi-user not fully hardened
  - Windows lightly tested
  - platform maturity/behavior differences across OpenClaw, Claude Code, and Codex
- Review release notes: `docs/releases/<version>.md`.
- Draft the release post from `docs/releases/RELEASE-POST-TEMPLATE.md`.
- Get approval on the exact public release-post copy before publishing it anywhere.

## 3) Version + tag
- Create annotated tag:
  - `git tag -a <version> -m "Quaid <version>"`
- Push branch + tag:
  - `git push origin release/0.3`
  - `git push origin <version>`

## 4) GitHub release
- Create release from tag `<version>`.
- Paste notes from `docs/releases/<version>.md`.
- Mark as a stable public release.

## 5) Post-release
- Publish the approved release post with links to the GitHub release and release notes.
- Open follow-up tracking issue for top alpha hardening work.
- Confirm bootstrap repo updates are also pushed from the configured
  `paths.developmentDirectory/bootstrap` checkout (separate repo).
