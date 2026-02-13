#!/usr/bin/env python3
"""
LongMemEval ingestion pipeline — extract and store memories from LME haystacks.

For each of the 500 entries:
1. Create a fresh test DB (isolated per entry)
2. Process sessions chronologically
3. Extract facts via Opus (or Haiku for budget runs)
4. Store facts + edges
5. Run embeddings after each session

Key difference from LoCoMo: each entry has its OWN independent haystack of ~48
sessions. Sessions are NOT shared across entries. We cache extractions by
session_id to avoid re-extracting shared sessions.
"""
import json
import os
import re
import shutil
import sqlite3
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
from memory_graph import store, create_edge, get_graph
from llm_clients import call_high_reasoning, call_low_reasoning, parse_json_response

from longmemeval.dataset import LMEEntry, format_session_transcript

# Cache directory for extraction results (keyed by session_id for dedup)
CACHE_DIR = _DIR / "data" / "extraction_cache"

# Extraction prompt adapted for user/assistant chat format
LME_EXTRACTION_PROMPT = """You are a memory extraction system. You will receive a conversation session between a user and an AI assistant. Your job is to extract personal facts AND relationship edges from this conversation.

This is a PERSONAL knowledge base for tracking what the user discusses.

EXTRACT facts that are EXPLICITLY STATED OR CONFIRMED in the conversation. Never infer, speculate, or extrapolate.

WHAT TO EXTRACT:
- Personal facts about the user (names, relationships, jobs, birthdays, health, locations)
- Preferences and opinions explicitly stated ("I like X", "I prefer Y", "I hate Z")
- Decisions with reasoning ("decided to use X because Y")
- Significant events or milestones ("graduated from X", "started new job at Y")
- Important relationships (family, friends, colleagues, partners)
- Plans and future events ("going to visit X next month")
- Life changes ("got engaged", "adopted a dog", "started a new diet")
- Information the assistant provided that the user acknowledged or used

WHAT NOT TO EXTRACT:
- Small talk greetings ("Hey, how are you?")
- Conversational filler ("That's great!", "Oh wow!")
- Vague statements without specific content
- Repetitions of previously stated facts within same session
- Meta-conversation about the chat itself
- General knowledge the assistant shared that isn't user-specific

QUALITY RULES:
- Use "the user" as subject (e.g., "The user started a new job at...")
- Each fact must be self-contained and understandable without context
- Be specific: "The user likes Thai food" > "The user likes food"
- Mark extraction_confidence "high" for clearly stated facts, "medium" for somewhat ambiguous
- Extract comprehensively — missed facts are gone forever

KEYWORDS (per fact):
For each fact, provide 3-5 searchable keywords as a space-separated string.

EDGE EXTRACTION:
For relationship facts, extract edges connecting entities.

EDGE DIRECTION RULES:
- parent_of: PARENT is subject
- sibling_of, spouse_of, friend_of: alphabetical order (symmetric)
- has_pet: OWNER is subject
- works_at, lives_at: PERSON is subject

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
  ]
}}

If nothing worth capturing, respond: {{"facts": []}}"""

EXTRACTION_USER_PREFIX = "Extract memorable facts from this session:\n\n"

# Confidence mapping
_CONFIDENCE_MAP = {"high": 0.9, "medium": 0.6, "low": 0.3}
_VALID_CATEGORIES = {"fact", "preference", "decision", "relationship", "entity"}


def _get_cache_path(session_id: str) -> Path:
    """Get cache file path for an extraction result, keyed by session_id."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize session_id for filesystem
    safe_id = session_id.replace("/", "_").replace("\\", "_")
    return CACHE_DIR / f"{safe_id}.json"


def _load_cached_extraction(session_id: str) -> Optional[Dict]:
    """Load cached extraction results if available."""
    path = _get_cache_path(session_id)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def _save_extraction_cache(session_id: str, data: Any):
    """Save extraction results to cache."""
    path = _get_cache_path(session_id)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _parse_extraction_response(raw: Optional[str]) -> Dict[str, Any]:
    """Parse the JSON response from extraction."""
    empty: Dict[str, Any] = {"facts": []}
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

    Matches LoCoMo ingest.py: explicitly passes db_path to MemoryGraph constructor
    because DB_PATH default param is captured at class definition time.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if fresh and db_path.exists():
        db_path.unlink()
    if fresh:
        for suffix in ["-wal", "-shm"]:
            wal = Path(str(db_path) + suffix)
            if wal.exists():
                wal.unlink()

    os.environ["MEMORY_DB_PATH"] = str(db_path)

    import memory_graph as mg
    from memory_graph import MemoryGraph

    # Checkpoint and close old graph's WAL before switching
    if hasattr(mg, '_graph') and mg._graph is not None:
        try:
            with mg._graph._get_conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    # Replace singleton with new MemoryGraph pointing to correct DB
    mg._graph = MemoryGraph(db_path=db_path)
    print(f"  [db] {'Created fresh' if fresh else 'Switched to'} DB at {db_path}")


def _run_embeddings() -> None:
    """Run embeddings backfill for newly stored facts."""
    try:
        from janitor import run_task_optimized
        run_task_optimized("embeddings", dry_run=False)
    except Exception as e:
        print(f"    [embeddings] Error: {e}")


def extract_session(
    session: List[Dict],
    session_id: str,
    session_date: str,
    use_cache: bool = True,
    extract_model: str = "opus",
) -> Dict[str, Any]:
    """Extract facts from a single session.

    Args:
        session: List of {role, content} turn dicts
        session_id: Unique session identifier (for caching)
        session_date: Date string for the session
        use_cache: Whether to use cached extractions
        extract_model: Model for extraction ("opus" or "haiku")

    Returns:
        Dict with 'facts' list
    """
    # Check cache first
    if use_cache:
        cached = _load_cached_extraction(session_id)
        if cached is not None:
            return cached

    # Format session transcript
    transcript = format_session_transcript(session, session_date, session_id)

    user_msg = EXTRACTION_USER_PREFIX + transcript

    # Call LLM for extraction
    if extract_model == "haiku":
        raw_response, duration = call_low_reasoning(
            prompt=user_msg,
            max_tokens=4000,
            system_prompt=LME_EXTRACTION_PROMPT,
        )
    else:
        raw_response, duration = call_high_reasoning(
            prompt=user_msg,
            max_tokens=4000,
            system_prompt=LME_EXTRACTION_PROMPT,
        )

    result = _parse_extraction_response(raw_response)

    # Cache the extraction
    _save_extraction_cache(session_id, result)

    return result


def store_facts(
    facts: List[Dict],
    owner_id: str,
    session_id: str,
) -> Dict[str, int]:
    """Store extracted facts and edges in the memory graph.

    Returns dict with counts: facts_stored, edges_created
    """
    stored = 0
    edges = 0

    for fact in facts:
        text = fact.get("text", "").strip()
        if not text or len(text) < 5:
            continue

        category = fact.get("category", "fact")
        if category not in _VALID_CATEGORIES:
            category = "fact"

        confidence_str = fact.get("extraction_confidence", "medium")
        confidence = _CONFIDENCE_MAP.get(confidence_str, 0.6)

        keywords = fact.get("keywords", "")
        if isinstance(keywords, list):
            keywords = " ".join(keywords)

        try:
            result = store(
                text=text,
                owner_id=owner_id,
                category=category,
                confidence=confidence,
                keywords=keywords,
                source_type="benchmark",
            )
            node_id = result["id"]
            stored += 1
        except Exception as e:
            print(f"      Store error: {e}")
            continue

        # Store edges
        for edge in fact.get("edges", []):
            try:
                create_edge(
                    subject_name=edge["subject"],
                    relation=edge["relation"],
                    object_name=edge["object"],
                    source_fact_id=node_id,
                    create_missing_entities=True,
                )
                edges += 1
            except Exception as e:
                pass  # Edge creation failures are non-fatal

    return {"facts_stored": stored, "edges_created": edges}


def ingest_entry(
    entry: LMEEntry,
    results_dir: Path,
    use_cache: bool = True,
    extract_model: str = "opus",
    owner_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Ingest a single LME entry's haystack into a fresh database.

    Args:
        entry: The LME entry with its haystack sessions
        results_dir: Root results directory
        use_cache: Use cached extractions
        extract_model: Model for extraction ("opus" or "haiku")
        owner_id: Override owner ID

    Returns:
        Dict with ingestion stats
    """
    oid = owner_id or entry.question_id
    entry_dir = results_dir / entry.question_id
    db_path = entry_dir / "memory.db"

    print(f"\n  Ingesting {entry.question_id}: {entry.num_sessions} sessions, "
          f"{entry.num_turns} turns")

    # Fresh DB for this entry
    _switch_to_db(db_path, fresh=True)

    total_facts = 0
    total_edges = 0
    cached_count = 0
    extracted_count = 0

    # Process sessions chronologically
    for i, (session, date, sid) in enumerate(zip(
        entry.haystack_sessions,
        entry.haystack_dates,
        entry.haystack_session_ids,
    )):
        if not session:
            continue

        # Check cache BEFORE extraction to track stats correctly
        was_cached = use_cache and _load_cached_extraction(sid) is not None

        # Extract
        extraction = extract_session(
            session=session,
            session_id=sid,
            session_date=date,
            use_cache=use_cache,
            extract_model=extract_model,
        )

        if was_cached:
            cached_count += 1
        else:
            extracted_count += 1

        # Store
        counts = store_facts(extraction.get("facts", []), oid, sid)
        total_facts += counts["facts_stored"]
        total_edges += counts["edges_created"]

        if (i + 1) % 10 == 0 or i == entry.num_sessions - 1:
            print(f"    [{i+1}/{entry.num_sessions}] {total_facts} facts, {total_edges} edges")

    # Run embeddings after all sessions
    _run_embeddings()

    return {
        "entry_id": entry.question_id,
        "sessions_processed": entry.num_sessions,
        "facts_stored": total_facts,
        "edges_created": total_edges,
        "cached_extractions": cached_count,
        "new_extractions": extracted_count,
    }


def ingest_all(
    entries: List[LMEEntry],
    results_dir: Path,
    entry_indices: Optional[List[int]] = None,
    use_cache: bool = True,
    extract_model: str = "opus",
) -> Dict[str, Any]:
    """Ingest multiple entries.

    Args:
        entries: Full list of parsed entries
        results_dir: Root results directory
        entry_indices: Which entries to process (default: all)
        use_cache: Use cached extractions
        extract_model: Model for extraction

    Returns:
        Dict with per-entry results and grand totals
    """
    if entry_indices is None:
        entry_indices = list(range(len(entries)))

    all_results = []
    grand_totals = {
        "facts_stored": 0,
        "edges_created": 0,
        "entries_processed": 0,
        "elapsed_seconds": 0,
    }
    t_start = time.monotonic()

    for idx in entry_indices:
        if idx < 0 or idx >= len(entries):
            continue

        entry = entries[idx]
        result = ingest_entry(
            entry=entry,
            results_dir=results_dir,
            use_cache=use_cache,
            extract_model=extract_model,
        )
        all_results.append(result)
        grand_totals["facts_stored"] += result["facts_stored"]
        grand_totals["edges_created"] += result["edges_created"]
        grand_totals["entries_processed"] += 1

    grand_totals["elapsed_seconds"] = round(time.monotonic() - t_start, 1)
    return {"entries": all_results, "grand_totals": grand_totals}
