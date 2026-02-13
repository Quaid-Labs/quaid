#!/usr/bin/env python3
"""
LongMemEval evaluation pipeline — recall + answer generation + GPT-4o judge.

For each of 500 entries:
1. Switch to entry's DB
2. Call recall() with the question
3. Generate answer via LLM (Haiku or Opus)
4. Judge answer via GPT-4o (matching paper methodology exactly)

Judge methodology matches the paper's evaluate_qa.py exactly:
- Model: gpt-4o-2024-08-06
- Temperature: 0
- Max tokens: 10
- 6 distinct judge prompts by question type + abstention
- Binary scoring: 'yes' in response.lower() → correct
"""
import json
import os
import sys
import time
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

# Path setup
_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _DIR.parent
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
if str(_RUNNER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR.parent))

import runner  # noqa: F401
from memory_graph import recall

from longmemeval.dataset import LMEEntry, format_full_haystack
from longmemeval.ingest import _switch_to_db

# System prompt for answer generation
ANSWER_SYSTEM_PROMPT = "You are a helpful assistant answering questions based on your memory of past conversations with the user. Answer naturally and completely."

# Answer generation template
_ANSWER_TEMPLATE = Template("""Based on the background knowledge and retrieved memories below, answer the user's question.

If the information doesn't contain enough detail to answer directly, make your best inference from available context. If you truly cannot answer based on the available information, say so clearly.

$context

Question: $question

Answer:""")

# Full-context answer template (system prompt, cacheable per entry)
_FULLCONTEXT_SYSTEM_TEMPLATE = Template("""You are a helpful assistant answering questions based on your memory of past conversations with the user.

Below is the complete history of all your past conversations. Use this to answer the question.

If the information doesn't contain enough detail to answer directly, make your best inference. If you truly cannot answer, say so clearly.

Complete conversation history:
$transcript""")


# ── GPT-4o Judge Prompts (matching paper exactly) ─────────────────────

# Standard judge prompt (single-session-user, single-session-assistant, multi-session)
# Matches paper's evaluate_qa.py exactly
_JUDGE_STANDARD = """I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no.

Question: {question}

Correct Answer: {answer}

Model Response: {prediction}

Is the model response correct? Answer yes or no only."""

# Temporal reasoning judge (adds off-by-one tolerance)
_JUDGE_TEMPORAL = """I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct.

Question: {question}

Correct Answer: {answer}

Model Response: {prediction}

Is the model response correct? Answer yes or no only."""

# Knowledge update judge
_JUDGE_KNOWLEDGE_UPDATE = """I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.

Question: {question}

Correct Answer: {answer}

Model Response: {prediction}

Is the model response correct? Answer yes or no only."""

# Preference judge
_JUDGE_PREFERENCE = """I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.

Question: {question}

Rubric: {answer}

Model Response: {prediction}

Is the model response correct? Answer yes or no only."""

# Abstention judge
_JUDGE_ABSTENTION = """I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.

Question: {question}

Explanation: {answer}

Model Response: {prediction}

Does the model correctly identify the question as unanswerable? Answer yes or no only."""


def _get_judge_prompt(
    question_type: str,
    question: str,
    answer: str,
    prediction: str,
    is_abstention: bool,
) -> str:
    """Select and format the correct judge prompt based on question type.

    Matches the paper's evaluate_qa.py `get_anscheck_prompt()` exactly.
    """
    kwargs = {"question": question, "answer": answer, "prediction": prediction}

    if is_abstention:
        return _JUDGE_ABSTENTION.format(**kwargs)
    elif question_type == "temporal-reasoning":
        return _JUDGE_TEMPORAL.format(**kwargs)
    elif question_type == "knowledge-update":
        return _JUDGE_KNOWLEDGE_UPDATE.format(**kwargs)
    elif question_type == "single-session-preference":
        return _JUDGE_PREFERENCE.format(**kwargs)
    else:
        # single-session-user, single-session-assistant, multi-session
        return _JUDGE_STANDARD.format(**kwargs)


_openai_key_warned = False


def judge_answer(
    question_type: str,
    question: str,
    answer: str,
    prediction: str,
    is_abstention: bool,
) -> Optional[bool]:
    """Judge a single answer using GPT-4o, matching paper methodology exactly.

    Returns True if correct, False if wrong, None on error/timeout.
    Uses gpt-4o-2024-08-06, temperature=0, max_tokens=10.
    """
    global _openai_key_warned
    import urllib.request
    import urllib.error

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        if not _openai_key_warned:
            print("  ERROR: OPENAI_API_KEY not set — all GPT-4o judge calls will fail!")
            print("  Fix: export OPENAI_API_KEY=$(grep '^OPENAI_API_KEY=' .env | cut -d= -f2)")
            _openai_key_warned = True
        return None

    prompt = _get_judge_prompt(question_type, question, answer, prediction, is_abstention)

    body = json.dumps({
        "model": "gpt-4o-2024-08-06",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 10,
    }).encode()

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                response_text = data["choices"][0]["message"]["content"].strip().lower()
                return "yes" in response_text
        except urllib.error.HTTPError as e:
            if e.code in {429, 500, 502, 503, 529} and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            print(f"  [judge] GPT-4o API error: HTTP {e.code}")
            return None
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            print(f"  [judge] GPT-4o error: {e}")
            return None

    return None


def _format_memories_for_prompt(memories: List[Dict]) -> str:
    """Format retrieved memories into a string for the answer prompt."""
    if not memories:
        return "(No relevant memories found)"
    lines = []
    for i, mem in enumerate(memories, 1):
        text = mem.get("text", mem.get("name", ""))
        lines.append(f"{i}. {text}")
    return "\n".join(lines)


def evaluate_single(
    entry: LMEEntry,
    owner_id: str,
    answer_model: str = "haiku",
    top_k: int = 10,
    run_judge: bool = True,
) -> Dict[str, Any]:
    """Evaluate a single LME entry using Quaid's recall pipeline.

    Args:
        entry: The LME entry with question and answer
        owner_id: Owner ID used during ingestion
        answer_model: LLM for answer generation ("haiku" or "opus")
        top_k: Number of memories to retrieve
        run_judge: Whether to run GPT-4o judge

    Returns:
        Dict with question, prediction, judge result, timing
    """
    # Stage 1: Retrieve memories
    t0 = time.monotonic()
    memories_raw = recall(
        query=entry.question,
        limit=top_k,
        owner_id=owner_id,
        use_routing=True,
        use_aliases=True,
        use_intent=True,
        use_multi_pass=True,
    )
    recall_ms = (time.monotonic() - t0) * 1000

    # Filter out entity/person nodes
    memories = [
        m for m in memories_raw
        if m.get("category") != "person" and len(m.get("text", m.get("name", ""))) >= 10
    ]

    # Stage 2: Generate answer (no 5-6 word constraint — LME judge is tolerant)
    memories_text = _format_memories_for_prompt(memories)
    context_block = f"Retrieved memories:\n{memories_text}"

    prompt = _ANSWER_TEMPLATE.safe_substitute(
        context=context_block,
        question=entry.question,
    )

    context_tokens_est = len(prompt) // 4

    t1 = time.monotonic()
    if answer_model == "haiku":
        from llm_clients import call_low_reasoning
        raw_answer, _ = call_low_reasoning(
            prompt=prompt,
            max_tokens=500,
            system_prompt=ANSWER_SYSTEM_PROMPT,
        )
    else:
        from llm_clients import call_high_reasoning
        raw_answer, _ = call_high_reasoning(
            prompt=prompt,
            max_tokens=500,
            system_prompt=ANSWER_SYSTEM_PROMPT,
        )
    answer_ms = (time.monotonic() - t1) * 1000

    prediction = (raw_answer or "").strip()

    # Stage 3: Judge
    # Empty predictions are judged as wrong (not excluded from scoring)
    judge_correct = None
    judge_ms = 0
    if run_judge:
        if not prediction:
            judge_correct = False  # No answer = wrong
        else:
            t2 = time.monotonic()
            judge_correct = judge_answer(
                question_type=entry.question_type,
                question=entry.question,
                answer=entry.answer,
                prediction=prediction,
                is_abstention=entry.is_abstention,
            )
            judge_ms = (time.monotonic() - t2) * 1000

    return {
        "question_id": entry.question_id,
        "question_type": entry.question_type,
        "question": entry.question,
        "ground_truth": entry.answer,
        "prediction": prediction,
        "is_abstention": entry.is_abstention,
        "memory_ability": entry.memory_ability,
        "judge_correct": judge_correct,
        "num_memories_raw": len(memories_raw),
        "num_memories": len(memories),
        "retrieved_memories": [
            {
                "text": m.get("text", m.get("name", "")),
                "similarity": m.get("similarity", 0),
                "category": m.get("category", ""),
            }
            for m in memories[:5]  # Top 5 for brevity
        ],
        "recall_latency_ms": round(recall_ms, 1),
        "answer_latency_ms": round(answer_ms, 1),
        "judge_latency_ms": round(judge_ms, 1),
        "context_tokens_est": context_tokens_est,
    }


def evaluate_single_fullcontext(
    entry: LMEEntry,
    answer_model: str = "haiku",
    cached_system_prompt: Optional[str] = None,
    run_judge: bool = True,
) -> Dict[str, Any]:
    """Evaluate a single entry using full-context baseline (no memory system).

    The entire haystack is placed in the system prompt for caching.
    """
    system_prompt = cached_system_prompt or _FULLCONTEXT_SYSTEM_TEMPLATE.safe_substitute(
        transcript=format_full_haystack(entry),
    )

    prompt = f"Question: {entry.question}\n\nAnswer:"
    context_tokens_est = len(system_prompt) // 4

    t0 = time.monotonic()
    if answer_model == "haiku":
        from llm_clients import call_low_reasoning
        raw_answer, _ = call_low_reasoning(
            prompt=prompt,
            max_tokens=500,
            system_prompt=system_prompt,
        )
    else:
        from llm_clients import call_high_reasoning
        raw_answer, _ = call_high_reasoning(
            prompt=prompt,
            max_tokens=500,
            system_prompt=system_prompt,
        )
    answer_ms = (time.monotonic() - t0) * 1000

    prediction = (raw_answer or "").strip()

    # Judge — empty predictions count as wrong
    judge_correct = None
    judge_ms = 0
    if run_judge:
        if not prediction:
            judge_correct = False
        else:
            t1 = time.monotonic()
            judge_correct = judge_answer(
                question_type=entry.question_type,
                question=entry.question,
                answer=entry.answer,
                prediction=prediction,
                is_abstention=entry.is_abstention,
            )
            judge_ms = (time.monotonic() - t1) * 1000

    return {
        "question_id": entry.question_id,
        "question_type": entry.question_type,
        "question": entry.question,
        "ground_truth": entry.answer,
        "prediction": prediction,
        "is_abstention": entry.is_abstention,
        "memory_ability": entry.memory_ability,
        "judge_correct": judge_correct,
        "num_memories_raw": 0,
        "num_memories": 0,
        "retrieved_memories": [],
        "recall_latency_ms": 0,
        "answer_latency_ms": round(answer_ms, 1),
        "judge_latency_ms": round(judge_ms, 1),
        "context_tokens_est": context_tokens_est,
        "baseline_mode": "full-context",
    }


def evaluate_all(
    entries: List[LMEEntry],
    results_dir: Path,
    entry_indices: Optional[List[int]] = None,
    answer_model: str = "haiku",
    top_k: int = 10,
    run_judge: bool = True,
    full_context: bool = False,
) -> Dict[str, Any]:
    """Evaluate multiple entries.

    Args:
        entries: Full list of LME entries
        results_dir: Root results directory (expects per-entry subdirs with DBs)
        entry_indices: Which entries to evaluate (default: all)
        answer_model: LLM for answer generation
        top_k: Number of memories to retrieve
        run_judge: Whether to run GPT-4o judge
        full_context: Use full-context baseline instead of recall

    Returns:
        Dict with all results and summary stats
    """
    if entry_indices is None:
        entry_indices = list(range(len(entries)))

    all_results = []
    correct = 0
    total = 0
    errors = 0

    for i, idx in enumerate(entry_indices):
        if idx < 0 or idx >= len(entries):
            continue

        entry = entries[idx]

        if (i + 1) % 25 == 0 or i == 0:
            pct = (correct / max(total, 1)) * 100
            print(f"    [{i+1}/{len(entry_indices)}] Running accuracy: {pct:.1f}% "
                  f"({correct}/{total}) | {entry.question_type}: {entry.question[:50]}...")

        try:
            if full_context:
                r = evaluate_single_fullcontext(
                    entry=entry,
                    answer_model=answer_model,
                    run_judge=run_judge,
                )
            else:
                # Switch to entry's DB
                db_path = results_dir / entry.question_id / "memory.db"
                if not db_path.exists():
                    print(f"    WARNING: No DB for {entry.question_id}, skipping")
                    continue
                _switch_to_db(db_path, fresh=False)

                r = evaluate_single(
                    entry=entry,
                    owner_id=entry.question_id,
                    answer_model=answer_model,
                    top_k=top_k,
                    run_judge=run_judge,
                )

            all_results.append(r)
            total += 1
            if r["judge_correct"] is True:
                correct += 1
            elif r["judge_correct"] is None:
                errors += 1

        except Exception as e:
            print(f"    ERROR on {entry.question_id}: {e}")
            all_results.append({
                "question_id": entry.question_id,
                "question_type": entry.question_type,
                "question": entry.question,
                "ground_truth": entry.answer,
                "prediction": "",
                "is_abstention": entry.is_abstention,
                "memory_ability": entry.memory_ability,
                "judge_correct": None,
                "num_memories_raw": 0,
                "num_memories": 0,
                "retrieved_memories": [],
                "recall_latency_ms": 0,
                "answer_latency_ms": 0,
                "judge_latency_ms": 0,
                "context_tokens_est": 0,
                "error": str(e),
            })
            errors += 1
            total += 1

    # Save per-entry results
    eval_file = results_dir / "evaluation_results.json"
    with open(eval_file, "w") as f:
        json.dump(all_results, f, indent=2)

    accuracy = (correct / max(total, 1)) * 100

    return {
        "results": all_results,
        "total": total,
        "correct": correct,
        "errors": errors,
        "overall_accuracy": round(accuracy, 2),
        "answer_model": answer_model,
        "full_context": full_context,
    }
