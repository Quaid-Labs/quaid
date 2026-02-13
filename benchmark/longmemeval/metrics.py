#!/usr/bin/env python3
"""
LongMemEval metrics — compute accuracy metrics matching the paper's methodology.

Paper: Wu et al. (ICLR 2025), "LongMemEval"
Methodology: Binary GPT-4o judge → per-type accuracy → overall + task-averaged

Two headline metrics:
1. Overall accuracy: mean(all binary labels) — weighted by count per type
2. Task-averaged accuracy: mean(per-type accuracies) — equal weight per type
"""
import json
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


def compute_metrics(results: List[Dict]) -> Dict[str, Any]:
    """Compute LongMemEval metrics from evaluation results.

    Matches the paper's methodology exactly:
    - Binary scoring: judge_correct True → 1, False → 0
    - judge_correct None (timeout/error) excluded from scoring
    - Overall accuracy: mean across all scored entries
    - Task-averaged: mean of per-type accuracies

    Args:
        results: List of evaluation result dicts (from evaluate.py)

    Returns:
        Dict with overall, per-type, per-ability metrics
    """
    # Group by type and ability
    type_scores: Dict[str, List[int]] = defaultdict(list)
    ability_scores: Dict[str, List[int]] = defaultdict(list)
    all_scores: List[int] = []
    abs_scores: List[int] = []

    scored = 0
    timeouts = 0

    for r in results:
        if r.get("judge_correct") is None:
            timeouts += 1
            continue

        score = 1 if r["judge_correct"] else 0
        scored += 1
        all_scores.append(score)

        qtype = r.get("question_type", "unknown")
        ability = r.get("memory_ability", "unknown")
        type_scores[qtype].append(score)
        ability_scores[ability].append(score)

        if r.get("is_abstention"):
            abs_scores.append(score)

    # Per-type accuracy
    per_type = {}
    for t, scores in sorted(type_scores.items()):
        per_type[t] = {
            "accuracy": round(_mean(scores) * 100, 2),
            "correct": sum(scores),
            "total": len(scores),
        }

    # Per-ability accuracy
    per_ability = {}
    for a, scores in sorted(ability_scores.items()):
        per_ability[a] = {
            "accuracy": round(_mean(scores) * 100, 2),
            "correct": sum(scores),
            "total": len(scores),
        }

    # Overall accuracy (weighted by count)
    overall_acc = _mean(all_scores) * 100 if all_scores else 0

    # Task-averaged accuracy (equal weight per type)
    type_accs = [_mean(scores) * 100 for scores in type_scores.values() if scores]
    task_avg_acc = _mean(type_accs) if type_accs else 0

    # Abstention accuracy
    abs_acc = _mean(abs_scores) * 100 if abs_scores else None

    # Confidence intervals (Wilson score)
    ci_95 = wilson_ci(sum(all_scores), len(all_scores), 0.95) if all_scores else (0, 0)
    ci_99 = wilson_ci(sum(all_scores), len(all_scores), 0.99) if all_scores else (0, 0)

    return {
        "overall_accuracy": round(overall_acc, 2),
        "task_averaged_accuracy": round(task_avg_acc, 2),
        "abstention_accuracy": round(abs_acc, 2) if abs_acc is not None else None,
        "per_type": per_type,
        "per_ability": per_ability,
        "scored": scored,
        "timeouts": timeouts,
        "total_entries": len(results),
        "confidence_intervals": {
            "wilson_95": (round(ci_95[0] * 100, 1), round(ci_95[1] * 100, 1)),
            "wilson_99": (round(ci_99[0] * 100, 1), round(ci_99[1] * 100, 1)),
        },
    }


def format_results_table(metrics: Dict, answer_model: str = "haiku") -> str:
    """Format metrics into a readable comparison table."""
    lines = []
    lines.append("=" * 80)
    lines.append("LongMemEval Benchmark Results")
    lines.append("=" * 80)

    lines.append(f"\nOverall accuracy:       {metrics['overall_accuracy']:.1f}%")
    lines.append(f"Task-averaged accuracy: {metrics['task_averaged_accuracy']:.1f}%")
    if metrics.get("abstention_accuracy") is not None:
        lines.append(f"Abstention accuracy:    {metrics['abstention_accuracy']:.1f}%")
    lines.append(f"Answer model:           {answer_model}")
    lines.append(f"Scored: {metrics['scored']}, Timeouts: {metrics['timeouts']}")

    ci = metrics.get("confidence_intervals", {})
    if ci:
        w95 = ci.get("wilson_95", (0, 0))
        lines.append(f"95% CI: [{w95[0]:.1f}%, {w95[1]:.1f}%] (Wilson)")

    lines.append(f"\nPer Question Type:")
    lines.append(f"  {'Type':<30} {'Accuracy':>10} {'Correct':>10} {'Total':>10}")
    lines.append(f"  {'-' * 60}")
    for t, data in sorted(metrics.get("per_type", {}).items()):
        lines.append(f"  {t:<30} {data['accuracy']:>9.1f}% {data['correct']:>10} {data['total']:>10}")

    lines.append(f"\nPer Memory Ability:")
    lines.append(f"  {'Ability':<30} {'Accuracy':>10} {'Correct':>10} {'Total':>10}")
    lines.append(f"  {'-' * 60}")
    for a, data in sorted(metrics.get("per_ability", {}).items()):
        lines.append(f"  {a:<30} {data['accuracy']:>9.1f}% {data['correct']:>10} {data['total']:>10}")

    # Comparison with known systems
    lines.append(f"\nLeaderboard Comparison (LongMemEval_S):")
    lines.append(f"  {'System':<30} {'Overall':>10}")
    lines.append(f"  {'-' * 40}")
    competitors = [
        ("Emergence AI", 86.0),
        ("Supermemory", 81.6),
        ("Zep + GPT-4o", 71.2),
        ("GPT-4o full-context", 63.8),
    ]
    # Insert Quaid in sorted position
    quaid_acc = metrics["overall_accuracy"]
    inserted = False
    for name, acc in competitors:
        if not inserted and quaid_acc >= acc:
            lines.append(f"  {'Quaid (ours)':<30} {quaid_acc:>9.1f}% <--")
            inserted = True
        lines.append(f"  {name:<30} {acc:>9.1f}%")
    if not inserted:
        lines.append(f"  {'Quaid (ours)':<30} {quaid_acc:>9.1f}% <--")

    lines.append("=" * 80)
    return "\n".join(lines)


def _mean(values: List) -> float:
    """Compute mean, returning 0 for empty lists."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def wilson_ci(successes: int, n: int, confidence: float = 0.95) -> Tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    Recommended by ICML 2025 spotlight paper for LLM evaluation.
    """
    if n == 0:
        return (0.0, 0.0)

    import math
    # z-scores for common confidence levels
    z_map = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}
    z = z_map.get(confidence, 1.96)

    p_hat = successes / n
    denom = 1 + z * z / n
    center = (p_hat + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))

    return (max(0, center - margin), min(1, center + margin))
