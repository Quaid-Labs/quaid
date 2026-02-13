#!/usr/bin/env python3
"""
reprocess.py — Re-ingest LoCoMo from cached extractions with full janitor pipeline.

Uses the 272 cached Opus extractions (zero extraction cost) and runs the full
production-matching pipeline:

1. Per-session: store facts + edges + snippets + journal + embeddings backfill
2. Periodic (every 5 sessions + final): snippet review + journal distillation
3. Per-conversation end: full janitor (temporal, duplicates, contradictions, decay)
4. Post-janitor: final snippet review + journal distillation

This mirrors production Quaid behavior where:
- Facts are extracted and stored at each compaction event
- Snippets and journal entries accumulate during the day
- Janitor runs nightly after all day's sessions are complete
- Snippet fold/review + journal distillation run as part of janitor

Cost: ~$5-15 total (Haiku reranker for dedup + Opus for contradictions + snippet/journal review)
No Opus extraction cost — all 272 session extractions are cached to disk.

Usage:
    source memory-stress-test/test.env

    # Full reprocess (all 10 conversations)
    python3 memory-stress-test/runner/locomo/reprocess.py

    # Single conversation (smoke test)
    python3 memory-stress-test/runner/locomo/reprocess.py --conversations 0

    # Dry run (show what would happen, no API calls)
    python3 memory-stress-test/runner/locomo/reprocess.py --dry-run
"""
import argparse
import json
import os
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

# Bootstrap quaid imports
import runner  # noqa: F401

from locomo.dataset import Conversation, load_dataset
from locomo.ingest import (
    CACHE_DIR,
    _load_cached_extraction,
    _run_periodic_review,
    _set_workspace,
    _setup_conversation_workspace,
    _switch_to_db,
    _write_snippets_to_file,
    parse_conv_range,
    run_janitor_cycle,
    store_facts,
)
from llm_clients import get_token_usage, estimate_cost, reset_token_usage

import soul_snippets

PRODUCTION_DB = os.path.join(os.environ.get("QUAID_WORKSPACE", "."), "data", "memory.db")
DEFAULT_RESULTS_DIR = _DIR / "data" / "results-fulljanitor"

# Janitor tasks to run at end of each conversation (production-matching)
# - temporal: resolve relative dates (no LLM, fast)
# - duplicates: find and merge duplicate facts (Haiku reranker)
# - decay: Ebbinghaus confidence decay (no LLM, fast)
#
# NOTE: 'contradictions' excluded because it only checks pending/approved facts
# (janitor.py:492). Our ingest stores facts as status="active" to skip the
# expensive Opus review step, so contradiction detection would find zero candidates.
# In production, contradictions are caught during the pending→active review phase.
#
# NOTE: 'decay' is a fast no-op here (facts just created = recent accessed_at),
# included for completeness.
END_OF_CONV_JANITOR_TASKS = "temporal,duplicates,decay"


def reprocess_conversation(
    conversation: Conversation,
    results_dir: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Re-ingest a conversation from cached extractions with full janitor.

    Args:
        conversation: Parsed LoCoMo conversation
        results_dir: Directory for per-conversation results
        dry_run: If True, show plan but don't execute

    Returns:
        Dict with per-session results and totals
    """
    conv_id = conversation.sample_id
    owner_id = conv_id
    conv_dir = results_dir / conv_id
    conv_dir.mkdir(parents=True, exist_ok=True)

    # Verify all cached extractions exist
    missing = []
    for session in conversation.sessions:
        cached = _load_cached_extraction(conv_id, session.session_num)
        if cached is None:
            missing.append(session.session_num)

    if missing:
        print(f"  ERROR: Missing cached extractions for sessions: {missing}")
        return {"error": f"missing_cache_{missing}", "conversation_id": conv_id}

    num_sessions = len(conversation.sessions)
    print(f"\n{'=' * 70}")
    print(f"Reprocessing {conv_id}: {conversation.speaker_a} & {conversation.speaker_b}")
    print(f"  Sessions: {num_sessions}, Turns: {conversation.num_turns}")
    print(f"  QA pairs: {len(conversation.qa_pairs)} ({len(conversation.scored_qa_pairs)} scored)")
    print(f"  Cache: {num_sessions} extractions found in {CACHE_DIR}")
    print(f"{'=' * 70}")

    if dry_run:
        print("  [DRY RUN] Would create fresh DB, re-ingest, run full janitor")
        return {"conversation_id": conv_id, "dry_run": True}

    # Create fresh DB
    db_path = conv_dir / "memory.db"
    _switch_to_db(db_path, fresh=True)

    # Set up per-conversation workspace (SPEAKERS.md + journal dir)
    _set_workspace(conv_dir)
    _setup_conversation_workspace(conv_dir, conversation)

    session_results = []
    totals = {
        "facts_extracted": 0,
        "facts_stored": 0,
        "edges_created": 0,
        "duplicates": 0,
        "skipped": 0,
        "errors": [],
        "snippets_extracted": 0,
        "journal_entries_written": 0,
    }
    review_stats_all = []

    # ── Phase 1: Per-session ingest (from cache) ──────────────────
    for i, session in enumerate(conversation.sessions):
        session_num = session.session_num
        session_id = f"locomo-{conv_id}-s{session_num}"

        # Parse session date for fact created_at
        created_at = None
        if session.date_time:
            for fmt in [
                "%I:%M %p on %d %B, %Y",
                "%I:%M %p on %d %B %Y",
                "%H:%M on %d %B, %Y",
            ]:
                try:
                    dt = datetime.strptime(session.date_time.strip(), fmt)
                    created_at = dt.isoformat()
                    break
                except ValueError:
                    continue

        # Load cached extraction
        cached = _load_cached_extraction(conv_id, session_num)
        if isinstance(cached, list):
            # Old format: just facts list
            extraction_data = {"facts": cached, "soul_snippets": {}, "journal_entries": {}}
        else:
            extraction_data = cached

        facts = extraction_data.get("facts", [])

        # Store facts + edges
        store_result = store_facts(
            facts=facts,
            owner_id=owner_id,
            session_id=session_id,
            created_at=created_at,
        )

        # Write snippets
        snippets_written = 0
        snippets_data = extraction_data.get("soul_snippets", {})
        for filename, snippet_list in snippets_data.items():
            if filename != "SPEAKERS.md" or "/" in filename or ".." in filename:
                continue
            if isinstance(snippet_list, list):
                snippets_written += _write_snippets_to_file(
                    conv_dir, filename, snippet_list, session,
                )

        # Write journal entries
        journal_written = 0
        journal_data = extraction_data.get("journal_entries", {})
        for filename, entry_text in journal_data.items():
            if filename != "SPEAKERS.md" or "/" in filename or ".." in filename:
                continue
            if isinstance(entry_text, list):
                entry_text = "\n\n".join(entry_text)
            if entry_text and entry_text.strip():
                date_str = created_at[:10] if created_at else None
                try:
                    wrote = soul_snippets.write_journal_entry(
                        filename, entry_text.strip(),
                        trigger=f"Session {session_num}",
                        date_str=date_str,
                    )
                    if wrote:
                        journal_written += 1
                except Exception as e:
                    totals["errors"].append(f"journal write error: {e}")

        # Per-session janitor: embeddings only (cheap, local)
        # Run unconditionally — if Ollama flickered during store(), some facts
        # may lack embeddings even when facts_stored==0 (all dupes this session
        # but prior session had embedding failures).
        janitor_result = run_janitor_cycle("embeddings")

        # Periodic snippet review + journal distillation (every 5 sessions + final)
        review_stats = _run_periodic_review(i, num_sessions)
        if review_stats:
            review_stats_all.append(review_stats)

        # Track results
        sr = {
            "session_num": session_num,
            "date_time": session.date_time,
            "turns": len(session.turns),
            "facts_extracted": len(facts),
            "facts_stored": store_result["facts_stored"],
            "edges_created": store_result["edges_created"],
            "duplicates": store_result["duplicates"],
            "skipped": store_result["skipped"],
            "snippets_written": snippets_written,
            "journal_written": journal_written,
            "janitor": janitor_result,
        }
        if review_stats:
            sr["review"] = review_stats
        session_results.append(sr)

        # Aggregate
        totals["facts_extracted"] += len(facts)
        totals["facts_stored"] += store_result["facts_stored"]
        totals["edges_created"] += store_result["edges_created"]
        totals["duplicates"] += store_result["duplicates"]
        totals["skipped"] += store_result["skipped"]
        totals["snippets_extracted"] += snippets_written
        totals["journal_entries_written"] += journal_written
        totals["errors"].extend(store_result["errors"])

        if (i + 1) % 5 == 0 or i == num_sessions - 1:
            print(f"  [{i+1}/{num_sessions}] Stored: {store_result['facts_stored']}, "
                  f"Edges: {store_result['edges_created']}, Dupes: {store_result['duplicates']}, "
                  f"Snippets: {snippets_written}, Journal: {journal_written}")

    # ── Phase 2: End-of-conversation janitor (production-matching) ──
    print(f"\n  [janitor] Running end-of-conversation janitor: {END_OF_CONV_JANITOR_TASKS}")
    janitor_final = run_janitor_cycle(END_OF_CONV_JANITOR_TASKS)
    print(f"  [janitor] Results: {janitor_final}")

    # Fix dedup merge owner_id bug: janitor.py hardcodes owner_id="default"
    # in store_memory() calls for merged facts. Update any such facts to use
    # the correct conversation owner_id.
    import sqlite3 as _sqlite3
    try:
        conn = _sqlite3.connect(str(db_path))
        fixed = conn.execute(
            "UPDATE nodes SET owner_id = ? WHERE owner_id = 'default' AND source = 'dedup_merge'",
            (owner_id,),
        ).rowcount
        conn.commit()
        conn.close()
        if fixed > 0:
            print(f"  [fixup] Fixed owner_id on {fixed} dedup-merged facts")
    except Exception as e:
        print(f"  [fixup] Warning: could not fix dedup owner_ids: {e}")

    # ── Phase 3: Post-janitor snippet review + journal distillation ──
    # Janitor may have merged/removed facts — run one final review cycle
    print(f"  [review] Running post-janitor snippet review + journal distillation...")
    try:
        sr = soul_snippets.run_soul_snippets_review(dry_run=False)
        post_janitor_snippets = {
            "total": sr.get("total_snippets", 0),
            "folded": sr.get("folded", 0),
            "rewritten": sr.get("rewritten", 0),
            "discarded": sr.get("discarded", 0),
        }
        print(f"    Snippets: {post_janitor_snippets}")
    except Exception as e:
        post_janitor_snippets = {"error": str(e)}
        print(f"    Snippets error: {e}")

    try:
        jr = soul_snippets.run_journal_distillation(
            dry_run=False, force_distill=True,
        )
        post_janitor_journal = {
            "total_entries": jr.get("total_entries", 0),
            "files_distilled": jr.get("files_distilled", 0),
            "additions": jr.get("additions", 0),
            "edits": jr.get("edits", 0),
        }
        print(f"    Journal: {post_janitor_journal}")
    except Exception as e:
        post_janitor_journal = {"error": str(e)}
        print(f"    Journal error: {e}")

    # Capture final core markdown state
    core_markdown_stats = {}
    speakers_path = conv_dir / "SPEAKERS.md"
    if speakers_path.exists():
        lines = speakers_path.read_text(encoding="utf-8").splitlines()
        core_markdown_stats["SPEAKERS.md"] = len(lines)

    # Capture final DB stats
    import sqlite3
    db_stats = {}
    try:
        conn = sqlite3.connect(str(db_path))
        statuses = dict(conn.execute(
            "SELECT status, COUNT(*) FROM nodes GROUP BY status"
        ).fetchall())
        db_stats["nodes"] = sum(statuses.values())
        db_stats["active"] = statuses.get("active", 0)
        db_stats["pending"] = statuses.get("pending", 0)
        db_stats["archived"] = statuses.get("archived", 0)
        db_stats["edges"] = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        try:
            db_stats["dedup_merges"] = conn.execute(
                "SELECT COUNT(*) FROM dedup_log"
            ).fetchone()[0]
        except Exception:
            db_stats["dedup_merges"] = 0
        conn.close()
    except Exception as e:
        db_stats["error"] = str(e)

    # Save results
    result = {
        "conversation_id": conv_id,
        "speakers": f"{conversation.speaker_a} & {conversation.speaker_b}",
        "sessions": session_results,
        "totals": totals,
        "db_path": str(db_path),
        "db_stats": db_stats,
        "janitor_final": janitor_final,
        "post_janitor_review": {
            "snippets": post_janitor_snippets,
            "journal": post_janitor_journal,
        },
        "review_stats": review_stats_all,
        "core_markdown_stats": core_markdown_stats,
    }

    results_file = conv_dir / "ingestion_results.json"
    with open(results_file, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n  Summary for {conv_id}:")
    print(f"    Facts stored:    {totals['facts_stored']}")
    print(f"    Edges created:   {totals['edges_created']}")
    print(f"    Duplicates:      {totals['duplicates']}")
    print(f"    Snippets:        {totals['snippets_extracted']}")
    print(f"    Journal entries: {totals['journal_entries_written']}")
    print(f"    DB: {db_stats.get('nodes', '?')} nodes "
          f"({db_stats.get('active', '?')} active, "
          f"{db_stats.get('pending', '?')} pending), "
          f"{db_stats.get('edges', '?')} edges, "
          f"{db_stats.get('dedup_merges', '?')} dedup merges")
    if core_markdown_stats:
        for fname, line_count in core_markdown_stats.items():
            print(f"    {fname}: {line_count} lines")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Re-ingest LoCoMo from cached extractions with full janitor pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
This script re-ingests LoCoMo conversations from cached Opus extractions,
adding the full janitor pipeline that was missing from the original run.

Zero Opus extraction cost — all 272 session extractions are cached.
Cost is ~$5-15 for janitor (dedup + contradictions + snippet review).

Pipeline mirrors production:
  1. Per-session: store facts + edges + snippets + journal + embeddings
  2. Every 5 sessions: snippet review (FOLD/REWRITE/DISCARD) + journal distillation
  3. End of conversation: temporal + duplicates + contradictions + decay
  4. Post-janitor: final snippet review + journal distillation

After reprocessing, run evaluation with:
  python3 run_locomo.py --conversations all --skip-ingest \\
    --results-dir memory-stress-test/runner/locomo/data/results-fulljanitor \\
    --answer-model haiku --judge-model gpt-4o-mini --trials 3
        """,
    )
    parser.add_argument(
        "--conversations", default="all",
        help="Which conversations: '0', '0-2', '0,3,5', 'all' (default: all)"
    )
    parser.add_argument(
        "--results-dir",
        default=str(DEFAULT_RESULTS_DIR),
        help=f"Output directory for results (default: {DEFAULT_RESULTS_DIR})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show plan but don't execute (no API calls)"
    )
    args = parser.parse_args()

    # Safety check
    db_path = os.environ.get("MEMORY_DB_PATH", "")
    if db_path and os.path.abspath(db_path) == os.path.abspath(PRODUCTION_DB):
        print("FATAL: MEMORY_DB_PATH points to production database!")
        return 1

    # Raise cost cap for full janitor runs
    if not os.environ.get("JANITOR_COST_CAP"):
        os.environ["JANITOR_COST_CAP"] = "50.0"

    conv_indices = parse_conv_range(args.conversations)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    reset_token_usage()
    t_start = time.monotonic()

    print(f"\nLoCoMo Reprocess — Full Janitor Pipeline")
    print(f"  Conversations: {conv_indices}")
    print(f"  Results dir: {results_dir}")
    print(f"  Cache dir: {CACHE_DIR}")
    print(f"  Per-session janitor: embeddings")
    print(f"  End-of-conv janitor: {END_OF_CONV_JANITOR_TASKS}")
    if args.dry_run:
        print(f"  Mode: DRY RUN")

    # Load dataset
    conversations = load_dataset()

    all_results = []
    grand_totals = {
        "facts_extracted": 0,
        "facts_stored": 0,
        "edges_created": 0,
        "duplicates": 0,
        "snippets_extracted": 0,
        "journal_entries_written": 0,
        "conversations_processed": 0,
    }

    for idx in conv_indices:
        if idx < 0 or idx >= len(conversations):
            print(f"WARNING: Conversation index {idx} out of range, skipping")
            continue

        r = reprocess_conversation(
            conversation=conversations[idx],
            results_dir=results_dir,
            dry_run=args.dry_run,
        )
        all_results.append(r)

        if not r.get("dry_run") and not r.get("error"):
            t = r["totals"]
            grand_totals["facts_extracted"] += t["facts_extracted"]
            grand_totals["facts_stored"] += t["facts_stored"]
            grand_totals["edges_created"] += t["edges_created"]
            grand_totals["duplicates"] += t["duplicates"]
            grand_totals["snippets_extracted"] += t.get("snippets_extracted", 0)
            grand_totals["journal_entries_written"] += t.get("journal_entries_written", 0)
            grand_totals["conversations_processed"] += 1

    elapsed = round(time.monotonic() - t_start, 1)

    # Token usage and cost
    token_usage = get_token_usage()
    est_cost = estimate_cost()

    print(f"\n{'=' * 70}")
    print(f"LoCoMo Reprocess Complete")
    print(f"  Conversations: {grand_totals['conversations_processed']}")
    print(f"  Facts stored:    {grand_totals['facts_stored']}")
    print(f"  Edges created:   {grand_totals['edges_created']}")
    print(f"  Duplicates:      {grand_totals['duplicates']}")
    print(f"  Snippets:        {grand_totals['snippets_extracted']}")
    print(f"  Journal entries: {grand_totals['journal_entries_written']}")
    print(f"  Token usage: {token_usage['input_tokens']:,} input, "
          f"{token_usage['output_tokens']:,} output")
    print(f"  Estimated cost: ${est_cost:.4f}")
    print(f"  Time: {elapsed}s")
    print(f"{'=' * 70}")

    # Save summary
    summary = {
        "metadata": {
            "timestamp": datetime.now().isoformat(),
            "conversations": conv_indices,
            "per_session_janitor": "embeddings",
            "end_of_conv_janitor": END_OF_CONV_JANITOR_TASKS,
            "elapsed_seconds": elapsed,
        },
        "token_usage": {
            "input_tokens": token_usage["input_tokens"],
            "output_tokens": token_usage["output_tokens"],
            "cache_read_tokens": token_usage["cache_read_tokens"],
            "estimated_cost_usd": est_cost,
        },
        "grand_totals": grand_totals,
        "conversations": all_results,
    }

    summary_file = results_dir / "reprocess_summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n  Summary saved to {summary_file}")

    print(f"\nNext step: run evaluation with")
    print(f"  python3 memory-stress-test/runner/locomo/run_locomo.py \\")
    print(f"    --conversations all --skip-ingest \\")
    print(f"    --results-dir {results_dir} \\")
    print(f"    --answer-model haiku --judge-model gpt-4o-mini --trials 3")

    return 0


if __name__ == "__main__":
    sys.exit(main())
