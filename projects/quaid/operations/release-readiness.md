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
- Command API path fully replaces slash-text fallback paths.
- Janitor modes split (`memory-maintenance` vs `workspace-editing`) to avoid incidental doc rewrites.
- Provider matrix smoke (`openai` + `anthropic`) is part of release gating.
- Remaining compatibility shims are removed from active import paths (notably `core/docs/*` shim entrypoints).

## Release Gate Doc

- Canonical go/no-go checklist: `projects/quaid/operations/release-checklist.md`

## Known Issues To Publish

- Some runtime/gateway restore flows can require a restart retry.
- Janitor scope currently includes workspace editing unless explicitly constrained.
- Non-OpenClaw adapter coverage is still early compared to the OpenClaw path.
- OpenClaw typed plugin hooks can miss `before_reset` across bundle boundaries (upstream: https://github.com/openclaw/openclaw/issues/23895). Quaid mitigates reset/new extraction via internal workspace command hooks (`command:new`, `command:reset`) while compaction remains on `before_compaction`.
- Janitor apply-mode E2E can block on approval-policy `ask`; this is expected unless policy is pinned to non-interactive behavior for the run.

## Notification Posture (Alpha)

- Keep recommended default as `normal`:
  - `janitor: summary`
  - `extraction: summary`
  - `retrieval: off`
- Use feature-level overrides for power users instead of a single global toggle.
- Route asynchronous actionable janitor health requests through adapter-managed delayed request queues.

## Immediate Post-Alpha Priorities

1. Command-API-only control flow.
2. Retrieval quality uplift for relationship/family queries (fact + graph parallel composition).
3. Cloud embeddings option (lower setup friction).
4. Graph/config UX surfaces (visualizer + dashboard).

## Contributor Call

Good first contribution tracks:
- Session control and hook reliability.
- Retrieval result shaping and graph traversal quality.
- Provider matrix automation and CI hardening.
- Janitor mode boundaries and safety controls.
