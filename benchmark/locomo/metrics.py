#!/usr/bin/env python3
"""
LoCoMo metrics — Token F1, BLEU-1, and LLM-Judge.

Implements the same metrics used by Mem0 for apples-to-apples comparison.

Token F1: Set-based token overlap (matching Mem0's simple_tokenize)
BLEU-1: NLTK sentence_bleu with SmoothingFunction method1, unigram weights
LLM-Judge: Binary CORRECT/WRONG judgment (Haiku, with note about GPT-4o-mini deviation)

Published baselines for reference:
    Mem0:    66.9% LLM-Judge overall
    Zep:     65.99% (Mem0's eval) / 75.14% (Zep's own eval)
    LangMem: 58.1%
    OpenAI:  52.9%
"""
import json
import math
import os
import re
import string
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _DIR.parent
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
if str(_RUNNER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR.parent))

from locomo.dataset import CATEGORY_NAMES, SCORED_CATEGORIES

# Published results for comparison table
PUBLISHED_RESULTS = {
    "Mem0": {
        "overall_judge": 66.9,
        "per_category_judge": {
            "single-hop": 67.13,
            "multi-hop": 51.15,
            "temporal": 55.51,
            "open-domain": 72.93,
        },
    },
    "Mem0^g (graph)": {
        "overall_judge": 68.9,
        "per_category_judge": {
            "single-hop": 65.71,
            "multi-hop": 47.19,
            "temporal": 58.13,
            "open-domain": 75.71,
        },
    },
    "Zep (Mem0 eval)": {
        "overall_judge": 65.99,
        "per_category_judge": {
            "single-hop": 61.70,
            "multi-hop": 41.35,
            "temporal": 49.31,
            "open-domain": 76.60,
        },
    },
    "LangMem": {"overall_judge": 58.1},
    "OpenAI": {"overall_judge": 52.9},
    "MemMachine v0.2": {"overall_judge": 91.23},
    "Backboard": {"overall_judge": 90.00},
}


# ── Token F1 ────────────────────────────────────────────────────────


def simple_tokenize(text: str) -> List[str]:
    """Tokenize text matching Mem0's simple_tokenize: lowercase, replace . , ! ? with space, split.

    Mem0's implementation only strips 4 punctuation characters (. , ! ?), NOT all of
    string.punctuation. This preserves hyphens, apostrophes, colons, etc. in tokens.
    """
    text = text.lower()
    # Match Mem0's exact punctuation handling: only these 4 characters
    text = text.replace(".", " ").replace(",", " ").replace("!", " ").replace("?", " ")
    # Split on whitespace and filter empty
    return [t for t in text.split() if t]


def token_f1(prediction: str, reference: str) -> float:
    """Compute set-based Token F1 between prediction and reference.

    Returns:
        F1 score between 0.0 and 1.0
    """
    pred_tokens = set(simple_tokenize(prediction))
    ref_tokens = set(simple_tokenize(reference))

    if not pred_tokens or not ref_tokens:
        return 0.0

    common = pred_tokens & ref_tokens
    if not common:
        return 0.0

    precision = len(common) / len(pred_tokens)
    recall = len(common) / len(ref_tokens)
    return 2 * precision * recall / (precision + recall)


# ── BLEU-1 ──────────────────────────────────────────────────────────


def bleu1(prediction: str, reference: str) -> float:
    """Compute BLEU-1 score using NLTK sentence_bleu.

    Uses SmoothingFunction method1 and unigram-only weights (1,0,0,0),
    matching Mem0's implementation.

    Returns:
        BLEU-1 score between 0.0 and 1.0
    """
    try:
        from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
        import nltk

        pred_tokens = nltk.word_tokenize(prediction.lower())
        ref_tokens = [nltk.word_tokenize(reference.lower())]

        if not pred_tokens or not ref_tokens[0]:
            return 0.0

        return sentence_bleu(
            ref_tokens,
            pred_tokens,
            weights=(1, 0, 0, 0),
            smoothing_function=SmoothingFunction().method1,
        )
    except ImportError:
        # Fallback: simple unigram precision if NLTK not available
        pred_tokens = simple_tokenize(prediction)
        ref_tokens = simple_tokenize(reference)
        if not pred_tokens or not ref_tokens:
            return 0.0
        ref_set = set(ref_tokens)
        matches = sum(1 for t in pred_tokens if t in ref_set)
        return matches / len(pred_tokens)


# ── LLM-Judge ───────────────────────────────────────────────────────

# Mem0's exact ACCURACY_PROMPT from mem0ai/mem0/evaluation/metrics/llm_judge.py
# This is the canonical judge prompt used by Mem0's published LoCoMo results (66.9%).
# Using this verbatim is required for peer-review-valid comparison.
MEM0_ACCURACY_PROMPT = """Your task is to label an answer to a question as 'CORRECT' or 'WRONG'. You will be given the following data:
 (1) a question (posed by one user to another user),
 (2) a 'gold' (ground truth) answer,
 (3) a generated answer
which you will score as CORRECT/WRONG.

The point of the question is to ask about something one user should know about the other user based on their prior conversations.
The gold answer will usually be a concise and short answer that includes the referenced topic, for example:
Question: Do you remember what I got the last time I went to Hawaii?
Gold answer: A shell necklace
The generated answer might be much longer, but you should be generous with your grading - as long as it touches on the same topic as the gold answer, it should be counted as CORRECT.

For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like "last Tuesday" or "next month"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., "May 7th" vs "7 May"), consider it CORRECT if it's the same date.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG.
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label"."""

# Legacy prompts kept for internal comparison only (not for published results)
LLM_JUDGE_PROMPT_LENIENT = """You are evaluating whether a generated answer is correct by comparing it to a gold answer.

Be LENIENT in your evaluation — as long as the generated answer touches on the same topic and conveys the same core information as the gold answer, count it as CORRECT.

Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

Respond with JSON only:
{{"label": "CORRECT"}} or {{"label": "WRONG"}}"""

LLM_JUDGE_PROMPT_NEUTRAL = """You are evaluating whether a generated answer is correct by comparing it to a gold answer.

Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

Is the generated answer correct? Respond with JSON only:
{{"label": "CORRECT"}} or {{"label": "WRONG"}}"""

JUDGE_PROMPTS = {
    "mem0": MEM0_ACCURACY_PROMPT,  # Canonical — use for published results
    "lenient": LLM_JUDGE_PROMPT_LENIENT,
    "neutral": LLM_JUDGE_PROMPT_NEUTRAL,
}

# Default for peer-review: Mem0's exact prompt
LLM_JUDGE_PROMPT = MEM0_ACCURACY_PROMPT


_openai_key_warned = False


def _judge_once_openai(prompt: str, model: str = "gpt-4o-mini") -> Optional[str]:
    """Single judge attempt via OpenAI API. Returns label or None on failure."""
    global _openai_key_warned
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        if not _openai_key_warned:
            print("  ERROR: OPENAI_API_KEY not set — all GPT judge calls will fail!")
            print("  Fix: export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2)")
            _openai_key_warned = True
        return None

    import urllib.request
    import urllib.error

    data = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},  # Match Mem0's exact call
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        raw = result["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"  [judge] OpenAI API error: {e}")
        return None

    return _parse_judge_label(raw)


def _parse_judge_label(raw: str) -> Optional[str]:
    """Extract CORRECT/WRONG label from LLM response text."""
    if not raw:
        return None
    try:
        import runner  # noqa: F401
        from llm_clients import parse_json_response
        parsed = parse_json_response(raw)
        return parsed.get("label", "WRONG").upper()
    except Exception:
        pass

    # Fallback: extract from plain text. Check for WRONG first since
    # "INCORRECT" contains "CORRECT" as a substring.
    upper = raw.upper()
    if "WRONG" in upper or "INCORRECT" in upper:
        return "WRONG"
    elif "CORRECT" in upper:
        return "CORRECT"
    return "WRONG"


def _judge_once(prompt: str, model: str) -> Optional[str]:
    """Single judge attempt. Returns label or None on failure."""
    # Route to OpenAI for GPT models
    if model.startswith("gpt-"):
        return _judge_once_openai(prompt, model)

    import runner  # noqa: F401
    from llm_clients import call_low_reasoning, call_high_reasoning

    try:
        if model == "opus":
            raw, _ = call_high_reasoning(prompt=prompt, max_tokens=100)
        else:
            raw, _ = call_low_reasoning(prompt=prompt, max_tokens=100)
    except Exception:
        return None

    return _parse_judge_label(raw)


def llm_judge(
    question: str,
    gold_answer: str,
    generated_answer: str,
    model: str = "gpt-4o-mini",
    prompt_style: str = "mem0",
) -> Tuple[str, float]:
    """Judge answer correctness via LLM with retry.

    Default: GPT-4o-mini with Mem0's exact ACCURACY_PROMPT (peer-review valid).
    For internal testing, pass model="haiku" and/or prompt_style="lenient".

    Retries once on failure. Returns "TIMEOUT" if both attempts fail
    (infrastructure failure, not a judgment).

    Args:
        question: The original question
        gold_answer: Ground truth answer
        generated_answer: Model's predicted answer
        model: "haiku" (default) or "opus"
        prompt_style: "lenient" (default, our prompt) or "neutral" (closer to Mem0)

    Returns:
        Tuple of (label, score) where label is "CORRECT"/"WRONG"/"TIMEOUT"
        and score is 1.0/0.0/0.0
    """
    template = JUDGE_PROMPTS.get(prompt_style, LLM_JUDGE_PROMPT)
    prompt = template.format(
        question=question,
        gold_answer=gold_answer,
        generated_answer=generated_answer,
    )

    # Try twice
    for attempt in range(2):
        label = _judge_once(prompt, model)
        if label is not None:
            score = 1.0 if label == "CORRECT" else 0.0
            return label, score

    # Both attempts failed — infrastructure failure
    return "TIMEOUT", 0.0


# ── Aggregate Scoring ───────────────────────────────────────────────


def score_results(
    eval_results: List[Dict],
    run_llm_judge: bool = True,
    judge_model: str = "gpt-4o-mini",
    judge_prompt_style: str = "mem0",
) -> Dict[str, Any]:
    """Score a list of evaluation results with all three metrics.

    Args:
        eval_results: List of per-question result dicts from evaluate.py
        run_llm_judge: Whether to run LLM-Judge (costs API calls)
        judge_model: Model for LLM-Judge ("gpt-4o-mini" for peer review, "haiku" for internal)
        judge_prompt_style: "mem0" (canonical, default), "lenient", or "neutral"

    Returns:
        Dict with per-category and overall scores for each metric
    """
    # Group by category
    by_category: Dict[str, List[Dict]] = {}
    for r in eval_results:
        cat = r.get("category_name", "unknown")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(r)

    # Compute metrics per category
    category_scores = {}
    all_f1 = []
    all_bleu = []
    all_judge = []
    all_timeouts = 0

    for cat_name, items in sorted(by_category.items()):
        cat_f1 = []
        cat_bleu = []
        cat_judge = []
        cat_timeouts = 0

        for i, item in enumerate(items):
            pred = item.get("prediction", "")
            ref = item.get("ground_truth", "")

            if not ref:
                continue

            # Token F1
            f1 = token_f1(pred, ref)
            cat_f1.append(f1)
            all_f1.append(f1)
            item["token_f1"] = f1

            # BLEU-1
            b1 = bleu1(pred, ref)
            cat_bleu.append(b1)
            all_bleu.append(b1)
            item["bleu1"] = b1

            # LLM-Judge
            if run_llm_judge:
                if (i + 1) % 50 == 0:
                    print(f"    [judge] {cat_name}: {i+1}/{len(items)}")
                label, score = llm_judge(
                    item["question"], ref, pred,
                    model=judge_model, prompt_style=judge_prompt_style,
                )
                item["judge_label"] = label
                item["judge_score"] = score
                if label == "TIMEOUT":
                    cat_timeouts += 1
                    all_timeouts += 1
                else:
                    cat_judge.append(score)
                    all_judge.append(score)

        category_scores[cat_name] = {
            "count": len(items),
            "token_f1": _mean(cat_f1) * 100,
            "bleu1": _mean(cat_bleu) * 100,
        }
        if cat_judge:
            category_scores[cat_name]["llm_judge"] = _mean(cat_judge) * 100
        if cat_timeouts:
            category_scores[cat_name]["judge_timeouts"] = cat_timeouts

    overall = {
        "total_questions": len(eval_results),
        "scored_questions": len(all_f1),
        "token_f1": _mean(all_f1) * 100,
        "bleu1": _mean(all_bleu) * 100,
    }
    if all_judge:
        overall["llm_judge"] = _mean(all_judge) * 100
    if all_timeouts:
        overall["judge_timeouts"] = all_timeouts
        overall["judge_scored"] = len(all_judge)

    return {
        "overall": overall,
        "per_category": category_scores,
        "judge_model": judge_model if run_llm_judge else None,
        "judge_prompt_style": judge_prompt_style if run_llm_judge else None,
        "note": (
            f"LLM-Judge uses {judge_model} with {judge_prompt_style} prompt"
            f"{' (matches Mem0 methodology)' if judge_model == 'gpt-4o-mini' and judge_prompt_style == 'mem0' else ' (deviation from Mem0)'}"
            ". Token F1 and BLEU-1 match Mem0's implementation. "
            "TIMEOUT labels excluded from judge denominator."
        ),
    }


def format_comparison_table(scores: Dict[str, Any]) -> str:
    """Format a comparison table with published results.

    Returns a formatted string showing Quaid results alongside published numbers.
    """
    lines = []
    lines.append("")
    lines.append("=" * 80)
    lines.append("LoCoMo Benchmark Results — Comparison Table")
    lines.append("=" * 80)

    # Overall LLM-Judge comparison
    lines.append("")
    lines.append("Overall LLM-Judge Accuracy:")
    lines.append("-" * 50)

    # Sort by score, add Quaid
    entries = []
    for name, data in PUBLISHED_RESULTS.items():
        entries.append((name, data.get("overall_judge", 0)))

    quaid_judge = scores.get("overall", {}).get("llm_judge")
    if quaid_judge is not None:
        entries.append(("Quaid (ours)", quaid_judge))

    entries.sort(key=lambda x: x[1], reverse=True)

    for name, score in entries:
        marker = " <--" if name == "Quaid (ours)" else ""
        lines.append(f"  {name:<25} {score:6.1f}%{marker}")

    # Per-category breakdown
    lines.append("")
    lines.append("Per-Category Breakdown (LLM-Judge %):")
    lines.append("-" * 80)

    categories = ["single-hop", "multi-hop", "temporal", "open-domain"]
    header = f"  {'System':<25}" + "".join(f"{c:>14}" for c in categories) + f"{'Overall':>14}"
    lines.append(header)
    lines.append("  " + "-" * (25 + 14 * 5))

    # Published results
    for name, data in PUBLISHED_RESULTS.items():
        cats = data.get("per_category_judge", {})
        if not cats:
            continue
        row = f"  {name:<25}"
        for cat in categories:
            val = cats.get(cat)
            row += f"{val:>13.1f}%" if val is not None else f"{'—':>14}"
        overall = data.get("overall_judge")
        row += f"{overall:>13.1f}%" if overall is not None else f"{'—':>14}"
        lines.append(row)

    # Quaid results
    quaid_cats = scores.get("per_category", {})
    row = f"  {'Quaid (ours)':<25}"
    for cat in categories:
        val = quaid_cats.get(cat, {}).get("llm_judge")
        row += f"{val:>13.1f}%" if val is not None else f"{'—':>14}"
    if quaid_judge is not None:
        row += f"{quaid_judge:>13.1f}%"
    else:
        row += f"{'—':>14}"
    lines.append(row)

    # Token F1 + BLEU overall
    lines.append("")
    lines.append("All Metrics (Quaid):")
    lines.append("-" * 50)
    overall = scores.get("overall", {})
    lines.append(f"  Token F1:  {overall.get('token_f1', 0):6.1f}%")
    lines.append(f"  BLEU-1:   {overall.get('bleu1', 0):6.1f}%")
    if quaid_judge is not None:
        lines.append(f"  LLM-Judge: {quaid_judge:6.1f}%")

    lines.append("")
    lines.append(f"  Note: {scores.get('note', '')}")
    lines.append("=" * 80)

    return "\n".join(lines)


def _mean(values: List[float]) -> float:
    """Safe mean that returns 0 for empty lists."""
    return sum(values) / len(values) if values else 0.0


def _std(values: List[float]) -> float:
    """Population standard deviation (matching Mem0's ± reporting)."""
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / len(values))


# ── Statistical Confidence Intervals ──────────────────────────────


def wilson_ci(successes: int, total: int, confidence: float = 0.95) -> Tuple[float, float]:
    """Wilson Score interval for a binomial proportion.

    Recommended by ICML 2025 spotlight paper "Don't Use the CLT in LLM Evals"
    over CLT-based intervals, especially for N < 300. Unlike CLT, Wilson intervals
    never extend outside [0, 1] and handle edge cases (0% or 100% accuracy) correctly.

    Args:
        successes: Number of CORRECT judgments
        total: Total number of judgments (excluding TIMEOUTs)
        confidence: Confidence level (default 0.95 for 95% CI)

    Returns:
        (lower, upper) bounds as proportions in [0, 1]
    """
    if total == 0:
        return (0.0, 0.0)

    # z-score for the given confidence level
    # For 95%: z ≈ 1.96, for 99%: z ≈ 2.576
    from statistics import NormalDist
    z = NormalDist().inv_cdf((1 + confidence) / 2)

    p_hat = successes / total
    denominator = 1 + z ** 2 / total
    center = (p_hat + z ** 2 / (2 * total)) / denominator
    spread = z * math.sqrt((p_hat * (1 - p_hat) + z ** 2 / (4 * total)) / total) / denominator

    return (max(0.0, center - spread), min(1.0, center + spread))


def bayesian_ci(successes: int, total: int, confidence: float = 0.95) -> Tuple[float, float]:
    """Bayesian Beta-Bernoulli credible interval with uniform prior.

    Uses Beta(1 + successes, 1 + failures) posterior with uniform Beta(1,1) prior.
    Recommended alongside Wilson by the ICML 2025 paper for small sample sizes.

    Falls back to Wilson CI if scipy is not available.

    Args:
        successes: Number of CORRECT judgments
        total: Total number of judgments (excluding TIMEOUTs)
        confidence: Credible interval level (default 0.95)

    Returns:
        (lower, upper) bounds as proportions in [0, 1]
    """
    if total == 0:
        return (0.0, 0.0)

    try:
        from scipy.stats import beta
        alpha_param = 1 + successes  # Prior: Beta(1, 1) = uniform
        beta_param = 1 + (total - successes)
        tail = (1 - confidence) / 2
        lower = beta.ppf(tail, alpha_param, beta_param)
        upper = beta.ppf(1 - tail, alpha_param, beta_param)
        return (float(lower), float(upper))
    except ImportError:
        # Fall back to Wilson if scipy not available
        return wilson_ci(successes, total, confidence)


def compute_ci_report(
    successes: int,
    total: int,
) -> Dict[str, Any]:
    """Compute both Wilson and Bayesian CIs at 95% and 99% confidence.

    Args:
        successes: Number of CORRECT judgments
        total: Total non-TIMEOUT judgments

    Returns:
        Dict with CI bounds as percentages
    """
    pct = (successes / total * 100) if total > 0 else 0.0
    w95 = wilson_ci(successes, total, 0.95)
    w99 = wilson_ci(successes, total, 0.99)
    b95 = bayesian_ci(successes, total, 0.95)
    b99 = bayesian_ci(successes, total, 0.99)
    return {
        "accuracy_pct": round(pct, 2),
        "n": total,
        "successes": successes,
        "wilson_95": (round(w95[0] * 100, 2), round(w95[1] * 100, 2)),
        "wilson_99": (round(w99[0] * 100, 2), round(w99[1] * 100, 2)),
        "bayesian_95": (round(b95[0] * 100, 2), round(b95[1] * 100, 2)),
        "bayesian_99": (round(b99[0] * 100, 2), round(b99[1] * 100, 2)),
    }


# ── Multi-Trial Scoring ───────────────────────────────────────────


def score_results_multi_trial(
    eval_results: List[Dict],
    num_trials: int = 10,
    judge_model: str = "gpt-4o-mini",
    judge_prompt_style: str = "mem0",
) -> Dict[str, Any]:
    """Score results with multiple independent judge trials for statistical rigor.

    Matches Mem0's methodology of "10 independent runs" with mean ± std reporting.
    Since answer generation uses temp=0 (deterministic), we re-run judging only.
    Each trial independently judges all questions, then we aggregate.

    Also computes Wilson and Bayesian CIs from a representative trial.

    Args:
        eval_results: List of per-question result dicts (with cached predictions)
        num_trials: Number of independent judge runs (default 10, matching Mem0)
        judge_model: Model for LLM-Judge
        judge_prompt_style: Judge prompt style

    Returns:
        Dict with per-trial scores, aggregated mean ± std, and CIs
    """
    # Group by category
    by_category: Dict[str, List[Dict]] = {}
    for r in eval_results:
        cat = r.get("category_name", "unknown")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(r)

    # Also compute Token F1 and BLEU-1 (deterministic — only need to compute once)
    all_f1 = []
    all_bleu = []
    cat_f1: Dict[str, List[float]] = {}
    cat_bleu: Dict[str, List[float]] = {}

    for cat_name, items in by_category.items():
        cat_f1[cat_name] = []
        cat_bleu[cat_name] = []
        for item in items:
            pred = item.get("prediction", "")
            ref = item.get("ground_truth", "")
            if not ref:
                continue
            f1 = token_f1(pred, ref)
            b1 = bleu1(pred, ref)
            cat_f1[cat_name].append(f1)
            cat_bleu[cat_name].append(b1)
            all_f1.append(f1)
            all_bleu.append(b1)

    # Run multiple judge trials
    trial_results = []  # List of per-trial overall scores
    trial_per_cat = {}  # cat_name -> list of per-trial scores

    for cat_name in by_category:
        trial_per_cat[cat_name] = []

    # For CI computation from first trial
    first_trial_successes = 0
    first_trial_total = 0
    first_trial_cat_successes: Dict[str, int] = {}
    first_trial_cat_total: Dict[str, int] = {}

    for trial_idx in range(num_trials):
        print(f"\n  Trial {trial_idx + 1}/{num_trials}")
        trial_scores = []  # all judge scores this trial
        trial_timeouts = 0
        cat_scores: Dict[str, List[float]] = {c: [] for c in by_category}

        for cat_name, items in sorted(by_category.items()):
            for i, item in enumerate(items):
                pred = item.get("prediction", "")
                ref = item.get("ground_truth", "")
                if not ref:
                    continue

                if (i + 1) % 100 == 0:
                    print(f"    [{cat_name}] {i+1}/{len(items)}")

                label, score = llm_judge(
                    item["question"], ref, pred,
                    model=judge_model, prompt_style=judge_prompt_style,
                )

                if label == "TIMEOUT":
                    trial_timeouts += 1
                else:
                    cat_scores[cat_name].append(score)
                    trial_scores.append(score)

                    # Track first trial for CIs
                    if trial_idx == 0:
                        first_trial_total += 1
                        first_trial_successes += int(score)
                        if cat_name not in first_trial_cat_successes:
                            first_trial_cat_successes[cat_name] = 0
                            first_trial_cat_total[cat_name] = 0
                        first_trial_cat_total[cat_name] += 1
                        first_trial_cat_successes[cat_name] += int(score)

        # Aggregate this trial
        trial_overall = _mean(trial_scores) * 100 if trial_scores else 0.0
        trial_results.append(trial_overall)
        for cat_name, scores in cat_scores.items():
            trial_per_cat[cat_name].append(_mean(scores) * 100 if scores else 0.0)

        print(f"    Overall: {trial_overall:.1f}% ({len(trial_scores)} scored, {trial_timeouts} timeouts)")

    # Aggregate across trials
    overall_mean = _mean(trial_results)
    overall_std = _std(trial_results)

    # Per-category aggregation
    category_scores = {}
    for cat_name in sorted(by_category.keys()):
        trials = trial_per_cat[cat_name]
        cat_mean = _mean(trials)
        cat_std_val = _std(trials)
        cat_ci = compute_ci_report(
            first_trial_cat_successes.get(cat_name, 0),
            first_trial_cat_total.get(cat_name, 0),
        )
        category_scores[cat_name] = {
            "count": len(by_category[cat_name]),
            "token_f1": _mean(cat_f1.get(cat_name, [])) * 100,
            "bleu1": _mean(cat_bleu.get(cat_name, [])) * 100,
            "llm_judge_mean": round(cat_mean, 2),
            "llm_judge_std": round(cat_std_val, 2),
            "llm_judge_trials": [round(t, 2) for t in trials],
            "confidence_intervals": cat_ci,
        }

    # Overall CIs
    overall_ci = compute_ci_report(first_trial_successes, first_trial_total)

    overall = {
        "total_questions": len(eval_results),
        "scored_questions": len(all_f1),
        "token_f1": _mean(all_f1) * 100,
        "bleu1": _mean(all_bleu) * 100,
        "llm_judge": round(overall_mean, 2),
        "llm_judge_mean": round(overall_mean, 2),
        "llm_judge_std": round(overall_std, 2),
        "llm_judge_trials": [round(t, 2) for t in trial_results],
        "confidence_intervals": overall_ci,
    }

    methodology_match = judge_model == "gpt-4o-mini" and judge_prompt_style == "mem0"
    note = (
        f"LLM-Judge: {num_trials} independent trials, {judge_model} with {judge_prompt_style} prompt, temp=0.0"
        f"{' (matches Mem0 methodology)' if methodology_match else ' (deviation from Mem0)'}. "
        f"Reporting mean ± std across trials. "
        f"Wilson and Bayesian 95% CIs from first trial. "
        f"Token F1 and BLEU-1 are deterministic (computed once). "
        f"TIMEOUT labels excluded from judge denominator. "
        f"Overall score uses weighted mean by question count (single-hop: {sum(1 for r in eval_results if r.get('category_name') == 'single-hop')}/1540)."
    )

    return {
        "overall": overall,
        "per_category": category_scores,
        "judge_model": judge_model,
        "judge_prompt_style": judge_prompt_style,
        "num_trials": num_trials,
        "note": note,
    }


def format_comparison_table_with_ci(scores: Dict[str, Any]) -> str:
    """Format comparison table including confidence intervals and trial statistics.

    Enhanced version of format_comparison_table() for multi-trial results.
    """
    lines = []
    lines.append("")
    lines.append("=" * 90)
    lines.append("LoCoMo Benchmark Results — Comparison Table (with Statistical Rigor)")
    lines.append("=" * 90)

    # Overall LLM-Judge comparison
    lines.append("")
    lines.append("Overall LLM-Judge Accuracy:")
    lines.append("-" * 60)

    entries = []
    for name, data in PUBLISHED_RESULTS.items():
        entries.append((name, data.get("overall_judge", 0), None))

    overall = scores.get("overall", {})
    quaid_judge = overall.get("llm_judge")
    quaid_std = overall.get("llm_judge_std")
    if quaid_judge is not None:
        entries.append(("Quaid (ours)", quaid_judge, quaid_std))

    entries.sort(key=lambda x: x[1], reverse=True)

    for name, score, std in entries:
        marker = " <--" if name == "Quaid (ours)" else ""
        if std is not None:
            lines.append(f"  {name:<25} {score:6.2f}% ± {std:.2f}{marker}")
        else:
            lines.append(f"  {name:<25} {score:6.1f}%{marker}")

    # Per-category with std
    lines.append("")
    lines.append("Per-Category Breakdown (LLM-Judge %, mean ± std):")
    lines.append("-" * 90)

    categories = ["single-hop", "multi-hop", "temporal", "open-domain"]
    header = f"  {'System':<25}" + "".join(f"{c:>16}" for c in categories) + f"{'Overall':>16}"
    lines.append(header)
    lines.append("  " + "-" * (25 + 16 * 5))

    # Published (with std where available from Mem0)
    MEM0_STD = {
        "Mem0": {"single-hop": 0.65, "multi-hop": 0.31, "temporal": 0.34, "open-domain": 0.11},
        "Zep (Mem0 eval)": {"single-hop": 0.32, "multi-hop": 0.48, "temporal": 0.50, "open-domain": 0.13},
    }
    for name, data in PUBLISHED_RESULTS.items():
        cats = data.get("per_category_judge", {})
        if not cats:
            continue
        row = f"  {name:<25}"
        std_data = MEM0_STD.get(name, {})
        for cat in categories:
            val = cats.get(cat)
            std = std_data.get(cat)
            if val is not None and std:
                row += f"{val:>10.1f}±{std:.1f}%"
            elif val is not None:
                row += f"{val:>14.1f}%"
            else:
                row += f"{'—':>16}"
        o = data.get("overall_judge")
        row += f"{o:>14.1f}%" if o is not None else f"{'—':>16}"
        lines.append(row)

    # Quaid row
    quaid_cats = scores.get("per_category", {})
    row = f"  {'Quaid (ours)':<25}"
    for cat in categories:
        val = quaid_cats.get(cat, {}).get("llm_judge_mean")
        std = quaid_cats.get(cat, {}).get("llm_judge_std")
        if val is not None and std is not None:
            row += f"{val:>10.1f}±{std:.1f}%"
        elif val is not None:
            row += f"{val:>14.1f}%"
        else:
            row += f"{'—':>16}"
    if quaid_judge is not None and quaid_std is not None:
        row += f"{quaid_judge:>10.1f}±{quaid_std:.1f}%"
    elif quaid_judge is not None:
        row += f"{quaid_judge:>14.1f}%"
    else:
        row += f"{'—':>16}"
    lines.append(row)

    # All metrics
    lines.append("")
    lines.append("All Metrics (Quaid):")
    lines.append("-" * 60)
    lines.append(f"  Token F1:  {overall.get('token_f1', 0):6.1f}%")
    lines.append(f"  BLEU-1:   {overall.get('bleu1', 0):6.1f}%")
    if quaid_judge is not None:
        lines.append(f"  LLM-Judge: {quaid_judge:6.2f}% ± {quaid_std:.2f}")

    # Confidence intervals
    ci = overall.get("confidence_intervals", {})
    if ci:
        lines.append("")
        lines.append("Confidence Intervals (overall LLM-Judge):")
        lines.append("-" * 60)
        w95 = ci.get("wilson_95", (0, 0))
        b95 = ci.get("bayesian_95", (0, 0))
        w99 = ci.get("wilson_99", (0, 0))
        lines.append(f"  Wilson  95% CI: [{w95[0]:.1f}%, {w95[1]:.1f}%]")
        lines.append(f"  Bayesian 95% CI: [{b95[0]:.1f}%, {b95[1]:.1f}%]")
        lines.append(f"  Wilson  99% CI: [{w99[0]:.1f}%, {w99[1]:.1f}%]")
        lines.append(f"  N = {ci.get('n', 0)} scored questions")

    # Trial variance
    num_trials = scores.get("num_trials", 0)
    if num_trials > 1:
        lines.append("")
        lines.append(f"Trial Variance ({num_trials} independent judge runs):")
        lines.append("-" * 60)
        trials = overall.get("llm_judge_trials", [])
        if trials:
            lines.append(f"  Trials: {', '.join(f'{t:.1f}%' for t in trials)}")
            lines.append(f"  Range:  {min(trials):.1f}% – {max(trials):.1f}%")
            lines.append(f"  Mean:   {_mean(trials):.2f}% ± {_std(trials):.2f}")

    lines.append("")
    note = scores.get("note", "")
    if note:
        lines.append(f"  Note: {note}")
    lines.append("=" * 90)

    return "\n".join(lines)
