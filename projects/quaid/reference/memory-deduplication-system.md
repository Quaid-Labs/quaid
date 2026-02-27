# Memory Deduplication System
<!-- PURPOSE: Dedup pipeline: content hash, store-time similarity, token recall, Haiku verification -->
<!-- SOURCES: janitor.py (dedup tasks), memory_graph.py (store dedup) -->

## Overview

Deduplication operates at two stages: **store-time** (before a fact enters the DB) and **nightly janitor** (batch comparison of existing facts).

---

## Store-Time Deduplication

When `store()` is called, three dedup checks run before embedding:

### 1. Content Hash (SHA256)
- Computes SHA256 of normalized text (lowercased, whitespace-collapsed)
- Checks `nodes.content_hash` for exact match within same owner
- If match found: logged as `hash_exact` in dedup_log, returns existing node ID
- **Cost:** Zero (pure SQL, no embeddings needed)

### 2. Auto-Reject Zone (≥0.98 similarity)
- Computes embedding via Ollama, compares against all owner's facts
- If similarity ≥ auto_reject_threshold (0.98) AND texts are near-identical: auto-rejected
- Near-identical check: `_texts_are_near_identical()` — order-aware text guard prevents false positives from subject/object swaps (e.g., "A loves B" vs "B loves A")
- Logged as `auto_reject` in dedup_log

### 3. Gray Zone (0.88-0.98 similarity)
- When `haiku_verify_enabled: true` in config
- Haiku is asked to confirm whether the texts are truly duplicates
- Logged as `haiku_reject` or `haiku_keep` in dedup_log
- **Cost:** ~$0.001 per verification

### 4. Below 0.88
- Stored as new fact (no dedup concern)

---

## Nightly Janitor Deduplication (Task 3)

### Token-Recall Approach
**Problem:** O(n²) pairwise comparison doesn't scale. At 467 memories, 214K comparisons.

**Solution:**
1. Extract key tokens per memory (nouns, proper names — not stopwords)
2. SQL LIKE search for candidates sharing tokens (~30 per memory vs full scan)
3. Vector similarity on candidates only
4. Batch pairs to Haiku for confirmation

### Batching
`TokenBatchBuilder` dynamically packs items based on model context window × budget percent (default 50%). Safety cap of 500 items per batch.

### Merge Safety
- Tracks already-merged node IDs within each run (prevents cascading merges)
- Individual merge operations wrapped in try/catch
- FK constraint violations caught and logged as warnings (not errors)
- Merged nodes hard-deleted with cascading cleanup (edges, contradictions, decay queue, dedup_log)

---

## Dedup Log

All dedup decisions are recorded in the `dedup_log` table:

| Column | Purpose |
|--------|---------|
| `new_text` | Text that was checked |
| `existing_node_id` | The node it matched against |
| `existing_text` | Text of the existing node |
| `similarity` | Cosine similarity score |
| `decision` | `hash_exact`, `auto_reject`, `haiku_reject`, `haiku_keep`, `fallback_reject` |
| `haiku_reasoning` | LLM explanation (if Haiku was called) |
| `review_status` | `unreviewed` / `confirmed` / `reversed` |
| `owner_id` | Owner of the fact being checked |

### Review Pipeline (Task 2b)
Nightly janitor reviews recent dedup rejections via `recall_pass`:
- **`hash_exact` entries are auto-confirmed** — identical text matches need no LLM review. These are excluded from the Opus review query and auto-confirmed at the start of the task.
- Embedding-based rejections (`auto_reject`, `haiku_reject`, `fallback_reject`) are sent to Opus for review
- Status: `unreviewed` → `confirmed` or `reversed`
- **REVERSE safety check**: Before restoring a reversed fact, checks if a living node with the same content hash already exists. If so, skips restoration to prevent duplicate creation.

---

## Configuration

```json
{
  "janitor": {
    "dedup": {
      "auto_reject_threshold": 0.98,
      "gray_zone_low": 0.88,
      "haiku_verify_enabled": true
    }
  }
}
```

---

## Session ID

Session filtering prevents feedback loops (just-stored facts appearing in immediate search):
- **Primary:** `ctx.sessionId` from gateway (UUID)
- **Fallback:** MD5 hash of first message timestamp
- Current-session memories excluded from search results
- Compaction time-gate: after `/compact`, pre-compaction memories re-injectable
