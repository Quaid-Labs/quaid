# Provider Modes

Quaid can run through multiple provider paths. They do not have the same cost profile.

## Modes

- `claude-code`:
  - Uses Claude Code (`claude -p`) through the host runtime.
  - Intended as the non-API spend path when your host has an active subscription.
- `anthropic`:
  - Direct Anthropic API key path.
  - Billed per token.
- `openai-compatible`:
  - Direct OpenAI-compatible API path.
  - Billed per token.
- `default`:
  - Delegates to active host/provider resolution.
  - In OpenClaw, this depends on gateway auth/profile state.

## Cost Safety Rules

- Avoid silent fallbacks from subscription paths into API-key paths.
- Prefer explicit provider configuration over implicit defaults.
- `retrieval.fail_hard` controls fallback behavior system-wide:
  - `true` (default): fail fast, no fallback behavior.
  - `false`: fallback behavior is allowed, but must emit explicit warning logs.
- Validate startup logs before long runs:
  - look for `[quaid][startup] ... provider=... model=...`
  - look for `[quaid][billing] paid provider active ...` warnings

## Recommended Config Pattern

- For lowest surprise:
  - set `models.llmProvider` explicitly
  - set `models.deepReasoningProvider` and `models.fastReasoningProvider` explicitly if they differ
- Use API paths intentionally and document them in runbooks/benchmarks.

## Troubleshooting

If provider resolution fails:

1. Check `config/memory.json` provider fields.
1. Check OpenClaw auth profiles and `lastGood` profile mapping.
1. Re-run release/runtime checks:
   - `bash scripts/release-check.sh`
   - `node modules/quaid/scripts/check-runtime-pairs.mjs --strict`
