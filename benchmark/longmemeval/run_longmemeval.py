#!/usr/bin/env python3
"""
LongMemEval benchmark runner — end-to-end evaluation of Quaid.

Orchestrates: download → ingest → recall → answer → judge → report

This runs Quaid against the LongMemEval dataset (ICLR 2025): 500 QA pairs,
7 question types, 5 memory abilities, ~48 sessions per entry (S variant).

Usage:
    source memory-stress-test/test.env
    export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2)

    # Smoke test: 5 entries with Haiku extraction + Haiku answers
    python3 memory-stress-test/runner/longmemeval/run_longmemeval.py \\
        --entries 0-4 --extract-model haiku --answer-model haiku

    # Full run: all 500 entries
    python3 memory-stress-test/runner/longmemeval/run_longmemeval.py \\
        --entries all --extract-model haiku --answer-model haiku

    # Skip ingestion (reuse existing DBs)
    python3 memory-stress-test/runner/longmemeval/run_longmemeval.py \\
        --entries all --skip-ingest --answer-model opus

    # Full-context baseline (no memory system)
    python3 memory-stress-test/runner/longmemeval/run_longmemeval.py \\
        --entries 0-4 --full-context --answer-model haiku
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Path setup
_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _DIR.parent
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
if str(_RUNNER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR.parent))

import runner  # noqa: F401

from longmemeval.dataset import load_dataset, get_dataset_stats
from longmemeval.ingest import ingest_all
from longmemeval.evaluate import evaluate_all
from longmemeval.metrics import compute_metrics, format_results_table
from llm_clients import get_token_usage, estimate_cost, reset_token_usage


PRODUCTION_DB = os.path.join(os.environ.get("QUAID_WORKSPACE", "."), "data", "memory.db")


def parse_entry_range(spec: str, max_entries: int = 500) -> List[int]:
    """Parse entry range specification.

    Examples: '0', '0-4', '0,5,10', 'all'
    """
    if spec.lower() == "all":
        return list(range(max_entries))

    indices = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-")
            indices.extend(range(int(start), int(end) + 1))
        else:
            indices.append(int(part))

    return sorted(set(indices))


def safety_check() -> bool:
    """Verify we're not pointing at the production database."""
    db_path = os.environ.get("MEMORY_DB_PATH", "")
    if db_path and os.path.abspath(db_path) == os.path.abspath(PRODUCTION_DB):
        print("FATAL: MEMORY_DB_PATH points to production database!")
        return False
    return True


def run_longmemeval(
    entry_indices: List[int],
    results_dir: Path,
    extract_model: str = "haiku",
    answer_model: str = "haiku",
    top_k: int = 10,
    skip_ingest: bool = False,
    full_context: bool = False,
    use_cache: bool = True,
) -> dict:
    """Run the full LongMemEval benchmark pipeline.

    Args:
        entry_indices: Which entries to process (0-499)
        results_dir: Output directory
        extract_model: Model for fact extraction ("haiku" or "opus")
        answer_model: Model for answer generation ("haiku" or "opus")
        top_k: Number of memories to retrieve per question
        skip_ingest: Skip ingestion (reuse existing DBs)
        full_context: Use full-context baseline
        use_cache: Use cached extraction results

    Returns:
        Dict with complete results
    """
    t_start = time.monotonic()
    results_dir.mkdir(parents=True, exist_ok=True)
    reset_token_usage()

    # Raise cost cap for benchmark runs
    if not os.environ.get("JANITOR_COST_CAP"):
        os.environ["JANITOR_COST_CAP"] = "200.0"

    # ── Phase 1: Download + Parse ──
    print("\n" + "=" * 80)
    print("Phase 1: Loading LongMemEval Dataset (S variant, cleaned)")
    print("=" * 80)

    entries = load_dataset()
    stats = get_dataset_stats(entries)
    print(f"  Loaded {stats['total_entries']} entries, "
          f"{stats['unique_session_ids']} unique sessions, "
          f"{stats['total_turns']} total turns")
    print(f"  Question types: {stats['question_types']}")

    # Clamp indices to dataset size
    entry_indices = [i for i in entry_indices if 0 <= i < len(entries)]
    print(f"  Processing entries: {len(entry_indices)}")

    # ── Phase 2: Ingest ──
    ingest_result = None
    if full_context:
        print("\n  Full-context baseline mode — skipping ingestion")
    elif not skip_ingest:
        print("\n" + "=" * 80)
        print(f"Phase 2: Ingesting Entries ({extract_model} extraction)")
        print(f"  Entries: {len(entry_indices)}")
        print(f"  Cached: {use_cache}")
        print("=" * 80)

        ingest_result = ingest_all(
            entries=entries,
            results_dir=results_dir,
            entry_indices=entry_indices,
            use_cache=use_cache,
            extract_model=extract_model,
        )

        gt = ingest_result["grand_totals"]
        print(f"\n  Ingestion complete: {gt['facts_stored']} facts, "
              f"{gt['edges_created']} edges, {gt['elapsed_seconds']}s")
    else:
        print("\n  Skipping ingestion (using existing DBs)")

    # ── Phase 3: Evaluate ──
    print("\n" + "=" * 80)
    if full_context:
        print("Phase 3: Evaluating (FULL-CONTEXT BASELINE — no memory system)")
    else:
        print("Phase 3: Evaluating (recall → answer → GPT-4o judge)")
    print(f"  Answer model: {answer_model}")
    print(f"  Judge: GPT-4o-2024-08-06 (paper methodology)")
    if not full_context:
        print(f"  Top-k: {top_k}")
    print("=" * 80)

    eval_result = evaluate_all(
        entries=entries,
        results_dir=results_dir,
        entry_indices=entry_indices,
        answer_model=answer_model,
        top_k=top_k,
        run_judge=True,
        full_context=full_context,
    )

    print(f"\n  Evaluated {eval_result['total']} entries, "
          f"accuracy: {eval_result['overall_accuracy']:.1f}%")

    # ── Phase 4: Compute Metrics ──
    print("\n" + "=" * 80)
    print("Phase 4: Computing Metrics")
    print("=" * 80)

    metrics = compute_metrics(eval_result["results"])
    table = format_results_table(metrics, answer_model)
    print(table)

    # ── Token Usage ──
    token_usage = get_token_usage()
    est_cost = estimate_cost()
    print(f"\n  Token usage: {token_usage['input_tokens']:,} input, "
          f"{token_usage['output_tokens']:,} output, "
          f"{token_usage['cache_read_tokens']:,} cache reads")
    print(f"  Estimated cost: ${est_cost:.4f}")

    # ── Save Results ──
    elapsed = round(time.monotonic() - t_start, 1)

    final_result = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "benchmark": "LongMemEval_S_cleaned",
            "entries_evaluated": len(entry_indices),
            "extract_model": extract_model if not full_context else None,
            "answer_model": answer_model,
            "judge_model": "gpt-4o-2024-08-06",
            "top_k": top_k if not full_context else None,
            "full_context_baseline": full_context,
            "total_elapsed_seconds": elapsed,
        },
        "metrics": metrics,
        "token_usage": {
            "input_tokens": token_usage["input_tokens"],
            "output_tokens": token_usage["output_tokens"],
            "cache_read_tokens": token_usage["cache_read_tokens"],
            "cache_creation_tokens": token_usage["cache_creation_tokens"],
            "api_calls": token_usage["api_calls"],
            "estimated_cost_usd": est_cost,
        },
        "dataset_stats": stats,
        "evaluation": eval_result,
    }
    if ingest_result:
        final_result["ingestion"] = ingest_result

    results_file = results_dir / "longmemeval_results.json"
    with open(results_file, "w") as f:
        json.dump(final_result, f, indent=2)
    print(f"\n  Results saved to {results_file}")

    table_file = results_dir / "results_table.txt"
    with open(table_file, "w") as f:
        f.write(table)
    print(f"  Table saved to {table_file}")

    print(f"\n  Total time: {elapsed}s")

    return final_result


def main():
    parser = argparse.ArgumentParser(
        description="Run Quaid against LongMemEval benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Smoke test (5 entries, Haiku, ~$0.50)
  python3 run_longmemeval.py --entries 0-4 --extract-model haiku

  # Full benchmark (500 entries, Haiku extraction, ~$60)
  python3 run_longmemeval.py --entries all --extract-model haiku

  # Opus answers (skip ingestion if already done)
  python3 run_longmemeval.py --entries all --skip-ingest --answer-model opus

  # Full-context baseline
  python3 run_longmemeval.py --entries 0-4 --full-context
        """,
    )
    parser.add_argument(
        "--entries", default="0-4",
        help="Which entries: '0', '0-4', '0,5,10', 'all' (default: 0-4)"
    )
    parser.add_argument(
        "--extract-model", choices=["haiku", "opus"], default="haiku",
        help="Model for fact extraction (default: haiku)"
    )
    parser.add_argument(
        "--answer-model", choices=["haiku", "opus"], default="haiku",
        help="Model for answer generation (default: haiku)"
    )
    parser.add_argument(
        "--top-k", type=int, default=10,
        help="Number of memories to retrieve per question (default: 10)"
    )
    parser.add_argument(
        "--skip-ingest", action="store_true",
        help="Skip ingestion (reuse existing DBs from prior run)"
    )
    parser.add_argument(
        "--full-context", action="store_true",
        help="Full-context baseline: dump entire haystack into prompt"
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Disable extraction caching (re-extract everything)"
    )
    parser.add_argument(
        "--results-dir",
        default=str(_DIR / "data" / "results"),
        help="Output directory for results"
    )
    args = parser.parse_args()

    if not safety_check():
        return 1

    entry_indices = parse_entry_range(args.entries)
    results_dir = Path(args.results_dir)

    print(f"\nLongMemEval Benchmark Runner")
    print(f"  Entries: {len(entry_indices)} ({args.entries})")
    print(f"  Extract model: {args.extract_model}")
    print(f"  Answer model: {args.answer_model}")
    if args.full_context:
        print(f"  Mode: FULL-CONTEXT BASELINE (no memory system)")
    print(f"  Results: {results_dir}")

    result = run_longmemeval(
        entry_indices=entry_indices,
        results_dir=results_dir,
        extract_model=args.extract_model,
        answer_model=args.answer_model,
        top_k=args.top_k,
        skip_ingest=args.skip_ingest,
        full_context=args.full_context,
        use_cache=not args.no_cache,
    )

    # Final verdict
    metrics = result["metrics"]
    print(f"\n{'='*60}")
    mode_label = "FULL-CONTEXT BASELINE" if args.full_context else "FINAL RESULTS"
    print(f"{mode_label}")
    print(f"  Overall accuracy:       {metrics['overall_accuracy']:.1f}%")
    print(f"  Task-averaged accuracy: {metrics['task_averaged_accuracy']:.1f}%")
    ci = metrics.get("confidence_intervals", {})
    if ci:
        w95 = ci.get("wilson_95", (0, 0))
        print(f"  95% CI: [{w95[0]:.1f}%, {w95[1]:.1f}%] (Wilson)")
    token_usage = result.get("token_usage", {})
    if token_usage:
        print(f"  Cost: ${token_usage.get('estimated_cost_usd', 0):.4f}")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
