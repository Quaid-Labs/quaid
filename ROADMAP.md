# Quaid Roadmap

This roadmap avoids dates and version promises.
It reflects where Quaid is headed after the first stable release: cleaner
architecture, stronger reliability, and a more capable long-term memory system
for agents.

## Current Focus

- **Architecture tightening**
  - Simplify the runtime into cleaner, easier-to-manage boundaries.
  - Reduce cross-layer coupling between adapters, core logic, ingest, and datastores.
  - Replace more ad hoc background behavior with clearer supervised runtime ownership.
  - Make Quaid easier to extend without adding more one-off special cases.

- **Reliability and correctness**
  - Harden extraction, recall, docs indexing, and janitor behavior under real long-running use.
  - Keep live memory work from being blocked by unrelated background maintenance.
  - Improve recovery after crashes, stalled workers, or host lifecycle edge cases.
  - Keep failure modes explicit and diagnosable instead of silent or ambiguous.

- **Host hardening**
  - Keep OpenClaw, Claude Code, and Codex stable as supported public integrations.
  - Close the highest-impact parity gaps between hosts without pretending they are identical.
  - Improve install, upgrade, and runtime behavior before expanding to more platforms.

- **Operational clarity**
  - Improve observability around recall results, misses, indexing state, and daemon health.
  - Keep installer, AI-install, release, and compatibility workflows aligned with actual system behavior.
  - Continue tightening docs so public guidance matches the real product surface.

## What Comes Next

- **A more unified Quaid runtime**
  - Move toward one clearer runtime ownership model per Quaid home instead of a collection of loosely related background processes.
  - Split long-running maintenance work into better-isolated workers so one slow task cannot stall the rest of the system.

- **Stronger shared-memory foundations**
  - Build the groundwork for multi-user, group, and shared-project memory without cross-user leakage.
  - Improve routing and ownership rules so shared contexts stay useful without becoming unsafe.

- **Better memory introspection**
  - Make it easier to answer:
    - why a memory was recalled
    - why an expected memory was missed
    - what source context a fact came from
  - Add better debugging and graph-level visibility for operators and developers.

- **Safer import and migration workflows**
  - Support audited ways to ingest prior agent history and external sources.
  - Improve portability without turning migration into a black box.

## Longer-Horizon Direction

- **Extensible datastore and plugin contracts**
  - Make future stores and plugins easier to add cleanly.
  - Reduce hard-coded assumptions in recall, writing, and maintenance paths.

- **Host-owned adapter ecosystem**
  - Keep the near-term first-party focus on OpenClaw, Claude Code, and Codex.
  - Tighten adapter contracts so other hosts can own their own adapters in their own codebases.
  - Make it plausible over time for even the current big-three adapters to move outward if their host teams want to own them directly.
  - Preserve cross-host behavior through stable contracts rather than pulling every integration into the core repo.

- **Operational UX**
  - Improve dashboards, configuration ergonomics, and day-2 operating surfaces as Quaid grows.

## What This Roadmap Is Not

- It is not a date-based promise list.
- It is not a claim that all host integrations already have the same maturity.
- It is not a commitment to broad compatibility before validation exists.
- It is not a plan to pause shipped-behavior work for cleanup-only refactors.
