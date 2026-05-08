import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeMemoryService:
    def __init__(self):
        self.store_calls = []
        self.rows = []

    def store_session_chunks(self, *, chunks, owner_id, **kwargs):
        self.store_calls.append({"chunks": list(chunks), "owner_id": owner_id, **kwargs})
        out = []
        start = int(kwargs.get("start_index", 0) or 0)
        session_id = kwargs.get("session_id")
        for offset, text in enumerate(chunks):
            row = {
                "chunk_id": f"sch-{owner_id}-{session_id}-{start + offset}",
                "text": text,
                "owner_id": owner_id,
                "session_id": session_id,
                "source_id": kwargs.get("source_id"),
                "chunk_index": start + offset,
                "source_channel": kwargs.get("source_channel"),
                "source_conversation_id": kwargs.get("source_conversation_id"),
                "conversation_id": kwargs.get("conversation_id"),
                "source_author_id": kwargs.get("source_author_id"),
                "status": "created",
            }
            out.append(row)
            self.rows.append(row)
        return out

    def list_session_chunks(self, *, owner_id, **kwargs):
        session_id = kwargs.get("session_id")
        return [
            row for row in self.rows
            if row["owner_id"] == owner_id and (not session_id or row["session_id"] == session_id)
        ]

    def get_session_chunk(self, chunk_id, *, owner_id, **_kwargs):
        for row in self.rows:
            if row["chunk_id"] == chunk_id and row["owner_id"] == owner_id:
                return row
        return None


def test_bridge_normalizes_session_chunk_metadata():
    from core.services.session_memory_bridge import DatastoreSessionMemoryBridge

    memory = FakeMemoryService()
    bridge = DatastoreSessionMemoryBridge(memory_service=memory)

    rows = bridge.store_session_chunks(
        chunks=["User: alpha", "Assistant: beta"],
        owner_id=" owner-1 ",
        session_id=" session-1 ",
        source_channel=" codex ",
        source_conversation_id=" conv-1 ",
        conversation_id="",
        source_author_id=" user-1 ",
        start_index=3,
    )

    assert [row["chunk_index"] for row in rows] == [3, 4]
    call = memory.store_calls[0]
    assert call["owner_id"] == "owner-1"
    assert call["session_id"] == "session-1"
    assert call["source_id"] == "session-1"
    assert call["source_channel"] == "codex"
    assert call["source_conversation_id"] == "conv-1"
    assert call["conversation_id"] == "conv-1"
    assert call["source_author_id"] == "user-1"

    assert bridge.list_session_chunks(owner_id="owner-1", session_id="session-1") == rows
    assert bridge.get_session_chunk(rows[0]["chunk_id"], owner_id="owner-2") is None


def test_bridge_indexes_session_transcript_through_core_contract():
    from core.services.session_memory_bridge import DatastoreSessionMemoryBridge

    captured = {}

    def fake_indexer(**kwargs):
        captured.update(kwargs)
        return {"status": "indexed", "session_id": kwargs["session_id"]}

    bridge = DatastoreSessionMemoryBridge(memory_service=FakeMemoryService(), session_log_indexer=fake_indexer)

    result = bridge.store_session_transcript(
        session_id=" sess-2 ",
        transcript="\nUser: alpha\n",
        owner_id=" owner-2 ",
        label=" cdx ",
        source_path=" /tmp/session.jsonl ",
        source_channel=" matrix ",
        conversation_id=" room-1 ",
        participant_ids=[" user-a ", ""],
        participant_aliases={" A ": " user-a ", "empty": ""},
        message_count=2,
        topic_hint=" topic ",
    )

    assert result == {"status": "indexed", "session_id": "sess-2"}
    assert captured["session_id"] == "sess-2"
    assert captured["transcript"] == "User: alpha"
    assert captured["owner_id"] == "owner-2"
    assert captured["source_label"] == "cdx"
    assert captured["source_path"] == "/tmp/session.jsonl"
    assert captured["source_channel"] == "matrix"
    assert captured["conversation_id"] == "room-1"
    assert captured["participant_ids"] == ["user-a"]
    assert captured["participant_aliases"] == {"A": "user-a"}
    assert captured["message_count"] == 2
    assert captured["topic_hint"] == "topic"


def test_bridge_preserves_memorydb_chunk_idempotency_and_updates(tmp_path):
    from core.services.session_memory_bridge import DatastoreSessionMemoryBridge
    from datastore.memorydb.memory_graph import MemoryGraph

    graph = MemoryGraph(db_path=tmp_path / "memory.db")
    bridge = DatastoreSessionMemoryBridge(memory_service=graph)

    first = bridge.store_session_chunks(
        chunks=["User: first", "Assistant: second"],
        owner_id="owner-3",
        session_id="sess-3",
        source_channel="codex",
        source_conversation_id="conv-3",
        start_index=0,
        embed=False,
        created_at="2026-05-08T00:00:00+00:00",
    )
    second = bridge.store_session_chunks(
        chunks=["User: first", "Assistant: second"],
        owner_id="owner-3",
        session_id="sess-3",
        source_channel="codex",
        source_conversation_id="conv-3",
        start_index=0,
        embed=False,
        created_at="2026-05-08T00:00:00+00:00",
    )
    changed = bridge.store_session_chunks(
        chunks=["User: changed first"],
        owner_id="owner-3",
        session_id="sess-3",
        start_index=0,
        embed=False,
        created_at="2026-05-08T00:00:01+00:00",
    )

    assert [row["chunk_id"] for row in second] == [row["chunk_id"] for row in first]
    assert changed[0]["chunk_id"] != first[0]["chunk_id"]

    rows = bridge.list_session_chunks(owner_id="owner-3", session_id="sess-3")
    assert len(rows) == 3
    assert rows[0]["created_at"] == "2026-05-08T00:00:00+00:00"
    assert rows[0]["source_channel"] == "codex"
    assert rows[0]["source_conversation_id"] == "conv-3"
    assert bridge.list_session_chunks(owner_id="other-owner", session_id="sess-3") == []


def test_session_logs_ingest_routes_indexing_through_core_bridge(monkeypatch, tmp_path):
    import ingest.session_logs_ingest as session_logs_ingest

    transcript_path = tmp_path / "session.txt"
    transcript_path.write_text("User: bridge canary\nAssistant: ack\n", encoding="utf-8")
    fake_bridge = MagicMock()
    fake_bridge.store_session_transcript.return_value = {"status": "indexed", "session_id": "sess-4"}
    monkeypatch.setattr(session_logs_ingest, "get_session_memory_bridge", lambda: fake_bridge)

    result = session_logs_ingest.run(
        session_id="sess-4",
        owner_id="owner-4",
        label="daemon-session_end",
        transcript_path=str(transcript_path),
        source_channel="openclaw",
        conversation_id="room-4",
        participant_ids=["u1"],
        participant_aliases={"User": "u1"},
        message_count=2,
        topic_hint="bridge canary",
    )

    assert result["status"] == "indexed"
    assert result["source_kind"] == "transcript_path"
    fake_bridge.store_session_transcript.assert_called_once()
    kwargs = fake_bridge.store_session_transcript.call_args.kwargs
    assert kwargs["session_id"] == "sess-4"
    assert kwargs["owner_id"] == "owner-4"
    assert kwargs["label"] == "daemon-session_end"
    assert "bridge canary" in kwargs["transcript"]
    assert kwargs["source_channel"] == "openclaw"
    assert kwargs["conversation_id"] == "room-4"


def test_bridge_storage_failure_raises_to_failhard_callers():
    from core.services.session_memory_bridge import DatastoreSessionMemoryBridge

    class FailingMemoryService:
        def store_session_chunks(self, **_kwargs):
            raise RuntimeError("storage down")

    bridge = DatastoreSessionMemoryBridge(memory_service=FailingMemoryService())

    with pytest.raises(RuntimeError, match="storage down"):
        bridge.store_session_chunks(
            chunks=["User: failhard canary"],
            owner_id="owner-5",
            session_id="sess-5",
        )
