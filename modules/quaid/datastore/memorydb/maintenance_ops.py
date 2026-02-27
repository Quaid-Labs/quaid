"""Datastore-owned memory maintenance intelligence routines."""

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

# Import datastore internals directly for janitor-only maintenance logic.
from datastore.memorydb.memory_graph import (
    get_graph,
    MemoryGraph,
    Node,
    Edge,
    store as store_memory,
    store_contradiction,
    get_pending_contradictions,
    resolve_contradiction,
    mark_contradiction_false_positive,
    soft_delete,
    get_recent_dedup_rejections,
    resolve_dedup_review,
    queue_for_decay_review,
    get_pending_decay_reviews,
    resolve_decay_review,
    ensure_keywords_for_relation,
    get_edge_keywords,
    delete_edges_by_source_fact,
    create_edge,
    content_hash,
    hard_delete_node,
    store_edge_keywords,
)
from lib.config import get_db_path
from lib.tokens import extract_key_tokens, estimate_tokens
from lib.archive import archive_node as _archive_node
from config import get_config
from lib.llm_clients import (
    call_fast_reasoning,
    call_deep_reasoning,
    call_llm,
    parse_json_response,
    reset_token_usage,
    get_token_usage,
    estimate_cost,
    set_token_budget,
    reset_token_budget,
    is_token_budget_exhausted,
    DEEP_REASONING_TIMEOUT,
    FAST_REASONING_TIMEOUT,
)
from lib.runtime_context import (
    get_repo_slug,
    get_install_url,
    get_llm_provider,
)
from lib.worker_pool import run_callables
from lib.fail_policy import is_fail_hard_enabled
from core.runtime.logger import janitor_logger

logger = logging.getLogger(__name__)

_ADAPTIVE_LLM_WORKERS_LOCK = threading.Lock()
_ADAPTIVE_LLM_WORKERS: Dict[str, int] = {}

# Configuration — resolved from config system
DB_PATH = get_db_path()

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


def _effective_llm_timeout(requested_seconds: Optional[float], default_seconds: float) -> float:
    """Bound per-call timeout by remaining budget when provided."""
    if requested_seconds is None:
        return float(default_seconds)
    try:
        requested = float(requested_seconds)
    except Exception:
        return float(default_seconds)
    if requested <= 0:
        return float(default_seconds)
    return max(5.0, min(float(default_seconds), requested))


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


def _default_owner_id() -> str:
    """Get the default owner ID from config."""
    try:
        return _cfg.users.default_owner
    except Exception as exc:
        if is_fail_hard_enabled():
            raise RuntimeError("Unable to resolve maintenance default owner from config") from exc
        logger.warning("maintenance default owner fallback to 'default': %s", exc)
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
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).lower():
                    raise
            conn.execute("DELETE FROM nodes WHERE id = ?", (oid,))

    return result


def _prompt_hash(text: str) -> str:
    """SHA256 hash of prompt text, first 12 chars. For tracking which prompt version produced a decision."""
    return hashlib.sha256(text.encode()).hexdigest()[:12]

# Performance settings — fixed sizes are now safety limits only;
# actual batch sizes are computed by TokenBatchBuilder based on context window.
LR_BATCH_SIZE = 100  # Safety cap (pairs per fast-reasoning call)
MAX_CONSECUTIVE_FAILURES = 3  # Stop batching after N consecutive failures
LLM_TIMEOUT = 30  # Timeout for individual LLM calls
MAX_EXECUTION_TIME = _cfg.janitor.task_timeout_minutes * 60  # From config (seconds)
MAX_PARALLEL_WORKERS = 8


def is_benchmark_mode() -> bool:
    """True when janitor is running under benchmark harness semantics."""
    return str(os.environ.get("QUAID_BENCHMARK_MODE", "")).strip().lower() in {
        "1", "true", "yes", "on"
    }


def _is_benchmark_mode() -> bool:
    """Backward-compatible alias for older imports."""
    return is_benchmark_mode()


def _diag_logging_enabled() -> bool:
    """Enable verbose janitor diagnostics only in benchmark/debug lanes."""
    debug_flag = str(os.environ.get("QUAID_JANITOR_DEBUG_DIAGNOSTICS", "")).strip().lower()
    return _is_benchmark_mode() or debug_flag in {"1", "true", "yes", "on"}


def _diag_truncate(value: Any, limit: int = 220) -> str:
    txt = str(value or "")
    if len(txt) <= limit:
        return txt
    return txt[:limit] + "..."


def _diag_log_decision(event: str, **payload: Any) -> None:
    """Best-effort structured logging for per-fact janitor decisions."""
    if not _diag_logging_enabled():
        return
    safe_payload = {}
    for k, v in payload.items():
        if isinstance(v, str):
            safe_payload[k] = _diag_truncate(v)
        elif isinstance(v, list):
            safe_payload[k] = [_diag_truncate(x, 120) for x in v[:20]]
        else:
            safe_payload[k] = v
    try:
        janitor_logger.info(event, **safe_payload)
    except Exception:
        pass


def _record_llm_batch_issue(metrics: "JanitorMetrics", message: str) -> None:
    """Record transient LLM batch issues.

    In benchmark mode, these are treated as warnings so one bad JSON/provider
    response does not invalidate the whole run.
    """
    if is_benchmark_mode():
        print(f"    WARN: {message} (non-fatal in benchmark mode)")
        return
    metrics.add_error(message)


def _lr_batch_timeout() -> int:
    """Timeout for batched fast-reasoning calls.

    Benchmark lanes can be slower under vLLM; default higher there unless
    explicitly overridden by env.
    """
    env_val = str(os.environ.get("QUAID_LR_BATCH_TIMEOUT", "")).strip()
    if env_val.isdigit():
        return int(env_val)
    return 300 if _is_benchmark_mode() else 120


def _janitor_review_model() -> str:
    """Review model override for janitor LLM maintenance tasks."""
    env_model = str(os.environ.get("QUAID_JANITOR_REVIEW_MODEL", "")).strip()
    if env_model:
        return env_model
    return _cfg.janitor.opus_review.model


def _parallel_key(task_name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", (task_name or "").strip().upper())
    return clean.strip("_")


def _timeout_like_error(err: Any) -> bool:
    """Classify timeout-shaped errors used for adaptive parallel backoff."""
    if isinstance(err, TimeoutError):
        return True
    msg = str(err or "").strip().lower()
    if not msg:
        return False
    return (
        "timed out" in msg
        or "timeout" in msg
        or "deadline exceeded" in msg
        or "readtimeout" in msg
        or "parallel call timed out" in msg
    )


def _resolve_adaptive_workers(task_name: str, configured_workers: int) -> int:
    """Return effective worker count after adaptive congestion backoff."""
    task_key = _parallel_key(task_name)
    configured = max(1, int(configured_workers))
    with _ADAPTIVE_LLM_WORKERS_LOCK:
        current = int(_ADAPTIVE_LLM_WORKERS.get(task_key, configured) or configured)
        if current < 1:
            current = 1
        if current > configured:
            current = configured
        _ADAPTIVE_LLM_WORKERS[task_key] = current
        return current


def _record_adaptive_workers(
    task_name: str,
    configured_workers: int,
    timeout_events: int,
    had_any_errors: bool,
    had_any_success: bool,
) -> None:
    """Update adaptive worker target based on timeout/success signal."""
    task_key = _parallel_key(task_name)
    configured = max(1, int(configured_workers))
    with _ADAPTIVE_LLM_WORKERS_LOCK:
        current = int(_ADAPTIVE_LLM_WORKERS.get(task_key, configured) or configured)
        current = max(1, min(current, configured))
        next_workers = current
        if timeout_events > 0:
            next_workers = max(1, current - 1)
            if next_workers < current:
                print(
                    f"    [parallel] timeout congestion detected task={task_name}; "
                    f"workers {current} -> {next_workers}"
                )
        elif had_any_success and not had_any_errors and current < configured:
            next_workers = min(configured, current + 1)
            if next_workers > current:
                print(
                    f"    [parallel] timeout-free recovery task={task_name}; "
                    f"workers {current} -> {next_workers}"
                )
        _ADAPTIVE_LLM_WORKERS[task_key] = next_workers


def _reset_adaptive_llm_workers() -> None:
    """Test helper to clear adaptive worker state between runs."""
    with _ADAPTIVE_LLM_WORKERS_LOCK:
        _ADAPTIVE_LLM_WORKERS.clear()


def _llm_parallel_workers(task_name: str) -> int:
    """Resolve LLM batch parallelism for a task from config."""
    core_cfg = getattr(_cfg, "core", None)
    parallel_cfg = getattr(core_cfg, "parallel", None) if core_cfg else None
    if parallel_cfg is None:
        raise RuntimeError("Missing required config: core.parallel")
    if not getattr(parallel_cfg, "enabled", True):
        return 1

    default_workers = int(getattr(parallel_cfg, "llm_workers", 4) or 4)
    task_workers = getattr(parallel_cfg, "task_workers", {}) or {}
    override = None
    if isinstance(task_workers, dict):
        candidate_keys = [
            str(task_name),
            str(task_name).lower(),
            _parallel_key(task_name),
            _parallel_key(task_name).lower(),
        ]
        for key in candidate_keys:
            if key in task_workers:
                override = task_workers.get(key)
                break

    raw = override if override is not None else default_workers
    try:
        value = int(raw)
    except Exception:
        value = default_workers
    return max(1, min(value, MAX_PARALLEL_WORKERS))


def _run_llm_batches_parallel(
    batches: List[list],
    task_name: str,
    runner,
    overall_timeout_seconds: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Run batch LLM calls with bounded parallelism; preserve output order."""
    if not batches:
        return []
    configured_workers = _llm_parallel_workers(task_name)
    adaptive_workers = _resolve_adaptive_workers(task_name, configured_workers)
    workers = min(adaptive_workers, len(batches))
    if workers <= 1:
        out = []
        timeout_events = 0
        had_any_errors = False
        had_any_success = False
        for idx, batch in enumerate(batches, 1):
            try:
                row = runner(idx, batch)
                if isinstance(row, dict) and row.get("error"):
                    had_any_errors = True
                    if _timeout_like_error(row.get("error")):
                        timeout_events += 1
                else:
                    had_any_success = True
                out.append(row)
            except Exception as exc:
                had_any_errors = True
                if _timeout_like_error(exc):
                    timeout_events += 1
                out.append(
                    {
                        "batch_num": idx,
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                        "response": None,
                        "duration": 0.0,
                    }
                )
        _record_adaptive_workers(
            task_name,
            configured_workers,
            timeout_events=timeout_events,
            had_any_errors=had_any_errors,
            had_any_success=had_any_success,
        )
        return out
    out: List[Optional[Dict[str, Any]]] = [None] * len(batches)
    calls = [
        (lambda batch_num, batch_data: (lambda: runner(batch_num, batch_data)))(idx, batch)
        for idx, batch in enumerate(batches, 1)
    ]
    results = run_callables(
        calls,
        max_workers=workers,
        pool_name="janitor-llm-batches",
        timeout_seconds=overall_timeout_seconds,
        return_exceptions=True,
    )
    timeout_events = 0
    had_any_errors = False
    had_any_success = False
    for idx, item in enumerate(results, 1):
        if isinstance(item, Exception):
            had_any_errors = True
            if _timeout_like_error(item):
                timeout_events += 1
            out[idx - 1] = {
                "batch_num": idx,
                "error": str(item),
                "error_type": item.__class__.__name__,
                "response": None,
                "duration": 0.0,
            }
        else:
            if isinstance(item, dict) and item.get("error"):
                had_any_errors = True
                if _timeout_like_error(item.get("error")):
                    timeout_events += 1
            else:
                had_any_success = True
            out[idx - 1] = item
    _record_adaptive_workers(
        task_name,
        configured_workers,
        timeout_events=timeout_events,
        had_any_errors=had_any_errors,
        had_any_success=had_any_success,
    )
    return [item if item is not None else {"batch_num": i + 1, "error": "missing-result"} for i, item in enumerate(out)]


class JanitorMetrics:
    """Track timing and performance metrics."""
    
    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.task_times = {}
        self.task_meta = {}
        self.current_task = None
        self._thread_task: Dict[int, str] = {}
        self._max_thread_task_entries = max(1, int(os.environ.get("JANITOR_METRICS_MAX_THREAD_TASKS", "1024") or 1024))
        self.llm_calls = 0
        self.llm_time = 0.0
        self.errors = []
        self.warnings = []
        self._max_event_entries = max(1, int(os.environ.get("JANITOR_METRICS_MAX_EVENTS", "500") or 500))

    def _prune_thread_tasks_locked(self) -> None:
        """Drop stale thread->task bindings to avoid unbounded growth."""
        if not self._thread_task:
            return
        alive = {t.ident for t in threading.enumerate() if t.ident is not None}
        for tid in list(self._thread_task.keys()):
            if tid not in alive:
                self._thread_task.pop(tid, None)
        if len(self._thread_task) > self._max_thread_task_entries:
            for tid in list(self._thread_task.keys())[:-self._max_thread_task_entries]:
                self._thread_task.pop(tid, None)
    
    def start_task(self, task_name: str):
        with self._lock:
            self._prune_thread_tasks_locked()
            self.task_times[task_name] = {"start": time.time(), "end": None}
            self.task_meta[task_name] = {
                "llm_calls": 0,
                "llm_time_seconds": 0.0,
                "errors": 0,
                "warnings": 0,
            }
            self.current_task = task_name
            self._thread_task[threading.get_ident()] = task_name
    
    def end_task(self, task_name: str):
        with self._lock:
            if task_name in self.task_times:
                self.task_times[task_name]["end"] = time.time()
            if self.current_task == task_name:
                self.current_task = None
            tid = threading.get_ident()
            if self._thread_task.get(tid) == task_name:
                self._thread_task.pop(tid, None)
    
    def task_duration(self, task_name: str) -> float:
        if task_name in self.task_times and self.task_times[task_name]["end"]:
            return self.task_times[task_name]["end"] - self.task_times[task_name]["start"]
        return 0.0
    
    def total_duration(self) -> float:
        return time.time() - self.start_time
    
    def add_llm_call(self, duration: float):
        with self._lock:
            self._prune_thread_tasks_locked()
            self.llm_calls += 1
            self.llm_time += duration
            task = self._thread_task.get(threading.get_ident()) or self.current_task
            if task and task in self.task_meta:
                self.task_meta[task]["llm_calls"] += 1
                self.task_meta[task]["llm_time_seconds"] += float(duration or 0.0)
    
    def add_error(self, error: str):
        with self._lock:
            self._prune_thread_tasks_locked()
            self.errors.append({"time": datetime.now().isoformat(), "error": error})
            if len(self.errors) > self._max_event_entries:
                self.errors = self.errors[-self._max_event_entries:]
            task = self._thread_task.get(threading.get_ident()) or self.current_task
            if task and task in self.task_meta:
                self.task_meta[task]["errors"] += 1

    def add_warning(self, warning: str):
        with self._lock:
            self._prune_thread_tasks_locked()
            self.warnings.append({"time": datetime.now().isoformat(), "warning": warning})
            if len(self.warnings) > self._max_event_entries:
                self.warnings = self.warnings[-self._max_event_entries:]
            task = self._thread_task.get(threading.get_ident()) or self.current_task
            if task and task in self.task_meta:
                self.task_meta[task]["warnings"] += 1

    @property
    def has_errors(self) -> bool:
        with self._lock:
            return len(self.errors) > 0

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            task_times = dict(self.task_times)
            task_meta = {k: dict(v) for k, v in self.task_meta.items()}
            llm_calls = self.llm_calls
            llm_time = self.llm_time
            errors = list(self.errors)
            warnings = list(self.warnings)

        task_durations = {
            name: round(
                (
                    (meta.get("end") or time.time()) - float(meta.get("start") or 0.0)
                ) if meta.get("start") else 0.0,
                2,
            )
            for name, meta in task_times.items()
        }
        task_metrics = {}
        for name, meta in task_meta.items():
            task_metrics[name] = {
                "duration_seconds": task_durations.get(name, 0.0),
                "llm_calls": int(meta.get("llm_calls", 0) or 0),
                "llm_time_seconds": round(float(meta.get("llm_time_seconds", 0.0) or 0.0), 2),
                "errors": int(meta.get("errors", 0) or 0),
                "warnings": int(meta.get("warnings", 0) or 0),
            }
        return {
            "total_duration_seconds": round(self.total_duration(), 2),
            "task_durations": task_durations,
            "task_metrics": task_metrics,
            "llm_calls": llm_calls,
            "llm_time_seconds": round(llm_time, 2),
            "errors": len(errors),
            "error_details": errors[-5:] if errors else [],  # Last 5 errors
            "warnings": len(warnings),
            "warning_details": warnings[-10:] if warnings else [],
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
                status TEXT DEFAULT 'running', -- running, completed, failed
                skipped_tasks_json TEXT,
                carryover_json TEXT,
                stage_budget_json TEXT,
                checkpoint_path TEXT,
                task_summary_json TEXT
            )
        """)
        existing_cols = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(janitor_runs)").fetchall()
        }
        required = {
            "skipped_tasks_json": "TEXT",
            "carryover_json": "TEXT",
            "stage_budget_json": "TEXT",
            "checkpoint_path": "TEXT",
            "task_summary_json": "TEXT",
        }
        for col, col_type in required.items():
            if col in existing_cols:
                continue
            conn.execute(f"ALTER TABLE janitor_runs ADD COLUMN {col} {col_type}")


def get_update_check_cache(graph: MemoryGraph, max_age_hours: int = 24) -> Optional[Dict[str, Any]]:
    """Return cached update-check payload when still fresh."""
    with graph._get_conn() as conn:
        row = conn.execute(
            "SELECT value, updated_at FROM janitor_metadata WHERE key = 'update_check'"
        ).fetchone()
    if not row:
        return None
    try:
        updated_at = datetime.fromisoformat(str(row["updated_at"]))
    except (ValueError, TypeError, KeyError):
        return None
    if datetime.now() - updated_at >= timedelta(hours=max_age_hours):
        return None
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError, KeyError):
        return None


def write_update_check_cache(graph: MemoryGraph, payload: Dict[str, Any]) -> None:
    """Persist update-check payload in datastore metadata."""
    with graph._get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO janitor_metadata (key, value, updated_at) VALUES (?, ?, datetime('now'))",
            ("update_check", json.dumps(payload)),
        )


def record_janitor_run(
    graph: MemoryGraph,
    task_name: str,
    started_at_iso: str,
    completed_at_iso: str,
    memories_processed: int,
    actions_taken: int,
    status: str,
    skipped_tasks: Optional[List[str]] = None,
    carryover: Optional[Dict[str, Any]] = None,
    stage_budget: Optional[Dict[str, Any]] = None,
    checkpoint_path: Optional[str] = None,
    task_summary: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist one janitor run row."""
    with graph._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO janitor_runs (
                task_name, started_at, completed_at, memories_processed, actions_taken, status,
                skipped_tasks_json, carryover_json, stage_budget_json, checkpoint_path, task_summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_name,
                started_at_iso,
                completed_at_iso,
                memories_processed,
                actions_taken,
                status,
                json.dumps(skipped_tasks or []),
                json.dumps(carryover or {}),
                json.dumps(stage_budget or {}),
                checkpoint_path or "",
                json.dumps(task_summary or {}),
            ),
        )


def count_nodes_by_status(graph: MemoryGraph, statuses: List[str]) -> Dict[str, int]:
    """Return node counts keyed by status value."""
    counts: Dict[str, int] = {}
    if not statuses:
        return counts
    with graph._get_conn() as conn:
        for status in statuses:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM nodes WHERE status = ?",
                (status,),
            ).fetchone()
            counts[status] = int((row["cnt"] if row and "cnt" in row.keys() else 0) or 0)
    return counts


def list_recent_fact_texts(graph: MemoryGraph, since_iso: str, limit: int = 25) -> List[str]:
    """Return recent fact texts for notification payloads."""
    with graph._get_conn() as conn:
        rows = conn.execute(
            """
            SELECT name AS text
            FROM nodes
            WHERE type = 'Fact'
              AND created_at >= ?
              AND status IN ('pending', 'approved', 'active')
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (since_iso, limit),
        ).fetchall()
    return [str(r["text"]) for r in rows]


def graduate_approved_to_active(graph: MemoryGraph) -> int:
    """Promote approved nodes to active. Returns affected row count."""
    with graph._get_conn() as conn:
        cursor = conn.execute(
            "UPDATE nodes SET status = 'active' WHERE status = 'approved'"
        )
        return int(cursor.rowcount or 0)


def checkpoint_wal(graph: MemoryGraph) -> None:
    """Run best-effort WAL checkpoint."""
    with graph._get_conn() as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def record_health_snapshot(graph: MemoryGraph, health: Dict[str, Any]) -> Dict[str, Any]:
    """Persist one health snapshot and return computed summary details."""
    total = int(health.get("total_nodes", 0) or 0)
    conf_stats = health.get("confidence_by_status", {}) or {}
    all_counts = sum(int(s.get("count", 0) or 0) for s in conf_stats.values())
    avg_conf = (
        sum(float(s.get("avg_confidence", 0) or 0) * int(s.get("count", 0) or 0) for s in conf_stats.values()) / all_counts
        if all_counts > 0
        else 0.0
    )
    with_emb_str = str(health.get("embedding_coverage", "0/0"))
    with_emb = int(with_emb_str.split("/")[0]) if "/" in with_emb_str else 0
    emb_pct = (with_emb / total * 100) if total > 0 else 0.0

    conf_dist = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, "0.9-1.0": 0}
    with graph._get_conn() as conn:
        for row in conn.execute("SELECT confidence FROM nodes").fetchall():
            c = float(row["confidence"] or 0)
            if c < 0.3:
                conf_dist["0.0-0.3"] += 1
            elif c < 0.5:
                conf_dist["0.3-0.5"] += 1
            elif c < 0.7:
                conf_dist["0.5-0.7"] += 1
            elif c < 0.9:
                conf_dist["0.7-0.9"] += 1
            else:
                conf_dist["0.9-1.0"] += 1

        conn.execute(
            """
            INSERT INTO health_snapshots
                (total_nodes, total_edges, avg_confidence, nodes_by_status,
                 confidence_distribution, staleness_distribution,
                 orphan_count, embedding_coverage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                total,
                int(health.get("total_edges", 0) or 0),
                round(avg_conf, 4),
                json.dumps({k: int(v.get("count", 0) or 0) for k, v in conf_stats.items()}),
                json.dumps(conf_dist),
                json.dumps(health.get("staleness_distribution", {})),
                int(health.get("orphan_nodes", 0) or 0),
                round(emb_pct, 1),
            ),
        )

    return {
        "total": total,
        "avg_confidence": avg_conf,
        "total_edges": int(health.get("total_edges", 0) or 0),
    }


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


def get_last_successful_janitor_completed_at(graph: MemoryGraph) -> Optional[str]:
    """Return ISO timestamp of most recent successful janitor run, if any."""
    try:
        with graph._get_conn() as conn:
            row = conn.execute(
                "SELECT MAX(completed_at) AS completed_at FROM janitor_runs WHERE status = 'completed'"
            ).fetchone()
        if row and row["completed_at"]:
            return str(row["completed_at"])
    except Exception as exc:
        logger.warning("Failed to read last successful janitor completion time: %s", exc)
        if is_fail_hard_enabled():
            raise RuntimeError("Failed to read janitor completion status from datastore") from exc
        return None
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
    tokens = extract_key_tokens(text)
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
        except Exception as exc:
            if is_fail_hard_enabled():
                raise RuntimeError(
                    "recall_candidates FTS query failed while fail-hard mode is enabled"
                ) from exc
            logger.warning("recall_candidates FTS query failed, falling back to LIKE: %s", exc)
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
                    except Exception as exc:
                        if is_fail_hard_enabled():
                            raise RuntimeError(
                                f"vec_nodes update failed during backfill for node {node_id}"
                            ) from exc
                        logger.warning("vec_nodes update skipped during backfill for node %s: %s", node_id, exc)
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


def recall_similar_pairs(
    graph: MemoryGraph,
    metrics: JanitorMetrics,
    since: Optional[datetime] = None,
    max_nodes: int = 0,
) -> Dict[str, Any]:
    """
    Single token-recall pass that buckets pairs by similarity range.

    Returns {"duplicates": [...], "contradictions": [...]} where each entry
    is a pair dict with id_a, text_a, id_b, text_b, similarity, etc.
    Dedup range: DUPLICATE_MIN_SIM..DUPLICATE_MAX_SIM
    Contradiction range: CONTRADICTION_MIN_SIM..CONTRADICTION_MAX_SIM (Facts only — let LLM decide)
    """
    metrics.start_task("recall_pass")

    all_nodes = get_nodes_since(graph, since) if since else get_nodes_since(graph, None)
    node_carryover = 0
    if max_nodes and int(max_nodes) > 0 and len(all_nodes) > int(max_nodes):
        node_carryover = len(all_nodes) - int(max_nodes)
        new_nodes = all_nodes[: int(max_nodes)]
    else:
        new_nodes = all_nodes
    print(
        f"  Recall pass: {len(new_nodes)} {'new' if since else 'all'} nodes"
        + (f" (carryover={node_carryover})" if node_carryover else "")
    )

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

    return {
        "duplicates": dup_candidates,
        "contradictions": contradiction_candidates,
        "node_carryover": node_carryover,
    }


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

    def _invoke_batch(batch_num: int, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        batch_start_time = time.time()
        return {
            "batch_num": batch_num,
            "batch": batch,
            "results": batch_duplicate_check(batch, metrics),
            "duration": time.time() - batch_start_time,
        }

    llm_results = _run_llm_batches_parallel(batches, "duplicates", _invoke_batch)
    for result in llm_results:
        batch_num = int(result.get("batch_num", 0) or 0)
        batch = result.get("batch") or []
        if result.get("error"):
            err = str(result.get("error"))
            metrics.add_error(f"Duplicate batch {batch_num} exception: {err}")
            print(f"    Batch {batch_num}/{total_batches}: FAILED ({err})")
            continue
        decisions = result.get("results") or []
        batch_duration = float(result.get("duration", 0.0) or 0.0)
        merge_count = sum(1 for r in decisions if r)
        for dup, suggestion in zip(batch, decisions):
            if suggestion:
                dup["suggestion"] = suggestion
                duplicates.append(dup)
        print(f"    Batch {batch_num}/{total_batches}: {merge_count} merge suggestions ({batch_duration:.1f}s)")

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

    response, duration = call_fast_reasoning(
        prompt,
        max_tokens=200 * len(pairs),
        timeout=_lr_batch_timeout(),
    )
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

    def _invoke_batch(batch_num: int, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
        batch_start_time = time.time()
        return {
            "batch_num": batch_num,
            "batch": batch,
            "results": batch_contradiction_check(batch, metrics),
            "duration": time.time() - batch_start_time,
        }

    overall_timeout = None
    remaining_budget = MAX_EXECUTION_TIME - (time.time() - task_start_time)
    if remaining_budget > 0:
        overall_timeout = remaining_budget
    llm_results = _run_llm_batches_parallel(
        batches,
        "contradictions",
        _invoke_batch,
        overall_timeout_seconds=overall_timeout,
    )
    for result in llm_results:
        elapsed = time.time() - task_start_time
        if elapsed > MAX_EXECUTION_TIME:
            print(f"  Time limit reached ({elapsed:.1f}s), stopping result processing")
            metrics.add_error(f"Contradiction check stopped: {elapsed:.1f}s > {MAX_EXECUTION_TIME}s")
            break
        batch_num = int(result.get("batch_num", 0) or 0)
        batch = result.get("batch") or []
        if result.get("error"):
            err = str(result.get("error"))
            metrics.add_error(f"Contradiction batch {batch_num} exception: {err}")
            print(f"    Batch {batch_num}/{total_batches}: FAILED ({err})")
            continue

        decisions = result.get("results") or []
        batch_duration = float(result.get("duration", 0.0) or 0.0)
        batch_confirmed = 0
        for pair, is_contradiction in zip(batch, decisions):
            if is_contradiction:
                contradictions.append({**pair, "explanation": is_contradiction})
                confirmed_contradictions += 1
                batch_confirmed += 1
                if not dry_run:
                    store_contradiction(pair["id_a"], pair["id_b"], is_contradiction)
        print(f"    Batch {batch_num}/{total_batches}: {batch_confirmed} contradictions ({batch_duration:.1f}s)")

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
    response, duration = call_fast_reasoning(
        prompt,
        max_tokens=150 * len(pairs),
        timeout=_lr_batch_timeout(),
    )
    metrics.add_llm_call(duration)

    if not response:
        _record_llm_batch_issue(metrics, f"Batch contradiction check failed ({len(pairs)} pairs)")
        return [None] * len(pairs)

    parsed = parse_json_response(response)
    if not isinstance(parsed, list):
        _record_llm_batch_issue(metrics, "Batch contradiction response was not a list")
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

def resolve_contradictions_with_opus(
    graph: MemoryGraph,
    metrics: JanitorMetrics,
    dry_run: bool = True,
    max_items: int = 0,
    llm_timeout_seconds: Optional[float] = None,
) -> Dict[str, int]:
    """Resolve pending contradictions using Opus for deep-reasoning decisions."""
    metrics.start_task("contradiction_resolution")
    results = {"resolved": 0, "false_positive": 0, "merged": 0, "decisions": [], "carryover": 0}

    batch_limit = 50
    if max_items and int(max_items) > 0:
        batch_limit = max(1, min(int(max_items), 5000))
    pending = get_pending_contradictions(limit=batch_limit)
    if batch_limit:
        with graph._get_conn() as conn:
            total_pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM contradictions WHERE status = 'pending'"
                ).fetchone()[0]
            )
        results["carryover"] = max(total_pending - len(pending), 0)
    if not pending:
        print("  No pending contradictions to resolve")
        metrics.end_task("contradiction_resolution")
        return results

    review_model = _janitor_review_model()
    print(f"  Resolving {len(pending)} pending contradictions via Opus ({review_model})...")

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
    def _invoke_batch(batch_num: int, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
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

        return {
            "batch_num": batch_num,
            "batch": batch,
            "prompt_tag": f"[prompt:{_prompt_hash(prompt)}] ",
            "response_duration": call_deep_reasoning(
                prompt,
                max_tokens=300 * len(batch),
                timeout=_effective_llm_timeout(llm_timeout_seconds, DEEP_REASONING_TIMEOUT),
                model=review_model,
            ),
        }

    llm_results = _run_llm_batches_parallel(batches, "contradiction_resolution", _invoke_batch)
    for result in llm_results:
        batch_num = int(result.get("batch_num", 0) or 0)
        batch = result.get("batch") or []
        prompt_tag = str(result.get("prompt_tag") or "")
        response, duration = result.get("response_duration", (None, 0.0))
        metrics.add_llm_call(float(duration or 0.0))
        if not response:
            _record_llm_batch_issue(metrics, f"Contradiction resolution batch {batch_num} failed")
            print(f"    Batch {batch_num}/{total_batches}: FAILED (no response)")
            continue

        parsed = parse_json_response(response)
        if not isinstance(parsed, list):
            _record_llm_batch_issue(metrics, f"Contradiction resolution batch {batch_num}: invalid JSON")
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
            reason = prompt_tag + item.get("reason", "")
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
                    now_iso = datetime.now().isoformat()
                    with graph._get_conn() as conn:
                        conn.execute(
                            """
                            UPDATE nodes SET superseded_by = ?, confidence = 0.1,
                                valid_until = ?, updated_at = ?
                            WHERE id = ? AND superseded_by IS NULL
                            """,
                            (c["node_a_id"], now_iso, now_iso, c["node_b_id"]),
                        )
                        conn.execute(
                            """
                            UPDATE contradictions
                            SET status = 'resolved', resolution = ?, resolution_reason = ?,
                                resolved_at = datetime('now')
                            WHERE id = ?
                            """,
                            ("keep_a", reason, c["id"]),
                        )
                results["resolved"] += 1
                batch_resolved += 1
                results["decisions"].append(decision_row)

            elif action == "KEEP_B":
                if dry_run:
                    print(f"    Would KEEP_B: supersede A ({c['text_a'][:40]}...) — {reason[:50]}")
                else:
                    now_iso = datetime.now().isoformat()
                    with graph._get_conn() as conn:
                        conn.execute(
                            """
                            UPDATE nodes SET superseded_by = ?, confidence = 0.1,
                                valid_until = ?, updated_at = ?
                            WHERE id = ? AND superseded_by IS NULL
                            """,
                            (c["node_b_id"], now_iso, now_iso, c["node_a_id"]),
                        )
                        conn.execute(
                            """
                            UPDATE contradictions
                            SET status = 'resolved', resolution = ?, resolution_reason = ?,
                                resolved_at = datetime('now')
                            WHERE id = ?
                            """,
                            ("keep_b", reason, c["id"]),
                        )
                results["resolved"] += 1
                batch_resolved += 1
                results["decisions"].append(decision_row)

            elif action == "KEEP_BOTH":
                if dry_run:
                    print(f"    Would KEEP_BOTH: {reason[:60]}")
                else:
                    mark_contradiction_false_positive(c["id"], reason)
                results["false_positive"] += 1
                batch_resolved += 1
                results["decisions"].append(decision_row)

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


EDGE_BATCH_SIZE = 25  # Safety cap for edge extraction batch size


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
                stored = store_edge_keywords(relation, keywords)
                if stored:
                    print(f"    Stored keywords for new relation '{relation}': {keywords[:5]}")

    return results


# =============================================================================
# Task 2b: Review Dedup Rejections (Opus)
# =============================================================================

def review_dedup_rejections(
    graph: MemoryGraph,
    metrics: JanitorMetrics,
    dry_run: bool = True,
    max_items: int = 0,
    llm_timeout_seconds: Optional[float] = None,
) -> Dict[str, int]:
    """Review recent dedup rejections using Opus to catch false positives."""
    metrics.start_task("dedup_review")
    results = {"reviewed": 0, "confirmed": 0, "reversed": 0, "carryover": 0}

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

    batch_limit = 50
    if max_items and int(max_items) > 0:
        batch_limit = max(1, min(int(max_items), 5000))
    pending = get_recent_dedup_rejections(hours=24, limit=batch_limit)
    if batch_limit:
        with graph._get_conn() as conn:
            total_pending = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM dedup_log
                    WHERE review_status = 'unreviewed'
                      AND decision != 'hash_exact'
                      AND created_at > datetime('now', '-24 hours')
                    """
                ).fetchone()[0]
            )
        results["carryover"] = max(total_pending - len(pending), 0)
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
    def _invoke_batch(batch_num: int, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
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

        return {
            "batch_num": batch_num,
            "batch": batch,
            "prompt_tag": f"[prompt:{_prompt_hash(prompt)}] ",
            "response_duration": call_deep_reasoning(
                prompt,
                max_tokens=200 * len(batch),
                timeout=_effective_llm_timeout(llm_timeout_seconds, DEEP_REASONING_TIMEOUT),
            ),
        }

    llm_results = _run_llm_batches_parallel(batches, "dedup_review", _invoke_batch)
    for result in llm_results:
        batch_num = int(result.get("batch_num", 0) or 0)
        batch = result.get("batch") or []
        dedup_prompt_tag = str(result.get("prompt_tag") or "")
        response, duration = result.get("response_duration", (None, 0.0))
        metrics.add_llm_call(float(duration or 0.0))
        if not response:
            _record_llm_batch_issue(metrics, f"Dedup review batch {batch_num} failed")
            _diag_log_decision(
                "dedup_review_batch_failed",
                batch_num=batch_num,
                total_batches=total_batches,
                reason="empty_response",
            )
            print(f"    Batch {batch_num}/{total_batches}: FAILED (no response)")
            continue

        parsed = parse_json_response(response)
        if not isinstance(parsed, list):
            _record_llm_batch_issue(metrics, f"Dedup review batch {batch_num}: invalid JSON")
            _diag_log_decision(
                "dedup_review_batch_failed",
                batch_num=batch_num,
                total_batches=total_batches,
                reason="invalid_json",
                response_preview=(response or "")[:220],
            )
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
            _diag_log_decision(
                "dedup_review_decision",
                dry_run=bool(dry_run),
                log_id=entry.get("id"),
                action=action,
                reason=reason,
                similarity=float(entry.get("similarity", 0.0) or 0.0),
                new_text=entry.get("new_text", ""),
                existing_text=entry.get("existing_text", ""),
            )

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

def review_decayed_memories(
    graph: MemoryGraph,
    metrics: JanitorMetrics,
    dry_run: bool = True,
    max_items: int = 0,
    llm_timeout_seconds: Optional[float] = None,
) -> Dict[str, int]:
    """Review memories queued for decay deletion using Opus."""
    metrics.start_task("decay_review")
    results = {"reviewed": 0, "deleted": 0, "extended": 0, "pinned": 0, "carryover": 0}

    batch_limit = 50
    if max_items and int(max_items) > 0:
        batch_limit = max(1, min(int(max_items), 5000))
    pending = get_pending_decay_reviews(limit=batch_limit)
    if batch_limit:
        with graph._get_conn() as conn:
            total_pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM decay_review_queue WHERE status = 'pending'"
                ).fetchone()[0]
            )
        results["carryover"] = max(total_pending - len(pending), 0)
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
    def _invoke_batch(batch_num: int, batch: List[Dict[str, Any]]) -> Dict[str, Any]:
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

        return {
            "batch_num": batch_num,
            "batch": batch,
            "prompt_tag": f"[prompt:{_prompt_hash(prompt)}] ",
            "response_duration": call_deep_reasoning(
                prompt,
                max_tokens=200 * len(batch),
                timeout=_effective_llm_timeout(llm_timeout_seconds, DEEP_REASONING_TIMEOUT),
            ),
        }

    llm_results = _run_llm_batches_parallel(batches, "decay_review", _invoke_batch)
    for result in llm_results:
        batch_num = int(result.get("batch_num", 0) or 0)
        batch = result.get("batch") or []
        decay_prompt_tag = str(result.get("prompt_tag") or "")
        response, duration = result.get("response_duration", (None, 0.0))
        metrics.add_llm_call(float(duration or 0.0))
        if not response:
            _record_llm_batch_issue(metrics, f"Decay review batch {batch_num} failed")
            print(f"    Batch {batch_num}/{total_batches}: FAILED (no response)")
            continue

        parsed = parse_json_response(response)
        if not isinstance(parsed, list):
            _record_llm_batch_issue(metrics, f"Decay review batch {batch_num}: invalid JSON")
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
                        # Keep delete + queue resolution in one transaction.
                        with graph._get_conn() as conn:
                            hard_delete_node(node_id, conn=conn)
                            resolve_decay_review(entry["id"], "delete", reason, conn=conn)
                    else:
                        print(f"    SKIPPED delete (archive failed): {entry['node_text'][:50]}...", file=sys.stderr)
                        continue
                print(f"    DELETE: {entry['node_text'][:50]}...")
                results["deleted"] += 1
            elif action == "EXTEND":
                if not dry_run:
                    # Apply node update and queue resolution together to avoid partial commit.
                    ext_conf = 0.3
                    with graph._get_conn() as conn:
                        row = conn.execute("SELECT attributes FROM nodes WHERE id = ?", (node_id,)).fetchone()
                        if row:
                            attrs_raw = row["attributes"] if "attributes" in row.keys() else None
                            if attrs_raw:
                                try:
                                    attrs = json.loads(attrs_raw)
                                    ext_conf = float(attrs.get("extraction_confidence", 0.3) or 0.3)
                                except Exception:
                                    ext_conf = 0.3
                        # Scale from node's extraction_confidence if available, otherwise 0.3
                        extend_conf = max(0.3, float(ext_conf) * 0.5) if ext_conf else 0.3
                        conn.execute(
                            "UPDATE nodes SET confidence = ?, accessed_at = ? WHERE id = ?",
                            (extend_conf, datetime.now().isoformat(), node_id)
                        )
                        conn.execute(
                            """
                            UPDATE decay_review_queue
                            SET decision = ?, decision_reason = ?, status = 'reviewed',
                                reviewed_at = datetime('now')
                            WHERE id = ?
                            """,
                            ("extend", reason, entry["id"]),
                        )
                print(f"    EXTEND: {entry['node_text'][:50]}...")
                results["extended"] += 1
            elif action == "PIN":
                if not dry_run:
                    # Apply node update and queue resolution together to avoid partial commit.
                    ext_conf = 0.7
                    with graph._get_conn() as conn:
                        row = conn.execute("SELECT attributes FROM nodes WHERE id = ?", (node_id,)).fetchone()
                        if row:
                            attrs_raw = row["attributes"] if "attributes" in row.keys() else None
                            if attrs_raw:
                                try:
                                    attrs = json.loads(attrs_raw)
                                    ext_conf = float(attrs.get("extraction_confidence", 0.7) or 0.7)
                                except Exception:
                                    ext_conf = 0.7
                        # Use max of 0.7 and node's extraction_confidence so high-value facts keep their score
                        pin_conf = max(0.7, float(ext_conf)) if ext_conf else 0.7
                        conn.execute(
                            "UPDATE nodes SET pinned = 1, confidence = ? WHERE id = ?",
                            (pin_conf, node_id)
                        )
                        conn.execute(
                            """
                            UPDATE decay_review_queue
                            SET decision = ?, decision_reason = ?, status = 'reviewed',
                                reviewed_at = datetime('now')
                            WHERE id = ?
                            """,
                            ("pin", reason, entry["id"]),
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
      - "linear": subtract flat rate per cycle.
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
            # Linear decay
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

def review_pending_memories(
    graph: MemoryGraph,
    dry_run: bool = True,
    metrics: Optional[JanitorMetrics] = None,
    max_items: int = 0,
) -> Dict[str, Any]:
    """
    Review all pending memories via the deep-reasoning LLM.
    Sends batches of memories for KEEP/DELETE/FIX/MERGE decisions and applies them immediately.
    """
    model = _janitor_review_model()
    max_tokens = _cfg.models.max_output('deep')

    # Get all pending memories (with optional per-run cap for backlog control)
    with graph._get_conn() as conn:
        total_pending = int(
            conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'pending'").fetchone()[0]
        )
        q = """
            SELECT id, type, name, created_at, verified, confidence, source, session_id, speaker
            FROM nodes
            WHERE status = 'pending'
            ORDER BY created_at DESC
        """
        params = []
        if max_items and int(max_items) > 0:
            q += " LIMIT ?"
            params.append(int(max_items))
        rows = conn.execute(q, params).fetchall()

    if not rows:
        print("No pending memories found")
        return {
            "total_reviewed": 0,
            "total_pending": total_pending,
            "carryover": max(total_pending, 0),
            "review_coverage_ratio": 1.0,
            "kept": 0,
            "deleted": 0,
            "fixed": 0,
            "merged": 0,
        }

    print(f"\n{'='*80}")
    print(f"MEMORY REVIEW - {len(rows)} pending memories via Opus API")
    print(f"{'='*80}")

    owner = _owner_display_name()
    owner_full = _owner_full_name()
    system_prompt = f"""You are reviewing memories in {owner}'s personal knowledge base.
For each memory, decide: KEEP, DELETE, or FIX.

This is a PERSONAL knowledge base with PROJECT continuity.
It stores facts about people ({owner}, family, friends, colleagues, pets), their preferences,
relationships, decisions, life events, and high-value project-state continuity.
System architecture docs and generic infrastructure references belong in documentation, not memory.

CRITERIA:
- KEEP: Personal facts, preferences, opinions, decisions (with reasoning), relationships,
  significant life events, health info, locations, schedules, emotional reactions.
- KEEP: Technical/project-state facts that are specific and reusable later, including bugs,
  fixes, root causes, architecture choices, schema/API details, version changes, deployment
  choices, tests, and concrete implementation constraints.
- KEEP: Assistant-originated technical guidance when it captures a concrete project decision,
  implementation detail, or operational state.
- DELETE: Conversational filler, generic platitudes, motivational chatter, one-off social
  suggestions with no lasting value, obvious duplicates, and vague/unactionable statements.
- DELETE: Generic tool boilerplate not tied to a specific project/work context.
- FIX: Good info with attribution errors or clarity issues (fix "The user" -> "{owner}")

HARD RULES:
- DO NOT delete a memory only because it is assistant-sourced.
- DO NOT delete a memory only because it is technical.
- For technical memories, prefer KEEP unless it is clearly generic boilerplate or clearly
  ephemeral with no future utility.
- Do NOT use MERGE in this pass. Return only KEEP/DELETE/FIX.

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

Any new_text for facts MUST be at least 3 words (subject + verb + object). Entity names (people, places) can be 1-2 words.

Respond with a JSON array only, no markdown fencing:
[
  {{"id": "uuid", "action": "KEEP"}},
  {{"id": "uuid", "action": "DELETE"}},
  {{"id": "uuid", "action": "FIX", "new_text": "corrected text"}},
  {{"id": "uuid", "action": "FIX", "new_text": "Beth is {owner}'s sister", "edges": [{{"subject": "Beth", "relation": "sibling_of", "object": "{owner_full}"}}]}}
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
    reviewed_ids = {str(r["id"]) for r in rows}
    covered_ids = set()
    missing_ids_overall = []

    def _synthesize_keep_decisions(ids: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for mid in ids:
            if isinstance(mid, str) and mid:
                out.append({"id": mid, "action": "KEEP"})
        return out

    def _normalize_review_decisions(payload: Any) -> List[Dict[str, Any]]:
        """Normalize wrapped/aliased review payloads into decision dicts."""
        cur = payload
        if isinstance(cur, dict):
            for key in ("decisions", "reviews", "results", "items", "data", "memories"):
                maybe = cur.get(key)
                if isinstance(maybe, list):
                    cur = maybe
                    break

        if not isinstance(cur, list):
            return []

        normalized: List[Dict[str, Any]] = []
        for item in cur:
            if not isinstance(item, dict):
                continue
            out = dict(item)
            if not out.get("id") and isinstance(out.get("memory_id"), str):
                out["id"] = out["memory_id"]
            if not out.get("action") and isinstance(out.get("decision"), str):
                out["action"] = out["decision"]
            action = str(out.get("action", "")).upper().strip()
            if action in {"APPROVE", "CONFIRM"}:
                action = "KEEP"
            elif action in {"REJECT", "REMOVE"}:
                action = "DELETE"
            elif action == "UPDATE":
                action = "FIX"
            out["action"] = action
            normalized.append(out)
        return normalized

    def _collect_covered_ids(decisions_list: List[Dict[str, Any]]) -> set[str]:
        out = set()
        for decision in decisions_list:
            if not isinstance(decision, dict):
                continue
            did = decision.get("id")
            if isinstance(did, str) and did:
                out.add(did)
            merge_ids = decision.get("merge_ids")
            if isinstance(merge_ids, list):
                for mid in merge_ids:
                    if isinstance(mid, str) and mid:
                        out.add(mid)
        return out

    batch_requests: List[Dict[str, Any]] = []
    for batch_num, batch_rows in enumerate(batches, 1):
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
        batch_requests.append(
            {
                "batch_rows": batch_rows,
                "batch_data": batch_data,
                "user_message": user_message,
                "batch_max_tokens": 200 * len(batch_data),
            }
        )

    def _invoke_review_batch(batch_num: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response_text, duration = call_llm(
                system_prompt=system_prompt,
                user_message=payload["user_message"],
                model=model,
                max_tokens=payload["batch_max_tokens"],
            )
            error: Optional[Exception] = None
        except Exception as exc:
            response_text, duration = None, 0.0
            error = exc
        return {
            "batch_num": batch_num,
            "batch_rows": payload["batch_rows"],
            "batch_data": payload["batch_data"],
            "response_text": response_text,
            "duration": duration,
            "error": error,
        }

    llm_results = _run_llm_batches_parallel(batch_requests, "review_pending", _invoke_review_batch)
    for result in llm_results:
        batch_num = int(result.get("batch_num", 0) or 0)
        batch_rows = result.get("batch_rows") or []
        batch_data = result.get("batch_data") or []
        response_text = result.get("response_text")
        duration = float(result.get("duration", 0.0) or 0.0)
        error = result.get("error")

        try:
            retry_cause = None
            if error is not None:
                raise error
            if metrics:
                metrics.add_llm_call(duration)

            if not response_text:
                print(f"    API returned empty response, skipping batch")
                _diag_log_decision(
                    "review_batch_failed",
                    batch_num=batch_num,
                    reason="empty_response",
                )
                if metrics:
                    _record_llm_batch_issue(metrics, f"Review batch {batch_num}: empty API response")
                continue

            decisions_raw = parse_json_response(response_text)
            decisions = _normalize_review_decisions(decisions_raw)
            if not decisions:
                if _is_benchmark_mode():
                    fallback_msg = (
                        f"Review batch {batch_num}: invalid JSON response; "
                        "benchmark-mode fallback applying KEEP to all batch ids"
                    )
                    print(f"    WARNING: {fallback_msg}")
                    _diag_log_decision(
                        "review_batch_fallback_keep",
                        batch_num=batch_num,
                        reason="invalid_json",
                        response_preview=(response_text or "")[:220],
                        fallback_ids=[str(r["id"]) for r in batch_rows],
                    )
                    if metrics:
                        metrics.add_warning(fallback_msg)
                    decisions = _synthesize_keep_decisions([str(r["id"]) for r in batch_rows])
                else:
                    print(f"    Failed to parse response as JSON array, skipping batch")
                    _diag_log_decision(
                        "review_batch_failed",
                        batch_num=batch_num,
                        reason="invalid_json",
                        response_preview=(response_text or "")[:220],
                    )
                    if metrics:
                        _record_llm_batch_issue(metrics, f"Review batch {batch_num}: invalid JSON response")
                    continue

            batch_ids = {str(r["id"]) for r in batch_rows}
            batch_covered = _collect_covered_ids(decisions)
            missing = sorted(batch_ids - batch_covered)
            if missing:
                coverage_ratio = (len(batch_ids) - len(missing)) / max(len(batch_ids), 1)
                msg = (
                    f"Review batch {batch_num}: incomplete decision coverage "
                    f"({len(batch_ids)-len(missing)}/{len(batch_ids)} = {coverage_ratio:.2%}); "
                    f"retrying missing IDs"
                )
                print(f"    WARNING: {msg}")
                _diag_log_decision(
                    "review_batch_coverage_retry",
                    batch_num=batch_num,
                    batch_size=len(batch_ids),
                    missing_ids=missing,
                )
                if metrics:
                    metrics.add_warning(msg)

                missing_payload = [item for item in batch_data if item["id"] in missing]
                retry_prompt = (
                    "You previously omitted decisions for some memories. "
                    "Return exactly one decision object per memory below. "
                    "Allowed actions: KEEP, DELETE, FIX. "
                    "Do not use MERGE in this retry pass.\n\n"
                    f"Memories:\n{json.dumps(missing_payload, indent=2)}\n\n"
                    "Respond with JSON array only:\n"
                    "[{\"id\":\"...\",\"action\":\"KEEP\"}]"
                )
                retry_text, retry_duration = call_llm(
                    system_prompt=system_prompt,
                    user_message=retry_prompt,
                    model=model,
                    max_tokens=max(200 * len(missing_payload), 300),
                )
                if metrics:
                    metrics.add_llm_call(retry_duration)
                retry_cause = None
                if not retry_text:
                    retry_cause = RuntimeError("retry returned empty response")
                retry_raw = parse_json_response(retry_text or "")
                retry_decisions = _normalize_review_decisions(retry_raw)
                if retry_text and not retry_decisions:
                    retry_cause = RuntimeError("retry returned invalid decision payload")
                if retry_decisions:
                    decisions.extend(retry_decisions)
                    batch_covered = _collect_covered_ids(decisions)
                    missing = sorted(batch_ids - batch_covered)

            if missing:
                missing_ids_overall.extend(missing)
                if _is_benchmark_mode():
                    msg = (
                        f"Review batch {batch_num}: incomplete coverage after retry; "
                        f"benchmark-mode fallback applying KEEP for missing_ids={missing}"
                    )
                    print(f"    WARNING: {msg}")
                    _diag_log_decision(
                        "review_batch_fallback_keep",
                        batch_num=batch_num,
                        reason="coverage_missing_after_retry",
                        missing_ids=missing,
                    )
                    if metrics:
                        metrics.add_warning(msg)
                    decisions.extend(_synthesize_keep_decisions(missing))
                    batch_covered = _collect_covered_ids(decisions)
                    missing = sorted(batch_ids - batch_covered)
                    if missing:
                        msg2 = (
                            f"Review batch {batch_num}: benchmark fallback failed to cover "
                            f"missing_ids={missing}"
                        )
                        if metrics:
                            metrics.add_error(msg2)
                        raise RuntimeError(msg2)
                    missing_ids_overall = [mid for mid in missing_ids_overall if mid not in batch_ids]
                else:
                    msg = (
                        f"Review batch {batch_num}: incomplete decision coverage after retry; "
                        f"missing_ids={missing}"
                    )
                    if metrics:
                        metrics.add_error(msg)
                    if retry_cause is not None:
                        raise RuntimeError(msg) from retry_cause
                    raise RuntimeError(msg)

            print(f"    Received {len(decisions)} decisions in {duration:.1f}s")
            covered_ids.update(batch_ids)

            # Apply in deterministic batch order.
            batch_result = apply_review_decisions_from_list(graph, decisions, dry_run)
            totals["kept"] += batch_result["kept"]
            totals["deleted"] += batch_result["deleted"]
            totals["fixed"] += batch_result["fixed"]
            totals["merged"] += batch_result["merged"]

        except RuntimeError as e:
            # Fatal review-path issue — abort all review batches
            print(f"    API key error: {e}")
            if metrics:
                metrics.add_error(f"Review aborted: {e}")
            raise
        except Exception as e:
            fatal_transport_error = isinstance(
                e,
                (ConnectionError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError),
            )
            if fatal_transport_error:
                print(f"    Review transport error (aborting): {e}")
                if metrics:
                    metrics.add_error(f"Review aborted: {e}")
                raise RuntimeError(str(e)) from e
            print(f"    Batch {batch_num} failed: {e}")
            if metrics:
                _record_llm_batch_issue(metrics, f"Review batch {batch_num}: {e}")
            continue

    print(f"\n  Review complete: {totals['kept']} kept, {totals['deleted']} deleted, "
          f"{totals['fixed']} fixed, {totals['merged']} merged")

    coverage_ratio = len(covered_ids) / max(len(reviewed_ids), 1)
    if metrics:
        metrics.add_warning(
            f"review coverage ratio={coverage_ratio:.4f} covered={len(covered_ids)} total={len(reviewed_ids)}"
        )

    return {
        "total_reviewed": len(rows),
        "total_pending": total_pending,
        "carryover": max(total_pending - len(rows), 0),
        "review_coverage_ratio": round(coverage_ratio, 4),
        "missing_ids": missing_ids_overall,
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
        reason = str(decision.get("reason", "") or "")

        # Handle MERGE
        if action == "MERGE" and "merge_ids" in decision and "merged_text" in decision:
            merge_ids = decision["merge_ids"]
            merged_text = decision["merged_text"]
            _diag_log_decision(
                "review_decision_merge",
                dry_run=bool(dry_run),
                merge_ids=merge_ids,
                merged_text=merged_text,
                reason=reason,
            )
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
            _diag_log_decision(
                "review_decision_skipped",
                dry_run=bool(dry_run),
                action=action,
                reason=reason,
                payload=decision,
                skip_reason="missing_id",
            )
            continue

        current_text = ""
        source_type = None
        source_value = None
        speaker_value = None
        with graph._get_conn() as conn:
            row = conn.execute(
                "SELECT name, status, source, speaker, attributes FROM nodes WHERE id = ?",
                (memory_id,),
            ).fetchone()
            current_status = row["status"] if row else "missing"
            if row and row["name"]:
                current_text = str(row["name"])
            if row:
                source_value = row["source"]
                speaker_value = row["speaker"]
                try:
                    attrs = json.loads(row["attributes"] or "{}")
                    if isinstance(attrs, dict):
                        source_type = attrs.get("source_type")
                except Exception:
                    source_type = None
            if action == "DELETE":
                _diag_log_decision(
                    "review_decision_delete",
                    dry_run=bool(dry_run),
                    memory_id=memory_id,
                    current_status=current_status,
                    current_text=current_text,
                    source=source_value,
                    speaker=speaker_value,
                    source_type=source_type,
                    reason=reason,
                )
                if dry_run:
                    print(f"    Would DELETE: {memory_id}")
                else:
                    # Delete fact edges + node in one transaction so crashes cannot leave partial state.
                    edges_deleted = conn.execute(
                        "DELETE FROM edges WHERE source_fact_id = ?",
                        (memory_id,),
                    ).rowcount
                    conn.execute("DELETE FROM edges WHERE source_id = ? OR target_id = ?", (memory_id, memory_id))
                    conn.execute("DELETE FROM contradictions WHERE node_a_id = ? OR node_b_id = ?", (memory_id, memory_id))
                    conn.execute("DELETE FROM decay_review_queue WHERE node_id = ?", (memory_id,))
                    try:
                        conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (memory_id,))
                    except Exception:
                        pass
                    conn.execute("DELETE FROM nodes WHERE id = ?", (memory_id,))
                    if edges_deleted > 0:
                        print(f"    DELETED {edges_deleted} edges from fact")
                    print(f"    DELETED: {memory_id}")
                deleted += 1

            elif action == "FIX" and "new_text" in decision:
                new_text = decision["new_text"]
                new_edges = decision.get("edges", [])
                _diag_log_decision(
                    "review_decision_fix",
                    dry_run=bool(dry_run),
                    memory_id=memory_id,
                    current_status=current_status,
                    current_text=current_text,
                    source=source_value,
                    speaker=speaker_value,
                    source_type=source_type,
                    new_text=new_text,
                    reason=reason,
                    new_edges_count=len(new_edges or []),
                )
                if dry_run:
                    print(f"    Would FIX: {memory_id} -> {new_text[:50]}...")
                    if new_edges:
                        print(f"    Would create {len(new_edges)} new edges")
                else:
                    # Delete old edges + update fact + recreate edges in one transaction.
                    edges_deleted = conn.execute(
                        "DELETE FROM edges WHERE source_fact_id = ?",
                        (memory_id,),
                    ).rowcount
                    # Update the fact text, embedding, and content_hash.
                    from lib.embeddings import get_embedding as _get_emb_fix, pack_embedding as _pack_emb_fix
                    new_emb = _get_emb_fix(new_text)
                    new_hash = content_hash(new_text)
                    packed_emb = _pack_emb_fix(new_emb) if new_emb else None
                    conn.execute(
                        "UPDATE nodes SET name = ?, embedding = ?, content_hash = ?, updated_at = ?, status = 'approved' WHERE id = ?",
                        (new_text, packed_emb, new_hash, datetime.now().isoformat(), memory_id)
                    )
                    # Best effort vec index refresh. Keep the core fix path resilient
                    # when embedding dims drift across providers/config.
                    if packed_emb:
                        try:
                            conn.execute(
                                "INSERT OR REPLACE INTO vec_nodes(node_id, embedding) VALUES (?, ?)",
                                (memory_id, packed_emb)
                            )
                        except Exception as exc:
                            if is_fail_hard_enabled():
                                raise RuntimeError(
                                    f"vec_nodes update failed during review FIX for node {memory_id}"
                                ) from exc
                            logger.warning("vec_nodes update skipped during review FIX for node %s: %s", memory_id, exc)
                    # Create new edges if provided.
                    for edge_data in new_edges:
                        if edge_data.get("subject") and edge_data.get("relation") and edge_data.get("object"):
                            norm = _normalize_edge(
                                edge_data["subject"], "entity",
                                edge_data["relation"],
                                edge_data["object"], "entity"
                            )
                            result = create_edge(
                                norm[0],
                                norm[2],
                                norm[3],
                                source_fact_id=memory_id,
                                _conn=conn,
                            )
                            if result["status"] == "created":
                                print(f"    Created edge: {edge_data['subject']} --{edge_data['relation']}--> {edge_data['object']}")
                    if edges_deleted > 0:
                        print(f"    DELETED {edges_deleted} old edges from fact")
                    print(f"    FIXED: {memory_id} -> {new_text[:50]}...")
                fixed += 1

            elif action == "KEEP":
                _diag_log_decision(
                    "review_decision_keep",
                    dry_run=bool(dry_run),
                    memory_id=memory_id,
                    current_status=current_status,
                    current_text=current_text,
                    source=source_value,
                    speaker=speaker_value,
                    source_type=source_type,
                    reason=reason,
                )
                if not dry_run:
                    conn.execute(
                        "UPDATE nodes SET status = 'approved' WHERE id = ?",
                        (memory_id,)
                    )
                kept += 1
            else:
                _diag_log_decision(
                    "review_decision_skipped",
                    dry_run=bool(dry_run),
                    memory_id=memory_id,
                    current_status=current_status,
                    current_text=current_text,
                    action=action,
                    reason=reason,
                    payload=decision,
                    skip_reason="unknown_action",
                )

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
                    except Exception as exc:
                        if is_fail_hard_enabled():
                            raise RuntimeError(
                                f"vec_nodes update failed during fact rewrite for node {fact_id}"
                            ) from exc
                        logger.warning("vec_nodes update skipped during fact rewrite for node %s: %s", fact_id, exc)
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
