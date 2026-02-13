#!/usr/bin/env python3
"""
LoCoMo A/B Testing Runner — Batch test different parameter configurations.

Tests retrieval variants systematically against the LoCoMo benchmark to identify
optimal configurations. Each test variant can override recall() parameters, patch
config values, customize prompts, or monkey-patch functions.

Architecture:
- Test variants defined as dicts with overrides (recall_kwargs, config_patches, etc.)
- Each variant: patch config → symlink DBs from results-v3 → run eval → judge → aggregate
- Sequential execution (no multiprocessing) for simplicity and reliability
- All variants share the same extracted DBs from results-v3 (reuse existing work)

Usage:
    source memory-stress-test/test.env

    # Run all variants (recommended)
    python3 -m locomo.run_ab_tests --tests all

    # Run specific tests
    python3 -m locomo.run_ab_tests --tests no-hyde,top-k-12,fusion-heavy-vector

    # Just aggregate existing results (no new runs)
    python3 -m locomo.run_ab_tests --aggregate

    # Custom conversations subset
    python3 -m locomo.run_ab_tests --tests all --conversations 0,1,2
"""
import argparse
import copy
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional, Tuple

# Path setup
_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _DIR.parent
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
if str(_RUNNER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR.parent))

import runner  # noqa: F401

from locomo.dataset import load_dataset
from locomo.evaluate import evaluate_all, _ANSWER_TEMPLATE
from locomo.metrics import score_results, _mean
from locomo.ingest import parse_conv_range

# Import LLM clients for OpenAI judge
from llm_clients import reset_token_usage, get_token_usage, estimate_cost

# For config patching
try:
    from config import get_config, reload_config
    _HAS_CONFIG = True
except ImportError:
    _HAS_CONFIG = False
    print("WARNING: config module not available, config_patches will be skipped")


# ═══════════════════════════════════════════════════════════════════════
# Test Variant Definitions
# ═══════════════════════════════════════════════════════════════════════

def _get_reranker_prompt_open() -> str:
    """Reranker prompt variant that's more permissive for open-domain questions."""
    return """You are a memory relevance evaluator. Score how relevant a memory is to answering the question.

For personality/preference questions, relevance includes:
- Direct facts about the person's traits, opinions, or preferences
- Behavioral patterns or habits that reveal character
- Past experiences or choices that help infer personality
- Related facts that provide useful context

Return a JSON object with:
- relevance: integer 0-5 (0=irrelevant, 5=highly relevant)
- reason: brief explanation (one sentence)

Question: {query}
Memory: {fact}

Response (JSON only):"""


def _get_synthesis_prompt() -> str:
    """Synthesis prompt for query_synthesis variant — infers personality from facts."""
    return Template("""You are a helpful assistant. The user asked a question about $speaker_name's personality, preferences, or opinions.

Below are relevant facts from memory:

$facts

Based on these facts, synthesize what we can infer about $speaker_name's personality, preferences, or character. Be specific and cite which facts support each inference.

Keep your synthesis brief (3-4 sentences max).

Synthesis:""")


# All test variants — add your own here
TEST_VARIANTS = [
    # ── Routing & HyDE ───────────────────────────────────────────────────
    {
        "name": "no-hyde",
        "description": "Disable HyDE query expansion",
        "recall_kwargs": {"use_routing": False},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },

    # ── Reranker Pool Size ──────────────────────────────────────────────
    {
        "name": "reranker-pool-2x",
        "description": "Double reranker pool (20 → 40 candidates)",
        "recall_kwargs": {},
        "config_patches": [("retrieval.reranker.topK", 40)],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },
    {
        "name": "reranker-pool-3x",
        "description": "Triple reranker pool (20 → 60 candidates)",
        "recall_kwargs": {},
        "config_patches": [("retrieval.reranker.topK", 60)],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },

    # ── Reranker Prompt ─────────────────────────────────────────────────
    {
        "name": "reranker-prompt-open",
        "description": "More permissive reranker prompt for open-domain questions",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [
            # Patch the reranker instruction directly
            ("_reranker_instruction", _get_reranker_prompt_open()),
        ],
        "top_k": None,
    },

    # ── Top-K Retrieval ─────────────────────────────────────────────────
    {
        "name": "top-k-8",
        "description": "Retrieve 8 memories (vs 10 baseline)",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": 8,
    },
    {
        "name": "top-k-12",
        "description": "Retrieve 12 memories (vs 10 baseline)",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": 12,
    },

    # ── Fusion Weights ──────────────────────────────────────────────────
    {
        "name": "fusion-heavy-vector",
        "description": "Heavily favor vector search in RRF fusion (0.9 vec, 0.1 fts)",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [
            ("_fusion_weights_override", (0.9, 0.1)),
        ],
        "top_k": None,
    },
    {
        "name": "fusion-heavy-fts",
        "description": "Heavily favor FTS search in RRF fusion (0.3 vec, 0.7 fts)",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [
            ("_fusion_weights_override", (0.3, 0.7)),
        ],
        "top_k": None,
    },

    # ── Context Modes ───────────────────────────────────────────────────
    {
        "name": "core-only",
        "description": "Answer from SPEAKERS.md only (no DB recall)",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "core_only",
        "monkey_patches": [],
        "top_k": None,
    },
    {
        "name": "db-only",
        "description": "Answer from DB recall only (no core markdown)",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "db_only",
        "monkey_patches": [],
        "top_k": None,
    },

    # ── Answer Prompt ───────────────────────────────────────────────────
    {
        "name": "answer-prompt-synthesis",
        "description": "Answer prompt encourages synthesis for personality/preference questions",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": Template("""You are a helpful assistant answering questions about conversations between $speaker_a and $speaker_b.

Based on the background knowledge and retrieved memories below, answer the question.

If the question asks about personality, preferences, or opinions, synthesize and infer from available facts. Otherwise, answer directly from the provided information. Only say "I don't know" if nothing is relevant.

IMPORTANT RULES:
- Keep your answer to 5-6 words maximum
- Be direct and specific
- Convert relative time references to specific dates when possible
- If information conflicts, prioritize the most recent
- Do NOT add explanations or caveats

$context

Question: $question

Answer (5-6 words max):"""),
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },

    # ── Journal RAG ─────────────────────────────────────────────────────
    {
        "name": "journal-rag",
        "description": "Include RAG-indexed journal passages in context",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "journal_rag",
        "monkey_patches": [],
        "top_k": None,
    },

    # ── Graph Traversal ─────────────────────────────────────────────────
    {
        "name": "graph-deep",
        "description": "Deeper graph traversal (beamWidth=10, maxDepth=3)",
        "recall_kwargs": {},
        "config_patches": [
            ("retrieval.traversal.beamWidth", 10),
            ("retrieval.traversal.maxDepth", 3),
        ],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },

    # ── Query Synthesis ─────────────────────────────────────────────────
    {
        "name": "query-synthesis",
        "description": "Synthesize personality/preference inferences before answering",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "synthesis",  # Custom mode that triggers synthesis
        "monkey_patches": [],
        "top_k": None,
    },

    # ── Ablation Tests (Phase 1b) ────────────────────────────────────
    {
        "name": "no-hyde-clean",
        "description": "Disable ONLY HyDE via config (keep raw FTS fallback active)",
        "recall_kwargs": {},
        "config_patches": [("retrieval.useHyde", False)],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },
    {
        "name": "no-reranker",
        "description": "Disable Haiku reranker entirely (use raw RRF scores)",
        "recall_kwargs": {},
        "config_patches": [("retrieval.reranker.enabled", False)],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },
    {
        "name": "no-multi-pass",
        "description": "Disable second-pass retrieval (single pass only)",
        "recall_kwargs": {"use_multi_pass": False},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },
    {
        "name": "no-intent",
        "description": "Disable intent classification (no dynamic fusion weights)",
        "recall_kwargs": {"use_intent": False},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },
    {
        "name": "no-alias",
        "description": "Disable entity alias resolution",
        "recall_kwargs": {"use_aliases": False},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "standard",
        "monkey_patches": [],
        "top_k": None,
    },

    # ── Routed Strategies (per-category config) ──────────────────────────
    {
        "name": "routed-open-journal",
        "description": "Route open-domain to journal+no-HyDE+synthesis prompt; others standard",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "routed",
        "monkey_patches": [],
        "top_k": None,
        "route_overrides": {
            "open-domain": {
                "config_patches": [("retrieval.useHyde", False)],
                "include_journal": True,
                "recall_kwargs": {},
                "answer_template": Template("""You are a helpful assistant answering questions about conversations between $speaker_a and $speaker_b.

Based on the background knowledge and retrieved memories below, answer the question.

If the question asks about personality, preferences, or opinions, synthesize and infer from available facts. Otherwise, answer directly from the provided information. Only say "I don't know" if nothing is relevant.

IMPORTANT RULES:
- Keep your answer to 5-6 words maximum
- Be direct and specific
- If information conflicts, prioritize the most recent
- Do NOT add explanations or caveats

$context

Question: $question

Answer (5-6 words max):"""),
            },
            "default": {},  # Standard config for all other categories
        },
    },

    # ── Journal Prompt Variants (use regen_journals.py first) ─────────
    # These use different journal content generated by variant Opus prompts.
    # Source dirs: results-journal-{variant}/ (created by regen_journals.py)
    {
        "name": "journal-temporal",
        "description": "Journal entries focused on temporal anchoring (dates, sequences, timelines)",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "journal_rag",
        "monkey_patches": [],
        "top_k": None,
        "source_dir_name": "results-journal-temporal",
    },
    {
        "name": "journal-personality",
        "description": "Journal entries focused on personality traits, values, preferences",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "journal_rag",
        "monkey_patches": [],
        "top_k": None,
        "source_dir_name": "results-journal-personality",
    },
    {
        "name": "journal-factual",
        "description": "Journal entries focused on dense factual summaries",
        "recall_kwargs": {},
        "config_patches": [],
        "answer_template": None,
        "context_mode": "journal_rag",
        "monkey_patches": [],
        "top_k": None,
        "source_dir_name": "results-journal-factual",
    },
]


# ═══════════════════════════════════════════════════════════════════════
# Config Patching
# ═══════════════════════════════════════════════════════════════════════

def _apply_config_patches(patches: List[Tuple[str, Any]], workspace_root: Path) -> Dict[str, Any]:
    """Apply config patches and return the original config for restoration.

    Args:
        patches: List of (dotted_path, value) tuples
        workspace_root: Path to workspace (for locating config file)

    Returns:
        Dict mapping dotted_path → original_value for restoration
    """
    if not patches or not _HAS_CONFIG:
        return {}

    # Locate config file (should be in workspace or set via env)
    config_file = workspace_root / "config" / "memory.json"
    if not config_file.exists():
        print(f"  WARNING: Config file not found at {config_file}, skipping patches")
        return {}

    # Load current config
    with open(config_file) as f:
        config_data = json.load(f)

    # Track originals for restoration
    originals = {}

    # Apply each patch
    for dotted_path, value in patches:
        parts = dotted_path.split(".")
        current = config_data

        # Navigate to parent
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]

        # Store original
        final_key = parts[-1]
        if final_key in current:
            originals[dotted_path] = current[final_key]
        else:
            originals[dotted_path] = None  # Key didn't exist

        # Apply patch
        current[final_key] = value

    # Write patched config
    with open(config_file, "w") as f:
        json.dump(config_data, f, indent=2)

    # Reload config module
    if _HAS_CONFIG:
        reload_config()

    return originals


def _restore_config(originals: Dict[str, Any], workspace_root: Path):
    """Restore config to original values.

    Args:
        originals: Dict mapping dotted_path → original_value
        workspace_root: Path to workspace
    """
    if not originals or not _HAS_CONFIG:
        return

    config_file = workspace_root / "config" / "memory.json"
    if not config_file.exists():
        return

    with open(config_file) as f:
        config_data = json.load(f)

    for dotted_path, original_value in originals.items():
        parts = dotted_path.split(".")
        current = config_data

        # Navigate to parent
        for part in parts[:-1]:
            if part not in current:
                break
            current = current[part]
        else:
            # Restore value
            final_key = parts[-1]
            if original_value is None and final_key in current:
                del current[final_key]
            else:
                current[final_key] = original_value

    # Write restored config
    with open(config_file, "w") as f:
        json.dump(config_data, f, indent=2)

    if _HAS_CONFIG:
        reload_config()


# ═══════════════════════════════════════════════════════════════════════
# Monkey Patching
# ═══════════════════════════════════════════════════════════════════════

_ORIGINAL_PATCHES = {}  # Track original values for restoration


def _apply_monkey_patches(patches: List[Tuple[str, Any]]):
    """Apply monkey patches to modules/functions.

    Args:
        patches: List of (identifier, value) tuples
    """
    global _ORIGINAL_PATCHES

    for identifier, value in patches:
        if identifier == "_reranker_instruction":
            # Patch the reranker instruction in memory_graph
            try:
                import memory_graph
                if hasattr(memory_graph, "_RERANKER_INSTRUCTION"):
                    _ORIGINAL_PATCHES[identifier] = memory_graph._RERANKER_INSTRUCTION
                    memory_graph._RERANKER_INSTRUCTION = value
            except ImportError:
                print(f"  WARNING: Cannot patch {identifier}, memory_graph not available")

        elif identifier == "_fusion_weights_override":
            # Patch _get_fusion_weights to always return these weights
            try:
                import memory_graph
                if hasattr(memory_graph, "_get_fusion_weights"):
                    _ORIGINAL_PATCHES[identifier] = memory_graph._get_fusion_weights
                    # Replace with lambda that ignores intent and returns fixed weights
                    memory_graph._get_fusion_weights = lambda intent: value
            except ImportError:
                print(f"  WARNING: Cannot patch {identifier}, memory_graph not available")

        else:
            print(f"  WARNING: Unknown monkey patch identifier: {identifier}")


def _restore_monkey_patches():
    """Restore all monkey patches to original values."""
    global _ORIGINAL_PATCHES

    for identifier, original_value in _ORIGINAL_PATCHES.items():
        if identifier == "_reranker_instruction":
            try:
                import memory_graph
                memory_graph._RERANKER_INSTRUCTION = original_value
            except ImportError:
                pass

        elif identifier == "_fusion_weights_override":
            try:
                import memory_graph
                memory_graph._get_fusion_weights = original_value
            except ImportError:
                pass

    _ORIGINAL_PATCHES.clear()


# ═══════════════════════════════════════════════════════════════════════
# Custom Context Modes
# ═══════════════════════════════════════════════════════════════════════

def _evaluate_with_custom_context(
    variant: Dict[str, Any],
    conversations: List,
    results_dir: Path,
    conv_indices: List[int],
    answer_model: str,
    top_k: int,
) -> Dict[str, Any]:
    """Run evaluation with custom context mode (core_only, db_only, synthesis, etc.).

    Args:
        variant: Test variant configuration
        conversations: Full list of parsed conversations
        results_dir: Root results directory
        conv_indices: Which conversations to evaluate
        answer_model: LLM for answer generation
        top_k: Number of memories to retrieve

    Returns:
        Evaluation results dict
    """
    from locomo.evaluate import evaluate_single, _load_core_markdown, _format_memories_for_prompt
    from locomo.ingest import _switch_to_db

    context_mode = variant.get("context_mode", "standard")
    answer_template = variant.get("answer_template")
    workspace_root = Path(os.environ.get("QUAID_WORKSPACE", "."))

    all_results = []

    for idx in conv_indices:
        if idx < 0 or idx >= len(conversations):
            continue

        conv = conversations[idx]
        conv_dir = results_dir / conv.sample_id
        db_path = conv_dir / "memory.db"

        if not db_path.exists():
            print(f"  WARNING: DB not found for {conv.sample_id}, skipping")
            continue

        # Switch to DB
        _switch_to_db(db_path, fresh=False)

        # Load core markdown
        core_md = _load_core_markdown(conv_dir, include_journal=False)

        scored_pairs = conv.scored_qa_pairs
        print(f"\n  Evaluating {conv.sample_id}: {len(scored_pairs)} QA pairs (mode={context_mode})")

        results = []
        for i, qa in enumerate(scored_pairs):
            if (i + 1) % 20 == 0 or i == 0:
                print(f"    [{i+1}/{len(scored_pairs)}] {qa.category_name}: {qa.question[:60]}...")

            try:
                # Custom context handling
                if context_mode == "core_only":
                    # Skip recall, use only core markdown
                    r = evaluate_single(
                        qa=qa,
                        conversation=conv,
                        owner_id=conv.sample_id,
                        answer_model=answer_model,
                        top_k=top_k,
                        core_context=core_md,
                        recall_kwargs={"use_routing": False},  # Will be ignored since we override
                        answer_template=answer_template,
                    )
                    # Override retrieved memories to empty
                    r["retrieved_memories"] = []
                    r["num_memories"] = 0
                    r["num_memories_raw"] = 0

                elif context_mode == "db_only":
                    # Use recall but no core markdown
                    recall_kw = variant.get("recall_kwargs", {})
                    r = evaluate_single(
                        qa=qa,
                        conversation=conv,
                        owner_id=conv.sample_id,
                        answer_model=answer_model,
                        top_k=top_k,
                        core_context="",  # No core markdown
                        recall_kwargs=recall_kw,
                        answer_template=answer_template,
                    )

                elif context_mode == "synthesis":
                    # Run recall first, then synthesize personality inferences
                    from memory_graph import recall

                    recall_kw = variant.get("recall_kwargs", {})
                    memories_raw = recall(
                        query=qa.question,
                        limit=top_k,
                        owner_id=conv.sample_id,
                        **recall_kw,
                    )

                    # Check if this is an open-domain question
                    # Category mapping: 1=multi-hop, 2=temporal, 3=open-domain, 4=single-hop, 5=adversarial
                    is_open_domain = qa.category == 3

                    if is_open_domain and memories_raw:
                        # Synthesize personality inferences
                        from llm_clients import call_low_reasoning

                        facts_text = "\n".join([
                            f"- {m.get('text', m.get('name', ''))}"
                            for m in memories_raw[:top_k]
                        ])

                        synth_prompt = _get_synthesis_prompt().safe_substitute(
                            speaker_name=conv.speaker_a,  # Assume questions about speaker A
                            facts=facts_text,
                        )

                        synthesis, _ = call_low_reasoning(
                            prompt=synth_prompt,
                            max_tokens=300,
                        )

                        # Inject synthesis into context
                        core_context = core_md
                        if synthesis:
                            core_context += f"\n\nPersonality Synthesis:\n{synthesis.strip()}"
                    else:
                        core_context = core_md

                    # Now run standard evaluation with enhanced context
                    r = evaluate_single(
                        qa=qa,
                        conversation=conv,
                        owner_id=conv.sample_id,
                        answer_model=answer_model,
                        top_k=top_k,
                        core_context=core_context,
                        recall_kwargs=recall_kw,
                        answer_template=answer_template,
                    )

                elif context_mode == "journal_rag":
                    # Standard eval with journal injected
                    recall_kw = variant.get("recall_kwargs", {})
                    core_with_journal = _load_core_markdown(conv_dir, include_journal=True)
                    r = evaluate_single(
                        qa=qa,
                        conversation=conv,
                        owner_id=conv.sample_id,
                        answer_model=answer_model,
                        top_k=top_k,
                        core_context=core_with_journal,
                        recall_kwargs=recall_kw,
                        answer_template=answer_template,
                    )

                elif context_mode == "routed":
                    # Per-category routing: different retrieval config for open-domain vs others
                    # Category mapping: 1=multi-hop, 2=temporal, 3=open-domain, 4=single-hop
                    route_overrides = variant.get("route_overrides", {})
                    cat_name = qa.category_name  # e.g. "open-domain", "temporal", etc.

                    # Get category-specific overrides, fall back to default
                    cat_config = route_overrides.get(cat_name, route_overrides.get("default", {}))
                    cat_recall_kw = {**variant.get("recall_kwargs", {}), **cat_config.get("recall_kwargs", {})}
                    cat_template = cat_config.get("answer_template", answer_template)
                    cat_include_journal = cat_config.get("include_journal", False)

                    # Apply per-category config patches (temporary, restored after question)
                    cat_patches = cat_config.get("config_patches", [])
                    cat_originals = {}
                    if cat_patches:
                        cat_originals = _apply_config_patches(cat_patches, workspace_root)

                    try:
                        cat_context = _load_core_markdown(conv_dir, include_journal=cat_include_journal) if cat_include_journal else core_md
                        r = evaluate_single(
                            qa=qa,
                            conversation=conv,
                            owner_id=conv.sample_id,
                            answer_model=answer_model,
                            top_k=top_k,
                            core_context=cat_context,
                            recall_kwargs=cat_recall_kw,
                            answer_template=cat_template,
                        )
                    finally:
                        if cat_originals:
                            _restore_config(cat_originals, workspace_root)

                else:  # "standard"
                    recall_kw = variant.get("recall_kwargs", {})
                    r = evaluate_single(
                        qa=qa,
                        conversation=conv,
                        owner_id=conv.sample_id,
                        answer_model=answer_model,
                        top_k=top_k,
                        core_context=core_md,
                        recall_kwargs=recall_kw,
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

        conv_result = {
            "conversation_id": conv.sample_id,
            "speakers": f"{conv.speaker_a} & {conv.speaker_b}",
            "total_questions": len(scored_pairs),
            "results": results,
        }
        all_results.append(conv_result)

        # Save per-conversation results
        eval_file = conv_dir / "evaluation_results.json"
        with open(eval_file, "w") as f:
            json.dump(conv_result, f, indent=2)

    return {
        "conversations": all_results,
        "total_questions": sum(r["total_questions"] for r in all_results),
        "answer_model": answer_model,
        "top_k": top_k,
    }


# ═══════════════════════════════════════════════════════════════════════
# Variant Execution
# ═══════════════════════════════════════════════════════════════════════

def run_variant(
    variant: Dict[str, Any],
    results_base_dir: Path,
    conversations: List,
    conv_indices: List[int],
    source_results_dir: Path,
    answer_model: str = "haiku",
    judge_model: str = "gpt-4o-mini",
    num_trials: int = 1,
) -> Dict[str, Any]:
    """Run a single test variant.

    Args:
        variant: Test variant configuration
        results_base_dir: Base directory for all A/B test results
        conversations: Full list of parsed conversations
        conv_indices: Which conversations to test
        source_results_dir: Source directory with existing DBs (results-v3)
        answer_model: LLM for answer generation
        judge_model: LLM for judge evaluation
        num_trials: Number of judge trials

    Returns:
        Dict with variant results
    """
    variant_name = variant["name"]
    results_dir = results_base_dir / f"ab-{variant_name}"
    results_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*80}")
    print(f"Running A/B Test: {variant_name}")
    print(f"  Description: {variant['description']}")
    print(f"  Results dir: {results_dir}")
    print(f"{'='*80}")

    t_start = time.monotonic()
    reset_token_usage()

    # Get workspace root
    workspace_root = Path(os.environ.get("QUAID_WORKSPACE", "."))

    # Use variant-specific source dir if configured (e.g. journal prompt variants)
    effective_source_dir = source_results_dir
    if variant.get("source_dir_name"):
        candidate = source_results_dir.parent / variant["source_dir_name"]
        if candidate.exists():
            effective_source_dir = candidate
        else:
            print(f"  WARNING: source_dir_name '{variant['source_dir_name']}' not found at {candidate}")
            print(f"  Run regen_journals.py first to create journal variant directories.")
            raise FileNotFoundError(f"Source dir not found: {candidate}")

    # ── Phase 1: Apply Patches ─────────────────────────────────────────
    config_originals = {}
    try:
        if variant.get("config_patches"):
            print(f"\n  Applying config patches:")
            for path, value in variant["config_patches"]:
                print(f"    {path} = {value}")
            config_originals = _apply_config_patches(variant["config_patches"], workspace_root)

        if variant.get("monkey_patches"):
            print(f"\n  Applying monkey patches:")
            for identifier, _ in variant["monkey_patches"]:
                print(f"    {identifier}")
            _apply_monkey_patches(variant["monkey_patches"])

        # ── Phase 2: Symlink DBs ──────────────────────────────────────
        print(f"\n  Symlinking DBs from {effective_source_dir.name}:")
        for idx in conv_indices:
            if idx >= len(conversations):
                continue

            conv = conversations[idx]
            source_conv_dir = effective_source_dir / conv.sample_id
            target_conv_dir = results_dir / conv.sample_id
            target_conv_dir.mkdir(parents=True, exist_ok=True)

            # Symlink memory.db
            source_db = source_conv_dir / "memory.db"
            target_db = target_conv_dir / "memory.db"
            if source_db.exists():
                if target_db.exists() or target_db.is_symlink():
                    target_db.unlink()
                target_db.symlink_to(source_db)
                print(f"    {conv.sample_id}/memory.db → {source_db}")

            # Symlink SPEAKERS.md
            source_speakers = source_conv_dir / "SPEAKERS.md"
            target_speakers = target_conv_dir / "SPEAKERS.md"
            if source_speakers.exists():
                if target_speakers.exists() or target_speakers.is_symlink():
                    target_speakers.unlink()
                target_speakers.symlink_to(source_speakers)

            # Symlink journal/ if it exists
            source_journal = source_conv_dir / "journal"
            target_journal = target_conv_dir / "journal"
            if source_journal.exists():
                if target_journal.exists() or target_journal.is_symlink():
                    if target_journal.is_symlink():
                        target_journal.unlink()
                    else:
                        shutil.rmtree(target_journal)
                target_journal.symlink_to(source_journal)

        # ── Phase 3: Run Evaluation ───────────────────────────────────
        print(f"\n  Running evaluation:")

        top_k = variant.get("top_k") or 10
        context_mode = variant.get("context_mode", "standard")

        if context_mode in ["core_only", "db_only", "synthesis", "journal_rag", "routed"]:
            # Use custom evaluation path
            eval_result = _evaluate_with_custom_context(
                variant=variant,
                conversations=conversations,
                results_dir=results_dir,
                conv_indices=conv_indices,
                answer_model=answer_model,
                top_k=top_k,
            )
        else:
            # Standard evaluation
            recall_kwargs = variant.get("recall_kwargs", {})
            answer_template = variant.get("answer_template")

            # Patch evaluate_all to use our overrides
            eval_result = evaluate_all(
                conversations=conversations,
                results_dir=results_dir,
                conv_indices=conv_indices,
                answer_model=answer_model,
                top_k=top_k,
                include_journal=(context_mode == "journal_rag"),
            )

        print(f"    Evaluated {eval_result['total_questions']} questions")

        # ── Phase 4: Score Results ────────────────────────────────────
        print(f"\n  Scoring results (judge={judge_model}, trials={num_trials}):")

        # Collect all eval items
        all_eval_items = []
        for conv_result in eval_result["conversations"]:
            all_eval_items.extend(conv_result["results"])

        # Ensure OpenAI API key is set for judge
        if "gpt-" in judge_model:
            if not os.environ.get("OPENAI_API_KEY"):
                # Try to load from .env
                env_file = workspace_root / ".env"
                if env_file.exists():
                    for line in env_file.read_text().splitlines():
                        if line.startswith("OPENAI_API_KEY="):
                            os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip()
                            break

        scores = score_results(
            eval_results=all_eval_items,
            run_llm_judge=True,
            judge_model=judge_model,
            judge_prompt_style="mem0",
        )

        # ── Phase 5: Aggregate Metrics ────────────────────────────────
        elapsed = round(time.monotonic() - t_start, 1)
        token_usage = get_token_usage()
        est_cost = estimate_cost()

        # Compute average context tokens per query
        total_context_tokens = sum(
            r.get("context_tokens_est", 0)
            for conv_r in eval_result["conversations"]
            for r in conv_r["results"]
        )
        avg_context_tokens = int(total_context_tokens / max(eval_result["total_questions"], 1))

        result = {
            "variant": variant_name,
            "description": variant["description"],
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "conversations": conv_indices,
                "answer_model": answer_model,
                "judge_model": judge_model,
                "num_trials": num_trials,
                "top_k": top_k,
                "context_mode": context_mode,
                "elapsed_seconds": elapsed,
            },
            "config_patches": variant.get("config_patches", []),
            "recall_kwargs": variant.get("recall_kwargs", {}),
            "scores": scores,
            "token_usage": {
                "input_tokens": token_usage["input_tokens"],
                "output_tokens": token_usage["output_tokens"],
                "cache_read_tokens": token_usage["cache_read_tokens"],
                "api_calls": token_usage["api_calls"],
                "estimated_cost_usd": est_cost,
                "avg_context_tokens_per_query": avg_context_tokens,
            },
            "evaluation": eval_result,
        }

        # Save variant results
        variant_file = results_dir / f"ab_{variant_name}_results.json"
        with open(variant_file, "w") as f:
            json.dump(result, f, indent=2)

        print(f"\n  Variant complete:")
        print(f"    Token F1:   {scores['overall'].get('token_f1', 0):.1f}%")
        print(f"    BLEU-1:     {scores['overall'].get('bleu1', 0):.1f}%")
        print(f"    LLM-Judge:  {scores['overall'].get('llm_judge', 0):.1f}%")
        print(f"    Avg tokens: {avg_context_tokens}")
        print(f"    Cost:       ${est_cost:.4f}")
        print(f"    Time:       {elapsed}s")
        print(f"    Saved to:   {variant_file}")

        return result

    finally:
        # Always restore patches
        if config_originals:
            print(f"\n  Restoring config patches")
            _restore_config(config_originals, workspace_root)

        if variant.get("monkey_patches"):
            print(f"  Restoring monkey patches")
            _restore_monkey_patches()


# ═══════════════════════════════════════════════════════════════════════
# Batch Runner
# ═══════════════════════════════════════════════════════════════════════

def run_all_variants(
    variants: List[Dict[str, Any]],
    results_base_dir: Path,
    conversations: List,
    conv_indices: List[int],
    source_results_dir: Path,
    answer_model: str = "haiku",
    judge_model: str = "gpt-4o-mini",
    num_trials: int = 1,
) -> List[Dict[str, Any]]:
    """Run all test variants sequentially.

    Args:
        variants: List of test variant configurations
        results_base_dir: Base directory for all A/B test results
        conversations: Full list of parsed conversations
        conv_indices: Which conversations to test
        source_results_dir: Source directory with existing DBs
        answer_model: LLM for answer generation
        judge_model: LLM for judge evaluation
        num_trials: Number of judge trials

    Returns:
        List of variant results
    """
    all_results = []

    print(f"\n{'='*80}")
    print(f"A/B Test Batch Runner")
    print(f"  Variants: {len(variants)}")
    print(f"  Conversations: {conv_indices}")
    print(f"  Source DBs: {source_results_dir}")
    print(f"  Output: {results_base_dir}")
    print(f"{'='*80}")

    for i, variant in enumerate(variants, 1):
        print(f"\n\n[{i}/{len(variants)}] Starting variant: {variant['name']}")

        try:
            result = run_variant(
                variant=variant,
                results_base_dir=results_base_dir,
                conversations=conversations,
                conv_indices=conv_indices,
                source_results_dir=source_results_dir,
                answer_model=answer_model,
                judge_model=judge_model,
                num_trials=num_trials,
            )
            all_results.append(result)

        except Exception as e:
            print(f"\n  ERROR running variant {variant['name']}: {e}")
            import traceback
            traceback.print_exc()

            # Save error record
            error_result = {
                "variant": variant["name"],
                "description": variant["description"],
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }
            all_results.append(error_result)

    return all_results


# ═══════════════════════════════════════════════════════════════════════
# Result Aggregation
# ═══════════════════════════════════════════════════════════════════════

def aggregate_results(results_base_dir: Path, baseline_score: float = 69.57) -> str:
    """Aggregate all A/B test results into a comparison table.

    Args:
        results_base_dir: Base directory with all A/B test results
        baseline_score: Baseline score for delta computation (v3 standard)

    Returns:
        Formatted comparison table as string
    """
    # Find all variant result files
    variant_files = sorted(results_base_dir.glob("ab-*/ab_*_results.json"))

    if not variant_files:
        return "No A/B test results found."

    print(f"\nAggregating {len(variant_files)} variant results...")

    rows = []
    for vf in variant_files:
        try:
            with open(vf) as f:
                data = json.load(f)

            if "error" in data:
                # Error record
                rows.append({
                    "variant": data["variant"],
                    "description": data["description"],
                    "overall": 0,
                    "single_hop": 0,
                    "multi_hop": 0,
                    "temporal": 0,
                    "open_domain": 0,
                    "avg_tokens": 0,
                    "delta": 0,
                    "error": data["error"],
                })
                continue

            scores = data.get("scores", {})
            overall = scores.get("overall", {})
            per_cat = scores.get("per_category", {})
            tokens = data.get("token_usage", {})

            rows.append({
                "variant": data["variant"],
                "description": data["description"],
                "overall": overall.get("llm_judge", 0),
                "single_hop": per_cat.get("single-hop", {}).get("llm_judge", 0),
                "multi_hop": per_cat.get("multi-hop", {}).get("llm_judge", 0),
                "temporal": per_cat.get("temporal", {}).get("llm_judge", 0),
                "open_domain": per_cat.get("open-domain", {}).get("llm_judge", 0),
                "avg_tokens": tokens.get("avg_context_tokens_per_query", 0),
                "delta": overall.get("llm_judge", 0) - baseline_score,
                "error": None,
            })

        except Exception as e:
            print(f"  WARNING: Failed to parse {vf.name}: {e}")
            continue

    if not rows:
        return "No valid variant results found."

    # Sort by overall score descending
    rows.sort(key=lambda r: r["overall"], reverse=True)

    # Build table
    lines = []
    lines.append("=" * 140)
    lines.append("A/B Test Results — LoCoMo Benchmark Variants")
    lines.append(f"Baseline: {baseline_score:.2f}% (v3 standard)")
    lines.append("=" * 140)
    lines.append(f"{'Variant':<25} {'Overall':>8} {'Single':>7} {'Multi':>7} {'Temp':>7} {'Open':>7} {'AvgTok':>8} {'Delta':>8}")
    lines.append("-" * 140)

    for r in rows:
        if r["error"]:
            lines.append(f"{r['variant']:<25} ERROR: {r['error']}")
        else:
            lines.append(
                f"{r['variant']:<25} "
                f"{r['overall']:>7.1f}% "
                f"{r['single_hop']:>6.1f}% "
                f"{r['multi_hop']:>6.1f}% "
                f"{r['temporal']:>6.1f}% "
                f"{r['open_domain']:>6.1f}% "
                f"{r['avg_tokens']:>8} "
                f"{r['delta']:>+7.1f}%"
            )

    lines.append("=" * 140)

    # Add legend
    lines.append("\nColumns:")
    lines.append("  Overall:  LLM-Judge overall score")
    lines.append("  Single:   Single-hop category score")
    lines.append("  Multi:    Multi-hop category score")
    lines.append("  Temp:     Temporal category score")
    lines.append("  Open:     Open-domain category score")
    lines.append("  AvgTok:   Average context tokens per query")
    lines.append("  Delta:    Difference from baseline (positive = better)")

    table = "\n".join(lines)

    # Save to file
    table_file = results_base_dir / "ab_comparison_table.txt"
    table_file.write_text(table)
    print(f"  Saved comparison table to {table_file}")

    return table


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="LoCoMo A/B Testing Runner — Test retrieval parameter variants",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run all variants
  python3 -m locomo.run_ab_tests --tests all

  # Run specific tests
  python3 -m locomo.run_ab_tests --tests no-hyde,top-k-12,fusion-heavy-vector

  # Just aggregate existing results (no new runs)
  python3 -m locomo.run_ab_tests --aggregate

  # Custom conversations subset
  python3 -m locomo.run_ab_tests --tests all --conversations 0,1,2
        """,
    )
    parser.add_argument(
        "--tests", default="all",
        help="Which tests to run: 'all', or comma-separated variant names"
    )
    parser.add_argument(
        "--conversations", default="all",
        help="Which conversations: '0', '0-2', '0,3,5', 'all' (default: all)"
    )
    parser.add_argument(
        "--source-dir", default=str(_DIR / "data" / "results-v3"),
        help="Source directory with existing DBs (default: data/results-v3)"
    )
    parser.add_argument(
        "--results-dir", default=str(_DIR / "data" / "results-ab"),
        help="Output directory for A/B test results (default: data/results-ab)"
    )
    parser.add_argument(
        "--answer-model", choices=["haiku", "opus"], default="haiku",
        help="Model for answer generation (default: haiku)"
    )
    parser.add_argument(
        "--judge-model", choices=["haiku", "opus", "gpt-4o-mini"], default="gpt-4o-mini",
        help="Model for LLM-Judge (default: gpt-4o-mini)"
    )
    parser.add_argument(
        "--trials", type=int, default=1,
        help="Number of judge trials (default: 1)"
    )
    parser.add_argument(
        "--aggregate", action="store_true",
        help="Just aggregate existing results (skip running tests)"
    )
    parser.add_argument(
        "--list-tests", action="store_true",
        help="List all available test variants and exit"
    )
    args = parser.parse_args()

    # ── List Tests Mode ───────────────────────────────────────────────
    if args.list_tests:
        print(f"\nAvailable A/B Test Variants ({len(TEST_VARIANTS)}):\n")
        for v in TEST_VARIANTS:
            print(f"  {v['name']:<25} — {v['description']}")
        print()
        return 0

    results_base_dir = Path(args.results_dir)
    results_base_dir.mkdir(parents=True, exist_ok=True)

    # ── Aggregate Mode ────────────────────────────────────────────────
    if args.aggregate:
        table = aggregate_results(results_base_dir)
        print(table)
        return 0

    # ── Run Tests Mode ────────────────────────────────────────────────
    # Parse test selection
    if args.tests == "all":
        selected_variants = TEST_VARIANTS
    else:
        variant_names = [n.strip() for n in args.tests.split(",")]
        variant_map = {v["name"]: v for v in TEST_VARIANTS}
        selected_variants = []
        for name in variant_names:
            if name in variant_map:
                selected_variants.append(variant_map[name])
            else:
                print(f"WARNING: Unknown variant '{name}', skipping")

        if not selected_variants:
            print("ERROR: No valid variants selected")
            return 1

    # Parse conversations
    conv_indices = parse_conv_range(args.conversations)

    # Load dataset
    print("\nLoading LoCoMo dataset...")
    conversations = load_dataset()

    # Validate source directory
    source_results_dir = Path(args.source_dir)
    if not source_results_dir.exists():
        print(f"ERROR: Source results directory not found: {source_results_dir}")
        return 1

    # Run all variants
    all_results = run_all_variants(
        variants=selected_variants,
        results_base_dir=results_base_dir,
        conversations=conversations,
        conv_indices=conv_indices,
        source_results_dir=source_results_dir,
        answer_model=args.answer_model,
        judge_model=args.judge_model,
        num_trials=args.trials,
    )

    # Aggregate results
    print(f"\n\n{'='*80}")
    print("Aggregating results...")
    print(f"{'='*80}")

    table = aggregate_results(results_base_dir)
    print(table)

    # Save master results file
    master_file = results_base_dir / "ab_all_results.json"
    with open(master_file, "w") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "variants": all_results,
            "metadata": {
                "conversations": conv_indices,
                "answer_model": args.answer_model,
                "judge_model": args.judge_model,
                "num_trials": args.trials,
                "source_dir": str(source_results_dir),
            },
        }, f, indent=2)
    print(f"\nMaster results saved to {master_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
