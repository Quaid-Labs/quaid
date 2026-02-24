"""Unified memory-graph datastore maintenance routine.

This module is datastore-owned and registered via the lifecycle registry.
Janitor remains the orchestration layer; datastore routines own task execution.
"""

from __future__ import annotations

import importlib


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register unified memory-graph maintenance routine."""

    def _run_memory_graph_maintenance(ctx):
        result = result_factory()
        try:
            # Import via canonical module path. Root-level `janitor.py` no longer
            # exists after boundary refactors.
            jan = importlib.import_module("core.lifecycle.janitor")
            graph = ctx.graph
            subtask = str((ctx.options or {}).get("subtask") or "all").strip().lower()
            metrics = jan.JanitorMetrics()

            if subtask == "review":
                r = jan.review_pending_memories(graph, dry_run=ctx.dry_run, metrics=metrics)
                result.metrics["memories_reviewed"] = int(r.get("total_reviewed", 0))
                result.metrics["memories_deleted"] = int(r.get("deleted", 0))
                result.metrics["memories_fixed"] = int(r.get("fixed", 0))
                return result

            if subtask == "temporal":
                r = jan.resolve_temporal_references(graph, dry_run=ctx.dry_run, metrics=metrics)
                result.metrics["temporal_found"] = int(r.get("found", 0))
                result.metrics["temporal_fixed"] = int(r.get("fixed", 0))
                return result

            if subtask == "dedup_review":
                r = jan.review_dedup_rejections(graph, metrics, dry_run=ctx.dry_run)
                result.metrics["dedup_reviewed"] = int(r.get("reviewed", 0))
                result.metrics["dedup_confirmed"] = int(r.get("confirmed", 0))
                result.metrics["dedup_reversed"] = int(r.get("reversed", 0))
                return result

            if subtask == "duplicates":
                pair_buckets = jan.recall_similar_pairs(graph, metrics, since=None)
                dups = jan.find_duplicates_from_pairs(pair_buckets.get("duplicates", []), metrics)
                merges_applied = 0
                merged_ids = set()
                for dup in dups:
                    if dup["id_a"] in merged_ids or dup["id_b"] in merged_ids:
                        continue
                    suggestion = dup.get("suggestion", {})
                    if suggestion.get("action") == "merge":
                        merged_text = suggestion.get("merged_text", "")
                        if (not ctx.dry_run) and merged_text:
                            jan._merge_nodes_into(graph, merged_text, [dup["id_a"], dup["id_b"]], source="dedup_merge")
                        if merged_text:
                            merged_ids.add(dup["id_a"])
                            merged_ids.add(dup["id_b"])
                            merges_applied += 1
                result.metrics["duplicates_merged"] = merges_applied
                return result

            if subtask == "contradictions":
                pair_buckets = jan.recall_similar_pairs(graph, metrics, since=None)
                contradictions = jan.find_contradictions_from_pairs(
                    pair_buckets.get("contradictions", []), metrics, dry_run=ctx.dry_run
                )
                result.metrics["contradictions_found"] = len(contradictions)
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
                r = jan.resolve_contradictions_with_opus(graph, metrics, dry_run=ctx.dry_run)
                result.metrics["contradictions_resolved"] = int(r.get("resolved", 0))
                result.metrics["contradictions_false_positive"] = int(r.get("false_positive", 0))
                result.metrics["contradictions_merged"] = int(r.get("merged", 0))
                if r.get("decisions"):
                    result.data["contradiction_decisions"] = list(r["decisions"][:50])
                return result

            if subtask == "decay":
                stale = jan.find_stale_memories_optimized(graph, metrics)
                r = jan.apply_decay_optimized(stale, graph, metrics, dry_run=ctx.dry_run)
                result.metrics["stale_found"] = len(stale)
                result.metrics["memories_decayed"] = int(r.get("decayed", 0))
                result.metrics["memories_deleted_by_decay"] = int(r.get("deleted", 0))
                result.metrics["decay_queued"] = int(r.get("queued", 0))
                return result

            if subtask == "decay_review":
                r = jan.review_decayed_memories(graph, metrics, dry_run=ctx.dry_run)
                result.metrics["decay_reviewed"] = int(r.get("reviewed", 0))
                result.metrics["decay_review_deleted"] = int(r.get("deleted", 0))
                result.metrics["decay_review_extended"] = int(r.get("extended", 0))
                result.metrics["decay_review_pinned"] = int(r.get("pinned", 0))
                return result

            result.errors.append(f"Unknown memory_graph_maintenance subtask: {subtask}")
        except RuntimeError as exc:
            result.errors.append(str(exc))
        except Exception as exc:
            result.errors.append(f"Memory graph maintenance failed: {exc}")
        return result

    registry.register("memory_graph_maintenance", _run_memory_graph_maintenance)
