# Quaid Release Checklist

Use this as the go/no-go gate for prelaunch and release candidates.

## 1) Boundary + FailHard

- `cd modules/quaid && npm run -s check:boundaries` passes.
- No silent fallback paths added in changed code.
- `retrieval.failHard=true` remains the default in config.

## 1.1) Plugin Contract Gate

- Plugin runtime preflight executes during config boot when `plugins.enabled=true`.
- `plugins.strict=true` hard-fails on:
  - invalid manifests/schema,
  - plugin ID conflicts,
  - slot references to missing plugin IDs,
  - slot/plugin type mismatches.
- `plugins.strict=false` keeps booting but emits loud plugin diagnostics.
- Contract suite passes:
  - `python3 -m pytest -q tests/test_plugin_runtime.py`

## 2) Core Test Gates

- Python janitor/failHard/provider suites pass:
  - `python3 -m pytest -q tests/test_janitor_apply_mode.py tests/test_janitor_benchmark_review_gate.py tests/test_janitor_lifecycle.py tests/test_maintenance_parallelism.py tests/test_llm_clients.py tests/test_mcp_server.py tests/test_provider_selection.py tests/test_providers.py`
- TypeScript orchestrator/session timeout integration passes:
  - `node test-runner.js tests/knowledge-orchestrator.test.ts tests/session-timeout-manager.test.ts`

## 3) E2E Runtime Gates

- Janitor dry-run E2E passes:
  - `bash scripts/run-quaid-e2e.sh --suite janitor --janitor-dry-run --quick-bootstrap --reuse-workspace --skip-llm-smoke --skip-live-events --skip-notify-matrix --janitor-timeout 300`
- Janitor apply-mode E2E passes with non-interactive approval policy:
  - run with `notifications.approvalPolicy.default=auto` (or equivalent) for the e2e workspace profile.
- E2E summary integrity checks:
  - no stage remains `running` in a `success` summary.

## 4) Provider Matrix Smoke

- At least one smoke lane each for `openai` and `anthropic`.
- No auth fallback surprises in logs (all credential paths explicit).

## 5) Operational Readiness

- Branch clean and pushed.
- Release notes and known issues updated.
- Benchmark lane notified only after all above gates pass.
