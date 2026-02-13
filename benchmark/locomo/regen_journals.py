#!/usr/bin/env python3
"""
Journal Prompt A/B — Re-generate journal entries with different Opus prompts.

This script re-generates ONLY the journal entries from LoCoMo conversations
using variant prompts. It does NOT re-extract facts, re-embed, or re-run
dedup — those stay identical from v3-journal. Only the journal text changes.

Architecture:
1. Load LoCoMo sessions (same transcripts as v3)
2. For each prompt variant × each conversation:
   a. Copy DB + SPEAKERS.md from v3-journal (identical base)
   b. Call Opus with variant-specific journal-only prompt for each session
   c. Write journal entries via soul_snippets.write_journal_entry()
   d. Run snippet review + journal distillation (same as ingest pipeline)
3. Output: results-journal-{variant}/ directories ready for eval

Cost: ~$1-2 per variant (272 sessions × ~2K input + ~200 output tokens each).

Usage:
    source memory-stress-test/test.env
    python3 -m locomo.regen_journals --variants all
    python3 -m locomo.regen_journals --variants temporal,personality
    python3 -m locomo.regen_journals --variants temporal --conversations 0,1
"""
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Path setup
_DIR = Path(__file__).resolve().parent
_RUNNER_DIR = _DIR.parent
if str(_RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR))
if str(_RUNNER_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_RUNNER_DIR.parent))

import runner  # noqa: F401 — sets up QUAID_WORKSPACE etc.

from locomo.dataset import (
    Conversation,
    Session,
    format_session_transcript,
    load_dataset,
)
from locomo.ingest import _set_workspace

import soul_snippets

# ═══════════════════════════════════════════════════════════════════════
# Journal Prompt Variants
# ═══════════════════════════════════════════════════════════════════════

# The current (baseline) journal prompt — kept here for reference
_BASELINE_PROMPT = """Write a short reflective paragraph about what was learned about the speakers
and their relationship in this session. This is diary-style, capturing themes and patterns.
Target file: "SPEAKERS.md"

Respond with JSON only:
{{
  "journal_entries": {{
    "SPEAKERS.md": "Reflective paragraph about what was learned"
  }}
}}

If nothing worth capturing, respond: {{"journal_entries": {{}}}}"""

JOURNAL_VARIANTS: Dict[str, Dict[str, Any]] = {
    "temporal": {
        "description": "Focus on temporal anchoring — dates, sequences, changes over time",
        "system_prompt": """You are a diary-keeping assistant. Read the following conversation between {speaker_a} and {speaker_b} and write a journal entry.

FOCUS: Temporal information. Anchor every observation to WHEN it happened or was mentioned.
Write about what changed, what's upcoming, what deadlines or dates were discussed.
Track the timeline: what happened before, what's happening now, what's planned next.

Keep it to 1-2 short paragraphs. Write in third person about both speakers.
Include specific dates, timeframes, and sequences whenever mentioned.

Conversation date: {date_time}

Respond with JSON only:
{{
  "journal_entries": {{
    "SPEAKERS.md": "Your temporal-focused journal entry"
  }}
}}

If nothing temporal worth capturing, respond: {{"journal_entries": {{}}}}""",
    },
    "personality": {
        "description": "Focus on personality traits, values, preferences, opinions",
        "system_prompt": """You are a diary-keeping assistant. Read the following conversation between {speaker_a} and {speaker_b} and write a journal entry.

FOCUS: Personality and character. What does this conversation reveal about WHO these people are?
Write about their values, communication styles, emotional patterns, decision-making approaches.
Note preferences, opinions, pet peeves, passions, and how they relate to each other.
Capture personality INFERENCES, not just stated facts.

Keep it to 1-2 short paragraphs. Write in third person about both speakers.
Go beyond what was explicitly said — what can you INFER about their personalities?

Respond with JSON only:
{{
  "journal_entries": {{
    "SPEAKERS.md": "Your personality-focused journal entry"
  }}
}}

If nothing personality-revealing worth capturing, respond: {{"journal_entries": {{}}}}""",
    },
    "factual": {
        "description": "Dense factual summary — maximize information density",
        "system_prompt": """You are a diary-keeping assistant. Read the following conversation between {speaker_a} and {speaker_b} and write a journal entry.

FOCUS: Information density. Pack as many concrete facts as possible into a concise summary.
Include names, places, events, numbers, plans, relationships, jobs, hobbies — anything factual.
Prioritize breadth over depth. List facts efficiently rather than writing narrative prose.
Use semicolons to separate distinct facts within sentences.

Keep it to 1-2 short paragraphs. Write in third person about both speakers.
Every sentence should contain at least 2-3 distinct facts.

Conversation date: {date_time}

Respond with JSON only:
{{
  "journal_entries": {{
    "SPEAKERS.md": "Your fact-dense journal entry"
  }}
}}

If nothing factual worth capturing, respond: {{"journal_entries": {{}}}}""",
    },
}

# ═══════════════════════════════════════════════════════════════════════
# Journal Generation
# ═══════════════════════════════════════════════════════════════════════

# Cache directory for journal-only Opus calls
JOURNAL_CACHE_DIR = _DIR / "data" / "journal_cache"


def _get_journal_cache_path(variant: str, conv_id: str, session_num: int) -> Path:
    """Get cache path for a journal-only extraction."""
    d = JOURNAL_CACHE_DIR / variant
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{conv_id}_session_{session_num:03d}.json"


def _generate_journal_entry(
    variant_name: str,
    system_prompt_template: str,
    conversation: Conversation,
    session: Session,
    use_cache: bool = True,
) -> Optional[str]:
    """Generate a single journal entry for one session using a variant prompt.

    Args:
        variant_name: Name of the prompt variant (for caching)
        system_prompt_template: The system prompt with {speaker_a}, {speaker_b}, {date_time} placeholders
        conversation: Parent conversation
        session: Session to generate journal for
        use_cache: If True, reuse cached results

    Returns:
        Journal entry text, or None if nothing worth capturing.
    """
    conv_id = conversation.sample_id
    session_num = session.session_num
    cache_path = _get_journal_cache_path(variant_name, conv_id, session_num)

    # Check cache
    if use_cache and cache_path.exists():
        with open(cache_path) as f:
            cached = json.load(f)
        return cached.get("journal_entries", {}).get("SPEAKERS.md")

    # Format transcript
    transcript = format_session_transcript(session)
    if not transcript.strip():
        # Cache empty result
        with open(cache_path, "w") as f:
            json.dump({"journal_entries": {}}, f)
        return None

    # Build prompt
    system_prompt = system_prompt_template.format(
        speaker_a=conversation.speaker_a,
        speaker_b=conversation.speaker_b,
        date_time=session.date_time or "unknown date",
    )
    user_content = f"Generate a journal entry for this conversation:\n\n{transcript[:100000]}"

    from llm_clients import call_high_reasoning, parse_json_response

    raw_response, _ = call_high_reasoning(
        prompt=user_content,
        system_prompt=system_prompt,
        max_tokens=1024,
    )

    # Parse response
    parsed = parse_json_response(raw_response)
    if parsed is None:
        # Try to extract JSON from the response
        import re
        match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                parsed = {}
        else:
            parsed = {}

    # Cache the result
    with open(cache_path, "w") as f:
        json.dump(parsed, f, indent=2)

    journal_entries = parsed.get("journal_entries", {})
    entry = journal_entries.get("SPEAKERS.md")
    if isinstance(entry, list):
        entry = "\n\n".join(entry)
    return entry if entry and entry.strip() else None


def _setup_variant_workspace(
    variant_name: str,
    conv_id: str,
    conversation: Conversation,
    source_dir: Path,
    output_base: Path,
) -> Path:
    """Create a variant workspace by copying DB + SPEAKERS.md from source.

    Returns the conversation directory for this variant.
    """
    conv_dir = output_base / f"results-journal-{variant_name}" / conv_id
    conv_dir.mkdir(parents=True, exist_ok=True)

    # Symlink DB from source (same extraction/dedup as v3-journal)
    db_dest = conv_dir / "memory.db"
    if db_dest.exists() or db_dest.is_symlink():
        db_dest.unlink()
    db_src = source_dir / conv_id / "memory.db"
    if db_src.exists():
        os.symlink(db_src.resolve(), db_dest)

    # Copy SPEAKERS.md from source (same snippet-folded core markdown)
    speakers_src = source_dir / conv_id / "SPEAKERS.md"
    speakers_dest = conv_dir / "SPEAKERS.md"
    if speakers_src.exists():
        shutil.copy2(speakers_src, speakers_dest)

    # Create fresh journal directory (we'll write new entries)
    journal_dir = conv_dir / "journal"
    # Clean stale journal files from prior runs
    if journal_dir.exists():
        for f in journal_dir.glob("*.journal.md"):
            f.unlink()
        archive_dir = journal_dir / "archive"
        if archive_dir.exists():
            for f in archive_dir.glob("*.md"):
                f.unlink()
        state_file = journal_dir / ".distillation-state.json"
        if state_file.exists():
            state_file.unlink()
    journal_dir.mkdir(exist_ok=True)

    return conv_dir


def process_variant(
    variant_name: str,
    variant_config: Dict[str, Any],
    conversations: List[Conversation],
    source_dir: Path,
    output_base: Path,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Process a single journal prompt variant across all conversations.

    Args:
        variant_name: e.g. "temporal", "personality", "factual"
        variant_config: Dict with "system_prompt" and "description"
        conversations: List of LoCoMo conversations to process
        source_dir: Path to v3-journal results (for DB + SPEAKERS.md)
        output_base: Base output directory (data/)
        use_cache: Cache Opus calls

    Returns:
        Dict with results per conversation.
    """
    system_prompt_template = variant_config["system_prompt"]
    results = {}

    print(f"\n{'='*70}")
    print(f"Journal Variant: {variant_name}")
    print(f"  Description: {variant_config['description']}")
    print(f"  Conversations: {len(conversations)}")
    print(f"{'='*70}")

    total_entries = 0
    total_cached = 0
    total_sessions = 0
    t0 = time.monotonic()

    for conv in conversations:
        conv_id = conv.sample_id
        print(f"\n  [{conv_id}] {len(conv.sessions)} sessions...")

        # Set up workspace
        conv_dir = _setup_variant_workspace(
            variant_name, conv_id, conv, source_dir, output_base,
        )

        # Switch workspace for soul_snippets operations
        _set_workspace(conv_dir)

        # Generate journal entries for each session
        entries_written = 0
        cached_count = 0
        for i, session in enumerate(conv.sessions):
            cache_path = _get_journal_cache_path(variant_name, conv_id, session.session_num)
            was_cached = cache_path.exists() and use_cache

            entry = _generate_journal_entry(
                variant_name=variant_name,
                system_prompt_template=system_prompt_template,
                conversation=conv,
                session=session,
                use_cache=use_cache,
            )

            if was_cached:
                cached_count += 1

            if entry:
                # Write journal entry using production code path
                date_str = None
                if session.date_time:
                    dt = _parse_date(session.date_time)
                    if dt:
                        date_str = dt[:10]

                try:
                    wrote = soul_snippets.write_journal_entry(
                        "SPEAKERS.md",
                        entry.strip(),
                        trigger=f"Session {session.session_num}",
                        date_str=date_str,
                    )
                    if wrote:
                        entries_written += 1
                except Exception as e:
                    print(f"    ERROR writing journal for session {session.session_num}: {e}")

            if (i + 1) % 10 == 0:
                print(f"    [{i+1}/{len(conv.sessions)}] entries={entries_written} cached={cached_count}")

        # Run distillation on all accumulated journal entries
        print(f"    Running snippet review + journal distillation...")
        try:
            sr = soul_snippets.run_soul_snippets_review(dry_run=False)
            print(f"      Snippets: folded={sr.get('folded', 0)}, "
                  f"rewritten={sr.get('rewritten', 0)}, discarded={sr.get('discarded', 0)}")
        except Exception as e:
            print(f"      Snippets error: {e}")

        try:
            jr = soul_snippets.run_journal_distillation(
                dry_run=False, force_distill=True,
            )
            print(f"      Journal: entries={jr.get('total_entries', 0)}, "
                  f"files_distilled={jr.get('files_distilled', 0)}")
        except Exception as e:
            print(f"      Journal distillation error: {e}")

        total_entries += entries_written
        total_cached += cached_count
        total_sessions += len(conv.sessions)

        results[conv_id] = {
            "sessions": len(conv.sessions),
            "entries_written": entries_written,
            "cached": cached_count,
        }

        print(f"    Done: {entries_written}/{len(conv.sessions)} entries written "
              f"({cached_count} cached)")

    elapsed = time.monotonic() - t0
    print(f"\n  Variant '{variant_name}' complete: "
          f"{total_entries} entries across {total_sessions} sessions "
          f"({total_cached} cached) in {elapsed:.0f}s")

    # Save variant metadata
    meta = {
        "variant": variant_name,
        "description": variant_config["description"],
        "timestamp": datetime.now().isoformat(),
        "total_sessions": total_sessions,
        "total_entries": total_entries,
        "total_cached": total_cached,
        "elapsed_seconds": round(elapsed, 1),
        "conversations": results,
    }
    meta_path = output_base / f"results-journal-{variant_name}" / "variant_meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    return results


# ═══════════════════════════════════════════════════════════════════════
# Date parsing helper (extracted from ingest.py pattern)
# ═══════════════════════════════════════════════════════════════════════

def _parse_date(date_time_str: str) -> Optional[str]:
    """Parse LoCoMo date string to ISO format."""
    for fmt in [
        "%I:%M %p on %d %B, %Y",
        "%I:%M %p on %d %B %Y",
        "%H:%M on %d %B, %Y",
    ]:
        try:
            dt = datetime.strptime(date_time_str.strip(), fmt)
            return dt.isoformat()
        except ValueError:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Re-generate journal entries with different Opus prompts",
    )
    parser.add_argument(
        "--variants",
        default="all",
        help="Comma-separated variant names or 'all' (default: all)",
    )
    parser.add_argument(
        "--conversations",
        default=None,
        help="Comma-separated conversation indices or range (e.g. 0,1,2 or 0-4). Default: all",
    )
    parser.add_argument(
        "--source-dir",
        default=str(_DIR / "data" / "results-v3-journal"),
        help="Source directory for DB + SPEAKERS.md (default: results-v3-journal)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Regenerate all journal entries (ignore cache)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available variants and exit",
    )
    args = parser.parse_args()

    if args.list:
        print("Available journal prompt variants:")
        for name, config in JOURNAL_VARIANTS.items():
            print(f"  {name}: {config['description']}")
        return

    # Load dataset
    print("Loading LoCoMo dataset...")
    conversations = load_dataset()

    # Filter conversations
    if args.conversations:
        indices = []
        for part in args.conversations.split(","):
            if "-" in part:
                start, end = part.split("-", 1)
                indices.extend(range(int(start), int(end) + 1))
            else:
                indices.append(int(part))
        conversations = [conversations[i] for i in indices if i < len(conversations)]
        print(f"  Using conversations: {[c.sample_id for c in conversations]}")

    # Select variants
    if args.variants == "all":
        variant_names = list(JOURNAL_VARIANTS.keys())
    else:
        variant_names = [v.strip() for v in args.variants.split(",")]
        for v in variant_names:
            if v not in JOURNAL_VARIANTS:
                print(f"ERROR: Unknown variant '{v}'. Available: {list(JOURNAL_VARIANTS.keys())}")
                sys.exit(1)

    source_dir = Path(args.source_dir)
    if not source_dir.exists():
        print(f"ERROR: Source directory does not exist: {source_dir}")
        sys.exit(1)

    output_base = _DIR / "data"
    use_cache = not args.no_cache

    print(f"\n{'='*70}")
    print(f"Journal Prompt A/B Generator")
    print(f"  Variants: {variant_names}")
    print(f"  Conversations: {len(conversations)}")
    print(f"  Source: {source_dir}")
    print(f"  Output: {output_base}")
    print(f"  Cache: {'enabled' if use_cache else 'disabled'}")
    print(f"{'='*70}")

    all_results = {}
    for variant_name in variant_names:
        variant_config = JOURNAL_VARIANTS[variant_name]
        results = process_variant(
            variant_name=variant_name,
            variant_config=variant_config,
            conversations=conversations,
            source_dir=source_dir,
            output_base=output_base,
            use_cache=use_cache,
        )
        all_results[variant_name] = results

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    for variant_name, results in all_results.items():
        total = sum(r["entries_written"] for r in results.values())
        cached = sum(r["cached"] for r in results.values())
        sessions = sum(r["sessions"] for r in results.values())
        print(f"  {variant_name}: {total}/{sessions} entries "
              f"({cached} cached)")
        print(f"    Output: {output_base / f'results-journal-{variant_name}'}")

    print(f"\nNext: Run eval with journal context:")
    for variant_name in variant_names:
        print(f"  python3 -m locomo.evaluate "
              f"--results-dir data/results-journal-{variant_name} "
              f"--with-journal --conversations 0-9")


if __name__ == "__main__":
    main()
