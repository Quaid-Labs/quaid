# AgentLife Technical Report

This page is the launch-facing technical summary for AgentLife.

Hosted benchmark pages:
- https://quaid.ai/benchmarks/agentlife
- https://quaid.ai/benchmarks/agentlife/technical-report

Technical source of truth (runbook with full matrix/run IDs):
- `agentlife-benchmark/published/runbooks/AGENTLIFE_TECHNICAL_REPORT.md`
- `agentlife-benchmark/published/runbooks/release-candidate/AGENTLIFE_RELEASE_CANDIDATE_20260328.md`

## Scope

- Judge model: `gpt-4o-mini`
- Dataset: canonical AgentLife query set (283 questions)
- Lanes: AL-S, AL-L, AL-L OBD, FC baselines, OpenClaw native baselines
- Run IDs retained for reproducibility

## Headline matrix (launch rows)

| Lane | Quaid Sonnet/Haiku | FC Sonnet | OpenClaw Native |
|---|---:|---:|---:|
| AL-S | 87.69% (`r880`) | 92.90% (`r606`) | 69.40% (`oc-native-als-20260315d`) |
| AL-L | 85.82% (`r895`) | 87.70% (`r857`) | 63.06% (`oc-native-all-20260315d`) |

## Sonnet-eval study

AL-L Sonnet-eval results from the same technical source:

- Quaid Haiku-ingest / Sonnet-eval: **88.69%** (`r944`)
- Quaid Sonnet-ingest / Sonnet-eval: **87.10%** (`r945`)
- FC Sonnet baseline (AL-L): **87.70%** (`r857`)

Interpretation:
- Quaid remains near FC Sonnet on the headline matrix while using far fewer eval tokens.
- For Sonnet-first production messaging, the Sonnet-ingest/Sonnet-eval row is **87.10%** (`r945`).
- Quaid materially outperforms OpenClaw native memory on both AL-S and AL-L.

## Method notes

- FC baselines are single-session upper bounds and do not persist state across resets.
- Quaid rows evaluate full memory lifecycle behavior (capture, maintenance, retrieval).
- Many runs are eval-only reruns on fixed ingest lineage to isolate recall/eval changes.
