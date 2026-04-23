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
  - `python3 -m pytest -q tests/test_janitor_apply_mode.py tests/test_janitor_benchmark_review_gate.py tests/test_janitor_lifecycle.py tests/test_maintenance_parallelism.py tests/test_llm_clients.py tests/test_provider_selection.py tests/test_providers.py`
- TypeScript orchestrator/session timeout integration passes:
  - `npm run test:integration`

## 3) Live Validation Gates

- Full current live suite passes, using:
  - `modules/quaid/tests/livetest/LIVE-TEST-GUIDE.md`
- Compatibility rows are written only after the live suite is green and the
  cleared runtime SHA is fixed.
- If `HEAD` moved after the clear, list the exact post-clear delta for release
  approval before tagging.

## 4) Provider Matrix Smoke

- At least one smoke lane each for `openai` and `anthropic`.
- No auth fallback surprises in logs (all credential paths explicit).
- Do not count contradiction-task output as a release gate signal; stale-fact validation is supersession/recency based in current janitor flow.

## 5) Operational Readiness

- Branch clean and pushed.
- Release notes and known issues updated.
- Full current live suite passes before release approval, using the current
  definition in `modules/quaid/tests/livetest/LIVE-TEST-GUIDE.md`.
- After the live suite clears, compare the cleared SHA against current `HEAD`
  and list any post-clear changes for Solomon before release approval.
- Compatibility rows are written for OpenClaw, Claude Code, and Codex, and only
  after the full live suite is green and the release-target SHA is fixed.
- Benchmark lane notified only after all above gates pass.
