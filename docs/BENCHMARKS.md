# Quaid Benchmark Results

> Results snapshot: **February 2026**

## LoCoMo (ACL 2024)

[LoCoMo](https://github.com/snap-research/locomo) is an industry-standard benchmark for conversational memory and knowledge systems. It consists of 10 long conversations (5,882 turns) with 1,540 scored question-answer pairs across four categories: single-hop lookup, multi-hop reasoning, temporal reasoning, and open-domain inference.

We evaluate Quaid using **Mem0's exact methodology** -- same judge model (GPT-4o-mini), same prompt, same temperature (0.0), same JSON response format, same scoring. This makes the comparison peer-review valid.

---

## Results

| System | Accuracy | Answer Model | Notes |
|--------|----------|-------------|-------|
| Quaid (Haiku answers) | 70.3% +/- 0.06 | Haiku | Fair comparison tier |
| Mem0 (graphRAG) | 68.9% | GPT-4o-mini | Apr 2025 |
| Mem0 | 66.9% +/- 0.7 | GPT-4o-mini | Apr 2025, 10 trials |
| Zep | 66.0% | GPT-4o-mini | Apr 2025 |
| LangMem | 58.1% | GPT-4o-mini | Apr 2025 |
| OpenAI Memory | 52.9% | GPT-4o-mini | Apr 2025 |
| **Quaid (Opus answers)** | **75.0%** | **Opus** | **Recommended production config** |

Full-context baselines (upper bound, no knowledge layer): Haiku 79.6%, Opus 86.6%.

The shipping configuration uses Opus for both extraction and answer generation. Quaid achieves 88.3% of full-context Haiku performance (fair comparison) and 86.6% of full-context Opus performance while injecting only the relevant memories.

> Mem0, Zep, LangMem, and OpenAI numbers are from their [April 2025 paper](https://arxiv.org/abs/2504.01094).

### Competitive Context

Quaid is competitive on LoCoMo, but this is not presented as SOTA. The strongest value proposition is the full lifecycle architecture (capture, retrieval, maintenance, personality synthesis, project awareness), not a single leaderboard position.

Reported competitor numbers evolve quickly and vary by model/backbone, so treat any table as a dated snapshot rather than a permanent ranking. The focus here is reproducible methodology and transparent tradeoffs.

---

## Per-Category Breakdown

| Category | Questions | Quaid (Opus) | Quaid (Haiku) | Mem0 | Mem0 (gRAG) | Zep |
|----------|-----------|--------------|---------------|------|-------------|-----|
| Single-hop | 841 | **78.6%** | 73.0% | 67.1% | 65.7% | 61.7% |
| Multi-hop | 282 | **75.1%** | 72.1% | 51.1% | 47.2% | 41.4% |
| Temporal | 321 | **70.7%** | 65.1% | 55.5% | 58.1% | 49.3% |
| Open-domain | 96 | 58.0% | 58.7% | **72.9%** | **75.7%** | **76.6%** |
| **Overall** | **1540** | **75.0%** | **70.3%** | **66.9%** | **68.9%** | **66.0%** |

All competitor systems use GPT-4o-mini for answer generation. Quaid (Haiku) is the fair comparison row; Quaid (Opus) reflects the recommended production config.

### Where Quaid Wins

- **Multi-hop (+21pp vs Mem0):** Graph edges extracted at capture time enable traversal across related entities. When asked about a person's child's preferences, Quaid follows `person -> child_of -> preference` edges rather than relying solely on keyword matching.

- **Temporal (+10pp vs Mem0):** Structured date-normalized facts and temporal resolution in the janitor pipeline preserve exact dates that raw keyword search would miss.

- **Single-hop (+6pp vs Mem0):** Hybrid retrieval (vector + FTS + graph) with LLM reranking consistently surfaces the right fact for direct lookup queries.

### Where Mem0 Wins

- **Open-domain (-14pp vs Mem0):** Open-domain questions require broader contextual inference rather than precision lookup -- asking what someone might think about a topic, or inferring personality traits from scattered facts. Quaid's retrieval pipeline is optimized for precision (exact facts, graph traversal, entity resolution), which excels at lookup queries but retrieves too narrowly for open-ended reasoning. Mem0 and Zep's chunk-based retrieval surfaces more contextual material, giving the answer model more to reason over. Early experiments with wider context injection (via journal archives) show promising open-domain improvements, and we're building intent-aware context expansion so open-domain queries automatically get a broader retrieval window.

---

## Memory Efficiency

Percentage of full-context baseline performance retained (higher is better):

| Category | Quaid (Haiku) | Quaid (Opus) |
|----------|---------------|--------------|
| Single-hop | 78.1% | 84.7% |
| Multi-hop | 98.3% | 93.3% |
| Temporal | 106.4% | 85.5% |
| Open-domain | 94.2% | 91.3% |
| **Overall** | **86.8%** | **86.6%** |

The recommended Opus configuration captures 86.6% of full-context performance while injecting only the relevant memories -- typically a few thousand tokens instead of the entire transcript.

Values over 100% mean Quaid's structured memory **outperforms** having the full raw transcript (temporal queries benefit from date normalization).

---

## Statistical Rigor

All results are from 3 independent trials with Wilson Score 95% confidence intervals (recommended by ICML 2025 spotlight paper over CLT-based CIs for LLM evaluation).

| Config | Mean +/- Std | 95% CI |
|--------|-------------|--------|
| Quaid (Opus) | 75.00% +/- 0.21 | [72.5%, 76.8%] |
| Quaid (Haiku) | 70.28% +/- 0.06 | [68.0%, 72.5%] |
| Full-context (Haiku) | 79.59% +/- 0.17 | [77.3%, 81.3%] |
| Full-context (Opus) | 86.62% +/- 0.09 | [84.8%, 88.2%] |

The 95% CI lower bound for Quaid+Opus (72.5%) exceeds Mem0's point estimate (66.9%) by 5.6pp. Note: Mem0's paper reports a single point estimate without full confidence intervals, so this comparison is against their reported mean.

---

## Evaluation Pipeline

```
Full conversation transcripts
    |
    v
High-reasoning LLM extracts facts + edges + snippets + journal entries
    |
    v
Store in SQLite (embeddings via Ollama, edges in graph)
    |
    v
Full janitor pipeline per conversation:
  - Embeddings backfill
  - Temporal date resolution
  - Duplicate detection (fast-reasoning LLM reranker)
  - Ebbinghaus confidence decay
  - Snippet review (FOLD/REWRITE/DISCARD)
  - Journal distillation (deep-reasoning LLM)
    |
    v
Hybrid recall: FTS5 + sqlite-vec + RRF fusion + LLM reranker + HyDE
    |
    v
Answer generation (fast-reasoning or deep-reasoning LLM)
    |
    v
GPT-4o-mini judge (Mem0's exact ACCURACY_PROMPT) --> CORRECT / WRONG
```

Quaid processes **full conversation transcripts end-to-end** -- not pre-atomized facts. This tests the entire pipeline including extraction quality, which is how the system actually works in production.

---

## Dataset

10 conversations from the [snap-research/locomo](https://huggingface.co/datasets/snap-research/locomo) dataset:

- 5,882 total turns across 272 sessions
- 1,986 QA pairs (1,540 scored, 446 adversarial excluded per Mem0's methodology)
- 4 question categories: single-hop (841), multi-hop (282), temporal (321), open-domain (96)

---

## Cost

| Phase | Cost |
|-------|------|
| Ingestion (Opus extraction, 10 conversations) | ~$25-30 |
| Janitor processing (full pipeline from cache) | ~$5-15 |
| Evaluation -- Haiku answers (1,540 QA) | ~$5.57 |
| Evaluation -- Opus answers (1,540 QA) | ~$27.27 |
| Judging (GPT-4o-mini, 3 trials) | ~$0.90 |
| **Total (all configs)** | **~$85** |

---

## Methodology Compliance

| Aspect | Mem0 | Quaid | Match? |
|--------|------|-------|--------|
| Judge model | GPT-4o-mini | GPT-4o-mini | Yes |
| Judge prompt | ACCURACY_PROMPT | Same (verbatim) | Yes |
| Temperature | 0.0 | 0.0 | Yes |
| Response format | JSON mode | JSON mode | Yes |
| Category 5 excluded | Yes | Yes | Yes |
| Overall score | Weighted mean | Weighted mean | Yes |
| Multiple trials | 10 | 3 | Partial |
| Answer model | GPT-4o-mini | Haiku (fair) / Opus (production) | Disclosed |

Mem0 uses GPT-4o-mini for answer generation. We report both Haiku answers (similar tier, fair comparison) and Opus answers (recommended production config).

---

## Next Benchmarks

- **LongMemEval** (ICLR 2025): Code written and smoke-tested at `benchmark/longmemeval/`. 500 QA pairs across 7 question types and 19,195 sessions. Pending full evaluation run. Top reported scores: Emergence AI 86%, Supermemory 81.6%, Zep+GPT-4o 71.2%.
- **Purpose-built lifecycle benchmark**: A longer-term project to build a dataset that tests the full memory lifecycle -- multi-session accumulation, fact evolution, maintenance decisions, and project-level awareness. No existing benchmark covers this. See [ROADMAP.md](../ROADMAP.md).

---

## Reproducing Results

The benchmark suite lives in `benchmark/` (public repo) or `memory-stress-test/runner/locomo/` (development). To run the LoCoMo evaluation:

```bash
cd benchmark/locomo
python3 run_locomo.py --config standard --answer-model haiku --trials 3
python3 run_locomo.py --rejudge --results-dir data/results/  # Re-score cached predictions
python3 run_locomo.py --full-context  # Full-context baseline (no knowledge layer)
```

Requirements: `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (for GPT-4o-mini judge) environment variables. Ollama running locally for embeddings. A full evaluation run costs approximately $85 across all configurations.

---

## References

- **LoCoMo:** Maharana et al. (ACL 2024), "LoCoMo: Long-Context Conversations with Memory-Enhanced LLMs"
- **Mem0 evaluation:** Chadha et al. (Apr 2025), Mem0 technical report
- **Wilson Score CIs:** ICML 2025 spotlight paper on statistical rigor in LLM evaluation
