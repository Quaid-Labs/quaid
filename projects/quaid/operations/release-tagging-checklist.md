# Release Tagging Checklist

Replace `<version>` below with the actual release tag (e.g. `v0.2.15-alpha`).

## 1) Pre-tag checks
- Run deterministic + integration + syntax gates.
- Run focused regression suite:
  - `tests/test_providers.py`
  - `tests/test_soul_snippets.py`
- Run e2e bootstrap flow at least once with quiet notifications.
- Confirm the configured `paths.devRoot` checkout is clean (`git status`).

## 2) Docs and messaging
- Confirm README + roadmap match current alpha posture.
- Confirm known limitations are explicit:
  - parallel session edge cases
  - multi-user not fully hardened
  - Windows lightly tested
  - OpenClaw-first maturity
- Review release notes: `docs/releases/<version>.md`.

## 3) Version + tag
- Create annotated tag:
  - `git tag -a <version> -m "Quaid <version>"`
- Push branch + tag:
  - `git push origin canary`
  - `git push origin <version>`

## 4) GitHub release
- Create release from tag `<version>`.
- Paste notes from `docs/releases/<version>.md`.
- Mark as pre-release/alpha.

## 5) Post-release
- Open follow-up tracking issue for top alpha hardening work.
- Confirm bootstrap repo updates are also pushed from the configured
  `paths.developmentDirectory/bootstrap` checkout (separate repo).
