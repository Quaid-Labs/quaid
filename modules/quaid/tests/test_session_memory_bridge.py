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
        pair_ids = list(kwargs.get("message_pair_ids") or [])
        microchunk_ids = list(kwargs.get("microchunk_ids") or [])
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
                "message_pair_id": pair_ids[offset] if offset < len(pair_ids) else kwargs.get("message_pair_id"),
                "microchunk_id": microchunk_ids[offset] if offset < len(microchunk_ids) else kwargs.get("microchunk_id"),
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

    def get_session_chunk(self, chunk_id, *, owner_id, **kwargs):
        for row in self.rows:
            if row["chunk_id"] == chunk_id and row["owner_id"] == owner_id:
                return row
        if kwargs.get("fail_hard") and any(row["chunk_id"] == chunk_id for row in self.rows):
            raise RuntimeError("Session chunk owner mismatch")
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
    with pytest.raises(RuntimeError, match="owner mismatch"):
        bridge.get_session_chunk(rows[0]["chunk_id"], owner_id="owner-2", fail_hard=True)


def test_datastore_bridge_registers_and_dispatches_callbacks():
    from core.services.datastore_bridge import DatastoreBridge

    bridge = DatastoreBridge()
    bridge.register_store(
        name="sessiondb",
        provides=["microchunk_id"],
        consumes=["memory_chunk_id"],
        callbacks={"fetch": lambda microchunk_id: {"microchunk_id": microchunk_id}},
    )

    assert bridge.call("sessiondb", "fetch", microchunk_id="mic-1") == {"microchunk_id": "mic-1"}
    assert bridge.list_routes()["sessiondb"]["provides"] == ["microchunk_id"]
    with pytest.raises(RuntimeError, match="route is not registered"):
        bridge.assert_route("sessiondb", "missing")


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


def test_bridge_projects_sessiondb_microchunks_to_memorydb(monkeypatch, tmp_path):
    from core.services.datastore_bridge import DatastoreBridge
    from core.services.session_memory_bridge import DatastoreSessionMemoryBridge
    from datastore.memorydb.memory_graph import MemoryGraph
    from datastore.sessiondb import session_store

    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    memory = MemoryGraph(db_path=tmp_path / "memory.db")
    bridge = DatastoreSessionMemoryBridge(memory_service=memory, datastore_bridge=DatastoreBridge())

    rows = bridge.store_session_source_text(
        text=(
            "User: Ada stores the launch checklist in the red binder.\n"
            "Assistant: Noted.\n"
            "User: Berto keeps the rover manual in cabinet seven."
        ),
        owner_id="owner-bridge",
        session_id="sess-bridge",
        source_id="source-bridge",
        start_index=8,
        max_microchunk_tokens=16,
        embed=False,
    )

    assert rows
    assert all(row["microchunk_id"] for row in rows)
    assert all(row["message_pair_id"] for row in rows)
    stored_rows = memory.list_session_chunks(owner_id="owner-bridge", session_id="sess-bridge")
    assert {row["microchunk_id"] for row in stored_rows} == {row["microchunk_id"] for row in rows}
    first_micro = session_store.fetch_microchunk(
        microchunk_id=rows[0]["microchunk_id"],
        owner_id="owner-bridge",
    )
    assert first_micro["memory_chunk_id"] == rows[0]["chunk_id"]
    expanded = bridge.expand_microchunk(rows[0]["microchunk_id"], owner_id="owner-bridge", after=1)
    assert expanded["pair"]["pair_id"] == rows[0]["message_pair_id"]
    assert expanded["window"]


def test_bridge_projects_indexed_session_transcript_microchunks(monkeypatch, tmp_path):
    from core.services.datastore_bridge import DatastoreBridge
    from core.services.session_memory_bridge import DatastoreSessionMemoryBridge
    from datastore.memorydb.memory_graph import MemoryGraph
    from datastore.sessiondb import session_store

    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    memory = MemoryGraph(db_path=tmp_path / "memory.db")
    bridge = DatastoreSessionMemoryBridge(memory_service=memory, datastore_bridge=DatastoreBridge())

    result = bridge.store_session_transcript(
        session_id="sess-transcript",
        transcript=(
            "User: Mira left the kiln key inside the green ledger.\n"
            "Assistant: I will remember that.\n"
            "User: Tomas keeps the clay receipt in drawer two."
        ),
        owner_id="owner-transcript",
        label="daemon-session_end",
        source_path="/tmp/sess-transcript.jsonl",
        source_channel="openclaw",
        conversation_id="room-transcript",
        message_count=4,
    )

    assert result["status"] == "indexed"
    assert result["microchunks_stored"] > 0
    rows = memory.list_session_chunks(owner_id="owner-transcript", session_id="sess-transcript")
    assert rows
    assert all(row["microchunk_id"] for row in rows)
    assert all(row["message_pair_id"] for row in rows)
    assert {row["source_channel"] for row in rows} == {"openclaw"}
    first_micro = session_store.fetch_microchunk(
        microchunk_id=rows[0]["microchunk_id"],
        owner_id="owner-transcript",
    )
    assert first_micro["memory_chunk_id"] == rows[0]["chunk_id"]


def test_sessiondb_appends_pair_chain_across_extraction_boundaries(monkeypatch, tmp_path):
    from datastore.sessiondb import session_store

    monkeypatch.setenv("SESSION_DB_PATH", str(tmp_path / "session.db"))
    first = session_store.store_session_source_text(
        text="User: First fact is in the red binder.\nAssistant: acknowledged.",
        owner_id="owner-chain",
        session_id="sess-chain",
        source_id="source-a",
        chunk_index=0,
        max_microchunk_tokens=12,
    )
    second = session_store.store_session_source_text(
        text="User: Second fact is in cabinet seven.\nAssistant: acknowledged.",
        owner_id="owner-chain",
        session_id="sess-chain",
        source_id="source-b",
        chunk_index=1,
        max_microchunk_tokens=12,
    )

    first_pair = first["pairs"][0]
    second_pair = second["pairs"][0]
    reloaded_first = session_store.fetch_pair(pair_id=first_pair["pair_id"], owner_id="owner-chain")
    assert reloaded_first["next_pair_id"] == second_pair["pair_id"]
    assert second_pair["next_pair_id"] is None
    assert first["microchunks"]
    assert all(row["pair_id"] == first_pair["pair_id"] for row in first["microchunks"])


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
