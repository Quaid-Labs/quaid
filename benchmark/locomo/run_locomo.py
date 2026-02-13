#!/usr/bin/env python3
"""
LoCoMo benchmark runner — end-to-end evaluation of Quaid against the LoCoMo dataset.

Orchestrates: download → ingest → query → score → report

This runs Quaid against the same 10-conversation, 1986-QA dataset that Mem0 (66.9%),
Zep (65.99%), LangMem (58.1%), and OpenAI (52.9%) benchmarked against, providing
apples-to-apples comparison numbers using the same metrics.

Usage:
    source memory-stress-test/test.env

    # Quick smoke test: 1 conversation
    python3 memory-stress-test/runner/locomo/run_locomo.py --conversations 0

    # Full run: all 10 conversations
    python3 memory-stress-test/runner/locomo/run_locomo.py --conversations all

    # Use cached extractions (extract once, evaluate many)
    python3 memory-stress-test/runner/locomo/run_locomo.py --conversations all --cached

    # Skip LLM-Judge (faster, cheaper — Token F1 + BLEU only)
    python3 memory-stress-test/runner/locomo/run_locomo.py --conversations all --no-judge

    # Different answer model
    python3 memory-stress-test/runner/locomo/run_locomo.py --conversations 0 --answer-model opus
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

from locomo.dataset import load_dataset, get_dataset_stats
from locomo.ingest import ingest_all, parse_conv_range
from locomo.evaluate import evaluate_all
from locomo.metrics import (
    score_results,
    format_comparison_table,
    score_results_multi_trial,
    format_comparison_table_with_ci,
)
from llm_clients import get_token_usage, estimate_cost, reset_token_usage


PRODUCTION_DB = os.path.join(os.environ.get("QUAID_WORKSPACE", "."), "data", "memory.db")


def load_cached_predictions(results_dir: Path, conv_indices: List[int], conversations) -> List[dict]:
    """Load cached evaluation predictions from a prior run.

    Reads per-conversation evaluation_results.json files and returns a flat list
    of all QA result dicts (with predictions). Used by --rejudge mode.
    """
    all_items = []
    for idx in conv_indices:
        if idx >= len(conversations):
            continue
        conv = conversations[idx]
        eval_file = results_dir / conv.sample_id / "evaluation_results.json"
        if not eval_file.exists():
            print(f"  WARNING: No cached eval for {conv.sample_id}")
            continue
        with open(eval_file) as f:
            data = json.load(f)
        items = data.get("results", [])
        all_items.extend(items)
        print(f"  Loaded {len(items)} cached predictions from {conv.sample_id}")
    return all_items


def run_judge_comparison(results_dir: Path, judge_model: str, conv_indices: List[int]) -> int:
    """Re-judge cached results with both lenient and neutral prompts.

    Loads predictions from the existing locomo_results.json and runs
    the LLM judge with both prompt styles on a sample for comparison.
    """
    from locomo.metrics import llm_judge, _mean, CATEGORY_NAMES

    # Load cached results
    # Try fullstack first, then standard results dir
    for suffix in ["results-fullstack", "results"]:
        candidate = results_dir.parent / suffix / "locomo_results.json"
        if candidate.exists():
            results_file = candidate
            break
    else:
        results_file = results_dir / "locomo_results.json"

    if not results_file.exists():
        print(f"No cached results found at {results_file}")
        print("Run a full benchmark first.")
        return 1

    print(f"\nJudge Comparison — Lenient vs Neutral Prompts")
    print(f"  Source: {results_file}")
    print(f"  Model: {judge_model}")
    print(f"  Conversations: {conv_indices}")
    print(f"{'=' * 70}")

    with open(results_file) as f:
        data = json.load(f)

    # Collect QA pairs from selected conversations
    qa_pairs = []
    for conv in data["evaluation"]["conversations"]:
        conv_id = conv["conversation_id"]
        # Filter by conversation index if specified
        conv_num = int(conv_id.split("-")[1]) if "-" in conv_id else -1
        # Map conv IDs to indices (conv-26=0, conv-30=1, etc.)
        conv_id_map = {c["conversation_id"]: i for i, c in enumerate(data["evaluation"]["conversations"])}
        conv_idx = conv_id_map.get(conv_id, -1)
        if conv_indices and conv_idx not in conv_indices:
            continue

        for r in conv["results"]:
            if r.get("ground_truth") and r.get("prediction"):
                qa_pairs.append({
                    "conversation": conv_id,
                    "question": r["question"],
                    "ground_truth": r["ground_truth"],
                    "prediction": r["prediction"],
                    "category": r.get("category_name", "unknown"),
                    "original_label": r.get("judge_label", "unknown"),
                })

    print(f"  QA pairs to judge: {len(qa_pairs)}")

    # Run both prompts
    results = {"lenient": [], "neutral": []}

    for i, qa in enumerate(qa_pairs):
        if (i + 1) % 25 == 0:
            print(f"  Progress: {i+1}/{len(qa_pairs)}")

        for style in ["lenient", "neutral"]:
            label, score = llm_judge(
                qa["question"], qa["ground_truth"], qa["prediction"],
                model=judge_model, prompt_style=style,
            )
            results[style].append({
                "label": label,
                "score": score,
                "category": qa["category"],
            })

    # Compute scores
    print(f"\n{'=' * 70}")
    print(f"Judge Comparison Results")
    print(f"{'=' * 70}")

    categories = sorted(set(qa["category"] for qa in qa_pairs))

    # Overall
    for style in ["lenient", "neutral"]:
        scores = [r["score"] for r in results[style] if r["label"] != "TIMEOUT"]
        pct = _mean(scores) * 100 if scores else 0
        timeouts = sum(1 for r in results[style] if r["label"] == "TIMEOUT")
        print(f"\n  {style.upper()} prompt: {pct:.1f}% ({len(scores)} scored, {timeouts} timeouts)")

    # Per-category
    print(f"\n  Per-Category:")
    print(f"  {'Category':<15} {'Lenient':>10} {'Neutral':>10} {'Delta':>10}")
    print(f"  {'-' * 45}")

    for cat in categories:
        lenient_scores = [r["score"] for r in results["lenient"]
                         if r["category"] == cat and r["label"] != "TIMEOUT"]
        neutral_scores = [r["score"] for r in results["neutral"]
                         if r["category"] == cat and r["label"] != "TIMEOUT"]
        l_pct = _mean(lenient_scores) * 100
        n_pct = _mean(neutral_scores) * 100
        delta = l_pct - n_pct
        print(f"  {cat:<15} {l_pct:>9.1f}% {n_pct:>9.1f}% {delta:>+9.1f}%")

    # Overall delta
    l_all = [r["score"] for r in results["lenient"] if r["label"] != "TIMEOUT"]
    n_all = [r["score"] for r in results["neutral"] if r["label"] != "TIMEOUT"]
    l_overall = _mean(l_all) * 100
    n_overall = _mean(n_all) * 100
    delta_overall = l_overall - n_overall

    print(f"  {'OVERALL':<15} {l_overall:>9.1f}% {n_overall:>9.1f}% {delta_overall:>+9.1f}%")

    # Disagreements
    disagree = sum(1 for l, n in zip(results["lenient"], results["neutral"])
                   if l["label"] != n["label"] and l["label"] != "TIMEOUT" and n["label"] != "TIMEOUT")
    total_judged = sum(1 for l, n in zip(results["lenient"], results["neutral"])
                       if l["label"] != "TIMEOUT" and n["label"] != "TIMEOUT")
    print(f"\n  Disagreements: {disagree}/{total_judged} ({disagree/total_judged*100:.1f}%)")

    interpretation = (
        "Numbers are CONSERVATIVE (lenient boosts less than expected)"
        if delta_overall <= 2.0
        else f"Lenient prompt inflates by ~{delta_overall:.1f}% — neutral score is more comparable to Mem0"
    )
    print(f"\n  Interpretation: {interpretation}")

    # Save comparison results
    comparison_file = results_file.parent / "judge_comparison.json"
    comparison_data = {
        "model": judge_model,
        "total_qa_pairs": len(qa_pairs),
        "conversations": conv_indices,
        "overall": {"lenient": l_overall, "neutral": n_overall, "delta": delta_overall},
        "per_category": {
            cat: {
                "lenient": _mean([r["score"] for r in results["lenient"]
                                  if r["category"] == cat and r["label"] != "TIMEOUT"]) * 100,
                "neutral": _mean([r["score"] for r in results["neutral"]
                                  if r["category"] == cat and r["label"] != "TIMEOUT"]) * 100,
            }
            for cat in categories
        },
        "disagreements": disagree,
        "disagreement_rate": disagree / total_judged if total_judged else 0,
    }
    with open(comparison_file, "w") as f:
        json.dump(comparison_data, f, indent=2)
    print(f"\n  Saved to {comparison_file}")

    print(f"{'=' * 70}")
    return 0


def safety_check() -> bool:
    """Verify we're not pointing at the production database."""
    db_path = os.environ.get("MEMORY_DB_PATH", "")
    if db_path and os.path.abspath(db_path) == os.path.abspath(PRODUCTION_DB):
        print("FATAL: MEMORY_DB_PATH points to production database!")
        return False
    return True


def run_locomo(
    conv_indices: List[int],
    results_dir: Path,
    use_cache: bool = False,
    answer_model: str = "haiku",
    judge_model: str = "haiku",
    run_judge: bool = True,
    top_k: int = 10,
    janitor_tasks: str = "embeddings",
    skip_ingest: bool = False,
    num_trials: int = 1,
    judge_prompt_style: str = "mem0",
    full_context: bool = False,
    include_journal: bool = False,
) -> dict:
    """Run the full LoCoMo benchmark pipeline.

    Args:
        conv_indices: Which conversations to process (0-9)
        results_dir: Output directory for all results
        use_cache: Use cached extraction results
        answer_model: LLM for answer generation
        judge_model: LLM for judge evaluation
        run_judge: Whether to run LLM-Judge (saves API cost if False)
        top_k: Number of memories to retrieve per question
        janitor_tasks: Janitor tasks after each session
        skip_ingest: Skip ingestion (use existing DBs from prior run)
        num_trials: Number of independent judge trials (default 1, Mem0 uses 10)
        include_journal: If True, inject journal entries alongside SPEAKERS.md in context
        judge_prompt_style: Judge prompt style ("mem0" for peer review)
        full_context: Use full conversation as context (baseline, no memory system)

    Returns:
        Dict with complete results
    """
    t_start = time.monotonic()
    results_dir.mkdir(parents=True, exist_ok=True)
    reset_token_usage()  # Clean counters for accurate per-run tracking

    # Raise cost cap for benchmark runs — full LoCoMo needs ~$30-40
    # (default $5 cap silently halts extraction mid-run)
    if not os.environ.get("JANITOR_COST_CAP"):
        os.environ["JANITOR_COST_CAP"] = "50.0"

    # ── Phase 1: Download + Parse ──────────────────────────────────
    print("\n" + "=" * 80)
    print("Phase 1: Loading LoCoMo Dataset")
    print("=" * 80)

    conversations = load_dataset()
    stats = get_dataset_stats(conversations)
    print(f"  Loaded {stats['conversations']} conversations, "
          f"{stats['total_turns']} turns, {stats['scored_qa']} scored QA pairs")

    # ── Phase 2: Ingest ────────────────────────────────────────────
    ingest_result = None
    if full_context:
        print("\n  Full-context baseline mode — skipping ingestion (no memory DB needed)")
    elif not skip_ingest:
        print("\n" + "=" * 80)
        print("Phase 2: Ingesting Conversations (Opus extraction → store → janitor)")
        print(f"  Conversations: {conv_indices}")
        print(f"  Cached mode: {use_cache}")
        print(f"  Janitor tasks: {janitor_tasks}")
        print("=" * 80)

        ingest_result = ingest_all(
            conversations=conversations,
            results_dir=results_dir,
            conv_indices=conv_indices,
            use_cache=use_cache,
            janitor_tasks=janitor_tasks,
        )

        gt = ingest_result["grand_totals"]
        print(f"\n  Ingestion complete: {gt['facts_stored']} facts stored, "
              f"{gt['edges_created']} edges, "
              f"{gt.get('snippets_extracted', 0)} snippets, "
              f"{gt.get('journal_entries_written', 0)} journal entries, "
              f"{gt['elapsed_seconds']}s")
    else:
        print("\n  Skipping ingestion (using existing DBs)")
        # Validate at least one DB exists
        found_any = False
        for idx in conv_indices:
            if idx < len(conversations):
                db_path = results_dir / conversations[idx].sample_id / "memory.db"
                if db_path.exists():
                    found_any = True
                    break
        if not found_any:
            print("  ERROR: No ingested DBs found. Run without --skip-ingest first.")
            return {"error": "no_dbs_found"}

    # ── Phase 3: Evaluate ──────────────────────────────────────────
    print("\n" + "=" * 80)
    if full_context:
        print("Phase 3: Evaluating QA Pairs (FULL-CONTEXT BASELINE — no memory system)")
    else:
        print("Phase 3: Evaluating QA Pairs (recall → generate answer)")
    print(f"  Answer model: {answer_model}")
    if not full_context:
        print(f"  Top-k: {top_k}")
    else:
        print(f"  Mode: Full conversation transcript in context (upper bound baseline)")
    print("=" * 80)

    if full_context:
        from locomo.evaluate import evaluate_all_fullcontext
        eval_result = evaluate_all_fullcontext(
            conversations=conversations,
            results_dir=results_dir,
            conv_indices=conv_indices,
            answer_model=answer_model,
        )
    else:
        eval_result = evaluate_all(
            conversations=conversations,
            results_dir=results_dir,
            conv_indices=conv_indices,
            answer_model=answer_model,
            top_k=top_k,
            include_journal=include_journal,
        )

    print(f"\n  Evaluated {eval_result['total_questions']} questions")

    # ── Phase 4: Score ─────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("Phase 4: Computing Metrics")
    if run_judge:
        print(f"  LLM-Judge model: {judge_model}")
        if num_trials > 1:
            print(f"  Trials: {num_trials} independent judge runs (matching Mem0 methodology)")
    else:
        print("  LLM-Judge: SKIPPED (--no-judge)")
    print("=" * 80)

    # Collect all results across conversations
    all_eval_items = []
    for conv_result in eval_result["conversations"]:
        all_eval_items.extend(conv_result["results"])

    if run_judge and num_trials > 1:
        scores = score_results_multi_trial(
            eval_results=all_eval_items,
            num_trials=num_trials,
            judge_model=judge_model,
            judge_prompt_style=judge_prompt_style,
        )
        table = format_comparison_table_with_ci(scores)
    else:
        scores = score_results(
            eval_results=all_eval_items,
            run_llm_judge=run_judge,
            judge_model=judge_model,
            judge_prompt_style=judge_prompt_style,
        )
        table = format_comparison_table(scores)
    print(table)

    # ── Save Final Results ─────────────────────────────────────────
    elapsed = round(time.monotonic() - t_start, 1)

    # Aggregate full-stack metrics across conversations
    snippet_stats = {"total_extracted": 0, "folded": 0, "rewritten": 0, "discarded": 0}
    journal_stats = {"entries_written": 0, "distillations_run": 0, "additions": 0, "edits": 0}
    workspace_stats = {"files_checked": 0, "over_limit": 0, "total_lines": 0}
    core_markdown_stats = {"conversations_with_core_md": 0, "total_lines": 0}

    if ingest_result:
        gt = ingest_result["grand_totals"]
        snippet_stats["total_extracted"] = gt.get("snippets_extracted", 0)
        journal_stats["entries_written"] = gt.get("journal_entries_written", 0)

        for conv_r in ingest_result.get("conversations", []):
            # Aggregate review stats
            for review in conv_r.get("review_stats", []):
                s = review.get("snippets", {})
                snippet_stats["folded"] += s.get("folded", 0)
                snippet_stats["rewritten"] += s.get("rewritten", 0)
                snippet_stats["discarded"] += s.get("discarded", 0)
                j = review.get("journal", {})
                if j:
                    journal_stats["distillations_run"] += 1
                    journal_stats["additions"] += j.get("additions", 0)
                    journal_stats["edits"] += j.get("edits", 0)
                w = review.get("workspace", {})
                if w:
                    workspace_stats["files_checked"] += w.get("files_checked", 0)
                    workspace_stats["over_limit"] += w.get("over_limit", 0)
                    workspace_stats["total_lines"] += w.get("total_lines", 0)

            # Core markdown stats from ingestion
            cm = conv_r.get("core_markdown_stats", {})
            if cm:
                core_markdown_stats["conversations_with_core_md"] += 1
                core_markdown_stats["total_lines"] += sum(cm.values())

    # Add eval-time core markdown stats
    for conv_eval in eval_result.get("conversations", []):
        if conv_eval.get("core_markdown_used"):
            core_markdown_stats["eval_conversations_using_core_md"] = (
                core_markdown_stats.get("eval_conversations_using_core_md", 0) + 1
            )

    # Capture token usage from llm_clients
    token_usage = get_token_usage()
    est_cost = estimate_cost()

    # Aggregate per-question context token estimates
    total_context_tokens = 0
    for conv_eval in eval_result.get("conversations", []):
        for r in conv_eval.get("results", []):
            total_context_tokens += r.get("context_tokens_est", 0)

    print(f"\n  Token usage: {token_usage['input_tokens']:,} input, "
          f"{token_usage['output_tokens']:,} output, "
          f"{token_usage['cache_read_tokens']:,} cache reads")
    print(f"  Estimated cost: ${est_cost:.4f}")
    if total_context_tokens > 0:
        avg_context = total_context_tokens / max(eval_result.get("total_questions", 1), 1)
        print(f"  Avg context per query: ~{int(avg_context)} tokens")

    final_result = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "conversations": conv_indices,
            "answer_model": answer_model,
            "judge_model": judge_model if run_judge else None,
            "judge_prompt_style": judge_prompt_style if run_judge else None,
            "num_trials": num_trials if run_judge else None,
            "top_k": top_k,
            "use_cache": use_cache,
            "janitor_tasks": janitor_tasks,
            "full_context_baseline": full_context,
            "total_elapsed_seconds": elapsed,
        },
        "token_usage": {
            "input_tokens": token_usage["input_tokens"],
            "output_tokens": token_usage["output_tokens"],
            "cache_read_tokens": token_usage["cache_read_tokens"],
            "cache_creation_tokens": token_usage["cache_creation_tokens"],
            "api_calls": token_usage["api_calls"],
            "estimated_cost_usd": est_cost,
            "avg_context_tokens_per_query": int(total_context_tokens / max(eval_result.get("total_questions", 1), 1)) if total_context_tokens else 0,
        },
        "dataset_stats": stats,
        "scores": scores,
        "full_stack_stats": {
            "snippet_stats": snippet_stats,
            "journal_stats": journal_stats,
            "workspace_stats": workspace_stats,
            "core_markdown_stats": core_markdown_stats,
        },
        "evaluation": eval_result,
    }
    if ingest_result:
        final_result["ingestion"] = ingest_result

    results_file = results_dir / "locomo_results.json"
    with open(results_file, "w") as f:
        json.dump(final_result, f, indent=2)
    print(f"\n  Results saved to {results_file}")

    # Save comparison table as text
    table_file = results_dir / "comparison_table.txt"
    with open(table_file, "w") as f:
        f.write(table)
    print(f"  Comparison table saved to {table_file}")

    print(f"\n  Total time: {elapsed}s")

    return final_result


def main():
    parser = argparse.ArgumentParser(
        description="Run Quaid against LoCoMo benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick smoke test (1 conversation, ~$3-5)
  python3 run_locomo.py --conversations 0

  # Full benchmark (all 10 conversations, ~$30-40)
  python3 run_locomo.py --conversations all

  # Cheap run: cached extraction + no judge (~$5)
  python3 run_locomo.py --conversations all --cached --no-judge

  # Rerun evaluation only (skip ingestion)
  python3 run_locomo.py --conversations all --skip-ingest
        """,
    )
    parser.add_argument(
        "--conversations", default="0",
        help="Which conversations: '0', '0-2', '0,3,5', 'all' (default: 0)"
    )
    parser.add_argument(
        "--cached", action="store_true",
        help="Use cached extraction results when available"
    )
    parser.add_argument(
        "--answer-model", choices=["haiku", "opus"], default="haiku",
        help="Model for answer generation (default: haiku)"
    )
    parser.add_argument(
        "--judge-model", choices=["haiku", "opus", "gpt-4o-mini"], default="gpt-4o-mini",
        help="Model for LLM-Judge evaluation (default: gpt-4o-mini, matching Mem0 methodology)"
    )
    parser.add_argument(
        "--no-judge", action="store_true",
        help="Skip LLM-Judge (only compute Token F1 + BLEU-1)"
    )
    parser.add_argument(
        "--top-k", type=int, default=10,
        help="Number of memories to retrieve per question (default: 10)"
    )
    parser.add_argument(
        "--janitor-tasks", default="embeddings",
        help="Janitor tasks after each session (default: embeddings)"
    )
    parser.add_argument(
        "--skip-ingest", action="store_true",
        help="Skip ingestion phase (reuse existing DBs from prior run)"
    )
    parser.add_argument(
        "--judge-prompt", choices=["mem0", "lenient", "neutral"], default="mem0",
        help="Judge prompt style: mem0 (canonical, matches Mem0 exactly), lenient, or neutral"
    )
    parser.add_argument(
        "--judge-comparison", action="store_true",
        help="Run judge comparison: re-judge cached results with both lenient and neutral prompts"
    )
    parser.add_argument(
        "--trials", type=int, default=1,
        help="Number of independent judge trials (default: 1, Mem0 uses 10 for published results)"
    )
    parser.add_argument(
        "--full-context", action="store_true",
        help="Full-context baseline: dump entire conversation into prompt (no memory system)"
    )
    parser.add_argument(
        "--with-journal", action="store_true",
        help="Include journal entries in context alongside SPEAKERS.md (A/B test)"
    )
    parser.add_argument(
        "--rejudge", action="store_true",
        help="Re-judge cached predictions only (skip ingest + eval, just re-score)"
    )
    parser.add_argument(
        "--results-dir",
        default=str(_DIR / "data" / "results"),
        help="Output directory for results"
    )
    args = parser.parse_args()

    if not safety_check():
        return 1

    conv_indices = parse_conv_range(args.conversations)
    results_dir = Path(args.results_dir)

    # ── Judge Comparison Mode ─────────────────────────────────────
    if args.judge_comparison:
        return run_judge_comparison(results_dir, args.judge_model, conv_indices)

    # ── Re-Judge Mode (skip ingest + eval, just re-score cached predictions) ──
    if args.rejudge:
        print(f"\nLoCoMo Re-Judge Mode")
        print(f"  Loading cached predictions from: {results_dir}")
        print(f"  Judge model: {args.judge_model}")
        print(f"  Judge prompt: {args.judge_prompt}")
        if args.trials > 1:
            print(f"  Trials: {args.trials} independent judge runs")

        conversations = load_dataset()
        all_items = load_cached_predictions(results_dir, conv_indices, conversations)
        if not all_items:
            print("ERROR: No cached predictions found. Run a full benchmark first.")
            return 1
        print(f"  Total QA pairs: {len(all_items)}")

        if args.trials > 1:
            scores = score_results_multi_trial(
                eval_results=all_items,
                num_trials=args.trials,
                judge_model=args.judge_model,
                judge_prompt_style=args.judge_prompt,
            )
            table = format_comparison_table_with_ci(scores)
        else:
            scores = score_results(
                eval_results=all_items,
                run_llm_judge=not args.no_judge,
                judge_model=args.judge_model,
                judge_prompt_style=args.judge_prompt,
            )
            table = format_comparison_table(scores)

        print(table)

        # Save results
        suffix = f"rejudge_{args.judge_model}_{args.judge_prompt}"
        if args.trials > 1:
            suffix += f"_{args.trials}trials"
        rejudge_file = results_dir / f"locomo_results_{suffix}.json"
        rejudge_data = {
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "mode": "rejudge",
                "conversations": conv_indices,
                "judge_model": args.judge_model,
                "judge_prompt_style": args.judge_prompt,
                "num_trials": args.trials,
                "source_results_dir": str(results_dir),
            },
            "scores": scores,
        }
        with open(rejudge_file, "w") as f:
            json.dump(rejudge_data, f, indent=2)
        print(f"\n  Saved to {rejudge_file}")

        table_file = results_dir / f"comparison_table_{suffix}.txt"
        with open(table_file, "w") as f:
            f.write(table)
        print(f"  Table saved to {table_file}")

        # Final verdict
        overall = scores["overall"]
        judge_val = overall.get("llm_judge", overall.get("llm_judge_mean", 0))
        std_val = overall.get("llm_judge_std")
        print(f"\n{'='*60}")
        print(f"RE-JUDGE RESULTS ({args.judge_model}, {args.judge_prompt} prompt)")
        print(f"  Token F1:   {overall.get('token_f1', 0):.1f}%")
        print(f"  BLEU-1:     {overall.get('bleu1', 0):.1f}%")
        if std_val and std_val > 0:
            print(f"  LLM-Judge:  {judge_val:.2f}% ± {std_val:.2f} ({args.trials} trials)")
        else:
            print(f"  LLM-Judge:  {judge_val:.1f}%")
        ci = overall.get("confidence_intervals", {})
        if ci:
            w95 = ci.get("wilson_95", (0, 0))
            print(f"  95% CI:     [{w95[0]:.1f}%, {w95[1]:.1f}%] (Wilson)")
        mem0 = 66.9
        delta = judge_val - mem0
        print(f"  vs Mem0:    {'+' if delta >= 0 else ''}{delta:.1f}% "
              f"({'AHEAD' if delta >= 0 else 'BEHIND'})")
        print(f"{'='*60}")
        return 0

    print(f"\nLoCoMo Benchmark Runner")
    print(f"  Conversations: {conv_indices}")
    print(f"  Cached: {args.cached}")
    print(f"  Answer model: {args.answer_model}")
    print(f"  LLM-Judge: {'skip' if args.no_judge else args.judge_model}")
    print(f"  Judge prompt: {args.judge_prompt}")
    if args.trials > 1:
        print(f"  Trials: {args.trials} independent judge runs")
    if args.full_context:
        print(f"  Mode: FULL-CONTEXT BASELINE (no memory system)")
    if args.with_journal:
        print(f"  Context: SPEAKERS.md + journal entries (A/B test)")
    print(f"  Results: {results_dir}")

    result = run_locomo(
        conv_indices=conv_indices,
        results_dir=results_dir,
        use_cache=args.cached,
        answer_model=args.answer_model,
        judge_model=args.judge_model,
        run_judge=not args.no_judge,
        top_k=args.top_k,
        janitor_tasks=args.janitor_tasks,
        skip_ingest=args.skip_ingest,
        num_trials=args.trials,
        judge_prompt_style=args.judge_prompt,
        full_context=args.full_context,
        include_journal=args.with_journal,
    )

    # Print final verdict
    overall = result["scores"]["overall"]
    fss = result.get("full_stack_stats", {})
    print(f"\n{'='*60}")
    mode_label = "FULL-CONTEXT BASELINE" if args.full_context else "FINAL RESULTS"
    print(f"{mode_label}")
    print(f"  Token F1:   {overall.get('token_f1', 0):.1f}%")
    print(f"  BLEU-1:     {overall.get('bleu1', 0):.1f}%")
    if "llm_judge" in overall:
        judge_val = overall["llm_judge"]
        std_val = overall.get("llm_judge_std")
        if std_val is not None and std_val > 0:
            print(f"  LLM-Judge:  {judge_val:.2f}% ± {std_val:.2f} ({args.trials} trials)")
        else:
            print(f"  LLM-Judge:  {judge_val:.1f}%")
        if overall.get("judge_timeouts"):
            print(f"  Timeouts:   {overall['judge_timeouts']} (excluded from score)")
        ci = overall.get("confidence_intervals", {})
        if ci:
            w95 = ci.get("wilson_95", (0, 0))
            print(f"  95% CI:     [{w95[0]:.1f}%, {w95[1]:.1f}%] (Wilson)")
        mem0 = 66.9
        delta = judge_val - mem0
        print(f"  vs Mem0:    {'+' if delta >= 0 else ''}{delta:.1f}% "
              f"({'AHEAD' if delta >= 0 else 'BEHIND'})")
    if fss:
        ss = fss.get("snippet_stats", {})
        js = fss.get("journal_stats", {})
        cms = fss.get("core_markdown_stats", {})
        print(f"\n  Full-Stack:")
        print(f"    Snippets: {ss.get('total_extracted', 0)} extracted, "
              f"{ss.get('folded', 0)} folded, {ss.get('discarded', 0)} discarded")
        print(f"    Journal:  {js.get('entries_written', 0)} entries, "
              f"{js.get('distillations_run', 0)} distillations, "
              f"{js.get('additions', 0)} additions")
        print(f"    Core MD:  {cms.get('total_lines', 0)} lines across "
              f"{cms.get('conversations_with_core_md', 0)} conversations")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
