# Live Test Milestone Guide

Per-milestone definitions for the Quaid live test suite. Testers read only the milestone file they are about to execute — not the full suite — so context stays small.

Milestones consolidate tightly cross-fed surfaces into single multi-part files. When several parts of the system exercise the same trigger, storage, or invariant, they are documented together as one milestone with explicit Parts.

## How to use

1. Coordinator briefs tester on which platform, tmux pane, and coordinator address to use (see `COORDINATOR.SKILL.md`).
2. Tester reads `TESTER.SKILL.md` + their platform supplement (`TESTER.OC.md` / `TESTER.CC.md` / `TESTER.CDX.md`) once at session start.
3. For each milestone, tester reads **only** the corresponding file below, executes every Part, verifies, and posts a STATUS (or ISSUE) to the coordinator.
4. After the last milestone, tester reads `XP.md` if their lane participates in cross-platform.

## Milestones

`ls tests/livetest/livetest-guide/` is the source of truth for the current milestone set. Milestones get added as coverage grows; this index is a convenience snapshot, not an authoritative cap. Any agent should enumerate the directory for itself before planning a run.

Current snapshot (run `ls` to confirm):

- `M0.md` — Agent-Driven Install
- `M1.md` — Extraction (lifecycle signals + rolling + timeout)
- `M2.md` — Recall (auto-inject + deliberate + graph + date-range)
- `M3.md` — Project System + Docs CLI
- `M4.md` — Janitor + Generated Artifacts
- `M5.md` — Silo Isolation (multi-agent + multi-instance)
- `M6.md` — Agent Notifications (deferred + provider-outage)
- `M7.md` — System Context Refresh on Lifecycle
- `M8.md` — Supervisor and Monitor Runtime Stability
- `XP.md` — Cross-Platform Project Linking Test

## Authoring rules

- **One milestone per file.** Keep it self-contained. Do not reference adjacent milestones by line number; reference by file link (e.g. `[M2.md](M2.md)`).
- **Multi-part milestones list Parts with explicit PASS criteria up front.** A tester should be able to decide PASS / PWN / FAIL per Part without re-reading the whole file.
- **Platform variants inline.** If a Part behaves differently on OC vs CC vs CDX, keep the variants side-by-side inside the same milestone file.
- **Procedure steps numbered.** Commands in fenced code blocks. Placeholders (e.g. `REMOTE_HOST`, `OWNER_NAME`, `<INSTANCE>`) resolved from the coordinator's opening message, not hard-coded.
- **No run-specific state.** Do not record the current VM IP, run number, or transient dashboard status in milestone files.

## Cross-cutting guidance

Content that applies across all milestones (core safety rules, coordinator run-loop, tester identity + reporting format, VM setup) lives in:

- `../LIVE-TEST-GUIDE.md` — rules-of-the-road + any not-yet-per-milestone content.
- `../COORDINATOR.SKILL.md` — coordinator responsibilities, preflight flow, post-M0 per-platform config, M4-lane timeout flip.
- `../TESTER.SKILL.md` — tester identity, core rules, reporting format.
- `../TESTER.OC.md` / `../TESTER.CC.md` / `../TESTER.CDX.md` — per-platform supplements.
- `../VM-SETUP.md` — VM/base image notes.
