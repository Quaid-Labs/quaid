# AgentLife Benchmark

AgentLife is Quaid's benchmark for **agentic memory systems**.

Existing benchmarks (for example LoCoMo and LongMemEval) are useful for QA-style memory recall, but they do not fully test production agent behavior:
- cross-session persistence
- recall after resets/restarts
- evolving knowledge over long horizons
- project and workspace memory under noise

AgentLife focuses on those failure modes directly.

## Why it exists

Production agents do not fail only on isolated question-answer tasks. They fail when:
- context gets reset and prior state disappears
- memory quality degrades over long runs
- project facts get drowned out by conversational noise
- system behavior changes across lanes, models, and maintenance policies

AgentLife was built to measure that lifecycle end-to-end.

## Headline results

Headline comparison rows (recommended Quaid lane vs strongest FC Sonnet baseline and OpenClaw native):

|                            | Quaid Sonnet/Haiku | FC Sonnet | OpenClaw Native |
|----------------------------|-------------------:|----------:|----------------:|
| AL-S | 87.69% (`r880`) | 92.90% (`r606`) | 69.40% (`oc-native-als-20260315d`) |
| AL-L | 85.82% (`r895`) | 87.70% (`r857`) | 63.06% (`oc-native-all-20260315d`) |

Sonnet-eval study result: **88.69%** on AL-L (`r944`), above FC Sonnet's **87.70%** baseline on the same corpus.

## Read next

- [AgentLife Technical Report](AGENTLIFE-TECHNICAL-REPORT.md)
- [Benchmark index](BENCHMARKS.md)
- Hosted overview: https://quaid.ai/benchmarks/agentlife
- Hosted technical report: https://quaid.ai/benchmarks/agentlife/technical-report
