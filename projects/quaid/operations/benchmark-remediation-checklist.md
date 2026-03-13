# Benchmark Remediation Checklist (P1, P2, P4, P5)

This checklist tracks the remaining work for benchmark reliability and retrieval quality.

> **Note:** `~/quaid/benchmark/` was removed 2026-03-01 (archived to `~/quaid/benchmark.REMOVED-20260301-094624`). Active benchmark harness is now at `~/quaid/agents/codex-bench/`. All `agentlife/eval/` commands below run from that directory.

## Scope

Priority items covered:
1. Graph edge creation reliability
2. Empty prediction elimination
3. Agent-action memory capture
4. Stale fact handling (supersession/temporal recency)

## P1: Graph Edge Creation Reliability

### Goal
Ensure extraction + janitor consistently create usable graph edges in production-like runs.

### Required work
- Verify edge creation path writes to the intended run workspace DB (no path leakage).
- Verify benchmark ingestion edge accounting reflects actual edge creation status.
- Verify janitor review path executes and can add/repair edges when applicable.
- Add/keep diagnostics for edge creation outcomes per fact batch.

### Acceptance criteria
- In a clean AL-S run, `data/memory.db` has `edges > 0` after ingestion.
- `janitor_stats.json` shows non-zero review activity when candidate items exist (for example, `memories_reviewed > 0` on non-trivial runs).
- Edge outcome logs show success statuses and no systematic failure mode.
- Re-running extraction in resume mode does not regress edge counts.

### Validation commands
- Single-chunk extraction debug (run from `~/quaid/agents/codex-bench`):
  - `python3 agentlife/eval/run_production_benchmark.py --mode ingest --results-dir <run> --only-chunk <idx> --skip-janitor --no-tier5`
- DB checks:
  - `sqlite3 <run>/data/memory.db "select count(*) from edges;"`
  - `sqlite3 <run>/data/memory.db "select status, count(*) from nodes group by status;"`

## P2: Empty Prediction Elimination

### Goal
Eliminate blank model outputs in eval flows.

### Required work
- Reproduce known empty query cases with single-query debug mode.
- Validate tool-loop fallback behavior returns a non-empty answer even when model output is blank.
- Confirm no accidental overwrite of canonical run artifacts during debug queries.

### Acceptance criteria
- Historically empty queries return non-empty `prediction` in `--only-query` runs.
- Full AL-S run has `0` empty predictions.
- Debug eval runs write only `*.debug.json` artifacts, leaving canonical `evaluation_results.json` intact.

### Validation commands (run from `~/quaid/agents/codex-bench`)
- `python3 agentlife/eval/run_production_benchmark.py --mode eval --results-dir <run> --only-query <idx> --no-judge --no-tier5`
- `python3 - <<'PY'\nimport json; r=json.load(open('<run>/evaluation_results.debug.json'))[0]; print(bool((r.get('prediction') or '').strip()))\nPY`

## P4: Agent-Action Memory Capture

### Goal
Capture what the assistant did/suggested/built as first-class memory facts.

### Required work
- Update extraction prompt guidance to explicitly capture assistant actions and recommendations.
- Ensure extraction schema captures actor/source role and action type where possible.
- Ensure dedup does not collapse materially distinct agent actions into unrelated user facts.

### Acceptance criteria
- On benchmark sessions with known assistant actions, extracted facts include those actions.
- `agent_retrieved` category improves meaningfully from current baseline.
- Manual spot-check confirms at least 5 representative agent-action facts are queryable.

### Validation commands
- Run targeted extraction on sessions with known agent contributions.
- Query DB for action-like facts and verify source/session attribution.

## P5: Stale Fact Handling (Supersession/Temporal Recency)

### Goal
Promote current facts and suppress stale/conflicting facts.

### Required work
- Confirm janitor review/dedup/decay tasks execute in benchmark runtime.
- Verify supersession metadata/flags are applied and respected in recall.
- Verify temporal-current queries prefer newest valid facts in spot checks.

### Acceptance criteria
- Known contested/stale facts are marked superseded/flagged after janitor.
- Supersession activity appears when stale data exists (for example: newer facts win in recall; stale facts lose ranking or are suppressed by status/validity).
- Retrieval for temporal-current queries prefers latest facts in validation spot-checks.

### Validation commands
- `python3 modules/quaid/core/lifecycle/janitor.py --task review --apply` (add `--approve` if `janitor.applyMode=ask`)
- `python3 modules/quaid/core/lifecycle/janitor.py --task duplicates --apply`
- `python3 modules/quaid/core/lifecycle/janitor.py --task decay_review --apply`
- Query DB for stale/superseded markers and verify recall behavior on contested prompts.

Note: the default `janitor.apply_mode` is `auto` so `--apply` works without `--approve`. Add `--approve` only when `applyMode=ask` is configured.

## Exit Criteria For Next Benchmark Pass

All must be true before accepting a new AL-S/AL-L comparison run:
- P1 acceptance criteria pass
- P2 acceptance criteria pass
- P4 has extraction + spot-check evidence
- P5 supersession/temporal recency checks pass
- Full run artifacts generated without debug-mode contamination
