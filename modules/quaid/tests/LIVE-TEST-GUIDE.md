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
- Do not call the run "release-ready" until the full current live suite is
  complete, including XP.

## Compatibility Update Rule

- Treat compatibility as a live-test output, not as a separate matrix promise.
- Only update `compatibility.json` after the full current live suite is green
  and Solomon has reviewed the clear run and accepted it as a real clear.
- Record host clears separately for `Quaid/OpenClaw` and `Quaid/Claude Code`.
- XP is part of release readiness, but it does not create its own compatibility row.
- Do not wait for release-SHA reconciliation once Solomon has accepted the clear.
- Use `node scripts/record-compatibility-clear.mjs` to write the cleared runtime
  SHA as the pending `quaid_range` for the host pair.
- If `HEAD` has already moved for docs, release, or other non-live work, pass
  `--sha <cleared-runtime-sha>` explicitly.
- Pass `--install-verified true` only if M0/install completed cleanly without
  manual config patching.
- Release flow will rewrite the SHA to the real release version only if the
  cleared SHA still matches release `HEAD`, and it will keep a `validated_sha`
  marker so later release runs can detect stale clears.
- Do not update compatibility entries for partial clears, failed runs, or
  single-adapter-only validation.
- If the cleared SHA is behind the intended release target, report that
  immediately so release can decide whether to rerun, approve the post-clear
  delta, or hold.
