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
  - model-tier execution (`deep_reasoning` / `fast_reasoning`)

## Runtime Flow (OpenClaw)

1. Quaid code requests an LLM tier (`deep_reasoning` or `fast_reasoning`).
2. `adaptors/openclaw/adapter.ts` resolves effective provider + model via config:
   - `models.llmProvider`
   - `models.deepReasoning` / `models.fastReasoning`
   - `models.deepReasoningModelClasses` / `models.fastReasoningModelClasses`
3. If tier model is `default`, provider model pair comes from the corresponding model-class map for the effective provider.
4. Resolved call goes through OpenClaw plugin endpoint (`/modules/quaid/llm`) and gateway auth.
5. Gateway provider (OAuth/API) executes the model call.

## Provider Resolution Rules

- Explicit tier model (`models.deepReasoning` / `models.fastReasoning` not `default`) is used directly.
- `default` tier model requires matching model-class map entry for the resolved provider.
- Effective provider:
  - explicit `models.llmProvider` if not `default`
  - otherwise inferred from active gateway provider/auth state
- Unknown/missing provider mapping is an error (fail loudly; no silent fallback).

## Key Files

- `modules/quaid/adaptors/openclaw/adapter.ts`
  - tier/provider resolution
  - extraction hooks (`agent_end`, `command`, `before_compaction`, `before_reset`)
  - gateway-bound LLM calls
- `modules/quaid/adaptors/openclaw/providers.py`
  - `GatewayLLMProvider` bridge to `/modules/quaid/llm`
- `modules/quaid/adaptors/openclaw/adapter.py`
  - OpenClaw adapter for paths, notifications, session metadata, gateway credential context
- `modules/quaid/lib/adapter.py`
  - adapter interface and selection
- `modules/quaid/lib/providers.py`
  - provider abstractions + implementations

## Config Contract

`config/memory.json`:

- `models.llmProvider`
- `models.deepReasoning`
- `models.fastReasoning`
- `models.deepReasoningModelClasses`
- `models.fastReasoningModelClasses`

Provider model constants should live in config, not scattered across core logic.
