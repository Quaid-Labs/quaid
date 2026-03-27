# Quaid Live Test Guide

This tracked file is intentionally public-safe and generic.

Machine-specific live-test procedures must not be committed here. Keep private
hostnames, operator identities, absolute machine paths, pane layouts, and
destructive command sequences in an untracked companion file such as
`modules/quaid/tests/LIVE-TEST-GUIDE.local.md`.

## Public Contract

- Run live validation from `canary`.
- Use real host adapters and visible interaction panes.
- Do not mock runtime behavior or patch code during the live run.
- Treat failures as product bugs, not as reasons to relax the milestone.
- Prefer relative repo paths rooted at the configured `devRoot`.

## Local Setup

- Copy `.quaid-dev.example.json` to `.quaid-dev.local.json`.
- Set `paths.devRoot`, `paths.runtimeWorkspace`, and `paths.openclawSource`.
- Set `liveTest.remoteHost` and `liveTest.remoteWorkspace` for your lab host.
- Keep any machine-specific install, wipe, restart, SSH, or tmux commands in
  `LIVE-TEST-GUIDE.local.md` or another ignored local note.

## Minimum Reporting

- Send milestone failures with the exact failing command and first error lines.
- Re-run a failed milestone after any fix.
- Record final live-test results in a sanitized log entry without private host or
  operator details.

## Compatibility Update Rule

- Treat compatibility as a live-test output, not as a separate matrix promise.
- Only update `compatibility.json` after a full live clear.
- Record host clears separately for `Quaid/OpenClaw` and `Quaid/Claude Code`.
- Use `node scripts/record-compatibility-clear.mjs` to write the current Quaid
  `HEAD` SHA as the pending `quaid_range` for the cleared host pair.
- Pass `--install-verified true` only if M0/install completed cleanly without
  manual config patching.
- Release flow will rewrite the SHA to the real release version only if the
  cleared SHA still matches release `HEAD`, and it will keep a `validated_sha`
  marker so later release runs can detect stale clears.
- Do not update compatibility entries for partial clears, failed runs, or
  single-adapter-only validation.
