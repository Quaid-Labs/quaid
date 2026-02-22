# Project: Quaid

## Purpose
Quaid is the knowledge layer and documentation system plugin for OpenClaw. It captures personal facts, recalls them with hybrid search, and keeps documentation current through project-aware indexing and janitor maintenance.

This file is the canonical entry point for all docs in `projects/quaid`.

## Core Runtime Docs

| File | Summary |
|---|---|
| `projects/quaid/AGENTS.md` | Runtime operating guide for the agent: three-layer memory model, when to use memory tools, and extraction behavior. |
| `projects/quaid/TOOLS.md` | Tooling and config reference used during runtime: memory tools, CLI commands, provider-switch validation, and doc pointers. |
| `projects/quaid/CONSTITUTION.md` | Philosophical and architectural constitution for Quaid’s core files and evolution model. |
| `projects/quaid/SOUL.md` | Guidance for what belongs in SOUL journaling and how entries should be written. |
| `projects/quaid/USER.md` | Guidance for user-understanding journaling (patterns, sensitivity, tone boundaries). |
| `projects/quaid/MEMORY.md` | Guidance for shared-moment/world-understanding journaling entries. |

## Technical Reference Docs

| File | Summary |
|---|---|
| `projects/quaid/reference/adapter-provider-architecture.md` | Provider abstraction contract: only adapter/provider + config are provider-aware; core calls deep/fast tiers only. |
| `projects/quaid/reference/memory-local-implementation.md` | Implementation-level reference for hooks, extraction flow, recall flow, config shape, and module boundaries. |
| `projects/quaid/reference/memory-system-design.md` | High-level system design, layer boundaries, lifecycle, and design principles. |
| `projects/quaid/reference/memory-schema.md` | SQLite schema reference for nodes/edges/FTS/indexes/project registry structures. |
| `projects/quaid/reference/memory-deduplication-system.md` | Store-time and janitor dedup pipeline, thresholds, and merge safety behavior. |
| `projects/quaid/reference/janitor-reference.md` | Nightly janitor pipeline reference: schedule, tasks, locking, and operational behavior. |
| `projects/quaid/reference/memory-operations-guide.md` | Operational handbook for memory/docs lifecycle and day-to-day usage. |
| `projects/quaid/reference/projects-cli-reference.md` | Quick CLI reference for project registry and docs search/update commands. |

## Operations & Testing Docs

| File | Summary |
|---|---|
| `projects/quaid/operations/project_onboarding.md` | Agent workflow for project discovery, review, and project registration. |
| `projects/quaid/operations/projects-testing.md` | End-to-end test plan for project-system behavior via agent workflows. |
| `projects/quaid/operations/testing-infrastructure.md` | Unified test stack reference: deterministic/unit/integration/build layers, e2e scripts, and pass/fail rubric. |
| `projects/quaid/operations/bootstrap-runtime-ops.md` | Bootstrap/runtime ownership model, key placement policy, and safe automation defaults. |
| `projects/quaid/operations/release-readiness.md` | Alpha release positioning, current go/hold criteria, known issues, and contributor-focused priorities. |
| `projects/quaid/operations/release-tagging-checklist.md` | Step-by-step release/tag/publish checklist for alpha cuts. |
| `projects/quaid/operations/repo-cleanup.md` | Repo hygiene log: removed OpenClaw carryover artifacts and pending scrub tasks. |

## Source Code
- `plugins/quaid/`

## Update Rule
When adding or moving any project doc under `projects/quaid`, update this index in the same change.
