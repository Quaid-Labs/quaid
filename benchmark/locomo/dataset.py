#!/usr/bin/env python3
"""
LoCoMo dataset downloader and parser.

Downloads locomo10.json from the snap-research/locomo GitHub repo and parses
conversations and QA pairs into a format suitable for Quaid ingestion.

Dataset: 10 conversations, ~1986 QA pairs across 5 categories.
Paper: https://arxiv.org/abs/2402.17753

IMPORTANT: Category numbers in the JSON file do NOT match the paper's prose.
In the data:
  1 = Multi-hop (cross-session reasoning)
  2 = Temporal (date/time questions)
  3 = Open-domain / Commonsense (inferential)
  4 = Single-hop (direct factual)
  5 = Adversarial (trick questions, skipped in scoring)
"""
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Where cached data lives
DATA_DIR = Path(__file__).resolve().parent / "data"

LOCOMO_URL = (
    "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
)
LOCOMO_FILE = DATA_DIR / "locomo10.json"

# Category mapping: JSON number → descriptive name
CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}

# Only these categories are scored (matching Mem0's methodology)
SCORED_CATEGORIES = {1, 2, 3, 4}


@dataclass
class Turn:
    """A single dialogue turn in a LoCoMo session."""
    speaker: str
    dia_id: str
    text: str
    img_url: Optional[List[str]] = None
    blip_caption: Optional[str] = None


@dataclass
class Session:
    """A single conversation session with its turns and metadata."""
    session_num: int
    turns: List[Turn]
    date_time: Optional[str] = None


@dataclass
class QAPair:
    """A single question-answer pair from the LoCoMo evaluation set."""
    question: str
    answer: Optional[str]  # None for most category-5 questions
    category: int
    category_name: str
    evidence: List[str] = field(default_factory=list)
    adversarial_answer: Optional[str] = None

    @property
    def is_scored(self) -> bool:
        return self.category in SCORED_CATEGORIES


@dataclass
class Conversation:
    """A full LoCoMo conversation with sessions and QA pairs."""
    sample_id: str
    speaker_a: str
    speaker_b: str
    sessions: List[Session]
    qa_pairs: List[QAPair]
    event_summaries: Dict[str, Any] = field(default_factory=dict)
    observations: Dict[str, Any] = field(default_factory=dict)
    session_summaries: Dict[str, Any] = field(default_factory=dict)

    @property
    def scored_qa_pairs(self) -> List[QAPair]:
        return [qa for qa in self.qa_pairs if qa.is_scored]

    @property
    def num_sessions(self) -> int:
        return len(self.sessions)

    @property
    def num_turns(self) -> int:
        return sum(len(s.turns) for s in self.sessions)


def download_dataset(force: bool = False) -> Path:
    """Download locomo10.json if not already cached."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if LOCOMO_FILE.exists() and not force:
        print(f"  [dataset] Using cached {LOCOMO_FILE}")
        return LOCOMO_FILE

    print(f"  [dataset] Downloading from {LOCOMO_URL}...")
    urllib.request.urlretrieve(LOCOMO_URL, LOCOMO_FILE)
    size_mb = LOCOMO_FILE.stat().st_size / (1024 * 1024)
    print(f"  [dataset] Downloaded {size_mb:.1f}MB -> {LOCOMO_FILE}")
    return LOCOMO_FILE


def _parse_session_key(key: str) -> Optional[int]:
    """Extract session number from key like 'session_1' or 'session_12'."""
    m = re.match(r"^session_(\d+)$", key)
    return int(m.group(1)) if m else None


def _parse_turns(raw_turns: List[Dict]) -> List[Turn]:
    """Parse a list of raw turn dicts into Turn objects."""
    turns = []
    for t in raw_turns:
        turns.append(Turn(
            speaker=t["speaker"],
            dia_id=t.get("dia_id", ""),
            text=t.get("text", ""),
            img_url=t.get("img_url"),
            blip_caption=t.get("blip_caption"),
        ))
    return turns


def _parse_qa_pairs(raw_qa: List[Dict]) -> List[QAPair]:
    """Parse QA pairs, handling type variations and missing fields."""
    pairs = []
    for qa in raw_qa:
        category = int(qa["category"])

        # Answer can be int, string, or absent (category 5)
        raw_answer = qa.get("answer")
        if raw_answer is not None:
            answer = str(raw_answer)
        else:
            answer = None

        pairs.append(QAPair(
            question=qa["question"],
            answer=answer,
            category=category,
            category_name=CATEGORY_NAMES.get(category, f"unknown-{category}"),
            evidence=qa.get("evidence", []),
            adversarial_answer=qa.get("adversarial_answer"),
        ))
    return pairs


def parse_conversation(raw: Dict) -> Conversation:
    """Parse a single raw conversation dict into a Conversation object."""
    conv = raw["conversation"]
    speaker_a = conv.get("speaker_a", "Speaker A")
    speaker_b = conv.get("speaker_b", "Speaker B")

    # Extract sessions — keys like 'session_1', 'session_2', etc.
    sessions = []
    session_keys = sorted(
        [k for k in conv if _parse_session_key(k) is not None],
        key=lambda k: _parse_session_key(k),
    )

    for key in session_keys:
        num = _parse_session_key(key)
        turns_data = conv[key]
        if not isinstance(turns_data, list) or len(turns_data) == 0:
            continue

        date_key = f"{key}_date_time"
        date_time = conv.get(date_key)

        sessions.append(Session(
            session_num=num,
            turns=_parse_turns(turns_data),
            date_time=date_time,
        ))

    # Parse QA pairs
    qa_pairs = _parse_qa_pairs(raw.get("qa", []))

    return Conversation(
        sample_id=raw["sample_id"],
        speaker_a=speaker_a,
        speaker_b=speaker_b,
        sessions=sessions,
        qa_pairs=qa_pairs,
        event_summaries=raw.get("event_summary", {}),
        observations=raw.get("observation", {}),
        session_summaries=raw.get("session_summary", {}),
    )


def load_dataset(force_download: bool = False) -> List[Conversation]:
    """Download (if needed) and parse the full LoCoMo dataset.

    Returns:
        List of 10 Conversation objects, each with sessions and QA pairs.
    """
    path = download_dataset(force=force_download)

    with open(path, "r") as f:
        raw_data = json.load(f)

    conversations = [parse_conversation(r) for r in raw_data]
    print(f"  [dataset] Parsed {len(conversations)} conversations, "
          f"{sum(c.num_turns for c in conversations)} turns, "
          f"{sum(len(c.qa_pairs) for c in conversations)} QA pairs")
    return conversations


def format_session_transcript(session: Session) -> str:
    """Format a LoCoMo session into a transcript string matching production format.

    This converts the structured turns into the same text format that
    production /compact events receive — plain text with speaker labels.
    Includes session date_time as a header so the extraction LLM has
    temporal context (critical for Category 2 temporal QA questions).
    """
    lines = []
    # Include temporal context so extraction can anchor facts to dates
    if session.date_time:
        lines.append(f"[Conversation on {session.date_time}]")
    for turn in session.turns:
        # Skip empty turns
        text = turn.text.strip()
        if not text:
            continue

        # Include image descriptions if present (adds context for extraction)
        if turn.blip_caption:
            text += f" [shared image: {turn.blip_caption}]"

        lines.append(f"{turn.speaker}: {text}")

    return "\n\n".join(lines)


def get_dataset_stats(conversations: List[Conversation]) -> Dict:
    """Compute summary statistics for the dataset."""
    total_qa = sum(len(c.qa_pairs) for c in conversations)
    scored_qa = sum(len(c.scored_qa_pairs) for c in conversations)
    total_turns = sum(c.num_turns for c in conversations)
    total_sessions = sum(c.num_sessions for c in conversations)

    # Per-category counts
    cat_counts = {}
    for c in conversations:
        for qa in c.qa_pairs:
            cat_counts[qa.category_name] = cat_counts.get(qa.category_name, 0) + 1

    return {
        "conversations": len(conversations),
        "total_sessions": total_sessions,
        "total_turns": total_turns,
        "total_qa": total_qa,
        "scored_qa": scored_qa,
        "adversarial_qa": total_qa - scored_qa,
        "category_counts": cat_counts,
        "per_conversation": [
            {
                "sample_id": c.sample_id,
                "speakers": f"{c.speaker_a} & {c.speaker_b}",
                "sessions": c.num_sessions,
                "turns": c.num_turns,
                "qa_pairs": len(c.qa_pairs),
                "scored_qa": len(c.scored_qa_pairs),
            }
            for c in conversations
        ],
    }


if __name__ == "__main__":
    conversations = load_dataset()
    stats = get_dataset_stats(conversations)
    print(f"\nLoCoMo Dataset Statistics:")
    print(f"  Conversations: {stats['conversations']}")
    print(f"  Sessions:      {stats['total_sessions']}")
    print(f"  Turns:         {stats['total_turns']}")
    print(f"  QA pairs:      {stats['total_qa']} ({stats['scored_qa']} scored)")
    print(f"  Categories:    {stats['category_counts']}")
    print(f"\nPer-conversation:")
    for c in stats["per_conversation"]:
        print(f"  {c['sample_id']}: {c['speakers']} | "
              f"{c['sessions']} sessions, {c['turns']} turns, "
              f"{c['qa_pairs']} QA ({c['scored_qa']} scored)")
