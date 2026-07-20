# Live Test Milestone Guide

Per-milestone definitions for the Quaid live test suite. Testers read only the
milestone file they are about to execute — not the full suite — so context
stays small.

Milestones consolidate tightly cross-fed surfaces into single multi-part
files. When several parts of the system exercise the same trigger, storage,
or invariant, they are documented together as one milestone with explicit
Parts.

## How to use

1. Coordinator briefs tester on which platform, tmux pane, and coordinator
   address to use (see `COORDINATOR.SKILL.md`).
2. Tester reads `TESTER.SKILL.md` + their platform supplement (`TESTER.OC.md`
   / `TESTER.CC.md` / `TESTER.CDX.md`) once at session start.
3. For each milestone, tester reads **only** the corresponding file below,
   executes every Part, verifies, and posts a STATUS (or ISSUE) to the
   coordinator.
4. After the last numbered milestone, `XP.md` runs on the first two lanes to
   finish. `GLOBAL.md` runs once at the very end of the suite, after every
   lane has passed M1–M7 and XP has completed.

## Milestones

`ls tests/livetest/livetest-guide/` is the source of truth for the current
milestone set. Milestones get added as coverage grows; this index is a
convenience snapshot, not an authoritative cap.

Current milestone order:

- `M0.md` — First Install
- `M1.md` — Supervisor and Monitor Runtime Stability
- `M2.md` — Extraction (lifecycle + rolling + timeout)
- `M3.md` — Recall (auto-inject + deliberate + graph + date-range)
- `M4.md` — Project System and Docs CLI
- `M5.md` — Silo Isolation
- `M6.md` — Agent Notifications (deferred + provider-outage)
- `M7.md` — System Context Refresh on Lifecycle
- `XP.md` — Cross-Platform Project Linking (first two lanes to finish)
- `GLOBAL.md` — Host-wide systems (janitor) — runs once at the very end

## Shared fixtures

Milestones reuse fixtures under `data/`:

- `data/rolling-transcript.md` — the canonical ~2000-token non-actionable
  user monologue driven through M2 Part B.
- `data/seed-historical.sh` — injects seven dated historical rows into the
  lane's `memory.db` for M3 Part D date-range probes.
- `data/test-project/` — the minimal "agentmsg" Python package used as M4's
  project fixture and as the XP shared project source.

## Authoring rules

- **One milestone per file.** Keep it self-contained.
- **Multi-part milestones list Parts with explicit PASS criteria up front.**
  A tester should be able to decide PASS / PWN / FAIL per Part without
  re-reading the whole file.
- **Platform specifics live in platform supplements, not in milestone files.**
  Milestone files describe the test; supplements describe how to invoke it
  on each platform.
- **Lines ≤ 100 chars** for narrative (tables and fenced code blocks are
  exempt).
- **No run-specific state.** Do not record the current VM IP, run number,
  or transient dashboard status in milestone files.

## Cross-cutting guidance

Content that applies across all milestones lives in:

- `../COORDINATOR.SKILL.md` — coordinator responsibilities, preflight flow,
  and post-M0 per-platform config. Timeout extraction setup lives in M2 Part C.
- `../TESTER.SKILL.md` — tester identity, core rules, test-integrity
  principles, reporting format.
- `../TESTER.OC.md` / `../TESTER.CC.md` / `../TESTER.CDX.md` — per-platform
  supplements defining `QCLI`, `SILO`, `LIFECYCLE`, and `SEND` for that
  lane, plus any platform-specific procedure variants.
- `../VM-SETUP.md` — VM / base-image notes.
