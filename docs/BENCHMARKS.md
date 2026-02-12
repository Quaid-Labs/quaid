# Quaid Benchmark Results

## LoCoMo (ACL 2024)

[LoCoMo](https://github.com/snap-research/locomo) is the industry-standard benchmark for conversational memory systems. It consists of 10 long conversations (5,882 turns) with 1,540 scored question-answer pairs across four categories: single-hop lookup, multi-hop reasoning, temporal reasoning, and open-domain inference.

We evaluate Quaid using **Mem0's exact methodology** -- same judge model (GPT-4o-mini), same prompt, same temperature (0.0), same JSON response format, same scoring. This makes the comparison peer-review valid.

---

## Results

| System | Accuracy | Notes |
|--------|----------|-------|
| **Quaid + Opus** | **75.0%** | Production config (Opus answers) |
| **Quaid + Journal** | **74.5% +/- 0.05** | Haiku answers + journal injection |
| **Quaid + Haiku** | **70.3% +/- 0.06** | Standard Haiku answers |
| Mem0 (graphRAG) | 68.9% | Apr 2025 |
| Mem0 | 66.9% +/- 0.7 | Apr 2025, 10 trials |
| Zep | 66.0% | Apr 2025 |
| LangMem | 58.1% | Apr 2025 |
| OpenAI Memory | 52.9% | Apr 2025 |

Full-context baselines (upper bound, no memory system): Haiku 79.6%, Opus 86.6%.

> Mem0, Zep, LangMem, and OpenAI numbers are from their [April 2025 paper](https://arxiv.org/abs/2504.01094).

---

## Per-Category Breakdown

| Category | Questions | Quaid+Journal | Quaid+Haiku | Quaid+Opus | Mem0 | Mem0^g | Zep |
|----------|-----------|---------------|-------------|------------|------|--------|-----|
| Single-hop | 841 | **77.7%** | 73.0% | 78.6% | 67.1% | 65.7% | 61.7% |
| Multi-hop | 282 | 74.6% | 72.1% | 75.1% | 51.1% | 47.2% | 41.4% |
| Temporal | 321 | **72.3%** | 65.1% | 70.7% | 55.5% | 58.1% | 49.3% |
| Open-domain | 96 | 53.1% | 58.7% | 58.0% | **72.9%** | **75.7%** | **76.6%** |
| **Overall** | **1540** | **74.5%** | **70.3%** | **75.0%** | **66.9%** | **68.9%** | **66.0%** |

### Where Quaid Wins

- **Multi-hop (+23pp vs Mem0):** Graph edges extracted at capture time enable traversal across related entities. When asked about a person's child's preferences, Quaid follows `person -> child_of -> preference` edges rather than relying solely on keyword matching.

- **Temporal (+7.6pp from journal injection):** Journal archives preserve exact dates and temporal context that get compressed during snippet distillation. Journal+Haiku **beats full-context Haiku** on temporal queries (72.3% vs 60.8%) -- structured date-normalized facts plus raw temporal narratives together outperform raw transcript search.

- **Single-hop (+6.8pp from journal):** The biggest per-question-count gain. Journal entries provide additional detail and phrasing that helps surface the right answer even for direct lookup queries.

### Where Mem0 Wins

- **Open-domain (-19.8pp):** These questions require inference and reasoning from retrieved facts rather than direct lookup. This is the one category where Mem0 consistently outperforms Quaid.

---

## Journal Impact

The journal system adds archived journal entries (~40-50% more context) alongside distilled core markdown. The cost/benefit tradeoff:

| Config | Accuracy | Avg Tokens/Query | Relative Cost |
|--------|----------|-------------------|---------------|
| Standard (core markdown only) | 69.1% | ~3,725 | 1x |
| Journal (core markdown + archives) | 74.5% | ~10,079 | 2.7x |
| Opus answers (no journal) | 75.0% | ~3,725 | ~5x |

**Key insight:** Journal+Haiku (74.5%) nearly matches Opus answers (75.0%) at roughly half the cost. Journal helps most on temporal (+7.6pp) and single-hop (+6.8pp) questions.

---

## Memory Efficiency

How much of the full-context baseline performance does Quaid retain?

| Category | Standard | Journal | Opus |
|----------|----------|---------|------|
| Single-hop | 78.1% | 85.6% | 84.7% |
| Multi-hop | 98.3% | 98.2% | 93.3% |
| Temporal | 106.4% | 118.9% | 85.5% |
| Open-domain | 94.2% | 96.7% | 91.3% |
| **Overall** | **86.8%** | **93.6%** | **86.6%** |

Journal mode captures 93.6% of full-context performance while using ~2.6x fewer tokens per query.

Values over 100% mean Quaid's structured memory **outperforms** having the full raw transcript.

---

## Statistical Rigor

All results are from 3 independent trials with Wilson Score 95% confidence intervals (recommended by ICML 2025 spotlight paper over CLT-based CIs for LLM evaluation).

| Config | Mean +/- Std | 95% CI |
|--------|-------------|--------|
| Quaid + Journal | 74.48% +/- 0.05 | [72.2%, 76.6%] |
| Quaid + Opus | 75.00% +/- 0.21 | [72.5%, 76.8%] |
| Quaid + Haiku | 70.28% +/- 0.06 | [68.0%, 72.5%] |
| FC-Haiku | 79.59% +/- 0.17 | [77.3%, 81.3%] |
| FC-Opus | 86.62% +/- 0.09 | [84.8%, 88.2%] |

The 95% CI lower bound for Quaid+Journal (72.2%) exceeds Mem0's point estimate (66.9%) by 5.3pp.

---

## Evaluation Pipeline

```
Full conversation transcripts
    |
    v
Opus extracts facts + edges + snippets + journal entries
    |
    v
Store in SQLite (embeddings via Ollama, edges in graph)
    |
    v
Full janitor pipeline per conversation:
  - Embeddings backfill
  - Temporal date resolution
  - Duplicate detection (Haiku reranker)
  - Ebbinghaus confidence decay
  - Snippet review (FOLD/REWRITE/DISCARD)
  - Journal distillation (Opus)
    |
    v
Hybrid recall: FTS5 + sqlite-vec + RRF fusion + Haiku reranker + HyDE
    |
    v
Answer generation (Haiku or Opus)
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

Mem0 uses GPT-4o-mini for answer generation. We report both Haiku answers (similar tier, fair comparison) and Opus answers (matches production config).

---

## References

- **LoCoMo:** Maharana et al. (ACL 2024), "LoCoMo: Long-Context Conversations with Memory-Enhanced LLMs"
- **Mem0 evaluation:** Chadha et al. (Apr 2025), Mem0 technical report
- **Wilson Score CIs:** ICML 2025 spotlight paper on statistical rigor in LLM evaluation
