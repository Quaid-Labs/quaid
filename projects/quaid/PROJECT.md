# Project: Quaid

## Purpose
Quaid is an active knowledge layer for agentic systems. It captures personal facts and project context, recalls them with hybrid search, keeps documentation current through project-aware indexing, and maintains knowledge health via nightly janitor maintenance. Supports multiple adapters (OpenClaw, Claude Code) sharing a single QUAID_HOME on the same machine.

This file is the canonical entry point for all docs in `projects/quaid`.

## Core Runtime Docs

| File | Summary |
|---|---|
| `projects/quaid/AGENTS.md` | Runtime operating guide for the agent: three-layer memory model, when to use memory tools, and extraction behavior. |
| `projects/quaid/TOOLS.md` | Runtime tool-usage guide: what to call, when to call it, practical recall patterns, project commands, and domain filter map. |
| `projects/quaid/CONSTITUTION.md` | Philosophical and architectural constitution for Quaid’s core files and evolution model. |
| `projects/quaid/SOUL.md` | Guidance for what belongs in SOUL journaling and how entries should be written. |
| `projects/quaid/USER.md` | Guidance for user-understanding journaling (patterns, sensitivity, tone boundaries). |
| `projects/quaid/MEMORY.md` | Guidance for shared-moment/world-understanding journaling entries. |

## Technical Reference Docs

| File | Summary |
|---|---|
| `projects/quaid/reference/adapter-provider-architecture.md` | Provider abstraction contract: only adapter/provider + config are provider-aware; core calls deep/fast tiers only. |
| `projects/quaid/reference/memory-local-implementation.md` | Implementation-level reference for hooks, extraction flow, recall flow, config shape (incl. 3-layer merge chain), instance management, and module boundaries. |
| `projects/quaid/reference/memory-system-design.md` | High-level system design, layer boundaries, lifecycle, and design principles. |
| `projects/quaid/reference/memory-schema.md` | SQLite schema reference: nodes/edges/FTS/indexes, doc_registry, doc_chunks, project_definitions. |
| `projects/quaid/reference/memory-deduplication-system.md` | Store-time and janitor dedup pipeline, thresholds, and merge safety behavior. |
| `projects/quaid/reference/janitor-reference.md` | Nightly janitor pipeline reference: schedule, tasks, 3-pass RAG reindex, locking, and operational behavior. |
| `projects/quaid/reference/memory-operations-guide.md` | Operational handbook for memory/docs lifecycle and day-to-day usage. |
| `projects/quaid/reference/projects-cli-reference.md` | CLI quick-reference: project registry, doc registry, cross-instance workflow, shared QUAID_HOME model. |
| `projects/quaid/reference/extraction-pipeline.md` | **New.** Full extraction pipeline reference: triggers, signal protocol, 14-step pipeline, all 7 hooks, daemon lifecycle, public API. |
| `projects/quaid/reference/config-instances.md` | **New.** Config system and instance model: 4-layer merge chain, per-instance layout, shared state, CLI reference, multi-instance patterns. |
| `projects/quaid/reference/projects-system.md` | **New.** Projects system internals: two-registry model, full lifecycle (create/link/unlink/delete), shadow git, project updater, sync engine. |
| `projects/quaid/reference/rag-docs-system.md` | **New.** RAG and docs system: indexing pipeline, chunking, search, staleness detection, doc auto-update, event processing, CLI reference. |
| `projects/quaid/reference/hooks-session-lifecycle.md` | **New.** Hook entry points and session lifecycle: all 7 hooks with stdin/stdout schemas, CC/OC hook wiring, signal system, subagent registry, notification model. |

## Operations & Testing Docs

| File | Summary |
|---|---|
| `projects/quaid/operations/project_onboarding.md` | Agent workflow for project discovery, registration, cross-instance participation (link/unlink), and safe deletion. |
| `projects/quaid/operations/projects-testing.md` | Live test protocol: OC CRUD + CC CRUD + cross-platform (XP) on example.local with shared QUAID_HOME. |
| `projects/quaid/operations/live-validation-log.md` | Live validation log: M0–M7 results per adapter version, projects system test results, bugs found and fixed. |
| `projects/quaid/operations/testing-infrastructure.md` | Unified test stack reference: deterministic/unit/integration/build layers, e2e scripts, and pass/fail rubric. |
| `projects/quaid/operations/bootstrap-runtime-ops.md` | Bootstrap/runtime ownership model, key placement policy, and safe automation defaults. |
| `projects/quaid/operations/release-readiness.md` | Alpha release positioning, current go/hold criteria, known issues, and contributor-focused priorities. |
| `projects/quaid/operations/release-checklist.md` | Canonical go/no-go release checklist used for final readiness verification. |
| `projects/quaid/operations/release-tagging-checklist.md` | Step-by-step release/tag/publish checklist for alpha cuts. |
| `projects/quaid/operations/repo-cleanup.md` | Repo hygiene log: removed OpenClaw carryover artifacts and pending scrub tasks. |
| `projects/quaid/operations/benchmark-remediation-checklist.md` | Benchmark remediation execution checklist and janitor/doc verification steps. |
| `projects/quaid/operations/plugin-framework-checklist.md` | Plugin framework readiness checklist and rollout guardrails. |
| `projects/quaid/operations/cli-audit.md` | CLI command/audit notes for operational correctness and UX consistency. |

## Source Code
- `modules/quaid/`

## Update Rule
When adding or moving any project doc under `projects/quaid`, update this index in the same change.
