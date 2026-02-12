# Quaid Benchmark Results

## LoCoMo (ACL 2024)

[LoCoMo](https://github.com/snap-research/locomo) is an industry-standard benchmark for conversational memory systems. It consists of 10 long conversations (5,882 turns) with 1,540 scored question-answer pairs across four categories: single-hop lookup, multi-hop reasoning, temporal reasoning, and open-domain inference.

We evaluate Quaid using **Mem0's exact methodology** -- same judge model (GPT-4o-mini), same prompt, same temperature (0.0), same JSON response format, same scoring. This makes the comparison peer-review valid.

---

## Results

| System | Accuracy | Notes |
|--------|----------|-------|
| **Quaid** | **75.0%** | Recommended settings (Opus reasoning + Ollama embeddings) |
| Quaid (Haiku answers) | 70.3% +/- 0.06 | Low-reasoning LLM for answer generation |
| Mem0 (graphRAG) | 68.9% | Apr 2025 |
| Mem0 | 66.9% +/- 0.7 | Apr 2025, 10 trials |
| Zep | 66.0% | Apr 2025 |
| LangMem | 58.1% | Apr 2025 |
| OpenAI Memory | 52.9% | Apr 2025 |

Full-context baselines (upper bound, no memory system): Haiku 79.6%, Opus 86.6%.

The shipping configuration uses Opus for both extraction and answer generation. Quaid achieves 87% of full-context Opus performance while injecting only the relevant memories.

> Mem0, Zep, LangMem, and OpenAI numbers are from their [April 2025 paper](https://arxiv.org/abs/2504.01094).

---

## Per-Category Breakdown

| Category | Questions | Quaid (Opus) | Quaid (Haiku) | Mem0 | Mem0^g | Zep |
|----------|-----------|--------------|---------------|------|--------|-----|
| Single-hop | 841 | **78.6%** | 73.0% | 67.1% | 65.7% | 61.7% |
| Multi-hop | 282 | **75.1%** | 72.1% | 51.1% | 47.2% | 41.4% |
| Temporal | 321 | **70.7%** | 65.1% | 55.5% | 58.1% | 49.3% |
| Open-domain | 96 | 58.0% | 58.7% | **72.9%** | **75.7%** | **76.6%** |
| **Overall** | **1540** | **75.0%** | **70.3%** | **66.9%** | **68.9%** | **66.0%** |

### Where Quaid Wins

- **Multi-hop (+23pp vs Mem0):** Graph edges extracted at capture time enable traversal across related entities. When asked about a person's child's preferences, Quaid follows `person -> child_of -> preference` edges rather than relying solely on keyword matching.

- **Temporal (+15pp vs Mem0):** Structured date-normalized facts and temporal resolution in the janitor pipeline preserve exact dates that raw keyword search would miss.

- **Single-hop (+11pp vs Mem0):** Hybrid retrieval (vector + FTS + graph) with LLM reranking consistently surfaces the right fact for direct lookup queries.

### Where Mem0 Wins

- **Open-domain (-15pp):** These questions require inference and reasoning from retrieved facts rather than direct lookup. This is the one category where Mem0 consistently outperforms Quaid.

---

## Journal A/B Test

Early data from an A/B test of the journal system, which injects archived journal entries (~40-50% more context) alongside distilled core markdown:

| Config | Accuracy | Avg Tokens/Query | Relative Cost |
|--------|----------|-------------------|---------------|
| Standard (core markdown only) | 69.1% | ~3,725 | 1x |
| Journal (core markdown + archives) | 74.5% | ~10,079 | 2.7x |
| Recommended (Opus, no journal) | 75.0% | ~3,725 | ~5x |

Journal injection showed a +5.4pp improvement with Haiku answers, nearly matching Opus. However, the 2.7x token increase raises scalability concerns for long-running agents with large journal archives. We are actively working to make journal injection more cost-effective before recommending it as a default configuration.

---

## Memory Efficiency

How much of the full-context baseline performance does Quaid retain?

| Category | Quaid (Haiku) | Quaid (Opus) |
|----------|---------------|--------------|
| Single-hop | 78.1% | 84.7% |
| Multi-hop | 98.3% | 93.3% |
| Temporal | 106.4% | 85.5% |
| Open-domain | 94.2% | 91.3% |
| **Overall** | **86.8%** | **86.6%** |

The recommended Opus configuration captures 87% of full-context performance while injecting only the relevant memories -- typically a few thousand tokens instead of the entire transcript.

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

The 95% CI lower bound for Quaid+Opus (72.5%) exceeds Mem0's point estimate (66.9%) by 5.6pp.

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
  - Duplicate detection (low-reasoning LLM reranker)
  - Ebbinghaus confidence decay
  - Snippet review (FOLD/REWRITE/DISCARD)
  - Journal distillation (high-reasoning LLM)
    |
    v
Hybrid recall: FTS5 + sqlite-vec + RRF fusion + LLM reranker + HyDE
    |
    v
Answer generation (low-reasoning or high-reasoning LLM)
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

Mem0 uses GPT-4o-mini for answer generation. We report both Haiku answers (similar tier, fair comparison) and Opus answers (recommended production config).

---

## Context & Roadmap

These results are from early alpha testing (February 2026). We are actively:

- **Testing and refining** extraction quality, retrieval parameters, and janitor decisions against LoCoMo
- **Evaluating journal scalability** -- the journal A/B test shows promise but token costs need optimization for long-running agents

Quaid conserves context across restarts -- when the agent compacts or resets, memories are extracted before the context is cleared. A full crash before compaction can cause memory loss for that session.

Because the system leans heavily on LLM reasoning for its decisions, results naturally improve as AI models improve -- without code changes.

---

## References

- **LoCoMo:** Maharana et al. (ACL 2024), "LoCoMo: Long-Context Conversations with Memory-Enhanced LLMs"
- **Mem0 evaluation:** Chadha et al. (Apr 2025), Mem0 technical report
- **Wilson Score CIs:** ICML 2025 spotlight paper on statistical rigor in LLM evaluation
