# Benchmarks

Evaluation infrastructure for testing Quaid against industry-standard memory benchmarks.

## Benchmarks Included

### LoCoMo (ACL 2024)

10 long conversations, 1,986 question-answer pairs testing memory extraction, temporal reasoning, and multi-hop recall.

```bash
# Full evaluation (~$5-10 depending on model)
python3 -m benchmark.locomo.run_locomo --db-path /path/to/test.db

# Re-judge cached predictions (free if using same judge)
python3 -m benchmark.locomo.run_locomo --rejudge --results-dir results/

# Full-context baseline (upper bound, no memory system)
python3 -m benchmark.locomo.run_locomo --full-context
```

Paper: [LoCoMo: Long-Context Conversation Memory](https://github.com/snap-research/locomo)

### LongMemEval (ICLR 2025)

500 question-answer pairs across 7 question types and 5 memory abilities, derived from 19,195 unique chat sessions.

```bash
# Full evaluation (~$60 with Haiku)
python3 -m benchmark.longmemeval.run_longmemeval --db-path /path/to/test.db
```

Paper: [LongMemEval](https://github.com/xiaowu0162/LongMemEval)

## Methodology

We follow Mem0's exact evaluation methodology for LoCoMo:
- **Judge model**: GPT-4o-mini with Mem0's ACCURACY_PROMPT
- **Temperature**: 0.0, JSON mode
- **Scoring**: Exclude Category 5 (adversarial), weighted mean by question count
- **Statistical rigor**: Multiple trials with mean +/- std, 95% confidence intervals

Full methodology details: [METHODOLOGY.md](METHODOLOGY.md)

## Requirements

- Quaid plugin installed and configured
- API keys: Anthropic (extraction + answering), OpenAI (judging)
- Ollama running locally (embeddings)
- `QUAID_WORKSPACE` environment variable pointing to your workspace

## Directory Structure

```
benchmark/
├── locomo/          # LoCoMo benchmark (ACL 2024)
├── longmemeval/     # LongMemEval benchmark (ICLR 2025)
├── baseline/        # OpenClaw baseline reimplementation
├── mem0_adapter.py  # Mem0 API adapter for comparison
├── metrics_logger.py # Structured result logging
├── METHODOLOGY.md   # Study design and fidelity justification
└── STRESS-TEST-REPORT.md  # Pipeline validation report
```
