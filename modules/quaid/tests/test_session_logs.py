import hashlib
import time
from concurrent.futures import ThreadPoolExecutor

from datastore.memorydb import session_logs
from lib.adapter import TestAdapter, reset_adapter, set_adapter


def _fake_embedding(text: str):
    h = hashlib.md5(text.encode("utf-8")).digest()
    return [float(b) / 255.0 for b in h] * 8


def setup_function():
    reset_adapter()


def teardown_function():
    reset_adapter()


def test_session_log_index_list_load_search(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr("datastore.memorydb.session_logs._lib_get_embedding", _fake_embedding)

    transcript = (
        "User: My mother's name is Wendy.\n\n"
        "Assistant: Got it.\n\n"
        "User: My father's name is Kent.\n\n"
        "Assistant: Noted."
    )

    out = session_logs.index_session_log(
        session_id="sess-a1",
        transcript=transcript,
        owner_id="quaid",
        source_label="Compaction",
        source_channel="telegram",
        conversation_id="chat-123",
        participant_ids=["quaid", "user:solomon"],
        participant_aliases={"FatMan26": "user:solomon"},
        message_count=4,
    )
    assert out["status"] == "indexed"
    assert out["chunks"] >= 1

    recent = session_logs.list_recent_sessions(limit=5, owner_id="quaid")
    assert len(recent) == 1
    assert recent[0]["session_id"] == "sess-a1"
    assert recent[0]["source_channel"] == "telegram"
    assert recent[0]["conversation_id"] == "chat-123"

    loaded = session_logs.load_session("sess-a1", owner_id="quaid")
    assert loaded is not None
    assert "Wendy" in loaded["transcript_text"]
    assert loaded["source_channel"] == "telegram"
    assert loaded["conversation_id"] == "chat-123"

    hits = session_logs.search_session_logs("mother Wendy", owner_id="quaid", limit=5, min_similarity=0.05)
    assert len(hits) >= 1
    assert any(h["session_id"] == "sess-a1" for h in hits)


def test_session_log_last_session_excludes_current(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr("datastore.memorydb.session_logs._lib_get_embedding", _fake_embedding)

    session_logs.index_session_log(
        session_id="sess-old",
        transcript="User: old fact\n\nAssistant: ack",
        owner_id="quaid",
        source_label="Reset",
        message_count=2,
    )
    session_logs.index_session_log(
        session_id="sess-new",
        transcript="User: new fact\n\nAssistant: ack",
        owner_id="quaid",
        source_label="Compaction",
        message_count=2,
    )

    last = session_logs.load_last_session(owner_id="quaid", exclude_session_id="sess-new")
    assert last is not None
    assert last["session_id"] == "sess-old"


def test_session_log_index_serializes_same_session(monkeypatch, tmp_path):
    adapter = TestAdapter(tmp_path); set_adapter(adapter)
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))

    def _slow_embedding(text: str):
        time.sleep(0.12)
        return _fake_embedding(text)

    monkeypatch.setattr("datastore.memorydb.session_logs._lib_get_embedding", _slow_embedding)

    t1 = "User: alpha fact\n\nAssistant: noted."
    t2 = "User: beta fact\n\nAssistant: noted."

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(session_logs.index_session_log, session_id="sess-lock", transcript=t1, owner_id="quaid")
        f2 = pool.submit(session_logs.index_session_log, session_id="sess-lock", transcript=t2, owner_id="quaid")
        r1 = f1.result()
        r2 = f2.result()
    elapsed = time.perf_counter() - started

    assert r1["status"] in {"indexed", "unchanged"}
    assert r2["status"] in {"indexed", "unchanged"}
    assert elapsed >= 0.22

    loaded = session_logs.load_session("sess-lock", owner_id="quaid")
    assert loaded is not None
    transcript_text = loaded["transcript_text"]
    content_hash = hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()

    with session_logs._lib_get_connection() as conn:
        row = conn.execute(
            "SELECT content_hash FROM session_logs WHERE session_id = ?",
            ("sess-lock",),
        ).fetchone()
        assert row is not None
        assert str(row["content_hash"]) == content_hash

        chunks = conn.execute(
            "SELECT content FROM session_log_chunks WHERE session_id = ? ORDER BY chunk_index ASC",
            ("sess-lock",),
        ).fetchall()
    assert len(chunks) >= 1
    for chunk in chunks:
        assert str(chunk["content"]) in transcript_text
