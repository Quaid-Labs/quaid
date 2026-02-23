"""Datastore-owned memory decay maintenance routines.

This module owns stale-memory discovery and decay application logic for the
memory datastore. Janitor should orchestrate via lifecycle registry only.
"""

from __future__ import annotations

import math
import sys
import hashlib
from datetime import datetime, timedelta
from typing import Any, Dict, List

from lib.archive import archive_node as _archive_node
from llm_clients import call_deep_reasoning, parse_json_response
from memory_graph import (
    queue_for_decay_review,
    hard_delete_node,
    get_pending_decay_reviews,
    resolve_decay_review,
)


def _ebbinghaus_retention(
    days_since_access: float,
    access_count: int,
    verified: bool,
    cfg: Any,
    storage_strength: float = 0.0,
) -> float:
    """Compute Ebbinghaus retention factor R = 2^(-t/half_life)."""
    base_hl = float(cfg.base_half_life_days)
    bonus = float(cfg.access_bonus_factor)

    half_life = base_hl * (1.0 + bonus * access_count)
    # Bjork: storage strength extends half-life (up to +50% per unit)
    half_life *= (1.0 + 0.5 * storage_strength)
    if verified:
        half_life *= 2.0

    if half_life <= 0:
        return 0.0
    return math.pow(2, -days_since_access / half_life)


def _find_stale_memories(graph, decay_cfg: Any) -> List[Dict[str, Any]]:
    threshold_days = int(decay_cfg.threshold_days)
    cutoff = (datetime.now() - timedelta(days=threshold_days)).isoformat()
    stale: List[Dict[str, Any]] = []
    with graph._get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM nodes
            WHERE accessed_at < ?
            AND confidence > 0.1
            AND pinned = 0
            AND status IN ('approved', 'active')
            ORDER BY accessed_at ASC
            """,
            (cutoff,),
        ).fetchall()

    for row in rows:
        node = graph._row_to_node(row)
        stale.append(
            {
                "id": node.id,
                "text": node.name,
                "confidence": node.confidence,
                "last_accessed": node.accessed_at,
                "access_count": node.access_count,
                "storage_strength": node.storage_strength,
                "extraction_confidence": node.extraction_confidence,
                "verified": node.verified,
                "owner_id": node.owner_id,
                "created_at": node.created_at,
                "type": node.type,
                "speaker": node.speaker,
            }
        )
    return stale


def _apply_decay(stale: List[Dict[str, Any]], graph, decay_cfg: Any, dry_run: bool) -> Dict[str, int]:
    deleted = 0
    decayed = 0
    queued = 0

    review_queue_enabled = bool(decay_cfg.review_queue_enabled)
    use_exponential = str(decay_cfg.mode or "exponential") == "exponential"
    min_conf = float(decay_cfg.minimum_confidence)
    linear_decay = float(decay_cfg.rate_percent) / 100.0

    for mem in stale:
        is_verified = bool(mem.get("verified", False))
        if use_exponential:
            last_accessed = mem.get("last_accessed", "")
            try:
                accessed_dt = datetime.fromisoformat(str(last_accessed))
            except Exception:
                accessed_dt = datetime.now() - timedelta(days=int(decay_cfg.threshold_days) + 1)
            days_elapsed = (datetime.now() - accessed_dt).total_seconds() / 86400.0
            retention = _ebbinghaus_retention(
                days_elapsed,
                int(mem.get("access_count", 0)),
                is_verified,
                decay_cfg,
                storage_strength=float(mem.get("storage_strength", 0.0)),
            )
            baseline = mem.get("extraction_confidence") or mem["confidence"]
            new_confidence = max(min_conf, float(baseline) * retention)
        else:
            decay_rate = linear_decay * 0.5 if is_verified else linear_decay
            new_confidence = max(min_conf, float(mem["confidence"]) - decay_rate)

        if new_confidence <= min_conf:
            if review_queue_enabled:
                if not dry_run:
                    queue_for_decay_review(mem)
                    queued += 1
            else:
                if not dry_run:
                    if _archive_node(mem, "confidence_decay"):
                        hard_delete_node(mem["id"])
                        deleted += 1
                    else:
                        print(f"  SKIPPED delete (archive failed): {mem['text'][:50]}...", file=sys.stderr)
        else:
            if not dry_run:
                with graph._get_conn() as conn:
                    conn.execute(
                        "UPDATE nodes SET confidence = ? WHERE id = ?",
                        (new_confidence, mem["id"]),
                    )
                decayed += 1

    return {"decayed": decayed, "deleted": deleted, "queued": queued}


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register datastore decay lifecycle routine."""

    def _run_memory_decay(ctx):
        result = result_factory()
        graph = ctx.graph
        if graph is None:
            result.errors.append("Memory decay requires graph in RoutineContext")
            return result

        try:
            decay_cfg = ctx.cfg.decay
            stale = _find_stale_memories(graph, decay_cfg)
            result.metrics["stale_found"] = len(stale)
            result.logs.append(
                f"Found {len(stale)} stale memories (>{int(decay_cfg.threshold_days)} days unused)"
            )
            if stale:
                decay_result = _apply_decay(stale, graph, decay_cfg, dry_run=ctx.dry_run)
                result.metrics["memories_decayed"] = int(decay_result["decayed"])
                result.metrics["memories_deleted_by_decay"] = int(decay_result["deleted"])
                result.metrics["decay_queued"] = int(decay_result["queued"])
            else:
                result.metrics["memories_decayed"] = 0
                result.metrics["memories_deleted_by_decay"] = 0
                result.metrics["decay_queued"] = 0
        except Exception as exc:
            result.errors.append(f"Memory decay failed: {exc}")
        return result

    registry.register("memory_decay", _run_memory_decay)

    def _run_memory_decay_review(ctx):
        result = result_factory()
        graph = ctx.graph
        if graph is None:
            result.errors.append("Memory decay review requires graph in RoutineContext")
            return result
        try:
            pending = get_pending_decay_reviews(limit=50)
            if not pending:
                result.logs.append("No pending decay reviews found")
                result.metrics["decay_reviewed"] = 0
                result.metrics["decay_review_deleted"] = 0
                result.metrics["decay_review_extended"] = 0
                result.metrics["decay_review_pinned"] = 0
                return result

            owner = "the user"
            try:
                default = ctx.cfg.users.default_owner
                identity = ctx.cfg.users.identities.get(default)
                if identity and identity.person_node_name:
                    owner = identity.person_node_name.split()[0]
            except Exception:
                pass

            reviewed = deleted = extended = pinned = 0
            batch_size = 12
            for i in range(0, len(pending), batch_size):
                batch = pending[i:i + batch_size]
                numbered = []
                for j, entry in enumerate(batch, 1):
                    numbered.append(
                        f'{j}. Queue ID: {entry["id"]}\n'
                        f'   Text: "{entry["node_text"]}"\n'
                        f'   Type: {entry.get("node_type", "unknown")}\n'
                        f'   Confidence: {entry["confidence_at_queue"]:.2f}\n'
                        f'   Access count: {entry.get("access_count", 0)}\n'
                        f'   Last accessed: {entry.get("last_accessed", "unknown")}\n'
                        f'   Verified: {"yes" if entry.get("verified") else "no"}'
                    )

                prompt = f"""You are reviewing {len(batch)} memories that reached confidence floor in {owner}'s knowledge base.
For each memory choose exactly one action:
- DELETE: outdated/noisy facts, or infra/system facts that belong in docs not memory
- EXTEND: still true but not recently relevant
- PIN: core identity or persistent preferences

{chr(10).join(numbered)}

Return JSON array in order:
[
  {{"item": 1, "action": "DELETE", "reason": "why"}},
  {{"item": 2, "action": "EXTEND", "reason": "why"}},
  {{"item": 3, "action": "PIN", "reason": "why"}}
]
JSON only."""

                prompt_tag = f"[prompt:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:10]}] "
                response, _duration = call_deep_reasoning(prompt, max_tokens=200 * len(batch))
                if not response:
                    result.errors.append(f"Decay review batch {i // batch_size + 1} failed: no response")
                    continue
                parsed = parse_json_response(response)
                if not isinstance(parsed, list):
                    result.errors.append(f"Decay review batch {i // batch_size + 1} failed: invalid JSON")
                    continue

                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    idx = item.get("item")
                    if not isinstance(idx, int) or idx < 1 or idx > len(batch):
                        continue
                    entry = batch[idx - 1]
                    action = str(item.get("action", "")).upper()
                    reason = prompt_tag + str(item.get("reason", "")).strip()
                    node_id = entry["node_id"]
                    node = graph.get_node(node_id)

                    if action == "DELETE":
                        if not ctx.dry_run:
                            resolve_decay_review(entry["id"], "delete", reason)
                            archived = _archive_node(
                                {
                                    "id": node_id,
                                    "type": entry.get("node_type"),
                                    "name": entry.get("node_text"),
                                    "confidence": entry.get("confidence_at_queue"),
                                    "access_count": entry.get("access_count", 0),
                                    "accessed_at": entry.get("last_accessed"),
                                    "created_at": entry.get("created_at_node"),
                                },
                                "decay_review_delete",
                            )
                            if archived:
                                hard_delete_node(node_id)
                            else:
                                continue
                        deleted += 1
                        reviewed += 1
                    elif action == "EXTEND":
                        if not ctx.dry_run:
                            resolve_decay_review(entry["id"], "extend", reason)
                            ext_conf = (node.attributes or {}).get("extraction_confidence", 0.3) if node else 0.3
                            extend_conf = max(0.3, float(ext_conf) * 0.5) if ext_conf else 0.3
                            with graph._get_conn() as conn:
                                conn.execute(
                                    "UPDATE nodes SET confidence = ?, accessed_at = ? WHERE id = ?",
                                    (extend_conf, datetime.now().isoformat(), node_id),
                                )
                        extended += 1
                        reviewed += 1
                    elif action == "PIN":
                        if not ctx.dry_run:
                            resolve_decay_review(entry["id"], "pin", reason)
                            ext_conf = (node.attributes or {}).get("extraction_confidence", 0.7) if node else 0.7
                            pin_conf = max(0.7, float(ext_conf)) if ext_conf else 0.7
                            with graph._get_conn() as conn:
                                conn.execute(
                                    "UPDATE nodes SET pinned = 1, confidence = ? WHERE id = ?",
                                    (pin_conf, node_id),
                                )
                        pinned += 1
                        reviewed += 1

            result.metrics["decay_reviewed"] = reviewed
            result.metrics["decay_review_deleted"] = deleted
            result.metrics["decay_review_extended"] = extended
            result.metrics["decay_review_pinned"] = pinned
        except Exception as exc:
            result.errors.append(f"Memory decay review failed: {exc}")
        return result

    registry.register("memory_decay_review", _run_memory_decay_review)
