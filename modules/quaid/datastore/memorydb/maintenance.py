"""Unified memory-graph datastore maintenance routine.

This module is datastore-owned and registered via the lifecycle registry.
Janitor remains the orchestration layer; datastore routines own task execution.
"""

from __future__ import annotations

import sqlite3
import time

from datastore.memorydb import maintenance_ops as ops
from datastore.memorydb.memory_graph import (
    _DATASTORE_BUSY_RETRY_DELAYS_SECONDS,
    _is_sqlite_busy_or_locked,
)


def _fail_hard_enabled() -> bool:
    try:
        from lib.fail_policy import is_fail_hard_enabled
    except Exception:
        return True
    return bool(is_fail_hard_enabled())


def _append_result_log(result, message: str) -> None:
    logs = getattr(result, "logs", None)
    if isinstance(logs, list):
        logs.append(message)


def _record_memory_maintenance_error(result, exc: BaseException):
    if isinstance(exc, ValueError):
        msg = str(exc)
        # Recoverable data-quality guard: malformed short facts should not fail
        # the whole janitor pass. Keep telemetry and continue.
        if "at least 3 words" in msg:
            result.metrics["invalid_facts_skipped"] = int(result.metrics.get("invalid_facts_skipped", 0)) + 1
            _append_result_log(result, f"Memory graph maintenance skipped malformed fact: {msg}")
            return result
    result.errors.append(
        f"Memory graph maintenance failed ({exc.__class__.__name__}): {exc}"
    )
    return result


def _is_recoverable_memory_maintenance_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and "at least 3 words" in str(exc)


def _run_memory_graph_maintenance_once(ctx, result):
    graph = ctx.graph
    subtask = str((ctx.options or {}).get("subtask") or "review").strip().lower()
    max_items = int((ctx.options or {}).get("max_items") or 0)
    llm_timeout_raw = float((ctx.options or {}).get("llm_timeout_seconds") or 0)
    llm_timeout_seconds = llm_timeout_raw if llm_timeout_raw > 0 else None
    metrics = ops.JanitorMetrics()

    if subtask == "review":
        r = ops.review_pending_memories(
            graph,
            dry_run=ctx.dry_run,
            metrics=metrics,
            max_items=max_items,
            llm_timeout_seconds=llm_timeout_seconds,
        )
        result.metrics["memories_reviewed"] = int(r.get("total_reviewed", 0))
        result.metrics["memories_deleted"] = int(r.get("deleted", 0))
        result.metrics["memories_fixed"] = int(r.get("fixed", 0))
        result.metrics["review_carryover"] = int(r.get("carryover", 0))
        result.metrics["review_coverage_ratio_pct"] = int(round(float(r.get("review_coverage_ratio", 1.0)) * 100))
        if r.get("missing_ids"):
            result.errors.append(
                f"Review returned missing decision coverage ids={r.get('missing_ids')}"
            )
        return result

    if subtask == "temporal":
        r = ops.resolve_temporal_references(graph, dry_run=ctx.dry_run, metrics=metrics)
        result.metrics["temporal_found"] = int(r.get("found", 0))
        result.metrics["temporal_fixed"] = int(r.get("fixed", 0))
        return result

    if subtask == "dedup_review":
        r = ops.review_dedup_rejections(
            graph,
            metrics,
            dry_run=ctx.dry_run,
            max_items=max_items,
            llm_timeout_seconds=llm_timeout_seconds,
        )
        result.metrics["dedup_reviewed"] = int(r.get("reviewed", 0))
        result.metrics["dedup_confirmed"] = int(r.get("confirmed", 0))
        result.metrics["dedup_reversed"] = int(r.get("reversed", 0))
        result.metrics["dedup_review_carryover"] = int(r.get("carryover", 0))
        return result

    if subtask == "duplicates":
        pair_buckets = ops.recall_similar_pairs(graph, metrics, since=None, max_nodes=max_items)
        dups = ops.find_duplicates_from_pairs(pair_buckets.get("duplicates", []), metrics)
        merges_applied = 0
        merged_ids = set()
        for dup in dups:
            if dup["id_a"] in merged_ids or dup["id_b"] in merged_ids:
                continue
            suggestion = dup.get("suggestion", {})
            if suggestion.get("action") == "merge":
                merged_text = suggestion.get("merged_text", "")
                if (not ctx.dry_run) and merged_text:
                    ops._merge_nodes_into(graph, merged_text, [dup["id_a"], dup["id_b"]], source="dedup_merge")
                if merged_text:
                    merged_ids.add(dup["id_a"])
                    merged_ids.add(dup["id_b"])
                    merges_applied += 1
        result.metrics["duplicates_merged"] = merges_applied
        result.metrics["duplicates_carryover"] = int(pair_buckets.get("node_carryover", 0))
        return result

    if subtask == "contradictions":
        pair_buckets = ops.recall_similar_pairs(graph, metrics, since=None, max_nodes=max_items)
        contradictions = ops.find_contradictions_from_pairs(
            pair_buckets.get("contradictions", []), metrics, dry_run=ctx.dry_run
        )
        result.metrics["contradictions_found"] = len(contradictions)
        result.metrics["contradictions_carryover"] = int(pair_buckets.get("node_carryover", 0))
        result.data["contradiction_findings"] = [
            {
                "text_a": c.get("text_a", ""),
                "text_b": c.get("text_b", ""),
                "reason": c.get("explanation", ""),
            }
            for c in contradictions[:25]
        ]
        return result

    if subtask == "contradictions_resolve":
        r = ops.resolve_contradictions_with_opus(
            graph,
            metrics,
            dry_run=ctx.dry_run,
            max_items=max_items,
            llm_timeout_seconds=llm_timeout_seconds,
        )
        result.metrics["contradictions_resolved"] = int(r.get("resolved", 0))
        result.metrics["contradictions_false_positive"] = int(r.get("false_positive", 0))
        result.metrics["contradictions_merged"] = int(r.get("merged", 0))
        result.metrics["contradictions_resolve_carryover"] = int(r.get("carryover", 0))
        if r.get("decisions"):
            result.data["contradiction_decisions"] = list(r["decisions"][:50])
        return result

    if subtask == "decay":
        stale = ops.find_stale_memories_optimized(graph, metrics)
        r = ops.apply_decay_optimized(stale, graph, metrics, dry_run=ctx.dry_run)
        result.metrics["stale_found"] = len(stale)
        result.metrics["memories_decayed"] = int(r.get("decayed", 0))
        result.metrics["memories_deleted_by_decay"] = int(r.get("deleted", 0))
        result.metrics["decay_queued"] = int(r.get("queued", 0))
        return result

    if subtask == "decay_review":
        r = ops.review_decayed_memories(
            graph,
            metrics,
            dry_run=ctx.dry_run,
            max_items=max_items,
            llm_timeout_seconds=llm_timeout_seconds,
        )
        result.metrics["decay_reviewed"] = int(r.get("reviewed", 0))
        result.metrics["decay_review_deleted"] = int(r.get("deleted", 0))
        result.metrics["decay_review_extended"] = int(r.get("extended", 0))
        result.metrics["decay_review_pinned"] = int(r.get("pinned", 0))
        result.metrics["decay_review_carryover"] = int(r.get("carryover", 0))
        return result

    result.errors.append(f"Unknown memory_graph_maintenance subtask: {subtask}")
    return result


def run_memory_graph_maintenance(ctx, result_factory):
    retry_logs = []
    attempts = len(_DATASTORE_BUSY_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        result = result_factory()
        for line in retry_logs:
            _append_result_log(result, line)
        try:
            return _run_memory_graph_maintenance_once(ctx, result)
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy_or_locked(exc) or attempt >= attempts:
                result.errors.append(
                    f"Memory graph maintenance failed ({exc.__class__.__name__}): {exc}"
                )
                return result
            delay = _DATASTORE_BUSY_RETRY_DELAYS_SECONDS[attempt - 1]
            retry_logs.append(
                "Memory graph maintenance database busy; "
                f"retrying in {delay:.2f}s (attempt {attempt}/{attempts}): {exc}"
            )
            time.sleep(delay)
        except Exception as exc:
            if _is_recoverable_memory_maintenance_error(exc):
                return _record_memory_maintenance_error(result, exc)
            if _fail_hard_enabled():
                raise
            return _record_memory_maintenance_error(result, exc)
    return result


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register unified memory-graph maintenance routine."""
    registry.register(
        "memory_graph_maintenance",
        lambda ctx: run_memory_graph_maintenance(ctx, result_factory),
    )
