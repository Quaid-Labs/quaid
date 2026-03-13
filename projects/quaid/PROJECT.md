# Project: Quaid

Quaid is an active knowledge layer for agentic systems. It captures personal facts and project context from conversations, recalls them with hybrid search, keeps documentation current through project-aware indexing, and maintains knowledge health via nightly janitor maintenance.

Supports multiple adapters (OpenClaw, Claude Code) sharing a single `QUAID_HOME`.

---

## Runtime Files (always loaded)

| File | Role |
|------|------|
| `AGENTS.md` | Operating guide: memory lifecycle, retrieval rules, interrupt policy |
| `TOOLS.md` | CLI reference: all commands, flags, domains, playbooks |
| `SOUL.md` | Quaid's reflective journaling guidance |
| `USER.md` | User-understanding journaling: patterns, tone, sensitivity |
| `MEMORY.md` | Shared-moment and world-understanding journaling |
| `CONSTITUTION.md` | Architectural principles and core file evolution model |

---

## Memory System

| Doc | What it covers |
|-----|----------------|
| `reference/memory-reference.md` | **Primary reference.** Architecture, all modules, hooks, shared libs, SQLite schema DDL, config. |
| `reference/memory-deduplication-system.md` | Store-time and janitor dedup pipeline, thresholds, merge safety. |
| `reference/memory-operations-guide.md` | Day-to-day operations: storing, recalling, maintaining memory health. |
| `reference/janitor-reference.md` | Nightly janitor: schedule, tasks, 3-pass RAG reindex, locking, LifecycleRegistry. |
| `reference/extraction-pipeline.md` | 14-step extraction pipeline, all hooks, daemon lifecycle, public API. |
| `reference/config-instances.md` | 4-layer config merge chain, per-instance layout, shared state, multi-instance patterns. |

---

## Projects System

| Doc | What it covers |
|-----|----------------|
| `reference/projects-reference.md` | **Primary reference.** Two-registry model, full lifecycle, shadow git, sync engine, project updater, CLI. |
| `reference/rag-docs-system.md` | RAG indexing pipeline, chunking, search, staleness detection, doc auto-update. |
| `operations/project_onboarding.md` | Workflow for project discovery, registration, cross-instance participation, deletion. |
| `operations/projects-testing.md` | Live test protocol: OC CRUD + CC CRUD + cross-platform with shared QUAID_HOME. |

---

## Hooks & Adapters

| Doc | What it covers |
|-----|----------------|
| `reference/hooks-session-lifecycle.md` | All 7 hooks with stdin/stdout schemas, CC/OC hook wiring, signal system, notifications. |
| `docs/INTERFACES.md` | Adapter/provider contracts, provider abstraction, `QuaidAdapter` ABC. |
| `docs/ARCHITECTURE.md` | System architecture: config layers, TypeScript layer, adapter types, ModelConfig. |

---

## Operations & Release

| Doc | What it covers |
|-----|----------------|
| `operations/live-validation-log.md` | M0–M7 live validation results per adapter version. |
| `operations/testing-infrastructure.md` | Test stack: unit/integration/e2e layers, scripts, pass/fail rubric. |
| `operations/release-readiness.md` | Go/hold criteria, known issues, release positioning. |
| `operations/release-checklist.md` | Go/no-go checklist for release cuts. |
| `operations/release-tagging-checklist.md` | Step-by-step tag/publish checklist. |
| `operations/cli-audit.md` | CLI command audit notes. |
| `operations/repo-cleanup.md` | Repo hygiene log. |

---

## Source Code

- `modules/quaid/` — main plugin source
  - `adaptors/` — OpenClaw and Claude Code adapters
  - `core/` — extraction, lifecycle, LLM, runtime
  - `datastore/` — memorydb, docsdb, notedb
  - `lib/` — shared libraries
  - `tests/` — test suite

---

## Update Rule

When adding or moving any doc under `projects/quaid`, update this index in the same change.
