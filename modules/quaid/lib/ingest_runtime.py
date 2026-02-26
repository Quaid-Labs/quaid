"""Runtime-safe ingest entrypoints for core event handlers.

Keeps core runtime decoupled from ingest module internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


def run_docs_ingest(transcript_path: Path, label: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    from ingest.docs_ingest import run as docs_ingest_run

    return docs_ingest_run(transcript_path, label, session_id)


def run_session_logs_ingest(
    *,
    session_id: str,
    owner_id: str,
    label: str,
    session_file: Optional[str] = None,
    transcript_path: Optional[str] = None,
    source_channel: Optional[str] = None,
    conversation_id: Optional[str] = None,
    participant_ids: Optional[list[str]] = None,
    participant_aliases: Optional[Dict[str, str]] = None,
    message_count: int = 0,
    topic_hint: str = "",
) -> Dict[str, Any]:
    from ingest.session_logs_ingest import run as session_logs_ingest_run

    return session_logs_ingest_run(
        session_id=session_id,
        owner_id=owner_id,
        label=label,
        session_file=session_file,
        transcript_path=transcript_path,
        source_channel=source_channel,
        conversation_id=conversation_id,
        participant_ids=participant_ids,
        participant_aliases=participant_aliases,
        message_count=message_count,
        topic_hint=topic_hint,
    )
