# Release Tagging Checklist (v0.20.0-alpha)

## 1) Pre-tag checks
- Run deterministic + integration + syntax gates.
- Run focused regression suite:
  - `tests/test_mcp_integration.py`
  - `tests/test_providers.py`
  - `tests/test_soul_snippets.py`
- Run e2e bootstrap flow at least once with quiet notifications.
- Confirm `~/quaid/dev` is clean (`git status`).

## 2) Docs and messaging
- Confirm README + roadmap match current alpha posture.
- Confirm known limitations are explicit:
  - parallel session edge cases
  - multi-user not fully hardened
  - Windows lightly tested
  - OpenClaw-first maturity
- Review release notes: `docs/releases/v0.20.0-alpha.md`.

## 3) Version + tag
- Create annotated tag:
  - `git tag -a v0.20.0-alpha -m "Quaid v0.20.0-alpha"`
- Push branch + tag:
  - `git push origin dev`
  - `git push origin v0.20.0-alpha`

## 4) GitHub release
- Create release from tag `v0.20.0-alpha`.
- Paste notes from `docs/releases/v0.20.0-alpha.md`.
- Mark as pre-release/alpha.

## 5) Post-release
- Open follow-up tracking issue for top alpha hardening work.
- Confirm bootstrap repo updates are also pushed from `~/quaid/bootstrap` (separate repo).
