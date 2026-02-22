#!/usr/bin/env python3
"""
Memory Janitor (Optimized) - Scalable LLM-powered maintenance for the memory graph.

Key optimizations:
1. Temporal filtering - only check new memories vs existing (not old-vs-old)
2. Vector similarity pre-filtering - cosine similarity >0.8 before LLM
3. Semantic clustering - group by domain, check within clusters
4. Robust timing and progress reporting
5. Incremental processing with metadata tracking

Tasks:
1. Find near-duplicates (85-94% similarity range) - candidates for merge  
2. Extract edges/relationships from fact text
3. Decay confidence on old unused facts
4. Detect potential contradictions (optimized)

Run modes:
  --dry-run  (default) Report findings without making changes
  --apply    Apply recommended changes

Usage:
  python3 janitor_optimized.py --task duplicates
  python3 janitor_optimized.py --task edges --apply
  python3 janitor_optimized.py --task all --dry-run
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

# Import from our memory_graph module
from memory_graph import (get_graph, MemoryGraph, Node, Edge, store as store_memory,
                         store_contradiction, get_pending_contradictions,
                         resolve_contradiction, mark_contradiction_false_positive, soft_delete,
                         get_recent_dedup_rejections, resolve_dedup_review,
                         queue_for_decay_review, get_pending_decay_reviews, resolve_decay_review,
                         ensure_keywords_for_relation, get_edge_keywords,
                         delete_edges_by_source_fact, create_edge,
                         content_hash, hard_delete_node)
from lib.config import get_db_path
from lib.tokens import extract_key_tokens as _lib_extract_key_tokens, STOPWORDS as _LIB_STOPWORDS, estimate_tokens
from lib.archive import archive_node as _archive_node
from logger import janitor_logger, rotate_logs
from config import get_config
from workspace_audit import run_workspace_check, backup_workspace_files
from docs_rag import DocsRAG
from llm_clients import (call_fast_reasoning, call_deep_reasoning, call_llm,
                         parse_json_response, reset_token_usage, get_token_usage,
                         estimate_cost, DEEP_REASONING_TIMEOUT, FAST_REASONING_TIMEOUT)

# Configuration — resolved from config system
DB_PATH = get_db_path()
def _workspace() -> Path:
    from lib.adapter import get_adapter
    return get_adapter().quaid_home()

def _data_dir() -> Path:
    from lib.adapter import get_adapter
    return get_adapter().data_dir()

def _logs_dir() -> Path:
    from lib.adapter import get_adapter
    return get_adapter().logs_dir()

WORKSPACE = None  # Lazy — use _workspace() instead

# Load config values (with fallbacks for safety)
def _get_config_value(getter, default):
    """Safely get config value with fallback."""
    try:
        return getter()
    except Exception:
        return default

# Thresholds - now loaded from config/memory.json
_cfg = get_config()
DUPLICATE_MIN_SIM = _cfg.janitor.dedup.similarity_threshold  # Lower bound for "might be duplicate"
DUPLICATE_MAX_SIM = _cfg.janitor.dedup.high_similarity_threshold  # Upper bound (auto-reject above)
CONTRADICTION_MIN_SIM = _cfg.janitor.contradiction.min_similarity  # Minimum similarity for contradiction checks
CONTRADICTION_MAX_SIM = _cfg.janitor.contradiction.max_similarity  # Maximum similarity for contradiction checks
CONFIDENCE_DECAY_DAYS = _cfg.decay.threshold_days  # Start decaying after this many days unused
CONFIDENCE_DECAY_RATE = _cfg.decay.rate_percent / 100.0  # Convert percent to decimal

# Fixed values (not in config)
RECALL_CANDIDATES_PER_NODE = 30  # Max candidates to recall per new memory


def _owner_display_name() -> str:
    """Get the owner's display name from config for use in prompts."""
    try:
        default = _cfg.users.default_owner
        identity = _cfg.users.identities.get(default)
        if identity and identity.person_node_name:
            return identity.person_node_name.split()[0]  # First name only
    except Exception:
        pass
    return "the user"


def _owner_full_name() -> str:
    """Get the owner's full name from config for use in edge examples."""
    try:
        default = _cfg.users.default_owner
        identity = _cfg.users.identities.get(default)
        if identity and identity.person_node_name:
            return identity.person_node_name
    except Exception:
        pass
    return "the user"

# Stopwords — imported from shared lib (kept as alias for backward compat)
_STOPWORDS = _LIB_STOPWORDS


def _default_owner_id() -> str:
    """Get the default owner ID from config."""
    try:
        return _cfg.users.default_owner
    except Exception:
        return "default"


def _merge_nodes_into(
    graph: MemoryGraph,
    merged_text: str,
    original_ids: List[str],
    source: str = "dedup_merge",
    dry_run: bool = False,
) -> Optional[Dict[str, Any]]:
    """Merge multiple nodes into one, preserving strength signals and migrating edges.

    Fixes for the destructive merge pattern:
    1. Inherits max confidence from originals (not hardcoded 0.9)
    2. Sums confirmation_count from originals
    3. Uses status="active" (not "approved")
    4. Migrates edges to the merged node (not deleted)
    5. Uses config-based owner_id (not hardcoded)
    """
    if dry_run:
        return None

    # Read originals to inherit their signals
    originals = []
    for oid in original_ids:
        node = graph.get_node(oid)
        if node:
            originals.append(node)

    # Inherit the strongest signals from originals
    max_confidence = max((n.confidence for n in originals), default=0.9)
    total_confirms = sum(n.confirmation_count for n in originals)
    max_storage = max((n.storage_strength for n in originals), default=0.0)
    # Use the earliest created_at
    created_dates = [n.created_at for n in originals if n.created_at]
    earliest_created = min(created_dates) if created_dates else None
    # Inherit owner from first original
    owner = originals[0].owner_id if originals else _default_owner_id()
    # Inherit category from first original (not hardcoded "fact")
    # Node uses .type (PascalCase: "Person", "Fact", etc.) → store uses lowercase category
    category = originals[0].type.lower() if originals else "fact"

    # Store merged version with inherited signals
    result = store_memory(
        text=merged_text,
        category=category,
        source=source,
        owner_id=owner,
        verified=True,
        confidence=max_confidence,
        skip_dedup=True,
        status="active",
        created_at=earliest_created,
    )
    merged_id = result.get("id")
    if not merged_id:
        return result

    # Update confirmation_count and storage_strength on the merged node
    merged_node = graph.get_node(merged_id)
    if merged_node:
        merged_node.confirmation_count = total_confirms
        merged_node.storage_strength = max_storage
        graph.update_node(merged_node)

    # Migrate edges: repoint to merged node instead of deleting
    with graph._get_conn() as conn:
        for oid in original_ids:
            # Repoint source_fact_id edges
            conn.execute(
                "UPDATE OR IGNORE edges SET source_fact_id = ? WHERE source_fact_id = ?",
                (merged_id, oid)
            )
            # Repoint source_id edges
            conn.execute(
                "UPDATE OR IGNORE edges SET source_id = ? WHERE source_id = ?",
                (merged_id, oid)
            )
            # Repoint target_id edges
            conn.execute(
                "UPDATE OR IGNORE edges SET target_id = ? WHERE target_id = ?",
                (merged_id, oid)
            )

        # Clean up any edges that now violate UNIQUE(source_id, target_id, relation)
        # The OR IGNORE above silently skips duplicates, leaving stale rows
        # Delete any remaining edges pointing to original nodes
        for oid in original_ids:
            conn.execute("DELETE FROM edges WHERE source_fact_id = ?", (oid,))
            conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (oid, oid))

        # Clean up self-referencing edges created when merging nodes that
        # had edges between each other (A→B becomes merged→merged)
        conn.execute(
            "DELETE FROM edges WHERE source_id = ? AND target_id = ?",
            (merged_id, merged_id)
        )

        # Delete originals
        for oid in original_ids:
            conn.execute("DELETE FROM contradictions WHERE node_a_id = ? OR node_b_id = ?", (oid, oid))
            conn.execute("DELETE FROM decay_review_queue WHERE node_id = ?", (oid,))
            try:
                conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (oid,))
            except Exception:
                pass  # vec_nodes may not exist
            conn.execute("DELETE FROM nodes WHERE id = ?", (oid,))

    return result


def _prompt_hash(text: str) -> str:
    """SHA256 hash of prompt text, first 12 chars. For tracking which prompt version produced a decision."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]

# Performance settings — fixed sizes are now safety limits only;
# actual batch sizes are computed by TokenBatchBuilder based on context window.
LR_BATCH_SIZE = 100  # Safety cap (pairs per fast-reasoning call)
LR_BATCH_TIMEOUT = 120  # Timeout for batched calls (longer than single-pair default)
MAX_CONSECUTIVE_FAILURES = 3  # Stop batching after N consecutive failures
LLM_TIMEOUT = 30  # Timeout for individual LLM calls
MAX_EXECUTION_TIME = _cfg.janitor.task_timeout_minutes * 60  # From config (seconds)


class JanitorMetrics:
    """Track timing and performance metrics."""
    
    def __init__(self):
        self.start_time = time.time()
        self.task_times = {}
        self.llm_calls = 0
        self.llm_time = 0.0
        self.errors = []
    
    def start_task(self, task_name: str):
        self.task_times[task_name] = {"start": time.time(), "end": None}
    
    def end_task(self, task_name: str):
        if task_name in self.task_times:
            self.task_times[task_name]["end"] = time.time()
    
    def task_duration(self, task_name: str) -> float:
        if task_name in self.task_times and self.task_times[task_name]["end"]:
            return self.task_times[task_name]["end"] - self.task_times[task_name]["start"]
        return 0.0
    
    def total_duration(self) -> float:
        return time.time() - self.start_time
    
    def add_llm_call(self, duration: float):
        self.llm_calls += 1
        self.llm_time += duration
    
    def add_error(self, error: str):
        self.errors.append({"time": datetime.now().isoformat(), "error": error})

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def summary(self) -> Dict[str, Any]:
        return {
            "total_duration_seconds": round(self.total_duration(), 2),
            "task_durations": {k: round(v, 2) for k, v in 
                             {name: self.task_duration(name) for name in self.task_times}.items()},
            "llm_calls": self.llm_calls,
            "llm_time_seconds": round(self.llm_time, 2),
            "errors": len(self.errors),
            "error_details": self.errors[-5:] if self.errors else []  # Last 5 errors
        }


class TokenBatchBuilder:
    """Build batches of items that fit within a model's context window.

    Uses token estimation to pack as many items as possible into each batch,
    replacing fixed batch sizes with dynamic, token-aware batching.
    """

    def __init__(self, model_tier: str = 'deep',
                 prompt_overhead_tokens: int = 0,
                 tokens_per_item_fn=None,
                 max_items: int = 500,
                 max_output_tokens: int = 8192,
                 output_tokens_per_item: int = 0):
        """
        Args:
            model_tier: 'deep' or 'fast' — selects context window from config
            prompt_overhead_tokens: Tokens used by system/user prompt template
            tokens_per_item_fn: Callable(item) -> int, estimates tokens per item
            max_items: Safety cap on items per batch (prevents degenerate cases)
            max_output_tokens: Reserved output tokens (default 8192)
            output_tokens_per_item: Expected output tokens per item (e.g. 200 for review).
                When >0, caps batch size so total output fits in max_output_tokens.
        """
        from lib.tokens import estimate_tokens as _est
        cfg = get_config()
        context_window = cfg.models.context_window(model_tier)
        budget_pct = cfg.models.batch_budget_percent

        # Budget = context * budget_pct - prompt overhead - output reserve
        self.budget = int(context_window * budget_pct) - prompt_overhead_tokens - max_output_tokens
        self.budget = max(self.budget, 1000)  # Minimum 1K tokens
        self.max_items = max_items
        self.max_output_tokens = max_output_tokens
        self.output_tokens_per_item = output_tokens_per_item

        # Cap batch size by output capacity
        if output_tokens_per_item > 0 and max_output_tokens > 0:
            output_cap = max_output_tokens // output_tokens_per_item
            self.max_items = min(self.max_items, max(output_cap, 1))

        self._estimate = tokens_per_item_fn or (lambda item: _est(str(item)))

    def build_batches(self, items: list) -> List[list]:
        """Split items into token-aware batches.

        Each batch stays within the token budget. A single oversized item
        still gets its own batch (never dropped).

        Returns list of batches (each batch is a list of items).
        """
        if not items:
            return []

        batches = []
        current_batch = []
        current_tokens = 0

        for item in items:
            item_tokens = self._estimate(item)

            # Would this item exceed the budget or max_items cap?
            if current_batch and (current_tokens + item_tokens > self.budget
                                  or len(current_batch) >= self.max_items):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0

            current_batch.append(item)
            current_tokens += item_tokens

        if current_batch:
            batches.append(current_batch)

        return batches


def init_janitor_metadata(graph: MemoryGraph):
    """Initialize janitor metadata and run tracking tables."""
    with graph._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS janitor_metadata (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # New janitor runs tracking table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS janitor_runs (
                id INTEGER PRIMARY KEY,
                task_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                memories_processed INTEGER DEFAULT 0,
                actions_taken INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running' -- running, completed, failed
            )
        """)


def get_last_run_time(graph: MemoryGraph, task: str) -> Optional[datetime]:
    """Get the last time a specific janitor task was completed."""
    with graph._get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(completed_at) FROM janitor_runs WHERE task_name = ? AND status = 'completed'",
            (task,)
        ).fetchone()
        
        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0])
            except (ValueError, TypeError):
                pass
    return None


def get_nodes_since(graph: MemoryGraph, since: Optional[datetime] = None) -> List[Node]:
    """Get nodes created since a specific datetime. Includes pending, active, and approved memories."""
    if not since:
        # If no timestamp, return all active nodes (full scan)
        with graph._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE embedding IS NOT NULL AND status IN ('approved', 'active', 'pending')"
            ).fetchall()
    else:
        with graph._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE embedding IS NOT NULL AND status IN ('approved', 'active', 'pending') AND created_at > ?",
                (since.isoformat(),)
            ).fetchall()
    
    return [graph._row_to_node(row) for row in rows]


# =============================================================================
# Token-based recall (scalable candidate finding)
# =============================================================================

def recall_candidates(graph: MemoryGraph, text: str, exclude_id: str,
                      limit: int = RECALL_CANDIDATES_PER_NODE) -> List[Node]:
    """Recall candidate memories by FTS5 MATCH (with LIKE fallback).

    For each significant token in `text`, find memories containing that token.
    Returns up to `limit` unique candidate nodes (excluding `exclude_id`).
    Scales as O(tokens * rows_matched) instead of O(total_nodes).
    """
    tokens = _lib_extract_key_tokens(text)
    if not tokens:
        return []

    with graph._get_conn() as conn:
        # Try FTS5 MATCH first
        try:
            fts_query = " OR ".join(f'"{t}"' for t in tokens)
            rows = conn.execute("""
                SELECT n.* FROM nodes_fts
                JOIN nodes n ON n.rowid = nodes_fts.rowid
                WHERE nodes_fts MATCH ?
                  AND n.id != ?
                  AND n.embedding IS NOT NULL
                  AND (n.status IS NULL OR n.status IN ('approved', 'pending', 'active'))
                LIMIT ?
            """, (fts_query, exclude_id, limit)).fetchall()
        except Exception:
            # Fallback to LIKE if FTS5 index not yet rebuilt
            conditions = " OR ".join(["LOWER(name) LIKE ?"] * len(tokens))
            params: list = [f"%{t}%" for t in tokens]
            params.append(exclude_id)
            params.append(limit)
            rows = conn.execute(f"""
                SELECT * FROM nodes
                WHERE ({conditions})
                  AND id != ?
                  AND embedding IS NOT NULL
                  AND (status IS NULL OR status IN ('approved', 'pending', 'active'))
                                 LIMIT ?
            """, params).fetchall()

    return [graph._row_to_node(r) for r in rows]


# =============================================================================
# Shared recall pass — builds candidate pairs once for both dedup & contradiction
# =============================================================================

def backfill_embeddings(graph: MemoryGraph, metrics: JanitorMetrics,
                        dry_run: bool = True) -> Dict[str, int]:
    """Re-embed nodes with NULL embeddings (safety net for Ollama outages).

    Returns dict with 'found' and 'embedded' counts.
    """
    metrics.start_task("embedding_backfill")
    from lib.embeddings import get_embedding as _get_emb, pack_embedding as _pack_emb

    with graph._get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name FROM nodes WHERE embedding IS NULL"
        ).fetchall()

    found = len(rows)
    embedded = 0
    print(f"  Found {found} nodes with NULL embeddings")

    for row in rows:
        node_id, name = row["id"], row["name"]
        if dry_run:
            print(f"    Would embed: {name[:50]}...")
        else:
            emb = _get_emb(name)
            if emb:
                packed = _pack_emb(emb)
                with graph._get_conn() as conn:
                    conn.execute(
                        "UPDATE nodes SET embedding = ? WHERE id = ?",
                        (packed, node_id)
                    )
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                            (node_id, packed)
                        )
                    except Exception:
                        pass  # vec_nodes may not exist
                embedded += 1
            else:
                metrics.add_error(f"Failed to embed node {node_id}: {name[:50]}")

    # FTS5 integrity check — rebuild if count mismatch or rowid drift detected
    fts_rebuilt = False
    try:
        with graph._get_conn() as conn:
            fts_count = conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
            node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            needs_rebuild = fts_count != node_count and node_count > 0
            if not needs_rebuild and node_count > 0:
                # Spot-check: verify latest node's rowid is in FTS
                sample = conn.execute("SELECT rowid, name FROM nodes ORDER BY rowid DESC LIMIT 1").fetchone()
                if sample:
                    fts_hit = conn.execute("SELECT rowid FROM nodes_fts WHERE rowid = ?", (sample[0],)).fetchone()
                    needs_rebuild = fts_hit is None
            if needs_rebuild:
                if dry_run:
                    print(f"  Would rebuild FTS5 index (nodes={node_count}, fts={fts_count})")
                else:
                    conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
                    fts_rebuilt = True
                    print(f"  Rebuilt FTS5 index (was out of sync: nodes={node_count}, fts={fts_count})")
            else:
                print(f"  FTS5 index OK ({fts_count} entries)")
    except Exception as e:
        print(f"  FTS5 check skipped: {e}")

    metrics.end_task("embedding_backfill")
    return {"found": found, "embedded": embedded, "fts_rebuilt": fts_rebuilt}


def recall_similar_pairs(graph: MemoryGraph, metrics: JanitorMetrics,
                         since: Optional[datetime] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    Single token-recall pass that buckets pairs by similarity range.

    Returns {"duplicates": [...], "contradictions": [...]} where each entry
    is a pair dict with id_a, text_a, id_b, text_b, similarity, etc.
    Dedup range: DUPLICATE_MIN_SIM..DUPLICATE_MAX_SIM
    Contradiction range: CONTRADICTION_MIN_SIM..CONTRADICTION_MAX_SIM (Facts only — let LLM decide)
    """
    metrics.start_task("recall_pass")

    new_nodes = get_nodes_since(graph, since) if since else get_nodes_since(graph, None)
    print(f"  Recall pass: {len(new_nodes)} {'new' if since else 'all'} nodes")

    seen_pairs: set = set()
    dup_candidates = []
    contradiction_candidates = []
    total_recalled = 0
    vector_comparisons = 0

    for new_node in new_nodes:
        if not new_node.embedding:
            continue

        candidates = recall_candidates(graph, new_node.name, new_node.id)
        total_recalled += len(candidates)

        for cand in candidates:
            if not cand.embedding:
                continue
            pair_key = tuple(sorted([new_node.id, cand.id]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            vector_comparisons += 1
            sim = graph.cosine_similarity(new_node.embedding, cand.embedding)

            # Dedup range (any type)
            if DUPLICATE_MIN_SIM <= sim < DUPLICATE_MAX_SIM:
                dup_candidates.append({
                    "id_a": new_node.id,
                    "text_a": new_node.name,
                    "id_b": cand.id,
                    "text_b": cand.name,
                    "similarity": round(sim, 3),
                    "type_a": new_node.type,
                    "type_b": cand.type,
                })

            # Contradiction range (Facts only — let LLM decide, no negation heuristic)
            if (CONTRADICTION_MIN_SIM <= sim < CONTRADICTION_MAX_SIM
                    and new_node.type == 'Fact' and cand.type == 'Fact'):
                contradiction_candidates.append({
                    "id_a": new_node.id,
                    "text_a": new_node.name,
                    "created_a": new_node.created_at,
                    "valid_from_a": new_node.valid_from,
                    "valid_until_a": new_node.valid_until,
                    "id_b": cand.id,
                    "text_b": cand.name,
                    "created_b": cand.created_at,
                    "valid_from_b": cand.valid_from,
                    "valid_until_b": cand.valid_until,
                    "similarity": round(sim, 3),
                })

    print(f"  Token recall: {total_recalled} candidates, {vector_comparisons} vector checks")
    print(f"  Buckets: {len(dup_candidates)} dedup, {len(contradiction_candidates)} contradiction")
    metrics.end_task("recall_pass")

    return {"duplicates": dup_candidates, "contradictions": contradiction_candidates}


# =============================================================================
# Task 3: Find Near-Duplicates (LLM verification on pre-built pairs)
# =============================================================================

def find_duplicates_from_pairs(dup_candidates: List[Dict[str, Any]],
                               metrics: JanitorMetrics) -> List[Dict[str, Any]]:
    """Run batched LLM dedup checks on pre-built candidate pairs."""
    metrics.start_task("duplicates")
    duplicates = []

    builder = TokenBatchBuilder(
        model_tier='fast',
        prompt_overhead_tokens=200,  # System prompt + instructions
        tokens_per_item_fn=lambda p: estimate_tokens(p["text_a"]) + estimate_tokens(p["text_b"]) + 30,
        max_items=500
    )
    batches = builder.build_batches(dup_candidates)
    total_batches = len(batches)
    print(f"  LLM analysis: {len(dup_candidates)} candidates in {total_batches} batches")

    consecutive_failures = 0
    for batch_num, batch in enumerate(batches, 1):
        try:
            batch_start_time = time.time()
            results = batch_duplicate_check(batch, metrics)
            batch_duration = time.time() - batch_start_time
            merge_count = sum(1 for r in results if r)
            for dup, suggestion in zip(batch, results):
                if suggestion:
                    dup["suggestion"] = suggestion
                    duplicates.append(dup)
            print(f"    Batch {batch_num}/{total_batches}: {merge_count} merge suggestions ({batch_duration:.1f}s)")
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            metrics.add_error(f"Duplicate batch {batch_num} exception: {e}")
            print(f"    Batch {batch_num}/{total_batches}: FAILED ({e})")

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            print(f"  {consecutive_failures} consecutive failures, aborting duplicate batches")
            metrics.add_error(f"Duplicates aborted: {consecutive_failures} consecutive batch failures")
            break

    duplicates.sort(key=lambda x: x["similarity"], reverse=True)
    metrics.end_task("duplicates")
    return duplicates


def batch_duplicate_check(pairs: List[Dict[str, Any]], metrics: JanitorMetrics) -> List[Optional[Dict[str, Any]]]:
    """Check a batch of duplicate pairs in a single fast-reasoning LLM call.

    Returns a list parallel to `pairs` — each entry is a suggestion dict or None.
    """
    if not pairs:
        return []

    numbered = []
    for i, p in enumerate(pairs):
        numbered.append(f'{i+1}. Fact A: "{p["text_a"]}"\n   Fact B: "{p["text_b"]}"\n   Similarity: {p["similarity"]}')

    prompt = f"""You are checking {len(pairs)} candidate duplicate pairs from a personal knowledge base.
For each pair, decide:
- merge: same fact reworded, OR one fact subsumes the other (contains all the same info plus more detail).
- keep_both: genuinely different information.

When merging, the merged_text should be the MORE SPECIFIC/DETAILED version. If Fact B says "Maya has a dog" and Fact A says "Maya has a dog named Biscuit", merge and keep "Maya has a dog named Biscuit".
For facts, the merged_text MUST be at least 3 words (subject + verb + object). Never produce a bare noun phrase like "Montrose, Houston" for a fact — instead write "Maya lives in Montrose, Houston". Entity names (people, places) can be 1-2 words.

IMPORTANT: Negation flips meaning. "likes X" vs "doesn't like X" = keep_both.

{chr(10).join(numbered)}

Respond with a JSON array of {len(pairs)} objects, one per pair in order:
[
  {{"pair": 1, "action": "merge", "merged_text": "best combined version"}},
  {{"pair": 2, "action": "keep_both", "reason": "why different"}}
]

JSON array only:"""

    response, duration = call_fast_reasoning(prompt, max_tokens=200 * len(pairs), timeout=LR_BATCH_TIMEOUT)
    metrics.add_llm_call(duration)

    if not response:
        metrics.add_error(f"Batch duplicate check failed ({len(pairs)} pairs)")
        return [None] * len(pairs)

    parsed = parse_json_response(response)
    if not isinstance(parsed, list):
        metrics.add_error(f"Batch duplicate response was not a list")
        return [None] * len(pairs)

    # Map results back to pairs by index
    results: List[Optional[Dict[str, Any]]] = [None] * len(pairs)
    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("pair")
        if isinstance(idx, int) and 1 <= idx <= len(pairs):
            if item.get("action") == "merge":
                results[idx - 1] = {"action": "merge", "merged_text": item.get("merged_text", "")}
            elif item.get("action") == "keep_both":
                results[idx - 1] = None  # not a duplicate

    return results


# =============================================================================
# Task 4: Verify Contradictions (LLM verification on pre-built pairs)
# =============================================================================

def find_contradictions_from_pairs(contradiction_candidates: List[Dict[str, Any]],
                                   metrics: JanitorMetrics,
                                   dry_run: bool = False) -> List[Dict[str, Any]]:
    """Run batched LLM contradiction checks on pre-built candidate pairs."""
    metrics.start_task("contradictions")
    contradictions = []
    task_start_time = time.time()

    confirmed_contradictions = 0
    builder = TokenBatchBuilder(
        model_tier='fast',
        prompt_overhead_tokens=200,
        tokens_per_item_fn=lambda p: estimate_tokens(p["text_a"]) + estimate_tokens(p["text_b"]) + 20,
        max_items=500
    )
    batches = builder.build_batches(contradiction_candidates)
    total_batches = len(batches)
    print(f"  LLM verification: {len(contradiction_candidates)} candidates in {total_batches} batches")

    consecutive_failures = 0
    for batch_num, batch in enumerate(batches, 1):
        elapsed = time.time() - task_start_time
        if elapsed > MAX_EXECUTION_TIME:
            print(f"  Time limit reached ({elapsed:.1f}s), stopping")
            metrics.add_error(f"Contradiction check stopped: {elapsed:.1f}s > {MAX_EXECUTION_TIME}s")
            break

        try:
            batch_start_time = time.time()
            results = batch_contradiction_check(batch, metrics)
            batch_duration = time.time() - batch_start_time
            batch_confirmed = 0
            for pair, is_contradiction in zip(batch, results):
                if is_contradiction:
                    contradictions.append({**pair, "explanation": is_contradiction})
                    confirmed_contradictions += 1
                    batch_confirmed += 1
                    # Persist to contradictions table (skip in dry-run)
                    if not dry_run:
                        store_contradiction(pair["id_a"], pair["id_b"], is_contradiction)
            print(f"    Batch {batch_num}/{total_batches}: {batch_confirmed} contradictions ({batch_duration:.1f}s)")
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            metrics.add_error(f"Contradiction batch {batch_num} exception: {e}")
            print(f"    Batch {batch_num}/{total_batches}: FAILED ({e})")

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            print(f"  {consecutive_failures} consecutive failures, aborting contradiction batches")
            metrics.add_error(f"Contradictions aborted: {consecutive_failures} consecutive batch failures")
            break

    print(f"\n  Confirmed: {confirmed_contradictions} contradictions")
    metrics.end_task("contradictions")
    return contradictions


def batch_contradiction_check(pairs: List[Dict[str, Any]], metrics: JanitorMetrics) -> List[Optional[str]]:
    """Check a batch of contradiction pairs in a single fast-reasoning LLM call.

    Returns a list parallel to `pairs` — each entry is an explanation string
    (if contradiction confirmed) or None.
    """
    if not pairs:
        return []

    numbered = []
    for i, p in enumerate(pairs):
        date_a = p.get("created_a", "unknown") or "unknown"
        date_b = p.get("created_b", "unknown") or "unknown"
        valid_a = ""
        if p.get("valid_from_a") or p.get("valid_until_a"):
            valid_a = f" [valid: {p.get('valid_from_a', '?')} to {p.get('valid_until_a', 'present')}]"
        valid_b = ""
        if p.get("valid_from_b") or p.get("valid_until_b"):
            valid_b = f" [valid: {p.get('valid_from_b', '?')} to {p.get('valid_until_b', 'present')}]"
        numbered.append(
            f'{i+1}. Fact A (recorded {date_a[:10]}){valid_a}: "{p["text_a"]}"\n'
            f'   Fact B (recorded {date_b[:10]}){valid_b}: "{p["text_b"]}"'
        )

    prompt = f"""Check {len(pairs)} fact pairs for contradictions in a personal knowledge base.

IMPORTANT: Facts from different time periods are NOT contradictions — they represent temporal succession.
For example, "lives in Austin" (2024) and "lives in Bali" (2025) is NOT a contradiction — it's a life change.
Only flag TRUE contradictions: facts that cannot both be true at the same time.

{chr(10).join(numbered)}

Respond with a JSON array of {len(pairs)} objects, one per pair in order:
[
  {{"pair": 1, "contradicts": true, "explanation": "why they contradict"}},
  {{"pair": 2, "contradicts": false}}
]

JSON array only:"""

    prompt_tag = f"[prompt:{_prompt_hash(prompt)}] "
    response, duration = call_fast_reasoning(prompt, max_tokens=150 * len(pairs), timeout=LR_BATCH_TIMEOUT)
    metrics.add_llm_call(duration)

    if not response:
        metrics.add_error(f"Batch contradiction check failed ({len(pairs)} pairs)")
        return [None] * len(pairs)

    parsed = parse_json_response(response)
    if not isinstance(parsed, list):
        metrics.add_error(f"Batch contradiction response was not a list")
        return [None] * len(pairs)

    results: List[Optional[str]] = [None] * len(pairs)
    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("pair")
        if isinstance(idx, int) and 1 <= idx <= len(pairs):
            if item.get("contradicts"):
                results[idx - 1] = prompt_tag + item.get("explanation", "contradiction confirmed")

    return results


# =============================================================================
# Task 4b: Resolve Contradictions (Opus-powered)
# =============================================================================

def resolve_contradictions_with_opus(graph: MemoryGraph, metrics: JanitorMetrics,
                                     dry_run: bool = True) -> Dict[str, int]:
    """Resolve pending contradictions using Opus for deep-reasoning decisions."""
    metrics.start_task("contradiction_resolution")
    results = {"resolved": 0, "false_positive": 0, "merged": 0, "decisions": []}

    pending = get_pending_contradictions(limit=50)
    if not pending:
        print("  No pending contradictions to resolve")
        metrics.end_task("contradiction_resolution")
        return results

    print(f"  Resolving {len(pending)} pending contradictions via Opus...")

    builder = TokenBatchBuilder(
        model_tier='deep',
        prompt_overhead_tokens=500,  # Prompt template overhead
        tokens_per_item_fn=lambda c: (
            estimate_tokens(c["text_a"]) + estimate_tokens(c["text_b"]) +
            estimate_tokens(c.get("explanation", "")) + 80
        ),
        max_items=100
    )
    batches = builder.build_batches(pending)
    total_batches = len(batches)
    for batch_num, batch in enumerate(batches, 1):
        numbered = []
        for i, c in enumerate(batch):
            numbered.append(
                f'{i+1}. Contradiction ID: {c["id"]}\n'
                f'   Memory A: "{c["text_a"]}"\n'
                f'     (confidence: {c["conf_a"]}, created: {c["created_a"]}, '
                f'source: {c["source_a"]}, speaker: {c["speaker_a"]}, access_count: {c["access_a"]})\n'
                f'   Memory B: "{c["text_b"]}"\n'
                f'     (confidence: {c["conf_b"]}, created: {c["created_b"]}, '
                f'source: {c["source_b"]}, speaker: {c["speaker_b"]}, access_count: {c["access_b"]})\n'
                f'   Detection reason: {c["explanation"]}'
            )

        prompt = f"""You are resolving {len(batch)} contradictions in a personal knowledge base about {_owner_display_name()}.

For each contradiction pair, decide:
- KEEP_A: Memory A is correct/more current, delete B
- KEEP_B: Memory B is correct/more current, delete A
- KEEP_BOTH: Not actually a contradiction (different contexts, time periods, etc.)
- MERGE: Combine into a single corrected memory (provide merged text)

Consider: recency, confidence, access frequency, source reliability,
whether the contradiction is temporal (facts changed over time).
If using MERGE, merged_text for facts MUST be at least 3 words (subject + verb + object). Entity names (people, places) can be 1-2 words.

{chr(10).join(numbered)}

Respond with a JSON array of {len(batch)} objects:
[
  {{"pair": 1, "action": "KEEP_A", "reason": "why"}},
  {{"pair": 2, "action": "KEEP_BOTH", "reason": "different time periods"}},
  {{"pair": 3, "action": "MERGE", "merged_text": "combined fact", "reason": "why"}}
]

JSON array only:"""

        resolution_prompt_tag = f"[prompt:{_prompt_hash(prompt)}] "
        response, duration = call_deep_reasoning(prompt, max_tokens=300 * len(batch))
        metrics.add_llm_call(duration)

        if not response:
            metrics.add_error(f"Contradiction resolution batch {batch_num} failed")
            print(f"    Batch {batch_num}/{total_batches}: FAILED (no response)")
            continue

        parsed = parse_json_response(response)
        if not isinstance(parsed, list):
            metrics.add_error(f"Contradiction resolution batch {batch_num}: invalid JSON")
            print(f"    Batch {batch_num}/{total_batches}: FAILED (invalid JSON)")
            continue

        batch_resolved = 0
        for item in parsed:
            if not isinstance(item, dict):
                continue
            idx = item.get("pair")
            if not isinstance(idx, int) or idx < 1 or idx > len(batch):
                continue

            c = batch[idx - 1]
            action = item.get("action", "").upper()
            reason = resolution_prompt_tag + item.get("reason", "")
            decision_row = {
                "id": c["id"],
                "action": action,
                "text_a": c["text_a"],
                "text_b": c["text_b"],
                "reason": reason,
                "dry_run": dry_run,
            }

            if action == "KEEP_A":
                if dry_run:
                    print(f"    Would KEEP_A: supersede B ({c['text_b'][:40]}...) — {reason[:50]}")
                else:
                    graph.supersede_node(c["node_b_id"], c["node_a_id"])
                    resolve_contradiction(c["id"], "keep_a", reason)
                results["resolved"] += 1
                batch_resolved += 1
                results["decisions"].append(decision_row)
                _append_decision_log("contradiction_resolution", decision_row)

            elif action == "KEEP_B":
                if dry_run:
                    print(f"    Would KEEP_B: supersede A ({c['text_a'][:40]}...) — {reason[:50]}")
                else:
                    graph.supersede_node(c["node_a_id"], c["node_b_id"])
                    resolve_contradiction(c["id"], "keep_b", reason)
                results["resolved"] += 1
                batch_resolved += 1
                results["decisions"].append(decision_row)
                _append_decision_log("contradiction_resolution", decision_row)

            elif action == "KEEP_BOTH":
                if dry_run:
                    print(f"    Would KEEP_BOTH: {reason[:60]}")
                else:
                    mark_contradiction_false_positive(c["id"], reason)
                results["false_positive"] += 1
                batch_resolved += 1
                results["decisions"].append(decision_row)
                _append_decision_log("contradiction_resolution", decision_row)

            elif action == "MERGE":
                merged_text = item.get("merged_text", "")
                if merged_text:
                    if dry_run:
                        print(f"    Would MERGE: {merged_text[:50]}... — {reason[:40]}")
                    else:
                        id_a, id_b = c["node_a_id"], c["node_b_id"]
                        # Record resolution BEFORE deleting (CASCADE would remove the row)
                        resolve_contradiction(c["id"], "merge", reason)
                        merge_result = _merge_nodes_into(
                            graph, merged_text,
                            [id_a, id_b],
                            source="contradiction_merge",
                        )

                        # Create resolution summary artifact
                        summary_text = (
                            f"Merged: \"{c['text_a'][:100]}\" + \"{c['text_b'][:100]}\". "
                            f"Reason: {reason}"
                        )
                        try:
                            summary_node = Node.create(
                                type="resolution_summary",
                                name=summary_text,
                                privacy="private",
                                source="contradiction_merge",
                                confidence=1.0,
                            )
                            summary_node.owner_id = _default_owner_id()
                            summary_node.status = "archived"
                            summary_id = graph.add_node(summary_node, embed=False)
                            # Link to surviving merged node
                            if merge_result and merge_result.get("id"):
                                surviving_id = merge_result["id"]
                                graph.add_edge(Edge.create(
                                    source_id=summary_id,
                                    target_id=surviving_id,
                                    relation="resolved_from"
                                ))
                        except Exception:
                            pass  # Resolution summary is best-effort
                    results["merged"] += 1
                    batch_resolved += 1
                    results["decisions"].append(decision_row)
                    _append_decision_log("contradiction_resolution", decision_row)

        print(f"    Batch {batch_num}/{total_batches}: {batch_resolved} resolved ({duration:.1f}s)")

    metrics.end_task("contradiction_resolution")
    return results


# =============================================================================
# Other Tasks (edges, decay) - Same logic, added timing
# =============================================================================

def find_edge_candidates_optimized(graph: MemoryGraph, metrics: JanitorMetrics,
                                    full_scan: bool = False) -> List[Dict[str, Any]]:
    """Find facts that could become edges (with timing).

    full_scan=True skips pattern pre-filter and returns all Fact nodes without edges.
    """
    metrics.start_task("edges_discovery")

    # Get Fact nodes without outgoing edges (excluding generic has_fact).
    # Normal runs: only pending/approved facts (not yet graduated to active).
    # Full scan: all facts regardless of status (for backfill).
    status_filter = "" if full_scan else "AND n.status IN ('pending', 'approved')"
    with graph._get_conn() as conn:
        rows = conn.execute(f"""
            SELECT n.* FROM nodes n
            LEFT JOIN edges e ON n.id = e.source_id AND e.relation != 'has_fact'
            WHERE n.type = 'Fact' AND e.id IS NULL
              AND n.owner_id IS NOT NULL
              {status_filter}
        """).fetchall()

    candidates = []
    for row in rows:
        node = graph._row_to_node(row)

        if not full_scan:
            # Pattern pre-filter for nightly runs
            text_lower = node.name.lower()
            patterns = [
                "'s mother", "'s father", "'s wife", "'s husband", "'s girlfriend",
                "'s boyfriend", "'s partner", "'s fiancé", "'s fiancée",
                "'s brother", "'s sister", "'s son", "'s daughter",
                "'s uncle", "'s aunt", "'s cousin", "'s grandma", "'s grandfather",
                "'s neighbor", "'s friend", "'s boss", "'s colleague",
                "lives at", "lives in", "lives near", "lives next to",
                "works at", "works for", "works as", "works on",
                "born in", "born on", "grew up in", "moved to", "from ",
                "owns", "manages", "founded", "created",
                "is a member of", "member of", "part of", "belongs to",
                "prefers", "likes", "loves", "hates", "enjoys", "dislikes",
                "allergic to", "favorite", "favourite",
                "married to", "engaged to", "dating",
                "studies at", "graduated from", "attended",
                "speaks ", "fluent in",
                "mother is", "father is", "sister is", "brother is",
                "neighbor", "next door",
            ]
            if not any(p in text_lower for p in patterns):
                continue

        candidates.append({
            "id": node.id,
            "text": node.name,
            "type": node.type,
            "owner_id": node.owner_id,
        })

    metrics.end_task("edges_discovery")
    return candidates


# Seed relation types — canonical forms only (no inverses).
# Parent/owner/employer is ALWAYS the subject in family/ownership/work relations.
_SEED_RELATIONS = [
    "parent_of", "spouse_of", "partner_of", "sibling_of", "family_of",
    "friend_of", "neighbor_of", "colleague_of",
    "lives_in", "born_in", "from",
    "works_at", "works_as",
    "member_of", "attended",
    "owns", "manages", "founded",
    "prefers", "dislikes",
    "speaks",
    "knows", "related_to",
    "diagnosed_with", "allergic_to",
    "has_pet",
    "caused_by",
]

# Inverse map: when Opus returns one of these, FLIP subject/object and use the canonical form.
# Use for relations where the direction is REVERSED from canonical.
# child_of(Alice, Carol) → parent_of(Carol, Alice) — child becomes object
_INVERSE_MAP = {
    "child_of": "parent_of",
    "son_of": "parent_of",
    "daughter_of": "parent_of",
    "owned_by": "owns",
    "managed_by": "manages",
    "founded_by": "founded",
    "employs": "works_at",     # employs(Acme, Alice) → works_at(Alice, Acme)
    "employed_by": "works_at",
    "pet_of": "has_pet",       # pet_of(Rex, Alice) → has_pet(Alice, Rex)
    "led_to": "caused_by",     # led_to(A, B) → caused_by(B, A) — A led to B
    "caused": "caused_by",     # caused(A, B) → caused_by(B, A) — A caused B
    "resulted_in": "caused_by",  # resulted_in(A, B) → caused_by(B, A)
    "triggered": "caused_by",   # triggered(A, B) → caused_by(B, A)
}

# Synonym map: rename to canonical form WITHOUT flipping subject/object.
# mother_of(Carol, Alice) → parent_of(Carol, Alice) — same direction
_SYNONYM_MAP = {
    "mother_of": "parent_of",
    "father_of": "parent_of",
    "married_to": "spouse_of",
    "engaged_to": "partner_of",
    "lives_next_to": "neighbor_of",
    "resides_in": "lives_in",
    "lives_at": "lives_in",
    "hometown": "from",
    "likes": "prefers",
    "enjoys": "prefers",
    "hates": "dislikes",
    "suffers_from": "diagnosed_with",
    "has_condition": "diagnosed_with",
    "studies_at": "attended",
    "graduated_from": "attended",
    "works_for": "works_at",
    "occupation": "works_as",
    "because_of": "caused_by",  # because_of(A, B) → caused_by(A, B) — same direction
    "due_to": "caused_by",      # due_to(A, B) → caused_by(A, B) — same direction
}

# Symmetric relations: order by alphabetical entity name to prevent A→B and B→A dupes.
_SYMMETRIC_RELATIONS = frozenset([
    "spouse_of", "partner_of", "sibling_of", "family_of",
    "friend_of", "neighbor_of", "colleague_of",
    "related_to", "knows",
])


def _normalize_edge(subject: str, subject_type: str, relation: str,
                    obj: str, obj_type: str) -> tuple:
    """Normalize an edge to its canonical form.

    Returns (subject, subject_type, relation, object, object_type).
    Handles inverse flipping, synonym resolution, and symmetric ordering.
    """
    relation = relation.strip().lower().replace(" ", "_")

    # 1. Inverse map: flip subject/object
    if relation in _INVERSE_MAP:
        relation = _INVERSE_MAP[relation]
        subject, obj = obj, subject
        subject_type, obj_type = obj_type, subject_type

    # 2. Synonym map: rename relation only
    if relation in _SYNONYM_MAP:
        relation = _SYNONYM_MAP[relation]

    # 3. Symmetric relations: alphabetical order by entity name
    if relation in _SYMMETRIC_RELATIONS:
        if subject.lower() > obj.lower():
            subject, obj = obj, subject
            subject_type, obj_type = obj_type, subject_type

    return subject, subject_type, relation, obj, obj_type


def _build_relations_list(graph: MemoryGraph) -> str:
    """Build the relation types list: seed + any new types discovered in DB.

    Excludes inverse/synonym keys so Opus only sees canonical forms.
    """
    excluded = set(_INVERSE_MAP.keys()) | set(_SYNONYM_MAP.keys()) | {"has_fact"}
    db_relations = graph.get_known_relations()
    # Merge seed + DB-discovered (excluding inverses/synonyms/has_fact)
    all_relations = list(dict.fromkeys(
        _SEED_RELATIONS + [r for r in db_relations if r not in excluded]
    ))
    return ", ".join(all_relations)


def _resolve_entity_node(graph: MemoryGraph, name: str, node_type: str,
                          owner_id: Optional[str] = None) -> Optional[str]:
    """Find an existing entity node by name, or create one.

    Returns the node ID, or None if name is too vague to create a node.
    """
    if not name or len(name.strip()) < 2:
        return None

    name = name.strip()

    # 1. Exact name match with matching type
    node = graph.find_node_by_name(name, type=node_type)
    if node:
        return node.id

    # 2. Exact name match any type (e.g., "Alice" might be stored as "Alice Smith")
    node = graph.find_node_by_name(name)
    if node and node.type in ("Person", "Place", "Concept", "Organization"):
        return node.id

    # 3. Case-insensitive partial match on entity types only
    # Escape LIKE wildcards in entity names to prevent wrong resolution
    safe_name = name.lower().replace("%", "\\%").replace("_", "\\_")
    with graph._get_conn() as conn:
        row = conn.execute(
            """SELECT id, name, type FROM nodes
               WHERE LOWER(name) LIKE ? ESCAPE '\\' AND type = ?
               LIMIT 1""",
            (f"%{safe_name}%", node_type)
        ).fetchone()
        if row:
            return row["id"]

        # Try broader: any entity type
        row = conn.execute(
            """SELECT id, name, type FROM nodes
               WHERE LOWER(name) LIKE ? ESCAPE '\\'
               AND type IN ('Person', 'Place', 'Concept', 'Organization')
               LIMIT 1""",
            (f"%{safe_name}%",)
        ).fetchone()
        if row:
            return row["id"]

    # 4. Create new entity node
    if node_type not in ("Person", "Place", "Concept", "Organization"):
        node_type = "Concept"  # safe fallback

    new_node = Node.create(
        type=node_type,
        name=name,
        attributes={},
        source="janitor_edge_extraction",
        owner_id=owner_id or _default_owner_id(),
        status="active",
        confidence=0.7,
        extraction_confidence=0.6,
    )
    graph.add_node(new_node)
    print(f"    Created {node_type} node: {name} ({new_node.id[:8]}...)")
    return new_node.id


EDGE_BATCH_SIZE = 25  # Legacy safety cap (replaced by TokenBatchBuilder)


def batch_extract_edges(facts: List[Dict[str, Any]], graph: MemoryGraph,
                        metrics: JanitorMetrics,
                        relations_list: str = "") -> List[Optional[Dict[str, Any]]]:
    """Extract edges from a batch of facts in a single Opus call.

    Returns a list parallel to `facts` — each entry is an extraction dict or None.
    """
    if not facts:
        return []

    numbered = []
    for i, f in enumerate(facts):
        numbered.append(f'{i+1}. "{f["text"]}"')

    prompt = f"""You are building a knowledge graph from personal facts about a user's life.
For each fact below, extract ONE relationship between two DISTINCT named entities if one exists.
Only extract edges between named entities (people, places, organizations, pets). Do not create edges for system concepts, tools, or infrastructure.

EXISTING relation types (use one of these whenever possible):
{relations_list}

DIRECTION RULES — follow these strictly:
- For family: the PARENT is always the subject. "Alice is Carol's child" → subject=Carol, relation=parent_of, object=Alice
- For ownership/management: the OWNER/MANAGER is the subject. "The car is owned by Alice" → subject=Alice, relation=owns, object=car
- For work: the PERSON is the subject, ORGANIZATION is the object. "Alice works at Acme" → subject=Alice, relation=works_at, object=Acme
- For symmetric relations (spouse_of, sibling_of, friend_of, etc.): put entity names in alphabetical order
- NEVER use child_of, son_of, daughter_of, mother_of, father_of — use parent_of instead
- NEVER use owned_by, managed_by — use owns, manages instead
- Extract only the MOST SPECIFIC relationship. "Carol is Alice's mother" = parent_of, NOT family_of

Only create a NEW relation type if absolutely none of the above fit. New types must be:
- snake_case, specific and reusable
- NOT synonyms or inverses of existing types
- If you DO create a new relation type, also include "keywords": a list of 5-10 words that would appear in user queries related to this relationship (for search triggering)

Facts:
{chr(10).join(numbered)}

Use "has_edge": false if the fact has no clear relationship between two distinct named entities.

Respond with a JSON array of {len(facts)} objects, one per fact in order:
[
  {{"fact": 1, "has_edge": true, "subject": "Alice", "subject_type": "Person", "relation": "lives_in", "object": "Paris", "object_type": "Place"}},
  {{"fact": 2, "has_edge": false}},
  {{"fact": 3, "has_edge": true, "subject": "Carol", "subject_type": "Person", "relation": "parent_of", "object": "Alice", "object_type": "Person"}},
  {{"fact": 4, "has_edge": true, "subject": "Alice", "subject_type": "Person", "relation": "mentors", "object": "Bob", "object_type": "Person", "keywords": ["mentor", "mentee", "mentoring", "coached", "guidance"]}}
]

JSON array only:"""

    response, duration = call_deep_reasoning(prompt, max_tokens=200 * len(facts),
                                   timeout=DEEP_REASONING_TIMEOUT)
    metrics.add_llm_call(duration)

    if not response:
        metrics.add_error(f"Batch edge extraction failed ({len(facts)} facts)")
        return [None] * len(facts)

    parsed = parse_json_response(response)
    if not isinstance(parsed, list):
        metrics.add_error("Batch edge response was not a list")
        return [None] * len(facts)

    # Map results back to facts by index
    results: List[Optional[Dict[str, Any]]] = [None] * len(facts)
    for item in parsed:
        if not isinstance(item, dict):
            continue
        idx = item.get("fact")
        if not isinstance(idx, int) or idx < 1 or idx > len(facts):
            continue
        if not item.get("has_edge"):
            continue

        subject = (item.get("subject") or "").strip()
        obj = (item.get("object") or "").strip()
        relation = (item.get("relation") or "").strip().lower().replace(" ", "_")

        if not subject or not obj or not relation:
            continue
        # Skip self-referential edges
        if subject.lower() == obj.lower():
            continue

        # Normalize: flip inverses, resolve synonyms, order symmetric
        subject_type = item.get("subject_type", "Person")
        obj_type = item.get("object_type", "Concept")
        subject, subject_type, relation, obj, obj_type = _normalize_edge(
            subject, subject_type, relation, obj, obj_type
        )

        results[idx - 1] = {
            "fact_id": facts[idx - 1]["id"],
            "fact_text": facts[idx - 1]["text"],
            "subject": subject,
            "subject_type": subject_type,
            "relation": relation,
            "object": obj,
            "object_type": obj_type,
        }

        # If LLM provided keywords for a new relation, store them
        keywords = item.get("keywords")
        if keywords and isinstance(keywords, list) and len(keywords) > 0:
            # Filter to strings only
            keywords = [k for k in keywords if isinstance(k, str) and k.strip()]
            if keywords:
                from memory_graph import store_edge_keywords
                stored = store_edge_keywords(relation, keywords)
                if stored:
                    print(f"    Stored keywords for new relation '{relation}': {keywords[:5]}")

    return results


# =============================================================================
# Task 2b: Review Dedup Rejections (Opus)
# =============================================================================

def review_dedup_rejections(graph: MemoryGraph, metrics: JanitorMetrics,
                            dry_run: bool = True) -> Dict[str, int]:
    """Review recent dedup rejections using Opus to catch false positives."""
    metrics.start_task("dedup_review")
    results = {"reviewed": 0, "confirmed": 0, "reversed": 0}

    # Auto-confirm hash_exact entries — they're identical text, no LLM needed
    with graph._get_conn() as conn:
        auto_confirmed = conn.execute("""
            UPDATE dedup_log
            SET review_status = 'confirmed',
                review_resolution = 'auto-confirmed: exact content hash match',
                reviewed_at = datetime('now')
            WHERE review_status = 'unreviewed'
              AND decision = 'hash_exact'
        """).rowcount
    if auto_confirmed:
        print(f"  Auto-confirmed {auto_confirmed} hash-exact dedup entries")
        results["confirmed"] += auto_confirmed
        results["reviewed"] += auto_confirmed

    pending = get_recent_dedup_rejections(hours=24, limit=50)
    if not pending:
        print("  No unreviewed dedup rejections found")
        metrics.end_task("dedup_review")
        return results

    print(f"  Reviewing {len(pending)} recent dedup rejections via Opus...")

    builder = TokenBatchBuilder(
        model_tier='deep',
        prompt_overhead_tokens=400,
        tokens_per_item_fn=lambda e: (
            estimate_tokens(e["new_text"]) + estimate_tokens(e["existing_text"]) +
            estimate_tokens(e.get("llm_reasoning", "")) + 40
        ),
        max_items=100
    )
    batches = builder.build_batches(pending)
    total_batches = len(batches)
    for batch_num, batch in enumerate(batches, 1):
        numbered = []
        for i, entry in enumerate(batch):
            numbered.append(
                f'{i+1}. Log ID: {entry["id"]}\n'
                f'   New text: "{entry["new_text"]}"\n'
                f'   Existing text: "{entry["existing_text"]}"\n'
                f'   Similarity: {entry["similarity"]:.3f}\n'
                f'   Decision: {entry["decision"]}\n'
                f'   LLM reasoning: {entry.get("llm_reasoning", "N/A")}'
            )

        prompt = f"""You are reviewing {len(batch)} dedup rejections in a personal knowledge base.

Each entry was rejected as a duplicate during memory storage. The dedup system has high
precision — most rejections are correct. Your job is to catch the RARE false positive
where genuinely different information was wrongly blocked.

CONFIRM (use in ~90% of cases): The two texts convey the same core information, even if
worded differently. Rephrasing, adding minor detail, or slight rewording = CONFIRM.
Examples of CONFIRM:
  - "Carol doesn't like spicy food" vs "Carol and spicy food don't mix" → same fact
  - "Maya is in Austin" vs "Maya lives in Austin" → same fact
  - "Maya's partner is David" vs "Maya lives with her partner David" → same fact
  - "She started running in January" vs "She began running in January" → same fact

REVERSE (use sparingly): The texts contain genuinely DIFFERENT information — different
subjects, different claims, or materially different facts that would be lost if merged.
Examples of REVERSE:
  - "Maya started at TechFlow" vs "Maya left TechFlow" → different events
  - "Maya likes Italian food" vs "Maya is allergic to shellfish" → different facts
  - "The app uses React" vs "The app uses Vue" → contradictory, both should exist

When in doubt, CONFIRM. Losing a duplicate costs nothing; losing a unique fact is permanent.

{chr(10).join(numbered)}

Respond with a JSON array of {len(batch)} objects:
[
  {{"item": 1, "action": "CONFIRM", "reason": "same fact about X"}},
  {{"item": 2, "action": "CONFIRM", "reason": "rephrased version of same info"}}
]

JSON array only:"""

        dedup_prompt_tag = f"[prompt:{_prompt_hash(prompt)}] "
        response, duration = call_deep_reasoning(prompt, max_tokens=200 * len(batch))
        metrics.add_llm_call(duration)

        if not response:
            metrics.add_error(f"Dedup review batch {batch_num} failed")
            print(f"    Batch {batch_num}/{total_batches}: FAILED (no response)")
            continue

        parsed = parse_json_response(response)
        if not isinstance(parsed, list):
            metrics.add_error(f"Dedup review batch {batch_num}: invalid JSON")
            print(f"    Batch {batch_num}/{total_batches}: FAILED (invalid JSON)")
            continue

        for item in parsed:
            if not isinstance(item, dict):
                continue
            idx = item.get("item")
            if not isinstance(idx, int) or idx < 1 or idx > len(batch):
                continue

            entry = batch[idx - 1]
            action = item.get("action", "").upper()
            reason = dedup_prompt_tag + item.get("reason", "")

            if action == "CONFIRM":
                if not dry_run:
                    resolve_dedup_review(entry["id"], "confirmed", reason)
                print(f"    CONFIRMED: {entry['new_text'][:40]}... = {entry['existing_text'][:40]}...")
                results["confirmed"] += 1
            elif action == "REVERSE":
                if not dry_run:
                    resolve_dedup_review(entry["id"], "reversed", reason)
                    # Check if a living node with the same content already exists
                    # before restoring — prevents infinite dedup/restore cycles
                    text_hash = content_hash(entry["new_text"])
                    with graph._get_conn() as conn:
                        owner_id = entry.get("owner_id", _default_owner_id())
                        alive = conn.execute("""
                            SELECT id FROM nodes WHERE content_hash = ?
                              AND deleted_at IS NULL
                              AND status IN ('approved', 'pending', 'active', 'flagged')
                              AND (owner_id = ? OR owner_id IS NULL)
                            LIMIT 1
                        """, (text_hash, owner_id)).fetchone()
                    if alive:
                        print(f"    SKIPPED REVERSE: {entry['new_text'][:40]}... (living copy exists: {alive['id'][:8]}...)")
                    else:
                        # Original was deleted/merged — restore it
                        store_memory(
                            entry["new_text"],
                            owner_id=entry.get("owner_id", _default_owner_id()),
                            source=entry.get("source"),
                            skip_dedup=True,
                            status="approved"
                        )
                        print(f"    REVERSED: {entry['new_text'][:40]}... (restoring)")
                else:
                    print(f"    REVERSED: {entry['new_text'][:40]}... (restoring)")
                results["reversed"] += 1
            results["reviewed"] += 1

        print(f"    Batch {batch_num}/{total_batches}: reviewed {len(batch)} entries")

    metrics.end_task("dedup_review")
    return results


# =============================================================================
# Task 5b: Review Decayed Memories (Opus)
# =============================================================================

def review_decayed_memories(graph: MemoryGraph, metrics: JanitorMetrics,
                            dry_run: bool = True) -> Dict[str, int]:
    """Review memories queued for decay deletion using Opus."""
    metrics.start_task("decay_review")
    results = {"reviewed": 0, "deleted": 0, "extended": 0, "pinned": 0}

    pending = get_pending_decay_reviews(limit=50)
    if not pending:
        print("  No pending decay reviews found")
        metrics.end_task("decay_review")
        return results

    print(f"  Reviewing {len(pending)} decayed memories via Opus...")

    builder = TokenBatchBuilder(
        model_tier='deep',
        prompt_overhead_tokens=600,  # Decay review prompt is longer
        tokens_per_item_fn=lambda e: estimate_tokens(e.get("node_text", "")) + 60,
        max_items=100
    )
    batches = builder.build_batches(pending)
    total_batches = len(batches)
    for batch_num, batch in enumerate(batches, 1):
        numbered = []
        for i, entry in enumerate(batch):
            numbered.append(
                f'{i+1}. Queue ID: {entry["id"]}\n'
                f'   Text: "{entry["node_text"]}"\n'
                f'   Type: {entry.get("node_type", "unknown")}\n'
                f'   Confidence at queue: {entry["confidence_at_queue"]:.2f}\n'
                f'   Access count: {entry.get("access_count", 0)}\n'
                f'   Last accessed: {entry.get("last_accessed", "unknown")}\n'
                f'   Created: {entry.get("created_at_node", "unknown")}\n'
                f'   Verified: {"yes" if entry.get("verified") else "no"}'
            )

        owner = _owner_display_name()
        prompt = f"""You are reviewing {len(batch)} memories that reached the confidence floor in {owner}'s personal knowledge base.
These memories haven't been accessed recently and their confidence has decayed to minimum.

For each memory, decide:
- DELETE: Temporal facts that are outdated, noise, vague/unactionable statements.
  Also DELETE: system architecture/infrastructure facts that don't belong in personal memory
  (these should live in documentation, not the memory DB).
- EXTEND: Personal facts that are still true but not recently relevant (reset decay timer)
- PIN: Identity facts (names, relationships, birthdays), core preferences, permanent knowledge (never decays again)

{chr(10).join(numbered)}

Respond with a JSON array of {len(batch)} objects:
[
  {{"item": 1, "action": "DELETE", "reason": "outdated temporal fact"}},
  {{"item": 2, "action": "EXTEND", "reason": "still true, just not recently mentioned"}},
  {{"item": 3, "action": "PIN", "reason": "core identity fact about {owner}"}}
]

JSON array only:"""

        decay_prompt_tag = f"[prompt:{_prompt_hash(prompt)}] "
        response, duration = call_deep_reasoning(prompt, max_tokens=200 * len(batch))
        metrics.add_llm_call(duration)

        if not response:
            metrics.add_error(f"Decay review batch {batch_num} failed")
            print(f"    Batch {batch_num}/{total_batches}: FAILED (no response)")
            continue

        parsed = parse_json_response(response)
        if not isinstance(parsed, list):
            metrics.add_error(f"Decay review batch {batch_num}: invalid JSON")
            print(f"    Batch {batch_num}/{total_batches}: FAILED (invalid JSON)")
            continue

        for item in parsed:
            if not isinstance(item, dict):
                continue
            idx = item.get("item")
            if not isinstance(idx, int) or idx < 1 or idx > len(batch):
                continue

            entry = batch[idx - 1]
            action = item.get("action", "").upper()
            reason = decay_prompt_tag + item.get("reason", "")
            node_id = entry["node_id"]

            if action == "DELETE":
                if not dry_run:
                    resolve_decay_review(entry["id"], "delete", reason)
                    # Archive then hard delete (only delete if archive succeeds)
                    archived = _archive_node({
                        "id": node_id,
                        "type": entry.get("node_type"),
                        "name": entry.get("node_text"),
                        "confidence": entry.get("confidence_at_queue"),
                        "access_count": entry.get("access_count", 0),
                        "accessed_at": entry.get("last_accessed"),
                        "created_at": entry.get("created_at_node"),
                    }, "decay_review_delete")
                    if archived:
                        hard_delete_node(node_id)
                    else:
                        print(f"    SKIPPED delete (archive failed): {entry['node_text'][:50]}...", file=sys.stderr)
                        continue
                print(f"    DELETE: {entry['node_text'][:50]}...")
                results["deleted"] += 1
            elif action == "EXTEND":
                if not dry_run:
                    resolve_decay_review(entry["id"], "extend", reason)
                    # Scale from node's extraction_confidence if available, otherwise 0.3
                    node = graph.get_node(node_id)
                    ext_conf = (node.attributes or {}).get("extraction_confidence", 0.3) if node else 0.3
                    extend_conf = max(0.3, float(ext_conf) * 0.5) if ext_conf else 0.3
                    with graph._get_conn() as conn:
                        conn.execute(
                            "UPDATE nodes SET confidence = ?, accessed_at = ? WHERE id = ?",
                            (extend_conf, datetime.now().isoformat(), node_id)
                        )
                print(f"    EXTEND: {entry['node_text'][:50]}...")
                results["extended"] += 1
            elif action == "PIN":
                if not dry_run:
                    resolve_decay_review(entry["id"], "pin", reason)
                    # Use max of 0.7 and node's extraction_confidence so high-value facts keep their score
                    node = node or graph.get_node(node_id)
                    ext_conf = (node.attributes or {}).get("extraction_confidence", 0.7) if node else 0.7
                    pin_conf = max(0.7, float(ext_conf)) if ext_conf else 0.7
                    with graph._get_conn() as conn:
                        conn.execute(
                            "UPDATE nodes SET pinned = 1, confidence = ? WHERE id = ?",
                            (pin_conf, node_id)
                        )
                print(f"    PIN: {entry['node_text'][:50]}...")
                results["pinned"] += 1
            results["reviewed"] += 1

        print(f"    Batch {batch_num}/{total_batches}: reviewed {len(batch)} entries")

    metrics.end_task("decay_review")
    return results


def find_stale_memories_optimized(graph: MemoryGraph, metrics: JanitorMetrics) -> List[Dict[str, Any]]:
    """Find memories that haven't been accessed in a while (with timing)."""
    metrics.start_task("decay_discovery")
    
    stale = []
    cutoff = (datetime.now() - timedelta(days=CONFIDENCE_DECAY_DAYS)).isoformat()
    
    with graph._get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM nodes
            WHERE accessed_at < ?
            AND confidence > 0.1
            AND pinned = 0  -- Never decay pinned memories
            AND status IN ('approved', 'active')  -- Don't decay pending/unreviewed
            ORDER BY accessed_at ASC
        """, (cutoff,)).fetchall()
    
    for row in rows:
        node = graph._row_to_node(row)
        stale.append({
            "id": node.id,
            "text": node.name,
            "type": node.type,
            "confidence": node.confidence,
            "last_accessed": node.accessed_at,
            "accessed_at": node.accessed_at,
            "access_count": node.access_count,
            "storage_strength": node.storage_strength,
            "extraction_confidence": node.extraction_confidence,
            "verified": node.verified,
            "owner_id": node.owner_id,
            "created_at": node.created_at,
            "speaker": node.speaker,
        })
    
    metrics.end_task("decay_discovery")
    return stale


def _ebbinghaus_retention(days_since_access: float, access_count: int,
                          verified: bool, cfg: Any = None,
                          storage_strength: float = 0.0) -> float:
    """Compute Ebbinghaus retention factor R = 2^(-t/half_life).

    Half-life scales with usage:
      - base_half_life_days (default 60) for 0-access facts
      - Each access extends half-life by access_bonus_factor (default 15%)
      - Storage strength extends half-life (Bjork: +50% per unit, up to 6x at ss=10)
      - Verified facts get 2x half-life

    Returns retention in [0, 1] where 1 = fully retained, 0 = fully forgotten.
    """
    if cfg is None:
        cfg = _cfg.decay
    base_hl = cfg.base_half_life_days
    bonus = cfg.access_bonus_factor

    # Extend half-life based on access count: each access adds 15% to half-life
    half_life = base_hl * (1.0 + bonus * access_count)
    # Bjork: storage strength extends half-life (up to +50% per unit, capped by ss=10 → 6x)
    half_life *= (1.0 + 0.5 * storage_strength)
    # Verified facts decay 2x slower
    if verified:
        half_life *= 2.0

    if half_life <= 0:
        return 0.0
    # R = 2^(-t / half_life)  — drops to 0.5 at t=half_life
    return math.pow(2, -days_since_access / half_life)


def apply_decay_optimized(stale: List[Dict[str, Any]], graph: MemoryGraph,
                        metrics: JanitorMetrics, dry_run: bool = True) -> Dict[str, int]:
    """Apply confidence decay to stale memories (with timing). Returns breakdown of actions.

    Supports two modes (config.decay.mode):
      - "exponential" (default): Ebbinghaus curve R = 2^(-t/half_life)
        New confidence = original_confidence * retention_factor
        Half-life scales with access count and verified status.
      - "linear": Legacy mode — subtract flat rate per cycle.
    """
    metrics.start_task("decay_application")
    deleted = 0
    decayed = 0
    queued = 0

    review_queue_enabled = _cfg.decay.review_queue_enabled
    use_exponential = _cfg.decay.mode == "exponential"
    min_conf = _cfg.decay.minimum_confidence

    for mem in stale:
        is_verified = mem.get("verified", False)

        if use_exponential:
            # Ebbinghaus exponential decay
            last_accessed = mem.get("last_accessed", "")
            try:
                accessed_dt = datetime.fromisoformat(last_accessed)
            except (ValueError, TypeError):
                accessed_dt = datetime.now() - timedelta(days=CONFIDENCE_DECAY_DAYS + 1)
            days_elapsed = (datetime.now() - accessed_dt).total_seconds() / 86400.0
            retention = _ebbinghaus_retention(
                days_elapsed, mem.get("access_count", 0), is_verified,
                storage_strength=mem.get("storage_strength", 0.0)
            )
            # Use extraction_confidence as baseline to avoid compounding decay
            # (applying retention to already-decayed confidence would create exp-of-exp)
            baseline = mem.get("extraction_confidence") or mem["confidence"]
            new_confidence = max(min_conf, baseline * retention)
            decay_type = f"EXP(R={retention:.3f})"
        else:
            # Legacy linear decay
            decay_rate = CONFIDENCE_DECAY_RATE * 0.5 if is_verified else CONFIDENCE_DECAY_RATE
            new_confidence = max(min_conf, mem["confidence"] - decay_rate)
            decay_type = "SLOW" if is_verified else "NORMAL"

        if new_confidence <= min_conf:
            if review_queue_enabled:
                if dry_run:
                    print(f"  Would QUEUE for review ({decay_type}): {mem['text'][:50]}... (confidence {mem['confidence']:.2f})")
                else:
                    queue_for_decay_review(mem)
                    print(f"  QUEUED for review ({decay_type}): {mem['text'][:50]}... (confidence {mem['confidence']:.2f})")
                    queued += 1
            else:
                if dry_run:
                    print(f"  Would DELETE ({decay_type}): {mem['text'][:50]}... (confidence {mem['confidence']:.2f} -> deleted)")
                else:
                    if _archive_node(mem, "confidence_decay"):
                        hard_delete_node(mem["id"])
                        print(f"  DELETED ({decay_type}): {mem['text'][:50]}... (confidence {mem['confidence']:.2f} -> deleted)")
                        deleted += 1
                    else:
                        print(f"  SKIPPED delete (archive failed): {mem['text'][:50]}...", file=sys.stderr)
        else:
            if dry_run:
                print(f"  Would decay ({decay_type}): {mem['text'][:50]}... ({mem['confidence']:.2f} -> {new_confidence:.2f})")
            else:
                with graph._get_conn() as conn:
                    conn.execute(
                        "UPDATE nodes SET confidence = ? WHERE id = ?",
                        (new_confidence, mem["id"])
                    )
                print(f"  Decayed ({decay_type}): {mem['text'][:50]}... ({mem['confidence']:.2f} -> {new_confidence:.2f})")
                decayed += 1

    metrics.end_task("decay_application")
    return {"decayed": decayed, "deleted": deleted, "queued": queued}


# =============================================================================
# Memory Review (Interactive)
# =============================================================================

def review_pending_memories(graph: MemoryGraph, dry_run: bool = True,
                            metrics: Optional[JanitorMetrics] = None) -> Dict[str, Any]:
    """
    Review all pending memories via the deep-reasoning LLM.
    Sends batches of memories for KEEP/DELETE/FIX/MERGE decisions and applies them immediately.
    """
    model = _cfg.janitor.opus_review.model
    max_tokens = _cfg.models.max_output('deep')

    # Get all pending memories
    with graph._get_conn() as conn:
        rows = conn.execute("""
            SELECT id, type, name, created_at, verified, confidence, source, session_id, speaker
            FROM nodes
            WHERE status = 'pending'            ORDER BY created_at DESC
        """).fetchall()

    if not rows:
        print("No pending memories found")
        return {"total_reviewed": 0, "kept": 0, "deleted": 0, "fixed": 0, "merged": 0}

    print(f"\n{'='*80}")
    print(f"MEMORY REVIEW - {len(rows)} pending memories via Opus API")
    print(f"{'='*80}")

    owner = _owner_display_name()
    owner_full = _owner_full_name()
    system_prompt = f"""You are reviewing memories in {owner}'s personal knowledge base.
For each memory, decide: KEEP, DELETE, FIX, or MERGE.

This is a PERSONAL knowledge base — it stores facts about people ({owner}, family, friends,
colleagues, pets), their preferences, relationships, decisions, and life events.
System architecture, infrastructure configs, and operational rules belong in documentation, NOT here.

CRITERIA:
- KEEP: Personal facts, preferences, opinions, decisions (with reasoning), relationships,
  significant life events, health info, locations, schedules, emotional reactions.
  Personal tech decisions count ("{owner} chose X because Y") — the decision is about the person.
- DELETE: Noise, conversational filler, temporary/ephemeral info, obvious duplicates,
  vague/unactionable statements (e.g. "The user wants a specific workflow" with no detail).
  Also DELETE: system architecture facts, infrastructure knowledge, operational rules for AI agents,
  tool/config descriptions, code implementation details — these belong in docs/RAG, not personal memory.
- FIX: Good info with attribution errors or clarity issues (fix "The user" -> "{owner}")
- MERGE: Multiple related memories -> consolidate into one

TEMPORAL RESOLUTION — IMPORTANT:
Each memory includes a "created_at" timestamp showing when it was recorded.
If a memory contains RELATIVE time references (tomorrow, yesterday, today, tonight,
this morning, next week, last month, etc.), use FIX to replace them with ABSOLUTE dates.
Use the created_at field to calculate the real date.
Example: created_at "2026-02-05", text "{owner} is meeting a friend tomorrow for tea"
  → FIX with new_text "{owner} met a friend for tea on 2026-02-06"
If the event is clearly in the past, use past tense. If clearly a one-time event that
has already passed, consider DELETE instead if the fact has no lasting value.
For recurring events or ongoing truths, just fix the date and keep.

EDGE EXTRACTION — For FIX operations:
When fixing a relationship fact, also provide edges to update the knowledge graph.
Edges connect named entities with directed relationships.

EDGE DIRECTION RULES:
- parent_of: PARENT is subject. "X is {owner}'s mom" → X --parent_of--> {owner}
- sibling_of: alphabetical order (symmetric)
- spouse_of: alphabetical order (symmetric)
- has_pet: OWNER is subject. "{owner} has a dog named Y" → {owner} --has_pet--> Y

Only include edges when the fact describes a relationship between named entities.
Do not include edges for facts that don't describe relationships (preferences, events, etc.).

Any merged_text or new_text for facts MUST be at least 3 words (subject + verb + object). Entity names (people, places) can be 1-2 words.

Respond with a JSON array only, no markdown fencing:
[
  {{"id": "uuid", "action": "KEEP"}},
  {{"id": "uuid", "action": "DELETE"}},
  {{"id": "uuid", "action": "FIX", "new_text": "corrected text"}},
  {{"id": "uuid", "action": "FIX", "new_text": "Beth is {owner}'s sister", "edges": [{{"subject": "Beth", "relation": "sibling_of", "object": "{owner_full}"}}]}},
  {{"action": "MERGE", "merge_ids": ["uuid1", "uuid2"], "merged_text": "consolidated"}}
]"""

    # Split into token-aware batches (output_tokens_per_item=200 caps batch size
    # so that total output fits in max_tokens — prevents truncation)
    builder = TokenBatchBuilder(
        model_tier='deep',
        prompt_overhead_tokens=estimate_tokens(system_prompt) + 100,
        tokens_per_item_fn=lambda row: estimate_tokens(row["name"]) + 60,
        max_items=500,
        max_output_tokens=max_tokens,
        output_tokens_per_item=200,
    )
    batches = builder.build_batches(list(rows))
    total_batches = len(batches)
    totals = {"kept": 0, "deleted": 0, "fixed": 0, "merged": 0}

    for batch_num, batch_rows in enumerate(batches, 1):
        # Build batch payload
        batch_data = []
        for row in batch_rows:
            batch_data.append({
                "id": row["id"],
                "text": row["name"],
                "type": row["type"],
                "created_at": row["created_at"],
                "source": row["source"],
                "speaker": row["speaker"] if "speaker" in row.keys() else None
            })

        user_message = f"Review batch {batch_num}/{total_batches} ({len(batch_data)} memories):\n\n{json.dumps(batch_data, indent=2)}"

        print(f"\n  Batch {batch_num}/{total_batches} ({len(batch_data)} memories)...")

        try:
            # Scale output tokens with batch size: ~200 tokens per item
            # Builder guarantees batch size fits in max_tokens via output_tokens_per_item
            batch_max_tokens = 200 * len(batch_data)
            response_text, duration = call_llm(
                system_prompt=system_prompt,
                user_message=user_message,
                model=model,
                max_tokens=batch_max_tokens
            )
            if metrics:
                metrics.add_llm_call(duration)

            if not response_text:
                print(f"    API returned empty response, skipping batch")
                if metrics:
                    metrics.add_error(f"Review batch {batch_num}: empty API response")
                continue

            decisions = parse_json_response(response_text)
            if not isinstance(decisions, list):
                print(f"    Failed to parse response as JSON array, skipping batch")
                if metrics:
                    metrics.add_error(f"Review batch {batch_num}: invalid JSON response")
                continue

            print(f"    Received {len(decisions)} decisions in {duration:.1f}s")

            # Apply immediately
            batch_result = apply_review_decisions_from_list(graph, decisions, dry_run)
            totals["kept"] += batch_result["kept"]
            totals["deleted"] += batch_result["deleted"]
            totals["fixed"] += batch_result["fixed"]
            totals["merged"] += batch_result["merged"]

        except RuntimeError as e:
            # API key issues — abort all review batches
            print(f"    API key error: {e}")
            if metrics:
                metrics.add_error(f"Review aborted: {e}")
            raise
        except Exception as e:
            print(f"    Batch {batch_num} failed: {e}")
            if metrics:
                metrics.add_error(f"Review batch {batch_num}: {e}")
            continue

    print(f"\n  Review complete: {totals['kept']} kept, {totals['deleted']} deleted, "
          f"{totals['fixed']} fixed, {totals['merged']} merged")

    return {
        "total_reviewed": len(rows),
        **totals
    }


def apply_review_decisions_from_list(graph: MemoryGraph, decisions: List[Dict[str, Any]],
                                     dry_run: bool = True) -> Dict[str, Any]:
    """Apply memory review decisions from a Python list of dicts (KEEP/DELETE/FIX/MERGE)."""
    kept = 0
    deleted = 0
    fixed = 0
    merged = 0

    for decision in decisions:
        action = decision.get("action", "").upper()

        # Handle MERGE
        if action == "MERGE" and "merge_ids" in decision and "merged_text" in decision:
            merge_ids = decision["merge_ids"]
            merged_text = decision["merged_text"]
            if dry_run:
                print(f"    Would MERGE {len(merge_ids)} memories -> {merged_text[:50]}...")
            else:
                try:
                    result = _merge_nodes_into(
                        graph, merged_text,
                        merge_ids,
                        source="janitor_merge",
                    )
                    new_id = result.get("id", "unknown")[:8] if result else "unknown"
                    print(f"    MERGED {len(merge_ids)} memories -> {new_id}... ({merged_text[:40]}...)")
                except (ValueError, Exception) as e:
                    print(f"    MERGE failed for {merge_ids}: {e}")
                    continue
            merged += 1
            continue

        memory_id = decision.get("id")
        if not memory_id:
            continue

        if action == "DELETE":
            if dry_run:
                print(f"    Would DELETE: {memory_id}")
            else:
                # Delete edges created from this fact before deleting the fact
                edges_deleted = delete_edges_by_source_fact(memory_id)
                if edges_deleted > 0:
                    print(f"    DELETED {edges_deleted} edges from fact")
                hard_delete_node(memory_id)
                print(f"    DELETED: {memory_id}")
            deleted += 1

        elif action == "FIX" and "new_text" in decision:
            new_text = decision["new_text"]
            new_edges = decision.get("edges", [])
            if dry_run:
                print(f"    Would FIX: {memory_id} -> {new_text[:50]}...")
                if new_edges:
                    print(f"    Would create {len(new_edges)} new edges")
            else:
                # Delete old edges created from this fact
                edges_deleted = delete_edges_by_source_fact(memory_id)
                if edges_deleted > 0:
                    print(f"    DELETED {edges_deleted} old edges from fact")

                # Update the fact text, embedding, and content_hash
                from lib.embeddings import get_embedding as _get_emb_fix, pack_embedding as _pack_emb_fix
                new_emb = _get_emb_fix(new_text)
                new_hash = content_hash(new_text)
                with graph._get_conn() as conn:
                    packed_emb = _pack_emb_fix(new_emb) if new_emb else None
                    conn.execute(
                        "UPDATE nodes SET name = ?, embedding = ?, content_hash = ?, updated_at = ?, status = 'approved' WHERE id = ?",
                        (new_text, packed_emb, new_hash, datetime.now().isoformat(), memory_id)
                    )
                    # Update vec_nodes index with new embedding
                    if packed_emb:
                        try:
                            conn.execute(
                                "INSERT OR REPLACE INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                                (memory_id, packed_emb)
                            )
                        except Exception:
                            pass  # vec_nodes may not exist
                print(f"    FIXED: {memory_id} -> {new_text[:50]}...")

                # Create new edges if provided
                for edge_data in new_edges:
                    if edge_data.get("subject") and edge_data.get("relation") and edge_data.get("object"):
                        try:
                            # Normalize edge relation before creating
                            norm = _normalize_edge(
                                edge_data["subject"], "entity",
                                edge_data["relation"],
                                edge_data["object"], "entity"
                            )
                            result = create_edge(
                                norm[0],
                                norm[2],
                                norm[3],
                                source_fact_id=memory_id
                            )
                            if result["status"] == "created":
                                print(f"    Created edge: {edge_data['subject']} --{edge_data['relation']}--> {edge_data['object']}")
                        except Exception as e:
                            print(f"    Failed to create edge: {e}")
            fixed += 1

        elif action == "KEEP":
            if not dry_run:
                with graph._get_conn() as conn:
                    conn.execute(
                        "UPDATE nodes SET status = 'approved' WHERE id = ?",
                        (memory_id,)
                    )
            kept += 1

    return {"kept": kept, "deleted": deleted, "fixed": fixed, "merged": merged}


# ── Temporal resolution (no LLM) ────────────────────────────────────────

# Regex patterns for relative temporal references
_RELATIVE_TEMPORAL_PATTERNS = [
    (r'\btomorrow\b', 1),
    (r'\byesterday\b', -1),
    (r'\btoday\b', 0),
    (r'\btonight\b', 0),
    (r'\bthis morning\b', 0),
    (r'\bthis afternoon\b', 0),
    (r'\bthis evening\b', 0),
    (r'\bnext week\b', 7),
    (r'\blast week\b', -7),
    (r'\bnext month\b', 30),
    (r'\blast month\b', -30),
    (r'\bnext year\b', 365),
    (r'\blast year\b', -365),
]

# Compiled regex to detect ANY relative temporal reference
_TEMPORAL_DETECTOR = re.compile(
    '|'.join(pat for pat, _ in _RELATIVE_TEMPORAL_PATTERNS),
    re.IGNORECASE
)


def _resolve_relative_date(text: str, created_at: str) -> Optional[str]:
    """Replace relative temporal references with absolute dates.

    Returns the fixed text, or None if no changes needed.
    """
    if not _TEMPORAL_DETECTOR.search(text):
        return None

    try:
        # Parse created_at (ISO format: 2026-02-05T15:16:27.535993)
        base_date = datetime.fromisoformat(created_at).date()
    except (ValueError, TypeError):
        return None

    new_text = text
    changed = False
    for pattern, delta_days in _RELATIVE_TEMPORAL_PATTERNS:
        # Loop to replace ALL occurrences of each pattern (not just the first)
        while True:
            match = re.search(pattern, new_text, re.IGNORECASE)
            if not match:
                break
            target_date = base_date + timedelta(days=delta_days)
            date_str = target_date.strftime("%Y-%m-%d")
            new_text = new_text[:match.start()] + f"on {date_str}" + new_text[match.end():]
            changed = True

    if not changed:
        return None

    # Fix tense: if the resolved date is in the past, adjust "is meeting" → "met" etc.
    # This is best-effort for common patterns
    today = datetime.now().date()
    if base_date < today:
        # Simple tense fixes for common patterns
        new_text = re.sub(r'\bis meeting\b', 'met', new_text)
        new_text = re.sub(r'\bis having\b', 'had', new_text)
        new_text = re.sub(r'\bis going to\b', 'went to', new_text)
        new_text = re.sub(r'\bis visiting\b', 'visited', new_text)
        new_text = re.sub(r'\bwill meet\b', 'met', new_text)
        new_text = re.sub(r'\bwill have\b', 'had', new_text)
        new_text = re.sub(r'\bwill visit\b', 'visited', new_text)
        # "shows the next run as on 2026-02-03" → clean up awkward phrasing
        new_text = re.sub(r'\bas on (\d{4}-\d{2}-\d{2})\b', r'as \1', new_text)
        new_text = re.sub(r'\bnow shows\b', 'showed', new_text)

    return new_text


def resolve_temporal_references(graph: MemoryGraph, dry_run: bool = True,
                                 metrics: Optional[JanitorMetrics] = None) -> Dict[str, Any]:
    """Find and fix facts containing relative temporal references.

    Replaces words like 'tomorrow', 'yesterday', 'today' etc. with absolute dates
    calculated from the fact's created_at timestamp. No LLM needed.
    """
    if metrics:
        metrics.start_task("temporal_resolution")

    with graph._get_conn() as conn:
        # Find facts with potential temporal references
        rows = conn.execute("""
            SELECT id, name, created_at FROM nodes
            WHERE deleted_at IS NULL
            AND (
                name LIKE '%tomorrow%' OR name LIKE '%yesterday%'
                OR name LIKE '%today%' OR name LIKE '%tonight%'
                OR name LIKE '%this morning%' OR name LIKE '%this afternoon%'
                OR name LIKE '%this evening%'
                OR name LIKE '%next week%' OR name LIKE '%last week%'
                OR name LIKE '%next month%' OR name LIKE '%last month%'
                OR name LIKE '%next year%' OR name LIKE '%last year%'
            )
        """).fetchall()

    if not rows:
        print("  No facts with relative temporal references found")
        if metrics:
            metrics.end_task("temporal_resolution")
        return {"found": 0, "fixed": 0, "skipped": 0}

    print(f"  Found {len(rows)} facts with relative temporal references")

    fixed = 0
    skipped = 0
    for row in rows:
        fact_id = row["id"]
        old_text = row["name"]
        created_at = row["created_at"]

        new_text = _resolve_relative_date(old_text, created_at)
        if new_text is None:
            skipped += 1
            continue

        if dry_run:
            print(f"    Would fix: {old_text[:60]}...")
            print(f"           →  {new_text[:60]}...")
        else:
            from lib.embeddings import get_embedding as _get_emb_temp, pack_embedding as _pack_emb_temp
            new_emb = _get_emb_temp(new_text)
            new_hash = content_hash(new_text)
            packed_emb = _pack_emb_temp(new_emb) if new_emb else None
            with graph._get_conn() as conn:
                conn.execute(
                    "UPDATE nodes SET name = ?, embedding = ?, content_hash = ?, updated_at = ? WHERE id = ?",
                    (new_text, packed_emb, new_hash, datetime.now().isoformat(), fact_id)
                )
                if packed_emb:
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                            (fact_id, packed_emb)
                        )
                    except Exception:
                        pass  # vec_nodes may not exist
            print(f"    Fixed: {old_text[:50]}... → {new_text[:50]}...")
        fixed += 1

    if metrics:
        metrics.end_task("temporal_resolution")

    return {"found": len(rows), "fixed": fixed, "skipped": skipped}


def get_completed_review_work_today() -> Dict[str, int]:
    """Check for review work completed today (since midnight).

    Note: With hard deletes, deleted rows are gone from nodes table.
    This now only counts approvals/fixes (deletions are tracked in janitor runs).
    """
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    from lib.database import get_connection as _get_db_conn
    with _get_db_conn(DB_PATH) as conn:
        # Count fixes (status changes from pending to approved) today
        cursor = conn.execute("""
            SELECT COUNT(*) FROM nodes
            WHERE status = 'approved'
            AND updated_at > ?
        """, (today_start.isoformat(),))
        reviewed = cursor.fetchone()[0]

    return {
        "deleted": 0,  # Hard-deleted rows are gone; tracked in janitor_runs
        "fixed": 0,
        "reviewed": reviewed
    }


def apply_review_decisions(graph: MemoryGraph, decisions_file: str, dry_run: bool = True) -> Dict[str, Any]:
    """Apply memory review decisions from a JSONL file (fallback for file-based workflow)."""
    decisions = []
    with open(decisions_file, "r") as f:
        for line in f:
            if line.strip():
                decisions.append(json.loads(line.strip()))

    print(f"\nApplying {len(decisions)} memory decisions from file...")
    result = apply_review_decisions_from_list(graph, decisions, dry_run)

    if not dry_run:
        os.remove(decisions_file)
        print(f"  Cleaned up decisions file")

    result["total_reviewed"] = len(decisions)
    return result


# =============================================================================
# Task 8: Run Unit Tests
# =============================================================================



def run_tests(metrics: JanitorMetrics) -> Dict[str, Any]:
    """Run npm test for quaid plugin and report pass/fail counts."""
    metrics.start_task("tests")
    plugin_dir = str(Path(__file__).parent)

    result = {"tests_passed": 0, "tests_failed": 0, "tests_total": 0, "success": False}

    try:
        proc = subprocess.run(
            ["npm", "test"],
            cwd=plugin_dir,
            capture_output=True,
            text=True,
            timeout=600  # vitest suite needs ~5min with Ollama embedding calls
        )

        output = proc.stdout + proc.stderr

        # Parse pass/fail from test-runner.js summary output
        # e.g. "Total: 20" / "Passed: 20" / "Failed: 0"
        total_match = re.search(r'Total:\s+(\d+)', output)
        passed_match = re.search(r'Passed:\s+(\d+)', output)
        failed_match = re.search(r'Failed:\s+(\d+)', output)

        if total_match and passed_match:
            result["tests_total"] = int(total_match.group(1))
            result["tests_passed"] = int(passed_match.group(1))
            result["tests_failed"] = int(failed_match.group(1)) if failed_match else 0
        else:
            # Fallback: vitest format "Tests  X failed | Y passed (Z)"
            vitest_match = re.search(r'Tests\s+(?:(\d+)\s+failed\s*\|\s*)?(\d+)\s+passed(?:\s*\((\d+)\))?', output)
            if vitest_match:
                result["tests_failed"] = int(vitest_match.group(1) or 0)
                result["tests_passed"] = int(vitest_match.group(2))
                result["tests_total"] = int(vitest_match.group(3) or result["tests_passed"] + result["tests_failed"])
            else:
                # Last fallback: check exit code
                result["tests_total"] = 1
                if proc.returncode == 0:
                    result["tests_passed"] = 1
                else:
                    result["tests_failed"] = 1

        result["success"] = proc.returncode == 0

        if proc.returncode == 0:
            print(f"  All tests passed ({result['tests_passed']}/{result['tests_total']})")
        else:
            print(f"  Tests failed: {result['tests_failed']}/{result['tests_total']}")
            # Print last few lines of output for context
            lines = output.strip().split('\n')
            for line in lines[-10:]:
                print(f"    {line}")

    except subprocess.TimeoutExpired:
        print(f"  Tests timed out after 600s")
        metrics.add_error("Unit tests timed out")
        result["tests_failed"] = 1
        result["tests_total"] = 1
    except FileNotFoundError:
        print(f"  npm not found")
        metrics.add_error("npm not found for unit tests")
    except Exception as e:
        print(f"  Test execution error: {e}")
        metrics.add_error(f"Unit test error: {e}")

    metrics.end_task("tests")
    return result


# =============================================================================
# Main Execution (Enhanced)
# =============================================================================

def _lock_file_path() -> Path:
    return _data_dir() / ".janitor.lock"
_lock_fd = None  # File descriptor for flock-based locking


def _acquire_lock() -> bool:
    """Acquire janitor lock using fcntl.flock (atomic, auto-releases on crash)."""
    global _lock_fd
    import fcntl
    try:
        _lock_file_path().parent.mkdir(parents=True, exist_ok=True)
        _lock_fd = open(_lock_file_path(), 'w')
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(f"{os.getpid()}\n{datetime.now().isoformat()}")
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False


def _release_lock():
    """Release janitor lock."""
    global _lock_fd
    import fcntl
    try:
        if _lock_fd:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
            _lock_fd = None
        _lock_file_path().unlink(missing_ok=True)
    except Exception:
        pass


def _check_for_updates() -> Optional[Dict[str, str]]:
    """Check GitHub releases API for a newer Quaid version.

    Returns dict with 'latest', 'current', 'url' if update available, None otherwise.
    Caches result for 24 hours in janitor metadata table.
    """
    import urllib.request
    import urllib.error

    from lib.adapter import get_adapter as _get_adapter
    REPO = _get_adapter().get_repo_slug()
    RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

    # Read current version
    version_file = Path(__file__).parent / "VERSION"
    if not version_file.exists():
        return None
    current = version_file.read_text().strip()
    if not current:
        return None

    # Check cache — skip if checked within 24h
    try:
        graph = get_graph()
        with graph._get_conn() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM metadata WHERE key = 'update_check'"
            ).fetchone()
            if row:
                last_check = datetime.fromisoformat(row["updated_at"])
                if datetime.now() - last_check < timedelta(hours=24):
                    cached = json.loads(row["value"])
                    if cached.get("latest") and cached["latest"] != current:
                        return cached
                    return None
    except Exception:
        pass

    # Fetch latest release from GitHub
    try:
        req = urllib.request.Request(RELEASES_URL, headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "quaid-update-checker",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest_tag = data.get("tag_name", "").lstrip("v")
        html_url = data.get("html_url", f"https://github.com/{REPO}/releases")
    except (urllib.error.URLError, Exception) as e:
        print(f"  Update check failed (network): {e}")
        return None

    # Cache the result
    result = {"latest": latest_tag, "current": current, "url": html_url}
    try:
        graph = get_graph()
        with graph._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                ("update_check", json.dumps(result)),
            )
    except Exception:
        pass

    if latest_tag and latest_tag != current:
        # Only notify if latest is actually newer (semver comparison)
        try:
            from packaging.version import Version
            if Version(latest_tag) <= Version(current):
                return None
        except Exception:
            # Fallback: simple string comparison — still skip if equal
            pass
        return result
    return None


def run_task_optimized(task: str, dry_run: bool = True, incremental: bool = True,
                       time_budget: int = 0, force_distill: bool = False,
                       user_approved: bool = False):
    """Run optimized janitor task with comprehensive reporting.

    Args:
        time_budget: Wall-clock budget in seconds. 0 = unlimited.
            When set, tasks are skipped if remaining time is insufficient.
        force_distill: Force journal distillation regardless of interval.
    """
    # Prevent concurrent janitor runs
    if not _acquire_lock():
        print("ERROR: Another janitor instance is already running. Exiting.")
        print(f"  Lock file: {_lock_file_path()}")
        print(f"  To force: delete the lock file and retry.")
        return {"error": "janitor_already_running", "success": False, "applied_changes": {}, "metrics": {}}

    try:
        return _run_task_optimized_inner(task, dry_run, incremental, time_budget, force_distill, user_approved)
    finally:
        _release_lock()


def _resolve_apply_mode(args_apply: bool, args_approve: bool) -> tuple[bool, Optional[str]]:
    """Resolve final dry-run state from CLI flags + janitor.apply_mode config.

    Returns (dry_run, warning_message). warning_message is user-facing guidance
    when an apply request is downgraded to dry-run by policy.
    """
    if not args_apply:
        return True, None

    mode = str(getattr(_cfg.janitor, "apply_mode", "auto") or "auto").strip().lower()
    if mode == "auto":
        return False, None
    if mode == "dry_run":
        return True, (
            "apply blocked by janitor.applyMode=dry_run; running dry-run only."
        )
    if mode == "ask":
        if args_approve:
            return False, None
        return True, (
            "approval required by janitor.applyMode=ask. "
            "Re-run with --approve to apply changes."
        )
    return True, f"unknown janitor.applyMode={mode}; running dry-run for safety."


def _decision_log_path() -> Path:
    p = _logs_dir() / "janitor" / "decision-log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append_decision_log(kind: str, payload: Dict[str, Any]) -> None:
    """Append janitor decision events for audit/debug."""
    try:
        row = {
            "ts": datetime.now().isoformat(),
            "kind": kind,
            **payload,
        }
        with _decision_log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _pending_approvals_json_path() -> Path:
    p = _logs_dir() / "janitor" / "pending-approval-requests.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _pending_approvals_md_path() -> Path:
    p = _logs_dir() / "janitor" / "pending-approval-requests.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _delayed_notifications_path() -> Path:
    p = _logs_dir() / "janitor" / "delayed-notifications.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _queue_delayed_notification(
    message: str,
    kind: str = "janitor",
    priority: str = "normal",
) -> None:
    """Queue notification for adapter-delayed delivery (next active user session)."""
    if not message:
        return
    path = _delayed_notifications_path()
    payload: Dict[str, Any] = {"version": 1, "items": []}
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                payload = {"version": 1, "items": []}
    except Exception:
        payload = {"version": 1, "items": []}

    items = payload.get("items", [])
    if not isinstance(items, list):
        items = []

    item = {
        "id": hashlib.sha256(f"{kind}|{message}".encode()).hexdigest()[:12],
        "created_at": datetime.now().isoformat(),
        "kind": kind,
        "priority": priority,
        "message": message,
        "status": "pending",
    }
    if not any(isinstance(x, dict) and x.get("id") == item["id"] and x.get("status") == "pending" for x in items):
        items.append(item)
    payload["items"] = items
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _append_decision_log(
        "delayed_notification_queued",
        {
            "id": item["id"],
            "kind": kind,
            "priority": priority,
        },
    )


def _queue_approval_request(scope: str, task_name: str, summary: str) -> None:
    """Queue a pending approval request for heartbeat + user follow-up."""
    entry = {
        "id": hashlib.sha256(f"{scope}|{task_name}|{summary}".encode()).hexdigest()[:12],
        "created_at": datetime.now().isoformat(),
        "scope": scope,
        "task": task_name,
        "summary": summary,
        "status": "pending",
    }
    path = _pending_approvals_json_path()
    data: Dict[str, Any] = {"version": 1, "requests": []}
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {"version": 1, "requests": []}
    except Exception:
        data = {"version": 1, "requests": []}

    reqs = data.get("requests", [])
    if not isinstance(reqs, list):
        reqs = []

    # De-dup same request id.
    is_new = not any(isinstance(r, dict) and r.get("id") == entry["id"] for r in reqs)
    if is_new:
        reqs.append(entry)
    data["requests"] = reqs
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    md = _pending_approvals_md_path()
    lines = [
        "# Janitor Pending Approval Requests",
        "",
        "These changes were held because policy is set to `ask`.",
        "Run with `--approve` once reviewed, or switch scope to `auto` in `quaid config edit`.",
        "",
    ]
    for r in reqs:
        if not isinstance(r, dict):
            continue
        lines.append(f"- [{r.get('status', 'pending')}] `{r.get('scope', 'unknown')}` "
                     f"({r.get('created_at', '?')}): {r.get('summary', '')}")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _append_decision_log("approval_queued", entry)
    if is_new:
        _queue_delayed_notification(
            f"[Quaid] ⚠️ Janitor held `{scope}` change: {summary}\n"
            f"Review logs/janitor/pending-approval-requests.md.\n"
            f"Run `quaid janitor --apply --task {task_name} --approve` to apply,\n"
            f"or switch this scope to auto via `quaid config edit`.",
            kind="approval_request",
            priority="high",
        )


def _run_task_optimized_inner(task: str, dry_run: bool = True, incremental: bool = True,
                              time_budget: int = 0, force_distill: bool = False,
                              user_approved: bool = False):
    """Inner implementation of run_task_optimized (with lock held)."""
    # Rotate logs at start of janitor run
    rotate_logs()
    reset_token_usage()

    janitor_logger.info("janitor_start", task=task, dry_run=dry_run, incremental=incremental)

    graph = get_graph()
    metrics = JanitorMetrics()

    # Initialize metadata tracking
    init_janitor_metadata(graph)

    # Determine if this is an incremental run
    last_run = None
    if incremental and task != "decay":  # Decay always needs full scan
        last_run = get_last_run_time(graph, task)

    print(f"\n{'='*80}")
    print(f"Memory Janitor - Task: {task}")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    print(f"Strategy: {'INCREMENTAL' if last_run else 'FULL SCAN'}")
    if time_budget > 0:
        print(f"Time budget: {time_budget}s")
    if last_run:
        print(f"Last run: {last_run.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}\n")

    def _scope_policy(scope: str) -> str:
        try:
            policy = str(_cfg.janitor.approval_policies.get(scope, "auto")).strip().lower()
            return policy if policy in ("auto", "ask", "dry_run") else "auto"
        except Exception:
            return "auto"

    def _can_apply_scope(scope: str, summary: str) -> bool:
        if dry_run:
            return False
        mode = _scope_policy(scope)
        if mode == "auto":
            return True
        if mode == "ask":
            if user_approved:
                return True
            _queue_approval_request(scope, task, summary)
            print(f"[policy] {scope}=ask, holding apply: {summary}")
            print("[policy] Re-run with --approve, or set this scope to auto in `quaid config edit`.")
            return False
        # dry_run policy
        _queue_approval_request(scope, task, summary + " (policy dry_run)")
        print(f"[policy] {scope}=dry_run, holding apply: {summary}")
        print("[policy] Switch scope to auto/ask in `quaid config edit` when ready.")
        return False

    # ⚠️ LLM PROVIDER CHECK — janitor needs a working LLM provider for most tasks.
    # The adapter layer handles provider selection and authentication.
    if task not in ("embeddings", "cleanup", "rag"):
        try:
            from lib.adapter import get_adapter
            _llm = get_adapter().get_llm_provider()
            _profiles = _llm.get_profiles()
            if not _profiles.get("deep", {}).get("available"):
                raise RuntimeError("High-reasoning model not available")
        except Exception as _provider_err:
            print("⚠️" * 10)
            print(f"⚠️  WARNING: LLM provider not available: {_provider_err}")
            print("⚠️  The janitor needs a working LLM provider for review, dedup, and decay tasks.")
            print("⚠️  Check your adapter configuration (config/memory.json adapter.type, gateway status, etc.).")
            print("⚠️" * 10)
            print()

    # Time budget helper — returns remaining seconds, or skips the task
    # Safety margin: stop 30s before budget to allow report generation
    _BUDGET_SAFETY_MARGIN = 30

    def _time_remaining() -> float:
        """Seconds remaining in the time budget. float('inf') if unlimited."""
        if time_budget <= 0:
            return float('inf')
        return time_budget - metrics.total_duration() - _BUDGET_SAFETY_MARGIN

    def _skip_if_over_budget(task_label: str, min_seconds: int = 10) -> bool:
        """Check if we should skip a task due to time budget.

        Returns True (skip) if remaining time < min_seconds.
        """
        remaining = _time_remaining()
        if remaining < min_seconds:
            print(f"[{task_label}] SKIPPED — time budget exhausted "
                  f"({metrics.total_duration():.0f}s elapsed, "
                  f"{time_budget}s budget)\n")
            _budget_skipped.append(task_label)
            return True
        return False

    # System gate mapping — which system must be enabled for each task
    _TASK_SYSTEM_GATE = {
        # Memory system
        "embeddings": "memory", "review": "memory", "temporal": "memory",
        "dedup_review": "memory", "duplicates": "memory", "contradictions": "memory",
        "decay": "memory", "decay_review": "memory",
        # Journal system
        "snippets": "journal", "soul_snippets": "journal", "journal": "journal",
        # Projects system
        "docs_staleness": "projects", "docs_cleanup": "projects", "rag": "projects",
        # Workspace system
        "workspace": "workspace",
        # Infrastructure tasks (always run): tests, cleanup
    }

    def _system_enabled_or_skip(task_name: str, task_label: str) -> bool:
        """Check if the system gate for a task is enabled. Prints skip message if disabled."""
        system = _TASK_SYSTEM_GATE.get(task_name)
        if not system:
            return True  # Infrastructure tasks always run
        enabled = getattr(_cfg.systems, system, True)
        if not enabled:
            print(f"[{task_label}] SKIPPED — {system} system disabled\n")
        return enabled

    # Memory pipeline health tracking — initialized here so it's always
    # available for the report section even if an exception occurs early.
    memory_pipeline_ok = True

    # Task execution with timing
    applied_changes = {
        "backup_keychain": False,
        "backup_core": False,
        "duplicates_merged": 0,
        "edges_created": 0,
        "memories_decayed": 0,
        "memories_deleted_by_decay": 0,
        "contradictions_found": 0,
        "contradictions_resolved": 0,
        "contradictions_false_positive": 0,
        "contradictions_merged": 0,
        "memories_reviewed": 0,
        "memories_deleted": 0,
        "memories_fixed": 0,
        "dedup_reviewed": 0,
        "dedup_confirmed": 0,
        "dedup_reversed": 0,
        "decay_queued": 0,
        "decay_reviewed": 0,
        "decay_review_deleted": 0,
        "decay_review_extended": 0,
        "decay_review_pinned": 0,
        "workspace_phase": "skipped",
        "workspace_moved_to_docs": 0,
        "workspace_moved_to_memory": 0,
        "rag_files_indexed": 0,
        "rag_chunks_created": 0,
        "rag_files_skipped": 0,
        "temporal_found": 0,
        "temporal_fixed": 0,
        "graduated_to_active": 0,
        "tests_passed": 0,
        "tests_failed": 0,
        "tests_total": 0
    }

    # Track which tasks were skipped due to budget
    _budget_skipped = []

    try:
        # --- Task 0b: Embedding Backfill ---
        # (Moved before memory pipeline — embeddings needed for recall pass)
        if task in ("embeddings", "all") and _system_enabled_or_skip("embeddings", "Task 0b: Embedding Backfill") and not _skip_if_over_budget("Task 0b: Embeddings", 15):
            print("[Task 0b: Embedding Backfill]")
            backfill_result = backfill_embeddings(graph, metrics, dry_run=dry_run)
            print(f"  Found: {backfill_result['found']}, Embedded: {backfill_result['embedded']}")
            print(f"Task completed in {metrics.task_duration('embedding_backfill'):.2f}s\n")

        # --- Task 2: Review Pending Memories (Opus API) ---
        # Memory pipeline starts here. If any task (2-6) fails,
        # remaining memory tasks are skipped and graduation is blocked.
        # Infrastructure tasks (0, 0b, 1, 7, 8) still run regardless.
        if task in ("review", "all") and _system_enabled_or_skip("review", "Task 2: Review Memories") and not _skip_if_over_budget("Task 2: Review Memories", 30):
            print("[Task 2: Review Pending Memories - Opus API]")
            metrics.start_task("review")

            try:
                review_result = review_pending_memories(graph, dry_run=dry_run, metrics=metrics)

                applied_changes["memories_reviewed"] = review_result["total_reviewed"]
                applied_changes["memories_deleted"] = review_result["deleted"]
                applied_changes["memories_fixed"] = review_result["fixed"]

                # Also check for work completed today
                today_work = get_completed_review_work_today()
                if today_work["deleted"] > 0 or today_work["fixed"] > 0 or today_work["reviewed"] > 0:
                    print(f"\n  Total review work completed today:")
                    print(f"   Reviewed: {today_work['reviewed']}")
                    print(f"   Deleted: {today_work['deleted']}")
                    print(f"   Fixed: {today_work['fixed']}")

                    applied_changes["memories_reviewed"] = max(applied_changes["memories_reviewed"], today_work["reviewed"])
                    applied_changes["memories_deleted"] = max(applied_changes["memories_deleted"], today_work["deleted"])
                    applied_changes["memories_fixed"] = max(applied_changes["memories_fixed"], today_work["fixed"])

                print(f"Reviewed {review_result['total_reviewed']} pending memories")
                print(f"{'Would delete' if dry_run else 'Deleted'}: {review_result['deleted']}")
                print(f"{'Would fix' if dry_run else 'Fixed'}: {review_result['fixed']}")
                print(f"Kept: {review_result['kept']}")
            except RuntimeError as e:
                print(f"  Opus API unavailable: {e}")
                print("  ABORTING memory pipeline — facts will remain as pending")
                metrics.add_error(f"Memory review failed (API error): {e}")
                memory_pipeline_ok = False

            metrics.end_task("review")
            print(f"Task completed in {metrics.task_duration('review'):.2f}s\n")

        # --- Task 2a: Temporal Resolution (no LLM) ---
        if task in ("temporal", "all") and _system_enabled_or_skip("temporal", "Task 2a: Temporal") and not _skip_if_over_budget("Task 2a: Temporal", 10):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 2a: Resolve Temporal References] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 2a: Resolve Temporal References]")
                temporal_result = resolve_temporal_references(graph, dry_run=dry_run, metrics=metrics)
                applied_changes["temporal_found"] = temporal_result["found"]
                applied_changes["temporal_fixed"] = temporal_result["fixed"]
                print(f"  Found: {temporal_result['found']}, Fixed: {temporal_result['fixed']}, Skipped: {temporal_result['skipped']}")
                print(f"Task completed in {metrics.task_duration('temporal_resolution'):.2f}s\n")

        # --- Task 2b: Review Dedup Rejections (Opus) ---
        if task in ("dedup_review", "all") and _system_enabled_or_skip("dedup_review", "Task 2b: Dedup Review") and not _skip_if_over_budget("Task 2b: Dedup Review", 20):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 2b: Review Dedup Rejections] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 2b: Review Dedup Rejections - Opus API]")

                try:
                    dedup_review_result = review_dedup_rejections(graph, metrics, dry_run=dry_run)

                    applied_changes["dedup_reviewed"] = dedup_review_result["reviewed"]
                    applied_changes["dedup_confirmed"] = dedup_review_result["confirmed"]
                    applied_changes["dedup_reversed"] = dedup_review_result["reversed"]

                    print(f"  Reviewed: {dedup_review_result['reviewed']}")
                    print(f"  Confirmed: {dedup_review_result['confirmed']}")
                    print(f"  Reversed (restored): {dedup_review_result['reversed']}")
                except RuntimeError as e:
                    print(f"  Opus API unavailable: {e}")
                    print("  ABORTING memory pipeline — facts will remain as pending")
                    metrics.add_error(f"Dedup review failed (API error): {e}")
                    memory_pipeline_ok = False

                print(f"Task completed in {metrics.task_duration('dedup_review'):.2f}s\n")

        # --- Tasks 3+4: Shared recall pass, then dedup + contradiction ---
        pair_buckets = None
        if task in ("duplicates", "contradictions", "all") and _system_enabled_or_skip("duplicates", "Tasks 3-4: Dedup+Contradictions") and not _skip_if_over_budget("Tasks 3-4: Dedup+Contradictions", 30):
            if task == "all" and not memory_pipeline_ok:
                print("[Tasks 3-4b: Dedup + Contradictions] SKIPPED — pipeline aborted\n")
            else:
                print("[Recall Pass: Building candidate pairs for dedup + contradictions]")
                _errors_before = len(metrics.errors)
                pair_buckets = recall_similar_pairs(graph, metrics, since=last_run)
                print(f"Recall pass completed in {metrics.task_duration('recall_pass'):.2f}s\n")

        if task in ("duplicates", "all") and _system_enabled_or_skip("duplicates", "Task 3: Find Near-Duplicates"):
            if task == "all" and not memory_pipeline_ok:
                pass  # Already printed skip message above
            else:
                print("[Task 3: Find Near-Duplicates]")
                dup_candidates = pair_buckets["duplicates"] if pair_buckets else []
                dups = find_duplicates_from_pairs(dup_candidates, metrics)
                print(f"Found {len(dups)} potential duplicates\n")

                merges_applied = 0
                merged_ids = set()  # Track already-merged node IDs
                dedup_apply_allowed = _can_apply_scope(
                    "destructive_memory_ops",
                    "dedup merge operations"
                )
                for dup in dups:
                    # Skip pairs where either node was already merged this run
                    if dup["id_a"] in merged_ids or dup["id_b"] in merged_ids:
                        continue

                    print(f"  Similarity: {dup['similarity']}")
                    print(f"    A: {dup['text_a'][:70]}...")
                    print(f"    B: {dup['text_b'][:70]}...")

                    suggestion = dup.get("suggestion", {})
                    if suggestion.get("action") == "merge":
                        merged_text = suggestion.get("merged_text", "")
                        print(f"    -> MERGE: {merged_text[:70]}...")
                        if (not dry_run) and dedup_apply_allowed and merged_text:
                            try:
                                id_a, id_b = dup["id_a"], dup["id_b"]
                                _merge_nodes_into(
                                    graph, merged_text,
                                    [id_a, id_b],
                                    source="dedup_merge",
                                )
                                _append_decision_log("dedup_merge", {
                                    "id_a": id_a,
                                    "id_b": id_b,
                                    "merged_text": merged_text[:400],
                                })
                                merged_ids.add(id_a)
                                merged_ids.add(id_b)
                                merges_applied += 1
                            except Exception as e:
                                print(f"    ERROR merging: {e}")
                                metrics.add_error(f"Dedup merge failed: {e}")
                    elif suggestion.get("action") == "keep_both":
                        print(f"    -> KEEP BOTH: {suggestion.get('reason', '')[:50]}...")
                    print()

                applied_changes["duplicates_merged"] = merges_applied
                print(f"Task completed in {metrics.task_duration('duplicates'):.2f}s\n")

        if task in ("contradictions", "all") and _system_enabled_or_skip("contradictions", "Task 4: Contradictions"):
            if task == "all" and not memory_pipeline_ok:
                pass  # Already printed skip message above
            else:
                print("[Task 4: Verify Contradictions]")
                contradiction_candidates = pair_buckets["contradictions"] if pair_buckets else []
                _errors_before = len(metrics.errors)  # Snapshot errors just before this task
                contradictions = find_contradictions_from_pairs(contradiction_candidates, metrics, dry_run=dry_run)

                applied_changes["contradictions_found"] = len(contradictions)
                if contradictions:
                    applied_changes["contradiction_findings"] = [
                        {
                            "text_a": c.get("text_a", ""),
                            "text_b": c.get("text_b", ""),
                            "reason": c.get("explanation", ""),
                        }
                        for c in contradictions[:25]
                    ]

                for contradiction in contradictions:
                    print(f"  CONTRADICTION FOUND:")
                    print(f"    A: {contradiction['text_a'][:60]}...")
                    print(f"    B: {contradiction['text_b'][:60]}...")
                    print(f"    Reason: {contradiction.get('explanation', '')[:50]}...")
                    print()
                    _append_decision_log("contradiction_found", {
                        "text_a": contradiction.get("text_a", ""),
                        "text_b": contradiction.get("text_b", ""),
                        "reason": contradiction.get("explanation", ""),
                    })

                # Check if batch functions added errors
                if len(metrics.errors) > _errors_before:
                    memory_pipeline_ok = False

                print(f"Task completed in {metrics.task_duration('contradictions'):.2f}s\n")

                # --- Task 4b: Resolve Contradictions (Opus) ---
                if not memory_pipeline_ok:
                    print("[Task 4b: Resolve Contradictions] SKIPPED — pipeline aborted\n")
                else:
                    print("[Task 4b: Resolve Contradictions]")
                    contradiction_apply_allowed = _can_apply_scope(
                        "destructive_memory_ops",
                        "contradiction resolution operations"
                    )
                    try:
                        resolution_result = resolve_contradictions_with_opus(
                            graph,
                            metrics,
                            dry_run=(dry_run or (not contradiction_apply_allowed))
                        )
                        applied_changes["contradictions_resolved"] = resolution_result["resolved"]
                        applied_changes["contradictions_false_positive"] = resolution_result["false_positive"]
                        applied_changes["contradictions_merged"] = resolution_result["merged"]
                        if resolution_result.get("decisions"):
                            applied_changes["contradiction_decisions"] = resolution_result["decisions"][:50]
                        print(f"  Resolved: {resolution_result['resolved']}, "
                              f"False positives: {resolution_result['false_positive']}, "
                              f"Merged: {resolution_result['merged']}")
                    except RuntimeError as e:
                        print(f"  Opus API unavailable: {e}")
                        print("  ABORTING memory pipeline — facts will remain as pending")
                        metrics.add_error(f"Contradiction resolution failed (API error): {e}")
                        memory_pipeline_ok = False
                    print(f"Task completed in {metrics.task_duration('contradiction_resolution'):.2f}s\n")

        # --- Task 5: Confidence Decay (no LLM) ---
        if task in ("decay", "all") and _system_enabled_or_skip("decay", "Task 5: Decay") and not _skip_if_over_budget("Task 5: Decay", 10):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 5: Confidence Decay] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 5: Confidence Decay]")
                stale = find_stale_memories_optimized(graph, metrics)
                print(f"Found {len(stale)} stale memories (>{CONFIDENCE_DECAY_DAYS} days unused)\n")
                decay_apply_allowed = _can_apply_scope(
                    "destructive_memory_ops",
                    "confidence decay updates/deletes"
                )
                decay_dry_run = dry_run or (not decay_apply_allowed)

                if stale:
                    decay_result = apply_decay_optimized(stale, graph, metrics, dry_run=decay_dry_run)
                    applied_changes["memories_decayed"] = decay_result["decayed"]
                    applied_changes["memories_deleted_by_decay"] = decay_result["deleted"]
                    applied_changes["decay_queued"] = decay_result.get("queued", 0)

                    total_updated = decay_result["decayed"] + decay_result["deleted"] + decay_result.get("queued", 0)
                    print(f"\n{'Would update' if decay_dry_run else 'Updated'} {total_updated} memories:")
                    print(f"  Decayed: {decay_result['decayed']}")
                    print(f"  Deleted: {decay_result['deleted']}")
                    print(f"  Queued for review: {decay_result.get('queued', 0)}")
                else:
                    print("  No stale memories found.")

                print(f"Task completed in {metrics.task_duration('decay_discovery') + metrics.task_duration('decay_application'):.2f}s\n")

        # --- Task 5b: Review Decayed Memories (Opus) ---
        if task in ("decay_review", "all") and _system_enabled_or_skip("decay_review", "Task 5b: Decay Review") and not _skip_if_over_budget("Task 5b: Decay Review", 20):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 5b: Review Decayed Memories] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 5b: Review Decayed Memories - Opus API]")
                decay_review_apply_allowed = _can_apply_scope(
                    "destructive_memory_ops",
                    "decay review decisions"
                )
                decay_review_dry_run = dry_run or (not decay_review_apply_allowed)

                try:
                    decay_review_result = review_decayed_memories(graph, metrics, dry_run=decay_review_dry_run)

                    applied_changes["decay_reviewed"] = decay_review_result["reviewed"]
                    applied_changes["decay_review_deleted"] = decay_review_result["deleted"]
                    applied_changes["decay_review_extended"] = decay_review_result["extended"]
                    applied_changes["decay_review_pinned"] = decay_review_result["pinned"]

                    print(f"  Reviewed: {decay_review_result['reviewed']}")
                    print(f"  Deleted: {decay_review_result['deleted']}")
                    print(f"  Extended: {decay_review_result['extended']}")
                    print(f"  Pinned: {decay_review_result['pinned']}")
                except RuntimeError as e:
                    print(f"  Opus API unavailable: {e}")
                    print("  ABORTING memory pipeline — facts will remain as pending")
                    metrics.add_error(f"Decay review failed (API error): {e}")
                    memory_pipeline_ok = False

                print(f"Task completed in {metrics.task_duration('decay_review'):.2f}s\n")

        # --- Task 1: Workspace Audit (Opus API) ---
        # (Runs after memory pipeline — memory tasks are higher priority under time budget)
        if task in ("workspace", "all") and _system_enabled_or_skip("workspace", "Task 1: Workspace Audit") and not _skip_if_over_budget("Task 1: Workspace Audit", 30):
            print("[Task 1: Workspace Audit - Single-Pass Opus Review]")
            metrics.start_task("workspace_audit")
            workspace_apply_allowed = _can_apply_scope(
                "workspace_file_moves_deletes",
                "workspace file moves/deletes"
            )
            workspace_dry_run = dry_run or (not workspace_apply_allowed)

            try:
                audit_result = run_workspace_check(dry_run=workspace_dry_run)

                phase = audit_result.get("phase", "unknown")
                applied_changes["workspace_phase"] = phase

                # Check for bloat warnings
                bloat_stats = audit_result.get("bloat_stats", {})
                bloated = [f for f, s in bloat_stats.items() if s.get("over_limit")]
                if bloated:
                    applied_changes["bloated_files"] = bloated
                    print(f"\n  Files over limit: {', '.join(bloated)}")
                    for f in bloated:
                        s = bloat_stats[f]
                        print(f"     {f}: {s['lines']}/{s['maxLines']} lines")

                if phase == "apply":
                    applied_changes["workspace_moved_to_docs"] = audit_result.get("moved_to_docs", 0)
                    applied_changes["workspace_moved_to_memory"] = audit_result.get("moved_to_memory", 0)
                    applied_changes["workspace_trimmed"] = audit_result.get("trimmed", 0)
                    applied_changes["workspace_bloat_warnings"] = audit_result.get("bloat_warnings", 0)
                    applied_changes["workspace_project_detected"] = audit_result.get("project_detected", 0)
                    print(f"\n{'Would apply' if workspace_dry_run else 'Applied'} review decisions:")
                    print(f"   Moved to docs: {audit_result.get('moved_to_docs', 0)}")
                    print(f"   Moved to memory: {audit_result.get('moved_to_memory', 0)}")
                    print(f"   Trimmed: {audit_result.get('trimmed', 0)}")
                    print(f"   Bloat warnings: {audit_result.get('bloat_warnings', 0)}")
                    project_detected = audit_result.get("project_detected", 0)
                    if project_detected > 0:
                        print(f"   Project content detected: {project_detected} (queued for agent review)")
                elif phase == "no_changes":
                    print(f"\n  No workspace files changed since last run")
                elif phase == "error":
                    print(f"\n  Workspace audit error: {audit_result.get('error', 'unknown')}")
            except RuntimeError as e:
                print(f"  Opus API unavailable: {e}")
                print("  Skipping workspace audit, continuing with local tasks...")
                metrics.add_error(f"Workspace audit skipped (API error): {e}")

            metrics.end_task("workspace_audit")
            print(f"Task completed in {metrics.task_duration('workspace_audit'):.2f}s\n")

        # --- Task 1b: Documentation Staleness Check ---
        # (Runs after memory pipeline — expensive Opus doc updates are lower priority)
        if task in ("docs_staleness", "all") and _system_enabled_or_skip("docs_staleness", "Task 1b: Doc Staleness") and not _skip_if_over_budget("Task 1b: Doc Staleness", 60):
            print("[Task 1b: Documentation Staleness Check]")
            metrics.start_task("docs_staleness")
            try:
                from docs_updater import check_staleness, update_doc_from_diffs, get_doc_purposes
                stale = check_staleness()
                if not stale:
                    print("  All docs up-to-date with source files")
                else:
                    print(f"  Found {len(stale)} stale doc(s):")
                    purposes = get_doc_purposes()
                    for doc_path, info in stale.items():
                        print(f"    {doc_path} ({info.gap_hours:.1f}h behind)")
                        for src in info.stale_sources:
                            print(f"      <- {src}")
                        doc_p = Path(doc_path)
                        is_root_md = len(doc_p.parts) == 1 and doc_p.suffix.lower() == ".md"
                        is_quaid_project_md = (
                            len(doc_p.parts) >= 2 and doc_p.parts[0] == "projects" and doc_p.parts[1] == "quaid"
                            and doc_p.suffix.lower() == ".md"
                        )
                        allow_apply = not dry_run
                        if is_root_md:
                            allow_apply = allow_apply and _can_apply_scope(
                                "core_markdown_writes", f"docs staleness update: {doc_path}"
                            )
                        elif not is_quaid_project_md:
                            allow_apply = allow_apply and _can_apply_scope(
                                "project_docs_writes", f"project docs staleness update: {doc_path}"
                            )
                        if allow_apply:
                            ok = update_doc_from_diffs(
                                doc_path, purposes.get(doc_path, ""),
                                info.stale_sources, dry_run=False
                            )
                            if ok:
                                applied_changes["docs_updated"] = \
                                    applied_changes.get("docs_updated", 0) + 1
            except Exception as e:
                print(f"  Staleness check failed: {e}")
                metrics.add_error(f"Docs staleness: {e}")
            metrics.end_task("docs_staleness")
            print(f"Task completed in {metrics.task_duration('docs_staleness'):.2f}s\n")

        # --- Task 1c: Documentation Cleanup (churn-based) ---
        if task in ("docs_cleanup", "all") and _system_enabled_or_skip("docs_cleanup", "Task 1c: Doc Cleanup") and not _skip_if_over_budget("Task 1c: Doc Cleanup", 60):
            print("[Task 1c: Documentation Cleanup]")
            metrics.start_task("docs_cleanup")
            try:
                from docs_updater import check_cleanup_needed, cleanup_doc, get_doc_purposes
                needs_cleanup = check_cleanup_needed()
                if not needs_cleanup:
                    print("  No docs need cleanup")
                else:
                    print(f"  Found {len(needs_cleanup)} doc(s) needing cleanup:")
                    purposes = get_doc_purposes()
                    for doc_path, info in needs_cleanup.items():
                        reason_str = {
                            "updates": f"{info.updates_since_cleanup} updates",
                            "growth": f"{info.growth_ratio:.1f}x growth",
                            "both": f"{info.updates_since_cleanup} updates + {info.growth_ratio:.1f}x growth",
                        }[info.reason]
                        print(f"    {doc_path} ({reason_str})")
                        doc_p = Path(doc_path)
                        is_root_md = len(doc_p.parts) == 1 and doc_p.suffix.lower() == ".md"
                        is_quaid_project_md = (
                            len(doc_p.parts) >= 2 and doc_p.parts[0] == "projects" and doc_p.parts[1] == "quaid"
                            and doc_p.suffix.lower() == ".md"
                        )
                        allow_apply = not dry_run
                        if is_root_md:
                            allow_apply = allow_apply and _can_apply_scope(
                                "core_markdown_writes", f"docs cleanup: {doc_path}"
                            )
                        elif not is_quaid_project_md:
                            allow_apply = allow_apply and _can_apply_scope(
                                "project_docs_writes", f"project docs cleanup: {doc_path}"
                            )
                        if allow_apply:
                            ok = cleanup_doc(doc_path, purposes.get(doc_path, ""), dry_run=False)
                            if ok:
                                applied_changes["docs_cleaned"] = \
                                    applied_changes.get("docs_cleaned", 0) + 1
            except Exception as e:
                print(f"  Cleanup check failed: {e}")
                metrics.add_error(f"Docs cleanup: {e}")
            metrics.end_task("docs_cleanup")
            print(f"Task completed in {metrics.task_duration('docs_cleanup'):.2f}s\n")

        # --- Task 1d-snippets: Soul Snippets Review (Opus API, nightly) ---
        if task in ("snippets", "soul_snippets", "all") and _system_enabled_or_skip("snippets", "Task 1d-snippets: Snippets") and not _skip_if_over_budget("Task 1d-snippets: Snippets", 30):
            print("[Task 1d-snippets: Soul Snippets Review]")
            metrics.start_task("snippets")
            snippets_apply_allowed = _can_apply_scope(
                "core_markdown_writes",
                "snippets fold into root core markdown"
            )
            snippets_dry_run = dry_run or (not snippets_apply_allowed)
            try:
                from soul_snippets import run_soul_snippets_review
                snippets_result = run_soul_snippets_review(dry_run=snippets_dry_run)
                applied_changes["snippets_folded"] = snippets_result.get("folded", 0)
                applied_changes["snippets_rewritten"] = snippets_result.get("rewritten", 0)
                applied_changes["snippets_discarded"] = snippets_result.get("discarded", 0)
            except Exception as e:
                print(f"  Snippets review failed: {e}")
                metrics.add_error(f"Snippets: {e}")
            metrics.end_task("snippets")
            print(f"Task completed in {metrics.task_duration('snippets'):.2f}s\n")

        # --- Task 1d-journal: Journal Distillation (Opus API, weekly) ---
        if task in ("journal", "all") and _system_enabled_or_skip("journal", "Task 1d-journal: Journal") and not _skip_if_over_budget("Task 1d-journal: Journal", 30):
            print("[Task 1d-journal: Journal Distillation]")
            metrics.start_task("journal")
            journal_apply_allowed = _can_apply_scope(
                "core_markdown_writes",
                "journal distillation updates root core markdown"
            )
            journal_dry_run = dry_run or (not journal_apply_allowed)
            try:
                from soul_snippets import run_journal_distillation
                journal_result = run_journal_distillation(dry_run=journal_dry_run, force_distill=force_distill)
                applied_changes["journal_additions"] = journal_result.get("additions", 0)
                applied_changes["journal_edits"] = journal_result.get("edits", 0)
                applied_changes["journal_entries_distilled"] = journal_result.get("total_entries", 0)
            except Exception as e:
                print(f"  Journal distillation failed: {e}")
                metrics.add_error(f"Journal: {e}")
            metrics.end_task("journal")
            print(f"Task completed in {metrics.task_duration('journal'):.2f}s\n")

        # --- Task 6: Extract Edges (DEPRECATED) ---
        # As of Feb 2026, edges are now created at extraction time (index.ts) when facts
        # are first captured. The janitor review (Task 2) handles edge updates for FIX
        # operations. This task is kept for backward compatibility with --task edges,
        # but is skipped by default in --task all runs.
        if task == "edges":
            # Manual run - still execute for backward compatibility
            print("[Task 6: Extract Edges (DEPRECATED - manual run)]")
            _errors_before_edges = len(metrics.errors)
            use_full_scan = not incremental  # --full-scan broadens candidate discovery
            candidates = find_edge_candidates_optimized(graph, metrics, full_scan=use_full_scan)
            print(f"Found {len(candidates)} facts without edges"
                  f"{' (full scan)' if use_full_scan else ' (pattern-filtered)'}\n")

            relations_list = _build_relations_list(graph)
            print(f"  Known relations: {relations_list}\n")

            metrics.start_task("edges_extraction")
            extracted = 0
            skipped = 0
            no_edge = 0

            # Process in token-aware batches via Opus
            edge_builder = TokenBatchBuilder(
                model_tier='deep',
                prompt_overhead_tokens=estimate_tokens(relations_list) + 500,
                tokens_per_item_fn=lambda f: estimate_tokens(f.get("text", "")) + 30,
                max_items=100
            )
            edge_batches = edge_builder.build_batches(candidates)
            total_batches = len(edge_batches)
            consecutive_failures = 0

            for batch_num, batch in enumerate(edge_batches, 1):
                print(f"  Batch {batch_num}/{total_batches} ({len(batch)} facts)...")

                extractions = batch_extract_edges(batch, graph, metrics,
                                                  relations_list=relations_list)

                # Check for total batch failure
                if all(e is None for e in extractions):
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        print(f"  3 consecutive batch failures, stopping edge extraction")
                        metrics.add_error("Edge extraction stopped: 3 consecutive failures")
                        memory_pipeline_ok = False
                        break
                else:
                    consecutive_failures = 0

                for i, extraction in enumerate(extractions):
                    if extraction is None:
                        no_edge += 1
                        continue

                    subj = extraction["subject"]
                    rel = extraction["relation"]
                    obj = extraction["object"]
                    print(f"    {'Would create' if dry_run else 'Creating'}: "
                          f"{subj} --[{rel}]--> {obj}")

                    if not dry_run:
                        owner = batch[i].get("owner_id", _default_owner_id())
                        source_id = _resolve_entity_node(
                            graph, subj, extraction["subject_type"], owner_id=owner)
                        target_id = _resolve_entity_node(
                            graph, obj, extraction["object_type"], owner_id=owner)

                        if source_id and target_id:
                            edge = Edge.create(source_id, target_id, rel)
                            graph.add_edge(edge)
                            extracted += 1
                            # Refresh relations list if a new type was introduced
                            if rel not in relations_list:
                                relations_list = _build_relations_list(graph)
                                print(f"    New relation type: {rel}")
                                # Generate keywords for the new relation type
                                if ensure_keywords_for_relation(rel):
                                    print(f"    Generated keywords for '{rel}'")
                        else:
                            skipped += 1
                            print(f"    Skipped (could not resolve entities)")

            metrics.end_task("edges_extraction")
            applied_changes["edges_created"] = extracted
            print(f"\nEdge extraction: {extracted} created, {no_edge} no-edge, {skipped} skipped")
            print(f"Task completed in {metrics.task_duration('edges_discovery') + metrics.task_duration('edges_extraction'):.2f}s\n")

            # Check if edge extraction added errors
            if len(metrics.errors) > _errors_before_edges:
                memory_pipeline_ok = False

        # Skip Task 6 when running "all" (deprecated)
        elif task == "all":
            print("[Task 6: Extract Edges] SKIPPED — deprecated (edges created at extraction time)\n")

        # --- Task 7: RAG Reindex + Project Discovery (Ollama embeddings) ---
        if task in ("rag", "all") and _system_enabled_or_skip("rag", "Task 7: RAG Reindex") and not _skip_if_over_budget("Task 7: RAG Reindex", 15):
            print("[Task 7: RAG Reindex + Project Discovery]")
            metrics.start_task("rag_reindex")

            try:
                # 7a: Process any queued project events (skip in dry-run — calls Opus)
                if _cfg.projects.enabled and not dry_run:
                    try:
                        from project_updater import process_all_events
                        print("  Processing queued project events...")
                        event_result = process_all_events()
                        applied_changes["project_events_processed"] = event_result.get("processed", 0)
                        if event_result.get("processed", 0) > 0:
                            print(f"    Processed {event_result['processed']} event(s)")
                    except Exception as e:
                        print(f"  Project event processing failed: {e}")
                elif _cfg.projects.enabled and dry_run:
                    print("  Skipping project event processing (dry-run)")

                # 7b: Auto-discover for autoIndex projects
                if _cfg.projects.enabled:
                    try:
                        from docs_registry import DocsRegistry
                        registry = DocsRegistry()
                        total_discovered = 0
                        for proj_name, proj_defn in _cfg.projects.definitions.items():
                            if proj_defn.auto_index:
                                discovered = registry.auto_discover(proj_name)
                                total_discovered += len(discovered)
                        applied_changes["project_files_discovered"] = total_discovered
                        if total_discovered > 0:
                            print(f"    Discovered {total_discovered} new file(s)")

                        # 7c: Sync PROJECT.md External Files -> registry
                        for proj_name in _cfg.projects.definitions:
                            try:
                                registry.sync_external_files(proj_name)
                            except Exception:
                                pass
                    except Exception as e:
                        print(f"  Project auto-discover failed: {e}")

                # 7d: RAG reindex
                rag = DocsRAG()
                # Resolve docs_dir relative to workspace
                docs_dir = str(_workspace() / _cfg.rag.docs_dir)

                print(f"  Reindexing {docs_dir}...")
                result = rag.reindex_all(docs_dir, force=False)

                # Also reindex project directories
                if _cfg.projects.enabled:
                    for proj_name, proj_defn in _cfg.projects.definitions.items():
                        proj_dir = str(_workspace() / proj_defn.home_dir)
                        if Path(proj_dir).exists():
                            print(f"  Reindexing project {proj_name}: {proj_dir}...")
                            proj_result = rag.reindex_all(proj_dir, force=False)
                            result["indexed_files"] = result.get("indexed_files", 0) + proj_result.get("indexed_files", 0)
                            result["total_chunks"] = result.get("total_chunks", 0) + proj_result.get("total_chunks", 0)
                            result["skipped_files"] = result.get("skipped_files", 0) + proj_result.get("skipped_files", 0)

                applied_changes["rag_files_indexed"] = result.get("indexed_files", 0)
                applied_changes["rag_chunks_created"] = result.get("total_chunks", 0)
                applied_changes["rag_files_skipped"] = result.get("skipped_files", 0)

                print(f"\n  RAG Index Updated:")
                print(f"    Total files: {result.get('total_files', 0)}")
                print(f"    Indexed: {result.get('indexed_files', 0)}")
                print(f"    Skipped (unchanged): {result.get('skipped_files', 0)}")
                print(f"    Chunks created: {result.get('total_chunks', 0)}")

            except Exception as e:
                print(f"  RAG reindex failed: {e}")
                metrics.add_error(f"RAG reindex failed: {e}")

            metrics.end_task("rag_reindex")
            print(f"Task completed in {metrics.task_duration('rag_reindex'):.2f}s\n")

        # --- Task 8: Unit Tests (subprocess) ---
        # Only run tests in dev mode: set QUAID_DEV=1 or janitor.run_tests=true in config
        _dev_mode = os.environ.get("QUAID_DEV", "").strip() in ("1", "true", "yes")
        _run_tests_cfg = getattr(_cfg.janitor, "run_tests", False) or _dev_mode
        if task == "tests" or (task == "all" and _run_tests_cfg):
            if not _skip_if_over_budget("Task 8: Tests", 30):
                print("[Task 8: Unit Tests]")
                test_result = run_tests(metrics)
                applied_changes["tests_passed"] = test_result["tests_passed"]
                applied_changes["tests_failed"] = test_result["tests_failed"]
                applied_changes["tests_total"] = test_result["tests_total"]
                print(f"Task completed in {metrics.task_duration('tests'):.2f}s\n")

        # --- Task 9: Audit Table Cleanup ---
        if task in ("cleanup", "all") and not _skip_if_over_budget("Task 9: Cleanup", 5):
            print("[Task 9: Audit Table Cleanup]")
            metrics.start_task("cleanup")
            cleanup_stats = {"recall_log": 0, "dedup_log": 0, "embedding_cache": 0, "health_snapshots": 0, "janitor_metadata": 0, "janitor_runs": 0}
            try:
                cleanup_queries = {
                    "recall_log": "DELETE FROM recall_log WHERE created_at < datetime('now', '-90 days')",
                    "dedup_log": "DELETE FROM dedup_log WHERE review_status != 'unreviewed' AND created_at < datetime('now', '-90 days')",
                    "health_snapshots": "DELETE FROM health_snapshots WHERE created_at < datetime('now', '-180 days')",
                    "embedding_cache": "DELETE FROM embedding_cache WHERE created_at < datetime('now', '-30 days')",
                    "janitor_metadata": "DELETE FROM metadata WHERE key LIKE 'janitor_%' AND updated_at < datetime('now', '-180 days')",
                    "janitor_runs": "DELETE FROM janitor_runs WHERE completed_at < datetime('now', '-180 days')",
                }
                with graph._get_conn() as conn:
                    for table, sql in cleanup_queries.items():
                        if dry_run:
                            # Use COUNT to preview without modifying data
                            count_sql = sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)
                            row = conn.execute(count_sql).fetchone()
                            cleanup_stats[table] = row[0] if row else 0
                        else:
                            cur = conn.execute(sql)
                            cleanup_stats[table] = cur.rowcount
                total = sum(cleanup_stats.values())
                action = "Would remove" if dry_run else "Removed"
                print(f"  {action}: {total} rows total")
                for table, count in cleanup_stats.items():
                    if count > 0:
                        print(f"    {table}: {count}")
                applied_changes["cleanup"] = cleanup_stats
            except Exception as e:
                print(f"  Cleanup error: {e}")
            metrics.end_task("cleanup")
            print(f"Task completed in {metrics.task_duration('cleanup'):.2f}s\n")

        # --- Task 10: Update Check ---
        update_info = None
        if task in ("update_check", "all"):
            print("[Task 10: Update Check]")
            metrics.start_task("update_check")
            try:
                update_info = _check_for_updates()
                if update_info:
                    print(f"  ⚠️  UPDATE AVAILABLE: v{update_info['current']} → v{update_info['latest']}")
                    from lib.adapter import get_adapter as _ga
                    print(f"  ⚠️  Update: curl -fsSL {_ga().get_install_url()} | bash")
                    print(f"  ⚠️  Release: {update_info['url']}")
                    applied_changes["update_available"] = update_info
                else:
                    print("  Up to date")
            except Exception as e:
                print(f"  Update check error: {e}")
            metrics.end_task("update_check")
            print(f"Task completed in {metrics.task_duration('update_check'):.2f}s\n")

        # --- Health Snapshot ---
        if task == "all":
            try:
                health = graph.get_health_metrics()
                total = health["total_nodes"]
                with_emb_str = health.get("embedding_coverage", "0/0")
                with_emb = int(with_emb_str.split("/")[0]) if "/" in str(with_emb_str) else 0
                emb_pct = (with_emb / total * 100) if total > 0 else 0.0

                # Avg confidence across all statuses
                conf_stats = health.get("confidence_by_status", {})
                all_counts = sum(s.get("count", 0) for s in conf_stats.values())
                avg_conf = (sum(s.get("avg_confidence", 0) * s.get("count", 0) for s in conf_stats.values()) / all_counts) if all_counts > 0 else 0.0

                # Confidence distribution buckets
                conf_dist = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, "0.9-1.0": 0}
                with graph._get_conn() as conn:
                    for row in conn.execute("SELECT confidence FROM nodes").fetchall():
                        c = row["confidence"]
                        if c < 0.3: conf_dist["0.0-0.3"] += 1
                        elif c < 0.5: conf_dist["0.3-0.5"] += 1
                        elif c < 0.7: conf_dist["0.5-0.7"] += 1
                        elif c < 0.9: conf_dist["0.7-0.9"] += 1
                        else: conf_dist["0.9-1.0"] += 1

                    conn.execute("""
                        INSERT INTO health_snapshots
                            (total_nodes, total_edges, avg_confidence, nodes_by_status,
                             confidence_distribution, staleness_distribution,
                             orphan_count, embedding_coverage)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        total,
                        health["total_edges"],
                        round(avg_conf, 4),
                        json.dumps({k: v.get("count", 0) for k, v in conf_stats.items()}),
                        json.dumps(conf_dist),
                        json.dumps(health.get("staleness_distribution", {})),
                        health.get("orphan_nodes", 0),
                        round(emb_pct, 1),
                    ))
                print(f"[Health Snapshot] Recorded: {total} nodes, {health['total_edges']} edges, avg conf {avg_conf:.3f}")
            except Exception as e:
                print(f"[Health Snapshot] Failed: {e}")

        # --- Final Step: Graduate approved → active ---
        # After all processing (review, dedup, contradiction, edges), promote
        # approved facts to active so they're never reprocessed by the pipeline.
        # CRITICAL: Only graduate if memory pipeline completed without errors.
        if task == "all" and not dry_run and _cfg.systems.memory:
            if not memory_pipeline_ok:
                error_count = len(metrics.errors)
                print(f"[Final: Graduate approved → active] BLOCKED")
                print(f"  {error_count} error(s) occurred during memory pipeline.")
                print(f"  Facts remain as approved/pending — will be reprocessed next run.\n")
            else:
                print("[Final: Graduate approved → active]")
                with graph._get_conn() as conn:
                    cursor = conn.execute(
                        "UPDATE nodes SET status = 'active' WHERE status = 'approved'"
                    )
                    graduated = cursor.rowcount
                applied_changes["graduated_to_active"] = graduated
                print(f"  Graduated {graduated} memories from approved → active\n")

    except Exception as e:
        metrics.add_error(f"Critical error in task {task}: {str(e)}")
        memory_pipeline_ok = False
        print(f"ERROR: {e}")
    
    # Generate comprehensive report
    final_metrics = metrics.summary()
    
    print(f"{'='*80}")
    print("JANITOR PERFORMANCE REPORT")
    print(f"{'='*80}")
    print(f"Total execution time: {final_metrics['total_duration_seconds']}s")
    print(f"LLM calls made: {final_metrics['llm_calls']}")
    print(f"LLM time total: {final_metrics['llm_time_seconds']}s")
    print(f"Errors encountered: {final_metrics['errors']}")
    
    if final_metrics['task_durations']:
        print(f"\nTask breakdown:")
        for task_name, duration in final_metrics['task_durations'].items():
            print(f"  {task_name}: {duration}s")
    
    print(f"\nChanges applied:")
    for change_type, count in applied_changes.items():
        print(f"  {change_type}: {count}")
    
    # Cost report
    usage = get_token_usage()
    cost = estimate_cost()
    if usage["api_calls"] > 0:
        print(f"\nAPI usage:")
        print(f"  Calls: {usage['api_calls']}")
        print(f"  Input tokens: {usage['input_tokens']:,}")
        print(f"  Output tokens: {usage['output_tokens']:,}")
        print(f"  Estimated cost: ${cost:.4f}")

    if _budget_skipped:
        print(f"\nSkipped due to time budget ({time_budget}s):")
        for label in _budget_skipped:
            print(f"  {label}")

    if final_metrics['errors'] > 0:
        print(f"\nRecent errors:")
        for error in final_metrics['error_details']:
            print(f"  {error['time']}: {error['error']}")

    print(f"{'='*80}")
    
    # Performance warning (adjusted for time-bounded execution)
    if final_metrics['total_duration_seconds'] > MAX_EXECUTION_TIME:  # Check CRITICAL first
        print(f"\nCRITICAL: Janitor exceeded time limit ({final_metrics['total_duration_seconds']/60:.1f}min > {MAX_EXECUTION_TIME/60:.1f}min)")
        print("Time-bounded execution may have missed some contradictions!")
    elif final_metrics['total_duration_seconds'] > 600:  # 10 minutes
        print(f"\nWARNING: Janitor took {final_metrics['total_duration_seconds']/60:.1f}min (>10min)")
        print("Consider running with --incremental or checking for performance issues")
    
    # Coverage warning for contradiction checks
    if 'contradictions_coverage' in applied_changes:
        coverage = applied_changes['contradictions_coverage']
        if coverage < 1.0:
            print(f"\n📊 INFO: Contradiction check coverage: {coverage*100:.1f}% due to time constraints")
    
    # Log completion with metrics
    success = final_metrics['errors'] == 0
    janitor_logger.info(
        "janitor_complete",
        task=task,
        success=success,
        duration_seconds=final_metrics['total_duration_seconds'],
        llm_calls=final_metrics['llm_calls'],
        errors=final_metrics['errors'],
        **{k: v for k, v in applied_changes.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
    )

    # Record run in janitor_runs table (enables incremental mode)
    if not dry_run:
        try:
            actions = sum(v for v in applied_changes.values() if isinstance(v, (int, float)) and v > 0)
            with graph._get_conn() as conn:
                conn.execute("""
                    INSERT INTO janitor_runs (task_name, started_at, completed_at, memories_processed, actions_taken, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (task, datetime.fromtimestamp(metrics.start_time).isoformat(), datetime.now().isoformat(),
                      applied_changes.get("memories_reviewed", 0) + applied_changes.get("duplicates_merged", 0),
                      actions, "completed" if success else "failed"))
        except Exception as e:
            print(f"  Warning: Failed to record janitor run: {e}")

    # Queue user notifications for adapter-delayed delivery (only for full runs, not dry-run)
    if task == "all" and not dry_run:
        try:
            duration = final_metrics.get("total_duration_seconds", 0)
            duration_label = f"{duration/60:.1f}min" if duration >= 60 else f"{duration:.0f}s"
            summary_lines = [
                "[Quaid] 🧹 Nightly Janitor Complete",
                f"Duration: {duration_label}",
                f"LLM calls: {final_metrics.get('llm_calls', 0)}",
                f"Errors: {final_metrics.get('errors', 0)}",
                "",
                "Changes:",
                f"- reviewed: {applied_changes.get('memories_reviewed', 0)}",
                f"- merged: {applied_changes.get('duplicates_merged', 0)}",
                f"- contradictions found: {applied_changes.get('contradictions_found', 0)}",
                f"- contradictions resolved: {applied_changes.get('contradictions_resolved', 0)}",
                f"- decayed: {applied_changes.get('memories_decayed', 0)}",
                f"- deleted_by_decay: {applied_changes.get('memories_deleted_by_decay', 0)}",
            ]
            # Always include full contradiction decision details.
            contradiction_findings = applied_changes.get("contradiction_findings") or []
            contradiction_decisions = applied_changes.get("contradiction_decisions") or []
            if contradiction_findings or contradiction_decisions:
                summary_lines.append("")
                summary_lines.append("Contradiction Details (full):")
                for f in contradiction_findings[:10]:
                    if isinstance(f, dict):
                        summary_lines.append(f"- Found: \"{f.get('text_a', '')}\" ↔ \"{f.get('text_b', '')}\"")
                        summary_lines.append(f"  Reason: {f.get('reason', '')}")
                for d in contradiction_decisions[:15]:
                    if isinstance(d, dict):
                        summary_lines.append(f"- Decision: {d.get('action', 'UNKNOWN')}")
                        summary_lines.append(f"  A: {d.get('text_a', '')}")
                        summary_lines.append(f"  B: {d.get('text_b', '')}")
                        summary_lines.append(f"  Why: {d.get('reason', '')}")

            if _cfg.notifications.should_notify("janitor", detail="summary"):
                _queue_delayed_notification("\n".join(summary_lines), kind="janitor_summary", priority="normal")
                print("[notify] Queued janitor summary for delayed adapter delivery")
            else:
                print("[notify] Janitor summary suppressed by notifications config")

            # Queue daily memory digest if enabled.
            if _cfg.notifications.should_notify("janitor", detail="summary"):
                today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                with graph._get_conn() as conn:
                    rows = conn.execute("""
                        SELECT name as text
                        FROM nodes
                        WHERE type = 'Fact'
                          AND created_at >= ?
                          AND status IN ('pending', 'approved', 'active')
                        ORDER BY created_at DESC
                        LIMIT 25
                    """, (today_start,)).fetchall()
                today_memories = [str(r["text"]) for r in rows]
                if today_memories:
                    digest_lines = ["[Quaid] 📚 Today's New Memories", f"Count: {len(today_memories)}", ""]
                    for text in today_memories[:10]:
                        digest_lines.append(f"- {text}")
                    if len(today_memories) > 10:
                        digest_lines.append(f"- ...and {len(today_memories)-10} more")
                    _queue_delayed_notification("\n".join(digest_lines), kind="janitor_daily_digest", priority="low")
                    print(f"[notify] Queued daily digest ({len(today_memories)} memories)")
                else:
                    print("[notify] No new memories today, skipping daily digest")

        except Exception as e:
            print(f"[notify] Failed to queue delayed notifications: {e}")

    # Return metrics for programmatic use
    # WAL checkpoint at end of run to reclaim WAL file space
    if task == "all" and not dry_run:
        try:
            with graph._get_conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass  # Checkpoint is best-effort

    return {
        "success": success,
        "memory_pipeline_ok": memory_pipeline_ok,
        "metrics": final_metrics,
        "applied_changes": applied_changes
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Memory Janitor (Optimized)")
    parser.add_argument("--task", choices=["embeddings", "workspace", "docs_staleness", "docs_cleanup", "snippets", "soul_snippets", "journal", "review", "dedup_review", "duplicates", "contradictions", "decay", "decay_review", "edges", "rag", "tests", "cleanup", "update_check", "all"],
                        default="all", help="Task to run")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--approve", action="store_true",
                        help="Confirm apply when janitor.applyMode is set to 'ask'")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no changes (default)")
    parser.add_argument("--full-scan", action="store_true", help="Force full scan instead of incremental")
    parser.add_argument("--force-distill", action="store_true",
                        help="Force journal distillation regardless of interval")
    parser.add_argument("--time-budget", type=int, default=0,
                        help="Wall-clock budget in seconds (0 = unlimited). Tasks are skipped when time runs low.")

    args = parser.parse_args()

    # dry_run is derived from apply flags and janitor apply policy.
    dry_run, apply_policy_warning = _resolve_apply_mode(args.apply, args.approve)
    if apply_policy_warning:
        print(f"[policy] {apply_policy_warning}")
    incremental = not args.full_scan

    result = run_task_optimized(args.task, dry_run=dry_run, incremental=incremental,
                                time_budget=args.time_budget,
                                force_distill=args.force_distill,
                                user_approved=args.approve)
    
    # Write stats to file for dashboard consumption
    stats_file = _logs_dir() / "janitor-stats.json"
    stats_file.parent.mkdir(parents=True, exist_ok=True)
    usage = get_token_usage()
    stats_data = {
        "last_run": datetime.now().isoformat(),
        "task": args.task,
        "dry_run": dry_run,
        "success": result["success"],
        "applied_changes": result["applied_changes"],
        "metrics": result["metrics"],
        "api_usage": {
            "calls": usage["api_calls"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "estimated_cost_usd": estimate_cost(),
        }
    }
    with open(stats_file, "w") as f:
        json.dump(stats_data, f, indent=2)
    print(f"\n📊 Stats written to {stats_file}")
    
    # Exit with error code if janitor failed
    exit(0 if result["success"] else 1)
