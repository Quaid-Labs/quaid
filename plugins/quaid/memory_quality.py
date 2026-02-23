"""Datastore-owned memory quality maintenance routines."""

from __future__ import annotations

import hashlib
from typing import Any

from llm_clients import call_deep_reasoning, parse_json_response
from memory_graph import (
    get_recent_dedup_rejections,
    resolve_dedup_review,
    store,
    content_hash,
)


def _chunked(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _default_owner_id(cfg: Any) -> str:
    try:
        return str(cfg.users.default_owner)
    except Exception:
        return "default"


def register_lifecycle_routines(registry, result_factory) -> None:
    """Register datastore quality-review lifecycle routines."""

    def _run_memory_dedup_review(ctx):
        result = result_factory()
        graph = ctx.graph
        if graph is None:
            result.errors.append("Memory dedup review requires graph in RoutineContext")
            return result
        try:
            with graph._get_conn() as conn:
                auto_confirmed = conn.execute(
                    """
                    UPDATE dedup_log
                    SET review_status = 'confirmed',
                        review_resolution = 'auto-confirmed: exact content hash match',
                        reviewed_at = datetime('now')
                    WHERE review_status = 'unreviewed'
                      AND decision = 'hash_exact'
                    """
                ).rowcount
            if auto_confirmed:
                result.logs.append(f"Auto-confirmed {auto_confirmed} hash-exact dedup entries")

            pending = get_recent_dedup_rejections(hours=24, limit=50)
            if not pending:
                result.logs.append("No unreviewed dedup rejections found")
                result.metrics["dedup_reviewed"] = int(auto_confirmed or 0)
                result.metrics["dedup_confirmed"] = int(auto_confirmed or 0)
                result.metrics["dedup_reversed"] = 0
                return result

            reviewed = int(auto_confirmed or 0)
            confirmed = int(auto_confirmed or 0)
            reversed_count = 0
            for batch in _chunked(pending, 12):
                numbered = []
                for i, entry in enumerate(batch, 1):
                    numbered.append(
                        f'{i}. Log ID: {entry["id"]}\n'
                        f'   New text: "{entry["new_text"]}"\n'
                        f'   Existing text: "{entry["existing_text"]}"\n'
                        f'   Similarity: {entry["similarity"]:.3f}\n'
                        f'   Decision: {entry["decision"]}\n'
                        f'   LLM reasoning: {entry.get("llm_reasoning", "N/A")}'
                    )

                prompt = f"""You are reviewing {len(batch)} dedup rejections in a personal knowledge base.
Most rejections are correct. Use:
- CONFIRM when entries are the same fact reworded.
- REVERSE only when they are genuinely different facts that were wrongly blocked.

{chr(10).join(numbered)}

Return JSON array:
[
  {{"item": 1, "action": "CONFIRM", "reason": "why"}},
  {{"item": 2, "action": "REVERSE", "reason": "why"}}
]
JSON only."""

                prompt_tag = f"[prompt:{hashlib.sha256(prompt.encode('utf-8')).hexdigest()[:10]}] "
                response, _duration = call_deep_reasoning(prompt, max_tokens=200 * len(batch))
                if not response:
                    result.errors.append("Dedup review batch failed: no response")
                    continue
                parsed = parse_json_response(response)
                if not isinstance(parsed, list):
                    result.errors.append("Dedup review batch failed: invalid JSON")
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

                    if action == "CONFIRM":
                        if not ctx.dry_run:
                            resolve_dedup_review(entry["id"], "confirmed", reason)
                        confirmed += 1
                        reviewed += 1
                    elif action == "REVERSE":
                        if not ctx.dry_run:
                            resolve_dedup_review(entry["id"], "reversed", reason)
                            text_hash = content_hash(entry["new_text"])
                            owner_id = entry.get("owner_id", _default_owner_id(ctx.cfg))
                            with graph._get_conn() as conn:
                                alive = conn.execute(
                                    """
                                    SELECT id FROM nodes WHERE content_hash = ?
                                      AND deleted_at IS NULL
                                      AND status IN ('approved', 'pending', 'active', 'flagged')
                                      AND (owner_id = ? OR owner_id IS NULL)
                                    LIMIT 1
                                    """,
                                    (text_hash, owner_id),
                                ).fetchone()
                            if not alive:
                                store(
                                    entry["new_text"],
                                    owner_id=owner_id,
                                    source=entry.get("source"),
                                    skip_dedup=True,
                                    status="approved",
                                )
                        reversed_count += 1
                        reviewed += 1

            result.metrics["dedup_reviewed"] = reviewed
            result.metrics["dedup_confirmed"] = confirmed
            result.metrics["dedup_reversed"] = reversed_count
        except Exception as exc:
            result.errors.append(f"Memory dedup review failed: {exc}")
        return result

    registry.register("memory_dedup_review", _run_memory_dedup_review)
