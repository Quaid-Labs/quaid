"""Core contract for session transcript evidence storage."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol


class SessionMemoryBridgePort(Protocol):
    """Core-owned bridge between session storage and MemoryDB evidence."""

    def store_session_transcript(
        self,
        *,
        session_id: str,
        transcript: str,
        owner_id: str = "default",
        label: str = "unknown",
        source_path: Optional[str] = None,
        source_channel: Optional[str] = None,
        conversation_id: Optional[str] = None,
        participant_ids: Optional[List[str]] = None,
        participant_aliases: Optional[Dict[str, str]] = None,
        message_count: int = 0,
        topic_hint: str = "",
    ) -> Dict[str, Any]: ...

    def store_session_chunks(
        self,
        *,
        chunks: List[str],
        owner_id: str,
        session_id: str,
        source_id: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]: ...

    def list_session_chunks(self, *, owner_id: str, **kwargs: Any) -> List[Dict[str, Any]]: ...

    def get_session_chunk(self, chunk_id: str, *, owner_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]: ...

    def list_session_transcripts(self, *, owner_id: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]: ...

    def load_session_transcript(self, *, session_id: str, owner_id: Optional[str] = None) -> Optional[Dict[str, Any]]: ...
