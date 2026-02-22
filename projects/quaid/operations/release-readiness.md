# Release Readiness (Alpha)

## Positioning

Quaid is ready for an **alpha OSS release** with explicit known issues and a public roadmap.

Recommended framing:
- "Alpha: production-minded architecture, active hardening in progress."
- "Known issues documented; contributions welcome on high-impact items."

## Go / Hold

### Go now (alpha)
- Deterministic integration and mock-core tests in place.
- E2E bootstrap/runtime flow exists with verification.
- Provider abstraction is substantially improved and testable.

### Hold for broader release until
- `/new` and `/reset` become parallel-session safe (remove last-active fallback behavior).
- Command API path fully replaces slash-text fallback paths.
- Janitor modes split (`memory-maintenance` vs `workspace-editing`) to avoid incidental doc rewrites.
- Provider matrix smoke (`openai` + `anthropic`) is part of release gating.

## Known Issues To Publish

- Session targeting edge cases in parallel conversations for reset/new extraction triggers.
- Some runtime/gateway restore flows can require a restart retry.
- Janitor scope currently includes workspace editing unless explicitly constrained.
- Non-OpenClaw adapter coverage is still early compared to the OpenClaw path.

## Notification Posture (Alpha)

- Keep recommended default as `normal`:
  - `janitor: summary`
  - `extraction: summary`
  - `retrieval: off`
- Use feature-level overrides for power users instead of a single global toggle.
- Route asynchronous actionable janitor health requests through adapter-managed delayed request queues.

## Immediate Post-Alpha Priorities

1. Parallel-session-safe extraction targeting.
2. Command-API-only control flow.
3. Retrieval quality uplift for relationship/family queries (fact + graph parallel composition).
4. Cloud embeddings option (lower setup friction).
5. Graph/config UX surfaces (visualizer + dashboard).

## Contributor Call

Good first contribution tracks:
- Session control and hook reliability.
- Retrieval result shaping and graph traversal quality.
- Provider matrix automation and CI hardening.
- Janitor mode boundaries and safety controls.
