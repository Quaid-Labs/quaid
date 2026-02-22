# Adapter/Provider Architecture

## Goal

Quaid core stays provider-agnostic. Provider and model selection are handled only by adapter/provider code plus config.

## Boundaries

- Core logic (memory graph, janitor, docs tooling) must not hardcode provider-specific behavior.
- Adapter layer owns:
  - workspace/path resolution
  - notification delivery
  - credential discovery
  - gateway integration
- Provider layer owns:
  - transport for LLM/embedding calls
  - model-tier execution (`high` / `low`)

## Runtime Flow (OpenClaw)

1. Quaid code requests an LLM tier (`high` or `low`).
2. `adapters/openclaw/index.ts` resolves effective provider + model via config:
   - `models.llmProvider`
   - `models.deepReasoning` / `models.fastReasoning`
   - `models.providerModelClasses`
3. If tier model is `default`, provider model pair comes from `providerModelClasses` for the effective provider.
4. Resolved call goes through OpenClaw plugin endpoint (`/plugins/quaid/llm`) and gateway auth.
5. Gateway provider (OAuth/API) executes the model call.

## Provider Resolution Rules

- Explicit tier model (`models.deepReasoning` / `models.fastReasoning` not `default`) is used directly.
- `default` tier model requires matching `providerModelClasses` entry.
- Effective provider:
  - explicit `models.llmProvider` if not `default`
  - otherwise inferred from active gateway provider/auth state
- Unknown/missing provider mapping is an error (fail loudly; no silent fallback).

## Key Files

- `plugins/quaid/adapters/openclaw/index.ts`
  - tier/provider resolution
  - extraction hooks (`agent_end`, `command`, `before_compaction`, `before_reset`)
  - gateway-bound LLM calls
- `plugins/quaid/adapters/openclaw/providers.py`
  - `GatewayLLMProvider` bridge to `/plugins/quaid/llm`
- `plugins/quaid/adapters/openclaw/adapter.py`
  - OpenClaw adapter for paths, notifications, session metadata, gateway credential context
- `plugins/quaid/lib/adapter.py`
  - adapter interface and selection
- `plugins/quaid/lib/providers.py`
  - provider abstractions + implementations

## Config Contract

`config/memory.json`:

- `models.llmProvider`
- `models.deepReasoning`
- `models.fastReasoning`
- `models.providerModelClasses[]`

Provider model constants should live in config, not scattered across core logic.
