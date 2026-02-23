# Quaid Roadmap

This roadmap intentionally avoids dates and version promises.
It reflects broad areas of active focus, not delivery guarantees.

## Current Focus

- **Reliability and correctness**
  - Continue hardening extraction, recall, and janitor paths.
  - Reduce failure ambiguity with better diagnostics and test isolation.
  - Keep OpenClaw integration stable as the primary production path.
  - Parallelize janitor task execution after architectural boundary enforcement is complete.

- **Benchmark rigor**
  - Keep LoCoMo runs reproducible and current.
  - Run and publish LongMemEval results.
  - Improve retrieval quality in weaker categories without overfitting benchmark prompts.

- **Knowledge-layer architecture**
  - Continue refining datastore routing/read paths.
  - Expand write-path contracts (DataWriters) so new datastores can be added cleanly.
  - Keep adapter/orchestrator/core boundaries explicit and testable.

- **Docs and OSS readiness**
  - Keep public docs aligned with actual system behavior.
  - Mark unproven surfaces as experimental instead of overpromising.
  - Improve contributor onboarding and operational documentation.

## Near-Term Exploration

- **Graph and memory introspection**
  - Better visibility into why recalls were returned (and why misses happened).
  - Optional graph visualization/debug views.

- **Import and migration workflows**
  - Evaluate practical import paths from other systems and prior agent histories.
  - Prioritize low-risk, auditable migration flows.

- **Host coverage beyond OpenClaw**
  - Expand and validate MCP usage across more clients.
  - Increase adapter portability while preserving behavior guarantees.

## Longer-Horizon Work

- **Lifecycle benchmark development**
  - Design and publish a benchmark focused on long-term maintenance/evolution, not only retrieval.

- **Multi-agent / multi-owner hardening**
  - Strengthen isolation, governance, and conflict behavior under concurrent workloads.

- **Operational UX**
  - Improve dashboards/visibility and config ergonomics as complexity grows.

## Explicit Non-Goals (for now)

- Broad claims of full compatibility across all MCP hosts before validation.
- Heavy platform-specific promises without repeatable install/test coverage.
- Roadmap commitments tied to specific release dates.

---

For detailed engineering tasks and speculative work, see internal TODO tracking.
