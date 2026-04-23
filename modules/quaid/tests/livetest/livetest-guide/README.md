# Live Test Milestone Guide

Per-milestone definitions for the Quaid live test suite. Testers read only the milestone file they are about to execute — not the full suite — so context stays small.

Milestones consolidate tightly cross-fed surfaces into single multi-part files. When several parts of the system exercise the same trigger, storage, or invariant, they are documented together as one milestone with explicit Parts.

## How to use

1. Coordinator briefs tester on which platform, tmux pane, and coordinator address to use (see `COORDINATOR.SKILL.md`).
2. Tester reads `TESTER.SKILL.md` + their platform supplement (`TESTER.OC.md` / `TESTER.CC.md` / `TESTER.CDX.md`) once at session start.
3. For each milestone, tester reads **only** the corresponding file below, executes every Part, verifies, and posts a STATUS (or ISSUE) to the coordinator.
4. After the last milestone, tester reads `XP.md` if their lane participates in cross-platform.

## Milestones

- [**M0 — Agent-Driven Install**](M0.md)
- [**M1 — Extraction**](M1.md) — lifecycle signals (`/new`, `/reset`, `/clear`, `/compact`) + rolling extraction + timeout extraction.
- [**M2 — Recall**](M2.md) — auto-inject + deliberate recall + graph traversal + date-range recall.
- [**M3 — Project System + Docs CLI**](M3.md) — project CRUD + `quaid docs`/`doctor`/`stats` CLIs.
- [**M4 — Janitor + Generated Artifacts**](M4.md) — janitor review cycle + snippet/journal/PROJECT.log generation.
- [**M5 — Silo Isolation**](M5.md) — multi-agent within one instance + multi-instance separation.
- [**M6 — Agent Notifications**](M6.md) — deferred notice surfacing + provider-outage fast path.
- [**M7 — System Context Refresh on Lifecycle**](M7.md)
- [**M8 — Supervisor and Monitor Runtime Stability**](M8.md)
- [**XP — Cross-Platform Project Linking Test**](XP.md)

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
