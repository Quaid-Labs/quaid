#!/usr/bin/env python3
"""
LoCoMo ingestion pipeline — extract and store memories from LoCoMo conversations.

For each conversation:
1. Create a fresh test DB (isolated per conversation)
2. Process sessions sequentially (each session = one compaction event)
3. Extract facts via Opus (same pipeline as production)
4. Store facts + edges
5. Run janitor after each session (embeddings, review, duplicates)

Supports cached mode: save extraction results to disk, reuse across runs.

Usage:
    source memory-stress-test/test.env
    python3 -m locomo.ingest --conversations 0         # Single conversation
    python3 -m locomo.ingest --conversations 0-2       # Range
    python3 -m locomo.ingest --cached                  # Use cached extractions
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
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
from memory_graph import store, create_edge, get_graph
from llm_clients import call_high_reasoning, parse_json_response

from locomo.dataset import (
    Conversation,
    Session,
    format_session_transcript,
    load_dataset,
)

import soul_snippets
from workspace_audit import check_bloat

# Cache directory for extraction results
CACHE_DIR = _DIR / "data" / "extraction_cache"

# LoCoMo-adapted extraction prompt — same structure as production but
# handles two-speaker conversations instead of user/assistant.
LOCOMO_EXTRACTION_PROMPT = """You are a memory extraction system. You will receive a conversation transcript between two people. Your job is to extract personal facts AND relationship edges from this conversation.

This is a PERSONAL knowledge base for tracking what's discussed in these conversations.

EXTRACT facts that are EXPLICITLY STATED OR CONFIRMED in the conversation. Never infer, speculate, or extrapolate.

SPEAKERS: {speaker_a} and {speaker_b}

WHAT TO EXTRACT:
- Personal facts about either speaker (names, relationships, jobs, birthdays, health, locations)
- Preferences and opinions explicitly stated ("I like X", "I prefer Y", "I hate Z")
- Decisions with reasoning ("decided to use X because Y")
- Significant events or milestones ("graduated from X", "started new job at Y", "moving to Z")
- Important relationships (family, friends, colleagues, partners)
- Emotional reactions or sentiments about specific things
- Plans and future events ("going to visit X next month")
- Life changes ("got engaged", "adopted a dog", "started a new diet")

WHAT NOT TO EXTRACT:
- Small talk greetings ("Hey, how are you?")
- Conversational filler ("That's great!", "Oh wow!")
- Vague statements without specific content
- Repetitions of previously stated facts within same session
- Meta-conversation about the chat itself

QUALITY RULES:
- Use the speaker's name as subject, third person (e.g., "{speaker_a} started a new job at...")
- Each fact must be self-contained and understandable without context
- Be specific: "{speaker_a} likes Thai food" > "{speaker_a} likes food"
- Mark extraction_confidence "high" for clearly stated facts, "medium" for somewhat ambiguous, "low" for weak signals
- Extract comprehensively — missed facts are gone forever

KEYWORDS (per fact):
For each fact, provide 3-5 searchable keywords.
Format as a space-separated string.

EDGE EXTRACTION:
For relationship facts, extract edges connecting entities.

EDGE DIRECTION RULES:
- parent_of: PARENT is subject
- sibling_of, spouse_of, friend_of: alphabetical order (symmetric)
- has_pet: OWNER is subject
- works_at, lives_at: PERSON is subject

EDGE FORMAT:
- subject: Source entity name (exact as mentioned)
- relation: One of: parent_of, sibling_of, spouse_of, has_pet, friend_of, works_at, lives_at, owns, colleague_of, knows
- object: Target entity name

SOUL SNIPPETS (personality & relationship observations):
Extract bullet-point observations about each speaker's personality, preferences, habits,
and relationship dynamics. These get folded into a profile document over time.
Target file: "SPEAKERS.md" (single file for both speakers, like production USER.md).

JOURNAL ENTRY (reflective summary):
Write a short reflective paragraph about what was learned about the speakers and their
relationship in this session. This is diary-style, capturing themes and patterns.
Target file: "SPEAKERS.md"

Respond with JSON only:
{{
  "facts": [
    {{
      "text": "the extracted fact",
      "category": "fact|preference|decision|relationship",
      "extraction_confidence": "high|medium|low",
      "keywords": "space separated search terms",
      "edges": [
        {{"subject": "Entity A", "relation": "relation_type", "object": "Entity B"}}
      ]
    }}
  ],
  "soul_snippets": {{
    "SPEAKERS.md": ["personality trait, preference, or relationship observation about either speaker"]
  }},
  "journal_entries": {{
    "SPEAKERS.md": "Reflective paragraph about what was learned about the speakers and their relationship in this session"
  }}
}}

If nothing worth capturing, respond: {{"facts": [], "soul_snippets": {{}}, "journal_entries": {{}}}}"""

EXTRACTION_USER_PREFIX = "Extract memorable facts from this conversation:\n\n"

# Confidence mapping matching production
_CONFIDENCE_MAP = {"high": 0.9, "medium": 0.6, "low": 0.3}
_VALID_CATEGORIES = {"fact", "preference", "decision", "relationship", "entity"}


# How often to run snippet review + journal distillation (in sessions)
_REVIEW_INTERVAL = 5


def _set_workspace(conv_dir: Path) -> None:
    """Switch workspace to per-conversation directory.

    Monkey-patches soul_snippets and workspace_audit module globals so that
    snippet/journal/audit operations target the conversation-specific directory
    instead of the production workspace.
    """
    os.environ["QUAID_WORKSPACE"] = str(conv_dir)
    soul_snippets.WORKSPACE_DIR = conv_dir
    soul_snippets.BACKUP_DIR = conv_dir / "backups" / "soul-snippets"

    # Patch target_files so snippet review + journal distillation find SPEAKERS.md
    soul_snippets._get_target_files = lambda: ["SPEAKERS.md"]

    # Ensure snippet/journal features are always enabled in benchmark
    # (production config may have them disabled)
    soul_snippets._is_enabled = lambda: True
    soul_snippets._is_snippets_enabled = lambda: True

    # Provide meaningful config for SPEAKERS.md (not in production config)
    soul_snippets._get_core_markdown_config = lambda filename: {
        "purpose": "Speaker personality traits, preferences, and relationship dynamics",
        "maxLines": 200,
    }

    import workspace_audit as wa
    wa.WORKSPACE_DIR = conv_dir
    # Patch monitored files so check_bloat checks SPEAKERS.md, not production files
    wa.get_monitored_files = lambda: {
        "SPEAKERS.md": {
            "purpose": "Speaker personality traits, preferences, and relationship dynamics",
            "maxLines": 200,
        },
    }


def _setup_conversation_workspace(
    conv_dir: Path, conversation: Conversation, fresh: bool = True,
) -> Path:
    """Create per-conversation core markdown and journal directory.

    Args:
        conv_dir: Conversation-specific directory
        conversation: Conversation metadata (speaker names)
        fresh: If True, clean any stale workspace files from prior runs

    Returns path to SPEAKERS.md.
    """
    if fresh:
        # Clean stale workspace files from prior runs to avoid dedup hits
        # and content doubling (DB is recreated fresh, workspace should match)
        for pattern in ["*.snippets.md", "journal/*.journal.md",
                        "journal/archive/*.md",
                        "journal/.distillation-state.json"]:
            for stale in conv_dir.glob(pattern):
                stale.unlink()

    speakers_path = conv_dir / "SPEAKERS.md"
    if fresh or not speakers_path.exists():
        speakers_path.write_text(
            f"# {conversation.speaker_a} & {conversation.speaker_b}\n\n"
            f"(Personality traits, preferences, life details, and "
            f"relationship dynamics learned through conversations)\n",
            encoding="utf-8",
        )

    journal_dir = conv_dir / "journal"
    journal_dir.mkdir(exist_ok=True)

    return speakers_path


def _write_snippets_to_file(
    conv_dir: Path,
    filename: str,
    snippet_list: list,
    session: Session,
) -> int:
    """Write extracted snippets to a .snippets.md file in production format.

    Returns number of snippets written.
    """
    # Filter empty snippets before counting or writing
    valid_snippets = [s for s in snippet_list if s and s.strip()]
    if not valid_snippets:
        return 0

    base_name = filename.removesuffix(".md")
    snippets_path = conv_dir / f"{base_name}.snippets.md"

    # Build section in production format
    date_str = session.date_time or "unknown"
    header = f"## Session {session.session_num} — {date_str}"
    bullets = "\n".join(f"- {s}" for s in valid_snippets)

    if snippets_path.exists():
        existing = snippets_path.read_text(encoding="utf-8")
    else:
        existing = f"# {base_name.upper()} Snippets\n"

    new_content = f"{existing.rstrip()}\n\n{header}\n{bullets}\n"
    snippets_path.write_text(new_content, encoding="utf-8")
    return len(valid_snippets)


def _run_periodic_review(session_num: int, total_sessions: int) -> dict:
    """Run snippet review + journal distillation if it's time.

    Runs every _REVIEW_INTERVAL sessions and after the final session.

    Returns dict with review stats (empty if skipped).
    """
    is_interval = (session_num + 1) % _REVIEW_INTERVAL == 0
    is_final = session_num == total_sessions - 1

    if not is_interval and not is_final:
        return {}

    label = "final" if is_final else f"periodic (every {_REVIEW_INTERVAL})"
    print(f"    [review] Running {label} snippet review + journal distillation...")

    stats = {}
    try:
        sr = soul_snippets.run_soul_snippets_review(dry_run=False)
        stats["snippets"] = {
            "total": sr.get("total_snippets", 0),
            "folded": sr.get("folded", 0),
            "rewritten": sr.get("rewritten", 0),
            "discarded": sr.get("discarded", 0),
        }
        print(f"      Snippets: {stats['snippets']}")
    except Exception as e:
        stats["snippets_error"] = str(e)
        print(f"      Snippets error: {e}")

    try:
        jr = soul_snippets.run_journal_distillation(
            dry_run=False, force_distill=True,
        )
        stats["journal"] = {
            "total_entries": jr.get("total_entries", 0),
            "files_distilled": jr.get("files_distilled", 0),
            "additions": jr.get("additions", 0),
            "edits": jr.get("edits", 0),
        }
        print(f"      Journal: {stats['journal']}")
    except Exception as e:
        stats["journal_error"] = str(e)
        print(f"      Journal error: {e}")

    # Run workspace audit after the final session
    if is_final:
        try:
            bloat = check_bloat()
            stats["workspace"] = {
                "files_checked": len(bloat),
                "over_limit": sum(1 for v in bloat.values() if v.get("over_limit")),
                "total_lines": sum(v.get("lines", 0) for v in bloat.values()),
                "details": {k: {"lines": v["lines"], "maxLines": v.get("maxLines", 0)}
                            for k, v in bloat.items()},
            }
            print(f"      Workspace: {stats['workspace']['files_checked']} files, "
                  f"{stats['workspace']['over_limit']} over limit")
        except Exception as e:
            stats["workspace_error"] = str(e)
            print(f"      Workspace audit error: {e}")

    return stats


def _get_cache_path(conv_id: str, session_num: int) -> Path:
    """Get the cache file path for an extraction result."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{conv_id}_session_{session_num:03d}.json"


def _load_cached_extraction(conv_id: str, session_num: int) -> Optional[List[Dict]]:
    """Load cached extraction results if available."""
    path = _get_cache_path(conv_id, session_num)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def _save_extraction_cache(conv_id: str, session_num: int, data: Any):
    """Save extraction results to cache (full dict or legacy list)."""
    path = _get_cache_path(conv_id, session_num)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _parse_extraction_response(raw: Optional[str]) -> Dict[str, Any]:
    """Parse the JSON response from extraction, handling markdown fencing.

    Returns the full parsed dict with 'facts', 'soul_snippets', 'journal_entries' keys.
    """
    empty: Dict[str, Any] = {"facts": [], "soul_snippets": {}, "journal_entries": {}}
    if not raw:
        return empty
    try:
        parsed = parse_json_response(raw)
        if parsed and "facts" in parsed:
            return parsed
    except Exception:
        pass

    # Regex fallback: strip markdown fencing
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and "facts" in parsed:
            return parsed
    except Exception:
        pass

    return empty


def _switch_to_db(db_path: Path, fresh: bool = True) -> None:
    """Switch the memory system to use a specific database.

    Since DB_PATH is captured at import time, we can't just set the env var.
    Instead we directly replace the cached singleton with a new MemoryGraph
    instance pointing to the desired path.

    Args:
        db_path: Path to the database file
        fresh: If True, delete any existing DB at that path first
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if fresh and db_path.exists():
        db_path.unlink()
    # Also remove WAL/SHM files
    if fresh:
        for suffix in ["-wal", "-shm"]:
            wal = Path(str(db_path) + suffix)
            if wal.exists():
                wal.unlink()

    # Set env for any code that reads it dynamically
    os.environ["MEMORY_DB_PATH"] = str(db_path)

    # Checkpoint and close the old graph's WAL before switching
    import memory_graph as mg
    from memory_graph import MemoryGraph
    if hasattr(mg, '_graph') and mg._graph is not None:
        try:
            with mg._graph._get_conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    # Replace the singleton graph with one pointing to the new DB.
    # DB_PATH default param is captured at class definition time, so we
    # must explicitly pass db_path to the constructor.
    mg._graph = MemoryGraph(db_path=db_path)

    print(f"  [db] {'Created fresh' if fresh else 'Switched to'} DB at {db_path}")


def extract_session(
    conversation: Conversation,
    session: Session,
    use_cache: bool = False,
) -> Dict[str, Any]:
    """Extract facts from a single LoCoMo session via Opus.

    Args:
        conversation: The parent conversation (for speaker names, sample_id)
        session: The session to extract from
        use_cache: If True, use cached extraction results when available

    Returns:
        Dict with extraction results: facts, timing, cost info
    """
    conv_id = conversation.sample_id
    session_num = session.session_num

    # Check cache first
    if use_cache:
        cached = _load_cached_extraction(conv_id, session_num)
        if cached is not None:
            # Cached data may be old-format (list) or new-format (dict)
            if isinstance(cached, list):
                facts = cached
                extraction_data = {"facts": cached, "soul_snippets": {}, "journal_entries": {}}
            else:
                facts = cached.get("facts", [])
                extraction_data = cached
            return {
                "session_num": session_num,
                "facts": facts,
                "soul_snippets": extraction_data.get("soul_snippets", {}),
                "journal_entries": extraction_data.get("journal_entries", {}),
                "cached": True,
                "elapsed_seconds": 0.0,
            }

    # Format transcript
    transcript = format_session_transcript(session)
    if not transcript.strip():
        return {
            "session_num": session_num,
            "facts": [],
            "soul_snippets": {},
            "journal_entries": {},
            "cached": False,
            "elapsed_seconds": 0.0,
        }

    # Build extraction prompt with speaker names
    system_prompt = LOCOMO_EXTRACTION_PROMPT.format(
        speaker_a=conversation.speaker_a,
        speaker_b=conversation.speaker_b,
    )
    user_content = EXTRACTION_USER_PREFIX + transcript[:100000]

    print(f"    [extract] Session {session_num}: calling Opus "
          f"({len(transcript)} chars, {len(session.turns)} turns)...")

    t0 = time.monotonic()
    raw_response, duration = call_high_reasoning(
        prompt=user_content,
        system_prompt=system_prompt,
        max_tokens=4096,
    )
    elapsed = time.monotonic() - t0

    extraction_data = _parse_extraction_response(raw_response)
    facts = extraction_data.get("facts", [])

    # Cache the full extraction (facts + snippets + journal)
    _save_extraction_cache(conv_id, session_num, extraction_data)

    return {
        "session_num": session_num,
        "facts": facts,
        "soul_snippets": extraction_data.get("soul_snippets", {}),
        "journal_entries": extraction_data.get("journal_entries", {}),
        "cached": False,
        "elapsed_seconds": round(elapsed, 2),
    }


def store_facts(
    facts: List[Dict],
    owner_id: str,
    session_id: str,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Store extracted facts and their edges into the database.

    Args:
        facts: List of fact dicts from extraction
        owner_id: Owner identifier for the conversation
        session_id: Session ID for tracking
        created_at: ISO timestamp for fact creation (from session date_time)

    Returns:
        Dict with store results: stored, duplicates, edges, errors
    """
    result = {
        "facts_stored": 0,
        "duplicates": 0,
        "edges_created": 0,
        "skipped": 0,
        "errors": [],
    }

    for fact in facts:
        text = fact.get("text", "").strip()
        if not text or len(text.split()) < 3:
            result["skipped"] += 1
            continue

        category = fact.get("category", "fact")
        if category not in _VALID_CATEGORIES:
            category = "fact"

        confidence_str = fact.get("extraction_confidence", "medium")
        confidence = _CONFIDENCE_MAP.get(confidence_str, 0.6)
        keywords = fact.get("keywords", "")

        try:
            store_result = store(
                text=text,
                category=category,
                knowledge_type=category if category in ("preference", "fact") else "fact",
                confidence=confidence,
                keywords=keywords,
                privacy="shared",
                source_type="user",
                owner_id=owner_id,
                session_id=session_id,
                status="active",
                created_at=created_at,
            )

            if store_result.get("status") != "duplicate":
                result["facts_stored"] += 1
                stored_id = store_result.get("id")

                # Create edges for this fact
                for edge in fact.get("edges", []):
                    subj = edge.get("subject", "")
                    rel = edge.get("relation", "")
                    obj = edge.get("object", "")
                    if subj and rel and obj:
                        try:
                            create_edge(
                                subj, rel, obj,
                                source_fact_id=stored_id,
                                owner_id=owner_id,
                            )
                            result["edges_created"] += 1
                        except Exception as e:
                            result["errors"].append(
                                f"edge error: {subj}->{rel}->{obj}: {e}"
                            )
            else:
                result["duplicates"] += 1
        except Exception as e:
            result["errors"].append(f"store error: {text[:60]}: {e}")

    return result


def run_janitor_cycle(tasks: str = "embeddings,review,duplicates") -> Dict[str, Any]:
    """Run a janitor cycle on the current DB.

    Args:
        tasks: Comma-separated task names or single task name

    Returns:
        Dict with janitor results per task
    """
    from janitor import run_task_optimized

    results = {}
    for task in tasks.split(","):
        task = task.strip()
        if not task:
            continue
        print(f"    [janitor] Running {task}...")
        try:
            r = run_task_optimized(task, dry_run=False, incremental=False)
            results[task] = {
                "success": r.get("success", True) if isinstance(r, dict) else True,
                "error": r.get("error") if isinstance(r, dict) else None,
            }
        except Exception as e:
            results[task] = {"success": False, "error": str(e)}
    return results


def ingest_conversation(
    conversation: Conversation,
    results_dir: Path,
    use_cache: bool = False,
    janitor_tasks: str = "embeddings",
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Ingest a full LoCoMo conversation into Quaid.

    For each session:
    1. Extract facts via Opus (or cache)
    2. Store facts + edges
    3. Run janitor cycle

    Args:
        conversation: Parsed LoCoMo conversation
        results_dir: Directory for per-conversation results
        use_cache: Whether to use cached extraction results
        janitor_tasks: Comma-separated janitor tasks to run after each session
        owner_id: Override owner (default: conversation sample_id)

    Returns:
        Dict with per-session results and totals
    """
    conv_id = conversation.sample_id
    if owner_id is None:
        owner_id = conv_id

    conv_dir = results_dir / conv_id
    conv_dir.mkdir(parents=True, exist_ok=True)

    # Create fresh DB for this conversation
    db_path = conv_dir / "memory.db"
    _switch_to_db(db_path, fresh=True)

    # Set up per-conversation workspace (core markdown + journal dir)
    _set_workspace(conv_dir)
    _setup_conversation_workspace(conv_dir, conversation)

    print(f"\n{'='*60}")
    print(f"Ingesting {conv_id}: {conversation.speaker_a} & {conversation.speaker_b}")
    print(f"  Sessions: {conversation.num_sessions}, Turns: {conversation.num_turns}")
    print(f"  QA pairs: {len(conversation.qa_pairs)} ({len(conversation.scored_qa_pairs)} scored)")
    print(f"{'='*60}")

    session_results = []
    totals = {
        "facts_extracted": 0,
        "facts_stored": 0,
        "edges_created": 0,
        "duplicates": 0,
        "skipped": 0,
        "errors": [],
        "cached_sessions": 0,
        "elapsed_seconds": 0.0,
        "snippets_extracted": 0,
        "journal_entries_written": 0,
    }
    review_stats_all = []

    for i, session in enumerate(conversation.sessions):
        print(f"\n  [{i+1}/{len(conversation.sessions)}] Session {session.session_num} "
              f"({len(session.turns)} turns, {session.date_time or 'no date'})")

        session_id = f"locomo-{conv_id}-s{session.session_num}"

        # Parse session date for fact created_at
        created_at = None
        if session.date_time:
            try:
                # LoCoMo dates like "1:56 pm on 8 May, 2023"
                # Try multiple formats
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
            except Exception:
                pass
            if created_at is None and session.date_time:
                print(f"    WARNING: Could not parse date '{session.date_time}'")

        # Stage 1: Extract
        extraction = extract_session(conversation, session, use_cache=use_cache)
        facts = extraction["facts"]

        if extraction.get("cached"):
            totals["cached_sessions"] += 1

        # Stage 2: Store
        store_result = store_facts(
            facts=facts,
            owner_id=owner_id,
            session_id=session_id,
            created_at=created_at,
        )

        # Stage 3: Write snippets + journal entries
        snippets_written = 0
        journal_written = 0
        _ALLOWED_FILES = {"SPEAKERS.md"}
        snippets_data = extraction.get("soul_snippets", {})
        for filename, snippet_list in snippets_data.items():
            if filename not in _ALLOWED_FILES or "/" in filename or ".." in filename:
                continue  # Skip unexpected filenames from LLM
            if isinstance(snippet_list, list):
                snippets_written += _write_snippets_to_file(
                    conv_dir, filename, snippet_list, session,
                )

        journal_data = extraction.get("journal_entries", {})
        for filename, entry_text in journal_data.items():
            if filename not in _ALLOWED_FILES or "/" in filename or ".." in filename:
                continue
            # LLM may return arrays instead of strings
            if isinstance(entry_text, list):
                entry_text = "\n\n".join(entry_text)
            if entry_text and entry_text.strip():
                date_str = None
                if created_at:
                    date_str = created_at[:10]  # YYYY-MM-DD
                try:
                    wrote = soul_snippets.write_journal_entry(
                        filename, entry_text.strip(),
                        trigger=f"Session {session.session_num}",
                        date_str=date_str,
                    )
                    if wrote:
                        journal_written += 1
                except Exception as e:
                    totals["errors"].append(f"journal write error: {e}")

        # Stage 4: Janitor
        janitor_result = {}
        if janitor_tasks and store_result["facts_stored"] > 0:
            janitor_result = run_janitor_cycle(janitor_tasks)

        # Stage 5: Periodic snippet review + journal distillation
        review_stats = _run_periodic_review(i, len(conversation.sessions))
        if review_stats:
            review_stats_all.append(review_stats)

        # Track results
        sr = {
            "session_num": session.session_num,
            "date_time": session.date_time,
            "turns": len(session.turns),
            "facts_extracted": len(facts),
            "facts_stored": store_result["facts_stored"],
            "edges_created": store_result["edges_created"],
            "duplicates": store_result["duplicates"],
            "skipped": store_result["skipped"],
            "snippets_written": snippets_written,
            "journal_written": journal_written,
            "cached": extraction.get("cached", False),
            "extraction_seconds": extraction["elapsed_seconds"],
            "janitor": janitor_result,
        }
        if review_stats:
            sr["review"] = review_stats
        session_results.append(sr)

        # Aggregate totals
        totals["facts_extracted"] += len(facts)
        totals["facts_stored"] += store_result["facts_stored"]
        totals["edges_created"] += store_result["edges_created"]
        totals["duplicates"] += store_result["duplicates"]
        totals["skipped"] += store_result["skipped"]
        totals["snippets_extracted"] += snippets_written
        totals["journal_entries_written"] += journal_written
        totals["errors"].extend(store_result["errors"])
        totals["elapsed_seconds"] += extraction["elapsed_seconds"]

        print(f"    Extracted: {len(facts)}, Stored: {store_result['facts_stored']}, "
              f"Edges: {store_result['edges_created']}, Dupes: {store_result['duplicates']}, "
              f"Snippets: {snippets_written}, Journal: {journal_written}")

    totals["elapsed_seconds"] = round(totals["elapsed_seconds"], 2)

    # Capture core markdown state at end of ingestion
    core_markdown_stats = {}
    speakers_path = conv_dir / "SPEAKERS.md"
    if speakers_path.exists():
        lines = speakers_path.read_text(encoding="utf-8").splitlines()
        core_markdown_stats["SPEAKERS.md"] = len(lines)

    # Save results
    result = {
        "conversation_id": conv_id,
        "speakers": f"{conversation.speaker_a} & {conversation.speaker_b}",
        "sessions": session_results,
        "totals": totals,
        "db_path": str(db_path),
        "review_stats": review_stats_all,
        "core_markdown_stats": core_markdown_stats,
    }

    results_file = conv_dir / "ingestion_results.json"
    with open(results_file, "w") as f:
        json.dump(result, f, indent=2)

    print(f"\n  Summary for {conv_id}:")
    print(f"    Facts extracted: {totals['facts_extracted']}")
    print(f"    Facts stored:    {totals['facts_stored']}")
    print(f"    Edges created:   {totals['edges_created']}")
    print(f"    Duplicates:      {totals['duplicates']}")
    print(f"    Snippets:        {totals['snippets_extracted']}")
    print(f"    Journal entries: {totals['journal_entries_written']}")
    print(f"    Cached sessions: {totals['cached_sessions']}")
    print(f"    Time:            {totals['elapsed_seconds']}s")
    if core_markdown_stats:
        for fname, lines in core_markdown_stats.items():
            print(f"    {fname}:     {lines} lines")
    if totals["errors"]:
        print(f"    Errors:          {len(totals['errors'])}")

    return result


def ingest_all(
    conversations: List[Conversation],
    results_dir: Path,
    conv_indices: Optional[List[int]] = None,
    use_cache: bool = False,
    janitor_tasks: str = "embeddings",
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Ingest multiple LoCoMo conversations.

    Args:
        conversations: Full list of parsed conversations
        results_dir: Root results directory
        conv_indices: Which conversations to process (default: all)
        use_cache: Whether to use cached extractions
        janitor_tasks: Janitor tasks per session
        owner_id: Override owner (default: per-conversation sample_id)

    Returns:
        Dict with per-conversation results and grand totals
    """
    if conv_indices is None:
        conv_indices = list(range(len(conversations)))

    results = []
    grand_totals = {
        "facts_extracted": 0,
        "facts_stored": 0,
        "edges_created": 0,
        "duplicates": 0,
        "snippets_extracted": 0,
        "journal_entries_written": 0,
        "conversations_processed": 0,
        "elapsed_seconds": 0.0,
    }

    for idx in conv_indices:
        if idx < 0 or idx >= len(conversations):
            print(f"WARNING: Conversation index {idx} out of range, skipping")
            continue

        conv = conversations[idx]
        r = ingest_conversation(
            conversation=conv,
            results_dir=results_dir,
            use_cache=use_cache,
            janitor_tasks=janitor_tasks,
            owner_id=owner_id,
        )
        results.append(r)

        t = r["totals"]
        grand_totals["facts_extracted"] += t["facts_extracted"]
        grand_totals["facts_stored"] += t["facts_stored"]
        grand_totals["edges_created"] += t["edges_created"]
        grand_totals["duplicates"] += t["duplicates"]
        grand_totals["snippets_extracted"] += t.get("snippets_extracted", 0)
        grand_totals["journal_entries_written"] += t.get("journal_entries_written", 0)
        grand_totals["elapsed_seconds"] += t["elapsed_seconds"]
        grand_totals["conversations_processed"] += 1

    grand_totals["elapsed_seconds"] = round(grand_totals["elapsed_seconds"], 2)

    return {
        "conversations": results,
        "grand_totals": grand_totals,
    }


def parse_conv_range(spec: str) -> List[int]:
    """Parse a conversation range spec like '0', '0-2', '0,3,5', 'all'."""
    if spec.lower() == "all":
        return list(range(10))

    indices = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            indices.extend(range(int(start), int(end) + 1))
        else:
            indices.append(int(part))
    return sorted(set(indices))


def main():
    parser = argparse.ArgumentParser(description="Ingest LoCoMo conversations into Quaid")
    parser.add_argument(
        "--conversations", default="all",
        help="Which conversations to process: '0', '0-2', '0,3,5', 'all' (default: all)"
    )
    parser.add_argument(
        "--cached", action="store_true",
        help="Use cached extraction results when available"
    )
    parser.add_argument(
        "--janitor-tasks", default="embeddings",
        help="Janitor tasks to run after each session (default: embeddings)"
    )
    parser.add_argument(
        "--results-dir",
        default=str(_DIR / "data" / "results"),
        help="Directory for results"
    )
    args = parser.parse_args()

    # Safety check
    db_path = os.environ.get("MEMORY_DB_PATH", "")
    prod_db = os.path.join(os.environ.get("QUAID_WORKSPACE", "."), "data", "memory.db")
    if db_path and os.path.abspath(db_path) == os.path.abspath(prod_db):
        print("FATAL: MEMORY_DB_PATH points to production database!")
        return 1

    conv_indices = parse_conv_range(args.conversations)
    print(f"LoCoMo Ingestion: conversations {conv_indices}")

    conversations = load_dataset()
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    result = ingest_all(
        conversations=conversations,
        results_dir=results_dir,
        conv_indices=conv_indices,
        use_cache=args.cached,
        janitor_tasks=args.janitor_tasks,
    )

    gt = result["grand_totals"]
    print(f"\n{'='*60}")
    print(f"LoCoMo Ingestion Complete")
    print(f"  Conversations: {gt['conversations_processed']}")
    print(f"  Facts extracted: {gt['facts_extracted']}")
    print(f"  Facts stored:    {gt['facts_stored']}")
    print(f"  Edges created:   {gt['edges_created']}")
    print(f"  Duplicates:      {gt['duplicates']}")
    print(f"  Snippets:        {gt['snippets_extracted']}")
    print(f"  Journal entries: {gt['journal_entries_written']}")
    print(f"  Time:            {gt['elapsed_seconds']}s")
    print(f"{'='*60}")

    # Save grand results
    with open(results_dir / "ingestion_summary.json", "w") as f:
        json.dump(result, f, indent=2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
