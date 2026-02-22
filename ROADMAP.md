# Quaid Roadmap

> Alpha milestone: **v0.20** (Feb 2026)

Quaid is now in a stronger adapter/provider architecture with working extraction triggers, janitor flow, and integration/e2e harnesses. This is a major milestone, but still **alpha** software.

## Alpha Status (Read First)

Quaid is usable for active testing, but not yet production-hard in all scenarios.

### Known limitations
- **Parallel sessions**: `/new` and `/reset` extraction targeting still has edge cases under parallel conversation load.
- **Multi-user**: core supports owner scoping, but real-world multi-user hardening is incomplete.
- **Windows**: supported path exists, but coverage and operational testing are still limited.
- **Provider matrix maturity**: OpenClaw-hosted flows are most mature; broader host/runtime coverage is still being expanded.
- **Notification ergonomics**: noisy configurations can spam channels during automation if not set to quiet.

## What v0.20 Delivers
- Adapter/provider-first model routing (`deep_reasoning` / `fast_reasoning`) with core logic provider-agnostic.
- OpenClaw integration stabilized for key lifecycle signals (`/compact`, `/new`, `/reset`, `/restart`, timeout).
- Deterministic + integration + e2e testing framework in place.
- Janitor apply path validated in automated runtime flows.
- Cleaner repo separation (`dev` vs runtime/bootstrap operations).

## Next Commitments (High-Level)

### 1) Reliability and session correctness
- Make reset/new extraction fully parallel-session-safe (remove fallback heuristics).
- Harden timeout/worker singleton behavior across restarts and long-running sessions.
- Expand failure visibility with clean operator diagnostics (without debug-log spam).

### 2) Host integrations beyond OpenClaw
- Keep Quaid host-agnostic through adapter boundaries.
- Add/expand first-class integration paths for agentic systems like:
  - Codex-style runtimes
  - Claude Code-style runtimes
- Keep auth and model routing owned by host adapters, not core memory logic.

### 3) Code-focused operating mode
- Build a dedicated **code mode** tuned for software workflows:
  - codebase-aware retrieval defaults
  - stronger project-doc + memory coordination
  - tighter relevance filtering for engineering queries

### 4) Release hardening toward beta
- Tighten release gating (provider matrix, restart behavior, migration checks).
- Improve installation ergonomics and operational docs.
- Reduce sharp edges around notifications, bootstrap safety, and environment portability.

## Backlog Management
Detailed implementation backlog is tracked in `~/quaid/TODO.md` and project operation docs under `projects/quaid/operations/`.

## Release Positioning
Near-term release should be positioned as:
- **Open source alpha**
- **Known limitations documented up front**
- **Roadmap visible and contributor-friendly**
