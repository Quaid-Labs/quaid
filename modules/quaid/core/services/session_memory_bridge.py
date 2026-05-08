"""Core-owned session storage to MemoryDB bridge.

Runtime and ingest code should use this module when session transcript material
needs to become durable SessionDB structure and MemoryDB evidence. Datastore
modules still own table schema, persistence, indexing, and search mechanics.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from core.contracts.session_memory_bridge import SessionMemoryBridgePort
from core.services.datastore_bridge import DatastoreBridge, get_datastore_bridge


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _optional(value: Any) -> Optional[str]:
    text = _clean(value)
    return text or None


def _owner(value: Any) -> str:
    return _clean(value) or "default"


def _session_id(value: Any) -> str:
    sid = _clean(value)
    if not sid:
        raise ValueError("session_id is required")
    return sid


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value or [])
    return [_clean(item) for item in items if _clean(item)]


def _alias_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    out: Dict[str, str] = {}
    for raw_key, raw_val in value.items():
        key = _clean(raw_key)
        val = _clean(raw_val)
        if key and val:
            out[key] = val
    return out


class DatastoreSessionMemoryBridge(SessionMemoryBridgePort):
    """Bridge implementation coordinating SessionDB and MemoryDB callbacks."""

    def __init__(
        self,
        *,
        memory_service: Optional[Any] = None,
        datastore_bridge: Optional[DatastoreBridge] = None,
        session_log_indexer: Optional[Callable[..., Dict[str, Any]]] = None,
        session_log_lister: Optional[Callable[..., List[Dict[str, Any]]]] = None,
        session_log_loader: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self._memory_service = memory_service
        if datastore_bridge is not None:
            self._datastore_bridge = datastore_bridge
        elif memory_service is not None or session_log_indexer is not None or session_log_lister is not None or session_log_loader is not None:
            self._datastore_bridge = DatastoreBridge()
        else:
            self._datastore_bridge = get_datastore_bridge()
        self._session_log_indexer = session_log_indexer
        self._session_log_lister = session_log_lister
        self._session_log_loader = session_log_loader
        self._register_datastore_routes()

    def _memory(self) -> Any:
        if self._memory_service is not None:
            return self._memory_service
        from core.services.memory_service import get_memory_service

        return get_memory_service()

    def _index_session_log(self, **kwargs: Any) -> Dict[str, Any]:
        if self._session_log_indexer is not None:
            return self._session_log_indexer(**kwargs)
        from datastore.sessiondb.session_store import index_session_log

        return index_session_log(**kwargs)

    def _list_session_logs(self, **kwargs: Any) -> List[Dict[str, Any]]:
        if self._session_log_lister is not None:
            return self._session_log_lister(**kwargs)
        from datastore.sessiondb.session_store import list_recent_sessions

        return list_recent_sessions(**kwargs)

    def _load_session_log(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        if self._session_log_loader is not None:
            return self._session_log_loader(**kwargs)
        from datastore.sessiondb.session_store import load_session

        return load_session(**kwargs)

    def _session_store_source_text(self, **kwargs: Any) -> Dict[str, Any]:
        from datastore.sessiondb.session_store import store_session_source_text

        return store_session_source_text(**kwargs)

    def _session_attach_memory_chunk(self, **kwargs: Any) -> Dict[str, Any]:
        from datastore.sessiondb.session_store import attach_memory_chunk

        return attach_memory_chunk(**kwargs)

    def _session_expand_microchunk(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        from datastore.sessiondb.session_store import expand_microchunk

        return expand_microchunk(**kwargs)

    def _memory_store_session_chunks(self, **kwargs: Any) -> List[Dict[str, Any]]:
        return self._memory().store_session_chunks(**kwargs)

    def _memory_list_session_chunks(self, **kwargs: Any) -> List[Dict[str, Any]]:
        return self._memory().list_session_chunks(**kwargs)

    def _memory_get_session_chunk(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        chunk_id = str(kwargs.pop("chunk_id", "") or "").strip()
        if not chunk_id:
            raise ValueError("chunk_id is required")
        return self._memory().get_session_chunk(chunk_id, **kwargs)

    def _register_datastore_routes(self) -> None:
        self._datastore_bridge.register_store(
            name="sessiondb",
            provides=["session_id", "chunk_id", "pair_id", "microchunk_id"],
            consumes=["memory_chunk_id"],
            callbacks={
                "store_session_transcript": self._index_session_log,
                "store_session_source_text": self._session_store_source_text,
                "list_session_transcripts": self._list_session_logs,
                "load_session_transcript": self._load_session_log,
                "attach_memory_chunk": self._session_attach_memory_chunk,
                "expand_microchunk": self._session_expand_microchunk,
            },
        )
        self._datastore_bridge.register_store(
            name="memorydb",
            provides=["memory_chunk_id"],
            consumes=["microchunk_id", "pair_id", "session_id"],
            callbacks={
                "store_session_chunks": self._memory_store_session_chunks,
                "list_session_chunks": self._memory_list_session_chunks,
                "get_session_chunk": self._memory_get_session_chunk,
            },
        )

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
    ) -> Dict[str, Any]:
        sid = _session_id(session_id)
        text = str(transcript or "").strip()
        if not text:
            return {"status": "skipped", "reason": "empty_transcript", "session_id": sid}
        return self._datastore_bridge.call(
            "sessiondb",
            "store_session_transcript",
            session_id=sid,
            transcript=text,
            owner_id=_owner(owner_id),
            source_label=_clean(label) or "unknown",
            source_path=_optional(source_path),
            source_channel=_optional(source_channel),
            conversation_id=_optional(conversation_id),
            participant_ids=_string_list(participant_ids),
            participant_aliases=_alias_map(participant_aliases),
            message_count=int(message_count or 0),
            topic_hint=_clean(topic_hint),
        )

    def index_session_log(self, **kwargs: Any) -> Dict[str, Any]:
        """Compatibility alias for callers that use indexing terminology."""
        return self.store_session_transcript(**kwargs)

    def store_session_chunks(
        self,
        *,
        chunks: List[str],
        owner_id: str,
        session_id: str,
        source_id: Optional[str] = None,
        source_channel: Optional[str] = None,
        source_conversation_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        source_author_id: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        sid = _session_id(session_id)
        options = dict(kwargs)
        options["source_id"] = _optional(source_id or options.get("source_id")) or sid
        options["session_id"] = sid
        options["source_channel"] = _optional(source_channel or options.get("source_channel"))
        resolved_conversation = _optional(
            source_conversation_id or conversation_id or options.get("source_conversation_id") or options.get("conversation_id")
        )
        options["source_conversation_id"] = resolved_conversation
        options["conversation_id"] = _optional(conversation_id or options.get("conversation_id")) or resolved_conversation
        options["source_author_id"] = _optional(source_author_id or options.get("source_author_id"))
        return self._memory().store_session_chunks(
            chunks=list(chunks or []),
            owner_id=_owner(owner_id),
            **options,
        )

    def store_session_source_text(
        self,
        *,
        text: str,
        owner_id: str,
        session_id: str,
        source_id: Optional[str] = None,
        source_channel: Optional[str] = None,
        source_conversation_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        source_author_id: Optional[str] = None,
        start_index: int = 0,
        chunk_index: int = 0,
        chunk_kind: str = "session",
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        sid = _session_id(session_id)
        owner = _owner(owner_id)
        session_result = self._datastore_bridge.call(
            "sessiondb",
            "store_session_source_text",
            text=str(text or ""),
            owner_id=owner,
            session_id=sid,
            source_id=_optional(source_id) or sid,
            source_channel=_optional(source_channel),
            source_conversation_id=_optional(source_conversation_id),
            conversation_id=_optional(conversation_id) or _optional(source_conversation_id),
            source_author_id=_optional(source_author_id),
            chunk_index=int(chunk_index or 0),
            chunk_kind=chunk_kind,
            max_microchunk_tokens=int(kwargs.pop("max_microchunk_tokens", 40) or 40),
        )
        microchunks = [row for row in list((session_result or {}).get("microchunks") or []) if isinstance(row, dict)]
        if not microchunks:
            return []
        memory_rows = self._datastore_bridge.call(
            "memorydb",
            "store_session_chunks",
            chunks=[str(row.get("text") or "") for row in microchunks],
            owner_id=owner,
            source_id=_optional(source_id) or sid,
            session_id=sid,
            start_index=int(start_index or 0),
            chunk_kind="micro",
            parent_chunk_id=str(((session_result or {}).get("chunk") or {}).get("chunk_id") or "").strip() or None,
            message_pair_ids=[str(row.get("pair_id") or "").strip() or None for row in microchunks],
            microchunk_ids=[str(row.get("microchunk_id") or "").strip() or None for row in microchunks],
            source_channel=_optional(source_channel),
            source_conversation_id=_optional(source_conversation_id),
            conversation_id=_optional(conversation_id) or _optional(source_conversation_id),
            source_author_id=_optional(source_author_id),
            **kwargs,
        )
        out: List[Dict[str, Any]] = []
        for microchunk, memory_row in zip(microchunks, list(memory_rows or [])):
            if not isinstance(memory_row, dict):
                continue
            microchunk_id = str(microchunk.get("microchunk_id") or "").strip()
            memory_chunk_id = str(memory_row.get("chunk_id") or "").strip()
            if microchunk_id and memory_chunk_id:
                self._datastore_bridge.call(
                    "sessiondb",
                    "attach_memory_chunk",
                    microchunk_id=microchunk_id,
                    memory_chunk_id=memory_chunk_id,
                    owner_id=owner,
                )
            item = dict(memory_row)
            item.setdefault("microchunk_id", microchunk_id)
            item.setdefault("message_pair_id", str(microchunk.get("pair_id") or "").strip() or None)
            item.setdefault("sessiondb_chunk_id", str(microchunk.get("chunk_id") or "").strip() or None)
            out.append(item)
        return out

    def store_session_chunk(
        self,
        *,
        text: str,
        owner_id: str,
        session_id: str,
        source_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        rows = self.store_session_chunks(
            chunks=[text],
            owner_id=owner_id,
            session_id=session_id,
            source_id=source_id,
            **kwargs,
        )
        if not rows:
            raise RuntimeError("session chunk store returned no rows")
        return rows[0]

    def store_source_chunks(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """Compatibility alias; public bridge terminology is session chunks."""
        return self.store_session_chunks(**kwargs)

    def list_session_chunks(self, *, owner_id: str, **kwargs: Any) -> List[Dict[str, Any]]:
        owner = _owner(owner_id)
        if not owner:
            raise ValueError("owner_id is required for session chunk reads")
        return self._memory().list_session_chunks(owner_id=owner, **kwargs)

    def list_source_chunks(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """Compatibility alias; public bridge terminology is session chunks."""
        return self.list_session_chunks(**kwargs)

    def get_session_chunk(self, chunk_id: str, *, owner_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        cid = _clean(chunk_id)
        if not cid:
            raise ValueError("chunk_id is required")
        owner = _owner(owner_id)
        if not owner:
            raise ValueError("owner_id is required for session chunk reads")
        return self._memory().get_session_chunk(cid, owner_id=owner, **kwargs)

    def get_source_chunk(self, chunk_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Compatibility alias; public bridge terminology is session chunks."""
        return self.get_session_chunk(chunk_id, **kwargs)

    def expand_microchunk(self, microchunk_id: str, *, owner_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        mid = _clean(microchunk_id)
        if not mid:
            raise ValueError("microchunk_id is required")
        return self._datastore_bridge.call(
            "sessiondb",
            "expand_microchunk",
            microchunk_id=mid,
            owner_id=_owner(owner_id),
            **kwargs,
        )

    def list_session_transcripts(self, *, owner_id: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
        return self._list_session_logs(limit=int(limit or 5), owner_id=_optional(owner_id))

    def load_session_transcript(self, *, session_id: str, owner_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self._load_session_log(session_id=_session_id(session_id), owner_id=_optional(owner_id))


_SESSION_MEMORY_BRIDGE: SessionMemoryBridgePort = DatastoreSessionMemoryBridge()


def get_session_memory_bridge() -> SessionMemoryBridgePort:
    return _SESSION_MEMORY_BRIDGE
