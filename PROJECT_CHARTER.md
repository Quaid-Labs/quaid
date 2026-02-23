# Quaid Project Charter

## Purpose

Quaid is a local-first knowledge layer for agentic systems.
It captures, maintains, and retrieves durable knowledge so agents can stay coherent over long-running use without replaying full history every turn.

## Scope

In scope:

- Fact capture and retrieval
- Knowledge maintenance lifecycle (review, dedup, contradiction handling, decay)
- Personality and project knowledge layers
- Adapter-based host integration (OpenClaw-first, system-agnostic architecture)

Out of scope (for now):

- Cloud memory SaaS as a primary mode
- Broad compatibility claims before validation
- Timeline-based promises disconnected from shipped behavior

## Design Principles

- **Local-first:** user data stays local by default.
- **No silent degradation:** avoid hidden fallbacks that mask failures.
- **Config over hardcode:** operational behavior should be explicit and inspectable.
- **Lifecycle over storage:** storing facts is not enough; quality must be maintained.
- **Adapter boundaries:** host-specific behavior belongs in adapters, not core.

## Quality Bar

- Changes should include tests and docs updates where behavior changes.
- Benchmarks and claims should be reproducible and date-stamped.
- Experimental surfaces must be labeled as such.

## Decision Model

- Maintainer-led decisions with public rationale in code/docs.
- Major architectural decisions should be recorded in docs.
- Community feedback is encouraged through issues and PRs.
