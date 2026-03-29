# Quaid Roadmap

This roadmap intentionally avoids dates and version promises.
It reflects broad areas of active focus, not delivery guarantees.

## Current Focus

- **Top priority after release: Codex adaptor**
  - Build a first-class Codex host adaptor now that hook support is available.
  - Match OpenClaw/Claude Code baseline capabilities for install, extraction, recall injection, and diagnostics.
  - Keep host-specific behavior inside adaptor surfaces while reusing shared runtime behavior.

- **Reliability and correctness**
  - Continue hardening extraction, recall, and janitor paths.
  - Reduce failure ambiguity with better diagnostics and test isolation.
  - Keep OpenClaw and Claude Code integrations stable as primary production paths.
  - Parallelize janitor task execution after architectural boundary enforcement is complete.

- **Benchmark rigor**
  - Keep AgentLife runs reproducible and current.
  - Publish clear release-candidate benchmark evidence with stable provenance.
  - Improve retrieval quality in weaker categories without overfitting benchmark prompts.

- **Docs and OSS readiness**
  - Keep public docs aligned with actual system behavior.
  - Mark unproven surfaces as experimental instead of overpromising.
  - Improve contributor onboarding and operational documentation.

## Near-Term Exploration

- **Datastore modularity/plugin friendliness**
  - Refactor datastore contracts so stores can be added or replaced cleanly without cross-layer coupling.
  - Reduce hard-coded datastore assumptions in recall/write/maintenance paths.
  - Define stable plugin-facing datastore capabilities before broadening host coverage.

- **Multi-user + group conversation memory**
  - Partition memory by user/group identity with explicit routing and ownership guarantees.
  - Support context muxing for fast participant/conversation switching.
  - Add mixed-recall policies for shared/group threads without cross-user leakage.

- **Graph and memory introspection**
  - Better visibility into why recalls were returned (and why misses happened).
  - Optional graph visualization/debug views.

- **Import and migration workflows**
  - Evaluate practical import paths from other systems and prior agent histories.
  - Prioritize low-risk, auditable migration flows.

## Longer-Horizon Work

- **Host coverage beyond current adapters**
  - Expand validated host integrations while preserving behavior guarantees.

- **Multi-agent / multi-owner hardening**
  - Strengthen isolation, governance, and conflict behavior under concurrent workloads.

- **Operational UX**
  - Improve dashboards/visibility and config ergonomics as complexity grows.

## Explicit Non-Goals (for now)

- Broad claims of full compatibility across unvalidated host runtimes before validation.
- Heavy platform-specific promises without repeatable install/test coverage.
- Roadmap commitments tied to specific release dates.

---

For detailed engineering tasks and speculative work, see internal TODO tracking.
