#!/usr/bin/env python3
"""
LoCoMo query and answer generation — evaluate Quaid against LoCoMo QA pairs.

For each QA pair (categories 1-4):
1. Call recall() with the question (top_k=10)
2. Format retrieved memories as context
3. Generate short answer via LLM
4. Store: question, ground_truth, prediction, retrieved_memories, latency

Matches Mem0's methodology:
- Short answers (5-6 words)
- Categories 1-4 only (skip adversarial)
- LLM generates answer from retrieved context

Usage:
    source memory-stress-test/test.env
    python3 -m locomo.evaluate --conversation 0 --answer-model haiku
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Path setup
_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _DIR.parent
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
if str(_RUNNER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR.parent))

import runner  # noqa: F401
from memory_graph import recall, get_graph

from locomo.dataset import (
    Conversation,
    QAPair,
    CATEGORY_NAMES,
    SCORED_CATEGORIES,
    load_dataset,
)
from locomo.ingest import _switch_to_db

# Answer generation system prompt — override llm_clients' default "Respond with JSON only"
ANSWER_SYSTEM_PROMPT = "You are a concise question-answering assistant. Answer in plain text, not JSON."

# Answer generation prompt — adapted from Mem0's methodology.
# Key: answers must be short (5-6 words) for Token F1 and BLEU metrics to work.
# Uses string.Template to avoid .format() crashes on curly braces in memories/questions.
from string import Template as _Template

_ANSWER_TEMPLATE = _Template("""You are a helpful assistant answering questions about conversations between $speaker_a and $speaker_b.

Based on the background knowledge and retrieved memories below, answer the question. If the information doesn't contain enough detail to answer directly, make your best inference from available context. Only say "I don't know" if nothing is relevant to the question.

IMPORTANT RULES:
- Keep your answer to 5-6 words maximum
- Be direct and specific
- Convert relative time references to specific dates when possible
- If information conflicts, prioritize the most recent
- Do NOT add explanations or caveats

$context

Question: $question

Answer (5-6 words max):""")


def _load_core_markdown(conv_dir: Path, include_journal: bool = False) -> str:
    """Load accumulated core markdown from the conversation directory.

    Reads SPEAKERS.md, skipping if it's still just the initial template
    (3 lines or fewer). Optionally appends journal entries for A/B testing.

    Args:
        conv_dir: Path to conversation directory
        include_journal: If True, also load journal/*.journal.md files

    Returns the markdown content, or empty string if nothing meaningful.
    """
    parts = []

    speakers_path = conv_dir / "SPEAKERS.md"
    if speakers_path.exists():
        content = speakers_path.read_text(encoding="utf-8").strip()
        # Skip if still just the initial template (header + blank + placeholder)
        if content.count("\n") > 3:
            parts.append(content)

    if include_journal:
        journal_dir = conv_dir / "journal"
        if journal_dir.exists():
            # Current (undistilled) journal entries
            for jf in sorted(journal_dir.glob("*.journal.md")):
                jcontent = jf.read_text(encoding="utf-8").strip()
                if jcontent and jcontent.count("\n") > 1:
                    parts.append(f"\n---\n## Journal: {jf.stem}\n{jcontent}")
            # Archived (distilled) journal entries — after full janitor,
            # entries move to archive/. Include these for the A/B test.
            archive_dir = journal_dir / "archive"
            if archive_dir.exists():
                for af in sorted(archive_dir.glob("*.md")):
                    acontent = af.read_text(encoding="utf-8").strip()
                    if acontent and acontent.count("\n") > 1:
                        parts.append(f"\n---\n## Journal Archive: {af.stem}\n{acontent}")

    return "\n\n".join(parts) if parts else ""


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
    qa: QAPair,
    conversation: Conversation,
    owner_id: str,
    answer_model: str = "haiku",
    top_k: int = 10,
    core_context: str = "",
    recall_kwargs: Optional[Dict[str, Any]] = None,
    answer_template: Optional[_Template] = None,
) -> Dict[str, Any]:
    """Evaluate a single QA pair.

    Args:
        qa: The QA pair to evaluate
        conversation: Parent conversation (for speaker names)
        owner_id: Owner ID used during ingestion
        answer_model: LLM for answer generation ("haiku" or "opus")
        top_k: Number of memories to retrieve
        core_context: Pre-loaded core markdown content (cached per conversation)
        recall_kwargs: Optional overrides for recall() parameters (for A/B testing)
        answer_template: Optional override for answer generation prompt template

    Returns:
        Dict with question, ground_truth, prediction, memories, timing
    """
    # Stage 1: Retrieve memories
    t0 = time.monotonic()
    # Recall with overrides
    recall_kw = {
        "use_routing": True,
        "use_aliases": True,
        "use_intent": True,
        "use_multi_pass": True,
    }
    if recall_kwargs:
        recall_kw.update(recall_kwargs)

    memories_raw = recall(
        query=qa.question,
        limit=top_k,
        owner_id=owner_id,
        **recall_kw,
    )
    recall_ms = (time.monotonic() - t0) * 1000

    # Filter out entity/person nodes that waste recall slots.
    # Minimum 10 chars (not 30 — short facts like "Emma is single" are valid)
    memories = [m for m in memories_raw if m.get("category") != "person" and len(m.get("text", m.get("name", ""))) >= 10]

    # Stage 2: Generate answer
    memories_text = _format_memories_for_prompt(memories)

    # Build context block: core markdown (if available) + retrieved memories
    if core_context:
        context_block = (
            f"Background knowledge:\n{core_context}\n\n"
            f"Retrieved memories:\n{memories_text}"
        )
    else:
        context_block = f"Retrieved memories:\n{memories_text}"

    # Use custom template if provided, otherwise use default
    template = answer_template if answer_template is not None else _ANSWER_TEMPLATE
    prompt = template.safe_substitute(
        speaker_a=conversation.speaker_a,
        speaker_b=conversation.speaker_b,
        context=context_block,
        question=qa.question,
    )

    t1 = time.monotonic()
    if answer_model == "haiku":
        from llm_clients import call_low_reasoning
        raw_answer, _ = call_low_reasoning(
            prompt=prompt, max_tokens=200,
            system_prompt=ANSWER_SYSTEM_PROMPT,
        )
    else:
        from llm_clients import call_high_reasoning
        raw_answer, _ = call_high_reasoning(
            prompt=prompt, max_tokens=200,
            system_prompt=ANSWER_SYSTEM_PROMPT,
        )
    answer_ms = (time.monotonic() - t1) * 1000

    prediction = (raw_answer or "").strip()
    # Clean up common artifacts
    if prediction.startswith('"') and prediction.endswith('"'):
        prediction = prediction[1:-1]

    # Estimate tokens sent for this query (rough: 1 token ≈ 4 chars)
    context_tokens_est = len(prompt) // 4

    return {
        "question": qa.question,
        "ground_truth": qa.answer or "",
        "prediction": prediction,
        "category": qa.category,
        "category_name": qa.category_name,
        "evidence": qa.evidence,
        "num_memories_raw": len(memories_raw),
        "num_memories": len(memories),
        "retrieved_memories": [
            {
                "text": m.get("text", m.get("name", "")),
                "similarity": m.get("similarity", 0),
                "category": m.get("category", ""),
            }
            for m in memories
        ],
        "recall_latency_ms": round(recall_ms, 1),
        "answer_latency_ms": round(answer_ms, 1),
        "context_tokens_est": context_tokens_est,
    }


def evaluate_conversation(
    conversation: Conversation,
    db_path: Path,
    answer_model: str = "haiku",
    top_k: int = 10,
    owner_id: Optional[str] = None,
    include_journal: bool = False,
    recall_kwargs: Optional[Dict[str, Any]] = None,
    answer_template: Optional[_Template] = None,
) -> Dict[str, Any]:
    """Evaluate all scored QA pairs for a conversation.

    Args:
        conversation: Parsed LoCoMo conversation
        db_path: Path to the ingested database for this conversation
        answer_model: LLM for answer generation
        top_k: Number of memories to retrieve per question
        owner_id: Owner ID used during ingestion
        include_journal: If True, inject journal entries alongside SPEAKERS.md
        recall_kwargs: Optional overrides for recall() parameters (for A/B testing)
        answer_template: Optional override for answer generation prompt template

    Returns:
        Dict with per-question results and aggregate stats
    """
    conv_id = conversation.sample_id
    if owner_id is None:
        owner_id = conv_id

    # Switch to the correct DB for this conversation (reuse shared helper)
    _switch_to_db(db_path, fresh=False)

    # Resolve conversation directory and pre-load core markdown (once per conversation)
    conv_dir = db_path.parent
    cached_core_md = _load_core_markdown(conv_dir, include_journal=include_journal)

    scored_pairs = conversation.scored_qa_pairs
    print(f"\n  Evaluating {conv_id}: {len(scored_pairs)} scored QA pairs")
    if cached_core_md:
        print(f"    Core markdown: {len(cached_core_md.splitlines())} lines loaded")

    results = []
    category_counts = {}
    total_recall_ms = 0
    total_answer_ms = 0

    for i, qa in enumerate(scored_pairs):
        if (i + 1) % 20 == 0 or i == 0:
            print(f"    [{i+1}/{len(scored_pairs)}] {qa.category_name}: {qa.question[:60]}...")

        try:
            r = evaluate_single(
                qa=qa,
                conversation=conversation,
                owner_id=owner_id,
                answer_model=answer_model,
                top_k=top_k,
                core_context=cached_core_md,
                recall_kwargs=recall_kwargs,
                answer_template=answer_template,
            )
        except Exception as e:
            print(f"    ERROR on QA #{i+1}: {e}")
            r = {
                "question": qa.question,
                "ground_truth": qa.answer or "",
                "prediction": "",
                "category": qa.category,
                "category_name": qa.category_name,
                "evidence": qa.evidence,
                "num_memories_raw": 0,
                "num_memories": 0,
                "retrieved_memories": [],
                "recall_latency_ms": 0,
                "answer_latency_ms": 0,
                "context_tokens_est": 0,
                "error": str(e),
            }
        results.append(r)

        cat = qa.category_name
        if cat not in category_counts:
            category_counts[cat] = {"total": 0, "with_memories": 0}
        category_counts[cat]["total"] += 1
        if r["num_memories"] > 0:
            category_counts[cat]["with_memories"] += 1

        total_recall_ms += r["recall_latency_ms"]
        total_answer_ms += r["answer_latency_ms"]

    # Track core markdown state used during evaluation
    core_md_lines = len(cached_core_md.splitlines()) if cached_core_md else 0

    return {
        "conversation_id": conv_id,
        "speakers": f"{conversation.speaker_a} & {conversation.speaker_b}",
        "total_questions": len(scored_pairs),
        "results": results,
        "category_counts": category_counts,
        "avg_recall_ms": round(total_recall_ms / max(len(results), 1), 1),
        "avg_answer_ms": round(total_answer_ms / max(len(results), 1), 1),
        "core_markdown_lines": core_md_lines,
        "core_markdown_used": core_md_lines > 0,
    }


def evaluate_all(
    conversations: List[Conversation],
    results_dir: Path,
    conv_indices: Optional[List[int]] = None,
    answer_model: str = "haiku",
    top_k: int = 10,
    owner_id: Optional[str] = None,
    include_journal: bool = False,
    recall_kwargs: Optional[Dict[str, Any]] = None,
    answer_template: Optional[_Template] = None,
) -> Dict[str, Any]:
    """Evaluate multiple conversations.

    Args:
        conversations: Full list of parsed conversations
        results_dir: Root results directory (expects per-conversation subdirs from ingest)
        conv_indices: Which conversations to evaluate (default: all)
        answer_model: LLM for answer generation
        top_k: Number of memories to retrieve
        owner_id: Override owner (default: per-conversation sample_id)
        include_journal: If True, inject journal entries alongside SPEAKERS.md
        recall_kwargs: Optional overrides for recall() parameters (for A/B testing)
        answer_template: Optional override for answer generation prompt template

    Returns:
        Dict with per-conversation evaluation results
    """
    if conv_indices is None:
        conv_indices = list(range(len(conversations)))

    all_results = []

    for idx in conv_indices:
        if idx < 0 or idx >= len(conversations):
            print(f"WARNING: Conversation index {idx} out of range, skipping")
            continue

        conv = conversations[idx]
        conv_dir = results_dir / conv.sample_id
        db_path = conv_dir / "memory.db"

        if not db_path.exists():
            print(f"WARNING: DB not found for {conv.sample_id} at {db_path}, skipping")
            continue

        oid = owner_id if owner_id else conv.sample_id
        r = evaluate_conversation(
            conversation=conv,
            db_path=db_path,
            answer_model=answer_model,
            top_k=top_k,
            owner_id=oid,
            include_journal=include_journal,
            recall_kwargs=recall_kwargs,
            answer_template=answer_template,
        )
        all_results.append(r)

        # Save per-conversation results
        eval_file = conv_dir / "evaluation_results.json"
        with open(eval_file, "w") as f:
            json.dump(r, f, indent=2)

    return {
        "conversations": all_results,
        "total_questions": sum(r["total_questions"] for r in all_results),
        "answer_model": answer_model,
        "top_k": top_k,
    }


# ── Full-Context Baseline ──────────────────────────────────────────

def _format_full_transcript(conversation: Conversation) -> str:
    """Format entire conversation transcript for full-context baseline.

    Concatenates all sessions with date headers, matching the format
    used in production compaction. This provides the upper-bound baseline:
    what accuracy is possible with all information in context (no memory system).
    """
    from locomo.dataset import format_session_transcript
    parts = []
    for session in conversation.sessions:
        transcript = format_session_transcript(session)
        if transcript.strip():
            parts.append(transcript)
    return "\n\n---\n\n".join(parts)


_FULLCONTEXT_SYSTEM_TEMPLATE = _Template("""You are a helpful assistant answering questions about conversations between $speaker_a and $speaker_b.

Based on the full conversation transcript below, answer the question directly.

IMPORTANT RULES:
- Keep your answer to 5-6 words maximum
- Be direct and specific
- Convert relative time references to specific dates when possible
- If information conflicts, prioritize the most recent
- Do NOT add explanations or caveats

Full conversation transcript:
$transcript""")

_FULLCONTEXT_QUESTION_TEMPLATE = _Template("""Question: $question

Answer (5-6 words max):""")


def evaluate_single_fullcontext(
    qa: QAPair,
    conversation: Conversation,
    full_transcript: str,
    answer_model: str = "haiku",
    cached_system_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate a single QA pair using full conversation context (baseline).

    Instead of recall from memory DB, dumps the entire conversation into the
    answer model's context. This establishes the upper bound for what's achievable
    without needing a memory system at all.

    Transcript is placed in system prompt for Anthropic prompt caching —
    all QA pairs in a conversation share the same system prompt, so the
    transcript is cached after the first call (~90% input cost savings).
    """
    # Use pre-built system prompt (cached per conversation) or build one
    system_prompt = cached_system_prompt or _FULLCONTEXT_SYSTEM_TEMPLATE.safe_substitute(
        speaker_a=conversation.speaker_a,
        speaker_b=conversation.speaker_b,
        transcript=full_transcript,
    )

    # User message is just the question (small, variable per QA pair)
    prompt = _FULLCONTEXT_QUESTION_TEMPLATE.safe_substitute(question=qa.question)

    t0 = time.monotonic()
    if answer_model == "haiku":
        from llm_clients import call_low_reasoning
        raw_answer, _ = call_low_reasoning(
            prompt=prompt, max_tokens=200,
            system_prompt=system_prompt,
        )
    else:
        from llm_clients import call_high_reasoning
        raw_answer, _ = call_high_reasoning(
            prompt=prompt, max_tokens=200,
            system_prompt=system_prompt,
        )
    answer_ms = (time.monotonic() - t0) * 1000

    prediction = (raw_answer or "").strip()
    if prediction.startswith('"') and prediction.endswith('"'):
        prediction = prediction[1:-1]

    return {
        "question": qa.question,
        "ground_truth": qa.answer or "",
        "prediction": prediction,
        "category": qa.category,
        "category_name": qa.category_name,
        "evidence": qa.evidence,
        "num_memories_raw": 0,
        "num_memories": 0,
        "retrieved_memories": [],
        "recall_latency_ms": 0,
        "answer_latency_ms": round(answer_ms, 1),
        "context_tokens_est": len(system_prompt) // 4,
        "baseline_mode": "full-context",
    }


def evaluate_all_fullcontext(
    conversations: List[Conversation],
    results_dir: Path,
    conv_indices: Optional[List[int]] = None,
    answer_model: str = "haiku",
) -> Dict[str, Any]:
    """Evaluate using full-context baseline (entire transcript in prompt).

    This is the "upper bound" baseline — no memory system needed, just dump
    everything into the LLM's context window. LoCoMo conversations are only
    16K-26K tokens, well within modern 128K+ context windows.
    """
    if conv_indices is None:
        conv_indices = list(range(len(conversations)))

    all_results = []

    for idx in conv_indices:
        if idx < 0 or idx >= len(conversations):
            continue

        conv = conversations[idx]
        full_transcript = _format_full_transcript(conv)
        transcript_tokens = len(full_transcript.split()) * 1.3  # rough estimate

        # Pre-build system prompt for this conversation (enables Anthropic prompt caching —
        # all QA pairs in this conversation share the same system prompt, so the ~20K token
        # transcript is cached after the first call, saving ~90% on input tokens)
        cached_system = _FULLCONTEXT_SYSTEM_TEMPLATE.safe_substitute(
            speaker_a=conv.speaker_a,
            speaker_b=conv.speaker_b,
            transcript=full_transcript,
        )

        scored_pairs = conv.scored_qa_pairs
        print(f"\n  Full-context baseline for {conv.sample_id}: "
              f"{len(scored_pairs)} QA pairs, ~{int(transcript_tokens)} tokens")

        results = []
        for i, qa in enumerate(scored_pairs):
            if (i + 1) % 20 == 0 or i == 0:
                print(f"    [{i+1}/{len(scored_pairs)}] {qa.category_name}: {qa.question[:60]}...")

            try:
                r = evaluate_single_fullcontext(
                    qa=qa,
                    conversation=conv,
                    full_transcript=full_transcript,
                    answer_model=answer_model,
                    cached_system_prompt=cached_system,
                )
            except Exception as e:
                print(f"    ERROR on QA #{i+1}: {e}")
                r = {
                    "question": qa.question,
                    "ground_truth": qa.answer or "",
                    "prediction": "",
                    "category": qa.category,
                    "category_name": qa.category_name,
                    "evidence": qa.evidence,
                    "num_memories_raw": 0,
                    "num_memories": 0,
                    "retrieved_memories": [],
                    "recall_latency_ms": 0,
                    "answer_latency_ms": 0,
                    "context_tokens_est": 0,
                    "error": str(e),
                    "baseline_mode": "full-context",
                }
            results.append(r)

        conv_result = {
            "conversation_id": conv.sample_id,
            "speakers": f"{conv.speaker_a} & {conv.speaker_b}",
            "total_questions": len(scored_pairs),
            "results": results,
            "baseline_mode": "full-context",
            "transcript_tokens_est": int(transcript_tokens),
        }
        all_results.append(conv_result)

        # Save per-conversation results
        conv_dir = results_dir / conv.sample_id
        conv_dir.mkdir(parents=True, exist_ok=True)
        eval_file = conv_dir / "evaluation_fullcontext.json"
        with open(eval_file, "w") as f:
            json.dump(conv_result, f, indent=2)

    return {
        "conversations": all_results,
        "total_questions": sum(r["total_questions"] for r in all_results),
        "answer_model": answer_model,
        "baseline_mode": "full-context",
    }
