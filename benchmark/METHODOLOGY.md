# Benchmark Methodology: Quaid vs OpenClaw Baseline Memory

**Version:** 2.0
**Date:** February 2026
**Author:** Test User (system design), Claude Code (implementation)

---

## 1. Study Objective

Quantify the recall quality improvement of the Quaid memory system over the OpenClaw built-in baseline memory, using identical test data, identical queries, and identical evaluation metrics across both systems.

The null hypothesis is: *Quaid provides no meaningful improvement in memory recall quality over the standard OpenClaw baseline.* We aim to reject this hypothesis with measurable evidence across multiple IR metrics.

---

## 2. Systems Under Test

### 2.1 Quaid (Treatment)

The Quaid memory plugin (`plugins/quaid/`), a purpose-built memory system with:

- **Storage:** SQLite with per-fact nodes, FTS5 full-text search, sqlite-vec ANN vector search
- **Recall pipeline:** Multi-stage hybrid search with Reciprocal Rank Fusion (RRF)
  - HyDE query expansion (hypothetical answer for better vector match)
  - Intent classification (factual, entity, temporal, why, preference)
  - Dynamic fusion weights per query intent
  - Haiku API graded reranker (0-5 relevance scale)
  - Entity alias resolution (e.g., "Mom" → "Linda")
  - Multi-pass retrieval (confidence-gated second pass)
  - Temporal contiguity (co-session facts surface together)
  - Graph path explanation (edge traversal for related entities)
- **Maintenance:** Nightly janitor pipeline (dedup, contradiction detection, Ebbinghaus decay, entity summaries)
- **Granularity:** Individual facts (~30-120 characters each)

### 2.2 OpenClaw Baseline (Control)

A faithful Python reimplementation of OpenClaw's built-in memory system (`runner/baseline/`), which provides:

- **Storage:** Markdown journal files, chunked and indexed in SQLite
- **Recall pipeline:** Simple hybrid search
  - 70% vector similarity + 30% BM25 text matching
  - AND-based FTS5 queries (all tokens must match)
  - Minimum score threshold of 0.35
  - No reranking, no HyDE, no entity resolution, no graph traversal
- **Maintenance:** None (no dedup, no contradiction handling, no decay)
- **Granularity:** Line-based markdown chunks (~800-1600 characters each)

### 2.3 Reimplementation Fidelity

The baseline reimplementation was built from the OpenClaw source code with the following verified correspondences:

| Component | OpenClaw Source | Baseline Implementation |
|-----------|----------------|------------------------|
| Chunking | `internal.ts:chunkMarkdown` | `baseline/indexer.py:chunk_markdown()` |
| Max chunk size | 1600 chars | 1600 chars (`MAX_CHARS`) |
| Overlap | 320 chars | 320 chars (`OVERLAP_CHARS`) |
| Hybrid weights | `hybrid.ts` VECTOR_WEIGHT=0.7, TEXT_WEIGHT=0.3 | Identical constants |
| FTS query | `hybrid.ts` AND-joined quoted tokens | `build_fts_query()` — identical logic |
| BM25 normalization | `1 / (1 + max(0, rank))` | `bm25_to_score()` — identical formula |
| Candidate multiplier | 4x limit | 4x limit (`CANDIDATE_MULTIPLIER`) |
| Default limit | 6 results | 6 results (`DEFAULT_LIMIT`) |
| Min score threshold | 0.35 | 0.35 (`MIN_SCORE`) |
| Schema | `memory-schema.ts` (chunks + FTS5 + vec0) | Matching SQLite schema |

---

## 3. Test Data

### 3.1 Fact Corpus (from `scenarios.py`)

The test corpus is generated from parameterized templates, producing deterministic facts per week:

| Category | Templates | Per Week (fast) | Per Week (full) |
|----------|-----------|-----------------|-----------------|
| Facts | 240 | ~36 | ~60 |
| Preferences | 49 | ~8 | ~12 |
| Entities | 9 | ~1 | ~2 |
| Decisions | 7 | ~1 | ~2 |
| **Total** | **305** | **~46** | **~76** |

Template categories include: workplace, health, finance, travel, media, relationships, seasonal, and evolving opinions.

Additional injections per week:
- **Week 0:** 10 EDGE_FACTS (family, friends, pets — clear entity relationships)
- **Week 1+:** 3-6 duplicate pairs (same meaning, different wording)
- **Week 2+:** 2-4 contradiction pairs (same subject, conflicting claims)
- **Per week:** Evolution chain facts (location changes, job changes, etc.)

Templates use placeholder substitution (`{NAME}`, `{FOOD}`, `{TECH}`, `{AMOUNT}`, `{PLACE}`, `{DATE}`, `{WEEK}`) with deterministic seeded randomization (`random.Random(hash(template) + week_num)`), ensuring reproducibility across runs.

### 3.2 Data Delivery to Each System

**Quaid** receives facts via `memory_graph.store()` — each fact becomes an individual node in the graph database with metadata (category, knowledge_type, source_type, created_at, owner_id, confidence).

**Baseline** receives the same facts formatted as structured markdown journal files:

```
memory/2025-01-06.md    (Mon — ~15 facts)
memory/2025-01-08.md    (Wed — ~15 facts)
memory/2025-01-10.md    (Fri — remaining facts)
```

Each journal file uses realistic structured markdown with headers:
```markdown
# 2025-01-06

## Summary
Notes from Monday — 15 items logged.

## What Happened
- Went for a 35-minute run in week 0 on January 08
- Had pad thai for dinner on January 11
...

## People
- Alex is my coworker and we work on the Python project together
- Mom Linda lives in Portland and works as a teacher

## Preferences & Decisions
- Prefer pad thai over other lunch options
- Decided to switch to TypeScript for the backend
```

Additionally, baseline receives a `MEMORY.md` generated from EDGE_FACTS, analogous to the curated core memory file real OpenClaw users maintain:
```markdown
# Core Memories

## Family
- Mom Linda lives in Portland and works as a teacher
- Dad is an engineer at Boeing in Seattle

## Friends & Colleagues
- Alex is my coworker and we work on the Python project together
...
```

### 3.3 Fairness of Data Delivery

| Concern | Resolution |
|---------|------------|
| Format advantage | Journals use realistic structured markdown (not unfairly sparse or dense) |
| MEMORY.md inclusion | Both systems get equivalent core entity data (Quaid via entity nodes + edges, baseline via MEMORY.md) |
| Evolution chains | Both systems receive old AND new facts — baseline keeps both in journals, Quaid dedup/contradiction resolution is the feature being tested |
| Fact completeness | Every fact from `get_week_facts()` appears in both systems |
| Randomization | Deterministic seeds ensure identical facts across runs |

---

## 4. Query Sets

### 4.1 Standard Recall (60 queries)

Broad topic queries with expected substring matches: "What exercise did I do?" → expects ["run", "yoga", "gym", ...]. Tests general-purpose recall across all fact categories.

### 4.2 Old Memory Recall (20 queries)

Queries targeting week-0 EDGE_FACTS after weeks of noise: "Where does Mom live?" → expects ["Portland", "Linda"]. Tests memory persistence over time.

### 4.3 Adversarial (20 queries, 3 difficulty tiers)

| Difficulty | Count | Types |
|-----------|-------|-------|
| Easy | 1 | Keyword stuffing (many keywords in query) |
| Medium | 9 | Entity ambiguity, synonym mismatch, alias resolution, compound multi-topic, temporal ambiguity, temporal month/start, comparison |
| Hard | 10 | Colloquial phrasing, negation, maximally vague, pronoun confusion, multi-hop entity/workplace, conversational |

Hard queries with no expected substrings are scored as N/A (not counted against either system).

### 4.4 Change Detection (11 queries)

- **Current State (6):** Should return LATEST fact only (e.g., "Where does Alex live now?" → "Austin", not "San Francisco")
- **Change Awareness (5):** Should surface BOTH old and new facts. Validated with split keywords — `old_keywords` (only in old fact) AND `new_keywords` (only in new fact) must both appear in results.

### 4.5 Null Queries (10 queries)

Queries that should NOT match anything in the corpus, for false positive rate testing. Examples: "What color is my neighbor's roof?" or "Did I meet the president?". Validates that systems don't hallucinate or return spurious results.

### 4.6 Query Filtering

Change detection queries are filtered by `max_week` — queries requiring evolution facts from weeks not yet simulated are excluded. This prevents false failures when running shorter benchmarks.

---

## 5. Metrics

### 5.1 Standard IR Metrics

| Metric | Definition | Range |
|--------|-----------|-------|
| **MRR** (Mean Reciprocal Rank) | Average of 1/rank_of_first_relevant_result | 0-1 (higher = better) |
| **Recall@K** (K=1,3,5) | Fraction of queries where any result in top-K is relevant | 0-1 |
| **Precision@5** | Average fraction of top-5 results that are relevant | 0-1 |
| **NDCG@5** | Normalized Discounted Cumulative Gain (position-weighted relevance) | 0-1 |

### 5.2 Domain-Specific Metrics

| Metric | Definition |
|--------|-----------|
| **Old Memory Recall@5** | Fraction of week-0 queries finding relevant results after N weeks of noise |
| **Adversarial Hit Rate** | Fraction of adversarial queries (with expected subs) finding relevant results |
| **Current State Accuracy** | Fraction of evolution queries returning the LATEST fact |
| **Change Awareness** | Fraction of evolution queries surfacing BOTH old AND new facts |

### 5.3 Quality Metrics (New for Comparison)

| Metric | Definition | Why It Matters |
|--------|-----------|---------------|
| **Relevance Density** | `keyword_chars / total_result_chars` per hit | Measures signal-to-noise ratio. A 1600-char chunk with a 10-char keyword match scores ~0.006. A 30-char Quaid fact with the same keyword scores ~0.33. |
| **Token Spend/Query** | `total_result_chars / 4` (standard token estimate) | Measures context window cost. Results injected into LLM context consume tokens — fewer is better. |
| **Token Efficiency Ratio** | `baseline_tokens / quaid_tokens` | How many times more efficient Quaid is in context usage |

### 5.4 Operational Metrics

| Metric | Definition |
|--------|-----------|
| **Avg Latency (ms)** | Mean wall-clock time per search call |

---

## 6. Relevance Scoring

### 6.1 Result-Level Relevance (Binary)

A result is **relevant** if any expected substring appears (case-insensitive) in the result text:

```python
def _check_result_relevance(result_text, expected_subs):
    text_lower = result_text.lower()
    return any(sub.lower() in text_lower for sub in expected_subs)
```

### 6.2 Known Bias and Mitigation

**Bias:** Substring matching in large chunks is easier — a 1600-char chunk containing 10+ facts will match many keywords even when the search didn't specifically "find" the answer. The chunk just happened to contain the keyword.

**Mitigation:** Relevance density penalizes this naturally. If a system returns a 1600-char chunk where only 10 characters are the matched keyword, its density is 0.006. A system returning a 30-char focused fact scores 0.33 — a 55x better signal-to-noise ratio.

Both binary and density metrics are reported, allowing the reader to interpret results at both granularities.

### 6.3 Change Detection Scoring

Change awareness requires finding keywords from BOTH the old and new fact in the combined result text:

```python
found_old = any(kw.lower() in combined_text for kw in old_keywords)
found_new = any(kw.lower() in combined_text for kw in new_keywords)
aware = found_old and found_new
```

This prevents false positives where a single new fact (e.g., "Alex moved to Austin last month") satisfies both criteria because it mentions both the old location and new location.

---

## 7. Experimental Procedure

### 7.1 Environment Isolation

```bash
# Isolated test environment (not production)
export MEMORY_DB_PATH=<test_dir>/test-memory.db  # Fresh DB per run
export QUAID_WORKSPACE=<workspace>             # Isolated workspace
export HOME=<stress-env>                           # Isolated home
export MOCK_EMBEDDINGS=1                           # Optional: deterministic embeddings
```

Safety check prevents running against production database:
```python
if os.path.abspath(db_path) == os.path.abspath(PRODUCTION_DB):
    abort("FATAL: MEMORY_DB_PATH points to production database!")
```

### 7.2 Execution Pipeline

```
Phase 1:  Generate journals → baseline/memory/*.md + MEMORY.md
Phase 2:  Build baseline index → baseline/baseline.db (chunk + embed)
Phase 3:  Feed Quaid → test-memory.db (store() per fact)
Phase 3a: (optional) Golden checkpoint — save DB snapshot per week (if --golden)
Phase 4:  Run all query sets against BOTH systems
Phase 5:  Compute relevance density + token spend
Phase 6:  Compute deltas, print report, save JSON
Phase 7:  (optional) LLM-as-judge graded evaluation
Phase 8:  (optional) LLM-generated unbiased queries
Phase 9:  (optional) Ablation study — feature contribution
Phase 10: (optional) Context budget analysis
Phase 11: (optional) Blind A/B agent quality comparison
Phase 12: (optional) Golden checkpoint parameter sweeps (if --golden)
```

### 7.3 Embedding Modes

| Mode | Flag | Use Case |
|------|------|----------|
| **Mock** | `--mock-embeddings` | CI/development testing. Deterministic MD5-based 128-dim vectors. Both systems use identical mock embeddings. |
| **Real** | (default) | Production benchmark. Ollama `qwen3-embedding:8b` for both systems. Requires running Ollama instance. |

Mock embeddings produce arbitrary similarity scores and are NOT valid for benchmarking recall quality. They verify pipeline correctness only. Real embeddings are required for valid comparison results.

### 7.4 Reproducibility

Reproducing a benchmark run:

```bash
# 1. Source the test environment
source memory-stress-test/test.env

# 2. Override DB path for fresh run
export MEMORY_DB_PATH=/path/to/fresh/test-memory.db

# 3. Run comparison (real embeddings, 4 weeks)
python3 memory-stress-test/runner/run_baseline_comparison.py \
    --weeks 4 --mode fast

# 4. Results saved to <db_dir>/baseline-comparison.json
```

All randomization uses seeded RNGs (`random.Random(42 + week_num)` for fact selection, `random.Random(hash(template) + week_num)` for template filling), ensuring identical facts across runs.

### 7.5 Audit Trail

- `--dump-journals` flag prints all generated journal files for human review of fairness
- `baseline-comparison.json` contains full per-query results for both systems
- Journal files are preserved in `<results_dir>/baseline/memory/` for post-hoc inspection

---

## 8. Scorecard Methodology

Per-metric wins are counted with a tie threshold of 0.005 (for rate metrics) or 1.0 (for token/latency metrics):

| Metric | Higher = Better? | Threshold |
|--------|-----------------|-----------|
| MRR, Recall@K, Precision@5, NDCG@5 | Yes | 0.005 |
| Old Memory Recall@5 | Yes | 0.005 |
| Adversarial Hit Rate | Yes | 0.005 |
| Current State Accuracy | Yes | 0.005 |
| Change Awareness | Yes | 0.005 |
| Relevance Density | Yes | 0.005 |
| Token Spend/Query | No (lower = better) | 1.0 |
| Avg Latency | No (lower = better) | 1.0 |

Total 13 metrics scored. A system "wins" the benchmark if it wins more metrics than the other. The scorecard counts wins, losses, and ties.

---

## 9. Credibility Enhancements (v1.1)

Four enhancements address validity weaknesses identified in self-critique: binary substring matching, hand-crafted query bias, no statistical significance testing, and no ablation showing component contributions.

### 9.1 Bootstrap Confidence Intervals + Significance Tests

**File:** `runner/bench_statistics.py` — Pure Python, zero cost, always runs.

Every comparison metric is annotated with:
- **95% Bootstrap CI:** Resamples per-query scores 1000 times, reports [lower, upper] bounds for the mean.
- **Paired Permutation Test:** Two-sided test (10,000 permutations) comparing Quaid vs Baseline per-query score distributions. Reports p-value with significance stars (* p<0.05, ** p<0.01, *** p<0.001).

Per-query scores are extracted from existing `details` lists:

| Metric | Extraction | Source |
|--------|-----------|--------|
| MRR | `1/first_hit_rank` if hit else `0.0` | `details[].first_hit_rank` |
| Precision@5 | `relevant_in_top5 / 5` | `details[].relevant_in_top5` |
| Recall@5 | `1.0` if hit else `0.0` | `details[].hit` |
| Old memory | `1.0` if hit else `0.0` | `details[].hit` |
| Adversarial | `1.0` if hit else `0.0` | `details[].hit` (only with expected_subs) |
| Change awareness | `1.0` if aware else `0.0` | `change_awareness_details[].change_aware` |

### 9.2 LLM-as-Judge Graded Evaluation

**File:** `runner/llm_judge.py` — Haiku API, ~$0.03/run. Flag: `--llm-judge`.

Replaces binary substring matching with 5-point Haiku relevance grading. Single biggest credibility boost — addresses the "large chunks match everything" bias.

- Each query's top-5 results are sent to Haiku in a single call
- Neutral prompt (no system identification): "Grade each search result's relevance to the query on a 1-5 scale"
- Scale: 5=Directly answers, 4=Highly relevant, 3=Partially relevant, 2=Tangentially related, 1=Not relevant
- Results truncated to 500 chars per result

Graded metrics:
| Metric | Definition |
|--------|-----------|
| Graded MRR | Reciprocal rank of first result with grade >= 4 |
| Graded NDCG@5 | NDCG using 1-5 relevance grades instead of binary |
| Graded Precision@5 | Sum of grades / 25 (max 5 results x 5 grade) |
| Graded Mean Relevance | Average grade across all results |

### 9.3 LLM-Generated Unbiased Queries

**File:** `runner/query_generator.py` — Haiku API, ~$0.001/run. Flag: `--generated-queries`.

Hand-crafted queries may be biased toward Quaid's features (entity resolution, temporal reasoning). Generated queries come from the fact corpus directly, with no feature-specific framing.

- Feeds ~100 sampled facts to Haiku
- Prompt frames user as "you told your AI these facts over weeks" — no mention of any Quaid feature
- Generates 30 natural queries with expected keywords
- Cached to `results/generated-queries.json` for reproducibility
- When enabled, runs BOTH hand-crafted AND generated queries — two separate sections in the report

### 9.4 Ablation Study (Feature Contribution)

**File:** `runner/ablation.py` + `memory_graph.py` recall() flags. Flag: `--ablation`.

Measures each Quaid feature's individual contribution by disabling one feature at a time. Zero LLM cost (reuses existing pipeline with feature flags).

New `recall()` parameters:
| Parameter | Default | Controls |
|-----------|---------|----------|
| `use_aliases` | `True` | Entity alias resolution (Mom → Linda) |
| `use_intent` | `True` | Query intent classification + dynamic fusion weights |
| `use_multi_pass` | `True` | Second-pass broader search on low-quality results |
| `use_reranker` | `None` | Override config reranker_enabled |

Ablation variants:
| Variant | Disabled Feature |
|---------|-----------------|
| `full` | None (baseline) |
| `no_hyde` | HyDE query expansion (`use_routing=False`) |
| `no_aliases` | Entity alias resolution |
| `no_reranker` | Cross-encoder reranking |
| `no_multi_pass` | Multi-pass retrieval |
| `no_intent` | Intent classification |

Report sorts variants by MRR impact (most impactful feature first).

### 9.5 BEAM Search Graph Traversal

**File:** `memory_graph.py` (integrated) — Free (heuristic mode), ~$0.001/hop (LLM mode).

Graph traversal for entity-related queries replaced breadth-first search (BFS) with scored BEAM search. BEAM search expands only the most promising paths at each depth level.

**Scoring Modes:**
| Mode | Cost | Method |
|------|------|--------|
| **Heuristic** | Free | Edge strength (0.3) + node confidence (0.7) |
| **LLM** | ~$0.001/hop | Haiku grades path relevance to query (1-5 scale) |
| **Hybrid** | Mixed | LLM only when top-2 heuristic candidates score within 0.1 |

**Config Parameters:**
- `traversal.useBeam` (default: `true`) — Enable BEAM search
- `traversal.beamWidth` (default: `5`) — Number of paths to keep per depth level
- `traversal.maxDepth` (default: `2`) — Maximum hops from seed nodes
- `traversal.scoringMode` (default: `"heuristic"`) — `"heuristic"`, `"llm"`, or `"hybrid"`

BEAM search reduces graph traversal noise — only the most promising entity relationships surface in results, improving precision for entity-centric queries.

### 9.6 Config Externalization

**File:** `config/memory.json` (new section: `retrieval.constants`)

All 15+ retrieval constants previously hardcoded in `memory_graph.py` moved to config. Enables parameter sweeps without code changes.

**Externalized Constants:**
- Fusion weights (vector, FTS, graph)
- Multi-pass thresholds (confidence, MRR)
- Search limits (vector, FTS, multi-pass)
- Graph traversal parameters (max depth, beam width)
- Reranker thresholds

This change enables golden checkpoint parameter sweeps (v2.0 enhancement 9.7).

### 9.7 Golden Dataset Checkpoints

**File:** `runner/golden_checkpoints.py` — Zero cost (pure DB operations).

Per-week database snapshots during benchmark runs. Enables 150x faster parameter sweeps (~2s vs ~5min per parameter value per week).

**Method:**
1. After feeding Quaid each week's facts, checkpoint the entire DB to `results/golden/week_NNN/golden.db`
2. Parameter sweeps restore from checkpoint instead of re-feeding facts
3. Each parameter value is tested against the same frozen memory state

**New Flag:** `--golden` — Enable checkpoint capture and restore.

**Limitations:**
- Checkpoints cannot tune Ollama-dependent features (embeddings, reranker) — those require full re-runs
- Checkpoints are config-specific — changing fact templates invalidates existing checkpoints

**Files:** `golden_checkpoints.py`, `test_checkpoints.py`, `test_golden_integration.py`, `example_parameter_sweep.py`.

### 9.8 Headline Metrics

Three top-level numbers summarizing benchmark results:

| Metric | Definition | Purpose |
|--------|-----------|---------|
| **Accuracy** | Composite score: MRR (30%) + Recall@5 (25%) + P@5 (20%) + Adversarial (15%) + Old Memory (10%) | Single number for "how good is recall?" |
| **Token Spend** | Average tokens per query result set | Context window cost |
| **Efficiency Ratio** | `baseline_tokens / quaid_tokens` | How much more efficient is Quaid? |

Accuracy provides a single headline number for media/exec summary. Component metrics remain available in detailed results.

### 9.9 Simulated Bloat Growth

**File:** `runner/simulate_bloat.py` — Pure simulation, $0/run.

Core markdown bloat simulation under two conditions:

| Condition | Method |
|-----------|--------|
| **Unmanaged** | Bloat grows unchecked (10-15 lines/week across SOUL.md, USER.md, MEMORY.md) |
| **Quaid-managed** | Workspace audit enforces maxLines limits via snippet system |

Calibrated from real NAS backup data (SOUL.md grew from 120 → 380 lines over 3 months before Quaid deployment).

Tests core markdown capacity limits under realistic growth patterns. Validates that snippet system keeps files within operational bounds.

### 9.10 Clean Baseline Environment

**Files:** `fixtures/blank-slate/*.md`

Test environment uses blank-slate templates from OpenClaw defaults, replacing production-specific SOUL.md/USER.md content. Ensures benchmark results generalize beyond the user's personal workspace.

Blank-slate files have minimal content (20-30 lines each), matching a fresh OpenClaw installation. All test facts are injected during benchmark runs — no pre-existing production context.

---

## 10. Beyond Retrieval: Evaluating System-Level Quality

Sections 1-9 focus on **memory retrieval** — one of four Quaid subsystems. The remaining three (journal/snippets, projects, core markdown management) improve agent quality and operational health but are harder to quantify with IR metrics. This section documents our approach to measuring them.

### 10.1 Agent Quality — Blind A/B Comparison

**File:** `runner/agent_quality.py` — Opus + Haiku, ~$1.50/run. Flag: `--agent-quality`.

Tests whether journal/snippet-enriched context produces measurably better agent responses.

**Method (Chatbot Arena-style):**
1. Construct two system prompts from the workspace's core markdown files:
   - **Enriched:** Core markdown (SOUL.md, USER.md, MEMORY.md) + journal entries + snippet content + recalled memories
   - **Minimal:** Core markdown only (no journal, no snippets, no recalled memories)
2. Send 10 identical user messages to Opus with each system prompt
3. Haiku judges response pairs blindly ("Response X" / "Response Y", randomized order)
4. Score on 4 dimensions independently + overall preference

**Evaluation Dimensions:**
| Dimension | 1 (Low) | 5 (High) |
|-----------|---------|----------|
| Personalization | Generic response | Deeply personal, specific to user |
| Self-awareness | No assistant reflection | Rich observations about the relationship |
| Emotional intelligence | Robotic tone | Deeply empathetic attunement |
| Helpfulness | Unhelpful | Excellent address of user needs |

**Test Probes:** 10 messages across 5 categories: self-awareness (3), personalization (2), memory integration (2), personality consistency (2), and 1 control probe (arithmetic — should show no difference).

**Statistical Analysis:** Win rate with 95% bootstrap CI. Per-dimension scores with paired permutation tests and significance stars.

**Precedent:** This approach follows MT-Bench (Zheng et al., 2023) and Chatbot Arena methodology, which demonstrate that LLM judges correlate well with human preference when order is randomized and prompts are neutral.

**Limitations:**
- Small sample size (10 probes) limits statistical power
- Single judge (Haiku) — inter-judge agreement not tested
- Synthetic context (workspace files, not real conversation history)
- Probes are designed to surface journal/snippet differences — a form of favorable framing

### 10.2 Context Budget Analysis

**File:** `runner/context_budget.py` — Pure measurement, $0/run. Flag: `--context-budget`.

Quantifies token efficiency of the projects system and core markdown management. No subjective component — this is pure measurement.

**Metrics:**
| Metric | What It Measures |
|--------|-----------------|
| Core markdown capacity | Line count vs configured maxLines per file |
| Core markdown tokens | Total tokens consumed by all core markdown files |
| Project token savings | Selective loading (one project) vs monolithic (all projects) |
| Journal injection cost | Tokens for full-mode journal injection vs distilled mode (zero) |
| Total context budget | Sum of all components with percentage breakdown |

These metrics quantify the operational cost of each subsystem in context window tokens, showing the efficiency gains from selective project loading and distilled journal mode.

### 10.3 Fact Quality — LLM-Graded Storage Quality

**File:** `runner/fact_quality.py` — Haiku, ~$0.01/run. Rides on `--llm-judge` flag.

Tests whether LLM-curated storage produces higher-quality knowledge units than raw text chunking. This validates the *input* side of the LLM advantage — extraction quality before retrieval even happens.

**Method:**
1. Sample 50 items from each system (Quaid: stratified by node type, Baseline: random chunks)
2. Interleave items across batch calls for blinding (5 Quaid + 5 Baseline per call, shuffled)
3. Haiku grades each item on 5 dimensions (1-5 scale)

**Dimensions:**
| Dimension | Weight | What It Measures |
|-----------|--------|-----------------|
| Atomicity | 0.25 | Is this a single, self-contained fact? |
| Clarity | 0.20 | Is the meaning unambiguous and well-formed? |
| Specificity | 0.20 | Does it contain concrete names, dates, amounts? |
| Self-Sufficiency | 0.20 | Can you understand it without surrounding context? |
| Dedup Quality | 0.15 | Is this information unique and non-redundant? |

**Output:** Per-dimension means, weighted overall score, and delta between systems. Items are fully blinded — the judge never knows which system produced which item.

**Why this matters:** Quaid stores atomic, curated facts; the baseline stores large text chunks with overlap. The fact quality metric directly quantifies this architectural difference.

### 10.4 Mem0 Integration (Optional Third Competitor)

**File:** `runner/mem0_adapter.py` — Flag: `--mem0`. Requires `pip install mem0ai`.

Mem0 is a popular open-source memory layer that also uses LLM extraction at add-time. Including it provides a three-way comparison: Quaid vs Baseline (no LLM) vs Mem0 (different LLM approach).

**Key differences from Quaid:**
- Mem0 uses its own LLM to extract/distill memories from conversations at ingest time
- Uses Qdrant vector store (in-memory for benchmark isolation)
- No graph traversal, no multi-pass retrieval, no Haiku reranker, no FTS fusion

**Configuration:** Reads from environment variables (MEM0_LLM_PROVIDER, MEM0_LLM_MODEL, etc.). Default: Anthropic Haiku for extraction, Ollama for embeddings.

**Note:** Mem0's LLM extraction at add-time is a deliberate design choice. The benchmark tests Mem0's full pipeline (extraction + storage + retrieval) — this is the fair comparison since Mem0's whole value proposition is the integrated pipeline.

---

## 10.5 Design Philosophy: LLM-as-Infrastructure

A key architectural insight underlying Quaid is that **LLM API calls are infrastructure components, not luxuries**. Every stage of the memory lifecycle uses an LLM call:

| Stage | LLM Call | Purpose |
|-------|----------|---------|
| **Input** | Opus extraction | Parse conversations into atomic, structured facts |
| **Curation** | Opus review | Evaluate pending facts for quality and relevance |
| **Dedup** | Haiku verification | Confirm near-duplicate pairs before merging |
| **Retrieval** | Haiku reranker | Grade candidate results 0-5 for query relevance |
| **Traversal** | Haiku scoring (hybrid mode) | Score graph expansion candidates during BEAM search |
| **Maintenance** | Opus workspace audit | Monitor and trim core markdown files |
| **Distillation** | Opus journal review | Extract themes from journal entries into core context |

This architecture has a structural advantage: **every component automatically improves as LLM models improve**. When a cheaper, faster, or more capable model becomes available, every pipeline stage benefits — no code changes required. This is plug-and-play quality scaling.

The benchmark validates this at two points:
- **Fact quality metric (Section 10.3):** Measures input quality — does LLM extraction produce better stored facts?
- **LLM-as-judge grading (Section 5.2):** Measures output quality — does LLM reranking surface better results?

Together, these demonstrate that the LLM infrastructure investment pays off at both ends of the pipeline.

---

## 11. Limitations and Caveats

### 11.1 Known Limitations

1. **Synthetic data:** Test facts are generated from templates, not real conversations. Real user data has more varied language and context.

2. **Baseline has no maintenance pipeline:** In production, OpenClaw's baseline accumulates all facts forever with no dedup or contradiction resolution. Our benchmark tests this faithfully but doesn't simulate degradation over months of use.

3. **Single-user scenario:** All facts belong to owner "default". Multi-user recall isolation is not tested.

4. **Mock embedding caveat:** Mock embeddings produce meaningless similarity scores. Only real embedding runs (Ollama) produce valid recall quality comparisons.

5. **LLM judge consistency:** Haiku grades are stochastic — re-running with the same inputs may produce slightly different grades. CIs and permutation tests help quantify this uncertainty.

6. **BEAM search ablation:** BEAM search (v2.0) adds a new ablation dimension (BEAM vs BFS). Ablation study now measures 7 features instead of 6.

7. **Golden checkpoint constraints:** Checkpoints cannot tune Ollama-dependent features (embeddings, reranker). Those require full benchmark re-runs.

### 11.2 Potential Biases

| Bias | Direction | Magnitude | Mitigation |
|------|-----------|-----------|------------|
| Substring matching favors large chunks | Helps baseline | Medium | LLM-as-judge graded evaluation (v1.1) |
| Hand-crafted queries biased toward Quaid features | Helps Quaid | Medium | LLM-generated queries (v1.1) |
| AND-based FTS is stricter than Quaid's OR-based | Hurts baseline | Low-Medium | — |
| MEMORY.md gives baseline extra entity coverage | Helps baseline | Low | — |
| Template-generated facts lack natural language variety | Neutral | — | Both systems receive identical data |

### 11.3 What This Benchmark Does NOT Test

- Real-time extraction quality (Opus extraction pipeline)
- Graph traversal depth (edge following beyond 1 hop)
- Multi-session temporal coherence
- Nightly janitor maintenance quality over weeks
- Gateway integration (recovery, notifications)
- Cost per query (API calls for reranking)

---

## 12. Running the Benchmark

### Quick Start (Mock Embeddings — Pipeline Validation Only)

```bash
source memory-stress-test/test.env
export MEMORY_DB_PATH=/tmp/bench-test/test.db
export MOCK_EMBEDDINGS=1
python3 memory-stress-test/runner/run_baseline_comparison.py \
    --weeks 1 --mode fast --mock-embeddings
```

### Full Benchmark (Real Embeddings — Valid Results)

```bash
source memory-stress-test/test.env
export MEMORY_DB_PATH=/tmp/bench-full/test.db
# Ensure Ollama is running: curl http://localhost:11434/api/tags
python3 memory-stress-test/runner/run_baseline_comparison.py \
    --weeks 4 --mode fast
```

### Full Benchmark with All Enhancements

```bash
python3 memory-stress-test/runner/run_baseline_comparison.py \
    --weeks 4 --mode fast --llm-judge --generated-queries --ablation \
    --context-budget --agent-quality
```

### Audit Journal Fairness

```bash
python3 memory-stress-test/runner/run_baseline_comparison.py \
    --dump-journals --weeks 4 --mode fast
```

### CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--weeks N` | 1 (fast), 4 (full) | Weeks of data to simulate |
| `--mode {fast,full}` | fast | Fact density per week |
| `--mock-embeddings` | off | Use deterministic fake embeddings |
| `--dump-journals` | off | Print journals and exit (fairness audit) |
| `--skip-quaid-feed` | off | Use existing Quaid DB (for re-running queries) |
| `--owner ID` | default | Owner ID for facts |
| `--llm-judge` | off | LLM-as-judge graded evaluation (~$0.03) |
| `--generated-queries` | off | Generate unbiased queries from facts (~$0.001) |
| `--ablation` | off | Ablation study — feature contribution analysis |
| `--context-budget` | off | Context budget analysis ($0) |
| `--agent-quality` | off | Blind A/B agent quality comparison (~$1.50) |
| `--golden` | off | Capture per-week DB checkpoints + enable parameter sweeps |

---

## 13. File Map

```
memory-stress-test/
├── runner/
│   ├── baseline/
│   │   ├── __init__.py              # Package docstring
│   │   ├── journal_generator.py     # Scenarios → memory/*.md journals
│   │   ├── indexer.py               # Chunk + embed + SQLite index
│   │   └── search.py               # OpenClaw hybrid search (70/30)
│   ├── run_baseline_comparison.py   # Orchestrator (12-phase pipeline)
│   ├── validate.py                  # Shared validation (search_fn param)
│   ├── scenarios.py                 # Test data library (305 templates)
│   ├── bench_statistics.py           # Bootstrap CIs + permutation tests (v1.1)
│   ├── llm_judge.py                 # LLM-as-judge graded evaluation (v1.1)
│   ├── query_generator.py           # LLM-generated unbiased queries (v1.1)
│   ├── ablation.py                  # Ablation study framework (v1.1)
│   ├── context_budget.py            # Context budget analysis (v1.2)
│   ├── agent_quality.py             # Blind A/B agent quality comparison (v1.2)
│   ├── golden_checkpoints.py        # Per-week DB checkpoint system (v2.0)
│   ├── test_checkpoints.py          # Unit tests for checkpoint system (v2.0)
│   ├── test_golden_integration.py   # Integration tests for golden sweeps (v2.0)
│   ├── example_parameter_sweep.py   # Example parameter sweep using checkpoints (v2.0)
│   ├── simulate_bloat.py            # Core markdown bloat simulation (v2.0)
│   └── ...                          # Other stress test modules
├── fixtures/
│   └── blank-slate/                 # Clean baseline environment templates (v2.0)
│       ├── SOUL.md
│       ├── USER.md
│       └── MEMORY.md
├── BENCHMARK-METHODOLOGY.md         # This document
└── results/
    └── <run-dir>/
        ├── baseline/
        │   ├── memory/*.md          # Generated journals
        │   ├── MEMORY.md            # Generated core memory
        │   └── baseline.db          # Baseline SQLite index
        ├── test-memory.db           # Quaid test database
        ├── baseline-comparison.json # Full results
        ├── generated-queries.json   # Cached LLM-generated queries (v1.1)
        └── golden/                  # Per-week checkpoints (v2.0)
            └── week_NNN/
                └── golden.db        # Frozen DB snapshot
```
