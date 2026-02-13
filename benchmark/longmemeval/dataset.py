#!/usr/bin/env python3
"""
LongMemEval dataset downloader and parser.

Downloads LongMemEval_S_cleaned.json from HuggingFace and parses entries
into a format suitable for Quaid ingestion and evaluation.

Dataset: 500 QA entries, each with its own independent haystack of ~48 sessions.
Paper: Wu et al. (ICLR 2025), "LongMemEval: Benchmarking Chat Assistants on
Long-Term Interactive Memory"

IMPORTANT: Unlike LoCoMo, each QA entry has its OWN independent haystack of
sessions. Sessions are NOT shared across entries.
"""
import json
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Where cached data lives
DATA_DIR = Path(__file__).resolve().parent / "data"

# HuggingFace direct download URL for the cleaned S variant
DATASET_URL = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
    "resolve/main/longmemeval_s_cleaned.json"
)
DATASET_FILE = DATA_DIR / "longmemeval_s_cleaned.json"

# Question types in the dataset
QUESTION_TYPES = [
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "knowledge-update",
    "temporal-reasoning",
]

# Memory abilities mapped from question types
ABILITY_MAP = {
    "single-session-user": "information_extraction",
    "single-session-assistant": "information_extraction",
    "single-session-preference": "information_extraction",
    "multi-session": "multi_session_reasoning",
    "knowledge-update": "knowledge_updates",
    "temporal-reasoning": "temporal_reasoning",
}


@dataclass
class LMEEntry:
    """A single LongMemEval evaluation entry with its haystack."""
    question_id: str
    question_type: str
    question: str
    answer: str  # int answers coerced to str at load time
    question_date: str
    haystack_dates: List[str]
    haystack_session_ids: List[str]
    haystack_sessions: List[List[Dict]]  # list of sessions, each a list of {role, content}
    answer_session_ids: List[str]

    @property
    def is_abstention(self) -> bool:
        return "_abs" in self.question_id

    @property
    def num_sessions(self) -> int:
        return len(self.haystack_sessions)

    @property
    def num_turns(self) -> int:
        return sum(len(s) for s in self.haystack_sessions)

    @property
    def memory_ability(self) -> str:
        if self.is_abstention:
            return "abstention"
        return ABILITY_MAP.get(self.question_type, "unknown")


def download_dataset(force: bool = False) -> Path:
    """Download LongMemEval_S_cleaned.json if not already cached."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if DATASET_FILE.exists() and not force:
        print(f"  [dataset] Using cached {DATASET_FILE}")
        return DATASET_FILE

    print(f"  [dataset] Downloading from {DATASET_URL}...")
    urllib.request.urlretrieve(DATASET_URL, DATASET_FILE)
    size_mb = DATASET_FILE.stat().st_size / (1024 * 1024)
    print(f"  [dataset] Downloaded {size_mb:.1f}MB -> {DATASET_FILE}")
    return DATASET_FILE


def load_dataset(force_download: bool = False) -> List[LMEEntry]:
    """Load and parse the LongMemEval dataset.

    Returns a list of 500 LMEEntry objects, each with its own haystack.
    """
    path = download_dataset(force=force_download)

    print(f"  [dataset] Parsing {path.name}...")
    with open(path) as f:
        raw = json.load(f)

    entries = []
    for item in raw:
        # Answer can be int or string — coerce to string
        answer = item.get("answer", "")
        if answer is not None:
            answer = str(answer)
        else:
            answer = ""

        entries.append(LMEEntry(
            question_id=item["question_id"],
            question_type=item["question_type"],
            question=item["question"],
            answer=answer,
            question_date=item.get("question_date", ""),
            haystack_dates=item.get("haystack_dates", []),
            haystack_session_ids=item.get("haystack_session_ids", []),
            haystack_sessions=item.get("haystack_sessions", []),
            answer_session_ids=item.get("answer_session_ids", []),
        ))

    print(f"  [dataset] Parsed {len(entries)} entries")
    return entries


def get_dataset_stats(entries: List[LMEEntry]) -> Dict[str, Any]:
    """Compute summary statistics for the dataset."""
    from collections import Counter

    type_counts = Counter(e.question_type for e in entries)
    ability_counts = Counter(e.memory_ability for e in entries)
    abstention_count = sum(1 for e in entries if e.is_abstention)

    total_sessions = sum(e.num_sessions for e in entries)
    total_turns = sum(e.num_turns for e in entries)

    # Unique session IDs across all entries
    all_session_ids = set()
    for e in entries:
        all_session_ids.update(e.haystack_session_ids)

    return {
        "total_entries": len(entries),
        "abstention_entries": abstention_count,
        "scored_entries": len(entries) - abstention_count,
        "question_types": dict(type_counts),
        "memory_abilities": dict(ability_counts),
        "total_sessions": total_sessions,
        "unique_session_ids": len(all_session_ids),
        "total_turns": total_turns,
        "avg_sessions_per_entry": round(total_sessions / max(len(entries), 1), 1),
        "avg_turns_per_entry": round(total_turns / max(len(entries), 1), 1),
    }


def format_session_transcript(
    session: List[Dict],
    session_date: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Format a single session's turns into a readable transcript.

    Args:
        session: List of {role, content} turn dicts
        session_date: Optional date string for the session header
        session_id: Optional session ID for the header
    """
    parts = []
    if session_date:
        header = f"--- Session"
        if session_id:
            header += f" ({session_id})"
        header += f" — {session_date} ---"
        parts.append(header)

    for turn in session:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        parts.append(f"{role}: {content}")

    return "\n".join(parts)


def format_full_haystack(entry: LMEEntry) -> str:
    """Format the entire haystack for an entry into a single transcript.

    Used for full-context baseline evaluation.
    """
    parts = []
    for i, (session, date, sid) in enumerate(zip(
        entry.haystack_sessions,
        entry.haystack_dates,
        entry.haystack_session_ids,
    )):
        transcript = format_session_transcript(session, date, sid)
        if transcript.strip():
            parts.append(transcript)
    return "\n\n".join(parts)


if __name__ == "__main__":
    entries = load_dataset()
    stats = get_dataset_stats(entries)
    print(f"\nDataset Statistics:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    # Show a sample entry
    if entries:
        e = entries[0]
        print(f"\nSample entry:")
        print(f"  ID: {e.question_id}")
        print(f"  Type: {e.question_type}")
        print(f"  Question: {e.question}")
        print(f"  Answer: {e.answer}")
        print(f"  Sessions: {e.num_sessions}")
        print(f"  Turns: {e.num_turns}")
        print(f"  Is abstention: {e.is_abstention}")
