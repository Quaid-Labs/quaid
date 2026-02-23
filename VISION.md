# Quaid Vision

Quaid is a local-first knowledge layer for agentic systems.

It is not a chat wrapper, not a hosted memory SaaS, and not a monolithic agent runtime.
Quaid sits between agent runtimes and long-term knowledge, turning noisy interaction history into structured, maintainable knowledge that improves with use.

## Mission

Provide a durable, private, and adaptable knowledge foundation that helps agents remember, retrieve, and evolve context over long-lived work.

## Product Boundary

Quaid owns:

- Knowledge extraction and ingestion into datastores
- Retrieval orchestration across datastores
- Lifecycle maintenance (dedup, contradictions, temporal/staleness handling)
- Adapter interfaces for host runtime integration

Quaid does not own:

- End-user chat UX as the primary product
- Closed cloud memory hosting as the default mode
- Tight coupling to a single host runtime

## Integration Posture

OpenClaw is the deepest integration today and a first-class reference adapter.

Quaid core remains adapter-agnostic by design:

- Runtime-specific behavior belongs in adapters
- Core/orchestrator logic must not require OpenClaw-only primitives
- New adapters should be possible without rewriting core behavior

## Design Principles

- Local-first by default: user knowledge stays on user-controlled infrastructure
- Explicit behavior over hidden magic: avoid silent, costly fallbacks
- Maintainability over novelty: lifecycle quality beats one-shot recall hacks
- Small, composable interfaces: adapters, datastores, and orchestration should evolve independently
- Observability: failures and degraded modes must be visible to operators

## Non-Goals (Current)

- Becoming a full agent framework that replaces host runtimes
- Optimizing for lowest-friction cloud-only onboarding at the expense of local control
- Shipping broad provider-specific hacks in core for benchmark edge cases

## Guardrails For Contributions

- Prefer focused PRs with one clear concern
- Preserve core/orchestrator/adapter boundaries
- Do not introduce hidden fallback paths that can increase cost or hide failures
- Keep docs aligned with behavior changes

## Near-Term Focus

- Retrieval quality and predictability
- Adapter eventing and delayed-action reliability
- Installer and operational UX hardening
- Reproducible benchmark and release workflows
